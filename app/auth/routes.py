"""Login (password, then TOTP) and logout pages.

The password step stores ``pending_user_id`` only; the session becomes a login once the TOTP code
verifies (``user_id`` + ``mfa``). Failed attempts render one generic message so the page never
says whether the email or the password was wrong. Lockout rules live in app.auth.service.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app import audit
from app.auth import service
from app.auth.deps import (
    SESSION_MFA_KEY,
    SESSION_PENDING_KEY,
    SESSION_USER_KEY,
    check_origin,
    csrf_protect,
    get_current_user,
    rotate_csrf_token,
)
from app.config import get_settings
from app.db import get_session
from app.enums import ActorType
from app.models import User
from app.review.filters import flash, render

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

GENERIC_LOGIN_ERROR = "That email and password combination was not accepted."
GENERIC_MFA_ERROR = "That code was not accepted. Enter the current code from your authenticator app."
QUEUE_PATH = "/queue"


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=303)


def _clear_login(request: Request) -> None:
    for key in (SESSION_USER_KEY, SESSION_MFA_KEY, SESSION_PENDING_KEY):
        request.session.pop(key, None)


@router.get("/login")
def login_form(request: Request, session: Session = Depends(get_session)) -> Response:
    if not get_settings().auth_required or get_current_user(request, session) is not None:
        return _redirect(QUEUE_PATH)
    return render(request, "pages/login.html", {"error": None})


@router.post("/login", dependencies=[Depends(check_origin)])
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    if not get_settings().auth_required:
        return _redirect(QUEUE_PATH)
    user = service.authenticate(session, email, password)
    session.commit()  # the failure counter and lockout must persist even when the login failed
    if user is None:
        return render(request, "pages/login.html", {"error": GENERIC_LOGIN_ERROR})
    _clear_login(request)
    request.session[SESSION_PENDING_KEY] = user.id
    return _redirect("/login/mfa")


@router.get("/login/mfa")
def mfa_form(request: Request, session: Session = Depends(get_session)) -> Response:
    if get_current_user(request, session) is not None:
        return _redirect(QUEUE_PATH)
    if not request.session.get(SESSION_PENDING_KEY):
        return _redirect("/login")
    return render(request, "pages/mfa.html", {"error": None})


@router.post("/login/mfa", dependencies=[Depends(check_origin)])
def mfa_submit(
    request: Request,
    code: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    pending = request.session.get(SESSION_PENDING_KEY)
    if not pending:
        return _redirect("/login")
    try:
        user = session.get(User, int(pending))
    except (TypeError, ValueError):
        user = None
    if user is None or not user.is_active or service.is_locked(user):
        _clear_login(request)
        flash(request, "Please log in again.", "error")
        return _redirect("/login")
    if not service.verify_totp(user, code):
        service.record_mfa_failure(session, user)
        session.commit()
        if service.is_locked(user):
            _clear_login(request)
            flash(request, "Too many failed attempts. The account is locked for a while.", "error")
            return _redirect("/login")
        return render(request, "pages/mfa.html", {"error": GENERIC_MFA_ERROR})
    service.complete_login(session, user)
    session.commit()
    request.session.clear()  # a fresh session for the authenticated user (no fixation)
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_MFA_KEY] = True
    rotate_csrf_token(request)
    return _redirect(QUEUE_PATH)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request, session: Session = Depends(get_session)) -> Response:
    user = get_current_user(request, session)
    if user is not None:
        audit.record(
            session,
            actor_type=ActorType.USER,
            actor_id=service.actor_for(user),
            action="logout",
            entity_type="user",
            entity_id=user.id,
        )
        session.commit()
    request.session.clear()
    if not get_settings().auth_required:
        flash(request, "Login is disabled in this environment (AUTH_REQUIRED=false).", "info")
        return _redirect(QUEUE_PATH)
    flash(request, "You have been logged out.", "info")
    return _redirect("/login")


__all__ = ["router"]
