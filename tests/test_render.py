"""Rendering a cut to a file you can watch.

ffmpeg is stood in for. What is worth testing is not that it encodes -- it
does -- but the decisions taken before it runs: what shape the reel comes out,
which shots make it in, that the joins carry audio, and that a render which
fails leaves nothing behind that looks like a finished one.
"""

from pathlib import Path

import pytest

from greels import config, export_reel, reel as store, render
from greels.reel import Reel, Shot
from greels.schema import Asset, Line, Segment, Sidecar


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    (tmp_path / "index").mkdir()
    (tmp_path / "raw").mkdir()
    return tmp_path


def _sidecar(stem, duration=120.0, fps="30", width=1080, height=1920,
             has_audio=True, segments=()):
    return Sidecar(
        asset=Asset(file=f"{stem}.MOV", stem=stem, duration=duration, fps=fps,
                    width=width, height=height, has_audio=has_audio),
        segments=list(segments),
    )


@pytest.fixture
def footage(root):
    """Three clips, with a file in raw/ and a proxy for each."""
    made = [
        _sidecar("a", segments=[
            Segment(id="a#00000.00", start=0, end=30, title="Marriage chat", lines=[
                Line(start=2, end=5, kok="Lagn zalato?", en="Are you married?"),
            ]),
        ]),
        _sidecar("b", duration=90.0),
        # The one landscape clip, and the only reason fill modes exist.
        _sidecar("c", duration=20.0, fps="24", width=1280, height=720),
    ]
    for sc in made:
        (config.raw_dir() / sc.asset.file).write_bytes(b"movie")
        proxy = config.proxy_dir()
        proxy.mkdir(parents=True, exist_ok=True)
        (proxy / f"{sc.asset.stem}.mp4").write_bytes(b"proxy")
    return made


def _reel(*shots, slug="test"):
    return Reel(slug=slug, title="Test reel", shots=list(shots))


def _resolve(reel, footage):
    return store.resolve(reel, footage)


def _plan(reel, footage, **kwargs):
    return render.build_plan(_resolve(reel, footage), Path("/out/test.mp4"), **kwargs)


def _graph(plan):
    return plan.args[plan.args.index("-filter_complex") + 1]


# --- what shape it comes out -------------------------------------------


def test_the_shape_follows_the_clip_with_the_most_screen_time(footage):
    """Not the first shot: a reel should not turn 720p because one opens it."""
    plan = _plan(_reel(
        Shot(stem="c", start=0, end=5),
        Shot(stem="a", start=0, end=30),
    ), footage)
    assert (plan.target.width, plan.target.height) == (1080, 1920)
    assert plan.target.fps == "30"


def test_a_reframe_fits_inside_the_source_rather_than_upscaling_it(footage):
    """A square from a vertical clip is 1080 out of the middle of it, not a
    1920x1920 blow-up of the same pixels."""
    plan = _plan(_reel(Shot(stem="a", start=0, end=10)), footage, aspect="1:1")
    assert (plan.target.width, plan.target.height) == (1080, 1080)

    plan = _plan(_reel(Shot(stem="a", start=0, end=10)), footage, aspect="16:9")
    assert (plan.target.width, plan.target.height) == (1080, 608)

    # And the width is always even: 720 * 9/16 is 405, which yuv420p cannot hold.
    plan = _plan(_reel(Shot(stem="c", start=0, end=10)), footage, aspect="9:16")
    assert (plan.target.width, plan.target.height) == (404, 720)


def test_an_explicit_height_is_a_resize_and_is_taken_at_its_word(footage):
    plan = _plan(_reel(Shot(stem="a", start=0, end=10)), footage, height=960)
    assert (plan.target.width, plan.target.height) == (540, 960)


def test_a_draft_off_the_proxies_is_proxy_sized(footage):
    plan = _plan(_reel(Shot(stem="a", start=0, end=10)), footage, source="proxy")
    assert plan.target.height == config.PROXY_HEIGHT
    assert plan.target.width == 270
    assert str(config.PROXY_FPS) == plan.target.fps
    assert plan.cuts[0].path == config.proxy_dir() / "a.mp4"


def test_an_unknown_aspect_is_refused_rather_than_guessed(footage):
    with pytest.raises(render.RenderError):
        _plan(_reel(Shot(stem="a", start=0, end=10)), footage, aspect="21:9")


# --- the same length as the timeline it stands in for ------------------


def test_shot_lengths_snap_to_the_sequence_timebase(footage):
    """The draft and the FCPXML have to be the same length, or it is not a draft."""
    reel = _reel(
        Shot(stem="a", start=0.0, end=10.017),   # 300.5 frames at 30 fps
        Shot(stem="b", start=5.0, end=9.99),
    )
    plan = _plan(reel, footage)
    assert [cut.duration for cut in plan.cuts] == [pytest.approx(10.0 + 1 / 30),
                                                   pytest.approx(5.0)]

    # Which is exactly what the timeline export lays out.
    document = export_reel.build_document(_resolve(reel, footage), captions=None)
    clips = document.getroot().iter("asset-clip")
    total = sum(
        int(clip.get("duration").split("/")[0]) for clip in clips
    ) / 3000
    assert total == pytest.approx(plan.duration)


def test_a_shot_shorter_than_a_frame_still_gets_one(footage):
    plan = _plan(_reel(Shot(stem="a", start=1.0, end=1.001)), footage)
    assert plan.cuts[0].duration == pytest.approx(1 / 30)


# --- which shots make it in --------------------------------------------


def test_a_loose_shot_is_left_out_and_said_out_loud(footage):
    """`resolve` has already reported it; the render must not compound that
    by dropping it silently."""
    plan = _plan(_reel(
        Shot(stem="a", start=0, end=10),
        Shot(stem="gone", start=0, end=5),
    ), footage)
    assert len(plan.cuts) == 1
    assert "no clip named gone" in plan.skipped[0]
    assert plan.skipped[0].startswith("shot 2")


def test_a_missing_proxy_is_reported_as_a_missing_proxy(footage):
    (config.proxy_dir() / "b.mp4").unlink()
    plan = _plan(_reel(
        Shot(stem="a", start=0, end=10),
        Shot(stem="b", start=0, end=5),
    ), footage, source="proxy")
    assert len(plan.cuts) == 1
    assert plan.skipped == ["shot 2: no proxy for b"]


def test_a_cut_with_nothing_left_is_an_error_not_an_empty_file(footage):
    with pytest.raises(render.RenderError):
        _plan(_reel(Shot(stem="gone", start=0, end=5)), footage)


# --- the filter graph --------------------------------------------------


def test_a_shot_already_the_right_shape_takes_the_cheap_chain(footage):
    graph = _graph(_plan(_reel(Shot(stem="a", start=0, end=10)), footage))
    assert "gblur" not in graph
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in graph


def test_a_landscape_shot_in_a_vertical_reel_gets_a_blurred_backing(footage):
    plan = _plan(_reel(
        Shot(stem="a", start=0, end=30),
        Shot(stem="c", start=0, end=5),
    ), footage)
    graph = _graph(plan)
    assert "gblur" in graph
    # Only the shot that needs it.
    assert graph.count("gblur") == 1
    assert "[bb1][ff1]overlay" in graph


def test_pad_and_crop_are_there_for_when_blur_is_wrong(footage):
    reel = _reel(Shot(stem="a", start=0, end=30), Shot(stem="c", start=0, end=5))
    assert "pad=1080:1920" in _graph(_plan(reel, footage, fill="pad"))
    assert "gblur" not in _graph(_plan(reel, footage, fill="crop"))
    with pytest.raises(render.RenderError):
        _plan(reel, footage, fill="letterbox")


def test_a_silent_clip_still_owes_the_join_a_track(footage):
    """Without one, concat drops a stream and every later shot slides against
    its picture."""
    footage[1] = _sidecar("b", duration=90.0, has_audio=False)
    plan = _plan(_reel(
        Shot(stem="a", start=0, end=10),
        Shot(stem="b", start=0, end=5),
        Shot(stem="a", start=20, end=25),
    ), footage)
    graph = _graph(plan)
    assert "anullsrc" in " ".join(plan.args)
    # The silent input is added after the shots, and lands in the right slot.
    assert "[3:a]asetpts=PTS-STARTPTS[a1]" in graph
    assert "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1" in graph


def test_the_shots_are_seeked_to_rather_than_decoded_up_to(footage):
    args = _plan(_reel(Shot(stem="a", start=42.5, end=52.5)), footage).args
    assert args[args.index("-i") - 4:args.index("-i")] == [
        "-ss", "42.500", "-t", "10.000"
    ]


def test_inputs_come_before_the_maps(footage):
    """ffmpeg reads its arguments positionally: a -map before an -i is taken as
    an option of that input."""
    args = _plan(_reel(Shot(stem="a", start=0, end=10)), footage).args
    assert max(i for i, a in enumerate(args) if a == "-i") < args.index("-map")


# --- levels ------------------------------------------------------------


def test_a_gain_is_applied_per_clip_and_the_mix_is_limited(footage):
    plan = _plan(_reel(
        Shot(stem="a", start=0, end=10),
        Shot(stem="b", start=0, end=10),
    ), footage, gains={"a": 4.3})
    graph = _graph(plan)
    assert "volume=4.3dB" in graph
    assert graph.count("volume=") == 1      # b was already where it should be
    assert "alimiter" in graph


def test_nothing_lifted_means_no_limiter(footage):
    graph = _graph(_plan(_reel(Shot(stem="a", start=0, end=10)), footage))
    assert "alimiter" not in graph
    assert "[acat]anull[aout]" in graph


# --- captions ----------------------------------------------------------


def test_captions_ride_along_as_a_track(footage, tmp_path):
    srt = tmp_path / "test.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    args = _plan(_reel(Shot(stem="a", start=0, end=10)), footage, subtitles=srt).args
    assert str(srt) in render.args_inputs(args)
    assert args[args.index("-c:s") - 2:args.index("-c:s")] == ["-map", "1:s"]
    assert "mov_text" in args


def test_the_subtitle_input_lands_after_the_silent_ones(footage, tmp_path):
    """Its index is a position in the input list, not a guess."""
    footage[1] = _sidecar("b", duration=90.0, has_audio=False)
    srt = tmp_path / "test.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    args = _plan(_reel(
        Shot(stem="a", start=0, end=10),
        Shot(stem="b", start=0, end=5),
    ), footage, subtitles=srt).args
    assert render.args_inputs(args)[-1] == str(srt)
    assert args[args.index("-c:s") - 1] == "3:s"


def test_burning_composites_a_card_per_line_and_maps_no_subtitle_stream(footage, tmp_path):
    """This ffmpeg has no libass and no drawtext, so a caption is a picture."""
    class Card:
        def __init__(self, start, end):
            self.path, self.start, self.end = tmp_path / "c.png", start, end
            self.width = self.height = 10

    cards = [Card(0.0, 2.0), Card(2.5, 4.0)]
    plan = _plan(_reel(Shot(stem="a", start=0, end=10)), footage,
                 burn=True, subtitles=tmp_path / "x.srt", cards=cards)
    graph = _graph(plan)

    assert graph.count("overlay=") == 2
    assert "enable='between(t,0.000,2.000)'" in graph
    assert "enable='between(t,2.500,4.000)'" in graph
    # Chained, so the second sits on top of the first's output.
    assert "[vb0][2:v]overlay" in graph
    assert "-c:s" not in plan.args
    assert plan.args[plan.args.index("-map") + 1] == "[vb1]"


def test_a_card_stops_when_its_line_does(footage, tmp_path):
    """A looped image is an infinite stream, and an overlay whose main input
    ends against one does not end -- it repeats the last frame for as long as
    the loop goes on. 28 of those turned a 70-second reel into 2 GB."""
    class Card:
        path, start, end, width, height = tmp_path / "c.png", 0.0, 2.0, 10, 10

    args = _plan(_reel(Shot(stem="a", start=0, end=10)), footage,
                 burn=True, cards=[Card()]).args
    at = args.index(str(Card.path))
    assert args[at - 7:at - 1] == ["-loop", "1", "-framerate", "2", "-t", "2.000"]
    assert "eof_action=pass" in _graph(
        _plan(_reel(Shot(stem="a", start=0, end=10)), footage,
              burn=True, cards=[Card()]))


def test_burning_with_nothing_to_draw_with_says_what_to_do_instead(footage, monkeypatch):
    from greels import captions

    monkeypatch.setattr(captions, "available", lambda: False)
    reel = _reel(Shot(stem="a", start=0, end=10), slug="test")
    with pytest.raises(render.RenderError) as caught:
        render.render(reel, sidecars=footage, captions="burn")
    assert "--captions soft" in str(caught.value)


# --- running one -------------------------------------------------------


@pytest.fixture
def ffmpeg(monkeypatch):
    """Record the calls, and write the file ffmpeg was told to write."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class Done:
            returncode = 0
            stderr = ""

        if args[0] == "ffmpeg" and args[-1].endswith(".mp4"):
            Path(args[-1]).write_bytes(b"video")
        return Done()

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    return calls


def test_a_render_is_moved_into_place_not_written_in_place(footage, ffmpeg):
    reel = _reel(Shot(stem="a", start=0, end=10))
    result = render.render(reel, sidecars=footage, captions="none")
    assert result.path == render.out_dir() / "test.mp4"
    assert result.path.exists()
    assert ffmpeg[0][-1].endswith(".tmp.mp4")
    assert not result.path.with_suffix(".tmp.mp4").exists()


def test_a_failed_render_leaves_nothing_that_looks_finished(footage, monkeypatch):
    class Failed:
        returncode = 1
        stderr = "Invalid data found"

    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: Failed())
    reel = _reel(Shot(stem="a", start=0, end=10))
    with pytest.raises(render.RenderError):
        render.render(reel, sidecars=footage, captions="none")
    assert not (render.out_dir() / "test.mp4").exists()


def test_the_gpu_failing_falls_back_to_software(footage, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class Result:
            returncode = 1 if "h264_videotoolbox" in args else 0
            stderr = "no encoder"

        if Result.returncode == 0:
            Path(args[-1]).write_bytes(b"video")
        return Result

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    render.render(_reel(Shot(stem="a", start=0, end=10)),
                  sidecars=footage, captions="none")
    assert "h264_videotoolbox" in calls[0]
    assert "libx264" in calls[1]


def test_a_render_is_recorded_on_the_cut_and_goes_stale_with_it(footage, ffmpeg):
    reel = _reel(Shot(stem="a", start=0, end=10))
    reel.save()
    render.render(reel, sidecars=footage, captions="none")

    reloaded = store.load("test")
    assert reloaded.rendered_at
    assert not reloaded.render_stale

    reloaded.shots[0].end = 12.0
    assert reloaded.render_stale
    # And the FCPXML's own staleness is a separate question.
    assert not reloaded.stale


def test_a_render_written_elsewhere_is_not_recorded_as_the_cut(footage, ffmpeg, tmp_path):
    """`--out somewhere.mp4` is a copy for someone, not the reel's own file."""
    reel = _reel(Shot(stem="a", start=0, end=10))
    reel.save()
    render.render(reel, dest=tmp_path / "for-mum.mp4", sidecars=footage,
                  captions="none")
    assert store.load("test").rendered_at is None


def test_the_captions_are_written_beside_the_work_not_over_the_export(footage, ffmpeg):
    """`<slug>.srt` belongs to `reel export`; two writers of one file is how
    a --lang both render silently replaces the English sidecar."""
    reel = _reel(Shot(stem="a", start=0, end=10))
    render.render(reel, sidecars=footage, lang="both")
    assert not (render.out_dir() / "test.srt").exists()
    written = config.media_root() / "work" / "render" / "test.both.srt"
    assert "Lagn zalato?" in written.read_text()


def test_a_file_too_big_to_send_says_what_would_fit(footage, ffmpeg):
    reel = _reel(Shot(stem="a", start=0, end=120))
    result = render.render(reel, sidecars=footage, captions="none")
    result.size_bytes = 250_000_000
    remark = result.too_big_to_send()
    assert "250 MB" in remark and "--bitrate 4M" in remark


def test_a_file_that_fits_says_nothing(footage, ffmpeg):
    result = render.render(_reel(Shot(stem="a", start=0, end=30)),
                           sidecars=footage, captions="none")
    assert result.too_big_to_send() == ""


@pytest.mark.parametrize("bad", [
    {"source": "originals"}, {"captions": "burnt"}, {"lang": "english"},
    {"aspect": "21:9"}, {"fill": "letterbox"},
])
def test_a_misspelt_option_is_refused_before_anything_renders(footage, ffmpeg, bad):
    """`--captions burnt` quietly writing a soft track is found out at the far
    end, by somebody watching a video with no captions on it."""
    with pytest.raises(render.RenderError):
        render.render(_reel(Shot(stem="a", start=0, end=10)),
                      sidecars=footage, **bad)
    assert ffmpeg == []


# --- saying how far in it has got ------------------------------------------


class _FakeProc:
    """Stands in for ffmpeg writing `-progress pipe:1` lines."""

    def __init__(self, lines, code=0):
        self.stdout = iter(lines)
        self._code = code

    def wait(self):
        return self._code


def test_progress_is_read_out_of_the_key_value_stream(monkeypatch):
    seen = []
    monkeypatch.setattr(render.subprocess, "Popen", lambda *a, **k: _FakeProc([
        "frame=30\n", "out_time_us=1000000\n", "progress=continue\n",
        "out_time_us=4500000\n", "progress=end\n",
    ]))
    render._run(["ffmpeg", "-i", "x", "out.mp4"], seen.append)
    assert seen == [1.0, 4.5]


def test_the_command_asks_for_progress_and_not_for_stats(monkeypatch):
    args = []
    monkeypatch.setattr(render.subprocess, "Popen",
                        lambda a, **k: args.extend(a) or _FakeProc([]))
    render._run(["ffmpeg", "-i", "x", "out.mp4"], lambda s: None)
    assert args[1:4] == ["-progress", "pipe:1", "-nostats"]


def test_a_failure_is_reported_with_what_ffmpeg_said(monkeypatch):
    monkeypatch.setattr(render.subprocess, "Popen", lambda *a, **k: _FakeProc([
        "out_time_us=1000000\n", "moov atom not found\n",
    ], code=1))
    with pytest.raises(render.RenderError, match="moov atom"):
        render._run(["ffmpeg", "-i", "x", "out.mp4"], lambda s: None)


def test_a_progress_line_that_makes_no_sense_is_ignored(monkeypatch):
    seen = []
    monkeypatch.setattr(render.subprocess, "Popen", lambda *a, **k: _FakeProc([
        "out_time_us=N/A\n", "out_time_us=2000000\n",
    ]))
    render._run(["ffmpeg", "-i", "x", "out.mp4"], seen.append)
    assert seen == [2.0]


def test_the_misnamed_millisecond_field_is_not_read(monkeypatch):
    """ffmpeg's `out_time_ms` is microseconds, and a field that is wrong about
    its own units will be a thousand times out the day somebody corrects it."""
    seen = []
    monkeypatch.setattr(render.subprocess, "Popen", lambda *a, **k: _FakeProc([
        "out_time_ms=9000000\n", "out_time_us=3000000\n",
    ]))
    render._run(["ffmpeg", "-i", "x", "out.mp4"], seen.append)
    assert seen == [3.0]


def test_a_render_the_disk_cannot_hold_is_refused_before_it_starts(footage, ffmpeg, monkeypatch):
    """This drive is 95% full, and running out halfway leaves a
    plausible-looking temporary file and nothing said about why."""
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(render.shutil, "disk_usage", lambda p: Usage(1, 1, 10_000_000))
    with pytest.raises(render.RenderError, match="GB free"):
        render.render(_reel(Shot(stem="a", start=0, end=120)),
                      sidecars=footage, captions="none")
    assert ffmpeg == []


def test_a_render_that_fits_is_not_mentioned(footage, ffmpeg, monkeypatch):
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(render.shutil, "disk_usage", lambda p: Usage(1, 1, 500_000_000_000))
    render.render(_reel(Shot(stem="a", start=0, end=120)),
                  sidecars=footage, captions="none")
    assert ffmpeg


def test_a_trim_made_while_it_rendered_is_not_overwritten(footage, monkeypatch):
    """A render takes a minute and the browser is not blocked during it. The
    copy being rendered is a minute out of date by the time it finishes."""
    reel = _reel(Shot(stem="a", start=0, end=10))
    reel.save()

    def edit_then_write(args, on_progress=None):
        moved = store.load("test")
        moved.shots[0].end = 25.0
        moved.save()
        Path(args[-1]).write_bytes(b"video")

    monkeypatch.setattr(render, "_run", edit_then_write)
    render.render(reel, sidecars=footage, captions="none")

    after = store.load("test")
    assert after.shots[0].end == 25.0          # the trim survived
    assert after.rendered_at                   # and the render was recorded
    assert after.render_stale                  # against what was rendered


def test_a_render_meant_to_be_sent_comes_down_to_fit(footage):
    target = render.Target(width=1080, height=1920, fps="30")
    full = render.bitrate_for(target)
    assert render.bitrate_for(target, duration=350, fit=True) < full
    # A short reel is already small and is left alone.
    assert render.bitrate_for(target, duration=70, fit=True) == full


def test_fitting_never_goes_below_something_watchable(footage):
    target = render.Target(width=1080, height=1920, fps="30")
    rate = render.bitrate_for(target, duration=36_000, fit=True)
    assert int(rate.rstrip("k")) >= 500
