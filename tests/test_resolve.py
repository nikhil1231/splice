"""Reading what Resolve holds, and handing it a timeline.

Resolve is stood in for the way ffmpeg and agy are: what is tested is the
decision taken before the API call, and what happens when the API is not there
-- which for Resolve is the ordinary case, since it answers only while it is
running.
"""

from pathlib import Path

import pytest

from splice import editors, resolve
from splice.export_fcpxml import (
    Reconciliation, held_clips, mark_offered, pending, preview, reconcile,
)
from splice.schema import Asset, ExportRecord, Segment, Sidecar


class FakeClip:
    def __init__(self, name):
        self._name = name

    def GetName(self):
        return self._name


class FakeFolder:
    """A media pool folder, which is a tree and not a list."""

    def __init__(self, clips=(), folders=()):
        self._clips = [FakeClip(c) for c in clips]
        self._folders = list(folders)

    def GetClipList(self):
        return self._clips

    def GetSubFolderList(self):
        return self._folders


class FakePool:
    def __init__(self, root, timeline_name="Imported"):
        self._root = root
        self.imported = []
        self._timeline_name = timeline_name

    def GetRootFolder(self):
        return self._root

    def ImportTimelineFromFile(self, path, options):
        self.imported.append((path, options))
        if self._timeline_name is None:
            return None

        class Timeline:
            GetName = staticmethod(lambda: options.get("timelineName", "Imported"))

        return Timeline()


def attach(monkeypatch, pool):
    monkeypatch.setattr(resolve, "_media_pool", lambda app=None: pool)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    from splice import config

    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    config.index_dir().mkdir(parents=True, exist_ok=True)
    return tmp_path


def clip(stem, sha_tag="one"):
    return Sidecar(
        asset=Asset(file=f"{stem}.MOV", stem=stem, duration=30.0, fps="30",
                    width=1080, height=1920),
        segments=[Segment(id=f"{stem}#0", start=0, end=30, title=sha_tag)],
    )


# --- reading the pool --------------------------------------------------


def test_the_pool_is_walked_to_the_bottom(monkeypatch):
    """Material filed into a bin is still material Resolve holds.

    Asking only the root folder reports a week-old project, where everything
    has been sorted into bins, as an empty pool -- and an empty pool is grounds
    for offering every clip again.
    """
    attach(monkeypatch, FakePool(FakeFolder(
        clips=["lunch.MOV"],
        folders=[FakeFolder(clips=["uncle_arrest.MOV"],
                            folders=[FakeFolder(clips=["house.mov"])])],
    )))
    assert resolve.imported_clips() == {
        "lunch": 1, "uncle_arrest": 1, "house": 1
    }


def test_a_pool_item_is_matched_by_stem_not_by_filename(monkeypatch):
    """Resolve names a clip after the file, extension and all.

    Final Cut uses the clip name. One set of stems has to match either, so the
    extension comes off here rather than in every caller.
    """
    assert resolve.base_name("uncle_arrest.MOV") == "uncle_arrest"
    assert resolve.base_name("already_a_stem") == "already_a_stem"


def test_resolve_not_running_is_unreadable_and_not_empty(monkeypatch):
    """None and {} decide opposite things, and Resolve is usually None.

    An empty pool means offer everything. An unreachable Resolve means change
    nothing -- otherwise closing Resolve would re-offer the whole archive.
    """
    monkeypatch.setattr(resolve, "connect", lambda: None)
    assert resolve.imported_clips() is None
    assert resolve.duplicate_names() == {}


def test_one_unreadable_clip_does_not_lose_the_others(monkeypatch):
    """A pool item can be a compound or a timeline that answers oddly."""

    class Awkward:
        def GetName(self):
            raise RuntimeError("not that kind of item")

    folder = FakeFolder(clips=["lunch.MOV"])
    folder._clips.append(Awkward())
    attach(monkeypatch, FakePool(folder))
    assert resolve.imported_clips() == {"lunch": 1}


def test_two_pool_entries_for_one_stem_are_reported(monkeypatch):
    """Resolve reuses a clip rather than duplicating it, so this is rare.

    It happens when the same footage sits at two paths, and that is worth being
    told about rather than silently counted twice.
    """
    attach(monkeypatch, FakePool(FakeFolder(clips=["lunch.MOV", "lunch.mov"])))
    assert resolve.duplicate_names() == {"lunch": ["lunch.MOV", "lunch.mov"]}


# --- handing it a timeline ---------------------------------------------


def test_sending_a_timeline_asks_for_the_sources_too(monkeypatch, tmp_path):
    """The timeline is the vehicle; the media pool is the destination.

    Without `importSourceClips` Resolve makes a timeline of offline clips,
    which looks like a successful import and plays nothing.
    """
    pool = FakePool(FakeFolder())
    attach(monkeypatch, pool)
    doc = tmp_path / "1961.fcpxml"
    doc.write_text("<fcpxml/>")

    assert resolve.import_timeline(doc, name="1961") == "1961"
    path, options = pool.imported[0]
    assert Path(path).is_absolute()
    assert options["importSourceClips"] is True
    assert options["timelineName"] == "1961"


def test_sending_to_a_resolve_that_is_not_running_says_what_to_do(
    monkeypatch, tmp_path
):
    """"Resolve is not running" is a sentence with a fix in it.

    A None from `scriptapp` is not.
    """
    monkeypatch.setattr(resolve, "_media_pool", lambda app=None: None)
    doc = tmp_path / "1961.fcpxml"
    doc.write_text("<fcpxml/>")
    with pytest.raises(resolve.NotRunning) as caught:
        resolve.import_timeline(doc)
    assert "open the project" in str(caught.value)


def test_a_timeline_resolve_declined_to_make_is_not_reported_as_sent(
    monkeypatch, tmp_path
):
    """`ImportTimelineFromFile` returns a falsy value rather than raising.

    Taking that for success is how an export gets marked delivered on the
    strength of nothing having crashed.
    """
    attach(monkeypatch, FakePool(FakeFolder(), timeline_name=None))
    doc = tmp_path / "1961.fcpxml"
    doc.write_text("<fcpxml/>")
    with pytest.raises(resolve.NotRunning) as caught:
        resolve.import_timeline(doc)
    assert "made no timeline" in str(caught.value)


# --- two editors, two sets of delivery state ---------------------------


def test_what_final_cut_holds_says_nothing_about_what_resolve_holds(
    archive, monkeypatch
):
    """A clip in one is not a clip in the other, and one record cannot say so.

    Sharing the Final Cut record would mark the whole archive as delivered to
    Resolve the moment the editor was switched, which is the eight-lost-clips
    failure with the sign flipped.
    """
    sidecars = [clip("lunch")]
    monkeypatch.setattr(
        "splice.fcp.imported_clips", lambda bundle=None: {"lunch": 1}
    )
    monkeypatch.setattr("splice.fcp.duplicate_names", lambda bundle=None: {})
    reconcile(sidecars, "fcp")

    assert sidecars[0].delivery("fcp").delivered
    assert not sidecars[0].delivery("resolve").delivered

    fresh, _ = pending(sidecars, "fcp")
    assert fresh == []
    fresh, _ = pending(sidecars, "resolve")
    assert [sc.asset.stem for sc in fresh] == ["lunch"]


def test_an_unreachable_resolve_leaves_every_record_alone(archive, monkeypatch):
    """The common case, and it must be inert.

    Final Cut's library is a file and answers whether or not it is open;
    Resolve answers only while it is running, so "cannot read" is what a sync
    at midnight sees, and it must not withdraw anything.
    """
    sidecars = [clip("lunch")]
    sidecars[0].set_delivery("resolve", ExportRecord(
        imported_at="2026-09-01T00:00:00+00:00", imported_sha="abc",
        fcp_name="lunch", fcp_copies=1,
    ))
    monkeypatch.setattr(resolve, "imported_clips", lambda app=None: None)

    report = reconcile(sidecars, "resolve")
    assert report == Reconciliation(seen=False, editor="resolve")
    assert sidecars[0].delivery("resolve").imported_at is not None
    assert preview(sidecars, "resolve") is None


def test_a_changed_clip_resolve_already_has_is_taken_as_re_imported(
    archive, monkeypatch
):
    """Resolve reuses the pool clip instead of making a second copy.

    Final Cut's proof that a re-offer landed is the copy count growing. There
    is no count to grow here, so an outstanding offer for a clip that is in the
    pool is taken as landed. What is unverified is whether the markers came
    with it, and a stale marker costs a re-import rather than a clip.
    """
    sidecars = [clip("lunch")]
    monkeypatch.setattr(resolve, "imported_clips", lambda app=None: {"lunch": 1})
    monkeypatch.setattr(resolve, "duplicate_names", lambda app=None: {})

    reconcile(sidecars, "resolve")
    record = sidecars[0].delivery("resolve")
    assert record.delivered

    sidecars[0].segments[0].title = "A better title"
    _, changed = pending(sidecars, "resolve")
    assert [sc.asset.stem for sc in changed] == ["lunch"]

    mark_offered(sidecars, held_clips("resolve"), "resolve")
    reconcile(sidecars, "resolve")
    assert sidecars[0].delivery("resolve").imported_sha == sidecars[0].content_sha()
    assert pending(sidecars, "resolve") == ([], [])


def test_a_clip_resolve_turns_out_not_to_have_comes_back(archive, monkeypatch):
    """The eight-clips rule, for the second editor.

    An offer that was written and never imported has to rejoin the queue, and
    that is true of a media pool exactly as it is of a Final Cut event.
    """
    sidecars = [clip("lunch")]
    sidecars[0].set_delivery("resolve", ExportRecord(
        offered_at="2026-09-01T00:00:00+00:00", offered_sha="abc",
    ))
    monkeypatch.setattr(resolve, "imported_clips", lambda app=None: {})
    monkeypatch.setattr(resolve, "duplicate_names", lambda app=None: {})

    report = reconcile(sidecars, "resolve")
    assert report.withdrawn == ["lunch"]
    assert sidecars[0].delivery("resolve") == ExportRecord()
    assert sidecars[0].delivery("fcp") == ExportRecord()


def test_a_sidecar_written_before_resolve_existed_still_loads(archive):
    """Every sidecar in the archive predates the second editor.

    They have no `deliveries` key at all, and must load as clips Final Cut
    holds and Resolve does not.
    """
    from splice import config

    path = config.sidecar_path("lunch")
    path.write_text(
        '{"asset": {"file": "lunch.MOV", "stem": "lunch"}, '
        '"exported": {"imported_at": "2026-09-01T00:00:00+00:00", '
        '"imported_sha": "abc", "fcp_name": "lunch", "fcp_copies": 1}, '
        '"segments": []}'
    )
    loaded = Sidecar.load(path)
    assert loaded.delivery("fcp").delivered
    assert not loaded.delivery("resolve").delivered


def test_the_default_editor_is_what_an_unasked_call_gets(archive, monkeypatch):
    """Every one of these takes an optional editor, and omitting it is common.

    Omitting it has to keep meaning Final Cut, or a caller that was never
    updated starts writing to the wrong record.
    """
    monkeypatch.delenv("SPLICE_EDITOR", raising=False)
    sidecars = [clip("lunch")]
    mark_offered(sidecars)
    assert sidecars[0].exported.offered_at is not None
    assert sidecars[0].deliveries == {}
    assert editors.get(None).name == "fcp"


def test_a_re_analysis_keeps_every_editors_delivery_record(archive):
    """The same bug the Final Cut carry-over was written for, one editor along.

    A re-analysis builds a fresh sidecar. If Resolve's record does not come
    with it, the next sync finds the clip in the media pool, takes it for a
    new arrival, and confirms it against the *new* segments -- leaving Resolve
    holding a stringout of markers for boundaries that no longer exist, and
    nothing saying so.
    """
    from splice.cli import _carry_review_state

    old = clip("lunch")
    old.set_delivery("resolve", ExportRecord(
        imported_at="2026-09-01T00:00:00+00:00", imported_sha="old-sha",
        fcp_name="lunch", fcp_copies=1))
    old.exported = ExportRecord(
        imported_at="2026-09-01T00:00:00+00:00", imported_sha="old-sha",
        fcp_name="lunch", fcp_copies=1)

    fresh = clip("lunch", sha_tag="re-analysed, different boundaries")
    _carry_review_state(old, fresh)

    assert fresh.delivery("fcp").imported_sha == "old-sha"
    assert fresh.delivery("resolve").imported_sha == "old-sha"
    # And so both are offered again, because neither matches what is there now.
    for editor in ("fcp", "resolve"):
        _, changed = pending([fresh], editor)
        assert [sc.asset.stem for sc in changed] == ["lunch"]
