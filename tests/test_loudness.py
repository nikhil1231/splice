"""Matching the level between clips recorded on different afternoons.

ffmpeg is stood in for: what is worth testing is what is done with the number
it returns, and that the number is asked for once per clip rather than once
per render.
"""

import json

import pytest

from splice import config, library, loudness, reel as store
from splice.reel import Reel, Shot
from splice.schema import Asset, Sidecar


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    (tmp_path / "index").mkdir()
    proxies = config.proxy_dir()
    proxies.mkdir(parents=True)
    for stem in ("quiet", "loud", "silent"):
        (proxies / f"{stem}.mp4").write_bytes(b"proxy")
    return tmp_path


LEVELS = {"quiet": -24.7, "loud": -14.0, "silent": -70.5}


@pytest.fixture
def ffmpeg(monkeypatch):
    """Answer like `loudnorm=print_format=json` does, and count the asks."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        stem = args[args.index("-i") + 1].rsplit("/", 1)[-1].removesuffix(".mp4")

        class Done:
            returncode = 0
            stdout = ""
            stderr = (
                "[Parsed_loudnorm_0 @ 0x1] \n"
                + json.dumps({
                    "input_i": f"{LEVELS[stem]:.2f}",
                    "input_tp": "-1.40",
                    "input_lra": "9.80",
                    "input_thresh": "-34.16",
                })
                + "\n[out#0/null] video:0KiB\n"
            )

        return Done()

    monkeypatch.setattr(loudness.subprocess, "run", fake_run)
    return calls


def _sidecar(stem):
    return Sidecar(asset=Asset(file=f"{stem}.MOV", stem=stem, duration=60.0))


def _resolved(*stems):
    reel = Reel(slug="r", title="R",
                shots=[Shot(stem=s, start=0, end=10) for s in stems])
    return store.resolve(reel, [_sidecar(s) for s in stems])


# --- the measurement ---------------------------------------------------


def test_a_quiet_clip_is_lifted_toward_the_target(ffmpeg):
    found = loudness.for_stem("quiet")
    assert found.lufs == -24.7
    assert found.gain_db == pytest.approx(loudness.TARGET_LUFS + 24.7)


def test_a_loud_one_is_brought_down(ffmpeg):
    assert loudness.for_stem("loud").gain_db == pytest.approx(-2.0)


def test_a_silent_track_is_not_amplified_into_noise(ffmpeg):
    """-70 LUFS is a room, not a recording; +12 dB of it is a worse room."""
    assert loudness.for_stem("silent") is None


def test_a_gain_is_capped_at_something_a_level_difference_could_explain(monkeypatch):
    huge = loudness.Measurement(lufs=-45.0, peak_db=-20.0)
    assert huge.gain_db == loudness.MAX_GAIN_DB


def test_unreadable_output_is_no_measurement_rather_than_a_crash(monkeypatch):
    class Done:
        returncode = 1
        stdout = ""
        stderr = "Invalid data found when processing input"

    monkeypatch.setattr(loudness.subprocess, "run", lambda *a, **k: Done())
    assert loudness.for_stem("quiet") is None


def test_a_clip_with_no_proxy_is_not_measured(ffmpeg):
    assert loudness.for_stem("missing") is None
    assert ffmpeg == []


# --- measuring once ----------------------------------------------------


def test_the_measurement_is_kept_and_not_taken_again(ffmpeg):
    loudness.for_stem("quiet")
    loudness.for_stem("quiet")
    assert len(ffmpeg) == 1
    assert "quiet" in json.loads(loudness.cache_path().read_text())


def test_a_replaced_clip_is_measured_again(ffmpeg):
    loudness.for_stem("quiet")
    proxy = library.proxy_for("quiet")
    proxy.write_bytes(b"a different recording entirely")
    loudness.for_stem("quiet")
    assert len(ffmpeg) == 2


def test_a_clip_that_cannot_be_measured_is_not_retried_every_render(ffmpeg):
    loudness.for_stem("silent")
    assert loudness.for_stem("silent") is None
    assert len(ffmpeg) == 1


def test_a_corrupt_cache_costs_a_measurement_not_a_render(ffmpeg):
    loudness.cache_path().parent.mkdir(parents=True, exist_ok=True)
    loudness.cache_path().write_text("{not json")
    assert loudness.for_stem("quiet").lufs == -24.7


# --- what a cut asks for -----------------------------------------------


def test_gains_are_per_clip_and_asked_for_once(ffmpeg):
    gains = loudness.gains_for(_resolved("quiet", "loud", "quiet"))
    assert set(gains) == {"quiet", "loud"}
    assert len(ffmpeg) == 2
    assert gains["loud"] == -2.0


def test_a_difference_nobody_can_hear_is_left_alone(ffmpeg, monkeypatch):
    monkeypatch.setitem(LEVELS, "loud", loudness.TARGET_LUFS - 0.1)
    assert loudness.gains_for(_resolved("loud")) == {}


def test_a_clip_that_cannot_be_measured_costs_the_match_not_the_render(ffmpeg):
    gains = loudness.gains_for(_resolved("silent", "quiet"))
    assert "silent" not in gains
    assert "quiet" in gains


# --- somewhere with no limiter ---------------------------------------------


@pytest.fixture
def peaky(monkeypatch):
    """Two clips 6 dB apart, the quieter one already peaking near full scale."""
    monkeypatch.setattr(loudness, "for_stem", lambda stem, cache=None: {
        "quiet": loudness.Measurement(lufs=-24.0, peak_db=-3.0),
        "loud": loudness.Measurement(lufs=-18.0, peak_db=-6.0),
    }[stem])


def test_without_a_ceiling_the_gains_reach_the_target(peaky):
    gains = loudness.gains_for(_resolved("quiet", "loud"))
    assert gains == {"quiet": 8.0, "loud": 2.0}


def test_a_ceiling_brings_the_whole_set_down_together(peaky):
    """Final Cut applies a clip's volume and clips whatever goes over, and it
    has no limiter on the way in. Capping each clip to its own headroom would
    undo the matching, which is the whole point."""
    gains = loudness.gains_for(_resolved("quiet", "loud"), ceiling=-1.0)
    # quiet would peak at +5.0; everything comes down by 6.
    assert gains == {"quiet": 2.0, "loud": -4.0}
    assert gains["quiet"] - gains["loud"] == 6.0


def test_a_set_with_headroom_to_spare_is_not_pulled_down(monkeypatch):
    monkeypatch.setattr(loudness, "for_stem", lambda stem, cache=None:
                        loudness.Measurement(lufs=-20.0, peak_db=-14.0))
    assert loudness.gains_for(_resolved("quiet"), ceiling=-1.0) == {"quiet": 4.0}
