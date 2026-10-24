"""What a cut has to survive.

A reel is the only thing here that is purely a decision -- nothing can
regenerate a running order -- so the tests that matter are about it not
quietly losing shots, and about the timeline arithmetic that decides whether
Final Cut shows black between them.
"""

from fractions import Fraction

import pytest

from greels import export_reel, reel as store
from greels.fcptime import timebase
from greels.reel import Reel, Shot
from greels.schema import Asset, Line, Segment, Sidecar


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    (tmp_path / "index").mkdir()
    return tmp_path


def _sidecar(stem, duration=120.0, fps="30", width=1080, height=1920, segments=()):
    return Sidecar(
        asset=Asset(file=f"{stem}.MOV", stem=stem, duration=duration, fps=fps,
                    width=width, height=height),
        segments=list(segments),
    )


@pytest.fixture
def footage():
    return [
        _sidecar("a", segments=[
            Segment(id="a#00000.00", start=0, end=30, title="Marriage chat", lines=[
                Line(start=2, end=5, kok="Lagn zalato?", en="Are you married?"),
                Line(start=20, end=24, en="Sixty years ago."),
            ]),
        ]),
        _sidecar("b", duration=90.0, segments=[
            Segment(id="b#00010.00", start=10, end=40, title="Rs 7 train"),
        ]),
        _sidecar("c", duration=20.0, fps="24", width=1280, height=720, segments=[
            Segment(id="c#00000.00", start=0, end=8, title="Router lights"),
        ]),
    ]


def _reel(*shots, title="Test reel"):
    return Reel(slug="test", title=title, shots=list(shots))


# --- staying attached to the footage -----------------------------------


def test_a_shot_survives_its_segment_being_renumbered(footage):
    """Re-analysis moves a boundary, so `a#00000.00` becomes `a#00002.00`.

    Segment ids are derived from their own start time. A cut that depended on
    them would silently empty itself the first time the prompt changed.
    """
    reel = _reel(Shot(stem="a", start=4.0, end=12.0, segment_id="a#00000.00"))
    renumbered = [
        _sidecar("a", segments=[
            Segment(id="a#00002.00", start=2, end=30, title="Marriage chat"),
        ])
    ]
    resolved = store.resolve(reel, renumbered)

    assert resolved.problems == []
    assert resolved.duration == 8.0
    # And it finds the segment that now covers the moment, so the shot keeps
    # its title and its dialogue.
    assert resolved.shots[0].segment.id == "a#00002.00"
    assert resolved.shots[0].title == "Marriage chat"


def test_a_shot_whose_clip_is_gone_is_reported_not_dropped(footage):
    reel = _reel(
        Shot(stem="a", start=0, end=10),
        Shot(stem="deleted", start=0, end=5),
    )
    resolved = store.resolve(reel, footage)

    assert len(resolved.shots) == 2
    assert "no clip named deleted" in resolved.problems[0]


def test_a_shot_past_the_end_of_a_replaced_file_is_reported(footage):
    reel = _reel(Shot(stem="c", start=10, end=45))  # c is 20s long
    assert "shot runs to 45.0s" in store.resolve(reel, footage).problems[0]


def test_a_broken_shot_is_kept_out_of_the_timeline(footage):
    """Reported by `resolve`, then skipped by the export.

    A document referring to a clip that is not in <resources> is one Final Cut
    refuses outright, which would take the good shots down with it.
    """
    reel = _reel(
        Shot(stem="a", start=0, end=10),
        Shot(stem="deleted", start=0, end=5),
    )
    root = export_reel.build_document(store.resolve(reel, footage)).getroot()
    assert len(root.find(".//spine")) == 1


def test_a_trim_belongs_to_the_reel_not_the_segment(footage):
    """Tightening a shot must not edit the archive's account of what was said."""
    reel = _reel(Shot(stem="a", start=6.0, end=9.0, segment_id="a#00000.00"))
    resolved = store.resolve(reel, footage)
    assert resolved.shots[0].segment.start == 0
    assert resolved.shots[0].duration == 3.0


# --- the timeline ------------------------------------------------------


def _seconds(value: str) -> float:
    if value.endswith("s"):
        value = value[:-1]
    return float(Fraction(value)) if "/" in value else float(value)


def test_the_spine_has_no_gaps(footage):
    """Each offset is the sum of the durations before it, to the frame.

    Rounding each offset from seconds independently leaves one-frame holes,
    which Final Cut renders as black flashes between shots.
    """
    reel = _reel(
        Shot(stem="a", start=0, end=4.03),
        Shot(stem="b", start=10, end=17.017),
        Shot(stem="a", start=20, end=23.99),
    )
    spine = export_reel.build_document(store.resolve(reel, footage)).getroot().find(".//spine")

    cursor = 0.0
    for clip in spine:
        assert _seconds(clip.get("offset")) == pytest.approx(cursor)
        cursor += _seconds(clip.get("duration"))


def test_the_sequence_is_as_long_as_its_shots(footage):
    reel = _reel(Shot(stem="a", start=0, end=4.03), Shot(stem="b", start=10, end=17.017))
    root = export_reel.build_document(store.resolve(reel, footage)).getroot()
    sequence = root.find(".//sequence")
    total = sum(_seconds(c.get("duration")) for c in root.find(".//spine"))
    assert _seconds(sequence.get("duration")) == pytest.approx(total)


def test_every_timeline_value_is_a_whole_number_of_frames(footage):
    """Final Cut rejects a document whose times are not frame multiples."""
    reel = _reel(
        Shot(stem="a", start=1.017, end=21.03),   # 30 fps, and the bulk of it
        Shot(stem="c", start=2.5, end=7.1),       # 24 fps
    )
    root = export_reel.build_document(store.resolve(reel, footage)).getroot()
    for clip in root.find(".//spine"):
        for attr, fps in (("offset", "30"), ("duration", "30"), ("start", None)):
            value = clip.get(attr)
            if value in (None, "0s"):
                continue
            numerator, denominator = (int(x) for x in value[:-1].split("/"))
            ticks, scale = timebase(fps) if fps else (None, denominator)
            assert denominator == scale
            assert numerator % (ticks if ticks else 1) == 0


def test_an_in_point_is_measured_in_its_own_source_frames(footage):
    """A 24 fps shot's in-point is 24ths, even on a 30 fps timeline."""
    reel = _reel(Shot(stem="a", start=0, end=30), Shot(stem="c", start=5.0, end=9.0))
    root = export_reel.build_document(store.resolve(reel, footage)).getroot()
    odd = root.find(".//spine")[1]
    _, scale = timebase("24")
    assert odd.get("start").endswith(f"/{scale}s")
    # while its place on the timeline is still counted in the sequence's.
    _, sequence_scale = timebase("30")
    assert odd.get("offset").endswith(f"/{sequence_scale}s")


def test_the_timeline_takes_the_shape_with_the_most_screen_time(footage):
    """Not the first shot's, or a single 720p cutaway sets the whole reel."""
    reel = _reel(
        Shot(stem="c", start=0, end=3),    # 720p24, opens the reel
        Shot(stem="a", start=0, end=30),   # 1080x1920, most of it
    )
    shape = export_reel.sequence_format(store.resolve(reel, footage))
    assert (shape.asset.width, shape.asset.height) == (1080, 1920)


def test_a_reel_with_nothing_usable_refuses_to_export(footage):
    reel = _reel(Shot(stem="gone", start=0, end=5))
    with pytest.raises(ValueError, match="no usable shots"):
        export_reel.build_document(store.resolve(reel, footage))


# --- captions and the running order ------------------------------------


def test_captions_are_rebased_onto_the_cut(footage):
    """A line 20s into the source is 5s into a reel that opened at 15s."""
    reel = _reel(Shot(stem="a", start=15.0, end=28.0, segment_id="a#00000.00"))
    body = export_reel.srt(store.resolve(reel, footage))
    assert "00:00:05,000 --> 00:00:09,000" in body
    assert "Sixty years ago." in body


def test_a_line_the_trim_only_half_covers_is_kept_and_clipped(footage):
    """A half-spoken sentence is still the sentence being said."""
    reel = _reel(Shot(stem="a", start=0.0, end=3.0, segment_id="a#00000.00"))
    body = export_reel.srt(store.resolve(reel, footage))
    assert "Are you married?" in body           # runs 2-5, cut at 3
    assert "00:00:02,000 --> 00:00:03,000" in body


def test_captions_run_in_reel_order_not_source_order(footage):
    reel = _reel(
        Shot(stem="a", start=18.0, end=26.0, segment_id="a#00000.00"),
        Shot(stem="a", start=0.0, end=8.0, segment_id="a#00000.00"),
    )
    body = export_reel.srt(store.resolve(reel, footage))
    assert body.index("Sixty years ago.") < body.index("Are you married?")


def test_the_running_order_names_what_has_come_loose(footage):
    reel = _reel(Shot(stem="gone", start=0, end=5))
    assert "!! no clip named gone" in export_reel.script(store.resolve(reel, footage))


# --- the store ---------------------------------------------------------


def test_a_new_reel_never_overwrites_an_existing_cut():
    first = store.create("Rs 7 train to Bombay")
    second = store.create("Rs 7 train to Bombay")
    assert first.slug == "rs-7-train-to-bombay"
    assert second.slug == "rs-7-train-to-bombay-2"
    assert store.load(first.slug) is not None


def test_a_reel_round_trips_through_its_file():
    reel = store.create("A cut", shots=[Shot(stem="a", start=1.5, end=9.25, note="opener")])
    again = store.load(reel.slug)
    assert again.shots[0].note == "opener"
    assert again.duration == pytest.approx(7.75)


def test_a_malformed_reel_does_not_kill_the_run(capsys):
    store.create("Fine")
    store.reel_dir().joinpath("broken.reel.json").write_text("{oh dear")
    assert [r.title for r in store.load_all()] == ["Fine"]
    assert "skipping malformed" in capsys.readouterr().out


# --- captions ----------------------------------------------------------


def _captions(reel, footage, **kw):
    root = export_reel.build_document(store.resolve(reel, footage), **kw).getroot()
    return list(root.iter("caption"))


def test_a_caption_is_offset_in_the_source_not_the_timeline(footage):
    """An anchored item's offset is in its parent's own time.

    For a clip on the spine that is *source* time — the same frame of reference
    as its `start`. Measuring from the timeline instead stacks every caption at
    the head of the reel.
    """
    reel = _reel(
        Shot(stem="b", start=10, end=20),                       # opens the reel
        Shot(stem="a", start=0, end=25, segment_id="a#00000.00"),
    )
    spoken = _captions(reel, footage)
    assert len(spoken) == 2
    assert _seconds(spoken[0].get("offset")) == pytest.approx(2)   # the line's own 2s
    assert _seconds(spoken[1].get("offset")) == pytest.approx(20)


def test_overlapping_estimates_are_clipped_to_the_next_line(footage):
    """Final Cut refuses captions that sit on top of each other in one role,
    and the model's line timings are estimates that sometimes overlap."""
    sidecars = [_sidecar("a", segments=[
        Segment(id="a#00000.00", start=0, end=30, title="Overlapping", lines=[
            Line(start=0, end=9, en="first"),
            Line(start=4, end=8, en="second"),
        ]),
    ])]
    reel = _reel(Shot(stem="a", start=0, end=20, segment_id="a#00000.00"))
    first, second = _captions(reel, sidecars)
    assert _seconds(first.get("duration")) == pytest.approx(4)
    assert _seconds(first.get("offset")) + _seconds(first.get("duration")) <= (
        _seconds(second.get("offset")) + 1e-6
    )


def test_a_caption_carries_no_pretty_printing(footage):
    """<text> is mixed content, so indentation inside it *is* the caption."""
    reel = _reel(Shot(stem="a", start=0, end=25, segment_id="a#00000.00"))
    text = _captions(reel, footage)[0].find("text")
    assert text.text is None
    assert text[0].text == "Are you married?"
    assert text[0].tail is None


def test_both_languages_become_two_lines_of_one_caption(footage):
    reel = _reel(Shot(stem="a", start=0, end=25, segment_id="a#00000.00"))
    spoken = _captions(reel, footage, captions="both")[0]
    assert [t[0].text for t in spoken.findall("text")] == [
        "Lagn zalato?", "Are you married?"]


def test_a_caption_never_runs_past_its_shot(footage):
    reel = _reel(Shot(stem="a", start=0, end=3.0, segment_id="a#00000.00"))
    spoken = _captions(reel, footage)[0]
    end = _seconds(spoken.get("offset")) + _seconds(spoken.get("duration"))
    assert end == pytest.approx(3.0)


def test_captions_can_be_left_out(footage):
    reel = _reel(Shot(stem="a", start=0, end=25, segment_id="a#00000.00"))
    assert _captions(reel, footage, captions=None) == []


@pytest.mark.skipif(
    __import__("greels.export_fcpxml", fromlist=["dtd_path"]).dtd_path() is None,
    reason="Final Cut is not installed, so Apple's DTD is not here to check against",
)
def test_a_written_reel_passes_apples_own_dtd(footage, tmp_path):
    reel = _reel(
        Shot(stem="a", start=0, end=25, segment_id="a#00000.00", note="opener"),
        Shot(stem="c", start=1, end=6),
        Shot(stem="b", start=10, end=30),
    )
    project, resolved = export_reel.write(
        reel, dest_dir=tmp_path / "out", lang="both", sidecars=footage
    )
    assert project.exists() and resolved.problems == []
    assert (tmp_path / "out" / "test.srt").exists()
    assert (tmp_path / "out" / "test.txt").exists()


# --- what was handed over ----------------------------------------------


def test_a_cut_knows_when_it_has_changed_since_its_export(footage, tmp_path):
    """Final Cut cannot update a project from XML any more than a clip: a
    re-export arrives as a second timeline, so this is worth saying first."""
    reel = store.create("Handed over", shots=[Shot(stem="a", start=0, end=10)])
    assert reel.exported_at is None and not reel.stale

    export_reel.write(reel, dest_dir=tmp_path / "out", sidecars=footage)
    assert store.load(reel.slug).exported_at
    assert not store.load(reel.slug).stale

    reel = store.load(reel.slug)
    reel.shots[0].end = 8
    reel.save()
    assert store.load(reel.slug).stale


def test_reordering_counts_as_a_change_but_renaming_a_note_style_does_not(footage, tmp_path):
    reel = store.create("Two shots", shots=[
        Shot(stem="a", start=0, end=10), Shot(stem="b", start=10, end=20),
    ])
    export_reel.write(reel, dest_dir=tmp_path / "out", sidecars=footage)

    reel = store.load(reel.slug)
    reel.shots.reverse()
    reel.save()
    assert store.load(reel.slug).stale

    reel = store.load(reel.slug)
    reel.shots.reverse()
    reel.save()
    assert not store.load(reel.slug).stale   # back to what was exported


def test_an_export_that_fails_records_nothing(footage, tmp_path):
    """A file Final Cut would reject is never written, so nothing was handed
    over -- stamping it would put the cut permanently out of the queue."""
    reel = store.create("Broken", shots=[Shot(stem="gone", start=0, end=5)])
    with pytest.raises(ValueError):
        export_reel.write(reel, dest_dir=tmp_path / "out", sidecars=footage)
    assert store.load(reel.slug).exported_at is None


def test_a_clip_awaiting_analysis_has_not_come_loose(footage):
    """A shot lifted before a re-scan points at a clip with no segments yet.

    That is a clip without an analysis, not a clip that is gone, and reporting
    it as loose would send you looking for a file that is sitting right there.
    """
    scanned_only = [_sidecar("a", segments=[])]
    reel = _reel(Shot(stem="a", start=0, end=10, segment_id="a#00000.00"))
    resolved = store.resolve(reel, scanned_only)

    assert resolved.problems == []
    assert resolved.shots[0].segment is None
    assert resolved.shots[0].title == "a"   # falls back to the clip's name


# --- reading a cut back to yourself ------------------------------------


def _notes(reel, footage):
    return {(n.shot, n.text) for n in store.critique(store.resolve(reel, footage))}


def test_a_cut_that_is_all_one_clip_is_a_trim_not_a_reel(footage):
    reel = _reel(Shot(stem="a", start=0, end=10), Shot(stem="a", start=12, end=20))
    assert any("one clip" in text for _, text in _notes(reel, footage))


def test_two_shots_running_straight_on_are_one_shot(footage):
    """A cut you cannot see is a cut you did not need to make."""
    reel = _reel(
        Shot(stem="b", start=0, end=8),
        Shot(stem="a", start=10, end=20),
        Shot(stem="a", start=20.2, end=28),
    )
    assert any("runs straight on from shot 2" in text for shot, text in _notes(reel, footage)
               if shot == 2)


def test_a_gap_between_two_shots_of_one_clip_is_a_real_cut(footage):
    reel = _reel(
        Shot(stem="b", start=0, end=8),
        Shot(stem="a", start=0, end=10),
        Shot(stem="a", start=18, end=26),
    )
    assert not any("runs straight on" in text for _, text in _notes(reel, footage))


def test_one_shot_carrying_the_reel_is_worth_saying(footage):
    reel = _reel(
        Shot(stem="a", start=0, end=28),
        Shot(stem="b", start=0, end=3),
        Shot(stem="c", start=0, end=3),
    )
    assert any("of the reel" in text for shot, text in _notes(reel, footage) if shot == 0)


def test_a_two_shot_cut_is_not_told_that_half_of_it_is_half_of_it(footage):
    reel = _reel(Shot(stem="a", start=0, end=15), Shot(stem="b", start=0, end=15))
    assert not any("of the reel" in text for _, text in _notes(reel, footage))


def test_a_flash_of_a_shot_is_worth_saying(footage):
    reel = _reel(
        Shot(stem="a", start=0, end=20), Shot(stem="b", start=0, end=0.6),
        Shot(stem="c", start=0, end=5),
    )
    assert any("flash" in text for shot, text in _notes(reel, footage) if shot == 1)


def test_a_cut_of_a_sensible_length_is_told_nothing(footage):
    reel = _reel(
        Shot(stem="a", start=0, end=20),
        Shot(stem="b", start=10, end=30),
        Shot(stem="c", start=0, end=8),
    )
    assert _notes(reel, footage) == set()


def test_a_loose_shot_is_left_out_of_the_reckoning(footage):
    """It is not in the timeline, so it is not what makes the reel long."""
    reel = _reel(Shot(stem="gone", start=0, end=400), Shot(stem="a", start=0, end=20),
                 Shot(stem="b", start=0, end=20), Shot(stem="c", start=0, end=8))
    assert not any("runs" in text for _, text in _notes(reel, footage))


def test_a_shot_that_reaches_into_the_next_moment_keeps_what_is_said_there():
    """A shot is not required to sit inside one segment.

    Widening the in point by five seconds reaches back into the moment before,
    and reading only the shot's own segment dropped everything said in the
    overlap -- from the captions, the .srt and the running order alike.
    """
    footage = [_sidecar("a", segments=[
        Segment(id="a#00000.00", start=0, end=20, title="First", lines=[
            Line(start=15, end=18, en="the end of the first"),
        ]),
        Segment(id="a#00020.00", start=20, end=40, title="Second", lines=[
            Line(start=22, end=25, en="the start of the second"),
        ]),
    ])]
    reel = _reel(Shot(stem="a", start=14, end=30, segment_id="a#00020.00"))
    said = [line.en for line in store.resolve(reel, footage).shots[0].lines]

    assert said == ["the end of the first", "the start of the second"]


def test_an_utterance_in_two_overlapping_segments_is_said_once():
    """Chunk overlap can leave the same line in two segments."""
    footage = [_sidecar("a", segments=[
        Segment(id="a#00000.00", start=0, end=25, title="First", lines=[
            Line(start=20, end=23, en="said in the overlap"),
        ]),
        Segment(id="a#00020.00", start=20, end=40, title="Second", lines=[
            Line(start=20, end=23, en="said in the overlap"),
        ]),
    ])]
    reel = _reel(Shot(stem="a", start=0, end=40))
    assert [l.en for l in store.resolve(reel, footage).shots[0].lines] == ["said in the overlap"]


# --- what the audio adds to a second viewing -------------------------------


def test_a_shot_that_opens_a_beat_late_is_pointed_at(footage):
    """The model's boundary showing through: the first syllable is clipped."""
    reel = _reel(Shot(stem="a", start=5.0, end=20.0))
    notes = store.critique(store.resolve(reel, footage), {"a": [[4.2, 25.0]]})
    assert any("0.8s after this one started talking" in n.text for n in notes)


def test_a_shot_that_cuts_somebody_off_is_pointed_at(footage):
    reel = _reel(Shot(stem="a", start=5.0, end=20.0))
    notes = store.critique(store.resolve(reel, footage), {"a": [[4.9, 20.9]]})
    assert any("0.9s before they stop talking" in n.text for n in notes)


def test_coming_in_over_the_middle_of_an_answer_is_an_edit_not_a_mistake(footage):
    """Past what `reel snap` would move, saying so points at a button that
    does nothing."""
    reel = _reel(Shot(stem="a", start=12.0, end=20.0))
    notes = store.critique(store.resolve(reel, footage), {"a": [[2.0, 25.0]]})
    assert not any("started talking" in n.text for n in notes)


def test_a_shot_already_on_the_word_says_nothing(footage):
    reel = _reel(Shot(stem="a", start=5.0, end=20.0))
    notes = store.critique(store.resolve(reel, footage), {"a": [[4.95, 20.1]]})
    assert not any("talking" in n.text for n in notes)


def test_without_the_audio_the_critique_is_what_it_always_was(footage):
    reel = _reel(Shot(stem="a", start=5.0, end=20.0))
    assert not any("talking" in n.text
                   for n in store.critique(store.resolve(reel, footage)))


def test_the_timeline_carries_the_level_match(footage):
    """So the Final Cut edit does not start with a fader ride."""
    reel = _reel(Shot(stem="a", start=0, end=10), Shot(stem="b", start=0, end=10))
    doc = export_reel.build_document(
        store.resolve(reel, footage), captions=None, gains={"a": 4.3, "b": 0.0}
    )
    clips = list(doc.getroot().iter("asset-clip"))
    assert clips[0].find("adjust-volume").get("amount") == "4.3dB"
    assert clips[1].find("adjust-volume") is None   # already where it should be


def test_a_silent_clip_gets_no_volume_to_adjust(footage):
    footage[1] = _sidecar("b", duration=90.0)
    footage[1].asset.has_audio = False
    reel = _reel(Shot(stem="b", start=0, end=10))
    doc = export_reel.build_document(
        store.resolve(reel, footage), captions=None, gains={"b": 6.0}
    )
    assert next(doc.getroot().iter("asset-clip")).find("adjust-volume") is None


def test_the_volume_sits_where_the_dtd_wants_it(footage):
    """After the note, before the captions -- which are anchored items."""
    reel = _reel(Shot(stem="a", start=0, end=30, note="the good bit"))
    doc = export_reel.build_document(
        store.resolve(reel, footage), captions="en", gains={"a": 4.3}
    )
    clip = next(doc.getroot().iter("asset-clip"))
    assert [child.tag for child in clip][:2] == ["note", "adjust-volume"]


def test_a_caption_nobody_could_read_is_pointed_at(footage):
    """It is on screen and the viewer is trying. The fix is a shorter
    translation, not a longer shot -- the speech is the speech."""
    footage[0] = _sidecar("a", segments=[
        Segment(id="a#0", start=0, end=30, title="Fast", lines=[
            Line(start=1, end=2, en="Everyone used to tell me that you were a hero at that time."),
            Line(start=4, end=9, en="Sixty years ago."),
        ]),
    ])
    notes = store.critique(store.resolve(_reel(Shot(stem="a", start=0, end=30)), footage))
    fast = [n for n in notes if "going by faster" in n.text]
    assert len(fast) == 1
    assert fast[0].shot == 0                     # which shot to go and look at
    assert "1 caption(s)" in fast[0].text


def test_captions_anybody_could_read_are_not_mentioned(footage):
    notes = store.critique(store.resolve(_reel(Shot(stem="a", start=0, end=30)), footage))
    assert not any("faster than" in n.text for n in notes)


def test_the_notes_read_in_the_order_of_the_cut(footage):
    """A list that jumps about is read as noise."""
    reel = _reel(
        Shot(stem="a", start=0, end=1.0),     # a flash, shot 1
        Shot(stem="b", start=0, end=5),
        Shot(stem="c", start=0, end=0.5),     # a flash, shot 3
    )
    notes = store.critique(store.resolve(reel, footage))
    order = [n.shot for n in notes]
    assert order == sorted(order, key=lambda s: (s is not None, s or 0))
    assert order[0] is None                   # the whole cut, before any shot


def test_the_moment_offers_more_lines_than_the_trim_holds(footage):
    """"Start where he says *sixty, seventy years ago*" is a line the shot does
    not yet contain, and you cannot click one that is not on the screen."""
    shot, = store.resolve(_reel(Shot(stem="a", start=10, end=25)), footage).shots
    assert [line.en for line in shot.lines] == ["Sixty years ago."]
    assert [line.en for line in shot.moment_lines] == [
        "Are you married?", "Sixty years ago.",
    ]


def test_a_shot_reaching_past_its_moment_keeps_what_it_reaches(footage):
    """A widened in point reaches back into the moment before, and what was
    said there belongs to the shot as much as to that moment."""
    footage.append(_sidecar("d", segments=[
        Segment(id="d#0", start=0, end=10, title="Before", lines=[
            Line(start=2, end=4, en="The one before."),
        ]),
        Segment(id="d#10", start=10, end=20, title="After", lines=[
            Line(start=12, end=14, en="The one after."),
        ]),
    ]))
    shot, = store.resolve(_reel(Shot(stem="d", start=3, end=15)), footage).shots
    assert shot.segment.id == "d#0"     # its midpoint is in the first moment
    assert [line.en for line in shot.moment_lines] == [
        "The one before.", "The one after.",
    ]


def test_a_shot_with_no_clip_has_no_moment_to_read(footage):
    shot, = store.resolve(_reel(Shot(stem="gone", start=0, end=5)), footage).shots
    assert shot.moment_lines == []


def test_two_captions_are_never_on_screen_at_once(footage):
    """Final Cut refuses captions that sit on top of each other, and two burnt
    cards enabled together are drawn at the same place on the frame."""
    footage[0] = _sidecar("a", segments=[
        Segment(id="a#0", start=0, end=30, title="Overlapping", lines=[
            Line(start=1, end=6, en="The first thing."),
            Line(start=4, end=8, en="The second thing."),
        ]),
    ])
    lines = export_reel.caption_lines(
        store.resolve(_reel(Shot(stem="a", start=0, end=30)), footage)
    )
    assert [(round(a, 2), round(b, 2)) for a, b, _, _ in lines] == [(1.0, 4.0), (4.0, 8.0)]


def test_a_caption_clipped_to_nothing_is_left_out(footage):
    footage[0] = _sidecar("a", segments=[
        Segment(id="a#0", start=0, end=30, title="Swallowed", lines=[
            Line(start=2, end=9, en="Swallowed whole."),
            Line(start=2, end=6, en="By this one."),
        ]),
    ])
    lines = export_reel.caption_lines(
        store.resolve(_reel(Shot(stem="a", start=0, end=30)), footage)
    )
    assert [text for _, _, text, _ in lines] == ["Swallowed whole."]


def test_a_line_spoken_in_english_is_not_printed_twice(footage):
    """Some of this footage is in English, and the romanised Konkani for such
    a line is the same words. Both, one above the other, reads as a mistake."""
    assert export_reel.caption_texts("I was a hero.", "I was a hero.", "both") == [
        "I was a hero.",
    ]
    assert export_reel.caption_texts("Are you married?", "Lagn zalato?", "both") == [
        "Lagn zalato?", "Are you married?",
    ]


def test_a_missing_half_falls_back_to_the_other(footage):
    assert export_reel.caption_texts("", "Hoi.", "en") == ["Hoi."]
    assert export_reel.caption_texts("Yes.", "", "kok") == ["Yes."]
    assert export_reel.caption_texts("  ", "", "both") == []


def test_an_edit_made_while_it_exported_is_not_overwritten(footage, tmp_path):
    """The same shape as the render: the copy being written is out of date by
    the time the files are on disk."""
    reel = _reel(Shot(stem="a", start=0, end=10))
    reel.save()

    handed = store.load("test")
    moved = store.load("test")
    moved.shots[0].end = 25.0
    moved.save()

    export_reel.write(handed, dest_dir=tmp_path / "out", sidecars=footage)

    after = store.load("test")
    assert after.shots[0].end == 25.0
    assert after.exported_at and after.stale
