# Plan phases, milestones and what this repository delivers

Source: "Denial Recovery Agent: Local MVP to Pilot Plan" (Oct 28, 2026). About 20 weeks to the
proof-of-concept gate, roughly 26 weeks to a cloud pilot.

## Phase 0: Groundwork (weeks 1 to 3) — people and paper, not code

| Task | Output | Done when | Status |
|---|---|---|---|
| Pick one specialty | Written choice with its top 3 payers and common denial codes | Agreed with the pilot clinic | open — `synthetic/profiles/dermatology.yaml` is an example profile; replace it |
| Recruit the pilot clinic | Named contact, biller, weekly review slot | Owner says yes in writing | open |
| Legal documents | Pilot agreement and BAA, reviewed by a healthcare attorney | Both signed by the clinic owner | open — `docs/PILOT_CHECKLIST.md` |
| Model access | HIPAA-ready API organization or cloud model account | BAA signed and confirmed active | open |
| Insurance | Errors-and-omissions and cyber policies | Certificate on file, shared with the clinic | open |
| Security basics | Risk analysis, short security policy, incident response plan | Documents written and dated | open |
| Harden the laptop | Every item in the local security checklist | Checklist complete and dated | open — `docs/SECURITY_CHECKLIST.md` |
| Coding advice | 2 to 4 hours with a certified coder on the specialty's denial patterns | Notes captured as rule inputs | open — feed into `app/classify/rules.yaml` and `app/playbooks/modifier_rules.yaml` |

## Phase 1: Local build on synthetic data (weeks 2 to 8) — this repository

| Milestone | What gets built | Done when | Where |
|---|---|---|---|
| M1 Skeleton | Repo, Docker Compose (app + Postgres), config, secret scanning on every commit | App starts locally with one command | `pyproject.toml`, `docker-compose.yml`, `.pre-commit-config.yaml`, `scripts/bootstrap.*` |
| M2 Data model | Tables for clinics, payers, tokenized patients, claims, claim lines, remittances, adjustments, work items, proposals, actions, outcomes, audit log | Migrations run cleanly; synthetic data loads | `app/models.py`, `app/migrations/` |
| M3 Ingestion | 835 parser, claim-export importer, remittance-to-claim matching, duplicate-safe imports | A 200-claim synthetic batch imports with zero unmatched remittances | `app/ingest/` |
| M4 Classification | Rules engine mapping CARC/RARC combinations to categories; the model only for ambiguous or free-text cases | 90% or better accuracy on the labeled synthetic set | `app/classify/` |
| M5 Redaction layer | Swaps identifiers for tokens before every model call and restores them after | Automated test proves no names, member IDs, addresses or birth dates reach the model | `app/redact/`, `app/llm/guard.py` |
| M6 Fix proposals and drafting | Playbook per category; corrected-claim builder showing a before/after diff; appeal letters drafted from templates, claim facts and quoted note excerpts | Every proposal cites the codes and note text it relies on | `app/playbooks/`, `app/drafting/` |
| M7 Review queue | Work list sorted by dollar value and filing deadline; approve, edit or reject with a reason; exports an approved package | A reviewer clears 20 synthetic items without touching the database | `app/review/`, `app/auth/` |
| M8 Outcomes and audit | Matches later 835s to submitted fixes; dashboard of recovered dollars, overturn rate, by category and payer; append-only audit log; login with MFA | Full loop runs on synthetic data, every step logged | `app/outcomes/`, `app/audit/` |

`docs/TESTING.md` maps the plan's testing table to the test files that prove each milestone.

## Phase 2: Shadow mode (weeks 8 to 11)

The agent works real, already-handled denials; nothing it produces is sent anywhere. Weekly: compare
the agent's category and action with the biller's, turn each disagreement into a rule, a prompt fix or
a labeled test case. Exit when the targets hold for two straight weekly reviews and the clinic owner
agrees in writing to the live pilot. Supporting code: `app.outcomes.metrics.category_agreement`.

| Measure | How it is counted | Target to exit |
|---|---|---|
| Category agreement | Agent's denial category matches the biller's | 80% or better |
| Action agreement | Agent's proposed action matches what worked, or the biller says it would have worked | 70% or better |
| Usable drafts | Appeals and corrections the biller would send with only minor edits | 70% or better |
| Missed money | Recoverable denials the agent marked as no action | Reviewed one by one; none from a repeatable rule gap |
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
in the reviewer seat, local data securely erased after migration is verified. Claim submission through
a clearinghouse API comes later, once clinic agreements explicitly authorize it.
