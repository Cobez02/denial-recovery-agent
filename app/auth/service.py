"""User accounts, argon2 password hashing, TOTP MFA and the login lockout rules (SPEC section 15).

Nothing here commits: the caller owns the transaction (the login route commits even after a
failed attempt so the failure counter and the lockout persist). Audit details carry ids, counts
and reason codes only, never an email address, a password or a TOTP code.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.attributes import set_committed_value

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


def totp_step(user: User, code: str, now: datetime | None = None) -> int | None:
    """The 30-second time step (RFC 6238 counter) a code belongs to: the current one or a neighbour.

    Returns None when the code is not valid in that window. Comparison is constant-time.
    """
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != TOTP_DIGITS or not user.totp_secret:
        return None
    totp = pyotp.TOTP(user.totp_secret)
    current = totp.timecode(now or utcnow())
    for offset in (0, -1, 1):
        step = current + offset
        if step >= 0 and hmac.compare_digest(totp.generate_otp(step), digits):
            return step
    return None


def verify_totp(
    user: User, code: str, now: datetime | None = None, *, session: Session | None = None
) -> bool:
    """Accept the current 30-second code or its neighbours (valid_window=1), each code only once.

    An accepted code consumes its time step: ``users.totp_last_step`` records it and any code at
    or below that step is refused afterwards (RFC 6238 section 5.2), so a code seen over a
    shoulder or in a screen share cannot be replayed within the window. The step is consumed with
    one conditional UPDATE in ``session`` (or the session the user is attached to), so two requests
    that both loaded the user before either committed cannot both be accepted: the row decides, not
    the loaded copy. A user outside any session is checked and updated in memory. The caller commits.
    """
    step = totp_step(user, code, now)
    if step is None:
        return False
    session = session if session is not None else object_session(user)
    if session is not None and user.id is not None:
        accepted = _consume_totp_step(session, user, step)
    else:
        last = user.totp_last_step
        accepted = last is None or step > int(last)
        if accepted:
            user.totp_last_step = step
    if not accepted:
        log.warning("user %s: replayed TOTP code refused", user.id)
    return accepted


def _consume_totp_step(session: Session, user: User, step: int) -> bool:
    """Record ``step`` on the user's row unless it already holds a step at or above it (one statement).

    ``UPDATE users SET totp_last_step = :step WHERE id = :id AND (totp_last_step IS NULL OR
    totp_last_step < :step)``: a concurrent request with the same code waits on the row and then
    matches nothing. Dialect-neutral (``with_for_update`` is a no-op on SQLite).
    """
    result = session.execute(
        update(User)
        .where(User.id == user.id, or_(User.totp_last_step.is_(None), User.totp_last_step < step))
        .values(totp_last_step=step)
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        return False
    set_committed_value(user, "totp_last_step", step)  # mirror the row; nothing is left to flush
    return True


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
    """Both factors passed: stamp the login, clear the failure counter and audit it.

    The accepted TOTP step was consumed on the row by ``verify_totp`` in this transaction; the
    caller's commit persists it with the login stamp.
    """
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
    "totp_step",
    "utcnow",
    "validate_password",
    "verify_password",
    "verify_totp",
]
