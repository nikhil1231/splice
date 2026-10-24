"""Read what Final Cut actually holds.

The pipeline used to *assert* that a clip had reached Final Cut: writing
`incoming.fcpxml` stamped the sidecar as exported, whether or not the import
ever happened. That is how eight clips ended up marked delivered and invisible
to `sync` while Final Cut had never seen them.

A Final Cut library is a bundle, and each event inside it keeps its catalogue in
`<Event>/CurrentVersion.fcpevent` -- a CoreData SQLite file whose rows carry
NSKeyedArchiver blobs. The clips in the browser are `FFAnchoredSequence` rows and
their `displayName` is the name shown there, so the set of names is readable
without Final Cut's help.

Nothing here writes. The database is opened read-only and, if Final Cut happens
to be mid-write, copied aside and retried; anything still unreadable degrades to
`None` rather than breaking a sync.
"""

from __future__ import annotations

import plistlib
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import config

# A second import of the same media does not replace the first: Final Cut keeps
# both and suffixes the newcomer. Collapsing the suffix is what lets a re-offered
# clip be recognised as the same clip -- and the copy count is what proves the
# import landed.
COPY_SUFFIX = re.compile(r" - a\d+$")

CLIP_TYPE = "FFAnchoredSequence"
ASSET_TYPE = "FFAsset"


def base_name(name: str) -> str:
    """'lunch - a1' -> 'lunch'. Idempotent for names Final Cut has not copied."""
    return COPY_SUFFIX.sub("", name).strip()


def library_path() -> Path | None:
    """The .fcpbundle to read, or None if there is not exactly one obvious one."""
    override = config.fcp_library()
    if override is not None:
        return override if override.is_dir() else None

    bundles = sorted(p for p in config.media_root().glob("*.fcpbundle") if p.is_dir())
    return bundles[0] if len(bundles) == 1 else None


def _unarchive_fields(blob: bytes) -> dict:
    """The scalar fields of an NSKeyedArchiver-encoded dictionary.

    Only the top-level dictionary is resolved, and only entries whose value is a
    plain scalar. That covers `displayName` and `md5String` and keeps us out of
    the business of walking Apple's object graph.
    """
    archive = plistlib.loads(blob)
    objects = archive["$objects"]

    def deref(uid):
        index = uid.data if hasattr(uid, "data") else int(uid)
        return objects[index]

    root = deref(archive["$top"]["root"])
    fields = {}
    for key, value in zip(root.get("NS.keys", []), root.get("NS.objects", [])):
        resolved = deref(value)
        if resolved is None or isinstance(resolved, (str, int, float, bool)):
            fields[deref(key)] = resolved
    return fields


def _connect(event_db: Path) -> sqlite3.Connection:
    """Read-only, and never on the file Final Cut may be writing to."""
    try:
        conn = sqlite3.connect(f"file:{event_db}?mode=ro", uri=True)
        conn.execute("SELECT COUNT(*) FROM ZCOLLECTION").fetchone()
        return conn
    except sqlite3.Error:
        pass

    # Mid-write, or a WAL we cannot see. A copy is consistent enough to read a
    # list of names from, and is never worse than giving up.
    staged = Path(tempfile.mkdtemp(prefix="greels-fcp-")) / event_db.name
    shutil.copyfile(event_db, staged)
    return sqlite3.connect(f"file:{staged}?mode=ro", uri=True)


def _rows(event_db: Path, ztype: str) -> list[dict]:
    conn = _connect(event_db)
    try:
        blobs = conn.execute(
            "SELECT md.ZDICTIONARYDATA FROM ZCOLLECTION col "
            "JOIN ZCOLLECTIONMD md ON md.ZCOLLECTION = col.Z_PK "
            "WHERE col.ZTYPE = ?",
            (ztype,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for (blob,) in blobs:
        if not blob:
            continue
        try:
            out.append(_unarchive_fields(blob))
        except Exception:  # one unreadable row must not lose the other fifty
            continue
    return out


def event_databases(bundle: Path | None = None) -> list[Path]:
    bundle = bundle or library_path()
    if bundle is None or not bundle.is_dir():
        return []
    return sorted(bundle.glob("*/CurrentVersion.fcpevent"))


def imported_clips(bundle: Path | None = None) -> dict[str, int] | None:
    """Clip name -> how many copies Final Cut holds, or None if it cannot be read.

    `None` and `{}` mean different things and callers depend on the difference:
    an empty library is grounds for offering every clip, an unreadable one is
    grounds for trusting whatever the sidecars already say.
    """
    databases = event_databases(bundle)
    if not databases:
        return None

    counts: dict[str, int] = {}
    read_any = False
    for event_db in databases:
        try:
            rows = _rows(event_db, CLIP_TYPE)
        except (sqlite3.Error, OSError):
            continue
        read_any = True
        for fields in rows:
            name = fields.get("displayName")
            if isinstance(name, str) and name:
                key = base_name(name)
                counts[key] = counts.get(key, 0) + 1

    return counts if read_any else None


def duplicate_names(bundle: Path | None = None) -> dict[str, list[str]]:
    """Base name -> every name Final Cut holds for it, where it holds more than one.

    Final Cut cannot update a clip from XML, so re-importing a clip whose
    analysis changed leaves the stale copy sitting next to the fresh one. This is
    what lets `sync` name the ones worth deleting instead of leaving you to spot
    them.
    """
    grouped: dict[str, list[str]] = {}
    for event_db in event_databases(bundle):
        try:
            rows = _rows(event_db, CLIP_TYPE)
        except (sqlite3.Error, OSError):
            continue
        for fields in rows:
            name = fields.get("displayName")
            if isinstance(name, str) and name:
                grouped.setdefault(base_name(name), []).append(name)
    return {k: sorted(v) for k, v in grouped.items() if len(v) > 1}


def asset_hashes(bundle: Path | None = None) -> dict[str, str]:
    """Clip name -> Final Cut's own md5 of the media, where it recorded one."""
    out: dict[str, str] = {}
    for event_db in event_databases(bundle):
        try:
            rows = _rows(event_db, ASSET_TYPE)
        except (sqlite3.Error, OSError):
            continue
        for fields in rows:
            name, digest = fields.get("displayName"), fields.get("md5String")
            if isinstance(name, str) and isinstance(digest, str):
                out[base_name(name)] = digest
    return out
