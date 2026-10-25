"""Plans as files, and the segment state derived from them.

A plan became a record the day a reel could be posted, so what is tested here
is what a record needs: that an old copy cannot be saved over a newer one,
that the trash keeps what it replaced without keeping every keystroke, and
that re-analysis renumbering the segments does not quietly lose its moments.
The states are the other half -- one derivation, and every badge in the
browser and the line in `splice status` reads it. The schedule is the third:
the order the ready ones go out in, and a posted reel keeping its place.
"""

from datetime import timedelta

import pytest

from splice import backup, config, index, plans, reel, trash
from splice.drafts import Conflict
from splice.schema import Asset, Segment, Sidecar
from splice.server import Api


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.setenv("SPLICE_ARCHIVE", str(tmp_path / "archive"))
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=120, fps="30"),
        segments=[
            Segment(id="a#00000.00", start=0, end=30, title="Marriage chat"),
            Segment(id="a#00030.00", start=30, end=60, title="Coconut scraper"),
        ],
    ).save(config.sidecar_path("a"))
    index.build()
    return Api()


def _plan(**fields) -> plans.Plan:
    return plans.Plan.model_validate({"title": "A plan", **fields})


def _seg(sid, rating="none", status="indexed"):
    return {"id": sid, "rating": rating, "status": status}


# --- the file ---------------------------------------------------------------

def test_a_plan_saved_over_a_newer_revision_is_refused(archive):
    """Two tabs, one plan. The second save is holding the first tab's copy of
    every field it did not touch, and writing it would undo the first."""
    made = archive.create_plan({"title": "Home", "moments": [{"id": "a#00000.00"}]})
    first = archive.update_plan(made["id"], {"revision": made["revision"], "desc": "Arriving"})
    with pytest.raises(Conflict):
        archive.update_plan(made["id"], {"revision": made["revision"], "title": "Stale"})
    with pytest.raises(Conflict):
        archive.delete_plan(made["id"], {"revision": made["revision"]})

    kept = plans.load(made["id"])
    assert (kept.title, kept.desc, kept.revision) == ("Home", "Arriving", first["revision"])


def test_an_edit_changes_only_the_fields_it_was_sent(archive):
    made = archive.create_plan({"title": "Home", "moments": [{"id": "a#00000.00", "in_edit": True}]})
    archive.update_plan(made["id"], {"revision": made["revision"], "stage": "ready"})

    kept = plans.load(made["id"])
    assert kept.title == "Home" and kept.moments[0].in_edit
    assert kept.stage == "ready" and kept.stage_at


def test_a_moment_remembers_where_it_is_in_the_footage(archive):
    """The id is derived from a start time and re-analysis renumbers it; the
    clip and the range are what find it again."""
    made = archive.create_plan({"moments": [{"id": "a#00030.00"}]})
    m = plans.load(made["id"]).moments[0]
    assert (m.stem, m.start, m.end) == ("a", 30, 60)


def test_typing_into_a_plan_keeps_one_trash_copy_not_one_per_save(archive, monkeypatch):
    """A description typed as you go is a save every few hundred ms. One copy
    a sitting, or a paragraph pushes corrected transcripts out of the trash."""
    made = archive.create_plan({"title": "Home"})
    revision = made["revision"]
    for word in ["A", "A visit", "A visit home"]:
        revision = archive.update_plan(made["id"], {"revision": revision, "desc": word})["revision"]
    assert len(trash.entries()) == 1

    monkeypatch.setattr(plans, "TRASH_EVERY", timedelta(seconds=-1))
    archive.update_plan(made["id"], {"revision": revision, "desc": "later"})
    assert len(trash.entries()) == 2


def test_deleting_a_plan_keeps_it_and_restores_it_to_the_plans_directory(archive):
    made = archive.create_plan({"title": "Home"})
    archive.delete_plan(made["id"], {"revision": made["revision"]})
    assert plans.load_all() == []

    entry = trash.entries()[0]
    assert entry.destination == plans.path_for(made["id"])
    trash.restore(entry)
    assert plans.load(made["id"]).title == "Home"


def test_a_deleted_plan_brought_back_keeps_its_id_and_its_place_on_the_schedule(archive):
    """The page's undo of a delete. Under a new id it would be a stranger to
    the schedule, which names plans by id, and a second copy in the backup."""
    a, b, c = (plans.create({"title": t, "stage": "ready"}) for t in "ABC")
    plans.save_schedule([c.id, b.id, a.id], None)
    gone = plans.load(b.id)
    plans.delete(b.id, gone.revision)

    back = plans.create(gone.model_dump(mode="json"))

    assert (back.id, back.created_at, back.title) == (b.id, gone.created_at, "B")
    assert [p.title for p in plans.scheduled(plans.load_all(), plans.load_schedule())] == ["C", "B", "A"]


def test_a_plan_is_never_brought_back_over_one_that_is_there(archive):
    plan = plans.create({"title": "Here"})
    with pytest.raises(Conflict):
        plans.create({"id": plan.id, "title": "Over it"})
    assert plans.load(plan.id).title == "Here"


def test_backup_mirrors_plans_into_the_archive(archive):
    made = archive.create_plan({"title": "Home"})
    backup.backup()
    assert (backup.archive_root() / "plans" / f'{made["id"]}.plan.json').exists()


def test_a_new_plan_goes_last_and_a_deleted_one_leaves_the_rest_in_order(archive):
    one = archive.create_plan({"title": "One"})
    archive.create_plan({"title": "Two"})
    archive.create_plan({"title": "Three"})
    archive.delete_plan(one["id"], {"revision": one["revision"]})
    assert [p["title"] for p in archive.plans()["plans"]] == ["Two", "Three"]
    assert "number" not in archive.plans()["plans"][0]   # only the schedule numbers anything


def _stack(api):
    return [(p["title"], p["order"]) for p in api.plans()["plans"]]


def test_plans_are_kept_in_stage_order_and_a_plan_changing_stage_joins_the_end_of_its_group(archive):
    """The page opens furthest along first, and that order is the files' own:
    a reload, another tab and the backup all see it. Moving up to gathering
    puts a plan after the ones already gathering, not back where it was
    thought of."""
    made = {t: archive.create_plan({"title": t, "stage": stage}) for t, stage in [
        ("Shelved", "shelved"), ("Idea one", "idea"), ("Ready", "ready"),
        ("Gathering", "gathering"), ("Idea two", "idea")]}
    assert _stack(archive) == [("Ready", 0), ("Gathering", 1), ("Idea one", 2),
                               ("Idea two", 3), ("Shelved", 4)]

    one = made["Idea one"]
    got = archive.update_plan(one["id"], {"revision": one["revision"], "stage": "gathering"})
    assert [t for t, _ in _stack(archive)] == ["Ready", "Gathering", "Idea one", "Idea two", "Shelved"]
    assert got["order"] == 2 and got["orders"] == {p["id"]: p["order"] for p in archive.plans()["plans"]}

    shelved = made["Shelved"]
    archive.update_plan(shelved["id"], {"revision": shelved["revision"], "stage": "idea"})
    assert [t for t, _ in _stack(archive)] == ["Ready", "Gathering", "Idea one", "Idea two", "Shelved"]
    gathering = plans.load(made["Gathering"]["id"])
    archive.update_plan(gathering.id, {"revision": gathering.revision, "stage": "idea"})
    assert [t for t, _ in _stack(archive)] == ["Ready", "Idea one", "Idea two", "Shelved", "Gathering"]


def test_restacking_moves_no_other_plans_revision_so_an_open_tab_can_still_save(archive):
    """One stage change renumbers the plans below it. Had their revisions
    moved, a tab holding them would be refused at its next keystroke on every
    one, for a change nobody made to them -- and nothing takes an order from
    a save, so a stale tab cannot put an old one back either."""
    idea = archive.create_plan({"title": "Idea"})
    other = archive.create_plan({"title": "Other"})
    archive.update_plan(other["id"], {"revision": other["revision"], "stage": "ready"})
    assert plans.load(idea["id"]).revision == idea["revision"]
    archive.update_plan(idea["id"], {"revision": idea["revision"], "title": "Idea, typed", "order": 99})
    assert _stack(archive) == [("Other", 0), ("Idea, typed", 1)]


def test_an_archive_from_before_stage_order_is_put_in_order_by_its_next_save(archive, monkeypatch):
    """The plans in the archive were numbered in the order they were thought
    of. Reading them must not write, so the first save does it -- once, and
    without a trash copy per plan pushing corrected transcripts out."""
    a = archive.create_plan({"title": "Idea"})
    b = archive.create_plan({"title": "Ready"})
    for plan, order, stage in [(plans.load(a["id"]), 0, "idea"), (plans.load(b["id"]), 1, "ready")]:
        plan.order, plan.stage = order, stage
        plans._put(plan)
    assert [t for t, _ in _stack(archive)] == ["Idea", "Ready"]

    before = {e.path for e in trash.entries()}
    archive.update_plan(a["id"], {"revision": a["revision"], "desc": "typed"})
    assert _stack(archive) == [("Ready", 0), ("Idea", 1)]
    # The plan that was edited keeps its copy; the one only renumbered does not.
    assert [e.original for e in trash.entries() if e.path not in before] == [f'{a["id"]}.plan.json']


def test_posting_a_reel_does_not_move_it_on_the_plans_page(archive):
    """Its place among the ready plans is where a reel the schedule never
    placed goes out, so moving it on posting renumbered the episode."""
    _ready(archive, "A")
    b = _ready(archive, "B")
    _ready(archive, "C")
    archive.update_plan(b["id"], {"revision": b["revision"], "posted": True})
    assert [t for t, _ in _stack(archive)] == ["A", "B", "C"]
    assert _order(archive) == [(1, "A"), (2, "B"), (3, "C")]


def test_the_ready_plans_are_in_the_order_the_schedule_sends_them_out(archive):
    """Two orders for the same reels, one per page, was one more thing that
    could disagree. A drag on the Schedule moves the plans on Plans, and a
    plan made ready lands where the Schedule puts it: after every reel
    already placed, whatever place it had as an idea."""
    early = archive.create_plan({"title": "Thought of first"})
    a, b = _ready(archive, "A"), _ready(archive, "B")
    archive.reorder_schedule({"revision": archive.schedule()["revision"], "order": [b["id"], a["id"]]})
    assert [t for t, _ in _stack(archive)] == ["B", "A", "Thought of first"]
    assert plans.load(a["id"]).revision == a["revision"]

    archive.update_plan(early["id"], {"revision": early["revision"], "stage": "ready"})
    assert [t for t, _ in _stack(archive)] == ["B", "A", "Thought of first"]
    assert _order(archive) == [(1, "B"), (2, "A"), (3, "Thought of first")]


def test_a_plan_id_cannot_become_a_path(archive):
    with pytest.raises(ValueError):
        plans.path_for("../../sidecars/a")


# --- moving out of the browser ----------------------------------------------

def test_the_browsers_plans_are_imported_in_their_order_with_their_ids(archive):
    assert archive.plans()["exists"] is False
    archive.import_plans({"plans": [
        {"id": "trip-home", "title": "Home", "moments": [{"id": "a#00000.00", "title": "Old name"}]},
        {"id": "4b1c3a6e-0000-4000-8000-000000000000", "title": "Mine", "desc": "typed"},
        {"id": "trip-home", "title": "A duplicate from a second merge"},
    ]})
    got = archive.plans()
    assert got["exists"] is True
    assert [(p["id"], p["title"]) for p in got["plans"]] == [
        ("trip-home", "Home"), ("4b1c3a6e-0000-4000-8000-000000000000", "Mine")]
    assert got["plans"][0]["stage"] == "idea"


def test_importing_is_refused_once_the_archive_has_plans(archive):
    """A second tab still holding the old `localStorage` copy must not bring
    back a plan deleted on purpose -- even when every plan has been deleted."""
    archive.import_plans({"plans": [{"id": "trip-home", "title": "Home"}]})
    only = archive.plans()["plans"][0]
    archive.delete_plan(only["id"], {"revision": only["revision"]})

    with pytest.raises(Conflict):
        archive.import_plans({"plans": [{"id": "trip-home", "title": "Home"}]})
    assert archive.plans()["plans"] == []


def test_a_plan_that_does_not_parse_imports_nothing(archive):
    """Half a list would count as imported, and the rest could never follow."""
    with pytest.raises(ValueError):
        archive.import_plans({"plans": [{"id": "good", "title": "Fine"},
                                        {"id": "bad", "stage": "someday"}]})
    assert archive.plans()["exists"] is False


# --- re-analysis -------------------------------------------------------------

def test_a_moment_renumbered_by_reanalysis_reattaches_by_its_midpoint(archive):
    made = archive.create_plan({"moments": [{"id": "a#00030.00", "title": "Coconut scraper"}]})
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=120, fps="30"),
        segments=[Segment(id="a#00000.00", start=0, end=28, title="Marriage chat"),
                  Segment(id="a#00028.00", start=28, end=64, title="Scraping the coconut")],
    ).save(config.sidecar_path("a"))
    index.build()

    moment = archive.plans()["plans"][0]["moments"][0]
    assert (moment["id"], moment["title"], moment["reattached"]) == (
        "a#00028.00", "Scraping the coconut", True)
    # Not written on a read: the file still says what it was told.
    assert plans.load(made["id"]).moments[0].id == "a#00030.00"


def test_a_moment_imported_with_only_its_id_still_finds_its_segment(archive):
    """What the browser had was an id and a title -- but the id is where it
    started."""
    live = {"a#00000.00": {"id": "a#00000.00", "stem": "a", "start": 0, "end": 31, "title": "New"}}
    plan = _plan(moments=[{"id": "a#00001.50"}])
    assert plans.reattach([plan], live) == {(plan.id, "a#00000.00")}


def test_a_moment_whose_footage_is_gone_stays_lost(archive):
    plan = _plan(moments=[{"id": "b#00010.00", "stem": "b", "start": 10, "end": 20}])
    assert plans.reattach([plan], {}) == set()
    assert plan.moments[0].id == "b#00010.00"


# --- state -------------------------------------------------------------------

def test_a_moment_in_an_idea_is_proposed_whether_or_not_it_is_ticked():
    plan = _plan(stage="idea", moments=[{"id": "x", "in_edit": True}, {"id": "y"}])
    got = plans.segment_states([plan], [], [_seg("x"), _seg("y")])
    assert (got["x"]["state"], got["y"]["state"]) == ("proposed", "proposed")


def test_only_the_ticked_takes_are_planned_when_a_plan_is_ready():
    """A ready plan still holds the takes it did not choose, and they are not
    planned."""
    plan = _plan(stage="ready", moments=[{"id": "x", "in_edit": True}, {"id": "y"}])
    got = plans.segment_states([plan], [], [_seg("x"), _seg("y")])
    assert (got["x"]["state"], got["y"]["state"]) == ("planned", "proposed")


def test_posting_a_reel_publishes_its_chosen_takes_and_frees_the_rest():
    plan = _plan(stage="ready", posted=True,
                 moments=[{"id": "x", "in_edit": True}, {"id": "y"}])
    got = plans.segment_states([plan], [], [_seg("x"), _seg("y")])
    assert (got["x"]["state"], got["y"]["state"]) == ("published", "unseen")
    assert got["y"]["plans"][0]["posted"] is True   # still says where it was


def test_a_plan_saved_by_an_earlier_shape_still_opens():
    """Stages once ran on past ready, and posting once stamped a date; a file
    written then must still open, and still say whether it went out."""
    load = plans.Plan.model_validate
    assert (load({"stage": "editing"}).stage, load({"stage": "editing"}).posted) == ("ready", False)
    assert (load({"stage": "published"}).stage, load({"stage": "published"}).posted) == ("ready", True)
    assert load({"stage": "ready", "posted": True}).posted is True
    assert load({"stage": "ready", "posted_at": None}).posted is False
    assert "posted_at" not in load({"stage": "ready", "posted_at": None}).model_dump()
    # Before there was a finished, a reel that went out had been.
    assert load({"stage": "published"}).finished is True
    assert load({"stage": "ready", "posted": True}).finished is True
    assert load({"stage": "editing"}).finished is False


def test_a_posted_reel_cannot_leave_ready(archive):
    made = archive.create_plan({"stage": "ready"})
    posted = archive.update_plan(made["id"], {"revision": made["revision"],
                                              "posted": True})
    with pytest.raises(ValueError, match="set it back to Ready on Schedule first"):
        archive.update_plan(made["id"], {"revision": posted["revision"], "stage": "gathering"})
    # Un-posting leaves it finished, which holds it too.
    with pytest.raises(ValueError, match="A finished reel stays ready"):
        archive.update_plan(made["id"], {"revision": posted["revision"],
                                         "stage": "gathering", "posted": False})
    # Back to Ready and moving it on in one save is allowed.
    back = archive.update_plan(made["id"], {"revision": posted["revision"], "stage": "gathering",
                                            "posted": False, "finished": False})
    assert (back["stage"], back["posted"], back["finished"]) == ("gathering", False, False)


def test_a_finished_reel_cannot_leave_ready_either(archive):
    """It has been cut. Sending it back to gathering would leave a finished
    mark on a plan still picking its takes, so the step on the Schedule has
    to be taken back first -- in the same save is enough."""
    made = archive.create_plan({"stage": "ready"})
    done = archive.update_plan(made["id"], {"revision": made["revision"], "finished": True})
    with pytest.raises(ValueError, match="set it back to Ready on Schedule first"):
        archive.update_plan(made["id"], {"revision": done["revision"], "stage": "idea"})
    back = archive.update_plan(made["id"], {"revision": done["revision"],
                                            "stage": "idea", "finished": False})
    assert (back["stage"], back["finished"]) == ("idea", False)


def test_a_reel_cannot_be_posted_without_being_finished(archive):
    """Posted is the step after finished, not a switch beside it: a reel
    marked posted and not finished is a Schedule tile that is at two places
    at once."""
    made = archive.create_plan({"stage": "ready"})
    with pytest.raises(ValueError, match="was finished"):
        archive.update_plan(made["id"], {"revision": made["revision"],
                                         "posted": True, "finished": False})
    posted = archive.update_plan(made["id"], {"revision": made["revision"],
                                              "posted": True, "finished": True})
    with pytest.raises(ValueError, match="was finished"):
        archive.update_plan(made["id"], {"revision": posted["revision"], "finished": False})
    # Un-posting leaves it finished: the cut did not stop existing.
    back = archive.update_plan(made["id"], {"revision": posted["revision"], "posted": False})
    assert (back["finished"], back["posted"]) == (True, False)


def test_finishing_a_reel_changes_no_moment_s_state():
    """Nothing has gone out, and a cut says for itself which shots it used."""
    plan = _plan(stage="ready", finished=True,
                 moments=[{"id": "x", "in_edit": True}, {"id": "y"}])
    got = plans.segment_states([plan], [], [_seg("x"), _seg("y")])
    assert (got["x"]["state"], got["y"]["state"]) == ("planned", "proposed")


def test_a_finished_reel_still_moves_on_the_schedule(archive):
    """Only going out fixes a reel's number; a cut waiting to be posted can
    still be sent out later than planned."""
    a, b = _ready(archive, "A"), _ready(archive, "B")
    archive.update_plan(a["id"], {"revision": a["revision"], "finished": True})
    revision = archive.schedule()["revision"]
    archive.reorder_schedule({"revision": revision, "order": [b["id"], a["id"]]})
    assert _order(archive) == [(1, "B"), (2, "A")]


def test_a_shelved_plan_claims_nothing():
    plan = _plan(stage="shelved", moments=[{"id": "x", "in_edit": True}])
    assert plans.segment_states([plan], [], [_seg("x", rating="favorite")])["x"]["state"] == "seen"


def test_a_shot_in_a_cut_is_used_without_any_plan():
    cut = reel.Reel(slug="home", title="Home", shots=[reel.Shot(stem="a", start=0, end=5, segment_id="x")])
    out = reel.Reel(slug="out", title="Out", status="published",
                    shots=[reel.Shot(stem="a", start=0, end=5, segment_id="y")])
    got = plans.segment_states([], [cut, out], [_seg("x"), _seg("y")])
    assert (got["x"]["state"], got["y"]["state"]) == ("used", "published")
    assert got["x"]["cuts"] == [{"slug": "home", "title": "Home", "status": "planned"}]


def test_the_furthest_state_wins_when_two_plans_disagree():
    idea = _plan(stage="idea", title="Idea", moments=[{"id": "x"}])
    out = _plan(stage="ready", title="Out", posted=True,
                moments=[{"id": "x", "in_edit": True}])
    got = plans.segment_states([idea, out], [], [_seg("x")])["x"]
    assert got["state"] == "published"
    assert [p["title"] for p in got["plans"]] == ["Idea", "Out"]


def test_a_moment_nobody_has_claimed_is_unseen_seen_or_rejected():
    got = plans.segment_states([], [], [
        _seg("a"), _seg("b", status="reviewed"), _seg("c", rating="favorite"), _seg("d", rating="rejected")])
    assert [got[k]["state"] for k in "abcd"] == ["unseen", "seen", "seen", "rejected"]
    assert got["c"]["favorite"] is True


def test_a_rejected_moment_still_in_a_plan_carries_a_warning():
    plan = _plan(stage="gathering", title="Home", moments=[{"id": "x"}])
    got = plans.segment_states([plan], [], [_seg("x", rating="rejected")])["x"]
    assert got["state"] == "proposed"
    assert got["warn"] == "rejected, but still in “Home”"


def test_a_moment_ticked_in_two_plans_carries_a_warning():
    """The one mistake the ticks exist to catch: the same take in two reels."""
    one = _plan(stage="ready", title="Home", moments=[{"id": "x", "in_edit": True}])
    shelved = _plan(stage="shelved", title="Old", moments=[{"id": "x", "in_edit": True}])
    two = _plan(stage="idea", title="Wi-Fi", moments=[{"id": "x", "in_edit": True}])
    got = plans.segment_states([one, shelved, two], [], [_seg("x")])["x"]
    assert got["warn"] == "in the edit for “Home” and “Wi-Fi”"


def test_segments_carry_their_state_and_can_be_filtered_by_it(archive):
    made = archive.create_plan({"stage": "ready", "moments": [{"id": "a#00030.00", "in_edit": True}]})
    rows = {r["id"]: r["state"] for r in archive.segments()}
    assert rows == {"a#00000.00": "unseen", "a#00030.00": "planned"}
    assert [r["id"] for r in archive.segments(state="planned")] == ["a#00030.00"]
    assert archive.states()["counts"]["planned"] == 1
    assert archive.clip("a")["segments"][1]["state"] == "planned"
    assert made["moments"][0]["reattached"] is False


# --- the schedule --------------------------------------------------------------

def _ready(api, title, **fields):
    return api.create_plan({"title": title, "stage": "ready", **fields})


def _order(api):
    return [(r["number"], r["title"]) for r in api.schedule()["reels"]]


def test_ready_plans_join_the_schedule_in_their_outline_order(archive):
    """The generated outline still says where a plan first lands, and nothing
    more: a plan with no week goes after the ones that have one."""
    archive.create_plan({"title": "Still an idea"})
    _ready(archive, "No week")
    _ready(archive, "Week 2", week=2, slot=1)
    _ready(archive, "Week 1, second", week=1, slot=2)
    _ready(archive, "Week 1, first", week=1, slot=1)
    assert _order(archive) == [(1, "Week 1, first"), (2, "Week 1, second"), (3, "Week 2"), (4, "No week")]


def test_a_reorder_is_kept_and_a_plan_that_becomes_ready_goes_after_it(archive):
    a, b = _ready(archive, "A"), _ready(archive, "B")
    got = archive.schedule()
    archive.reorder_schedule({"revision": got["revision"], "order": [b["id"], a["id"]]})
    _ready(archive, "C")
    assert _order(archive) == [(1, "B"), (2, "A"), (3, "C")]


def test_a_plan_that_leaves_ready_leaves_the_schedule(archive):
    a, b = _ready(archive, "A"), _ready(archive, "B")
    got = archive.schedule()
    archive.reorder_schedule({"revision": got["revision"], "order": [b["id"], a["id"]]})
    archive.update_plan(b["id"], {"revision": b["revision"], "stage": "gathering"})
    assert _order(archive) == [(1, "A")]


def test_a_reorder_over_a_newer_schedule_is_refused(archive):
    a, b = _ready(archive, "A"), _ready(archive, "B")
    stale = archive.schedule()["revision"]
    archive.reorder_schedule({"revision": stale, "order": [b["id"], a["id"]]})
    with pytest.raises(Conflict):
        archive.reorder_schedule({"revision": stale, "order": [a["id"], b["id"]]})
    assert _order(archive) == [(1, "B"), (2, "A")]


def test_a_posted_reel_keeps_its_place_when_others_move(archive):
    """Episode 2 went out as episode 2. Moving the others round it is
    planning; moving it is rewriting what happened."""
    a, b, c = _ready(archive, "A"), _ready(archive, "B"), _ready(archive, "C")
    archive.update_plan(b["id"], {"revision": b["revision"], "posted": True})
    revision = archive.schedule()["revision"]
    with pytest.raises(ValueError, match="keeps its place"):
        archive.reorder_schedule({"revision": revision, "order": [b["id"], a["id"], c["id"]]})
    archive.reorder_schedule({"revision": revision, "order": [c["id"], b["id"], a["id"]]})
    assert _order(archive) == [(1, "C"), (2, "B"), (3, "A")]


def test_a_reorder_must_name_exactly_the_scheduled_plans(archive):
    """A tab that loaded the schedule before a plan became ready would
    otherwise drop that plan from the order it saves."""
    a = _ready(archive, "A")
    revision = archive.schedule()["revision"]
    b = _ready(archive, "B")
    with pytest.raises(Conflict):
        archive.reorder_schedule({"revision": revision, "order": [a["id"]]})
    with pytest.raises(Conflict):
        archive.reorder_schedule({"revision": revision, "order": [a["id"], b["id"], a["id"]]})


def test_the_schedule_is_backed_up_and_restores_beside_the_plans(archive, monkeypatch):
    a, b = _ready(archive, "A"), _ready(archive, "B")
    first = archive.reorder_schedule({"revision": None, "order": [b["id"], a["id"]]})
    backup.backup()
    assert (backup.archive_root() / "plans" / "schedule.json").exists()
    assert (backup.archive_root() / "plans" / f'{a["id"]}.plan.json').exists()

    monkeypatch.setattr(plans, "TRASH_EVERY", timedelta(seconds=-1))
    archive.reorder_schedule({"revision": first["revision"], "order": [a["id"], b["id"]]})
    entry = next(e for e in trash.entries() if e.original == "schedule.json")
    assert entry.destination == plans.schedule_path()
    trash.restore(entry)
    assert _order(archive) == [(1, "B"), (2, "A")]
