# `.env` keys

Copy `.env.example` to `.env` (git-ignored; `scripts/bootstrap.*` do this and fill in `SECRET_KEY`)
and set the values below. Never commit real values; gitleaks blocks commits that look like they carry
a secret.

How the file is read (`app/config.py`): always `<project root>/.env`, whatever directory a command
is started from; a variable set in the process environment wins over the file; the test suite
(`APP_ENV=test`) ignores the file and clears every one of these variables first. Secrets are never
logged. The list is every field of `Settings`, plus the one key Docker Compose reads itself.

| Key | Default | Used by | Notes |
|---|---|---|---|
| `APP_ENV` | `local` | app | `local`, `test` or `docker` (Compose sets `docker`). |
| `SECRET_KEY` | placeholder | app (session cookies) | **Required.** Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. `create_app` refuses an empty, short (< 24 characters) or placeholder key outside tests, so `denial-agent serve` and the app container exit instead of signing cookies with a known value. The other CLI commands do not need it. |
| `DATABASE_URL` | `sqlite:///./data/denial_agent.db` | app, worker, CLI, Alembic | Relative SQLite paths resolve against the project root; every SQLite connection sets `PRAGMA foreign_keys=ON` and `PRAGMA secure_delete=ON`. Compose overrides it with Postgres. |
| `DATA_DIR` | `./data` | app, CLI | Uploads land under `DATA_DIR/uploads/<kind>/<sha16>_<name>`. On the encrypted disk. |
| `EXPORT_DIR` | `./data/exports` | app, CLI | Export packages (`work_item_<id>_<ccn>_v<n>_<stamp>.zip`) and the `purge-clinic` hand-over zip. `purge-clinic --confirm` deletes every file under `DATA_DIR/uploads` and `EXPORT_DIR`, then writes the hand-over zip there and records the file names it removed; on SQLite it runs `VACUUM` afterwards (`vacuumed` in its report), on PostgreSQL the DBA runs `VACUUM FULL`; delete that zip once the clinic has received it. |
| `JOB_BACKEND` | `inline` | app, CLI | `inline` runs imports and the pipeline synchronously (no Redis); `rq` queues them for `python -m app.jobs.worker` (Compose). |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | app, worker | Compose sets `redis://redis:6379/0`. |
| `LLM_PROVIDER` | `stub` | pipeline | `stub` is offline and deterministic; `anthropic` makes real calls (plain Messages API) and needs `ANTHROPIC_API_KEY`; `replay` serves recorded fixtures from `LLM_REPLAY_DIR` (tests). |
| `LLM_MODEL` | `claude-opus-5-5` | Anthropic client | Pinned. Run `tests/test_prompt_regression.py` (and the `live_llm` test with a key) before changing it. |
| `LLM_EFFORT` | `medium` | Anthropic client | `low`, `medium`, `high`, `xhigh` or `max` (`output_config.effort`). |
| `LLM_MAX_RETRIES` | `2` | Anthropic client | SDK retries on transport errors. |
| `LLM_TIMEOUT_SECONDS` | `120` | Anthropic client | Per request. |
| `LLM_SERVER_FALLBACKS` | `false` | Anthropic client | Keep off: a server-side fallback would answer with a model other than the pinned one (uses the beta API when on). |
| `ANTHROPIC_API_KEY` | unset | Anthropic SDK | Only with `LLM_PROVIDER=anthropic`, from the HIPAA-ready organization. Export it in the process environment (an OS secret store can do that from your shell profile; the app has no keychain integration) or put it in this git-ignored file. Never logged. |
| `CLASSIFY_MODEL_THRESHOLD` | `0.75` | classifier | Rules confidence below which the model is consulted; it is also consulted for category `other` and whenever a payer letter is attached. |
| `MODEL_ENABLED_FOR_DRAFTING` | `true` | drafting | Lets the model rewrite the appeal narrative paragraph; the rewrite is fact-checked and the template kept when it fails. |
| `LLM_RECORD` | `false` | Anthropic client | Development only: wraps the real client and writes replay fixtures into `LLM_REPLAY_DIR`. Never record against real data. |
| `LLM_REPLAY_DIR` | `tests/fixtures/llm` | replay / recording | Inside the repository and scanned by gitleaks. |
| `AUTH_REQUIRED` | `true` | app | Keep `true` outside the test suite; `false` replaces the login with a local admin (`user:local` in the audit log). |
| `SESSION_COOKIE_SECURE` | `false` | app | `true` once the app is served over HTTPS. |
| `SESSION_MAX_AGE_SECONDS` | `28800` | app | Session cookie lifetime (8 hours). |
| `LOGIN_MAX_FAILURES` | `5` | login | Wrong passwords or TOTP codes before the account locks. |
| `LOGIN_LOCKOUT_MINUTES` | `15` | login | Length of the lock. |
| `LOG_LEVEL` | `INFO` | app, CLI, worker | Every handler carries the PHI-masking filter whatever the level. |
| `DEADLINE_WARN_DAYS` | `30,14` | queue, dashboard | Badge thresholds in days. |
| `CLINIC_ID` | unset | CLI, imports page | Default clinic for `import-claims/notes/documents` and the upload page when several clinics exist; with one clinic it is found automatically. Leave it commented out rather than empty. |
| `DEFAULT_TIMELY_FILING_DAYS` | `90` | claims importer | Windows given to a payer the first time it is seen. Change a payer later with `denial-agent set-payer --payer-id X --timely-filing-days N --corrected-claim-days N --appeal-days N` (audited); the importer never overwrites them. |
| `DEFAULT_CORRECTED_CLAIM_DAYS` | `90` | claims importer | As above. |
| `DEFAULT_APPEAL_DAYS` | `180` | claims importer | As above. |
| `POSTGRES_PASSWORD` | unset | **Docker Compose only** | Password of the `denial` Postgres role. `docker compose up` refuses to start without it and builds `DATABASE_URL=postgresql+psycopg://denial:${POSTGRES_PASSWORD}@db:5432/denial` for the app and worker. Letters and digits only (`@`, `/`, `:` or `%` would break the URL). Changing it after the first start also requires `ALTER ROLE denial PASSWORD '...'` inside Postgres. Not read by the application. |

Empty values: `SECRET_KEY=` is allowed (and refused by the server); an empty numeric key such as
`CLINIC_ID=` is a validation error, so comment optional keys out instead.

## Docker Compose

```
cp .env.example .env            # set SECRET_KEY and add POSTGRES_PASSWORD=<letters and digits>
docker compose up --build       # Postgres, Redis, app (127.0.0.1:8000) and one RQ worker
docker compose exec app denial-agent create-clinic --name "Pilot Clinic"
docker compose exec app denial-agent create-user --email reviewer@example.test --role admin
```

The `app` container runs `denial-agent init-db --migrate` and then `denial-agent serve` (the CLI
hands uvicorn `log_config=None`, so uvicorn's loggers propagate to the PHI-masking root handler
instead of printing unmasked tracebacks). The `worker` container runs `python -m app.jobs.worker`,
not `rq worker`: it configures the same masked logging and logs or stores only the exception class
name and frames of a failed job, never its message or arguments. It starts once `app` is healthy
(`/api/health` answers 200), so migrations have run. All ports are bound to 127.0.0.1. `JOB_BACKEND`,
`DATABASE_URL`, `REDIS_URL` and `APP_ENV` from `.env` are overridden by the compose file; everything
else in `.env` reaches both containers.

Postgres keeps its data in the Docker named volume `pgdata` (`docker volume ls`); it is removed
only by `docker compose down -v`. The containers run as uid 1000 and write uploads and export
packages to `./data` (mounted at `/app/data`): on Linux hosts make that directory writable by uid
1000 (`sudo chown -R 1000:1000 data`). Files to import from the command line go under `./data` first.
The Dockerfile fails the build when WeasyPrint cannot be imported, so the image always renders
`appeal_letter.pdf`. The stack is reviewed but has not been run on the development machine.

## Secret scanning on every commit

`scripts/bootstrap.*` run `pre-commit install` when pre-commit is available in the venv (it is a dev
dependency) and print a warning when it is not; CI scans the whole history as well. The hook
builds gitleaks from source on its first run, which needs a Go toolchain (`scripts/precommit-gitleaks.sh`
is the plain-git-hook alternative for a machine with the `gitleaks` binary on PATH). The ruff hook
is pinned to the same ruff version as `uv.lock`, so the hook and CI agree.
