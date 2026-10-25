"""A reel -> an editable timeline, its captions, and something you can read.

`export_fcpxml` hands the editor *material*: whole clips with their ranges
marked, which is a good browser and not a cut. This hands it a `<project>` -- a
spine of trimmed shots in order, which opens as an editable rough cut on the
timeline. That is the difference between an archive and an edit.

It also carries the level match onto the timeline as a per-clip volume, so the
edit does not open with a fader ride across every join -- pulled down together
by however much the loudest would have clipped, since neither editor has a
limiter on the way in.

The two editors take the lines differently, and `editors.py` says how. Final
Cut takes `<caption>` elements off the spine and puts them in its caption
editor. Resolve ignores them entirely, so its timeline carries a marker per
shot instead -- something to navigate by -- and the `.srt` written beside it
is how the lines get there, as a subtitle track Resolve will actually style.

The awkward part is frame arithmetic across mixed sources. This footage is
mostly 30 fps portrait, with a few 29.97 clips and one 24 fps 720p among them,
so a reel's shots do not all share a timebase. Every position on the timeline
is therefore accumulated in whole *sequence* frames, while each shot's
in-point stays in its own asset's frames. Rounding each offset from seconds
independently leaves one-frame gaps between shots, which Final Cut renders as
black flashes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree as ET

from . import config, editors, export_fcpxml, export_srt
from .editors import Editor
from .export_fcpxml import Resources
from .fcptime import from_frames, frames, rational
from .reel import Reel, Resolved, load, resolve
from .schema import Sidecar


def sequence_format(resolved: Resolved) -> Sidecar | None:
    """The clip whose shape the timeline takes.

    Whichever shape holds the most screen time, so a reel does not become
    720p because a single short 720p shot happened to open it. Ties go to
    first appearance, which keeps the choice stable between runs.
    """
    weight: dict[tuple, float] = defaultdict(float)
    first: dict[tuple, Sidecar] = {}
    for shot in resolved.shots:
        if not shot.sidecar:
            continue
        key = (shot.sidecar.asset.fps, shot.sidecar.asset.width, shot.sidecar.asset.height)
        weight[key] += shot.duration
        first.setdefault(key, shot.sidecar)
    if not weight:
        return None
    best = max(weight, key=lambda k: weight[k])
    return first[best]


def build_document(
    resolved: Resolved,
    event_name: str | None = None,
    raw_dir: Path | None = None,
    captions: str | None = "en",
    gains: dict[str, float] | None = None,
    editor: Editor | str | None = None,
) -> ET.ElementTree:
    """The reel as an FCPXML project. Shots that cannot be resolved are skipped.

    Skipping is safe here and nowhere else: `resolve` has already reported them,
    and a document referring to a clip that is not in `<resources>` is one Final
    Cut refuses outright, taking the good shots with it.
    """
    which = editors.get(editor)
    event_name = event_name or config.event_name()
    usable = [s for s in resolved.shots if s.sidecar and not s.problem]
    root = ET.Element("fcpxml", version=which.fcpxml_version)
    resources = Resources(root, raw_dir)
    resources.declare(resolved.sidecars)

    shape = sequence_format(resolved)
    if shape is None:
        raise ValueError(f"reel {resolved.reel.slug!r} has no usable shots")
    seq_fps = shape.asset.fps
    seq_format = resources.format_for(shape)

    library_el = ET.SubElement(root, "library")
    event = ET.SubElement(library_el, "event", name=event_name)
    project = ET.SubElement(event, "project", name=resolved.reel.title)

    # Lay the spine out first: the sequence needs the total it comes to.
    placements: list[tuple] = []
    cursor = 0  # whole sequence frames
    for shot in usable:
        length = max(1, frames(shot.duration, seq_fps))
        placements.append((shot, cursor, length))
        cursor += length

    sequence = ET.SubElement(
        project,
        "sequence",
        format=seq_format,
        duration=from_frames(cursor, seq_fps),
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate="48k",
    )
    spine = ET.SubElement(sequence, "spine")
    styles = _Styles()

    for shot, offset, length in placements:
        sc = shot.sidecar
        attrs = {
            "ref": resources.assets[sc.asset.stem],
            "name": shot.title[:120] or sc.asset.stem,
            "offset": from_frames(offset, seq_fps),
            # The in-point lives in the source's own frames, not the timeline's.
            "start": rational(shot.start, sc.asset.fps),
            "duration": from_frames(length, seq_fps),
            "format": resources.format_for(sc),
        }
        if sc.asset.has_audio:
            attrs["audioRole"] = "dialogue"
        clip = ET.SubElement(spine, "asset-clip", attrs)
        if shot.shot.note:
            # <note> is the first child the DTD allows, and survives the import
            # as the clip's notes field.
            ET.SubElement(clip, "note").text = shot.shot.note[:500]
        # The same level match the render applies, carried onto the timeline so
        # the edit does not start with a fader ride. The DTD wants it after the
        # note and before the captions, which are anchored items.
        gain = (gains or {}).get(sc.asset.stem, 0.0)
        if sc.asset.has_audio and abs(gain) >= 0.1:
            ET.SubElement(clip, "adjust-volume", amount=f"{gain:.1f}dB")
        if captions and which.clip_captions:
            _caption(clip, shot, captions, styles)
        elif which.marker_ranges:
            # Resolve throws `<caption>` away, so the timeline would arrive
            # with nothing on it saying which shot is which -- twelve
            # near-identical portrait clips of the same room. A marker at the
            # head of each shot is what the running order becomes, and it
            # takes the shot's own title rather than its first line, because
            # the lines are going in as a subtitle track anyway.
            # Anchored to the clip, so both numbers are in the source's own
            # frames -- the same frame of reference as the clip's `start`, not
            # the timeline's.
            ET.SubElement(
                clip, "marker", start=rational(shot.start, sc.asset.fps),
                duration=rational(shot.duration, sc.asset.fps),
                value=(shot.title or sc.asset.stem)[:120],
            )

    tree = export_fcpxml.finish(root)
    _tighten_captions(root)
    return tree


def _tighten_captions(root: ET.Element) -> None:
    """Undo pretty-printing inside <text>.

    `<text>` is mixed content, so the indentation ElementTree adds around its
    `<text-style>` child *is* part of the caption -- every line would import
    with a newline and four spaces around it.
    """
    for element in root.iter("text"):
        element.text = None
        for child in element:
            child.tail = None


# Caption ids have to be unique across the document, and one style definition
# per caption is how Final Cut writes its own.
class _Styles:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"ts{self.n}"


def _caption(clip: ET.Element, shot, lang: str, styles: "_Styles") -> None:
    """Anchor the spoken lines to a shot as Final Cut captions.

    Captions rather than titles: they land in the caption editor, come with a
    language, and burn in or export as a sidecar on the way out -- which is what
    Konkani footage for an English-speaking audience needs. Titles would be
    seventeen text generators to reposition by hand.

    An anchored item's `offset` is measured in its parent's own time, which for
    a clip on the spine is *source* time -- the same frame of reference as the
    clip's `start`, not the timeline's. Getting that wrong stacks every caption
    at the head of the reel.
    """
    fps = shot.sidecar.asset.fps
    role = f"iTT?captionFormat=ITT.{'en' if lang != 'kok' else 'kok'}"
    lines = sorted(shot.lines, key=lambda line: line.start)

    for i, line in enumerate(lines):
        start = max(line.start, shot.start)
        end = min(line.end, shot.end)
        # The model estimates these rather than measuring them, and two of its
        # estimates can overlap. Final Cut refuses captions that sit on top of
        # each other in one role, so the next line's start is the hard limit.
        if i + 1 < len(lines):
            end = min(end, lines[i + 1].start)
        length = frames(end - start, fps)
        if length <= 0:
            continue

        texts = caption_texts(line.en, line.kok, lang)
        if not texts:
            continue

        style_id = styles.next()
        caption = ET.SubElement(
            clip, "caption",
            name=texts[-1][:60],
            lane="1",
            offset=rational(start, fps),
            duration=from_frames(length, fps),
            role=role,
        )
        for text in texts:
            element = ET.SubElement(caption, "text")
            ET.SubElement(element, "text-style", ref=style_id).text = text
        definition = ET.SubElement(caption, "text-style-def", id=style_id)
        ET.SubElement(
            definition, "text-style",
            font="Helvetica Neue", fontSize="13", fontFace="Regular",
            fontColor="1 1 1 1", backgroundColor="0 0 0 0", alignment="center",
        )


# --- the readable outputs ----------------------------------------------


def caption_texts(en: str, kok: str, lang: str) -> list[str]:
    """What a caption says, in the language asked for, without saying it twice.

    Some of this footage is spoken in English -- "Exact time I was looking and
    I was a hero" -- and the romanised Konkani for a line like that is the
    same words. `--lang both` then printed them one above the other, which
    reads as a mistake because it is one.
    """
    if lang == "kok":
        parts = [kok or en]
    elif lang == "both":
        parts = [kok, en] if (kok or "").strip() != (en or "").strip() else [en or kok]
    else:
        parts = [en or kok]
    return [part.strip() for part in parts if part and part.strip()]


class Caption(NamedTuple):
    """One caption on a cut's own timeline, and who says it."""

    start: float
    end: float
    en: str
    kok: str
    speaker: str = ""


def caption_rows(resolved: Resolved) -> list[Caption]:
    """`caption_lines`, but carrying the speaker too.

    Kept separate because everything that draws a caption wants the same four
    values and only the styled titles can do anything with a fifth -- an .srt
    has nowhere to put a speaker, and Final Cut's caption renderer would not
    colour one if it did.
    """
    out: list[Caption] = []
    for shot in resolved.shots:
        for line in shot.lines:
            start = max(0.0, line.start - shot.start) + shot.offset
            end = min(line.end - shot.start, shot.duration) + shot.offset
            out.append(Caption(start, max(end, start + 0.2), line.en, line.kok,
                               getattr(line, "speaker", "")))
    out.sort()
    clipped = [
        row._replace(end=min(row.end, out[i + 1].start) if i + 1 < len(out) else row.end)
        for i, row in enumerate(out)
    ]
    return [row for row in clipped if row.end > row.start]


def caption_lines(resolved: Resolved) -> list[tuple[float, float, str, str]]:
    """Every spoken line, rebased onto the cut: (start, end, en, kok).

    A per-segment timing is no use once the segment has been trimmed and
    moved. Lines that the trim only partly covers are kept and clipped, since
    a half-spoken sentence is still the sentence being said.

    One list, because everything that shows a caption wants the same one: the
    .srt, the browser's WebVTT, and the burnt-in cards.
    """
    return [(row.start, row.end, row.en, row.kok) for row in caption_rows(resolved)]


def srt_of(rows, lang: str = "en") -> str:
    """An .srt body from any set of captions, numbered from one."""
    blocks: list[str] = []
    for row in rows:
        text = "\n".join(caption_texts(row[2], row[3], lang))
        if not text:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n{export_srt.timecode(row[0])} --> "
            f"{export_srt.timecode(row[1])}\n{text.strip()}\n"
        )
    return "\n".join(blocks)


def srt(resolved: Resolved, lang: str = "en") -> str:
    """Captions for the finished reel, timed from its own start."""
    return srt_of(caption_rows(resolved), lang)


def srt_by_speaker(resolved: Resolved, lang: str = "en") -> dict[str, str]:
    """One .srt per voice, because an .srt cannot say who is talking.

    The format is a timecode and a line of text and has nowhere to put a
    speaker -- so an editor that imports .srt can colour a *track* but not a
    line. One file per voice turns that limitation into the answer: import
    them as separate caption tracks and style each once.

    Timings stay absolute, so the tracks lie on top of each other exactly as
    they did in one file. Lines nobody has attributed go to `unattributed`
    rather than being dropped or guessed at.
    """
    groups: dict[str, list] = {}
    for row in caption_rows(resolved):
        groups.setdefault(row.speaker or "unattributed", []).append(row)
    out = {who: srt_of(rows, lang) for who, rows in groups.items()}
    return {who: body for who, body in out.items() if body}


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:d}:{total % 60:02d}"


def script(resolved: Resolved) -> str:
    """The running order as prose: what is on screen and what is being said."""
    reel = resolved.reel
    out = [f"{reel.title}", "=" * len(reel.title), ""]
    if reel.hook:
        out += [f"Hook: {reel.hook}", ""]
    out += [
        f"{len(resolved.shots)} shots from {len(reel.clips)} clips, "
        f"{_clock(resolved.duration)} total",
        "",
    ]
    for shot in resolved.shots:
        head = (
            f"{shot.index + 1:2d}. [{_clock(shot.offset)}] {shot.title}  "
            f"({shot.duration:.1f}s)"
        )
        out.append(head)
        out.append(f"    {shot.shot.stem} {shot.start:.1f}-{shot.end:.1f}")
        if shot.problem:
            out.append(f"    !! {shot.problem}")
        for line in shot.lines:
            if line.en:
                out.append(f"    “{line.en}”")
        out.append("")
    if reel.notes:
        out += ["--", reel.notes, ""]
    return "\n".join(out)


# --- writing -----------------------------------------------------------


def timeline_name(slug: str, editor: Editor | str | None = None) -> str:
    """What the timeline file is called for one editor.

    Final Cut keeps the plain name it has always had. Resolve's is marked,
    because the two documents are not interchangeable and the same cut is
    worth being able to hand to both -- one overwriting the other would mean
    switching editors silently invalidated the file you were about to import.
    """
    which = editors.get(editor)
    return f"{slug}.fcpxml" if which.name == editors.FCP else (
        f"{slug}.{which.name}.fcpxml"
    )


def write(
    reel: Reel,
    dest_dir: Path | None = None,
    lang: str = "en",
    sidecars: list[Sidecar] | None = None,
    captions: bool = True,
    gains: dict[str, float] | None = None,
    editor: Editor | str | None = None,
) -> tuple[Path, Resolved]:
    """Write the timeline, `<slug>.srt` and `<slug>.txt`. Returns the project.

    The FCPXML is validated against Apple's DTD before anything is written --
    the same rule the archive export follows, for the same reason: a file the
    editor rejects is worse than no file, because you find out at import time.
    Resolve's document is held to Apple's 1.10 schema, which is the newest
    both programs speak.
    """
    which = editors.get(editor)
    dest_dir = dest_dir or (config.export_dir() / "reels")
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved = resolve(reel, sidecars)

    project = export_fcpxml.write_validated(
        build_document(
            resolved, captions=lang if captions else None, gains=gains,
            editor=which,
        ),
        dest_dir / timeline_name(reel.slug, which),
    )

    captions = srt(resolved, lang=lang)
    subtitle = dest_dir / f"{reel.slug}.srt"
    if captions:
        subtitle.write_text(captions)
    elif subtitle.exists():
        subtitle.unlink()
    (dest_dir / f"{reel.slug}.txt").write_text(script(resolved))

    # Recorded only now, with the files on disk -- writing an export is the one
    # thing here that justifies saying one happened. Stamped onto the cut as it
    # stands rather than onto the copy this export was handed, so an edit made
    # while it ran is not written over by a version from before it; the sha is
    # still that of what was exported, so the timeline correctly reports itself
    # as behind.
    current = load(reel.slug) or reel
    current.exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    current.exported_sha = reel.content_sha()
    current.exported_editor = which.name
    current.save()
    reel.exported_at, reel.exported_sha = current.exported_at, current.exported_sha
    reel.exported_editor = current.exported_editor

    return project, resolved
