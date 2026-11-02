"""Put the real identifiers back into export packages made from a pseudonymized batch
(docs/SPEC.md section 16): ``denial-agent deanonymize PACKAGE.zip ... --key KEY.json --out OUT_DIR``.

An approved item's export package (``app/review/export.py``) carries the pseudonyms the app worked
with, and the marks of a pseudonymized batch (``app/review/pseudonymized.py``). Before a person
submits the corrected claim or the appeal, the clinic runs this on its own computer, where the key
is, with the newest key of the chain (the run log next to the key shows a newer one, and the run is
refused). Every package must first prove it belongs to this key:

- its patient is found by the corrected claim's account number, else through its claim number and
  the key's claim index; with replaced claim numbers every claim number must be one of the key's;
- every non-empty patient-block value of the corrected claim CSV and JSON maps back, and to that
  patient (an older key never copies a later batch's pseudonym onto a claim);
- the letter's patient block (``Patient:``, ``Member ID:``, ``Date of birth:``) is this key's
  pseudonyms of that patient (an already restored package, or another run's, is refused);
- no other patient's pseudonym is in the text (full names, name parts of four letters or more that
  this patient does not share, ids, dates of birth written as such, street lines, claim numbers):
  checked before anything is rewritten, since a pseudonym two patients share would otherwise turn
  into this patient's real name;
- no provider name of the package (the corrected claim's rendering provider, the letter's
  ``Rendering provider:`` and ``on behalf of`` lines) holds the patient's pseudonym; provider names
  are kept as they are while the rest is restored.

Then every member is rebuilt: ``corrected_claim.csv`` through :func:`restore_claim_row` (written
without the pseudonymized marker column); ``summary.json`` and ``corrected_claim.json`` field by field
(claim numbers and patient ids through the key, free text restored like a letter, the pseudonymized
mark removed); ``appeal_letter.txt`` and ``clinic_question.txt`` without the banner, every pseudonym
spelling of the patient back to its original (initials and every written form of the date of birth
included) and other claim numbers through the key; ``appeal_letter.html`` rebuilt from the restored
letter, ``appeal_letter.pdf`` rendered again (``README.txt`` when WeasyPrint is missing; the app's
pseudonymized PDF is dropped). ``audit_excerpt.json`` is left out: it is the app's record, in
pseudonyms, and never part of a submission. With a test-only key that shifted dates, data dates move
back (the app's ``*_at`` timestamps, the date a reviewer typed a note and the letter's first-line
drafting date stay).

A ``[[MASKED_...]]`` marker left by ``--allow-residual`` cannot be restored: it is reported per package
so a person fills it in by hand. Packages are written under temporary names and renamed only when
every one passed, outside the repository and any synced folder, never into a package's own folder
and never over an existing file, under the name the app gives a package with the real claim number.
One JSON line goes to the run log next to the key. Logs and stdout carry file names, member names
and counts only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.anonymize import paths
from app.anonymize.batch import restore_claim_row
from app.anonymize.matching import TEXT_DATE_RE, date_matches, date_values, name_key
from app.anonymize.pseudonyms import NAME_TOKEN_RE, AnonymizeError, PseudonymKey, Subject, load_key, value_key
from app.anonymize.text import CLAIM_ID, NAME, TOKEN_RE, TextIndex
from app.config import PROJECT_ROOT
from app.drafting.pdf import letter_html, render_pdf
from app.review import pseudonymized
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
TEXT_MEMBERS: frozenset[str] = frozenset(
    {SUMMARY, CORRECTED_JSON, CORRECTED_CSV, LETTER_TXT, QUESTION, LETTER_HTML}
)
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_RATIO = 200

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
    r"^(?:PSEUDONYMIZED_)?work_item_(?P<id>\d+)_(?P<ccn>.+)_v(?P<version>\d+)_(?P<stamp>\d{8}T\d{6}Z)\.zip$"
)
HEADER_RE = re.compile(
    r"^(?P<label>Patient|Member ID|Date of birth|Claim control number):[ \t]*(?P<value>.*?)[ \t]*$", re.M
)
PATIENT_TOKEN_RE = re.compile(r"PT-[0-9A-Z]{4,}")
PROVIDER_LINE_RE = re.compile(
    r"^[ \t]*(?:Rendering provider:|on behalf of)[ \t]+(?P<name>.+?)(?:[ \t]+\(NPI[ \t]+\d+\))?[ \t]*$", re.M
)
MASKED_RE = re.compile(r"\[\[MASKED_[A-Z_]+_\d+\]\]")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")  # as app.review.export.package_file_name writes the claim number

RESTORED = "restored"
REBUILT = "rebuilt from the restored letter"
NO_PDF = "written: the PDF renderer is not installed here, so the app's pseudonymized PDF was dropped"


@dataclass
class RestoredPackage:
    """One package rebuilt with the real identifiers (``payload`` is the new zip)."""

    source: str
    target: str
    members: dict[str, str]
    text_replacements: int
    fields_restored: int
    masked_markers: dict[str, int]
    payload: bytes = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "package": self.source,
            "restored": self.target,
            "members": self.members,
            "identifiers_replaced": self.text_replacements + self.fields_restored,
            "text_replacements": self.text_replacements,
            "fields_restored": self.fields_restored,
            "masked_markers": self.masked_markers,
            "audit_excerpt": "left out: the app's own record, in pseudonyms; never submit it",
        }


@dataclass
class RestoreResult:
    out_dir: Path
    key_path: Path
    packages: list[RestoredPackage]
    test_only_key: bool = False

    def summary(self) -> dict[str, Any]:
        """What the CLI prints: paths, file and member names, counts."""
        return {
            "out_dir": str(self.out_dir),
            "key_file": str(self.key_path),
            "test_only_key": self.test_only_key,
            "packages": [package.as_dict() for package in self.packages],
        }


def check_restore_paths(
    packages: list[Path], key_path: Path, out_dir: Path, *, project_root: Path = PROJECT_ROOT
) -> tuple[list[Path], Path, Path]:
    """Refused before anything is read: a missing package or key; a key or an output folder inside
    the repository or a synced folder (restored packages carry the real identifiers); an output that
    is not a folder, or that is, holds or sits inside the folder of a package (the download folder)."""
    root = paths.canonical(project_root)
    key = paths.canonical(key_path)
    out = paths.canonical(out_dir)
    sources = [paths.canonical(path) for path in packages]
    if not sources:
        raise AnonymizeError("name at least one export package")
    for source in sources:
        if not source.is_file():
            raise AnonymizeError(f"package not found: {source}")
    if not key.is_file():
        raise AnonymizeError(f"--key {key} not found")
    if paths.inside(key, root):
        raise AnonymizeError(
            f"--key {key} is inside the repository {root}; a key never lives there: move it back to the "
            "clinic's computer"
        )
    if paths.inside(out, root):
        raise AnonymizeError(
            f"--out {out} is inside the repository {root}; restored packages carry the real identifiers: "
            "write them on the clinic's computer, outside the repository"
        )
    for path, what in ((key, "--key"), (out, "--out")):
        client = paths.synced_folder(path)
        if client is not None:
            raise AnonymizeError(
                f"{what} {path} is in a {client} folder, which copies it to a cloud outside the clinic's "
                "agreements; use a local folder"
            )
    for source in sources:
        if paths.overlap(out, source.parent):
            raise AnonymizeError(
                "--out must not be, hold or sit inside the folder a package came in (that folder holds "
                "what came from the Company); write the restored packages to a folder of their own"
            )
    if out.exists() and not out.is_dir():
        raise AnonymizeError(f"--out {out} exists and is not a folder")
    return sources, key, out


def _read_package(path: Path) -> dict[str, bytes]:
    """The members of one export package, in their order; refused: a member this software never
    exports, a member named twice, an oversized member, no summary."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            unknown = sorted(name for name in set(names) if name not in PACKAGE_MEMBERS)
            if unknown:
                raise AnonymizeError(
                    f"{path.name}: members an export package never holds: {', '.join(unknown)}"
                )
            if len(set(names)) != len(names):
                raise AnonymizeError(
                    f"{path.name}: a member is in the package twice; a package is never built so"
                )
            if SUMMARY not in names:
                raise AnonymizeError(f"{path.name}: no {SUMMARY}, so not an export package")
            total = 0
            for info in infos:
                total += info.file_size
                ratio = info.file_size / max(info.compress_size, 1)
                if info.file_size > MAX_MEMBER_BYTES or total > MAX_PACKAGE_BYTES or ratio > MAX_RATIO:
                    raise AnonymizeError(
                        f"{path.name}: {info.filename} is larger than any export package member"
                    )
            return {info.filename: archive.read(info) for info in infos}
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


def _decode(name: str, member: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnonymizeError(f"{name}: {member} is not UTF-8 text") from exc


class _Pseudonyms:
    """Every subject's pseudonyms, to find another patient's in a package (built once per run)."""

    def __init__(self, key: PseudonymKey) -> None:
        self.key = key
        pseudo_dobs = {
            date.fromisoformat(entry["pseudonym"])
            for document in key.subjects.values()
            for entry in (document.get("dob") or {}).values()
        }
        # Another patient's date of birth counts only where the text says it is one.
        self.index = TextIndex(data_dates=frozenset(pseudo_dobs))
        for subject_key in key.subjects:
            subject = key.subject(subject_key)
            if subject is not None:
                self.index.add_subject(subject, reverse=True, claim_numbers=True)

    def others(self, text: str, subject: Subject, own_parts: set[str]) -> list[str]:
        """Kinds of other patients' pseudonyms in ``text`` (shared name parts and short ones left out)."""
        kinds: list[str] = []
        for hit in self.index.find(text, owner=subject.key):
            if hit.owner in ("", subject.key):
                continue
            if hit.kind == NAME:
                keys = [name_key(t) for t in NAME_TOKEN_RE.findall(text[hit.start : hit.end])]
                if len(keys) == 1 and (keys[0] in own_parts or len(keys[0]) < 4):
                    continue
            kinds.append(hit.kind)
        return kinds


class _PackageRestore:
    """One package and the key's record of its patient."""

    def __init__(
        self, key: PseudonymKey, name: str, members: dict[str, bytes], pseudonyms: _Pseudonyms
    ) -> None:
        self.key = key
        self.name = name
        self.members = members
        self.shift = key.date_shift_days
        self.summary = self._json_member(SUMMARY)
        self.marked = bool(self.summary.pop(pseudonymized.SUMMARY_KEY, False))
        account = self._account()
        subject = key.subject(account) if account is not None else None
        if account is None or subject is None:
            raise AnonymizeError(
                f"{name}: its patient is not in this key (a package from another batch, or another key?)"
            )
        self.subject = subject
        self.pseudonyms = pseudonyms
        self.own_parts = {
            name_key(token)
            for spelling in subject.spellings
            for value in (spelling.pseudo_first, spelling.pseudo_last, spelling.pseudo_middle)
            for token in NAME_TOKEN_RE.findall(value)
        }
        self.reverse = TextIndex(data_dates=frozenset(self._package_dates()))
        self.reverse.add_subject(subject, reverse=True, claim_numbers=True)
        for family in ("claim_number", "payer_claim_number"):
            for original, pseudonym in ((key.document.get("values") or {}).get(family) or {}).items():
                self.reverse.add_claim_number(pseudonym, original)
        self.own = TextIndex()
        self.own.add_subject(subject, reverse=True)
        self.text_replacements = 0
        self.fields_restored = 0
        self.claim_number = ""
        self.masked: dict[str, int] = {}

    # -- the patient --------------------------------------------------------------------------

    def _json_member(self, member: str) -> Any:
        try:
            return json.loads(_decode(self.name, member, self.members[member]))
        except json.JSONDecodeError as exc:
            raise AnonymizeError(f"{self.name}: {member} is not readable JSON") from exc

    def _csv_rows(self) -> list[dict[str, str]]:
        reader = csv.DictReader(io.StringIO(_decode(self.name, CORRECTED_CSV, self.members[CORRECTED_CSV])))
        return [dict(row) for row in reader]

    def _claim_number(self, value: str, where: str) -> str:
        """A claim number of the package back to the original; refused when the key replaced claim
        numbers and does not know this one."""
        original = self.key.original_claim_number(value)
        if original is None and self.key.replace_claim_numbers:
            raise AnonymizeError(f"{self.name}: {where} is not a claim number of this key (another key?)")
        return original or value

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
        if not claim_number:
            return None
        original = self._claim_number(claim_number, "summary.json claim_control_number")
        return (self.key.index.get("claims") or {}).get(value_key(original))

    def _package_dates(self) -> set[date]:
        """The data dates of the package's structured members (what a date of birth must not be)."""
        found: set[date] = set()

        def walk(value: Any, key: str | None = None) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, k)
            elif isinstance(value, list):
                for item in value:
                    walk(item, key)
            elif isinstance(value, str) and key in DATA_DATE_KEYS:
                try:
                    found.add(date.fromisoformat(value[:10]))
                except ValueError:
                    pass

        walk(self.summary)
        if CORRECTED_JSON in self.members:
            walk(self._json_member(CORRECTED_JSON))
        return found

    def check_letter(self, text: str) -> None:
        """The letter's patient block must be this key's pseudonyms of the package's patient."""
        subject = self.subject
        names = {
            " ".join(f"{s.pseudo_first} {s.pseudo_last}".split()).casefold()
            for s in subject.spellings
            if s.pseudo_first or s.pseudo_last
        }
        ids = {value_key(value) for value in subject.ids.values()}
        dobs = {pseudonym for pseudonym, _offset in subject.dobs.values()}
        for match in HEADER_RE.finditer(text):
            label, value = match.group("label"), match.group("value")
            if not value:
                continue
            if label == "Patient":
                ok = (
                    " ".join(value.split()).casefold() in names
                    or PATIENT_TOKEN_RE.fullmatch(value) is not None
                )
            elif label == "Member ID":
                ok = value_key(value) in ids
            elif label == "Date of birth":
                ok = any(v in dobs for m, found in date_matches(value) for v in date_values(m, found))
            else:
                ok = not self.key.replace_claim_numbers or self.key.original_claim_number(value) is not None
            if not ok:
                raise AnonymizeError(
                    f"{self.name}: the letter's '{label}' line is not this key's pseudonym of the package's "
                    "patient (an already restored package, a package of another run, or an older key?)"
                )

    def check_providers(self, texts: dict[str, str]) -> None:
        """A provider name of the package must not hold the patient's pseudonym (a provider who joined
        after the pseudonym was given): the restore would write the patient's real name into it.
        Provider names are kept as they are while the rest is restored."""
        names: set[str] = set()
        if CORRECTED_CSV in self.members:
            names |= {row.get("rendering_provider_name") or "" for row in self._csv_rows()}
        for text in texts.values():
            names |= {match.group("name") for match in PROVIDER_LINE_RE.finditer(text)}
        for name in sorted(n for n in names if n.strip()):
            if {name_key(token) for token in NAME_TOKEN_RE.findall(name)} & self.own_parts:
                raise AnonymizeError(
                    f"{self.name}: a provider's name in the package is also the patient's pseudonym; nothing was "
                    "written (restore this package by hand)"
                )
            self.reverse.add_keep_name(name, person=True)

    def check_others(self, member: str, text: str) -> None:
        kinds = self.pseudonyms.others(text, self.subject, self.own_parts)
        if kinds:
            raise AnonymizeError(
                f"{self.name}: {member} names another patient ({', '.join(dict.fromkeys(kinds))}); nothing was "
                "written (a reviewer quoting another patient? take it out of the letter in the app and export "
                "again)"
            )

    # -- values -------------------------------------------------------------------------------

    def text(self, value: str, *, letter: bool = False) -> str:
        """Pseudonyms back to the originals; with a shifting test key, dates move back (a letter
        keeps its own first-line drafting date)."""
        head = ""
        if letter and self.shift:
            first, newline, rest = value.partition("\n")
            if TEXT_DATE_RE.fullmatch(first.strip()):
                head, value = first + newline, rest
        result = self.reverse.process(value, owner=self.subject.key, shift_days=-self.shift, scan=False)
        self.text_replacements += result.replaced
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
            restored = self._claim_number(value, key)
            self.fields_restored += restored != value
            return restored
        if key in PATIENT_ID_KEYS:
            original = self.key.original("patient_id", value)
            if original is None or value_key(original) not in {value_key(k) for k in self.subject.ids}:
                raise AnonymizeError(
                    f"{self.name}: {key} is not a pseudonym of the package's patient in this key"
                )
            self.fields_restored += 1
            return original
        if key in DATA_DATE_KEYS:
            # the day a reviewer typed a note on the item page is a real date, never shifted
            return value if key == "note_date" and typed_by_reviewer else self._date(value)
        if key in TEXT_KEYS:
            return self.text(value)
        return value

    # -- the package --------------------------------------------------------------------------

    def run(self) -> tuple[dict[str, bytes], dict[str, str]]:
        """The restored members (in the package's order) and what was done to each."""
        texts: dict[str, str] = {}
        for member in (LETTER_TXT, QUESTION):
            if member in self.members:
                texts[member], _bannered = pseudonymized.strip_banner(
                    _decode(self.name, member, self.members[member])
                )
        if LETTER_TXT in texts:
            self.check_letter(texts[LETTER_TXT])
        self.check_providers(texts)
        for member in (SUMMARY, CORRECTED_JSON):
            if member in self.members:
                self.check_others(member, _decode(self.name, member, self.members[member]))
        for member, text in texts.items():
            self.check_others(member, text)

        out: dict[str, bytes] = {}
        done: dict[str, str] = {}
        restored_summary = self.json_value(self.summary)
        self.claim_number = str(restored_summary.get("claim_control_number") or "")
        for member in self.members:
            if member == SUMMARY:
                out[member] = _dumps(restored_summary)
            elif member == CORRECTED_JSON:
                out[member] = _dumps(self.json_value(self._json_member(member)))
            elif member == CORRECTED_CSV:
                rows = []
                for row in self._csv_rows():
                    try:
                        rows.append(restore_claim_row(row, self.key))
                    except AnonymizeError as exc:
                        raise AnonymizeError(f"{self.name}: {CORRECTED_CSV} {exc}") from exc
                    self.fields_restored += sum(rows[-1].get(c) != row.get(c) for c in rows[-1])
                out[member] = csv_bytes(rows)
            elif member in texts:
                before = self.text_replacements
                out[member] = self.text(texts[member], letter=member == LETTER_TXT).encode("utf-8")
                if (
                    member == LETTER_TXT
                    and _has_patient_block(texts[member])
                    and self.text_replacements == before
                ):
                    raise AnonymizeError(
                        f"{self.name}: the letter has a patient block but nothing in it is this key's pseudonym"
                    )
            else:  # the HTML and the PDF follow the restored letter, below; the audit excerpt stays out
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
        for name in order:
            if name in TEXT_MEMBERS:
                count = len(MASKED_RE.findall(out[name].decode("utf-8")))
                if count:
                    self.masked[name] = count
        return {name: out[name] for name in order}, {name: done[name] for name in order}

    def leftovers(self, members: dict[str, bytes]) -> list[str]:
        """``member (kind)`` for every pseudonym of the patient still found in a restored member."""
        found: list[str] = []
        for member, data in members.items():
            if member not in TEXT_MEMBERS:
                continue
            text = data.decode("utf-8")
            text = TOKEN_RE.sub(" ", text)
            kinds = [hit.kind for hit in self.own.find(text, owner=self.subject.key) if hit.kind != CLAIM_ID]
            found.extend(f"{member} ({kind})" for kind in dict.fromkeys(kinds))
            if pseudonymized.BANNER in text or pseudonymized.CSV_COLUMN in text:
                found.append(f"{member} (PSEUDONYMIZED mark)")
        return found

    def target_name(self) -> str:
        match = PACKAGE_NAME_RE.match(self.name)
        bare = self.name.removeprefix(pseudonymized.ZIP_PREFIX)
        if match is None or not self.claim_number:
            return bare
        ccn = _SAFE.sub("-", self.claim_number).strip("-")[:40]
        return f"work_item_{match['id']}_{ccn}_v{match['version']}_{match['stamp']}.zip"


def _has_patient_block(text: str) -> bool:
    """The letter names the patient (not only by the app's patient token)."""
    return any(
        match.group("label") != "Claim control number"
        and match.group("value")
        and PATIENT_TOKEN_RE.fullmatch(match.group("value")) is None
        for match in HEADER_RE.finditer(text)
    )


def restore_packages(
    packages: list[Path], *, key_path: Path, out_dir: Path, project_root: Path = PROJECT_ROOT
) -> RestoreResult:
    """Restore every package (see the module docstring); write them only when all of them passed."""
    sources, key_file, out = check_restore_paths(packages, key_path, out_dir, project_root=project_root)
    key = load_key(key_file)
    newer = paths.newer_runs([key_file.parent], key.chain_id, key.run_number)
    if newer:
        names = ", ".join(sorted({str(entry.get("key") or "?") for entry in newer}))
        raise AnonymizeError(
            f"--key is not the newest key of its chain: the run log next to it shows a later run ({names}); "
            "use the newest key, which holds every earlier one"
        )
    pseudonyms = _Pseudonyms(key)
    restored: list[RestoredPackage] = []
    for source in sources:
        restore = _PackageRestore(key, source.name, _read_package(source), pseudonyms)
        members, done = restore.run()
        left = restore.leftovers(members)
        if left:
            raise AnonymizeError(
                f"{source.name}: a pseudonym is left after the restore in {', '.join(left)}; nothing was "
                "written (put the real values in by hand, or check that this is the newest key)"
            )
        restored.append(
            RestoredPackage(
                source.name,
                restore.target_name(),
                done,
                restore.text_replacements,
                restore.fields_restored,
                restore.masked,
                _write_package(members),
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
    publication = paths.Publication()
    try:
        staged = [
            (publication.stage_file(out / package.target, package.payload), package) for package in restored
        ]
        for temporary, package in staged:
            publication.publish(temporary, out / package.target)
    except BaseException:
        publication.roll_back()
        raise
    paths.append_run_log(
        key_file.parent,
        {
            "command": "deanonymize",
            "chain": key.chain_id,
            "run": key.run_number,
            "key": key_file.name,
            "packages": [
                {
                    "package": package.source,
                    "restored_sha256": hashlib.sha256(package.payload).hexdigest(),
                    "members": sorted(package.members),
                    "masked_markers": sum(package.masked_markers.values()),
                }
                for package in restored
            ],
        },
    )
    log.info("restored %d export package(s) into the output folder", len(restored))
    return RestoreResult(out_dir=out, key_path=key_file, packages=restored, test_only_key=key.test_only)


__all__ = ["RestoreResult", "RestoredPackage", "check_restore_paths", "restore_packages"]
