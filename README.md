# Denial Recovery Agent — local MVP

Reads insurance remittance files (X12 835) and a clinic's claim export, classifies each denial,
proposes a fix (corrected claim, appeal, or a question for the clinic), drafts the appeal letter, and
tracks every claim until it is paid or closed. **A person approves every action; the software never
submits anything.** This is Phase 1 of the plan in `docs/PHASES.md`: everything runs locally on
synthetic data until the clinic BAA, the model-provider BAA and the laptop security checklist are done.

- Build specification: `docs/SPEC.md`
- Plan phases and milestone status: `docs/PHASES.md`
- Testing plan → test files: `docs/TESTING.md`
- Before any real data: `docs/SECURITY_CHECKLIST.md`, `docs/PILOT_CHECKLIST.md`

## Quick start (no Docker, no API key)

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env            # Windows: copy .env.example .env
uv run denial-agent init-db --migrate
uv run denial-agent create-clinic --name "Pilot Clinic"   # prints the clinic id (1 on a fresh database)
uv run denial-agent create-user --email you@example.com   # prompts for a password, prints the TOTP enrolment for your authenticator app
uv run denial-agent generate-synthetic --out data/synthetic --seed 42 --claims 200
uv run denial-agent import-claims data/synthetic/claims_export.csv --clinic-id 1
uv run denial-agent import-notes data/synthetic/notes.csv --clinic-id 1
uv run denial-agent import-documents data/synthetic/payer_documents.csv --clinic-id 1
uv run denial-agent import-835 data/synthetic/remit_NW001.835      # repeat for each remit_*.835; denials are processed on import
uv run denial-agent process                                         # creates work items for anything still pending
uv run denial-agent serve                                           # http://127.0.0.1:8000
```

`scripts/bootstrap.ps1` (Windows) and `scripts/bootstrap.sh` run those steps for you.

With the default `.env` the model client is the offline **stub** (`LLM_PROVIDER=stub`): the rules engine
classifies every denial and appeal letters come straight from the templates. To use the real model, set
`LLM_PROVIDER=anthropic` and put the API key from the HIPAA-ready organization in `.env` or the OS
keychain. The model id is pinned (`LLM_MODEL`); run the prompt regression tests before changing it.

## Docker Compose (app + Postgres 16 + Redis)

```bash
cp .env.example .env    # set SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build
```

Postgres, Redis and the app bind to `127.0.0.1` only. Migrations run on start (`alembic upgrade head`).
Create the first user with `docker compose exec app denial-agent create-user --email you@example.com`.

## What is in the box

| Area | Code | Plan milestone |
|---|---|---|
| 835 parser, claim-export importer, matching, duplicate-safe imports | `app/ingest/` | M3 |
| Rules engine (CARC/RARC → category) + model classifier for ambiguous cases | `app/classify/` | M4 |
| Redaction (tokens for names, member ids, addresses, dates) and the model guard | `app/redact/`, `app/llm/` | M5 |
| Playbooks per category, corrected-claim diff, appeal letters, fact check, PDF | `app/playbooks/`, `app/drafting/` | M6 |
| Review queue, approve / edit / reject, export package, imports page | `app/review/`, `app/auth/` | M7 |
| Outcome matching from later 835s, dashboard, append-only audit log, MFA login | `app/outcomes/`, `app/audit/` | M8 |
| Synthetic patients, claims, 835 files, payer letters, labeled test set | `synthetic/` | Phase 1 test data |

## Tests

```bash
uv run pytest -q
```

The suite needs no services: SQLite, inline jobs and the stub model client. See `docs/TESTING.md` for
how each test maps to the plan's testing table (parser golden files, classifier accuracy, prompt
regression, redaction, PHI in logs, injection, end-to-end synthetic loop).

## Secret scanning

```bash
uv run pre-commit install      # runs gitleaks and ruff before every commit (gitleaks hook needs Go or a gitleaks binary)
```

CI (`.github/workflows/ci.yml`) runs gitleaks, ruff and the test suite on every push.

## Ground rules enforced in code

See `docs/SPEC.md` section 0: no submission, flagged coding changes that require a quoted note excerpt,
minimum-necessary redacted model calls, append-only audit log, payer documents treated as untrusted
data, and a `purge-clinic` command that exports then destroys a clinic's data with a dated record.
