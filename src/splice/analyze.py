"""Drive `agy` over proxies/chunks and merge the results into a sidecar.

Two verified facts shape this module:

* `agy --json-schema S --output-format json` returns the parsed object under
  the top-level `structured_output` key. (`--json-schema` is rejected unless
  the output format is json/stream-json.) The `response` string also holds the
  JSON but with stray `toolAction`/`toolSummary` keys mixed in, so
  `structured_output` is the one to read.
* Headless `agy -p` auto-denies the `read_file` tool, so a permission grant is
  mandatory. A project-local `.antigravity/settings.json` was tried and is not
  picked up, so this passes --dangerously-skip-permissions rather than editing
  the user's global agy config behind their back. The prompt tells the model to
  read only, and the flag scopes to this one subprocess. Set
  SPLICE_AGY_SKIP_PERMISSIONS=0 if you have allow-rules configured elsewhere.

  The denial is worth guarding for explicitly: agy reports it as
  `status: SUCCESS` with an empty response and a `denied_actions` list, which
  would otherwise surface as a confusing "no segments" error.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import config, library, legacy
from .agy import AnalysisError, run_structured
from .schema import (AnalysisRun, Asset, Line, Segment,
                     Sidecar, agy_schema, speakers, write_agy_schema)
from .taxonomy import Taxonomy

# Launching several agy processes in the same instant made all five chunks of
# the first interview fail their first attempt with an empty ERROR, then all
# succeed on retry -- which doubled a 44-minute run. Staggering the starts is
# cheap insurance against whatever that cold-start contention is.
LAUNCH_STAGGER = 4.0
_launch_lock = threading.Lock()
_last_launch = 0.0

MIN_SEGMENT = 1.0  # seconds; shorter fragments are merge artefacts
OVERLAP_TOLERANCE = 0.5
CONTAINED_FRACTION = 0.6


# --- prompt ------------------------------------------------------------


def build_prompt(
    video_path: Path,
    taxonomy: Taxonomy,
    legacy_prose: str = "",
    chunk_index: int | None = None,
    chunk_count: int | None = None,
    chunk_offset: float = 0.0,
) -> str:
    template = config.fill_prompt(config.prompt_path().read_text())

    if chunk_index is None:
        chunk_note = "You are given the clip in full."
        relative_note = " from the start of the clip"
    else:
        start = _hhmmss(chunk_offset)
        chunk_note = (
            f"This is part {chunk_index + 1} of {chunk_count} of a longer recording. "
            f"It begins at {start} in the full recording, but every timestamp you "
            f"return must be measured from the start of THIS file, starting at 0. "
            f"The last few seconds overlap the next part; cover them anyway."
        )
        relative_note = " from the start of THIS file"

    if legacy_prose:
        hint = (
            "An earlier, rougher pass over the full recording produced the notes "
            "below. Their timestamps are unreliable minute buckets, so do not copy "
            "them — but they name people, places and events correctly, so use them "
            "for spelling and to avoid missing something. Only describe what is "
            "actually in the part you were given.\n\n"
            f"{legacy_prose}"
        )
    else:
        hint = ""

    return (
        template.replace("{video_path}", str(video_path))
        .replace("{chunk_note}", chunk_note)
        .replace("{relative_note}", relative_note)
        .replace("{taxonomy}", _indent(taxonomy.prompt_block(), "  "))
        .replace("{legacy_hint}", hint)
    )


def _indent(text: str, pad: str) -> str:
    return "\n".join(pad + line if line else line for line in text.splitlines())


def _hhmmss(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def prompt_sha() -> str:
    """Fingerprint of what would make a re-analysis produce different output.

    Deliberately excludes the taxonomy. Adding a tag to the vocabulary used to
    invalidate every clip in the archive and trigger hours of re-analysis for
    almost no gain -- growing the vocabulary is what `reclassify` is for, and
    `analyze --force` is there when a re-run really is wanted.
    """
    payload = (config.fill_prompt(config.prompt_path().read_text())
               + json.dumps(agy_schema(), sort_keys=True))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# --- agy ---------------------------------------------------------------


def run_agy(prompt: str, schema_path: Path) -> dict:
    return run_structured(prompt, schema_path)


def _stagger() -> None:
    global _last_launch
    with _launch_lock:
        wait = _last_launch + LAUNCH_STAGGER - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_launch = time.monotonic()


def _call_with_retry(prompt: str, schema_path: Path, label: str) -> dict:
    last: Exception | None = None
    for attempt in range(config.AGY_RETRIES + 1):
        _stagger()
        try:
            return run_agy(prompt, schema_path)
        except AnalysisError as exc:
            last = exc
            if attempt < config.AGY_RETRIES:
                print(f"    {label}: attempt {attempt + 1} failed ({exc}); retrying")
    raise AnalysisError(f"{label}: {last}")


# --- normalising and merging -------------------------------------------


def to_segments(raw: dict, offset: float, stem: str, taxonomy: Taxonomy) -> list[Segment]:
    """Model output for one chunk -> Segments on the asset's own timeline."""
    out: list[Segment] = []
    for item in raw.get("segments") or []:
        try:
            start = float(item["start"]) + offset
            end = float(item["end"]) + offset
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue

        known, proposed = taxonomy.split(item.get("tags") or [])
        lines = []
        for line in item.get("lines") or []:
            try:
                ls, le = float(line["start"]) + offset, float(line["end"]) + offset
            except (KeyError, TypeError, ValueError):
                continue
            lines.append(
                Line(start=ls, end=le, kok=(line.get("kok") or "").strip(),
                     en=(line.get("en") or "").strip(),
                     speaker=_speaker(line.get("speaker")))
            )

        out.append(
            Segment(
                id=f"{stem}#{start:08.2f}",
                start=start,
                end=end,
                title=(item.get("title") or "Untitled").strip(),
                summary=(item.get("summary") or "").strip(),
                konkani=(item.get("konkani") or "").strip(),
                english=(item.get("english") or "").strip(),
                lines=sorted(lines, key=lambda l: l.start),
                tags=known,
                proposed_tags=proposed,
            )
        )
    return out


def _times(raw: dict) -> list[float]:
    values = []
    for item in raw.get("segments") or []:
        for thing in [item, *(item.get("lines") or [])]:
            for key in ("start", "end"):
                try:
                    values.append(float(thing[key]))
                except (KeyError, TypeError, ValueError):
                    pass
    return values


def reads_as_a_clock(raw: dict, length: float) -> bool:
    """Whether the model wrote its timestamps as minutes and seconds.

    Asked for seconds, Gemini sometimes answers a whole chunk in M.SS: a line
    runs from 57 to 100, meaning 0:57 to 1:00. Nothing downstream can tell,
    and it is worse than a wrong caption. The clock reaches past the end of
    the file (4:05 is 405), so the merge clamps what it can, drops the lines
    beyond the clip, and throws away the next chunk's real minutes as
    duplicates of it. `grandpa_nana_drew_the_house_plan` is 129 seconds long
    and came back running to 2:09: everything after 0:59 was stretched out of
    place, and the English at 1:48 that Whisper hears plainly ("these people
    took the initiative and they built this house") was dropped as past the
    end.

    A clock never shows :60 to :99, and seconds nearly always do, so a chunk
    is read as a clock only when none of its timestamps past a minute do
    *and* either one of them lies past the end of the file, which seconds
    cannot, or there are too many of them for that to be chance: a 245-second
    chunk puts 43% of its range in those bands, and twelve values missing
    them all is 0.57^12, about one in a thousand. A chunk that mixes the two
    is left alone -- it cannot be converted without guessing line by line.
    """
    past_a_minute = [t for t in _times(raw) if t >= 60]
    if not past_a_minute or any(t % 100 >= 60 for t in past_a_minute):
        return False
    return max(past_a_minute) > length + 1.0 or len(past_a_minute) >= 12


def clock_to_seconds(raw: dict) -> dict:
    """The same answer with every M.SS timestamp turned into seconds."""
    def convert(value):
        try:
            t = float(value)
        except (TypeError, ValueError):
            return value
        minutes = int(t // 100)
        return minutes * 60 + (t - minutes * 100)

    out = json.loads(json.dumps(raw))
    for item in out.get("segments") or []:
        for thing in [item, *(item.get("lines") or [])]:
            for key in ("start", "end"):
                if key in thing:
                    thing[key] = convert(thing[key])
    return out


def merge_segments(segments: list[Segment], duration: float) -> list[Segment]:
    """Resolve chunk-overlap duplicates into one monotonic, disjoint timeline.

    Chunks overlap by CHUNK_OVERLAP seconds, so the same moment can arrive
    twice: once whole from the earlier chunk, once truncated from the later
    one. Keep the first, drop anything largely inside it, and trim the rest
    back to the boundary.
    """
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    kept: list[Segment] = []

    for seg in ordered:
        seg.start = max(0.0, min(seg.start, duration))
        seg.end = max(0.0, min(seg.end, duration))
        if seg.end - seg.start < MIN_SEGMENT:
            continue

        if kept:
            previous = kept[-1]
            overlap = previous.end - seg.start
            if overlap > OVERLAP_TOLERANCE:
                if overlap >= CONTAINED_FRACTION * (seg.end - seg.start):
                    continue  # a duplicate of something already kept
                seg.start = previous.end
                if seg.end - seg.start < MIN_SEGMENT:
                    continue

        seg.id = f"{seg.id.split('#')[0]}#{seg.start:08.2f}"
        seg.lines = [l for l in seg.lines if l.end > seg.start and l.start < seg.end]
        for line in seg.lines:
            line.start = max(line.start, seg.start)
            line.end = min(line.end, seg.end)
        kept.append(seg)

    return kept


# --- top level ---------------------------------------------------------


def _speaker(value) -> str:
    """The line's speaker, or empty for one nobody has decided.

    Held to the same vocabulary the `person:` tags use, so the colours and the
    keyword sidebar in Final Cut are talking about the same people.
    """
    name = str(value or "").strip().lower()
    return name if name in speakers() else ""


def _units_for(asset: Asset, chunk_seconds: int) -> list[tuple[float, Path]]:
    """The proxy, or its chunks if it is long enough to need them."""
    from .proxy import build_chunks, build_proxy

    proxy = build_proxy(config.raw_dir() / asset.file, library.proxy_for(asset.stem))
    return build_chunks(proxy, asset.stem, chunk_seconds=chunk_seconds) or [(0.0, proxy)]


def _analyse_unit(
    asset: Asset,
    index: int,
    total: int,
    offset: float,
    path: Path,
    chunked: bool,
    length: float,
    taxonomy: Taxonomy,
    schema_path: Path,
    legacy_prose: str,
) -> tuple[list[Segment], str | None]:
    label = f"{asset.stem} [{index + 1}/{total}]"
    prompt = build_prompt(
        path,
        taxonomy,
        legacy_prose=legacy_prose,
        chunk_index=index if chunked else None,
        chunk_count=total if chunked else None,
        chunk_offset=offset,
    )
    try:
        raw = _call_with_retry(prompt, schema_path, label)
    except AnalysisError as exc:
        return [], str(exc)
    if reads_as_a_clock(raw, length):
        print(f"    {label}: timestamps came back as minutes.seconds; converted")
        raw = clock_to_seconds(raw)
    return to_segments(raw, offset, asset.stem, taxonomy), None


def analyze_assets(
    assets: list[Asset],
    taxonomy: Taxonomy,
    schema_path: Path,
    legacy_prose: dict[str, str] | None = None,
    jobs: int = config.DEFAULT_JOBS,
    chunk_seconds: int = config.CHUNK_SECONDS,
    on_done: Callable[[Sidecar], None] | None = None,
) -> list[Sidecar]:
    """Analyse many assets, parallelising across every chunk of every clip.

    Pooling per-asset would leave the 30-odd single-chunk clips running strictly
    serially, so the unit of work here is one chunk of one clip regardless of
    which clip it belongs to.
    """
    legacy_prose = legacy_prose or {}
    plan: list[tuple[Asset, int, int, float, Path, bool, float]] = []
    for asset in assets:
        units = _units_for(asset, chunk_seconds)
        chunked = len(units) > 1
        for i, (offset, path) in enumerate(units):
            # What `build_chunks` cut, so a timestamp past it can be recognised.
            length = (min(chunk_seconds + config.CHUNK_OVERLAP, asset.duration - offset)
                      if chunked else asset.duration)
            plan.append((asset, i, len(units), offset, path, chunked, length))

    by_stem = {a.stem: a for a in assets}
    segments: dict[str, list[Segment]] = {a.stem: [] for a in assets}
    errors: dict[str, list[str]] = {a.stem: [] for a in assets}
    total_units: dict[str, int] = {a.stem: 0 for a in assets}
    remaining: dict[str, int] = {a.stem: 0 for a in assets}
    for asset, _, total, _, _, _, _ in plan:
        total_units[asset.stem] = total
        remaining[asset.stem] = total

    def finish(stem: str) -> Sidecar:
        asset = by_stem[stem]
        return Sidecar(
            asset=asset,
            analysis=AnalysisRun(
                model=config.AGY_MODEL,
                run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                chunks=total_units[stem],
                chunk_seconds=chunk_seconds if total_units[stem] > 1 else 0,
                prompt_sha=prompt_sha(),
                errors=errors[stem],
            ),
            segments=merge_segments(segments[stem], asset.duration),
        )

    done: dict[str, Sidecar] = {}

    # Completed clips are handed back as they land rather than at the end, so a
    # long backfill saves as it goes and an interrupted run keeps its work.
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {
            pool.submit(
                _analyse_unit, asset, i, total, offset, path, chunked, length,
                taxonomy, schema_path, legacy_prose.get(asset.stem, ""),
            ): asset.stem
            for asset, i, total, offset, path, chunked, length in plan
        }
        for future in as_completed(futures):
            stem = futures[future]
            unit_segments, error = future.result()
            segments[stem].extend(unit_segments)
            if error:
                errors[stem].append(error)
            remaining[stem] -= 1
            if remaining[stem] == 0:
                done[stem] = finish(stem)
                if on_done:
                    on_done(done[stem])

    return [done[a.stem] for a in assets if a.stem in done]


def analyze_asset(
    asset: Asset,
    taxonomy: Taxonomy,
    schema_path: Path,
    legacy_prose: str = "",
    jobs: int = config.DEFAULT_JOBS,
    chunk_seconds: int = config.CHUNK_SECONDS,
) -> Sidecar:
    """Single-asset convenience wrapper."""
    (sidecar,) = analyze_assets(
        [asset], taxonomy, schema_path,
        legacy_prose={asset.stem: legacy_prose}, jobs=jobs, chunk_seconds=chunk_seconds,
    )
    if not sidecar.segments and sidecar.analysis.errors:
        raise AnalysisError("; ".join(sidecar.analysis.errors))
    return sidecar


def schema_file() -> Path:
    path = Path(tempfile.gettempdir()) / "splice_agy_schema.json"
    return write_agy_schema(path)


@lru_cache(maxsize=1)
def _legacy_index() -> dict[str, dict[str, str]]:
    return legacy.parse()


def legacy_prose_for(stem: str) -> str:
    """The old log's notes for one clip, parsed once per process."""
    return (_legacy_index().get(stem) or {}).get("prose", "")
