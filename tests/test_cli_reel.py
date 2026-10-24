"""The `greels reel` commands, driven the way they are actually used.

The reel model and the exporter are tested directly elsewhere; what these
cover is the part that only exists in the CLI -- resolving a shot by position
or by title, refusing an ambiguous one, and the guards that stop an edit
doing something quietly wrong.
"""

import pytest
import typer

from greels import cli_reel, config, index, reel as store
from greels.schema import Asset, Segment, Sidecar


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=100, fps="30",
                    width=1080, height=1920, created="2026-02-01T00:00:00Z"),
        segments=[
            Segment(id="a#00000.00", start=0, end=30, title="Marriage chat",
                    tags=["theme:marriage"], rating="favorite"),
            Segment(id="a#00030.00", start=30, end=60, title="Coconut scraper",
                    tags=["object:coconut_scraper"]),
        ],
    ).save(config.sidecar_path("a"))
    Sidecar(
        asset=Asset(file="b.MOV", stem="b", duration=100, fps="30",
                    width=1080, height=1920, created="2026-01-01T00:00:00Z"),
        segments=[
            Segment(id="b#00010.00", start=10, end=40, title="Marriage at twenty",
                    tags=["theme:marriage"], rating="favorite"),
        ],
    ).save(config.sidecar_path("b"))
    index.build()
    return tmp_path


def test_new_from_a_query_takes_what_the_search_finds(archive):
    cli_reel.reel_new("Marriage", from_tag=None, segment=[], said=None, search="",
                      tag=["theme:marriage"], rating="", limit=40, order="found", hook="")

    reel = store.load("marriage")
    assert {s.stem for s in reel.shots} == {"a", "b"}
    assert reel.duration == 60


def test_order_shot_follows_when_the_footage_was_taken(archive):
    """Which is what makes a set of moments read as an afternoon."""
    cli_reel.reel_new("Marriage", from_tag=None, segment=[], said=None, search="",
                      tag=["theme:marriage"], rating="", limit=40, order="shot", hook="")

    assert [s.stem for s in store.load("marriage").shots] == ["b", "a"]


def test_a_rating_filter_narrows_the_query(archive):
    cli_reel.reel_new("Keepers", from_tag=None, segment=[], said=None, search="", tag=[],
                      rating="favorite", limit=40, order="found", hook="")

    kept = {s.segment_id for s in store.load("keepers").shots}
    assert kept == {"a#00000.00", "b#00010.00"}   # not the unrated scraper


def test_a_rating_that_matches_nothing_stops(archive):
    with pytest.raises(typer.Exit):
        cli_reel.reel_new("Rejects", from_tag=None, segment=[], said=None, search="", tag=[],
                          rating="rejected", limit=40, order="found", hook="")
    assert store.load_all() == []


def test_a_query_that_finds_nothing_stops_rather_than_making_an_empty_cut(archive):
    with pytest.raises(typer.Exit):
        cli_reel.reel_new("Nothing", from_tag=None, segment=[], said=None, search="",
                          tag=["theme:nonexistent"], rating="", limit=40,
                          order="found", hook="")
    assert store.load_all() == []


def test_adding_a_range_the_cut_already_holds_does_not_stack_it(archive):
    cli_reel.reel_new("Marriage", from_tag=None, segment=["a#00000.00"], said=None, search="",
                      tag=[], rating="", limit=40, order="found", hook="")
    cli_reel.reel_add("marriage", ["a#00000.00"], at=None, line=None, search="", tag=[],
                      rating="", limit=40, order="found")

    assert len(store.load("marriage").shots) == 1


def test_an_ambiguous_title_is_refused_rather_than_guessed(archive):
    cli_reel.reel_new("Marriage", from_tag=None, segment=[], said=None, search="", tag=[],
                      rating="", limit=40, order="found", hook="")
    with pytest.raises(typer.Exit):
        cli_reel.reel_add("marriage", ["Marriage"], at=None, line=None, search="", tag=[],
                          rating="", limit=40, order="found")


def test_removing_several_shots_resolves_them_before_removing_any(archive):
    """`rm 2 3` must not delete 2 and then 4."""
    reel = store.create("Four", shots=[
        store.Shot(stem="a", start=i * 10, end=i * 10 + 5) for i in range(4)
    ])
    cli_reel.reel_rm(reel.slug, ["2", "3"])

    assert [s.start for s in store.load(reel.slug).shots] == [0, 30]


def test_a_trim_cannot_run_past_the_end_of_the_clip(archive):
    reel = store.create("One", shots=[store.Shot(stem="a", start=0, end=30)])
    cli_reel.reel_trim(reel.slug, "1", start=None, end=500, head=None, tail=None)

    shot = store.load(reel.slug).shots[0]
    assert shot.end == 100          # the clip's own length
    assert shot.start < shot.end


def test_an_in_point_at_the_very_end_still_leaves_a_shot(archive):
    reel = store.create("One", shots=[store.Shot(stem="a", start=0, end=30)])
    cli_reel.reel_trim(reel.slug, "1", start=100, end=None, head=None, tail=None)

    shot = store.load(reel.slug).shots[0]
    assert shot.end <= 100 and shot.end > shot.start


def test_a_shot_position_outside_the_cut_is_refused(archive):
    reel = store.create("One", shots=[store.Shot(stem="a", start=0, end=30)])
    with pytest.raises(typer.Exit):
        cli_reel.reel_trim(reel.slug, "5", start=1, end=None, head=None, tail=None)


def test_moving_a_shot_reorders_without_losing_one(archive):
    reel = store.create("Three", shots=[
        store.Shot(stem="a", start=0, end=5),
        store.Shot(stem="a", start=10, end=15),
        store.Shot(stem="a", start=20, end=25),
    ])
    cli_reel.reel_move(reel.slug, "3", 1)

    assert [s.start for s in store.load(reel.slug).shots] == [20, 0, 10]


def test_naming_a_reel_that_does_not_exist_stops(archive):
    with pytest.raises(typer.Exit):
        cli_reel.reel_show("no-such-cut", lines=False)


def test_ordering_by_when_the_footage_was_taken(archive):
    reel = store.create("Both", shots=[
        store.Shot(stem="a", start=0, end=10),
        store.Shot(stem="b", start=0, end=10),
    ])
    cli_reel.reel_order(reel.slug, by="shot")   # b was shot a month earlier
    assert [s.stem for s in store.load(reel.slug).shots] == ["b", "a"]

    cli_reel.reel_order(reel.slug, by="clip")
    assert [s.stem for s in store.load(reel.slug).shots] == ["a", "b"]


def test_an_unknown_ordering_is_refused(archive):
    reel = store.create("Both", shots=[store.Shot(stem="a", start=0, end=10)])
    with pytest.raises(typer.Exit):
        cli_reel.reel_order(reel.slug, by="vibes")


def test_a_duplicate_is_a_copy_and_not_a_reference(archive):
    """So trying a shorter version cannot damage the one you have."""
    original = store.create("Long one", shots=[store.Shot(stem="a", start=0, end=30)])
    cli_reel.reel_duplicate(original.slug, "Short one")

    copy = store.load("short-one")
    assert copy.slug != original.slug
    cli_reel.reel_trim(copy.slug, "1", start=None, end=10, head=None, tail=None)

    assert store.load(copy.slug).duration == 10
    assert store.load(original.slug).duration == 30


# --- snapping a cut onto speech --------------------------------------------


@pytest.fixture
def heard(monkeypatch):
    """Talking from 0.5 to 8s and from 9.5 to 29.5s, with a pause between."""
    monkeypatch.setattr(
        "greels.speech.runs_for", lambda stem, cache=None: [[0.5, 8.0], [9.5, 29.5]]
    )


def test_snap_moves_every_shot_onto_an_utterance(archive, heard):
    """The segment says 0-30; the talking is 0.5-29.5, and a shot should open
    on the word rather than on half a second of nothing."""
    cli_reel.reel_new("Marriage", from_tag=None, segment=["a#00000.00"], said=None, search="",
                      tag=[], rating="", limit=40, order="found", hook="")
    cli_reel.reel_snap("marriage", window=None, dry_run=False)

    shot = store.load("marriage").shots[0]
    assert shot.start == pytest.approx(0.5 - 0.08)
    assert shot.end == 29.5


def test_a_boundary_no_utterance_is_near_is_left_alone(archive, monkeypatch):
    """A b-roll shot of a cupboard has nothing to snap to, and saying so is
    better than moving it two seconds to the nearest voice."""
    monkeypatch.setattr(
        "greels.speech.runs_for", lambda stem, cache=None: [[40.0, 50.0]]
    )
    cli_reel.reel_new("Marriage", from_tag=None, segment=["a#00000.00"], said=None, search="",
                      tag=[], rating="", limit=40, order="found", hook="")
    cli_reel.reel_snap("marriage", window=None, dry_run=False)
    assert store.load("marriage").shots[0].start == 0.0


def test_a_dry_run_changes_nothing(archive, heard):
    cli_reel.reel_new("Marriage", from_tag=None, segment=["a#00000.00"], said=None, search="",
                      tag=[], rating="", limit=40, order="found", hook="")
    cli_reel.reel_snap("marriage", window=None, dry_run=True)
    assert store.load("marriage").shots[0].start == 0.0


def test_snapping_keeps_the_cut_it_is_about_to_change(archive, heard):
    from greels import trash

    cli_reel.reel_new("Marriage", from_tag=None, segment=["a#00000.00"], said=None, search="",
                      tag=[], rating="", limit=40, order="found", hook="")
    cli_reel.reel_snap("marriage", window=None, dry_run=False)
    kept = [e for e in trash.entries() if e.original.startswith("marriage")]
    assert kept, "a cut is a decision; the version before an automatic edit is worth keeping"


def test_a_snap_cannot_push_a_shot_past_the_end_of_its_clip(archive, monkeypatch):
    monkeypatch.setattr(
        "greels.speech.runs_for", lambda stem, cache=None: [[50.0, 400.0]]
    )
    cli_reel.reel_new("Long", from_tag=None, segment=["a#00030.00"], said=None, search="",
                      tag=[], rating="", limit=40, order="found", hook="")
    cli_reel.reel_snap("long", window=500.0, dry_run=False)
    assert store.load("long").shots[0].end <= 100.0


# --- a supercut ------------------------------------------------------------


def test_said_makes_one_shot_per_time_it_is_said(archive, heard, monkeypatch):
    """"Every time he mentions money" is a reel format, and one command."""
    monkeypatch.setattr("greels.index.search_lines", lambda q, limit=30: [
        {"segment_id": "b#00010.00", "stem": "b", "start": 12.0, "end": 14.0,
         "en": "Seven rupees.", "kok": "Satt rupya."},
        {"segment_id": "a#00000.00", "stem": "a", "start": 4.0, "end": 6.0,
         "en": "Fourteen rupees.", "kok": ""},
    ])
    cli_reel.reel_new("Rupees", from_tag=None, segment=[], said="rupees", search="",
                      tag=[], rating="", limit=40, order="found", hook="")

    reel = store.load("rupees")
    assert len(reel.shots) == 2
    # In the order the footage was taken: b was shot a month before a.
    assert [s.stem for s in reel.shots] == ["b", "a"]
    # Each shot is the sentence, and named by it.
    assert reel.shots[0].note == "Seven rupees."
    assert reel.shots[0].duration < 4


def test_a_phrase_nobody_says_stops_rather_than_making_an_empty_cut(archive, monkeypatch):
    monkeypatch.setattr("greels.index.search_lines", lambda q, limit=30: [])
    with pytest.raises(typer.Exit):
        cli_reel.reel_new("Nothing", from_tag=None, segment=[], said="helicopter",
                          search="", tag=[], rating="", limit=40, order="found", hook="")


def test_export_refuses_a_language_it_does_not_have(archive):
    cli_reel.reel_new("Marriage", from_tag=None, segment=["a#00000.00"], said=None,
                      search="", tag=[], rating="", limit=40, order="found", hook="")
    with pytest.raises(typer.Exit):
        cli_reel.reel_export("marriage", every=False, stale=False, lang="english",
                             captions=True, level=False, editor=None, send=False)


def test_a_capped_supercut_says_it_is_capped(archive, heard, monkeypatch, capsys):
    """"Every time he says it" that is really "the first forty times" is a
    different offer."""
    monkeypatch.setattr("greels.index.search_lines", lambda q, limit=30: [
        {"segment_id": "a#00000.00", "stem": "a", "start": float(i), "end": i + 1.0,
         "en": f"line {i}", "kok": ""} for i in range(limit)
    ])
    cli_reel.reel_new("Every", from_tag=None, segment=[], said="a", search="",
                      tag=[], rating="", limit=5, order="found", hook="")
    assert "this is the first 5" in capsys.readouterr().out
