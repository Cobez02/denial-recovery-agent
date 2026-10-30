"""`denial-agent anonymize` (docs/SPEC.md section 16).

Every test builds its own raw batch with ``synthetic.generate.generate(...)`` in a temporary folder
(no committed fixture folder is read), pseudonymizes it and checks the promises of the command: no
original identifier survives in any output byte; the pseudonymized batch imports with zero
unmatched remittance claims and yields the same work items, categories and actions as the original
(two databases, rules with the stub model); the key maps every pseudonym back; ``--seed`` makes a
run reproducible; a planted phone number stops the run; output paths inside the repository are
refused; every 835 parses to the same transactions apart from the replaced values.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.anonymize import AnonymizeOptions, PseudonymKey, anonymize_batch, restore_claim_row
from app.anonymize.pseudonyms import Originals, Pseudonymizer
from app.anonymize.text import DOB_FORMATS, TextReplacer, shift_text_dates, subject_pairs
from app.cli import main
from app.config import PROJECT_ROOT
from app.ingest.claims_import import NOTE_COLUMNS
from app.ingest.parse_835 import parse_835
from app.ingest.x12 import read_delimiters, tokenize
from app.redact.registry import date_formats
from app.schemas import CLAIM_EXPORT_COLUMNS
from synthetic.generate import generate
from synthetic.profile import resolve_profile_path

SEED = 20261030
N_CLAIMS = 160
SHIFT_DAYS = 45
CLAIMS_FILE = "claims_export.csv"
NOTES_FILE = "notes.csv"
DOCUMENTS_FILE = "payer_documents.csv"
PLANTED_PHONE = "(503) 555-0187"

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
    for path in sorted(raw_dir.glob("*.835")):
        for transaction in parse_835(path.read_text(encoding="utf-8")):
            for claim in transaction.claims:
                add(NAME, f"{claim.patient_first_name} {claim.patient_last_name}")
                add(NAME_PART, claim.patient_first_name)
                add(NAME_PART, claim.patient_last_name)
                add(MEMBER_ID, claim.member_id)
    return list(found)


def _profile_words(raw_dir: Path) -> set[str]:
    """Words of the specialty profile's own text (clinic, payers, templates): a patient name token
    that is also one of them proves nothing on its own, so only full names are checked for it."""
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    text = resolve_profile_path(manifest["profile"]).read_text(encoding="utf-8")
    return {word.lower() for word in re.findall(r"[A-Za-z]+", text)}


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
    out, key = base / name, base / f"{name}.key.json"
    result = anonymize_batch(raw_dir, out, key, AnonymizeOptions(**options))
    assert result.written, result.summary()
    return out, key, result


def _process(batch_dir: Path, db_dir: Path, today: date) -> dict[str, Any]:
    """Import the batch into a database of its own, run the pipeline, and return what it made."""
    from app.config import get_settings, reset_settings_cache
    from app.db import configure_engine, dispose_engine, init_db, make_engine, session_factory
    from app.ingest.claims_import import import_claims_csv, import_notes_csv, import_payer_documents_csv
    from app.ingest.remit_import import import_835
    from app.models import Claim, Clinic, RemittanceClaim, WorkItem
    from app.pipeline import process_pending
    from tests.conftest import set_test_env

    set_test_env(db_dir)
    reset_settings_cache()
    dispose_engine()
    settings = get_settings()
    engine = make_engine(settings.database_url)
    configure_engine(engine)
    init_db(engine)
    session = session_factory()()
    try:
        clinic = Clinic(
            name="Anonymize Test Clinic", npi="1234567893", tax_id="12-3456789", pm_system="TestPM"
        )
        session.add(clinic)
        session.commit()
        settings.clinic_id = clinic.id
        imports: dict[str, dict[str, Any]] = {}
        for name, importer in (
            (CLAIMS_FILE, import_claims_csv),
            (NOTES_FILE, import_notes_csv),
            (DOCUMENTS_FILE, import_payer_documents_csv),
        ):
            batch = importer(session, batch_dir / name, clinic_id=clinic.id, created_by="tests")
            session.commit()
            imports[name] = {"status": str(batch.status), **(batch.counts or {})}
        for path in sorted(batch_dir.glob("*.835")):
            batch = import_835(session, path, file_name=path.name, created_by="tests")
            session.commit()
            imports[path.name] = {"status": str(batch.status), **(batch.counts or {})}
        process_pending(session, today=today)
        session.commit()
        items: dict[str, dict[str, Any]] = {}
        stmt = select(WorkItem, Claim.claim_control_number).join(Claim, WorkItem.claim_id == Claim.id)
        for work_item, ccn in session.execute(stmt):
            proposal = work_item.current_proposal
            assert proposal is not None
            items[ccn] = {
                "category": str(work_item.category),
                "action": str(proposal.action_type),
                "coding_change": proposal.coding_change,
                "confidence": work_item.category_confidence,
                "source": str(work_item.classification_source),
                "dollar_value": work_item.dollar_value,
                "flags": sorted(work_item.flags or []),
                "deadline": work_item.deadline,
                "deadline_kind": work_item.deadline_kind,
                "priority_score": work_item.priority_score,
                "status": str(work_item.status),
            }
        unmatched = session.execute(
            select(func.count()).select_from(RemittanceClaim).where(RemittanceClaim.claim_id.is_(None))
        ).scalar_one()
        return {"imports": imports, "items": items, "unmatched": unmatched}
    finally:
        session.rollback()
        session.close()
        dispose_engine()
        engine.dispose()
        reset_settings_cache()


# --------------------------------------------------------------------------------------
# One raw batch, two pseudonymized copies and three processed databases for the module
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def raw_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("raw")
    generate(seed=SEED, n_claims=N_CLAIMS, out_dir=out)
    return out


@pytest.fixture(scope="module")
def as_of(raw_dir: Path) -> date:
    return date.fromisoformat(json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))["as_of"])


@pytest.fixture(scope="module")
def runs(raw_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path, Any]]:
    """`plain` uses the defaults; `flagged` also replaces claim numbers and shifts every date."""
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
    background = _profile_words(raw_dir)
    assert {kind for kind, _ in identifiers} == {NAME, NAME_PART, MEMBER_ID, ACCOUNT, DOB, ADDRESS}
    original_rows = _rows(raw_dir / CLAIMS_FILE)
    cities = {row["patient_city"].lower() for row in original_rows}
    zips = {row["patient_zip"] for row in original_rows}
    for label, (out_dir, _key_path, _result) in runs.items():
        outputs = sorted(out_dir.iterdir())
        assert {p.name for p in outputs} == {p.name for p in raw_dir.iterdir()} - {
            "labels.json",
            "manifest.json",
        }
        for path in outputs:
            leaks = _leaks(path.read_bytes().decode("utf-8"), identifiers, background)
            assert not leaks, f"{label}/{path.name}: {leaks}"
        for row in _rows(out_dir / CLAIMS_FILE):
            assert row["patient_city"].lower() not in cities and row["patient_zip"] not in zips


def test_the_pseudonymized_batch_processes_like_the_original(processed, runs):
    original = processed["original"]
    assert len(original["items"]) >= 50, "the synthetic batch should carry plenty of denials"
    assert len({item["category"] for item in original["items"].values()}) >= 6
    for label, run in processed.items():
        assert run["unmatched"] == 0, label
        for name, counts in run["imports"].items():
            assert counts["status"] == "done", (label, name)
            if name.endswith(".835"):
                assert counts["unmatched"] == 0 and counts["matched"] == counts["claims"], (label, name)
    assert processed["plain"]["imports"] == original["imports"]
    assert processed["flagged"]["imports"] == original["imports"]
    assert processed["plain"]["items"] == original["items"]

    claim_numbers = _key(runs["flagged"][1]).document["values"]["claim_number"]
    shift = timedelta(days=SHIFT_DAYS)
    expected = {
        claim_numbers[ccn]: {**item, "deadline": item["deadline"] and item["deadline"] + shift}
        for ccn, item in original["items"].items()
    }
    assert processed["flagged"]["items"] == expected


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

    files = sorted(raw_dir.glob("*.835"))
    assert files
    for original_path in files:
        original_text = original_path.read_text(encoding="utf-8")
        output_text = (out_dir / original_path.name).read_text(encoding="utf-8")
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
        anonymized = _rows(out_dir / CLAIMS_FILE)
        assert len(anonymized) == len(original)
        assert [restore_claim_row(row, key) for row in anonymized] == original, label
        document = key.document
        assert document["date_shift_days"] == (SHIFT_DAYS if label == "flagged" else 0)
        assert set(document["subjects"]) == {row["patient_account_number"] for row in original}


def test_dates_of_birth_keep_their_year_and_ids_keep_their_shape(raw_dir, runs):
    original = _rows(raw_dir / CLAIMS_FILE)
    for label, (out_dir, _key_path, _result) in runs.items():
        shift = timedelta(days=SHIFT_DAYS if label == "flagged" else 0)
        for before, after in zip(original, _rows(out_dir / CLAIMS_FILE), strict=True):
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
    assert contents(other)[CLAIMS_FILE] != contents(first)[CLAIMS_FILE]


def test_a_phone_number_planted_in_a_note_stops_the_run(raw_dir, tmp_path, capsys):
    source = tmp_path / "raw"
    shutil.copytree(raw_dir, source)
    notes = _rows(source / NOTES_FILE)
    target = 3
    notes[target]["text"] += f" Patient asked for a call back at {PLANTED_PHONE}."
    _write_rows(source / NOTES_FILE, NOTE_COLUMNS, notes)
    out, key = tmp_path / "out", tmp_path / "key.json"
    args = ["anonymize", str(source), "--out", str(out), "--key-out", str(key), "--seed", "3"]

    assert main(args) == 1
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["status"] == "residual" and summary["written"] is False
    assert summary["residual"] == [{"file": NOTES_FILE, "row": target + 2, "field": "text", "kind": "PHONE"}]
    for value in (PLANTED_PHONE, "555-0187", "5550187"):
        assert value not in captured.out + captured.err
    assert "residual identifier" in captured.err
    assert not out.exists() and not key.exists()

    assert main([*args, "--allow-residual"]) == 0
    capsys.readouterr()
    masked = _rows(out / NOTES_FILE)[target]["text"]
    assert "[[MASKED_PHONE_1]]" in masked and "555-0187" not in masked
    assert key.exists()


def test_output_paths_inside_the_repository_are_refused(raw_dir, tmp_path, capsys):
    inside = PROJECT_ROOT / "data" / "anonymize-refusal-test"
    assert not inside.exists()
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "existing.json").write_text("{}", encoding="utf-8")
    cases = [
        (inside, tmp_path / "k1.json", "inside the repository"),
        (tmp_path / "o2", inside / "key.json", "inside the repository"),
        (tmp_path / "o3", tmp_path / "o3" / "key.json", "must not be inside --out"),
        (tmp_path / "busy", tmp_path / "k4.json", "not an empty folder"),
        (tmp_path / "o5", tmp_path / "existing.json", "already exists"),
        (raw_dir, tmp_path / "k6.json", "must not be the input folder"),
    ]
    for out, key, message in cases:
        assert main(["anonymize", str(raw_dir), "--out", str(out), "--key-out", str(key), "--seed", "1"]) == 1
        captured = capsys.readouterr()
        assert message in captured.err and captured.out == ""
    assert not inside.exists()
    for name in ("o2", "o3", "o5", "k1.json", "k4.json", "k6.json"):
        assert not (tmp_path / name).exists()
    assert (tmp_path / "existing.json").read_text(encoding="utf-8") == "{}"


def test_stdout_and_logs_carry_no_identifier(raw_dir, tmp_path, capsys, caplog):
    identifiers = _original_identifiers(raw_dir)
    background = _profile_words(raw_dir)
    args = [
        "anonymize",
        str(raw_dir),
        "--out",
        str(tmp_path / "out"),
        "--key-out",
        str(tmp_path / "key.json"),
    ]
    with caplog.at_level(logging.DEBUG, logger="app"):
        assert main([*args, "--replace-claim-numbers", "--shift-dates", "10"]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["status"] == "ok" and summary["patients"] > 0 and summary["options"]["seed"] == "random"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert not _leaks(captured.out + captured.err + logged, identifiers, background)


# --------------------------------------------------------------------------------------
# Parts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("day", [date(2026, 1, 5), date(1975, 11, 23), date(2000, 2, 29), date(1961, 7, 14)])
def test_free_text_dob_spellings_cover_the_redaction_registry(day):
    assert set(date_formats(day)) <= {spell(day) for spell in DOB_FORMATS}


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
    replacer = TextReplacer(subject_pairs(subject))

    text = "YELVERTON, HOLLIS (acct 8941) had 98941 on 2026-06-01; hollis said DOB: 05/05/1974 and 5/5/74."
    result = replacer.process(text)
    assert result.residual == []
    assert f"{last.upper()}, {first.upper()}" in result.text and first.lower() in result.text
    assert f"acct {account})" in result.text and "98941" in result.text and "2026-06-01" in result.text
    assert f"DOB: {dob:%m/%d/%Y}" in result.text
    assert f"{dob.month}/{dob.day}/{dob.year % 100:02d}." in result.text
    assert not re.search(r"hollis|yelverton|05/05/1974|\b8941\b", result.text, re.IGNORECASE)

    leftover = replacer.process("Insured DOB: 03/14/1950, e-mail pat@example.test, SSN 123-45-6789")
    assert sorted(set(leftover.residual)) == ["DOB", "EMAIL", "SSN"]
    masked = replacer.process("Insured DOB: 03/14/1950, call 5035550187", allow_residual=True)
    assert masked.masked >= 2 and "03/14/1950" not in masked.text and "5035550187" not in masked.text


def test_dates_written_in_text_shift_in_their_own_format():
    text = "Seen 2026-06-12, 06/12/2026, 6/1/26, June 12, 2026, Jun. 1st, 2026 and 12 June 2026 (code 99213)."
    shifted, count = shift_text_dates(text, 30)
    assert count == 6
    assert shifted == (
        "Seen 2026-07-12, 07/12/2026, 7/1/26, July 12, 2026, Jul. 1st, 2026 and 12 July 2026 (code 99213)."
    )
    assert shift_text_dates("Due 02/30/2026 or 13/01/2026.", 5) == ("Due 02/30/2026 or 13/01/2026.", 0)


def _plant_segments(text: str, account: str) -> str:
    """After the first NM1*QC add a subscriber who shares the patient's surname, a corrected name,
    an employer named as the other subscriber, REF*EA (the account number), REF*1W (the member id)
    and REF*F8, then fix SE01."""
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
        "REF*F8*ORIG4455667788",
    ]
    segments[index + 1 : index + 1] = planted
    st = next(i for i, seg in enumerate(segments) if seg.startswith("ST*"))
    se = next(i for i, seg in enumerate(segments) if seg.startswith("SE*"))
    se_parts = segments[se].split("*")
    se_parts[1] = str(se - st + 1)
    segments[se] = "*".join(se_parts)
    return "~\n".join(segments)


def test_835_subscriber_corrected_name_and_patient_references(tmp_path):
    raw = tmp_path / "raw"
    generate(seed=SEED + 1, n_claims=8, out_dir=raw)
    rows = _rows(raw / CLAIMS_FILE)
    remit = sorted(raw.glob("*.835"))[0]
    text = remit.read_text(encoding="utf-8")
    first_ccn = parse_835(text)[0].claims[0].patient_control_number
    row = next(r for r in rows if r["claim_control_number"] == first_ccn)
    remit.write_text(_plant_segments(text, row["patient_account_number"]), encoding="utf-8")
    out, _key_path, _result = _anonymize(raw, tmp_path, "out", seed=2, replace_claim_numbers=True)

    output_text = (out / remit.name).read_text(encoding="utf-8")
    after = _rows(out / CLAIMS_FILE)[rows.index(row)]
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


def test_file_names_that_carry_an_identifier_are_renamed(tmp_path, capsys):
    raw = tmp_path / "raw"
    generate(seed=SEED + 4, n_claims=6, out_dir=raw)
    member_id = _rows(raw / CLAIMS_FILE)[0]["member_id"]
    remit = sorted(raw.glob("*.835"))[0]
    remit.rename(raw / f"eob_{member_id}.835")
    out, key = tmp_path / "out", tmp_path / "key.json"
    assert main(["anonymize", str(raw), "--out", str(out), "--key-out", str(key), "--seed", "8"]) == 0
    captured = capsys.readouterr()
    assert member_id not in captured.out + captured.err
    assert "835_1.835" in {p.name for p in out.iterdir()}
    assert not any(member_id in p.name for p in out.iterdir())
    assert _key(key).document["files"][f"eob_{member_id}.835"] == "835_1.835"


def test_other_delimiters_and_crlf_line_ends_survive(tmp_path):
    raw = tmp_path / "raw"
    generate(seed=SEED + 2, n_claims=8, out_dir=raw)
    remits = sorted(raw.glob("*.835"))
    for remit in remits:
        text = remit.read_text(encoding="utf-8")
        remit.write_bytes(text.replace("*", "|").replace(":", ">").replace("~\n", "~\r\n").encode("utf-8"))
    out, _key_path, _result = _anonymize(
        raw, tmp_path, "out", seed=4, replace_claim_numbers=True, shift_dates_days=-20
    )
    for remit in remits:
        before = remit.read_bytes().decode("utf-8")
        after = (out / remit.name).read_bytes().decode("utf-8")
        delimiters = read_delimiters(after)
        assert (delimiters.element, delimiters.sub_element, delimiters.segment) == ("|", ">", "~")
        assert after.count("~\r\n") == before.count("~\r\n") and "*" not in after
        assert len(after.split("~", 1)[0]) == len(before.split("~", 1)[0]) == 105  # the fixed-width ISA
        assert [c.patient_last_name for c in parse_835(after)[0].claims] != [
            c.patient_last_name for c in parse_835(before)[0].claims
        ]


def test_extra_columns_and_rows_for_unknown_claims_are_dropped(tmp_path):
    raw = tmp_path / "raw"
    generate(seed=SEED + 3, n_claims=8, out_dir=raw)
    rows = _rows(raw / CLAIMS_FILE)
    for row in rows:
        row["patient_phone"] = "503-555-0101"
    _write_rows(raw / CLAIMS_FILE, [*CLAIM_EXPORT_COLUMNS, "patient_phone"], rows)
    notes = _rows(raw / NOTES_FILE)
    notes.append({"claim_control_number": "UNKNOWN-1", "note_date": "2026-06-01", "author": "", "text": "x"})
    _write_rows(raw / NOTES_FILE, NOTE_COLUMNS, notes)
    out, _key_path, result = _anonymize(raw, tmp_path, "out", seed=9)
    with open(out / CLAIMS_FILE, newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == list(CLAIM_EXPORT_COLUMNS)
    assert "503-555-0101" not in (out / CLAIMS_FILE).read_text(encoding="utf-8")
    assert len(_rows(out / NOTES_FILE)) == len(notes) - 1
    assert result.counts["columns_dropped"] == 1 and result.counts["rows_dropped_unknown_claim"] == 1
