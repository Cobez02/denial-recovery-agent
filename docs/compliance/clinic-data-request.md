# Clinic Data Request

This request lists exactly what to send for the Plumbline denial recovery pilot with Betz Technology
Providers (BTP), in what format, and how. Please send nothing beyond what is listed: the less patient
information leaves the office, the better for everyone. Files are sent only after the business associate
agreement and the pilot agreement have been signed.

| | |
|---|---|
| Clinic | |
| Remittance (denial) dates: from | |
| Remittance (denial) dates: to | |
| Secure transfer service | |
| Folder for files to us | |
| Folder for files back to the Clinic | |
| Weekly upload day (live phase) | |

## 1. Summary

| No. | What | Which ones | Format |
|---|---|---|---|
| 1 | Remittance (835) files from your clearinghouse | All 835 files dated within the remittance (denial) dates above | The X12 files exactly as delivered, pseudonymized before upload (Sections 2 and 8) |
| 2 | Claims report | Denied claims only (fully or partly denied) that appear in those 835 files, whatever their dates of service, plus any claim your system sent under a new number to replace one of them | CSV, one row per claim line (Section 3) |
| 3 | Visit note excerpts | Only for the denied claims: the parts that describe the denied visit and, for a medical-necessity denial, the initial examination or the latest re-examination | CSV (Section 4) |
| 4 | Payer letters and portal messages | Only for the denied claims | CSV (Section 5) |
| 5 | Payer deadlines | Each payer's filing, corrected-claim and appeal deadlines | The table in Section 6 (no patient information) |

During the live phase of the pilot, please send the same items once a week for new denials, on the weekly
upload day shown above.

## 2. Remittance (835) Files

Download the files from your clearinghouse for the remittance dates above and put them, unchanged, in
the folder you run `denial-agent anonymize` on (Section 8); send only the folder it writes. An 835
edited any other way cannot be read: anonymize replaces only the patient identifiers and, with
`--replace-claim-numbers`, the claim numbers. Send the X12 835 (ERA) data files, often named `.835`,
`.era`, `.edi` or `.txt`, not PDF or printed remittances or a clearinghouse spreadsheet report. The
files also list paid claims. That is expected: only the denials are worked.

Please tell us which payers send paper explanations of benefits only, or are not set up for electronic
remittances: their denials cannot be read from an 835 file.

## 3. Claims Report (Denied Claims Only)

### The claim number

The `claim_control_number` column must contain the claim number your system sent on the electronic claim:
the 837 "patient control number," printed in box 26 of the paper CMS-1500 form. Payers return that same
number in the 835, which is how each denial is matched to its claim. If your report shows a different
number, such as a visit or invoice number, please let us know before sending.

Please also tell us whether your system sends a different number for every claim, or the patient's account
number for all of that patient's claims. A different number for every claim is needed.

Also include every claim your system sent under a new claim number to replace a denied claim already
reported, whether the payer paid or denied it, with the replaced claim's number in
`original_claim_control_number`: that is how Plumbline ties the payer's answer to the item.

### Layout

CSV, UTF-8, with a header row and one row per service line. Rows of the same claim go together. Dates are
written as YYYY-MM-DD. Where a cell holds several values, separate them with a vertical bar, for example
diagnosis codes `M99.03|M54.50`, modifiers `AT|GP` or pointers `1|2`.

If you prepare the file in Excel, format every column as Text before pasting the data, so that leading
zeros, long claim and member numbers and YYYY-MM-DD dates are kept, and save it as "CSV UTF-8."

Every column below must appear in the header row. Required columns need a value on every row. The other
columns may be left blank, but the strongly recommended ones determine how much can be done for each
denial. Corrected claims are built from this report: fill in every modifier, unit count, diagnosis code and
diagnosis pointer the claim carried. A blank cell is read as none (or, for units, as 1), and the corrected
claim would leave it out. If your system cannot produce this layout, send us first the list of column names
its denied-claims report has, with no patient data, and we will tell you how to arrange them before any
data is sent.

| Column | What it is | Where to find it | Need |
|---|---|---|---|
| `claim_control_number` | Claim number sent on the claim (see above) | Box 26 / 837 CLM01 | Required |
| `patient_account_number` | Patient's account number in your system | Practice-management system | Required |
| `patient_first_name` | Patient's first name | Box 2 | Required |
| `patient_last_name` | Patient's last name | Box 2 | Required |
| `patient_dob` | Date of birth, YYYY-MM-DD | Box 3 | Strongly recommended |
| `patient_sex` | M, F or U | Box 3 | Helpful |
| `patient_address_line1` | Street address | Box 5 | Helpful |
| `patient_city` | City | Box 5 | Helpful |
| `patient_state` | State | Box 5 | Helpful |
| `patient_zip` | ZIP code | Box 5 | Helpful |
| `member_id` | Insurance member identifier | Box 1a | Strongly recommended |
| `payer_id` | The payer's electronic payer identifier at your clearinghouse | Clearinghouse payer list | Required |
| `payer_name` | Payer name | Top of the form | Required |
| `billing_provider_npi` | Billing NPI | Box 33a | Strongly recommended |
| `rendering_provider_npi` | Treating chiropractor's NPI | Box 24J | Strongly recommended |
| `rendering_provider_name` | Treating chiropractor's name | Practice-management system | Helpful |
| `referring_provider_npi` | Referring or ordering provider's NPI (for example, on X-ray lines) | Box 17b | Helpful |
| `place_of_service` | Place of service code (usually 11) | Box 24B | Helpful |
| `diagnosis_codes` | ICD-10 codes in claim order, separated by a vertical bar | Box 21 | Required |
| `prior_auth_number` | Authorization number, if any | Box 23 | Helpful |
| `original_submission_date` | Date the claim was first sent, YYYY-MM-DD | Claim history or clearinghouse | Strongly recommended |
| `claim_frequency_code` | 1 original, 7 replacement, 8 void (blank means 1) | Box 22 | Helpful |
| `original_claim_control_number` | For a replacement or void: your claim number for the original claim (not the payer's reference number) | Claim history | Helpful |
| `line_number` | Line number on the claim (1, 2, 3 and so on) | Box 24 row | Required |
| `service_date` | Date of service, YYYY-MM-DD | Box 24A | Required |
| `procedure_code` | CPT or HCPCS code | Box 24D | Required |
| `modifiers` | Modifiers, separated by a vertical bar | Box 24D | Required if the line carried any |
| `units` | Units (blank means 1) | Box 24G | Required if more than 1 |
| `charge_amount` | Line charge, for example 60.00 | Box 24F | Required |
| `diagnosis_pointers` | The diagnosis codes the line points to, as numbers 1 to 12 (A = 1, B = 2 and so on), separated by a vertical bar | Box 24E | Required |
| `ndc` | Drug code, if any (rare in chiropractic) | Box 24, shaded area | Helpful |
| `line_item_control_number` | Line identifier sent on the electronic claim, if your system shows it | 837 REF*6R | Helpful |
| `claim_total_charge` | Total charge for the claim | Box 28 | Helpful |

## 4. Visit Note Excerpts (`notes.csv`)

Only for the denied claims, and only the parts of the note for the denied visit that describe it: the
complaint and history; the examination findings (pain, asymmetry, range of motion, tissue changes); the
subluxation levels and the segments adjusted; the assessment; the plan (frequency, duration, goals); and
outcome measures (pain scale, range of motion, Oswestry or Neck Disability Index). For a medical-necessity
denial (including a denial for maintenance care or for a missing plan of care), also include the initial
examination or the most recent re-examination note. Please do not send complete charts.

| Column | What it is |
|---|---|
| `claim_control_number` | The denied claim's number (as in the claims report) |
| `note_date` | Date of the note, YYYY-MM-DD |
| `author` | Who wrote the note (name and credentials) |
| `text` | The excerpt, copied as text |

Enclose the text in double quotation marks; if the text itself contains a double quotation mark, type it
twice. Use one row per excerpt; several rows for the same claim are fine.

## 5. Payer Letters and Portal Messages (`payer_documents.csv`)

| Column | What it is |
|---|---|
| `claim_control_number` | The denied claim's number |
| `document_type` | `letter`, `portal` or `other` |
| `received_date` | Date received, YYYY-MM-DD |
| `text` | The words of the letter or message, copied as text |

Text is required, not images. If a letter exists only on paper or as a scan, copy the key paragraphs (the
denial reason and any appeal instructions) as text, or let us know and we will ask only for what is
needed.

## 6. Payer Deadlines (no patient information)

| Payer name | Payer identifier | Timely filing (days from date of service) | Corrected claim (days from the denial) | Appeal (days from the denial) | Source (contract or payer manual) |
|---|---|---|---|---|---|
| | | | | | |
| | | | | | |
| | | | | | |

For Medicare Part B, enter the reopening window (one year from the initial determination) as the
corrected-claim figure. A claim Medicare returned as unprocessable (remark code MA130) is not a denial that
can be reopened: it is resubmitted as a new claim within 12 months of the date of service, and the
deadline Plumbline shows for it must be checked against that limit.

## 7. Please Do Not Send

Complete charts; Social Security numbers; images of insurance cards or identification; driver's licenses;
bank or payment card details; passwords; claims that were not denied (other than within the 835 files,
and the claims your system sent to replace reported denials); or claims denied outside the remittance
dates above.

## 8. How to Send

1. Before each upload, put the 835 files, the claims report, the note excerpts and the payer letters in
   one folder on a Clinic computer and run `denial-agent anonymize` on it, as the Clinic Set-up Sheet
   BTP provides with the install file describes: with `--replace-claim-numbers`, a new `--key-out` in
   the Clinic's key folder and, from the second batch on, `--key-in` with the newest key. Upload only
   the folder it writes. The raw files and the key folder stay on that computer; keep dated backup
   copies of the key folder on Clinic media.
2. Upload to the secure transfer service, in the folder for files to us. Never send files by e-mail or on a
   USB drive.
3. Send the folder as anonymize wrote it: its file names carry no patient names. Do not edit or rename
   the files. Anything you send outside that folder is named by date and type only, never with a
   patient name.
4. Then send a short e-mail or text message with no patient details, for example: "Files uploaded for
   November 2."
5. Corrected claims, appeal letters and questions come back in the folder for files back to the Clinic, for
   your team's review. Each package's name starts with `PSEUDONYMIZED_` and it carries invented names:
   restore it on the Clinic's computer with `denial-agent deanonymize` and the newest key before anyone
   submits it, and never send a `PSEUDONYMIZED_` package to a payer. Nothing is sent to any payer by BTP
   or by Plumbline: your team decides and submits.

## 9. How Your Data Is Protected

Your data is kept on one encrypted computer, in an account used only for this pilot, and on its encrypted
backup drive; it moves only through the secure transfer service, and it is protected by the business
associate agreement. If a language model provider is engaged later, under its own business associate
agreement and after written notice to you, tokenized parts of a denial are also sent to it. When the pilot
ends, you receive a copy of your records in Plumbline (patients, claims, note excerpts, payer letters,
work items, proposals, review actions and outcomes); the copy carries the pseudonyms, which only your
key links to your patients and claims, so keep the newest key until every open item is resolved.
Remittance details and the audit log are destroyed, not returned. All other copies are deleted, and you
receive a signed certificate of return and destruction.

## 10. Contacts

| | Name | Telephone | E-mail |
|---|---|---|---|
| Pilot contact (Betz Technology Providers) | Connor Betz | | |
| Clinic contact | | | |
| Clinic biller | | | |
