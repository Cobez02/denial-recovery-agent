# Local machine security checklist

From the plan: complete and date every item **before real patient data lands on the machine**. A laptop
holding PHI is the project's single biggest breach risk during Phases 2 to 4. Keep this file updated;
it is part of the Phase 0 exit criteria and the risk analysis.

| Done | Control | Setting | Completed on | By |
|---|---|---|---|---|
| [ ] | Disk encryption | BitLocker (Windows) or FileVault (macOS) on; recovery key stored offline | | |
| [ ] | Dedicated account | A separate OS user account used only for this project | | |
| [ ] | Sign-in | Strong password plus MFA where the OS supports it; screen locks after 5 minutes | | |
| [ ] | No cloud sync | Project and data folders excluded from OneDrive, iCloud, Dropbox and Google Drive | | |
| [ ] | Database | Postgres and Redis published on `127.0.0.1` only (`docker-compose.yml`); Postgres data in the Docker named volume `pgdata`, so Docker's data root must live on the encrypted disk; uploads and export packages under `./data` (git-ignored) | | |
| [ ] | Backups | Encrypted, to an encrypted external drive kept locked away; no unencrypted copies | | |
| [ ] | Network | Firewall on; no remote-access tools open to the internet; the app bound to `127.0.0.1:8000` | | |
| [ ] | Updates and protection | Automatic OS updates; endpoint protection running; dependencies updated monthly | | |
| [ ] | Code repository | `data/` git-ignored; gitleaks runs before every commit (`uv run pre-commit install`; `.gitleaks.toml` allowlists only `synthetic/fixtures/`, `tests/fixtures/835/` and `tests/fixtures/claims/`); no real data in test fixtures | | |
| [ ] | Logs | Ids, counts and codes only; `PHIMaskingFilter` (`app/logging_config.py`) as defence in depth on every handler, including uvicorn's and the rq worker's; `tests/test_phi_in_logs.py` fails the build if an application logger ever needs it | | |
| [ ] | Keys | `SECRET_KEY` generated, never a placeholder; model API keys from the HIPAA-ready organization only, exported in the process environment or kept in the git-ignored `.env` (there is no keychain integration; an OS secret store may populate the environment); never personal or consumer AI accounts | | |
| [ ] | Physical | Device never left unattended unlocked; device tracking and remote wipe enabled | | |
| [ ] | Disposal | Clinic data erased at pilot end or after cloud migration, with a dated record (`denial-agent purge-clinic <id> --confirm`), then the hand-over zip deleted after delivery | | |

## Software controls already in place (verify, do not assume)

Each line names the code and the way to check it.

- **Placeholder `SECRET_KEY` refused.** `app/config.py` `is_placeholder_secret` and `app/main.py`
  `create_app` raise outside the test suite when the key is empty, shorter than 24 characters or one of
  `change-me`, `changeme`, `secret`; `denial-agent serve` checks first (`app/cli.py` `cmd_serve`) and
  exits 1 with the instruction to generate a key. Check: `SECRET_KEY=change-me denial-agent serve`
  prints `error: SECRET_KEY is missing or a placeholder ...` and exits 1
  (`tests/test_cli.py::test_serve_refuses_a_placeholder_secret_key_with_a_readable_error`).
- **Session cookie** `denial_session`: `HttpOnly`, `SameSite=Strict`, 8-hour lifetime
  (`SESSION_MAX_AGE_SECONDS`), `Secure` when `SESSION_COOKIE_SECURE=true` (`app/main.py`).
- **Login**: argon2id password hashes, TOTP (pyotp, 30-second step, one step either side), each code
  accepted once (`users.totp_last_step`), `LOGIN_MAX_FAILURES=5` wrong passwords or codes lock the
  account for `LOGIN_LOCKOUT_MINUTES=15`, every attempt audited with a reason code (`app/auth/service.py`).
  Check: `uv run pytest tests/test_auth.py -q`.
- **State-changing requests**: `require_user` then `csrf_protect` (`app/auth/deps.py`): the Origin or
  Referer header, when present, must name this host, and the request must carry the `HX-Request` header
  or the per-session `_csrf` form token. Upload routes check the login before a byte of the multipart
  body is read and refuse more than 20 MB (`app/review/routes.py`).
- **Export download** (`GET /items/{id}/export`) changes state and writes a zip under `EXPORT_DIR`
  behind the session cookie alone; accepted because the cookie is `SameSite=Strict` (a cross-site link
  never carries it) and every template autoescapes (`app/review/filters.py`), so no injected same-site
  link exists. Check: `tests/test_review_queue.py::test_export_without_the_session_cookie_is_refused`.
- **One path to the model**: only `app/llm/guard.py` calls a client; it tokenizes names (the clinic's
  and the payer's spelling), member ids, account numbers, addresses, DOBs, claim numbers, provider
  names and dates, masks SSN/e-mail/phone-shaped values it cannot tokenize, scans the rendered prompt
  and refuses it (`RedactionError`, audited) when anything is left. Check:
  `uv run pytest tests/test_redaction.py tests/test_redaction_e2e.py -q`.
- **Untrusted text**: payer letters and note excerpts are scanned for planted instructions on import
  and again when a context is built (`app/redact/injection.py`); a flagged document or a model answer
  reporting injection can never override the rules' category (`app/classify/model_classifier.py`), a
  flagged note is never quoted (`app/playbooks/base.py`, `app/drafting/appeal.py`), and the work item
  carries `injection_flagged`. Check: `uv run pytest tests/test_injection.py tests/test_injection_unit.py -q`.
- **Logs**: `configure_logging` installs the masking filter on every root handler; exception messages
  are never logged (frames and class name only) by the CLI, inline jobs, the rq worker
  (`python -m app.jobs.worker`) and uvicorn (`denial-agent serve` passes `log_config=None` so uvicorn's
  loggers propagate to the masked handler). Check: `uv run pytest tests/test_phi_in_logs.py tests/test_jobs.py -q`.
- **Audit log** append-only and hash-chained: ORM listeners refuse update/delete on every database,
  the Postgres migration adds `BEFORE UPDATE/DELETE` triggers, `app.audit.verify_chain` walks the chain.
  Check: `denial-agent verify-audit` prints `OK` (exit 1 when broken); `/audit` verifies on every page
  view; `/api/health` reports the verdict from a 60-second cache.
- **No submission**: approved items become an export package (`app/review/export.py`,
  `summary.json` carries `"submitted_by_software": false`); marking an item submitted is a human action.
- **Coding changes**: every code or modifier edit is `coding_change=True` and cannot be approved
  without a quoted note excerpt (`app/review/service.py` `approve`); an excerpt typed by the reviewer
  is stored with source `reviewer` and labelled as such on the item page and in `summary.json`.
- **JSON API** `GET /api/items/{id}` leaves out letters, note text and the patient block and passes
  the remaining free text through the claim's identifier tokenizer (`app/api/routes.py`).
- **Pilot termination**: `denial-agent purge-clinic <id> --confirm` deletes every uploaded file under
  `DATA_DIR/uploads` and every export package under `EXPORT_DIR`, exports the clinic's rows to one zip,
  deletes the rows (every remittance claim, matched or not, loses the patient name and member id the
  payer echoed back) and records counts and file names in the `clinic_purged` audit event; it refuses to
  report destruction when a file could not be removed. On SQLite it runs `VACUUM` afterwards and every
  connection sets `PRAGMA secure_delete=ON` (`app/db.py`), so deleted rows do not stay readable in the
  file; on PostgreSQL the DBA runs `VACUUM FULL` after a purge. The zip is the hand-over copy: delete it
  once delivered. Check: `uv run pytest tests/test_cli.py -q -k purge` (one test greps the SQLite file
  for the purged names).
- **Pseudonymized batches** (`denial-agent anonymize`, `app/anonymize/`, `docs/SPEC.md` section 16): a
  pseudonymized batch keeps dates of service, so it is a limited data set and still PHI: every control
  in this file applies to it as it does to raw data. The command needs no database and runs on the
  clinic's own computer; it refuses an output folder or key inside the repository and a key inside the
  output folder, never overwrites a key, and writes nothing while a note or letter still carries an
  SSN-, phone-, e-mail- or DOB-shaped value. The key holds the originals: it stays with the clinic and
  never travels with the batch. Check: `uv run pytest tests/test_anonymize.py -q`.
- **Patient pseudonym**: `patients.token` is `PT-` plus six hex characters of
  `sha256("<clinic_id>:<account_number>")`. It is a display label inside the clinic's own system (queue,
  letters without the patient block, API), not a security boundary: anyone with the database can read
  the patient row next to it.
- **Secret scanning**: `.pre-commit-config.yaml` (gitleaks + ruff), `.github/workflows/ci.yml` scans the
  whole history; `tests/fixtures/llm` (recorded model output) is scanned, not allowlisted.

## Documents to write and date (Phase 0)

- [ ] Risk analysis
- [ ] Security policy (short)
- [ ] Incident response plan (who is called, breach-notice timing from the BAA)
