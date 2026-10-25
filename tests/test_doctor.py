"""`splice doctor`.

The question it answers is not "where has the footage got to" but "does the
machine around it still work" — after a laptop change, an OS upgrade, or a
drive that did not mount. So the tests are about what it notices missing.
"""

import pytest
import typer

from splice import cli, config, index, library
from splice.schema import Asset, Line, Segment, Sidecar


@pytest.fixture
def healthy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.delenv("SPLICE_FCP_LIBRARY", raising=False)
    config.raw_dir().mkdir(parents=True)
    config.proxy_dir().mkdir(parents=True)
    (config.raw_dir() / "a.MOV").write_bytes(b"x")
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=60, fps="30"),
        segments=[Segment(id="a#00000.00", start=0, end=30, title="One")],
    ).save(config.sidecar_path("a"))
    index.build()
    return tmp_path


def _run():
    try:
        cli.doctor(editor=None)
        return 0
    except typer.Exit as exc:
        return exc.exit_code


def test_a_healthy_archive_reports_no_problems(healthy, capsys):
    assert _run() == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    # The archive it reports on is this one, not the real one. Asserting the
    # real archive's segment count is absent used to do this, until a tmp
    # directory called pytest-192 happened.
    assert "1 segment" in out
    assert str(healthy) in out


def test_a_sidecar_whose_footage_is_gone_is_noticed(healthy, capsys):
    (config.raw_dir() / "a.MOV").unlink()
    _run()
    out = capsys.readouterr().out
    assert "raw/ does not have" in out


def test_a_clip_that_has_never_been_scanned_is_noticed(healthy, capsys):
    (config.raw_dir() / "b.MOV").write_bytes(b"x")
    _run()
    assert "have no sidecar" in capsys.readouterr().out


def test_an_index_behind_the_sidecars_is_noticed(healthy, capsys):
    sidecar = Sidecar.model_validate_json(config.sidecar_path("a").read_text())
    sidecar.segments.append(Segment(id="a#00030.00", start=30, end=50, title="Two"))
    sidecar.save(config.sidecar_path("a"))

    _run()
    assert "run `splice index`" in capsys.readouterr().out


def test_an_unmounted_media_root_is_a_failure_not_a_note(healthy, monkeypatch, tmp_path, capsys):
    """This is the one that means "stop and check the drive"."""
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path / "unmounted"))
    assert _run() == 1
    assert "unmounted volume" in capsys.readouterr().out


def test_a_cut_with_a_shot_that_came_loose_is_a_failure(healthy, capsys):
    from splice import reel as store

    store.create("A cut", shots=[store.Shot(stem="gone", start=0, end=5)])
    assert _run() == 1
    assert "no clip named gone" in capsys.readouterr().out


def test_a_missing_tool_is_reported_against_what_it_is_for(healthy, monkeypatch, capsys):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None if name == "ffmpeg" else "/bin/" + name)
    assert _run() == 1
    out = capsys.readouterr().out
    assert "FAIL  ffmpeg" in out
    assert "poster frames" in out


def test_status_says_how_far_the_review_pass_has_got(healthy, capsys):
    """Everything downstream waits on it: favourites-first suggestions, `reel
    unused`, and not being offered again a moment you turned down."""
    from splice import library

    cli.status(editor=None)
    assert "judged   : 0 of 1" in capsys.readouterr().out

    sidecar = library.load_sidecar("a")
    sidecar.segments[0].rating = "favorite"
    sidecar.save(config.sidecar_path("a"))
    index.build()

    cli.status(editor=None)
    assert "judged   : 1 of 1  (1 kept, 0 rejected)" in capsys.readouterr().out


def test_doctor_says_how_much_room_is_left(healthy, capsys, monkeypatch):
    """Proxies, poster frames and renders all land on this disk, and a render
    is a couple of hundred megabytes."""
    from collections import namedtuple

    import shutil

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(1, 1, 4_000_000_000))
    assert _run() == 0
    out = capsys.readouterr().out
    assert "note" in out and "4 GB free" in out


def test_the_browser_is_offered_the_same_checks_the_terminal_prints(healthy):
    """`doctor` printed all of this into a terminal, which on an evening spent
    in the browser is nowhere. Both read one list, so the header cannot end up
    reassuring you about an archive the terminal is complaining about."""
    from splice import health
    from splice.server import Api, Autosave

    payload = Api(autosave=Autosave(after=0)).health()

    assert payload["problems"] == 0
    assert {c["label"] for c in payload["checks"]} == {
        c.label for c in health.checkup().checks
    }
    assert {"section", "label", "detail", "level", "fix"} == set(payload["checks"][0])


def test_a_check_names_the_command_that_would_deal_with_it(healthy, monkeypatch):
    """"12 line timings sit off the speech" does not tell anybody it means
    `align`. The browser puts the fix under the complaint, so the name has to
    come from here rather than from a lookup table in the page."""
    from splice import health, speech

    monkeypatch.setattr(speech, "_load_cache", lambda: {"a": {"runs": [[5.0, 9.0]]}})
    monkeypatch.setattr(
        speech, "snap_lines", lambda runs, lines, window=None: [(5.0, 9.0) for _ in lines]
    )
    sidecar = library.load_sidecar("a")
    sidecar.segments[0].lines = [Line(start=0.0, end=3.0, kok="x", en="x")]
    sidecar.save(config.sidecar_path("a"))

    subtitles = next(c for c in health.checkup().checks if c.label == "subtitles")
    assert subtitles.level == health.NOTE
    assert subtitles.fix == "align"
