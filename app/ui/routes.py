from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"


def install_ui(app: FastAPI) -> None:
    """Serve only packaged assets, never arbitrary filesystem paths."""

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/ui/app.js", include_in_schema=False)
    async def javascript():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/ui/today.js", include_in_schema=False)
    async def today_script():
        return FileResponse(STATIC / "today.js", media_type="text/javascript")

    @app.get("/ui/inbox.js", include_in_schema=False)
    async def inbox_script():
        return FileResponse(STATIC / "inbox.js", media_type="text/javascript")

    @app.get("/ui/automations.js", include_in_schema=False)
    async def automations_script():
        return FileResponse(STATIC / "automations.js", media_type="text/javascript")

    @app.get("/ui/settings.js", include_in_schema=False)
    async def settings_script():
        return FileResponse(STATIC / "settings.js", media_type="text/javascript")

    @app.get("/ui/onboarding.js", include_in_schema=False)
    async def onboarding_script():
        return FileResponse(STATIC / "onboarding.js", media_type="text/javascript")

    @app.get("/ui/mini.js", include_in_schema=False)
    async def mini_js():
        return FileResponse(STATIC / "mini.js", media_type="text/javascript")

    @app.get("/ui/panel.js", include_in_schema=False)
    async def panel_js():
        return FileResponse(STATIC / "panel.js", media_type="text/javascript")

    @app.get("/ui/command.js", include_in_schema=False)
    async def command_js():
        return FileResponse(STATIC / "command.js", media_type="text/javascript")

    @app.get("/ui/mini.css", include_in_schema=False)
    async def mini_css():
        return FileResponse(STATIC / "mini.css", media_type="text/css")

    @app.get("/ui/panel.html", include_in_schema=False)
    async def panel_html():
        return FileResponse(STATIC / "panel.html", media_type="text/html")

    @app.get("/ui/command.html", include_in_schema=False)
    async def command_html():
        return FileResponse(STATIC / "command.html", media_type="text/html")

    @app.get("/ui/style.css", include_in_schema=False)
    async def stylesheet():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get("/ui/bridge-logo.png", include_in_schema=False)
    async def bridge_logo():
        return FileResponse(STATIC / "bridge-logo.png", media_type="image/png")

    @app.get("/ui/bridge-mark.png", include_in_schema=False)
    async def bridge_mark():
        return FileResponse(STATIC / "bridge-mark.png", media_type="image/png")

    # Bridge on your phone: the Home Screen app's script, icons, manifest and service worker.
    @app.get("/ui/work.js", include_in_schema=False)
    async def work_js():
        return FileResponse(STATIC / "work.js", media_type="text/javascript")

    @app.get("/ui/brief.js", include_in_schema=False)
    async def brief_js():
        return FileResponse(STATIC / "brief.js", media_type="text/javascript")

    @app.get("/ui/promises.js", include_in_schema=False)
    async def promises_js():
        return FileResponse(STATIC / "promises.js", media_type="text/javascript")

    @app.get("/ui/phone.js", include_in_schema=False)
    async def phone_js():
        return FileResponse(STATIC / "phone.js", media_type="text/javascript")

    def icon(name: str):
        async def serve():
            return FileResponse(STATIC / name, media_type="image/png")

        return serve

    for size in (180, 192, 512):
        app.add_api_route(f"/ui/icon-{size}.png", icon(f"icon-{size}.png"), include_in_schema=False)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        # At the root, so it covers the whole app.
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")
