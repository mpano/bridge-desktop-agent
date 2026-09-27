import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.schemas import AgentResponse, ConfirmationRequest, MessageRequest
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.diagnostics import collect_diagnostics
from app.preferences.service import ApplicationPreferencesInput
from app.tools.files.projects import ProjectAlias, RememberProject
from app.workflows.retention import RetentionPolicy, WorkflowFilter, WorkflowQuery


def create_app(settings: Settings | None = None, agent=None, *, enable_ui: bool = False) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.agent = agent or build_agent(settings)
        try:
            yield
        finally:
            await app.state.agent.close()

    app = FastAPI(title="Desktop Agent", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            )
        return response

    async def authorize(request: Request):
        expected = settings.api_token.get_secret_value()
        if not expected:
            raise HTTPException(503, "Configure API_TOKEN before using the API.")
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(401, "Invalid bearer token.")
        origin = request.headers.get("origin")
        local_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin and (not enable_ui or origin != local_origin):
            raise HTTPException(403, "Browser origin is not allowed.")
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            raise HTTPException(403, "Cross-origin browser requests are disabled.")

    if enable_ui:
        from app.ui.routes import install_ui

        install_ui(app)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/capabilities", dependencies=[Depends(authorize)])
    async def capabilities(request: Request):
        return {"tools": request.app.state.agent.capabilities()}

    @app.get("/api/v1/diagnostics", dependencies=[Depends(authorize)])
    async def diagnostics():
        return collect_diagnostics(settings)

    @app.post(
        "/api/v1/agent/message", response_model=AgentResponse, dependencies=[Depends(authorize)]
    )
    async def message(payload: MessageRequest, request: Request):
        return await request.app.state.agent.message(payload.message)

    @app.post(
        "/api/v1/agent/confirm", response_model=AgentResponse, dependencies=[Depends(authorize)]
    )
    async def confirm(payload: ConfirmationRequest, request: Request):
        try:
            return await request.app.state.agent.confirm(payload.token, payload.approved)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/tasks", status_code=202, dependencies=[Depends(authorize)])
    async def submit_task(payload: MessageRequest, request: Request):
        try:
            return request.app.state.agent.submit(payload.message)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/tasks", dependencies=[Depends(authorize)])
    async def list_tasks(request: Request):
        return {"tasks": request.app.state.agent.list_tasks()}

    @app.post("/api/v1/tasks/confirm", status_code=202, dependencies=[Depends(authorize)])
    async def confirm_task(payload: ConfirmationRequest, request: Request):
        try:
            return request.app.state.agent.submit_confirmation(payload.token, payload.approved)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/tasks/{request_id}", dependencies=[Depends(authorize)])
    async def task_progress(request_id: str, request: Request):
        try:
            return request.app.state.agent.task_progress(request_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/v1/tasks/{request_id}/cancel", dependencies=[Depends(authorize)])
    async def stop_task(request_id: str, request: Request):
        try:
            return request.app.state.agent.stop_task(request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/workflows", dependencies=[Depends(authorize)])
    async def workflows(
        request: Request,
        status: WorkflowFilter | None = None,
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        return request.app.state.agent.workflow_page(
            WorkflowQuery(status=status, offset=offset, limit=limit)
        )

    @app.post(
        "/api/v1/workflows/cleanup/preview",
        response_model=AgentResponse,
        dependencies=[Depends(authorize)],
    )
    async def preview_cleanup(payload: RetentionPolicy, request: Request):
        return await request.app.state.agent.preview_workflow_cleanup(payload)

    @app.get("/api/v1/workflows/{request_id}", dependencies=[Depends(authorize)])
    async def workflow(request_id: str, request: Request):
        try:
            return request.app.state.agent.get_workflow(request_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post(
        "/api/v1/workflows/{request_id}/resume",
        response_model=AgentResponse,
        dependencies=[Depends(authorize)],
    )
    async def resume(request_id: str, request: Request):
        try:
            return await request.app.state.agent.resume_workflow(request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(
        "/api/v1/workflows/{request_id}/cancel",
        response_model=AgentResponse,
        dependencies=[Depends(authorize)],
    )
    async def cancel(request_id: str, request: Request):
        try:
            return await request.app.state.agent.cancel_workflow(request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/conversation/reset", dependencies=[Depends(authorize)])
    async def reset(request: Request):
        async with request.app.state.agent.lock:
            request.app.state.agent.reset_conversation()
        return {"status": "completed"}

    @app.get("/api/v1/projects", dependencies=[Depends(authorize)])
    async def projects(request: Request):
        return {"projects": request.app.state.agent.list_projects()}

    @app.post("/api/v1/projects", response_model=AgentResponse, dependencies=[Depends(authorize)])
    async def save_project(payload: RememberProject, request: Request):
        try:
            return await request.app.state.agent.request_tool(
                "remember_project", payload.model_dump()
            )
        except ValueError:
            raise HTTPException(400, "Project arguments are invalid or blocked.") from None

    @app.post(
        "/api/v1/projects/forget", response_model=AgentResponse, dependencies=[Depends(authorize)]
    )
    async def forget_project(payload: ProjectAlias, request: Request):
        return await request.app.state.agent.request_tool("forget_project", payload.model_dump())

    @app.get("/api/v1/preferences", dependencies=[Depends(authorize)])
    async def preferences(request: Request):
        return request.app.state.agent.get_preferences()

    @app.post(
        "/api/v1/preferences", response_model=AgentResponse, dependencies=[Depends(authorize)]
    )
    async def save_preferences(payload: ApplicationPreferencesInput, request: Request):
        return await request.app.state.agent.request_tool("set_preferences", payload.model_dump())

    return app
