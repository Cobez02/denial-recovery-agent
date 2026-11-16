"""Where `denial-agent anonymize` and `deanonymize` may read and write (docs/SPEC.md section 16).

- :func:`canonical` folds the Windows spellings of one place together (``\\\\?\\C:\\x``,
  ``\\\\?\\UNC\\host\\share``, an administrative share of this computer such as
  ``\\\\localhost\\C$\\x``) before resolving, and :func:`inside` also compares by file identity
  (``os.path.samefile`` on the nearest existing folder), so a junction, an 8.3 name or an alias never
  slips a path into the repository.
- :func:`synced_folder` names the sync client a path is under (OneDrive, including the Documents
  and Desktop folders Windows redirects into it, Dropbox, Google Drive, iCloud, Box and the like):
  a key or a batch there is copied to a cloud outside any business associate agreement.
- :func:`removable_or_remote` tells a removable or network volume (where a key must never sit next
  to the batch it maps).
- :func:`publish_folder` and :func:`publish_new_file` write under a temporary name and rename at the
  end, never over an existing file; :func:`restrict_to_owner` gives a key an owner-only ACL on
  Windows (``icacls``; the 0o600 mode of ``os.open`` sets nothing there) and mode 600 elsewhere.
- :func:`append_run_log` keeps the append-only run log next to the key: one JSON line per run with
  the time, the operating-system user, the command, the key chain and run, file names, hashes and
  counts (never a value). :func:`newer_runs` reads it back to refuse a key that is not the newest.
"""

from __future__ import annotations

import errno
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUN_LOG = "denial-agent-runs.log"
SYNC_FOLDER_NAMES: frozenset[str] = frozenset(
    {
        "dropbox",
        "google drive",
        "googledrive",
        "my drive",
        "icloud drive",
        "iclouddrive",
        "icloud",
        "box sync",
        "pcloud drive",
        "nextcloud",
        "owncloud",
        "sharefile",
    }
)
_ADMIN_SHARE_RE = re.compile(r"^\\\\([^\\]+)\\([A-Za-z])\$(\\.*)?$")
DRIVE_REMOVABLE, DRIVE_REMOTE = 2, 4


def _local_hosts() -> set[str]:
    names = {
        "localhost",
        "127.0.0.1",
        "::1",
        "[::1]",
        socket.gethostname(),
        os.environ.get("COMPUTERNAME", ""),
    }
    return {name.casefold() for name in names if name}


def _unalias(text: str) -> str:
    value = text.replace("/", "\\")
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith(("\\\\?\\", "\\\\.\\")):
        value = value[4:]
    match = _ADMIN_SHARE_RE.match(value)
    if match is not None and match.group(1).casefold() in _local_hosts():
        value = f"{match.group(2)}:{match.group(3) or chr(92)}"
    return value


def canonical(path: Path) -> Path:
    """Absolute and resolved, with the Windows aliases of one place folded together."""
    text = str(path.expanduser())
    if os.name == "nt":
        text = _unalias(text)
    return Path(text).resolve()


def inside(path: Path, parent: Path) -> bool:
    """``path`` is ``parent`` or somewhere under it (by name, then by file identity)."""
    path, parent = canonical(path), canonical(parent)
    if path == parent or parent in path.parents:
        return True
    if not parent.exists():
        return False
    for candidate in (path, *path.parents):
        if candidate.exists():
            try:
                if os.path.samefile(candidate, parent):
                    return True
            except OSError:
                return False
    return False


def overlap(first: Path, second: Path) -> bool:
    """One of the two paths is the other or inside it."""
    return inside(first, second) or inside(second, first)


def synced_folder(path: Path) -> str | None:
    """The sync client whose folder holds ``path``, or None."""
    resolved = canonical(path)
    for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(variable)
        if root and inside(resolved, Path(root)):
            return "OneDrive"
    for part in resolved.parts:
        lowered = part.casefold().rstrip("\\/")
        if lowered == "onedrive" or lowered.startswith("onedrive - "):
            return "OneDrive"
        if lowered in SYNC_FOLDER_NAMES:
            return part
    if inside(resolved, Path.home() / "Box"):
        return "Box"
    return None


def removable_or_remote(path: Path) -> bool:
    """True for a removable or network volume (Windows); a network share path always counts."""
    resolved = canonical(path)
    anchor = resolved.anchor
    if anchor.startswith("\\\\"):
        return True
    if os.name != "nt" or not anchor:
        return False
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    return int(windll.kernel32.GetDriveTypeW(anchor)) in (DRIVE_REMOVABLE, DRIVE_REMOTE)


def same_volume(first: Path, second: Path) -> bool:
    return canonical(first).anchor.casefold() == canonical(second).anchor.casefold()


def restrict_to_owner(path: Path) -> str:
    """Make ``path`` readable by its owner only; returns what was done (for stdout)."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return "owner-only (mode 600)"
    try:
        # Inherited entries removed; one entry left for OWNER RIGHTS (S-1-3-4), the file's owner,
        # which needs no account-name lookup.
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-3-4:F"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "not set (icacls is not available): the folder's permissions apply"
    if result.returncode != 0:
        return "not set (icacls refused): the folder's permissions apply"
    return "owner-only (icacls)"


def _temporary(path: Path) -> Path:
    return path.parent / f".{path.name}.partial-{secrets.token_hex(4)}"


def _rename_new(source: Path, target: Path) -> None:
    """Rename without ever replacing an existing target."""
    if os.name == "nt":
        os.rename(source, target)  # refuses an existing target on Windows
        return
    if source.is_dir() and not source.is_symlink():
        # A folder cannot be hard-linked. rename() refuses a target folder that holds anything and
        # a target that is a file; Publication.publish removed an empty target folder, and one that
        # has appeared since is refused here.
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        os.rename(source, target)
        return
    os.link(source, target)  # refuses an existing target
    os.unlink(source)


def write_new(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


class Publication:
    """Files written under temporary names and made visible together, or not at all."""

    def __init__(self) -> None:
        self._temporaries: list[Path] = []
        self._published: list[Path] = []

    def stage_folder(self, out: Path, files: dict[str, bytes]) -> Path:
        temporary = _temporary(out)
        temporary.mkdir(parents=True)
        self._temporaries.append(temporary)
        for name, data in sorted(files.items()):
            write_new(temporary / name, data)
        return temporary

    def stage_file(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary(path)
        write_new(temporary, data)
        self._temporaries.append(temporary)
        return temporary

    def publish(self, temporary: Path, target: Path) -> None:
        if temporary.is_dir() and target.exists():
            target.rmdir()  # empty: checked before anything was read
        _rename_new(temporary, target)
        self._temporaries.remove(temporary)
        self._published.append(target)

    def roll_back(self) -> None:
        for path in [*self._temporaries, *self._published]:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        self._temporaries.clear()
        self._published.clear()


def publish_folder(out: Path, files: dict[str, bytes]) -> None:
    """Write every file into a temporary sibling of ``out`` and rename it to ``out`` at the end."""
    publication = Publication()
    try:
        publication.publish(publication.stage_folder(out, files), out)
    except BaseException:
        publication.roll_back()
        raise


def publish_new_file(path: Path, data: bytes) -> None:
    publication = Publication()
    try:
        publication.publish(publication.stage_file(path, data), path)
    except BaseException:
        publication.roll_back()
        raise


def operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is not a reason to stop
        return "unknown"


def append_run_log(folder: Path, entry: dict[str, Any]) -> None:
    """One JSON line in ``folder``/denial-agent-runs.log (created on first use, only ever appended)."""
    record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "user": operator(), **entry}
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / RUN_LOG, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def newer_runs(folders: Iterable[Path], chain_id: str, run: int) -> list[dict[str, Any]]:
    """Run-log entries of the same key chain with a later run number."""
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for folder in folders:
        log_path = canonical(folder) / RUN_LOG
        if log_path in seen or not log_path.is_file():
            continue
        seen.add(log_path)
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(entry, dict)
                and entry.get("command") == "anonymize"
                and entry.get("chain") == chain_id
                and int(entry.get("run") or 0) > run
            ):
                found.append(entry)
    return found


__all__ = [
    "RUN_LOG",
    "Publication",
    "append_run_log",
    "canonical",
    "inside",
    "newer_runs",
    "operator",
    "overlap",
    "publish_folder",
    "publish_new_file",
    "removable_or_remote",
    "restrict_to_owner",
    "same_volume",
    "synced_folder",
    "write_new",
]
