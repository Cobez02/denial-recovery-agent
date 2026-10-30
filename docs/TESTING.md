# Testing plan → test suite

The plan's testing table, mapped to the automated suite. Everything here runs on every code change
with no Docker, Redis, network or API key: `tests/conftest.py` points every test at a fresh SQLite
database, the deterministic stub model client and inline jobs, clears every `Settings` variable
first and makes `get_settings()` skip the operator's `.env` (`APP_ENV=test`), so nothing on the
machine changes what the suite tests.

```bash
uv run pytest -q                                   # the whole suite (pyproject addopts: -q -p no:cacheprovider)
uv run pytest tests/test_classifier_set.py -q -s    # one file, with the per-category output
```

On this machine (Windows, path with a space) the same thing is
`"C:/Users/conno/Denial Agent/.venv/Scripts/python.exe" -m pytest tests -q -o addopts=""`.

Counted on Oct 30, 2026 with `pytest tests --co -q`: **851 tests in 28 files**. One of them
(`tests/test_prompt_regression.py::test_live_model_outputs_are_schema_valid_and_faithful`, marker
`live_llm`) skips unless `ANTHROPIC_API_KEY` and `LLM_PROVIDER=anthropic` are set; nothing else skips.
The numbers move as the suite grows; the commands above regenerate them.

## The plan's testing table

| Plan test | Phase | What the tests check | Pass when | Test files |
|---|---|---|---|---|
| Parser golden files | 1 onward | 835 parsing and the claim-export import against checked-in expected output; member ids under every NM1 qualifier with the NM1*IL fallback; duplicate-safe re-imports that re-link remittance lines | Exact match on every field | `tests/test_parser_golden.py`, `tests/test_claims_import_golden.py`, `tests/test_remit_import.py` |
| Classifier test set | 1 onward | Category and expected action on the 150 labeled denials of `synthetic/fixtures/labeled_set_v1/labels.json` (rules alone, stub model), plus every rule in `app/classify/rules.yaml` and the code catalog | >= 0.90 category agreement and >= 0.80 action agreement, asserted in the test and never lowered (today 150/150 and 150/150, see below) | `tests/test_classifier_set.py`, `tests/test_classifier_rules.py`, `tests/test_codes.py` |
| Prompt regression | 1 onward | Fixed inputs to both prompts (`classify_denial.v1`, `draft_appeal.v1`): rendering, schema validity, version hash, the committed replay fixtures fact-checked with `app.drafting.factcheck.verify`; the real model only through the opt-in `live_llm` test | No structure break; no fact in the output that is absent from the inputs | `tests/test_prompt_regression.py`, `tests/test_llm_clients.py`, `tests/test_drafting.py` |
| Redaction | 1 onward | Every prompt that reaches the model client during a full synthetic run is scanned for names (the clinic's and the payer's spelling), member ids, account numbers, addresses and every DOB spelling; the guard refuses a payload that still carries one | Zero identifiers in any captured payload | `tests/test_redaction.py`, `tests/test_redaction_e2e.py` |
| PHI in logs | 1 onward | Log output captured during a full run is scanned raw, and `app.logging_config.MASK_HITS` proves no `app.*` logger ever needed the masking filter; route exceptions, inline jobs, the rq worker and `denial-agent serve` log class names and frames only | Zero identifiers, zero mask hits from application loggers | `tests/test_phi_in_logs.py`, `tests/test_jobs.py`, `tests/test_cli.py` |
| Injection | 1 onward | What the suite proves: planted letters are flagged on import and on the work item; the classification is the same with and without the letter; a deliberately obedient fake guard that answers with the planted category at confidence 0.99 cannot override the rules (`test_obedient_model_never_overrides_the_rules_on_a_planted_document`); planted text never reaches a proposal, a letter or the audit log; flagged note excerpts are never quoted; prompt-section markers inside a note cannot hijack the model narrative | Classification unchanged; document flagged; nothing planted in the output | `tests/test_injection_unit.py`, `tests/test_injection.py`, `tests/test_playbooks.py`, `tests/test_drafting.py` |
| End-to-end synthetic | 1 | The 200-claim batch through HTTP only: imports, queue order and injection banner, a review sample, export packages, mark submitted, follow-up 835s recording outcomes, take-backs (two paid fixes reversed by later 835s reopen, one of them is paid again), dashboard with the taken-back tile, every step in the audit log, no identifier in model payloads or logs (`test_1_...` to `test_9_...`, `test_6b_...` between 6 and 7, in order) | Whole loop completes; audit chain verifies | `tests/test_end_to_end_synthetic.py` |
| Shadow comparison | 2 | Agent versus the biller's real decisions | Phase 2 exit targets | `app.outcomes.metrics.category_agreement` + the weekly review (manual) |
| Live outcomes | 3 | Payer results on submitted fixes | Phase 4 gate | `/dashboard` + the weekly review (manual) |

Other layers the plan implies:

| Layer | Test files | What they pin down |
|---|---|---|
| Audit append-only | `tests/test_audit.py` | ORM and raw-SQL update/delete refused or detected; hash chain tamper detection |
| Review queue | `tests/test_review_queue.py` | Ordering, pagination with exact totals, every action and its status rule, the note-excerpt gate, export packages (and that an export without the session cookie is refused), the JSON API tokenizing free text, the health-check cache, 20 items cleared over HTTP |
| Auth / MFA | `tests/test_auth.py` | Wrong password or code rejected, a TOTP code accepted once, lockout, CSRF and origin checks, anonymous uploads refused before the body is read, oversized uploads refused before parsing |
| Migrations | `tests/test_migrations.py` | `alembic upgrade head` on a fresh SQLite file equals the models (the full `compare_metadata` diff is empty), downgrade and re-upgrade, the Postgres-only audit trigger in the offline SQL |
| Outcomes and metrics | `tests/test_outcomes.py`, `tests/test_metrics.py` | The outcome rule, the date and payer gates, reversals leaving the item submitted, take-backs reopening a closed item (the two passes, the reopen events, the date-before-outcome gate, what can never be reopened, idempotence, a take-back from an 835 without a payment date still leaving recovered dollars), per-work-item counting by latest outcome, reversed fixes and taken-back dollars |
| Pipeline, jobs, matching | `tests/test_pipeline.py`, `tests/test_jobs.py`, `tests/test_matching.py` | One proposed work item per label with every citation resolving, savepoint isolation of a failing claim, inline and rq job runners, the worker's masked failures, every matching method |
| Playbooks and drafting | `tests/test_playbooks.py`, `tests/test_drafting.py` | Every category's behaviour, deadlines counted from the remittance, corrected-claim builder and diff, letters for every category, fact check, PDF fallback |
| Synthetic data | `tests/test_synthetic.py` | Determinism, the committed fixture reproduced byte for byte, 835 balancing, labels versus adjustments, follow-up amounts following the outcome rule |
| Pseudonymized real data | `tests/test_anonymize.py` | `denial-agent anonymize` on batches the tests generate themselves: no original name, member id, account number, date of birth or address in any output byte; zero unmatched remittances and the same work items, categories, actions, flags, dollars and deadlines from two databases (with and without `--replace-claim-numbers` and `--shift-dates`); every 835 parses to the original once the key's values are put back; the key restores the claim export; `--seed` reproducible; a planted phone stops the run without echoing it; repository paths refused; subscriber, corrected-name, employer and REF ids in the 835; other delimiters and CRLF; a later batch (follow-up 835s and a letter) pseudonymized with `--key-in` matches the earlier claims and records the same outcomes as the raw one, and mismatched options or foreign keys are refused; `deanonymize` turns the pseudonymized database's corrected-claim and appeal packages into the raw database's with no pseudonym left, renders the PDF again, and refuses leftovers, foreign keys, unknown members, repository paths and overwrites |
| CLI | `tests/test_cli.py` | Every command including `list-payers`/`set-payer`, exit codes (`partial` pipeline runs exit 1), `serve` refusing a placeholder `SECRET_KEY` with a readable error, `purge-clinic` deleting uploads and export packages, refusing to report destruction when a file survives, clearing unmatched CLPs and leaving no purged name in the SQLite file |

## Milestone done-when checks that are tests

| Milestone | Test |
|---|---|
| M1 app starts with one command | `scripts/bootstrap.ps1` / `scripts/bootstrap.sh` (manual); every command in `tests/test_cli.py` |
| M2 migrations run cleanly | `tests/test_migrations.py::test_upgrade_head_matches_models` |
| M3 200-claim batch imports with zero unmatched remittances; re-import is a no-op | `tests/test_pipeline.py::test_m3_import_done_when`, `tests/test_end_to_end_synthetic.py::test_1_imports_through_the_ui` |
| M4 90% on the labeled set | `tests/test_classifier_set.py::test_category_accuracy_meets_bar` (and `::test_expected_action_agreement_meets_bar`, bar 0.80) |
| M5 no identifiers reach the model | `tests/test_redaction_e2e.py::test_no_identifier_reaches_a_model_payload`, `tests/test_end_to_end_synthetic.py::test_9_no_identifier_reaches_the_model_or_the_logs` |
| M6 every proposal cites the codes and note text it relies on | `tests/test_pipeline.py::test_process_pending_creates_one_proposed_work_item_per_label` (every citation resolves on the 150-item batch), `tests/test_playbooks.py` |
| M7 a reviewer clears 20 items without touching the database | `tests/test_review_queue.py::test_reviewer_clears_twenty_items_over_http` |
| M8 full loop (take-backs after the close included), every step logged, audit append-only | `tests/test_end_to_end_synthetic.py` (`test_6b_payments_taken_back_and_repaid`), `tests/test_audit.py`, `tests/test_outcomes.py`, `tests/test_metrics.py` |

## Tests per file

From `pytest tests --co -q` on Oct 30, 2026 (851 in all).

| File | Tests | Covers |
|---|---|---|
| `tests/test_anonymize.py` | 24 | `denial-agent anonymize`: no identifier left, same processing in two databases, 835 parse and key round trip, determinism, residual check, refusals, a later batch with `--key-in`; `deanonymize` restoring export packages |
| `tests/test_audit.py` | 11 | append-only guards, hash chain, canonical JSON |
| `tests/test_auth.py` | 15 | accounts, login, TOTP replay guard (row-level), lockout, CSRF, upload gates and the declared-size refusal |
| `tests/test_claims_import_golden.py` | 27 | claim export golden file, re-imports, notes and documents with the real injection scanner, payer windows |
| `tests/test_classifier_rules.py` | 177 | every rule, priorities, model merge policy, `classify()` consultation rule |
| `tests/test_classifier_set.py` | 2 | category and action agreement on the labeled set |
| `tests/test_cli.py` | 31 | every `denial-agent` command, `partial` exit code, placeholder-key refusal, purge VACUUM and unmatched CLPs |
| `tests/test_codes.py` | 102 | CARC/RARC catalog coverage |
| `tests/test_drafting.py` | 39 | corrected-claim builder, letters, fact check, model rewrite, PDF fallback |
| `tests/test_end_to_end_synthetic.py` | 10 | the full loop over HTTP, take-backs and the re-payment included |
| `tests/test_injection.py` | 4 | planted letters end to end, obedient model, direct guarded call |
| `tests/test_injection_unit.py` | 31 | every detector pattern, wrapping and marker neutralisation |
| `tests/test_jobs.py` | 15 | inline and rq runners, tasks, masked worker |
| `tests/test_llm_clients.py` | 29 | factory, Anthropic request shape and failure mapping, stub, replay, recording |
| `tests/test_matching.py` | 11 | each matching method and its refusals |
| `tests/test_metrics.py` | 13 | dashboard numbers, per-item counting, reversed fixes and taken-back dollars (totals and JSON), buckets, category agreement |
| `tests/test_migrations.py` | 4 | upgrade/downgrade, model parity, Postgres trigger SQL |
| `tests/test_outcomes.py` | 65 | the outcome rule and its gates (patient-responsibility codes, procedure-scoped lines, reversal status), take-backs reopening closed items and the two passes, what a reversal never reopens, outcomes dated by their remittance when the 835 carries no payment date |
| `tests/test_parser_golden.py` | 25 | 835 parser golden files and edge cases |
| `tests/test_phi_in_logs.py` | 3 | raw log scan, route exceptions, uvicorn folding |
| `tests/test_pipeline.py` | 4 | M3 done-when, one work item per label, audit trail and flags, idempotence and regenerate |
| `tests/test_playbooks.py` | 66 | every category's playbook, deadlines, priority, citations |
| `tests/test_prompt_regression.py` | 14 | prompt rendering, schema, stub and replay fidelity, opt-in live model |
| `tests/test_redaction.py` | 43 | registry, tokenizer, scanner, generic masks, guard |
| `tests/test_redaction_e2e.py` | 1 | zero identifiers in every payload of a full run |
| `tests/test_remit_import.py` | 27 | 835 import, denial detection, line linking, outcomes hook, savepoints |
| `tests/test_review_queue.py` | 29 | queue, item page, actions, export, API, health cache, partial-run notice, 20-item run |
| `tests/test_synthetic.py` | 29 | generator, fixture, 835 writer, follow-ups |

Fixtures: `tests/fixtures/835/` and `tests/fixtures/claims/` (golden inputs and expected JSON),
`tests/fixtures/llm/` (recorded model fixtures for the replay client; scanned by gitleaks),
`synthetic/fixtures/labeled_set_v1/` (the committed 200-claim batch). `tests/conftest.py` provides
`settings`, `engine`, `session`, `clinic`, `app`, `client` (logged in through the real password and
TOTP flow), `anon_client`, `reviewer`, `synthetic_batch` and `captured_llm_payloads`; `tests/helpers.py`
runs the whole batch once per module for the heavier suites.

## Classifier set, current output

`pytest tests/test_classifier_set.py -q -s` on Oct 28, 2026:

```
category accuracy 1.000 (150/150), bar 0.90
  authorization 19/19   bundling 19/19   coordination_of_benefits 9/9   duplicate 12/12
  eligibility 17/17     medical_necessity 14/14   missing_information 28/28   modifier 19/19
  other 3/3             timely_filing 10/10
expected-action agreement 1.000 (150/150), bar 0.80
  appeal 40/40   clinic_question 41/41   corrected_claim 47/47   no_action 22/22
```

The labels are the generator's ground truth, not a human judgement; the hand-check the plan asks
for is still owed (`synthetic/fixtures/labeled_set_v1/REVIEW.md`). A perfect score on a set the
same code base generated says the rules match the profile, not that the profile matches reality.

## Metric definitions

From the plan, as implemented in `app/outcomes/metrics.py` (counted per work item; a work item with
several outcomes stands for its latest one):

- submitted fixes = work items the reviewer marked submitted (`submitted_at` set), still submitted or
  closed since; an exported package never marked submitted is not a fix;
- overturn rate = fixes whose latest outcome is `paid` or `partial` / submitted fixes;
- recovered dollars = the latest outcome's `paid_amount` over fixes paid or partially paid;
- reversed fixes = submitted fixes whose latest outcome is `reversed` (a payment taken back, or the
  original adjudication reversed, with the re-adjudication still awaited): in the denominator, counted
  neither paid nor partial, nothing recovered;
- taken-back dollars = over those fixes, the `paid_amount` of the most recent paid or partial outcome
  before the reversal (0 when nothing had been paid); totals only, shown on the dashboard as
  "Taken back (N fix(es) awaiting re-adjudication)";
- review time = mean `ReviewAction.review_seconds` / 60, where `review_seconds` = `decided_at` -
  `first_viewed_at` and a reopen clears `first_viewed_at` so the next decision measures the new review;
- category agreement (Phase 2) = share of work items whose category equals the biller's label
  (`category_agreement(session, labels)`).

## What is not automated

- The Docker Compose stack (Postgres 16, Redis, the app and worker containers, healthchecks, WeasyPrint
  in the image): defined in `docker-compose.yml` and `Dockerfile`, not run by the suite and not run on
  the development machine so far. The Postgres append-only trigger is checked only as offline SQL
  (`tests/test_migrations.py::test_offline_sql_adds_audit_trigger_only_on_postgres`).
- PDF rendering: `render_pdf` returns `None` when WeasyPrint is not importable; the tests then assert
  the `README.txt` fallback in the export package.
- The rq worker against a real Redis: unit-tested with fakes (`tests/test_jobs.py`).
- The real model: only the opt-in `live_llm` test.
- Shadow comparison and live outcomes: measured with real data in Phases 2 and 3.
