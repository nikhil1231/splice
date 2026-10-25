"""The browsing/editing API.

Tested through Api rather than over HTTP: the routing is a thin shell, the
interesting behaviour is that an edit lands in the sidecar and that a bad one
is refused before it does.
"""

import contextlib
import threading
from pathlib import Path

import pytest

from splice import config, index
from splice.schema import Asset, Line, Segment, Sidecar
from splice.server import Api


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    config.raw_dir().mkdir(parents=True, exist_ok=True)
    (config.raw_dir() / "a.MOV").write_bytes(b"movie")
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=120, fps="30", width=1080, height=1920),
        segments=[
            Segment(id="a#00000.00", start=0, end=30, title="Marriage chat",
                    summary="They talk about a wedding.", english="Is he married?",
                    tags=["person:grandma", "theme:marriage"],
                    lines=[Line(start=0, end=2, kok="Lagn zalato?", en="Is he married?")]),
            Segment(id="a#00030.00", start=30, end=60, title="Coconut scraper",
                    tags=["object:coconut_scraper"]),
        ],
    ).save(config.sidecar_path("a"))
    index.build()
    return Api()


def test_overview_counts_the_archive(api):
    o = api.overview()
    assert (o["clips"], o["analysed"], o["segments"]) == (1, 1, 2)


def test_clip_returns_its_segments(api):
    assert len(api.clip("a")["segments"]) == 2


def test_unknown_clip_is_a_key_error(api):
    with pytest.raises(KeyError):
        api.clip("nope")


def test_segments_can_be_filtered(api):
    assert len(api.segments(query="marriage")) == 1
    assert len(api.segments(tags=["object:coconut_scraper"])) == 1
    assert api.segments(query="marriage", tags=["object:coconut_scraper"]) == []


def test_a_search_reaches_the_transcript_and_says_which_line_matched(api):
    """What the removed Said-out-loud tab was for. Only the line carries the
    Konkani here, and the row has to say where in the moment it is said."""
    row, = api.segments(query="Lagn")
    assert row["id"] == "a#00000.00"
    assert [line["start"] for line in row["said"]] == [0]


def test_texts_returns_every_segment_and_its_read_only_prose(api):
    texts = api.texts()

    assert [item["id"] for item in texts] == ["a#00000.00", "a#00030.00"]
    assert texts[0]["summary"] == "They talk about a wedding."
    assert texts[0]["english"] == "Is he married?"
    assert texts[0]["konkani"] == ""
    assert "rating" not in texts[0]


def test_editing_writes_through_to_the_sidecar(api):
    api.update_segment("a#00000.00", {"title": "Renamed", "rating": "favorite"})

    from splice import library

    saved = library.load_sidecar("a").segments[0]
    assert saved.title == "Renamed"
    assert saved.rating == "favorite"
    # and the index follows, so search sees it without a manual rebuild
    assert api.segments(query="Renamed")[0]["title"] == "Renamed"


def test_editing_quarantines_an_unknown_tag(api):
    result = api.update_segment("a#00000.00", {"tags": ["person:grandma", "theme:invented"]})
    assert result["tags"] == ["person:grandma"]
    assert result["proposed_tags"] == ["theme:invented"]


def test_an_invalid_rating_is_refused_and_changes_nothing(api):
    with pytest.raises(Exception):
        api.update_segment("a#00000.00", {"rating": "amazing"})

    from splice import library

    assert library.load_sidecar("a").segments[0].rating == "none"


def test_fields_that_are_not_editable_are_ignored(api):
    """Timings come from the analysis; the UI must not be able to shift them."""
    result = api.update_segment("a#00000.00", {"start": 999, "end": 1000, "id": "hacked"})
    assert (result["start"], result["end"]) == (0, 30)
    assert result["id"] == "a#00000.00"


def test_deleting_a_segment_leaves_the_rest(api):
    assert api.delete_segment("a#00030.00")["remaining"] == 1

    from splice import library

    assert [s.id for s in library.load_sidecar("a").segments] == ["a#00000.00"]


def test_a_save_answers_with_the_patch_that_undoes_it_and_that_one_with_the_redo(api):
    """The page's undo sends back what the server says the save changed. A
    corrected line also rewrote the prose, and an unknown tag moved into
    `proposed_tags`; neither was in the patch the page sent."""
    first = api.update_segment("a#00000.00", {
        "title": "Renamed", "tags": ["person:grandma", "theme:invented"],
        "lines": [{"start": 0, "end": 2, "kok": "Lagn zalato?", "en": "Married?"}],
    })
    assert first["undo"] == {
        "title": "Marriage chat",
        "english": "Is he married?",
        "lines": [{"start": 0.0, "end": 2.0, "kok": "Lagn zalato?", "en": "Is he married?",
                   "speaker": ""}],
        "tags": ["person:grandma", "theme:marriage"],
    }

    undone = api.update_segment("a#00000.00", first["undo"])
    segment = api.clip("a")["segments"][0]
    assert (segment["title"], segment["english"], segment["tags"], segment["proposed_tags"]) == (
        "Marriage chat", "Is he married?", ["person:grandma", "theme:marriage"], [])
    assert segment["lines"][0]["en"] == "Is he married?"

    api.update_segment("a#00000.00", undone["undo"])
    segment = api.clip("a")["segments"][0]
    assert (segment["title"], segment["english"], segment["proposed_tags"]) == (
        "Renamed", "Married?", ["theme:invented"])


def test_a_save_that_changed_nothing_has_nothing_to_undo(api):
    assert api.update_segment("a#00000.00", {"title": "Marriage chat"})["undo"] == {}


def test_a_deleted_segment_is_put_back_where_it_was_and_only_once(api):
    """Undo without the trash: restoring the trash copy would put back the
    whole sidecar, and take every correction made to the clip since with it."""
    api.update_segment("a#00000.00", {"title": "Corrected after the delete"})
    gone = api.delete_segment("a#00000.00")["segment"]
    assert gone["title"] == "Corrected after the delete"
    api.update_segment("a#00030.00", {"title": "Also corrected"})

    api.restore_segment({"segment": gone})

    from splice import library
    from splice.drafts import Conflict

    assert [(s.id, s.title) for s in library.load_sidecar("a").segments] == [
        ("a#00000.00", "Corrected after the delete"), ("a#00030.00", "Also corrected")]
    assert api.segments(query="Corrected")[0]["id"] == "a#00000.00"
    with pytest.raises(Conflict):
        api.restore_segment({"segment": gone})


def test_deleting_something_that_is_not_there(api):
    with pytest.raises(KeyError):
        api.delete_segment("a#99999.00")


# --- cuts --------------------------------------------------------------


def test_a_cut_starts_from_segments_and_takes_their_ranges(api):
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})

    assert cut["slug"] == "marriage"
    assert cut["shots"][0]["start"] == 0 and cut["shots"][0]["end"] == 30
    assert cut["shots"][0]["segment_id"] == "a#00000.00"
    assert cut["duration"] == 30


def test_a_cut_without_a_title_is_refused(api):
    with pytest.raises(ValueError):
        api.create_cut({"title": "  "})


def test_the_running_order_is_replaced_whole(api):
    """Reordering and trimming arrive as one array, not as position edits.

    A diff would have a position to race against, and a half-applied reorder
    is a cut you cannot reason about.
    """
    api.create_cut({"title": "Marriage", "segments": ["a#00000.00", "a#00030.00"]})
    cut = api.update_cut("marriage", {"shots": [
        {"stem": "a", "start": 30, "end": 45, "segment_id": "a#00030.00"},
        {"stem": "a", "start": 4, "end": 12, "segment_id": "a#00000.00"},
    ]})

    assert [s["display_title"] for s in cut["shots"]] == ["Coconut scraper", "Marriage chat"]
    assert [s["offset"] for s in cut["shots"]] == [0, 15]
    assert cut["duration"] == 23


def test_a_trim_does_not_touch_the_segment(api):
    api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    api.update_cut("marriage", {"shots": [
        {"stem": "a", "start": 6, "end": 9, "segment_id": "a#00000.00"},
    ]})

    segment = api.clip("a")["segments"][0]
    assert (segment["start"], segment["end"]) == (0, 30)


def test_a_bad_status_is_refused(api):
    api.create_cut({"title": "Marriage"})
    with pytest.raises(Exception):
        api.update_cut("marriage", {"status": "nearly"})


def test_a_segment_knows_which_cuts_use_it(api):
    api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    assert [c["slug"] for c in api.cuts_using("a#00000.00")] == ["marriage"]
    assert api.cuts_using("a#00030.00") == []


def test_adding_to_a_cut_appends_by_default(api):
    api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    cut = api.add_to_cut("marriage", {"segment": "a#00030.00"})
    assert [s["segment_id"] for s in cut["shots"]] == ["a#00000.00", "a#00030.00"]

    cut = api.add_to_cut("marriage", {"segment": "a#00030.00", "at": 0})
    assert cut["shots"][0]["segment_id"] == "a#00030.00"


def test_an_empty_cut_will_not_export(api):
    api.create_cut({"title": "Marriage"})
    with pytest.raises(ValueError, match="empty"):
        api.export_cut("marriage")


def test_a_cut_is_indexed_so_it_survives_a_rebuild(api):
    api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    index.build()
    conn = index.connect()
    assert conn.execute("SELECT title FROM reels").fetchone()[0] == "Marriage"


# --- serving the proxies -----------------------------------------------
#
# Tested over real HTTP, unlike everything above: the whole point of this route
# is the Range handling, which lives in the request handler and not in Api.


@pytest.fixture
def http(api, tmp_path):
    import threading

    from splice import config
    from splice.server import serve

    proxy = config.proxy_dir() / "a.mp4"
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(bytes(range(256)) * 8)

    httpd = serve(port=0)
    # `shutdown` waits for the serving loop to notice, and the loop only looks
    # between polls -- at the 0.5s default that is half a second of teardown on
    # every test here, which was a third of the whole suite's runtime.
    thread = threading.Thread(
        target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url, headers=None):
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_the_page_and_its_json_are_never_cached(http):
    """A tab left open across an edit kept showing the old UI.

    The page is one file rewritten by every change to the browser, and it was
    going out with no cache headers, so browsers cached it on their own
    heuristic. Only what goes through `_send` is covered: a poster frame sets
    its own `max-age` and never reaches it, which is why it is not asserted
    here -- there is no real frame behind a fake proxy, so it would answer
    with a 404 that does go through `_send`.
    """
    for path in ("/", "/api/overview", "/api/clips"):
        _, headers, _ = _get(http + path)
        assert headers.get("Cache-Control") == "no-store", path


def test_the_optional_plan_schedule_is_served_without_changing_it(http, tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("SPLICE_CONFIG", str(tmp_path / "splice.yaml"))
    status, _, body = _get(http + "/api/plans/seed")
    assert status == 200
    assert json.loads(body) is None
    path = tmp_path / "plans" / "schedule.json"
    path.parent.mkdir()
    schedule = {"id": "family-trip", "plans": [{"id": "one", "moments": []}]}
    path.write_text(json.dumps(schedule))
    before = path.read_bytes()
    status, headers, body = _get(http + "/api/plans/seed")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == schedule
    assert path.read_bytes() == before


def test_a_proxy_is_served_whole_when_nothing_is_asked_for(http):
    status, headers, body = _get(f"{http}/media/a.mp4")
    assert status == 200
    assert headers["Content-Type"] == "video/mp4"
    assert headers["Accept-Ranges"] == "bytes"
    assert len(body) == 2048


def test_a_range_request_gets_exactly_that_range(http):
    """Without 206 the browser refetches the file to move the playhead a
    second, which makes scrubbing to a trim point unusable."""
    status, headers, body = _get(f"{http}/media/a.mp4", {"Range": "bytes=100-199"})
    assert status == 206
    assert headers["Content-Range"] == "bytes 100-199/2048"
    assert body == (bytes(range(256)) * 8)[100:200]


def test_an_open_ended_range_runs_to_the_end(http):
    status, headers, body = _get(f"{http}/media/a.mp4", {"Range": "bytes=2040-"})
    assert (status, len(body)) == (206, 8)
    assert headers["Content-Range"] == "bytes 2040-2047/2048"


def test_a_clip_with_no_proxy_is_a_404(http):
    assert _get(f"{http}/media/nope.mp4")[0] == 404


def test_a_path_cannot_climb_out_of_the_proxy_directory(http, tmp_path):
    (tmp_path / "secret.mp4").write_bytes(b"private")
    for attempt in ("../secret.mp4", "..%2F..%2Fsecret.mp4", "%2e%2e%2fsecret.mp4"):
        assert _get(f"{http}/media/{attempt}")[0] == 404


def test_a_clip_says_how_much_of_it_is_in_a_cut(api):
    """The Clips view is where you notice footage nothing has used."""
    before = api.clips()[0]
    assert (before["used"], before["poster"]) == (0, "a#00000.00")

    api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    after = api.clips()[0]
    assert after["used"] == 1 and after["segments"] == 2


def test_a_clip_with_no_segments_has_no_poster(api, tmp_path):
    from splice import config
    from splice.schema import Asset, Sidecar

    Sidecar(asset=Asset(file="b.MOV", stem="b", duration=10, fps="30")).save(
        config.sidecar_path("b")
    )
    clip = next(c for c in api.clips() if c["stem"] == "b")
    assert clip["poster"] is None and clip["used"] == 0


# --- rendering a cut -------------------------------------------------------


def _wait(predicate, seconds=5.0):
    import time

    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def rendering(api, monkeypatch, tmp_path):
    """A render that writes a file without an encoder anywhere near it."""
    from splice import render

    def fake_run(args, on_progress=None):
        if on_progress:
            on_progress(1.0)
        Path(args[-1]).write_bytes(b"rendered")

    monkeypatch.setattr(render, "_run", fake_run)
    return api


def test_rendering_a_cut_writes_a_file_and_records_it(rendering):
    from splice import reel as store

    cut = rendering.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    started = rendering.render_cut(cut["slug"])
    assert started["state"] == "running"

    assert _wait(lambda: rendering.render_state(cut["slug"])["state"] == "done")
    state = rendering.render_state(cut["slug"])
    assert state["exists"] and not state["stale"]
    assert store.load(cut["slug"]).rendered_at


def test_an_empty_cut_will_not_render(rendering):
    cut = rendering.create_cut({"title": "Nothing"})
    with pytest.raises(ValueError):
        rendering.render_cut(cut["slug"])


def test_a_failed_render_is_reported_rather_than_lost_on_a_thread(api, monkeypatch):
    from splice import render

    def explode(args, on_progress=None):
        raise render.RenderError("ffmpeg failed: no such file")

    monkeypatch.setattr(render, "_run", explode)
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    api.render_cut(cut["slug"])
    assert _wait(lambda: api.render_state(cut["slug"])["state"] == "failed")
    assert "no such file" in api.render_state(cut["slug"])["error"]


def test_only_one_render_runs_at_a_time(api, monkeypatch):
    """Two encodes at once is not twice the work, it is both at half speed."""
    import threading

    from splice import render

    release = threading.Event()
    monkeypatch.setattr(render, "_run",
                        lambda args, on_progress=None: release.wait(5))

    first = api.create_cut({"title": "One", "segments": ["a#00000.00"]})
    second = api.create_cut({"title": "Two", "segments": ["a#00030.00"]})
    api.render_cut(first["slug"])
    try:
        with pytest.raises(ValueError, match=first["slug"]):
            api.render_cut(second["slug"])
    finally:
        release.set()


def test_asking_again_for_the_one_running_is_not_an_error(api, monkeypatch):
    import threading

    from splice import render

    release = threading.Event()
    monkeypatch.setattr(render, "_run",
                        lambda args, on_progress=None: release.wait(5))
    cut = api.create_cut({"title": "One", "segments": ["a#00000.00"]})
    api.render_cut(cut["slug"])
    try:
        assert api.render_cut(cut["slug"])["state"] == "running"
    finally:
        release.set()


def test_a_cut_with_no_render_says_so_rather_than_404(api):
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    state = api.render_state(cut["slug"])
    assert state["state"] == "none" and not state["exists"]


def test_a_render_goes_stale_when_the_cut_moves(rendering):
    cut = rendering.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    rendering.render_cut(cut["slug"])
    assert _wait(lambda: rendering.render_state(cut["slug"])["state"] == "done")

    shots = rendering.cut(cut["slug"])["shots"]
    shots[0]["end"] = 12.0
    rendering.update_cut(cut["slug"], {"shots": shots})
    assert rendering.render_state(cut["slug"])["stale"]


def test_captions_come_back_as_webvtt_rebased_onto_the_cut(api):
    """Rebased: the line is 0-2s into the clip, and the shot starts at 10s."""
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    body = api.captions(cut["slug"])
    assert body.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.000" in body
    assert "Is he married?" in body
    assert "," not in body.splitlines()[2]


def test_a_rendered_cut_is_served_with_ranges_and_nothing_else_is(http, api, tmp_path):
    from splice import render

    out = render.out_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / "seen.mp4").write_bytes(bytes(range(256)))
    assert _get(f"{http}/render/seen.mp4")[0] == 200
    assert _get(f"{http}/render/nope.mp4")[0] == 404
    (tmp_path / "secret.mp4").write_bytes(b"private")
    assert _get(f"{http}/render/..%2F..%2Fsecret.mp4")[0] == 404


def test_snapping_a_cut_moves_it_onto_speech_and_keeps_the_old_one(api, monkeypatch):
    from splice import trash

    monkeypatch.setattr(
        "splice.speech.runs_for", lambda stem, cache=None: [[0.5, 29.5]]
    )
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    result = api.snap_cut(cut["slug"])

    assert result["moved"] == 1
    assert result["shots"][0]["start"] == pytest.approx(0.42)
    assert [e for e in trash.entries() if e.original.startswith("marriage")]


def test_snapping_a_cut_that_is_already_on_speech_changes_nothing(api, monkeypatch):
    monkeypatch.setattr("splice.speech.runs_for", lambda stem, cache=None: [])
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    assert api.snap_cut(cut["slug"])["moved"] == 0


def test_a_line_can_go_into_a_cut_trimmed_to_itself(api, monkeypatch):
    """A quote, not the two minutes it sits inside."""
    monkeypatch.setattr("splice.speech.runs_for", lambda stem, cache=None: [])
    cut = api.create_cut({"title": "Money"})
    result = api.add_line_to_cut(cut["slug"], {
        "stem": "a", "segment_id": "a#00000.00",
        "start": 10.0, "end": 12.0, "en": "Is he married?",
    })
    shot, = result["shots"]
    assert (shot["start"], shot["end"]) == (9.6, 12.4)
    # The sentence names the shot, in the running order and in Final Cut.
    assert shot["note"] == "Is he married?"
    assert shot["segment_id"] == "a#00000.00"


def test_a_line_is_snapped_to_where_it_actually_starts(api, monkeypatch):
    monkeypatch.setattr(
        "splice.speech.runs_for", lambda stem, cache=None: [[9.9, 12.8]]
    )
    cut = api.create_cut({"title": "Money"})
    shot, = api.add_line_to_cut(cut["slug"], {
        "stem": "a", "segment_id": "a#00000.00",
        "start": 10.0, "end": 12.0, "en": "Is he married?",
    })["shots"]
    assert shot["start"] == pytest.approx(9.82)
    assert shot["end"] == 12.8


def test_lines_need_something_to_search_for(api):
    assert api.lines("  ") == []


# --- keeping an evening of corrections -------------------------------------


def test_a_write_starts_the_clock_on_a_backup(api, monkeypatch):
    """`backup` was a button you had to remember, and a corrected transcript
    is the least regenerable thing in the archive."""
    from splice.server import Autosave

    api.autosave = Autosave(after=0.01, on_message=lambda m: None)
    ran = []
    monkeypatch.setattr("splice.backup.backup", lambda: ran.append(True) or (1, 0, 0))

    api.update_segment("a#00000.00", {"summary": "corrected"})
    assert _wait(lambda: ran, 2.0)


def test_a_burst_of_edits_is_one_backup(monkeypatch):
    """A review pass writes a rating every two seconds; copying sixty files
    each time would be silly."""
    from splice.server import Autosave

    ran = []
    monkeypatch.setattr("splice.backup.backup", lambda: ran.append(True) or (1, 0, 0))
    saver = Autosave(after=0.15, on_message=lambda m: None)
    for _ in range(5):
        saver.touch()
    saver.stop()
    assert len(ran) == 1


def test_nothing_to_save_is_not_a_backup(monkeypatch):
    from splice.server import Autosave

    ran = []
    monkeypatch.setattr("splice.backup.backup", lambda: ran.append(True) or (0, 0, 0))
    Autosave(after=10, on_message=lambda m: None).stop()
    assert ran == []


def test_a_refused_backup_is_said_out_loud_and_not_raised(monkeypatch):
    """A safety net that brings the server down when the media root is
    unmounted is worse than none."""
    from splice import backup as store
    from splice.server import Autosave

    def refuse():
        raise store.Refused("index/ does not exist")

    monkeypatch.setattr("splice.backup.backup", refuse)
    said = []
    saver = Autosave(after=0.01, on_message=said.append)
    saver.touch()
    saver.stop()
    assert any("not backed up" in m for m in said)


def test_autosave_can_be_turned_off(monkeypatch):
    from splice.server import Autosave

    ran = []
    monkeypatch.setattr("splice.backup.backup", lambda: ran.append(True) or (1, 0, 0))
    saver = Autosave(after=0, on_message=lambda m: None)
    saver.touch()
    saver.stop()
    assert ran == []


def test_a_cut_keeps_its_own_notes_alongside_the_critique(api):
    """They were both called `notes`, and the critique won -- so the browser's
    Notes box held "[object Object]" and Save would have written it back."""
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    api.update_cut(cut["slug"], {"notes": "open on the laugh"})

    payload = api.cut(cut["slug"])
    assert payload["notes"] == "open on the laugh"
    assert isinstance(payload["observations"], list)
    assert all(set(n) == {"shot", "text"} for n in payload["observations"])


def test_a_render_records_which_captions_it_has(rendering):
    """The two renders overwrite each other, and a subtitle track laid over a
    video with the captions already drawn on shows both."""
    cut = rendering.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    rendering.render_cut(cut["slug"], {"captions": "none"})
    assert _wait(lambda: rendering.render_state(cut["slug"])["state"] == "done")
    assert rendering.render_state(cut["slug"])["rendered_captions"] == "none"


def test_a_shot_offers_the_whole_moment_and_says_what_the_trim_holds(api):
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    api.update_cut(cut["slug"], {"shots": [
        {"stem": "a", "start": 5.0, "end": 20.0, "segment_id": "a#00000.00"},
    ]})
    shot, = api.cut(cut["slug"])["shots"]
    assert [line["inside"] for line in shot["lines"]] == [False]
    assert shot["lines"][0]["en"] == "Is he married?"


def test_a_cut_can_be_copied_to_try_a_shorter_one(api):
    """Finding the 100 seconds means throwing eight things away and wanting
    two of them back."""
    cut = api.create_cut({"title": "Money", "segments": ["a#00000.00", "a#00030.00"]})
    api.update_cut(cut["slug"], {"hook": "seven rupees", "notes": "open on the laugh"})

    copy = api.duplicate_cut(cut["slug"], {"title": "Money, shorter"})
    assert copy["slug"] == "money-shorter" and copy["slug"] != cut["slug"]
    assert len(copy["shots"]) == 2
    assert (copy["hook"], copy["notes"]) == ("seven rupees", "open on the laugh")

    # And the two are separate from then on.
    api.update_cut(copy["slug"], {"shots": copy["shots"][:1]})
    assert len(api.cut(cut["slug"])["shots"]) == 2


def test_a_cut_carries_the_frame_it_opens_on(api):
    """What a cut looks like in a list of them."""
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00030.00", "a#00000.00"]})
    assert api.cut(cut["slug"])["poster"] == "a#00030.00"


def test_a_cut_with_nothing_in_it_has_no_frame(api):
    assert api.create_cut({"title": "Empty"})["poster"] is None


# --- correcting a line -----------------------------------------------------


def test_a_corrected_line_is_saved_and_the_prose_follows_it(api):
    """The critique's advice for an unreadable caption is "a shorter
    translation", and a caption is made of lines, not of the segment's prose."""
    api.update_segment("a#00000.00", {"lines": [
        {"start": 0, "end": 2, "kok": "Lagn zalato?", "en": "Married?"},
    ]})
    segment = next(s for s in api.clip("a")["segments"] if s["id"] == "a#00000.00")
    assert segment["lines"][0]["en"] == "Married?"
    assert segment["english"] == "Married?"


def test_prose_edited_in_the_same_save_wins(api):
    api.update_segment("a#00000.00", {
        "english": "Something else entirely.",
        "lines": [{"start": 0, "end": 2, "kok": "", "en": "Married?"}],
    })
    segment = next(s for s in api.clip("a")["segments"] if s["id"] == "a#00000.00")
    assert segment["english"] == "Something else entirely."
    assert segment["lines"][0]["en"] == "Married?"


def test_prose_that_had_already_diverged_is_left_alone(api):
    api.update_segment("a#00000.00", {"english": "A summary of sorts."})
    api.update_segment("a#00000.00", {"lines": [
        {"start": 0, "end": 2, "kok": "", "en": "Married?"},
    ]})
    segment = next(s for s in api.clip("a")["segments"] if s["id"] == "a#00000.00")
    assert segment["english"] == "A summary of sorts."


def test_correcting_a_line_keeps_a_copy_of_the_sidecar(api):
    from splice import trash

    api.update_segment("a#00000.00", {"lines": [
        {"start": 0, "end": 2, "kok": "", "en": "Married?"},
    ]})
    assert [e for e in trash.entries() if e.original.startswith("a.seg")]


def test_a_malformed_line_is_refused_before_anything_is_written(api):
    with pytest.raises(Exception):
        api.update_segment("a#00000.00", {"lines": [{"en": "no times"}]})
    segment = next(s for s in api.clip("a")["segments"] if s["id"] == "a#00000.00")
    assert segment["lines"][0]["en"] == "Is he married?"


def test_the_segment_list_is_not_quietly_capped_at_a_page(api):
    """A review pass reviews what this returns, so a cap quietly reached is
    moments nobody ever looks at."""
    assert api.LIMIT >= 2000
    assert len(api.segments()) == 2


def test_a_caller_can_still_ask_for_fewer(api):
    assert len(api.segments(limit=1)) == 1


def test_a_line_search_returns_what_was_asked_for(api):
    assert len(api.lines("married", limit=1)) == 1
    assert api.lines("married", limit=0) == api.lines("married", limit=1)


# --- the routes the page actually calls ------------------------------------


PAGE = Path(__file__).resolve().parents[1] / "src" / "splice" / "web" / "index.html"


def _page_routes() -> set[str]:
    """Every path literal the browser builds a request from."""
    import re

    text = PAGE.read_text()
    out = set(re.findall(r"'(/(?:api|thumb|media|render|captions)/[a-z/]*)", text))
    out |= {
        "/api/cut/" + tail
        for tail in re.findall(r"'/api/cut/' \+ encodeURIComponent\(\w+(?:\.\w+)?\) \+ '/(\w+)'", text)
    }
    return {r.rstrip("?") for r in out}


def test_every_route_the_page_calls_is_one_the_server_answers(http, api):
    """A renamed endpoint breaks the UI and nothing else, which is the kind of
    break you find by opening the page rather than by running the tests."""
    cut = api.create_cut({"title": "Marriage", "segments": ["a#00000.00"]})
    concrete = {
        "/api/overview": "/api/overview",
        "/api/plans/seed": "/api/plans/seed",
        "/api/plans": "/api/plans",
        "/api/plans/import": None,                   # POST only
        "/api/plan/": None,                          # PATCH and DELETE only
        "/api/states": "/api/states",
        "/api/schedule": "/api/schedule",
        "/api/clips": "/api/clips",
        "/api/clip/": "/api/clip/a",
        "/api/segments": "/api/segments",
        "/api/segment/": None,                       # PATCH and DELETE only
        "/api/texts": "/api/texts",
        "/api/tags": "/api/tags",
        "/api/reels": "/api/reels",
        "/api/drafts": "/api/drafts",
        "/api/drafts/generate": None,
        "/api/drafts/generation": "/api/drafts/generation",
        "/api/draft/": "/api/draft/" + api.create_draft({"title": "Draft"})["id"],
        "/api/cuts": "/api/cuts",
        "/api/cut/": f"/api/cut/{cut['slug']}",
        "/api/lines": "/api/lines?q=married",
        "/api/related/": "/api/related/a%2300000.00",
        "/api/using/": "/api/using/a%2300000.00",
        "/api/backup": None,                         # POST only
        "/api/commands": "/api/commands",
        "/api/health": "/api/health",
        "/api/captions": None,                       # POST only
        "/api/cut/export": None,
        "/api/cut/shots": None,
        "/api/cut/line": None,
        "/api/cut/snap": None,
        "/api/cut/duplicate": None,
        "/api/cut/render": f"/api/cut/{cut['slug']}/render",
        "/thumb/": "/thumb/a%2300000.00",
        "/media/": "/media/a.mp4",
        "/render/": None,                            # nothing rendered here
        "/captions/": f"/captions/{cut['slug']}.vtt",
    }
    missing = _page_routes() - set(concrete)
    assert not missing, f"the page calls {sorted(missing)} and this test does not know it"

    # A route that exists and has nothing to give -- no poster frame for a
    # fake proxy -- says what is missing. The router's own fallback says "not
    # found", and that is the answer this is looking for.
    for route, url in concrete.items():
        if url is None:
            continue
        status, _, body = _get(http + url)
        assert b'"error": "not found"' not in body, (
            f"{route}: nothing answers {url}"
        )


def test_the_overview_counts_what_has_been_judged(api):
    """The number everything else waits on, on every screen rather than one."""
    assert api.overview()["judged"] == 0
    api.update_segment("a#00000.00", {"rating": "favorite"})
    assert api.overview()["judged"] == 1


# --- captions for a cut assembled in Final Cut ------------------------------


def _fcpxml(spine: str) -> str:
    """A project the way Final Cut exports one, around whatever spine is given."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
      <fcpxml version="1.11">
        <resources>
          <format id="r1" frameDuration="100/3000s" width="1080" height="1920"/>
          <asset id="r2" name="a" start="0s" duration="3600/30s" hasVideo="1"
                 hasAudio="1" audioSources="1" audioChannels="2">
            <media-rep kind="original-media" src="file:///raw/a.MOV"/>
          </asset>
          <asset id="r3" name="score" start="0s" duration="3600/30s" hasAudio="1">
            <media-rep kind="original-media" src="file:///sounds/score.mp3"/>
          </asset>
        </resources>
        <library><event name="E"><project name="Ep1">
          <sequence format="r1" tcStart="0s" duration="300/30s">
            <spine>{spine}</spine>
          </sequence>
        </project></event></library>
      </fcpxml>"""


def test_a_final_cut_timeline_is_captioned_from_the_document_it_is_given(api):
    """The browser is handed a dropped file, which has a name and no directory,
    so the document travels in the request rather than as a path."""
    result = api.caption_timeline({"xml": _fcpxml(
        '<asset-clip ref="r2" offset="60/30s" name="a" start="0s" duration="120/30s"/>'
    ), "lang": "en"})

    assert result["project"] == "Ep1"
    assert [c["text"] for c in result["captions"]] == ["Is he married?"]
    # The shot sits two seconds in, and so does what is said in it.
    assert result["captions"][0]["start"] == pytest.approx(2.0)
    assert "00:00:02,000 --> " in result["srt"]


def test_the_music_under_a_cut_is_named_rather_than_treated_as_a_fault(api):
    """Every finished cut has something in it the archive has never seen. That
    is what a score is, and it must not read as an error."""
    result = api.caption_timeline({"xml": _fcpxml(
        '<asset-clip ref="r2" offset="0s" name="a" start="0s" duration="120/30s"/>'
        '<asset-clip ref="r3" lane="-2" offset="0s" name="score" start="0s"'
        ' duration="300/30s"/>'
    )})

    assert result["skipped"] == ["score"]
    assert result["problems"] == []
    assert len(result["captions"]) == 1


def test_an_empty_drop_says_what_was_expected(api):
    """Dropping the wrong thing is the likeliest way to arrive here."""
    with pytest.raises(ValueError, match="fcpxml"):
        api.caption_timeline({"xml": "  "})


def test_the_panel_gets_one_srt_a_voice_as_well_as_the_whole(api):
    """An .srt has nowhere to put a speaker, so an editor that imports one can
    colour a track and not a line. The split is how the separation survives the
    trip out, and it has to hold every caption exactly once."""
    result = api.caption_timeline({"xml": _fcpxml(
        '<asset-clip ref="r2" offset="0s" name="a" start="0s" duration="120/30s"/>'
    )})

    assert set(result["by_speaker"]) == {"unattributed"}
    assert sum(body.count(" --> ") for body in result["by_speaker"].values()) \
        == result["srt"].count(" --> ")


def test_correcting_a_caption_does_not_un_attribute_it(api):
    """The dialog edits the English and the romanised Konkani, and rebuilt each
    line from exactly those two fields -- so fixing a typo silently dropped the
    speaker, and with it the colour that tells two people apart. The same bug
    the analysis merge had, in the other direction."""
    from splice import library

    api.update_segment("a#00000.00", {"lines": [
        {"start": 0, "end": 2, "kok": "Lagn zalato?", "en": "Is he married?",
         "speaker": "grandma"},
    ]})
    api.update_segment("a#00000.00", {"lines": [
        {"start": 0, "end": 2, "kok": "Lagn zalato?", "en": "Is he married then?",
         "speaker": "grandma"},
    ]})

    line = library.load_sidecar("a").segments[0].lines[0]
    assert (line.en, line.speaker) == ("Is he married then?", "grandma")


def test_the_browser_is_told_who_a_line_may_be_attributed_to(api):
    """One vocabulary, so the dialog, the analysis merge and the caption
    colours cannot drift apart from each other."""
    assert api.overview()["speakers"] == ["grandma", "grandpa", "nikhil", "relative"]


def test_a_corrected_caption_reaches_the_prose_that_search_reads(api):
    """The model writes the prose and the lines separately and joins them with
    whichever separator it felt like -- 19 segments here are newline-joined and
    76 space-joined. Comparing only against newlines meant a corrected caption
    reached the prose in 12% of segments, and the docstring claiming otherwise
    was the only place it reliably happened."""
    from splice import library

    api.update_segment("a#00000.00", {"english": "Is he married?"})  # space-join of one
    api.update_segment("a#00000.00", {"lines": [
        {"start": 0, "end": 2, "kok": "Lagn zalato?", "en": "Is he married then?"},
    ]})

    assert library.load_sidecar("a").segments[0].english == "Is he married then?"


def test_prose_edited_by_hand_in_the_same_save_wins(api):
    """Typing in the prose box and correcting a line at once is a person
    saying which one they meant."""
    from splice import library

    api.update_segment("a#00000.00", {
        "english": "Something else entirely",
        "lines": [{"start": 0, "end": 2, "kok": "x", "en": "Is he married then?"}],
    })

    assert library.load_sidecar("a").segments[0].english == "Something else entirely"


# --- sync ---------------------------------------------------------------
#
# The commands themselves are stood in for throughout: what is worth testing
# here is what the browser is allowed to do while one is running, not agy.


def _job(api):
    return api.command_menu()["job"] or {}


@contextlib.contextmanager
def _command_that_waits(api, name, attr, check=False):
    """A command held open at its slowest point, which is where the questions
    are: what a save does now, and what a second click does."""
    from splice import cli

    started, release, calls = threading.Event(), threading.Event(), []

    def slow(**kw):
        calls.append(kw)
        started.set()
        assert release.wait(5), "the command was never let go of"

    real = getattr(cli, attr)
    setattr(cli, attr, slow)
    try:
        api.run_command({"name": name, "check": check})
        assert started.wait(5), "the command never got going"
        yield calls
    finally:
        release.set()
        assert _wait(lambda: _job(api).get("state") != "running")
        setattr(cli, attr, real)


def test_the_sync_button_checks_before_it_spends_an_hour(api, monkeypatch):
    """The first click is `sync --dry-run`. A real run is hours of agy and a
    set of renames; the check costs seconds and changes nothing, so what it
    will cost is on screen before it is spent."""
    calls = []
    monkeypatch.setattr("splice.cli.sync", lambda **kw: calls.append(kw))

    api.run_command({"name": "sync", "check": True})

    assert _wait(lambda: _job(api)["state"] == "done")
    assert calls == [{"jobs": config.DEFAULT_JOBS, "rename_new": True,
                      "dry_run": True, "editor": "fcp"}]


def test_a_dry_run_leaves_the_archive_open_for_edits(api):
    """It changes nothing, so refusing edits for its duration would be a lie
    -- and the check is the one you run while still deciding."""
    with _command_that_waits(api, "sync", "sync", check=True):
        api.update_segment("a#00000.00", {"title": "Still editable"})


def test_an_edit_during_a_sync_is_refused_rather_than_written_over(api):
    """`reclassify` inside a sync loads every sidecar up front and writes back
    the ones it changed, so an edit that lands mid-walk is written over. A
    hand-corrected transcript is the one thing here nothing can produce again;
    a sentence saying to wait is a better outcome than a lost evening."""
    from splice import drafts, library

    with _command_that_waits(api, "sync", "sync"):
        with pytest.raises(drafts.Conflict, match="Sync raw/ is running"):
            api.update_segment("a#00000.00", {"title": "Typed mid-sync"})

    assert _wait(lambda: _job(api)["state"] == "done")
    assert library.load_sidecar("a").segments[0].title == "Marriage chat"
    api.update_segment("a#00000.00", {"title": "Editable once it is over"})


def test_a_sync_that_fails_still_hands_the_archive_back(api, monkeypatch):
    """Otherwise the only way to edit anything again is to restart the server,
    and the reason would be a stack trace nobody saw."""
    monkeypatch.setattr("splice.cli.sync", lambda **kw: 1 / 0)

    api.run_command({"name": "sync", "check": False})

    assert _wait(lambda: _job(api)["state"] == "failed")
    assert "ZeroDivisionError" in _job(api)["error"]
    api.update_segment("a#00000.00", {"title": "Editable again"})


def test_what_a_sync_prints_reaches_the_browser(api, monkeypatch, capsys):
    """`sync` says what it is doing through stdout, and a browser shown none
    of it looks like a hang. It is a tee rather than a capture because whoever
    started `splice serve` is watching that terminal too."""
    import typer

    def talk(**kw):
        typer.echo("26 file(s) in raw/")
        typer.echo("nothing new to import")
        print("cut off mid-", end="")   # no newline: a crash, or a prompt

    monkeypatch.setattr("splice.cli.sync", talk)

    api.run_command({"name": "sync", "check": True})

    assert _wait(lambda: _job(api)["state"] == "done")
    assert _job(api)["lines"] == [
        "26 file(s) in raw/", "nothing new to import", "cut off mid-",
    ]
    assert "nothing new to import" in capsys.readouterr().out


def test_opening_the_page_again_joins_the_sync_already_running(api):
    """A sync outlives the tab that started it. A second one over the top of
    the sidecars the first is writing is the accident worth preventing."""
    with _command_that_waits(api, "sync", "sync") as calls:
        joined = api.run_command({"name": "align", "check": True})
        assert joined["state"] == "running"
        assert joined["name"] == "sync"   # the one running, not the one asked for

    assert _wait(lambda: _job(api)["state"] == "done")
    assert len(calls) == 1


def test_a_finished_sync_starts_the_clock_on_a_backup(api, monkeypatch):
    """It has just written sidecars nothing has mirrored, which is exactly
    when the archive is most worth having in git."""
    from splice.server import Autosave

    api.autosave = Autosave(after=0.01, on_message=lambda m: None)
    ran = []
    monkeypatch.setattr("splice.backup.backup", lambda: ran.append(True) or (1, 0, 0))
    monkeypatch.setattr("splice.cli.sync", lambda **kw: None)

    api.run_command({"name": "sync", "check": False})

    assert _wait(lambda: ran, 2.0)


def test_a_dry_run_is_not_a_reason_to_back_anything_up(api, monkeypatch):
    """Nothing changed, so there is nothing to mirror."""
    from splice.server import Autosave

    api.autosave = Autosave(after=0.01, on_message=lambda m: None)
    ran = []
    monkeypatch.setattr("splice.backup.backup", lambda: ran.append(True) or (1, 0, 0))
    monkeypatch.setattr("splice.cli.sync", lambda **kw: None)

    api.run_command({"name": "sync", "check": True})

    assert _wait(lambda: _job(api)["state"] == "done")
    assert not _wait(lambda: ran, 0.5)


def test_a_sync_that_quits_reports_why_and_not_its_exit_code(api, monkeypatch):
    """`sync` says "proxy build failed: ..." and then raises `typer.Exit`,
    whose str() is "1". The line it printed is the answer; the code is not."""
    import typer

    def give_up(**kw):
        typer.echo("! proxy build failed: ffmpeg -y failed: moov atom not found")
        raise typer.Exit(1)

    monkeypatch.setattr("splice.cli.sync", give_up)

    api.run_command({"name": "sync", "check": False})

    assert _wait(lambda: _job(api)["state"] == "failed")
    assert _job(api)["error"] == (
        "! proxy build failed: ffmpeg -y failed: moov atom not found"
    )


# Which `splice` command each menu entry is. Written out rather than derived,
# so that adding one to the registry without saying what it runs fails here.
MENU_REACHES = {
    "sync": "sync",
    "analyse": "analyze",
    "align": "align",
    "incoming": "export_fcpxml_cmd",
    "rename": "rename_cmd",
    "reclassify": "reclassify",
    "exports": "export_all",
    "thumbs": "thumbs",
    "index": "index_cmd",
    "status": "status",
    "doctor": "doctor",
}


def test_the_menu_offers_exactly_the_commands_this_file_knows_about():
    from splice import server

    assert set(server.commands()) == set(MENU_REACHES)


@pytest.mark.parametrize("name", sorted(MENU_REACHES))
def test_every_command_in_the_menu_reaches_the_one_it_names(api, monkeypatch, name):
    """Each entry in the registry is a lambda, and a typo in one is a 500 the
    first time somebody picks it -- months after it was written, and only ever
    in the item nobody uses."""
    from splice import cli

    called = []
    monkeypatch.setattr(cli, MENU_REACHES[name], lambda *a, **kw: called.append(kw))

    api.run_command({"name": name, "check": False})

    assert _wait(lambda: _job(api)["state"] == "done"), _job(api).get("error")
    assert len(called) == 1


def test_a_report_does_not_refuse_edits_while_it_runs(api):
    """`doctor` and `status` read the archive and write nothing, and they are
    the two you run *because* something already looks wrong. Refusing a
    correction for their duration would be a lie."""
    with _command_that_waits(api, "doctor", "doctor"):
        api.update_segment("a#00000.00", {"title": "Still editable"})


def test_a_command_with_no_dry_run_will_not_pretend_to_have_one(api):
    """Checking something that cannot be checked would run the real thing
    under a word that promises it will not."""
    with pytest.raises(ValueError, match="no dry run"):
        api.run_command({"name": "reclassify", "check": True})


def test_an_unknown_command_is_not_found(api):
    with pytest.raises(KeyError):
        api.run_command({"name": "not a command", "check": False})


def test_the_menu_says_which_commands_change_anything(api):
    """The dialog warns that edits are refused only for the ones that do, and
    opens with a dry run only for the ones that have one. Both come from here
    rather than from a second list in the page."""
    menu = {c["name"]: c for c in api.command_menu()["commands"]}

    assert (menu["align"]["holds"], menu["align"]["check"]) == (True, True)
    assert (menu["incoming"]["holds"], menu["incoming"]["check"]) == (True, False)
    assert (menu["doctor"]["holds"], menu["doctor"]["check"]) == (False, False)


def _send(url, method, body):
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_a_plan_is_made_edited_refused_and_deleted_over_http(http):
    """The routes the page saves plans through, including the one answer the
    page acts on by itself: a 409 reloads the plan rather than retrying."""
    status, made = _send(http + "/api/plans", "POST", {"title": "Home", "moments": [{"id": "a#00000.00"}]})
    assert status == 201 and made["stage"] == "idea"

    status, edited = _send(http + f"/api/plan/{made['id']}", "PATCH",
                           {"revision": made["revision"], "stage": "ready"})
    assert status == 200 and edited["stage"] == "ready"

    status, refused = _send(http + f"/api/plan/{made['id']}", "PATCH",
                            {"revision": made["revision"], "title": "Stale"})
    assert status == 409 and "another window" in refused["error"]

    status, _ = _send(http + "/api/plans/import", "POST", {"plans": [{"id": "x"}]})
    assert status == 409

    status, _ = _send(http + f"/api/plan/{made['id']}", "DELETE", {"revision": edited["revision"]})
    assert status == 200
    assert _get(http + "/api/plans")[2] == b'{"exists": true, "plans": [], "stages": ["idea", "gathering", "ready", "shelved"]}'


def test_the_schedule_is_reordered_over_http_and_says_why_it_refuses(http):
    made = [_send(http + "/api/plans", "POST", {"title": t, "stage": "ready"})[1] for t in "ABC"]
    a, b, c = (m["id"] for m in made)
    _, got = _send(http + "/api/schedule", "PUT", {"revision": None, "order": [c, a, b]})
    assert [r["number"] for r in got["reels"]] == [1, 2, 3]
    assert [r["title"] for r in got["reels"]] == ["C", "A", "B"]

    status, refused = _send(http + "/api/schedule", "PUT", {"revision": None, "order": [a, b, c]})
    assert status == 409

    _send(http + f"/api/plan/{a}", "PATCH", {"revision": made[0]["revision"], "posted": True})
    status, refused = _send(http + "/api/schedule", "PUT", {"revision": got["revision"], "order": [a, c, b]})
    assert status == 400 and "keeps its place" in refused["error"]


def test_every_tab_has_an_address_that_serves_the_page(http):
    """A reload on Segments used to land on Plans.

    The tab lived only in the page's memory; it is in the path now, and a
    path the server does not know would be a 404 instead of the page. With
    or without the trailing slash, since that is how people type them.
    """
    from splice.server import PAGES

    _, _, page = _get(http + "/")
    for path in sorted(PAGES):
        for asked in (path, path + "/", path + "?seg=a%23000&tag=x"):
            status, headers, body = _get(http + asked)
            assert (status, body) == (200, page), asked
            assert headers.get("Cache-Control") == "no-store", asked


def test_an_address_that_is_not_a_page_is_still_not_found(http):
    """Only the tabs are served as the page: a mistyped API route loading a
    page that then cannot find its data is a worse error than a 404."""
    for path in ("/segmnets", "/api/segmnets", "/plans/extra"):
        status, _, _ = _get(http + path)
        assert status == 404, path


# --- which editor the page is aimed at ---------------------------------


def test_the_menu_names_the_editor_the_page_is_set_to(api):
    """The Run menu is built from the server, not from a list in the page.

    A row that says "Final Cut import of what is new" to somebody who no
    longer has Final Cut is an instruction for the wrong program, and the page
    cannot correct it without keeping its own copy of the labels.
    """
    resolve = api.command_menu("resolve")
    labels = {c["name"]: c["label"] for c in resolve["commands"]}
    assert labels["incoming"] == "DaVinci Resolve import of what is new"
    assert "incoming.resolve.fcpxml" in next(
        c["blurb"] for c in resolve["commands"] if c["name"] == "incoming")
    assert resolve["editor"] == "resolve"
    assert [e["name"] for e in resolve["editors"]] == ["fcp", "resolve"]

    fcp = api.command_menu(None)
    assert {c["name"]: c["label"] for c in fcp["commands"]}["incoming"] == (
        "Final Cut Pro import of what is new")


def test_exporting_a_cut_says_which_menu_to_use_afterwards(api):
    """Two menu paths that have nothing in common.

    The page used to carry one sentence, and it was Final Cut's.
    """
    from splice import reel as store

    store.Reel(slug="r", title="R",
               shots=[store.Shot(stem="a", start=1.0, end=9.0)]).save()

    fcp = api.export_cut("r")
    assert fcp["import_menu"] == "File > Import > XML"
    assert fcp["captions_on_timeline"] is True
    assert fcp["path"].endswith("r.fcpxml")

    resolve = api.export_cut("r", "resolve")
    assert resolve["import_menu"] == "File > Import > Timeline"
    assert resolve["captions_on_timeline"] is False
    assert resolve["path"].endswith("r.resolve.fcpxml")


def test_the_clips_table_asks_about_one_editor_at_a_time(api):
    """Delivery is per editor, and the column claims a clip has arrived.

    Reading Final Cut's record while the page says Resolve would tell you the
    footage was in a media pool nothing has ever sent it to.
    """
    from splice import config, library
    from splice.schema import ExportRecord

    sidecar = library.load_sidecar("a")
    sidecar.set_delivery("fcp", ExportRecord(
        imported_at="2026-09-01T00:00:00+00:00",
        imported_sha=sidecar.content_sha(), fcp_name="a", fcp_copies=1))
    sidecar.save(config.sidecar_path("a"))

    assert api.clips(None)[0]["exported"]
    assert not api.clips("resolve")[0]["exported"]


def test_a_cut_read_back_is_read_the_same_way_whichever_wrote_it(api):
    """Only the advice afterwards differs, and that is the point.

    Asking which program wrote it would be a question with no good answer --
    the dialects overlap -- and a wrong guess would refuse a readable file.
    """
    from splice import reel as store

    store.Reel(slug="r", title="R",
               shots=[store.Shot(stem="a", start=1.0, end=9.0)]).save()
    written = Path(api.export_cut("r", "resolve")["path"]).read_text()

    for name, menu in (("fcp", "File > Import > Captions"),
                       ("resolve", "Import Subtitle")):
        read = api.caption_timeline({"xml": written, "lang": "en", "editor": name})
        assert read["project"] == "R"
        assert len(read["shots"]) == 1
        assert menu in read["captions_menu"]
