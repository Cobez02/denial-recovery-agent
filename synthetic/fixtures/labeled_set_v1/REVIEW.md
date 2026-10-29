# Labeled synthetic set v1 - review notes

This directory is the committed output of

```python
from synthetic.generate import generate

generate(seed=20261028, n_claims=200, out_dir="synthetic/fixtures/labeled_set_v1")
```

(defaults: profile `dermatology`, `denial_rate=0.75`, `injection_rate=0.20`, `as_of=2026-10-28`).
Re-running that call reproduces every file byte for byte; `tests/test_synthetic.py` checks it.
Do not edit the generated files by hand: change the generator or the profile and regenerate.

## What the labels are

`labels.json` holds the **generator's ground truth**, not a human judgement. Each denied claim was
built from a named denial scenario in `synthetic/profiles/dermatology.yaml`, and the label simply
records that scenario's category, expected action, CARC/RARC and level. The plan's hand-labeling
step is still owed: the clinic's biller or a certified coder should walk the set and confirm, for
each claim, that

1. the CARC/RARC combination in the 835 really means what the category says,
2. the expected action is what an experienced biller would do with the claim, its notes and the
   payer letter in front of them (in particular the `appeal` vs `no_action` splits for duplicates,
   bundling and timely filing, and the `corrected_claim` cases that depend on a documented
   diagnosis, an NDC or a prior authorization already being on the claim record),
3. the note excerpts would actually support the appeal or coding change.

Disagreements go back into the profile (scenario definitions, note templates) or into
`app/classify/rules.yaml`, and the set is regenerated as `labeled_set_v2`.

## Contents

- `claims_export.csv`, `notes.csv`, `payer_documents.csv` - the clinic's side (SPEC 4.1)
- `remit_NW001.835`, `remit_CMA02.835`, `remit_BH003.835`, `remit_EV004.835` - one remittance per
  payer with every claim of that payer (SPEC 4.2)
- `labels.json` - one entry per denied claim
- `manifest.json` - counts, scenario counts, per-payer totals and SHA-256 of every file

Counts are in `manifest.json`; the set has 200 claims, 150 labeled denials covering every
actionable category at least six times (`other` at least three), and at least ten payer letters
with planted instructions (`injection_planted: true`). Everything is fictional; see
`synthetic/README.md` for the formats.
