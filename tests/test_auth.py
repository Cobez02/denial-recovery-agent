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
    code = pyotp.TOTP(user.totp_secret).now()
    assert user.totp_last_step is None
    assert service.verify_totp(user, code)
    assert user.totp_last_step == service.totp_step(user, code)
    assert not service.verify_totp(user, code)  # consumed: the same code is a replay
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


def test_totp_code_cannot_be_replayed(app, session, reviewer, reviewer_password):
    """RFC 6238 section 5.2: an accepted code consumes its time step (users.totp_last_step), so a code
    seen over a shoulder or in a screen share is useless even inside the 90-second window."""
    now = datetime.now(UTC)
    totp = pyotp.TOTP(reviewer.totp_secret)
    code = totp.at(now)

    first = _client(app)
    r = first.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 303
    r = first.post("/login/mfa", data={"code": code})
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    session.expire_all()
    user = session.get(User, reviewer.id)
    assert user.totp_last_step == totp.timecode(now)

    # the same code again, from a second browser, inside the window: refused and audited
    second = _client(app)
    r = second.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa"
    r = second.post("/login/mfa", data={"code": code})
    assert r.status_code == 200 and "not accepted" in r.text
    assert second.get("/queue").status_code == 303
    failures = [e for e in _events(session, "login_failed") if e.details.get("reason") == "totp"]
    assert len(failures) == 1 and failures[0].actor_id == f"user:{reviewer.id}"
    assert code not in str(failures[0].details)

    # the step before the accepted one is a replay too (it was inside the accepted window)
    r = second.post("/login/mfa", data={"code": totp.at(now, -1)})
    assert r.status_code == 200 and "not accepted" in r.text

    # the next step's code is new and logs the second browser in
    r = second.post("/login/mfa", data={"code": totp.at(now, 1)})
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    assert second.get("/queue").status_code == 200
    session.expire_all()
    assert session.get(User, reviewer.id).totp_last_step == totp.timecode(now) + 1
    assert len(_events(session, "login_success")) == 2


def test_totp_replay_guard_is_decided_by_the_row_not_the_loaded_copy(engine, session):
    """Two MFA submissions with the same code that both loaded the user before either committed
    (two browsers, one shoulder-surfed code): the step is consumed with one conditional UPDATE, so
    the second request is refused although its in-memory copy still says the code is unused."""
    from app.db import session_factory

    user = service.create_user(session, email="race@example.test", password="a very long password 123")
    session.commit()
    uid, secret = user.id, user.totp_secret
    code = pyotp.TOTP(secret).now()
    step = service.totp_step(user, code)
    first, second = session_factory()(), session_factory()()
    try:
        u1, u2 = first.get(User, uid), second.get(User, uid)
        assert u1.totp_last_step is None and u2.totp_last_step is None
        assert service.verify_totp(u1, code, session=first)
        assert u1.totp_last_step == step
        first.commit()
        assert u2.totp_last_step is None  # the second request still holds the stale copy
        assert not service.verify_totp(u2, code, session=second)
        assert u2.totp_last_step is None  # nothing was written for the refused request
        second.commit()
        # the next step's code is new and goes through for the second request
        later = pyotp.TOTP(secret).at(datetime.now(UTC), 1)
        assert service.verify_totp(u2, later, session=second, now=datetime.now(UTC) + timedelta(seconds=30))
        second.commit()
    finally:
        first.close()
        second.close()
    session.expire_all()
    assert session.get(User, uid).totp_last_step == step + 1


def test_verify_totp_without_a_session_checks_the_loaded_copy():
    """A user outside any session (pure-function callers) is checked and updated in memory."""
    user = User(
        email="pure@example.test",
        password_hash="x",
        totp_secret=pyotp.random_base32(),
        role="reviewer",
        is_active=True,
        failed_logins=0,
    )
    code = pyotp.TOTP(user.totp_secret).now()
    assert service.verify_totp(user, code)
    assert user.totp_last_step == service.totp_step(user, code)
    assert not service.verify_totp(user, code)  # consumed
    assert not service.verify_totp(user, _wrong_code(user.totp_secret))


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
    # the login check runs before the CSRF check on every POST: no session means the login page
    plain_post = anon_client.post("/items/1/approve")
    assert plain_post.status_code == 303 and plain_post.headers["location"] == "/login"
    upload = anon_client.post("/imports/835", files={"file": ("x.835", b"ISA", "text/plain")})
    assert upload.status_code == 303 and upload.headers["location"] == "/login"
    assert anon_client.get("/").status_code == 303
    assert anon_client.get("/login").status_code == 200


def test_anonymous_upload_is_refused_before_the_body_is_read(anon_client, settings, monkeypatch):
    """An upload route parses its multipart body only after the login check passed, so an anonymous
    client cannot make the server parse and spool a large body (nor store anything) for a 303."""
    from starlette.requests import Request

    parsed: list[str] = []
    original_form = Request.form

    def spying_form(self, *args, **kwargs):
        parsed.append(self.url.path)
        return original_form(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", spying_form)
    body = b"ISA*00*" + b"x" * 50_000
    for kind in ("835", "claims", "notes", "documents"):
        r = anon_client.post(f"/imports/{kind}", files={"file": (f"big.{kind}", body, "text/plain")})
        assert r.status_code == 303 and r.headers["location"] == "/login", kind
    assert parsed == []
    assert not (settings.data_path / "uploads").exists()
    # an HTMX-style anonymous upload is turned away the same way
    r = anon_client.post("/imports/835", files={"file": ("x.835", body, "text/plain")}, headers=HX)
    assert r.status_code == 303 and r.headers.get("hx-redirect") == "/login" and parsed == []


def test_upload_routes_refuse_a_declared_oversized_body_before_parsing(client, settings, monkeypatch):
    from starlette.requests import Request

    from app.review import routes

    parsed: list[str] = []
    original_form = Request.form

    def spying_form(self, *args, **kwargs):
        parsed.append(self.url.path)
        return original_form(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", spying_form)
    # the HTMX header stands in for the token, so nothing before the handler needs the form
    r = client.post(
        "/imports/835",
        content=b"not really this long",
        headers={
            **HX,
            "content-type": "multipart/form-data; boundary=x",
            "content-length": str(routes.MAX_UPLOAD_BYTES + routes.UPLOAD_ENVELOPE_BYTES + 1),
        },
    )
    assert r.status_code == 413 and "20 MB" in r.text and parsed == []
    assert not (settings.data_path / "uploads").exists()
    # the imports page's own forms are plain multipart posts carrying the token in the body: the
    # declared size is refused before the CSRF check would parse that body for the token
    for kind in ("835", "claims", "notes", "documents"):
        r = client.post(
            f"/imports/{kind}",
            content=b"not really this long either",
            headers={
                "content-type": "multipart/form-data; boundary=x",
                "content-length": str(routes.MAX_UPLOAD_BYTES + routes.UPLOAD_ENVELOPE_BYTES + 1),
            },
        )
        assert r.status_code == 413 and "20 MB" in r.text, kind
    assert parsed == []
    assert not (settings.data_path / "uploads").exists()
    # once the size is acceptable the CSRF check still guards the upload (403 without the token)
    r = client.post("/imports/835", files={"file": ("x.835", b"ISA", "text/plain")})
    assert r.status_code == 403 and parsed == ["/imports/835"]
    parsed.clear()
    # a form without a file part is a plain refusal, not a server error
    r = client.post("/imports/835", data={"nothing": "here"}, headers=HX)
    assert r.status_code == 400 and "Choose a file" in r.text


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
    body = r.json()
    assert set(body) == {"status", "db_ok", "audit_chain_ok", "audit_chain_verified_at"}
    assert (body["status"], body["db_ok"], body["audit_chain_ok"]) == ("ok", True, True)
    assert datetime.fromisoformat(body["audit_chain_verified_at"]) <= datetime.now(UTC)
