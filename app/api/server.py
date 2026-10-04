import asyncio
import inspect
import secrets
import time
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.app_settings import install as install_app_settings
from app.api.automations import install as install_automations
from app.api.inbox import install as install_inbox
from app.api.launch import LaunchTickets
from app.api.onboarding import install as install_onboarding
from app.api.phone import install as install_phone
from app.api.schemas import (
    AgentResponse,
    ChatDelete,
    ChatRef,
    ConfirmationRequest,
    ForgetRequest,
    LaunchRequest,
    MemoryRequest,
    MessageRequest,
)
from app.api.today import install as install_today
from app.bootstrap import build_agent
from app.config.settings import Settings
from app.diagnostics import collect_diagnostics
from app.integrations.routes import install_connections
from app.phone.access import PhoneAccess, phone_may_use
from app.preferences.service import ApplicationPreferencesInput
from app.text_actions import (
    DictationRequest,
    TextActionRequest,
    clean_dictation,
    run_text_action,
)
from app.tools.files.projects import ProjectAlias, RememberProject
from app.tools.system.proactive import SettingsInput as ProactiveSettings
from app.tools.system.proactive import WatchInput as WatchRequest
from app.workflows.retention import RetentionPolicy, WorkflowFilter, WorkflowQuery

LOCAL_HOSTS = {"localhost", "127.0.0.1", "testserver"}


def create_app(
    settings: Settings | None = None,
    agent=None,
    *,
    enable_ui: bool = False,
    launch_tickets: LaunchTickets | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.agent = agent or build_agent(settings)
        app.state.usage = {}  # When each shortcut was last used, for onboarding.
        # Scheduled requests and proactive heads-ups run only while this service is up.
        loops = [
            asyncio.create_task(worker.run())
            for worker in (
                getattr(app.state.agent, "scheduler", None),
                getattr(app.state.agent, "proactive", None),
            )
            if worker is not None and inspect.iscoroutinefunction(getattr(worker, "run", None))
        ]
        try:
            yield
        finally:
            for task in loops:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await app.state.agent.close()

    app = FastAPI(title="Bridge", lifespan=lifespan)

    def phone_access(request: Request):
        agent_now = getattr(request.app.state, "agent", None) or agent
        access = getattr(agent_now, "phone", None)
        return access if isinstance(access, PhoneAccess) else None

    def from_phone(request: Request) -> bool:
        phone = phone_access(request)
        return bool(phone is not None and phone.is_phone(request))

    @app.middleware("http")
    async def phone_gate(request: Request, call_next):
        """Local names only, plus the phone address when it really came through Tailscale.

        From the phone, only the phone's screens are reachable."""
        host = (request.headers.get("host") or "").split(":")[0].lower()
        phone = phone_access(request)
        forwarded = any(h in request.headers for h in ("tailscale-user-login", "x-forwarded-host"))
        if host in LOCAL_HOSTS and forwarded:
            # Relayed from another device but addressed as this Mac: never treat it as local.
            return PlainTextResponse("Invalid host header", status_code=400)
        if host not in LOCAL_HOSTS:
            if phone is None or not phone.enabled or host != phone.host:
                return PlainTextResponse("Invalid host header", status_code=400)
            if not phone.is_phone(request):
                return JSONResponse({"detail": "Not allowed."}, status_code=403)
            if not phone_may_use(request.method, request.url.path):
                return JSONResponse(
                    {"detail": "Open Bridge on your Mac for this."}, status_code=403
                )
        return await call_next(request)

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
                "form-action 'none'; frame-ancestors 'none'; "
                "manifest-src 'self'; worker-src 'self'"
            )
        return response

    def same_origin(request: Request):
        origin = request.headers.get("origin")
        local_origin = (
            phone_access(request).origin
            if from_phone(request)
            else f"{request.url.scheme}://{request.url.netloc}"
        )
        if origin and (not enable_ui or origin != local_origin):
            raise HTTPException(403, "Browser origin is not allowed.")
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            raise HTTPException(403, "Cross-origin browser requests are disabled.")

    def api_token_valid(request: Request) -> bool:
        expected = settings.api_token.get_secret_value()
        supplied = request.headers.get("authorization", "")
        return bool(expected) and secrets.compare_digest(
            supplied.encode(), f"Bearer {expected}".encode()
        )

    auth = None
    if enable_ui:
        from app.auth.routes import install_auth
        from app.ui.routes import install_ui

        install_ui(app)
        auth = install_auth(app, settings, same_origin, api_token_valid, from_phone)

    async def authorize(request: Request):
        """The API token (menu bar, voice, scripts) or a signed-in dashboard session."""
        if api_token_valid(request) and not from_phone(request):
            same_origin(request)
            return
        if auth is not None and request.cookies.get("bridge_session"):
            if auth.owner_session(request) is not None:
                same_origin(request)
                # Cookies ride along automatically, so changes must come from the page itself.
                if request.method not in {"GET", "HEAD"} and not request.headers.get("origin"):
                    raise HTTPException(403, "Use the Bridge dashboard to do this.")
                return
            raise HTTPException(401, "Your session ended. Sign in again.")
        if not settings.api_token.get_secret_value():
            raise HTTPException(503, "Configure API_TOKEN before using the API.")
        raise HTTPException(401, "Invalid bearer token.")

    if auth is not None and launch_tickets is not None:

        @app.post("/api/v1/session/launch", include_in_schema=False)
        async def launch(payload: LaunchRequest, request: Request):
            """Opened from the Bridge menu bar: sign the owner in, or allow first-run setup."""
            same_origin(request)
            if request.headers.get("origin") is None:
                raise HTTPException(403, "Open the dashboard from the Bridge menu.")
            if not launch_tickets.redeem(payload.ticket):
                raise HTTPException(401, "This dashboard link expired. Open it again from Bridge.")
            has_owner = auth.store.owner() is not None
            response = JSONResponse({"signed_in": has_owner, "setup": not has_owner})
            return auth.sign_in(response, request, kind="owner" if has_owner else "setup")

    install_connections(app, authorize)
    install_today(app, authorize)
    install_inbox(app, authorize)
    install_automations(app, authorize)
    install_app_settings(app, authorize, settings)
    install_onboarding(app, authorize, settings)
    install_phone(app, authorize, settings, auth, from_phone)

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
        if request.headers.get("x-bridge-source") == "voice":
            request.app.state.usage["talk"] = time.time()
        try:
            return request.app.state.agent.submit(payload.message, from_phone=from_phone(request))
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

    @app.get("/api/v1/memories", dependencies=[Depends(authorize)])
    async def memories(request: Request):
        store = request.app.state.agent.memories
        return {"memories": [fact.__dict__ for fact in store.list()]}

    @app.post("/api/v1/memories", dependencies=[Depends(authorize)])
    async def remember(payload: MemoryRequest, request: Request):
        try:
            fact = request.app.state.agent.memories.add(payload.text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return fact.__dict__

    @app.post("/api/v1/memories/forget", dependencies=[Depends(authorize)])
    async def forget(payload: ForgetRequest, request: Request):
        if request.app.state.agent.memories.forget(payload.id) is None:
            raise HTTPException(404, "That memory was already forgotten.")
        return {"forgotten": payload.id}

    @app.get("/api/v1/proactive", dependencies=[Depends(authorize)])
    async def proactive(request: Request):
        data = await request.app.state.agent.proactive_controller.list(None)
        data["followups"] = [
            {"id": f.id, "name": f.name, "about": f.about, "due": f.due, "status": f.status}
            for f in request.app.state.agent.proactive_store.followups()
            if f.status in {"waiting", "overdue", "replied"}
        ][-20:]
        return data

    @app.post("/api/v1/followups/cancel", dependencies=[Depends(authorize)])
    async def cancel_followup(payload: ForgetRequest, request: Request):
        request.app.state.agent.proactive_store.set_followup(payload.id, "cancelled")
        return {"cancelled": payload.id}

    @app.post("/api/v1/proactive/settings", dependencies=[Depends(authorize)])
    async def proactive_settings(payload: ProactiveSettings, request: Request):
        try:
            return await request.app.state.agent.proactive_controller.configure(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/watches", dependencies=[Depends(authorize)])
    async def add_watch(payload: WatchRequest, request: Request):
        try:
            return await request.app.state.agent.proactive_controller.watch(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/v1/watches/remove", dependencies=[Depends(authorize)])
    async def remove_watch(payload: ForgetRequest, request: Request):
        if not request.app.state.agent.proactive_store.remove_watch(payload.id):
            raise HTTPException(404, "That watch was already removed.")
        return {"removed": payload.id}

    @app.post("/api/v1/text/transform", dependencies=[Depends(authorize)])
    async def transform_text(payload: TextActionRequest, request: Request):
        from app.llm.models import LLMProviderError

        try:
            request.app.state.usage["act"] = time.time()
            result = await run_text_action(request.app.state.agent.planner.llm, payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except (LLMProviderError, RuntimeError) as exc:
            raise HTTPException(503, str(exc)) from None
        except Exception:
            raise HTTPException(
                503, "OpenAI didn't answer. Check your connection and API key."
            ) from None
        return {"result": result}

    @app.post("/api/v1/text/dictation", dependencies=[Depends(authorize)])
    async def clean_up_dictation(payload: DictationRequest, request: Request):
        try:
            request.app.state.usage["dictate"] = time.time()
            result = await clean_dictation(request.app.state.agent.planner.llm, payload)
        except Exception:
            result = payload.text.strip()  # Typing the raw words beats losing them.
        return {"result": result}

    @app.get("/api/v1/focus", dependencies=[Depends(authorize)])
    async def focus_status(request: Request):
        focus = getattr(request.app.state.agent, "focus", None)
        return focus.status() if focus is not None else {"active": False}

    @app.post("/api/v1/focus/stop", dependencies=[Depends(authorize)])
    async def focus_stop(request: Request):
        focus = getattr(request.app.state.agent, "focus", None)
        try:
            return await focus.stop()
        except (ValueError, AttributeError):
            raise HTTPException(409, "You're not in focus mode.") from None

    @app.get("/api/v1/conversation", dependencies=[Depends(authorize)])
    async def conversation(request: Request):
        agent = request.app.state.agent
        return {"messages": agent.conversation(), "chat_id": agent.chat_id}

    @app.get("/api/v1/chats", dependencies=[Depends(authorize)])
    async def chats(request: Request):
        agent = request.app.state.agent
        store = agent.chats
        return {
            "chats": store.recent() if store is not None else [],
            "current": agent.chat_id,
            "days": store.days if store is not None else 0,
        }

    @app.post("/api/v1/chats/open", dependencies=[Depends(authorize)])
    async def open_chat(payload: ChatRef, request: Request):
        agent = request.app.state.agent
        async with agent.lock:
            if not agent.open_chat(payload.id):
                raise HTTPException(404, "That chat is gone — chats are kept for a few days.")
        return {"messages": agent.conversation(), "chat_id": agent.chat_id}

    @app.post("/api/v1/chats/delete", dependencies=[Depends(authorize)])
    async def delete_chats(payload: ChatDelete, request: Request):
        agent = request.app.state.agent
        async with agent.lock:
            removed = agent.delete_chats(None if payload.all else payload.id)
        return {"deleted": removed, "chat_id": agent.chat_id}

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
