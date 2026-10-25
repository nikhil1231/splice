"""Segments -> .srt.

Timings are rebased to the segment, so the file drops straight onto a clip
already trimmed to that range in CapCut or Final Cut without an offset.

Caveat worth knowing: the model estimates line timings rather than measuring
them, so they land within a second or so rather than on the word. Good enough
to nudge into place, not good enough to burn in unchecked.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config, library
from .schema import Segment, Sidecar

_SLUG = re.compile(r"[^a-z0-9]+")


def timecode(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render(segment: Segment, lang: str = "en") -> str:
    """One .srt body, with times relative to the segment's own start."""
    blocks: list[str] = []
    for i, line in enumerate(segment.lines, start=1):
        start = max(0.0, line.start - segment.start)
        end = max(start + 0.2, line.end - segment.start)

        if lang == "kok":
            text = line.kok or line.en
        elif lang == "both":
            text = "\n".join(part for part in (line.kok, line.en) if part)
        else:
            text = line.en or line.kok
        if not text.strip():
            continue

        blocks.append(f"{i}\n{timecode(start)} --> {timecode(end)}\n{text.strip()}\n")
    return "\n".join(blocks)


_TIME = re.compile(r"(\d\d:\d\d:\d\d),(\d\d\d)")


def to_vtt(body: str) -> str:
    """The same captions as WebVTT, which is the only thing a browser reads.

    A <track> cannot take .srt, and an mp4's own subtitle stream is not shown
    reliably by any of them -- so watching a rendered cut in the tab it was
    made in needs this. The two formats differ, for our purposes, in a header
    and a decimal point.
    """
    return "WEBVTT\n\n" + _TIME.sub(r"\1.\2", body)


def slug(text: str, limit: int = 40) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:limit] or "segment"


def filename(sidecar: Sidecar, segment: Segment) -> str:
    return (
        f"{sidecar.asset.stem}__{int(segment.start):05d}-"
        f"{int(segment.end):05d}__{slug(segment.title)}.srt"
    )


def write(
    dest_dir: Path | None = None, only: str | None = None, lang: str = "en"
) -> tuple[Path, int, int]:
    """Write one .srt per segment and remove the ones no segment claims.

    The filename carries the segment's start and end, so a re-analysis that
    moves a boundary by a second writes a new file and orphans the old one. That
    went unnoticed until the directory held 210 files for 155 segments, three of
    them different answers for the same opening moment. Pruning is only safe
    because this directory is written by nothing else.
    """
    dest_dir = dest_dir or (config.export_dir() / "srt")
    sidecars = library.load_all()
    if only:
        wanted = set(library.match([sc.asset.stem for sc in sidecars], only))
        sidecars = [sc for sc in sidecars if sc.asset.stem in wanted]

    dest_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    current: set[str] = set()
    for sc in sidecars:
        for segment in sc.segments:
            body = render(segment, lang=lang)
            if not body:
                continue  # silent segment
            name = filename(sc, segment)
            current.add(name)
            (dest_dir / name).write_text(body)
            written += 1

    # A partial run knows nothing about the clips it was not asked for, so it
    # may only prune within those clips.
    stems = {sc.asset.stem for sc in sidecars} if only else None
    pruned = 0
    for stale in dest_dir.glob("*.srt"):
        if stale.name in current:
            continue
        if stems is not None and stale.name.split("__", 1)[0] not in stems:
            continue
        stale.unlink()
        pruned += 1

    return dest_dir, written, pruned
