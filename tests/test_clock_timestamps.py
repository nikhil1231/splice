"""Timestamps the model wrote as minutes and seconds.

Asked for seconds, Gemini sometimes answers a chunk in M.SS -- a line from 57
to 100 that means 0:57 to 1:00. Converted before the merge, nothing is lost;
merged as seconds, the clip's later lines fall past its end and the next
chunk's real minutes are thrown away as duplicates.
"""

from greels.analyze import clock_to_seconds, reads_as_a_clock


def answer(*spans):
    """One segment per (start, end), each carrying a line with the same span."""
    return {"segments": [
        {"start": s, "end": e, "title": "t",
         "lines": [{"start": s, "end": e, "kok": "", "en": ""}]}
        for s, e in spans
    ]}


def test_a_clock_that_runs_past_the_end_of_the_file_is_recognised():
    """grandpa_nana_drew_the_house_plan: 129 seconds long, answered up to 2:09."""
    raw = answer((7, 38), (38, 57), (57, 100), (100, 128), (128, 209))
    assert reads_as_a_clock(raw, length=128.9)


def test_seconds_that_cross_the_sixties_are_left_alone():
    raw = answer((0, 30), (30, 65), (65, 110), (110, 128))
    assert not reads_as_a_clock(raw, length=128.9)


def test_a_clip_under_a_minute_reads_the_same_either_way():
    assert not reads_as_a_clock(answer((0, 20), (20, 55)), length=56)


def test_a_full_chunk_of_clock_is_recognised_without_running_past_the_end():
    """Twelve values past a minute that all miss :60 to :99 is not chance."""
    spans = [(100, 110), (110, 120), (120, 130), (130, 140), (140, 150), (150, 159)]
    assert reads_as_a_clock(answer(*spans), length=245)


def test_a_few_values_that_happen_to_miss_the_sixties_are_not_enough():
    """Eight values in 100-159 are one in ninety by chance; not proof of a clock."""
    assert not reads_as_a_clock(answer((100, 120), (140, 150)), length=245)


def test_a_chunk_that_mixes_the_two_is_not_converted():
    """Half-converting a chunk would be a guess made line by line."""
    raw = answer((57, 100), (100, 130), (130, 175))
    assert not reads_as_a_clock(raw, length=245)


def test_conversion_turns_minutes_and_seconds_into_seconds_everywhere():
    raw = answer((57, 100), (128.5, 209))
    out = clock_to_seconds(raw)
    first, second = out["segments"]
    assert (first["start"], first["end"]) == (57, 60)
    assert (first["lines"][0]["start"], first["lines"][0]["end"]) == (57, 60)
    assert (second["start"], second["end"]) == (88.5, 129)
    assert raw["segments"][0]["end"] == 100, "the model's answer is not changed in place"
