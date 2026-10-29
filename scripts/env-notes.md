# `.env` keys

Copy `.env.example` to `.env` (git-ignored; `scripts/bootstrap.*` do this for you) and set the values
below. Never commit real values; gitleaks blocks commits that look like they carry a secret.

| Key | Used by | Notes |
|---|---|---|
| `SECRET_KEY` | app (session cookies) | Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Bootstrap writes one. |
| `DATABASE_URL` | app, worker, CLI, Alembic | Local default `sqlite:///./data/denial_agent.db`. Docker Compose overrides it with Postgres. |
| `POSTGRES_PASSWORD` | **Docker Compose only** | Password of the `denial` Postgres role. Required by `docker compose up`: the `db` service refuses to start without it and the `app`/`worker` services build `DATABASE_URL=postgresql+psycopg://denial:${POSTGRES_PASSWORD}@db:5432/denial` from it. Use letters and digits only (a value with `@`, `/`, `:` or `%` would break the URL). Changing it after the first start also requires changing the role password inside Postgres (`ALTER ROLE denial PASSWORD '...'`). |
| `REDIS_URL` | app, worker | Compose sets `redis://redis:6379/0`; local default `redis://127.0.0.1:6379/0`. |
| `JOB_BACKEND` | app, CLI | `inline` (default; no Redis) or `rq` (Compose). |
| `APP_ENV` | app | `local`, `test` or `docker`. |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_EFFORT`, `LLM_SERVER_FALLBACKS`, `CLASSIFY_MODEL_THRESHOLD` | pipeline | `stub` is offline and deterministic; `anthropic` needs `ANTHROPIC_API_KEY` from the HIPAA-ready organization. |
| `ANTHROPIC_API_KEY` | Anthropic SDK | Only in the OS keychain or this git-ignored file. Never logged. |
| `AUTH_REQUIRED`, `SESSION_COOKIE_SECURE` | app | Keep `AUTH_REQUIRED=true` outside tests. |
| `DATA_DIR`, `EXPORT_DIR` | app, CLI | `./data` (git-ignored, on the encrypted disk). `purge-clinic` writes its export zip to `EXPORT_DIR`. |
| `LOG_LEVEL`, `DEADLINE_WARN_DAYS`, `CLINIC_ID` | app, CLI | `CLINIC_ID` is the default clinic for `import-claims/notes/documents` when `--clinic-id` is omitted. |

## Docker Compose

```
cp .env.example .env            # then set SECRET_KEY and add POSTGRES_PASSWORD=<letters and digits>
docker compose up --build       # Postgres, Redis, app (127.0.0.1:8000) and one RQ worker
docker compose exec app denial-agent create-user --email reviewer@example.test --role admin
```

The `app` container runs `alembic upgrade head` before `uvicorn`. All ports are bound to
127.0.0.1. The containers run as uid 1000 and write to `./data` (Postgres data, exports): on Linux
hosts make that directory writable by uid 1000 (`sudo chown -R 1000:1000 data`).
