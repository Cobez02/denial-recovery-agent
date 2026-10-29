# Testing plan → test suite

The plan's testing table, mapped to the automated suite. Every layer here runs on every code change
(`uv run pytest`, also in CI) with no Docker, Redis, network or API key: tests use SQLite and the
deterministic stub model client. Shadow comparison (Phase 2) and live outcomes (Phase 3) are measured
with real data and are not automated tests.

| Plan test | Phase | What it checks | Pass when | Test files |
|---|---|---|---|---|
| Parser golden files | 1 onward | 835 and claim-export parsing against known-correct outputs | Exact match on every field | `tests/test_parser_golden.py`, `tests/test_claims_import_golden.py` |
| Classifier test set | 1 onward | Denial categories on the labeled synthetic denials (`synthetic/fixtures/labeled_set_v1/labels.json`), later grown with real labeled cases | 90% synthetic; 80% on real labels | `tests/test_classifier_set.py` (threshold asserted), `tests/test_classifier_rules.py` |
| Prompt regression | 1 onward | Fixed inputs to every prompt; output structure and cited facts | No structure breaks; no fact absent from the inputs | `tests/test_prompt_regression.py`, `tests/test_drafting.py` (fact check) |
| Redaction | 1 onward | Every model payload scanned for names, member IDs, addresses, birth dates | Zero identifiers found | `tests/test_redaction.py`, `tests/test_redaction_e2e.py` |
| PHI in logs | 1 onward | Log output scanned during test runs | Zero identifiers found | `tests/test_phi_in_logs.py` |
| Injection | 1 onward | Synthetic payer letters containing planted instructions | Agent ignores them; flags the document | `tests/test_injection_unit.py`, `tests/test_injection.py` |
| End-to-end synthetic | 1 | Import to proposal to approval to outcome on a 200-claim batch | Whole loop completes; every step in the audit log | `tests/test_end_to_end_synthetic.py` |
| Shadow comparison | 2 | Agent versus the biller's real decisions | Phase 2 exit targets met | `app.outcomes.metrics.category_agreement` + weekly review (manual) |
| Live outcomes | 3 | Payer results on submitted fixes | Phase 4 gate met | dashboard (`/dashboard`) + weekly review (manual) |

Milestone done-when checks that are also tests:

| Milestone | Test |
|---|---|
| M1 app starts with one command | `scripts/bootstrap.*`; `tests/test_cli.py` |
| M2 migrations run cleanly | `tests/test_migrations.py` |
| M3 200-claim batch imports with zero unmatched remittances | `tests/test_end_to_end_synthetic.py::test_import_batch_matches_everything` |
| M4 90% on labeled set | `tests/test_classifier_set.py` |
| M5 no identifiers reach the model | `tests/test_redaction_e2e.py` |
| M6 every proposal cites codes and note text | `tests/test_playbooks.py`, `tests/test_end_to_end_synthetic.py` |
| M7 reviewer clears 20 items without touching the database | `tests/test_review_queue.py::test_reviewer_clears_twenty_items_over_http` |
| M8 full loop, every step logged, audit append-only | `tests/test_end_to_end_synthetic.py`, `tests/test_audit.py`, `tests/test_outcomes.py` |

Metric definitions (from the plan): category agreement = share of denials where the agent's category
matches the biller's; overturn rate = paid fixes / submitted fixes; recovered dollars count only payments
that follow an agent-assisted fix; review time = average minutes a reviewer spends per item.
