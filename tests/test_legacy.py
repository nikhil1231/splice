"""The old hand-written log, read back as a hint for the analysis.

Its minute-bucket prose is not good enough to edit from, but it is a real
record of what is in each clip, and feeding it back means a new pass starts
from what was already found. Forty clips of this archive have an entry.
"""

from splice import config, legacy

LOG = """\
# Video logs

## cooking_chat_about_vlogging.MOV (Original: IMG_6272.MOV)
- **00:00** Grandma frying fish.
- **00:15** She asks whether her face looks all right.

## grandma_cooking_fish_silent.MOV
- **00:00** No speech.

## trailing.MOV (Original: IMG_9999.MOV)
- **00:00** The last one, with nothing after it.
"""


def _parse(tmp_path, text=LOG):
    path = tmp_path / "video_logs.txt"
    path.write_text(text)
    return legacy.parse(path)


def test_every_clip_in_the_log_comes_back(tmp_path):
    assert set(_parse(tmp_path)) == {
        "cooking_chat_about_vlogging", "grandma_cooking_fish_silent", "trailing",
    }


def test_the_camera_name_is_kept_where_there_is_one(tmp_path):
    parsed = _parse(tmp_path)
    assert parsed["cooking_chat_about_vlogging"]["original"] == "IMG_6272.MOV"
    assert parsed["grandma_cooking_fish_silent"]["original"] == ""


def test_the_prose_stops_at_the_next_heading(tmp_path):
    prose = _parse(tmp_path)["cooking_chat_about_vlogging"]["prose"]
    assert "frying fish" in prose and "No speech" not in prose


def test_the_last_entry_is_not_dropped(tmp_path):
    """It has no heading after it to flush it."""
    assert "nothing after it" in _parse(tmp_path)["trailing"]["prose"]


def test_anything_before_the_first_heading_is_ignored(tmp_path):
    assert "" not in _parse(tmp_path)


def test_no_log_is_no_hints_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    assert legacy.parse() == {}
    assert legacy.parse(config.legacy_log_path()) == {}
