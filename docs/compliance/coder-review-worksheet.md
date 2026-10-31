# Coder Review Worksheet

Worksheet for a two-to-four-hour review of Plumbline's chiropractic coding assumptions and denial rules
with a certified coder.

| | |
|---|---|
| Coder name | |
| Credential and number | |
| Session date | |
| Session length | |
| BTP reviewer | Connor Betz |

The session uses synthetic examples only; no real patient information is shown unless the coder is a
member of the Clinic's workforce and the business associate agreement is in effect. The coder's answers
inform Plumbline's rules. The Clinic's own coder or the treating provider continues to confirm every
coding change before the Clinic submits it.

After the session, each correction is made in the classification rules, the modifier rules, the
chiropractic profile or the playbook code, a synthetic test is added for it, and the full test suite
is run.

## Agenda

| Time | Topic | Rows |
|---|---|---|
| 0:00 to 0:15 | What Plumbline does and does not do; how a rule, a playbook and a quoted note excerpt fit together | |
| 0:15 to 0:45 | Payers and deadlines | A1 to A4 |
| 0:45 to 1:30 | Codes, modifiers and the modifier playbook | A5 to A12, M1 to M16 |
| 1:30 to 1:40 | Break | |
| 1:40 to 2:20 | Diagnoses and documentation | A13 to A18, K1 |
| 2:20 to 2:50 | Remittance codes, corrections, payers and ages | A19 to A27 |
| 2:50 to 3:40 | Classification rule families | F1 to F13 |
| 3:40 to 4:00 | Optional spot check of labeled examples; open questions; sign-off | |

A shorter session can end at 2:50 and cover the rule families separately.

## Part 1. Coding Assumptions

| No. | Assumption | Where it is applied | Confirmed | Correction | Notes |
|---|---|---|---|---|---|
| A1 | The Medicare contractor denies a CMT line without modifier AT as CO-50 with RARC N115 (some contractors use M25 or no RARC); the rule keys on the line's modifiers, not the RARC, except that a review finding (N10), a missing record (M127) or an unsupported level of service (M25) makes it a medical-necessity review. At a contractor that attaches M25 to the no-AT denial, every such line therefore goes to the medical-necessity playbook, which never adds AT and usually proposes an appeal instead. Which remark does the Clinic's contractor attach to the no-AT denial, and should a CO-50 M25 line without AT go to the modifier rule? | Rule `modifier_50_cmt_without_at` | ☐ | | |
| A2 | Deadlines (filing / corrected claim / appeal, in days): Medicare 365/365/120; Medicare Advantage 365/365/65; PPO 90/90/180; Medicaid 180/180/60. Plumbline counts each proposal's deadline from the remittance date: an appeal uses the appeal window, and every other proposal, clinic questions included, uses the corrected-claim window. A Medicare claim returned as unprocessable (CO-16 with MA130, for example for a missing initial treatment date, MA122) must come back as a new claim within 12 months of the date of service, so its displayed deadline falls after that limit. Should the deadline for such a claim be the earlier of that limit and the corrected-claim deadline? The appeal windows are for the first level; Plumbline has no window for a later level. The Medicare Advantage 65 days is the regulatory reconsideration window (42 CFR 422.582). If the Clinic is in network with the plan, does the plan contract set a different window? | Payer settings; deadline calculation | ☐ | | |
| A3 | The Medicare Advantage plan pays E/M and therapy services under a supplemental benefit and does not cover X-rays or 98943 | Chiropractic profile | ☐ | | |
| A4 | Medicaid covers CMT (20 visits a year without authorization), limited E/M with modifier 25 and spinal X-rays, and excludes adjunctive therapies and 98943 | Chiropractic profile | ☐ | | |
| A5 | Non-CMT lines billed to Medicare with GY deny PR-96 N425; an X-ray line without GY denies PR-170 N95; which edit applies first varies by contractor | Profile; rule families F1 and F11 | ☐ | | |
| A6 | A GZ maintenance line denies CO-50 with no RARC, is contractual and is never appealed; a GA line (PR-50) is patient responsibility | Rule family F1 | ☐ | | |
| A7 | The remark code for E/M billed with CMT is N626; M15 or N19 would classify the denial as bundling | Rule families F6 and F9 | ☐ | | |
| A8 | GP is required on therapy codes by the Medicare Advantage plan and the PPO; some payers require modifier 97 instead | Modifier rule M10 | ☐ | | |
| A9 | NCCI pairs: 98940, 98941 and 98942 with 97140, 97124 and 97112; 97140 with 97124; modifier indicator 1; whether 98943 pairs with 97140 | Modifier rule M11 | ☐ | | |
| A10 | Medically unlikely edits: CMT one unit per day; timed therapy codes one to two units in the examples | Chiropractic profile | ☐ | | |
| A11 | Modifier 25 only for a significant, separately identifiable E/M service beyond the pre-manipulation assessment (for example a new injury, or an initial or re-examination with its own documented history, examination and decision making); new-patient status alone is not enough (NCCI ch. 11). The modifier and bundling playbooks count any note sentence that contains one M5 keyword as support, including "re-examination", "evaluation and management" and "medical decision making". Are the M5 keywords specific enough, or would an ordinary E/M note match them? | Modifier rule M5 | ☐ | | |
| A12 | AT is never billed on E/M or X-ray lines and is removed from any line outside 98940 to 98942 | Modifier rule M8 | ☐ | | |
| A13 | A secondary diagnosis is treated as mandatory, one pair per region; M99.00 and M99.04 are not billed. WPS A56273 and Palmetto A56616 list M99.04 as a valid primary diagnosis, and the sacral region is one of the five CMT regions. Is M99.04 billed for a sacral-region adjustment? | Chiropractic profile | ☐ | | |
| A14 | Secondary codes are drawn from the WPS A56273 groups; M54.5 is billed as M54.50 and M51.36 as M51.360 | Chiropractic profile | ☐ | | |
| A15 | Region counts match the CMT code. For three to five regions the claim carries only the two most pertinent diagnosis pairs, and the record holds the rest. The four-diagnosis limit applies to a line (one pointer per line on a Medicare paper claim); the claim itself can list up to 12 diagnoses (Item 21). Should the claim list the subluxation level of each region treated, with the CMT line leading with a level? | Chiropractic profile | ☐ | | |
| A16 | Item 14 / DTP*454 carries the initial treatment date or the date of an exacerbation. Item 19 / DTP*455 carries the X-ray date only when an X-ray taken within 12 months before or 3 months after the start of treatment demonstrated the subluxation. Neither field is in the claim export. When a denial carries MA122 or MA121, the playbook asks the Clinic for the date; its questions mention neither the exacerbation date nor the X-ray window | Missing-information playbook | ☐ | | |
| A17 | The note elements the Medicare contractors require are the ones the example notes contain | Chiropractic profile | ☐ | | |
| A18 | With notes on file, the playbook proposes an appeal of a maintenance denial whenever no note sentence matches the M16 maintenance wording, unless a note names a diagnosis code the claim lacks (it then proposes a corrected claim, M15). The appeal quotes the sentences that contain a K1 keyword, including generic words such as "treatment" or "assessment"; with none, it quotes the opening of the latest note and flags weak support. The denial stands, with an ABN advised going forward, only when the M16 wording appears; the coder decides when another sentence also contains a K1 keyword. Should an appeal require a sentence documenting measurable improvement (a pain-scale, range-of-motion or outcome-score change)? | Medical-necessity playbook | ☐ | | |
| A19 | Commercial benefit-maximum denials arrive as CO-119 N587 (written off unless the accumulator is wrong); PR-119 is the patient's balance | Rule families F11 and F13 | ☐ | | |
| A20 | Medicaid over-limit denials arrive as CO-119 N640 and are classified as authorization; 273/N435 is an alternative | Rule family F5 | ☐ | | |
| A21 | Deactivated codes still recognized: CARC 15; RARCs M78, N29 and N225 | Rule families F5, F6 and F10 | ☐ | | |
| A22 | Duplicates: OA-18 for every payer; the rule keys on CARC 18 alone. The profile adds N522, the crossover-duplicate remark that Noridian pairs with OA-18; other payers may send another remark or none. A second same-day CMT is never payable (one manipulation per patient per day), so no repeat modifier is proposed for 98940 to 98943 | Rule family F7; modifier rules M13 and M14 | ☐ | | |
| A23 | Work-injury redirects arrive as CO-19 N418; CARC 20 (liability) and 21 (no-fault) map to the same category | Rule family F4 | ☐ | | |
| A24 | 2026 national Medicare amounts: 98940 $26.72, 98941 $38.41, 98942 $49.77; the pilot uses the Clinic's locality and charge master | Chiropractic profile | ☐ | | |
| A25 | Medicare Part B corrections are reopenings or new claims, never frequency-7 replacements; Plumbline still builds a frequency-7 replacement, except for a redirect to another payer, which is a new claim | Corrected-claim builder; the profile's Medicare letters | ☐ | | |
| A26 | AT is a Medicare, Medicare Advantage and Medicaid requirement; until the claim filing indicator reaches Plumbline, every AT proposal asks the reviewer to confirm the payer requires AT | Modifier rule M9 | ☐ | | |
| A27 | Patient ages: Medicare 88% and Medicare Advantage 92% aged 65 to 92, the rest 40 to 64; PPO 18 to 64 (5% 65 to 72); Medicaid 18 to 64 | Chiropractic profile | ☐ | | |

## Part 2. Classification Rule Families (64 rules)

Rules are checked in order and the first match wins for each denial reason. A rule with a confidence below
0.75 sends the case to the model once the model is engaged; a reason that matches no rule becomes "other"
at 0.30. When a claim has several reasons, a higher-priority category can outrank a higher-dollar one, in
this order: timely filing, eligibility, coordination of benefits, authorization, duplicate, bundling,
modifier, medical necessity, missing information, other, contractual, patient responsibility.

| No. | Family and rules | What they match (category, confidence) | Confirmed | Correction | Notes |
|---|---|---|---|---|---|
| F1 | Line-specific chiropractic rules: `contractual_50_expected_denial_gz`, `patient_responsibility_50_abn`, `modifier_50_cmt_without_at`, `patient_responsibility_170_provider_type_pr` | CO, PI or OA-50 with GZ: contractual (0.90); PR-50: patient responsibility (0.85); 50 on 98940 to 98942 without AT, GA, GY, GZ or GX and without RARC N10, M127 or M25: modifier (0.85); PR-170: patient responsibility (0.85) | ☐ | | |
| F2 | Timely filing: `timely_filing_29` | CARC 29: timely filing (0.95) | ☐ | | |
| F3 | Eligibility: `eligibility_coverage_period`, `eligibility_member_identity`, `eligibility_24_managed_care`, `eligibility_16_patient_identifier`, `eligibility_109_wrong_payer` | 26, 27, 200 (0.90); 31, 32, 33, 140, 177 (0.90); 24 (0.85); 16 with N382 (0.85); 109 without N4 or N418 (0.80) | ☐ | | |
| F4 | Coordination of benefits: `cob_accident_liability_19_20_21`, `cob_109_other_payer`, `cob_22_other_coverage`, `cob_23_unpaid_secondary`, `cob_rarc` | 19, 20, 21 (0.85); 109 with N4 or N418 (0.85); 22 (0.90); 23 when nothing was paid (0.70); RARC N4 or MA04 (0.80) | ☐ | | |
| F5 | Authorization: `authorization_16_auth_number`, `authorization_core`, `authorization_119_service_limit`, `authorization_referral_243_288`, `authorization_rarc` | 16 with M62 or N54 (0.85); 197, 198, 15, 62, 39 (0.90); 119 or 273 with N640 or N435 (0.80); 243, 288 (0.80); RARC M62 or N54 (0.75) | ☐ | | |
| F6 | Modifier: `modifier_16_modifier`, `modifier_core`, `modifier_97_em_with_chiropractic_n626`, `modifier_rarc` | 16 with M78, N519, N822 or N823 (0.85); 4, 182 (0.90); 97 with N626 (0.85); those RARCs alone (0.75) | ☐ | | |
| F7 | Duplicate: `duplicate_18`, `duplicate_97_b20_n111`, `duplicate_rarc` | 18 (0.95); 97 or B20 with N111 (0.85); RARC N522 or M86 (0.80) | ☐ | | |
| F8 | Medical necessity: `medical_necessity_16_diagnosis`, `medical_necessity_core`, `medical_necessity_diagnosis_11`, `medical_necessity_level_frequency`, `medical_necessity_b22_diagnosis`, `medical_necessity_rarc`, `medical_necessity_noncovered_co` | 16 with M76 (0.80); 50, 56, 167 (0.90); 11 (0.85); 150, 151 (0.85); B22 (0.80); RARC N115, N661, M76, M25 or N10 (0.80); CO-96, 204 or 49 (0.50) | ☐ | | |
| F9 | Bundling: `bundling_core`, `bundling_multiple_procedure_59`, `bundling_rarc` | 97, 234, 236, 231 (0.85); 59 (0.70); RARC M15, N19, M144, N390 or N525 (0.80). CARC 59 is also the code for the therapy multiple-procedure payment reduction (MLN Matters MM7564), a partial payment that no modifier changes. A CO-59 adjustment still creates a work item. On a therapy line without 59 or an X modifier, the bundling playbook then proposes a corrected claim whenever a note sentence contains an M11 keyword: XS when a sentence names an extremity or a different site or region, 59 otherwise. Should a CO-59 adjustment be informational (no work item) instead of bundling? | ☐ | | |
| F10 | Missing information: the ten `missing_info_16_*` rules, `missing_info_documentation_252`, `missing_info_npi_206`, `missing_info_requested_226_227`, `missing_info_billing_error_125`, `missing_info_rarc`, `missing_info_16_default` | 16 with: MA122, MA121 or M122 (chiropractic claim fields); N264, N265, N285 or N286 (ordering or referring provider); M119 (NDC); N29, N225, M127, N706 or N393 (documentation); N56, M20 or M51 (procedure code); MA130; N179; N59; M79; N290 (0.75 to 0.85). 252, 206, 226 or 227, 125 (0.75 to 0.90); those RARCs alone (0.70); 16 alone (0.70) | ☐ | | |
| F11 | Patient responsibility: `patient_responsibility_cost_share`, `patient_responsibility_noncovered_pr`, `patient_responsibility_pr_generic` | 1, 2, 3, 66 (0.95); PR with 96, 204, 49 or 119 (0.85); any other PR (0.60). The cost-share codes and PR-96, 204, 49 or 119 alone create no work item unless the payer denied the whole claim (claim status 4); any other PR code creates one | ☐ | | |
| F12 | Contractual and informational: `contractual_informational` | 45, 94, 253, 23, 104, 118, 137, 144, 187, 237 with CO, PI, OA or CR (0.95). Alone, these create no work item unless the payer denied the whole claim (claim status 4). The exception is CARC 23 in group CO, PI or OA, which creates one | ☐ | | |
| F13 | Other (low-confidence rules the model reviews): `noncovered_plan_rules_co`, `benefit_maximum_co`, `claim_data_inconsistency`, `provider_not_eligible_b7`, `provider_type_not_payable_co`, `network_restriction_242`, `qualifying_service_b15` | CO-96, 204, 49 or 119 with N130 or N30 (0.50); CO-119 (0.50); 5, 6 (0.55); B7 (0.55); CO-170 or 185 (0.55); 242 (0.55); B15 (0.50) | ☐ | | |

For each family: do the Clinic's payers send these codes this way; is the category right for the fix the
Clinic would make; is the confidence reasonable; is a common code missing?

## Part 3. Modifier Playbook

A proposal that adds a modifier without a supporting note sentence is flagged as an unsupported coding
change, and its confidence is capped at 0.4. A sentence counts as support when it contains one keyword
from the situation's keyword list, in any note on file for the claim. Removing an invalid, repeated,
redundant or misplaced modifier (M1, M6 to M8) needs no supporting sentence. When two situations change
the same line, their supporting sentences are pooled, so a change with no supporting sentence of its own
is shown as supported by the other change's sentence. For example, when a 97140 line billed the same day
as CMT is denied for a missing GP and a note sentence supports only GP, the proposal adds GP and 59 at
confidence 0.8, with no unsupported flag. Every modifier change is a coding change. Approval requires a
quoted note excerpt attached to the work item; one excerpt covers every change in the proposal. Should
each change need its own supporting sentence?

| No. | Setting or situation | What it does | Confirmed | Correction | Notes |
|---|---|---|---|---|---|
| M1 | Valid modifiers list | Any modifier not on the list, on a line denied for a modifier problem, is removed as invalid. Is a legitimate modifier missing? | ☐ | | |
| M2 | Distinct-service modifiers (59, XS, XU, XE, XP) and laterality modifiers (50, LT, RT) | No 59 is added on top of these; no 50 is added on top of LT or RT | ☐ | | |
| M3 | E/M range 99202 to 99215; procedures with a global period 10000 to 69999 and 98940 to 98943 | An E/M line on the same day as any code in these ranges is treated as needing modifier 25 (M5). Modifier 25 applies only to a significant, separately identifiable E/M. The E/M at which the decision for a 090-day procedure is made takes 57, which the playbooks never propose, and not every code from 10000 to 69999 has a global period. The profile bills no code from 10000 to 69999 | ☐ | | |
| M4 | Modifier denial codes: CARC 4 and 182; RARC M78, N519, N822 and N823 | Codes that send a line to the modifier playbook | ☐ | | |
| M5 | E/M on the same day as a procedure | Adds 25 to the E/M line when the notes show a separately identifiable service (keyword list) | ☐ | | |
| M6 | Invalid or repeated modifier | Removes an invalid or repeated modifier | ☐ | | |
| M7 | 59 billed with an X modifier | Removes 59 and keeps the X modifier | ☐ | | |
| M8 | AT outside its codes | Removes AT from any line outside 98940 to 98942 | ☐ | | |
| M9 | AT required on CMT | Adds AT to a 98940 to 98942 line the denial names, unless the line carries GA, GX, GY or GZ. Any note sentence that contains one M9 keyword, such as "subluxation", "range of motion", "improvement" or "goals", counts as support, so a routine-visit note that only names a subluxation supports AT. Without such a sentence, AT is still proposed and flagged as unsupported. When a note sentence matches the M16 maintenance wording, AT is not added. If no other change is proposed, the denial stands, or the coder decides when another sentence contains an M9 keyword. AT is meant only for the Medicare, Medicare Advantage and Medicaid filing indicators; until the indicator reaches Plumbline (A26), it is proposed for every payer with a notice to confirm. Which words, on their own, show active, corrective treatment? | ☐ | | |
| M10 | GP required on therapy codes | Adds GP to a denied 97010 to 97799, G0283 or G0281 line; never with GO or GN | ☐ | | |
| M11 | Bundled pairs | Adds a modifier to the column-2 line of a same-day pair (98940, 98941 or 98942 with 97140, 97124 or 97112; 97140 with 97124) when that line carries no 59, XS, XU, XE or XP: XS when any note sentence names an extremity or a different site or region, 59 otherwise. A sentence that only says "separate", "distinct", "unrelated" or "second procedure" counts as support, even when no other region is named. The modifier is also added to a line denied for another modifier problem, such as a missing GP, where that change's sentence then counts as its support. NCCI ch. 11 allows 59 or XS with CMT only for therapy in a different region, and 59 or XU for 97140 with 97124 only for separate 15-minute intervals. Should XS be added only when a different region is documented, and should 97140 with 97124 take 59 or XU instead of XS? | ☐ | | |
| M12 | Bilateral service | Adds 50 to the one line the denial names, or to the only line of the claim, whenever any note sentence contains "bilateral", "both sides", "left and right" or a similar phrase, whatever the line's code: CMT, therapy, X-ray or E/M. For example, a CMT line denied for a missing AT, with an exam note that reads "bilateral paraspinal hypertonicity", comes back with AT and 50. CMT is billed by spinal region, not by side. Should this situation be removed for the chiropractic profile, whose codes (CMT, therapy, spinal X-rays, E/M) are not billed with 50? | ☐ | | |
| M13 | Repeat-service keywords | Words that indicate a distinct repeat service (modifier 76) or a repeat by another physician (modifier 77) | ☐ | | |
| M14 | Once-per-day codes 98940 to 98943 | A duplicate denial on one of these is never answered with a repeat modifier; the denial stands | ☐ | | |
| M15 | Primary diagnosis rule | For a 98940 to 98942 line the denial names, when an M99.0x code written in a note is missing from the claim and the line's first diagnosis pointer holds a code of another family, the level replaces that code in the claim's diagnosis list. The displaced code leaves the claim, so a claim that carried only the symptom code loses its secondary diagnosis (A13), and every other line that pointed to it now points to the level. The medical-necessity playbook makes this change. Should the displaced code stay on the claim as the line's second pointer instead? | ☐ | | |
| M16 | Maintenance-care wording; payer-specific modifiers | The phrases that mark maintenance care (they rule out AT and a medical-necessity appeal) and the modifiers flagged for re-derivation when a claim is redirected to another payer (GA, GX, GY, GZ, AT, GP, KX, 97) | ☐ | | |

Also review the keyword lists used by M5, M9, M10, M11, M12 and M16: would the Clinic's notes use these
words, and would a note that does not support the change also match them?

## Part 4. Medical-Necessity Keywords

| No. | Item | Confirmed | Correction | Notes |
|---|---|---|---|---|
| K1 | The diagnosis families whose words the medical-necessity playbook looks for in the notes when the claim carries a code of the family: M99 subluxation; M54 spine pain; M50 and M51 disc; M47 spondylosis; M43, M46, M48 and M53 other dorsopathies and spondylopathies; S13, S16, S23, S29, S33 and S39 sprain, strain and injury; G43, G44 and R51 headache; M25, M60, M62 and M79 muscle and extremity. It also looks for generic terms on every claim, such as "treatment", "assessment", "diagnosis" and "plan of care" | ☐ | | |

## Part 5. Open Questions and Sign-off

| No. | Question raised | Answer | Follow-up |
|---|---|---|---|
| | | | |

| | Signature | Date |
|---|---|---|
| Coder | | |
| BTP reviewer | | |
