"""Users, argon2 password hashes, TOTP MFA, session login and the CSRF/origin check.

Entry points: ``app.auth.service`` (create_user, authenticate, verify_totp, ...),
``app.auth.deps`` (require_user, csrf_protect) and ``app.auth.routes`` (login, MFA, logout).
"""
