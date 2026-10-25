"""A poster frame per segment.

192 rows of prose is a slow way to look for a moment. The proxies already
exist, and a frame out of one costs ~130 ms and 8 KB -- so the browser can
show you what each moment looks like without anything new being encoded.

Built on demand and cached. Which frame matters more than it sounds: the first
frame of a segment is often the tail of the previous shot, or a hand crossing
the lens, so this takes one a quarter of the way in.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

from . import config, library
from .schema import Segment, Sidecar

WIDTH = 200
QUALITY = 6
# Far enough in to miss the tail of the last shot, near enough to still be
# this moment rather than the next one.
INTO = 0.25
INTO_CAP = 3.0

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

# Frames are built inside the request that asks for one, and a fast scroll can
# ask for twenty at once. Each is cheap; twenty ffmpegs at once are not.
_slots = threading.Semaphore(4)


def thumb_dir() -> Path:
    return config.media_root() / "work" / "thumbs"


def path_for(segment_id: str) -> Path:
    """A filename that survives a segment id containing anything.

    The id carries the clip's stem and its start time, so a re-analysis that
    moves a boundary asks for a different file rather than reusing a frame from
    the wrong moment.
    """
    return thumb_dir() / f"{_SAFE.sub('_', segment_id)}.jpg"


def poster_time(segment: Segment) -> float:
    return segment.start + min(segment.duration * INTO, INTO_CAP)


def build(sidecar: Sidecar, segment: Segment, force: bool = False) -> Path | None:
    """Extract one frame. Returns None when there is no proxy to take it from."""
    dest = path_for(segment.id)
    if dest.exists() and not force:
        return dest
    source = library.proxy_for(sidecar.asset.stem)
    if not source.exists():
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    # A distinct temp name per caller: two requests for the same frame arriving
    # together must not write over each other half way.
    tmp = dest.with_suffix(f".{threading.get_ident():x}.tmp.jpg")
    with _slots:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-nostdin", "-v", "error",
                # -ss before -i seeks by keyframe, which is what makes this fast.
                "-ss", f"{poster_time(segment):.2f}",
                "-i", str(source),
                "-frames:v", "1",
                "-vf", f"scale={WIDTH}:-2",
                "-q:v", str(QUALITY),
                str(tmp),
            ],
            capture_output=True, text=True,
        )
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return dest


def ensure(segment_id: str) -> Path | None:
    """The frame for a segment id, building it if this is the first ask."""
    dest = path_for(segment_id)
    if dest.exists():
        return dest
    stem = segment_id.split("#", 1)[0]
    sidecar = library.load_sidecar(stem)
    if sidecar is None:
        return None
    segment = next((s for s in sidecar.segments if s.id == segment_id), None)
    return build(sidecar, segment) if segment else None


def build_all(sidecars: list[Sidecar] | None = None, force: bool = False):
    """(built, skipped, failed) over every segment."""
    built = skipped = failed = 0
    for sidecar in sidecars if sidecars is not None else library.load_all():
        for segment in sidecar.segments:
            if path_for(segment.id).exists() and not force:
                skipped += 1
            elif build(sidecar, segment, force=force):
                built += 1
            else:
                failed += 1
    return built, skipped, failed


def prune(sidecars: list[Sidecar] | None = None) -> int:
    """Drop frames for segments that no longer exist.

    Re-analysis renumbers segments, so without this the directory keeps a
    picture of every boundary the model has ever proposed.
    """
    root = thumb_dir()
    if not root.is_dir():
        return 0
    live = {
        path_for(segment.id).name
        for sidecar in (sidecars if sidecars is not None else library.load_all())
        for segment in sidecar.segments
    }
    n = 0
    for stale in root.glob("*.jpg"):
        if stale.name not in live:
            stale.unlink()
            n += 1
    return n
