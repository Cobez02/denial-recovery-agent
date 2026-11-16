# Denial Recovery Agent — local MVP

Reads insurance remittance files (X12 835) and a clinic's claim export, classifies each denial,
proposes a fix (corrected claim, appeal, or a question for the clinic), drafts the appeal letter and
tracks every claim until it is paid or closed. **A person approves every action; the software never
submits anything.** This is Phase 1 of the plan in `docs/PHASES.md`: everything runs locally on
synthetic data until the clinic BAA, the model-provider BAA and the laptop security checklist are done.

- Build specification: `docs/SPEC.md`
- The specialty (chiropractic, chosen 2026-10-29), its payer mix, denial patterns and the coder's
  assumption list: `docs/SPECIALTY.md`
- Plan phases and milestone status: `docs/PHASES.md`
- Testing plan → test files: `docs/TESTING.md`
- Before any real data: `docs/SECURITY_CHECKLIST.md`, `docs/PILOT_CHECKLIST.md`, then
  [Running real data](#running-real-data)
- Every `.env` key: `scripts/env-notes.md`; synthetic data: `synthetic/README.md`

## Quick start (no Docker, no API key)

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). The commands below are the same on
Windows (PowerShell) and Unix shells except where noted; `scripts/bootstrap.ps1` and
`scripts/bootstrap.sh` run steps 1 to 10 for you.

```bash
# 1. Dependencies (everything, including the dev tools, from uv.lock)
uv sync

# 2. Configuration: copy the example and give SECRET_KEY a random value (the server refuses to
#    start with an empty or placeholder key; the CLI commands below do not need one)
cp .env.example .env                                    # Windows: copy .env.example .env
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste the output after SECRET_KEY=

# 3. Database schema (SQLite at data/denial_agent.db by default; `alembic upgrade head`)
uv run denial-agent init-db --migrate

# 4. The clinic (prints {"clinic_id": 1, ...}; the imports use it)
uv run denial-agent create-clinic --name "Pilot Clinic"

# 5. A reviewer account: asks for a password twice (12 characters or more), then prints the TOTP
#    provisioning URI and its QR code exactly once - enrol it in an authenticator app now
uv run denial-agent create-user --email you@example.com --role admin

# 6. Synthetic data (defaults: seed 20261028, 200 claims, the chiropractic profile; --profile picks another)
uv run denial-agent generate-synthetic --out data/synthetic

# 7. The clinic's side: claims, note excerpts, payer letters (creates the payer rows too)
uv run denial-agent import-claims data/synthetic/claims_export.csv --clinic-id 1
uv run denial-agent import-notes data/synthetic/notes.csv --clinic-id 1
uv run denial-agent import-documents data/synthetic/payer_documents.csv --clinic-id 1

# 8. Optional: per-payer filing, corrected-claim and appeal windows. New payers get the defaults
#    (90 / 90 / 180 days); set the real windows before the 835s create work items. The synthetic
#    profile's payers are GBMB1 (Medicare Part B), GPMA2 (Medicare Advantage), SWPPO3 (a PPO, the
#    defaults) and TLMC4 (Medicaid):
uv run denial-agent list-payers
uv run denial-agent set-payer --payer-id GBMB1 --timely-filing-days 365 --corrected-claim-days 365 --appeal-days 120
uv run denial-agent set-payer --payer-id GPMA2 --timely-filing-days 365 --corrected-claim-days 365 --appeal-days 65
uv run denial-agent set-payer --payer-id TLMC4 --timely-filing-days 180 --corrected-claim-days 180 --appeal-days 60

# 9. The payers' side: one 835 per payer. Each import matches the CLPs to claims, detects the
#    denials and runs the pipeline on them (work items with classified, cited proposals)
uv run denial-agent import-835 data/synthetic/remit_GBMB1.835
uv run denial-agent import-835 data/synthetic/remit_GPMA2.835
uv run denial-agent import-835 data/synthetic/remit_SWPPO3.835
uv run denial-agent import-835 data/synthetic/remit_TLMC4.835

# 10. Anything still pending (for example after a pipeline failure, which leaves the claim queued)
uv run denial-agent process

# 11. The web app on http://127.0.0.1:8000 (bind to 127.0.0.1 only; `--host`/`--port` to change)
uv run denial-agent serve
```

Every command prints ids, counts and file names only (never names or note text) and exits 0 on
success, 1 on a user-facing error, a failed import batch, a failed pipeline run or a broken audit
chain, 2 on an unexpected exception (class name and frames, never the message). `uv run denial-agent
<command> --help` lists the flags; `python -m app.cli` is the same program.

### Logging in

`/login` asks for the email and password, then `/login/mfa` for the current 6-digit code from the
authenticator app, and lands on `/queue`. Five failed attempts (password or code) lock the account
for 15 minutes; a code is accepted once, so a code seen over a shoulder cannot be replayed. The
session is a signed `HttpOnly`, `SameSite=Strict` cookie that expires after 8 hours.

### The pages

| Page | What it is for |
|---|---|
| `/queue` | The work list: deadline first (none last), then dollars, then confidence; filters for status, category, payer and flag; 100 rows per page with exact totals; deadline badges at 30 and 14 days. Closed and rejected items are hidden unless you pick them. |
| `/items/{id}` | One denial: claim facts, remittance adjustments with the CARC/RARC text, payer letters (with a warning when planted instructions were found), note excerpts (flagged ones are never quoted), the current proposal with its citations, the corrected-claim before/after diff or the appeal letter. The first view moves the item to `in_review`. Actions: approve, edit (reason required), reject (reason required), attach a quoted note excerpt, regenerate, close, reopen, mark submitted. |
| `/imports` | Upload a claim export, notes, payer documents or an 835 (20 MB limit), run the pipeline on pending denials, re-match unmatched remittance claims, see every batch and the unmatched CLPs. |
| `/dashboard` | Items by status, dollars at stake, submitted fixes, overturn rate, recovered dollars, taken back (payments the payer reversed after the fix was closed, with the number of fixes awaiting their re-adjudication), review minutes, by category and by payer, deadline buckets, model calls. |
| `/audit` | The append-only audit log, 100 rows per page, filterable by claim or action, with the hash-chain verification result. |
| `/api/health`, `/api/metrics`, `/api/items/{id}` | JSON. Health is public (database reachable, audit chain verified at most once a minute); the others need the login. The item JSON carries no patient identifiers: free text is tokenized, letters and note text are left out. |

### Approve, export, submit, mark submitted

Approving a proposal moves it to `approved`. A proposal that changes a code or modifier is flagged
`coding_change` and cannot be approved until a quoted note excerpt is attached (typed on the item
page or in the approve form; the page and the export say that it was typed by the reviewer).
**Download export package** on an approved item writes a zip under `EXPORT_DIR` (`data/exports`)
and moves the item to `exported`: `summary.json`, `corrected_claim.json` and `corrected_claim.csv`
(claim-export columns) for a corrected claim, `appeal_letter.txt`, `.html` and `.pdf` (or a
`README.txt` when WeasyPrint is not installed) for an appeal, `clinic_question.txt` for a question,
and `audit_excerpt.json`. A person sends the corrected claim or the appeal through the clinic's own
channel, then uses **Mark submitted** with the date and the control number it went out under (the
default for a corrected claim is `<original>-R1`). Nothing in this software talks to a payer or a
clearinghouse.

### Closing the loop

When the payer answers, import the later 835 (`import-835` or the imports page). Each CLP that
matches a submitted work item (same claim, or the corrected claim's control number) records an
outcome: `paid` when no denial adjustment remains on the disputed lines or the claim, or the payment
reaches 95% of the work item's dollar value; `partial` when something is paid but a denial remains;
`denied_again` when nothing is paid; `reversed` for CLP02 22. Paid, partial and denied again close
the item; a reversal leaves it submitted for the re-adjudication that follows. A reversal that
arrives after a payment closed the item (a take-back) reopens it: the item returns to submitted,
the claim to the status its submission gave it (corrected or appealed; otherwise it is left as it
was), the audit log records the reopen, and the payment leaves recovered dollars (the dashboard
shows it as taken back) until a later 835 pays the claim again. A remittance dated before the
submission, or before the payment it would take back, or from another payer, is ignored. The
dashboard counts each work item once, by its latest outcome. For a synthetic run,
`synthetic.followup.generate_followup_files` writes the follow-up 835s (see `synthetic/README.md`).

## Running real data

Real files come only after the steps before "the Clinic sends its files" in the order of execution
in `docs/compliance/README.md` are done (the clinic BAA and the Pilot Agreement signed among them)
and `docs/SECURITY_CHECKLIST.md` is complete and dated; pseudonymizing a batch does not change that.
A pseudonymized batch still carries dates of service, so it is a HIPAA limited data set: still PHI,
still covered by the BAA, moved only through the secure transfer service to the encrypted disk of
the checklist. Pseudonymizing is this pilot's standard procedure, not an option: the clinic
pseudonymizes every batch before it uploads it and restores every package before it submits it.
`denial-agent anonymize` is defence in depth on top of those controls: it turns one raw batch into
a copy the app processes as before (same remittance matches, same work items, categories and
actions) with every patient identifier replaced, and it writes nothing unless a final sweep of
every output byte finds none of the originals. `denial-agent deanonymize` puts the real identifiers
back into the export packages before anything is submitted. The clinic's workforce runs both on the
clinic's own computer, where the raw files and the key stay; only the pseudonymized folder leaves
it, uploaded through the secure transfer service (the Pilot Agreement's Secure Transfer Method;
never by e-mail or on removable media), and the marked packages come back the same way.

**Install on the clinic's computer** (no copy of the repository, no git credentials, no dev tools):
the clinic's workforce installs both commands there, from a wheel the Company supplies, and runs
them; the Company never runs the commands, never handles the key and never receives the raw files
or the restored packages. On the Company machine,

```bash
uv build --wheel
```

builds `dist/denial_agent-0.1.0-py3-none-any.whl` (it fetches the build backend from PyPI once).
On the clinic's computer:

```bash
uv tool install ./denial_agent-0.1.0-py3-none-any.whl
```

`uv tool install` downloads the runtime dependencies from PyPI (and Python 3.12 or later if the
computer has none), so it needs internet once. Without internet, download the wheels on a connected
machine (`uv export --no-dev --format requirements-txt` gives the list) into a folder and install with
`uv tool install --offline --find-links WHEELS ./denial_agent-0.1.0-py3-none-any.whl`. The PDF
renderer (WeasyPrint, the `pdf` extra) needs system libraries; without it a restored appeal carries
`appeal_letter.html` and a `README.txt` instead of a PDF: open the HTML in a browser and print it.
From an installed wheel, the "inside the repository" check compares against the installed package
folder; keep batches and keys out of any checkout anyway.

**Each upload** (the backlog first, then once a week in the live phase, as the Pilot Agreement
requires; one run per upload, on the clinic's computer):

```bash
# The first batch: a new key, and its folder becomes the clinic's key folder. The output folder is
# a local folder apart from the key folder; the raw folder and the key folder stay on this computer
denial-agent anonymize D:/clinic/raw/2026-11-02 --out D:/clinic/to-company/2026-11-02 --key-out D:/clinic/keys/2026-11-02.key.json --replace-claim-numbers

# Then upload D:/clinic/to-company/2026-11-02, as anonymize wrote it, through the secure transfer
# service (never by e-mail, never on a USB drive)

# Every later batch (the next week's files, including the follow-up 835s that pay or deny submitted
# fixes): the newest key in, a new key out, in the same key folder; then upload the output folder
denial-agent anonymize D:/clinic/raw/2026-11-09 --out D:/clinic/to-company/2026-11-09 --key-in D:/clinic/keys/2026-11-02.key.json --key-out D:/clinic/keys/2026-11-09.key.json --replace-claim-numbers

# Before anything goes out: restore the export packages downloaded from the secure transfer
# service, with the newest key
denial-agent deanonymize D:/clinic/from-company/PSEUDONYMIZED_work_item_14_KXW938899_v1_20261112T210501Z.zip --key D:/clinic/keys/2026-11-09.key.json --out D:/clinic/to-submit
```

- **Use `--replace-claim-numbers` for every real batch.** Practice-management systems often build
  claim numbers from the account number; a run whose claim numbers carry an account number or
  member id is refused without the flag.
- **Input**: the claim export CSV, optionally `notes.csv` and `payer_documents.csv`, and any number
  of 835 files, recognised by their content and named in every message by their position in the
  folder's sorted listing ("input file 3"), never by name. Headers are compared without case; a CSV
  that looks like one of these but lacks a column is refused with the missing column names, and a
  row with more or fewer fields than its header (an unquoted comma) is refused with its row number.
  A file is read as UTF-8, else as Windows-1252 (Excel's CSV; counted); damaged text (U+FFFD) is
  refused. An X12 file is used only when it is an 835 throughout: an 837, a 277CA or a 271 is
  skipped, never copied, and listed. Anything else, and any subfolder, is skipped and listed too.
- **Replaced, consistently in every file**: each patient's names (invented names, one pair per
  patient, never a provider's or a payer's word), account number and member id (same shape), date of
  birth (same year; the day moves), address line, city and ZIP, and the identity columns of the
  export the contract drops (insured, guarantor, secondary ids) wherever they reappear; in the 835
  the patient, subscriber and corrected names and ids, patient REF ids, and member ids Medicare puts
  inside PLB references; in notes and letters every patient of the batch, also when a note about one
  patient names another, with accents, apostrophes, joined or split surnames, ids written with
  spaces or dashes, every way a date of birth is written and USPS street abbreviations. A name that
  is an ordinary word ("May", "Low") is replaced only where it names someone ("Mrs. Low", "May
  Fall"), never in "Low back pain". Provider and payer names stay as they are.
- **Stops on leftovers**: a note or letter that still carries an SSN, phone, e-mail, a date after
  "DOB", an MBI, or an id after an MRN, chart, account, policy, member, subscriber, ID#, HICN,
  Medicare or Medicaid label stops the run with exit 1 and a list of file, row, field and kind
  (never the value). Fix those rows, or pass `--allow-residual` to mask each one as a whole value.
  Names the batch does not carry (a relative who is not a patient) cannot be found: read the notes.
- **Stops on any leak**: before writing, every output byte and file name is searched for every
  original value the key holds; one hit and nothing is written (that is a gap of the tool: report
  the place it prints, never the value, and do not send the batch).
- **Written last and all at once**: the output goes to a temporary folder renamed at the end, the
  key last, with owner-only permissions (`icacls` on Windows), never over an existing file; a failed
  run leaves neither behind.
- **The key and its chain**: the key is the only file with original values. It stays in one folder
  on the clinic's computer and is never uploaded: never in the repository, never in OneDrive
  (Windows often redirects Documents and Desktop there), Dropbox, Google Drive, iCloud or Box,
  never inside or beside the output folder and never on a removable or network volume the output
  is written to; the command refuses all of those places. Each key extends the one before it (same
  chain, next run number) and the run log `denial-agent-runs.log` in the key folder records every
  run: an older key of the chain is refused for the next batch and for restores, and a key of
  another clinic (no billing NPI in common) is refused. Keep dated backup copies of the key folder
  on clinic media; only the newest key is used.
- **Marked until restored**: the claims file anonymize writes carries one extra, last column after
  the contract's columns, `pseudonymized_batch` = `yes`; the claims importer records it on every
  claim of that import, so the marks do not depend on the file's name (the upload folder is still
  sent as anonymize wrote it). The app marks every package it exports for such a claim:
  `PSEUDONYMIZED_` in front of the zip name, a banner on top of the letter and the clinic question,
  `"pseudonymized": true` in `summary.json`, a marker column in the corrected claim CSV. A marked
  package never goes to a payer.
- **Restoring**: `deanonymize` refuses a package it cannot vouch for: one from another key or run,
  one already restored, one whose patient block an older key cannot map, one naming another patient,
  one whose provider name is the patient's pseudonym. It writes the restored packages (marks
  removed, real patient block, ids, claim numbers, letter text; the HTML and PDF rebuilt) under the
  app's package name with the real claim number, never into the folder the packages came in, never
  over an existing file. The audit excerpt is left out (it is the app's record, in pseudonyms; the
  original package keeps it). A `[[MASKED_...]]` marker cannot be restored: the command lists it,
  fill it in by hand.
- **Test-only options**: `--test-only` with `--seed N` (a reproducible run) or `--shift-dates DAYS`
  (every date moved) is for synthetic data. Neither is allowed on a real batch: a seed makes the
  date-of-birth offsets replayable, and the app compares deadlines and outcomes with the real today.

Neither command has a database, so neither records audit events; the run log is their record and
the import of the pseudonymized folder is audited like every import. The full rules, every check
and the known limits are in `docs/SPEC.md` section 16.

## Model configuration

With the example `.env` the model client is the offline **stub** (`LLM_PROVIDER=stub`): the rules
engine classifies every denial and appeal letters come straight from the templates (the stub returns
the template narrative unchanged). To use the real model:

- `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` from the **HIPAA-ready organization only**, as a
  process environment variable or in the git-ignored `.env`. There is no keychain integration; if you
  keep the key in an OS secret store, your shell profile has to export it. The key is never logged.
- `LLM_MODEL` is pinned (`claude-opus-5-5` by default). Run `uv run pytest tests/test_prompt_regression.py`
  before changing it; with a key set, the opt-in `live_llm` test records and fact-checks real output.
- `LLM_SERVER_FALLBACKS=false` stays off: a server-side fallback would answer with a model other than
  the pinned one.
- The model is consulted only when the rules are unsure (confidence below `CLASSIFY_MODEL_THRESHOLD`,
  0.75), landed on `other`, or a payer letter is attached; it may rewrite the appeal narrative only
  when `MODEL_ENABLED_FOR_DRAFTING=true` and the fact check accepts the rewrite. Every call goes
  through `app/llm/guard.py`: identifiers are tokenized first, the rendered prompt is scanned, and a
  letter or note that carries planted instructions can never override the rules' category.

`LLM_PROVIDER=replay`, `LLM_RECORD` and `LLM_REPLAY_DIR` serve the test suite (recorded fixtures under
`tests/fixtures/llm`); do not record against real data.

## Docker Compose (app + worker + Postgres 16 + Redis)

```bash
cp .env.example .env            # set SECRET_KEY (generated as above) and POSTGRES_PASSWORD=<letters and digits>
docker compose up --build       # db, redis, app on http://127.0.0.1:8000, one RQ worker
docker compose exec app denial-agent create-clinic --name "Pilot Clinic"
docker compose exec app denial-agent create-user --email you@example.com --role admin
```

`docker compose up` refuses to start without `POSTGRES_PASSWORD`; the app container refuses to start
with an empty or placeholder `SECRET_KEY`. Every published port is bound to `127.0.0.1`. The app
container runs `denial-agent init-db --migrate` and then `denial-agent serve`; the worker runs
`python -m app.jobs.worker` (not `rq worker`: it logs failures as class names and frames only). Postgres
keeps its data in the named volume `pgdata`; `./data` is mounted at `/app/data` for uploads and
export packages, so files you want to import from the command line go under `./data` first, for
example `docker compose exec app denial-agent import-835 data/synthetic/remit_GBMB1.835`. The
compose stack is defined and reviewed but is not exercised by the automated tests (see
`docs/PHASES.md`).

## Tests

```bash
uv run pytest -q
```

1014 tests across 28 files at the time of writing (the opt-in `live_llm` test skips without an API
key), all on SQLite with the stub model client and inline jobs: no Docker, Redis, network or API key. `docs/TESTING.md` maps the plan's testing table
and every milestone's done-when check to the test files and gives the per-file counts. The labeled
synthetic set (chiropractic, `synthetic/fixtures/labeled_set_v2`) classifies at 150/150 categories
and 150/150 expected actions (bars 0.90 and 0.80; never lower them).

## Secret scanning

```bash
uv run pre-commit install      # gitleaks + ruff before every commit; the bootstrap scripts do this too
```

The gitleaks hook builds the scanner on its first run (needs a Go toolchain); on a machine with the
`gitleaks` binary on PATH, `scripts/precommit-gitleaks.sh` is the plain git-hook alternative. CI
(`.github/workflows/ci.yml`) runs gitleaks over the whole history, `ruff check`, `ruff format --check`
and the test suite on every push.

## Ending a pilot

`uv run denial-agent purge-clinic 1` prints what would go; `--confirm` deletes every uploaded file
under `DATA_DIR/uploads` and every export package under `EXPORT_DIR`, exports the clinic's rows to one
zip, deletes the rows (remittances stay as the payment ledger without the patient names and member
ids the payer echoed back), records the destruction (counts and file names) in the audit log and, on
SQLite, runs `VACUUM` so the deleted rows' bytes leave the file; on PostgreSQL have the DBA run
`VACUUM FULL`. The zip is the hand-over copy for the clinic: deliver it, then delete it.

## Ground rules enforced in code

See `docs/SPEC.md` section 0 and section 15: no submission; coding changes flagged and gated on a
quoted note excerpt; minimum-necessary, tokenized model calls through one guard; payer letters and
note excerpts treated as untrusted data; identifiers never in logs; an append-only, hash-chained
audit log of every action by a person or an agent (one exception: `anonymize` and `deanonymize`,
which the clinic runs on its own computer, record no audit events; each completed run goes to the
run log in the clinic's key folder, and the import of a pseudonymized batch is audited like every
import); a placeholder `SECRET_KEY` refused; `purge-clinic` for pilot termination.
