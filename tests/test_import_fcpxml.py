"""Reading a cut back out of Final Cut.

Final Cut is not installed here, so an FCPXML it exported cannot be the
fixture. Two things stand in. The documents splice writes itself are real
FCPXML and go through the real reader, which makes the round trip -- export a
cut, read it back, get the same captions -- the strongest check available
without the application. The rest are hand-built spines holding the shapes
Final Cut writes and splice never does: connected clips, gaps, compounds,
retimes, and footage the archive has never seen.
"""

from xml.etree import ElementTree as ET

import pytest

from splice import export_reel, import_fcpxml, reel
from splice.schema import Asset, Line, Segment, Sidecar


@pytest.fixture
def sidecars():
    """Two clips, each with lines on known round seconds."""
    return [
        Sidecar(
            asset=Asset(file="talk.MOV", stem="talk", duration=60.0, fps="30",
                        width=1080, height=1920),
            segments=[Segment(id="talk#0", start=0, end=60, title="Kitchen", lines=[
                Line(start=0, end=2, en="one", kok="ek"),
                Line(start=10, end=12, en="two", kok="don"),
                Line(start=30, end=32, en="three", kok="teen"),
                Line(start=50, end=52, en="four", kok="chaar"),
            ])],
        ),
        Sidecar(
            asset=Asset(file="birds.MOV", stem="birds", duration=40.0, fps="30",
                        width=1080, height=1920),
            segments=[Segment(id="birds#0", start=0, end=40, title="Balcony", lines=[
                Line(start=5, end=7, en="crow", kok="kaulo"),
            ])],
        ),
    ]


def _doc(spine_xml: str, extra_assets: str = "") -> ET.ElementTree:
    """An FCPXML project whose spine is whatever the test needs."""
    return ET.ElementTree(ET.fromstring(f"""
      <fcpxml version="1.11">
        <resources>
          <format id="r1" frameDuration="100/3000s" width="1080" height="1920"/>
          <asset id="r2" name="talk" start="0s" duration="1800/30s" hasVideo="1"
                 hasAudio="1" audioSources="1" audioChannels="2">
            <media-rep kind="original-media" src="file:///raw/talk.MOV"/>
          </asset>
          <asset id="r3" name="birds" start="0s" duration="1200/30s" hasVideo="1"
                 hasAudio="1" audioSources="1" audioChannels="2">
            <media-rep kind="original-media" src="file:///raw/birds.MOV"/>
          </asset>
          {extra_assets}
        </resources>
        <library><event name="E"><project name="Ep1">
          <sequence format="r1" tcStart="0s" duration="3000/30s">
            <spine>{spine_xml}</spine>
          </sequence>
        </project></event></library>
      </fcpxml>"""))


def _read(tmp_path, spine_xml, sidecars, extra_assets=""):
    path = tmp_path / "Ep1.fcpxml"
    _doc(spine_xml, extra_assets).write(path)
    return import_fcpxml.read(path, sidecars=sidecars)


def _captions(cut):
    return [(round(a, 3), round(b, 3), en) for a, b, en, _ in
            export_reel.caption_lines(cut)]


def test_a_shot_is_captioned_where_the_timeline_puts_it_not_where_the_source_did(
    tmp_path, sidecars
):
    """The whole point. A line spoken 30s into the clip, used in a shot that
    sits 2s into the cut, is a caption at 2s -- and a running order Final Cut
    decided is only knowable from the offsets it writes down."""
    cut = _read(tmp_path, """
      <asset-clip ref="r3" offset="0s" name="birds" start="150/30s" duration="60/30s"/>
      <asset-clip ref="r2" offset="60/30s" name="talk" start="900/30s" duration="120/30s"/>
    """, sidecars)
    assert _captions(cut) == [(0.0, 2.0, "crow"), (2.0, 4.0, "three")]


def test_the_captions_survive_a_round_trip_through_a_real_document(tmp_path, sidecars):
    """Export a cut to FCPXML and read it straight back. Anything the writer
    and the reader disagree about shows up here as a caption that moved --
    which is the failure the offsets are easy to get wrong in both directions."""
    cut = reel.Reel(slug="r", title="R", shots=[
        reel.Shot(stem="talk", start=8.0, end=14.0),
        reel.Shot(stem="birds", start=4.0, end=9.0),
        reel.Shot(stem="talk", start=28.0, end=34.0),
    ])
    resolved = reel.resolve(cut, sidecars=sidecars)
    path = tmp_path / "r.fcpxml"
    export_reel.build_document(resolved, raw_dir=tmp_path).write(path)

    back = import_fcpxml.read(path, sidecars=sidecars)
    assert _captions(back) == _captions(resolved)
    assert back.project == "R"


def test_a_connected_clips_offset_is_read_in_its_parents_time(tmp_path, sidecars):
    """An anchored item's offset is measured in the clip it hangs off, in that
    clip's *source* time. Reading it as a timeline position stacks every
    connected shot at the head of the cut -- the same trap the writer has."""
    cut = _read(tmp_path, """
      <asset-clip ref="r2" offset="0s" name="talk" start="0s" duration="1200/30s">
        <asset-clip ref="r3" lane="1" offset="600/30s" name="birds"
                    start="150/30s" duration="60/30s"/>
      </asset-clip>
    """, sidecars)
    anchored = [s for s in cut.shots if s.lane == 1]
    assert len(anchored) == 1
    # 600/30s into the parent, whose own start is 0 -> 20s along the timeline.
    assert anchored[0].offset == pytest.approx(20.0)
    assert (20.0, 22.0, "crow") in _captions(cut)


def test_two_clips_talking_at_once_leave_one_caption(tmp_path, sidecars):
    """A connected shot laid over someone mid-sentence is a real edit, and a
    caption track cannot hold both. The existing rule -- the next line's start
    is the hard limit -- resolves it, and what must not happen is two captions
    on top of each other, which Final Cut refuses on import."""
    cut = _read(tmp_path, """
      <asset-clip ref="r2" offset="0s" name="talk" start="0s" duration="1200/30s">
        <asset-clip ref="r3" lane="1" offset="300/30s" name="birds"
                    start="150/30s" duration="60/30s"/>
      </asset-clip>
    """, sidecars)
    at_ten = [c for c in _captions(cut) if c[0] == 10.0]
    assert len(at_ten) == 1
    starts = [c[0] for c in _captions(cut)]
    assert starts == sorted(starts)
    assert all(b <= c for (_, b, _), (c, _, _) in
               zip(_captions(cut), _captions(cut)[1:]))


def test_a_gap_pushes_what_follows_it_along(tmp_path, sidecars):
    """Final Cut writes a gap as a real spine element. Skipping it without
    reading its offset would pull every later shot forward on top of it."""
    cut = _read(tmp_path, """
      <gap offset="0s" name="Gap" start="0s" duration="90/30s"/>
      <asset-clip ref="r3" offset="90/30s" name="birds" start="150/30s" duration="60/30s"/>
    """, sidecars)
    assert _captions(cut) == [(3.0, 5.0, "crow")]


def test_footage_the_archive_has_never_seen_is_named_and_not_an_error(tmp_path, sidecars):
    """A cut scored with a music track is the normal case. Nothing can be said
    for it, and refusing to caption the rest because of it would be absurd."""
    cut = _read(tmp_path, """
      <asset-clip ref="r3" offset="0s" name="birds" start="150/30s" duration="60/30s"/>
      <asset-clip ref="r9" offset="0s" lane="-2" name="ryoish-days" start="0s"
                  duration="900/30s"/>
    """, sidecars, extra_assets="""
      <asset id="r9" name="ryoish-days" start="0s" duration="6000/30s" hasAudio="1">
        <media-rep kind="original-media" src="file:///sounds/ryoish-days.mp3"/>
      </asset>""")
    assert cut.skipped == ["ryoish-days"]
    assert _captions(cut) == [(0.0, 2.0, "crow")]


def test_a_retimed_shot_spreads_its_lines_over_the_time_it_really_runs(tmp_path, sidecars):
    """A shot slowed to half speed covers half the source in twice the time.
    Timing its captions off the source range would run them out before the
    shot ends and caption the same words twice."""
    cut = _read(tmp_path, """
      <asset-clip ref="r2" offset="0s" name="talk" start="0s" duration="1200/30s">
        <timeMap>
          <timept time="0s" value="0s" interp="linear"/>
          <timept time="1200/30s" value="600/30s" interp="linear"/>
        </timeMap>
      </asset-clip>
    """, sidecars)
    # 40s of timeline covering 0-20s of source: "one" at 0-2 stays, "two" at
    # 10-12 lands at 20-24, and "three" at 30s is past the end of the source.
    assert _captions(cut) == [(0.0, 4.0, "one"), (20.0, 24.0, "two")]
    assert cut.problems and "retimed" in cut.problems[0]


def test_a_compound_clip_is_opened_rather_than_passed_over(tmp_path, sidecars):
    """Shots nested into a compound are still shots, and a cut built that way
    would otherwise come back with nothing said in it at all."""
    cut = _read(tmp_path, """
      <ref-clip ref="r8" offset="60/30s" name="Nested" start="0s" duration="120/30s"/>
    """, sidecars, extra_assets="""
      <media id="r8" name="Nested">
        <sequence format="r1" tcStart="0s" duration="120/30s">
          <spine>
            <asset-clip ref="r2" offset="0s" name="talk" start="900/30s" duration="120/30s"/>
          </spine>
        </sequence>
      </media>""")
    assert _captions(cut) == [(2.0, 4.0, "three")]


def test_an_event_export_says_what_it_is_missing(tmp_path, sidecars):
    """Exporting the wrong thing in Final Cut -- the event, not the project --
    gives a valid FCPXML with no timeline in it. That is a mistake worth
    naming, since the fix is four clicks away and not in this program."""
    path = tmp_path / "event.fcpxml"
    path.write_text('<?xml version="1.0"?><fcpxml version="1.11">'
                    '<library><event name="E"/></library></fcpxml>')
    with pytest.raises(import_fcpxml.Unreadable, match="File > Export XML"):
        import_fcpxml.read(path, sidecars=sidecars)


# --- the shapes Final Cut writes and splice never does ----------------------
#
# Everything above this line passed while the reader was badly wrong, because
# the documents it builds are the ones splice writes: a flat spine of
# `<asset-clip>`. A real export of a real cut wraps shots in `<clip>`, detaches
# audio under a `<gap>`, and writes a `timeMap` on anything that has been near
# the retime editor. These are taken from Ep1's own export.


def test_a_wrapped_shot_is_the_wrapper_and_not_the_video_inside_it(tmp_path, sidecars):
    """Final Cut wraps a shot in a `<clip>` carrying the trim, and puts a
    `<video>` inside it that spans the whole asset. Reading the inner element
    as the shot gives you the entire clip, every line in it, at an offset that
    can be negative -- Ep1 came back 38.9s long with 74 captions in it."""
    cut = _read(tmp_path, """
      <clip offset="0s" name="talk" start="900/30s" duration="120/30s">
        <video ref="r2" offset="0s" duration="1800/30s"/>
        <audio ref="r2" offset="0s" duration="1800/30s" role="dialogue"/>
      </clip>
    """, sidecars)

    assert len(cut.shots) == 1
    assert cut.shots[0].offset == 0.0
    assert (cut.shots[0].shot.start, cut.shots[0].shot.end) == (30.0, 34.0)
    assert _captions(cut) == [(0.0, 2.0, "three")]


def test_detached_audio_does_not_say_everything_a_second_time(tmp_path, sidecars):
    """Detaching a shot's audio, or splitting it to ride a level, leaves the
    same words on an audio-only component anchored to the picture. Captioning
    both is what had Ep1 saying "Yes" at itself eleven times."""
    cut = _read(tmp_path, """
      <clip offset="0s" name="talk" start="900/30s" duration="120/30s">
        <video ref="r2" offset="0s" duration="1800/30s"/>
        <clip lane="-1" offset="900/30s" name="talk" start="900/30s" duration="120/30s">
          <gap name="Gap" offset="0s" duration="1800/30s">
            <audio ref="r2" lane="-1" offset="0s" duration="1800/30s" role="dialogue"/>
          </gap>
        </clip>
      </clip>
    """, sidecars)

    assert _captions(cut) == [(0.0, 2.0, "three")]


def test_a_picture_only_cutaway_says_nothing(tmp_path, sidecars):
    """A cutaway laid over someone still talking holds a `<video>` and nothing
    else, so its own dialogue is not in the mix. Captioning it puts words on
    screen nobody is saying and cuts the sentence underneath in half."""
    cut = _read(tmp_path, """
      <asset-clip ref="r2" offset="0s" name="talk" start="0s" duration="600/30s">
        <clip lane="1" offset="150/30s" name="cutaway" start="900/30s" duration="60/30s">
          <video ref="r2" offset="0s" duration="1800/30s"/>
        </clip>
      </asset-clip>
    """, sidecars)

    cutaway = [s for s in cut.shots if s.lane == 1]
    assert cutaway and cutaway[0].silent and cutaway[0].lines == []
    # The line underneath it survives intact rather than being cut in two.
    assert (10.0, 12.0, "two") in _captions(cut)


def test_an_identity_time_map_is_not_a_retime(tmp_path, sidecars):
    """Final Cut writes a `timeMap` for any clip that has been near the retime
    editor, and both of Ep1's are identity. Reading the values as a span over
    the clip's duration made one look like a 14x speed-up, which dragged nine
    lines into a two-second shot."""
    cut = _read(tmp_path, """
      <clip offset="0s" name="talk" start="900/30s" duration="120/30s">
        <timeMap>
          <timept time="0s" value="0s" interp="linear"/>
          <timept time="1800/30s" value="1800/30s" interp="linear"/>
        </timeMap>
        <video ref="r2" offset="0s" duration="1800/30s"/>
        <audio ref="r2" offset="0s" duration="1800/30s" role="dialogue"/>
      </clip>
    """, sidecars)

    assert cut.problems == []
    assert _captions(cut) == [(0.0, 2.0, "three")]


def test_the_bundle_final_cut_actually_exports_is_read(tmp_path, sidecars):
    """Since 10.6.6 the default export is a `.fcpxmld` bundle -- a folder with
    Info.fcpxml in it. Finder shows it as one file, so the obvious thing to
    hand over is the thing that is not a document."""
    bundle = tmp_path / "Ep1.fcpxmld"
    bundle.mkdir()
    _doc('<asset-clip ref="r3" offset="0s" name="birds" start="150/30s"'
         ' duration="60/30s"/>').write(bundle / "Info.fcpxml")

    assert _captions(import_fcpxml.read(bundle, sidecars=sidecars)) == \
        [(0.0, 2.0, "crow")]


def test_a_folder_that_is_not_a_bundle_says_so(tmp_path, sidecars):
    with pytest.raises(import_fcpxml.Unreadable, match="Info.fcpxml"):
        import_fcpxml.read(tmp_path, sidecars=sidecars)


def test_an_srt_cannot_say_who_is_talking_so_there_is_one_per_voice(tmp_path, sidecars):
    """The format is a timecode and a line of text and has nowhere to put a
    speaker, so an editor that imports .srt can colour a track but not a line.
    One file per voice makes that limitation the answer -- and between them the
    files have to hold every caption exactly once, at the same times."""
    for line in sidecars[0].segments[0].lines:
        line.speaker = "grandma" if line.start >= 10 else "nikhil"
    sidecars[1].segments[0].lines[0].speaker = ""

    cut = _read(tmp_path, """
      <asset-clip ref="r2" offset="0s" name="talk" start="0s" duration="1800/30s"/>
      <asset-clip ref="r3" offset="1800/30s" name="birds" start="0s" duration="600/30s"/>
    """, sidecars)
    parts = export_reel.srt_by_speaker(cut)

    assert set(parts) == {"grandma", "nikhil", "unattributed"}
    # Nothing is dropped and nothing is said twice.
    assert sum(body.count(" --> ") for body in parts.values()) \
        == export_reel.srt(cut).count(" --> ")
    # Each file is numbered from one, or players show the first caption only.
    for body in parts.values():
        assert body.startswith("1\n")


# --- reading a cut back out of Resolve ---------------------------------
#
# Resolve writes a different dialect of the same format: the timeline starts at
# an hour rather than at zero, a shot is a `<clip>` wrapping a `<video>` rather
# than a bare `<asset-clip>`, and nothing it writes is called `.fcpxmld`. These
# are the shapes that dialect brings, built by hand because Resolve cannot be
# run inside a test.


def _resolve_doc(spine_xml: str, tc_start: str = "3600s") -> str:
    """A project the way Resolve exports one, at FCPXML 1.10."""
    return f"""
      <fcpxml version="1.10">
        <resources>
          <format id="r1" frameDuration="100/3000s" width="1080" height="1920"/>
          <asset id="r2" name="talk" start="0s" duration="1800/30s" hasVideo="1"
                 hasAudio="1" audioSources="1" audioChannels="2">
            <media-rep kind="original-media" src="file:///raw/talk.MOV"/>
          </asset>
          <asset id="r3" name="birds" start="0s" duration="1200/30s" hasVideo="1"
                 hasAudio="1" audioSources="1" audioChannels="2">
            <media-rep kind="original-media" src="file:///raw/birds.MOV"/>
          </asset>
        </resources>
        <library><event name="E"><project name="Ep1">
          <sequence format="r1" tcStart="{tc_start}" duration="3000/30s">
            <spine>{spine_xml}</spine>
          </sequence>
        </project></event></library>
      </fcpxml>"""


def test_a_resolve_timeline_that_starts_at_an_hour_still_starts_the_cut_at_zero(
    tmp_path, sidecars
):
    """Resolve's timelines conventionally begin at 01:00:00:00.

    Reading the offsets as absolute would put every caption an hour into a
    33-second episode, which is not an error anybody would see until they
    imported the .srt and found it empty.
    """
    path = tmp_path / "Ep1.fcpxml"
    path.write_text(_resolve_doc("""
      <clip name="birds" offset="3600s" start="150/30s" duration="60/30s" format="r1">
        <video ref="r3" offset="0s" start="0s" duration="1200/30s"/>
        <audio ref="r3" offset="0s" start="0s" duration="1200/30s" role="dialogue"/>
      </clip>
      <clip name="talk" offset="108060/30s" start="900/30s" duration="120/30s" format="r1">
        <video ref="r2" offset="0s" start="0s" duration="1800/30s"/>
        <audio ref="r2" offset="0s" start="0s" duration="1800/30s" role="dialogue"/>
      </clip>
    """))
    cut = import_fcpxml.read(path, sidecars=sidecars)
    assert cut.project == "Ep1"
    assert _captions(cut) == [(0.0, 2.0, "crow"), (2.0, 4.0, "three")]


def test_the_resolve_timeline_splice_writes_reads_back_as_the_cut_it_was(
    tmp_path, sidecars
):
    """The round trip, for the second editor.

    The Resolve document carries markers where the Final Cut one carries
    captions, and a marker sitting where a caption used to must not be read as
    a shot -- eight markers would become eight phantom pieces of footage.
    """
    cut = reel.Reel(slug="r", title="R", shots=[
        reel.Shot(stem="talk", start=8.0, end=14.0),
        reel.Shot(stem="birds", start=4.0, end=9.0),
        reel.Shot(stem="talk", start=28.0, end=34.0),
    ])
    resolved = reel.resolve(cut, sidecars=sidecars)
    path = tmp_path / "r.resolve.fcpxml"
    export_reel.build_document(
        resolved, raw_dir=tmp_path, editor="resolve"
    ).write(path)

    back = import_fcpxml.read(path, sidecars=sidecars)
    assert len(back.shots) == len(resolved.shots)
    assert _captions(back) == _captions(resolved)


def test_the_fcp7_xml_resolve_also_offers_is_named_rather_than_called_broken(
    tmp_path
):
    """It sits one line above FCPXML in Resolve's export menu.

    "not an FCPXML document" is true and sends you looking for a corrupt file
    rather than back to the menu.
    """
    path = tmp_path / "Ep1.xml"
    path.write_text('<xmeml version="5"><sequence/></xmeml>')
    with pytest.raises(import_fcpxml.Unreadable) as caught:
        import_fcpxml.read(path)
    assert "Final Cut Pro 7 XML" in str(caught.value)
    assert "FCPXML 1.10" in str(caught.value)


def test_an_otio_export_says_what_it_is_instead_of_a_parser_error(tmp_path):
    """OTIO and DRT sit beside FCPXML in the same menu, and are not XML.

    "syntax error: line 1, column 0" is what the parser says about a JSON file
    and tells nobody which menu item they wanted.
    """
    path = tmp_path / "Ep1.otio"
    path.write_text('{"OTIO_SCHEMA": "Timeline.1"}')
    with pytest.raises(import_fcpxml.Unreadable) as caught:
        import_fcpxml.read(path)
    assert "not FCPXML" in str(caught.value)


def test_an_event_export_names_both_menus(tmp_path, sidecars):
    """The commonest mistake, and it is made in both programs.

    Naming only Final Cut's menu leaves a Resolve user reading an instruction
    for an application they no longer have.
    """
    path = tmp_path / "Ep1.fcpxml"
    path.write_text(
        '<fcpxml version="1.10"><resources/>'
        '<library><event name="E"><asset-clip name="talk"/></event></library>'
        "</fcpxml>"
    )
    with pytest.raises(import_fcpxml.Unreadable) as caught:
        import_fcpxml.read(path, sidecars=sidecars)
    assert "File > Export XML" in str(caught.value)
    assert "FCPXML 1.10" in str(caught.value)
