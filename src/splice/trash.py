"""A copy of anything about to be overwritten or deleted.

The sidecars hold hours of Gemini time and, once a transcript has been
corrected by hand, something no re-run can produce. `backup` covers that
between commits, but the gap between two backups is exactly where a mistyped
edit or a mis-clicked Delete lives -- and both write straight to the file.

So every destructive write drops the previous version in `index/.trash/` first.
These are 10 KB of JSON; the cost is nothing and the alternative is a
transcript that cannot be got back.

Not every write: a rating or a status is a keystroke in review, it happens
hundreds of times an hour, and losing one costs a keystroke. What is kept is
prose, tags, and whole files -- the things that were expensive to make.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Old enough copies stop being a safety net and start being clutter. This is
# generous: at ~10 KB each it is a few megabytes.
KEEP = 300

_STAMP = re.compile(r"^(\d{8}T\d{6}Z)(?:-\d+)?__(.+)$")


def trash_dir() -> Path:
    return config.index_dir() / ".trash"


@dataclass
class Entry:
    path: Path
    when: datetime
    original: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def destination(self) -> Path:
        """Where restoring would put it back.

        Reel files live one level down, and the name says which is which -- so
        a restore does not need to remember what kind of thing it was.
        """
        if self.original.endswith(".draft.json"):
            return config.index_dir() / "drafts" / self.original
        if self.original.endswith(".reel.json"):
            return config.index_dir() / "reels" / self.original
        if self.original.endswith(".plan.json") or self.original == "schedule.json":
            return config.index_dir() / "plans" / self.original
        return config.index_dir() / self.original


def keep(path: Path, reason: str = "") -> Path | None:
    """Put a copy of `path` in the trash. Returns where it went."""
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trash_dir().mkdir(parents=True, exist_ok=True)

    # Two writes in the same second must neither overwrite each other -- which
    # is what correcting several segments in a row would do -- nor sort out of
    # order. The counter is always present and zero-padded, so the filename
    # alone orders the trash.
    n = 1
    target = trash_dir() / f"{stamp}-{n:03d}__{path.name}"
    while target.exists():
        n += 1
        target = trash_dir() / f"{stamp}-{n:03d}__{path.name}"

    shutil.copy2(path, target)
    if reason:
        target.with_suffix(target.suffix + ".why").write_text(reason + "\n")
    _prune()
    return target


def entries() -> list[Entry]:
    """Everything kept, newest first."""
    root = trash_dir()
    if not root.is_dir():
        return []
    out = []
    for path in root.glob("*.json"):
        match = _STAMP.match(path.name)
        if not match:
            continue
        stamp, original = match.groups()
        try:
            when = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
        except ValueError:
            continue
        out.append(Entry(path=path, when=when.replace(tzinfo=timezone.utc), original=original))
    return sorted(out, key=lambda e: e.path.name, reverse=True)


def why(entry: Entry) -> str:
    note = entry.path.with_suffix(entry.path.suffix + ".why")
    return note.read_text().strip() if note.exists() else ""


def restore(entry: Entry) -> Path:
    """Put a kept copy back, keeping whatever it displaces."""
    destination = entry.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    keep(destination, reason=f"displaced by restoring {entry.name}")
    shutil.copy2(entry.path, destination)
    return destination


def empty() -> int:
    root = trash_dir()
    if not root.is_dir():
        return 0
    n = 0
    for path in list(root.iterdir()):
        path.unlink()
        n += 1
    return n


def _prune() -> None:
    kept = entries()
    for entry in kept[KEEP:]:
        entry.path.unlink(missing_ok=True)
        entry.path.with_suffix(entry.path.suffix + ".why").unlink(missing_ok=True)
