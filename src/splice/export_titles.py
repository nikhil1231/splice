"""Captions as styled titles, added to a project you already have.

Final Cut's caption renderer draws white text on a solid black slab and offers
almost nothing to change about it. That is not a preference, it is the format:
Apple's own DTD puts `strokeColor`, `shadowColor` and `font` on `text-style`,
and marks `backgroundColor` "for captions". A caption gets a box. The look
everyone means by "like a reel" -- heavy type, white, a hard black outline, a
soft shadow, sitting clear of Instagram's furniture -- is a *title*.

So this writes the same lines a second way, as `<title>` elements. It does not
rebuild the project to do it: the document Final Cut exported is parsed, the
titles are added to that tree, and it is written back out. Everything else --
the colour, the crops, the retimes, the music, the keywords -- is carried
across because it is never re-interpreted, only reprinted.

The result imports as a *second* project sitting next to the first, so the
edit you have keeps working while you look at the one with type on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

from . import config, export_fcpxml, export_reel, import_fcpxml
from .import_fcpxml import Unreadable, seconds

# Final Cut's own plain title, and the one template that is on every machine.
BASIC_TITLE = (
    ".../Titles.localized/Bumper:Opener.localized/"
    "Basic Title.localized/Basic Title.moti"
)


@dataclass(frozen=True)
class Style:
    """How the type looks. The numbers are for a 1080x1920 frame.

    Helvetica Neue Bold because it is on every Mac and is the closest thing
    macOS ships to what Instagram sets its own captions in -- the alternative
    was Arial Bold, which the burnt-in path uses and which reads a little
    wider at this size.
    """

    font: str = "Helvetica Neue"
    face: str = "Bold"
    # The template is authored 1920x1080 and conformed into a 1080x1920
    # project, which scales the type up with it. 38 lands at about the 68 it
    # was drawn for; written as 68 it came out roughly twice that.
    size: int = 38
    colour: str = "1 1 1 1"
    # An outline rather than a box. Negative width is Final Cut's way of
    # saying the stroke sits outside the letterform, which is what keeps it
    # legible over a white sari without thinning the type.
    stroke: str = "0 0 0 1"
    # Final Cut takes the width as given and draws it both sides of the glyph,
    # so 9 on 68pt type met in the middle and rendered every caption as a
    # solid black blob. A reel caption's outline is nearer a tenth of the type.
    stroke_width: int = 3
    shadow: str = "0 0 0 0.75"
    # "distance angle", and 315 is down-and-right.
    shadow_offset: str = "4 315"
    shadow_blur: int = 10
    # **Per cent of the frame, not pixels.** Written as -520 meaning "520px
    # down in a 1920-tall frame", Final Cut's inspector read back -9984 --
    # exactly 19.2x, which is 1920/100. So the unit is hundredths of the frame
    # dimension, and the type had been thrown eight frames below the picture.
    # -27 puts it 27% of the height below centre: a quarter of the way up,
    # clear of the caption and buttons Instagram draws over the bottom sixth.
    position: str = "0 -27"
    line_spacing: int = -8
    # One colour per voice, so a two-hander reads without name labels. Unset,
    # each speaker takes the `colour` the project gives them -- see
    # `speaker_colours`.
    speakers: dict = None  # type: ignore[assignment]


# White for anyone the project gives no colour, and for a line nobody has
# attributed: the default rather than a guess.
WHITE = "1 1 1 1"


def speaker_colours() -> dict[str, str]:
    """One title colour per voice, from splice.yaml.

    Worth keeping pale and high-value rather than saturated: captions sit over
    lit kitchens and white clothes, where a mid-toned colour disappears. And
    whoever is behind the camera asking the questions is best left plain
    white -- the person being watched is the one worth colouring.
    """
    colours = {p.id: p.colour or WHITE for p in config.project().people}
    colours[""] = WHITE
    return colours


def _next_id(root: ET.Element) -> "callable":
    """Ids that cannot collide with the ones Final Cut already wrote."""
    used = {el.get("id") for el in root.iter() if el.get("id")}
    counter = [1]

    def make(prefix: str) -> str:
        while f"{prefix}{counter[0]}" in used:
            counter[0] += 1
        rid = f"{prefix}{counter[0]}"
        used.add(rid)
        return rid

    return make


def anchors(sequence: ET.Element) -> list[tuple[ET.Element, float, float, float]]:
    """Each thing on the spine: (element, where it starts, its own origin, how long).

    A title is connected to the shot it sits over rather than to the first one
    in the cut, because an anchored item's offset is read in its parent's own
    time -- so a title anchored to the wrong shot is a title in the wrong
    place, and one anchored to a shot that ends before it does is a title
    Final Cut has to argue with.
    """
    spine = sequence.find("spine")
    if spine is None:
        raise Unreadable("the project has no <spine> to hang titles on")

    origin = seconds(sequence.get("tcStart"), 0.0)
    out = []
    for child in spine:
        if child.tag in ("transition",) or child.get("lane"):
            continue
        offset = seconds(child.get("offset"), origin)
        duration = seconds(child.get("duration"), 0.0)
        if duration <= 0:
            continue
        out.append((child, offset - origin, seconds(child.get("start"), 0.0), duration))
    return out


def _lane(root: ET.Element) -> int:
    """Above everything already there, so a title never hides behind b-roll."""
    lanes = [int(el.get("lane")) for el in root.iter() if (el.get("lane") or "").lstrip("-").isdigit()]
    return max(lanes + [0]) + 1


# Everything the DTD puts *after* the anchored items. A title appended to the
# end of a clip that carries a keyword range is a document Final Cut refuses
# outright -- Apple's own DTD caught this, which is the whole reason the export
# is held to it.
_AFTER_ANCHORS = {
    "marker", "chapter-marker", "rating", "keyword", "analysis-marker",
    "hidden-clip-marker", "audio-channel-source", "filter-video",
    "filter-video-mask", "filter-audio", "metadata",
}


def _anchor_index(parent: ET.Element) -> int:
    """Where an anchored item has to go: before the first marker or keyword."""
    for i, child in enumerate(parent):
        if child.tag in _AFTER_ANCHORS:
            return i
    return len(parent)


def add_titles(
    tree: ET.ElementTree,
    lines: list[tuple[float, float, str, str]],
    lang: str = "en",
    style: Style | None = None,
    rename: str | None = None,
) -> int:
    """Put one styled title on the timeline per caption. Returns how many."""
    style = style or Style()
    root = tree.getroot()
    sequence, _ = import_fcpxml._sequence(root)
    placed = anchors(sequence)
    if not placed:
        raise Unreadable("nothing on the spine to hang titles on")

    make_id = _next_id(root)
    resources = root.find("resources")
    if resources is None:
        resources = ET.Element("resources")
        root.insert(0, resources)
    effect = ET.SubElement(
        resources, "effect",
        id=make_id("r"), name="Basic Title", uid=BASIC_TITLE,
    )

    lane = str(_lane(root))
    fps = _fps(root, sequence)
    colours = (style.speakers or speaker_colours())
    count = 0
    for row in lines:
        start, end, en, kok = row[0], row[1], row[2], row[3]
        speaker = row[4] if len(row) > 4 else ""
        texts = export_reel.caption_texts(en, kok, lang)
        if not texts or end <= start:
            continue

        # The shot this caption sits over, or the last one that began before
        # it -- a caption in the tail of a cut still has to hang on something.
        anchor, at, local, length = placed[0]
        for candidate in placed:
            if candidate[1] <= start + 1e-6:
                anchor, at, local, length = candidate
        title = ET.Element("title", {
            "ref": effect.get("id"),
            "lane": lane,
            "offset": _time(local + (start - at), fps),
            "name": texts[-1][:60],
            "start": "0s",
            "duration": _time(end - start, fps),
        })
        anchor.insert(_anchor_index(anchor), title)
        style_id = make_id("ts")
        text = ET.SubElement(title, "text")
        ET.SubElement(text, "text-style", ref=style_id).text = "\n".join(texts)
        definition = ET.SubElement(title, "text-style-def", id=style_id)
        ET.SubElement(definition, "text-style", {
            "font": style.font,
            "fontSize": str(style.size),
            "fontFace": style.face,
            "fontColor": colours.get(speaker, style.colour),
            "bold": "1",
            "strokeColor": style.stroke,
            "strokeWidth": str(style.stroke_width),
            "shadowColor": style.shadow,
            "shadowOffset": style.shadow_offset,
            "shadowBlurRadius": str(style.shadow_blur),
            "alignment": "center",
            "lineSpacing": str(style.line_spacing),
        })
        # The DTD wants the intrinsic params after the style definitions.
        ET.SubElement(title, "adjust-transform", position=style.position)
        count += 1

    if rename:
        for project in root.iter("project"):
            project.set("name", rename)
            # The document is a copy, so it arrives carrying the *original*
            # project's uid -- which tells Final Cut these are the same
            # project. It made two separate ones anyway, but a uid that says
            # "overwrite the cut you spent the evening on" is not a thing to
            # leave lying in an export. Dropping it makes Final Cut mint one.
            project.attrib.pop("uid", None)
            project.attrib.pop("modDate", None)
            break
    return count


def _fps(root: ET.Element, sequence: ET.Element) -> str:
    """The sequence's own frame rate, so a title lands on a frame boundary.

    Read off the `<format>` the sequence points at rather than assumed: this
    archive is mostly 30 fps with some 29.97 and one 24, and a title written
    at the wrong rate is one Final Cut refuses on import.
    """
    wanted = sequence.get("format")
    for fmt in root.iter("format"):
        if fmt.get("id") == wanted:
            frame = seconds(fmt.get("frameDuration"), 0.0)
            if frame > 0:
                return str(Fraction(1 / frame).limit_denominator(30000))
    return "30/1"


def _time(value: float, fps: str) -> str:
    from .fcptime import rational

    return rational(max(0.0, value), fps)


def build(
    source: Path | str,
    lang: str = "en",
    style: Style | None = None,
    sidecars: list | None = None,
) -> tuple[ET.ElementTree, int, str]:
    """The project as Final Cut gave it, plus its captions as titles."""
    if isinstance(source, Path):
        source = import_fcpxml.document(source)
        tree = ET.parse(source)
    else:
        tree = ET.ElementTree(ET.fromstring(source))
    cut = import_fcpxml.read(source, sidecars=sidecars)

    count = add_titles(
        tree, export_reel.caption_rows(cut), lang=lang, style=style,
        rename=f"{cut.project} + captions",
    )
    return tree, count, cut.project


def write(
    source: Path,
    dest: Path | None = None,
    lang: str = "en",
    style: Style | None = None,
    sidecars: list | None = None,
) -> tuple[Path, int]:
    """Read a Final Cut project, add its captions as titles, write it back."""
    tree, count, _ = build(source, lang=lang, style=style, sidecars=sidecars)
    dest = dest or source.with_name(f"{source.stem} captions.fcpxml")
    return export_fcpxml.write_validated(tree, dest), count


def to_text(tree: ET.ElementTree) -> str:
    """The same document as a string, held to Apple's DTD on the way through.

    The browser hands the result over as a download, and a document that would
    not import is not worth downloading -- so it is staged and validated
    exactly as a written one is, rather than trusted because it came from here.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return export_fcpxml.write_validated(
            tree, Path(tmp) / "titles.fcpxml"
        ).read_text()
