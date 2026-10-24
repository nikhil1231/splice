"""Captions as titles, which is the only way to get the look.

Final Cut's caption renderer draws white type on a black slab. That is the
format rather than a setting: Apple's DTD puts `backgroundColor` on a caption
and `strokeColor`, `shadowColor` and `font` on a title, and there is no third
option. So the reel look is titles, and these check the two things that makes
dangerous -- that the document is still the one Final Cut exported, and that
every title sits exactly where its caption did.

Apple's DTD is the oracle where Final Cut is installed; `test_export.py` says
why it cannot be relied on everywhere, so the structural assertions here stand
on their own.
"""

from xml.etree import ElementTree as ET

import pytest

from greels import export_reel, export_titles, import_fcpxml
from greels.schema import Asset, Line, Segment, Sidecar


@pytest.fixture
def sidecars():
    return [Sidecar(
        asset=Asset(file="talk.MOV", stem="talk", duration=60.0, fps="30",
                    width=1080, height=1920),
        segments=[Segment(id="talk#0", start=0, end=60, title="Kitchen", lines=[
            Line(start=0, end=2, en="one", kok="ek"),
            Line(start=10, end=12, en="two", kok="don"),
            Line(start=30, end=32, en="three", kok="teen"),
        ])],
    )]


# A shot trimmed thirty seconds in, with a keyword range on it -- which is what
# makes the ordering rule below bite.
DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
  <fcpxml version="1.11">
    <resources>
      <format id="r1" frameDuration="100/3000s" width="1080" height="1920"/>
      <asset id="r2" name="talk" start="0s" duration="1800/30s" hasVideo="1"
             hasAudio="1" audioSources="1">
        <media-rep kind="original-media" src="file:///raw/talk.MOV"/>
      </asset>
    </resources>
    <library><event name="E"><project name="Ep1" uid="76332754-F636-423D-8A95-0A20ABE29C48">
      <sequence format="r1" tcStart="0s" duration="600/30s">
        <spine>
          <asset-clip ref="r2" offset="0s" name="talk" start="0s" duration="450/30s">
            <keyword start="0s" duration="16s" value="kitchen"/>
            <marker start="0s" duration="100/3000s" value="frying"/>
          </asset-clip>
          <clip offset="450/30s" name="talk" start="900/30s" duration="90/30s">
            <video ref="r2" offset="0s" duration="1800/30s"/>
            <audio ref="r2" offset="0s" duration="1800/30s" role="dialogue"/>
            <keyword start="0s" duration="16s" value="kitchen"/>
          </clip>
        </spine>
      </sequence>
    </project></event></library>
  </fcpxml>"""


def _built(sidecars, lang="en"):
    return export_titles.build(DOCUMENT, lang=lang, sidecars=sidecars)


def _timeline_positions(root):
    """Where each title actually lands, read back the way the reader reads it."""
    sequence, _ = import_fcpxml._sequence(root)
    out = []
    for el, at, local, _ in export_titles.anchors(sequence):
        for title in el.findall("title"):
            offset = import_fcpxml.seconds(title.get("offset"))
            out.append((round(at + (offset - local), 3),
                        round(import_fcpxml.seconds(title.get("duration")), 3),
                        title.get("name")))
    return sorted(out)


def test_every_title_lands_exactly_where_its_caption_did(sidecars):
    """A title is anchored to the shot it sits over, and an anchored item's
    offset is read in its parent's *source* time -- so the arithmetic that puts
    it there is the same one that reads it back, and has to agree with itself."""
    tree, count, project = _built(sidecars)
    wanted = export_reel.caption_lines(
        import_fcpxml.read(DOCUMENT, sidecars=sidecars))

    assert count == len(wanted) == 3, "a vacuous pass is the trap here"
    assert project == "Ep1"
    assert _timeline_positions(tree.getroot()) == sorted(
        (round(start, 3), round(end - start, 3), en)
        for start, end, en, _ in wanted
    )


def test_an_anchored_title_goes_before_the_keywords_and_not_after(sidecars):
    """Apple's DTD puts anchored items before the marker group, and a title
    appended to the end of a clip that carries a keyword range is a document
    Final Cut refuses outright -- taking the whole import with it."""
    tree, _, _ = _built(sidecars)
    for clip in tree.getroot().iter():
        tags = [child.tag for child in clip]
        if "title" not in tags:
            continue
        last_title = len(tags) - 1 - tags[::-1].index("title")
        after = {"marker", "keyword", "rating", "metadata"}
        assert not any(tag in after for tag in tags[:last_title]), tags


def test_the_edit_itself_comes_back_unchanged(sidecars):
    """The document is Final Cut's own, added to rather than rebuilt: a cut
    round-tripped through a model of it loses whatever the model has no field
    for, which here would be the colour, the crops and the volume curves."""
    before = import_fcpxml.read(DOCUMENT, sidecars=sidecars)
    tree, _, _ = _built(sidecars)
    after = import_fcpxml.read(ET.tostring(tree.getroot(), encoding="unicode"),
                               sidecars=sidecars)

    assert [(s.shot.stem, s.offset, s.shot.start, s.shot.end) for s in after.shots] \
        == [(s.shot.stem, s.offset, s.shot.start, s.shot.end) for s in before.shots]
    assert after.duration == before.duration
    # and the things this program has no model for at all
    assert [k.get("value") for k in tree.getroot().iter("keyword")] \
        == ["kitchen", "kitchen"]


def test_a_title_carries_the_outline_a_caption_cannot(sidecars):
    """The whole reason this exists. A caption gets `backgroundColor` and a
    black slab; a title gets a stroke and a shadow, which is what reads over a
    white sari against a window without a box around it."""
    tree, _, _ = _built(sidecars)
    style = next(tree.getroot().iter("text-style-def")).find("text-style")

    assert style.get("strokeColor") == "0 0 0 1"
    assert style.get("fontColor") == "1 1 1 1"
    assert style.get("shadowColor")
    assert style.get("backgroundColor") is None
    # Final Cut draws the stroke both sides of the glyph, so a wide one meets
    # in the middle and renders the caption as a solid black blob. Read back
    # out of the library, a width of 9 on 68pt type had done exactly that.
    assert 0 < int(style.get("strokeWidth")) <= 0.15 * int(style.get("fontSize"))


def test_the_titles_sit_above_everything_already_on_the_timeline(sidecars):
    """A title on the lane a cutaway already occupies is a title behind it."""
    tree, _, _ = _built(sidecars)
    lanes = [int(t.get("lane")) for t in tree.getroot().iter("title")]
    others = [int(el.get("lane")) for el in tree.getroot().iter()
              if el.tag != "title" and (el.get("lane") or "").lstrip("-").isdigit()]
    assert lanes and all(lane > max(others + [0]) for lane in lanes)


def test_both_languages_go_on_one_title(sidecars):
    """One title per caption, not two stacked on each other."""
    tree, count, _ = _built(sidecars, lang="both")
    texts = [t.text for t in tree.getroot().iter("text-style") if t.text]

    assert len(texts) == count
    assert "ek\none" in texts


def test_a_project_with_nothing_on_its_spine_says_so(sidecars):
    empty = DOCUMENT.replace(
        DOCUMENT[DOCUMENT.index("<spine>"):DOCUMENT.index("</spine>")], "<spine>")
    with pytest.raises(import_fcpxml.Unreadable, match="spine"):
        export_titles.build(empty, sidecars=sidecars)


def test_the_document_is_held_to_apples_dtd_when_final_cut_is_here(sidecars):
    """The only real oracle available, and it has already earned its keep --
    it caught the anchored-item ordering above before Final Cut could."""
    if export_titles.export_fcpxml.dtd_path("1.11") is None:
        pytest.skip("Final Cut is not installed, so its DTD cannot judge this")
    tree, _, _ = _built(sidecars)
    assert "<title" in export_titles.to_text(tree)


def test_the_copy_does_not_claim_to_be_the_project_it_came_from(sidecars):
    """The document is Final Cut's own, so it arrives carrying the original
    project's uid -- which says "these are the same project". Final Cut made
    two anyway, but a uid pointing at the cut somebody spent the evening on is
    not a thing to leave in an export that gets imported twice."""
    tree, _, _ = _built(sidecars)
    project = next(tree.getroot().iter("project"))

    assert project.get("name") == "Ep1 + captions"
    assert project.get("uid") is None


def test_the_titles_sit_on_the_picture_and_not_below_it(sidecars):
    """`adjust-transform position` is in hundredths of the frame, not pixels.

    Written as -520, meaning 520px down a 1920-tall frame, Final Cut's own
    inspector read back **-9984** -- exactly 19.2x, which is 1920/100. The type
    was eight frame-heights below the picture, which looks precisely like a
    title that failed to import. Anything outside +/-50 is off the frame.
    """
    tree, _, _ = _built(sidecars)
    positions = [t.get("position") for t in tree.getroot().iter("adjust-transform")]

    assert positions
    for position in positions:
        x, y = (float(v) for v in position.split())
        assert abs(x) <= 50 and abs(y) <= 50, position
        assert y < 0, "captions sit below centre, not above it"
