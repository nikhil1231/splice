"""An FCPXML project -> the shots it is made of.

Everything else here writes FCPXML. This reads it, and it exists because a cut
assembled by hand in Final Cut is not a `.reel.json`: the captions greels can
build from a reel are timed for the reel's own running order, and an edit with
b-roll cut into it, music under it and eight shots off one clip shares no
timings with that at all.

Reading Final Cut's own library was the obvious alternative and does not work.
`Grandparents.fcpbundle/*/Ep1/CurrentVersion.fcpevent` yields the clips and
their source ranges -- the reconstructed spine summed to 33.533s, exactly the
`mediaRange` the sequence records for itself -- but the parent/child table is
an unordered many-to-many with no sort column, so the running order is not in
there. `File > Export XML` states every clip's `offset` outright, which is the
whole problem solved by asking rather than by inference.

What comes back is a `reel.Resolved`, the same thing `resolve()` builds from a
cut, so the .srt, the browser's WebVTT and the running order are the existing
ones and not a second implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

from . import fcp, library
from .reel import Reel, Resolved, ResolvedShot, Shot
from .schema import Line, Sidecar

# Final Cut writes times as a rational number of seconds ("11500/3000s"), and
# whole seconds without a denominator ("5s", "0s").
_TIME = re.compile(r"^(-?\d+)(?:/(\d+))?s$")


def seconds(value: str | None, default: float = 0.0) -> float:
    if not value:
        return default
    match = _TIME.match(value.strip())
    if not match:
        return default
    num, den = match.group(1), match.group(2)
    return float(Fraction(int(num), int(den) if den else 1))


class Unreadable(ValueError):
    """The file is not an FCPXML project we can take a running order from."""


@dataclass
class Placement:
    """One piece of source footage, somewhere on the master timeline."""

    stem: str
    name: str
    source_in: float
    source_out: float
    offset: float          # seconds from the start of the timeline
    duration: float        # how long it runs *on the timeline*
    lane: int = 0          # 0 is the spine; anything else is connected
    audio_only: bool = False
    silent: bool = False
    retimed: bool = False

    @property
    def end(self) -> float:
        return self.offset + self.duration


# Containers that hold other clips rather than referring to footage themselves.
_CONTAINERS = {"clip", "sync-clip", "mc-clip", "gap", "audition", "spine"}
# Elements that can stand for a shot, and so can be a connected clip.
_SHOT_TAGS = {"clip", "sync-clip", "mc-clip", "asset-clip", "ref-clip",
              "audition", "video", "audio"}
# Things on a spine that are not footage and carry no captions.
_IGNORED = {"title", "transition", "caption", "marker", "chapter-marker",
            "keyword", "rating", "note", "adjust-volume", "audio-channel-source",
            "filter-video", "filter-audio", "conform-rate", "timeMap",
            "adjust-transform", "adjust-crop", "smart-collection", "spine"}


class _Document:
    """Resource lookups: which file an id means, and what a compound holds."""

    def __init__(self, root: ET.Element):
        self.assets: dict[str, str] = {}
        self.audio: dict[str, bool] = {}
        self.media: dict[str, ET.Element] = {}
        self.formats: dict[str, ET.Element] = {}
        for resources in root.iter("resources"):
            for asset in resources.findall("asset"):
                rid = asset.get("id")
                if rid:
                    self.assets[rid] = _stem_of(asset)
                    self.audio[rid] = asset.get("hasAudio") == "1"
            for media in resources.findall("media"):
                rid = media.get("id")
                if rid is not None:
                    self.media[rid] = media
            for fmt in resources.findall("format"):
                rid = fmt.get("id")
                if rid is not None:
                    self.formats[rid] = fmt


def _stem_of(asset: ET.Element) -> str:
    """The clip stem an `<asset>` stands for.

    The filename, not the `name` attribute: a second import of the same media
    is named `lunch - a1` in the browser while the file on disk is still
    `lunch.mov`, and the sidecars are keyed by the file.
    """
    rep = asset.find("media-rep")
    src = (rep.get("src") if rep is not None else None) or asset.get("src") or ""
    if src:
        from urllib.parse import unquote, urlparse

        name = Path(unquote(urlparse(src).path)).name
        if name:
            return Path(name).stem
    return fcp.base_name(asset.get("name") or "")


def _carries_audio(el: ET.Element, doc: "_Document", stem: str) -> bool:
    """Whether this shot is heard, as opposed to only seen.

    A cutaway laid over someone still talking is a `<clip>` holding a `<video>`
    and nothing else: its own dialogue is not in the mix, so captioning it puts
    words on screen that nobody is saying and cuts the sentence underneath in
    half. Ep1 has one, and it was interrupting "Look, I made these for you".

    The audio of a shot whose sound has been detached is a lane of its own
    holding `<audio>` for the same file, so the two cases are told apart by
    what the clip contains rather than by where it sits.
    """
    if el.tag == "audio":
        return True
    if el.tag in ("asset-clip", "video") and doc.audio.get(el.get("ref") or ""):
        return el.tag == "asset-clip"
    for child in el.iter():
        if child is el:
            continue
        if doc.assets.get(child.get("ref") or "") != stem:
            continue
        if child.tag == "audio":
            return True
        if child.tag == "asset-clip" and doc.audio.get(child.get("ref") or ""):
            return True
    return False


def _time_map(el: ET.Element):
    """A retimed clip's local time -> the source time it shows, or None.

    `<timept time= value=>` is exactly that mapping: `time` runs in the clip's
    own retimed timeline, `value` is the frame of the source that lands there.
    Reading the values as a span and dividing by the clip's duration -- which
    is what this did first -- makes an *identity* map look like a 14x speed-up,
    because Final Cut writes the map across the whole asset while the clip uses
    three seconds of it. Ep1 has two such maps and both are identity: Final Cut
    writes one for any clip that has been near the retime editor.
    """
    tm = el.find("timeMap")
    if tm is None:
        return None
    points = sorted(
        (seconds(p.get("time")), seconds(p.get("value")))
        for p in tm.findall("timept")
    )
    if len(points) < 2:
        return None

    def at(t: float) -> float:
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if t <= t1 or (t1, v1) == points[-1]:
                return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return points[-1][1]

    return at


def _primary(el: ET.Element, doc: "_Document") -> tuple[str, float, float, bool] | None:
    """The footage a container clip is actually made of.

    Final Cut wraps a shot in a `<clip>` whose own `start` and `duration` are
    the trim, and puts a `<video>` inside it that spans the entire asset. Read
    the inner element as the shot and you get the whole clip, every line in it,
    at an offset that can be negative. The wrapper is the shot; this only says
    which file it points at, and how the wrapper's local time maps onto it.

    Returns (stem, its start, its offset, audio-only). Connected clips are left
    alone -- they are shots in their own right, and `_walk` reaches them.
    """
    for child in el:
        if child.tag in _IGNORED and child.tag not in _CONTAINERS:
            continue
        if child.get("lane") and child.tag in _SHOT_TAGS:
            continue
        stem = doc.assets.get(child.get("ref") or "")
        if stem:
            return (stem, seconds(child.get("start"), 0.0),
                    seconds(child.get("offset"), 0.0), child.tag == "audio")
        deeper = _primary(child, doc)
        if deeper:
            return deeper
    return None


def _walk(
    parent: ET.Element,
    doc: _Document,
    tl_start: float,
    local_origin: float,
    lane: int,
    out: list[Placement],
    depth: int = 0,
    anchored_only: bool = False,
) -> None:
    if depth > 12:  # a compound clip inside itself; Final Cut allows the file
        return

    for child in parent:
        tag = child.tag
        if tag in _IGNORED and tag not in _CONTAINERS:
            continue
        if anchored_only and not child.get("lane"):
            continue

        offset = seconds(child.get("offset"), local_origin)
        start = seconds(child.get("start"), 0.0)
        duration = seconds(child.get("duration"), 0.0)
        # An anchored item's offset is measured in its parent's own time, which
        # for a clip on the spine is *source* time. Subtracting the parent's
        # own origin is what stops every connected clip landing at zero -- or,
        # on a shot trimmed thirty seconds in, at minus thirty.
        here = tl_start + (offset - local_origin)
        child_lane = int(child.get("lane") or 0) or lane

        if tag == "ref-clip":
            media = doc.media.get(child.get("ref") or "")
            inner = media.find("sequence") if media is not None else None
            spine = inner.find("spine") if inner is not None else None
            if spine is not None:
                # The compound's own timeline starts at its tcStart; the
                # in-point into it is the ref-clip's `start`.
                _walk(spine, doc, here,
                      seconds(inner.get("tcStart"), 0.0) + start,
                      child_lane, out, depth + 1)
            continue

        if tag == "gap":
            # No footage of its own, but connected clips hang off it.
            _walk(child, doc, here, start, child_lane, out, depth + 1)
            continue

        stem = doc.assets.get(child.get("ref") or "")
        audio_only = tag == "audio"
        if stem:
            source_in = start
        elif tag in _CONTAINERS:
            found = _primary(child, doc)
            if found is None:
                _walk(child, doc, here, start, child_lane, out, depth + 1)
                continue
            stem, inner_start, inner_offset, audio_only = found
            # The wrapper's `start` is a position in its own local timeline;
            # the inner element says what source sits at that position.
            source_in = start + (inner_start - inner_offset)
        else:
            continue

        if duration > 0:
            mapper = _time_map(child)
            if mapper is not None:
                # With a map, `start` is a position in the retimed timeline and
                # the map says which frame of the source is there.
                source_in, source_out = mapper(start), mapper(start + duration)
            else:
                source_out = source_in + duration
            rate = (source_out - source_in) / duration if duration else 1.0
            out.append(Placement(
                stem=stem,
                name=child.get("name") or stem,
                silent=not _carries_audio(child, doc, stem),
                source_in=source_in,
                source_out=source_out,
                offset=here,
                duration=duration,
                lane=child_lane,
                audio_only=audio_only,
                retimed=abs(rate - 1.0) > 0.001,
            ))

        # Only the connected clips: the primary child is this shot already.
        _walk(child, doc, here, start, child_lane, out, depth + 1,
              anchored_only=True)


def _sequence(root: ET.Element) -> tuple[ET.Element, str]:
    """The sequence to read, and what the project is called."""
    for project in root.iter("project"):
        seq = project.find("sequence")
        if seq is not None:
            return seq, project.get("name") or "Untitled"
    for seq in root.iter("sequence"):
        return seq, "Untitled"
    raise Unreadable(
        "no <project> with a <sequence> in this file -- an event export holds "
        "loose clips, and captions need a timeline. In Final Cut select the "
        "project in the browser, then File > Export XML. In Resolve, "
        "right-click the timeline in the media pool: Timelines > Export > "
        "FCPXML 1.10."
    )


def document(path: Path) -> Path:
    """The XML inside what the editor actually hands you.

    Since 10.6.6 Final Cut's default export is a `.fcpxmld` *bundle* -- a
    directory with `Info.fcpxml` in it -- and in Finder it looks like a single
    file, so the obvious thing to type is the thing that is not a document.
    Both are accepted, because the difference is Final Cut's business and not
    yours. Resolve writes a plain file and needs none of this.
    """
    if path.is_dir():
        inside = path / "Info.fcpxml"
        if inside.is_file():
            return inside
        raise Unreadable(
            f"{path.name} is a folder with no Info.fcpxml in it -- expected a "
            f".fcpxml, or the .fcpxmld bundle Final Cut writes"
        )
    return path


def placements(source: Path | str) -> tuple[list[Placement], str]:
    """Every piece of footage on a Final Cut timeline, in timeline order.

    Takes a path or the document itself, because the browser is handed a
    dropped file and a dropped file has a name but no directory.
    """
    label = source.name if isinstance(source, Path) else "the document"
    if isinstance(source, Path):
        source = document(source)
    try:
        root = (ET.parse(source).getroot() if isinstance(source, Path)
                else ET.fromstring(source))
    except ET.ParseError as exc:
        # Resolve's export menu has OTIO and DRT in it too, and both are picked
        # by mistake because they sit next to FCPXML. Neither is XML, so the
        # parser's "syntax error at line 1" is true and useless.
        hint = ""
        if label.lower().endswith((".otio", ".json", ".drt", ".edl", ".aaf")):
            hint = (
                f" -- {Path(label).suffix} is not FCPXML; export the timeline "
                "as FCPXML 1.10"
            )
        raise Unreadable(f"{label} is not readable as XML: {exc}{hint}") from exc
    if root.tag == "xmeml":
        # Resolve's export menu offers "Final Cut Pro 7 XML" one line above
        # FCPXML, and it is a different format with a different root element --
        # no `<asset>`, no `<media-rep>`, nothing this reader knows. Saying so
        # is the difference between a second export and an evening.
        raise Unreadable(
            f"{label} is Final Cut Pro 7 XML, which is a different format -- "
            "export FCPXML 1.10 instead (Resolve: right-click the timeline, "
            "Timelines > Export > FCPXML 1.10)"
        )
    if root.tag != "fcpxml":
        raise Unreadable(f"{label} is not an FCPXML document")

    doc = _Document(root)
    sequence, name = _sequence(root)
    spine = sequence.find("spine")
    if spine is None:
        raise Unreadable(f"the project {name!r} has no <spine> to read")

    found: list[Placement] = []
    _walk(spine, doc, 0.0, seconds(sequence.get("tcStart"), 0.0), 0, found)

    found.sort(key=lambda p: (p.offset, p.audio_only, p.lane, p.stem))

    # Detaching a shot's audio, or splitting it to ride a level, leaves Final
    # Cut holding the same words twice: once on the picture and once on an
    # audio-only component anchored to it. Captioning both is what turned an
    # 8-shot cut into 74 captions saying "Yes" at each other. The picture wins;
    # an audio-only piece with no picture of its own -- a J-cut, a voiceover --
    # is still the only place those words are said, and is kept.
    pictured = [p for p in found if not p.audio_only]
    def doubled(p: Placement) -> bool:
        return p.audio_only and any(
            q.stem == p.stem and q.offset < p.end and p.offset < q.end
            for q in pictured
        )

    # One `<clip>` can also name its video and its audio separately, and both
    # refer to the same footage at the same moment.
    seen: set[tuple] = set()
    unique = []
    for p in found:
        key = (p.stem, round(p.offset, 3), round(p.source_in, 3))
        if key in seen or doubled(p):
            continue
        seen.add(key)
        unique.append(p)
    return unique, name


@dataclass
class TimelineShot(ResolvedShot):
    """A shot lifted off a Final Cut timeline rather than out of a cut."""

    rate: float = 1.0
    lane: int = 0
    silent: bool = False

    @property
    def lines(self) -> list[Line]:
        """The spoken lines this shot holds, in timeline-relative source time.

        A retimed shot covers more (or less) source than it runs for, so the
        lines are selected over the source it really covers and then mapped
        into the evenly-running space everything downstream assumes. Without
        that a shot slowed to half speed captions its second half twice.
        """
        if not self.sidecar or self.silent:
            return []
        start = self.shot.start
        span = self.shot.duration * self.rate
        spoken = [
            line
            for segment in self.sidecar.segments
            for line in segment.lines
            if line.end > start and line.start < start + span
        ]
        # Segments overlap at their edges, so the same utterance arrives twice.
        seen: set[tuple] = set()
        out: list[Line] = []
        for line in sorted(spoken, key=lambda l: (l.start, l.end)):
            key = (round(line.start, 2), round(line.end, 2), line.en, line.kok)
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
        if abs(self.rate - 1.0) < 0.001:
            return out
        return [
            line.model_copy(update={
                "start": start + (line.start - start) / self.rate,
                "end": start + (line.end - start) / self.rate,
            })
            for line in out
        ]


@dataclass
class TimelineCut(Resolved):
    """What was read off the timeline, and what could not be."""

    project: str = ""
    skipped: list[str] = None  # type: ignore[assignment]

    @property
    def duration(self) -> float:
        """Where the last thing ends -- not the sum, which counts lanes twice."""
        return max((s.offset + s.duration for s in self.shots), default=0.0)


def read(source: Path | str, sidecars: list[Sidecar] | None = None) -> TimelineCut:
    """A Final Cut project as a cut we can caption.

    Footage the archive has never seen -- music, a title card, a clip imported
    straight into Final Cut -- is not an error. It is listed and left alone,
    because a reel scored with a track greels does not know about is the normal
    case, not a broken one.
    """
    found, project = placements(source)
    known = {sc.asset.stem: sc for sc in (sidecars if sidecars is not None
                                          else library.load_all())}

    shots: list[TimelineShot] = []
    skipped: dict[str, int] = {}
    for index, p in enumerate(found):
        sidecar = known.get(p.stem)
        if sidecar is None:
            skipped[p.name or p.stem] = skipped.get(p.name or p.stem, 0) + 1
            continue
        shots.append(TimelineShot(
            shot=Shot(stem=p.stem, start=p.source_in,
                      end=p.source_in + p.duration, note=""),
            index=index,
            sidecar=sidecar,
            segment=None,
            offset=p.offset,
            problem=("retimed -- its captions are spaced evenly and want a look"
                     if p.retimed else None),
            rate=(p.source_out - p.source_in) / p.duration if p.duration else 1.0,
            lane=p.lane,
            silent=p.silent,
        ))

    reel = Reel(slug=_slug(project), title=project,
                shots=[s.shot for s in shots])
    return TimelineCut(
        reel=reel, shots=shots, project=project,
        skipped=[f"{name} x{n}" if n > 1 else name
                 for name, n in sorted(skipped.items())],
    )


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "timeline"
