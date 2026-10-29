# Local machine security checklist

From the plan: complete and date every item **before real patient data lands on the machine**. A laptop
holding PHI is the project's single biggest breach risk during Phases 2 to 4. Keep this file updated;
it is part of the Phase 0 exit criteria and the risk analysis.

| Done | Control | Setting | Completed on | By |
|---|---|---|---|---|
| [ ] | Disk encryption | BitLocker (Windows) or FileVault (macOS) on; recovery key stored offline | | |
| [ ] | Dedicated account | A separate OS user account used only for this project | | |
| [ ] | Sign-in | Strong password plus MFA where the OS supports it; screen locks after 5 minutes | | |
| [ ] | No cloud sync | Project and data folders excluded from OneDrive, iCloud, Dropbox and Google Drive | | |
| [ ] | Database | Postgres bound to localhost only (`docker-compose.yml` binds 127.0.0.1); data volume on the encrypted disk | | |
| [ ] | Backups | Encrypted, to an encrypted external drive kept locked away; no unencrypted copies | | |
| [ ] | Network | Firewall on; no remote-access tools open to the internet | | |
| [ ] | Updates and protection | Automatic OS updates; endpoint protection running; dependencies updated monthly | | |
| [ ] | Code repository | `data/` git-ignored; gitleaks runs before every commit (`pre-commit install`); no real data in test fixtures | | |
| [ ] | Logs | PHI masked before writing (`app/logging_config.py`); `tests/test_phi_in_logs.py` fails the build if identifiers appear | | |
| [ ] | Keys | Model API keys from the HIPAA-ready organization only, kept in the OS keychain or the git-ignored `.env`; never personal or consumer AI accounts | | |
| [ ] | Physical | Device never left unattended unlocked; device tracking and remote wipe enabled | | |
| [ ] | Disposal | Clinic data erased at pilot end or after cloud migration, with a dated record (`denial-agent purge-clinic`) | | |

## Software controls already in place (verify, do not assume)

- Session cookie is `HttpOnly`, `SameSite=Strict`; login requires password + TOTP; five failed logins lock the account for 15 minutes.
- Only `app/llm/guard.py` can reach a model, after tokenizing names, member ids, addresses, dates and claim numbers; a payload the scanner flags is refused and audited.
- The audit log is append-only and hash-chained; `denial-agent verify-audit` must print `OK`.
- The application never submits a claim; approved items are exported for a person to submit.

## Documents to write and date (Phase 0)

- [ ] Risk analysis
- [ ] Security policy (short)
- [ ] Incident response plan (who is called, breach-notice timing from the BAA)
