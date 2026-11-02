# Security Policies and Procedures

| | |
|---|---|
| Organization | Betz Technology Providers ("BTP") |
| Security Official | Connor Betz |
| Security Official telephone and e-mail | |
| Effective date | |
| Version | 1.0 |
| Next scheduled review | |

These policies and procedures implement the HIPAA Security Rule (45 C.F.R. §§ 164.308 through 164.316)
for Betz Technology Providers ("BTP") in its work as a business associate of its clinic client (the
"Clinic"), using Plumbline, BTP's denial-recovery software (the "Software"). "PHI" means the Clinic's
protected health information in any form. Records kept under these policies contain no patient names or
other direct identifiers and identify items by the Software's work item number. Because the Clinic can link
a work item number to a patient, these records are treated as PHI: they are retained for six years after
the Clinic's other PHI is destroyed, under Section 5.4 of the business associate agreement, and protected
as that agreement requires.

## 1. Security Official (§ 164.308(a)(2))

The Security Official named above is responsible for these policies, the risk analysis, training,
incident response and the records these policies require, and serves as BTP's privacy contact. The
Security Official also handles the requests described in the business associate agreement: forwards to
the Clinic within five business days any request from an individual for access, amendment or an
accounting of disclosures; responds to the Clinic's own such requests within ten business days; and keeps
a log of every disclosure that must be included in an accounting (date, recipient, description of the
PHI, purpose), including disclosures required by law.

## 2. Scope and Workforce (§ 164.308(a)(3), (a)(4))

These policies apply to all PHI handled by BTP and to every person who can access it. The
Security Official is the only workforce member with access on the effective date. Any other person
receives access only after training, a signed policy acknowledgment, a documented need, and written notice
to the Clinic.

## 3. Access Control and Authentication (§§ 164.308(a)(3) through (a)(5), 164.312(a), (d))

**Policy.** Each person uses an individual account. Multi-factor authentication protects the Software and
every online service that holds PHI; the workstation account is protected by a password together with the
BitLocker start-up PIN. Access is limited to what the person needs and ends on the day it is no longer
needed.

**Procedure.**

1. Windows: PHI is accessed only from the dedicated local standard account CBWORK, protected by a password
   of at least fifteen characters stored in a password manager. No other person signs in to CBWORK or uses
   its files. Shared accounts are prohibited. A Clinic reviewer working in person uses the Software only in
   the CBWORK session that the Security Official opens and attends, through the reviewer's own Software
   account, without using files or other applications; each reviewer signs out of the Software before
   leaving the workstation, and the browser does not save Software passwords.
2. The Software: the setting `AUTH_REQUIRED=true` is confirmed in the CBWORK configuration at setup and at
   each weekly review, because the Software starts without a login when it is false; the session signing
   key is randomly generated (the Software refuses to start with a placeholder key). Each reviewer receives
   an individual account with the reviewer role; only the Security Official holds the administrator role.
   The Software records roles but gives every account the same functions, so reviewers are instructed to
   use only the queue and item pages; role-based limits are a remediation item in the risk analysis.
   Reviewers choose their own password of at least twelve characters and enroll their authenticator
   application in person; the enrollment code is displayed once and is never sent by e-mail or chat.
3. Built-in safeguards, confirmed during setup: argon2id password hashing; one-time codes, each accepted
   once; lockout for fifteen minutes after five failed attempts; every log-in attempt recorded with a
   reason code; session cookie marked HttpOnly and SameSite=Strict, valid for eight hours.
4. Automatic logoff: Windows locks after five minutes of inactivity; Software sessions end after eight
   hours or when the user signs out. The Software is stopped when not in use.
5. Removing access the same day: stop the Software; in the CBWORK installation, run a one-line database
   update that sets `users.is_active` to false for that user's id; restart the Software; and record the
   change in the access log below, because the Software's audit log does not record a direct database
   update. Every request from an inactive account is refused, so replacing the session signing key is
   optional. A lost authenticator is handled the same way, followed by a new account under a different
   e-mail address, since the Software does not accept an address that already has an account.
6. Other accounts used for the Clinic's work use unique passwords from the password manager. Online
   services that hold PHI (the secure transfer service, and the e-mail or Google account used to sign in to
   the model provider console) use multi-factor authentication; a service that cannot provide it is not
   used for PHI. The business e-mail account also uses multi-factor authentication.
7. Emergency access (§ 164.312(a)(2)(ii)): no person other than the Security Official accesses ePHI. If the
   Security Official is unavailable, the Clinic continues from its own source data and the weekly list of
   open items (Section 10, item 4), and the alternate contact named in the Incident Response Plan notifies
   the Clinic.

**Access log**

| Date | Person | System | Granted or removed | Reason | By |
|---|---|---|---|---|---|
| | | | | | |

## 4. Workstation and Device Security (§ 164.310)

**Policy.** PHI is kept on one encrypted workstation, in one dedicated account, in a private room.

**Procedure.**

1. The workstation is listed in the risk analysis asset inventory. It is never left unlocked and
   unattended; device tracking and remote wipe are enabled where the hardware supports them.
2. CBWORK is used only for the Clinic's work: no personal browsing, e-mail or downloads other than the
   secure transfer service and the Software; no browser sign-in or synchronization; no OneDrive, with the
   OneDrive policies "Prevent users from moving their Windows known folders to OneDrive" and "Prevent users
   from syncing personal OneDrive accounts" enabled; no Windows Backup to the cloud. In the CBWORK browser
   (Microsoft Edge), spell check is set to Basic (policy MicrosoftEditorProofingEnabled disabled), text
   prediction is off (policy TextPredictionEnabled disabled), and Copilot in Edge and its access to page
   content are off; Windows Copilot and Windows Recall are off.
3. The daily account never holds PHI. Development tools, including AI coding assistants, run only in the
   daily account, never with administrator rights, and are never given CBWORK files, paths or screenshots
   containing PHI. User Account Control is set to "Always notify." Development servers in the daily account
   use a port other than 8000. The daily account is signed out, not switched away from, while CBWORK works
   with PHI, and the Software is used only after its own console in CBWORK shows it listening.
4. Automatic Windows and browser updates are on. Microsoft Defender real-time protection is on, with
   Controlled folder access protecting the data and backup folders and the Software's Python interpreter
   (the installation's `.venv\Scripts\python.exe`) added as an allowed app; automatic sample submission is
   never set to send all samples. Windows diagnostic data is set to required only.
5. The firewall is on and no remote-access tools are installed. The Software listens only on the local
   machine address, which every account on the workstation can reach, so its login is the barrier between
   accounts. No remote access path for reviewers is enabled unless the risk analysis has been updated to
   cover it.
6. Phones, tablets and other computers never hold PHI. No removable storage is used other than the backup
   drive. PHI is not printed.
7. The data and transfer folders are excluded from Windows Search indexing.
8. Software updates reach CBWORK only as a tagged release of the source repository whose full test suite
   passed. The Security Official reviews the list of changes, installs the release with its lock file, and
   records the version and date.
9. The Software's installation (with its data and export folders and its database) and the transfer
   folder are inside `C:\Users\CBWORK`, never at a drive root or in a shared folder. The backup drive is
   formatted NTFS with access limited to CBWORK, SYSTEM and Administrators, and is unlocked only while the
   daily account is signed out. `icacls` confirms these permissions at setup and at each annual review.

## 5. Encryption (§ 164.312(a)(2)(iv), (e)(2)(ii))

**Policy.** PHI is encrypted wherever it is stored or transmitted.

**Procedure.** BitLocker protects the workstation disk with the TPM and a start-up PIN; the recovery key is
printed and stored offline, apart from the workstation. Backups are kept on a BitLocker To Go drive whose
recovery key is stored the same way. Before PHI arrives, and at each annual review, the Security Official
confirms at account.microsoft.com/devices/recoverykey that no Microsoft account holds a recovery key for
the workstation or the backup drive; if one does, the recovery password is replaced with a new one,
printed and stored offline, and the online copy is deleted. Transfers use only encrypted connections.
Encryption follows HHS guidance, under which properly encrypted information whose key has not been
compromised is not unsecured protected health information.

## 6. Artificial-Intelligence Tools

**Policy.** The only artificial-intelligence service that may receive PHI is the model provider's
HIPAA-ready API organization, under a signed business associate agreement, reached only through the
Software's redaction safeguard. No other AI tool receives PHI.

**Procedure.**

1. Until the model provider's business associate agreement is in effect and the conditions in Exhibit B of
   the Clinic's business associate agreement are met, the Software runs with its local stub model and sends
   nothing to any model provider. Calls to the stub are recorded with the model id "stub" and never leave
   the workstation.
2. Once engaged: a dedicated HIPAA-ready organization used for no other purpose; a dedicated workspace
   whose allowed and default inference locations are "us" and whose storage location is "us"; a service
   account API key created in that workspace, kept only in the CBWORK configuration file or the CBWORK
   user's environment, never in code and never logged, with no other `ANTHROPIC_API_KEY` defined for the
   machine; the variables `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS` and `ANTHROPIC_LOG` not set for
   the CBWORK user or the machine, because they would redirect requests or log their contents; a pinned
   model version; automatic model substitution disabled; only Messages API features the provider lists as
   HIPAA-eligible (the Software does not use the Files API, Batch API, code execution, web fetch or
   connectors to outside services); no PHI in output schemas (the Software's schemas are fixed).
3. PHI is never entered into consumer chat assistants, the Claude app or claude.ai on personal plans,
   Claude Code (which the provider states is not covered by its HIPAA readiness), any other coding
   assistant, the model provider's console or its playground, AI features built into the browser or the
   operating system, AI meeting note-takers or transcription services, or AI browser extensions. This
   applies to pasting, uploading, screenshots and dictation.
4. A person approves every action the Software proposes. Payer letters and clinical notes are treated as
   untrusted text.
5. Model tests run only on synthetic data.

## 7. Transmission Security (§ 164.312(e))

**Policy.** PHI moves only through the secure transfer service agreed with the Clinic.

**Procedure.** No e-mail attachments; no PHI in e-mail text, text messages or chat; file names contain no
patient names; by telephone, items are identified by work item number or claim number only. During the
weekly review, PHI is shown only in person or on a video service covered by a business associate
agreement, with recording off. PHI received by e-mail is deleted the same day from every folder, including
deleted items; the sender is asked to use the secure transfer service; and the event is recorded under the
Incident Response Plan.

## 8. Audit Controls and Activity Review (§§ 164.308(a)(1)(ii)(D), 164.312(b))

**Policy.** Every import, processing step, model request, item view, review action and log-in attempt in
BTP's installation of the Software is recorded; other access to PHI is recorded in the access and
incident logs; and the records are reviewed.

**Procedure.**

1. The Software records every import, processing step, model request, item view, review action and log-in
   attempt in an append-only, hash-chained audit log. Page views other than item views, direct database
   queries and file handling in CBWORK are not recorded there. The Software's anonymize and deanonymize
   commands, which the Clinic runs on its own computer, record no audit events; each completed run is
   recorded in a run log in the Clinic's key folder, and each import of a pseudonymized batch is audited
   like every import.
2. Weekly, as part of the review: run `denial-agent verify-audit` (it prints "OK"; any other result opens
   an incident); record the latest audit entry and its hash in the review record, using the audit query in
   Section 2 of the Weekly Shadow-Phase Review, because the Software shows no hash on its pages; confirm
   `AUTH_REQUIRED=true`; review the audit log for failed log-ins and lockouts, blocked or failed model
   requests, and processing failures.
3. Before and after the clinic purge: run `denial-agent verify-audit` and record the result in the
   Certificate of Return and Destruction.
4. Monthly: review failed Windows sign-ins for CBWORK in Event Viewer.

## 9. Integrity (§ 164.312(c))

**Policy.** PHI and the work based on it are protected from improper alteration.

**Procedure.** The audit log hash chain; coding changes cannot be approved without a quoted excerpt from the
clinical note; every export package carries the claim's audit records; imports are protected against
duplication; payer letters and notes are screened for planted instructions.

## 10. Contingency Plan and Backups (§§ 164.308(a)(7), 164.310(d)(2)(iv))

**Policy.** The Clinic's work can be restored, and the Clinic can continue without the Software.

**Procedure.**

1. Backup: on each day the Software is used, stop the Software and copy its data folder and configuration
   file to the BitLocker To Go drive; keep the seven most recent backups; store the drive in locked storage
   under BTP's sole control, away from the workstation. Each removal and return of the drive is logged with
   the date and the person (§ 164.310(d)(2)(iii)).
2. Restore test every three months: restore into a separate folder, point the Software's database setting
   at the restored copy, run `denial-agent verify-audit`, record the date, and delete the test copy.
3. Disaster recovery: on replacement hardware, apply these policies first, restore the latest backup,
   verify the audit log, and replace the session signing key and the API key. Target: restored within
   72 hours.
4. Emergency operations: the Software is not on the Clinic's critical path. The Clinic retains its source
   data and makes every submission, and receives each week, through the secure transfer service, a list of
   open items and their deadlines so that it can continue without the Software. The list carries, for each
   item, the work item number, the claim number as the Software shows it, the payer, the date of service,
   the total charge, the proposed action and the deadline only, without patient names. For a
   pseudonymized batch the claim number is the batch's pseudonym, which the Clinic's newest key maps
   back; the date of service and the charge are unchanged by pseudonymization.
5. Criticality: the Software's database is the only asset that cannot be rebuilt from the Clinic's files
   (work status, approvals and the audit log).

## 11. Minimum Necessary (§§ 164.502(b), 164.514(d))

**Policy.** BTP requests, uses and discloses only the PHI the work requires.

**Procedure.** Requests are limited to the Clinic Data Request (denied claims within the agreed remittance
dates and the claims sent to replace them, the claim fields the Software needs, and note excerpts only
for denied claims in scope, limited to the denied visit and, for a medical-necessity denial, the initial
examination or the latest re-examination). The model provider receives only the tokenized context for
one denial at a time. The Software's data interface omits patient names, dates of birth, member
identifiers, addresses and account numbers, but carries claim numbers and service dates. Messages and
records use work item numbers.

## 12. Vendor Management (§§ 164.308(b), 164.314(a))

**Policy.** No third party creates, receives, maintains or transmits PHI for BTP unless a business
associate agreement has been signed and the Clinic has consented in writing.

**Procedure.** The Vendor Register is kept current. Each vendor's agreement status is checked before first
use and annually.

## 13. Security Awareness and Training (§ 164.308(a)(5))

**Policy.** Every member of BTP's workforce is trained before receiving access to PHI and at least
annually.

**Procedure.** Topics and records are kept in the Training Record. Security reminders are given at least
quarterly and after any incident or significant change, and each is recorded in the Training Record.

## 14. Sanctions (§ 164.308(a)(1)(ii)(C))

A person who violates these policies is subject to sanctions proportionate to the violation, up to and
including removal of access and termination of the person's engagement. The Security Official records each
violation, its investigation and the sanction applied. Violations by the Security Official are recorded in
the same way and reported to the Clinic as the business associate agreement requires.

## 15. Security Incidents (§ 164.308(a)(6))

Security incidents are handled under the Incident Response Plan and reported to the Clinic within the time
required by the business associate agreement.

## 16. Disposal and Media Re-use (§ 164.310(d)(2)(i), (ii))

**Policy.** PHI is destroyed when the engagement ends or when storage media leave service, and each
destruction is documented.

**Procedure.** The Software's clinic purge (`denial-agent purge-clinic <clinic id> --confirm`), followed by
every step of the Certificate of Return and Destruction. Storage devices leaving service are sanitized in
accordance with NIST SP 800-88 Revision 2 and recorded in Part 7 of that certificate.

## 17. Documentation, Retention and Review (§ 164.316)

**Policy.** These policies, the risk analysis and every record they require are maintained in writing,
retained for six years from the date of creation or the date when last in effect, whichever is later,
made available to the persons responsible for implementing them, and reviewed at least annually and in
response to environmental or operational changes. This review is the evaluation required by
§ 164.308(a)(8).

**Procedure.** The current version of each document is kept with BTP's compliance records, and
superseded versions are retained. Reviews are also triggered by a second clinic, engagement of the model
provider, any remote access, a hosted deployment, a security incident, or a change in the HIPAA Rules.

| Review date | Reviewer | Changes | Next review |
|---|---|---|---|
| | | | |

## 18. Adoption

| | |
|---|---|
| Adopted on | |
| Name | Connor Betz |
| Title | Owner and Security Official |
| Signature | |

## References

- 45 C.F.R. Part 164, Subpart C (Security Rule): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C
- 45 C.F.R. §§ 164.502(b) and 164.514(d) (minimum necessary): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E
- HHS, Minimum Necessary Requirement: https://www.hhs.gov/hipaa/for-professionals/privacy/guidance/minimum-necessary-requirement/index.html
- HHS, Guidance to Render Unsecured Protected Health Information Unusable, Unreadable, or Indecipherable to Unauthorized Individuals: https://www.hhs.gov/hipaa/for-professionals/breach-notification/guidance/index.html
- NIST SP 800-66 Revision 2: https://csrc.nist.gov/pubs/sp/800/66/r2/final
- NIST SP 800-88 Revision 2, Guidelines for Media Sanitization: https://csrc.nist.gov/pubs/sp/800/88/r2/final
- NIST SP 800-63B-4, Digital Identity Guidelines: Authentication and Authenticator Management: https://csrc.nist.gov/pubs/sp/800/63/b/4/final
- Anthropic, API and data retention: https://platform.claude.com/docs/en/manage-claude/api-and-data-retention
- Anthropic, Data residency: https://platform.claude.com/docs/en/manage-claude/data-residency
- Microsoft, Microsoft Edge policies: https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies
