"""Reading a file's shape, and deciding what makes it the same file.

ffprobe is stood in for. Three things here are load-bearing and none of them
were checked: the fingerprint, which is what makes a clip re-copied off the
phone the same clip; the frame rate, which the FCPXML exporter has to land on
exactly; and the rotation, without which every vertical clip in this archive
reads as 1920x1080 and every export is wrong.
"""

from fractions import Fraction

import pytest

from greels import probe


# --- the frame rate ----------------------------------------------------


def test_a_rational_rate_is_kept_as_one():
    """A float round-trip loses the exactness FCPXML needs."""
    assert probe.parse_fps("30000/1001") == Fraction(30000, 1001)
    assert probe.parse_fps("30/1") == Fraction(30, 1)


def test_a_rate_that_makes_no_sense_falls_back_to_thirty():
    for bad in ("0/0", "", "not a rate", "-30/1"):
        assert probe.parse_fps(bad) == Fraction(30, 1)


# --- identity ----------------------------------------------------------


def test_the_same_bytes_are_the_same_clip(tmp_path):
    a, b = tmp_path / "a.MOV", tmp_path / "IMG_1.MOV"
    a.write_bytes(b"the same footage")
    b.write_bytes(b"the same footage")
    assert probe.fingerprint(a) == probe.fingerprint(b)


def test_one_byte_different_is_a_different_clip(tmp_path):
    a, b = tmp_path / "a.MOV", tmp_path / "b.MOV"
    a.write_bytes(b"footage" * 100)
    b.write_bytes(b"footage" * 100 + b".")
    assert probe.fingerprint(a) != probe.fingerprint(b)


def test_a_long_file_is_read_at_both_ends_and_not_in_between(tmp_path):
    """14 GB of footage is not hashed on every sync; the ends and the exact
    size are decisive in practice."""
    window = probe.FINGERPRINT_WINDOW
    head, tail = b"H" * window, b"T" * window
    a, b = tmp_path / "a.MOV", tmp_path / "b.MOV"
    a.write_bytes(head + b"A" * window + tail)
    b.write_bytes(head + b"B" * window + tail)
    assert probe.fingerprint(a) == probe.fingerprint(b)   # the middle is unread
    b.write_bytes(head + b"B" * (window + 1) + tail)
    assert probe.fingerprint(a) != probe.fingerprint(b)   # the size is not


# --- the shape ---------------------------------------------------------


_DEFAULT_FORMAT = {"duration": "12.5"}


def _stub(monkeypatch, video, audio=True, fmt=_DEFAULT_FORMAT):
    streams = [dict({"codec_type": "video"}, **video)]
    if audio:
        streams.append({"codec_type": "audio"})
    monkeypatch.setattr(probe, "_ffprobe", lambda path: {
        "streams": streams, "format": fmt,
    })


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "a.MOV"
    path.write_bytes(b"footage")
    return path


def test_a_clip_the_phone_rotated_reports_the_shape_you_see(clip, monkeypatch):
    """Every clip in this archive is one of these. Storage is 1920x1080 with a
    quarter turn; what Final Cut and a reel want is 1080x1920."""
    _stub(monkeypatch, {"width": 1920, "height": 1080, "r_frame_rate": "30/1",
                        "tags": {"rotate": "90"}})
    asset = probe.probe_asset(clip)
    assert (asset.width, asset.height) == (1080, 1920)


def test_the_rotation_can_arrive_as_side_data(clip, monkeypatch):
    """Which is where newer ffprobe puts it, as a negative number."""
    _stub(monkeypatch, {"width": 1920, "height": 1080, "r_frame_rate": "30/1",
                        "side_data_list": [{"rotation": -90}]})
    assert probe.probe_asset(clip).width == 1080


def test_a_half_turn_is_not_a_swap(clip, monkeypatch):
    _stub(monkeypatch, {"width": 1920, "height": 1080, "r_frame_rate": "30/1",
                        "tags": {"rotate": "180"}})
    assert probe.probe_asset(clip).width == 1920


def test_nonsense_rotation_is_no_rotation(clip, monkeypatch):
    _stub(monkeypatch, {"width": 1920, "height": 1080, "r_frame_rate": "30/1",
                        "tags": {"rotate": "sideways"}})
    assert probe.probe_asset(clip).width == 1920


def test_a_clip_with_no_sound_says_so(clip, monkeypatch):
    _stub(monkeypatch, {"width": 1080, "height": 1920, "r_frame_rate": "30/1"},
          audio=False)
    assert probe.probe_asset(clip).has_audio is False


def test_a_duration_the_container_does_not_give_comes_off_the_stream(clip, monkeypatch):
    _stub(monkeypatch, {"width": 1080, "height": 1920, "r_frame_rate": "30/1",
                        "duration": "8.25"}, fmt={})
    assert probe.probe_asset(clip).duration == 8.25


def test_a_file_with_no_picture_is_refused(clip, monkeypatch):
    monkeypatch.setattr(probe, "_ffprobe", lambda path: {
        "streams": [{"codec_type": "audio"}], "format": {},
    })
    with pytest.raises(probe.ProbeError):
        probe.probe_asset(clip)
