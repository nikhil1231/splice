"""Finding where somebody is talking, and moving a trim onto it.

ffmpeg is stood in for. What matters is the arithmetic on either side of it:
that silences become runs of speech and not the other way round, that a
boundary only moves when there is somewhere near to move it, and that a snap
can never turn a shot into nothing.
"""

import json

import pytest

from splice import config, library, speech
from splice.schema import Asset, Sidecar


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    (tmp_path / "index").mkdir()
    proxies = config.proxy_dir()
    proxies.mkdir(parents=True)
    (proxies / "a.mp4").write_bytes(b"proxy")
    Sidecar(asset=Asset(file="a.MOV", stem="a", duration=30.0)).save(
        config.sidecar_path("a")
    )
    return tmp_path


def _detector(monkeypatch, silences, code=0):
    """Answer like `silencedetect` does: only the gaps, in its log format."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        lines = []
        for start, end in silences:
            lines.append(f"[silencedetect @ 0x1] silence_start: {start}")
            if end is not None:
                lines.append(
                    f"[silencedetect @ 0x1] silence_end: {end} | "
                    f"silence_duration: {end - start}"
                )

        class Done:
            returncode = code
            stdout = ""
            stderr = "\n".join(lines) + "\n"

        return Done()

    monkeypatch.setattr(speech.subprocess, "run", fake_run)
    return calls


# --- hearing the file --------------------------------------------------


def test_the_runs_are_the_gaps_between_the_silences(monkeypatch):
    _detector(monkeypatch, [(0.0, 2.0), (8.0, 9.5)])
    assert speech.runs_for("a") == [[2.0, 8.0], [9.5, 30.0]]


def test_a_file_with_no_pause_worth_reporting_is_one_run(monkeypatch):
    _detector(monkeypatch, [])
    assert speech.runs_for("a") == [[0.0, 30.0]]


def test_a_silence_still_open_at_the_end_closes_at_the_end(monkeypatch):
    _detector(monkeypatch, [(25.0, None)])
    assert speech.runs_for("a") == [[0.0, 25.0]]


def test_a_clip_with_no_proxy_is_not_listened_to(monkeypatch):
    calls = _detector(monkeypatch, [])
    library.proxy_for("a").unlink()
    assert speech.runs_for("a") == []
    assert calls == []


def test_a_failed_detection_is_no_runs_rather_than_wrong_ones(monkeypatch):
    _detector(monkeypatch, [(0.0, 2.0)], code=1)
    assert speech.runs_for("a") == []


# --- listening once ----------------------------------------------------


def test_a_clip_is_listened_to_once(monkeypatch):
    calls = _detector(monkeypatch, [(0.0, 2.0)])
    speech.runs_for("a")
    speech.runs_for("a")
    assert len(calls) == 1
    assert "a" in json.loads(speech.cache_path().read_text())


def test_a_replaced_clip_is_listened_to_again(monkeypatch):
    calls = _detector(monkeypatch, [(0.0, 2.0)])
    speech.runs_for("a")
    library.proxy_for("a").write_bytes(b"a different recording entirely")
    speech.runs_for("a")
    assert len(calls) == 2


def test_a_corrupt_cache_costs_a_measurement_not_a_trim(monkeypatch):
    _detector(monkeypatch, [(0.0, 2.0)])
    speech.cache_path().parent.mkdir(parents=True, exist_ok=True)
    speech.cache_path().write_text("{not json")
    assert speech.runs_for("a") == [[2.0, 30.0]]


# --- moving a boundary onto one ----------------------------------------

RUNS = [[2.0, 8.0], [9.5, 20.0], [26.0, 30.0]]


def test_an_in_point_inside_an_utterance_backs_up_to_its_start():
    """Half a word in is the defect you notice first and cannot name."""
    assert speech.snap_in(RUNS, 2.6) == pytest.approx(2.0 - speech.LEAD_IN)


def test_an_in_point_in_the_pause_before_one_moves_forward_to_it():
    assert speech.snap_in(RUNS, 8.8) == pytest.approx(9.5 - speech.LEAD_IN)


def test_an_in_point_nowhere_near_speech_is_left_where_it_is():
    """A b-roll shot of a cupboard has no utterance to open on."""
    assert speech.snap_in(RUNS, 23.0) == 23.0


def test_an_out_point_inside_an_utterance_finishes_the_sentence():
    assert speech.snap_out(RUNS, 19.4) == 20.0


def test_an_out_point_sitting_in_a_pause_pulls_back_off_the_room_tone():
    assert speech.snap_out(RUNS, 8.9) == 8.0


def test_a_boundary_further_away_than_the_window_does_not_move():
    assert speech.snap_in(RUNS, 4.0) == 4.0        # inside, but 2s in
    assert speech.snap_out(RUNS, 23.0) == 23.0     # in a long gap


def test_the_window_is_the_models_own_error_bar_and_can_be_widened():
    assert speech.snap_in(RUNS, 4.0, window=3.0) == pytest.approx(2.0 - speech.LEAD_IN)


def test_a_snap_never_collapses_a_shot_to_nothing():
    """Both ends moving toward each other could otherwise cross."""
    start, end = speech.snap_range(RUNS, 7.9, 8.1)
    assert (start, end) == (7.9, 8.1)


def test_a_clip_nothing_was_heard_in_leaves_every_shot_alone():
    assert speech.snap_range([], 5.0, 12.0) == (5.0, 12.0)


def test_an_in_point_at_the_very_start_does_not_go_negative():
    assert speech.snap_in([[0.0, 5.0]], 0.3) == 0.0


def test_a_blip_too_short_to_be_an_utterance_is_not_one(monkeypatch):
    """silencedetect's own ramp-up leaves a 26 ms run at the head of most of
    these clips, and an in-point snapped onto it has snapped onto nothing."""
    _detector(monkeypatch, [(0.026, 0.82), (10.0, 12.0)])
    assert speech.runs_for("a") == [[0.82, 10.0], [12.0, 30.0]]


# --- moving the subtitles onto the speech -------------------------------


class _L:
    def __init__(self, start, end):
        self.start, self.end = start, end


def test_a_line_keeps_its_length_when_it_moves():
    """The model's estimate of how long an utterance runs is better than its
    estimate of when it began; only the offset is wrong."""
    (start, end), = speech.snap_lines([[4.7, 9.0]], [_L(4.0, 6.5)])
    assert start == pytest.approx(4.7 - speech.LEAD_IN)
    assert end - start == pytest.approx(2.5)


def test_two_lines_cannot_both_have_the_same_onset():
    """A run of quick exchanges would otherwise collapse onto one instant."""
    lines = [_L(10.0, 11.0), _L(10.4, 11.4)]
    (a_start, _), (b_start, _) = speech.snap_lines([[9.6, 20.0]], lines)
    assert a_start == pytest.approx(9.6 - speech.LEAD_IN)
    assert b_start == 10.4


def test_a_line_may_not_start_before_the_one_in_front_of_it():
    """A snap is a correction of half a second, not a re-ordering."""
    lines = [_L(10.0, 12.0), _L(12.1, 13.0)]
    (a_start, _), (b_start, _) = speech.snap_lines([[9.6, 11.0], [9.0, 9.2]], lines)
    assert b_start >= a_start


def test_a_line_with_no_onset_near_it_is_left_alone():
    (start, end), = speech.snap_lines([[40.0, 50.0]], [_L(4.0, 6.5)])
    assert (start, end) == (4.0, 6.5)


def test_aligning_twice_changes_nothing_the_second_time(monkeypatch):
    runs = [[4.7, 9.0], [12.0, 20.0]]
    once = speech.snap_lines(runs, [_L(4.0, 6.5), _L(12.4, 14.0)])
    twice = speech.snap_lines(runs, [_L(*pair) for pair in once])
    assert once == twice


def test_an_in_point_in_the_tail_of_a_long_answer_opens_on_the_next_one():
    """Half a second before an utterance ends is not a place to start a shot;
    the sentence beginning a second later is."""
    runs = [[0.0, 5.0], [5.5, 20.0]]
    assert speech.snap_in(runs, 4.5) == pytest.approx(5.5 - speech.LEAD_IN)
    # And from further inside it, where nothing is near, it stays put.
    assert speech.snap_in(runs, 3.0) == 3.0
