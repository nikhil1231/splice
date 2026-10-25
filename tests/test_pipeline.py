"""Behaviour around re-running the pipeline."""

import subprocess

import pytest

from splice import proxy
from splice.cli import _carry_review_state
from splice.schema import Asset, Segment, Sidecar


def sidecar(segments):
    return Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=100, fps="30"), segments=segments
    )


def test_review_state_follows_the_moment_not_the_index():
    # The model re-segmented: the old 40-60s favourite now sits inside 35-70s.
    old = sidecar([
        Segment(id="a#0", start=0, end=40, title="one"),
        Segment(id="a#40", start=40, end=60, title="two", rating="favorite", status="used"),
    ])
    new = sidecar([
        Segment(id="a#0", start=0, end=35, title="A"),
        Segment(id="a#35", start=35, end=70, title="B"),
        Segment(id="a#70", start=70, end=100, title="C"),
    ])
    _carry_review_state(old, new)
    assert [s.rating for s in new.segments] == ["none", "favorite", "none"]
    assert new.segments[1].status == "used"


def test_untouched_segments_carry_nothing():
    old = sidecar([Segment(id="a#0", start=0, end=50, title="one")])
    new = sidecar([Segment(id="a#0", start=0, end=50, title="A")])
    _carry_review_state(old, new)
    assert new.segments[0].rating == "none"
    assert new.segments[0].status == "indexed"


def test_a_reanalysis_remembers_what_final_cut_was_given():
    """A fresh delivery record made a re-timed clip look newly imported, so
    sync confirmed it against its new segments and never offered it again."""
    old = sidecar([Segment(id="a#0", start=0, end=50, title="one")])
    old.exported.imported_at = "2026-09-13T10:00:00+00:00"
    old.exported.imported_sha = old.content_sha()
    new = sidecar([Segment(id="a#0", start=0, end=30, title="A")])
    _carry_review_state(old, new)
    assert new.exported.imported_at == "2026-09-13T10:00:00+00:00"
    assert new.exported.imported_sha != new.content_sha(), "so it is offered again"


def test_state_is_dropped_when_nothing_covers_the_old_moment():
    old = sidecar([Segment(id="a#90", start=90, end=100, title="one", rating="favorite")])
    new = sidecar([Segment(id="a#0", start=0, end=10, title="A")])
    _carry_review_state(old, new)  # must not raise
    assert new.segments[0].rating == "none"


# --- encoding ----------------------------------------------------------


def test_hwaccel_is_retried_without_acceleration(monkeypatch):
    """Not every container decodes on the GPU; a failure must not be fatal."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        failed = "-hwaccel" in cmd
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, "", "boom")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    proxy._run(["ffmpeg", "-hwaccel", "videotoolbox", "-i", "x"],
               allow_hwaccel_fallback=True)

    assert len(calls) == 2
    assert "-hwaccel" not in calls[1]


def test_a_real_failure_still_raises(monkeypatch):
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no such file"),
    )
    with pytest.raises(proxy.EncodeError, match="no such file"):
        proxy._run(["ffmpeg", "-i", "missing.MOV"])


# --- what invalidates an analysis --------------------------------------


def test_growing_the_vocabulary_does_not_invalidate_the_archive(tmp_path, monkeypatch):
    """Promoting one tag used to mark every clip stale and trigger hours of
    re-analysis. Vocabulary growth is reclassify's job, not analyze's."""
    from splice import config
    from splice.analyze import prompt_sha

    before = prompt_sha()

    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text("namespaces: [object]\ntags:\n  object:glasses: {fcp_label: Glasses}\n")
    monkeypatch.setattr(config, "taxonomy_path", lambda: taxonomy)

    assert prompt_sha() == before


def test_editing_the_prompt_does_invalidate(tmp_path, monkeypatch):
    from splice import config
    from splice.analyze import prompt_sha

    before = prompt_sha()
    edited = tmp_path / "analyze.md"
    edited.write_text(config.prompt_path().read_text() + "\nAlso note the weather.\n")
    monkeypatch.setattr(config, "prompt_path", lambda name="analyze.md": edited)

    assert prompt_sha() != before


# --- what a proxy is ---------------------------------------------------


def _ffmpeg(monkeypatch, keyframes=(0.0, 240.0, 480.0)):
    """Record the commands and pretend each one wrote its output.

    ffprobe is answered rather than recorded: `build_chunks` asks it where the
    keyframes are before deciding whether a chunk can be copied, and a test
    about the encode should not have to count that call. `keyframes` is what
    it finds -- pass none to stand in for a proxy built before they were
    forced at the chunk boundaries.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        from pathlib import Path

        if cmd[0] == "ffprobe":
            packets = "".join(f"{t:.6f},K__\n" for t in keyframes)
            return subprocess.CompletedProcess(cmd, 0, packets, "")
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    return calls


def test_a_proxy_is_480p_mono_and_seekable(tmp_path, monkeypatch):
    """Everything downstream reads this: the model, the browser's player, the
    poster frames, the loudness and the speech detection."""
    from splice import config

    calls = _ffmpeg(monkeypatch)
    source = tmp_path / "a.MOV"
    source.write_bytes(b"footage")
    proxy.build_proxy(source, tmp_path / "a.mp4")

    cmd = calls[0]
    # The rate drop is inside the filter chain and ahead of the scale, not an
    # output `-r` behind it: 186.5 s against 160.9 s for a 17-minute clip,
    # because the scaler is what the dropped frames would otherwise cost.
    assert cmd[cmd.index("-vf") + 1] == f"fps={config.PROXY_FPS},scale=-2:{config.PROXY_HEIGHT}"
    assert "-r" not in cmd
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert "+faststart" in cmd            # the browser seeks in this
    assert "-hwaccel" in cmd              # 45 s against 16 for the same slice


def test_a_proxy_carries_a_keyframe_where_every_chunk_will_be_cut(tmp_path, monkeypatch):
    """Otherwise a copied chunk snaps back to x264's own keyframe, up to 16.7 s
    earlier, and every timestamp the model reports for it is wrong by that."""
    from splice import config

    calls = _ffmpeg(monkeypatch)
    source = tmp_path / "a.MOV"
    source.write_bytes(b"footage")
    proxy.build_proxy(source, tmp_path / "a.mp4")

    cmd = calls[0]
    assert cmd[cmd.index("-force_key_frames") + 1] == (
        f"expr:gte(t,n_forced*{config.CHUNK_SECONDS})"
    )


def test_a_proxy_is_not_built_twice(tmp_path, monkeypatch):
    calls = _ffmpeg(monkeypatch)
    source = tmp_path / "a.MOV"
    source.write_bytes(b"footage")
    dest = tmp_path / "a.mp4"

    proxy.build_proxy(source, dest)
    proxy.build_proxy(source, dest)
    assert len(calls) == 1

    proxy.build_proxy(source, dest, force=True)
    assert len(calls) == 2


def test_a_replaced_original_is_proxied_again(tmp_path, monkeypatch):
    """The proxy is older than the file it stands for, so it is not the proxy
    of that file any more."""
    import os
    import time

    calls = _ffmpeg(monkeypatch)
    source = tmp_path / "a.MOV"
    source.write_bytes(b"footage")
    dest = tmp_path / "a.mp4"
    proxy.build_proxy(source, dest)

    later = time.time() + 10
    os.utime(source, (later, later))
    proxy.build_proxy(source, dest)
    assert len(calls) == 2


def test_a_chunk_is_named_for_where_it_starts_and_overlaps_the_next(tmp_path, monkeypatch):
    from splice import config

    calls = _ffmpeg(monkeypatch)
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "duration_of", lambda path: 500.0)
    source = tmp_path / "a.mp4"
    source.write_bytes(b"proxy")

    chunks = proxy.build_chunks(source, "a", chunk_seconds=240)

    assert [round(offset) for offset, _ in chunks] == [0, 240, 480]
    assert [path.name for _, path in chunks] == [
        "a__00000.mp4", "a__00240.mp4", "a__00480.mp4",
    ]
    first = calls[0]
    assert first[first.index("-ss") + 1] == "0.000"
    # 240 plus the overlap that stops a sentence being cut dead at the join.
    assert first[first.index("-t") + 1] == f"{240 + config.CHUNK_OVERLAP:.3f}"


def test_a_chunk_that_lands_on_a_keyframe_is_copied_rather_than_encoded(tmp_path, monkeypatch):
    """The proxy is already 480p15 at the quality analysis reads, so cutting it
    up is a copy of some of its packets. 5.04 s to 0.10 s per chunk."""
    calls = _ffmpeg(monkeypatch)
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "duration_of", lambda path: 500.0)
    source = tmp_path / "a.mp4"
    source.write_bytes(b"proxy")

    proxy.build_chunks(source, "a", chunk_seconds=240)

    for cmd in calls:
        assert cmd[cmd.index("-c") + 1] == "copy"
        assert "libx264" not in cmd


def test_a_proxy_without_those_keyframes_is_chunked_the_slow_way(tmp_path, monkeypatch):
    """A proxy built before they were forced has keyframes every 16.7 s and
    none at 240. Copying from there would move the chunk without saying so, so
    it is encoded instead -- slower, and still where it claims to start."""
    calls = _ffmpeg(monkeypatch, keyframes=(0.0, 16.667, 33.333))
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.setattr(proxy, "duration_of", lambda path: 500.0)
    source = tmp_path / "a.mp4"
    source.write_bytes(b"proxy")

    proxy.build_chunks(source, "a", chunk_seconds=240)

    # The first chunk starts at 0, where every proxy has a keyframe.
    assert calls[0][calls[0].index("-c") + 1] == "copy"
    for cmd in calls[1:]:
        assert "libx264" in cmd
        assert "-c" not in cmd
