"""FastAPI application factory.

Routers are registered from the review, auth and api packages. Each is imported defensively so
the application still starts while a package is under construction; a missing router is logged.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings, is_placeholder_secret
from app.logging_config import configure_logging

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"

# (module path, attribute name). Order matters: auth first so its middleware/dependencies exist.
ROUTER_MODULES: tuple[tuple[str, str], ...] = (
    ("app.auth.routes", "router"),
    ("app.api.routes", "router"),
    ("app.review.routes", "router"),
)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.app_env != "test" and is_placeholder_secret(settings.secret_key):
        raise RuntimeError(
            "SECRET_KEY is missing or a placeholder; session cookies would be forgeable. Put a random value "
            'in .env (python -c "import secrets; print(secrets.token_urlsafe(48))") and start again.'
        )

    app = FastAPI(title="Denial Recovery Agent", version="0.1.0", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="denial_session",
        max_age=settings.session_max_age_seconds,
        same_site="strict",
        https_only=settings.session_cookie_secure,
    )
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for module_path, attr in ROUTER_MODULES:
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:  # package not built yet
            if exc.name and module_path.startswith(exc.name):
                log.warning("router %s not available yet: %s", module_path, exc)
                continue
            raise
        router = getattr(module, attr, None)
        if router is None:
            log.warning("router %s has no attribute %s", module_path, attr)
            continue
        app.include_router(router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        # Never echo request content back; log the type and path only.
        log.exception("unhandled error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
        return JSONResponse({"error": "internal error"}, status_code=500)

    return app


app = create_app()
