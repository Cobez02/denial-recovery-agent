"""Put the real identifiers back into export packages made from a pseudonymized batch
(docs/SPEC.md section 16): ``denial-agent deanonymize PACKAGE.zip ... --key KEY.json --out OUT_DIR``.

An approved item's export package (``app/review/export.py``) carries the pseudonyms the app worked
with. Before a person submits the corrected claim or the appeal, the clinic runs this on its own
computer, where the key is, and every package is rebuilt member by member:

- ``corrected_claim.csv``: every row through :func:`restore_claim_row` (the patient block, account
  number and member id, date of birth, address, claim numbers, a corrected claim's ``-R<n>`` number
  included, dates moved back), written the way the export writes it;
- ``summary.json`` and ``corrected_claim.json``: field by field: claim numbers and patient ids
  through the key, data dates (the deadline, service and submission dates, a note's date) moved
  back, free text (summary, rationale, citation and note text, a change's reason) restored like a
  letter; the app's own timestamps (``*_at``) and the date a reviewer typed a note stay as they are;
- ``appeal_letter.txt`` and ``clinic_question.txt``: every pseudonym spelling of the patient the key
  knows (names, ids, date of birth, address, claim numbers) back to its original and every other
  date moved back, except the letter's own date (its first line: the day it was drafted);
- ``appeal_letter.html`` rebuilt from the restored letter with ``letter_html``, and
  ``appeal_letter.pdf`` rendered from that again (``README.txt`` when the renderer is missing);
- ``audit_excerpt.json`` kept as exported: it is the app's own hash-chained record, in pseudonyms.

Every restored member except the audit excerpt is then searched for a pseudonym left: the patient's
own spellings, and every other patient's full name and long ids from the key. A package with one
left, a package whose patient the key does not know, or a member this software never exports is
refused; nothing is written unless every package passed. Restored packages carry the real
identifiers: they are written outside the repository, never over an existing file, under the name
the app gives a package (``work_item_<id>_<claim number>_v<version>_<stamp>.zip``) with the real
claim number. Logs and stdout carry file names, member names and counts only.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.anonymize.batch import restore_claim_row
from app.anonymize.pseudonyms import AnonymizeError, PseudonymKey, Subject, load_key, value_key
from app.anonymize.text import LONG_ID_LENGTH, TEXT_DATE_RE, TextReplacer, subject_pairs
from app.config import PROJECT_ROOT
from app.drafting.pdf import letter_html, render_pdf
from app.enums import IdentifierKind
from app.redact.registry import name_variants
from app.review.export import PDF_README, csv_bytes

log = logging.getLogger(__name__)

SUMMARY = "summary.json"
CORRECTED_JSON = "corrected_claim.json"
CORRECTED_CSV = "corrected_claim.csv"
LETTER_TXT = "appeal_letter.txt"
LETTER_HTML = "appeal_letter.html"
LETTER_PDF = "appeal_letter.pdf"
PDF_NOTE = "README.txt"
QUESTION = "clinic_question.txt"
AUDIT = "audit_excerpt.json"
PACKAGE_MEMBERS: frozenset[str] = frozenset(
    {SUMMARY, CORRECTED_JSON, CORRECTED_CSV, LETTER_TXT, LETTER_HTML, LETTER_PDF, PDF_NOTE, QUESTION, AUDIT}
)
NOT_SEARCHED: frozenset[str] = frozenset({AUDIT, LETTER_PDF})

CLAIM_NUMBER_KEYS: frozenset[str] = frozenset(
    {"claim_control_number", "original_claim_control_number", "line_item_control_number"}
)
PATIENT_ID_KEYS: frozenset[str] = frozenset({"patient_account_number", "member_id"})
DATA_DATE_KEYS: frozenset[str] = frozenset(
    {
        "deadline",
        "note_date",
        "received_date",
        "payment_date",
        "service_date",
        "service_date_from",
        "service_date_to",
        "original_submission_date",
    }
)
TEXT_KEYS: frozenset[str] = frozenset({"summary", "rationale", "text", "reason", "author"})
CHANGE_PATH_RE = re.compile(r"^(?:lines\[\d+\]\.)?(?P<field>[a-z_]+)$")
PACKAGE_NAME_RE = re.compile(
    r"^work_item_(?P<id>\d+)_(?P<ccn>.+)_v(?P<version>\d+)_(?P<stamp>\d{8}T\d{6}Z)\.zip$"
)
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")  # as app.review.export.package_file_name writes the claim number

RESTORED = "restored"
REBUILT = "rebuilt from the restored letter"
KEPT = "kept as exported (the app's hash-chained record)"
NO_PDF = "kept: the PDF renderer is not installed here"


@dataclass
class RestoredPackage:
    """One package rebuilt with the real identifiers (``payload`` is the new zip)."""

    source: str
    target: str
    members: dict[str, str]
    replaced: int
    payload: bytes = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "package": self.source,
            "restored": self.target,
            "members": self.members,
            "identifiers_replaced": self.replaced,
        }


@dataclass
class RestoreResult:
    out_dir: Path
    key_path: Path
    packages: list[RestoredPackage]

    def summary(self) -> dict[str, Any]:
        """What the CLI prints: paths, file and member names, counts."""
        return {
            "out_dir": str(self.out_dir),
            "key_file": str(self.key_path),
            "packages": [package.as_dict() for package in self.packages],
        }


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def check_restore_paths(
    packages: list[Path], key_path: Path, out_dir: Path, *, project_root: Path = PROJECT_ROOT
) -> tuple[list[Path], Path, Path]:
    """Refused before anything is read: a missing package or key, a key or an output folder inside
    the repository (restored packages carry the real identifiers), an output that is not a folder."""
    root = project_root.resolve()
    key = key_path.expanduser().resolve()
    out = out_dir.expanduser().resolve()
    sources = [path.expanduser().resolve() for path in packages]
    if not sources:
        raise AnonymizeError("name at least one export package")
    for source in sources:
        if not source.is_file():
            raise AnonymizeError(f"package not found: {source}")
    if not key.is_file():
        raise AnonymizeError(f"--key {key} not found")
    if _inside(key, root):
        raise AnonymizeError(
            f"--key {key} is inside the repository {root}; a key never lives there: move it back to the "
            "clinic's computer"
        )
    if _inside(out, root):
        raise AnonymizeError(
            f"--out {out} is inside the repository {root}; restored packages carry the real identifiers: "
            "write them on the clinic's computer, outside the repository"
        )
    if out.exists() and not out.is_dir():
        raise AnonymizeError(f"--out {out} exists and is not a folder")
    return sources, key, out


def _read_package(path: Path) -> dict[str, bytes]:
    """The members of one export package, in their order; anything this software never exports is
    refused (a member that cannot be restored must not travel next to restored ones)."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            unknown = sorted(name for name in names if name not in PACKAGE_MEMBERS)
            if unknown:
                raise AnonymizeError(
                    f"{path.name}: members an export package never holds: {', '.join(unknown)}"
                )
            if SUMMARY not in names:
                raise AnonymizeError(f"{path.name}: no {SUMMARY}, so not an export package")
            return {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise AnonymizeError(f"{path.name} is not a zip file") from exc


def _write_package(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _dumps(data: Any) -> bytes:
    """JSON the way app.review.export writes it."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")


class _PackageRestore:
    """One package and the key's record of its patient."""

    def __init__(self, key: PseudonymKey, name: str, members: dict[str, bytes]) -> None:
        self.key = key
        self.name = name
        self.members = members
        self.shift = key.date_shift_days
        self.summary = self._json_member(SUMMARY)
        account = self._account()
        document = key.subjects.get(account) if account is not None else None
        if account is None or document is None:
            raise AnonymizeError(
                f"{name}: its patient is not in this key (a package from another batch, or another key?)"
            )
        pairs = subject_pairs(Subject.from_document(account, document, None))
        self.reverse = TextReplacer([(kind, pseudonym, original) for kind, original, pseudonym in pairs])
        self.check = TextReplacer(
            [(kind, pseudonym, "") for kind, _original, pseudonym in pairs] + self._other_patients(account)
        )
        self.replaced = 0
        self.claim_number = ""

    # -- the patient --------------------------------------------------------------------------

    def _json_member(self, member: str) -> Any:
        try:
            return json.loads(self.members[member].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnonymizeError(f"{self.name}: {member} is not readable JSON") from exc

    def _csv_rows(self) -> list[dict[str, str]]:
        reader = csv.DictReader(io.StringIO(self.members[CORRECTED_CSV].decode("utf-8")))
        return [dict(row) for row in reader]

    def _account(self) -> str | None:
        """The patient's original account number: from the corrected claim, else through the claim."""
        pseudo_account: str | None = None
        if CORRECTED_CSV in self.members:
            rows = self._csv_rows()
            pseudo_account = rows[0].get("patient_account_number") if rows else None
        elif CORRECTED_JSON in self.members:
            pseudo_account = (self._json_member(CORRECTED_JSON).get("after") or {}).get(
                "patient_account_number"
            )
        if pseudo_account:
            return self.key.original("patient_id", pseudo_account)
        claim_number = str(self.summary.get("claim_control_number") or "")
        original = self.key.original_claim_number(claim_number) or claim_number
        return (self.key.index.get("claims") or {}).get(value_key(original)) if original else None

    def _other_patients(self, account: str) -> list[tuple[IdentifierKind, str, str]]:
        """Every other patient's pseudonymized full names and long ids: one of them in this package
        is a pseudonym the restore cannot put back (a reviewer quoting another patient, say)."""
        found: list[tuple[IdentifierKind, str, str]] = []
        for key, document in self.key.subjects.items():
            if key == account:
                continue
            for spelling in document.get("spellings") or []:
                variants = name_variants(spelling.get("pseudo_first"), spelling.get("pseudo_last"))
                if spelling.get("pseudo_first") and spelling.get("pseudo_last"):
                    found.extend((IdentifierKind.PATIENT_NAME, variant, "") for variant in variants[:3])
            for pseudonym in (document.get("ids") or {}).values():
                if len(value_key(pseudonym)) >= LONG_ID_LENGTH:
                    found.append((IdentifierKind.MEMBER_ID, pseudonym, ""))
        return found

    # -- values -------------------------------------------------------------------------------

    def text(self, value: str, *, letter: bool = False) -> str:
        """Pseudonyms back to the originals, dates moved back; a letter keeps its own first-line date."""
        head = ""
        if letter:
            first, newline, rest = value.partition("\n")
            if TEXT_DATE_RE.fullmatch(first.strip()):
                head, value = first + newline, rest
        result = self.reverse.process(value, shift_days=-self.shift, scan=False)
        self.replaced += result.replaced
        return head + result.text

    def _date(self, value: str) -> str:
        if not self.shift:
            return value
        try:
            return (date.fromisoformat(value) - timedelta(days=self.shift)).isoformat()
        except ValueError:
            return value

    def json_value(self, value: Any, key: str | None = None, *, typed_by_reviewer: bool = False) -> Any:
        """Restore one JSON value by the name of the field that holds it (see the module docstring)."""
        if isinstance(value, dict):
            if {"path", "before", "after"} <= value.keys():  # a corrected-claim change: by its path
                match = CHANGE_PATH_RE.match(str(value["path"]))
                target = match.group("field") if match else None
                return {
                    k: self.json_value(v, target if k in ("before", "after") else k) for k, v in value.items()
                }
            typed = bool(value.get("typed_by_reviewer"))
            return {k: self.json_value(v, k, typed_by_reviewer=typed) for k, v in value.items()}
        if isinstance(value, list):
            return [self.json_value(item, key, typed_by_reviewer=typed_by_reviewer) for item in value]
        if not isinstance(value, str) or not value.strip():
            return value
        if key in CLAIM_NUMBER_KEYS:
            return self.key.original_claim_number(value) or value
        if key in PATIENT_ID_KEYS:
            return self.key.original("patient_id", value) or value
        if key in DATA_DATE_KEYS:
            # the day a reviewer typed a note on the item page is a real date, never shifted
            return value if key == "note_date" and typed_by_reviewer else self._date(value)
        if key in TEXT_KEYS:
            return self.text(value)
        return value

    # -- the package --------------------------------------------------------------------------

    def run(self) -> tuple[dict[str, bytes], dict[str, str]]:
        """The restored members (in the package's order) and what was done to each."""
        out: dict[str, bytes] = {}
        done: dict[str, str] = {}
        restored_summary = self.json_value(self.summary)
        self.claim_number = str(restored_summary.get("claim_control_number") or "")
        for member, data in self.members.items():
            if member == SUMMARY:
                out[member] = _dumps(restored_summary)
            elif member == CORRECTED_JSON:
                out[member] = _dumps(self.json_value(self._json_member(member)))
            elif member == CORRECTED_CSV:
                out[member] = csv_bytes([restore_claim_row(row, self.key) for row in self._csv_rows()])
            elif member in (LETTER_TXT, QUESTION):
                out[member] = self.text(data.decode("utf-8"), letter=member == LETTER_TXT).encode("utf-8")
            elif member == AUDIT:
                out[member], done[member] = data, KEPT
                continue
            else:  # the HTML and the PDF follow the restored letter, below
                continue
            done[member] = RESTORED
        if LETTER_TXT in out:
            html = letter_html(out[LETTER_TXT].decode("utf-8"), f"Appeal of claim {self.claim_number}")
            if LETTER_HTML in self.members:
                out[LETTER_HTML], done[LETTER_HTML] = html.encode("utf-8"), REBUILT
            if LETTER_PDF in self.members or PDF_NOTE in self.members:
                pdf = render_pdf(html)
                if pdf:
                    out[LETTER_PDF], done[LETTER_PDF] = pdf, REBUILT
                else:
                    out[PDF_NOTE], done[PDF_NOTE] = PDF_README.encode("utf-8"), NO_PDF
        order = [name for name in self.members if name in out] + [
            name for name in out if name not in self.members
        ]
        return {name: out[name] for name in order}, {name: done[name] for name in order}

    def leftovers(self, members: dict[str, bytes]) -> list[str]:
        """``member (kind)`` for every pseudonym still found in a restored member."""
        found: list[str] = []
        for member, data in members.items():
            if member in NOT_SEARCHED:
                continue
            for kind in dict.fromkeys(self.check.remaining(data.decode("utf-8"))):
                found.append(f"{member} ({kind})")
        return found

    def target_name(self) -> str:
        match = PACKAGE_NAME_RE.match(self.name)
        if match is None or not self.claim_number:
            return self.name
        ccn = _SAFE.sub("-", self.claim_number).strip("-")[:40]
        return f"work_item_{match['id']}_{ccn}_v{match['version']}_{match['stamp']}.zip"


def restore_packages(
    packages: list[Path], *, key_path: Path, out_dir: Path, project_root: Path = PROJECT_ROOT
) -> RestoreResult:
    """Restore every package (see the module docstring); write them only when all of them passed."""
    sources, key_file, out = check_restore_paths(packages, key_path, out_dir, project_root=project_root)
    key = load_key(key_file)
    restored: list[RestoredPackage] = []
    for source in sources:
        restore = _PackageRestore(key, source.name, _read_package(source))
        members, done = restore.run()
        left = restore.leftovers(members)
        if left:
            raise AnonymizeError(
                f"{source.name}: a pseudonym is left after the restore in {', '.join(left)}; nothing was "
                "written (put the real values in by hand, or check that this is the batch's own key)"
            )
        restored.append(
            RestoredPackage(
                source.name, restore.target_name(), done, restore.replaced, _write_package(members)
            )
        )
    targets = [package.target.casefold() for package in restored]
    if len(set(targets)) != len(targets):
        raise AnonymizeError("two packages would be restored under the same name; restore them one at a time")
    for package in restored:
        if (out / package.target).exists():
            raise AnonymizeError(
                f"{package.target} already exists in --out; a restored package is never overwritten"
            )
    out.mkdir(parents=True, exist_ok=True)
    for package in restored:
        with open(out / package.target, "xb") as handle:
            handle.write(package.payload)
    log.info("restored %d export package(s) into the output folder", len(restored))
    return RestoreResult(out_dir=out, key_path=key_file, packages=restored)


__all__ = ["RestoreResult", "RestoredPackage", "check_restore_paths", "restore_packages"]
