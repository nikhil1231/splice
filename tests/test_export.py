"""Structural checks on the editor-facing output.

Final Cut is not installed here, so its DTD cannot be the oracle. These
assertions stand in for it: every ref resolves, every keyword range sits inside
its clip, and every time is frame-aligned.
"""

from fractions import Fraction
from xml.etree import ElementTree as ET

import pytest

from splice.export_fcpxml import build_document
from splice.export_srt import render, timecode
from splice.fcptime import timebase
from splice.schema import Asset, Line, Segment, Sidecar
from splice.taxonomy import Taxonomy


@pytest.fixture
def taxonomy():
    return Taxonomy.load()


@pytest.fixture
def sidecars():
    portrait = Asset(file="a.MOV", stem="a", duration=35.17, fps="30",
                     width=1080, height=1920)
    ntsc = Asset(file="b.MOV", stem="b", duration=1190.34, fps="30000/1001",
                 width=1920, height=1080, has_audio=False)
    return [
        Sidecar(asset=portrait, segments=[
            Segment(id="a#0", start=0, end=27, title="Marriage chat",
                    tags=["person:grandma", "theme:marriage"], rating="favorite"),
            Segment(id="a#27", start=27, end=35.17, title="Coconut scraper",
                    tags=["object:coconut_scraper"]),
        ]),
        Sidecar(asset=ntsc, segments=[
            Segment(id="b#241", start=241.8, end=318.2, title="Rs 7 train to Bombay",
                    tags=["theme:money", "location:bombay"], rating="rejected"),
        ]),
    ]


def _root(sidecars, taxonomy):
    return build_document(sidecars, taxonomy).getroot()


def test_every_ref_resolves(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    ids = {el.get("id") for el in root.find("resources")}
    for clip in root.iter("asset-clip"):
        assert clip.get("ref") in ids
        assert clip.get("format") in ids
    for asset in root.iter("asset"):
        assert asset.get("format") in ids


def test_one_format_per_distinct_shape(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    formats = root.findall("resources/format")
    assert len(formats) == 2
    assert {f.get("frameDuration") for f in formats} == {"100/3000s", "1001/30000s"}


def test_media_rep_points_at_the_original_not_the_proxy(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    for rep in root.iter("media-rep"):
        assert rep.get("src").startswith("file:///")
        assert rep.get("src").endswith(".MOV")
        assert "/proxies/" not in rep.get("src")


def test_silent_asset_declares_no_audio(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    b = next(a for a in root.iter("asset") if a.get("name") == "b")
    assert b.get("hasAudio") is None


def _seconds(value: str) -> float:
    value = value.removesuffix("s")
    return float(Fraction(value)) if "/" in value else float(value)


def test_every_time_is_frame_aligned(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    for clip in root.iter("asset-clip"):
        fps = "30" if clip.get("name") == "a" else "30000/1001"
        ticks, scale = timebase(fps)
        for el in clip.iter():
            for attr in ("start", "duration"):
                value = el.get(attr)
                if not value or value == "0s":
                    continue
                numerator, denominator = (int(x) for x in value.removesuffix("s").split("/"))
                assert denominator == scale
                assert numerator % ticks == 0


def test_keyword_ranges_stay_inside_their_clip(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    for clip in root.iter("asset-clip"):
        limit = _seconds(clip.get("duration"))
        for el in list(clip.iter("keyword")) + list(clip.iter("marker")):
            start = _seconds(el.get("start"))
            end = start + _seconds(el.get("duration"))
            assert 0 <= start <= limit
            assert end <= limit + 1e-6


def test_tags_become_comma_separated_final_cut_labels(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    values = [k.get("value") for k in root.iter("keyword")]
    assert "Grandma, Marriage" in values
    assert "Money, Bombay" in values


def test_note_precedes_the_marker_group(taxonomy):
    asset = Asset(file="c.MOV", stem="c", duration=10, fps="30", width=100, height=100)
    sc = Sidecar(asset=asset, segments=[Segment(id="c#0", start=0, end=5, title="t")])
    sc.analysis.errors = ["chunk 2 failed"]
    clip = next(_root([sc], taxonomy).iter("asset-clip"))
    assert [child.tag for child in clip][0] == "note"


def test_ratings_use_the_values_fcpxml_accepts(sidecars, taxonomy):
    root = _root(sidecars, taxonomy)
    assert {r.get("value") for r in root.iter("rating")} == {"favorite", "reject"}


def test_document_is_serialisable(sidecars, taxonomy, tmp_path):
    dest = tmp_path / "out.fcpxml"
    build_document(sidecars, taxonomy).write(dest, encoding="utf-8", xml_declaration=True)
    assert ET.parse(dest).getroot().get("version") == "1.11"


# --- subtitles ---------------------------------------------------------


def test_subtitle_times_are_rebased_to_the_segment():
    segment = Segment(
        id="a#27", start=27.0, end=35.0, title="t",
        lines=[Line(start=27.0, end=29.0, kok="Adoli lagat.", en="The scraper will hurt you.")],
    )
    body = render(segment)
    assert "00:00:00,000 --> 00:00:02,000" in body
    assert body.startswith("1\n")


def test_bilingual_puts_konkani_above_english():
    segment = Segment(id="a#0", start=0, end=2, title="t",
                      lines=[Line(start=0, end=1, kok="Hoy hoy.", en="Yes, yes.")])
    assert "Hoy hoy.\nYes, yes." in render(segment, lang="both")


def test_silent_segment_renders_nothing():
    assert render(Segment(id="a#0", start=0, end=5, title="Silent")) == ""


def test_timecode_format():
    assert timecode(0) == "00:00:00,000"
    assert timecode(3661.5) == "01:01:01,500"


# --- what Final Cut already holds --------------------------------------


def _sc(stem, segments, exported=None):
    from splice.schema import ExportRecord

    sc = Sidecar(
        asset=Asset(file=f"{stem}.MOV", stem=stem, duration=100, fps="30",
                    width=1080, height=1920),
        segments=segments,
    )
    if exported:
        sc.exported = ExportRecord(
            imported_at="2026-09-04T00:00:00+00:00", imported_sha=exported, fcp_name=stem
        )
    return sc


def test_content_sha_tracks_what_an_export_would_contain():
    a = _sc("a", [Segment(id="a#0", start=0, end=10, title="t", tags=["theme:money"])])
    b = _sc("a", [Segment(id="a#0", start=0, end=10, title="t", tags=["theme:money"])])
    assert a.content_sha() == b.content_sha()

    # A summary rewrite changes nothing Final Cut can see.
    b.segments[0].summary = "different prose"
    assert a.content_sha() == b.content_sha()

    # A retag does.
    b.segments[0].tags = ["theme:food"]
    assert a.content_sha() != b.content_sha()


def test_pending_splits_never_exported_from_changed():
    from splice.export_fcpxml import pending

    never = _sc("never", [Segment(id="never#0", start=0, end=10, title="t")])
    current = _sc("current", [Segment(id="current#0", start=0, end=10, title="t")])
    current.exported.imported_sha = current.content_sha()
    current.exported.imported_at = "2026-09-04T00:00:00+00:00"
    changed = _sc("changed", [Segment(id="changed#0", start=0, end=10, title="t")],
                  exported="staleeeeeeeeeeee")

    fresh, changed_out = pending([never, current, changed])
    assert [s.asset.stem for s in fresh] == ["never"]
    assert [s.asset.stem for s in changed_out] == ["changed"]


def test_unanalysed_clips_are_never_pending():
    from splice.export_fcpxml import pending

    assert pending([_sc("empty", [])]) == ([], [])


def test_export_rejects_a_document_apples_dtd_would_refuse(tmp_path, monkeypatch, taxonomy):
    """Validation must be able to fail, not just be present."""
    import splice.export_fcpxml as mod

    monkeypatch.setattr(mod, "validate", lambda *a, **k: "element marker: validity error")
    with pytest.raises(ValueError, match="DTD"):
        mod.write(dest=tmp_path / "out.fcpxml",
                  sidecars=[_sc("a", [Segment(id="a#0", start=0, end=10, title="t")])])


def test_marking_a_clip_imported_takes_it_out_of_pending(tmp_path, monkeypatch):
    from splice import config
    from splice.export_fcpxml import mark_exported, pending

    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    sc = _sc("a", [Segment(id="a#0", start=0, end=10, title="t")])
    sc.save(config.sidecar_path("a"))

    assert pending([sc])[0] == [sc]
    mark_exported([sc])
    assert pending([sc]) == ([], [])

    # ...until the analysis changes, which Final Cut cannot be told about.
    sc.segments[0].tags = ["theme:money"]
    assert pending([sc])[1] == [sc]


# --- offered vs imported -----------------------------------------------
#
# These two were the same fact until eight clips were marked delivered that
# Final Cut had never seen. Writing a file is something we do; importing it is
# something only the library can tell us.


@pytest.fixture
def archive(tmp_path, monkeypatch):
    from splice import config

    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.delenv("SPLICE_FCP_LIBRARY", raising=False)
    config.index_dir().mkdir(parents=True, exist_ok=True)
    return tmp_path


def _saved(stem, **kw):
    from splice import config

    sc = _sc(stem, [Segment(id=f"{stem}#0", start=0, end=10, title="t")], **kw)
    sc.save(config.sidecar_path(stem))
    return sc


def _library(monkeypatch, held, duplicates=None):
    """Stand in for Final Cut. `None` means the library could not be read."""
    import splice.export_fcpxml as mod

    monkeypatch.setattr(mod.fcp, "imported_clips", lambda *a, **k: held)
    monkeypatch.setattr(mod.fcp, "duplicate_names", lambda *a, **k: duplicates or {})


def test_an_offer_that_was_never_imported_comes_back(archive, monkeypatch):
    """The bug that lost eight clips: writing the file marked them delivered."""
    from splice.export_fcpxml import mark_offered, pending, reconcile

    sc = _saved("airtel_sim_card")
    mark_offered([sc])
    _library(monkeypatch, {})

    report = reconcile([sc])
    assert report.withdrawn == ["airtel_sim_card"]
    assert pending([sc])[0] == [sc]


def test_an_offer_final_cut_accepted_is_confirmed(archive, monkeypatch):
    from splice.export_fcpxml import mark_offered, pending, reconcile

    sc = _saved("lunch")
    mark_offered([sc])
    _library(monkeypatch, {"lunch": 1})

    report = reconcile([sc])
    assert report.confirmed == ["lunch"]
    assert sc.exported.delivered and sc.exported.offered_at is None
    assert pending([sc]) == ([], [])


def test_a_clip_imported_by_hand_is_noticed(archive, monkeypatch):
    """Importing grandparents.fcpxml yourself should not leave it in the queue."""
    from splice.export_fcpxml import pending, reconcile

    sc = _saved("lunch")
    _library(monkeypatch, {"lunch": 1})

    reconcile([sc])
    assert pending([sc]) == ([], [])


def test_a_changed_clip_is_offered_again(archive, monkeypatch):
    from splice.export_fcpxml import pending, reconcile

    sc = _saved("lunch")
    _library(monkeypatch, {"lunch": 1})
    reconcile([sc])

    sc.segments[0].tags = ["theme:money"]
    assert pending([sc])[1] == [sc]


def test_a_re_offer_is_confirmed_when_final_cut_grows_a_second_copy(archive, monkeypatch):
    """Final Cut cannot update a clip, so the copy count is the only signal."""
    from splice.export_fcpxml import mark_offered, pending, reconcile

    sc = _saved("lunch")
    _library(monkeypatch, {"lunch": 1})
    reconcile([sc])

    sc.segments[0].tags = ["theme:money"]
    mark_offered([sc], {"lunch": 1})
    assert sc.exported.fcp_copies == 1

    # Still one copy: the import has not happened, so it stays on the list.
    reconcile([sc])
    assert pending([sc])[1] == [sc]

    _library(monkeypatch, {"lunch": 2})
    reconcile([sc])
    assert sc.exported.imported_sha == sc.content_sha()
    assert pending([sc]) == ([], [])


def test_a_clip_renamed_after_import_is_still_recognised(archive, monkeypatch):
    from splice import config
    from splice.export_fcpxml import pending, reconcile

    sc = _saved("IMG_8768")
    _library(monkeypatch, {"IMG_8768": 1})
    reconcile([sc])

    # As `rename` leaves it: new stem, old name still in the trail.
    sc.asset.previous_names = ["IMG_8768"]
    sc.asset.stem = "grandpa_guessing_echo_show"
    sc.save(config.sidecar_path(sc.asset.stem))

    reconcile([sc])
    assert pending([sc]) == ([], [])


def test_an_unreadable_library_changes_nothing(archive, monkeypatch):
    """Guessing either way is worse than trusting what the sidecars say."""
    from splice.export_fcpxml import mark_offered, pending, reconcile

    sc = _saved("lunch")
    mark_offered([sc])
    before = sc.exported.model_dump()
    _library(monkeypatch, None)

    report = reconcile([sc])
    assert report.seen is False
    assert sc.exported.model_dump() == before
    assert pending([sc])[0] == [sc]


def test_reconcile_writes_its_corrections_to_disk(archive, monkeypatch):
    from splice import config
    from splice.export_fcpxml import mark_offered, reconcile
    from splice.schema import Sidecar

    sc = _saved("airtel_sim_card")
    mark_offered([sc])
    _library(monkeypatch, {})
    reconcile([sc])

    assert not Sidecar.load(config.sidecar_path("airtel_sim_card")).exported.delivered


def test_a_legacy_sidecar_keeps_its_import_state(archive):
    """The 55 sidecars written before the split must still load correctly."""
    import json

    from splice import config
    from splice.schema import Sidecar

    sc = _sc("lunch", [Segment(id="lunch#0", start=0, end=10, title="t")])
    payload = sc.model_dump(mode="json")
    payload["exported"] = {"fcpxml_at": "2026-09-04T00:00:00+00:00", "fcpxml_sha": "deadbeef"}
    config.sidecar_path("lunch").write_text(json.dumps(payload))

    loaded = Sidecar.load(config.sidecar_path("lunch"))
    assert loaded.exported.imported_at == "2026-09-04T00:00:00+00:00"
    assert loaded.exported.imported_sha == "deadbeef"
    assert loaded.exported.delivered


# --- subtitles ---------------------------------------------------------


def test_srt_export_removes_files_no_segment_claims(archive):
    """The filename carries the segment's boundaries, so a re-analysis that
    nudges one orphans a file. 210 files for 155 segments went unnoticed."""
    from splice import config
    from splice.export_srt import write
    from splice.schema import Line

    sc = _sc("lunch", [
        Segment(id="lunch#0", start=0, end=27, title="Chat",
                lines=[Line(start=0, end=2, en="hello")]),
    ])
    sc.save(config.sidecar_path("lunch"))

    srt_dir = config.export_dir() / "srt"
    srt_dir.mkdir(parents=True)
    orphan = srt_dir / "lunch__00000-00013__chat.srt"     # an earlier boundary
    orphan.write_text("1\n")
    foreign = srt_dir / "notes.txt"                        # not ours to delete
    foreign.write_text("keep me")

    _, written, pruned = write()

    assert written == 1 and pruned == 1
    assert not orphan.exists()
    assert foreign.exists()
    assert (srt_dir / "lunch__00000-00027__chat.srt").exists()


def test_a_partial_srt_export_only_prunes_what_it_looked_at(archive):
    from splice import config
    from splice.export_srt import write
    from splice.schema import Line

    for stem in ("lunch", "balcony"):
        sc = _sc(stem, [
            Segment(id=f"{stem}#0", start=0, end=27, title="Chat",
                    lines=[Line(start=0, end=2, en="hello")]),
        ])
        sc.save(config.sidecar_path(stem))

    srt_dir = config.export_dir() / "srt"
    srt_dir.mkdir(parents=True)
    (srt_dir / "balcony__00000-00013__chat.srt").write_text("1\n")

    _, _, pruned = write(only="lunch")
    assert pruned == 0
    assert (srt_dir / "balcony__00000-00013__chat.srt").exists()


# --- the backup refuses to destroy itself ------------------------------


@pytest.fixture
def mirrored(tmp_path, monkeypatch):
    """A live index and an archive that already holds copies of it."""
    from splice import backup as store, config

    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("SPLICE_ARCHIVE", str(tmp_path / "archive"))

    config.index_dir().mkdir(parents=True)
    for stem in "abcdef":
        _sc(stem, [Segment(id=f"{stem}#0", start=0, end=5, title=stem)]).save(
            config.sidecar_path(stem)
        )
    store.backup()
    assert len(list((store.archive_root() / "sidecars").glob("*.seg.json"))) == 6
    return store


def _index_dir():
    from splice import config

    return config.index_dir()


def _sidecar_path(stem):
    from splice import config

    return config.sidecar_path(stem)


def test_an_unreadable_media_root_does_not_empty_the_backup(mirrored, monkeypatch, tmp_path):
    """The media root is a separate volume. Unmounted, it is not an archive
    with nothing in it -- it is an archive nobody can see."""
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path / "unmounted"))

    with pytest.raises(mirrored.Refused, match="does not exist"):
        mirrored.backup()
    assert len(list((mirrored.archive_root() / "sidecars").glob("*.seg.json"))) == 6


def test_a_live_directory_that_went_empty_does_not_empty_the_backup(mirrored):
    for path in _index_dir().glob("*.seg.json"):
        path.unlink()

    with pytest.raises(mirrored.Refused, match="no sidecars"):
        mirrored.backup()
    assert len(list((mirrored.archive_root() / "sidecars").glob("*.seg.json"))) == 6


def test_deleting_most_of_the_archive_needs_saying_so(mirrored):
    for stem in "abcde":
        _sidecar_path(stem).unlink()

    with pytest.raises(mirrored.Refused, match="would delete 5 of 6"):
        mirrored.backup()
    assert len(list((mirrored.archive_root() / "sidecars").glob("*.seg.json"))) == 6

    assert mirrored.backup(force=True)[2] == 5
    assert len(list((mirrored.archive_root() / "sidecars").glob("*.seg.json"))) == 1


def test_an_ordinary_rename_still_prunes(mirrored):
    """One clip renamed is exactly what the pruning is for."""
    _sidecar_path("a").rename(_sidecar_path("a_renamed"))

    added, _, removed = mirrored.backup()
    assert (added, removed) == (1, 1)
    assert not ((mirrored.archive_root() / "sidecars") / "a.seg.json").exists()


def test_emptying_the_backup_on_purpose_is_allowed(mirrored):
    """Deleting the sidecars and saying so is a decision, not an accident."""
    for path in _index_dir().glob("*.seg.json"):
        path.unlink()

    assert mirrored.backup(force=True)[2] == 6
    assert list((mirrored.archive_root() / "sidecars").glob("*.seg.json")) == []


def test_no_amount_of_forcing_mirrors_a_directory_that_is_not_there(
    mirrored, monkeypatch, tmp_path
):
    """An empty live directory is "I deleted them"; a missing one is "the drive
    is not mounted", and there is nothing to mirror, only something to lose."""
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path / "unmounted"))

    with pytest.raises(mirrored.Refused, match="does not exist"):
        mirrored.backup(force=True)
    assert len(list((mirrored.archive_root() / "sidecars").glob("*.seg.json"))) == 6


def test_correcting_a_translation_does_not_re_offer_the_clip(taxonomy):
    """An evening spent fixing Konkani in the browser should cost no imports.

    None of that prose reaches Final Cut; what does is the boundaries, the
    title, the keywords and the rating.
    """
    sidecar = _sc("a", [Segment(id="a#0", start=0, end=30, title="Marriage chat",
                                english="was", tags=["theme:marriage"])])
    before = sidecar.content_sha()

    sidecar.segments[0].english = "corrected"
    sidecar.segments[0].konkani = "corrected"
    sidecar.segments[0].summary = "corrected"
    assert sidecar.content_sha() == before

    sidecar.segments[0].title = "A better title"
    assert sidecar.content_sha() != before


def test_a_failed_export_leaves_the_previous_one_intact(tmp_path, monkeypatch, taxonomy, sidecars):
    """`incoming.fcpxml` is promised to be safe to import whenever it exists.

    Writing straight to the destination and raising afterwards left an invalid
    file there — and, worse, replaced a perfectly good one with it.
    """
    from splice import export_fcpxml

    dest = tmp_path / "incoming.fcpxml"
    export_fcpxml.write_validated(build_document(sidecars, taxonomy), dest)
    good = dest.read_text()

    monkeypatch.setattr(export_fcpxml, "validate", lambda *a, **k: "not a chance")
    with pytest.raises(ValueError, match="DTD"):
        export_fcpxml.write_validated(build_document(sidecars, taxonomy), dest)

    assert dest.read_text() == good
    assert list(tmp_path.glob("*.tmp")) == []


def test_export_all_writes_the_whole_library_not_the_incoming_queue(archive, monkeypatch, capsys):
    """`export all` used to call the FCPXML command without `new=`, which
    arrives as an OptionInfo and is truthy -- so it wrote incoming.fcpxml with
    only the undelivered clips, and stamped them as offered on the way."""
    from splice import cli, config
    from splice.export_fcpxml import mark_offered

    _library(monkeypatch, {})
    kept = _saved("already_offered")
    mark_offered([kept])
    _saved("brand_new")

    cli.export_all(only=None, editor=None)

    out = capsys.readouterr().out
    assert "2 clips" in out
    assert (config.export_dir() / "grandparents.fcpxml").exists()
    assert not (config.export_dir() / "incoming.fcpxml").exists()


def test_a_backup_that_is_interrupted_leaves_the_old_copy_whole(mirrored, monkeypatch):
    """This is the copy of the only two things here nothing regenerates."""
    from splice import backup as store, config

    live = next(config.index_dir().glob("*.seg.json"))
    saved = (store.archive_root() / "sidecars") / live.name
    before = saved.read_bytes()
    live.write_text(live.read_text().replace('"segments"', '"SEGMENTS"'))

    def die(source, target):
        target.write_bytes(b"half a tra")
        raise KeyboardInterrupt

    monkeypatch.setattr(store.shutil, "copy2", die)
    with pytest.raises(KeyboardInterrupt):
        store.backup()

    assert saved.read_bytes() == before
    assert not list((store.archive_root() / "sidecars").glob("*.tmp"))
