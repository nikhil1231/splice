"""A cut, through everything that happens to the archive under it.

The reel model, the exporter and the CLI are each tested on their own. What
this covers is the property that only exists between them: a cut is the one
thing here nothing can regenerate, and the ordinary operations of the archive
-- a rename, a re-analysis, an index rebuild, a restore from backup -- must all
leave it standing.
"""

from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from splice import (
    backup, cli_reel, config, export_reel, index, library, reel as store, rename, thumbs,
)
from splice.schema import Asset, Line, Segment, Sidecar


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.setenv("SPLICE_ARCHIVE", str(tmp_path / "repo"))
    monkeypatch.setattr("splice.export_fcpxml.validate", lambda *a, **k: None)

    config.raw_dir().mkdir(parents=True)
    for stem, title in (("IMG_9001", "The money box"), ("lunch", "Lunch"),
                        ("balcony", "The balcony")):
        (config.raw_dir() / f"{stem}.MOV").write_bytes(b"footage")
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=120, fps="30",
                        width=1080, height=1920, created="2026-01-01T00:00:00Z"),
            segments=[
                Segment(id=f"{stem}#00000.00", start=0, end=40, title=title,
                        tags=["theme:money"], lines=[
                            Line(start=5, end=8, kok="poixe", en="money"),
                        ]),
                Segment(id=f"{stem}#00040.00", start=40, end=90, title=f"{title}, later",
                        tags=["theme:money"]),
            ],
        ).save(config.sidecar_path(stem))
    index.build()
    return tmp_path


@pytest.fixture
def cut(archive):
    cli_reel.reel_new("Money", from_tag=None, segment=[], said=None, search="", tag=["theme:money"],
                      rating="", limit=40, order="found", hook="What things cost")
    return store.load("money")


def _seconds(value: str) -> float:
    return float(Fraction(value[:-1])) if value.endswith("s") else float(value)


def test_a_search_becomes_a_cut_that_exports(cut, tmp_path):
    assert len(cut.shots) == 6
    project, resolved = export_reel.write(cut, dest_dir=tmp_path / "out")

    root = ET.parse(project).getroot()
    spine = root.find(".//spine")
    assert len(spine) == 6
    assert [c.get("name") for c in spine][0] == "The money box"
    # every ref resolves, and the timeline has no holes
    ids = {el.get("id") for el in root.find("resources")}
    cursor = 0.0
    for clip in spine:
        assert clip.get("ref") in ids
        assert _seconds(clip.get("offset")) == pytest.approx(cursor)
        cursor += _seconds(clip.get("duration"))
    assert _seconds(root.find(".//sequence").get("duration")) == pytest.approx(cursor)
    assert (tmp_path / "out" / "money.srt").read_text().count("money") == 3


def test_a_rename_leaves_the_cut_standing(cut):
    """The trims stay right while the clip they name stops existing."""
    plans, _ = rename.build_plans(library.load_all())
    assert [p.stem for p in plans] == ["IMG_9001"]
    rename.apply(plans[0])

    after = store.load("money")
    assert {s.stem for s in after.shots} == {"lunch", "balcony", plans[0].new_stem}
    assert [round(s.duration, 2) for s in after.shots] == [
        round(s.duration, 2) for s in cut.shots
    ]
    assert store.resolve(after).problems == []


def test_a_re_analysis_that_renumbers_the_segments_leaves_it_standing(cut):
    """Segment ids are derived from their own start time, so a prompt change
    renumbers every one of them."""
    sidecar = library.load_sidecar("lunch")
    sidecar.segments = [
        Segment(id="lunch#00003.00", start=3, end=45, title="Lunch, re-cut",
                tags=["theme:money"]),
        Segment(id="lunch#00045.00", start=45, end=90, title="Later, re-cut",
                tags=["theme:money"]),
    ]
    sidecar.save(config.sidecar_path("lunch"))

    resolved = store.resolve(store.load("money"))
    assert resolved.problems == []
    lunch = [s for s in resolved.shots if s.shot.stem == "lunch"]
    # The ids it recorded are gone; the ranges are not, and each reattaches to
    # whatever now covers its midpoint.
    assert [s.title for s in lunch] == ["Lunch, re-cut", "Later, re-cut"]


def test_an_index_rebuild_leaves_it_standing(cut):
    index.build()
    conn = index.connect()
    assert conn.execute("SELECT title FROM reels").fetchone()[0] == "Money"
    assert conn.execute("SELECT count(*) FROM reel_shots").fetchone()[0] == 6
    conn.close()


def test_a_backup_and_restore_round_trips_the_cut(cut):
    backup.backup()
    assert ((backup.archive_root() / "reels") / "money.reel.json").exists()

    store.path_for("money").unlink()
    assert store.load("money") is None

    backup.restore()
    assert store.load("money").duration == cut.duration


def test_a_clip_deleted_under_the_cut_is_reported_and_the_rest_still_exports(cut, tmp_path):
    config.sidecar_path("balcony").unlink()

    resolved = store.resolve(store.load("money"))
    assert len(resolved.problems) == 2
    assert all("no clip named balcony" in p for p in resolved.problems)

    project, _ = export_reel.write(store.load("money"), dest_dir=tmp_path / "out")
    spine = ET.parse(project).getroot().find(".//spine")
    assert len(spine) == 4
    assert all("balcony" not in (c.get("name") or "") for c in spine)


def test_a_poster_frame_follows_a_rename(cut, monkeypatch):
    """The frame is named for the segment id, which carries the clip's stem."""
    monkeypatch.setattr(thumbs.subprocess, "run", lambda cmd, **k: (
        __import__("pathlib").Path(cmd[-1]).write_bytes(b"jpeg"),
        type("Done", (), {"returncode": 0})(),
    )[-1])
    proxy = library.proxy_for("IMG_9001")
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(b"proxy")

    thumbs.build_all()
    before = {p.name for p in thumbs.thumb_dir().glob("IMG_9001*.jpg")}
    assert before

    plans, _ = rename.build_plans(library.load_all())
    rename.apply(plans[0])
    assert thumbs.prune() == len(before)
    assert not list(thumbs.thumb_dir().glob("IMG_9001*.jpg"))


def test_the_same_cut_renders_to_a_file_the_same_length_as_the_timeline(cut, tmp_path, monkeypatch):
    """The draft stands in for the timeline. One that runs two seconds shorter
    than the thing it stands in for is worse than no draft."""
    from splice import render

    calls = []

    def fake_run(args, on_progress=None):
        calls.append(args)
        Path(args[-1]).write_bytes(b"video")

    monkeypatch.setattr(render, "_run", fake_run)
    monkeypatch.setattr("splice.speech.runs_for", lambda stem, cache=None: [])

    project, resolved = export_reel.write(cut, dest_dir=tmp_path / "out")
    result = render.render(cut, dest=tmp_path / "out" / "money.mp4", captions="none")

    timeline = sum(
        _seconds(clip.get("duration"))
        for clip in ET.parse(project).getroot().find(".//spine")
    )
    assert result.duration == pytest.approx(timeline)
    assert len(result.plan.cuts) == len(cut.shots)


def test_a_render_of_the_cut_carries_every_caption_once(cut, tmp_path, monkeypatch):
    from splice import captions, render

    monkeypatch.setattr(render, "_run", lambda args, on_progress=None:
                        Path(args[-1]).write_bytes(b"video"))
    monkeypatch.setattr("splice.speech.runs_for", lambda stem, cache=None: [])
    if not captions.available():
        pytest.skip("no font or no Pillow on this machine")

    render.render(cut, dest=tmp_path / "out" / "money.mp4", captions="burn")
    lines = export_reel.caption_lines(store.resolve(cut))
    assert len(lines) == 3          # one per clip; the later segments are silent
    assert all(b > a for a, b, _, _ in lines)
