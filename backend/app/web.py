"""One-container entry point for a hosted instance (Phase 15).

    uvicorn app.web:site

Serves the built frontend and the API from one origin, so the browser needs no CORS
and the frontend's default `/api` base URL works unchanged: the API (`app.main`) is
mounted under `/api`, the same prefix the Vite dev proxy strips. Any other path that
isn't a built file gets `index.html`, so client-side routes survive a page reload.

`FRONTEND_DIST` points at the built frontend (default `frontend/dist` next to
`backend/`). If it doesn't exist the site is the API alone, under `/api`.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

from app.core.config import get_settings
from app.main import app as api

DEFAULT_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class HashedAssets(StaticFiles):
    """The build names each file after its content (`index-DsHF6Uvp.js`), so a file never
    changes under its name and browsers may keep it for good."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """A mounted app's own lifespan doesn't run, so run the API's from here."""
    async with api.router.lifespan_context(api):
        yield


def create_site(dist: Path | None = None) -> FastAPI:
    dist = dist or Path(os.environ.get("FRONTEND_DIST", DEFAULT_DIST))
    site = FastAPI(
        title="ACME Salary Management", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    @site.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if get_settings().compress_responses:
        site.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    site.mount("/api", api)

    index = dist / "index.html"
    if index.is_file():
        site.mount("/assets", HashedAssets(directory=dist / "assets"), name="assets")

        @site.get("/{path:path}", include_in_schema=False)
        def page(path: str) -> Response:
            file = (dist / path).resolve()
            # A real built file (favicon, ...) is served as is; everything else is a
            # client-side route. Never leave the dist directory.
            if path and file.is_file() and dist.resolve() in file.parents:
                return FileResponse(file)
            return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return site


site = create_site()
