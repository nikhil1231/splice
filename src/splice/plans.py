"""Plans: the reels the footage might become, and how far each one has got.

A plan lived in the browser's `localStorage` for as long as it was only an
idea -- a name, a reason and some moments that might go together. It moved
into the archive the day it could be marked *published*: from then on it is
the only record of which moments actually went out, and clearing site data
must not be the way that record is lost. So a plan is a file now, with a
revision token and a copy in the trash, like a draft.

It is still not a cut. A plan's moments are a pool of alternate takes (the
September outline puts thirteen behind a 60-second reel), and the ones ticked
*in the edit* are the ones it used. A cut is a running order with trims.

Planning and scheduling are separate. A plan goes from idea to *ready* and no
further; when and in what order the ready ones go out is the schedule, one
file beside the plans, and *finished* and *posted* are marks on a plan in it.
They were one thing -- a week and a slot on every plan, from the generated outline -- and a
page of ideas that could not be moved was the result.

Segment *state* is derived here and nowhere else: from the plans, the cuts and
the sidecar's own rating and status. Nothing stores it, because every one of
its inputs is already a file and a stored copy would only be a way for them
to disagree.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Mapping, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import config, trash
from .drafts import Conflict

Stage = Literal["idea", "gathering", "ready", "shelved"]
STAGES: tuple[str, ...] = get_args(Stage)

# Ascending: when two things claim a moment, the one further along wins. A
# moment published in one plan and still an idea in another has been used.
STATES = ("unseen", "seen", "rejected", "proposed", "planned", "used", "published")
_RANK = {s: i for i, s in enumerate(STATES)}

# Typing into a plan saves as you go, and every save of a file this valuable
# would otherwise put a copy in the trash -- a paragraph is two hundred of
# them, and `trash.KEEP` is 300, so one description would push the last
# corrected transcripts out. One copy per plan per ten minutes keeps what the
# plan looked like before this sitting without keeping every keystroke of it.
TRASH_EVERY = timedelta(minutes=10)

# Seeded ids are slugs (`trip-2026-09-home`) and the browser's are UUIDs. Both
# become a filename, so nothing else may.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")

# Two tabs saving the same plan at once must not both pass the revision check.
# Not the server's archive hold: no command writes plans, and refusing a title
# while `reclassify` runs would guard nothing.
_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token() -> str:
    return uuid.uuid4().hex


class PlanMoment(BaseModel):
    # What the browser sent before this was a file had only an id and a title,
    # and it may carry keys this does not know; neither is a reason to refuse.
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""
    # Where it is, as well as what it was called. Segment ids are derived from
    # start times and re-analysis renumbers them; `(clip, in, out)` is what
    # lets a moment find its segment again afterwards.
    stem: str = ""
    start: float | None = None
    end: float | None = None
    # Chosen for the reel, rather than one of the takes it was chosen from.
    in_edit: bool = False
    note: str = ""


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = 1
    id: str = Field(default_factory=token, pattern=_ID.pattern)
    revision: str = Field(default_factory=token)
    # Where it sits on the Plans page: grouped by stage, furthest along first,
    # and renumbered by `_restack` whenever that changes. Not the order they go
    # out in: that is the schedule's.
    order: int = 0
    title: str = Field(default="", max_length=300)
    desc: str = ""
    stage: Stage = "idea"
    week: int | None = None
    slot: int | None = None
    strand: str = ""
    target: str = ""
    moments: list[PlanMoment] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    # When it reached the stage it is in; `updated_at` moves with every
    # retyped word.
    stage_at: str | None = None
    # Whether the reel has been cut and is waiting to go out. A ready plan has
    # its takes chosen; that is not the same as an edit anyone could post, and
    # the Schedule could not tell the reel due on Friday from the one nobody
    # has opened Final Cut for. It changes no moment's state: nothing has gone
    # out, and the cut itself says which shots it used.
    finished: bool = False
    # Whether it has gone out. Only a finished plan can be, and while it is the
    # moments ticked for its edit are published. Yes or no rather than a date:
    # the date anyone could record is when the switch was clicked, which is not
    # when the reel went out, and a date that has to be clicked at the right
    # moment to be true is one nobody should be asked to keep.
    posted: bool = False

    @model_validator(mode="before")
    @classmethod
    def _older_stages(cls, data):
        """A plan saved by an earlier shape of this file.

        Stages once ran on to *editing* and *published*; both were a ready plan
        on its way out, and published had gone out. For a while after that,
        posting stamped a `posted_at` date. And before *finished* there was
        nothing between ready and posted, so a reel that went out then was
        finished without anyone saying so.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("stage") in ("editing", "published"):
            data["posted"] = data.get("posted") or data["stage"] == "published"
            data["stage"] = "ready"
        if data.pop("posted_at", None):
            data["posted"] = True
        if data.get("posted") and "finished" not in data:
            data["finished"] = True
        return data

    @model_validator(mode="after")
    def _posted_is_finished_is_ready(self):
        if self.posted and not self.finished:
            raise ValueError("A posted reel was finished — un-post it on Schedule first.")
        if self.stage != "ready" and (self.posted or self.finished):
            raise ValueError(_held(self.posted))
        return self


def _held(posted: bool) -> str:
    """Why a plan marked on the Schedule cannot leave Ready, in a sentence.

    Ready rather than un-post: un-posting leaves a reel finished, which holds
    it just the same, and one click on Ready clears both.
    """
    return (f"A {'posted' if posted else 'finished'} reel stays ready — "
            "set it back to Ready on Schedule first.")


# What a PATCH may change. The rest is the file's own bookkeeping.
EDITABLE = ("title", "desc", "stage", "week", "slot", "strand", "target", "moments",
            "finished", "posted")


def directory():
    return config.index_dir() / "plans"


def path_for(plan_id: str):
    if not _ID.match(plan_id or ""):
        raise ValueError("invalid plan id")
    return directory() / f"{plan_id}.plan.json"


def load(plan_id: str) -> Plan:
    path = path_for(plan_id)
    if not path.exists():
        raise KeyError(plan_id)
    return Plan.model_validate_json(path.read_text())


def load_all() -> list[Plan]:
    if not directory().is_dir():
        return []
    return sorted((Plan.model_validate_json(p.read_text())
                   for p in directory().glob("*.plan.json")),
                  key=lambda p: (p.order, p.created_at, p.id))


def _copied_lately(path) -> bool:
    cutoff = datetime.now(timezone.utc) - TRASH_EVERY
    return any(e.original == path.name and e.when >= cutoff for e in trash.entries())


def place(plan: Plan, live: Mapping[str, Mapping] | None) -> Plan:
    """Write down where each moment is while its id still says.

    Only for ids the index knows: a moment already lost keeps whatever it
    remembered, which is the only thing that can bring it back.
    """
    for m in plan.moments:
        row = (live or {}).get(m.id)
        if row:
            m.stem, m.start, m.end = row["stem"], row["start"], row["end"]
    return plan


def _put(plan: Plan) -> None:
    directory().mkdir(parents=True, exist_ok=True)
    path = path_for(plan.id)
    staged = path.with_suffix(".tmp")
    staged.write_text(plan.model_dump_json(indent=2) + "\n")
    staged.replace(path)


def _write(plan: Plan) -> Plan:
    plan.revision = token()
    plan.updated_at = now()
    _put(plan)
    return plan


def rank(plan: Plan) -> int:
    """Its group on the Plans page, furthest along first.

    In the order they were thought of, the one reel nearly ready sat under
    forty-one ideas. A posted reel stays with the ready ones. Sending it to the
    end of the list once moved its episode number too: a reel the schedule has
    never placed goes out in this order, and episode 2 went out as episode 2.
    """
    return {"ready": 0, "gathering": 1, "idea": 2, "shelved": 3}[plan.stage]


def _restack(moved: str | None = None) -> dict[str, int]:
    """Renumber every plan so the files themselves are in the page's order.

    Within a group the order the plans already had holds; `moved` has just
    changed group and joins the end of its new one. The ready plans are in
    the order they go out, from `scheduled`: two orders for the same reels,
    one on each page, was one more thing that could disagree. A plan that
    becomes ready therefore lands where the Schedule puts it, after every
    reel already placed. Returns every plan's order.

    Only `order` is written, and the revision does not move. An open tab
    holding the other forty plans would otherwise have every one of them
    refused at its next keystroke, for a change nobody made to them. It is
    safe because nothing sends an order: `edit` takes it from the file, never
    from the body. And no trash copy: an order is worked out from the stages,
    and one copy per plan per click would push corrected transcripts out of
    the trash within a dozen stage changes.
    """
    found = load_all()
    was = {p.id: p.order for p in found}
    # Last as well as last among equals: `scheduled` breaks its own ties on
    # `order`, and an old place near the top would win them.
    for plan in found:
        if plan.id == moved:
            plan.order = max(was.values()) + 1
    going_out = {p.id: i for i, p in enumerate(scheduled(found, load_schedule()))}
    stacked = sorted(found, key=lambda p: (rank(p), going_out.get(p.id, 0), p.order, p.created_at, p.id))
    for i, plan in enumerate(stacked):
        plan.order = i
        if was[plan.id] != i:
            _put(plan)
    return {p.id: p.order for p in stacked}


def save(plan: Plan, expected: str | None = None,
         live: Mapping[str, Mapping] | None = None) -> Plan:
    with _lock:
        path = path_for(plan.id)
        if path.exists():
            current = load(plan.id)
            if current.revision != expected:
                raise Conflict(f"Plan “{current.title or plan.id}” changed in "
                               "another window. Reload before saving.")
            if not _copied_lately(path):
                trash.keep(path, reason="plan edited")
        elif expected is not None:
            raise Conflict("That plan was deleted. Reload before saving.")
        return _write(place(plan, live))


def create(body: Mapping, live: Mapping[str, Mapping] | None = None) -> Plan:
    """A new plan goes last in its group, so no plan above it moves.

    An `id` is taken only for a plan being brought back -- the page's undo of
    a delete. Under its old id it is the same plan to the schedule, which
    names plans by id, and to the backup, which would otherwise hold it twice.
    An id that is already a file is refused, never written over.
    """
    with _lock:
        existing = load_all()
        fields = {k: body[k] for k in EDITABLE if k in body}
        if body.get("id"):
            if path_for(body["id"]).exists():
                raise Conflict("A plan with that id already exists. Reload before bringing it back.")
            fields |= {k: body[k] for k in ("id", "created_at", "stage_at") if body.get(k)}
        plan = Plan.model_validate({
            "order": max((p.order for p in existing), default=-1) + 1,
            "stage_at": now(),
        } | fields)
        plan = save(plan, live=live)
        plan.order = _restack()[plan.id]
        return plan


def edit(plan_id: str, body: Mapping, live: Mapping[str, Mapping] | None = None) -> Plan:
    """Only the fields sent, onto the file as it is now.

    Loaded under the lock and immediately before the write, so a second tab
    that saved a moment ago is refused by its revision rather than written
    over by this one's older copy of the fields it did not touch.
    """
    with _lock:
        current = load(plan_id)
        if current.revision != body.get("revision"):
            raise Conflict(f"Plan “{current.title or plan_id}” changed in another "
                           "window. Reload before saving.")
        fields = {k: body[k] for k in EDITABLE if k in body}
        # A reel said to have gone out was finished, whether or not the save
        # says so too; one that says it was not is refused by the model.
        if fields.get("posted") and "finished" not in fields:
            fields["finished"] = True
        if "stage" in fields and fields["stage"] != current.stage:
            # Said here in a sentence; the model's own check would say it too,
            # wrapped in a validation report nobody should have to read.
            posted = fields.get("posted", current.posted)
            if fields["stage"] != "ready" and (posted or fields.get("finished", current.finished)):
                raise ValueError(_held(posted))
            fields["stage_at"] = now()
        updated = Plan.model_validate(current.model_dump() | fields)
        saved = save(updated, current.revision, live=live)
        # Every edit, not only a stage change: the first save after this
        # arrived is what puts an archive that predates it into order.
        saved.order = _restack(moved=plan_id if rank(updated) != rank(current) else None)[plan_id]
        return saved


def delete(plan_id: str, revision: str | None) -> None:
    with _lock:
        plan = load(plan_id)
        if plan.revision != revision:
            raise Conflict(f"Plan “{plan.title or plan_id}” changed in another "
                           "window. Reload before deleting.")
        path = path_for(plan_id)
        trash.keep(path, reason="plan deleted")
        path.unlink()
        _restack()


def import_plans(found: Iterable[Mapping], live: Mapping[str, Mapping] | None = None) -> list[Plan]:
    """Bring the browser's plans into the archive, once.

    The directory existing is the mark that this happened. A second tab still
    holding the old copy in its `localStorage` would otherwise import it over
    everything done since -- including a plan deleted on purpose.

    Validated whole before anything is written, and written to one side and
    moved into place, so a plan that does not parse cannot leave half a list
    behind it that then counts as imported.
    """
    with _lock:
        if directory().exists():
            raise Conflict("Plans are already in the archive; nothing was imported.")
        seen: set[str] = set()
        plans: list[Plan] = []
        for body in found:
            plan = Plan.model_validate({k: v for k, v in body.items()
                                        if k not in {"revision", "order"}})
            if plan.id in seen:
                continue
            seen.add(plan.id)
            plan.order = len(plans)
            plan.stage_at = plan.stage_at or now()
            plans.append(place(plan, live))

        staging = directory().with_name("plans.importing")
        if staging.exists():
            for leftover in staging.iterdir():
                leftover.unlink()
        staging.mkdir(parents=True, exist_ok=True)
        for plan in plans:
            plan.revision, plan.updated_at = token(), now()
            (staging / f"{plan.id}.plan.json").write_text(plan.model_dump_json(indent=2) + "\n")
        staging.replace(directory())
        _restack()
        return plans


def _point(m: PlanMoment) -> tuple[str, float] | None:
    """Where in the footage a moment was, for finding it again.

    The middle of what it remembered, as `analyze` does for a rating. A moment
    brought over from the browser knows only its id -- but the id is the stem
    and the start time, so it still says where it was.
    """
    if m.stem and m.start is not None and m.end is not None:
        return m.stem, (m.start + m.end) / 2
    stem, _, at = m.id.rpartition("#")
    try:
        return stem, float(at) + 0.01
    except ValueError:
        return None


def reattach(plans: list[Plan], live: Mapping[str, Mapping]) -> set[tuple[str, str]]:
    """Point moments whose segment was renumbered at the one now covering them.

    In place, and not saved: a GET must not write. The browser sends the new
    ids back with its next save of that plan. Returns `(plan id, moment id)`
    for each one moved, so the page can say which.
    """
    by_stem: dict[str, list[Mapping]] = {}
    for row in live.values():
        by_stem.setdefault(row["stem"], []).append(row)
    moved: set[tuple[str, str]] = set()
    for plan in plans:
        held = {m.id for m in plan.moments}
        for m in plan.moments:
            if m.id in live:
                continue
            point = _point(m)
            if point is None:
                continue
            stem, at = point
            match = next((r for r in by_stem.get(stem, [])
                          if r["start"] <= at < r["end"] and r["id"] not in held), None)
            if match is None:
                continue
            held.add(match["id"])
            m.id, m.title = match["id"], match["title"] or m.title
            m.stem, m.start, m.end = match["stem"], match["start"], match["end"]
            moved.add((plan.id, m.id))
    return moved


def _claim(stage: str, in_edit: bool, posted: bool = False) -> str | None:
    """What being in a plan at this stage makes a moment.

    Only a ticked moment goes further than proposed: a ready plan is still
    holding the takes it did not choose, and they are not planned. A posted
    reel lets its unchosen takes go -- they are free for the next one -- and a
    shelved plan claims nothing at all.
    """
    if stage == "shelved":
        return None
    if stage != "ready":
        return "proposed"
    if posted:
        return "published" if in_edit else None
    return "planned" if in_edit else "proposed"


def _named(claim: Mapping) -> str:
    return f"“{claim['title'] or 'Untitled plan'}”"


def segment_states(plans: list[Plan], reels: Iterable, segments: Iterable[Mapping]) -> dict[str, dict]:
    """Every segment's state, and what gave it that state.

    `segments` need only `id`, `rating` and `status`; `reels` are cuts.
    """
    claims: dict[str, list[dict]] = {}
    for plan in plans:
        for m in plan.moments:
            claims.setdefault(m.id, []).append({
                "id": plan.id, "title": plan.title, "stage": plan.stage,
                "posted": plan.posted, "in_edit": m.in_edit,
            })
    cuts: dict[str, list[dict]] = {}
    for reel in reels:
        for sid in dict.fromkeys(s.segment_id for s in reel.shots if s.segment_id):
            cuts.setdefault(sid, []).append(
                {"slug": reel.slug, "title": reel.title, "status": reel.status})

    out: dict[str, dict] = {}
    for seg in segments:
        sid, rating, status = seg["id"], seg.get("rating") or "none", seg.get("status") or "indexed"
        mine, used_in = claims.get(sid, []), cuts.get(sid, [])
        found = [_claim(c["stage"], c["in_edit"], c["posted"]) for c in mine]
        found += ["published" if c["status"] == "published" else "used" for c in used_in]
        found = [f for f in found if f]
        base = ("rejected" if rating == "rejected"
                else "seen" if rating == "favorite" or status != "indexed"
                else "unseen")
        state = max([base, *found], key=_RANK.__getitem__)

        # By title: plans have no numbers, since the order they go out in is
        # the schedule's and not theirs.
        warn = []
        if rating == "rejected" and found:
            where = [_named(c) for c in mine if _claim(c["stage"], c["in_edit"], c["posted"])]
            where += [f"cut {c['slug']}" for c in used_in]
            warn.append("rejected, but still in " + ", ".join(where))
        ticked = [c for c in mine if c["in_edit"] and c["stage"] != "shelved"]
        if len(ticked) > 1:
            warn.append("in the edit for " + " and ".join(_named(c) for c in ticked))
        out[sid] = {"state": state, "favorite": rating == "favorite",
                    "warn": "; ".join(warn), "plans": mine, "cuts": used_in}
    return out


def counts(states: Mapping[str, Mapping]) -> dict[str, int]:
    tally = dict.fromkeys(STATES, 0)
    for s in states.values():
        tally[s["state"]] += 1
    return tally


# --- the schedule --------------------------------------------------------------

class Schedule(BaseModel):
    """The order the ready plans go out in.

    One file rather than a position on each plan: an order is one decision,
    and dragging a tile should be one write with one revision -- not a new
    revision on every plan it passes, each of which a Plans tab left open
    would then be refused for.
    """
    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = 1
    revision: str | None = None
    order: list[str] = Field(default_factory=list)
    updated_at: str | None = None


def schedule_path():
    return directory() / "schedule.json"


def load_schedule() -> Schedule:
    path = schedule_path()
    return Schedule.model_validate_json(path.read_text()) if path.exists() else Schedule()


def scheduled(plans: list[Plan], schedule: Schedule) -> list[Plan]:
    """Every ready plan, in the order they go out.

    The ones the schedule names, where it put them; then any it has not seen
    yet, by the outline's week and slot where they have one -- the generated
    outline still gets to say where a plan first lands, and nothing more. A
    plan that is no longer ready is not on it, whatever the file says.
    """
    ready = {p.id: p for p in plans if p.stage == "ready"}
    placed = [ready[i] for i in dict.fromkeys(schedule.order) if i in ready]
    named = {p.id for p in placed}
    rest = sorted((p for p in ready.values() if p.id not in named),
                  key=lambda p: (p.week is None, p.week or 0, p.slot is None, p.slot or 0, p.order))
    return placed + rest


def save_schedule(order: list[str], expected: str | None) -> Schedule:
    with _lock:
        if not directory().is_dir():
            raise Conflict("There are no plans in the archive to schedule.")
        current = load_schedule()
        if current.revision != expected:
            raise Conflict("The schedule changed in another window. Reload before moving anything.")
        now_showing = scheduled(load_all(), current)
        if sorted(order) != sorted(p.id for p in now_showing) or len(set(order)) != len(order):
            raise Conflict("The ready plans changed since the schedule was loaded. Reload it.")
        # Something already posted is episode 3 for good; the rest move round it.
        for at, plan in enumerate(now_showing):
            if plan.posted and order[at] != plan.id:
                raise ValueError(f"“{plan.title or plan.id}” is posted, and a posted reel keeps its place.")
        path = schedule_path()
        if path.exists() and not _copied_lately(path):
            trash.keep(path, reason="schedule reordered")
        saved = Schedule(order=list(order), revision=token(), updated_at=now())
        staged = path.with_suffix(".tmp")
        staged.write_text(saved.model_dump_json(indent=2) + "\n")
        staged.replace(path)
        _restack()
        return saved
