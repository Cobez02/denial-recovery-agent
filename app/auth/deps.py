"""Request-scoped dependencies: the current user, the login requirement and the CSRF/origin check.

Sessions are signed cookies (SessionMiddleware in app.main). A session counts as logged in only
after both factors: ``user_id`` is set by the password step and ``mfa`` by the TOTP step. When
``AUTH_REQUIRED=false`` (sandbox runs) every route sees a synthetic local admin instead.

State-changing routes depend on ``csrf_protect``: the Origin/Referer header, when present, must
name this host, and the request must carry either the ``HX-Request`` header (HTMX) or the
per-session ``_csrf`` form token. Login POSTs are exempt from the token (there is no session yet)
but still origin-checked through ``check_origin``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import User

SESSION_USER_KEY = "user_id"
SESSION_MFA_KEY = "mfa"
SESSION_PENDING_KEY = "pending_user_id"
SESSION_CSRF_KEY = "csrf"
CSRF_FIELD = "_csrf"
HX_HEADER = "HX-Request"
LOGIN_PATH = "/login"
API_PREFIX = "/api"


@dataclass(frozen=True)
class LocalUser:
    """Stands in for a logged-in user when AUTH_REQUIRED=false. Its audit actor id is ``user:local``."""

    id: None = None
    email: str = "local@localhost"
    role: str = "admin"
    is_active: bool = True


CurrentUser = User | LocalUser


def get_current_user(request: Request, session: Session) -> User | None:
    """The active user behind this session, only once both factors passed."""
    if not request.session.get(SESSION_MFA_KEY):
        return None
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    try:
        user = session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    if user is None or not user.is_active:
        return None
    return user


def is_api_path(request: Request) -> bool:
    return request.url.path.startswith(API_PREFIX)


def require_user(request: Request, session: Session = Depends(get_session)) -> CurrentUser:
    """Dependency: a logged-in user, a redirect to /login for pages, or 401 JSON under /api."""
    if not get_settings().auth_required:
        user: CurrentUser = LocalUser()
        request.state.user = user
        return user
    found = get_current_user(request, session)
    if found is None:
        if is_api_path(request):
            raise HTTPException(status_code=401, detail="login required")
        headers = {"Location": LOGIN_PATH}
        if request.headers.get(HX_HEADER):
            headers["HX-Redirect"] = LOGIN_PATH
        raise HTTPException(status_code=303, detail="login required", headers=headers)
    request.state.user = found
    return found


def csrf_token(request: Request) -> str:
    """The per-session token every form carries as the hidden ``_csrf`` field (created on first use)."""
    token = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)
    return request.session[SESSION_CSRF_KEY]


def _same_host(header_value: str | None, request: Request) -> bool:
    if not header_value:
        return True
    parsed = urlsplit(header_value.strip())
    host = parsed.netloc or parsed.path  # "null" and bare values never match
    return host.lower() == request.url.netloc.lower()


def check_origin(request: Request) -> None:
    """Refuse a request whose Origin or Referer header names another host (403)."""
    for header in ("origin", "referer"):
        if not _same_host(request.headers.get(header), request):
            raise HTTPException(status_code=403, detail="cross-origin request refused")


async def csrf_protect(request: Request) -> None:
    """Dependency for every state-changing route: same origin plus HX-Request or the session token."""
    check_origin(request)
    if request.headers.get(HX_HEADER):
        return
    expected = request.session.get(SESSION_CSRF_KEY)
    form = await request.form()
    supplied = form.get(CSRF_FIELD)
    if (
        not isinstance(expected, str)
        or not expected
        or not isinstance(supplied, str)
        or not secrets.compare_digest(supplied, expected)
    ):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")


__all__ = [
    "API_PREFIX",
    "CSRF_FIELD",
    "HX_HEADER",
    "LOGIN_PATH",
    "SESSION_CSRF_KEY",
    "SESSION_MFA_KEY",
    "SESSION_PENDING_KEY",
    "SESSION_USER_KEY",
    "CurrentUser",
    "LocalUser",
    "check_origin",
    "csrf_protect",
    "csrf_token",
    "get_current_user",
    "is_api_path",
    "require_user",
    "rotate_csrf_token",
]
