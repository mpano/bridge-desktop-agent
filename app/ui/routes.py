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

    @app.get("/ui/style.css", include_in_schema=False)
    async def stylesheet():
        return FileResponse(STATIC / "style.css", media_type="text/css")
