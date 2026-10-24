"""Poster frames.

ffmpeg is stood in for: what matters here is which frame is asked for, where it
is cached, and that a re-analysis does not leave you looking at a picture of a
boundary that no longer exists.
"""

import pytest

from greels import config, library, thumbs
from greels.schema import Asset, Segment, Sidecar


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    proxy = library.proxy_for("a")
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(b"proxy")
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=120, fps="30"),
        segments=[
            Segment(id="a#00000.00", start=0, end=40, title="One"),
            Segment(id="a#00040.00", start=40, end=44, title="Two"),
        ],
    ).save(config.sidecar_path("a"))
    return tmp_path


@pytest.fixture
def ffmpeg(monkeypatch):
    """Record what was asked for, and write a file where it was asked to."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # the output path is the last argument
        from pathlib import Path

        Path(cmd[-1]).write_bytes(b"jpeg")

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(thumbs.subprocess, "run", fake_run)
    return calls


def _seek(cmd):
    return float(cmd[cmd.index("-ss") + 1])


def test_the_frame_is_taken_a_beat_into_the_moment(archive, ffmpeg):
    """The first frame of a segment is often the tail of the last shot."""
    sidecar = library.load_sidecar("a")
    thumbs.build(sidecar, sidecar.segments[0])
    assert _seek(ffmpeg[0]) == pytest.approx(3.0)   # capped, not 25% of 40s


def test_a_short_moment_takes_a_quarter_of_the_way_in(archive, ffmpeg):
    sidecar = library.load_sidecar("a")
    thumbs.build(sidecar, sidecar.segments[1])
    assert _seek(ffmpeg[0]) == pytest.approx(41.0)  # 40 + 25% of 4s


def test_a_frame_is_only_taken_once(archive, ffmpeg):
    sidecar = library.load_sidecar("a")
    thumbs.build(sidecar, sidecar.segments[0])
    thumbs.build(sidecar, sidecar.segments[0])
    assert len(ffmpeg) == 1

    thumbs.build(sidecar, sidecar.segments[0], force=True)
    assert len(ffmpeg) == 2


def test_a_clip_with_no_proxy_has_no_frame(archive, ffmpeg):
    library.proxy_for("a").unlink()
    sidecar = library.load_sidecar("a")
    assert thumbs.build(sidecar, sidecar.segments[0]) is None
    assert ffmpeg == []


def test_ensure_finds_the_segment_from_its_id_alone(archive, ffmpeg):
    assert thumbs.ensure("a#00040.00") is not None
    assert thumbs.ensure("a#nope") is None
    assert thumbs.ensure("missing#00000.00") is None


def test_a_renumbered_segment_asks_for_a_different_frame(archive, ffmpeg):
    """So a re-analysis cannot leave you looking at the wrong moment."""
    sidecar = library.load_sidecar("a")
    thumbs.build_all([sidecar])
    assert len(list(thumbs.thumb_dir().glob("*.jpg"))) == 2

    sidecar.segments = [Segment(id="a#00002.00", start=2, end=40, title="One, later")]
    sidecar.save(config.sidecar_path("a"))

    reloaded = library.load_sidecar("a")
    thumbs.build_all([reloaded])
    assert len(list(thumbs.thumb_dir().glob("*.jpg"))) == 3
    assert thumbs.prune([reloaded]) == 2
    assert len(list(thumbs.thumb_dir().glob("*.jpg"))) == 1


def test_a_failed_extraction_leaves_nothing_behind(archive, monkeypatch):
    class Failed:
        returncode = 1

    monkeypatch.setattr(thumbs.subprocess, "run", lambda *a, **k: Failed())
    sidecar = library.load_sidecar("a")
    assert thumbs.build(sidecar, sidecar.segments[0]) is None
    assert list(thumbs.thumb_dir().glob("*")) == [] or not thumbs.thumb_dir().exists()
