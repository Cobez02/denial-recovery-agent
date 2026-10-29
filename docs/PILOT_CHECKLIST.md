# Pilot clinic consent and agreement checklist

The clinic's consent is a signed Business Associate Agreement plus a pilot agreement, signed by someone
authorized to bind the practice. Patients do not sign anything: HIPAA allows a provider to share data with a
business associate for payment purposes once a BAA is in place. **Have a healthcare attorney review both
documents before signing.** Nothing in this repository is legal advice.

| Done | Item | What it covers |
|---|---|---|
| [ ] | Authorized signer | The owner or an officer signs; not front-desk staff |
| [ ] | Business associate agreement | Your HIPAA obligations, permitted uses, subcontractors (model provider under its own BAA), breach notice timing (commonly 5 to 10 business days, within HIPAA's 60-day maximum) |
| [ ] | Pilot scope and length | Phases 2 and 3, about 12 weeks, with an option to extend |
| [ ] | Who decides and submits | Clinic staff approve every action and submit every claim; the software only drafts (the export package's `summary.json` says `"submitted_by_software": false`) |
| [ ] | Data in scope | 835 files, denied-claim list, needed claim fields, note excerpts for appeals only; a set date range |
| [ ] | How data moves | Encrypted share or SFTP; no email attachments, no shared logins; each reviewer has an account of their own (`denial-agent create-user`) with an authenticator app |
| [ ] | Fees | Free, or a success fee on recovered backlog dollars (for example 15%); state which |
| [ ] | Liability and insurance | Each party's responsibilities; errors-and-omissions and cyber certificates attached |
| [ ] | Use of results | Permission to report results and use a de-identified case study |
| [ ] | Ending the pilot | Either side can stop with notice; data returned or destroyed within a set number of days, with written confirmation. `denial-agent purge-clinic <id> --confirm` deletes every uploaded file and export package on disk, exports the clinic's rows to one zip, deletes the rows, vacuums the SQLite file (on PostgreSQL the DBA runs `VACUUM FULL`) and writes the destruction record (`clinic_purged` audit event with counts and file names); the zip is the hand-over copy to deliver and then delete |
| [ ] | Contacts and cadence | Named clinic contact, biller, and the weekly 30-minute review |

## Model provider

- [ ] HIPAA-ready API organization confirmed and BAA active (plain Messages API calls only; batch, files,
      code execution and computer use are outside the BAA and are not used by this software)
- [ ] API key issued from that organization and provided to the app as the `ANTHROPIC_API_KEY`
      environment variable or in the git-ignored `.env`; never in code, never logged, never a personal key
- [ ] `LLM_MODEL` pinned and the prompt regression run against it; `LLM_SERVER_FALLBACKS=false`

## Software setup the clinic's data needs (before shadow mode)

- [ ] `SECRET_KEY` generated (the server refuses a placeholder), `AUTH_REQUIRED=true`
- [ ] `denial-agent create-clinic`, one `create-user` per reviewer, authenticator apps enrolled
- [ ] Each payer's timely-filing, corrected-claim and appeal windows entered with
      `denial-agent set-payer` after the first claims import (new payers get 90 / 90 / 180 days)
- [ ] `docs/SECURITY_CHECKLIST.md` complete and dated

## Phase 0 exit

Phase 0 ends when the pilot clinic has signed, the model provider BAA is active, and the laptop passes
`docs/SECURITY_CHECKLIST.md`.
