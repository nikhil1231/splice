"""Renaming camera-numbered clips.

This moves real source media, so the guards matter more than the heuristic.
"""

import pytest

from greels import config, library, rename
from greels.schema import Asset, Line, Segment, Sidecar


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    config.raw_dir().mkdir(parents=True)
    config.proxy_dir().mkdir(parents=True)
    config.chunk_dir().mkdir(parents=True)
    (config.export_dir() / "srt").mkdir(parents=True)
    return tmp_path


def make(stem, segments, suffix=".MOV"):
    (config.raw_dir() / f"{stem}{suffix}").write_bytes(b"x")
    library.proxy_for(stem).write_bytes(b"x")
    sc = Sidecar(
        asset=Asset(file=f"{stem}{suffix}", stem=stem, duration=100, fps="30"),
        segments=segments,
    )
    sc.save(config.sidecar_path(stem))
    return sc


@pytest.mark.parametrize(
    "stem,expected",
    [("IMG_8768", True), ("IMG8768", True), ("DSC_0001", True), ("PXL_20240101", True),
     ("6fa891f7-26a0-458f-b942-6cd50fc630d3", True),
     ("grandpa_old_money_box", False), ("lunch_conversation_part1", False),
     ("ScreenRecording_09-03-2026 15-11-50_1", False)],
)
def test_camera_named_detection(stem, expected):
    assert rename.is_camera_named(stem) is expected


def test_proposal_uses_the_longest_segment_not_the_first(archive):
    sc = make("IMG_8764", [
        Segment(id="IMG_8764#0", start=0, end=8, title="Do you know what this is?"),
        Segment(id="IMG_8764#8", start=8, end=70, title="Rattling coins in the money box",
                tags=["person:grandpa"]),
    ])
    # A short opening question makes a poor filename.
    assert rename.propose(sc) == "grandpa_rattling_coins_in_the_money_box"


def test_dominant_person_is_prefixed_only_when_absent(archive):
    sc = make("IMG_1", [
        Segment(id="IMG_1#0", start=0, end=50, title="Grandma blesses the router",
                tags=["person:grandma"]),
    ])
    assert rename.propose(sc) == "grandma_blesses_the_router"


def test_nikhil_alone_is_not_prefixed(archive):
    sc = make("IMG_2", [
        Segment(id="IMG_2#0", start=0, end=50, title="Explaining the internet",
                tags=["person:nikhil"]),
    ])
    assert rename.propose(sc) == "explaining_the_internet"


def test_unanalysed_clip_cannot_be_named(archive):
    sc = make("IMG_3", [])
    with pytest.raises(rename.RenameError, match="analyse it first"):
        rename.propose(sc)


def test_already_named_clips_are_left_alone_by_default(archive):
    named = make("grandpa_old_money_box", [Segment(id="x#0", start=0, end=9, title="t")])
    camera = make("IMG_9", [Segment(id="IMG_9#0", start=0, end=9, title="A new thing")])
    plans, _ = rename.build_plans([named, camera])
    assert [p.stem for p in plans] == ["IMG_9"]


def test_collision_with_an_existing_clip_is_skipped_not_fatal(archive):
    """One bad name must not cost the whole batch its naming."""
    make("grandpa_old_money_box", [Segment(id="a#0", start=0, end=9, title="t")])
    camera = make("IMG_9", [Segment(id="IMG_9#0", start=0, end=9, title="t")])
    plans, skips = rename.build_plans([camera], {"IMG_9": "grandpa_old_money_box"})
    assert plans == []
    assert [(s.stem, "collides" in s.reason) for s in skips] == [("IMG_9", True)]


def test_two_clips_cannot_claim_the_same_name(archive):
    a = make("IMG_1", [Segment(id="IMG_1#0", start=0, end=9, title="t")])
    b = make("IMG_2", [Segment(id="IMG_2#0", start=0, end=9, title="t")])
    plans, skips = rename.build_plans([a, b], {"IMG_1": "same", "IMG_2": "same"})
    assert [p.stem for p in plans] == ["IMG_1"]
    assert [s.stem for s in skips] == ["IMG_2"]


def test_an_unanalysed_clip_is_skipped_rather_than_blocking_the_batch(archive):
    """A single failed analysis used to leave the whole archive camera-numbered."""
    broken = make("IMG_1", [])
    good = make("IMG_2", [Segment(id="IMG_2#0", start=0, end=9, title="A new thing")])
    plans, skips = rename.build_plans([broken, good])
    assert [p.stem for p in plans] == ["IMG_2"]
    assert [(s.stem, s.reason) for s in skips] == [("IMG_1", "not analysed yet")]


def test_renaming_an_unknown_clip_is_refused(archive):
    a = make("IMG_1", [Segment(id="IMG_1#0", start=0, end=9, title="t")])
    with pytest.raises(rename.RenameError, match="no such clip"):
        rename.build_plans([a], {"IMG_404": "whatever"})


def test_apply_moves_every_reference(archive):
    make("IMG_8768", [
        Segment(id="IMG_8768#00000.00", start=0, end=60, title="Guessing the gadget",
                lines=[Line(start=0, end=2, en="hi")]),
    ])
    (config.chunk_dir() / "IMG_8768__00000.mp4").write_bytes(b"x")
    (config.chunk_dir() / "IMG_8768__00240.mp4").write_bytes(b"x")
    stale = config.export_dir() / "srt" / "IMG_8768__00000-00060__x.srt"
    stale.write_text("1\n")

    ((plan,), _) = rename.build_plans(
        [Sidecar.load(config.sidecar_path("IMG_8768"))], {"IMG_8768": "grandpa_guessing_echo_show"}
    )
    rename.apply(plan)

    assert (config.raw_dir() / "grandpa_guessing_echo_show.MOV").exists()
    assert not (config.raw_dir() / "IMG_8768.MOV").exists()
    assert library.proxy_for("grandpa_guessing_echo_show").exists()
    assert [p.name for _, p in library.chunks_for("grandpa_guessing_echo_show")] == [
        "grandpa_guessing_echo_show__00000.mp4",
        "grandpa_guessing_echo_show__00240.mp4",
    ]
    assert not stale.exists(), "stale subtitle files must not linger under the old name"

    sc = Sidecar.load(config.sidecar_path("grandpa_guessing_echo_show"))
    assert sc.asset.stem == "grandpa_guessing_echo_show"
    assert sc.asset.file == "grandpa_guessing_echo_show.MOV"
    assert sc.segments[0].id == "grandpa_guessing_echo_show#00000.00"
    assert not config.sidecar_path("IMG_8768").exists()


def test_the_cameras_own_number_is_preserved(archive):
    make("IMG_8768", [Segment(id="IMG_8768#00000.00", start=0, end=60, title="t")])
    ((plan,), _) = rename.build_plans(
        [Sidecar.load(config.sidecar_path("IMG_8768"))], {"IMG_8768": "renamed"}
    )
    rename.apply(plan)
    assert Sidecar.load(config.sidecar_path("renamed")).asset.original_name == "IMG_8768.MOV"


def test_a_second_rename_keeps_the_first_original_name(archive):
    make("IMG_8768", [Segment(id="IMG_8768#00000.00", start=0, end=60, title="t")])
    for old, new in (("IMG_8768", "first"), ("first", "second")):
        ((plan,), _) = rename.build_plans(
            [Sidecar.load(config.sidecar_path(old))], {old: new}, only_camera_named=False
        )
        rename.apply(plan)
    assert Sidecar.load(config.sidecar_path("second")).asset.original_name == "IMG_8768.MOV"


@pytest.mark.parametrize(
    "raw,expected",
    [("Grandpa's old money box", "grandpa_s_old_money_box"),
     ("₹7 train to Bombay", "7_train_to_bombay"),
     ("  Spaces  ", "spaces")],
)
def test_slug(raw, expected):
    assert rename.slug(raw) == expected


def test_a_rename_carries_the_cuts_that_use_the_clip(tmp_path, monkeypatch):
    """A shot is (clip, in, out), so a rename is the one thing that breaks one.

    Everything else a reel depends on -- a renumbered segment, a re-analysis,
    an index rebuild -- it survives on its own.
    """
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    from greels import reel as store

    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "IMG_9001.MOV").write_bytes(b"x")
    Sidecar(
        asset=Asset(file="IMG_9001.MOV", stem="IMG_9001", duration=60, fps="30"),
        segments=[Segment(id="IMG_9001#00000.00", start=0, end=40,
                          title="Grandpa the hero", tags=["person:grandpa"])],
    ).save(config.sidecar_path("IMG_9001"))

    store.create("A cut", shots=[
        store.Shot(stem="IMG_9001", start=4, end=12, segment_id="IMG_9001#00000.00"),
        store.Shot(stem="elsewhere", start=0, end=3),
    ])

    plans, _ = rename.build_plans(library.load_all())
    rename.apply(plans[0])

    shots = store.load("a-cut").shots
    assert shots[0].stem == plans[0].new_stem
    assert shots[0].segment_id == f"{plans[0].new_stem}#00000.00"
    assert (shots[0].start, shots[0].end) == (4, 12)   # the trim is untouched
    assert shots[1].stem == "elsewhere"                # and so is everything else


def test_a_rename_carries_the_plans_that_hold_the_clip(tmp_path, monkeypatch):
    """A plan finds its moments by (clip, in, out) too, and it is the only
    record of what was published: a rename that left it behind orphaned them."""
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    from greels import plans as store

    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "IMG_9002.MOV").write_bytes(b"x")
    Sidecar(
        asset=Asset(file="IMG_9002.MOV", stem="IMG_9002", duration=60, fps="30"),
        segments=[Segment(id="IMG_9002#00000.00", start=0, end=40,
                          title="Building the house", tags=["person:relative"])],
    ).save(config.sidecar_path("IMG_9002"))
    held = store.create({"title": "The house", "moments": [
        {"id": "IMG_9002#00000.00", "title": "Building the house", "stem": "IMG_9002",
         "start": 0, "end": 40, "note": "check the age"},
        {"id": "elsewhere#00003.00", "stem": "elsewhere", "start": 3, "end": 9},
    ]})

    renames, _ = rename.build_plans(library.load_all())
    rename.apply(renames[0])

    first, second = store.load(held.id).moments
    assert first.stem == renames[0].new_stem
    assert first.id == f"{renames[0].new_stem}#00000.00"
    assert (first.start, first.end, first.note) == (0, 40, "check the age")
    assert second.stem == "elsewhere"
