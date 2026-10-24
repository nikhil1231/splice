"""`greels align` -- moving the subtitle timings onto the speech.

The detector is stood in for. What matters is what the command does with what
it hears: that lines move, that segment boundaries do not, and that running it
twice is not different from running it once.
"""

import pytest

from greels import cli, config, library
from greels.schema import Asset, Line, Segment, Sidecar


# --- greels align ----------------------------------------------------------


@pytest.fixture
def spoken(tmp_path, monkeypatch):
    """A clip whose lines were estimated a little off the actual speech."""
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    proxies = config.proxy_dir()
    proxies.mkdir(parents=True, exist_ok=True)
    (proxies / "a.mp4").write_bytes(b"proxy")
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=40, fps="30"),
        segments=[
            Segment(id="a#00000.00", start=0, end=30, title="Chat", lines=[
                Line(start=4.0, end=6.5, en="Grandpa, when is this from?"),
                Line(start=20.0, end=22.0, en="Sixty, seventy years ago."),
            ]),
        ],
    ).save(config.sidecar_path("a"))
    monkeypatch.setattr(
        "greels.speech.runs_for", lambda stem, cache=None: [[4.7, 9.0], [19.0, 25.0]]
    )
    return tmp_path


def test_align_moves_the_lines_onto_the_speech(spoken):
    cli.align(only=None, window=None, dry_run=False)
    lines = library.load_sidecar("a").segments[0].lines
    assert lines[0].start == pytest.approx(4.62)
    assert lines[0].end - lines[0].start == pytest.approx(2.5)
    assert lines[1].start == pytest.approx(18.92)


def test_align_never_moves_a_segment_boundary(spoken):
    """Segment ids are derived from their start time: moving one renumbers it,
    which costs every reel that names it its provenance."""
    cli.align(only=None, window=None, dry_run=False)
    segment = library.load_sidecar("a").segments[0]
    assert (segment.start, segment.end, segment.id) == (0, 30, "a#00000.00")


def test_a_dry_run_moves_nothing(spoken):
    cli.align(only=None, window=None, dry_run=True)
    assert library.load_sidecar("a").segments[0].lines[0].start == 4.0


def test_align_keeps_the_transcript_it_is_about_to_change(spoken):
    from greels import trash

    cli.align(only=None, window=None, dry_run=False)
    assert [e for e in trash.entries() if e.original.startswith("a.seg")]


def test_align_is_idempotent(spoken):
    cli.align(only=None, window=None, dry_run=False)
    first = [l.model_copy() for l in library.load_sidecar("a").segments[0].lines]
    cli.align(only=None, window=None, dry_run=False)
    after = library.load_sidecar("a").segments[0].lines
    assert [(l.start, l.end) for l in after] == [(l.start, l.end) for l in first]


def test_a_clip_with_no_proxy_is_left_alone(spoken, monkeypatch):
    monkeypatch.setattr("greels.speech.runs_for", lambda stem, cache=None: [])
    cli.align(only=None, window=None, dry_run=False)
    assert library.load_sidecar("a").segments[0].lines[0].start == 4.0


def test_align_reads_each_sidecar_again_before_changing_it(spoken, monkeypatch):
    """Sixty sidecars are loaded at the start; by the sixtieth the first is
    seconds old, which is long enough for a correction typed in the browser."""
    real = library.load_sidecar

    def edit_once(stem):
        got = real(stem)
        if got and not getattr(edit_once, "done", False):
            edit_once.done = True
            got.segments[0].title = "Corrected in the browser"
            got.save(config.sidecar_path(stem))
            return real(stem)
        return got

    monkeypatch.setattr(library, "load_sidecar", edit_once)
    cli.align(only=None, window=None, dry_run=False)

    after = real("a")
    assert after.segments[0].title == "Corrected in the browser"
    assert after.segments[0].lines[0].start == pytest.approx(4.62)
