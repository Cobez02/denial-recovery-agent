# Weekly Shadow-Phase Review

One record per week during the pilot (Pilot Agreement, Sections 3.1 and 15.2, and Exhibit A). During the
shadow phase, complete every section. During the live phase, complete the header (leaving the item counts
blank), the Security measure in Section 1, and Sections 5 and 6. Items are identified by work item number
only, never by patient name. Working files that list claim numbers, such as the biller's labels, contain
protected health information: they are kept only in the CBWORK account and destroyed with the rest of the
Clinic's data at the end of the engagement.

| | |
|---|---|
| Week number | |
| Period (from / to) | |
| Meeting date | |
| Format | ☐ In person ☐ Video service covered by a business associate agreement |
| Attendees | |
| Items labeled this week | |
| Items compared this week | |
| `denial-agent verify-audit` result | ☐ OK ☐ Failed |
| Latest audit-log entry and hash (first 16 characters) | |

## 1. Measures

| Measure | How it is counted | Target | This week | Last week | Met |
|---|---|---|---|---|---|
| Category agreement | Plumbline's denial category matches the biller's | 80% or better | | | |
| Action agreement | Plumbline's proposed action matches what worked, or the biller confirms it would have worked | 70% or better | | | |
| Usable drafts | Appeals and corrections the biller would send with only minor edits | 70% or better | | | |
| Missed recoveries | Recoverable denials Plumbline marked as requiring no action or did not turn into a work item (every item whose current proposal is "no action," and every labeled claim without a work item) | Each reviewed individually; none caused by a repeatable rule gap | | | |
| Security | Incidents involving protected health information; protected health information found in logs | Zero | | | |

**Exit rule.** Every target met in two consecutive weekly reviews, and the Clinic's written agreement to
begin the live phase.

| Consecutive weeks with every target met | Clinic's written agreement (date and form) |
|---|---|
| ☐ 0 ☐ 1 ☐ 2 | |

## 2. Counting Method

**Blind comparison.** The biller records a category and an action for each item before seeing
Plumbline's answer.

**Category agreement.** The biller's categories are recorded in a labels file inside CBWORK that maps each
claim number to a category, using Plumbline's category names: `eligibility`, `authorization`,
`modifier`, `duplicate`, `timely_filing`, `medical_necessity`, `bundling`, `coordination_of_benefits`,
`missing_information`, `contractual`, `patient_responsibility` and `other`.

The biller labels each claim with the one category Plumbline would give it:

- The category names the fix, not the wording of the reason code. For example, a manipulation line denied
  CO-50 without the AT modifier is `modifier`; a GZ line is `contractual`; an X-ray denied PR-170 is
  `patient_responsibility`; a Medicaid CO-119 with N640 is `authorization`; a commercial CO-119 with N587 is
  `other`; CO-97 with N626 is `modifier`, while CO-97 with M15 is `bundling`.
- When a claim has several denial reasons, Plumbline keeps the reasons whose amount is at least one fifth of
  the largest and gives the claim the category among them that comes first in this order: timely filing,
  eligibility, coordination of benefits, authorization, duplicate, bundling, modifier, medical necessity,
  missing information, other, contractual, patient responsibility.

The agreement rate is then computed in the CBWORK installation:

```python
import json
from app.db import session_scope
from app.outcomes.metrics import category_agreement, labels_from_json

with open("biller_labels_week_N.json", encoding="utf-8") as fh:
    labels = labels_from_json(json.load(fh))
with session_scope() as session:
    share, compared = category_agreement(session, labels)
print(f"category agreement {share:.0%} over {compared} items")
```

Work items without a label are skipped. The script compares every work item of each labeled claim,
earlier remittances included, so the number compared equals the number of work items for the labeled
claims: a claim denied twice counts twice. When a labeled claim has more than one work item, count its
category agreement from the Section 3 table instead.

**Action agreement and usable drafts.** Counted from the table in Section 3. Plumbline's actions are
`corrected_claim`, `appeal`, `clinic_question` and `no_action`. The biller's action is mapped to them as
follows: a Medicare reopening counts as `corrected_claim`; a redetermination counts as `appeal`; a
write-off or a bill to the patient counts as `no_action`; and a service authorization request counts as
`clinic_question`. A draft that could be filed as a Medicare reopening with only the change it lists
counts as usable. When the biller's recorded action differs from Plumbline's and nothing has worked yet,
the item counts as a disagreement unless BTP and the biller record why the biller's action was wrong.

**Missed recoveries.** Every item whose current proposal is `no_action` is reviewed. The queue's
`missed_money_review` flag marks only expired timely-filing items, so the complete list is produced in the
CBWORK installation:

```python
from sqlalchemy import select
from app.db import session_scope
from app.models import Proposal, WorkItem

with session_scope() as session:
    rows = session.execute(
        select(WorkItem.id, WorkItem.category, WorkItem.flags)
        .join(Proposal, Proposal.work_item_id == WorkItem.id)
        .where(Proposal.is_current.is_(True), Proposal.action_type == "no_action")
        .order_by(WorkItem.id)
    ).all()
for item_id, category, flags in rows:
    print(item_id, category, flags)
```

Every claim the biller labeled that has no work item is also listed in Section 3 as a disagreement and
reviewed as a possible missed recovery. For example, a PR-96 or PR-119 adjustment creates no work item
unless the whole claim was denied.

A repeatable rule gap is one that the same denial pattern would trigger again; it is corrected before the
next review.

**Audit log.** The Software shows no hash on its pages. The latest audit entry and its hash are read in the
CBWORK installation:

```python
from sqlalchemy import select
from app.db import session_scope
from app.models import AuditLog

with session_scope() as session:
    entry = session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).scalar_one()
    print(entry.id, entry.ts.isoformat(), entry.action, entry.hash)
```

**Security.** Incidents opened during the week; blocked model requests in the audit log (each one is
investigated to find why an identifier remained); and, where console output was saved to a file, a search
of that file for the masking markers `[SSN]`, `[EMAIL]`, `[PHONE]`, `[DATE]`, `[ID]` and `[MASKED]`. A marker
on a line written by an `app.` logger means a log message required masking and is handled as an incident.

## 3. Item Comparison

| Work item | Payer identifier | Plumbline category | Biller category | Match | Plumbline action | Biller action or what worked | Match | Draft usable (yes / minor edits / no / not applicable) | Cause of disagreement | Fix | Owner |
|---|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | | |

**Cause of disagreement:** rule gap; payer-specific behavior; data missing from the claims report; note
excerpt missing; template wording; biller label corrected; other.

**Fix:** classification rule; modifier rule; playbook code; payer deadline; prompt; letter template;
synthetic test case; change to the data request; none. Test cases written from a disagreement use
synthetic data modeled on the case, never the real record.

## 4. Agenda (30 minutes)

| Minutes | Item |
|---|---|
| 0 to 5 | Security check: incidents, audit-log verification, blocked model requests; last week's actions |
| 5 to 12 | The five measures and the exit rule |
| 12 to 24 | Disagreements and missed recoveries, one at a time: cause and fix |
| 24 to 30 | Decisions, owners and dates; data needed for next week; readiness for the live phase |

## 5. Decisions and Actions

| No. | Action | Owner | Due | Done |
|---|---|---|---|---|
| | | | | |

## 6. Rule and Template Changes Since Last Week

| Change | Component | Synthetic test added | Tests passed on |
|---|---|---|---|
| | | | |
