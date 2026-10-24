"""Frame-alignment maths. Final Cut rejects a file that gets this wrong."""

from fractions import Fraction

import pytest

from greels.fcptime import frame_duration, frames, rational, snap, timebase


def test_integer_rate_uses_apple_timescale():
    assert frame_duration("30") == "100/3000s"
    assert frame_duration("25") == "100/2500s"


def test_drop_frame_rate_keeps_its_rational():
    assert frame_duration("30000/1001") == "1001/30000s"


def test_zero_is_written_bare():
    assert rational(0, "30") == "0s"


@pytest.mark.parametrize("fps", ["30", "25", "60", "30000/1001", "24000/1001"])
@pytest.mark.parametrize("seconds", [0.0, 1.0, 62.1, 318.2, 1190.34])
def test_every_time_is_a_whole_number_of_frames(fps, seconds):
    text = rational(seconds, fps)
    if text == "0s":
        return
    numerator, denominator = (int(x) for x in text.removesuffix("s").split("/"))
    ticks, scale = timebase(fps)
    assert denominator == scale
    assert numerator % ticks == 0, "time must land on a frame boundary"


@pytest.mark.parametrize("fps", ["30", "30000/1001"])
def test_snap_agrees_with_the_string_that_gets_written(fps):
    for seconds in (0.0, 1.0, 62.1, 1190.34):
        text = rational(seconds, fps)
        if text == "0s":
            assert snap(seconds, fps) == 0.0
            continue
        numerator, denominator = (int(x) for x in text.removesuffix("s").split("/"))
        assert snap(seconds, fps) == pytest.approx(numerator / denominator)


def test_snapping_never_moves_a_time_by_more_than_half_a_frame():
    for fps in ("30", "30000/1001", "60"):
        for seconds in (0.017, 62.1, 318.23, 1190.34):
            assert abs(snap(seconds, fps) - seconds) <= 0.5 / float(Fraction(fps)) + 1e-9


def test_degenerate_rate_falls_back_rather_than_dividing_by_zero():
    assert frames(1.0, "0/1") == 30
    assert timebase("0/1") == (100, 3000)
