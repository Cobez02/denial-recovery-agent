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
| [ ] | Who decides and submits | Clinic staff approve every action and submit every claim; the software only drafts |
| [ ] | Data in scope | 835 files, denied-claim list, needed claim fields, note excerpts for appeals only; a set date range |
| [ ] | How data moves | Encrypted share or SFTP; no email attachments, no shared logins |
| [ ] | Fees | Free, or a success fee on recovered backlog dollars (for example 15%); state which |
| [ ] | Liability and insurance | Each party's responsibilities; errors-and-omissions and cyber certificates attached |
| [ ] | Use of results | Permission to report results and use a de-identified case study |
| [ ] | Ending the pilot | Either side can stop with notice; data returned or destroyed within a set number of days, with written confirmation (`denial-agent purge-clinic` produces the export and the destruction record) |
| [ ] | Contacts and cadence | Named clinic contact, biller, and the weekly 30-minute review |

## Model provider

- [ ] HIPAA-ready API organization confirmed and BAA active (plain Messages API calls only; batch, files, code execution and computer use are outside the BAA and are not used by this software)
- [ ] API key issued from that organization and stored in the OS keychain or the git-ignored `.env`

## Phase 0 exit

Phase 0 ends when the pilot clinic has signed, the model provider BAA is active, and the laptop passes
`docs/SECURITY_CHECKLIST.md`.
