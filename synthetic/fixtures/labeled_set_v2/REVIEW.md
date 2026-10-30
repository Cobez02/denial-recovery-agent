# Labeled synthetic set v2 (chiropractic) - review notes

This directory is the committed output of

```python
from synthetic.generate import generate

generate(seed=20261028, n_claims=200, out_dir="synthetic/fixtures/labeled_set_v2")
```

(defaults: profile `chiropractic`, `denial_rate=0.75`, `injection_rate=0.20`, `as_of=2026-10-28`;
generator version 3), or `denial-agent generate-synthetic --out synthetic/fixtures/labeled_set_v2`.
Re-running that call reproduces every file byte for byte;
`tests/test_synthetic.py::test_committed_fixture_is_reproducible` checks it. Do not edit the generated
files by hand: change the generator or the profile (`synthetic/profiles/chiropractic.yaml`) and
regenerate. v1 was the dermatology example set; the specialty was fixed as chiropractic on
2026-10-29 (`docs/SPECIALTY.md`). The set was regenerated on 2026-10-30 after the review of the
specialty switch (region-paired diagnoses, payer ages, G0283, sequestration, the ordering provider
on X-ray claims, three new scenarios and three relabeled ones, listed below).

## What the labels are

`labels.json` holds the **generator's ground truth**, not a human judgement. Each denied claim was
built from a named denial scenario in the profile, and the label records that scenario's category,
expected action, CARC/RARC, group and level, the denied line and amount, and `dollar_value` (what the
ingest rule stores on the work item: every non-informational adjustment on the claim, the
Medicare contractor's PR-96 lines included). The plan's hand-labeling step is still owed: the
clinic's biller or a certified coder should walk the set and confirm, for each claim, that

1. the CARC/RARC combination in the 835 really means what the category says (in particular the
   line-aware uses of CARC 50: a CMT line without AT is `modifier` unless the remark is a review
   finding such as N10, a GZ line is `contractual`, a PR-group line is `patient_responsibility`,
   everything else `medical_necessity`),
2. the expected action is what an experienced chiropractic biller would do with the claim, its notes
   and the payer letter in front of them (the `appeal` vs `no_action` splits for duplicates, bundling,
   maintenance care and timely filing; the `corrected_claim` cases that depend on a documented
   subluxation level, an authorization number, the ordering chiropractor or a separate region; the
   `clinic_question` for the Medicaid service limit, the visit maximum, other coverage and the
   initial treatment date, which the claim export cannot carry),
3. the note excerpts would actually support the appeal or coding change (active treatment with PART
   findings, a plan with goals and measurable improvement for the AT and medical-review cases; the
   separate region and minutes for the 97140 case; the separately identifiable complaint for the
   E/M case; the therapy minutes and plan of care for the GP case), and that a note documenting
   maintenance care is never used to add AT.

Disagreements go back into the profile (scenario definitions, note templates, payer rules) or into
`app/classify/rules.yaml` / `app/playbooks/modifier_rules.yaml`, and the set is regenerated as
`labeled_set_v3`.

## How to hand-check a claim

1. Find the claim's rows in `claims_export.csv` (modifiers, diagnosis codes and pointers, the
   prior-authorization and referring/ordering NPI fields).
2. Find its CLP in `remit_<payer_id>.835`: the CAS group, CARC and amounts per line, the LQ remark
   codes, CLP05 (the patient's share, PR-group lines included).
3. Read its notes in `notes.csv` and its letter or portal text in `payer_documents.csv` (untrusted
   data; some carry planted instructions on purpose, `injection_planted` in the label).
4. Compare with the label and with `docs/SPECIALTY.md` section 2 (the research pattern and source
   behind the scenario) and tick the numbered assumptions in section 4 that the claim depends on.

## Scenarios whose label is a judgement call

- `modifier_at_missing_maintenance_care` (6 claims): CO-50 N115 on a CMT line billed without AT;
  the notes say the patient is clinically stable and the visits prevent deterioration, and no ABN
  is on file. Label `no_action`: AT is never added to maintenance care; the clinic obtains an ABN
  (and bills GA) going forward. Compare with `modifier_at_missing` (6 claims), where the notes
  document active treatment and the label is a corrected claim adding AT.
- `med_nec_um_vendor_review` (5 claims, the PPO): CO-50 with N10, the review organization's peer
  review; the notes carry a re-examination with outcome scores. Label `appeal`. The rule set reads
  N10 as a medical-necessity review, not as the Medicare no-AT edit (a commercial plan's CMT line
  never carries AT).
- `duplicate_second_cmt_same_day` (5 claims, the PPO): a second line of the CMT code on the same
  day after a fall, with notes for two encounters. Label `no_action` (relabeled from `appeal`):
  spinal CMT is payable once per patient per day, so no repeat modifier makes the second line
  payable.
- `bundling_97140_separate_region` (4 claims): 97140 to an extremity billed without XS or 59. Label
  `corrected_claim` adding XS (relabeled from `appeal`): the claim never reported the distinct
  service, so the fix is a corrected claim; an appeal fits only when XS was already billed.
- `auth_medicaid_service_limit` (5 claims): more than 20 manipulations without a service
  authorization; the visit note on file says nothing about an authorization. Label
  `clinic_question` (a service authorization request), whatever a note says.
- `cob_medicare_secondary_payer` (3 claims, the Medicare contractor): CO-22 N598, Medicare's records
  show another plan is primary. Label `clinic_question` (the primary payer's adjudication or an
  updated coordination-of-benefits record).
- `missing_ordering_provider_xray` (2 claims): the X-ray claim reached the Medicare contractor with
  the ordering field empty; the note says the treating chiropractor ordered the films. Label
  `corrected_claim` with the rendering chiropractor as the ordering provider.
- `contractual_gz_maintenance` (7) and `pr_xray_without_gy` (8): expected denials (a GZ line, an
  X-ray billed without GY) that still create a work item; label `no_action`. They point at a billing
  habit to fix, not at money to recover.

## Billing assumptions the profile makes (for the coder, code by code)

Payer rules (one row per synthetic payer; the real contracts replace them):

- `GBMB1` Medicare Part B: AT on 98940-98942; GP GY on 97012 / G0283 / 97035 / 97110 / 97140 /
  97124, GY on 99202-99214, 72040 / 72070 / 72100 and 98943; those lines are denied PR-96 N425 and
  create no work item. G0283 is billed in place of 97014; the rendering chiropractor is the ordering
  provider on every claim with an X-ray line (12 of the 14 X-ray claims; the other 2 are the
  scenario that denies its absence); 2% sequestration (CO-253) on every payment; about 60% of the
  claims with a paid line carry patient responsibility on it, the CMT (20% coinsurance, sometimes a
  deductible); beneficiaries
  65-92 (88%) or 40-64 (12%); windows 365 / 365 / 120; its corrected-claim letters say Medicare
  takes reopenings and new claims, not frequency-7 replacements.
- `GPMA2` Medicare Advantage: AT on CMT; GP on therapy codes (paid under a supplemental benefit);
  GY on X-rays and 98943 (PR-96 N425); G0283 for 97014; the ordering provider on X-ray claims; 2%
  sequestration; copays $20 / $30; members 65-92 (92%) or 40-64 (8%); windows 365 / 365 / 65.
- `SWPPO3` commercial PPO: no AT; GP on therapy codes; every line kind paid (97014 as itself);
  copays $25 / $35 / $45; members 18-64 (95%) or 65-72 (5%); windows 90 / 90 / 180.
- `TLMC4` Medicaid: AT on CMT; therapies and 98943 are never billed to the plan; no cost sharing;
  members 18-64; windows 180 / 180 / 60.

Codes (charge, units, what the notes say):

- 99202 $150, 99203 $205, 99204 $285 (new patient, with 25 when billed with CMT); 99212 $95, 99213
  $120, 99214 $170 (re-examination or new injury, with 25). An E/M appears only on the initial,
  re-examination and exacerbation visits.
- 98940 $60 (1-2 regions), 98941 $78 (3-4), 98942 $98 (5): one unit; the note's `detail` names the
  segment adjusted in as many regions as the code requires (the sacral segment on 98942 is kept in
  the record, not paired on the claim); the line points to one subluxation level and the condition
  of the same region per treated region, two pairs at most, so 2 to 4 pointers (3 when one
  condition, low back pain, serves both the lumbar and the pelvic level).
- 98943 $45 extraspinal (shoulder), points to the shoulder diagnosis.
- 97110 $55 per unit, 1-2 units, 15 minutes per unit, supervised one-on-one, the exercise text for
  the region of the first pair; 97140 $55 per unit, 1-2 units (XS and an extremity diagnosis when the
  region was not manipulated; in a manipulated region only on the claims the same-region scenario
  denies); 97012 $32, 97014 / G0283 $32 untimed; 97035 $35, 8 minutes; 97124 $50 in the fee schedule
  only.
- 72040 $95 (3 views, cervical), 72070 $100 (2 views, thoracic), 72100 $105 (2 views, lumbosacral) at
  the initial examination, always of a region the CMT treated, ordered by the treating
  chiropractor.
- Allowed ratios 0.50 / 0.55 / 0.72 / 0.45 of charge for GBMB1 / GPMA2 / SWPPO3 / TLMC4.

Diagnoses: primary M99.01 (cervical), M99.02 (thoracic), M99.03 (lumbar) or M99.05 (pelvic), each
with a condition of the same region: cervical M54.2, M54.12, M50.30, M47.812, S13.4XXA; thoracic
M54.6, S23.3XXA; lumbar M54.16, M51.360, M47.816, S33.5XXA; pelvic S33.6XXA; M54.50 (lumbar or
pelvic) and M62.830 (thoracic or lumbar). Extremity work points to M25.511 / M25.512 (shoulder),
M25.551 / M25.552 (hip) or M25.561 / M25.562 (knee). G44.86, G44.209, R51.9, M54.9 and M79.10 are
billed by no encounter: they appear only where the two inconsistent-diagnosis scenarios swapped one
in for the subluxation level the chart documents.

## Contents

- `claims_export.csv`, `notes.csv`, `payer_documents.csv` - the clinic's side (SPEC 4.1)
- `remit_GBMB1.835`, `remit_GPMA2.835`, `remit_SWPPO3.835`, `remit_TLMC4.835` - one remittance per
  payer with every claim of that payer (SPEC 4.2)
- `labels.json` - one entry per denied claim
- `manifest.json` - counts, scenario counts, per-payer totals and SHA-256 of every file

Counts are in `manifest.json`; the set has 200 claims (396 lines, 140 patients), 193 notes, 63 payer
documents and 150 labeled denials from all 29 scenarios, covering every actionable category at
least six times (`other` included) and the two informational categories (`contractual`,
`patient_responsibility`) at least six times each, and 13 payer letters with planted instructions
(`injection_planted: true`). Everything is fictional; see `synthetic/README.md` for the formats.
