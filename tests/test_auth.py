"""Auth and MFA (docs/SPEC.md section 15): argon2 hashes, TOTP, lockout, session requirement,
CSRF/origin checks on POST routes, the AUTH_REQUIRED=false sandbox mode and the public health route.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import service
from app.models import AuditLog, User

BASE32 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')
HX = {"HX-Request": "true"}


def _wrong_code(secret: str) -> str:
    """A six-digit code that is not valid in the current window or its neighbours."""
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    valid = {totp.at(now - 30), totp.at(now), totp.at(now + 30)}
    for candidate in ("000000", "111111", "222222", "333333"):
        if candidate not in valid:
            return candidate
    raise AssertionError("unreachable: four candidates cannot all be valid")


def _client(app) -> TestClient:
    return TestClient(app, base_url="http://testserver", follow_redirects=False)


def _csrf(client: TestClient) -> str:
    page = client.get("/queue")
    assert page.status_code == 200, page.text
    match = CSRF_RE.search(page.text)
    assert match is not None
    return match.group(1)


def _events(session, action: str) -> list[AuditLog]:
    return list(
        session.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)).scalars()
    )


# ---- service ---------------------------------------------------------------------------


def test_create_user_hashes_with_argon2_and_stores_a_base32_secret(session):
    user = service.create_user(
        session, email="Coder@Example.test", password="a very long password 123", role="admin"
    )
    assert user.password_hash.startswith("$argon2")
    assert len(user.totp_secret) == 32 and set(user.totp_secret) <= BASE32
    assert user.email == "coder@example.test" and user.role == "admin" and user.is_active is True
    assert service.verify_password(user, "a very long password 123")
    assert not service.verify_password(user, "a very long password 124")
    assert service.verify_totp(user, pyotp.TOTP(user.totp_secret).now())
    assert not service.verify_totp(user, _wrong_code(user.totp_secret))
    uri = service.provisioning_uri(user)
    assert uri.startswith("otpauth://totp/") and "Denial%20Recovery%20Agent" in uri

    (event,) = _events(session, "user_created")
    assert event.entity_id == user.id and event.actor_id == "system"
    assert event.details == {"role": "admin"}
    assert "example.test" not in str(event.details)


def test_create_user_rejects_short_passwords_duplicates_and_unknown_roles(session):
    with pytest.raises(ValueError):
        service.create_user(session, email="short@example.test", password="short")
    service.create_user(session, email="dup@example.test", password="long enough password 1")
    with pytest.raises(ValueError):
        service.create_user(session, email="DUP@example.test", password="long enough password 1")
    with pytest.raises(ValueError):
        service.create_user(
            session, email="role@example.test", password="long enough password 1", role="root"
        )
    with pytest.raises(ValueError):
        service.create_user(session, email="not-an-email", password="long enough password 1")


# ---- login flow --------------------------------------------------------------------------


def test_wrong_password_gives_no_session(app, session, reviewer):
    c = _client(app)
    r = c.post("/login", data={"email": reviewer.email, "password": "definitely not the password"})
    assert r.status_code == 200 and "not accepted" in r.text
    assert c.get("/queue").status_code == 303
    assert c.get("/queue").headers["location"] == "/login"
    assert c.get("/login/mfa").status_code == 303  # no pending password step either
    (event,) = _events(session, "login_failed")
    assert event.actor_id == f"user:{reviewer.id}" and event.details["reason"] == "password"
    assert "example.test" not in str(event.details)

    # an unknown email fails the same way and is audited anonymously, never with the address
    r = c.post("/login", data={"email": "nobody@example.test", "password": "whatever password!!"})
    assert r.status_code == 200 and "not accepted" in r.text
    unknown = _events(session, "login_failed")[-1]
    assert unknown.actor_id == "anonymous" and unknown.details == {"reason": "unknown_user"}


def test_wrong_totp_then_right_totp(app, session, reviewer, reviewer_password):
    c = _client(app)
    r = c.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa"
    assert c.get("/queue").status_code == 303  # the password alone is not a login
    assert c.get("/login/mfa").status_code == 200

    r = c.post("/login/mfa", data={"code": _wrong_code(reviewer.totp_secret)})
    assert r.status_code == 200 and "not accepted" in r.text
    assert c.get("/queue").status_code == 303

    r = c.post("/login/mfa", data={"code": pyotp.TOTP(reviewer.totp_secret).now()})
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    assert c.get("/queue").status_code == 200

    session.expire_all()
    user = session.get(User, reviewer.id)
    assert user.last_login_at is not None and user.failed_logins == 0
    (success,) = _events(session, "login_success")
    assert success.actor_id == f"user:{reviewer.id}"
    totp_failure = [e for e in _events(session, "login_failed") if e.details.get("reason") == "totp"]
    assert len(totp_failure) == 1


def test_lockout_after_max_failures(app, session, reviewer, reviewer_password, settings):
    c = _client(app)
    for _ in range(settings.login_max_failures):
        r = c.post("/login", data={"email": reviewer.email, "password": "wrong password, sorry!"})
        assert r.status_code == 200
    # even the right password fails while the lock holds
    r = c.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 200 and "not accepted" in r.text
    assert c.get("/queue").status_code == 303

    session.expire_all()
    user = session.get(User, reviewer.id)
    assert user.failed_logins == settings.login_max_failures
    assert user.locked_until is not None
    locked = _events(session, "login_locked")
    assert len(locked) == 1 and locked[0].actor_id == f"user:{reviewer.id}"
    assert locked[0].details["failed_logins"] == settings.login_max_failures
    for event in _events(session, "login_failed"):
        assert "example.test" not in str(event.details) and "password" != event.details.get("value")

    # once the lock expires the right password works again and the counter resets
    user.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    session.commit()
    r = c.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa"
    session.expire_all()
    assert session.get(User, reviewer.id).failed_logins == 0


def test_lockout_after_repeated_wrong_totp_codes(app, session, reviewer, reviewer_password, settings):
    c = _client(app)
    r = c.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 303
    wrong = _wrong_code(reviewer.totp_secret)
    for _ in range(settings.login_max_failures - 1):
        r = c.post("/login/mfa", data={"code": wrong})
        assert r.status_code == 200 and "not accepted" in r.text
    r = c.post("/login/mfa", data={"code": wrong})
    assert r.status_code == 303 and r.headers["location"] == "/login"  # locked: back to the start
    assert c.get("/queue").status_code == 303
    assert c.get("/login/mfa").status_code == 303  # the pending password step was cleared

    session.expire_all()
    user = session.get(User, reviewer.id)
    assert user.locked_until is not None and user.failed_logins == settings.login_max_failures
    (locked,) = _events(session, "login_locked")
    assert locked.actor_id == f"user:{reviewer.id}"
    totp_failures = [e for e in _events(session, "login_failed") if e.details.get("reason") == "totp"]
    assert len(totp_failures) == settings.login_max_failures
    # the lock holds at the password step too, even with the right password
    r = c.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 200 and "not accepted" in r.text
    assert c.get("/login/mfa").status_code == 303


# ---- session requirement and CSRF ----------------------------------------------------------


def test_anonymous_access_is_redirected_or_401(anon_client):
    for path in ("/queue", "/items/1", "/imports", "/dashboard", "/audit", "/items/1/export"):
        r = anon_client.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/login", path
    assert anon_client.get("/api/metrics").status_code == 401
    assert anon_client.get("/api/items/1").status_code == 401
    hx = anon_client.get("/queue", headers=HX)
    assert hx.status_code == 303 and hx.headers.get("hx-redirect") == "/login"
    hx_post = anon_client.post("/items/1/approve", headers=HX)
    assert hx_post.status_code == 303 and hx_post.headers.get("hx-redirect") == "/login"
    assert anon_client.post("/items/1/approve").status_code == 403  # no session, no token either
    assert (
        anon_client.post("/imports/835", files={"file": ("x.835", b"ISA", "text/plain")}).status_code == 403
    )
    assert anon_client.get("/").status_code == 303
    assert anon_client.get("/login").status_code == 200


def test_posts_need_the_csrf_token_or_hx_header_and_the_same_origin(client, session, reviewer):
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", data={"_csrf": "not-the-token"}).status_code == 403
    assert client.post("/items/1/approve").status_code == 403
    token = _csrf(client)
    foreign = {"Origin": "http://evil.example"}
    assert client.post("/logout", data={"_csrf": token}, headers=foreign).status_code == 403
    assert client.post("/items/1/approve", data={"_csrf": token}, headers=foreign).status_code == 403
    assert client.post("/items/1/approve", headers={**HX, **foreign}).status_code == 403
    referer = {"Referer": "http://evil.example/page"}
    assert client.post("/logout", data={"_csrf": token}, headers=referer).status_code == 403
    # the HX-Request header stands in for the token (same origin): the request reaches the handler
    assert client.post("/items/999999/approve", headers=HX).status_code == 404
    # same-origin Origin header plus the token is accepted
    r = client.post("/logout", data={"_csrf": token}, headers={"Origin": "http://testserver"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/queue").status_code == 303
    (event,) = _events(session, "logout")
    assert event.actor_id == f"user:{reviewer.id}"
    # login POSTs are exempt from the token but still origin-checked
    bad = client.post("/login", data={"email": "a@example.test", "password": "x" * 12}, headers=foreign)
    assert bad.status_code == 403


def test_auth_not_required_allows_access_as_the_local_user(engine, monkeypatch):
    from app.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("AUTH_REQUIRED", "false")
    reset_settings_cache()
    try:
        c = _client(create_app())
        assert c.get("/queue").status_code == 200
        assert c.get("/dashboard").status_code == 200
        assert c.get("/imports").status_code == 200
        assert c.get("/api/metrics").status_code == 200
        assert c.get("/login").status_code == 303  # nothing to log in to
        assert "local@localhost" in c.get("/queue").text
    finally:
        reset_settings_cache()


def test_health_is_public(anon_client):
    r = anon_client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db_ok": True, "audit_chain_ok": True}
