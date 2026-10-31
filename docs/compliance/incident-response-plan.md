# Incident Response Plan

| | |
|---|---|
| Organization | Betz Technology Providers ("BTP") |
| Incident lead (Security Official) | Connor Betz |
| Effective date | |
| Version | 1.0 |
| Next scheduled review | |

This plan sets out how Betz Technology Providers ("BTP") detects, contains, assesses and reports security
incidents and breaches affecting the protected health information ("PHI") of its clinic client (the
"Clinic"), as required by 45 C.F.R. § 164.308(a)(6), the Breach Notification Rule (45 C.F.R. §§ 164.400
through 164.414) and BTP's business associate agreement with the Clinic. The "Software" is Plumbline,
BTP's denial-recovery software.

## 1. Definitions

- **Security incident:** "the attempted or successful unauthorized access, use, disclosure, modification,
  or destruction of information or interference with system operations in an information system"
  (45 C.F.R. § 164.304).
- **Breach:** "the acquisition, access, use, or disclosure of protected health information in a manner not
  permitted under subpart E of this part which compromises the security or privacy of the protected health
  information" (45 C.F.R. § 164.402), subject to the exceptions and the risk assessment in Section 5.
- **Unsecured PHI:** PHI that has not been rendered unusable, unreadable or indecipherable to unauthorized
  persons by a technology or methodology specified by HHS (encryption with an uncompromised key, or
  destruction).
- **Discovery:** the first day on which a breach is known to BTP or, by exercising reasonable
  diligence, would have been known to it (45 C.F.R. § 164.410(a)(2)). The deadlines in Section 6 run from
  discovery.

## 2. Roles and Contacts

Keep a printed copy of this table away from the workstation.

| Role | Name | Telephone | E-mail | Notes |
|---|---|---|---|---|
| Incident lead and Security Official | Connor Betz | | | Directs the response under this plan |
| Clinic privacy officer | | | | Receives every report under the business associate agreement |
| Clinic owner | | | | Decides on notices with the Clinic privacy officer |
| BTP's healthcare attorney | | | | Breach assessment; state law |
| Cyber insurer breach hotline (policy number) | | | | Notified before any outside vendor is engaged |
| Forensic firm (through the insurer) | | | | Signs a business associate agreement before seeing PHI |
| Model provider security contact | | | | Once the model provider is engaged |
| Secure transfer service support | | | | Account lockout and access logs |
| Local police (non-emergency) | | | | Theft or burglary |
| FBI field office; Internet Crime Complaint Center (www.ic3.gov) | | | | Ransomware, extortion or fraud |
| Alternate contact if the incident lead is unavailable | | | | Notifies the Clinic; has no access to PHI |
| Successor custodian if the incident lead cannot act | | | | Holds the sealed recovery keys and written destruction instructions; signs a business associate agreement before any access |

## 3. Severity Levels

| Level | Examples | Response |
|---|---|---|
| 1. Event | A single failed log-in; a blocked model request in the audit log; a payer letter flagged for planted instructions; a port scan stopped by the firewall | Logged and reviewed at the weekly review; covered by the business associate agreement's standing notice of unsuccessful security incidents. A blocked model request is also corrected as a defect |
| 2. Security incident without known PHI exposure | Malware found in the daily account; an account locked by attempts that did not come from a reviewer; a failed audit-log verification; loss of the encrypted backup drive; PHI received by e-mail | Sections 4 and 5; reported to the Clinic within five (5) business days if the incident was successful |
| 3. Possible breach | Loss or theft of the workstation; PHI sent to the wrong person or place; PHI entered into an AI tool or e-mail; ransomware in the CBWORK account; an incident at the model provider or the secure transfer service affecting the Clinic's data | The full plan; the deadlines in Section 6 apply |

## 4. Response Procedure

The procedure follows the incident response functions in NIST SP 800-61 Revision 3: detect, respond
(analyze, contain, eradicate) and recover.

### 4.1 Detect and Record (first hour)

1. Open an incident record with a number, the date and time of discovery, who discovered it and what was
   observed. The record contains no PHI and identifies items by work item number.
2. Review the sources: the Software's audit log (failed log-ins, lockouts, blocked or failed model
   requests, processing failures, purge events); `denial-agent verify-audit`; the Software's health check
   (a "degraded" status means the database is unreachable or the audit log failed verification); Microsoft
   Defender protection history; the secure transfer service's access log; reports from the Clinic or a
   vendor.
3. Assign a severity level. For Level 3, telephone the Clinic privacy officer the same day, even before all
   facts are known.

### 4.2 Contain

Apply the steps that fit the incident and record each one in the evidence log (Section 8).

- Stop the Software. It is reachable only from the workstation, so stopping it ends all access to the
  review queue.
- Suspected malware: disconnect the workstation from the network; leave it powered on unless the forensic
  firm advises otherwise; do not wipe or reinstall it; do not connect the backup drive.
- Credentials: from another device, revoke the model API key in the provider's console; change the secure
  transfer service password and end its sessions; ask the Clinic to revoke shared links.
- Software sessions: replace the session signing key, which ends every session. To remove a single
  reviewer, follow Section 3, item 5 of the Security Policies and Procedures.
- Lost or stolen device: file a police report; record whether BitLocker was on and where the recovery key
  is kept; use device tracking or remote wipe if available.
- Misdirected file: ask the recipient in writing to delete it and confirm deletion; remove it from the
  transfer service.
- PHI received by e-mail: delete the message the same day from every folder, including deleted items; ask
  the sender to use the secure transfer service; and tell the Clinic privacy officer, because the
  disclosure began on the Clinic's side.

### 4.3 Analyze and Preserve Evidence

- With the Software stopped, copy the database file and relevant logs to an evidence folder inside CBWORK
  (on the encrypted disk) and record each file's SHA-256 value (`certutil -hashfile <file> SHA256`).
- Run `denial-agent verify-audit` and record the result; record the relevant audit entries by identifier
  and action.
- Determine what PHI was involved, for whom, during what period, and whether it was encrypted. The
  Software's model request records are linked to work items, so the affected items can be listed by
  identifier.

### 4.4 Eradicate and Recover

Remove the cause (malware, compromised credential or misconfiguration); restore from the latest clean
backup if needed (Security Policies and Procedures, Section 10); verify the audit log; replace keys;
restart the Software; and monitor closely for two weeks.

### 4.5 Close

Complete the breach risk assessment (Section 5), send the report (Section 6), hold a review within two weeks
of closing (Section 10), and update the risk analysis and policies.

## 5. Breach Risk Assessment (45 C.F.R. § 164.402)

An impermissible use or disclosure of PHI "is presumed to be a breach unless the covered entity or business
associate, as applicable, demonstrates that there is a low probability that the protected health
information has been compromised based on a risk assessment of at least the following factors." BTP
completes this assessment and provides it to the Clinic, which decides on notices. Each completed
assessment is retained for six years.

| Step | Question | Answer and evidence |
|---|---|---|
| A | Incident number; date of discovery; date of the event | |
| B | Was PHI involved? If not, record the basis and close the assessment | |
| C | Was the PHI unsecured? Encryption under HHS guidance with an uncompromised key means the PHI was secured; record the encryption method and where the key was kept | |
| D | Does an exception apply? (i) unintentional, good-faith access or use by a workforce member acting within the scope of authority, without further impermissible use or disclosure; (ii) inadvertent disclosure between persons authorized at the same covered entity or business associate, without further impermissible use or disclosure; (iii) a good-faith belief that the unauthorized recipient could not reasonably have retained the information | |
| Factor 1 | The nature and extent of the PHI involved, including the types of identifiers and the likelihood of re-identification (names, dates of birth, member identifiers, addresses, diagnosis and procedure codes, note text; number of individuals; tokenized or not) | |
| Factor 2 | The unauthorized person who used the PHI or to whom the disclosure was made (another covered entity or business associate bound by HIPAA; a service without a business associate agreement; unknown) | |
| Factor 3 | Whether the PHI was actually acquired or viewed (forensic findings, access logs, the recipient's statement) | |
| Factor 4 | The extent to which the risk has been mitigated (written assurance of deletion, credentials revoked, device recovered, data encrypted) | |
| Conclusion | ☐ Low probability of compromise; no notification required; documentation retained ☐ Breach of unsecured PHI; report under Section 6 | |
| Prepared by; date | | |

## 6. Reporting to the Clinic (45 C.F.R. § 164.410)

**Timing.**

- Without unreasonable delay and no later than five (5) business days after discovery, as the business
  associate agreement requires, and in every case "in no case later than 60 calendar days after discovery
  of a breach" (45 C.F.R. § 164.410(b)).
- If a law enforcement official states that a report would impede a criminal investigation or damage
  national security, delay it for the period stated in writing, or for no more than thirty days after an
  oral statement unless a written statement follows (45 C.F.R. § 164.412). Record the official's name and
  statement.
- Successful security incidents that are not breaches are also reported within five (5) business days;
  unsuccessful security incidents are covered by the standing notice in the business associate agreement.

**Content** (45 C.F.R. § 164.410(c)), to the extent possible, supplemented as facts become available:

1. the identification of each individual whose unsecured PHI has been, or is reasonably believed to have
   been, accessed, acquired, used or disclosed, delivered through the secure transfer service and never by
   e-mail; and
2. any other available information the Clinic must include in its notice to individuals under
   45 C.F.R. § 164.404(c): what happened, including the date of the breach and the date of discovery; the
   types of PHI involved; steps individuals should take to protect themselves; what is being done to
   investigate, mitigate harm and prevent recurrence; and contact procedures.

**Notices to individuals, HHS and the media.** The Clinic decides whether and how to notify individuals
(within sixty days of discovery), the Secretary of HHS (within sixty days for breaches affecting 500 or
more individuals; otherwise within sixty days after the end of the calendar year) and prominent media
outlets (for breaches affecting more than 500 residents of a state or jurisdiction). BTP gives
those notices only if the Clinic directs it to in writing. Notification decisions rest with the Clinic and
its counsel and are made solely on the facts and the law.

**Report form** (by telephone first, then in writing through the secure transfer service; any e-mail
cover note contains no PHI):

```
INCIDENT REPORT

Incident number:                     Report type: Initial / Update / Final
Date of this report:
From (business associate):
To (covered entity):
Date or period of the event:         Date of discovery:
Description of what happened:
Systems and data involved:
Types of PHI involved:
Number of individuals:               List of individuals (file name on the transfer service):
Was the PHI encrypted? (method):
Breach risk assessment:              Attached / To follow by (date):
Containment and mitigation to date:
Law enforcement involvement (agency, report number, any requested delay):
Steps individuals may take:
Contact (name, telephone, e-mail):
```

## 7. Other Notifications

- **Cyber insurer:** as soon as a Level 3 incident is suspected and before any outside vendor is engaged,
  as the policy requires.
- **Law enforcement:** theft, ransomware, extortion or fraud (police report; FBI through the Internet Crime
  Complaint Center). Reports to law enforcement or threat-sharing organizations contain no PHI unless
  HIPAA permits the disclosure.
- **Model provider or secure transfer service:** when their systems are involved.
- **State authorities:** as the Clinic and counsel determine under Section 9.

## 8. Evidence Log

This log contains no PHI. Evidence files that contain PHI remain in the encrypted CBWORK account.

| No. | Date and time (UTC) | Item | Source | Collected by | SHA-256 | Location | Custody notes |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

## 9. State Breach Notification Laws

Every state has a breach notification law. Several also require a vendor that maintains data for another
business to notify that business, some within short deadlines, and some treat compliance with HIPAA as
compliance with the state law. List the Clinic's state and each state in which affected individuals
reside.

| State | Statute | Covers medical or health insurance information | Vendor notice duty and deadline | Notice to the attorney general or regulator (threshold, deadline) | HIPAA compliance accepted as compliance |
|---|---|---|---|---|---|
| | | | | | |
| | | | | | |

## 10. Post-Incident Review

Within two weeks of closing an incident, record what happened, what worked, what did not, and the changes
made to the risk analysis, the policies, the Software or training.

| Incident | Review date | Changes decided | Completed |
|---|---|---|---|
| | | | |

## 11. Tabletop Exercise

Conducted before PHI is first received and then annually. Duration: 60 to 90 minutes. Participants: the
Security Official, the Clinic privacy officer, and, where possible, BTP's attorney or the
insurer's breach counsel. Select two scenarios. The facilitator reads each development and asks the
questions; a participant records decisions and gaps. No real PHI is used.

**Scenario A: workstation stolen.**

1. At 7:00 a.m. a burglary is discovered; the workstation is gone, and the backup drive was kept in the same
   room. Who is called first? Was BitLocker on, with a start-up PIN? Where is the recovery key? Is this a
   breach of unsecured PHI (Section 5, steps B and C)?
2. The police report number arrives, and the Clinic privacy officer asks whether patients must be notified.
   What goes into the report to the Clinic, and when? Who decides on patient notices?
3. Replacement hardware arrives. What are the restore steps, which keys are replaced, and how long until
   work resumes? Which deadlines are at risk, and how does the Clinic learn of them?

**Scenario B: ransomware.**

1. A ransom note appears in the daily account, and Defender reports blocked encryption attempts. How is the
   workstation isolated? Is CBWORK data affected? Was the backup drive connected?
2. Files in the CBWORK profile now have unfamiliar extensions. HHS presumes that ransomware encryption of
   ePHI is a breach unless a low probability of compromise is demonstrated; what evidence would demonstrate
   it? Who calls the insurer, the FBI and the Clinic?
3. The attacker threatens to publish data. Who decides whether to pay, and on what advice? What does the
   Clinic need to know, and by when?

**Scenario C: PHI entered into a consumer AI tool.**

1. Late at night, the text of a payer letter (patient name, member identifier, date of service) is pasted
   into a consumer chat assistant in the daily account to summarize it, and the mistake is noticed ten
   minutes later. Which policy was violated? What can be done now (delete the conversation, review the
   service's retention and training settings)?
2. The service retains conversations and has no business associate agreement. Apply the four factors. Can
   a low probability of compromise be demonstrated? When is the report to the Clinic due?
3. Which control would have prevented it?

**Scenario D: file sent to the wrong place.**

1. An export package is uploaded to a folder visible to a person outside the Clinic. Does any exception
   apply? What written assurance is needed from the recipient?

**Scenario E: incident at the model provider.**

1. The model provider reports that API data for a stated period may have been exposed. How many model
   requests fell within that period, and for which work items? What did those requests contain? When is the
   date of discovery? What goes to the Clinic?

**Scenario F: the incident lead is unavailable.**

1. The Security Official is in hospital for two weeks while appeal deadlines run, and a report to the
   Clinic falls due. Who notifies the Clinic? Who can reach the sealed recovery keys and the backup drive,
   and under what agreement? How does the Clinic continue from its source data and the weekly list of open
   items? If the engagement ends meanwhile, who completes the return and destruction and signs the
   certificate?

**Exercise record**

| Date | Participants | Scenarios | Gaps identified | Actions, owner, date |
|---|---|---|---|---|
| | | | | |

## 12. Adoption and Maintenance

This plan is reviewed annually, after every Level 2 or Level 3 incident, and after every tabletop exercise.

| | |
|---|---|
| Adopted on | |
| Name | Connor Betz |
| Title | Owner and Security Official |
| Signature | |

## References

- 45 C.F.R. §§ 164.400 through 164.414 (Breach Notification Rule): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-D
- 45 C.F.R. § 164.304 and § 164.308(a)(6) (security incidents): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C
- HHS, Breach Notification Rule: https://www.hhs.gov/hipaa/for-professionals/breach-notification/index.html
- HHS, Guidance to Render Unsecured Protected Health Information Unusable, Unreadable, or Indecipherable to Unauthorized Individuals: https://www.hhs.gov/hipaa/for-professionals/breach-notification/guidance/index.html
- HHS Office for Civil Rights, Cyber-Attack Quick Response Checklist: https://www.hhs.gov/sites/default/files/cyber-attack-checklist-06-2017.pdf
- HHS, Fact Sheet: Ransomware and HIPAA: https://www.hhs.gov/sites/default/files/RansomwareFactSheet.pdf
- NIST SP 800-61 Revision 3, Incident Response Recommendations and Considerations for Cybersecurity Risk Management: https://csrc.nist.gov/pubs/sp/800/61/r3/final
- CISA, Tabletop Exercise Packages: https://www.cisa.gov/resources-tools/services/cisa-tabletop-exercise-packages
- National Conference of State Legislatures, Security Breach Notification Laws: https://www.ncsl.org/technology-and-communication/security-breach-notification-laws
