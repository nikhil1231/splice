"""How loud each clip is, so a cut does not jump between them.

Sixty clips shot across several afternoons on a phone held at whatever distance
the conversation happened to be at. Between the quietest and the loudest there
is about 12 dB, which as pictures would be a colour cast nobody could ignore
and as sound is the thing that makes a cut feel unfinished: you reach for the
volume at every join.

So each clip is measured once -- integrated loudness, the EBU R128 number that
corresponds to how loud something *sounds* rather than how big its samples are
-- and the cut applies a fixed gain per clip to bring them together. A fixed
gain, not a compressor: the point is to match one recording to another, not to
flatten what happens inside either. A limiter on the finished mix catches the
peaks that a boost would otherwise push past full scale.

Measured on the proxy, which is 32 kHz mono and takes under a second, because
this is a relative measurement and the proxy is the same recording. Cached in
`work/loudness.json` against the proxy's size and mtime, so it is computed once
per clip ever and recomputed the moment the clip is replaced.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, library

# What a reel is normalised to. Instagram and YouTube both play back around
# -14 LUFS; speech recorded on a phone sits nearer -20 and sounds thin against
# everything else in a feed.
TARGET_LUFS = -16.0
# A gain bigger than this is not a level difference, it is a broken recording,
# and lifting it 20 dB lifts the room with it.
MAX_GAIN_DB = 12.0
# Below this, matching costs more than it buys: nobody hears half a decibel.
MIN_GAIN_DB = 0.3
# Where the limiter on the finished mix sits, in dBTP.
CEILING_DB = -1.0

_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


@dataclass
class Measurement:
    """One clip's loudness, as EBU R128 sees it."""

    lufs: float
    peak_db: float

    @property
    def gain_db(self) -> float:
        """What to add to bring this clip to the target, within reason."""
        return max(-MAX_GAIN_DB, min(MAX_GAIN_DB, TARGET_LUFS - self.lufs))


def cache_path() -> Path:
    return config.media_root() / "work" / "loudness.json"


def _load_cache() -> dict:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}  # a derived file; a bad one is worth nothing and costs nothing


def _save_cache(cache: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _stamp(path: Path) -> str:
    """What makes a measurement stale: the file it was taken from changing."""
    stat = path.stat()
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def measure(path: Path) -> Measurement | None:
    """Integrated loudness and true peak, or None if there is nothing to hear.

    `loudnorm` in analysis mode rather than `ebur128`: it prints JSON, which is
    a great deal less brittle to read than a log summary whose layout is a
    presentational detail of ffmpeg.
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-i", str(path),
            "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    match = _JSON.search(proc.stderr)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        lufs = float(data["input_i"])
        peak = float(data["input_tp"])
    except (ValueError, KeyError):
        return None
    # A silent track measures as -inf, which normalises to nothing but noise.
    if lufs < -70 or lufs != lufs:
        return None
    return Measurement(lufs=lufs, peak_db=peak)


def for_stem(stem: str, cache: dict | None = None) -> Measurement | None:
    """One clip's measurement, from the cache or by measuring its proxy."""
    proxy = library.proxy_for(stem)
    if not proxy.exists():
        return None
    store = _load_cache() if cache is None else cache
    stamp = _stamp(proxy)
    entry = store.get(stem)
    if entry and entry.get("stamp") == stamp:
        if entry.get("lufs") is None:
            return None
        return Measurement(lufs=entry["lufs"], peak_db=entry.get("peak_db", 0.0))

    found = measure(proxy)
    store[stem] = {
        "stamp": stamp,
        "lufs": found.lufs if found else None,
        "peak_db": found.peak_db if found else None,
    }
    if cache is None:
        _save_cache(store)
    return found


def gains_for(resolved, ceiling: float | None = None) -> dict[str, float]:
    """The gain to apply to each clip in a cut, keyed by stem.

    Clips it cannot measure are simply absent, which the render reads as no
    change -- a missing proxy should cost you a level match, not a render.

    `ceiling` is for somewhere that has no limiter to catch a lift: Final Cut
    applies a clip's volume and clips whatever goes over. Speech sits far below
    its own peaks, so the clip that needs the most lift is often the one
    already peaking, and limiting each clip to its own headroom would undo the
    matching -- which is the whole point. Instead the *whole set* comes down by
    however much the worst one overshoots. Quieter than the target, and still
    matched.
    """
    cache = _load_cache()
    before = json.dumps(cache, sort_keys=True)
    found: dict[str, Measurement] = {}
    for shot in resolved.shots:
        stem = shot.shot.stem
        if stem in found or not shot.sidecar:
            continue
        measured = for_stem(stem, cache)
        if measured:
            found[stem] = measured
    if json.dumps(cache, sort_keys=True) != before:
        _save_cache(cache)

    offset = 0.0
    if ceiling is not None and found:
        overshoot = max(m.gain_db + m.peak_db - ceiling for m in found.values())
        offset = -max(0.0, overshoot)

    return {
        stem: round(m.gain_db + offset, 1)
        for stem, m in found.items()
        if abs(m.gain_db + offset) >= MIN_GAIN_DB
    }


def measure_all(stems: list[str] | None = None) -> tuple[int, int]:
    """Measure everything up front. Returns (measured, unmeasurable)."""
    cache = _load_cache()
    names = stems if stems is not None else [
        sc.asset.stem for sc in library.load_all()
    ]
    done = failed = 0
    for stem in names:
        if for_stem(stem, cache):
            done += 1
        else:
            failed += 1
    _save_cache(cache)
    return done, failed
