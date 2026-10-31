# Vendor Register

| | |
|---|---|
| Organization | Betz Technology Providers ("BTP") |
| Maintained by (Security Official) | Connor Betz |
| Last reviewed | |
| Version | 1.0 |

No third party creates, receives, maintains or transmits the Clinic's protected health information ("PHI")
on BTP's behalf unless a business associate agreement has been signed first and the Clinic has
consented in writing (45 C.F.R. §§ 164.308(b), 164.314(a); Security Policies and Procedures, Section 12).
A subcontractor also keeps PHI in the United States (Business Associate Agreement, Section 2.11). A service
that stores PHI is a business associate even if it cannot read the data. A service that only transmits
data, with no more than transient storage, such as an internet service provider, is a conduit and does not
require a business associate agreement. The encrypted backup drive is stored only in locked storage under
BTP's sole control; any third-party custodian would be listed in this register with its agreement status.
The "Software" is Plumbline, BTP's denial-recovery software.

## Register

| No. | Category | Vendor | Service | Access to PHI | Role under HIPAA | Agreement status | Date signed | Next review | Conditions |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Language model | Anthropic, PBC (Claude API) | Model consulted by the Software through its redaction safeguard | Yes, once engaged: tokenized requests and responses | Subcontractor business associate | Not engaged; no PHI is sent until its business associate agreement is signed and the Clinic is notified | | | Dedicated HIPAA-ready organization used for no other purpose; agreement covers the features the Software uses; dedicated workspace whose allowed and default inference locations and storage location are "us"; service account API key created in that workspace; no environment variable that redirects requests or logs their contents; automatic model substitution disabled; written answer that request data is not accessed outside the United States. The provider may retain request data for its stated retention period, and longer where required by law or where its trust-and-safety systems flag a request |
| 2 | Secure file transfer | | Encrypted file share or SFTP for files to and from the Clinic | Yes | If provided by the Clinic: the Clinic's own vendor under the Clinic's agreement. If provided by BTP: BTP's subcontractor | ☐ Provided by the Clinic (Clinic confirms its agreement covers the service) ☐ Provided by BTP: business associate agreement with the vendor, and PHI kept in the United States, before first use; the Clinic's selection on the Pilot Agreement Cover Page is its written consent (Business Associate Agreement, Sections 2.4 and 2.11) | | | Encryption in transit and at rest; multi-factor authentication on BTP's account; access logs; BTP access removed at the end of the engagement; if provided by BTP, the Clinic's files, earlier versions and trash deleted at the end of the engagement and the provider's deletion or retention terms recorded (Certificate of Return and Destruction, Part 6) |
| 3 | Video conferencing (weekly review, if held by video) | | Weekly review | Possibly | The host of the meeting must have a business associate agreement with the service if PHI is shown; if BTP hosts it, BTP's subcontractor | ☐ Not used (review held in person) ☐ Provided by the Clinic and covered by its business associate agreement ☐ Provided by BTP: business associate agreement with the vendor, and PHI kept in the United States, before first use; the Clinic's selection on the Pilot Agreement Cover Page is its written consent | | | Recording off; no AI note-taker or transcription |
| 4 | Operating system and security software | Microsoft (Windows 11 Pro, BitLocker, Microsoft Defender, Edge) | Workstation operating system, disk encryption, antivirus and browser | Not intended | Software vendor; no business associate agreement (no PHI is sent by design) | Not applicable | | | Diagnostic data set to required only; Defender sample submission never set to send all samples; no Microsoft account, OneDrive or browser sync in CBWORK, with the OneDrive policies "Prevent users from moving their Windows known folders to OneDrive" and "Prevent users from syncing personal OneDrive accounts" enabled; in CBWORK, Edge spell check set to Basic (policy MicrosoftEditorProofingEnabled disabled), text prediction off (policy TextPredictionEnabled disabled), Copilot in Edge and its page-content access off, and Windows Copilot and Recall off; BitLocker recovery keys stored offline, and before PHI arrives account.microsoft.com/devices/recoverykey confirms that no Microsoft account holds a recovery key for this workstation or the backup drive (any escrowed key is replaced with a new one, printed and stored offline, and the online copy deleted) |
| 5 | Cyber liability insurance | | Cyber liability policy | No | Not a business associate | Not applicable | | | Policy number and breach hotline recorded in the Incident Response Plan |
| 6 | Technology errors-and-omissions insurance | | Professional liability policy | No | Not a business associate | Not applicable | | | Policy number |
| 7 | Breach response (insurer's panel: breach counsel, forensics, notification and call center) | | Engaged only during an incident | Yes, if engaged | Subcontractor business associates for the engagement | Business associate agreement signed at engagement, before any PHI is shared | | | Engaged through the insurer, with the Clinic's written consent; PHI kept in the United States |
| 8 | Legal counsel | | Contract review; incident advice | Not normally | Business associate only if given PHI | Business associate agreement signed, and the Clinic's written consent obtained, before any PHI is shared | | | PHI kept in the United States |
| 9 | E-mail | | Business e-mail | No | Not a business associate (no PHI sent) | Not applicable | | | No PHI in messages or attachments; multi-factor authentication on; PHI received by e-mail is deleted the same day from every folder, including deleted items, the sender is asked to use the secure transfer service, and the event is recorded under the Incident Response Plan |
| 10 | Password manager | | Credentials for the accounts above | No | Not applicable | Not applicable | | | Holds no PHI |
| 11 | Authenticator application | | One-time codes for the Software and other accounts | No | Not applicable | Not applicable | | | Phone screen lock |
| 12 | Internet service | | Home internet | Encrypted traffic only | Conduit | Not required | | | Router administrator password changed; firmware current; WPA2 or WPA3 |
| 13 | Development tools | Anthropic (Claude Code; Claude app on personal plans) | Software development in the daily account | No | Not a business associate | None; the provider states Claude Code is not covered by its HIPAA readiness | | | Synthetic data only; never used in CBWORK; never run with administrator rights |
| 14 | Source code hosting, if used | | Source code and automated tests | No | Not applicable | Not applicable | | | The repository contains no PHI; secret scanning before every commit once the pre-commit hook is installed (risk analysis, M23) |
| 15 | Clinic's practice-management system and clearinghouse | | Source of the Clinic's data; the Clinic's submission channel | Yes (the Clinic's own vendors) | Business associates of the Clinic, not of BTP | The Clinic's responsibility | | | Listed for the data flow only; the Software never connects to them |

## Annual Review

| Date | Reviewer | Changes | Next review |
|---|---|---|---|
| | | | |

## References

- 45 C.F.R. §§ 164.308(b) and 164.314(a) (business associate contracts, including with subcontractors): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C
- 45 C.F.R. § 164.502(e) (disclosures to business associates): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E
- HHS, Business Associates: https://www.hhs.gov/hipaa/for-professionals/privacy/guidance/business-associates/index.html
- HHS, Guidance on HIPAA and Cloud Computing: https://www.hhs.gov/hipaa/for-professionals/special-topics/health-information-technology/cloud-computing/index.html
- Anthropic, API and data retention: https://platform.claude.com/docs/en/manage-claude/api-and-data-retention
- Anthropic, Data residency: https://platform.claude.com/docs/en/manage-claude/data-residency
- Microsoft, Microsoft Edge policies: https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies
