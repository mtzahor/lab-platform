from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from lab_platform.control_plane.config import WebSettings
from starlette.exceptions import HTTPException

_PACKAGED_WEB_DIRECTORY = Path(__file__).with_name("web_dist")
_FINGERPRINTED_FILE = re.compile(r"(?:^|[._-])[A-Za-z0-9_-]{8,}(?=\.)")
_NEVER_SPA_PREFIXES = frozenset(
    {
        ".well-known",
        "api",
        "docs",
        "redoc",
    }
)


def create_web_router(
    settings: WebSettings,
    *,
    directory: Path | None = None,
) -> APIRouter:
    """Create the final, catch-all router for an integrated Vite build.

    Include this router after every API, documentation, and websocket route. An
    enabled dashboard intentionally fails during application construction when
    ``index.html`` is absent, rather than starting a server whose root always 404s.
    """

    router = APIRouter(tags=["web"])
    if not settings.enabled:
        return router

    web_root = ensure_web_bundle(directory)
    index_file = web_root / "index.html"
    csp = web_content_security_policy(settings)

    async def serve(request: Request, path: str) -> FileResponse:
        return _web_response(
            request,
            path,
            root=web_root,
            index_file=index_file,
            settings=settings,
            csp=csp,
        )

    async def serve_root(request: Request) -> FileResponse:
        return await serve(request, "")

    router.add_api_route(
        "/",
        serve_root,
        methods=["GET", "HEAD"],
        include_in_schema=False,
        response_class=FileResponse,
    )
    router.add_api_route(
        "/{path:path}",
        serve,
        methods=["GET", "HEAD"],
        include_in_schema=False,
        response_class=FileResponse,
    )
    return router


def ensure_web_bundle(directory: Path | None = None) -> Path:
    root = (directory or _PACKAGED_WEB_DIRECTORY).resolve()
    index_file = root / "index.html"
    if not index_file.is_file():
        raise RuntimeError(
            "The web dashboard is enabled but its packaged web_dist/index.html is missing. "
            "Build apps/web before starting the control plane."
        )
    return root


def web_content_security_policy(settings: WebSettings) -> str:
    """Return the document policy static responses publish before API middleware."""

    connect_sources = {"'self'"}
    parsed_api = urlsplit(settings.api_base_url)
    if parsed_api.scheme in {"http", "https"} and parsed_api.netloc:
        origin = f"{parsed_api.scheme}://{parsed_api.netloc}"
        connect_sources.add(origin)
        websocket_scheme = "wss" if parsed_api.scheme == "https" else "ws"
        connect_sources.add(f"{websocket_scheme}://{parsed_api.netloc}")
    return "; ".join(
        (
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            "manifest-src 'self'",
            "worker-src 'self'",
            "connect-src " + " ".join(sorted(connect_sources)),
        )
    )


def _web_response(
    request: Request,
    path: str,
    *,
    root: Path,
    index_file: Path,
    settings: WebSettings,
    csp: str,
) -> FileResponse:
    normalized = path.strip("/")
    if _excluded_from_spa(normalized, settings):
        raise HTTPException(status_code=404, detail="Resource not found")

    candidate = (root / normalized).resolve() if normalized else index_file
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=404, detail="Resource not found")
    if candidate.is_file() and candidate.suffix.casefold() != ".map":
        return _file_response(request, candidate, index_file=index_file, csp=csp)

    # A missing file-like URL is an asset miss, not a client-side route. This
    # prevents the HTML shell from being cached or interpreted as JS/CSS/images.
    if Path(normalized).suffix or normalized == "assets" or normalized.startswith("assets/"):
        raise HTTPException(status_code=404, detail="Resource not found")
    return _file_response(request, index_file, index_file=index_file, csp=csp)


def _file_response(
    request: Request,
    path: Path,
    *,
    index_file: Path,
    csp: str,
) -> FileResponse:
    is_html = path == index_file or path.suffix.casefold() == ".html"
    if is_html:
        cache_control = "no-cache"
    elif _FINGERPRINTED_FILE.search(path.name):
        cache_control = "public, max-age=31536000, immutable"
    else:
        cache_control = "no-cache"
    headers = {
        "Cache-Control": cache_control,
        "Content-Security-Policy": csp,
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
    return FileResponse(
        path,
        headers=headers,
        media_type="text/html" if is_html else None,
        filename=None,
    )


def _excluded_from_spa(path: str, settings: WebSettings) -> bool:
    if not path:
        return False
    parts = Path(path).parts
    if any(part.startswith(".") for part in parts):
        return True
    first = parts[0].casefold()
    if first in _NEVER_SPA_PREFIXES or first == "openapi.json":
        return True
    api_path = urlsplit(settings.api_base_url).path.strip("/")
    return bool(api_path and (path == api_path or path.startswith(api_path + "/")))


__all__ = [
    "create_web_router",
    "ensure_web_bundle",
    "web_content_security_policy",
]
