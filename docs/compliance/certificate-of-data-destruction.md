# Certificate of Return and Destruction of Protected Health Information

This certificate records the return and destruction of protected health information at the end of the
business associate relationship described below or, after a verified migration, the destruction of the
copy left in the environment identified in Part 1. It contains counts, dates and the hand-over file name
only, and no patient information. Attach the purge report printed by Plumbline with the file names under
`files_deleted` removed, because export package names contain claim numbers; record only how many there
were.

## Part 1. Parties and Reason

| | |
|---|---|
| Covered Entity | |
| Business Associate | Betz Technology Providers |
| Date of the Business Associate Agreement | |
| Clinic identifier in Plumbline | |
| Period of the data covered (from / to) | |
| Reason (check one) | ☐ End of the pilot ☐ Covered Entity's request ☐ Migration to a new environment, verified on the date entered below ☐ Termination for cause |
| Migration verified on (if applicable) | |
| Environment covered (for a migration, the environment left behind) | |

## Part 2. Record Counts Before the Purge

Command run without confirmation (it reports what will be removed and changes nothing):
`denial-agent purge-clinic <clinic id>`

| Record type | Report field | Count |
|---|---|---|
| Patients | `counts.patients` | |
| Claims | `counts.claims` | |
| Claim lines | `counts.claim_lines` | |
| Clinical note excerpts | `counts.note_excerpts` | |
| Payer documents | `counts.payer_documents` | |
| Work items | `counts.work_items` | |
| Proposals | `counts.proposals` | |
| Review actions | `counts.actions` | |
| Outcomes | `counts.outcomes` | |
| Unmatched remittance claims | `counts.remittance_claims_unmatched` | |
| Uploaded files on disk | `files_on_disk.uploads` | |
| Export packages on disk | `files_on_disk.exports` | |

| | |
|---|---|
| Run on (date and time) | |
| Run by | |
| Output saved as (file name) | |

## Part 3. Purge

Command: `denial-agent purge-clinic <clinic id> --confirm --reason "<reason>"`

The purge (1) deletes every uploaded file and every export package stored by Plumbline, stopping before
any record is changed if a file cannot be deleted; (2) writes the records counted in Part 2 into a single
hand-over file with a manifest; (3) deletes those records, except unmatched remittance claims, which stay
in the database until it is destroyed under Part 6, after step (4) clears their names and member
identifiers; (4) removes the patient names and member identifiers that payers returned on every remittance
claim; (5) records the deletion in Plumbline's
audit log as a `clinic_purged` event; and (6) on a SQLite database, rebuilds the database file so that
deleted records do not remain readable in it.

| Record type | Report field | Count or value |
|---|---|---|
| Purge completed | `deleted` | |
| Patients deleted | `counts.patients` | |
| Claims deleted | `counts.claims` | |
| Claim lines deleted | `counts.claim_lines` | |
| Clinical note excerpts deleted | `counts.note_excerpts` | |
| Payer documents deleted | `counts.payer_documents` | |
| Work items deleted | `counts.work_items` | |
| Proposals deleted | `counts.proposals` | |
| Review actions deleted | `counts.actions` | |
| Outcomes deleted | `counts.outcomes` | |
| Model call records unlinked | `counts.model_calls_unlinked` | |
| Remittance lines unlinked | `counts.remittance_lines_unlinked` | |
| Remittance claims unlinked | `counts.remittance_claims_unlinked` | |
| Unmatched remittance claims cleared | `counts.remittance_claims_unmatched_cleared` | |
| Uploaded files deleted (number of names listed) | `files_deleted.uploads` | |
| Export packages deleted (number of names listed) | `files_deleted.exports` | |
| Database file rebuilt | `vacuumed` | |
| Rebuild error, if any | `vacuum_error` | |
| Hand-over file | `export_file` | |
| Exit code | | |

The names of the deleted files are removed from the attached report, because export package names contain
claim numbers; only their number is recorded above. The names remain in the `clinic_purged` audit event,
which is destroyed with the database file under Part 6. If the database is PostgreSQL rather than SQLite,
`vacuumed` reads false by design; record here when `VACUUM FULL` was run and by whom:

| | |
|---|---|
| `VACUUM FULL` run on, by (PostgreSQL only) | |

## Part 4. Audit Trail

| | |
|---|---|
| Result of `denial-agent verify-audit` immediately before the purge | ☐ OK ☐ Failed |
| `clinic_purged` audit event: identifier, timestamp, actor | |
| Hash of that audit event | |
| Result of `denial-agent verify-audit` immediately after the purge | ☐ OK ☐ Failed |

Plumbline shows no hash on its pages. Read it with the audit query in Section 2 of the Weekly Shadow-Phase
Review, adding `.where(AuditLog.action == "clinic_purged")` before `.order_by`.

## Part 5. Hand-over Copy

| | |
|---|---|
| Delivered to (name and role) | |
| Delivery method | |
| Delivered on | |
| Receipt confirmed on, by | |
| Covered Entity declined the copy on (if applicable) | |
| Written reminder sent on (no reply within ten Business Days after delivery) | |
| Treated as declined on (no reply within ten Business Days after the reminder) | |
| Hand-over file permanently deleted on | |
| Removed from the transfer service on | |

The hand-over copy contains one data file per record type counted in Part 2 (the open work items and
their deadlines are in the work items file) and a manifest of counts. The list of open items and their
deadlines required by Section 14.4 of the Pilot Agreement is delivered with it.

## Part 6. Other Copies Destroyed

The purge removes Covered Entity's records from Plumbline. The items below are destroyed separately.

| Item | Description | Method of destruction | Date | Initials |
|---|---|---|---|---|
| Remaining database file | Contains the remittance payment ledger (claim numbers, dates of service, amounts and codes, without patient names or member identifiers), the audit log, import records, model call records without content, and reviewer accounts | Plumbline stopped; database file permanently deleted | | |
| Files received from Covered Entity | Remittance files and reports read from the transfer folder | Permanently deleted | | |
| Browser downloads | Export packages saved by the browser of the dedicated account | Permanently deleted | | |
| Browser cache and history | Review pages cached by the browser of the dedicated account | Browsing data cleared for all time | | |
| Recycle Bin | Files deleted through Windows Explorer | Emptied | | |
| Windows Search index | Text extracted from data files, if the folders were indexed | Index rebuilt | | |
| Volume shadow copies and restore points | Earlier versions of deleted files kept by Windows System Protection | Deleted, or confirmed that none exist | | |
| Incident evidence copies and working files | Evidence copies made under Section 4.3 of the Incident Response Plan; working files such as the shadow-phase label files | Permanently deleted, or retained as stated below with the reason | | |
| Backups | Encrypted copies on the external backup drive | Backup sets deleted, and the drive purged by cryptographic erase (BitLocker keys and every copy of the drive's recovery key destroyed) or by its sanitize command, or destroyed; recorded in Part 7 | | |
| Backup drive recovery key | Printed BitLocker recovery key for the backup drive | Cross-cut shredded | | |
| Paper | Any printed pages or notes | Cross-cut shredded | | |
| Model Provider | Data retained by the Model Provider under its business associate agreement, if the model was used | Retention terms and any deletion request recorded below; or ☐ no requests were sent to the Model Provider (calls recorded with model id "stub" are local) | | |
| Secure transfer service, if provided by Business Associate | Covered Entity's files in both folders, their earlier versions and the service's trash | All files deleted; the provider's deletion or retention terms recorded below; account closed | | |
| Access to Covered Entity's systems | Business Associate's access to the transfer service; reviewer accounts | Access removed; reviewer accounts deleted with the database file | | |
| Dedicated Windows account | Application copy, configuration file and keys | Model API key revoked; account and profile deleted, if no other work continues in it | | |

Model Provider and secure transfer service retention terms and deletion requests (if applicable):

| | |
|---|---|
| Details | |

Records kept under Section 5.4 of the Business Associate Agreement: compliance and billing records that
identify items by work item number (fee statements and Covered Entity's reports under Sections 8.5 and 8.6
of the Pilot Agreement, weekly review records, and access and incident records). They are kept for six
years, used only for Business Associate's legal responsibilities and protected as that Agreement requires.

Other protected health information whose return or destruction is not feasible, the reason, and the
protections that continue to apply:

| | |
|---|---|
| Details (or "None") | |

## Part 7. Media Sanitization Record

Complete one record for each storage device that is sanitized or leaves service.

| | |
|---|---|
| Person performing sanitization (name, title, organization, location, telephone, e-mail) | |
| Media (make, model number, serial number, property number) | |
| Media type and source | |
| Sensitivity | Electronic protected health information |
| Operational or damaged | |
| Sanitization method | ☐ Clear ☐ Purge ☐ Destroy |
| Sanitization technique | |
| Tool used and version | |
| Verification method and result | |
| Validated by | |
| Notes | |
| Destination | ☐ Internal reuse ☐ External reuse ☐ Recycling facility ☐ Manufacturer ☐ Other |
| Destination details | |
| Signature and date | |

## Part 8. Attestation

The undersigned certifies that, as of the date below, Business Associate has returned to Covered Entity the
hand-over copy described in Part 5 and has destroyed all protected health information received from, or
created, maintained or received on behalf of, Covered Entity, in every form, by the methods recorded above,
and retains no copies, except as stated in Part 6. Deleted data on the workstation drive, which remains in
service, is unreadable without its BitLocker keys; the drive will be sanitized and recorded in Part 7 when
it leaves service. If the reason in Part 1 is migration, this certificate covers only the environment
identified in Part 1, and the migrated copy remains subject to the Business Associate Agreement. The
information in this certificate is accurate to the best of the undersigned's knowledge.

| BUSINESS ASSOCIATE | ACKNOWLEDGED BY COVERED ENTITY |
|---|---|
| Entity: Betz Technology Providers | Entity: |
| Signature: | Signature: |
| Name: Connor Betz | Name: |
| Title: Owner | Title: |
| Date: | Date: |
