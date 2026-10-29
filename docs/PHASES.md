# Plan phases, milestones and what this repository delivers

Source: "Denial Recovery Agent: Local MVP to Pilot Plan" (Oct 28, 2026). About 20 weeks to the
proof-of-concept gate, roughly 26 weeks to a cloud pilot. Status below is as of Oct 28, 2026.

## Phase 0: Groundwork (weeks 1 to 3) — people and paper, not code

| Task | Output | Done when | Status |
|---|---|---|---|
| Pick one specialty | Written choice with its top 3 payers and common denial codes | Agreed with the pilot clinic | open — `synthetic/profiles/dermatology.yaml` is an example profile; replace it and regenerate the labeled set |
| Recruit the pilot clinic | Named contact, biller, weekly review slot | Owner says yes in writing | open |
| Legal documents | Pilot agreement and BAA, reviewed by a healthcare attorney | Both signed by the clinic owner | open — `docs/PILOT_CHECKLIST.md` |
| Model access | HIPAA-ready API organization or cloud model account | BAA signed and confirmed active | open |
| Insurance | Errors-and-omissions and cyber policies | Certificate on file, shared with the clinic | open |
| Security basics | Risk analysis, short security policy, incident response plan | Documents written and dated | open |
| Harden the laptop | Every item in the local security checklist | Checklist complete and dated | open — `docs/SECURITY_CHECKLIST.md` |
| Coding advice | 2 to 4 hours with a certified coder on the specialty's denial patterns | Notes captured as rule inputs | open — review `app/classify/rules.yaml` and `app/playbooks/modifier_rules.yaml` with the coder |

## Phase 1: Local build on synthetic data (weeks 2 to 8) — this repository

| Milestone | What gets built | Done when | Where | Status |
|---|---|---|---|---|
| M1 Skeleton | Repo, Docker Compose (app, worker, Postgres, Redis), config, secret scanning on every commit | App starts locally with one command | `pyproject.toml`, `docker-compose.yml`, `Dockerfile`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `scripts/bootstrap.*`, `app/cli.py` | built; the CLI and bootstrap path are tested here (`tests/test_cli.py`); the Compose stack is defined but has not been run on this machine |
| M2 Data model | Tables for clinics, payers, tokenized patients, claims, claim lines, remittances, adjustments, work items, proposals, actions, outcomes, audit log | Migrations run cleanly; synthetic data loads | `app/models.py`, `app/migrations/` | built; `tests/test_migrations.py` proves `alembic upgrade head` equals the models on SQLite; the Postgres append-only trigger is checked as offline SQL only |
| M3 Ingestion | 835 parser, claim-export importer, remittance-to-claim matching, duplicate-safe imports | A 200-claim synthetic batch imports with zero unmatched remittances | `app/ingest/` | built and tested (`tests/test_pipeline.py::test_m3_import_done_when`, golden files) |
| M4 Classification | Rules engine mapping CARC/RARC combinations to categories; the model only for ambiguous or free-text cases, never overriding the rules on a flagged document | 90% or better accuracy on the labeled synthetic set | `app/classify/` | built; 150/150 categories and 150/150 expected actions on the generator's labels (`tests/test_classifier_set.py`); the hand-check of those labels is still owed |
| M5 Redaction layer | Swaps identifiers for tokens before every model call and restores them after; one guard | Automated test proves no names, member IDs, addresses or birth dates reach the model | `app/redact/`, `app/llm/guard.py` | built and tested (`tests/test_redaction_e2e.py`, `tests/test_redaction.py`) |
| M6 Fix proposals and drafting | Playbook per category; corrected-claim builder with a before/after diff; appeal letters from templates, claim facts and quoted note excerpts; fact-checked model rewrite | Every proposal cites the codes and note text it relies on | `app/playbooks/`, `app/drafting/` | built; every citation on the 150-item batch resolves (`tests/test_pipeline.py`); the letter templates and `modifier_rules.yaml` still need a coder's and a biller's review |
| M7 Review queue | Work list sorted by deadline and dollar value; approve, edit or reject with a reason; export package; imports page | A reviewer clears 20 synthetic items without touching the database | `app/review/`, `app/auth/`, `app/templates/` | built and tested (`tests/test_review_queue.py::test_reviewer_clears_twenty_items_over_http`) |
| M8 Outcomes and audit | Matches later 835s to submitted fixes; dashboard; append-only audit log; login with MFA | Full loop runs on synthetic data, every step logged | `app/outcomes/`, `app/audit/`, `app/api/` | built and tested (`tests/test_end_to_end_synthetic.py`, `tests/test_outcomes.py`, `tests/test_audit.py`) |

`docs/TESTING.md` maps the plan's testing table to the test files (793 tests on Oct 28, 2026, all
green on SQLite with the stub model). An adversarial review on the same day produced the fixes now in
the code (outcome rule, deadlines, note scanning, injection policy, purge-clinic disk cleanup, worker
and uvicorn log masking, placeholder `SECRET_KEY` refusal, per-payer windows, pagination, TOTP replay
guard); `docs/SPEC.md` describes the result.

### Untested on this machine

- `docker compose up`: Postgres 16 with the `pgdata` volume, Redis, the app container (migrations
  then `denial-agent serve`), the worker container (`python -m app.jobs.worker`), the healthchecks and
  WeasyPrint inside the image. The files are reviewed, not run.
- The Postgres `audit_log` trigger live (only its SQL is tested).
- PDF rendering of appeal letters (WeasyPrint is not installed in the local venv; the export package
  then carries `README.txt` instead of `appeal_letter.pdf`).
- The real model API (the `live_llm` test needs a key from the HIPAA-ready organization).

### Still human work before Phase 2

- Hand-check the labels in `synthetic/fixtures/labeled_set_v1/` (`REVIEW.md`): the 150/150 scores
  compare the rules with the generator's own ground truth.
- Coder review of `app/classify/rules.yaml` (48 rules) and `app/playbooks/modifier_rules.yaml`; biller
  review of the letter templates in `app/templates/letters/`.
- Pick the specialty and replace `synthetic/profiles/dermatology.yaml`; regenerate as `labeled_set_v2`.
- Enter each real payer's filing, corrected-claim and appeal windows with `denial-agent set-payer`
  (new payers get 90 / 90 / 180 days).
- Run the prompt regression and the `live_llm` test against the pinned model with the real key, and
  keep `LLM_SERVER_FALLBACKS=false`.
- Every Phase 0 item above (clinic, BAAs, insurance, security documents, laptop checklist).

## Phase 2: Shadow mode (weeks 8 to 11)

The agent works real, already-handled denials; nothing it produces is sent anywhere. Weekly: compare
the agent's category and action with the biller's, turn each disagreement into a rule, a prompt fix or
a labeled test case. Exit when the targets hold for two straight weekly reviews and the clinic owner
agrees in writing to the live pilot. Supporting code: `app.outcomes.metrics.category_agreement`.
Because the backfill imports older 835s, the outcome matcher ignores a remittance dated before the
fix was submitted, so already-handled denials do not close shadow-mode items by accident.

| Measure | How it is counted | Target to exit |
|---|---|---|
| Category agreement | Agent's denial category matches the biller's | 80% or better |
| Action agreement | Agent's proposed action matches what worked, or the biller says it would have worked | 70% or better |
| Usable drafts | Appeals and corrections the biller would send with only minor edits | 70% or better |
| Missed money | Recoverable denials the agent marked as no action (`no_action` items carry the `missed_money_review` flag) | Reviewed one by one; none from a repeatable rule gap |
| Security | PHI incidents, PHI found in logs | Zero |

## Phase 3: Assisted live pilot (weeks 11 to 20)

Weekly loop: clinic sends new 835 files and unworked denials; the agent classifies, proposes and
drafts; the biller or owner approves, edits or rejects in the review queue; approved items go out as an
export package and the clinic submits them itself; the clinic marks each item submitted; later 835
files close the loop automatically. Work corrected-claim resubmissions first (they resolve faster),
ordered by filing deadline, then dollar value, then confidence; start formal appeals in parallel.

## Phase 4: Proof-of-concept gate (around week 20)

| Measure | Recommended threshold |
|---|---|
| First agent-assisted claim paid | At least 1 |
| Real denials processed (shadow plus live) | At least 30 |
| Category agreement with the biller | 80% or better |
| Drafts approved with minor or no edits | 70% or better |
| Overturn rate on submitted fixes | Tracked; compare against the clinic's own historical rate |
| Review time per item | Under 5 minutes on average |
| Dollars recovered that the clinic would likely have written off | Reported in total |
| Security incidents | Zero |
| Clinic owner wants to continue and pay | Yes, in writing |

Go: start Phase 5. Iterate: extend 4 to 6 weeks with targeted fixes. Stop or pivot: clinic
disengages or the agent recovers little beyond what the biller already gets.

## Phase 5: Cloud and billing specialist (weeks 21 to 26)

Same containers in a HIPAA-eligible cloud account (US regions, provider BAA, HIPAA-eligible services
only; e.g. ECS Fargate + RDS Postgres + KMS + Secrets Manager + CloudTrail), one database schema per
clinic, model calls through Bedrock or the HIPAA-ready Anthropic API, a certified billing specialist
in the reviewer seat, local data securely erased after migration is verified (`denial-agent
purge-clinic`). Claim submission through a clearinghouse API comes later, once clinic agreements
explicitly authorize it.
