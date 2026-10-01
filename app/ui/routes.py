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

    @app.get("/ui/style.css", include_in_schema=False)
    async def stylesheet():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get("/ui/bridge-logo.png", include_in_schema=False)
    async def bridge_logo():
        return FileResponse(STATIC / "bridge-logo.png", media_type="image/png")

    @app.get("/ui/bridge-mark.png", include_in_schema=False)
    async def bridge_mark():
        return FileResponse(STATIC / "bridge-mark.png", media_type="image/png")
