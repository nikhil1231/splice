"""Choosing an editor, and what choosing one changes about the document.

The differences between Final Cut and Resolve are not preferences, they are
facts about two importers, so these are checks that the right fact reached the
file -- not that a flag was passed along.
"""

from fractions import Fraction

import pytest

from greels import editors
from greels.export_fcpxml import build_document
from greels.schema import Asset, Segment, Sidecar
from greels.taxonomy import Taxonomy


@pytest.fixture
def taxonomy():
    return Taxonomy.load()


@pytest.fixture
def sidecars():
    return [
        Sidecar(
            asset=Asset(file="a.MOV", stem="a", duration=35.17, fps="30",
                        width=1080, height=1920),
            segments=[
                Segment(id="a#0", start=0, end=27, title="Marriage chat",
                        summary="They talk about the wedding.",
                        tags=["person:grandma", "theme:marriage"],
                        rating="favorite"),
                Segment(id="a#27", start=27, end=35.17, title="Coconut scraper",
                        tags=["object:coconut_scraper"]),
            ],
        ),
        Sidecar(
            asset=Asset(file="b.MOV", stem="b", duration=119.34,
                        fps="30000/1001", width=1920, height=1080),
            segments=[
                Segment(id="b#41", start=41.8, end=98.2, title="Rs 7 train",
                        tags=["theme:money"], rating="rejected"),
            ],
        ),
    ]


def _seconds(value: str) -> float:
    return float(Fraction(value.rstrip("s"))) if value and value != "0s" else 0.0


# --- naming the editor -------------------------------------------------


def test_the_two_editors_are_reachable_by_the_names_people_say():
    """`davinci` and `fcpx` are what the programs are called out loud.

    Refusing them would be correct and useless.
    """
    for said in ("resolve", "davinci", "DaVinci", "blackmagic", "DAVINCI RESOLVE"):
        assert editors.get(said).name == editors.RESOLVE
    for said in ("fcp", "fcpx", "Final Cut", "final_cut", "apple"):
        assert editors.get(said).name == editors.FCP


def test_a_name_that_is_neither_is_refused_rather_than_guessed_at():
    """A typo must not quietly write a Final Cut document and claim success.

    The whole point of the editor flag is that the two files are different; a
    silent fallback turns a typo into an import that does nothing.
    """
    with pytest.raises(editors.Unknown) as caught:
        editors.get("resolv")
    assert "resolve" in str(caught.value)


def test_nothing_asked_for_means_final_cut(monkeypatch):
    """Every sidecar here carries a Final Cut delivery record.

    A bare `greels sync` that started meaning Resolve would re-offer the whole
    archive on the next run.
    """
    monkeypatch.delenv("GREELS_EDITOR", raising=False)
    assert editors.get(None).name == editors.FCP
    assert editors.default().name == editors.FCP


def test_the_environment_can_change_the_default(monkeypatch):
    monkeypatch.setenv("GREELS_EDITOR", "resolve")
    assert editors.default().name == editors.RESOLVE
    assert editors.get(None).name == editors.RESOLVE


def test_an_unreadable_preference_does_not_stop_a_run(monkeypatch):
    """A mistyped environment variable should not be an exception at import.

    It is read on every command; raising here would make the archive
    unreachable until somebody found the export line in a shell profile.
    """
    monkeypatch.setenv("GREELS_EDITOR", "premiere")
    assert editors.default().name == editors.FCP


# --- what the choice does to the document ------------------------------


def test_final_cut_gets_keyword_ranges_and_resolve_gets_none(sidecars, taxonomy):
    """Resolve has no keyword ranges at all, so a `<keyword>` is dead weight.

    Worse than dead: it is the thing the Final Cut export is *for*, so leaving
    it in would read as though the tags had made the trip.
    """
    fcp = build_document(sidecars, taxonomy, editor="fcp").getroot()
    resolve = build_document(sidecars, taxonomy, editor="resolve").getroot()
    assert list(fcp.iter("keyword"))
    assert not list(resolve.iter("keyword"))
    assert list(fcp.iter("rating"))
    assert not list(resolve.iter("rating"))


def test_resolve_gets_the_tags_in_the_marker_note_instead(sidecars, taxonomy):
    """Losing the keywords must not mean losing what they said.

    A marker note is searchable in Resolve's Edit Index, so the tags stay
    findable even though the blue bars do not survive.
    """
    root = build_document(sidecars, taxonomy, editor="resolve").getroot()
    notes = [m.get("note") or "" for m in root.iter("marker")]
    assert any("Grandma" in n and "Marriage" in n for n in notes)
    assert any("Money" in n for n in notes)


def test_a_resolve_marker_carries_the_length_of_its_segment(sidecars, taxonomy):
    """In Final Cut the keyword holds the range and the marker is a signpost.

    In Resolve the marker is all there is, so a one-frame marker would turn a
    56-second answer into a dot.
    """
    root = build_document(sidecars, taxonomy, editor="resolve").getroot()
    marker = next(m for m in root.iter("marker") if m.get("value") == "Rs 7 train")
    assert _seconds(marker.get("duration")) == pytest.approx(56.4, abs=0.05)

    fcp = build_document(sidecars, taxonomy, editor="fcp").getroot()
    point = next(m for m in fcp.iter("marker") if m.get("value") == "Rs 7 train")
    assert _seconds(point.get("duration")) < 0.05


def test_resolve_gets_a_timeline_because_an_event_imports_as_nothing(
    sidecars, taxonomy
):
    """Resolve's only importer is `ImportTimelineFromFile`.

    An event full of clips is a document it reads without complaint and makes
    nothing out of, which is the worst failure available: no error, no clips.
    """
    resolve = build_document(sidecars, taxonomy, editor="resolve").getroot()
    assert resolve.find(".//project/sequence/spine") is not None
    assert len(resolve.findall(".//spine/asset-clip")) == len(sidecars)

    fcp = build_document(sidecars, taxonomy, editor="fcp").getroot()
    assert fcp.find(".//project") is None
    assert len(fcp.findall("./library/event/asset-clip")) == len(sidecars)


def test_the_stringout_has_no_gaps_even_with_mixed_frame_rates(sidecars, taxonomy):
    """A clip's duration on a spine is the timeline's, not its own.

    Writing the source's duration there puts a 30000/1001 clip 19 ms out of
    step with the 30 fps clips beside it, and every later clip inherits the
    drift -- black flashes at the joins, and a last clip hanging past the end
    of the sequence.
    """
    root = build_document(sidecars, taxonomy, editor="resolve").getroot()
    cursor = 0.0
    for clip in root.findall(".//spine/asset-clip"):
        assert _seconds(clip.get("offset")) == pytest.approx(cursor, abs=1e-9)
        cursor += _seconds(clip.get("duration"))
    sequence = root.find(".//sequence")
    assert _seconds(sequence.get("duration")) == pytest.approx(cursor, abs=1e-9)


def test_each_editor_is_handed_a_version_it_speaks(sidecars, taxonomy):
    """Resolve 20 exports FCPXML 1.10 at the newest and reads no further.

    Its own scripting README lists EXPORT_FCPXML_1_8/_1_9/_1_10 and records
    1.3-1.7 as dropped, so 1.11 is a version this archive can write and that
    editor cannot open.
    """
    assert build_document(sidecars, taxonomy, editor="fcp").getroot().get(
        "version"
    ) == "1.11"
    assert build_document(sidecars, taxonomy, editor="resolve").getroot().get(
        "version"
    ) == "1.10"


# --- the commands ------------------------------------------------------


@pytest.fixture
def archive(tmp_path, monkeypatch):
    from greels import config

    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    monkeypatch.delenv("GREELS_EDITOR", raising=False)
    config.index_dir().mkdir(parents=True, exist_ok=True)
    return tmp_path


def _two_clips():
    from greels import config

    for stem, duration in (("lunch", 40.0), ("house", 90.0)):
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=duration,
                        fps="30", width=1080, height=1920),
            segments=[Segment(id=f"{stem}#0", start=0, end=duration / 2,
                              title=f"{stem} opening", tags=["theme:money"])],
        ).save(config.sidecar_path(stem))


def test_the_two_incoming_files_do_not_overwrite_each_other(archive, capsys):
    """`incoming.fcpxml` promises that if it is there, importing it is safe.

    One editor's document written over the other's breaks that promise
    silently: the file is still there, still valid, and now for the wrong
    program.
    """
    from greels import cli, config

    _two_clips()
    cli.export_fcpxml_cmd(only=None, dest=None, event=None,
                          new=True, editor="fcp")
    cli.export_fcpxml_cmd(only=None, dest=None, event=None,
                          new=True, editor="resolve")

    assert (config.export_dir() / "incoming.fcpxml").exists()
    assert (config.export_dir() / "incoming.resolve.fcpxml").exists()


def test_a_command_told_an_editor_it_does_not_know_stops(archive, capsys):
    """Falling back to Final Cut would write the wrong file and say it worked.

    The point of the flag is that the two documents differ.
    """
    import typer

    from greels import cli, config

    _two_clips()
    with pytest.raises(typer.Exit):
        cli.export_fcpxml_cmd(only=None, dest=None, event=None,
                              new=True, editor="premiere")
    assert not (config.export_dir() / "incoming.fcpxml").exists()
    assert "no such editor" in capsys.readouterr().out


def test_sending_a_timeline_to_final_cut_is_refused_rather_than_ignored(
    archive, capsys
):
    """Final Cut has no scripting door of this kind.

    Writing the file and saying nothing would leave `--send` looking like it
    had sent something.
    """
    import typer

    from greels import cli_reel, reel as store

    _two_clips()
    store.Reel(slug="r", title="R",
               shots=[store.Shot(stem="lunch", start=1.0, end=9.0)]).save()
    with pytest.raises(typer.Exit):
        cli_reel.reel_export("r", every=False, stale=False, lang="en",
                             captions=True, level=False, editor="fcp",
                             send=True)
    assert "no way in" in capsys.readouterr().out


def test_a_resolve_that_will_not_answer_still_leaves_the_timeline_written(
    archive, capsys, monkeypatch
):
    """`--send` is a convenience on top of a file, not instead of one.

    Resolve is usually shut. Losing the export because of that would make the
    flag actively dangerous to reach for.
    """
    from greels import cli_reel, config, reel as store, resolve

    _two_clips()
    store.Reel(slug="r", title="R",
               shots=[store.Shot(stem="lunch", start=1.0, end=9.0)]).save()
    monkeypatch.setattr(resolve, "_media_pool", lambda app=None: None)

    cli_reel.reel_export("r", every=False, stale=False, lang="en",
                         captions=True, level=False, editor="resolve",
                         send=True)

    out = capsys.readouterr().out
    assert "Resolve is not running" in out
    assert (config.export_dir() / "reels" / "r.resolve.fcpxml").exists()


def test_a_cut_remembers_which_editor_its_timeline_was_written_for(archive):
    """Two filenames, one timestamp. Without this nothing says which.

    `reel list` reporting a cut as exported while the file on disk is the
    other editor's is a sentence that reads true and is not.
    """
    from greels import cli_reel, reel as store

    _two_clips()
    store.Reel(slug="r", title="R",
               shots=[store.Shot(stem="lunch", start=1.0, end=9.0)]).save()

    cli_reel.reel_export("r", every=False, stale=False, lang="en",
                         captions=True, level=False, editor="resolve",
                         send=False)
    assert store.load("r").exported_editor == "resolve"

    cli_reel.reel_export("r", every=False, stale=False, lang="en",
                         captions=True, level=False, editor="fcp", send=False)
    assert store.load("r").exported_editor == "fcp"


def test_styled_titles_are_refused_for_resolve_rather_than_written_empty(
    archive, capsys, tmp_path
):
    """The styled title is built on Final Cut's own Basic Title template.

    Resolve resolves that reference to nothing, so the export would import as
    a project full of empty generators -- which looks like it worked.
    """
    import typer

    from greels import cli

    doc = tmp_path / "Ep1.fcpxml"
    doc.write_text("<fcpxml/>")
    with pytest.raises(typer.Exit):
        cli.captions(project=doc, lang="en", dest=None, by_speaker=False,
                     titles=True, editor="resolve", dry_run=False)
    assert "Basic Title" in capsys.readouterr().out


def test_a_stringout_takes_the_shape_that_holds_the_most_footage(taxonomy):
    """A drop is not sorted, and the odd clip is often the first one.

    Conforming the whole stringout to whatever happened to be at the head
    letterboxes thirty portrait clips behind one landscape shot of a cupboard.
    """
    odd = Sidecar(
        asset=Asset(file="wide.MOV", stem="wide", duration=4.0, fps="24",
                    width=1280, height=720),
        segments=[Segment(id="wide#0", start=0, end=4, title="A cupboard")],
    )
    tall = [
        Sidecar(
            asset=Asset(file=f"{n}.MOV", stem=n, duration=90.0, fps="30",
                        width=1080, height=1920),
            segments=[Segment(id=f"{n}#0", start=0, end=90, title=n)],
        )
        for n in ("one", "two")
    ]
    root = build_document([odd, *tall], taxonomy, editor="resolve").getroot()

    shape = root.find(".//sequence").get("format")
    portrait = {
        f.get("id") for f in root.iter("format") if f.get("height") == "1920"
    }
    assert shape in portrait
