# Clinic Set-up Sheet

For the Clinic's biller or IT contact. Two of Plumbline's commands run on one Clinic computer:
`denial-agent anonymize` replaces the patient identifiers in each batch before it is uploaded, and
`denial-agent deanonymize` puts them back into the packages that come back, before anything is
submitted. Betz Technology Providers (BTP) never runs them and never receives the raw files, the key or
the restored packages. This sheet is the set-up that Section 8 of the Clinic Data Request (Exhibit B of
the Pilot Agreement, which its Section 5(h) points to) refers to.

## 1. The computer and its folders

- Use one Clinic computer with disk encryption turned on (BitLocker on Windows), used by the people who
  prepare the uploads. The raw files, the key folder and the restored packages stay on it.
- Make these folders on the computer's own disk: not on the Desktop or in Documents when OneDrive syncs
  them, not in Dropbox, Google Drive, iCloud or Box, and not on a USB or network drive. The commands
  refuse a key or an output folder in a synced folder or inside the program's own folder, and a key on
  the USB or network drive the output folder is written to; keeping everything else on the computer's
  own disk is this sheet's rule, not a check. The names are examples; keep the layout.

| Folder | Holds |
|---|---|
| `D:/clinic/raw/<upload date>` | the files of one upload, unchanged: 835 files, claims report, note excerpts, payer letters |
| `D:/clinic/to-company/<upload date>` | what `anonymize` writes: the folder you upload |
| `D:/clinic/keys` | the key folder: one key file per upload, and the run log the commands keep |
| `D:/clinic/from-company` | packages downloaded from the secure transfer service |
| `D:/clinic/to-submit` | restored packages, ready to submit |

## 2. Install (once, under an hour)

1. Install `uv` from astral.sh (one installer; it manages Python for the commands).
2. Copy the install file BTP supplies, `denial_agent-0.1.0-py3-none-any.whl`, to the computer and run,
   in a terminal opened in the folder that holds it:

   ```
   uv tool install ./denial_agent-0.1.0-py3-none-any.whl
   ```

   It downloads the program's components once, so the computer needs internet for this step. For a
   computer without internet, BTP supplies those components in a folder with the install file, and the
   install line is then `uv tool install --offline --find-links <that folder> ./denial_agent-0.1.0-py3-none-any.whl`.
3. `denial-agent --help` lists the commands. The install creates no database and starts no server.

## 3. Before each upload

The first upload makes the first key. Every later upload reads the newest key and writes a new one,
in the same key folder. The folder and key names below are examples by upload date.

```
denial-agent anonymize D:/clinic/raw/2026-11-02 --out D:/clinic/to-company/2026-11-02 --key-out D:/clinic/keys/2026-11-02.key.json --replace-claim-numbers

denial-agent anonymize D:/clinic/raw/2026-11-09 --out D:/clinic/to-company/2026-11-09 --key-in D:/clinic/keys/2026-11-02.key.json --key-out D:/clinic/keys/2026-11-09.key.json --replace-claim-numbers
```

- Always use `--replace-claim-numbers`: claim numbers often carry the patient's account number, and the
  command refuses such a batch without it.
- The command prints counts and file positions only, never a patient's details. It writes the whole
  output folder or nothing.
- If it stops with a list of file, row and field, a note or letter still holds something it cannot
  replace (a Social Security number, a phone number, an e-mail address, a Medicare beneficiary
  identifier, a date after "DOB", an id after a label such as MRN, member or account). Fix that row in
  your system's export and run the command again. Do not send the batch until the command finishes.
- If it stops with "the leak check found", do not send the batch; tell BTP the file, place and field it
  printed, never the value.
- Upload the `--out` folder as it is, through the secure transfer service, in the folder for files to
  BTP. Do not rename or edit its files. Never e-mail it, never put it on a USB drive, and never upload
  the key folder.
- After each run, copy the key folder to the Clinic's backup media and date the copy. Only the newest
  key works for the next upload and for restoring packages; an older key is refused.

## 4. Before each submission

Every package that comes back is named `PSEUDONYMIZED_...` and carries invented names. Download it to
`D:/clinic/from-company` and restore it with the newest key:

```
denial-agent deanonymize D:/clinic/from-company/PSEUDONYMIZED_work_item_14_KXW938899_v1_20261112T210501Z.zip --key D:/clinic/keys/2026-11-09.key.json --out D:/clinic/to-submit
```

Several packages can be named at once. The restored package in `D:/clinic/to-submit` carries the real
names and claim numbers, under the app's package name without the `PSEUDONYMIZED_` prefix and with the
real claim number; submit from it through the Clinic's usual channels. Never send a `PSEUDONYMIZED_`
package to a payer. If a restored letter shows `[[MASKED_...]]`, fill that spot in by hand: it is a
value the command masked. If an appeal package holds `appeal_letter.html` and no PDF, open the HTML in
a browser and print it.

## 5. If something goes wrong

- A refusal names its rule: a key next to the output folder, on the USB or network drive the output
  folder is written to or in a synced folder, an existing key that would be overwritten, or an older key
  once a newer one exists. Fix the path or the key and run again; nothing was written.
- A lost key folder: restore it from the backup. Without the newest key, that batch's packages cannot be
  restored and the next upload cannot continue the chain; contact BTP before uploading anything.
- When reporting a problem to BTP, name the work item number or the file position and row the message
  printed, never a patient's details.

## 6. What the command replaces and what it keeps

Replaced, with the same pseudonym each time a value recurs: patient names, account numbers, member ids,
dates of birth (the year stays), addresses, cities and ZIP codes, and claim numbers; the same values
inside 835 files, notes and letters, including another patient of the batch named in a note. Kept:
dates of service, procedure and diagnosis codes, amounts, payer names, provider names and NPIs. A
pseudonymized batch still holds dates of service, so under the agreements it remains protected health
information and travels only through the secure transfer service.

Contacts: Clinic Data Request, Section 10.
