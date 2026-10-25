"""A proposed story must remain grounded and must never replace an edit."""
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from fractions import Fraction
from xml.etree import ElementTree as ET

import pytest

from splice import backup, config, draft_generation as gen, drafts, index, reel, trash
from splice.schema import Asset, Line, Segment, Sidecar
from splice.server import Api, serve


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.setenv("SPLICE_ARCHIVE", str(tmp_path / "archive"))
    config.raw_dir().mkdir(parents=True)
    (config.raw_dir() / "a.MOV").write_bytes(b"footage")
    sc = Sidecar(asset=Asset(file="a.MOV", stem="a", fingerprint="original", duration=180,
                             fps="30", width=1080, height=1920), segments=[
        Segment(id="a#0", start=0, end=130, title="Guessing a router", summary="A question and a funny guess.",
                rating="favorite", lines=[
                    Line(start=2, end=5, en="What do you think it is?", kok="Kitem?", speaker="nikhil"),
                    Line(start=6, end=10, en="A flour machine?", kok="Pitt?", speaker="grandma"),
                    Line(start=110, end=115, en="It brings the internet.", speaker="nikhil"),
                ]),
        Segment(id="a#130", start=130, end=145, title="The blessing", lines=[
            Line(start=132, end=136, en="May it work well.", speaker="grandma")]),
        Segment(id="a#150", start=150, end=170, title="Rejected", rating="rejected", lines=[
            Line(start=152, end=158, en="Do not pick me.")]),
    ])
    sc.save(config.sidecar_path("a"))
    index.build()
    return sc


def beat(start=2, end=10):
    return {"stem": "a", "fingerprint": "original", "start": start, "end": end,
            "role": "Question and guess", "note": "Leave the pause."}


def new(api=None):
    return (api or Api()).create_draft({"title": "The flour machine", "beats": [beat()]})


def test_a_draft_does_not_count_as_used_footage(archive):
    d = new()
    index.build()
    conn = index.connect()
    assert conn.execute("SELECT count(*) FROM reel_shots").fetchone()[0] == 0
    conn.close()
    assert d["beats"][0]["snapshot"][0]["en"] == "What do you think it is?"


def test_stale_edits_and_deletes_cannot_replace_a_newer_draft(archive):
    api = Api()
    d = new(api)
    updated = api.update_draft(d["id"], {"revision": d["revision"], "notes": "Keep the laugh"})
    with pytest.raises(drafts.Conflict):
        api.update_draft(d["id"], {"revision": d["revision"], "notes": "Old copy"})
    with pytest.raises(drafts.Conflict):
        api.delete_draft(d["id"], {"revision": d["revision"]})
    assert drafts.load(d["id"]).notes == "Keep the laugh"
    assert updated["revision"] != d["revision"]


def test_backup_restore_and_trash_keep_the_draft_in_its_own_directory(archive):
    api = Api()
    d = new(api)
    backup.backup()
    assert (backup.archive_root() / "drafts" / f'{d["id"]}.draft.json').exists()
    api.delete_draft(d["id"], {"revision": d["revision"]})
    entry = trash.entries()[0]
    assert entry.destination == drafts.path_for(d["id"])
    trash.restore(entry)
    assert drafts.load(d["id"]).beats[0].start == 2
    drafts.path_for(d["id"]).unlink()
    backup.restore()
    assert drafts.load(d["id"]).title == d["title"]


def test_renames_and_segment_renumbering_do_not_change_an_excerpt(archive):
    d = new()
    archive.asset.stem = "renamed"
    archive.asset.file = "renamed.MOV"
    archive.asset.previous_names = ["a"]
    archive.segments[0].id = "renamed#1"
    (config.raw_dir() / "a.MOV").rename(config.raw_dir() / "renamed.MOV")
    config.sidecar_path("a").unlink()
    archive.save(config.sidecar_path("renamed"))
    after = Api().draft(d["id"])["beats"][0]
    assert (after["stem"], after["start"], after["end"]) == ("renamed", 2, 10)
    assert after["warnings"] == []
    assert after["segment_id"] == "renamed#1"


def test_corrected_dialogue_is_flagged_and_notes_do_not_erase_the_evidence(archive):
    api = Api()
    d = new(api)
    archive.segments[0].lines[0].en = "What is this thing?"
    archive.save(config.sidecar_path("a"))
    after = api.draft(d["id"])
    assert "changed" in after["beats"][0]["warnings"][0]
    assert after["beats"][0]["lines"][0]["en"] == "What is this thing?"
    after["beats"][0]["note"] = "New note"
    saved = api.update_draft(d["id"], {"revision": d["revision"], "beats": after["beats"]})
    assert saved["beats"][0]["snapshot"][0]["en"] == "What do you think it is?"
    assert saved["beats"][0]["warnings"]


def test_missing_footage_stays_in_the_draft_but_prevents_incomplete_export(archive):
    d = new()
    (config.raw_dir() / "a.MOV").unlink()
    assert Api().draft(d["id"])["beats"][0]["problem"]
    with pytest.raises(ValueError, match="incomplete"):
        drafts.snapshot_cut(drafts.load(d["id"]))
    assert len(drafts.load(d["id"]).beats) == 1
    assert reel.load_all() == []


def test_a_replacement_with_the_same_filename_does_not_steal_the_excerpt(archive):
    d = new()
    archive.asset.fingerprint = "replacement"
    archive.save(config.sidecar_path("a"))
    assert "replaced" in Api().draft(d["id"])["beats"][0]["problem"]


@pytest.mark.parametrize("values", [(20, 10), (-1, 10), (1, 181), (2, float('nan')), (150, 159)])
def test_bad_or_rejected_manual_ranges_are_not_saved(archive, values):
    with pytest.raises(ValueError):
        Api().create_draft({"title": "Bad", "beats": [beat(*values)]})
    assert drafts.load_all() == []


def test_context_includes_the_neighbouring_moment_without_selecting_it(archive):
    d = new()
    context = d["beats"][0]["context"]
    assert any(l["en"] == "May it work well." for l in context)
    assert all(l["en"] != "May it work well." for l in d["beats"][0]["lines"])


def scripted_model(monkeypatch, transform=lambda x: x):
    calls = []
    def ask(instruction, data, schema, key):
        calls.append((key, data))
        if key == "stories":
            return gen.Discovery(stories=[gen.Story(title="A flour machine?", angle="A surprising guess",
                                                    segments=["a#0", "a#130"])])
        payload = {"scripts": [{"title": "A flour machine?", "angle": "A surprising guess", "beats": [
            {"segment_id": "a#0", "first_line": 0, "last_line": 1, "role": "Setup and guess",
             "alternatives": [{"segment_id": "a#130", "first_line": 0, "last_line": 0}]}]}]}
        return gen.Scripts.model_validate(transform(payload))
    monkeypatch.setattr(gen, "ask", ask)
    return calls


def test_generation_can_use_a_short_excerpt_of_a_long_single_clip(archive, monkeypatch):
    calls = scripted_model(monkeypatch)
    found = gen.generate("Keep the question")
    assert len(found) == 1
    assert (found[0].beats[0].start, found[0].beats[0].end) == (2, 10)
    assert found[0].beats[0].snapshot == archive.segments[0].lines[:2]
    assert found[0].beats[0].alternatives[0].start == 132
    assert not drafts.load_all()  # generating alone never saves over anything
    rows = calls[0][1]["moments"]
    assert {r["id"] for r in rows} == {"a#0", "a#130"}
    assert rows[0]["favourite"]


@pytest.mark.parametrize("mutation", [
    {"segment_id": "invented"}, {"first_line": 99}, {"first_line": 2, "last_line": 0},
    {"quote": "Invented words"}, {"start": 12}, {"segment_id": "a#150"},
])
def test_model_inventions_are_refused_before_any_draft_is_saved(archive, monkeypatch, mutation):
    def transform(payload):
        payload["scripts"][0]["beats"][0].update(mutation)
        return payload
    scripted_model(monkeypatch, transform)
    with pytest.raises(ValueError):
        gen.generate()
    assert drafts.load_all() == []


def test_a_model_cannot_select_dialogue_it_was_not_shown(archive, monkeypatch):
    scripted_model(monkeypatch)
    with pytest.raises(ValueError, match="not given"):
        gen.generate(selected=["a#0"])


def test_no_dialogue_fails_without_calling_the_model(archive, monkeypatch):
    monkeypatch.setattr(gen, "ask", lambda *a: pytest.fail("model should not be called"))
    with pytest.raises(ValueError, match="rejected"):
        gen.generate(selected=["a#150"])
    archive.segments = []
    archive.save(config.sidecar_path("a"))
    with pytest.raises(ValueError, match="No eligible"):
        gen.generate()


def wait_job(api):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = api.draft_jobs.status()
        if job["state"] != "running":
            return job
        time.sleep(.01)
    pytest.fail("generation did not finish")


def test_regeneration_creates_variants_and_never_overwrites_an_edit(archive, monkeypatch):
    scripted_model(monkeypatch)
    api = Api()
    api.generate_drafts({})
    first = wait_job(api)
    d = api.draft(first["drafts"][0])
    api.update_draft(d["id"], {"revision": d["revision"], "notes": "My personal edit"})
    api.generate_drafts({})
    second = wait_job(api)
    assert first["drafts"] != second["drafts"]
    assert drafts.load(d["id"]).notes == "My personal edit"


def test_failed_generation_is_visible_and_can_be_retried(archive, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("quota exhausted")
    monkeypatch.setattr(gen, "generate", fail)
    api = Api()
    api.generate_drafts({"brief": "Funny"})
    job = wait_job(api)
    assert (job["state"], job["error"]) == ("failed", "quota exhausted")
    monkeypatch.setattr(gen, "generate", lambda *a, **k: [drafts.edited({"title": "Retry", "beats": [beat()]})])
    api.generate_drafts(job["request"])
    assert wait_job(api)["state"] == "done"


def test_discovery_batches_fit_the_prompt_budget(archive, monkeypatch):
    monkeypatch.setattr(config, "DRAFT_PROMPT_CHARS", 12000)
    rows = [{"summary": "x" * 700} for _ in range(20)]
    groups = list(gen.batches(rows))
    assert len(groups) > 1
    assert sum(len(g) for g in groups) == len(rows)
    assert all(len(gen.encoded(g).encode()) < config.DRAFT_PROMPT_CHARS for g in groups)


def test_drafting_uses_inline_schemas_and_never_grants_tool_permissions(archive, monkeypatch):
    calls = []
    def run(prompt, path, **kwargs):
        calls.append(kwargs)
        assert "$ref" not in path.read_text()
        assert "DATA:" in prompt
        return {"stories": [], "reason": "No story"}
    monkeypatch.setattr(gen.agy, "run_structured", run)
    assert not gen.ask("Discover", {}, gen.Discovery, "stories").stories
    assert calls[0]["allow_tools"] is False
    assert calls[0]["model"] == config.DRAFT_MODEL


def test_export_snapshots_keep_order_trims_and_rebased_captions(archive, monkeypatch):
    monkeypatch.setattr("splice.export_fcpxml.validate", lambda *a, **k: None)
    monkeypatch.setattr("splice.loudness.gains_for", lambda *a, **k: {})
    api = Api()
    d = api.create_draft({"title": "Guess", "beats": [beat(132, 136), beat(2, 10)]})
    one = api.export_draft(d["id"], {"revision": d["revision"]})
    two = api.export_draft(d["id"], {"revision": d["revision"]})
    assert one["cut"] != two["cut"]
    saved = reel.load(one["cut"])
    assert [(s.start, s.end) for s in saved.shots] == [(132, 136), (2, 10)]
    xml = ET.parse(one["path"])
    spine = xml.find(".//spine")
    assert [float(Fraction(c.get("offset")[:-1])) for c in spine] == [0, 4]
    srt = (config.export_dir() / "reels" / f'{one["cut"]}.srt').read_text()
    assert srt.index("May it work well.") < srt.index("What do you think it is?")
    assert "00:00:04,000" in srt
    assert drafts.load(d["id"]).revision == d["revision"]


def test_http_routes_expose_conflicts_and_fresh_export_snapshots(archive, monkeypatch):
    monkeypatch.setattr("splice.export_fcpxml.validate", lambda *a, **k: None)
    monkeypatch.setattr("splice.loudness.gains_for", lambda *a, **k: {})
    server = serve(port=0, autosave=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def request(path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as response:
            return json.load(response)
    try:
        d = request("/api/drafts", "POST", {"title": "Draft", "beats": [beat()]})
        assert request("/api/drafts")[0]["id"] == d["id"]
        path = "/api/draft/" + d["id"]
        updated = request(path, "PATCH", {"revision": d["revision"], "notes": "Keep"})
        with pytest.raises(urllib.error.HTTPError) as error:
            request(path, "PATCH", {"revision": d["revision"], "notes": "Stale"})
        assert error.value.code == 409
        assert request(path + "/export", "POST", {"revision": updated["revision"]})["cut"]
        assert request(path)["notes"] == "Keep"
        request(path, "DELETE", {"revision": updated["revision"]})
        assert request("/api/drafts") == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def one_at_a_time(monkeypatch):
    """Two eligible moments in one batch each, which is what 192 of them do."""
    monkeypatch.setattr(gen, "batches", lambda rows, overhead=8000: ([r] for r in rows))


def test_a_reducer_that_declines_to_choose_does_not_empty_the_archive(archive, monkeypatch):
    """Discovery reads the library in batches, so a productive run comes back
    with more than three candidates and has to be reduced. A reducer answering
    "these all cover the same ground" was emptying the list, and the filmmaker
    was told there was no story in an archive that had just offered six."""
    one_at_a_time(monkeypatch)

    def ask(instruction, data, schema, key):
        if "Find up to three" in instruction:
            return gen.Discovery(stories=[gen.Story(title=f"Story {i}", angle="an angle",
                                                    segments=[data["moments"][0]["id"]])
                                          for i in range(3)])
        if "Select up to three strongest" in instruction:
            return gen.Discovery(stories=[], reason="These proposals all cover the same ground.")
        sid = data["story"]["segments"][0]
        return gen.Scripts(scripts=[gen.Script(title="Kept", angle="an angle", beats=[
            gen.Pick(segment_id=sid, first_line=0, last_line=0, role="The guess")])])
    monkeypatch.setattr(gen, "ask", ask)

    found = gen.generate()
    assert [d.title for d in found] == ["Kept"] * 3


def test_an_empty_discovery_repeats_the_reason_it_gave(archive, monkeypatch):
    """"Try a broader brief" is not a finding. When the model explains why it
    found nothing, that explanation is the only thing that tells the filmmaker
    whether to change the brief or go and tag some more footage."""
    monkeypatch.setattr(gen, "ask", lambda *a, **k: gen.Discovery(
        stories=[], reason="Every moment is B-roll with no spoken exchange."))
    with pytest.raises(ValueError, match="B-roll with no spoken exchange"):
        gen.generate("something about trains")


@pytest.mark.skipif(sys.platform != "darwin", reason="argv limits are per-kernel")
def test_the_drafting_budget_still_fits_on_one_command_line():
    """agy takes the prompt as a single argv string, so the ceiling on a draft
    prompt is ARG_MAX and not the model's context. Measured rather than
    asserted, because the number that matters is what execve actually accepts:
    this machine takes 1,043,707 bytes in one argument. Linux caps a single
    argument at 128 KB regardless of ARG_MAX, so this budget is macOS's."""
    prompt = "x" * config.DRAFT_PROMPT_CHARS
    done = subprocess.run(["/usr/bin/python3", "-c",
                           "import sys; print(len(sys.argv[1]))", prompt],
                          capture_output=True, text=True)
    assert done.stdout.strip() == str(config.DRAFT_PROMPT_CHARS)
