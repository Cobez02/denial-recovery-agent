"""`denial-agent anonymize` and `deanonymize` (docs/SPEC.md section 16).

Every test builds its own raw batch with ``synthetic.generate.generate(...)`` in a temporary folder
(no committed fixture folder is read), pseudonymizes it and checks the promises of the commands: no
original identifier survives in any output byte (also for spelling variants, other patients named
in a note, ordinary-word names, provider names, embedded PLB ids and files that are not 835s); the
pseudonymized batch imports with zero unmatched remittance claims and yields the same work items,
categories and actions as the original (two databases, rules with the stub model); the leak sweep
stops a run that missed something; the key maps every pseudonym back and chains from month to
month; export packages restore to the raw database's, with the newest key only; the marks of a
pseudonymized batch come from the marker column the importer records, not from the file name.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import unicodedata
import warnings
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.anonymize import AnonymizeOptions, PseudonymKey, anonymize_batch, restore_claim_row, restore_packages
from app.anonymize import paths as anonymize_paths
from app.anonymize import restore as restore_module
from app.anonymize import text as text_module
from app.anonymize.matching import date_matches, date_values
from app.anonymize.pseudonyms import Originals, Pseudonymizer
from app.anonymize.text import TextIndex, shift_text_dates
from app.auth.service import create_user
from app.cli import main
from app.config import PROJECT_ROOT, get_settings, reset_settings_cache
from app.db import configure_engine, dispose_engine, init_db, make_engine, session_factory
from app.drafting.pdf import letter_html
from app.ingest.claims_import import (
    NOTE_COLUMNS,
    PAYER_DOCUMENT_COLUMNS,
    import_claims_csv,
    import_notes_csv,
    import_payer_documents_csv,
)
from app.ingest.parse_835 import parse_835
from app.ingest.remit_import import import_835
from app.ingest.x12 import read_delimiters, tokenize
from app.models import Claim, Clinic, Outcome, RemittanceClaim, WorkItem
from app.pipeline import process_pending
from app.redact.registry import date_formats
from app.review import pseudonymized
from app.review import service as review_service
from app.review.export import build_export_package
from app.review.service import default_submitted_control_number
from app.schemas import CLAIM_EXPORT_COLUMNS
from synthetic.followup import PLAN_VALUES, generate_followup_files
from synthetic.generate import generate
from synthetic.profile import resolve_profile_path
from tests.conftest import set_test_env

SEED = 20261030
N_CLAIMS = 160
SHIFT_DAYS = 45
CLAIMS_FILE = "claims_export.csv"
NOTES_FILE = "notes.csv"
DOCUMENTS_FILE = "payer_documents.csv"
CLAIMS, NOTES, DOCUMENTS, REMITS = "claims", "notes", "payer_documents", "835"
PLANTED_PHONE = "(503) 555-0187"
PROVIDER_WORDS = frozenset({"rowan", "halvorsen", "imogen", "castellane"})
# Patients of the module's batch renamed to names that are ordinary words, and two towns that are.
COMMON_NAMES = (
    ("Ray", "Low"),
    ("May", "Fall"),
    ("Will", "Long"),
    ("April", "Rose"),
    ("Frank", "White"),
    ("Gene", "Hill"),
    ("Grace", "Young"),
    ("Mark", "Little"),
    ("Bill", "Brown"),
    ("Art", "Hall"),
)
COMMON_TOWNS = ("Normal", "Reading")
CLINICAL = (
    "Low back pain after a fall on ice. X-ray of the lumbar spine reviewed. Pt will continue care and may "
    "benefit from 3 visits per week. Re-exam on May 3, 2024 showed marked improvement in range of motion."
)

NAME, NAME_PART, MEMBER_ID, ACCOUNT, DOB, ADDRESS = (
    "NAME",
    "NAME_PART",
    "MEMBER_ID",
    "ACCOUNT",
    "DOB",
    "ADDRESS",
)
WORD_BOUNDED = frozenset({NAME, NAME_PART, ADDRESS})
# A date of birth spelled 1/16/1960 is not "in" 11/16/1960 (another date): digits bound it.
DIGIT_BOUNDED = frozenset({DOB})
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# --------------------------------------------------------------------------------------
# Helpers (independent of app.anonymize, so the tests do not trust the code under test)
# --------------------------------------------------------------------------------------


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, columns: tuple[str, ...] | list[str], rows: list[dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _files(batch_dir: Path) -> dict[str, list[Path]]:
    """A batch folder's files by kind (raw names or the pseudonymized ones), each list sorted."""
    found: dict[str, list[Path]] = {CLAIMS: [], NOTES: [], DOCUMENTS: [], REMITS: []}
    for path in sorted(p for p in batch_dir.iterdir() if p.is_file()):
        name = path.name
        if name.endswith(".835"):
            found[REMITS].append(path)
        elif name == CLAIMS_FILE or "_claims_" in name:
            found[CLAIMS].append(path)
        elif name == NOTES_FILE or "_notes_" in name:
            found[NOTES].append(path)
        elif name == DOCUMENTS_FILE or "_payer_documents_" in name:
            found[DOCUMENTS].append(path)
    return found


def _one(batch_dir: Path, kind: str) -> Path:
    [path] = _files(batch_dir)[kind]
    return path


def _output_for(raw_dir: Path, out_dir: Path, remit: Path) -> Path:
    """The output 835 of one raw 835 (outputs are numbered in input order)."""
    return _files(out_dir)[REMITS][_files(raw_dir)[REMITS].index(remit)]


def _dob_spellings(dob: date) -> list[str]:
    return [
        dob.isoformat(),
        f"{dob.month:02d}/{dob.day:02d}/{dob.year}",
        f"{dob.month}/{dob.day}/{dob.year}",
        f"{dob.month:02d}{dob.day:02d}{dob.year}",
        f"{dob.year}{dob.month:02d}{dob.day:02d}",
        f"{MONTHS[dob.month - 1]} {dob.day}, {dob.year}",
    ]


def _original_identifiers(raw_dir: Path) -> list[tuple[str, str]]:
    """Every (kind, value) a patient of the raw batch can be recognised by, in either spelling."""
    found: dict[tuple[str, str], None] = {}

    def add(kind: str, value: str | None) -> None:
        text = " ".join((value or "").split())
        if len(text) >= 3 or (kind in (NAME, NAME_PART) and len(text) >= 2):
            found.setdefault((kind, text), None)

    for row in _rows(raw_dir / CLAIMS_FILE):
        first, last = row["patient_first_name"], row["patient_last_name"]
        add(NAME, f"{first} {last}")
        add(NAME, f"{last}, {first}")
        add(NAME_PART, first)
        add(NAME_PART, last)
        add(MEMBER_ID, row["member_id"])
        add(ACCOUNT, row["patient_account_number"])
        for spelling in _dob_spellings(date.fromisoformat(row["patient_dob"])):
            add(DOB, spelling)
        add(ADDRESS, row["patient_address_line1"])
        add(ADDRESS, row["patient_address_line1"].split(",")[0])
    for path in _files(raw_dir)[REMITS]:
        for transaction in parse_835(path.read_text(encoding="utf-8")):
            for claim in transaction.claims:
                add(NAME, f"{claim.patient_first_name} {claim.patient_last_name}")
                add(NAME_PART, claim.patient_first_name)
                add(NAME_PART, claim.patient_last_name)
                add(MEMBER_ID, claim.member_id)
    return list(found)


def _background(raw_dir: Path) -> set[str]:
    """Words a single name token may share without proving anything: the specialty profile's own
    text, words the raw batch writes in lower case, and the ordinary-word names given on purpose
    (full names are still checked for those)."""
    words: set[str] = set()
    manifest = raw_dir / "manifest.json"
    if manifest.exists():
        profile = json.loads(manifest.read_text(encoding="utf-8"))["profile"]
        words |= {
            w.lower()
            for w in re.findall(r"[A-Za-z]+", resolve_profile_path(profile).read_text(encoding="utf-8"))
        }
    for kind in (NOTES, DOCUMENTS):
        for path in _files(raw_dir)[kind]:
            words |= set(re.findall(r"\b[a-z]+\b", path.read_text(encoding="utf-8")))
    words |= {w.lower() for pair in COMMON_NAMES for w in pair} | {t.lower() for t in COMMON_TOWNS}
    return words


def _preview(value: str) -> str:
    return value[:2] + "*" * max(len(value) - 2, 0)


def _leaks(text: str, identifiers: list[tuple[str, str]], background: set[str]) -> list[tuple[str, str]]:
    """(kind, masked preview) of every identifier found in ``text``; names and addresses whole-word."""
    lowered = " ".join(text.split()).lower()
    leaks: list[tuple[str, str]] = []
    for kind, value in identifiers:
        needle = value.lower()
        if needle not in lowered or (kind == NAME_PART and needle in background):
            continue
        if kind in WORD_BOUNDED:
            pattern = r"(?<![a-z0-9])" + r"\s+".join(map(re.escape, needle.split())) + r"(?![a-z0-9])"
            if re.search(pattern, lowered) is None:
                continue
        if kind in DIGIT_BOUNDED and re.search(r"(?<!\d)" + re.escape(needle) + r"(?!\d)", lowered) is None:
            continue
        leaks.append((kind, _preview(value)))
    return leaks


def _shape(value: str) -> str:
    return "".join(
        "9" if ch.isdigit() else "A" if ch.isupper() else "a" if ch.islower() else ch for ch in value
    )


def _key(key_path: Path) -> PseudonymKey:
    return PseudonymKey(json.loads(key_path.read_text(encoding="utf-8")))


def _anonymize(raw_dir: Path, base: Path, name: str, **options: Any) -> tuple[Path, Path, Any]:
    """Pseudonymize into base/name with the key in base/keys (never beside the output folder)."""
    out, key = base / name, base / "keys" / f"{name}.key.json"
    if options.get("seed") is not None or options.get("shift_dates_days"):
        options.setdefault("test_only", True)
    result = anonymize_batch(raw_dir, out, key, AnonymizeOptions(**options))
    assert result.written, result.summary()
    return out, key, result


def _replace_words(folder: Path, mapping: dict[str, str]) -> None:
    """Whole-word, case-keeping replacement in every CSV and 835 of a raw batch."""
    if not mapping:
        return
    lowered = {old.lower(): new for old, new in mapping.items()}
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, mapping)) + r")\b", re.IGNORECASE)

    def swap(match: re.Match[str]) -> str:
        old, new = match.group(0), lowered[match.group(0).lower()]
        return new.upper() if old.isupper() else new

    for path in folder.iterdir():
        if path.suffix in (".csv", ".835"):
            path.write_bytes(pattern.sub(swap, path.read_text(encoding="utf-8")).encode("utf-8"))


def _rename_patients(raw_dir: Path, names: tuple[tuple[str, str], ...]) -> list[str]:
    """Give the first patients of a raw batch these names, in every file; returns their accounts."""
    rows = _rows(raw_dir / CLAIMS_FILE)
    taken = set(PROVIDER_WORDS)
    mapping: dict[str, str] = {}
    accounts: list[str] = []
    for row in rows:
        if len(accounts) == len(names):
            break
        account = row["patient_account_number"]
        old = (row["patient_first_name"], row["patient_last_name"])
        if account in accounts or {w.lower() for w in old} & taken:
            continue
        first, last = names[len(accounts)]
        mapping.setdefault(old[0], first)
        mapping.setdefault(old[1], last)
        taken |= {w.lower() for w in old}
        accounts.append(account)
    _replace_words(raw_dir, mapping)
    return accounts


@contextmanager
def _database(db_dir: Path) -> Iterator[tuple[Session, Clinic]]:
    """A database of its own with one clinic (kept when the folder is opened again); closed on exit."""
    set_test_env(db_dir)
    reset_settings_cache()
    dispose_engine()
    settings = get_settings()
    engine = make_engine(settings.database_url)
    configure_engine(engine)
    init_db(engine)
    session = session_factory()()
    try:
        clinic = session.execute(select(Clinic).order_by(Clinic.id)).scalars().first()
        if clinic is None:  # the synthetic profile's clinic: follow-up 835s name it as the payee
            clinic = Clinic(
                name="Anonymize Test Clinic", npi="1975310867", tax_id="83-5559012", pm_system="TestPM"
            )
            session.add(clinic)
            session.commit()
        settings.clinic_id = clinic.id
        yield session, clinic
    finally:
        session.rollback()
        session.close()
        dispose_engine()
        engine.dispose()
        reset_settings_cache()


def _import(session: Session, clinic: Clinic, batch_dir: Path) -> dict[str, dict[str, Any]]:
    """Import whatever the folder holds: claims, notes, payer documents, then every 835 (keyed by
    kind and, for 835s, by their order, so a raw and a pseudonymized batch compare)."""
    imports: dict[str, dict[str, Any]] = {}
    files = _files(batch_dir)
    for kind, importer in (
        (CLAIMS, import_claims_csv),
        (NOTES, import_notes_csv),
        (DOCUMENTS, import_payer_documents_csv),
    ):
        for path in files[kind]:
            batch = importer(session, path, clinic_id=clinic.id, created_by="tests")
            session.commit()
            imports[kind] = {"status": str(batch.status), **(batch.counts or {})}
    for number, path in enumerate(files[REMITS], start=1):
        batch = import_835(session, path, file_name=path.name, created_by="tests")
        session.commit()
        imports[f"835#{number}"] = {"status": str(batch.status), **(batch.counts or {})}
    return imports


def _work_items(session: Session) -> dict[str, dict[str, Any]]:
    """What the pipeline decided per claim. The confidence and the priority score are read as floats,
    as the app's export and API read them: both are Numeric columns the pipeline sets with a float, the
    session keeps that float (expire_on_commit=False) while an item loaded again from the database holds
    a Decimal, and which one a run sees depends on the session's memory, not on the decision."""
    items: dict[str, dict[str, Any]] = {}
    stmt = select(WorkItem, Claim.claim_control_number).join(Claim, WorkItem.claim_id == Claim.id)
    for work_item, ccn in session.execute(stmt):
        proposal = work_item.current_proposal
        assert proposal is not None
        items[ccn] = {
            "category": str(work_item.category),
            "action": str(proposal.action_type),
            "coding_change": proposal.coding_change,
            "confidence": float(work_item.category_confidence),
            "source": str(work_item.classification_source),
            "dollar_value": work_item.dollar_value,
            "flags": sorted(work_item.flags or []),
            "deadline": work_item.deadline,
            "deadline_kind": work_item.deadline_kind,
            "priority_score": float(work_item.priority_score),
            "status": str(work_item.status),
        }
    return items


def _unmatched(session: Session) -> int:
    stmt = select(func.count()).select_from(RemittanceClaim).where(RemittanceClaim.claim_id.is_(None))
    return int(session.execute(stmt).scalar_one())


def _process(batch_dir: Path, db_dir: Path, today: date) -> dict[str, Any]:
    """Import the batch into a database of its own, run the pipeline, and return what it made."""
    with _database(db_dir) as (session, clinic):
        imports = _import(session, clinic, batch_dir)
        process_pending(session, today=today)
        session.commit()
        return {"imports": imports, "items": _work_items(session), "unmatched": _unmatched(session)}


def _work_item_for(session: Session, ccn: str) -> WorkItem:
    stmt = (
        select(WorkItem).join(Claim, WorkItem.claim_id == Claim.id).where(Claim.claim_control_number == ccn)
    )
    work_item = session.execute(stmt.order_by(WorkItem.id)).scalars().first()
    assert work_item is not None, "a chosen claim has no work item"
    return work_item


def _submit(session: Session, ccns: list[str], day: date) -> list[WorkItem]:
    """Mark the work items of these claims submitted on ``day`` under the app's default control
    number (a corrected claim's ``-R1`` number, else the claim's own): what "Mark submitted" with
    the defaults records."""
    items: list[WorkItem] = []
    for ccn in ccns:
        work_item = _work_item_for(session, ccn)
        work_item.status = "submitted"
        work_item.submitted_at = datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)
        work_item.submitted_claim_control_number = default_submitted_control_number(work_item)
        items.append(work_item)
    session.commit()
    return items


def _outcomes(session: Session) -> dict[str, tuple[str, str, str]]:
    """{claim number: (outcome, paid amount, work item status)} for every recorded outcome."""
    stmt = (
        select(Outcome, WorkItem, Claim.claim_control_number)
        .join(WorkItem, Outcome.work_item_id == WorkItem.id)
        .join(Claim, WorkItem.claim_id == Claim.id)
    )
    return {
        ccn: (str(outcome.result), str(outcome.paid_amount), str(work_item.status))
        for outcome, work_item, ccn in session.execute(stmt)
    }


def _small_batch(base: Path, seed: int, n_claims: int = 10, name: str = "raw") -> Path:
    raw = base / name
    generate(seed=seed, n_claims=n_claims, out_dir=raw)
    return raw


def _patients(raw: Path) -> list[dict[str, str]]:
    """One claim-export row per patient, in the order the export first names them."""
    seen: dict[str, dict[str, str]] = {}
    for row in _rows(raw / CLAIMS_FILE):
        seen.setdefault(row["patient_account_number"], row)
    return list(seen.values())


def _add_notes(raw: Path, notes: list[tuple[str, str]], author: str = "Clinic Staff") -> list[int]:
    """Append (claim number, text) notes; returns their row numbers in the notes file."""
    rows = _rows(raw / NOTES_FILE)
    start = len(rows)
    rows.extend(
        {"claim_control_number": ccn, "note_date": "2026-06-02", "author": author, "text": text}
        for ccn, text in notes
    )
    _write_rows(raw / NOTES_FILE, NOTE_COLUMNS, rows)
    return list(range(start, start + len(notes)))


def _subject_pseudonyms(key: PseudonymKey, account: str) -> dict[str, Any]:
    """The pseudonym first and last name, member id and date of birth the key gives a patient."""
    subject = key.subjects[account]
    spelling = subject["spellings"][0]
    dob = next(iter(subject["dob"].values()))["pseudonym"] if subject["dob"] else None
    return {
        "first": spelling["pseudo_first"],
        "last": spelling["pseudo_last"],
        "ids": dict(subject["ids"]),
        "dob": dob,
    }


def _fix_se(text: str) -> str:
    """Recount SE01 of every transaction set of an 835 written with ``~\\n`` terminators."""
    segments = text.split("~\n")
    start = 0
    for index, segment in enumerate(segments):
        if segment.startswith("ST*"):
            start = index
        elif segment.startswith("SE*"):
            parts = segment.split("*")
            parts[1] = str(index - start + 1)
            segments[index] = "*".join(parts)
    return "~\n".join(segments)


# --------------------------------------------------------------------------------------
# One raw batch, two pseudonymized copies and three processed databases for the module
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def raw_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """160 claims; ten patients carry names that are ordinary words and two live in towns that are."""
    out = tmp_path_factory.mktemp("raw")
    generate(seed=SEED, n_claims=N_CLAIMS, out_dir=out)
    accounts = _rename_patients(out, COMMON_NAMES)
    rows = _rows(out / CLAIMS_FILE)
    for row in rows:
        if row["patient_account_number"] in accounts[: len(COMMON_TOWNS)]:
            row["patient_city"] = COMMON_TOWNS[accounts.index(row["patient_account_number"])]
    _write_rows(out / CLAIMS_FILE, CLAIM_EXPORT_COLUMNS, rows)
    return out


@pytest.fixture(scope="module")
def as_of(raw_dir: Path) -> date:
    return date.fromisoformat(json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))["as_of"])


@pytest.fixture(scope="module")
def runs(raw_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path, Any]]:
    """`plain` uses the defaults; `flagged` also replaces claim numbers and (a test-only option)
    shifts every date."""
    base = tmp_path_factory.mktemp("anonymized")
    return {
        "plain": _anonymize(raw_dir, base, "plain", seed=11),
        "flagged": _anonymize(
            raw_dir, base, "flagged", seed=12, replace_claim_numbers=True, shift_dates_days=SHIFT_DAYS
        ),
    }


@pytest.fixture(scope="module")
def processed(
    raw_dir: Path,
    runs: dict[str, tuple[Path, Path, Any]],
    as_of: date,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, dict[str, Any]]]:
    base = tmp_path_factory.mktemp("databases")
    yield {
        "original": _process(raw_dir, base / "original", as_of),
        "plain": _process(runs["plain"][0], base / "plain", as_of),
        # every date moved by SHIFT_DAYS, so "today" moves with them
        "flagged": _process(runs["flagged"][0], base / "flagged", as_of + timedelta(days=SHIFT_DAYS)),
    }


# --------------------------------------------------------------------------------------
# The promises
# --------------------------------------------------------------------------------------


def test_no_original_identifier_survives_in_any_output_byte(raw_dir, runs):
    identifiers = _original_identifiers(raw_dir)
    background = _background(raw_dir)
    assert {kind for kind, _ in identifiers} == {NAME, NAME_PART, MEMBER_ID, ACCOUNT, DOB, ADDRESS}
    original_rows = _rows(raw_dir / CLAIMS_FILE)
    cities = {row["patient_city"].lower() for row in original_rows}
    zips = {row["patient_zip"] for row in original_rows}
    assert set(COMMON_TOWNS) <= {row["patient_city"] for row in original_rows}
    expected = {kind: len(found) for kind, found in _files(raw_dir).items()}
    for label, (out_dir, _key_path, result) in runs.items():
        assert {kind: len(found) for kind, found in _files(out_dir).items()} == expected
        assert all(path.name.startswith("pseudonymized_run1_") for path in out_dir.iterdir())
        for path in sorted(out_dir.iterdir()):
            leaks = _leaks(path.read_bytes().decode("utf-8"), identifiers, background)
            assert not leaks, f"{label}/{path.name}: {leaks}"
        for row in _rows(_one(out_dir, CLAIMS)):
            assert row["patient_city"].lower() not in cities and row["patient_zip"] not in zips
        assert result.counts["leaks"] == 0 and result.summary()["status"] == "ok"


MODEL_FIELDS = ("confidence", "source")


def _comparable(items: dict[str, dict[str, Any]], ordinary: set[str]) -> dict[str, dict[str, Any]]:
    """Work items with what the model decides left out for the claims of patients whose names are
    ordinary words: in the raw database the app's own redaction guard (app/llm/guard.py) blocks every
    model call for such a patient (a prompt template holds the word "may"), while their pseudonyms
    never are words, so the pseudonymized database consults the model. Everything else must agree."""
    out: dict[str, dict[str, Any]] = {}
    for ccn, item in items.items():
        if ccn in ordinary:
            item = {k: v for k, v in item.items() if k not in MODEL_FIELDS}
            item["flags"] = [flag for flag in item["flags"] if not flag.startswith("model_")]
        out[ccn] = item
    return out


def _ordinary_word_claims(raw_dir: Path) -> set[str]:
    words = {w for pair in COMMON_NAMES for w in pair}
    return {
        row["claim_control_number"]
        for row in _rows(raw_dir / CLAIMS_FILE)
        if {row["patient_first_name"], row["patient_last_name"]} & words
    }


def test_the_pseudonymized_batch_processes_like_the_original(processed, runs, raw_dir):
    original = processed["original"]
    ordinary = _ordinary_word_claims(raw_dir)
    assert ordinary
    assert len(original["items"]) >= 50, "the synthetic batch should carry plenty of denials"
    assert len({item["category"] for item in original["items"].values()}) >= 6
    for label, run in processed.items():
        assert run["unmatched"] == 0, label
        for name, counts in run["imports"].items():
            assert counts["status"] == "done", (label, name)
            if name.startswith("835"):
                assert counts["unmatched"] == 0 and counts["matched"] == counts["claims"], (label, name)
    assert processed["plain"]["imports"] == original["imports"]
    assert processed["flagged"]["imports"] == original["imports"]
    assert _comparable(processed["plain"]["items"], ordinary) == _comparable(original["items"], ordinary)
    others = {ccn: item for ccn, item in original["items"].items() if ccn not in ordinary}
    assert others and all(processed["plain"]["items"][ccn] == item for ccn, item in others.items())

    claim_numbers = _key(runs["flagged"][1]).document["values"]["claim_number"]
    shift = timedelta(days=SHIFT_DAYS)
    expected = {
        claim_numbers[ccn]: {**item, "deadline": item["deadline"] and item["deadline"] + shift}
        for ccn, item in original["items"].items()
    }
    mapped = {claim_numbers[ccn] for ccn in ordinary}
    assert _comparable(processed["flagged"]["items"], mapped) == _comparable(expected, mapped)


def test_every_835_parses_to_the_same_transactions_apart_from_the_replaced_values(raw_dir, runs):
    """Parse original and output with app.ingest.parse_835; put the key's originals back into the
    replaced fields (and move the dates back): the result is the original, field for field."""
    out_dir, key_path, _result = runs["flagged"]
    key = _key(key_path)
    shift = timedelta(days=SHIFT_DAYS)
    accounts = {
        row["claim_control_number"]: row["patient_account_number"] for row in _rows(raw_dir / CLAIMS_FILE)
    }

    def back(value: str | None) -> str | None:
        return None if value is None else (date.fromisoformat(value) - shift).isoformat()

    files = _files(raw_dir)[REMITS]
    assert files
    for original_path in files:
        original_text = original_path.read_text(encoding="utf-8")
        output_text = _output_for(raw_dir, out_dir, original_path).read_text(encoding="utf-8")
        assert read_delimiters(output_text) == read_delimiters(original_text)
        before, after = parse_835(original_text), parse_835(output_text)
        assert len(before) == len(after)
        for old, new in zip(before, after, strict=True):
            restored = new.model_dump(mode="json")
            restored["interchange_date"] = back(restored["interchange_date"])
            for name in ("payment_date", "production_date"):
                restored["payment"][name] = back(restored["payment"][name])
            for claim in restored["claims"]:
                claim["patient_control_number"] = key.original(
                    "claim_number", claim["patient_control_number"]
                )
                claim["payer_claim_control_number"] = key.original(
                    "payer_claim_number", claim["payer_claim_control_number"]
                )
                claim["member_id"] = key.original("patient_id", claim["member_id"])
                subject = key.subjects[accounts[claim["patient_control_number"]]]
                claim["patient_last_name"] = key.original_name(subject, "family", claim["patient_last_name"])
                claim["patient_first_name"] = key.original_name(subject, "given", claim["patient_first_name"])
                for name in ("claim_received_date", "statement_from", "statement_to"):
                    claim[name] = back(claim[name])
                for service in claim["services"]:
                    service["line_item_control_number"] = key.original(
                        "claim_number", service["line_item_control_number"]
                    )
                    service["service_date"] = back(service["service_date"])
            assert restored == old.model_dump(mode="json")
            assert new.claims and all(
                a.patient_last_name != b.patient_last_name and a.member_id != b.member_id
                for a, b in zip(old.claims, new.claims, strict=True)
            )
        # Segment counts and control numbers still agree inside the rewritten envelope.
        segments = tokenize(output_text)
        ids = [seg.id for seg in segments]
        st, se = ids.index("ST"), ids.index("SE")
        assert int(segments[se].element(1)) == se - st + 1
        assert segments[st].element(2) == segments[se].element(2)
        assert segments[ids.index("GS")].element(6) == segments[ids.index("GE")].element(2)
        assert segments[ids.index("ISA")].element(13) == segments[ids.index("IEA")].element(2)


def test_the_key_maps_every_pseudonym_back(raw_dir, runs):
    original = _rows(raw_dir / CLAIMS_FILE)
    for label, (out_dir, key_path, _result) in runs.items():
        key = _key(key_path)
        anonymized = _rows(_one(out_dir, CLAIMS))
        assert len(anonymized) == len(original)
        assert [restore_claim_row(row, key) for row in anonymized] == original, label
        document = key.document
        assert document["options"]["shift_dates_days"] == (SHIFT_DAYS if label == "flagged" else 0)
        assert document["options"]["replace_claim_numbers"] is (label == "flagged")
        assert document["options"]["test_only"] is True
        assert len(document["runs"]) == 1 and document["chain"]["run"] == 1
        assert document["clinic"]["billing_npis"] == sorted({row["billing_provider_npi"] for row in original})
        assert set(document["subjects"]) == {row["patient_account_number"] for row in original}
        # every pseudonym name is drawn outside the batch's provider and organization names
        tokens = {
            token.lower()
            for subject in document["subjects"].values()
            for role in ("given", "family")
            for token in subject["tokens"][role].values()
        }
        assert not tokens & PROVIDER_WORDS


def test_dates_of_birth_keep_their_year_and_ids_keep_their_shape(raw_dir, runs):
    original = _rows(raw_dir / CLAIMS_FILE)
    for label, (out_dir, _key_path, _result) in runs.items():
        shift = timedelta(days=SHIFT_DAYS if label == "flagged" else 0)
        for before, after in zip(original, _rows(_one(out_dir, CLAIMS)), strict=True):
            dob, pseudo = (
                date.fromisoformat(before["patient_dob"]),
                date.fromisoformat(after["patient_dob"]) - shift,
            )
            assert pseudo.year == dob.year and pseudo != dob
            for column in ("patient_account_number", "member_id"):
                assert after[column] != before[column] and _shape(after[column]) == _shape(before[column])
            for column in (
                "procedure_code",
                "modifiers",
                "diagnosis_codes",
                "charge_amount",
                "units",
                "payer_id",
            ):
                assert after[column] == before[column]
            if label == "plain":
                assert after["service_date"] == before["service_date"]
                assert after["claim_control_number"] == before["claim_control_number"]
            else:
                assert date.fromisoformat(after["service_date"]) - shift == date.fromisoformat(
                    before["service_date"]
                )
                assert after["claim_control_number"] != before["claim_control_number"]
                assert after["line_item_control_number"].startswith(after["claim_control_number"] + "-")


def test_the_same_seed_gives_the_same_bytes(raw_dir, tmp_path):
    options = {"replace_claim_numbers": True, "shift_dates_days": SHIFT_DAYS}
    first = _anonymize(raw_dir, tmp_path, "first", seed=5, **options)
    second = _anonymize(raw_dir, tmp_path, "second", seed=5, **options)
    other = _anonymize(raw_dir, tmp_path, "other", seed=6, **options)

    def contents(run: tuple[Path, Path, Any]) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(run[0].iterdir())}

    assert contents(first) == contents(second)
    assert first[1].read_bytes() == second[1].read_bytes()
    claims_name = _one(first[0], CLAIMS).name
    assert contents(other)[claims_name] != contents(first)[claims_name]


def test_a_phone_number_planted_in_a_note_stops_the_run(raw_dir, tmp_path, capsys):
    source = tmp_path / "raw"
    shutil.copytree(raw_dir, source)
    notes = _rows(source / NOTES_FILE)
    target = 3
    notes[target]["text"] += f" Patient asked for a call back at {PLANTED_PHONE}."
    _write_rows(source / NOTES_FILE, NOTE_COLUMNS, notes)
    out, key = tmp_path / "out", tmp_path / "keys" / "key.json"
    args = ["anonymize", str(source), "--out", str(out), "--key-out", str(key)]

    assert main(args) == 1
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["status"] == "residual" and summary["written"] is False
    assert summary["residual"] == [
        {"file": "pseudonymized_run1_notes_1.csv", "row": target + 2, "field": "text", "kind": "PHONE"}
    ]
    for value in (PLANTED_PHONE, "555-0187", "5550187"):
        assert value not in captured.out + captured.err
    assert "residual identifier" in captured.err
    assert not out.exists() and not key.exists()

    assert main([*args, "--allow-residual"]) == 0
    capsys.readouterr()
    masked = _rows(_one(out, NOTES))[target]["text"]
    assert "[[MASKED_PHONE_1]]" in masked and "555-0187" not in masked
    assert key.exists()


def test_paths_that_could_leak_are_refused(raw_dir, tmp_path, capsys, monkeypatch):
    inside = PROJECT_ROOT / "data" / "anonymize-refusal-test"
    assert not inside.exists()
    keys = tmp_path / "keys"
    keys.mkdir()
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "file.txt").write_text("x", encoding="utf-8")
    (keys / "existing.json").write_text("{}", encoding="utf-8")
    synced = tmp_path / "OneDrive"
    synced.mkdir()
    monkeypatch.setenv("OneDrive", str(synced))
    cases = [
        (raw_dir, inside, keys / "k1.json", "inside the repository"),
        (raw_dir, tmp_path / "o2", inside / "key.json", "inside the repository"),
        (raw_dir, tmp_path / "o3", tmp_path / "o3" / "key.json", "must not be inside --out"),
        (raw_dir, tmp_path / "busy", keys / "k4.json", "not an empty folder"),
        (raw_dir, tmp_path / "o5", keys / "existing.json", "already exists"),
        (raw_dir, raw_dir, keys / "k6.json", "must not be the input folder"),
        (raw_dir, raw_dir / "nested", keys / "k7.json", "inside one another"),
        (raw_dir, tmp_path / "o8", tmp_path / "k8.json", "must not sit beside --out"),
        (raw_dir, synced / "o9", keys / "k9.json", "OneDrive"),
        (raw_dir, tmp_path / "o10", synced / "k10.json", "OneDrive"),
        (PROJECT_ROOT / "synthetic", tmp_path / "o11", keys / "k11.json", "inside the repository"),
    ]
    if os.name == "nt":  # the other Windows spellings of a path inside the repository
        text = str(inside)
        cases.append((raw_dir, Path("\\\\?\\" + text), keys / "k12.json", "inside the repository"))
        cases.append(
            (
                raw_dir,
                Path(f"\\\\localhost\\{text[0]}$" + text[2:]),
                keys / "k13.json",
                "inside the repository",
            )
        )
    for source, out, key, message in cases:
        assert main(["anonymize", str(source), "--out", str(out), "--key-out", str(key)]) == 1
        captured = capsys.readouterr()
        assert message in captured.err and captured.out == "", (out, captured.err)
    assert not inside.exists() and not (raw_dir / "nested").exists()
    for name in ("o2", "o3", "o5", "o8", "o10", "o11", "k8.json"):
        assert not (tmp_path / name).exists()
    assert sorted(p.name for p in keys.iterdir()) == ["existing.json"]
    assert (keys / "existing.json").read_text(encoding="utf-8") == "{}"
    assert sorted(p.name for p in synced.iterdir()) == []


def test_stdout_logs_and_the_run_log_carry_no_identifier(raw_dir, tmp_path, capsys, caplog):
    identifiers = _original_identifiers(raw_dir)
    background = _background(raw_dir)
    keys = tmp_path / "keys"
    args = ["anonymize", str(raw_dir), "--out", str(tmp_path / "out"), "--key-out", str(keys / "key.json")]
    with caplog.at_level(logging.DEBUG, logger="app"):
        assert main([*args, "--replace-claim-numbers"]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["status"] == "ok" and summary["patients"] > 0 and summary["options"]["seed"] == "random"
    assert summary["options"]["test_only"] is False and summary["chain"]["run"] == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert not _leaks(captured.out + captured.err + logged, identifiers, background)
    run_log = (keys / anonymize_paths.RUN_LOG).read_text(encoding="utf-8")
    [entry] = [json.loads(line) for line in run_log.splitlines()]
    assert entry["command"] == "anonymize" and entry["run"] == 1 and entry["key"] == "key.json"
    assert set(entry["outputs"]) == {p.name for p in (tmp_path / "out").iterdir()}
    assert not _leaks(run_log, identifiers, background)


def test_seed_and_shift_dates_are_test_only(raw_dir, tmp_path, capsys):
    for index, extra in enumerate((["--seed", "3"], ["--shift-dates", "10"])):
        out, key = tmp_path / f"out{index}", tmp_path / "keys" / f"key{index}.json"
        assert main(["anonymize", str(raw_dir), "--out", str(out), "--key-out", str(key), *extra]) == 1
        assert "--test-only" in capsys.readouterr().err
        assert not out.exists() and not key.exists()


# --------------------------------------------------------------------------------------
# Parts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("day", [date(2026, 1, 5), date(1975, 11, 23), date(2000, 2, 29), date(1961, 7, 14)])
def test_free_text_reads_every_date_spelling_of_the_redaction_registry(day):
    for spelling in date_formats(day):
        [(match, value)] = list(date_matches(f"Born {spelling}."))
        assert day in date_values(match, value), spelling


def test_free_text_keeps_case_and_leaves_codes_alone():
    originals = Originals()
    for value in ("Hollis", "Yelverton"):
        originals.add_name(value)
    originals.add_patient_id("8941")
    originals.dobs.add(date(1974, 5, 5))
    pseudo = Pseudonymizer(originals, seed=1)
    subject = pseudo.subject("8941")
    subject.seed("Hollis", "Yelverton")
    first, last, _ = subject.person("Hollis", "Yelverton")
    account = pseudo.patient_id("8941", subject)
    dob = pseudo.dob(subject, date(1974, 5, 5))
    index = TextIndex(codes=frozenset({"98941"}))
    index.add_subject(subject)

    text = "YELVERTON, HOLLIS (acct 8941) had 98941 on 2026-06-01; hollis said DOB: 05/05/1974 and 5/5/74."
    result = index.process(text, owner="8941")
    assert result.residual == []
    assert f"{last.upper()}, {first.upper()}" in result.text and first.lower() in result.text
    assert f"acct {account})" in result.text and "98941" in result.text and "2026-06-01" in result.text
    assert f"DOB: {dob:%m/%d/%Y}" in result.text
    assert f"{dob.month}/{dob.day}/{dob.year % 100:02d}." in result.text
    assert not re.search(r"hollis|yelverton|05/05/1974|\b8941\b", result.text, re.IGNORECASE)

    leftover = index.process(
        "Insured DOB: 03/14/1950, e-mail pat@example.test, SSN 123-45-6789", owner="8941"
    )
    assert sorted(set(leftover.residual)) == ["DOB", "EMAIL", "SSN"]
    masked = index.process("Insured DOB: 03/14/1950, call 5035550187", owner="8941", allow_residual=True)
    assert masked.masked >= 2 and "03/14/1950" not in masked.text and "5035550187" not in masked.text


def test_labelled_ids_the_batch_does_not_know_are_caught_and_masked_whole():
    index = TextIndex()
    texts = [
        "SSN: 123 45 6789 on intake form.",
        "SSN 123456789 on intake form.",
        "Social Security Number: 123456789 on intake form.",
        "Member ID: 123 456 789 per card.",
        "Member ID: XEH 123456789 per card.",
        "Medicare MBI: 1EG4-TE5-MK73 per card.",
        "MRN: 445566 per chart.",
        "Chart #445566 reviewed.",
        "Acct #: 100234 statement sent.",
        "Policy #: XYZ123456789 on file.",
        "Subscriber ID: XYZ123456789 on file.",
        "ID#: W123456789 on file.",
        "HICN 123456789A on file.",
        "Medicaid ID 22345678901 on file.",
        "DOB: September 21st, 1994 confirmed.",
    ]
    for text in texts:
        result = index.process(text, owner=None)
        assert result.residual, text
        masked = index.process(text, owner=None, allow_residual=True)
        assert not re.search(r"\d", re.sub(r"\[\[MASKED_[A-Z_]+_\d+\]\]", "", masked.text)), (
            text,
            masked.text,
        )
    for text in (
        "Pt will continue care for 3 visits.",
        "Member of the family, 2 children.",
        "Thistle Creek, ID 83614.",
    ):
        assert index.process(text, owner=None).residual == [], text


def test_dates_written_in_text_shift_in_their_own_format():
    text = "Seen 2026-06-12, 06/12/2026, 6/1/26, June 12, 2026, Jun. 1st, 2026 and 12 June 2026 (code 99213)."
    shifted, count = shift_text_dates(text, 30)
    assert count == 6
    assert shifted == (
        "Seen 2026-07-12, 07/12/2026, 7/1/26, July 12, 2026, Jul. 1st, 2026 and 12 July 2026 (code 99213)."
    )
    assert shift_text_dates("Due 02/30/2026 or 13/01/2026.", 5) == ("Due 02/30/2026 or 13/01/2026.", 0)
    for written in ("3/4/2026", "2 Jan 2026", "1/2/26", "Sept 3, 2026"):
        forward, _ = shift_text_dates(written, SHIFT_DAYS)
        assert shift_text_dates(forward, -SHIFT_DAYS)[0] == written or written.startswith("Sept")


def _plant_segments(text: str, account: str) -> str:
    """After the first NM1*QC add a subscriber who shares the patient's surname, a corrected name,
    an employer named as the other subscriber, REF*EA (the account number), REF*1W (the member id),
    REF*6P (a policy number equal to the member id) and REF*F8, then fix SE01."""
    segments = text.split("~\n")
    index = next(i for i, seg in enumerate(segments) if seg.startswith("NM1*QC*"))
    qc = segments[index].split("*")
    last, member_id = qc[3], qc[9]
    planted = [
        f"NM1*IL*1*{last}*QUENTIN*R***MI*{member_id}",
        f"NM1*74*1*{last}-THORNE*{qc[4]}****C*ZX{member_id[2:]}",
        "NM1*GB*2*BRAMBLECROFT TOOL WORKS*****FI*987654321",
        f"REF*EA*{account}",
        f"REF*1W*{member_id}",
        f"REF*6P*{member_id}",
        "REF*F8*ORIG4455667788",
    ]
    segments[index + 1 : index + 1] = planted
    return _fix_se("~\n".join(segments))


def test_835_subscriber_corrected_name_and_patient_references(tmp_path):
    raw = _small_batch(tmp_path, SEED + 1, 8)
    rows = _rows(raw / CLAIMS_FILE)
    remit = _files(raw)[REMITS][0]
    text = remit.read_text(encoding="utf-8")
    first_ccn = parse_835(text)[0].claims[0].patient_control_number
    row = next(r for r in rows if r["claim_control_number"] == first_ccn)
    remit.write_text(_plant_segments(text, row["patient_account_number"]), encoding="utf-8")
    out, _key_path, _result = _anonymize(raw, tmp_path, "out", seed=2, replace_claim_numbers=True)

    output_text = _output_for(raw, out, remit).read_text(encoding="utf-8")
    after = _rows(_one(out, CLAIMS))[rows.index(row)]
    all_segments = tokenize(output_text)
    clps = [i for i, seg in enumerate(all_segments) if seg.id == "CLP"]
    first_loop = all_segments[clps[0] : clps[1] if len(clps) > 1 else len(all_segments)]
    segments = {f"{seg.id}*{seg.element(1)}": seg for seg in first_loop if seg.id in ("NM1", "REF")}
    qc, il, corrected = segments["NM1*QC"], segments["NM1*IL"], segments["NM1*74"]
    assert il.element(3) == qc.element(3)  # the surname the subscriber shares stays shared
    assert corrected.element(3).startswith(qc.element(3) + "-") and corrected.element(4) == qc.element(4)
    assert (
        il.element(4) not in ("QUENTIN", qc.element(4)) and len(il.element(5)) == 1 and il.element(5) != "R"
    )
    assert segments["REF*EA"].element(2) == after["patient_account_number"]
    assert segments["REF*1W"].element(2) == after["member_id"] == il.element(9) == qc.element(9)
    assert segments["REF*6P"].element(2) == after["member_id"]
    corrected_id = "ZX" + row["member_id"][2:]
    assert corrected.element(9) != corrected_id and _shape(corrected.element(9)) == _shape(corrected_id)
    payer_reference = segments["REF*F8"].element(2)
    assert payer_reference != "ORIG4455667788" and _shape(payer_reference) == _shape("ORIG4455667788")
    employer = segments["NM1*GB"]
    assert employer.element(3) != "BRAMBLECROFT TOOL WORKS" and len(employer.element(3).split()) == 3
    assert employer.element(9) != "987654321" and employer.element(9).isdigit()
    for value in ("QUENTIN", "THORNE", "BRAMBLECROFT", row["patient_account_number"], row["member_id"]):
        assert value not in output_text
    assert "ORIG4455667788" not in output_text and "987654321" not in output_text


def test_outputs_are_named_by_kind_and_skipped_inputs_by_position(tmp_path, capsys):
    raw = _small_batch(tmp_path, SEED + 4, 6)
    patient = _patients(raw)[0]
    member_id, dob = patient["member_id"], patient["patient_dob"]
    remit = _files(raw)[REMITS][0]
    remit.rename(raw / f"eob_{member_id}.835")
    (raw / f"scan_{dob}.pdf").write_bytes(b"%PDF-1.4\x00\x01binary")
    (raw / f"{patient['patient_last_name']}_letters").mkdir()
    out, key = tmp_path / "out", tmp_path / "keys" / "key.json"
    args = ["anonymize", str(raw), "--out", str(out), "--key-out", str(key), "--test-only", "--seed", "8"]
    assert main(args) == 0
    captured = capsys.readouterr()
    for value in (member_id, dob, patient["patient_last_name"]):
        assert value not in captured.out + captured.err
    summary = json.loads(captured.out)
    reasons = {entry["reason"] for entry in summary["skipped_files"]}
    assert {"not a text file", "a folder: its files are not read"} <= reasons
    assert all(set(entry) == {"position", "reason"} for entry in summary["skipped_files"])
    names = sorted(p.name for p in out.iterdir())
    assert all(name.startswith("pseudonymized_run1_") for name in names) and not any(
        member_id in n for n in names
    )
    assert _key(key).runs[-1]["files"][f"eob_{member_id}.835"] in names


def test_other_delimiters_and_crlf_line_ends_survive(tmp_path):
    raw = _small_batch(tmp_path, SEED + 2, 8)
    remits = _files(raw)[REMITS]
    for remit in remits:
        text = remit.read_text(encoding="utf-8")
        remit.write_bytes(text.replace("*", "|").replace(":", ">").replace("~\n", "~\r\n").encode("utf-8"))
    out, _key_path, _result = _anonymize(
        raw, tmp_path, "out", seed=4, replace_claim_numbers=True, shift_dates_days=-20
    )
    for remit in remits:
        before = remit.read_bytes().decode("utf-8")
        after = _output_for(raw, out, remit).read_bytes().decode("utf-8")
        delimiters = read_delimiters(after)
        assert (delimiters.element, delimiters.sub_element, delimiters.segment) == ("|", ">", "~")
        assert after.count("~\r\n") == before.count("~\r\n") and "*" not in after
        assert len(after.split("~", 1)[0]) == len(before.split("~", 1)[0]) == 105  # the fixed-width ISA
        assert [c.patient_last_name for c in parse_835(after)[0].claims] != [
            c.patient_last_name for c in parse_835(before)[0].claims
        ]


def test_extra_columns_are_dropped_after_their_identities_are_harvested(tmp_path):
    raw = _small_batch(tmp_path, SEED + 3, 8)
    rows = _rows(raw / CLAIMS_FILE)
    first_account = rows[0]["patient_account_number"]
    extra = ["patient_phone", "insured_first_name", "insured_last_name", "secondary_member_id"]
    for row in rows:
        row["patient_phone"] = "503-555-0101"
        mine = row["patient_account_number"] == first_account
        row["insured_first_name"] = "Thaddeus" if mine else ""
        row["insured_last_name"] = "Marchbanks" if mine else ""
        row["secondary_member_id"] = "ZX99887766" if mine else ""
    _write_rows(raw / CLAIMS_FILE, [*CLAIM_EXPORT_COLUMNS, *extra], rows)
    ccn = rows[0]["claim_control_number"]
    notes = _rows(raw / NOTES_FILE)
    notes += [
        {"claim_control_number": "UNKNOWN-1", "note_date": "2026-06-01", "author": "", "text": "x"},
        {"claim_control_number": "", "note_date": "2026-06-01", "author": "", "text": "no claim"},
        {
            "claim_control_number": ccn,
            "note_date": "2026-06-01",
            "author": "Clinic Staff",
            "text": "Insured: Thaddeus Marchbanks, secondary id ZX99887766, on file.",
        },
    ]
    _write_rows(raw / NOTES_FILE, NOTE_COLUMNS, notes)
    out, _key_path, result = _anonymize(raw, tmp_path, "out", seed=9)
    with open(_one(out, CLAIMS), newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == [*CLAIM_EXPORT_COLUMNS, pseudonymized.IMPORT_COLUMN]
    output = "".join(path.read_text(encoding="utf-8") for path in out.iterdir())
    for value in ("503-555-0101", "Marchbanks", "ZX99887766"):
        assert value not in output
    assert len(_rows(_one(out, NOTES))) == len(notes) - 2
    assert result.counts["columns_dropped"] == 4
    assert result.counts["rows_dropped_unknown_claim"] == 1 and result.counts["rows_dropped_blank_claim"] == 1
    assert result.counts["identity_values_harvested"] == 3 * sum(
        r["patient_account_number"] == first_account for r in rows
    )


# --------------------------------------------------------------------------------------
# The leaks the review found
# --------------------------------------------------------------------------------------

VARIANT_PATIENTS = (
    {
        "patient_first_name": "José",
        "patient_last_name": "Quiñonero-Vélez",
        "member_id": "NW458049016",
        "patient_dob": "1994-09-21",
        "patient_address_line1": "7927 Pinecone Court, Apt 5",
        "patient_city": "Larchmont Springs",
        "patient_state": "WA",
        "patient_zip": "98427",
    },
    {"patient_first_name": "Seamus", "patient_last_name": "O'Thistlebarrow"},
    {
        "patient_first_name": "Ana",
        "patient_last_name": "De La Ventrosa",
        "patient_address_line1": "8979 Quarry Stone Road Apt 7",
    },
    {"patient_first_name": "Quentin", "patient_last_name": "OBellmarsh"},
)
VARIANTS = (
    (0, "Seen: José Quiñonero-Vélez today."),
    (0, "Seen: Jose Quinonero Velez today."),
    (0, "Seen: JOSE QUINONEROVELEZ today."),
    (0, "Patient: QUIÑONERO-VÉLEZ,JOSÉ."),
    (0, "Seen: Mr Quiñonero–Vélez today."),
    (0, "Mr. Quinonero-Velez's back pain improved."),
    (0, "Insurance card shows NW458 049 016 on front."),
    (0, "Insurance card shows NW-458-049-016 on front."),
    (0, "Member ID: NW 458049016 verified."),
    (0, "Born 9/21/94, lives alone."),
    (0, "Born 09-21-1994, lives alone."),
    (0, "Born September 21st 1994, lives alone."),
    (0, "Born 21 Sep 1994, lives alone."),
    (0, "Born Sept 21, 1994, lives alone."),
    (0, "Born Sep. 21, 1994, lives alone."),
    (0, "Born 9-21-94, lives alone."),
    (0, "Born 09.21.1994, lives alone."),
    (0, "Pt (born the 21st of September 1994) lives alone."),
    (0, "Lives at 7927 Pinecone Ct near the park."),
    (0, "Mail to 7927 PINECONE CT APT 5 please."),
    (0, "Mail to 7927 Pinecone Court #5 please."),
    (0, "ZIP on file 98427-2208."),
    (0, "Larchmont Springs, WA 98427 address."),
    (1, "Seen: Seamus O’Thistlebarrow today."),
    (1, "Seen: Seamus OThistlebarrow today."),
    (1, "Seen: Seamus O Thistlebarrow today."),
    (1, "Patient: O'THISTLEBARROW, SEAMUS."),
    (1, "Mrs. O'Thistlebarrow's husband called."),
    (2, "Seen: Ana DeLaVentrosa today. Patient: ANA DELAVENTROSA."),
    (2, "Seen: Ana de la Ventrosa today."),
    (2, "Lives at 8979 Quarry Stone Rd near the mill."),
    (3, "Seen: Quentin O'Bellmarsh today."),
)
VARIANT_FRAGMENTS = (
    "jose",
    "quinonero",
    "velez",
    "458049016",
    "458 049 016",
    "458-049-016",
    "1994-09-21",
    "9/21/94",
    "09-21-1994",
    "september 21",
    "21 sep 1994",
    "sept 21",
    "sep. 21",
    "9-21-94",
    "09.21.1994",
    "21st of september",
    "pinecone",
    "98427",
    "larchmont",
    "seamus",
    "thistlebarrow",
    "ventrosa",
    "quarry stone",
    "bellmarsh",
)


def _fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).lower()


def test_spelling_variants_of_known_identifiers_are_replaced(tmp_path):
    """Accents, curly apostrophes, joined and split surnames, ids with separators, every way a date
    of birth is written, USPS street spellings: none of them survives, and the leak sweep agrees."""
    raw = _small_batch(tmp_path, SEED + 6, 12)
    patients = _patients(raw)[: len(VARIANT_PATIENTS)]
    overrides = {p["patient_account_number"]: v for p, v in zip(patients, VARIANT_PATIENTS, strict=True)}
    rows = _rows(raw / CLAIMS_FILE)
    for row in rows:
        row.update(overrides.get(row["patient_account_number"], {}))
    _write_rows(raw / CLAIMS_FILE, CLAIM_EXPORT_COLUMNS, rows)
    ccns = [p["claim_control_number"] for p in patients]
    _add_notes(raw, [(ccns[index], text) for index, text in VARIANTS])
    out, key_path, result = _anonymize(raw, tmp_path, "out", seed=13)
    assert result.counts["leaks"] == 0
    notes = _rows(_one(out, NOTES))[-len(VARIANTS) :]
    for (_index, original), row in zip(VARIANTS, notes, strict=True):
        folded = _fold(row["text"])
        for fragment in VARIANT_FRAGMENTS:
            assert fragment not in folded, (original, fragment, row["text"])
    key = _key(key_path)
    signed = _subject_pseudonyms(key, patients[0]["patient_account_number"])
    assert f"Seen: {signed['first']} {signed['last']} today." in {row["text"] for row in notes}


def test_another_patient_named_in_a_note_gets_that_patients_pseudonyms(tmp_path):
    raw = _small_batch(tmp_path, SEED + 7, 10)
    first, other = _patients(raw)[:2]
    mention = f"{other['patient_first_name']} {other['patient_last_name']}"
    _add_notes(
        raw,
        [
            (
                first["claim_control_number"],
                f"Accompanied by her son {mention}, also our patient (member {other['member_id']}).",
            )
        ],
    )
    docs = _rows(raw / DOCUMENTS_FILE)
    docs.append(
        {
            "claim_control_number": first["claim_control_number"],
            "document_type": "letter",
            "received_date": "2026-10-01",
            "text": f"Recoupment notice. Also affected: {mention}, member {other['member_id']}.",
        }
    )
    _write_rows(raw / DOCUMENTS_FILE, PAYER_DOCUMENT_COLUMNS, docs)
    out, key_path, result = _anonymize(raw, tmp_path, "out", seed=14)
    pseudonyms = _subject_pseudonyms(_key(key_path), other["patient_account_number"])
    expected_name = f"{pseudonyms['first']} {pseudonyms['last']}"
    expected_id = pseudonyms["ids"][other["member_id"]]
    note = _rows(_one(out, NOTES))[-1]["text"]
    letter = _rows(_one(out, DOCUMENTS))[-1]["text"]
    for text in (note, letter):
        assert expected_name in text and expected_id in text
        assert other["patient_last_name"] not in text and other["member_id"] not in text
    assert result.counts["text_other_patient_mentions"] >= 4


def _x12_file(isa: str, gs01: str, st01: str, body: list[str]) -> str:
    segments = [f"GS*{gs01}*SENDER*RECEIVER*20261028*1200*7*X*005010", f"ST*{st01}*0001", *body]
    segments.append(f"SE*{len(segments)}*0001")
    segments += ["GE*1*7", "IEA*1*000000007"]
    return isa + "\n" + "~\n".join(segments) + "~\n"


def test_files_that_are_not_835s_are_never_copied(tmp_path):
    raw = _small_batch(tmp_path, SEED + 8, 8)
    patient = _patients(raw)[0]
    remits = _files(raw)[REMITS]
    isa = remits[0].read_text(encoding="utf-8").split("\n", 1)[0]
    person = [
        f"NM1*IL*1*{patient['patient_last_name'].upper()}*{patient['patient_first_name'].upper()}****MI*{patient['member_id']}",
        f"N3*{patient['patient_address_line1'].upper()}",
        f"DMG*D8*{patient['patient_dob'].replace('-', '')}*F",
        f"REF*EA*{patient['patient_account_number']}",
    ]
    (raw / "claims_837.txt").write_text(_x12_file(isa, "HC", "837", person), encoding="utf-8")
    (raw / "ack_277.edi").write_text(_x12_file(isa, "HN", "277", person), encoding="utf-8")
    mixed = remits[0].read_text(encoding="utf-8")
    (raw / "zz_mixed.835").write_text(mixed + mixed.replace("*", "|"), encoding="utf-8")
    out, _key_path, result = _anonymize(raw, tmp_path, "out", seed=15)
    reasons = {entry["reason"] for entry in result.skipped_files}
    assert {
        "X12 837, not an 835",
        "X12 277, not an 835",
        "a later interchange uses other delimiters",
    } <= reasons
    assert len(_files(out)[REMITS]) == len(remits) and len(list(out.iterdir())) == 3 + len(remits)
    identifiers = [
        (NAME_PART, patient["patient_last_name"]),
        (MEMBER_ID, patient["member_id"]),
        (ACCOUNT, patient["patient_account_number"]),
        (DOB, patient["patient_dob"].replace("-", "")),
    ]
    for path in out.iterdir():
        assert not _leaks(path.read_text(encoding="utf-8"), identifiers, set()), path.name


def test_plb_references_lose_the_member_ids_medicare_puts_in_them(tmp_path):
    raw = _small_batch(tmp_path, SEED + 9, 10)
    remit = _files(raw)[REMITS][0]
    text = remit.read_text(encoding="utf-8")
    member = parse_835(text)[0].claims[0].member_id
    assert member and len(member) == 11
    historic = "1EG4TE5MK73"  # an MBI-shaped id of a beneficiary the batch does not carry
    provider = re.search(r"N1\*PE\*[^*]*\*XX\*(\d+)", text).group(1)
    plb = f"PLB*{provider}*20261231*WO:0021926000123456789{member}*-25*FB:0021926000987654321{historic}*-40"
    remit.write_text(_fix_se(text.replace("~\nSE*", f"~\n{plb}~\nSE*", 1)), encoding="utf-8")
    for options in ({"seed": 16}, {"seed": 17, "replace_claim_numbers": True}):
        out, _key_path, result = _anonymize(raw, tmp_path, f"out{options['seed']}", **options)
        body = _output_for(raw, out, remit).read_text(encoding="utf-8")
        assert member not in body and historic not in body
        references = [a.reference for t in parse_835(body) for a in t.provider_adjustments]
        assert [ref[:19] for ref in references] == ["0021926000123456789", "0021926000987654321"]
        assert all(len(ref) == 30 for ref in references) and result.counts["plb_references_replaced"] == 2


def test_csv_files_that_do_not_fit_their_contract_are_refused_or_read_strictly(tmp_path, capsys):
    raw = _small_batch(tmp_path, SEED + 10, 6)
    keys = tmp_path / "keys"

    def run(name: str, *extra: str) -> tuple[int, str, str]:
        code = main(
            [
                "anonymize",
                str(raw),
                "--out",
                str(tmp_path / name),
                "--key-out",
                str(keys / f"{name}.json"),
                *extra,
            ]
        )
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    claims_text = (raw / CLAIMS_FILE).read_text(encoding="utf-8")
    rows = _rows(raw / CLAIMS_FILE)
    lines = claims_text.split("\n")
    number = next(
        i for i in range(1, len(rows) + 1) if f",{rows[i - 1]['patient_address_line1']}," in lines[i]
    )
    address = rows[number - 1]["patient_address_line1"]  # written without quotes
    lines[number] = lines[number].replace(address, address + ", Apt 5", 1)  # the comma nobody quoted
    (raw / CLAIMS_FILE).write_text("\n".join(lines), encoding="utf-8")
    code, out, err = run("shifted")
    header_fields = len(lines[0].split(","))
    expected = (
        f"input file 1 row {number + 1} has {header_fields + 1} fields where its header has {header_fields}"
    )
    assert code == 1 and out == "" and expected in err
    assert CLAIMS_FILE not in err and not (tmp_path / "shifted").exists()
    (raw / CLAIMS_FILE).write_text(claims_text, encoding="utf-8")

    notes_text = (raw / NOTES_FILE).read_text(encoding="utf-8")
    (raw / NOTES_FILE).write_text(
        notes_text.replace("author", "Author", 1).replace(",text", ",TEXT", 1), encoding="utf-8"
    )
    assert run("cased", "--test-only", "--seed", "1")[0] == 0
    with open(_one(tmp_path / "cased", NOTES), newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == list(NOTE_COLUMNS)

    (raw / NOTES_FILE).write_text(notes_text.replace("author", "auther", 1), encoding="utf-8")
    code, _out, err = run("typo")
    assert code == 1 and "looks like a notes CSV but lacks the column(s) author" in err

    patient = _patients(raw)[0]
    _replace_words(raw, {patient["patient_last_name"]: "OBrien"})
    rows = _rows(raw / CLAIMS_FILE)
    for row in rows:
        if row["patient_account_number"] == patient["patient_account_number"]:
            row["patient_last_name"] = "O'Brien"
    _write_rows(raw / CLAIMS_FILE, CLAIM_EXPORT_COLUMNS, rows)
    note = f"{patient['claim_control_number']},2026-06-02,Clinic Staff,Mrs. O’Brien called back.\n"
    (raw / NOTES_FILE).write_bytes((notes_text + note).encode("cp1252", errors="replace"))
    code, out, _err = run("windows", "--test-only", "--seed", "2")
    assert code == 0 and json.loads(out)["counts"]["files_read_as_windows_1252"] == 1
    assert "brien" not in _rows(_one(tmp_path / "windows", NOTES))[-1]["text"].lower()

    (raw / NOTES_FILE).write_text(
        notes_text + f"{patient['claim_control_number']},2026-06-02,x,In�s\n", encoding="utf-8"
    )
    code, _out, err = run("damaged")
    assert code == 1 and "U+FFFD" in err


def test_names_that_are_ordinary_words_change_only_where_they_name_someone(tmp_path):
    raw = _small_batch(tmp_path, SEED + 11, 12)
    low, fall = _rename_patients(raw, (("Ray", "Low"), ("May", "Fall")))
    claims = {row["patient_account_number"]: row["claim_control_number"] for row in _rows(raw / CLAIMS_FILE)}
    _add_notes(
        raw,
        [
            (claims[low], CLINICAL),
            (claims[low], "Patient Ray Low was seen with his wife. RAY LOW, DOB on file."),
            (claims[fall], CLINICAL),
            (claims[fall], "Mrs. Fall called to reschedule. May Fall prefers mornings."),
        ],
    )
    out, key_path, _result = _anonymize(raw, tmp_path, "out", seed=18)
    key = _key(key_path)
    ray, may = _subject_pseudonyms(key, low), _subject_pseudonyms(key, fall)
    notes = [row["text"] for row in _rows(_one(out, NOTES))[-4:]]
    assert notes[0] == CLINICAL and notes[2] == CLINICAL
    assert notes[1] == (
        f"Patient {ray['first']} {ray['last']} was seen with his wife. {ray['first'].upper()} {ray['last'].upper()}, "
        "DOB on file."
    )
    assert (
        notes[3] == f"Mrs. {may['last']} called to reschedule. {may['first']} {may['last']} prefers mornings."
    )


def test_provider_names_are_kept_and_never_become_pseudonyms(tmp_path):
    raw = _small_batch(tmp_path, SEED + 12, 10)
    _replace_words(raw, {"Rowan": "Magnus", "Halvorsen": "Ostrander"})  # pool words, on purpose
    patient = next(p for p in _patients(raw) if p["rendering_provider_name"].startswith("Magnus"))
    _replace_words(raw, {patient["patient_last_name"]: "Ostrander"})
    ccn = patient["claim_control_number"]
    _add_notes(
        raw,
        [(ccn, "Dr. Ostrander reviewed the films. Mrs. Ostrander reports less pain.")],
        author="Magnus Ostrander, DC",
    )
    out, key_path, result = _anonymize(raw, tmp_path, "out", seed=19)
    key = _key(key_path)
    pseudonyms = _subject_pseudonyms(key, patient["patient_account_number"])
    note = _rows(_one(out, NOTES))[-1]
    assert note["author"] == "Magnus Ostrander, DC"
    assert note["text"] == f"Dr. Ostrander reviewed the films. Mrs. {pseudonyms['last']} reports less pain."
    providers = {row["rendering_provider_name"] for row in _rows(_one(out, CLAIMS))}
    assert "Magnus Ostrander, DC" in providers
    assert "NM1*82*1*OSTRANDER*MAGNUS" in "".join(p.read_text(encoding="utf-8") for p in _files(out)[REMITS])
    tokens = {
        token.lower()
        for subject in key.document["subjects"].values()
        for role in ("given", "family")
        for token in subject["tokens"][role].values()
    }
    assert not tokens & {"magnus", "ostrander"} and result.counts["provider_authors_kept"] > 0


def test_a_zip_code_that_is_a_procedure_code_is_left_alone(tmp_path):
    raw = _small_batch(tmp_path, SEED + 13, 14)
    rows = _rows(raw / CLAIMS_FILE)
    row = next(r for r in rows if r["procedure_code"] == "97140")
    account = row["patient_account_number"]
    for other in rows:
        if other["patient_account_number"] == account:
            other["patient_zip"] = "97140"
    _write_rows(raw / CLAIMS_FILE, CLAIM_EXPORT_COLUMNS, rows)
    text = f"Manual therapy 97140 performed today. Mailing address on file: {row['patient_address_line1']}, {row['patient_city']}, {row['patient_state']} 97140."
    _add_notes(raw, [(row["claim_control_number"], text)])
    out, _key_path, _result = _anonymize(raw, tmp_path, "out", seed=20)
    note = _rows(_one(out, NOTES))[-1]["text"]
    assert note.startswith("Manual therapy 97140 performed today.") and note.endswith(
        f"{row['patient_state']} 97140."
    )
    assert row["patient_address_line1"] not in note
    assert all(
        r["patient_zip"] != "97140" for r in _rows(_one(out, CLAIMS)) if r["procedure_code"] == "97140"
    )


def test_claim_numbers_that_carry_an_account_need_replace_claim_numbers(tmp_path, capsys):
    raw = _small_batch(tmp_path, SEED + 14, 8)
    numbering: dict[str, str] = {}
    for row in _rows(raw / CLAIMS_FILE):
        numbering.setdefault(
            row["claim_control_number"], f"{row['patient_account_number']}-{len(numbering) + 1}"
        )
    for path in raw.iterdir():
        if path.suffix in (".csv", ".835"):
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"CLM\d{6}", lambda m: numbering.get(m.group(0), m.group(0)), text)
            path.write_text(text, encoding="utf-8")
    out, key = tmp_path / "out", tmp_path / "keys" / "key.json"
    args = ["anonymize", str(raw), "--out", str(out), "--key-out", str(key)]
    assert main(args) == 1
    assert "carry a patient's account number" in capsys.readouterr().err and not out.exists()
    assert main([*args, "--replace-claim-numbers"]) == 0
    capsys.readouterr()
    accounts = [(ACCOUNT, row["patient_account_number"]) for row in _patients(raw)]
    for path in out.iterdir():
        assert not _leaks(path.read_text(encoding="utf-8"), accounts, set()), path.name


def test_the_leak_sweep_stops_a_run_that_missed_a_name(tmp_path, capsys, monkeypatch):
    raw = _small_batch(tmp_path, SEED + 15, 8)
    monkeypatch.setattr(
        text_module.TextIndex, "process", lambda self, text, **kwargs: text_module.TextResult(text=text)
    )
    out, key = tmp_path / "out", tmp_path / "keys" / "key.json"
    assert main(["anonymize", str(raw), "--out", str(out), "--key-out", str(key)]) == 1
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["status"] == "leak" and summary["written"] is False and summary["leaks"]
    assert all(set(hit) == {"file", "place", "field", "kind"} for hit in summary["leaks"])
    assert {hit["field"] for hit in summary["leaks"]} <= {"text", "author", "document_type"}
    assert "leak check" in captured.err and not out.exists() and not key.exists()
    assert not _leaks(captured.out + captured.err, _original_identifiers(raw), _background(raw))


def test_a_failed_write_leaves_nothing_behind(tmp_path, monkeypatch):
    raw = _small_batch(tmp_path, SEED + 16, 6)
    publish = anonymize_paths.Publication.publish

    def failing(self: Any, temporary: Path, target: Path) -> None:
        if target.suffix == ".json":
            raise OSError("the disk is full")
        publish(self, temporary, target)

    monkeypatch.setattr(anonymize_paths.Publication, "publish", failing)
    out, key = tmp_path / "out", tmp_path / "keys" / "key.json"
    with pytest.raises(OSError):
        anonymize_batch(raw, out, key, AnonymizeOptions())
    assert not out.exists() and not key.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert not list((tmp_path / "keys").iterdir())


def test_an_output_folder_is_renamed_into_place_and_never_over_an_existing_one(tmp_path):
    """The staged folder becomes the output folder on every platform (a folder cannot be
    hard-linked, so Linux and macOS rename it too), and a folder that already holds a file is never
    replaced: publishing into it fails, leaves its file alone and leaves no temporary behind."""
    anonymize_paths.publish_folder(tmp_path / "out", {"a.csv": b"first"})
    assert (tmp_path / "out" / "a.csv").read_bytes() == b"first"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "keep.txt").write_bytes(b"keep")
    with pytest.raises(OSError):
        anonymize_paths.publish_folder(taken, {"a.csv": b"second"})
    staged = tmp_path / ".staged"
    staged.mkdir()
    with pytest.raises(OSError):
        anonymize_paths._rename_new(staged, taken)
    assert sorted(p.name for p in taken.iterdir()) == ["keep.txt"]
    assert (taken / "keep.txt").read_bytes() == b"keep" and staged.is_dir()


def test_a_key_of_another_clinic_is_refused(tmp_path, capsys):
    first = _small_batch(tmp_path, SEED + 17, 6, "first")
    _out, key, _result = _anonymize(first, tmp_path, "first_out")
    second = _small_batch(tmp_path, SEED + 18, 6, "second")
    npi = _rows(second / CLAIMS_FILE)[0]["billing_provider_npi"]
    _replace_words(second, {npi: "1999999993"})
    args = [
        "anonymize",
        str(second),
        "--out",
        str(tmp_path / "o"),
        "--key-out",
        str(tmp_path / "k" / "k.json"),
    ]
    assert main([*args, "--key-in", str(key)]) == 1
    assert "another clinic" in capsys.readouterr().err


# --------------------------------------------------------------------------------------
# A later batch with the same key (--key-in)
# --------------------------------------------------------------------------------------

FOLLOWUP_DAYS = 20


def _chosen_claims(session: Session, per_action: int = 4) -> list[str]:
    """Claim numbers of the first work items proposing a corrected claim and an appeal, so both
    ways a follow-up CLP01 is written (the corrected claim's ``-R1`` number, the claim's own) are met."""
    items = _work_items(session)
    corrected = sorted(ccn for ccn, item in items.items() if item["action"] == "corrected_claim")
    appeals = sorted(ccn for ccn, item in items.items() if item["action"] == "appeal")
    assert len(corrected) >= per_action and len(appeals) >= per_action
    return sorted(corrected[:per_action] + appeals[:per_action])


def _followup_letter(folder: Path, raw_row: dict[str, str], received: date) -> None:
    """A payer letter about an earlier claim, naming the patient the way letters do."""
    text = (
        f"Claim number: {raw_row['claim_control_number']}\n"
        f"Member: {raw_row['patient_last_name']}, {raw_row['patient_first_name']}\n"
        f"Member ID: {raw_row['member_id']}\n"
        f"Date of birth: {date.fromisoformat(raw_row['patient_dob']):%m/%d/%Y}\n"
        "The corrected claim was received and is being processed."
    )
    row = {
        "claim_control_number": raw_row["claim_control_number"],
        "document_type": "letter",
        "received_date": received.isoformat(),
        "text": text,
    }
    _write_rows(folder / DOCUMENTS_FILE, PAYER_DOCUMENT_COLUMNS, [row])


def test_a_later_batch_reuses_the_key_and_its_remittances_find_their_claims(raw_dir, runs, as_of, tmp_path):
    """The loop across batches. Follow-up 835s that pay, partly pay, deny again or reverse eight
    submitted fixes, plus a letter, are built from the raw batch's database; pseudonymized with the
    first run's key they match the same claims in the pseudonymized database and record exactly the
    outcomes the raw ones record in the raw database."""
    anon_dir, key_path, _result = runs["flagged"]
    key_before = key_path.read_bytes()
    claim_numbers = _key(key_path).document["values"]["claim_number"]
    shift = timedelta(days=SHIFT_DAYS)
    payment_date = as_of + timedelta(days=FOLLOWUP_DAYS)
    raw_rows = {row["claim_control_number"]: row for row in _rows(raw_dir / CLAIMS_FILE)}
    followup = tmp_path / "followup"
    followup.mkdir()

    with _database(tmp_path / "raw_db") as (session, clinic):
        _import(session, clinic, raw_dir)
        process_pending(session, today=as_of)
        session.commit()
        chosen = _chosen_claims(session)
        items = _submit(session, chosen, as_of)
        plan = {item.id: PLAN_VALUES[index % len(PLAN_VALUES)] for index, item in enumerate(items)}
        for payer_id, text in generate_followup_files(session, items, plan, payment_date).items():
            (followup / f"followup_{payer_id}.835").write_text(text, encoding="utf-8")
        _followup_letter(followup, raw_rows[chosen[0]], payment_date)
        raw_imports = _import(session, clinic, followup)
        raw_outcomes = _outcomes(session)
    assert len(raw_outcomes) == len(chosen)
    assert {result for result, _paid, _status in raw_outcomes.values()} == set(PLAN_VALUES)

    options = AnonymizeOptions(
        seed=21, replace_claim_numbers=True, shift_dates_days=SHIFT_DAYS, key_in=key_path, test_only=True
    )
    out, new_key = tmp_path / "followup_anon", tmp_path / "keys" / "followup.key.json"
    result = anonymize_batch(followup, out, new_key, options)
    assert result.written and result.counts["remittance_subjects"] == 0 and result.chain["run"] == 2
    assert result.patients == len({raw_rows[ccn]["patient_account_number"] for ccn in chosen})
    # The same run again (another key folder: a second key from one parent is a fork the log refuses).
    again, again_key = tmp_path / "again", tmp_path / "keys_again" / "again.key.json"
    assert anonymize_batch(followup, again, again_key, options).written
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in again.iterdir()
    }
    assert new_key.read_bytes() == again_key.read_bytes()

    with _database(tmp_path / "anon_db") as (session, clinic):
        _import(session, clinic, anon_dir)
        process_pending(session, today=as_of + shift)
        session.commit()
        _submit(session, [claim_numbers[ccn] for ccn in chosen], as_of + shift)
        anon_imports = _import(session, clinic, out)
        anon_outcomes = _outcomes(session)
    for name, counts in anon_imports.items():
        assert counts["status"] == "done", name
        if name.startswith("835"):
            assert counts["unmatched"] == 0 and counts["matched"] == counts["claims"], name
    assert anon_imports[DOCUMENTS]["documents"] == 1
    assert anon_imports == raw_imports
    assert anon_outcomes == {claim_numbers[ccn]: value for ccn, value in raw_outcomes.items()}

    # The second batch names its patients with the first batch's pseudonyms ...
    anon_rows = {row["claim_control_number"]: row for row in _rows(_one(anon_dir, CLAIMS))}
    first = anon_rows[claim_numbers[chosen[0]]]
    [letter] = _rows(_one(out, DOCUMENTS))
    assert letter["claim_control_number"] == first["claim_control_number"]
    for expected in (
        f"Member: {first['patient_last_name']}, {first['patient_first_name']}",
        f"Member ID: {first['member_id']}",
        f"Date of birth: {date.fromisoformat(first['patient_dob']):%m/%d/%Y}",
    ):
        assert expected in letter["text"]
    expected_patients = {
        (row["patient_last_name"].upper(), row["patient_first_name"].upper(), row["member_id"])
        for row in (anon_rows[claim_numbers[ccn]] for ccn in chosen)
    }
    remitted = {
        (claim.patient_last_name, claim.patient_first_name, claim.member_id)
        for path in _files(out)[REMITS]
        for transaction in parse_835(path.read_text(encoding="utf-8"))
        for claim in transaction.claims
    }
    assert remitted == expected_patients
    # ... and carries none of the originals.
    identifiers, background = _original_identifiers(raw_dir), _background(raw_dir)
    for path in sorted(out.iterdir()):
        assert not _leaks(path.read_bytes().decode("utf-8"), identifiers, background), path.name

    # The extended key keeps every pair of the first key and adds this run; the first key is untouched.
    old, new = _key(key_path).document, _key(new_key).document
    assert key_path.read_bytes() == key_before
    for family, mapping in old["values"].items():
        assert new["values"][family].items() >= mapping.items(), family
    assert set(new["subjects"]) >= set(old["subjects"]) and new["options"] == old["options"]
    assert len(new["runs"]) == 2 and new["runs"][0] == old["runs"][0]
    assert new["chain"]["id"] == old["chain"]["id"] and new["chain"]["run"] == 2
    assert result.counts["subjects_from_key_in"] == len(old["subjects"])


def test_a_reused_key_must_exist_match_and_stay_untouched(raw_dir, runs, tmp_path, capsys):
    _out, key_path, _result = runs["flagged"]
    before = key_path.read_bytes()
    followup = tmp_path / "followup"
    followup.mkdir()
    remit = _files(raw_dir)[REMITS][0]
    shutil.copy(remit, followup / remit.name)  # a batch of 835s only: the patients come from the key
    old_format = tmp_path / "old.json"
    old_format.write_text(
        json.dumps({"format": "denial-agent/anonymize-key", "version": 2}), encoding="utf-8"
    )
    flags = ["--test-only", "--replace-claim-numbers", "--shift-dates", str(SHIFT_DAYS)]
    cases = [
        (["--test-only"], key_path, "run with it too"),
        (["--test-only", "--replace-claim-numbers"], key_path, "another --shift-dates value"),
        (
            ["--test-only", "--replace-claim-numbers", "--shift-dates", str(SHIFT_DAYS + 1)],
            key_path,
            "another --shift-dates value",
        ),
        ([], runs["plain"][1], "was made with --test-only"),  # a real run never extends a test key
        (flags, tmp_path / "missing.json", "not found"),
        (flags, raw_dir / "labels.json", "not an anonymize key"),
        (flags, old_format, "version 3"),
        (flags, None, "needs --key-in"),
    ]
    for index, (extra, key_in, message) in enumerate(cases):
        out, key_out = tmp_path / f"out{index}", tmp_path / "keys" / f"key{index}.json"
        args = [
            "anonymize",
            str(followup),
            "--out",
            str(out),
            "--key-out",
            str(key_out),
            "--seed",
            "1",
            *extra,
        ]
        if "--test-only" not in extra:
            args.remove("--seed")
            args.remove("1")
        assert main([*args, *(["--key-in", str(key_in)] if key_in is not None else [])]) == 1
        captured = capsys.readouterr()
        assert message in captured.err and captured.out == "", (index, captured.err)
        assert not out.exists() and not key_out.exists()
    same = ["anonymize", str(followup), "--out", str(tmp_path / "same"), "--key-out", str(key_path)]
    assert main([*same, "--key-in", str(key_path), *flags]) == 1
    assert "must be a new file" in capsys.readouterr().err
    assert key_path.read_bytes() == before

    accepted = [
        "anonymize",
        str(followup),
        "--out",
        str(tmp_path / "ok"),
        "--key-out",
        str(tmp_path / "keys" / "ok.json"),
    ]
    assert main([*accepted, "--key-in", str(key_path), *flags]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["key_in"] == str(key_path) and summary["options"]["reuses_key"] is True
    assert summary["counts"]["remittance_subjects"] == 0 and summary["patients"] > 0
    assert key_path.read_bytes() == before


# --------------------------------------------------------------------------------------
# Export packages back to the real identifiers (deanonymize)
# --------------------------------------------------------------------------------------

EXCERPT = "Separate, distinct procedure documented in the visit note."
EXPORT_ACTIONS = ("corrected_claim", "appeal")


def _reviewer(session: Session) -> Any:
    user = create_user(
        session, email="restore@example.test", password="correct horse battery staple 42!", role="reviewer"
    )
    session.commit()
    return user


def _typed_excerpt(row: dict[str, str]) -> str:
    """A sentence a reviewer types on the item page, naming the patient the way people write."""
    dob = date.fromisoformat(row["patient_dob"])
    suffix = "th" if 10 <= dob.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(dob.day % 10, "th")
    return (
        f"Spoke with {row['patient_first_name']} {row['patient_last_name']} (born {MONTHS[dob.month - 1]} "
        f"{dob.day}{suffix}, {dob.year}) about the visit."
    )


def _approve_and_export(
    session: Session, ccn: str, reviewer: Any, typed: str | None = None
) -> tuple[str, bytes]:
    """Approve the claim's work item (with a note excerpt when the proposal needs one, else the
    reviewer's typed sentence) and download its export package, the way a reviewer does."""
    work_item = _work_item_for(session, ccn)
    assert work_item.current_proposal is not None
    note = EXCERPT if review_service.needs_note_excerpt(work_item.current_proposal) else typed
    review_service.approve(session, work_item, reviewer, note_excerpt=note)
    payload, name = build_export_package(session, work_item, reviewer)
    session.commit()
    return name, payload


def _members(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A small batch, pseudonymized with both flags; in the raw and in the pseudonymized database the
    same corrected claim and appeal are approved and exported (the appeal with a sentence the
    reviewer typed, naming the patient and the date of birth)."""
    base = tmp_path_factory.mktemp("restore")
    raw = base / "raw"
    generate(seed=SEED + 5, n_claims=40, out_dir=raw)
    as_of = date.fromisoformat(json.loads((raw / "manifest.json").read_text(encoding="utf-8"))["as_of"])
    anon, key, _result = _anonymize(
        raw, base, "anon", seed=31, replace_claim_numbers=True, shift_dates_days=SHIFT_DAYS
    )
    claim_numbers = _key(key).document["values"]["claim_number"]
    raw_rows = {row["claim_control_number"]: row for row in _rows(raw / CLAIMS_FILE)}
    anon_rows = {row["claim_control_number"]: row for row in _rows(_one(anon, CLAIMS))}
    packages: dict[str, dict[str, tuple[str, bytes]]] = {"raw": {}, "anon": {}}
    with _database(base / "raw_db") as (session, clinic):
        _import(session, clinic, raw)
        process_pending(session, today=as_of)
        session.commit()
        items = _work_items(session)
        chosen = {
            action: min(ccn for ccn, item in items.items() if item["action"] == action)
            for action in EXPORT_ACTIONS
        }
        reviewer = _reviewer(session)
        for action, ccn in chosen.items():
            packages["raw"][action] = _approve_and_export(
                session, ccn, reviewer, _typed_excerpt(raw_rows[ccn])
            )
    with _database(base / "anon_db") as (session, clinic):
        _import(session, clinic, anon)
        process_pending(session, today=as_of + timedelta(days=SHIFT_DAYS))
        session.commit()
        reviewer = _reviewer(session)
        for action, ccn in chosen.items():
            pseudo_ccn = claim_numbers[ccn]
            packages["anon"][action] = _approve_and_export(
                session, pseudo_ccn, reviewer, _typed_excerpt(anon_rows[pseudo_ccn])
            )
    folder = base / "packages"
    folder.mkdir()
    for name, payload in packages["anon"].values():
        (folder / name).write_bytes(payload)
    return {
        "base": base,
        "raw": raw,
        "anon": anon,
        "key": key,
        "chosen": chosen,
        "claim_numbers": claim_numbers,
        "packages": packages,
        "folder": folder,
    }


def _pseudonyms(exported: dict[str, Any], ccn: str) -> list[tuple[str, str]]:
    """(kind, value) of everything the pseudonymized batch calls this claim's patient by."""
    pseudo_ccn = exported["claim_numbers"][ccn]
    row = next(r for r in _rows(_one(exported["anon"], CLAIMS)) if r["claim_control_number"] == pseudo_ccn)
    dob = date.fromisoformat(row["patient_dob"])
    found = [
        (NAME, f"{row['patient_first_name']} {row['patient_last_name']}"),
        (NAME_PART, row["patient_first_name"]),
        (NAME_PART, row["patient_last_name"]),
        (MEMBER_ID, row["member_id"]),
        (ACCOUNT, row["patient_account_number"]),
        (ACCOUNT, pseudo_ccn),
        (ADDRESS, row["patient_address_line1"]),
        (ADDRESS, row["patient_city"]),
    ]
    return found + [(DOB, spelling) for spelling in _dob_spellings(dob)]


def _without_timestamps(summary: dict[str, Any]) -> dict[str, Any]:
    """summary.json without the app's own timestamps (when this database's reviewer acted)."""
    return {key: value for key, value in summary.items() if not key.endswith("_at")}


def _without_patient_token(corrected: dict[str, Any]) -> dict[str, Any]:
    """corrected_claim.json without patient_token, a hash of the account number the app saw."""
    return {
        key: (
            {k: v for k, v in value.items() if k != "patient_token"} if key in ("before", "after") else value
        )
        for key, value in corrected.items()
    }


def test_export_packages_restore_to_the_raw_batchs_identifiers(exported, tmp_path, capsys):
    """Restored with the key, the pseudonymized database's packages equal the raw database's member for
    member (the app's own timestamps and the letter's drafting date aside, the audit excerpt left
    out), the marks of a pseudonymized batch are gone, and none of the patient's pseudonyms is left."""
    folder, out = exported["folder"], tmp_path / "restored"
    sources = [str(path) for path in sorted(folder.iterdir())]
    assert main(["deanonymize", *sources, "--key", str(exported["key"]), "--out", str(out)]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert len(summary["packages"]) == len(EXPORT_ACTIONS) and "real identifiers" in captured.err
    identifiers = _original_identifiers(exported["raw"])
    assert not _leaks(captured.out + captured.err, identifiers, set())
    restored = {path.name: _members(path.read_bytes()) for path in out.iterdir()}
    for action, ccn in exported["chosen"].items():
        raw_name, raw_payload = exported["packages"]["raw"][action]
        anon_name, anon_payload = exported["packages"]["anon"][action]
        assert anon_name.startswith(pseudonymized.ZIP_PREFIX) and not raw_name.startswith(
            pseudonymized.ZIP_PREFIX
        )
        entry = next(entry for entry in summary["packages"] if entry["package"] == anon_name)
        assert re.fullmatch(rf"work_item_\d+_{re.escape(ccn)}_v1_\d{{8}}T\d{{6}}Z\.zip", entry["restored"])
        raw, anon, members = _members(raw_payload), _members(anon_payload), restored[entry["restored"]]
        assert set(members) == set(raw) - {"audit_excerpt.json"} and "audit_excerpt.json" in anon
        assert entry["identifiers_replaced"] > 0 and entry["masked_markers"] == {}
        assert json.loads(anon["summary.json"])[pseudonymized.SUMMARY_KEY] is True
        assert pseudonymized.SUMMARY_KEY not in json.loads(raw["summary.json"])
        assert _without_timestamps(json.loads(members["summary.json"])) == _without_timestamps(
            json.loads(raw["summary.json"])
        )
        if action == "corrected_claim":
            assert pseudonymized.CSV_COLUMN in anon["corrected_claim.csv"].decode("utf-8")
            assert (
                members["corrected_claim.csv"] == raw["corrected_claim.csv"] and entry["fields_restored"] > 0
            )
            assert _without_patient_token(
                json.loads(members["corrected_claim.json"])
            ) == _without_patient_token(json.loads(raw["corrected_claim.json"]))
        else:
            anon_letter = anon["appeal_letter.txt"].decode("utf-8")
            assert anon_letter.startswith(pseudonymized.BANNER)
            letter = members["appeal_letter.txt"].decode("utf-8")
            assert letter.splitlines()[1:] == raw["appeal_letter.txt"].decode("utf-8").splitlines()[1:]
            # the letter's own date is the day the pseudonymized run drafted it, and stays
            assert letter.splitlines()[0] == pseudonymized.strip_banner(anon_letter)[0].splitlines()[0]
            assert members["appeal_letter.html"].decode("utf-8") == letter_html(
                letter, f"Appeal of claim {ccn}"
            )
            assert members["README.txt"] == raw["README.txt"]
        pseudonyms = _pseudonyms(exported, ccn)
        for member, data in members.items():
            text = data.decode("utf-8")
            assert not _leaks(text, pseudonyms, set()), (action, member)
            assert pseudonymized.BANNER not in text and pseudonymized.CSV_COLUMN not in text


def test_a_restore_refuses_what_it_cannot_vouch_for(exported, tmp_path, capsys):
    key = exported["key"]
    appeal_name, appeal_payload = exported["packages"]["anon"]["appeal"]
    source = exported["folder"] / appeal_name

    def restore(package: Path, out: Path, key_path: Path = key) -> str:
        assert main(["deanonymize", str(package), "--key", str(key_path), "--out", str(out)]) == 1
        captured = capsys.readouterr()
        assert captured.out == "" and not out.exists()
        return captured.err

    def tampered(name: str, members: dict[str, bytes]) -> Path:
        path = tmp_path / name / appeal_name
        path.parent.mkdir()
        path.write_bytes(_zip(members))
        return path

    # Another patient's pseudonym typed into the letter: refused before anything is rewritten.
    appeal_ccn = exported["claim_numbers"][exported["chosen"]["appeal"]]
    rows = _rows(_one(exported["anon"], CLAIMS))
    account = next(r["patient_account_number"] for r in rows if r["claim_control_number"] == appeal_ccn)
    other = next(r for r in rows if r["patient_account_number"] != account)
    members = _members(appeal_payload)
    members["appeal_letter.txt"] += (
        f"\nSee also {other['patient_first_name']} {other['patient_last_name']}.".encode()
    )
    err = restore(tampered("other_patient", members), tmp_path / "out1")
    assert "names another patient (PATIENT_NAME)" in err and other["patient_last_name"] not in err

    # A provider whose name is the patient's pseudonym: the restore would write the real name into it.
    own = next(r for r in rows if r["patient_account_number"] == account)
    members = _members(appeal_payload)
    letter = re.sub(
        r"(?m)^Rendering provider: .*$",
        f"Rendering provider: Dana {own['patient_last_name']}, DC (NPI 1428573012)",
        members["appeal_letter.txt"].decode("utf-8"),
    )
    members["appeal_letter.txt"] = letter.encode("utf-8")
    assert "is also the patient's pseudonym" in restore(tampered("provider", members), tmp_path / "out1b")

    # The key of another run does not know the package's claim number.
    _out, other_key, _result = _anonymize(
        exported["raw"], tmp_path, "other", seed=99, replace_claim_numbers=True, shift_dates_days=SHIFT_DAYS
    )
    assert "not a claim number of this key" in restore(source, tmp_path / "out2", other_key)

    # A member the export never writes, a member twice, an oversized member, a file that is not a
    # zip, an output inside the repository or in the packages' own folder.
    members = _members(appeal_payload)
    members["notes.txt"] = b"anything"
    assert "never holds: notes.txt" in restore(tampered("extra", members), tmp_path / "out3")
    duplicate = tmp_path / "duplicate" / appeal_name
    duplicate.parent.mkdir()
    with warnings.catch_warnings(), zipfile.ZipFile(duplicate, "w") as archive:
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name it is asked to write
        for name, data in _members(appeal_payload).items():
            archive.writestr(name, data)
        archive.writestr("appeal_letter.txt", b"a second letter")
    assert "twice" in restore(duplicate, tmp_path / "out4")
    members = _members(appeal_payload)
    members["README.txt"] = b"x" * (11 * 1024 * 1024)
    assert "larger than any export package member" in restore(tampered("big", members), tmp_path / "out5")
    not_zip = tmp_path / "not_zip" / "not_a_package.zip"
    not_zip.parent.mkdir()
    not_zip.write_text("not a zip", encoding="utf-8")
    assert "is not a zip file" in restore(not_zip, tmp_path / "out6")
    inside = PROJECT_ROOT / "data" / "restore-refusal-test"
    assert "inside the repository" in restore(source, inside)
    assert "the folder a package came in" in restore(source, exported["folder"] / "restored")

    # A restored package is never overwritten, and never restored a second time.
    out = tmp_path / "out7"
    assert main(["deanonymize", str(source), "--key", str(key), "--out", str(out)]) == 0
    [written] = list(out.iterdir())
    before = written.read_bytes()
    capsys.readouterr()
    assert main(["deanonymize", str(source), "--key", str(key), "--out", str(out)]) == 1
    assert "never overwritten" in capsys.readouterr().err and written.read_bytes() == before
    assert "not a claim number of this key" in restore(written, tmp_path / "out8")

    # A masked value cannot be restored: the package goes through, with the marker reported.
    members = _members(appeal_payload)
    members["appeal_letter.txt"] += b"\nCall back at [[MASKED_PHONE_1]]."
    result = restore_packages([tampered("masked", members)], key_path=key, out_dir=tmp_path / "out9")
    assert result.packages[0].masked_markers.get("appeal_letter.txt") == 1


def test_a_restored_appeal_gets_its_pdf_rendered_again(exported, tmp_path, monkeypatch):
    monkeypatch.setattr(restore_module, "render_pdf", lambda html: b"%PDF-1.7 restored letter")
    appeal_name, payload = exported["packages"]["anon"]["appeal"]
    source = tmp_path / "in" / appeal_name
    source.parent.mkdir()
    source.write_bytes(payload)
    result = restore_packages([source], key_path=exported["key"], out_dir=tmp_path / "out")
    [package] = result.packages
    members = _members((tmp_path / "out" / package.target).read_bytes())
    assert "README.txt" not in members and members["appeal_letter.pdf"] == b"%PDF-1.7 restored letter"
    assert package.members["appeal_letter.pdf"] == "rebuilt from the restored letter"


def test_the_marks_come_from_the_marker_column_not_the_file_name(exported, tmp_path):
    """The claim export anonymize writes carries a last column, ``pseudonymized_batch`` = ``yes``,
    which the importer records on every claim: a batch renamed by date and type (the way a biller
    files things) still exports marked packages, a later import of the same claims without the column
    keeps the mark, and a raw import carries none."""
    anon, raw = exported["anon"], exported["raw"]
    as_of = date.fromisoformat(json.loads((raw / "manifest.json").read_text(encoding="utf-8"))["as_of"])
    with open(_one(anon, CLAIMS), newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == [*CLAIM_EXPORT_COLUMNS, pseudonymized.IMPORT_COLUMN]
    anon_rows = _rows(_one(anon, CLAIMS))
    assert anon_rows and all(
        row[pseudonymized.IMPORT_COLUMN] == pseudonymized.IMPORT_VALUE for row in anon_rows
    )
    for kind in (NOTES, DOCUMENTS):
        with open(_one(anon, kind), newline="", encoding="utf-8") as handle:
            assert pseudonymized.IMPORT_COLUMN not in next(csv.reader(handle))

    upload = tmp_path / "upload"
    upload.mkdir()
    renamed = {  # the files renamed the way a biller files them: by date and type
        CLAIMS: "2026-11-02_claims.csv",
        NOTES: "2026-11-02_notes.csv",
        DOCUMENTS: "2026-11-02_payer_documents.csv",
    }
    for kind, name in renamed.items():
        shutil.copy(_one(anon, kind), upload / name)
    remits = [
        Path(shutil.copy(path, upload / f"2026-11-02_remit_{number}.835"))
        for number, path in enumerate(_files(anon)[REMITS], start=1)
    ]
    claims = upload / renamed[CLAIMS]
    chosen = {action: exported["claim_numbers"][ccn] for action, ccn in exported["chosen"].items()}
    with _database(tmp_path / "renamed_db") as (session, clinic):
        batch = import_claims_csv(session, claims, clinic_id=clinic.id, created_by="tests")
        session.commit()
        session.expire_all()  # read the mark back from the database, not from the identity map
        assert str(batch.status) == "done" and not pseudonymized.is_pseudonymized_file(batch.file_name)
        claims_in_db = session.execute(select(Claim)).scalars().all()
        assert len(claims_in_db) == len({row["claim_control_number"] for row in anon_rows})
        assert all(claim.extra.get(pseudonymized.EXTRA_KEY) is True for claim in claims_in_db)
        for importer, kind in ((import_notes_csv, NOTES), (import_payer_documents_csv, DOCUMENTS)):
            companion = importer(session, upload / renamed[kind], clinic_id=clinic.id, created_by="tests")
            assert str(companion.status) == "done", kind
        for remit in remits:
            assert str(import_835(session, remit, file_name=remit.name, created_by="tests").status) == "done"
        session.commit()
        process_pending(session, today=as_of + timedelta(days=SHIFT_DAYS))
        session.commit()
        reviewer = _reviewer(session)
        marks: set[str] = set()
        for action, ccn in chosen.items():
            name, payload = _approve_and_export(session, ccn, reviewer)
            members = _members(payload)
            assert name.startswith(pseudonymized.ZIP_PREFIX), action
            assert json.loads(members["summary.json"])[pseudonymized.SUMMARY_KEY] is True
            if "corrected_claim.csv" in members:
                assert pseudonymized.CSV_COLUMN in members["corrected_claim.csv"].decode("utf-8")
                marks.add("csv column")
            for member in ("appeal_letter.txt", "clinic_question.txt"):
                if member in members:
                    assert members[member].decode("utf-8").startswith(pseudonymized.BANNER)
                    marks.add("banner")
        assert marks == {"csv column", "banner"}

        # The same claims imported again without the column (a later export carries none): the mark
        # is sticky, and the new import's file name proves nothing either.
        later = upload / "2026-11-09_claims.csv"
        _write_rows(
            later, CLAIM_EXPORT_COLUMNS, [{c: row[c] for c in CLAIM_EXPORT_COLUMNS} for row in anon_rows]
        )
        batch = import_claims_csv(session, later, clinic_id=clinic.id, created_by="tests")
        session.commit()
        session.expire_all()
        assert str(batch.status) == "done" and batch.counts["claims_updated"] == len(claims_in_db)
        assert all(claim.extra.get(pseudonymized.EXTRA_KEY) is True for claim in claims_in_db)
        work_item = _work_item_for(session, chosen["corrected_claim"])
        payload, name = build_export_package(session, work_item, reviewer)
        session.commit()
        assert name.startswith(pseudonymized.ZIP_PREFIX)
        assert json.loads(_members(payload)["summary.json"])[pseudonymized.SUMMARY_KEY] is True

    with _database(tmp_path / "raw_db") as (session, clinic):
        batch = import_claims_csv(session, raw / CLAIMS_FILE, clinic_id=clinic.id, created_by="tests")
        session.commit()
        assert str(batch.status) == "done"
        assert not any(
            pseudonymized.is_marked_claim(claim.extra) for claim in session.execute(select(Claim)).scalars()
        )
    for name, payload in exported["packages"]["raw"].values():
        assert not name.startswith(pseudonymized.ZIP_PREFIX)
        assert pseudonymized.SUMMARY_KEY not in json.loads(_members(payload)["summary.json"])


def test_a_pseudonym_typed_in_another_form_comes_back_right():
    """A reviewer writes the patient's pseudonyms as people write: an initial, the date of birth
    with an ordinal or with dashes. The restore puts back the original in that same form."""
    originals = Originals()
    for value in ("Hollis", "Yelverton"):
        originals.add_name(value)
    originals.add_patient_id("ACC555123")
    originals.dobs.add(date(1969, 6, 20))
    pseudo = Pseudonymizer(originals, seed=3)
    subject = pseudo.subject("ACC555123")
    subject.seed("Hollis", "Yelverton")
    first, last, _ = subject.person("Hollis", "Yelverton")
    pseudo.patient_id("ACC555123", subject)
    dob = pseudo.dob(subject, date(1969, 6, 20))
    index = TextIndex()
    index.add_subject(subject, reverse=True)
    suffix = "th" if 10 <= dob.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(dob.day % 10, "th")
    text = (
        f"Spoke with {first[0]}. {last} (born {MONTHS[dob.month - 1]} {dob.day}{suffix}, {dob.year}; also "
        f"{dob.month}-{dob.day}-{dob.year % 100:02d}) and {first} {last}."
    )
    restored = index.process(text, owner="ACC555123", scan=False).text
    assert restored == "Spoke with H. Yelverton (born June 20th, 1969; also 6-20-69) and Hollis Yelverton."


# --------------------------------------------------------------------------------------
# Three months of real-run use: a chained key, follow-ups, a returning patient, restores
# --------------------------------------------------------------------------------------


def _renumber(raw: Path, tag: str) -> None:
    """Give a generated batch its own account and claim numbers so that batches of different months
    never collide (ACC1… -> ACC<tag>…, CLM… -> CL<tag>…)."""
    for path in raw.iterdir():
        if path.suffix in (".csv", ".835"):
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"\bACC1(\d{5})\b", rf"ACC{tag}\1", text)
            text = re.sub(r"\bCLM(\d{6})", rf"CL{tag}\1", text)
            path.write_text(text, encoding="utf-8")


def test_three_chained_monthly_batches_restore_with_the_newest_key(tmp_path, capsys):
    """A rehearsal of the pilot: three monthly batches pseudonymized as a real run would be (no seed,
    claim numbers replaced), each with the newest key; month 2 brings back a month-1 patient with a
    new member id and address, month 3 carries the follow-up 835s of month 1's submitted fixes. Both
    databases record the same outcomes; packages exported after month 3 restore with the newest key
    to the raw database's, an older key is refused, and a forked chain is refused."""
    keys = tmp_path / "clinic_keys"
    a0 = date(2026, 10, 28)
    raws = []
    for month, tag in enumerate(("1", "2", "3")):
        raw = tmp_path / f"raw{tag}"
        generate(
            seed=SEED + 40 + month,
            n_claims=40 if tag == "1" else 24,
            out_dir=raw,
            as_of=a0 + timedelta(days=30 * month),
        )
        if tag != "1":
            _renumber(raw, tag)
        raws.append(raw)

    def anonymize(month: int, key_in: Path | None) -> tuple[Path, Path]:
        out, key = tmp_path / f"anon{month}", keys / f"month{month}.key.json"
        result = anonymize_batch(
            raws[month - 1], out, key, AnonymizeOptions(replace_claim_numbers=True, key_in=key_in)
        )
        assert result.written and result.chain["run"] == month, result.summary()
        return out, key

    def chosen_month1(session: Session) -> tuple[str, list[str]]:
        """A corrected claim kept for after month 3, and two fixes submitted now."""
        items = _work_items(session)
        corrected = sorted(ccn for ccn, item in items.items() if item["action"] == "corrected_claim")
        appeals = sorted(ccn for ccn, item in items.items() if item["action"] == "appeal")
        assert len(corrected) >= 2 and appeals
        return corrected[0], [corrected[1], appeals[0]]

    anon1, key1 = anonymize(1, None)
    with _database(tmp_path / "raw_db") as (session, clinic):
        _import(session, clinic, raws[0])
        process_pending(session, today=a0)
        session.commit()
        later, submitted = chosen_month1(session)
        items = _submit(session, submitted, a0 + timedelta(days=1))
        plan = {item.id: PLAN_VALUES[index % len(PLAN_VALUES)] for index, item in enumerate(items)}
        followups = generate_followup_files(session, items, plan, a0 + timedelta(days=50))
    returning = next(r for r in _rows(raws[0] / CLAIMS_FILE) if r["claim_control_number"] == later)
    month2 = _rows(raws[1] / CLAIMS_FILE)
    template = [r for r in month2 if r["claim_control_number"] == month2[0]["claim_control_number"]]
    for index, row in enumerate(template, start=1):
        moved = dict(row)
        for column in (
            "patient_account_number",
            "patient_first_name",
            "patient_last_name",
            "patient_dob",
            "patient_sex",
            "patient_city",
            "patient_state",
        ):
            moved[column] = returning[column]
        moved.update(
            claim_control_number="CL2999999",
            line_item_control_number=f"CL2999999-{index}",
            member_id="ZQ771122334",
            patient_address_line1="8 Hearthwood Terrace",
            patient_zip="98001",
        )
        month2.append(moved)
    _write_rows(raws[1] / CLAIMS_FILE, CLAIM_EXPORT_COLUMNS, month2)
    for payer_id, text in followups.items():
        (raws[2] / f"followup_{payer_id}.835").write_text(text, encoding="utf-8")

    anon2, key2 = anonymize(2, key1)
    # a forked chain: month 3 against the month-1 key, which the run log shows is not the newest
    forked = [
        "anonymize",
        str(raws[2]),
        "--out",
        str(tmp_path / "forked"),
        "--key-out",
        str(keys / "forked.json"),
    ]
    assert main([*forked, "--replace-claim-numbers", "--key-in", str(key1)]) == 1
    assert "not the newest key" in capsys.readouterr().err
    anon3, key3 = anonymize(3, key2)
    claim_numbers = _key(key3).document["values"]["claim_number"]

    exports: dict[str, dict[str, tuple[str, bytes]]] = {"raw": {}, "anon": {}}
    outcomes: dict[str, dict[str, Any]] = {}
    with _database(tmp_path / "raw_db") as (session, clinic):  # month 1 is in already
        for month, batch in ((2, raws[1]), (3, raws[2])):
            _import(session, clinic, batch)
            process_pending(session, today=a0 + timedelta(days=30 * (month - 1)))
            session.commit()
        outcomes["raw"] = _outcomes(session)
        items = _work_items(session)
        month3 = min(
            ccn
            for ccn, item in items.items()
            if ccn.startswith("CL3") and item["action"] in ("appeal", "corrected_claim")
        )
        reviewer = _reviewer(session)
        exports["raw"]["month3"] = _approve_and_export(session, month3, reviewer)
        exports["raw"]["later"] = _approve_and_export(session, later, reviewer)
    with _database(tmp_path / "anon_db") as (session, clinic):
        _import(session, clinic, anon1)
        process_pending(session, today=a0)
        session.commit()
        _submit(session, [claim_numbers[ccn] for ccn in submitted], a0 + timedelta(days=1))
        for month, batch in ((2, anon2), (3, anon3)):
            _import(session, clinic, batch)
            process_pending(session, today=a0 + timedelta(days=30 * (month - 1)))
            session.commit()
        outcomes["anon"] = _outcomes(session)
        reviewer = _reviewer(session)
        exports["anon"]["month3"] = _approve_and_export(session, claim_numbers[month3], reviewer)
        exports["anon"]["later"] = _approve_and_export(session, claim_numbers[later], reviewer)
    assert outcomes["raw"] and outcomes["anon"] == {claim_numbers[c]: v for c, v in outcomes["raw"].items()}

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name, payload in exports["anon"].values():
        (inbox / name).write_bytes(payload)
    packages = sorted(str(path) for path in inbox.iterdir())
    for old_key in (key1, key2):
        assert main(["deanonymize", *packages, "--key", str(old_key), "--out", str(tmp_path / "old")]) == 1
        assert "not the newest key" in capsys.readouterr().err
    stale = tmp_path / "stale_keys"
    stale.mkdir()
    shutil.copy(
        key1, stale / key1.name
    )  # an older key away from the run log: the package still proves it wrong
    later_package = inbox / exports["anon"]["later"][0]
    assert (
        main(
            [
                "deanonymize",
                str(later_package),
                "--key",
                str(stale / key1.name),
                "--out",
                str(tmp_path / "stale"),
            ]
        )
        == 1
    )
    assert "not a pseudonym of" in capsys.readouterr().err
    assert main(["deanonymize", *packages, "--key", str(key3), "--out", str(tmp_path / "restored")]) == 0
    summary = json.loads(capsys.readouterr().out)
    restored = {
        entry["package"]: _members((tmp_path / "restored" / entry["restored"]).read_bytes())
        for entry in summary["packages"]
    }
    for label in ("month3", "later"):
        raw = _members(exports["raw"][label][1])
        members = restored[exports["anon"][label][0]]
        assert set(members) == set(raw) - {"audit_excerpt.json"}
        assert _without_timestamps(json.loads(members["summary.json"])) == _without_timestamps(
            json.loads(raw["summary.json"])
        )
        for member in (
            "corrected_claim.csv",
            "appeal_letter.txt",
            "appeal_letter.html",
            "clinic_question.txt",
        ):
            if member in raw:
                assert members[member] == raw[member], (label, member)
    later_rows = list(
        csv.DictReader(
            io.StringIO(restored[exports["anon"]["later"][0]]["corrected_claim.csv"].decode("utf-8"))
        )
    )
    assert {r["member_id"] for r in later_rows} == {
        "ZQ771122334"
    }  # the patient came back with a new member id
    log_lines = (keys / anonymize_paths.RUN_LOG).read_text(encoding="utf-8").splitlines()
    commands = [json.loads(line)["command"] for line in log_lines]
    assert commands == ["anonymize", "anonymize", "anonymize", "deanonymize"]
