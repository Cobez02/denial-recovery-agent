"""User accounts, argon2 password hashing, TOTP MFA and the login lockout rules (SPEC section 15).

Nothing here commits: the caller owns the transaction (the login route commits even after a
failed attempt so the failure counter and the lockout persist). Audit details carry ids, counts
and reason codes only, never an email address, a password or a TOTP code.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.enums import ActorType, UserRole
from app.models import User

log = logging.getLogger(__name__)

ISSUER_NAME = "Denial Recovery Agent"
MIN_PASSWORD_LENGTH = 12
SYSTEM_ACTOR = "system"
ANONYMOUS_ACTOR = "anonymous"
LOCAL_ACTOR = "user:local"
TOTP_DIGITS = 6

_hasher = PasswordHasher()
# Verified against when the email is unknown, so a failed login costs the same time either way.
_DUMMY_HASH = _hasher.hash("not-a-real-password-used-to-equalise-login-timing")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for DateTime(timezone=True) columns; they are UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def actor_for(user: User | object | None) -> str:
    """Audit actor id for a user: ``user:<id>``, ``user:local`` for the sandbox user, else anonymous."""
    if user is None:
        return ANONYMOUS_ACTOR
    user_id = getattr(user, "id", None)
    return LOCAL_ACTOR if user_id is None else f"user:{user_id}"


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def find_user(session: Session, email: str) -> User | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    stmt = select(User).where(func.lower(User.email) == normalized)
    return session.execute(stmt).scalars().first()


def create_user(session: Session, *, email: str, password: str, role: str = "reviewer") -> User:
    """Create an account with an argon2id password hash and a fresh TOTP secret (flushed, not committed)."""
    normalized = normalize_email(email)
    if "@" not in normalized:
        raise ValueError("email must be an email address")
    validate_password(password)
    role_value = str(UserRole(str(role)))
    if find_user(session, normalized) is not None:
        raise ValueError("a user with that email already exists")
    user = User(
        email=normalized,
        password_hash=_hasher.hash(password),
        totp_secret=pyotp.random_base32(),
        role=role_value,
        is_active=True,
        failed_logins=0,
    )
    session.add(user)
    session.flush()
    audit.record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=SYSTEM_ACTOR,
        action="user_created",
        entity_type="user",
        entity_id=user.id,
        details={"role": role_value},
    )
    log.info("user %s created with role %s", user.id, role_value)
    return user


def verify_password(user: User, password: str) -> bool:
    """argon2 verify; the hash is upgraded in place when the parameters are out of date."""
    try:
        _hasher.verify(user.password_hash, password or "")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    return True


def verify_totp(user: User, code: str) -> bool:
    """Accept the current 30-second code or its neighbours (valid_window=1)."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != TOTP_DIGITS or not user.totp_secret:
        return False
    return bool(pyotp.TOTP(user.totp_secret).verify(digits, valid_window=1))


def provisioning_uri(user: User) -> str:
    """otpauth:// URL for an authenticator app (embeds the shared secret: print once, never log)."""
    return pyotp.TOTP(user.totp_secret).provisioning_uri(name=user.email, issuer_name=ISSUER_NAME)


def is_locked(user: User, now: datetime | None = None) -> bool:
    until = as_utc(user.locked_until)
    return until is not None and until > (now or utcnow())


def _record_failure(session: Session, user: User, *, reason: str, now: datetime) -> None:
    """Count one failed attempt; lock the account when the configured maximum is reached."""
    settings = get_settings()
    user.failed_logins = int(user.failed_logins or 0) + 1
    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_for(user),
        action="login_failed",
        entity_type="user",
        entity_id=user.id,
        details={"reason": reason, "failed_logins": user.failed_logins},
    )
    if user.failed_logins >= settings.login_max_failures:
        user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
        audit.record(
            session,
            actor_type=ActorType.USER,
            actor_id=actor_for(user),
            action="login_locked",
            entity_type="user",
            entity_id=user.id,
            details={"failed_logins": user.failed_logins, "lockout_minutes": settings.login_lockout_minutes},
        )
        log.warning("user %s locked after %d failed logins", user.id, user.failed_logins)


def authenticate(session: Session, email: str, password: str) -> User | None:
    """Password step of the login. Returns the user when the password is right and the account is usable.

    Failures are audited with a reason code (unknown_user, inactive, locked, password) and counted
    towards the lockout; a locked account fails even with the right password until the lock expires.
    """
    now = utcnow()
    user = find_user(session, email)
    if user is None:
        try:
            _hasher.verify(_DUMMY_HASH, password or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        audit.record(
            session,
            actor_type=ActorType.USER,
            actor_id=ANONYMOUS_ACTOR,
            action="login_failed",
            details={"reason": "unknown_user"},
        )
        return None
    if not user.is_active:
        audit.record(
            session,
            actor_type=ActorType.USER,
            actor_id=actor_for(user),
            action="login_failed",
            entity_type="user",
            entity_id=user.id,
            details={"reason": "inactive"},
        )
        return None
    password_ok = verify_password(user, password)
    if is_locked(user, now):
        audit.record(
            session,
            actor_type=ActorType.USER,
            actor_id=actor_for(user),
            action="login_failed",
            entity_type="user",
            entity_id=user.id,
            details={"reason": "locked", "failed_logins": int(user.failed_logins or 0)},
        )
        return None
    if not password_ok:
        _record_failure(session, user, reason="password", now=now)
        return None
    user.failed_logins = 0
    user.locked_until = None
    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_for(user),
        action="login_password_ok",
        entity_type="user",
        entity_id=user.id,
    )
    return user


def record_mfa_failure(session: Session, user: User) -> None:
    """A wrong TOTP code counts like a wrong password so codes cannot be guessed."""
    _record_failure(session, user, reason="totp", now=utcnow())


def complete_login(session: Session, user: User) -> None:
    """Both factors passed: stamp the login, clear the failure counter and audit it."""
    user.last_login_at = utcnow()
    user.failed_logins = 0
    user.locked_until = None
    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_for(user),
        action="login_success",
        entity_type="user",
        entity_id=user.id,
    )
    log.info("user %s logged in", user.id)


__all__ = [
    "ANONYMOUS_ACTOR",
    "ISSUER_NAME",
    "LOCAL_ACTOR",
    "MIN_PASSWORD_LENGTH",
    "actor_for",
    "as_utc",
    "authenticate",
    "complete_login",
    "create_user",
    "find_user",
    "is_locked",
    "normalize_email",
    "provisioning_uri",
    "record_mfa_failure",
    "utcnow",
    "validate_password",
    "verify_password",
    "verify_totp",
]
