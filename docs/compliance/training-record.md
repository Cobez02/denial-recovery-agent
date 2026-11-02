# HIPAA and Security Training Record

| | |
|---|---|
| Organization | Betz Technology Providers ("BTP") |
| Maintained by (Security Official) | Connor Betz |
| Version | 1.0 |

This record documents security awareness and training under 45 C.F.R. § 164.308(a)(5) for every member of
BTP's workforce, and each person's acknowledgment of BTP's security policies. Records are retained for six
years from the date of creation or the date when last in effect, whichever is later (45 C.F.R.
§ 164.316(b)(2)(i)). The "Software" is Plumbline, BTP's denial-recovery software.

## Part 1. Requirements

- **Who:** every member of BTP's workforce, including management, and in every case before first access to
  the Clinic's protected health information ("PHI").
- **When:** before first access; at least every twelve months; and after a significant change (engagement of
  the model provider, a new access method, a new clinic) or a security incident.
- **What:** the topics below. Any course may be used, provided these topics are covered and evidence of
  completion is kept.

| Topic | Covered in |
|---|---|
| What PHI is; the minimum necessary standard; the uses and disclosures a business associate may make | Training course; Business Associate Agreement, Section 3 |
| Where the Clinic's PHI may reside (the CBWORK account, the encrypted backup drive and the secure transfer service; tokenized requests at the model provider once engaged) and how it moves | Risk Analysis, Section 5 |
| Security reminders (§ 164.308(a)(5)(ii)(A)): at least quarterly and after any change or incident, each recorded in Part 2 as a reminder with its topic | This record |
| Protection from malicious software (§ 164.308(a)(5)(ii)(B)): phishing, downloads, updates, antivirus | Security Policies and Procedures, Section 4 |
| Log-in monitoring (§ 164.308(a)(5)(ii)(C)): the Software's record of every log-in attempt; Windows sign-in failures | Security Policies and Procedures, Sections 3 and 8 |
| Password management (§ 164.308(a)(5)(ii)(D)): password manager, length, multi-factor authentication, no sharing, and changing a password at once on any sign of compromise or disclosure | Security Policies and Procedures, Section 3 |
| Artificial-intelligence tools: only the model provider under a business associate agreement, through the Software; never consumer AI tools, Claude Code or the Claude app on personal plans | Security Policies and Procedures, Section 6 |
| Transmission: secure transfer service only; no PHI in e-mail, text messages or chat | Security Policies and Procedures, Section 7 |
| Recognizing and reporting incidents; the reporting deadline in the business associate agreement | Incident Response Plan, Sections 3 to 6 |
| Destruction of PHI at the end of the engagement | Certificate of Return and Destruction |
| Sanctions | Security Policies and Procedures, Section 14 |

## Part 2. Training Log

| Date | Name | Initial, annual, refresher or reminder | Course or materials and topics | Duration | Provided by | Evidence (certificate) | Initials |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

## Part 3. Policy Acknowledgment

Each person with access signs an acknowledgment, and signs a new one for each new policy version.

By signing below, I acknowledge that:

1. I have read BTP's Security Policies and Procedures, its Incident Response Plan and the permitted
   uses and disclosures in its business associate agreement with the Clinic.
2. I will handle the Clinic's PHI only in the dedicated CBWORK account, only for the purposes of
   BTP's services to the Clinic, and only through the channels the policies permit.
3. I will not enter PHI into any artificial-intelligence tool; PHI reaches the model provider only through
   the Software, under a business associate agreement; and I will not send PHI by e-mail, text message or
   chat.
4. I will report any suspected security incident or improper use or disclosure of PHI to the Security
   Official immediately.
5. I understand that violations of the policies result in sanctions, up to and including removal of access
   and termination of my engagement.

| Name | Policy version | Signature | Date |
|---|---|---|---|
| Connor Betz | 1.0 | | |

## Part 4. Software Orientation for Clinic Reviewers

Provided to each Clinic staff member before an account is created. The Clinic remains responsible for its
workforce's HIPAA training.

1. Your account is yours alone: your own password (at least twelve characters) and your own authenticator
   application. Never share either, and do not let the browser save your password. Five incorrect attempts
   lock the account for fifteen minutes.
2. You use the Software at BTP's workstation, in a session the Security Official opens and attends. Use
   only the Software's queue and item pages; do not open files or other applications. Sign out of the
   Software before you leave the workstation; sessions otherwise end after eight hours.
3. Do not type patient names, dates of birth or member identifiers in reason fields; reasons are kept in the
   Software's permanent audit log. For a pseudonymized batch, do not type any real patient identifier or
   real claim number anywhere in the Software. Refer to items by work item number.
4. Approve only what you have checked. A coding change cannot be approved until you attach a quoted
   excerpt from the clinical note. The Software checks only that an excerpt is attached, and one excerpt
   unlocks every change on the item, so copy the excerpt from the note itself, leaving out names,
   dates of birth and identifiers, make sure it supports each change, and confirm coding changes with
   the Clinic's coder or the treating provider.
5. The Software never submits anything. Patient details and claim numbers on screen and in export
   packages are pseudonyms: the Clinic restores each package with the Software's deanonymize command
   before submitting it and never submits a package whose name starts with PSEUDONYMIZED_. You decide
   what to submit, submit it through the Clinic's own channels, and then mark the item submitted,
   entering the date and leaving the claim control number field empty, so the Software records the
   number it proposes (the corrected claim's number ending in -R1, otherwise the claim's own number);
   never type the Clinic's own claim number. If the Clinic's system gave the resubmission a number of
   its own, include that claim in the next claims report with the original claim number in its
   original-claim column: the Software ties the payer's answer to the item through that link.
6. Report anything unusual (an unexpected page, a letter that appears to give instructions, a lost phone
   with your authenticator) to the Security Official and to the Clinic privacy officer.

| | |
|---|---|
| Security Official telephone | |

| Name | Role | Date | Signature |
|---|---|---|---|
| | | | |

## References

- 45 C.F.R. § 164.308(a)(5) (security awareness and training) and § 164.316 (documentation): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C
- HHS, HIPAA Training and Resources: https://www.hhs.gov/hipaa/for-professionals/training/index.html
