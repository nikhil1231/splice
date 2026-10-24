"""Discovery of source footage, and reading the old prose log."""

import pytest

from greels import config, legacy, library


@pytest.fixture
def raw(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    root = tmp_path / "raw"
    (root / "from_phone").mkdir(parents=True)
    for name in [
        "balcony_view_complex.MOV", "balcony_view_complex.mp4",  # .mp4 is a downscale
        "grandma_tries_airpods.MOV", "orphan_only_as_mp4.mp4",
        "notes.txt", ".DS_Store",
    ]:
        (root / name).touch()
    (root / "from_phone" / "IMG_9001.MOV").touch()
    return root


def test_one_entry_per_stem_preferring_the_original(raw):
    names = [p.name for p in library.discover_raw()]
    assert names == [
        "IMG_9001.MOV",
        "balcony_view_complex.MOV",
        "grandma_tries_airpods.MOV",
        "orphan_only_as_mp4.mp4",
    ]


def test_a_folder_dropped_into_the_bin_is_picked_up(raw):
    assert any(p.name == "IMG_9001.MOV" for p in library.discover_raw())


def test_a_directory_can_still_be_excluded(raw, monkeypatch):
    monkeypatch.setattr(config, "RAW_SKIP_DIRS", {"from_phone"})
    assert not any(p.name == "IMG_9001.MOV" for p in library.discover_raw())


def test_non_video_is_ignored(raw):
    assert not any(p.suffix in {".txt", ""} for p in library.discover_raw())


@pytest.mark.parametrize(
    "pattern,expected",
    [("grandma", ["grandma_tries_airpods"]), ("*airpods", ["grandma_tries_airpods"]),
     ("nothing", []), (None, ["balcony_view_complex", "grandma_tries_airpods"])],
)
def test_match_accepts_globs_and_bare_substrings(pattern, expected):
    stems = ["balcony_view_complex", "grandma_tries_airpods"]
    assert library.match(stems, pattern) == expected


def test_chunks_are_returned_in_time_order(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    config.chunk_dir().mkdir(parents=True)
    for offset in ("00960", "00000", "00240"):
        (config.chunk_dir() / f"clip__{offset}.mp4").touch()
    (config.chunk_dir() / "clip__notanumber.mp4").touch()
    assert [o for o, _ in library.chunks_for("clip")] == [0.0, 240.0, 960.0]


# --- the old log -------------------------------------------------------


def test_legacy_log_is_split_per_clip(tmp_path):
    path = tmp_path / "video_logs.txt"
    path.write_text(
        "# Grandparents Video Content Logs\n\n"
        "## cooking_chat_about_vlogging.MOV (Original: IMG_6272.MOV)\n"
        "- **00:00 - 00:15:** Grandma cooking fish.\n\n"
        "## grandma_tries_airpods.MOV (Original: IMG_6295.MOV)\n"
        "- **00:00 - 00:47:** She is confused about which ear.\n"
    )
    parsed = legacy.parse(path)
    assert set(parsed) == {"cooking_chat_about_vlogging", "grandma_tries_airpods"}
    assert parsed["cooking_chat_about_vlogging"]["original"] == "IMG_6272.MOV"
    assert "cooking fish" in parsed["cooking_chat_about_vlogging"]["prose"]
    assert "confused" not in parsed["cooking_chat_about_vlogging"]["prose"]


def test_missing_log_is_not_an_error(tmp_path):
    assert legacy.parse(tmp_path / "absent.txt") == {}
