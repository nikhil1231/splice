"""A clip is its content, not its filename.

Every case here used to end the same way: a second sidecar, a second proxy, a
second Gemini run, and a second copy of the same footage in Final Cut.
"""

import pytest

from greels import config, identity, library
from greels.identity import Verdict
from greels.probe import fingerprint
from greels.schema import Asset, Segment, Sidecar


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    config.raw_dir().mkdir(parents=True)
    config.proxy_dir().mkdir(parents=True)
    config.chunk_dir().mkdir(parents=True)
    (config.export_dir() / "srt").mkdir(parents=True)
    return tmp_path


def drop(stem, content=b"footage", suffix=".MOV"):
    """Put a file in raw/ without telling the archive about it."""
    path = config.raw_dir() / f"{stem}{suffix}"
    path.write_bytes(content)
    return path


def known(stem, content=b"footage", suffix=".MOV", segments=None):
    """A clip already in the archive: file in raw/, sidecar, proxy."""
    path = drop(stem, content, suffix)
    library.proxy_for(stem).write_bytes(b"proxy")
    stat = path.stat()
    sc = Sidecar(
        asset=Asset(
            file=path.name, stem=stem, duration=100, fps="30",
            fingerprint=fingerprint(path), mtime=stat.st_mtime, size_bytes=stat.st_size,
        ),
        segments=segments if segments is not None else [
            Segment(id=f"{stem}#00000.00", start=0, end=60, title="A moment")
        ],
    )
    sc.save(config.sidecar_path(stem))
    return sc


def verdicts(paths=None):
    paths = paths or library.discover_raw()
    survey = identity.survey(paths, library.load_all())
    return {f.stem: f for f in survey.findings}


def test_an_untouched_clip_is_left_alone(archive):
    known("lunch")
    assert verdicts()["lunch"].verdict is Verdict.UNCHANGED


def test_a_clip_never_seen_before_is_new(archive):
    drop("IMG_8768")
    assert verdicts()["IMG_8768"].verdict is Verdict.NEW


def test_recopying_a_renamed_clip_off_the_phone_is_a_duplicate(archive):
    """The case that costs the most: same bytes, camera's name, original still here."""
    known("grandpa_guessing_echo_show", b"the same footage")
    drop("IMG_8768", b"the same footage")

    found = verdicts()["IMG_8768"]
    assert found.verdict is Verdict.DUPLICATE
    assert found.known_as == "grandpa_guessing_echo_show"


def test_a_duplicate_never_becomes_its_own_clip(archive):
    known("grandpa_guessing_echo_show", b"the same footage")
    drop("IMG_8768", b"the same footage")

    survey = identity.survey(library.discover_raw(), library.load_all())
    assert survey.skip_stems == {"IMG_8768"}
    assert not config.sidecar_path("IMG_8768").exists()


def test_two_extra_copies_are_both_duplicates(archive):
    known("original", b"same")
    drop("copy_one", b"same")
    drop("copy_two", b"same")

    found = verdicts()
    assert found["copy_one"].verdict is Verdict.DUPLICATE
    assert found["copy_two"].verdict is Verdict.DUPLICATE


def test_a_rename_done_in_finder_is_adopted(archive):
    known("IMG_8768", b"the same footage")
    (config.raw_dir() / "IMG_8768.MOV").rename(config.raw_dir() / "renamed_by_hand.MOV")

    found = verdicts()["renamed_by_hand"]
    assert found.verdict is Verdict.ADOPTED
    assert found.known_as == "IMG_8768"


def test_adoption_keeps_the_analysis(archive):
    """Re-analysing here would spend Gemini time to rebuild what we already have."""
    known("IMG_8768", b"the same footage")
    (config.raw_dir() / "IMG_8768.MOV").rename(config.raw_dir() / "renamed_by_hand.MOV")

    identity.adopt(verdicts()["renamed_by_hand"])

    moved = Sidecar.load(config.sidecar_path("renamed_by_hand"))
    assert [s.title for s in moved.segments] == ["A moment"]
    assert moved.segments[0].id == "renamed_by_hand#00000.00"
    assert moved.asset.previous_names == ["IMG_8768"]
    assert library.proxy_for("renamed_by_hand").exists()
    assert not config.sidecar_path("IMG_8768").exists()


def test_replacing_a_file_in_place_is_noticed(archive):
    """Duration equality used to be the only check, so a re-export slipped past."""
    known("lunch", b"first cut")
    (config.raw_dir() / "lunch.MOV").write_bytes(b"a different cut entirely")

    assert verdicts()["lunch"].verdict is Verdict.REPLACED


def test_an_unchanged_file_is_not_rehashed(archive, monkeypatch):
    """A steady-state sync must not read 14 GB of footage."""
    import greels.probe as probe

    known("lunch")
    calls = []
    monkeypatch.setattr(probe, "fingerprint", lambda path: calls.append(path))

    identity.survey(library.discover_raw(), library.load_all())
    assert calls == []


def test_a_touched_file_is_rehashed(archive):
    sc = known("lunch")
    sc.asset.mtime = 0.0
    sc.save(config.sidecar_path("lunch"))
    # Same bytes, so it still resolves to unchanged -- but only after hashing.
    assert verdicts()["lunch"].verdict is Verdict.UNCHANGED


def test_a_name_the_clip_has_returned_to_is_not_a_former_name(archive):
    """Renaming back and forth must not leave the current name in the trail."""
    known("original", b"same footage")
    raw = config.raw_dir()

    (raw / "original.MOV").rename(raw / "temporary.MOV")
    identity.adopt(verdicts()["temporary"])
    (raw / "temporary.MOV").rename(raw / "original.MOV")
    identity.adopt(verdicts()["original"])

    sc = Sidecar.load(config.sidecar_path("original"))
    assert sc.asset.stem == "original"
    assert sc.asset.previous_names == ["temporary"]
