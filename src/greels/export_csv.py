"""Segments -> a flat cut list.

The fallback for editors that cannot read FCPXML: sortable, filterable, and
enough to jump straight to a moment instead of scrubbing for it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from . import config, library

COLUMNS = [
    "file", "stem", "segment_id", "start_tc", "end_tc", "start_s", "end_s",
    "duration_s", "title", "summary", "tags", "proposed_tags", "english",
    "konkani", "rating", "status", "reels",
]


def _reels_by_segment() -> dict[str, list[str]]:
    """Which cuts each moment is in, so a sorted sheet shows what is spoken for."""
    from . import reel as store

    out: dict[str, list[str]] = {}
    for reel in store.load_all():
        for shot in reel.shots:
            if shot.segment_id:
                out.setdefault(shot.segment_id, []).append(reel.slug)
    return out


def timecode(seconds: float) -> str:
    total = int(seconds)
    frac = int(round((seconds - total) * 100))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}.{frac:02d}"


def write(dest: Path | None = None, only: str | None = None) -> tuple[Path, int]:
    dest = dest or (config.export_dir() / "cutlist.csv")
    sidecars = library.load_all()
    if only:
        wanted = set(library.match([sc.asset.stem for sc in sidecars], only))
        sidecars = [sc for sc in sidecars if sc.asset.stem in wanted]

    used = _reels_by_segment()
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with dest.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for sc in sidecars:
            for seg in sc.segments:
                writer.writerow(
                    {
                        "file": sc.asset.file,
                        "stem": sc.asset.stem,
                        "segment_id": seg.id,
                        "start_tc": timecode(seg.start),
                        "end_tc": timecode(seg.end),
                        "start_s": f"{seg.start:.2f}",
                        "end_s": f"{seg.end:.2f}",
                        "duration_s": f"{seg.duration:.2f}",
                        "title": seg.title,
                        "summary": seg.summary,
                        "tags": " ".join(seg.tags),
                        "proposed_tags": " ".join(seg.proposed_tags),
                        "english": seg.english.replace("\n", " / "),
                        "konkani": seg.konkani.replace("\n", " / "),
                        "rating": seg.rating,
                        "status": seg.status,
                        "reels": " ".join(used.get(seg.id, ())),
                    }
                )
                rows += 1
    return dest, rows
