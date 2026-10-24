"""Copies kept of anything a write was about to destroy.

The point is narrow: `backup` covers the sidecars between commits, and the gap
between two backups is where a mistyped edit or a mis-clicked Delete lives.
"""

import pytest

from greels import config, index, trash
from greels.schema import Asset, Segment, Sidecar
from greels.server import Api


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=120, fps="30"),
        segments=[
            Segment(id="a#00000.00", start=0, end=30, title="Marriage chat",
                    konkani="Lagn zalato?", english="Is he married?"),
            Segment(id="a#00030.00", start=30, end=60, title="Coconut scraper"),
        ],
    ).save(config.sidecar_path("a"))
    index.build()
    return Api()


def test_deleting_a_segment_keeps_the_sidecar_that_had_it(api):
    api.delete_segment("a#00000.00")
    assert len(api.clip("a")["segments"]) == 1

    kept = trash.entries()
    assert len(kept) == 1
    assert "deleted a#00000.00" in trash.why(kept[0])

    trash.restore(kept[0])
    assert len(api.clip("a")["segments"]) == 2


def test_correcting_a_transcript_keeps_what_it_replaced(api):
    api.update_segment("a#00000.00", {"english": "oops"})

    kept = trash.entries()
    assert len(kept) == 1
    trash.restore(kept[0])
    assert api.clip("a")["segments"][0]["english"] == "Is he married?"


def test_a_rating_is_not_worth_keeping_a_copy_of(api):
    """It is a keystroke in review, it happens hundreds of times an hour, and
    losing one costs a keystroke."""
    api.update_segment("a#00000.00", {"rating": "favorite"})
    api.update_segment("a#00000.00", {"status": "reviewed"})
    assert trash.entries() == []


def test_restoring_keeps_whatever_it_displaces(api):
    """So restoring the wrong copy costs nothing."""
    api.update_segment("a#00000.00", {"title": "first change"})
    trash.restore(trash.entries()[0])

    assert len(trash.entries()) == 2
    assert "displaced by restoring" in trash.why(trash.entries()[0])
    trash.restore(trash.entries()[0])
    assert api.clip("a")["segments"][0]["title"] == "first change"


def test_two_copies_in_the_same_second_do_not_overwrite_each_other(api):
    api.update_segment("a#00000.00", {"title": "one"})
    api.update_segment("a#00000.00", {"title": "two"})
    api.update_segment("a#00030.00", {"title": "three"})
    assert len(trash.entries()) == 3


def test_deleting_a_cut_keeps_it(api):
    api.create_cut({"title": "A cut", "segments": ["a#00000.00"]})
    api.delete_cut("a-cut")
    assert api.cuts() == []

    kept = trash.entries()[0]
    assert kept.original == "a-cut.reel.json"
    # A reel lives one level down, and the name is what says so.
    assert kept.destination == config.index_dir() / "reels" / "a-cut.reel.json"

    trash.restore(kept)
    assert [c["slug"] for c in api.cuts()] == ["a-cut"]


def test_the_trash_does_not_look_like_a_sidecar_to_anything_else(api, monkeypatch, tmp_path):
    """It lives in a subdirectory, so the globs that find sidecars miss it."""
    from greels import backup as store, library

    monkeypatch.setenv("GREELS_ARCHIVE", str(tmp_path / "archive"))
    api.delete_segment("a#00000.00")
    assert trash.entries()
    assert [p.name for p in library.sidecars()] == ["a.seg.json"]
    assert [s.name for s in store.sets()][0] == "sidecars"
    assert trash.trash_dir().parent == config.index_dir()


def test_the_kept_copies_are_capped(api, monkeypatch):
    monkeypatch.setattr(trash, "KEEP", 3)
    for i in range(6):
        api.update_segment("a#00000.00", {"title": f"take {i}"})
    assert len(trash.entries()) == 3


def test_copies_from_the_same_second_stay_in_order(api):
    """The filename alone has to order the trash: `restore` gives you the
    newest, and a mistyped correction is often the second edit in a second."""
    for title in ("first", "second", "third"):
        api.update_segment("a#00000.00", {"title": title})

    names = [e.name for e in trash.entries()]
    assert names == sorted(names, reverse=True)
    trash.restore(trash.entries()[0])
    assert api.clip("a")["segments"][0]["title"] == "second"
