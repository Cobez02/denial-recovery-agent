# Denial Recovery Agent — Build Specification (Phase 1: local build on synthetic data)

Source plan: "Denial Recovery Agent: Local MVP to Pilot Plan" (Oct 28, 2026). This document turns the
plan's Phase 1 milestones (M1–M8) and testing plan into a concrete, buildable specification. Every
module author builds against the interfaces here. When this spec and a module disagree, fix the module.

## 0. Ground rules that are enforced in code

| Rule from the plan | Enforcement in this codebase |
|---|---|
| No real patient data until the BAAs and the security checklist are done | `data/` is git-ignored; all fixtures are synthetic; `docs/SECURITY_CHECKLIST.md` must be dated before Phase 2 |
| The software never submits anything | There is no clearinghouse/payer integration. Approved items become an **export package**; a human marks them submitted |
| Never change codes or add modifiers to raise payment unless documentation supports it | Every corrected-claim change carries `coding_change: bool`; a coding change sets `requires_note_excerpt=True`, is listed under `flags`, and the reviewer must attach or confirm a quoted note excerpt before approval |
| Minimum-necessary data to the model; names, member IDs, addresses, DOBs tokenized | `app/redact` tokenizes before every model call and restores after; `app/llm/guard.py` refuses to send a payload the scanner flags |
| Log every action by every person and agent | `app/audit` append-only, hash-chained log; every pipeline step and review action records an event |
| Payer letters, notes and portal text are untrusted | Documents are wrapped as data in prompts, scanned for planted instructions, flagged on the work item; the injection test suite proves the agent ignores them |
| The clinic can stop the pilot; data returned or destroyed | `denial-agent purge-clinic <id>` exports then deletes a clinic's data and writes a destruction record |

## 1. Stack and layout

- Python 3.12+ (local dev on 3.13 is fine), FastAPI, Jinja2 server-rendered pages with HTMX (vendored at `app/static/htmx.min.js`).
- SQLAlchemy 2.0 ORM + Alembic. PostgreSQL 16 in Docker for runtime; **SQLite for the automated test suite** so tests run with no services. Models must therefore stay dialect-neutral (`JSON`, `Numeric`, `String`, `DateTime(timezone=True)`; no Postgres-only column types; Postgres-only triggers are added in migrations guarded by dialect).
- Background jobs: RQ + Redis in Docker; `JOB_BACKEND=inline` runs jobs synchronously (default locally and in tests).
- Model client: Anthropic Python SDK, plain Messages API only (no batch, files, code execution — those are outside the BAA). Pinned model id from config. Prompts are versioned files in `app/llm/prompts/`.
- PDFs: WeasyPrint when importable (Docker image has it); otherwise the HTML letter is exported and the package notes that the PDF was skipped.
- Tests: pytest. Secret scanning: gitleaks via pre-commit and CI.

```
denial-agent/
  docker-compose.yml  Dockerfile  .env.example  pyproject.toml  alembic.ini
  app/
    main.py            FastAPI app factory, middleware, router registration
    config.py          Settings (pydantic-settings), env-driven
    db.py              engine/session factory, `get_session` dependency, `init_db()`
    enums.py           StrEnums shared by every module
    models.py          SQLAlchemy models (M2)
    schemas.py         Pydantic contracts passed between modules
    logging_config.py  stdlib logging with PHI-masking filter
    pipeline.py        orchestrates import -> denial detection -> classify -> propose -> draft
    api/               JSON/HTML routes that are not the review UI (health, imports, dashboard data)
    ingest/            x12 tokenizer, 835 parser, claim-export importer, matching, import batches
    classify/          rules engine (CARC/RARC -> category), model classifier for ambiguous cases
    playbooks/         fix logic per category -> ProposalDraft
    drafting/          corrected-claim builder + diff, appeal letter templates, PDF rendering
    redact/            identifier registry, tokenizer/restore, scanner, injection detector
    review/            review queue pages and actions, export package
    outcomes/          payment matching for submitted fixes, dashboard metrics
    audit/             append-only audit log with hash chain
    llm/               client protocol, Anthropic client, stub/replay clients, prompt registry, guard
    auth/              users, argon2 passwords, TOTP MFA, session login
    jobs/              job runner (inline | rq)
    migrations/        alembic env + versions
    templates/         Jinja2 templates (pages/, partials/, letters/)
    static/            htmx.min.js, app.css
    cli.py             `denial-agent` command (init-db, create-user, import-835, import-claims, process, generate-synthetic, verify-audit, purge-clinic)
  synthetic/           generators, specialty profile(s), committed labeled fixture set
  tests/               pytest suite + golden fixtures
  docs/                SPEC.md (this), SECURITY_CHECKLIST.md, PILOT_CHECKLIST.md, TESTING.md, PHASES.md
  data/                git-ignored runtime data (encrypted disk)
```

## 2. Data model (M2) — `app/models.py`

All tables have integer autoincrement `id`. Money is `Numeric(12, 2)` (Decimal). Timestamps are
timezone-aware UTC. JSON columns use SQLAlchemy `JSON`.

| Table | Purpose / key columns |
|---|---|
| `clinics` | name, npi, tax_id, pm_system, created_at |
| `payers` | name, `payer_id` (X12 payer identifier, unique), timely_filing_days (default 90), corrected_claim_days (default 90), appeal_days (default 180), address JSON |
| `users` | email (unique), password_hash (argon2), totp_secret, role (`reviewer`/`admin`), is_active, failed_logins, locked_until, created_at, last_login_at |
| `patients` | clinic_id, `token` (stable pseudonym like `PT-7F3A2C`, unique), account_number (PM account, unique per clinic), first_name, last_name, dob, sex, address_line1, city, state, zip, member_id |
| `claims` | clinic_id, payer_id, patient_id, `claim_control_number` (unique per clinic), frequency_code (`1` original, `7` replacement, `8` void), original_claim_id (self FK for corrected claims), billing_provider_npi, rendering_provider_npi, rendering_provider_name, referring_provider_npi, place_of_service, diagnosis_codes JSON list, prior_auth_number, service_date_from, service_date_to, total_charge, original_submission_date, status (`ClaimStatus`), import_batch_id, extra JSON |
| `claim_lines` | claim_id, line_number (unique per claim), service_date, procedure_code, modifiers JSON list, units Numeric(8,2), charge_amount, diagnosis_pointers JSON list (1-based ints), ndc, line_item_control_number |
| `remittances` | payer_id (nullable until matched), file_name, `file_sha256` (unique: duplicate-safe), trace_number (TRN02), payer_identifier (TRN03), payment_method (BPR04), payment_amount (BPR02), payment_date (BPR16), production_date (DTM*405), payer_name, payee_npi, received_at, import_batch_id, raw_meta JSON |
| `remittance_claims` | remittance_id, claim_id (nullable), sequence, patient_control_number (CLP01), claim_status_code (CLP02), charge_amount, paid_amount, patient_responsibility, filing_indicator, payer_claim_control_number (CLP07), facility_type_code, frequency_code (CLP09), patient_last_name, patient_first_name, member_id, claim_received_date, statement_from, statement_to, remark_codes JSON, is_denial, denied_amount, match_method |
| `remittance_lines` | remittance_claim_id, claim_line_id (nullable), line_number, procedure_code, modifiers JSON, charge_amount, paid_amount, units, service_date, line_item_control_number, remark_codes JSON |
| `adjustments` | remittance_claim_id, remittance_line_id (nullable), level (`claim`/`line`), group_code (CO/PR/OA/PI/CR), carc, amount, quantity, is_denial |
| `note_excerpts` | claim_id, note_date, author, text, source |
| `payer_documents` | claim_id (nullable), remittance_claim_id (nullable), document_type (`letter`/`portal`/`other`), received_date, text, flagged_injection, injection_findings JSON |
| `work_items` | claim_id, remittance_claim_id, category (`DenialCategory`), category_confidence, classification_source (`rules`/`model`/`rules+model`), all_reasons JSON, status (`WorkItemStatus`), dollar_value, deadline, deadline_kind (`corrected_claim`/`appeal`), priority_score, assigned_user_id, first_viewed_at, decided_at, submitted_at, submitted_claim_control_number, closed_at, flags JSON list |
| `proposals` | work_item_id, version, is_current, action_type (`ActionType`), summary, rationale, citations JSON, corrected_claim JSON `{before, after, changes}`, appeal_letter (text), clinic_question, coding_change, requires_note_excerpt, confidence, prompt_version, model_id, generated_by |
| `actions` (`ReviewAction`) | work_item_id, proposal_id, user_id, action (`ReviewActionType`), reason, edited_payload JSON, review_seconds |
| `outcomes` | work_item_id, remittance_claim_id, result (`OutcomeResult`), paid_amount, payment_date, matched_at, details JSON |
| `audit_log` | ts, actor_type (`user`/`agent`/`system`), actor_id, action, entity_type, entity_id, claim_id, reason, details JSON, prev_hash, hash — **append-only** |
| `import_batches` | kind (`835`/`claims`/`notes`/`payer_documents`), file_name, file_sha256, status, counts JSON, error, started_at, finished_at, created_by |
| `model_calls` | ts, prompt_name, prompt_version, model_id, purpose, work_item_id, input_tokens, output_tokens, cache_read_tokens, latency_ms, redaction_counts JSON, request_id, status (`ok`/`refusal`/`error`), payload_sha256 |

Append-only audit log: (1) ORM listeners raise on UPDATE/DELETE of `AuditLog`; (2) the Postgres migration
adds a trigger that raises on UPDATE/DELETE; (3) each row's `hash = sha256(prev_hash + canonical JSON of the row
without hash fields)`; `app.audit.verify_chain(session)` walks the table and returns the first broken row id or None.

## 3. Shared enums — `app/enums.py`

- `DenialCategory`: `eligibility, authorization, modifier, duplicate, timely_filing, medical_necessity, bundling, coordination_of_benefits, missing_information, contractual, patient_responsibility, other`
- `ActionType`: `corrected_claim, appeal, clinic_question, no_action`
- `WorkItemStatus`: `new, proposed, in_review, approved, rejected, exported, submitted, closed`
- `ReviewActionType`: `view, approve, edit, reject, export, mark_submitted, reopen, close, attach_note`
- `ClaimStatus`: `submitted, denied, paid, partial, corrected, appealed, closed`
- `OutcomeResult`: `paid, partial, denied_again, reversed`
- `ClassificationSource`: `rules, model, rules+model`
- `ActorType`: `user, agent, system`

## 4. File formats

### 4.1 Claim export (the one practice-management system) — CSV, UTF-8, header row, one row per claim line

Columns, in order:
`claim_control_number, patient_account_number, patient_first_name, patient_last_name, patient_dob (YYYY-MM-DD), patient_sex (M/F/U), patient_address_line1, patient_city, patient_state, patient_zip, member_id, payer_id, payer_name, billing_provider_npi, rendering_provider_npi, rendering_provider_name, referring_provider_npi, place_of_service, diagnosis_codes (pipe-separated ICD-10), prior_auth_number, original_submission_date (YYYY-MM-DD), claim_frequency_code (1/7/8), original_claim_control_number, line_number, service_date (YYYY-MM-DD), procedure_code, modifiers (pipe-separated), units, charge_amount, diagnosis_pointers (pipe-separated 1-based), ndc, line_item_control_number, claim_total_charge`

Rules: rows of one claim share `claim_control_number` and must be contiguous; `claim_total_charge` must equal the sum of line charges (importer warns and uses the line sum when they differ); re-importing the same file is a no-op (`import_batches.file_sha256`); re-importing a claim with the same control number updates mutable fields and never duplicates lines.

Companion files (same importer family):
- `notes.csv`: `claim_control_number, note_date, author, text`
- `payer_documents.csv`: `claim_control_number, document_type, received_date, text`

### 4.2 X12 835 (005010X221A1) — what the parser must handle

Delimiters are read from ISA (element `*`, sub-element `:`, segment terminator `~`, with optional trailing newlines). Segments and loops:

```
ISA GS ST
BPR (BPR02 amount, BPR04 method, BPR16 date CCYYMMDD) TRN (TRN02 trace, TRN03 payer id) REF*EV DTM*405
N1*PR payer (N102 name) N3 N4 REF*2U payer id PER
N1*PE payee (N102 name, N104 NPI) N3 N4 REF*TJ tax id
LX
  CLP (01 patient control #, 02 status, 03 charge, 04 paid, 05 patient resp, 06 filing indicator, 07 payer claim #, 08 facility type, 09 frequency)
  CAS (claim level; repeating triplets CAS02-04, 05-07, ... up to 6 groups)
  NM1*QC patient (03 last, 04 first, 08 MI, 09 member id)   NM1*IL insured (optional)   NM1*82 rendering (optional, 09 NPI)
  REF*1L / REF*EA (optional)   DTM*050 received   DTM*232 / DTM*233 statement period
  AMT*AU (optional)
  SVC (01 composite HC:proc:mod1:mod2:mod3:mod4, 02 charge, 03 paid, 04 revenue code, 05 units, 06 original composite)
    DTM*472 service date   CAS (line level)   REF*6R line item control number   AMT*B6   LQ*HE rarc (repeating)
  LQ*HE (claim level RARC, optional)
PLB (provider-level adjustments, optional)
SE GE IEA
```

Parser output is `schemas.Parsed835` (section 5). Golden tests compare the parsed JSON to a checked-in expected file exactly. Unknown segments are preserved in `unparsed_segments` and never crash the parser. A file with more than one ST/SE transaction set yields one `Parsed835` per transaction.

### 4.3 CARC/RARC catalog

`app/classify/codes.yaml` holds human-readable text for every CARC and RARC used by the rules and the synthetic generator (text from the WPC/X12 public code lists, paraphrased). Citations use these texts.

## 5. Shared contracts — `app/schemas.py` (Pydantic v2)

Written in the foundation and imported by every module. Names are final; add fields rather than renaming.

- `Parsed835`, `ParsedPayer`, `ParsedPayee`, `ParsedPayment`, `ParsedClaimPayment`, `ParsedServicePayment`, `ParsedAdjustment`, `ParsedProviderAdjustment`
- `ClaimExportRow` (one CSV row, validated types), `NoteRow`, `PayerDocumentRow`
- `DenialReason` `{level, group_code, carc, carc_text, rarcs, rarc_texts, amount, line_number, procedure_code}`
- `DenialContext` — everything the classifier, playbooks and drafter need for one denied claim: claim facts, lines, patient token + identifiers (for redaction only), payer facts (timely-filing/appeal windows), remittance facts, `reasons: list[DenialReason]`, `documents: list[DocumentContext]`, `notes: list[NoteContext]`, `today: date`
- `ClassificationResult` `{category, confidence, source, rule_id, rationale, injection_detected, secondary_categories}`
- `Citation` `{kind: carc|rarc|claim_field|remittance_field|note_excerpt|payer_document|payer_policy, ref, text}`
- `CorrectedClaimChange` `{path, before, after, reason, coding_change, citation_refs}`
- `CorrectedClaim` `{before: dict, after: dict, changes: list[CorrectedClaimChange]}`
- `ProposalDraft` `{action_type, summary, rationale, citations, corrected_claim, appeal_letter, clinic_question, coding_change, requires_note_excerpt, confidence, deadline, deadline_kind, flags}`
- `LLMUsage`, `LLMResult` `{content (dict|str), model_id, prompt_name, prompt_version, usage, request_id, stop_reason, status}`
- `RenderedPrompt` `{name, version, system, user, schema, max_tokens}`
- `RedactionMap` `{tokens: dict[token, original], counts: dict[kind, int]}`
- `InjectionFinding` `{pattern, snippet, start, end}`
- `DashboardMetrics` (section 10)

## 6. Ingestion (M3) — `app/ingest/`

- `x12.py`: `tokenize(text) -> list[Segment]` where `Segment(id, elements: list[str], raw)`; handles composite separator by leaving composites as strings and exposing `split_composite()`.
- `parse_835.py`: `parse_835(text: str) -> list[Parsed835]`.
- `claims_import.py`: `import_claims_csv(session, path | text, clinic_id, created_by) -> ImportBatch`; `import_notes_csv`, `import_payer_documents_csv`. Creates/updates patients (by clinic + account_number), payers (by payer_id), claims, lines. Computes `claims.status` = submitted on import.
- `remit_import.py`: `import_835(session, path | text, file_name, created_by) -> ImportBatch`. Dedup by sha256. Stores remittance, claims, lines, adjustments. Calls `matching.match_remittance_claim()` and `denials.detect()` for each CLP. For CLPs matched to claims with `submitted` work items it calls `outcomes.matching.record_outcome()`.
- `matching.py`: `match_remittance_claim(session, rc) -> Claim | None`. Order: exact `patient_control_number` == `claim_control_number`; then CLP01 == `submitted_claim_control_number` of a work item (corrected claims); then (payer_claim_control_number previously seen); then (member_id + service date + total charge) fuzzy; records match method in `match_method`. Unmatched CLPs stay with `claim_id=None` and are listed on the imports page.
- `denials.py`: `detect(rc, adjustments) -> (is_denial, denied_amount, reasons: list[DenialReason])`. Denial = CLP02 in {4} OR any adjustment with group in {CO, PI, OA, PR} whose CARC is not in `informational_carcs` and amount > 0. Informational CARCs (never a denial by themselves): 1, 2, 3, 45, 66, 94, 104, 118, 137, 144, 187, 237, 253. Reasons are sorted by amount desc; `denied_amount` = sum of denial adjustment amounts.
- M3 done-when test: the 200-claim synthetic batch imports with zero unmatched remittance claims, re-import is a no-op.

## 7. Classification (M4) — `app/classify/`

- `rules.yaml`: ordered rules. Each: `id, category, confidence, match: {carc: [..], rarc_any: [..] (optional), group: [..] (optional), level: claim|line|any}, rationale`. First match wins per reason; the work item category is the category of the highest-amount reason unless a higher-priority category outranks it (priority order: timely_filing > eligibility > coordination_of_benefits > authorization > duplicate > bundling > modifier > medical_necessity > missing_information > other). Starting rule map (extend with the coder session):
  - eligibility: CARC 26, 27, 31, 32, 33, 140, 177, 200; 109 (wrong payer) with RARC N4/N418 → coordination_of_benefits, otherwise eligibility
  - authorization: CARC 197, 198, 15, 62, 39; CARC 16 with RARC M62 / N54
  - modifier: CARC 4, 182; CARC 16 with RARC M78 / N519
  - duplicate: CARC 18; RARC N522 / M86
  - timely_filing: CARC 29
  - medical_necessity: CARC 50, 56, 167, 11, 150, 151 (frequency); RARC N115 / M76 / N661
  - bundling: CARC 97, 234, 236, 231, 59; RARC M15 / N19 / M144 / N390
  - coordination_of_benefits: CARC 22, 23 (when denial amount > 0 and claim not paid), 109 with N4
  - missing_information: CARC 16 (default when no more specific RARC rule), 125, 226, 227, 252, 206; RARCs M119 (NDC), N29, N56, MA130, M127, N179, N225
  - contractual: 45, 94, 253 ... (informational; no work item is created for these alone)
  - patient_responsibility: 1, 2, 3, PR group with 96/204 (plan exclusions) → `patient_responsibility` unless CO
  - other: anything unmatched → confidence 0.3
- `engine.py`: `classify_rules(ctx: DenialContext) -> ClassificationResult`.
- `model_classifier.py`: `classify_with_model(ctx, guard_call) -> ClassificationResult` using prompt `classify_denial` with a JSON schema; invoked when rules confidence < `CLASSIFY_MODEL_THRESHOLD` (0.75), category is `other`, or a payer document is attached. Final result: model result if its confidence > rules confidence, else rules; source recorded accordingly. With `LLM_PROVIDER=stub` the stub returns a deterministic result from the CARC in the variables (so tests are hermetic).
- `classify(ctx, llm_guard) -> ClassificationResult` is the single entry point used by the pipeline.
- M4 done-when: >= 90% category accuracy on the labeled synthetic set (`synthetic/fixtures/labeled_set_v1/labels.json`) using rules alone with stub model.

## 8. Redaction (M5) and model calls — `app/redact/`, `app/llm/`

- `redact/registry.py`: `IdentifierRegistry.from_context(ctx)` collects: patient first/last/full names (and reversed "Last, First"), member id, address line, city+zip, DOB in formats `YYYY-MM-DD`, `MM/DD/YYYY`, `MMDDYYYY`, `Month D, YYYY`; patient account number; claim control numbers; provider names; service dates (`YYYY-MM-DD`, `MM/DD/YYYY`, `CCYYMMDD`). Each identifier has a `kind`: `PATIENT_NAME, MEMBER_ID, DOB, ADDRESS, ACCOUNT, CLAIM_ID, PROVIDER_NAME, DATE`.
- `redact/tokenizer.py`: `redact(value: str | dict | list, registry) -> (redacted, RedactionMap)`; tokens look like `[[PATIENT_NAME_1]]`, same original → same token within a call; longest identifiers replaced first; case-insensitive for names. `restore(value, map)` reverses it in strings nested in dicts/lists.
- `redact/scanner.py`: `scan(value, registry) -> list[Finding]` finds any registry identifier still present plus generic patterns (SSN `\d{3}-\d{2}-\d{4}`, email, US phone, `DOB:`-adjacent dates). Used by the guard (raise `RedactionError`) and by tests.
- `redact/injection.py`: `detect_injection(text) -> list[InjectionFinding]` — patterns such as "ignore (all|any)? (previous|prior|above|earlier) instructions", "disregard .* instructions", "you are now", "system prompt", "approve (this|the) (claim|appeal|payment)", "mark .* as (paid|approved)", "do not flag", "assistant:", "<\|im_start\|>", "###\s*(system|instruction)", "output the following", base64 blobs > 80 chars. Documents with findings are stored with `flagged_injection=True`.
- `llm/base.py`: `LLMClient` protocol: `complete(rendered: RenderedPrompt) -> LLMResult`. Implementations: `AnthropicClient` (pinned `LLM_MODEL`, `thinking={"type":"adaptive"}`, `output_config={"effort": LLM_EFFORT, "format": {json_schema}}` when a schema is present, streaming with `get_final_message()` for `max_tokens > 8000`, handles `stop_reason == "refusal"` by returning status `refusal`, never logs payload content, records `_request_id`), `StubClient` (deterministic, offline), `ReplayClient` (fixtures in `tests/fixtures/llm/`), `RecordingClient` (wraps a client and writes fixtures when `LLM_RECORD=1`).
- `llm/prompts.py`: prompt files `app/llm/prompts/<name>.v<N>.md` with YAML front matter `{name, version, schema (optional), max_tokens}`, body split by a `--- user ---` marker into system and user Jinja templates. `render(name, variables) -> RenderedPrompt`; `prompt_version` is `f"{name}.v{N}@{sha256(file)[:8]}"`.
- `llm/guard.py`: `guarded_call(session, client, prompt_name, variables, registry, *, purpose, work_item_id=None) -> GuardedResult(content, llm_result, redaction_map, model_call_id)`. Steps: redact variables → render → scan system+user (raise `RedactionError` on any finding) → wrap untrusted documents (variables ending in `_documents` are rendered inside `<untrusted_document>` tags by the templates) → call client → restore tokens in output → write `model_calls` row and audit event `model_call` with counts only. The guard is the only path to a model; nothing else may import the Anthropic SDK.
- M5 done-when test: for every model call made during the end-to-end synthetic run, the captured payload contains zero names, member IDs, addresses or DOBs from the synthetic patient set (test monkeypatches the client to capture rendered prompts).

## 9. Playbooks and drafting (M6) — `app/playbooks/`, `app/drafting/`

- `playbooks/base.py`: `Playbook.propose(ctx, classification) -> ProposalDraft`; `playbooks/registry.py` maps category → playbook. Every proposal cites the CARC/RARC it relies on and any claim field or note excerpt it uses (`citations`), and sets `deadline`/`deadline_kind`.
- Category behaviours:
  - `eligibility`: `clinic_question` (verify coverage on DOS, obtain correct payer/member id); if the payer document names the correct payer, propose `corrected_claim` to that payer (flagged, not a coding change).
  - `authorization`: if `prior_auth_number` exists on the claim but was not on the remittance context → `corrected_claim` adding REF*G1; else `appeal` (retro-authorization) with note excerpts for medical necessity; `clinic_question` if no note excerpt exists.
  - `modifier`: `corrected_claim` with the modifier fix derived from `playbooks/modifier_rules.yaml` (e.g. E/M on same day as procedure → 25; distinct procedural service → 59/XS/XU; bilateral → 50; professional/technical → 26/TC). **Always `coding_change=True`, `requires_note_excerpt=True`.**
  - `duplicate`: `no_action` when the remittance shows the same service already paid; `appeal` with modifier 76/77/91 only when notes show a distinct service (coding change).
  - `timely_filing`: `appeal` with proof of timely submission when `original_submission_date` is within the payer's window; otherwise `no_action` with reason `timely_filing_expired` (reviewer sees it in the missed-money review).
  - `medical_necessity`: `appeal` quoting note excerpts tied to the diagnosis; `corrected_claim` when a documented diagnosis is missing from the claim (coding change).
  - `bundling`: `appeal` with note excerpts supporting a distinct service and modifier 59/XS (coding change); `no_action` (contractual) when nothing in the notes supports it.
  - `coordination_of_benefits`: `clinic_question` to obtain other-coverage info; `corrected_claim` to the primary payer when the document names it.
  - `missing_information`: `corrected_claim` filling the field named by the RARC when it exists in the claim record (NDC, referring NPI, auth number, diagnosis pointer, dates); else `clinic_question` naming the field.
  - `contractual` / `patient_responsibility`: `no_action`.
  - `other`: `clinic_question` with the raw reasons.
- `drafting/corrected_claim.py`: `build_corrected_claim(ctx, changes) -> CorrectedClaim` (before/after dicts in the claim-export shape, frequency code `7`, `original_claim_control_number` set, new control number `<orig>-R<n>`); `diff()` output is the `changes` list rendered in the UI.
- `drafting/appeal.py`: `draft_appeal(ctx, classification, proposal_facts, llm_guard | None) -> str`. The letter is built from `templates/letters/appeal_<category>.html` with only facts from `ctx` and quoted note excerpts; when the model is enabled, prompt `draft_appeal` may rewrite the narrative paragraph only, and `drafting/factcheck.py` verifies every number, code and date in the output exists in the inputs (otherwise the template version is used and the proposal gets flag `model_draft_rejected`).
- `drafting/pdf.py`: `render_pdf(html) -> bytes | None` (WeasyPrint optional).
- M6 done-when: every proposal's `citations` is non-empty and each citation resolves to a code text, claim field or note excerpt.

## 10. Review queue (M7), outcomes and audit (M8)

Routes (all behind login + MFA unless `AUTH_REQUIRED=false`):

| Route | Purpose |
|---|---|
| `GET /login`, `POST /login`, `GET/POST /login/mfa`, `POST /logout` | password then TOTP |
| `GET /` → `/queue` | work list: default sort by deadline asc (nulls last), then dollar_value desc, then confidence desc; filters: status, category, payer, flagged; deadline badges at <=30 and <=14 days |
| `GET /items/{id}` | claim facts, remittance adjustments with code text, documents (with injection warning), notes, current proposal, citations, corrected-claim diff, appeal letter preview; records `first_viewed_at` and a `view` action |
| `POST /items/{id}/approve`, `/edit`, `/reject`, `/mark-submitted`, `/reopen`, `/attach-note` | HTMX forms; reject/edit require a reason; approve of a coding change requires a note excerpt; each writes an `actions` row and an audit event; `review_seconds` = decided_at - first_viewed_at |
| `GET /items/{id}/export` | zip: `summary.json`, `corrected_claim.json`, `corrected_claim.csv`, `appeal_letter.html`, `appeal_letter.pdf` (if available), `audit_excerpt.json`; sets status `exported` |
| `GET /imports`, `POST /imports/835`, `POST /imports/claims`, `POST /imports/notes`, `POST /imports/documents` | upload files → job → results with unmatched list |
| `GET /dashboard` | `DashboardMetrics`: recovered dollars, overturn rate, items by status, by category and payer, avg review minutes, deadline buckets |
| `GET /audit` | paginated audit log, chain verification status |
| `GET /api/health`, `GET /api/metrics` | JSON |

Outcomes (`app/outcomes/matching.py`): when an imported CLP matches a claim (directly or via `submitted_claim_control_number`) whose work item is `submitted`, create an `outcomes` row: `paid` if paid_amount >= 0.95 × denied dollar_value, `partial` if 0 < paid < that, `denied_again` if paid == 0 with denial adjustments, `reversed` for CLP02 22; set work item `closed`, claim status accordingly, audit event `outcome_recorded`.

Metrics (`app/outcomes/metrics.py`): `compute_dashboard(session) -> DashboardMetrics`. Definitions from the plan: overturn rate = paid (full or partial) / submitted; recovered dollars = sum of outcome `paid_amount` for agent-assisted items; review time = mean `review_seconds` / 60.

## 11. Pipeline — `app/pipeline.py`

`process_remittance_claim(session, rc, *, llm_guard, actor="agent") -> WorkItem | None` builds `DenialContext`, runs `classify`, picks the playbook, drafts, writes `work_items` + `proposals` (status `proposed`), records audit events `work_item_created`, `classified`, `proposal_created`. `process_pending(session)` processes every denied remittance claim without a work item. Jobs call these.

## 12. Synthetic data — `synthetic/`

- `synthetic/profiles/dermatology.yaml`: example specialty profile (procedures, E/M codes, modifiers, diagnoses, payers with filing/appeal windows, denial scenarios with weights, each scenario listing the CARC/RARC/group/level to emit, the ground-truth category and expected action, note templates, payer letter templates). Phase 0 replaces this with the chosen specialty.
- `synthetic/generate.py`: `generate(seed, n_claims, out_dir, profile, denial_rate=0.35, injection_rate=0.05) -> Manifest`. Writes `claims_export.csv`, `notes.csv`, `payer_documents.csv`, one or more `remit_*.835`, `labels.json` (every denied claim: control number, category, expected action, scenario id), `manifest.json`. Deterministic for a seed. Names/addresses come from word lists in `synthetic/names.py` (no third-party faker).
- `synthetic/followup.py`: `generate_followup_835(session, work_items, outcome_plan) -> str` builds a later 835 paying/denying submitted fixes for the full-loop test.
- `synthetic/fixtures/labeled_set_v1/`: committed output of `generate(seed=20261028, n_claims=200)` with 150+ labeled denials, plus `REVIEW.md` explaining that labels are generator ground truth to be hand-checked.

## 13. Tests — `tests/` (maps to the plan's testing table)

| Plan test | File | Pass when |
|---|---|---|
| Parser golden files | `test_parser_golden.py`, `test_claims_import_golden.py` | exact match on every field |
| Classifier test set | `test_classifier_set.py` | >= 90% on labeled synthetic set |
| Prompt regression | `test_prompt_regression.py` | fixed inputs → schema-valid output; every fact in output exists in inputs |
| Redaction | `test_redaction.py` | zero identifiers in any captured model payload |
| PHI in logs | `test_phi_in_logs.py` | zero identifiers in captured log output during a full run |
| Injection | `test_injection.py` | planted instructions ignored; document flagged; classification unchanged |
| End-to-end synthetic | `test_end_to_end_synthetic.py` | 200-claim batch: import → proposals → approvals → export → follow-up 835 → outcomes; every step in the audit log; chain verifies |
| Audit append-only | `test_audit.py` | update/delete raise; chain tamper detected |
| Review queue | `test_review_queue.py` | ordering, actions, export package; 20 synthetic items cleared through HTTP only |
| Auth/MFA | `test_auth.py` | wrong password/TOTP rejected; session required |
| Migrations | `test_migrations.py` | `alembic upgrade head` on a fresh SQLite file; tables match models |

`tests/conftest.py` provides `settings` (SQLite temp DB, `LLM_PROVIDER=stub`, `JOB_BACKEND=inline`, `AUTH_REQUIRED` toggle), `session`, `client` (FastAPI TestClient logged in through the real MFA flow), `synthetic_batch` (generated into tmp_path) and `captured_llm_payloads`.

## 14. Configuration — `app/config.py` (`Settings`, env prefix none, `.env` file)

`APP_ENV` (local|test|docker), `SECRET_KEY`, `DATABASE_URL` (default `sqlite:///./data/denial_agent.db`; docker: postgres), `REDIS_URL`, `JOB_BACKEND` (inline|rq), `LLM_PROVIDER` (stub|anthropic|replay), `LLM_MODEL` (default `claude-opus-5-5`, pinned), `LLM_EFFORT` (default `medium`), `LLM_MAX_RETRIES`, `LLM_TIMEOUT_SECONDS`, `LLM_SERVER_FALLBACKS` (default false — pinned model; opt-in), `ANTHROPIC_API_KEY` (read by the SDK from the environment, never logged), `CLASSIFY_MODEL_THRESHOLD` (0.75), `AUTH_REQUIRED` (true), `SESSION_COOKIE_SECURE`, `DATA_DIR` (`./data`), `EXPORT_DIR`, `LOG_LEVEL`, `DEADLINE_WARN_DAYS` (`30,14`), `CLINIC_ID` (default clinic for imports).

## 15. Security controls in code

- Postgres and Redis bound to `127.0.0.1` in `docker-compose.yml`; app on `127.0.0.1:8000`.
- Session cookie `HttpOnly`, `SameSite=Strict`; state-changing routes require the session and reject cross-origin requests (Origin/`HX-Request` check).
- Argon2id password hashes; TOTP (pyotp) with 30-second window ±1 step; failed logins audited; 5 failures lock the account for 15 minutes.
- Logs carry ids and counts only; `PHIMaskingFilter` masks DOB-like dates, member-id-like tokens, emails and SSNs as defence in depth; the PHI-in-logs test fails the build on any synthetic identifier.
- `.gitleaks.toml` + pre-commit + CI; `data/` ignored; no real data in fixtures.
- `denial-agent purge-clinic` for pilot termination.
