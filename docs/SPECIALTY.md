# Specialty choice: chiropractic (Phase 0 "pick one specialty")

Decided 2026-10-29. The first pilot clinic is a chiropractic practice, so chiropractic is the one
specialty this repository models. Everything that was dermatology-specific (the synthetic profile,
the labeled fixture set, the modifier playbook's code pairs, the medical-necessity keyword table) is
now chiropractic-specific. The generator, the rules engine and the playbooks stay profile-driven, so
another specialty is added later by writing another profile (`synthetic/README.md`, "Adding another
specialty profile"), never by a branch in Python.

The clinic's own payer mix, contracts and windows are still to be confirmed with the clinic
(`docs/PHASES.md`, Phase 0); the numbered assumptions at the end are the list the certified coder
ticks off in the Phase 0 coding session.

Sources: two research briefs (Medicare Part B contractor rules; Medicare Advantage, commercial and
Medicaid rules) compiled on 2026-10-29 from the CMS manuals (Benefit Policy Manual ch. 15 sec. 240,
Claims Processing Manual ch. 1, 12, 23, 26, 34), the NCCI policy manual ch. 11, MLN Matters SE1601
and SE1602, the OIG report OEI-01-14-00200, MAC pages (Noridian, Palmetto GBA, First Coast, NGS, WPS,
CGS), the X12 CARC/RARC lists, UnitedHealthcare/Optum, Cigna/ASH, Aetna, Premera, BCBS Vermont and
BCBS Tennessee payer policies, and the North Dakota and West Virginia Medicaid chiropractic manuals.
Section 5 lists the URLs behind every scenario. Sources may name real payers; the synthetic payers
below are fictional and stand for payer types.

Synthetic identifiers: the clinic NPI (1975310867) and the two chiropractors' NPIs (1428573012,
1732050806) pass the check digit and had no record in the CMS NPPES registry when each was looked up
(`https://npiregistry.cms.hhs.gov/api/?version=2.1&number=<npi>` returned `result_count: 0` on
2026-10-30). The check digit alone cannot tell an unassigned NPI from a real one: the first
chiropractic clinic NPI and the dermatology clinic NPI both passed it and both belonged to real
organisations, so they were replaced and `tests/test_synthetic.py` keeps their hashes out for good.

## 1. The practice and its payer mix

A single-location chiropractic office with two chiropractors (DC), place of service 11. A visit is
spinal chiropractic manipulative treatment (CMT, 98940-98942) with, depending on the payer and the
visit, an evaluation and management service (new patient or re-examination), spinal X-rays at the
initial examination, and physical-medicine services (therapeutic exercise 97110, manual therapy 97140,
mechanical traction 97012, unattended electrical stimulation 97014, billed as G0283 to the Medicare
contractor and the MA plan, ultrasound 97035) and the occasional extraspinal manipulation (98943).

A typical chiropractic practice bills four kinds of payer. The synthetic profile has exactly one of
each (`synthetic/profiles/chiropractic.yaml`):

| Synthetic payer | Stands for | Windows (timely filing / corrected claim / appeal, days) | What it pays a chiropractor |
|---|---|---|---|
| Great Basin Medicare Part B (`GBMB1`, filing indicator MB) | a traditional Medicare Part B contractor (MAC) | 365 / 365 / 120 (12-month filing limit, 42 CFR 424.44; corrections are reopenings within one year or, for an unprocessable claim, a new claim; redetermination within 120 days) | Spinal CMT only, with modifier AT and a subluxation primary diagnosis. X-rays, E/M visits, therapies and 98943 are statutorily non-covered when a chiropractor furnishes them: billed with GY (GP GY on therapies) and denied PR-96 N425, the patient liable. G0283 in place of 97014; the treating chiropractor as the ordering provider on every X-ray claim; 2% sequestration (CO-253) on every payment; beneficiaries mostly 65 and over (88%; the rest 40-64, disability or ESRD) |
| Granite Peak Medicare Advantage (`GPMA2`, filing indicator 16) | a Medicare Advantage plan with a supplemental routine-chiropractic benefit and a therapy prior-authorization program | 365 / 365 / 65 (reconsideration within 60 days of receipt, receipt presumed 5 days after the notice, 42 CFR 422.582) | CMT (AT required) plus E/M and therapies under the supplemental benefit (GP on therapy lines, NCCI bundling, modifier 25 rules); X-rays and 98943 non-covered (GY, PR-96 N425); a plan of care must be authorized after 6 visits or 8 weeks; Medicare billing rules (G0283, the ordering provider on X-rays) and 2% sequestration; members mostly 65 and over |
| Sawtooth Health Plan PPO (`SWPPO3`, filing indicator 12) | a commercial PPO with a 20-visit annual limit and a delegated utilization-management vendor | 90 / 90 / 180 | CMT (no AT), E/M, X-rays and therapies; GP on therapy codes; NCCI bundling; a treatment plan review after 5 visits; CO-119 once the visit limit is reached; working-age members |
| Timberline Medicaid Plan (`TLMC4`, filing indicator MC) | a state Medicaid program with a limited chiropractic benefit | 180 / 180 / 60 | CMT (AT required), limited E/M with modifier 25, spinal X-rays; 20 manipulations per calendar year without a service authorization; adjunctive therapies and extraspinal manipulation not covered (the clinic does not bill them to this payer); adults under 65 |

The research supports these windows and benefit shapes; the clinic's actual contracts set the real
values (`denial-agent set-payer`), see assumptions 1 to 4.

## 2. Common denial codes and patterns

Every scenario in the profile comes from one of the research patterns. "documented" means a payer
or CMS page shows the CARC/RARC pair for this situation; "typical" means the rule is documented but
the exact remittance codes are the usual pairing, not shown by the source. Section 5 lists the URLs.

| Scenario (profile id) | Pattern and source | CARC / RARC (group, level) | Status | Payers | Category / expected action |
|---|---|---|---|---|---|
| `elig_coverage_terminated` | Coverage ended before the DOS (BCBS Tennessee remittance crosswalk 27/N30) | CO-27 N30 (claim) | documented | any | eligibility / clinic_question |
| `elig_ma_enrollee_billed_to_medicare` | Beneficiary enrolled in an MA plan on the DOS; FFS Medicare denies "covered under a capitation agreement/managed care plan" (Noridian CO-24 article) | CO-24 (claim) | documented | GBMB1 | eligibility / corrected_claim: a new claim (frequency 1) to the MA plan the letter names |
| `auth_ma_plan_of_care_absent` | UHC/Optum Medicare Advantage: a visit after the first 6 in the episode, the plan of care never requested (UHC notice 2024-2025; 197/N54 the usual pairing) | CO-197 N54 (line, CMT) | typical | GPMA2 | authorization / appeal (retro-authorization with the plan of care) |
| `auth_number_not_on_claim` | The UM vendor's or Medicaid's authorization number was left off the claim; CARC 15 was deactivated 05/01/2018, so 16 + M62 | CO-16 M62 (claim) | typical | GPMA2, SWPPO3, TLMC4 | authorization / corrected_claim |
| `auth_medicaid_service_limit` | North Dakota / West Virginia Medicaid: more than 20 manipulations in the calendar year without a service authorization (manuals: "denied as non-covered"; 119/N640 the usual pairing); the visit note on file says nothing about an authorization | CO-119 N640 (line, CMT) | typical | TLMC4 | authorization / clinic_question (a service authorization request, never a retro-authorization appeal) |
| `modifier_at_missing` | 98940-98942 without AT is adjudicated as maintenance therapy (BPM ch. 15 sec. 240.1.3; CPM ch. 12 sec. 220: CO-50; Noridian pairs 50 with N115) while the notes document active treatment | CO-50 N115 (line, CMT without AT) | documented | GBMB1, GPMA2 | modifier / corrected_claim (add AT; coding change with a quoted note) |
| `modifier_at_missing_maintenance_care` | The same edit when the notes document maintenance care: AT must never be added to maintenance care and the denial stands (BPM ch. 15 sec. 240.1.3; research: "If the note shows maintenance ... do not add AT") | CO-50 N115 (line, CMT without AT) | documented | GBMB1, GPMA2 | modifier / no_action |
| `modifier_gp_missing` | UHC and affiliates require GP on "always therapy" codes since 2019 (Illinois Chiropractic Society, Chiropractic Economics); M78 is deactivated, N822 is the active remark | CO-4 N822 (line, therapy) | typical | GPMA2, SWPPO3 | modifier / corrected_claim (add GP) |
| `modifier_25_missing_em_with_cmt` | An established patient's E/M on the same day as CMT (000 global) without 25; NCCI ch. 11; N626 is the remark X12 created for this denial | CO-97 N626 (line, E/M) | typical | GPMA2, SWPPO3, TLMC4 | modifier / corrected_claim (add 25) |
| `duplicate_exact` | Exact duplicate claim (Noridian OA-18 N522; the X12 usage note for CARC 18 asks for Group OA, some commercial crosswalks show CO) | OA-18 N522 (claim) | documented | any | duplicate / no_action |
| `duplicate_second_cmt_same_day` | A second manipulation the same day after a new injury, billed as a second line of the CMT code; spinal CMT is payable once per patient per day (Premera CP.PP.390, ND Medicaid, Medicare one manipulation a day), so no repeat modifier cures it | CO-18 M86 (line) | typical | SWPPO3 | duplicate / no_action |
| `timely_filing_expired` | Claim received after the filing window (BCBS Tennessee crosswalk 29) | CO-29 (claim) | documented | any (in the fixture only SWPPO3: the generator's 150-day service window holds only a 90-day filing limit plus the lag) | timely_filing / no_action |
| `timely_filing_in_window` | Timely filing applied although the claim was accepted inside the window; the clearinghouse acceptance report is the proof. Not the Medicare contractor: "a claim denied for timely filing is not afforded appeal rights" (Noridian) | CO-29 (claim) | documented | GPMA2, SWPPO3, TLMC4 | timely_filing / appeal |
| `med_nec_maintenance_review` | AT billed but medical review judged the care maintenance (CERT 2024: 33.6% improper payment rate, 95.5% insufficient documentation; Noridian CO-50/N115 top-5 denial) while the notes show active treatment and measurable improvement | CO-50 N115 (line, CMT with AT) | documented | GBMB1, GPMA2 | medical_necessity / appeal |
| `med_nec_um_vendor_review` | The commercial plan's UM vendor found the visit not medically necessary on peer review while the re-examination documents measurable progress (ASH process; Aetna CPB 0107; Cigna CPG 278; N10 the review-organization remark) | CO-50 N10 (line, CMT) | typical | SWPPO3 | medical_necessity / appeal |
| `diagnosis_subluxation_primary_missing` | The claim led with a symptom code instead of M99.0x (SE1601; Palmetto A56616; WPS A56273; OIG $21M paid without a covered primary diagnosis) | CO-50 N115 (line, CMT) | documented | GBMB1, GPMA2 | medical_necessity / corrected_claim (the documented M99.0x leads the CMT line) |
| `diagnosis_inconsistent_with_cmt` | Diagnosis does not support the CMT level (BCBS Tennessee crosswalk 11/N657; ND Medicaid two-diagnosis rule) | CO-11 N657 (line, CMT) | documented | GPMA2, SWPPO3, TLMC4 | medical_necessity / corrected_claim (the documented M99.0x leads the CMT line) |
| `bundling_97140_separate_region` | 97140 to an extremity with CMT, billed without 59/XS although the notes show a region not manipulated (NCCI ch. 11 sec. S; BCBS Vermont; Premera): the research fix is a corrected claim adding XS (an appeal only when XS and the extremity diagnosis were already on the claim) | CO-97 M15 (line, 97140) | typical | GPMA2, SWPPO3 | bundling / corrected_claim (XS, coding change) |
| `bundling_97140_same_region` | 97140 in a spinal region that received CMT: never separately reportable (NCCI; CPM ch. 23 sec. 20.9.3.1: CO-236) | CO-236 N20 (line, 97140) | documented | GPMA2, SWPPO3 | bundling / no_action |
| `cob_other_coverage_primary` | Another plan is primary; Medicaid is the payer of last resort (ND Medicaid; 22/N4 the usual pairing) | CO-22 N4 (claim) | typical | GPMA2, SWPPO3, TLMC4 | coordination_of_benefits / clinic_question |
| `cob_medicare_secondary_payer` | Medicare's records show other coverage is primary (Noridian 22/N598) | CO-22 N598 (claim) | documented | GBMB1 | coordination_of_benefits / clinic_question |
| `cob_misrouted_wrong_payer` | Claim sent to the wrong plan (an MCO or MA plan); the letter names the responsible plan (109/N418) | CO-109 N418 (claim) | typical | any | coordination_of_benefits / corrected_claim (a new claim to the named plan) |
| `cob_work_injury_liability` | Injury diagnosis with a work accident: the workers' compensation carrier is liable (BCBS Tennessee crosswalk 19/N418; Cigna CPG 278) | CO-19 N418 (claim) | documented | SWPPO3 | coordination_of_benefits / clinic_question (carrier, claim number, date of injury) |
| `missing_initial_treatment_date` | Chiropractic claim without the initial treatment date (CMS-1500 Item 14 / 837P DTP*454): unprocessable, no appeal rights (CPM ch. 12 sec. 220.D.2; Noridian) | CO-16 MA122 MA130 (claim) | documented | GBMB1, GPMA2 | missing_information / clinic_question (the question quotes the date the note states) |
| `missing_ordering_provider_xray` | X-ray line from a chiropractor sent without the ordering provider name/NPI (Palmetto FAQ: required "even though these services are non-covered"; Noridian index 16 / N264, N265); the note says the treating chiropractor ordered the films | CO-16 N264 N265 (line, X-ray) | documented | GBMB1 | missing_information / corrected_claim (the rendering chiropractor reported as the ordering provider) |
| `missing_records_not_received` | The UM vendor asked for records and denied for lack of information (Anthem/ASH FAQ; N29/N225 deactivated 2016, N706 active) | CO-226 N706 (claim) | typical | GPMA2, SWPPO3 | missing_information / clinic_question |
| `contractual_gz_maintenance` | Maintenance CMT billed with GZ (no ABN): auto-denied, provider liable (CPM ch. 1 sec. 60.5: Group CO, CARC 50, RARC N/A) | CO-50 (line, CMT with GZ) | documented | GBMB1 | contractual / no_action |
| `pr_xray_without_gy` | X-ray by a chiropractor billed without GY: provider-type exclusion, patient liable (Palmetto "X-Rays: Denied for Chiropractors"; First Coast PR-170 tips) | PR-170 N95 (line, X-ray) | documented | GBMB1 | patient_responsibility / no_action |
| `other_ppo_visit_limit` | Annual chiropractic visit maximum reached (SISC/ASH 30 visits; BCBS Tennessee crosswalk 119/N587); the clinic's visit log disagrees with the accumulator | CO-119 N587 (line, CMT) | documented | SWPPO3 | other / clinic_question (verify the accumulator against the visit log) |

Non-denials on paid claims: CO-45 contractual write-off, PR-1 / PR-2 / PR-3 deductible, coinsurance
and copay (a copay or deductible may take a whole allowed amount, leaving a processed claim with
nothing paid); CO-253 sequestration (2%) on every Medicare and MA payment; and, on the Medicare
contractor's and the MA plan's claims, the statutorily excluded lines (GY) denied PR-96 N425 (CPM
ch. 1 sec. 60.5; Noridian top denials Q4 2025). None of these creates a work item on its own
(`docs/SPEC.md` section 6). The expected denials that are not exempt under that rule do create a
work item, classified as informational and proposed `no_action` for the reviewer to close: the GZ
line (CO-50, `contractual`) and the X-ray without GY (PR-170, `patient_responsibility`); both
point at a billing process to fix (an ABN before maintenance visits, GY on the clinic's films).

Research patterns the profile does not carry as a scenario (candidates for the next version): the
198/N351 "visits exceed the authorization" variant, the AT + GA invalid combination (16/N519), the GA
maintenance line (PR-50), the region-count downcode (50/M25), the B22 level/condition edit, the
Medicaid adjunctive-therapy exclusion (96/N130), MUE unit denials (151/N362), out-of-network
UM-vendor denials (242), the E/M bundled into CMT with M15 (the bundling playbook proposes modifier
25 for it, never 59 or XS), and a Medicare timely-filing denial with N211 (the timely-filing playbook
asks the clinic for proof of a date-of-service or administrative error instead of appealing).

## 3. Coding and coverage rules the profile encodes

- **CMT codes and regions.** 98940 = 1-2 spinal regions, 98941 = 3-4, 98942 = 5, 98943 = extraspinal;
  the five regions are cervical, thoracic, lumbar, sacral and pelvic; one spinal CMT per day, one unit.
  The note names the segments adjusted (a region name alone is insufficient for Medicare review):
  every CMT `detail` in the profile maps each region it names to the segment adjusted there, as many
  regions as the code requires, and a note that names the subluxation segment names one of those.
- **Diagnosis pairs by region.** Primary = the subluxation level, M99.01 (cervical), M99.02
  (thoracic), M99.03 (lumbar) or M99.05 (pelvic); secondary = the neuromusculoskeletal condition of
  the same region: cervical M54.2, M54.12, M50.30, M47.812, S13.4XXA; thoracic M54.6, S23.3XXA;
  lumbar M54.16, M51.360, M47.816, S33.5XXA; pelvic S33.6XXA; M54.50 (lumbar or pelvic) and M62.830
  (thoracic or lumbar) serve either of their regions. The CMT line points to one pair per treated
  region, two pairs at most (a service line points to four diagnoses at most, CMS-1500 Item 24E /
  837P SV107, so for 3-5 regions the two most pertinent pairs go on the line and the record holds the
  rest); the generator draws the pairs from the regions the
  CMT detail names (`pair_regions`), so a level never meets a condition of another region. M99.00
  and M99.04 are not billed. Extremity conditions (M25.511 / M25.512 shoulder, M25.551 / M25.552
  hip, M25.561 / M25.562 knee) only for extraspinal work and manual therapy to a region not
  manipulated. A therapy line points to the condition of the first pair and its note text names an
  exercise for that region; an X-ray series matches a treated region (72040 cervical, 72070 thoracic,
  72100 lumbosacral). G44.86, G44.209, R51.9, M54.9 and M79.10 are billed by no encounter: they are
  the declared pool the two inconsistent-diagnosis scenarios swap onto the claim.
- **Modifier AT** on every 98940-98942 line billed to the Medicare contractor, the MA plan and the
  Medicaid plan (active/corrective treatment); never on E/M or X-ray lines; never with GA/GY/GZ/GX
  on the same line; never on maintenance care. The commercial PPO does not use AT.
- **ABN modifiers.** GA = ABN on file, patient liable (Group PR); GZ = no ABN, provider liable (Group
  CO, CARC 50, no RARC); GY = statutorily excluded (Group PR, CARC 96, RARC N425). The clinic bills GY
  on X-ray, E/M, therapy and 98943 lines to the Medicare contractor and GY on X-ray and 98943 lines to
  the MA plan.
- **GP** on every physical-medicine line (97012, G0283 / 97014, 97035, 97110, 97140) billed to the MA
  plan and the PPO; GP plus GY to the Medicare contractor (the MAC rejects a therapy line without a
  therapy modifier as unprocessable even though it is non-covered). Timed codes (97110, 97140) are
  15-minute units; 97012 and G0283 / 97014 are untimed, once per visit; 97035 is 8 minutes, one unit.
- **Modifier 25** on an E/M billed with CMT only when the visit is significant and separately
  identifiable (new patient, new injury, re-examination that changes the plan); an E/M bundled into
  CMT never takes 59 or an X modifier.
- **NCCI.** 97140, 97124 and 97112 are not separately reportable in a spinal region undergoing CMT;
  in a different region (an extremity) they take 59 or XS and point to that region's diagnosis.
  `app/playbooks/modifier_rules.yaml` lists the pairs; 97012/97014 with 97140 is not an edit the
  research supports and is not listed. Manual therapy in a manipulated region appears in the fixture
  only with the scenario that denies it (`scenario_only`), so no paid claim carries the pattern.
- **Once per day.** Spinal and extraspinal CMT are one code, one unit per day: a second same-day line
  is never payable (`once_per_day_code_ranges`).
- **Initial treatment date** (Item 14 / DTP*454) on every Medicare and MA chiropractic claim. The
  claim export in this repository has no field for it (`app/schemas.py`, a foundation file), so the
  playbook asks the clinic for it, quoting the date the note on file states.
- **Ordering provider.** The treating chiropractor orders the office's X-rays; the Medicare
  contractor and the MA plan get the rendering NPI in the ordering/referring field of every claim
  with an X-ray line (`ordering_provider_kinds`), the scenario that denies a claim without it aside.
- **Corrections to the Medicare contractor** are reopenings (within one year) or, for an
  unprocessable claim or one that belongs to another plan, new claims; Medicare does not accept a
  frequency-7 replacement. The profile's GBMB1 letters say so (`letters` on the payer). The app still
  builds every correction as a frequency-7 replacement (assumption 25).
- **Fee schedule.** Charges per the research ranges (98940 $60, 98941 $78, 98942 $98, 97110 $55 per
  unit, 97140 $55 per unit, 72040 $95, 99203 $205, ...); allowed ratios 0.50 (Medicare contractor),
  0.55 (MA), 0.72 (PPO), 0.45 (Medicaid) against 2026 national Medicare amounts, national commercial
  averages and Medicaid state medians; Medicare and MA payments less 2% sequestration.
- **Maintenance care** is the single most common medical-necessity finding: a stable condition with no
  expectation of objective improvement. The profile's SOAP notes for active care carry the elements
  the MACs ask for (history of onset, PART findings with at least two of the four criteria, one of
  them asymmetry or range of motion, the subluxation level, a plan whose frequency, length and visit
  count agree, measurable goals, re-examination with outcome scores, the segments adjusted). A note
  sentence that documents maintenance care (`maintenance_documentation` in modifier_rules.yaml:
  "clinically stable", "no further objective improvement", "prevent deterioration", ...; "not
  maintenance care" is not one) never supports adding AT or a medical-necessity appeal of CMT.
- **Patients by payer.** Medicare and MA beneficiaries mostly 65 and over (a minority under 65 with
  disability or ESRD entitlement); PPO members of working age (a few still employed past 65);
  Medicaid adults under 65 (`age_bands`).

## 4. Assumptions for the certified coder (tick each one)

Payers and windows

1. The pilot's Medicare contractor emits CO-50 with N115 for a CMT line without AT (Noridian pattern);
   some MACs use M25 or no RARC, and whether the edit ever returns CO-4 or CO-16 must be read from real
   835s. The rule `modifier_50_cmt_without_at` keys on the line's modifiers, not on the RARC, except
   that a review finding (N10), a missing record (M127) or an unsupported level of service (M25) makes
   it a medical-necessity review. The rule cannot see the payer yet (assumption 26).
2. Filing, corrected-claim and appeal windows: Medicare 365 / 365 / 120, MA 365 / 365 / 65, PPO
   90 / 90 / 180, Medicaid 180 / 180 / 60. Commercial windows are contract-driven; the MA
   reconsideration window may be modelled as 60 days from receipt (regulation) or 65 from the remittance.
3. The MA plan pays E/M and therapies under a supplemental benefit and non-covers X-rays and 98943.
   Real MA plans differ: some pay nothing beyond the Medicare CMT benefit, some add a
   routine-chiropractic rider with a flat copay.
4. The Medicaid plan covers CMT (20 per year without authorization), limited E/M with 25 and spinal
   X-rays, and excludes adjunctive therapies and 98943; the clinic bills those to the patient and not
   to the plan (North Dakota and West Virginia shapes; the pilot's state may differ). Its over-limit
   denial is answered by a service authorization request, never a retro-authorization appeal.

Codes and modifiers

5. Non-CMT lines billed to the Medicare contractor with GY deny PR-96 N425 (Noridian); an X-ray line
   without GY denies PR-170 N95 (Palmetto, First Coast; the N95 remark is the usual one, not shown by
   those pages). Which fires first when both GY is missing and NCCI applies is MAC-specific.
6. A GZ maintenance line denies CO-50 with no RARC (CPM ch. 1 sec. 60.5), is classified
   `contractual`, and is never appealed. A GA line (PR-50) would be `patient_responsibility`; the rule
   exists but the fixture carries no GA scenario.
7. The E/M-with-CMT remark: the profile uses N626 (classified `modifier`, add 25); payers may use M15
   or N19 instead, which the rules read as `bundling`; the bundling playbook then proposes modifier 25
   when the notes document a separately identifiable service and `no_action` otherwise, never 59 or XS
   (NCCI forbids 59 and X{EPSU} on E/M codes).
8. The GP requirement applies to the MA plan and the PPO (UnitedHealthcare model); Humana requires 97
   instead; the pilot's main commercial payer decides.
9. NCCI pairs listed in `modifier_rules.yaml`: 98940/98941/98942 (column 1) with 97140, 97124 and
   97112 (column 2), 97140 with 97124; modifier indicator 1 (59/XS allowed). Confirm against the
   quarterly practitioner PTP file for the dates of service, including whether 98943 pairs with 97140.
   A claim that left 59/XS off a documented separate-region service gets a corrected claim; an appeal
   only when the modifier was already billed.
10. MUE values: CMT 1 unit per day; timed therapy codes 1-2 units in the fixture (97110's practitioner
    MUE is 6; 97140's 4 or 6 depending on the source). The fixture never exceeds them.
11. Modifier 25 is required only for a significant, separately identifiable E/M; the fixture's
    routine visits carry no E/M, and its initial, re-examination and exacerbation visits carry one.
    The modifier-25 scenario uses established patients only.
12. AT is never placed on E/M or X-ray lines (ND Medicaid denies 4/N519); the playbook removes it
    from any line outside 98940-98942.

Diagnoses and documentation

13. Secondary diagnosis mandatory (Palmetto 2015 guideline, Noridian per-region pairs) rather than
    recommended (SE1602); every CMT line in the fixture points to a level and a condition of the same
    region. Whether M99.00 (head/occiput) is accepted differs by MAC (Palmetto lists M99.01-M99.05
    only); the profile bills neither M99.00 nor M99.04 (a sacral region adjusted on 98942 is kept in
    the record, not paired on the claim).
14. The secondary codes billed are from the WPS A56273 Groups 2-4 lists (short, moderate and long-term
    conditions): M54.5 is billed as M54.50 (the parent code is not billable since Oct 2021) and M51.36
    as M51.360 (split into six-character codes on 2024-10-01; M51.360 is "with discogenic back pain
    only"). S33.6XXA (sacroiliac sprain) is in Group 3. G44.86 is not on those lists; it appears only
    as a swapped-in code the scenario says is wrong.
15. Region counts in the notes match the CMT code (98940: one or two regions, 98941: three or four,
    98942: five); for 3-5 regions the CMT line carries the two most pertinent diagnosis pairs (a line
    points to four diagnoses at most, Item 24E) and the record holds the rest; a condition that covers two treated regions (low
    back pain for lumbar and pelvic) may serve both pairs.
16. The initial treatment date is reported in Item 14 / DTP*454 and the X-ray date (Item 19 /
    DTP*455) only when an X-ray demonstrated the subluxation; Palmetto and WPS recommend leaving Item 19
    empty otherwise. The claim export does not carry either field.
17. The SOAP note elements the MACs require (history, PART with two of four criteria, level, plan
    with frequency / duration / goals / objective measures, subsequent-visit change assessment, the
    segments manipulated, legible signature with credentials) are what the profile's templates
    produce; confirm the templates read like the clinic's own notes. A plan's frequency, length and
    visit count are drawn together (3 x 2 + 2 x 4 = 14 visits) and a visit number never exceeds them.
18. A maintenance denial with notes that document measurable improvement (pain scale, range of
    motion, Oswestry / Neck Disability Index) is appealed; with notes that document maintenance care
    the denial stands and an ABN is obtained going forward (`modifier_at_missing_maintenance_care`
    carries that variant for the no-AT edit; the medical-necessity playbook applies the same rule to
    a maintenance finding on an AT line). Notes that document both go to the coder.

Remittance codes

19. Commercial benefit-maximum denials are emitted CO-119 N587 (write-off unless the accumulator is
    wrong); a PR-119 would be the patient's balance and, under SPEC section 6, never a work item. The
    question asks the clinic to compare the accumulator with its visit log.
20. Medicaid over-limit denials are emitted CO-119 N640 and classified `authorization`; 273/N435 is
    the alternative pairing some states use.
21. Deactivated codes the profile no longer emits: CARC 15 (2018), RARC M78 (2006), N29 and N225
    (2016). The rules still recognise them in case a real payer emits them.
22. Duplicate denials: OA-18 N522 for every payer, as the X12 usage note for CARC 18 asks (some
    commercial crosswalks show CO-18). A second same-day CMT is never payable (one manipulation per
    patient per day), so no repeat modifier is proposed for 98940-98943; 76/77/91 remain for services
    that can legitimately repeat (a repeat radiograph, a laboratory test).
23. Work-injury redirects are emitted CO-19 N418 (BCBS Tennessee); CARC 20 (liability) and 21
    (no-fault) are mapped to the same category; the question asks for the carrier, its claim number
    and the date of injury.
24. Fee amounts: 2026 national Medicare 98940 $26.72, 98941 $38.41, 98942 $49.77 (trade sources); the
    pilot's locality amounts come from the CMS PFS look-up and the clinic's own charge master. The
    Medicare contractor and the MA plan withhold 2% sequestration (CO-253) from every payment.
25. Medicare Part B corrections are reopenings or new claims, never frequency-7 replacements (CPM
    ch. 34; Noridian reopening page). The profile's letters say so; the app's corrected-claim builder
    (`app/drafting/corrected_claim.py`) still emits frequency 7 for every payer, and a payer redirect
    (CO-24 / CO-109 with a named plan) is the only new claim (frequency 1) it builds. Changing that
    needs a payer correction method in the foundation (`docs/PHASES.md`, open items).
26. AT is a Medicare / MA / Medicaid requirement. The modifier playbook adds it only for the claim
    filing indicators MB, 16 and MC once the remittance context carries the filing indicator (CLP06);
    until the foundation adds that field, every AT proposal tells the reviewer to confirm the payer
    requires AT, and a commercial CO-50 on a CMT line is still classified `modifier`.
27. Patient ages: Medicare 88% / MA 92% aged 65-92, the rest 40-64; PPO 18-64 (5% 65-72); Medicaid
    18-64.

## 5. Sources behind the scenarios (URLs from the research briefs)

- Eligibility: BCBS Tennessee remittance crosswalk https://content.bcbst.com/api/public/content/commercial-remittance-advice-code-descriptions.pdf;
  Noridian CO-24 https://med.noridianmedicare.com/web/jeb/topics/claim-submission/reason-code-guidance/medicare-advantage-plan
- MA plan of care: https://www.uhcprovider.com/en/resource-library/news/2025/med-adv-outpatient-therapy-pa.html;
  https://inte.asha.org/news/2024/unitedhealthcare-announces-broad-prior-authorization-requirements-for-therapy-and-chiropractic-services-under-its-medicare-advantage-plans/
- Authorization codes and deactivations: https://x12.org/codes/claim-adjustment-reason-codes; https://x12.org/codes/remittance-advice-remark-codes
- Medicaid service limit: https://www.hhs.nd.gov/sites/www/files/documents/medicaid-policies/chiropractic-services.pdf;
  https://bms.wv.gov/media/41169/download; https://bms.wv.gov/media/40980/download
- AT, maintenance, GZ / GA / GY: CMS BPM ch. 15 https://www.cms.gov/Regulations-and-Guidance/Guidance/Manuals/Downloads/bp102c15.pdf;
  CPM ch. 1 https://www.cms.gov/Regulations-and-Guidance/Guidance/Manuals/Downloads/clm104c01.pdf;
  CPM ch. 12 https://www.cms.gov/Regulations-and-Guidance/Guidance/Manuals/Downloads/clm104c12.pdf;
  SE1602 https://www.cms.gov/outreach-and-education/medicare-learning-network-mln/mlnmattersarticles/downloads/se1602.pdf;
  Noridian https://med.noridianmedicare.com/web/jeb/specialties/chiropractic/claim-submission-requirements and
  https://med.noridianmedicare.com/web/jeb/topics/modifiers/gz;
  CMS compliance tips https://www.cms.gov/training-education/medicare-learning-networkr-mln/compliance/medicare-provider-compliance-tips/chiropractic-services;
  Noridian top denials Q4 2025 https://med.noridianmedicare.com/web/jeb/article-detail/-/view/10525/top-denials-and-solutions-q4-2025
- GP: https://ilchiro.org/new-uhc-optum-modifier-requirements-for-therapy/
- Modifier 25, NCCI pairs: NCCI ch. 11 https://www.cms.gov/files/document/11-chapter11a-ncci-medicare-policy-manual-2026-final.pdf;
  CPM ch. 23 https://www.cms.gov/Regulations-and-Guidance/Guidance/Manuals/Downloads/clm104c23.pdf;
  https://bluecrossvt.org/documents/tip-sheet-physical-medicine-codes-chiropractic-manipulative-treatment;
  Premera CP.PP.390 https://www.premera.com/portals/provider/paymentpolicies/cmi_162304.pdf
- Duplicates and once per day: https://med.noridianmedicare.com/web/jeb/topics/claim-submission/reason-code-guidance/exact-duplicate-claim-service;
  Premera CP.PP.390 (above); Palmetto A56616 https://www.cms.gov/medicare-coverage-database/view/article.aspx?articleid=56616
- Timely filing: https://med.noridianmedicare.com/web/jeb/topics/claim-submission/reason-code-guidance/timely-filing
- Medical necessity and the UM vendor: Noridian documentation guidelines https://med.noridianmedicare.com/web/jeb/specialties/chiropractic/documentation-guidelines;
  https://ilchiro.org/?p=4432; Aetna CPB 0107 https://www.aetna.com/cpb/medical/data/100_199/0107.html;
  Cigna CPG 278 https://v.static.cigna.com/assets/chcp/pdf/coveragePolicies/medical/cpg278_chiropractic_care.pdf
- Primary diagnosis: SE1601 https://www.cms.gov/outreach-and-education/medicare-learning-network-mln/mlnmattersarticles/downloads/se1601.pdf;
  WPS A56273 https://www.cms.gov/medicare-coverage-database/view/article.aspx?articleId=56273;
  https://www.stedi.com/changelog/new-claim-edit-invalid-primary-diagnosis-on-medicare-chiropractic-claims;
  M51.36 split https://www.chiroeco.com/what-you-need-to-know-about-the-new-2025-icd-10-codes-for-chiropractic/amp/
- Coordination of benefits: https://med.noridianmedicare.com/web/jeb/topics/claim-submission/reason-code-guidance/medicare-is-secondary-payer;
  https://med.noridianmedicare.com/web/jeb/topics/claim-submission/denial-resolution
- Initial treatment date: https://med.noridianmedicare.com/web/jeb/topics/claim-submission/reason-code-guidance/chiropractic-services-initial-treatment-date;
  https://www.stedi.com/changelog/new-claim-edit-missing-initial-treatment-date-for-medicare-chiropractic-claims
- Ordering provider on X-rays: Palmetto FAQ https://www.dmepdac.com/palmetto/rr.nsf/DID/DDUWHI2PXW
- Records not received: https://www.tustin.k12.ca.us/uploaded/District_Office/Personnel_Services/New_forms/Anthem_Chiropractic_FAQ.pdf
- X-ray without GY: https://www.dmepdac.com/palmetto/rr.nsf/DID/8EELMZ6473; https://medicare.fcso.com/claims/tips-prevent-claim-adjustment-reason-code-carc-pr-170
- Visit limit: https://santacruzcoe.org/wp-content/uploads/2022/10/SISC-HMO-Acupuncture-and-Chiro-Benefits-Flyer-FINAL.pdf
- Sequestration and 2026 amounts: https://cdn.ymaws.com/wichiro.org/resource/resmgr/helpdesk/2026/2026medicare_physician__2_.pdf
- Reopenings: CPM ch. 34 https://www.cms.gov/Regulations-and-Guidance/Guidance/Manuals/Downloads/clm104c34.pdf;
  https://med.noridianmedicare.com/web/jfb/topics/appeals/reopening
