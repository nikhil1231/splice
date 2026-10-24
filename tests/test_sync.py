"""The whole loop, run for real with only the model and ffmpeg stood in for.

The point of `sync` is that running it twice is safe, and that nothing falls out
of the pipeline between runs. Those are properties of the sequence, not of any
one step, so they are only worth testing end to end.
"""

import json
import re

import pytest

from greels import cli, config, library
from greels.schema import Asset, Sidecar


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    monkeypatch.delenv("GREELS_FCP_LIBRARY", raising=False)
    for path in (config.raw_dir(), config.proxy_dir(), config.chunk_dir()):
        path.mkdir(parents=True)
    (config.export_dir() / "srt").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def stubbed(monkeypatch):
    """ffmpeg, ffprobe and agy stand in; everything else is the real thing."""
    import greels.analyze as analyze
    import greels.probe as probe
    import greels.proxy as proxy

    analysed: list[str] = []

    def fake_probe(path):
        stat = path.stat()
        return Asset(
            file=path.name, stem=path.stem, duration=60.0, fps="30",
            width=1080, height=1920, fingerprint=probe.fingerprint(path),
            mtime=stat.st_mtime, size_bytes=stat.st_size,
        )

    monkeypatch.setattr(probe, "probe_asset", fake_probe)
    monkeypatch.setattr(proxy, "build_proxy", lambda src, dest, force=False: (
        dest.parent.mkdir(parents=True, exist_ok=True), dest.write_bytes(b"proxy"), dest
    )[-1])
    monkeypatch.setattr(proxy, "build_chunks", lambda *a, **k: [])

    def fake_units(asset, chunk_seconds):
        path = library.proxy_for(asset.stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"proxy")
        return [(0.0, path)]

    monkeypatch.setattr(analyze, "_units_for", fake_units)
    monkeypatch.setattr(analyze, "LAUNCH_STAGGER", 0.0)

    def fake_agy(prompt, schema_path):
        # The prompt names the proxy, which is named for the clip.
        stem = re.search(r"([^/\s`]+)\.mp4", prompt).group(1)
        analysed.append(stem)
        return {"segments": [{
            "start": 0, "end": 60, "title": "Talking about the router",
            "summary": "A summary.", "konkani": "kitem", "english": "what is it",
            "tags": ["object:router"],
            "lines": [{"start": 0, "end": 3, "kok": "kitem", "en": "what is it"}],
        }]}

    monkeypatch.setattr(analyze, "run_agy", fake_agy)
    monkeypatch.setattr(
        "greels.export_fcpxml.validate", lambda *a, **k: None
    )
    return analysed


def drop(stem, content=None, suffix=".MOV"):
    path = config.raw_dir() / f"{stem}{suffix}"
    path.write_bytes(content or f"footage for {stem}".encode())
    return path


def held(monkeypatch, clips):
    import greels.export_fcpxml as mod

    monkeypatch.setattr(mod.fcp, "imported_clips", lambda *a, **k: clips)
    monkeypatch.setattr(mod.fcp, "duplicate_names", lambda *a, **k: {})


def incoming():
    return config.export_dir() / "incoming.fcpxml"


def test_a_clip_dropped_in_raw_comes_out_the_other_end(archive, stubbed, monkeypatch):
    held(monkeypatch, {})
    drop("IMG_8768")

    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    # Analysed, named from that analysis, indexed, and offered.
    sc = Sidecar.load(config.sidecar_path("talking_about_the_router"))
    assert sc.segments[0].english == "what is it"
    assert sc.segments[0].summary == "A summary."
    assert sc.segments[0].tags == ["object:router"]
    assert sc.asset.previous_names == ["IMG_8768"]
    assert config.db_path().exists()
    assert incoming().exists()
    assert (config.export_dir() / "srt").glob("talking_about_the_router__*.srt")


def test_running_it_again_analyses_nothing_and_offers_nothing_new(
    archive, stubbed, monkeypatch
):
    held(monkeypatch, {})
    drop("IMG_8768")
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)
    stubbed.clear()

    # Final Cut now holds it, under the name it was given.
    held(monkeypatch, {"talking_about_the_router": 1})
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    assert stubbed == []
    # The offer file must not survive as something already imported.
    assert not incoming().exists()


def test_an_offer_that_was_never_imported_is_offered_again(archive, stubbed, monkeypatch):
    """The failure that lost eight clips: the file was written, never imported,
    and the next run deleted it and moved on."""
    held(monkeypatch, {})
    drop("IMG_8768")
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)
    assert incoming().exists()

    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)
    assert incoming().exists()
    assert not Sidecar.load(
        config.sidecar_path("talking_about_the_router")
    ).exported.delivered


def test_the_same_footage_dropped_again_costs_nothing(archive, stubbed, monkeypatch):
    held(monkeypatch, {})
    drop("IMG_8768", b"identical bytes")
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)
    held(monkeypatch, {"talking_about_the_router": 1})
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)
    stubbed.clear()

    # Re-copied off the phone under the camera's name, months later.
    drop("IMG_8768", b"identical bytes")
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    assert stubbed == []
    assert not config.sidecar_path("IMG_8768").exists()
    assert not library.proxy_for("IMG_8768").exists()
    assert not incoming().exists()


def test_a_rename_in_finder_does_not_cost_an_analysis(archive, stubbed, monkeypatch):
    held(monkeypatch, {})
    drop("grandpa_old_money_box")
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)
    stubbed.clear()

    (config.raw_dir() / "grandpa_old_money_box.MOV").rename(
        config.raw_dir() / "the_money_box.MOV"
    )
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    assert stubbed == []
    moved = Sidecar.load(config.sidecar_path("the_money_box"))
    assert moved.segments[0].title == "Talking about the router"
    assert moved.asset.previous_names == ["grandpa_old_money_box"]


def test_a_promoted_tag_reaches_the_export_without_a_second_command(
    archive, stubbed, monkeypatch, tmp_path
):
    """`sync` never reclassified, so growing the vocabulary changed nothing."""
    held(monkeypatch, {})
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text("namespaces: [object]\ntags: {}\n")
    monkeypatch.setattr(config, "taxonomy_path", lambda: taxonomy)

    drop("IMG_1")
    cli.sync(jobs=1, rename_new=False, dry_run=False, editor=None)
    sc = Sidecar.load(config.sidecar_path("IMG_1"))
    assert sc.segments[0].proposed_tags == ["object:router"]

    taxonomy.write_text(
        "namespaces: [object]\ntags:\n  object:router: {fcp_label: Router}\n"
    )
    cli.sync(jobs=1, rename_new=False, dry_run=False, editor=None)

    sc = Sidecar.load(config.sidecar_path("IMG_1"))
    assert sc.segments[0].tags == ["object:router"]
    assert sc.segments[0].proposed_tags == []


def test_a_failed_analysis_does_not_stop_the_others_being_named(
    archive, stubbed, monkeypatch
):
    import greels.analyze as analyze

    held(monkeypatch, {})
    drop("IMG_1")
    drop("IMG_2")

    real = analyze.run_agy

    def flaky(prompt, schema_path):
        if "IMG_1" in prompt:
            raise analyze.AnalysisError("agy fell over")
        return real(prompt, schema_path)

    monkeypatch.setattr(analyze, "run_agy", flaky)
    monkeypatch.setattr(analyze.config, "AGY_RETRIES", 0)

    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    assert config.sidecar_path("talking_about_the_router").exists()
    assert config.sidecar_path("IMG_1").exists()  # kept, still camera-named


def test_an_empty_drop_bin_is_not_an_error(archive, stubbed, monkeypatch):
    held(monkeypatch, None)
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)
    assert not incoming().exists()


def test_dry_run_changes_nothing(archive, stubbed, monkeypatch):
    held(monkeypatch, {})
    drop("IMG_8768")

    cli.sync(jobs=1, rename_new=True, dry_run=True, editor=None)

    assert not config.sidecar_path("IMG_8768").exists()
    assert not incoming().exists()
    assert stubbed == []


def test_sync_passes_every_option_to_the_commands_it_calls():
    """A typer default left unpassed arrives as an OptionInfo, which is truthy.

    Calling a command function directly is convenient and this is its one trap:
    `scan(force=False)` once silently suppressed scan's own output because the
    flag it did not pass defaulted to an object rather than to False.
    """
    import ast
    import inspect

    import typer

    from greels import cli

    tree = ast.parse(inspect.getsource(cli.sync))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        target = getattr(cli, node.func.id, None)
        if target is None or not callable(target):
            continue
        expected = {
            name for name, param in inspect.signature(target).parameters.items()
            if isinstance(param.default, typer.models.OptionInfo)
        }
        passed = {kw.arg for kw in node.keywords}
        assert not (expected - passed), (
            f"sync calls {node.func.id}() without {sorted(expected - passed)}; "
            "those defaults arrive as OptionInfo objects, not values"
        )


def test_sync_reports_what_it_scanned(archive, stubbed, monkeypatch, capsys):
    held(monkeypatch, {})
    drop("IMG_8768")

    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    out = capsys.readouterr().out
    assert "1 clips" in out
    assert "-> " in out


def test_sync_leaves_the_audio_measurements_warm(archive, stubbed, monkeypatch):
    """Both are under a second a clip and both are wanted the moment you start
    cutting; doing them here means the first cut does not wait on ffmpeg."""
    from greels import loudness, speech

    held(monkeypatch, {})
    drop("IMG_8768")
    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    assert speech.cache_path().exists()
    assert loudness.cache_path().exists()
    stem = next(iter(json.loads(speech.cache_path().read_text())))
    assert stem in json.loads(loudness.cache_path().read_text())


def test_a_sync_of_a_scratch_archive_does_not_mirror_over_the_real_one(
    archive, stubbed, monkeypatch, capsys
):
    """`archive/` mirrors one media root and deletes what that root does not
    have. A sync of somewhere else, backing up without being asked, is how the
    transcripts that took hours of agy stop existing."""
    monkeypatch.delenv("GREELS_ARCHIVE", raising=False)
    held(monkeypatch, {})
    drop("IMG_8768")

    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    said = capsys.readouterr().out
    assert "not backed up" in said
    # The repo's own archive was never even looked at.
    assert not (archive / "archive").exists()


def test_a_sync_mirrors_the_transcripts_it_just_made(
    archive, stubbed, monkeypatch, capsys
):
    """The end of a sync is the only moment a new sidecar exists nowhere else.
    The browser's copy of this loop had the server's autosave behind it; a run
    from a terminal had nothing, and eleven of them sat outside git for two
    days before anybody opened the page."""
    saved = archive / "kept"
    monkeypatch.setenv("GREELS_ARCHIVE", str(saved))
    held(monkeypatch, {})
    drop("IMG_8768")

    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    assert (saved / "sidecars" / "talking_about_the_router.seg.json").exists()
    assert json.loads((saved / "manifest.json").read_text())["clips"] == 1
    assert "backed up into archive/" in capsys.readouterr().out


def test_a_sync_puts_the_new_subtitles_on_the_speech(archive, stubbed, monkeypatch):
    """The model estimates line times and is about half a second out, which is
    a caption arriving before the mouth moves. `align` was a thing you had to
    remember, on the evening you are least likely to."""
    from greels import speech

    monkeypatch.setenv("GREELS_ARCHIVE", str(archive / "kept"))
    # The utterance really starts at 0.9; the model guessed 0. Inside the
    # 1.2s window, because a snap is a correction of half a second and not a
    # licence to move a caption anywhere.
    monkeypatch.setattr(speech, "runs_for", lambda stem, cache=None: [[0.9, 3.0]])
    held(monkeypatch, {})
    drop("IMG_8768")

    cli.sync(jobs=1, rename_new=True, dry_run=False, editor=None)

    sc = Sidecar.load(config.sidecar_path("talking_about_the_router"))
    # On the onset less LEAD_IN, so the caption is up as the mouth moves
    # rather than a frame behind it.
    assert sc.segments[0].lines[0].start == pytest.approx(0.9 - speech.LEAD_IN)
    # Only the lines. Moving a segment boundary renumbers its id, which costs
    # every reel that names it its provenance.
    assert sc.segments[0].start == 0
