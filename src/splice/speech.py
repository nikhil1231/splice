"""Where somebody is actually talking, measured rather than estimated.

The model's line timings are estimates and read like it: 0.5, 4.0, 10.0, 12.5,
15.5. Good to a second, which is fine for finding a moment and not fine for
deciding where a shot begins -- a cut that opens half a word in is the defect
you notice first and the one nobody can put their finger on.

The audio knows. `silencedetect` over a proxy takes well under a second and
comes back with every pause, and the gaps between the pauses are the runs of
speech. That is not a transcript aligner and does not pretend to be: it cannot
tell you where the third word starts, only where somebody started talking and
where they stopped. Those are exactly the two places a trim wants to be.

Cached in `work/speech.json` against the proxy's size and mtime, like the
loudness measurements next to it -- computed once per clip, and again the
moment the clip is replaced.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import config, library

# The threshold that found the pauses in this footage without finding pauses
# inside sentences. It is a room-tone question, not a loudness one: these were
# all shot in the same flat.
NOISE_DB = -32
# Shorter than this is a breath or a consonant, not a pause between utterances.
MIN_SILENCE = 0.35
# And shorter than this is not an utterance: the detector's own ramp-up leaves
# a 26 ms "run" at the head of most of these clips, and snapping an in-point
# onto it would be snapping onto nothing.
MIN_RUN = 0.2
# How far a boundary may move. A second is the model's own error bar; letting
# it move further means snapping to the wrong pause entirely.
WINDOW = 1.2
# Land a hair before the first syllable rather than on it, which otherwise
# clips the attack of the word.
LEAD_IN = 0.08

_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def cache_path() -> Path:
    return config.media_root() / "work" / "speech.json"


def _load_cache() -> dict:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}  # derived; a bad one costs a measurement and nothing else


def _save_cache(cache: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _stamp(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def detect(path: Path, duration: float | None = None) -> list[list[float]]:
    """The runs of speech in a file, as [start, end] pairs.

    Built as the complement of the silences, which is the only thing
    `silencedetect` reports. A file with no silence long enough to report is
    one continuous run, which is the right answer and not an empty one.
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-vn",
            "-af", f"silencedetect=noise={NOISE_DB}dB:d={MIN_SILENCE}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    if duration is None:
        from .probe import duration_of

        duration = duration_of(path)

    starts = [float(m) for m in _START.findall(proc.stderr)]
    ends = [float(m) for m in _END.findall(proc.stderr)]
    # A silence still open at the end of the file has a start and no end.
    if len(starts) > len(ends):
        ends.append(duration)

    runs: list[list[float]] = []
    cursor = 0.0
    for start, end in zip(starts, ends):
        if start - cursor >= MIN_RUN:
            runs.append([round(cursor, 3), round(start, 3)])
        cursor = max(cursor, end)
    if duration - cursor >= MIN_RUN:
        runs.append([round(cursor, 3), round(duration, 3)])
    return runs


def runs_for(stem: str, cache: dict | None = None) -> list[list[float]]:
    """One clip's speech runs, from the cache or by listening to its proxy."""
    proxy = library.proxy_for(stem)
    if not proxy.exists():
        return []
    store = _load_cache() if cache is None else cache
    stamp = _stamp(proxy)
    entry = store.get(stem)
    if entry and entry.get("stamp") == stamp:
        return entry["runs"]

    sidecar = library.load_sidecar(stem)
    found = detect(proxy, sidecar.asset.duration if sidecar else None)
    store[stem] = {"stamp": stamp, "runs": found}
    if cache is None:
        _save_cache(store)
    return found


def measure_all(stems: list[str] | None = None) -> tuple[int, int]:
    """Listen to everything up front. Returns (heard, silent)."""
    cache = _load_cache()
    names = stems if stems is not None else [
        sc.asset.stem for sc in library.load_all()
    ]
    heard = silent = 0
    for stem in names:
        if runs_for(stem, cache):
            heard += 1
        else:
            silent += 1
    _save_cache(cache)
    return heard, silent


# --- moving a boundary to one -----------------------------------------


def _nearest(edges: list[float], value: float, window: float) -> float | None:
    """The closest edge within `window`, or None if none is near enough."""
    near = [e for e in edges if abs(e - value) <= window]
    return min(near, key=lambda e: abs(e - value)) if near else None


def snap_in(runs, at: float, window: float = WINDOW) -> float:
    """An in-point moved to the start of the nearest utterance within reach.

    Backwards to the start of the one it has landed inside, so a shot that
    opens half a word in opens on the word -- that first, because it is the
    common case and the one worth fixing. Otherwise to whichever utterance
    starts nearest, forwards or back: from a pause, that is the sentence about
    to begin, and from the last half-second of a long answer it is the next
    one, which is a cleaner place to open than the tail of a word.
    """
    inside = next((r for r in runs if r[0] < at < r[1]), None)
    if inside and at - inside[0] <= window:
        return max(0.0, inside[0] - LEAD_IN)
    edge = _nearest([r[0] for r in runs], at, window)
    return max(0.0, edge - LEAD_IN) if edge is not None else at


def snap_out(runs, at: float, window: float = WINDOW) -> float:
    """An out-point moved to the end of the nearest utterance within reach.

    Forwards when it fell inside one, so the shot finishes the sentence it was
    cutting through -- the fault worth fixing, since a shot ending a beat late
    reads as a breath and one ending mid-word reads as a mistake. Otherwise to
    whichever utterance ends nearest, which from a pause means pulling back off
    a second of room tone the cut never meant to carry.
    """
    inside = next((r for r in runs if r[0] < at < r[1]), None)
    if inside and inside[1] - at <= window:
        return inside[1]
    edge = _nearest([r[1] for r in runs], at, window)
    return edge if edge is not None else at


def snap_range(runs, start: float, end: float, window: float = WINDOW) -> tuple[float, float]:
    """Both ends of a shot, kept in order and never collapsed to nothing."""
    if not runs:
        return start, end
    new_start = snap_in(runs, start, window)
    new_end = snap_out(runs, end, window)
    if new_end - new_start < 0.2:
        return start, end
    return new_start, new_end


def snap_lines(runs, lines, window: float = WINDOW) -> list[tuple[float, float]]:
    """New (start, end) for each subtitle line, aligned to the speech.

    The model returns suspiciously round line times -- 0.5, 4.0, 10.0, 12.5 --
    because it is estimating them rather than measuring them. A caption that
    appears before the mouth moves is the tell everyone notices, and half a
    second is enough.

    Two guards, and they are what stops this making things worse:

    **One onset per line.** Where two lines both sit near the start of the
    same utterance, only the first may have it. Otherwise a run of quick
    exchanges collapses onto one instant.

    **Order is preserved.** A line may not start before the one in front of
    it ended, and may not end before it starts. A snap is a correction of
    half a second, not a re-ordering.
    """
    out: list[tuple[float, float]] = []
    claimed: set[float] = set()
    floor = 0.0

    for line in sorted(lines, key=lambda l: (l.start, l.end)):
        start, end = line.start, line.end
        onset = _nearest(
            [r[0] for r in runs if r[0] not in claimed], start, window
        )
        inside = next((r for r in runs if r[0] < start < r[1]), None)
        if inside and inside[0] not in claimed and start - inside[0] <= window:
            onset = inside[0]
        if onset is not None and onset >= floor - window:
            claimed.add(onset)
            end += max(0.0, onset - LEAD_IN) - start
            start = max(0.0, onset - LEAD_IN)

        start = max(start, floor)
        end = max(end, start + 0.2)
        out.append((round(start, 3), round(end, 3)))
        floor = start
    return out


def runs_map(stems) -> dict[str, list[list[float]]]:
    """Speech runs for a set of clips, measured once between them."""
    cache = _load_cache()
    before = json.dumps(cache, sort_keys=True)
    out = {stem: runs_for(stem, cache) for stem in dict.fromkeys(stems)}
    if json.dumps(cache, sort_keys=True) != before:
        _save_cache(cache)
    return out
