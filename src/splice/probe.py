"""ffprobe wrappers.

`fps` is kept as the verbatim `r_frame_rate` rational ("30000/1001"), never a
float: the FCPXML exporter has to land every time on a frame boundary, and a
float round-trip loses that.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path

from .schema import Asset

# Enough of each end to separate two clips, without reading 14 GB every sync.
# Camera files differ within the first frames and in their trailing moov atom,
# so head + tail + exact size is decisive in practice and costs microseconds.
FINGERPRINT_WINDOW = 1 << 20


class ProbeError(RuntimeError):
    pass


def _ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed for {path.name}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def parse_fps(rate: str) -> Fraction:
    """'30000/1001' -> Fraction. Falls back to 30 for degenerate values."""
    try:
        frac = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return Fraction(30, 1)
    return frac if frac > 0 else Fraction(30, 1)


def duration_of(path: Path) -> float:
    """Cheap duration-only probe, used for proxies and chunks."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed for {path.name}: {proc.stderr.strip()}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise ProbeError(f"no duration for {path.name}") from exc


def fingerprint(path: Path) -> str:
    """Content identity for a clip.

    Identity used to be the filename, which meant re-copying a clip off the
    phone after it had been renamed looked like a brand new clip: another
    proxy, another Gemini run, another copy in Final Cut. Hashing the ends and
    the exact size makes sameness a property of the bytes instead.
    """
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(FINGERPRINT_WINDOW))
        if size > 2 * FINGERPRINT_WINDOW:
            handle.seek(-FINGERPRINT_WINDOW, 2)
            digest.update(handle.read(FINGERPRINT_WINDOW))
    return digest.hexdigest()[:32]


def probe_asset(path: Path) -> Asset:
    info = _ffprobe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError(f"{path.name} has no video stream")

    fmt = info.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    # Rotated phone footage reports storage dimensions; FCPXML wants display.
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width

    created = (fmt.get("tags") or {}).get("creation_time")
    stat = path.stat()

    return Asset(
        file=path.name,
        stem=path.stem,
        fingerprint=fingerprint(path),
        mtime=stat.st_mtime,
        duration=duration,
        fps=str(parse_fps(video.get("r_frame_rate", "30/1"))),
        width=width,
        height=height,
        has_audio=audio is not None,
        created=created,
        size_bytes=stat.st_size,
    )


def _rotation(video: dict) -> int:
    tag = (video.get("tags") or {}).get("rotate")
    if tag:
        try:
            return abs(int(float(tag))) % 360
        except ValueError:
            pass
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                return abs(int(float(side["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    return 0
