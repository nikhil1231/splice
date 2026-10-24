"""A local browsing and editing UI for the archive.

Deliberately dependency-free: this is one person on localhost looking at their
own family footage, and a web framework would be more machinery than the job
needs. It reads the sidecars, serves them as JSON, and writes edits straight
back -- so it binds to 127.0.0.1 only, and never to anything else.

Three things here are not request/response. A **render** takes tens of
seconds, so it runs on a thread and reports how far in it has got; one at a
time, because encoding takes the whole machine. The header's **commands** are `greels`
itself -- sync, align, rename -- and take as long as they take, so they run
on a thread too, one at a time, and own the archive against edits while they
do. And every write starts a clock on a **backup** into the repo,
because an evening of correcting Konkani is the most expensive thing that
happens in this UI and `backup` used to be a button you had to remember.
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config, editors, index, library, plans, trash, drafts, draft_generation
from .schema import Segment, Sidecar, speakers
from .taxonomy import Taxonomy

WEB_DIR = Path(__file__).parent / "web"

# Every tab of the page has an address, so a reload lands where it was rather
# than back on Plans. They are named here rather than served for any path: a
# mistyped `/api/segmnets` should be the 404 it is, not a page that loads and
# then fails to find its data. `tests/test_web.py` holds this to the page's own
# list of tabs.
PAGES = frozenset({"/plans", "/schedule", "/segments", "/review", "/clips",
                   "/tags", "/reels", "/texts"})


class Writes:
    """The write lock, and the one job that owns the archive for minutes.

    Every write reindexes, and a browser fires edits faster than that
    finishes, so ordinary writes queue behind each other -- a review pass
    lands a rating every two seconds and waiting a moment for the one before
    is right.

    A sync is not that. It rewrites sidecars for as long as agy takes, and a
    save that waited forty minutes for the lock is a save whose window was
    closed and reloaded past long ago. Worse, `reclassify` inside a sync loads
    every sidecar up front and writes back the ones it changed, so an edit
    that lands mid-walk is written over -- and a hand-corrected transcript is
    the one thing here that cannot be produced again. So a sync *marks* the
    lock rather than sitting on it, and an edit that arrives during one is
    refused with a sentence saying why.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.busy = ""

    def __enter__(self) -> "Writes":
        if self.busy:
            raise drafts.Conflict(self.busy)
        self._lock.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()

    @contextlib.contextmanager
    def held(self, why: str):
        """Own the archive for a slow job, refusing edits for the duration."""
        with self:
            # Taking it once lets the edit already in flight finish and
            # reindex; releasing it means nothing is queued behind the job.
            self.busy = why
        try:
            yield
        finally:
            self.busy = ""


_write_lock = Writes()

# How long after the last edit to mirror the sidecars into the repo. An evening
# of correcting Konkani is the most expensive thing that happens in this UI and
# the least regenerable, and `backup` was a button you had to remember. Long
# enough that a burst of review keystrokes is one backup, short enough that
# nothing you typed is more than a couple of minutes from being in git.
AUTOSAVE_AFTER = 90.0


class Autosave:
    """Mirror the archive into the repo a while after the last edit.

    Debounced rather than per-write: a review pass writes a rating every two
    seconds, and copying 60 files each time would be silly. Failures are
    reported and swallowed -- this is a safety net, and a safety net that
    brings the server down when the media root is unmounted is worse than
    none.
    """

    def __init__(self, after: float = AUTOSAVE_AFTER, on_message=print) -> None:
        self.after = after
        self.on_message = on_message
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pending = False

    def touch(self) -> None:
        if not self.after:
            return
        with self._lock:
            self._pending = True
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.after, self.run)
            self._timer.daemon = True
            self._timer.start()

    def run(self) -> None:
        """Back up now, if anything has changed since the last one."""
        with self._lock:
            if not self._pending:
                return
            self._pending = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        from . import backup as store

        try:
            added, updated, removed = store.backup()
        except store.Refused as exc:
            self.on_message(f"  ! not backed up: {exc}")
            return
        except Exception as exc:
            self.on_message(f"  ! not backed up: {type(exc).__name__}: {exc}")
            return
        if added or updated or removed:
            self.on_message(
                f"  backed up {added + updated} file(s) into archive/"
                + (f", {removed} removed" if removed else "")
                + " -- commit them when you are done"
            )

    def stop(self) -> None:
        """Flush on the way out, so quitting is not how an evening is lost."""
        self.run()


def _dedupe(shots: list) -> list:
    """One shot per range. A search and a suggestion routinely overlap."""
    seen: set[tuple] = set()
    out = []
    for shot in shots:
        key = (shot.stem, round(shot.start, 2), round(shot.end, 2))
        if key not in seen:
            seen.add(key)
            out.append(shot)
    return out


@dataclass
class Job:
    """A render in progress, or the last one that ran.

    Kept in memory rather than on disk: it is worth nothing once the process
    ends, and the file it produced is the durable half.
    """

    slug: str
    total: float
    state: str = "running"          # running | done | failed
    done: float = 0.0
    error: str = ""
    size: int = 0

    def payload(self) -> dict:
        return {
            "slug": self.slug, "state": self.state, "error": self.error,
            "done": round(self.done, 1), "total": round(self.total, 1),
            "progress": min(1.0, self.done / self.total) if self.total else 0.0,
            "size": self.size,
        }


class Renders:
    """One render at a time, and what it is doing.

    One, because encoding takes the whole machine: two at once is not twice
    the work, it is both of them at half speed and a browser that has stopped
    playing proxies. A second request is refused with the name of the first,
    which is more use than a queue nobody is watching.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._running: str | None = None

    def start(self, slug: str, work) -> Job:
        with self._lock:
            if self._running == slug:
                return self._jobs[slug]
            if self._running:
                raise ValueError(f"already rendering {self._running}")
            job = self._jobs[slug] = Job(slug=slug, total=0.0)
            self._running = slug

        def run() -> None:
            try:
                work(job)
                job.state = "done"
            except Exception as exc:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._running = None

        threading.Thread(target=run, daemon=True, name=f"render-{slug}").start()
        return job

    def get(self, slug: str) -> Job | None:
        return self._jobs.get(slug)


def _holds(label: str) -> str:
    """What an edit is told while a command owns the archive.

    It says what would have happened rather than "busy", because the thing at
    risk is a transcript somebody typed by hand.
    """
    return (
        f"{label} is running: it is rewriting the sidecars, and an edit saved "
        "now would be written over. Wait for it to finish, then save again."
    )


class Echo:
    """Tee what a command prints, to the terminal and into a job.

    These commands say what they are doing through `typer.echo`, which is
    stdout, so standing in front of stdout is the only way to put it in a
    browser. It is a tee rather than a capture because whoever started
    `greels serve` is watching that terminal, and a run that went silent
    there would look like a hang. Nothing else writes to stdout during one
    except the autosave notice, and that line is worth seeing in both places.
    """

    def __init__(self, stream, on_line) -> None:
        self._stream, self._on_line, self._buffered = stream, on_line, ""

    def write(self, text: str) -> int:
        self._stream.write(text)
        self._buffered += text
        while "\n" in self._buffered:
            line, self._buffered = self._buffered.split("\n", 1)
            self._on_line(line)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def done(self) -> None:
        """Emit a last line with no newline after it -- a prompt, or a crash
        cut short. Losing it would lose the only thing the browser can show."""
        if self._buffered:
            self._on_line(self._buffered)
            self._buffered = ""


@dataclass(frozen=True)
class Command:
    """One of the commands the header offers, and what it costs to run.

    `check` is the command's own dry run where it has one -- `sync`, `align`
    and `rename` all say what they would do and change nothing -- and it is
    what the dialog opens with, so that an hour of agy, or a set of renames
    that moves the originals in raw/, is something agreed to rather than
    something clicked.

    `holds` is whether a real run refuses edits while it goes. It is true of
    anything that loads sidecars, works, and saves them: an edit that lands
    mid-walk is written over, and a hand-corrected transcript is the one thing
    here that nothing can produce again. It is false of the two reports and of
    the derived things -- exports, poster frames, the index -- which read the
    archive and write somewhere else entirely.
    """

    name: str
    label: str
    blurb: str
    run: object
    check: object = None
    holds: bool = True

    def payload(self) -> dict:
        return {
            "name": self.name, "label": self.label, "blurb": self.blurb,
            "check": self.check is not None, "holds": self.holds,
        }


def commands(editor=None) -> dict[str, Command]:
    """Every command the header offers, in the order the menu shows them.

    The browser builds its menu from this rather than from a list of its own,
    so a command added here appears there and cannot drift from it. They are
    written out as `cli.x(...)` calls rather than looked up by name because
    that is the only form `tests/test_command_calls.py` can see, and an option
    added to a command silently changes what every existing caller does.

    The ones that hand work to an editor are built around whichever editor the
    page is set to, so the menu says "Resolve import of what is new" rather
    than naming Final Cut at somebody who no longer has it.
    """
    from . import cli   # late: `cli.serve` is what starts this server

    which = editors.get(editor)
    incoming = cli.incoming_path(which).name

    found = [
        Command(
            "sync", "Sync raw/",
            "Scan, proxy, analyse what is new, name it, reindex, align the "
            f"subtitles to the speech, write export/{incoming} for "
            f"{which.label} and back the archive up. The whole loop.",
            run=lambda: cli.sync(jobs=config.DEFAULT_JOBS, rename_new=True,
                                 dry_run=False, editor=which.name),
            check=lambda: cli.sync(jobs=config.DEFAULT_JOBS, rename_new=True,
                                   dry_run=True, editor=which.name),
        ),
        Command(
            "analyse", "Analyse whatever still needs it",
            "Clips with no analysis, the ones that failed, and the ones "
            "analysed against an older prompt. This is the retry `sync` tells "
            "you to run when a clip comes back FAILED.",
            run=lambda: cli.analyze(only=None, force=False, jobs=config.DEFAULT_JOBS,
                                    chunk_seconds=config.CHUNK_SECONDS),
        ),
        Command(
            "align", "Align subtitles to speech",
            "The model estimates line times and is about half a second out; "
            "silencedetect can hear where each utterance actually begins. "
            "Moves the lines only -- segment boundaries stay put.",
            run=lambda: cli.align(only=None, window=None, dry_run=False),
            check=lambda: cli.align(only=None, window=None, dry_run=True),
        ),
        Command(
            "incoming", f"{which.label} import of what is new",
            f"Writes export/{incoming} with only the clips {which.label} has "
            f"not been given yet. Import it with {which.import_menu}.",
            run=lambda: cli.export_fcpxml_cmd(
                only=None, dest=None, event=None, new=True,
                editor=which.name),
        ),
        Command(
            "rename", "Name camera-numbered clips",
            "IMG_8768 becomes something like grandpa_guessing_echo_show, out of its own "
            "analysis. Renames the original in raw/ and everything pointing "
            "at it: sidecar, proxy, chunks and segment ids.",
            run=lambda: cli.rename_cmd(only=None, set_=[], apply_=True, all_=False),
            check=lambda: cli.rename_cmd(only=None, set_=[], apply_=False, all_=False),
        ),
        Command(
            "reclassify", "Re-split tags against the vocabulary",
            "A tag you promoted or aliased in taxonomy.yaml reaches the "
            "archive, and anything quarantined that is now known comes out.",
            run=lambda: cli.reclassify(),
        ),
        Command(
            "exports", "Write every export again",
            "The whole-library FCPXML, one subtitle file per segment, and the "
            "flat cut list.",
            run=lambda: cli.export_all(only=None, editor=which.name), holds=False,
        ),
        Command(
            "thumbs", "Build every poster frame",
            "Built as you browse anyway; this is for when you would rather "
            "wait once than a tenth of a second at a time.",
            run=lambda: cli.thumbs(only=None, force=False, prune=True), holds=False,
        ),
        Command(
            "index", "Rebuild the index",
            "library.sqlite, from the sidecars. Derived, and rebuilt after "
            "every edit anyway -- this is for when index/ changed underneath.",
            run=lambda: cli.index_cmd(), holds=False,
        ),
        Command(
            "status", "Where every clip has got to",
            f"Scanned, proxied, analysed, judged, delivered to {which.label}. "
            "Reads the archive and changes nothing.",
            run=lambda: cli.status(editor=which.name), holds=False,
        ),
        Command(
            "doctor", "Check the machine around the archive",
            f"ffmpeg, agy, {which.label}, the backup, the cuts. The question "
            "after a laptop change or a drive that did not mount.",
            run=lambda: cli.doctor(editor=which.name), holds=False,
        ),
    ]
    return {command.name: command for command in found}


@dataclass
class CommandJob:
    """A command in flight, or the last one that ran, and everything it said."""

    name: str
    label: str
    check: bool
    state: str = "running"          # running | done | failed
    error: str = ""
    lines: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "name": self.name, "label": self.label, "check": self.check,
            "state": self.state, "error": self.error, "lines": list(self.lines),
        }


class Commands:
    """One command at a time, and everything it has said.

    One, because these are the things that move the archive around underneath
    each other: `rename` moves the originals in raw/ while `align` is walking
    the sidecars that name them. A second request joins the one already
    running rather than queueing, which is also what makes opening the page
    again during a long run reconnect to it instead of starting a second.
    """

    def __init__(self, writes: Writes) -> None:
        self._lock = threading.Lock()
        self._writes = writes
        self.job: CommandJob | None = None

    def menu(self, editor=None) -> dict:
        """The commands on offer and whatever is running, in one answer: the
        browser polls this while a run is going and draws its menu from it."""
        return {"commands": [c.payload() for c in commands(editor).values()],
                "job": self.status(),
                "editor": editors.get(editor).name,
                "editors": [
                    {"name": e.name, "label": e.label} for e in editors.ALL
                ]}

    def status(self) -> dict | None:
        with self._lock:
            return self.job.payload() if self.job else None

    def start(self, name: str, check: bool, after=None, editor=None) -> dict:
        import typer

        command = commands(editor).get(name)
        if command is None:
            raise KeyError(name)
        work = command.check if check else command.run
        if work is None:
            raise ValueError(f"{command.label} has no dry run")

        with self._lock:
            if self.job and self.job.state == "running":
                return self.job.payload()
            job = self.job = CommandJob(name=command.name, label=command.label, check=check)

        def line(text: str) -> None:
            with self._lock:
                job.lines.append(text)

        def run() -> None:
            echo = Echo(sys.stdout, line)
            changes = command.holds and not check
            try:
                with contextlib.redirect_stdout(echo):
                    if changes:
                        with self._writes.held(_holds(command.label)):
                            work()
                    else:
                        work()
                echo.done()
                job.state = "done"
                # It has just written sidecars nothing has mirrored yet, which
                # is also when the archive is most worth having in git.
                if changes and after:
                    after()
            except Exception as exc:
                echo.done()
                job.state = "failed"
                # These commands report their own reason -- "! proxy build
                # failed: ..." -- and then quit with `typer.Exit`, whose str()
                # is the exit code. "Exit: 1" tells you nothing the line above
                # it has not already said better.
                job.error = (
                    next((ln for ln in reversed(job.lines) if ln.strip()), "it stopped")
                    if isinstance(exc, typer.Exit)
                    else f"{type(exc).__name__}: {exc}"
                )

        threading.Thread(target=run, daemon=True, name=f"command-{name}").start()
        return self.status()


class Api:
    """The read/write surface. Kept apart from HTTP so it can be tested."""

    def __init__(self, autosave: "Autosave | None" = None) -> None:
        self.renders = Renders()
        self.draft_jobs = draft_generation.Jobs()
        self.commands = Commands(_write_lock)
        self.autosave = autosave or Autosave(after=0)

    def _wrote(self) -> None:
        """Every write ends here: rebuild the index, and start the clock on a
        backup. The index is derived and must follow immediately; the backup
        can wait for the typing to stop."""
        index.build()
        self.autosave.touch()

    def list_drafts(self) -> list[dict]:
        return [dict(id=d.id, title=d.title, angle=d.angle, updated_at=d.updated_at,
                     duration=sum(b.end - b.start for b in d.beats), beats=len(d.beats))
                for d in drafts.load_all()]

    def draft(self, draft_id: str) -> dict:
        return drafts.detail(drafts.load(draft_id))

    def create_draft(self, body: dict) -> dict:
        with _write_lock:
            draft = drafts.save(drafts.edited(body))
            self.autosave.touch()
        return drafts.detail(draft)

    def update_draft(self, draft_id: str, body: dict) -> dict:
        with _write_lock:
            current = drafts.load(draft_id)
            if current.revision != body.get("revision"):
                raise drafts.Conflict("Draft changed in another window. Reload before saving.")
            draft = drafts.save(drafts.edited(body, current), current.revision)
            self.autosave.touch()
        return drafts.detail(draft)

    def delete_draft(self, draft_id: str, body: dict) -> dict:
        with _write_lock:
            drafts.delete(draft_id, body.get("revision"))
            self.autosave.touch()
        return {"deleted": draft_id}

    def generate_drafts(self, body: dict) -> dict:
        def persist(found):
            with _write_lock:
                # A review verdict may have landed while Gemini was thinking.
                # Validate against today's sources without replacing yesterday's
                # quote snapshots: corrections must remain visible as warnings.
                sidecars = library.load_all()
                for draft in found:
                    for beat in draft.beats:
                        for excerpt in [beat, *beat.alternatives]:
                            drafts.capture(excerpt, sidecars)
                for draft in found:
                    drafts.save(draft)
                self.autosave.touch()
            return [d.id for d in found]
        return self.draft_jobs.start(body, persist)

    def export_draft(self, draft_id: str, body: dict, editor=None) -> dict:
        with _write_lock:
            draft = drafts.load(draft_id)
            if draft.revision != body.get("revision"):
                raise drafts.Conflict("Draft changed. Reload before exporting.")
            cut = drafts.snapshot_cut(draft)
            self._wrote()
        # Export is slow. Only the new snapshot is involved after releasing
        # the write lock; edits to the draft cannot change this delivery.
        result = self.export_cut(cut.slug, editor)
        return result | {"cut": cut.slug, "warnings": [w for b in drafts.detail(draft)["beats"]
                                                     for w in b["warnings"]]}

    # --- plans -----------------------------------------------------------

    # Every segment the index has, which is what a plan's moments are checked
    # and re-attached against. One query rather than 139 sidecars: this runs on
    # every search keystroke that asks for state.
    def _live(self) -> dict[str, dict]:
        try:
            rows = index.search(limit=1_000_000)
        except FileNotFoundError:  # no index yet: every moment reads as lost
            return {}
        return {r["id"]: r for r in rows}

    def _plans_and_states(self) -> tuple[list, set, dict]:
        from . import reel as store

        live = self._live()
        found = plans.load_all()
        moved = plans.reattach(found, live)
        return found, moved, plans.segment_states(found, store.load_all(), live.values())

    @staticmethod
    def _plan_json(plan, moved: set = frozenset()) -> dict:
        body = plan.model_dump(mode="json")
        for m in body["moments"]:
            m["reattached"] = (plan.id, m["id"]) in moved
        return body

    def plans(self) -> dict:
        found, moved, _ = self._plans_and_states()
        return {"exists": plans.directory().is_dir(),
                "plans": [self._plan_json(p, moved) for p in found],
                "stages": list(plans.STAGES)}

    def create_plan(self, body: dict) -> dict:
        plan = plans.create(body, live=self._live())
        self.autosave.touch()
        return self._plan_json(plan) | {"orders": self._orders()}

    def update_plan(self, plan_id: str, body: dict) -> dict:
        plan = plans.edit(plan_id, body, live=self._live())
        self.autosave.touch()
        return self._plan_json(plan) | {"orders": self._orders()}

    @staticmethod
    def _orders() -> dict[str, int]:
        """Every plan's place, since one stage change renumbers the others and
        their revisions -- deliberately -- do not say so."""
        return {p.id: p.order for p in plans.load_all()}

    def schedule(self) -> dict:
        """The ready plans in the order they go out, numbered by it.

        The number is the schedule's and nothing else's: it is a place in the
        running order, and moves when the order does.
        """
        schedule = plans.load_schedule()
        return {"revision": schedule.revision,
                "reels": [self._plan_json(p) | {"number": i}
                          for i, p in enumerate(plans.scheduled(plans.load_all(), schedule), 1)]}

    def reorder_schedule(self, body: dict) -> dict:
        plans.save_schedule(list(body.get("order") or []), body.get("revision"))
        self.autosave.touch()
        return self.schedule()

    def delete_plan(self, plan_id: str, body: dict) -> dict:
        plans.delete(plan_id, body.get("revision"))
        self.autosave.touch()
        return {"deleted": plan_id}

    def import_plans(self, body: dict) -> dict:
        found = plans.import_plans(body.get("plans") or [], live=self._live())
        self.autosave.touch()
        return {"imported": len(found)}

    def states(self) -> dict:
        _, _, states = self._plans_and_states()
        return {"segments": states, "counts": plans.counts(states)}

    def overview(self) -> dict:
        sidecars = library.load_all()
        analysed = [sc for sc in sidecars if sc.segments]
        segments = sum(len(sc.segments) for sc in analysed)
        statuses: dict[str, int] = {}
        ratings: dict[str, int] = {}
        judged = 0
        for sidecar in analysed:
            for segment in sidecar.segments:
                statuses[segment.status] = statuses.get(segment.status, 0) + 1
                ratings[segment.rating] = ratings.get(segment.rating, 0) + 1
                # Counted the way Review counts it -- a verdict or a "seen".
                if segment.rating != "none" or segment.status != "indexed":
                    judged += 1
        proposed: dict[str, int] = {}
        for sidecar in analysed:
            for segment in sidecar.segments:
                for tag in segment.proposed_tags:
                    proposed[tag] = proposed.get(tag, 0) + 1
        return {
            "clips": len(sidecars),
            "analysed": len(analysed),
            "segments": segments,
            "duration": sum(sc.asset.duration for sc in sidecars),
            "statuses": statuses,
            "ratings": ratings,
            "judged": judged,
            "proposed": proposed,
            # The vocabulary the browser offers for "who says this line", so
            # the dialog, the merge and the caption colours cannot drift apart.
            "speakers": list(speakers()),
            # What the original lines are called -- "Konkani", or "Original"
            # for a project that has not said.
            "language": config.project().language_label,
            "media_root": str(config.media_root()),
        }

    def clips(self, editor=None) -> list[dict]:
        # Delivery is per editor, so the column saying whether a clip has
        # reached one has to know which is being asked about. A clip in Final
        # Cut's library is not a clip in Resolve's media pool.
        which = editors.get(editor)
        used = self._segments_in_cuts()
        out = []
        for sidecar in library.load_all():
            ids = [s.id for s in sidecar.segments]
            delivery = sidecar.delivery(which.name)
            out.append(
                {
                    "stem": sidecar.asset.stem,
                    "file": sidecar.asset.file,
                    "original_name": sidecar.asset.original_name,
                    "duration": sidecar.asset.duration,
                    "created": sidecar.asset.created,
                    "segments": len(sidecar.segments),
                    # The frame for the first moment stands for the clip.
                    "poster": ids[0] if ids else None,
                    "used": sum(1 for i in ids if i in used),
                    "favorites": sum(1 for s in sidecar.segments if s.rating == "favorite"),
                    "chunks": sidecar.analysis.chunks,
                    "errors": sidecar.analysis.errors,
                    "exported": delivery.imported_at,
                    "stale": bool(
                        delivery.imported_at
                        and delivery.imported_sha != sidecar.content_sha()
                    ),
                }
            )
        return out

    def _segments_in_cuts(self) -> set[str]:
        from . import reel as store

        return {
            shot.segment_id
            for reel in store.load_all()
            for shot in reel.shots
            if shot.segment_id
        }

    def clip(self, stem: str) -> dict:
        sidecar = library.load_sidecar(stem)
        if sidecar is None:
            raise KeyError(stem)
        payload = sidecar.model_dump(mode="json")
        payload["labels"] = {t: Taxonomy.load().label(t) for s in sidecar.segments for t in s.tags}
        # The 480p proxy analysis already built is enough to find a trim point,
        # and it is the only thing here small enough to scrub over HTTP.
        payload["proxy"] = library.proxy_for(stem).exists()
        # So the pane can wear the same badge as the row it was opened from.
        _, _, states = self._plans_and_states()
        for seg in payload["segments"]:
            standing = states.get(seg["id"], {})
            seg["state"], seg["warn"] = standing.get("state", "unseen"), standing.get("warn", "")
        return payload

    # High enough not to be reached. A review pass reviews what this returns,
    # so a cap quietly reached is moments nobody ever looks at -- and this is
    # one person on localhost, where 2000 rows of JSON is two megabytes.
    LIMIT = 2000

    def segments(self, query: str = "", tags: list[str] | None = None,
                 status: str = "", rating: str = "", used: str = "",
                 order: str = "clip", limit: int | None = None,
                 state: str = "") -> list[dict]:
        rows = index.search(
            query=query, tags=tags or [], status=status or None,
            in_cut={"yes": True, "no": False}.get(used), limit=limit or self.LIMIT,
            order=order if order in index.ORDERS else "clip",
        )
        if rating:
            rows = [r for r in rows if r["rating"] == rating]
        _, _, states = self._plans_and_states()
        for row in rows:
            row["state"] = states.get(row["id"], {}).get("state", "unseen")
        if state:
            rows = [r for r in rows if r["state"] == state]
        return rows

    def lines(self, query: str, limit: int = 60) -> list[dict]:
        """Individual utterances matching a query, with when they were said.

        The caller says how many it wants, so that it can tell whether it got
        all of them: "add all of these as a supercut" and "add the first sixty
        of these" are different offers.
        """
        if not query.strip():
            return []
        return index.search_lines(query, limit=max(1, min(limit, 1000)))

    def add_line_to_cut(self, slug: str, body: dict) -> dict:
        """Put spoken lines into a cut, each trimmed to itself.

        The finest unit the archive has, and the one you want while
        assembling: a quote, not the two minutes it sits inside. Takes one
        line or a whole set of them -- "every time he mentions money, back to
        back" is a reel format, and doing it a line at a time would rebuild
        the index once per shot.
        """
        from . import reel as store

        wanted = body.get("lines") or [body]
        with _write_lock:
            reel = store.load(slug)
            if reel is None:
                raise KeyError(slug)
            shots = [self._shot_for_line(one) for one in wanted]

            at = body.get("at")
            where = len(reel.shots) if at is None else max(0, min(int(at), len(reel.shots)))
            reel.shots[where:where] = shots
            reel.shots = _dedupe(reel.shots)
            reel.save()
            self._wrote()
        return self._cut(reel, detail=True)

    def _shot_for_line(self, one: dict):
        """A shot cut to one utterance, snapped to where it really runs."""
        from . import reel as store, speech
        from .schema import Line

        sidecar = library.load_sidecar(one["stem"])
        if sidecar is None:
            raise KeyError(one["stem"])
        segment = next(
            (s for s in sidecar.segments if s.id == one.get("segment_id")), None
        )
        if segment is None:
            raise KeyError(one.get("segment_id"))

        spoken = Line(
            start=float(one["start"]), end=float(one["end"]),
            en=one.get("en") or "", kok=one.get("kok") or "",
        )
        shot = store.shot_from_line(sidecar, segment, spoken)
        start, end = speech.snap_range(
            speech.runs_for(sidecar.asset.stem), shot.start, shot.end
        )
        return shot.model_copy(update={
            "start": start, "end": min(end, sidecar.asset.duration),
        })

    def texts(self) -> list[dict]:
        """All prose in the archive, without the search endpoint's row limit."""
        return [
            {
                "id": segment.id,
                "stem": sidecar.asset.stem,
                "start": segment.start,
                "end": segment.end,
                "title": segment.title,
                "summary": segment.summary,
                "english": segment.english,
                "konkani": segment.konkani,
            }
            for sidecar in library.load_all()
            for segment in sidecar.segments
        ]

    def tags(self) -> list[dict]:
        from .similarity import Space

        taxonomy = Taxonomy.load()
        space = Space.load()
        out = []
        for tag in sorted(set(taxonomy.tags) | set(space.idf)):
            members = [s for s in space.segments.values() if tag in s.tags]
            out.append(
                {
                    "tag": tag,
                    "namespace": tag.split(":", 1)[0],
                    "label": taxonomy.label(tag),
                    "segments": len(members),
                    "clips": len({s.stem for s in members}),
                    "signal": round(space.idf.get(tag, 0.0), 2),
                    "in_vocabulary": tag in taxonomy.tags,
                }
            )
        return out

    def reels(self, limit: int = 12) -> list[dict]:
        from .similarity import Space

        return [
            {
                "title": reel.title,
                "tag": reel.tag,
                "coherence": round(reel.coherence, 2),
                "duration": reel.duration,
                "clips": sorted(reel.clips),
                "segments": [
                    {
                        "id": s.id, "stem": s.stem, "start": s.start, "end": s.end,
                        "duration": s.duration, "title": s.title,
                        "candidate": "quality:reel_candidate" in s.tags,
                    }
                    for s in reel.segments
                ],
            }
            for reel in Space.load().reels(limit=limit)
        ]

    # --- cuts ----------------------------------------------------------
    #
    # `reels` above are suggestions the tag space computes; these are the cuts
    # you actually made. Kept apart on purpose: one is a proposal that changes
    # whenever the vocabulary does, the other is a decision.

    def cuts(self) -> list[dict]:
        from . import reel as store

        # Read the sidecars once for the whole list: resolving each cut on its
        # own re-read all 60 of them, which is most of the request.
        sidecars = library.load_all()
        return [self._cut(r, sidecars=sidecars) for r in store.load_all()]

    def cut(self, slug: str) -> dict:
        from . import reel as store

        reel = store.load(slug)
        if reel is None:
            raise KeyError(slug)
        return self._cut(reel, detail=True)

    def _cut(self, reel, detail: bool = False, sidecars: list[Sidecar] | None = None) -> dict:
        from . import reel as store, speech

        resolved = store.resolve(reel, sidecars)
        # Only for the cut you have open: listening to every clip behind every
        # cut would make the list view wait on ffmpeg.
        heard = speech.runs_map(
            s.shot.stem for s in resolved.shots if s.sidecar
        ) if detail else None
        payload = reel.model_dump(mode="json")
        first = next((s for s in resolved.shots if s.segment), None)
        payload |= {
            "duration": resolved.duration,
            "clips": sorted(reel.clips),
            # The frame of the moment it opens on, which is what a cut looks
            # like in a list of them.
            "poster": first.segment.id if first else None,
            "problems": resolved.problems,
            "stale": reel.stale,
            "render_stale": reel.render_stale,
            # Not "notes": the reel has a `notes` field of its own, and this
            # dict is built on top of its own model_dump. Calling both by the
            # same name put the critique in the browser's Notes box, where
            # pressing Save would have written "[object Object]" over whatever
            # the cut actually said about itself.
            "observations": [
                {"shot": n.shot, "text": n.text}
                for n in store.critique(resolved, heard)
            ],
        }
        if detail:
            payload["shots"] = [
                {
                    **shot.shot.model_dump(mode="json"),
                    "offset": shot.offset,
                    "duration": shot.duration,
                    "display_title": shot.title,
                    "problem": shot.problem,
                    "clip_duration": shot.sidecar.asset.duration if shot.sidecar else None,
                    "proxy": library.proxy_for(shot.shot.stem).exists(),
                    "segment_start": shot.segment.start if shot.segment else None,
                    "segment_end": shot.segment.end if shot.segment else None,
                    # The whole moment, not only what the trim holds: the
                    # line you want when you are trimming is usually just
                    # outside it, and you cannot click one that is not shown.
                    "lines": [
                        line.model_dump(mode="json")
                        | {"inside": line.end > shot.start and line.start < shot.end}
                        for line in shot.moment_lines
                    ],
                }
                for shot in resolved.shots
            ]
        return payload

    def create_cut(self, body: dict) -> dict:
        from . import reel as store

        title = (body.get("title") or "").strip()
        if not title:
            raise ValueError("a reel needs a title")

        with _write_lock:
            shots = [self._shot_for(sid) for sid in body.get("segments") or []]
            reel = store.create(title, shots=_dedupe(shots), hook=body.get("hook", ""))
            self._wrote()
        return self._cut(reel, detail=True)

    def update_cut(self, slug: str, patch: dict) -> dict:
        """Replace the parts of a cut named in the patch.

        `shots` arrives as the whole running order rather than as a diff, so
        reordering, trimming and removing are one operation and cannot half
        apply -- there is no position to race against.
        """
        from . import reel as store

        with _write_lock:
            reel = store.load(slug)
            if reel is None:
                raise KeyError(slug)

            fields = {
                k: v for k, v in patch.items()
                if k in {"title", "status", "hook", "notes"}
            }
            if "shots" in patch:
                fields["shots"] = [store.Shot.model_validate(s) for s in patch["shots"]]

            updated = reel.model_copy(update=fields)
            store.Reel.model_validate(updated.model_dump())  # reject a bad status
            updated.save()
            self._wrote()
        return self._cut(updated, detail=True)

    def add_to_cut(self, slug: str, body: dict) -> dict:
        """Put a segment into a cut -- the move the segment view is there for."""
        from . import reel as store

        with _write_lock:
            reel = store.load(slug)
            if reel is None:
                raise KeyError(slug)
            # One segment, or a whole search result. Adding a set of moments in
            # one go is how a cut actually starts.
            wanted = body.get("segments") or [body["segment"]]
            at = body.get("at")
            where = len(reel.shots) if at is None else max(0, min(int(at), len(reel.shots)))
            reel.shots[where:where] = [self._shot_for(sid) for sid in wanted]
            reel.shots = _dedupe(reel.shots)
            reel.save()
            self._wrote()
        return self._cut(reel, detail=True)

    def _shot_for(self, segment_id: str):
        from . import reel as store

        stem = segment_id.split("#", 1)[0]
        sidecar = library.load_sidecar(stem)
        if sidecar is None:
            raise KeyError(stem)
        segment = next((s for s in sidecar.segments if s.id == segment_id), None)
        if segment is None:
            raise KeyError(segment_id)
        return store.shot_from_segment(sidecar, segment)

    def duplicate_cut(self, slug: str, body: dict | None = None) -> dict:
        """Copy a cut so a shorter one can be tried without losing this one.

        Which is the move a long cut asks for: `money` runs 5:49 against the
        100 seconds a reel gets watched for, and finding the 100 means
        throwing eight things away and wanting two of them back.
        """
        from . import reel as store

        with _write_lock:
            reel = store.load(slug)
            if reel is None:
                raise KeyError(slug)
            copy = store.create(
                (body or {}).get("title") or f"{reel.title} (again)",
                shots=[shot.model_copy(deep=True) for shot in reel.shots],
                hook=reel.hook, notes=reel.notes,
            )
            self._wrote()
        return self._cut(copy, detail=True)

    def delete_cut(self, slug: str) -> dict:
        from . import reel as store

        with _write_lock:
            trash.keep(store.path_for(slug), reason=f"deleted the cut {slug}")
            if not store.delete(slug):
                raise KeyError(slug)
            self._wrote()
        return {"deleted": slug}

    def export_cut(self, slug: str, editor=None) -> dict:
        from . import export_reel, loudness, reel as store

        which = editors.get(editor)
        reel = store.load(slug)
        if reel is None:
            raise KeyError(slug)
        if not reel.shots:
            raise ValueError("nothing to export: the cut is empty")
        gains = loudness.gains_for(store.resolve(reel), ceiling=loudness.CEILING_DB)
        path, resolved = export_reel.write(reel, gains=gains, editor=which)
        return {
            "path": str(path),
            "dir": str(path.parent),
            "shots": len(resolved.shots),
            "duration": resolved.duration,
            "problems": resolved.problems,
            # The page says what to do next, and the two menu paths are not
            # interchangeable -- the sentence is the editor's, not the page's.
            "editor": which.name,
            "editor_label": which.label,
            "import_menu": which.import_menu,
            "captions_menu": which.captions_menu,
            "captions_on_timeline": which.clip_captions,
        }

    def snap_cut(self, slug: str, body: dict | None = None) -> dict:
        """Pull every shot to where somebody starts and stops talking.

        The in-points come from the model's segment boundaries, which are
        estimates good to about a second -- so every shot added to a cut opens
        on a guess. This is the one-button correction, and the previous cut
        goes to the trash first, because it is a decision and decisions are
        the only thing here nothing can regenerate.
        """
        from . import reel as store, speech

        window = float((body or {}).get("window") or speech.WINDOW)
        # Listen outside the lock: a cold clip takes a tenth of a second to
        # measure, and every other write waits behind this one.
        opening = store.load(slug)
        if opening is None:
            raise KeyError(slug)
        heard = speech.runs_map(shot.stem for shot in opening.shots)

        with _write_lock:
            reel = store.load(slug)
            if reel is None:
                raise KeyError(slug)

            moved, shots = 0, []
            for shot in reel.shots:
                runs = heard.get(shot.stem) or speech.runs_for(shot.stem)
                start, end = speech.snap_range(runs, shot.start, shot.end, window)
                sidecar = library.load_sidecar(shot.stem)
                if sidecar:
                    end = min(end, sidecar.asset.duration)
                if abs(start - shot.start) > 0.01 or abs(end - shot.end) > 0.01:
                    moved += 1
                    shot = shot.model_copy(update={"start": start, "end": end})
                shots.append(shot)

            if moved:
                trash.keep(store.path_for(slug), reason=f"snapped {slug} to speech")
                reel.shots = shots
                reel.save()
                self._wrote()
        return self._cut(reel, detail=True) | {"moved": moved}

    # --- rendering -----------------------------------------------------

    def render_cut(self, slug: str, body: dict | None = None) -> dict:
        """Start a render, and hand back something to poll.

        The work happens on a thread because a render is tens of seconds and
        the browser has to stay usable -- and because the interesting thing to
        show while it runs is how far in it is, which needs somewhere to put
        the number.
        """
        from . import loudness, reel as store, render as engine

        reel = store.load(slug)
        if reel is None:
            raise KeyError(slug)
        if not reel.shots:
            raise ValueError("nothing to render: the cut is empty")

        options = body or {}
        source = options.get("source") or "raw"
        lang = options.get("lang") or "en"
        captions = options.get("captions") or "soft"
        fit = bool(options.get("fit"))

        def work(job: Job) -> None:
            fresh = store.load(slug)
            resolved = store.resolve(fresh)
            job.total = engine.build_plan(
                resolved, engine.out_dir() / f"{slug}.mp4", source=source,
            ).duration
            gains = loudness.gains_for(resolved) if options.get("level", True) else {}
            result = engine.render(
                fresh, source=source, lang=lang, captions=captions, fit=fit,
                gains=gains, resolved=resolved,
                on_progress=lambda seconds: setattr(job, "done", seconds),
            )
            job.done, job.size = job.total, result.size_bytes

        return self.renders.start(slug, work).payload()

    def render_state(self, slug: str) -> dict:
        """What there is to watch: a render in flight, or a file on disk."""
        from . import reel as store, render as engine

        reel = store.load(slug)
        if reel is None:
            raise KeyError(slug)
        path = engine.out_dir() / f"{slug}.mp4"
        job = self.renders.get(slug)
        payload = job.payload() if job else {"slug": slug, "state": "none"}
        payload |= {
            "exists": path.exists(),
            "stale": reel.render_stale,
            "rendered_at": reel.rendered_at,
            "rendered_captions": reel.rendered_captions,
            "path": str(path) if path.exists() else "",
            "bytes": path.stat().st_size if path.exists() else 0,
        }
        return payload

    def captions(self, slug: str, lang: str = "en") -> str:
        """A cut's captions as WebVTT, rebased onto the cut.

        Computed rather than served off disk: the cut moves under you while
        you trim it, and a caption file from before the last trim would sit
        half a shot out.
        """
        from . import export_reel, export_srt, reel as store

        reel = store.load(slug)
        if reel is None:
            raise KeyError(slug)
        return export_srt.to_vtt(export_reel.srt(store.resolve(reel), lang=lang))

    def caption_timeline(self, body: dict) -> dict:
        """Captions for a cut assembled by hand, from the XML the editor wrote.

        The document arrives in the request rather than as a path: the browser
        is handed a dropped file, and a dropped file has a name but no
        directory. Nothing is written -- the page offers the .srt as a
        download, and the editor takes it from wherever that landed.

        Which editor wrote it is not asked, because it does not need to be:
        the reader walks both dialects. The editor only decides which menu
        path the page tells you to use afterwards.
        """
        from . import export_reel, import_fcpxml

        which = editors.get((body or {}).get("editor"))
        xml = (body or {}).get("xml") or ""
        if not xml.strip():
            raise ValueError(
                f"no document: drop the .fcpxml {which.label} exported"
            )
        lang = (body or {}).get("lang") or "en"

        cut = import_fcpxml.read(xml)
        return {
            "project": cut.project,
            "editor": which.name,
            "captions_menu": which.captions_menu,
            "slug": cut.reel.slug,
            "duration": cut.duration,
            "shots": [
                {
                    "stem": s.shot.stem,
                    "offset": s.offset,
                    "start": s.shot.start,
                    "end": s.shot.end,
                    "lane": s.lane,
                    "lines": len(s.lines),
                    "problem": s.problem,
                }
                for s in cut.shots
            ],
            "skipped": cut.skipped,
            "problems": cut.problems,
            "captions": [
                {"start": a, "end": b,
                 "text": "\n".join(export_reel.caption_texts(en, kok, lang))}
                for a, b, en, kok in export_reel.caption_lines(cut)
            ],
            "srt": export_reel.srt(cut, lang=lang),
            # One body per voice. An .srt has nowhere to put a speaker, so an
            # editor that imports one can colour a track and not a line; the
            # split is how the separation survives the trip out of here.
            "by_speaker": export_reel.srt_by_speaker(cut, lang=lang),
        }

    def cuts_using(self, segment_id: str) -> list[dict]:
        """Which cuts already use this moment, so you do not add it twice."""
        from . import reel as store

        return [
            {"slug": reel.slug, "title": reel.title}
            for reel in store.load_all()
            if any(shot.segment_id == segment_id for shot in reel.shots)
        ]

    def related(self, segment_id: str, limit: int = 10) -> list[dict]:
        from .similarity import Space

        space = Space.load()
        if segment_id not in space.segments:
            raise KeyError(segment_id)
        return [
            {
                "score": round(score, 2), "id": s.id, "stem": s.stem,
                "start": s.start, "end": s.end, "title": s.title, "via": shared,
            }
            for score, s, shared in space.related(segment_id, limit=limit)
        ]

    # --- writes --------------------------------------------------------

    EDITABLE = {"title", "summary", "konkani", "english", "rating", "status"}

    def update_segment(self, segment_id: str, patch: dict) -> dict:
        stem = segment_id.split("#", 1)[0]
        with _write_lock:
            sidecar = library.load_sidecar(stem)
            if sidecar is None:
                raise KeyError(stem)
            segment = next((s for s in sidecar.segments if s.id == segment_id), None)
            if segment is None:
                raise KeyError(segment_id)

            fields = {k: v for k, v in patch.items() if k in self.EDITABLE}
            if "tags" in patch:
                known, proposed = Taxonomy.load().split(list(patch["tags"]))
                fields["tags"], fields["proposed_tags"] = known, proposed
            if "lines" in patch:
                fields |= self._edited_lines(segment, patch, fields)

            # A rating is a keystroke in review and happens hundreds of times an
            # hour; prose and tags are what cost hours to get right. Only the
            # second kind is worth keeping a copy of.
            if set(fields) - {"rating", "status"}:
                trash.keep(config.sidecar_path(stem), reason=f"edited {segment_id}")

            updated = segment.model_copy(update=fields)
            Segment.model_validate(updated.model_dump())  # reject a bad rating/status
            sidecar.segments[sidecar.segments.index(segment)] = updated
            sidecar.save(config.sidecar_path(stem))
            self._wrote()
            return updated.model_dump(mode="json") | {"undo": self._undo_patch(segment, updated)}

    @staticmethod
    def _undo_patch(before: Segment, after: Segment) -> dict:
        """The patch that puts back what this save changed, and nothing else.

        Worked out here rather than in the browser, because the browser does
        not know everything a save touched: a corrected line rewrites the
        prose, and an unknown tag moves into `proposed_tags`. Sending that
        patch back returns this same shape for it, which is the redo.
        """
        old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
        patch = {k: old[k] for k in ("title", "summary", "konkani", "english",
                                     "rating", "status", "lines") if old[k] != new[k]}
        if (old["tags"], old["proposed_tags"]) != (new["tags"], new["proposed_tags"]):
            patch["tags"] = old["tags"] + old["proposed_tags"]
        return patch

    @staticmethod
    def _edited_lines(segment: Segment, patch: dict, fields: dict) -> dict:
        """Corrected subtitle lines, and the prose that has to follow them.

        The critique's advice for a caption nobody can read is "a shorter
        translation", and until now the only translation you could shorten was
        the segment's prose -- which is not what a caption is made of.

        `english` and `konkani` are the same words as the lines, joined; the
        model writes them that way. So a corrected line updates the prose too,
        unless the same save also edited the prose by hand, in which case what
        was typed there wins.
        """
        from .schema import Line

        lines = [Line.model_validate(line) for line in patch["lines"]]
        out: dict = {"lines": lines}
        for key, attr in (("english", "en"), ("konkani", "kok")):
            current = fields.get(key, getattr(segment, key))
            # The model writes the prose and the lines separately, and joins
            # them with whichever separator it felt like: of 163 segments here,
            # 19 are newline-joined and 76 space-joined. Comparing only against
            # newlines meant this fired for 12% of segments, so a corrected
            # caption almost never reached the prose that search reads.
            for sep in ("\n", " "):
                if current == sep.join(getattr(line, attr) for line in segment.lines):
                    out[key] = sep.join(getattr(line, attr) for line in lines).strip()
                    break
        return out

    def delete_segment(self, segment_id: str) -> dict:
        stem = segment_id.split("#", 1)[0]
        with _write_lock:
            sidecar = library.load_sidecar(stem)
            if sidecar is None:
                raise KeyError(stem)
            was = sidecar.segments
            sidecar.segments = [s for s in sidecar.segments if s.id != segment_id]
            if len(sidecar.segments) == len(was):
                raise KeyError(segment_id)
            gone = next(s for s in was if s.id == segment_id)
            trash.keep(config.sidecar_path(stem), reason=f"deleted {segment_id}")
            sidecar.save(config.sidecar_path(stem))
            self._wrote()
            # The whole segment, so the page can undo the delete without the
            # trash: `greels trash restore` puts back the whole sidecar, and
            # with it undoes every correction made to the clip since.
            return {"deleted": segment_id, "remaining": len(sidecar.segments),
                    "segment": gone.model_dump(mode="json")}

    def restore_segment(self, body: dict) -> dict:
        """A deleted segment, put back where it was.

        Only under an id nothing holds: a second segment with the same start
        is not a restore, it is a duplicate, and would be one the index then
        has to pick between.
        """
        segment = Segment.model_validate(body.get("segment") or {})
        stem = segment.id.split("#", 1)[0]
        with _write_lock:
            sidecar = library.load_sidecar(stem)
            if sidecar is None:
                raise KeyError(stem)
            if any(s.id == segment.id for s in sidecar.segments):
                raise drafts.Conflict(f"{segment.id} is already there.")
            sidecar.segments = sorted([*sidecar.segments, segment], key=lambda s: s.start)
            sidecar.save(config.sidecar_path(stem))
            self._wrote()
            return segment.model_dump(mode="json")

    def command_menu(self, editor=None) -> dict:
        """What the header's menu offers, and what is running right now."""
        return self.commands.menu(editor)

    def run_command(self, body: dict) -> dict:
        """Start one of them, or its dry run.

        Handed back immediately with something to poll: the shortest of these
        reads every sidecar, and the longest is agy.
        """
        return self.commands.start(
            body.get("name", ""), bool(body.get("check")),
            after=self.autosave.touch, editor=body.get("editor"))

    def health(self, editor=None) -> dict:
        """The same checks `greels doctor` prints, for the header to draw.

        The evenings happen in the browser, so a drive that did not mount, an
        index behind the sidecars or transcripts that are in one place only
        were things you found out by remembering to run something. 52 ms on
        this archive, which is cheap enough to ask for on every page load and
        after every command rather than on a timer.
        """
        from . import health

        return health.checkup(editor).payload()

    def backup(self) -> dict:
        """Mirror the sidecars and cuts into the repo.

        A refusal is reported rather than raised past the caller: this is a
        button, and "the media root looks wrong" is the answer, not a stack
        trace. Forcing it is deliberately only possible from the CLI.
        """
        from . import backup as store

        try:
            added, updated, removed = store.backup()
        except store.Refused as exc:
            return {"refused": str(exc), "added": 0, "updated": 0, "removed": 0}
        return {"added": added, "updated": updated, "removed": removed}


class Handler(BaseHTTPRequestHandler):
    server_version = "greels"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, api: Api, **kwargs):
        self.api = api
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # quieter than the default
        pass

    # --- plumbing ------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # The whole UI is one HTML file that is rewritten every time anything
        # here changes, and it went out with no cache headers at all -- so a
        # browser applied its own heuristic and a tab left open across an edit
        # kept serving yesterday's page back. Nothing this sends is worth
        # caching: the JSON is the archive as it stands this second. Poster
        # frames and proxies write their own headers and keep theirs.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _thumb(self, segment_id: str) -> None:
        """A poster frame, built on first ask.

        Lazily rather than in a batch: the browser only requests what is on
        screen, so a first look at the archive costs a handful of frames
        instead of all 192.
        """
        from . import thumbs

        path = thumbs.ensure(segment_id)
        if path is None:
            self._json({"error": "no frame for that segment"}, 404)
            return
        # Short: a re-analysis that moves a boundary changes the id, so the
        # only staleness left is a rebuilt frame for the same range.
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "max-age=120")
        body = path.read_bytes()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, stem: str) -> None:
        """A clip's proxy, by name lookup rather than by joining the path:
        `../` in a URL should not be able to reach anything, localhost or not."""
        path = library.proxy_for(stem)
        if "/" in stem or "\\" in stem or not path.is_file():
            self._json({"error": "no proxy for that clip"}, 404)
            return
        self._video(path)

    def _render(self, slug: str) -> None:
        """A rendered cut, so it can be watched in the tab it was made in."""
        from . import render as engine

        path = engine.out_dir() / f"{slug}.mp4"
        if "/" in slug or "\\" in slug or not path.is_file():
            self._json({"error": "that cut has not been rendered"}, 404)
            return
        self._video(path)

    def _video(self, path: Path) -> None:
        """Stream a file, honouring Range so the browser can seek.

        Without 206 the browser downloads the whole thing to move the playhead
        one second, which makes scrubbing to a trim point unusable. A proxy is
        ~1 MB, so this is cheap either way -- but a rendered cut is 50 MB, and
        there it is the difference between seeking and waiting.
        """
        size = path.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range", "")
        partial = header.startswith("bytes=")
        if partial:
            first, _, last = header[len("bytes="):].partition("-")
            start = int(first) if first else 0
            end = int(last) if last else size - 1
            start, end = max(0, start), min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the player seeked away; not an error
                remaining -= len(chunk)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    # --- routes --------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        route = url.path
        query = parse_qs(url.query)

        try:
            if route in ("/", "/index.html") or route.rstrip("/") in PAGES:
                self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif route.startswith("/media/"):
                self._proxy(unquote(route[len("/media/"):]).removesuffix(".mp4"))
            elif route.startswith("/captions/"):
                slug = unquote(route[len("/captions/"):]).removesuffix(".vtt")
                self._send(200, self.api.captions(
                    slug, (query.get("lang") or ["en"])[0]).encode(),
                    "text/vtt; charset=utf-8")
            elif route.startswith("/render/"):
                self._render(unquote(route[len("/render/"):]).removesuffix(".mp4"))
            elif route.startswith("/thumb/"):
                self._thumb(unquote(route[len("/thumb/"):]))
            elif route == "/api/overview":
                self._json(self.api.overview())
            elif route == "/api/plans":
                self._json(self.api.plans())
            elif route == "/api/states":
                self._json(self.api.states())
            elif route == "/api/schedule":
                self._json(self.api.schedule())
            elif route == "/api/plans/seed":
                path = config.schedule_seed_path()
                self._json(json.loads(path.read_text()) if path.exists() else None)
            elif route == "/api/commands":
                self._json(self.api.command_menu(query.get("editor", [None])[0]))
            elif route == "/api/health":
                self._json(self.api.health(query.get("editor", [None])[0]))
            elif route == "/api/clips":
                self._json(self.api.clips(query.get("editor", [None])[0]))
            elif route.startswith("/api/clip/"):
                self._json(self.api.clip(unquote(route[len("/api/clip/"):])))
            elif route == "/api/segments":
                self._json(
                    self.api.segments(
                        query=(query.get("q") or [""])[0],
                        tags=query.get("tag") or [],
                        status=(query.get("status") or [""])[0],
                        rating=(query.get("rating") or [""])[0],
                        used=(query.get("used") or [""])[0],
                        order=(query.get("order") or ["clip"])[0],
                        state=(query.get("state") or [""])[0],
                    )
                )
            elif route == "/api/lines":
                self._json(self.api.lines(
                    (query.get("q") or [""])[0],
                    int((query.get("limit") or ["60"])[0]),
                ))
            elif route == "/api/texts":
                self._json(self.api.texts())
            elif route == "/api/tags":
                self._json(self.api.tags())
            elif route == "/api/drafts":
                self._json(self.api.list_drafts())
            elif route == "/api/drafts/generation":
                self._json(self.api.draft_jobs.status())
            elif route.startswith("/api/draft/"):
                self._json(self.api.draft(unquote(route[len("/api/draft/"):])))
            elif route == "/api/reels":
                self._json(self.api.reels())
            elif route == "/api/cuts":
                self._json(self.api.cuts())
            elif route.startswith("/api/cut/") and route.endswith("/render"):
                self._json(self.api.render_state(
                    unquote(route[len("/api/cut/"):-len("/render")])))
            elif route.startswith("/api/cut/"):
                self._json(self.api.cut(unquote(route[len("/api/cut/"):])))
            elif route.startswith("/api/using/"):
                self._json(self.api.cuts_using(unquote(route[len("/api/using/"):])))
            elif route.startswith("/api/related/"):
                self._json(self.api.related(unquote(route[len("/api/related/"):])))
            else:
                self._json({"error": "not found"}, 404)
        except drafts.Conflict as exc:
            self._json({"error": str(exc)}, 409)
        except KeyError as exc:
            self._json({"error": f"not found: {exc}"}, 404)
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 503)
        except Exception as exc:  # a broken sidecar should not kill the server
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_PATCH(self) -> None:
        route = urlparse(self.path).path
        try:
            if route.startswith("/api/plan/"):
                self._json(self.api.update_plan(unquote(route[len("/api/plan/"):]), self._body()))
            elif route.startswith("/api/draft/"):
                self._json(self.api.update_draft(unquote(route[len("/api/draft/"):]), self._body()))
            elif route.startswith("/api/segment/"):
                segment_id = unquote(route[len("/api/segment/"):])
                self._json(self.api.update_segment(segment_id, self._body()))
            elif route.startswith("/api/cut/"):
                self._json(self.api.update_cut(unquote(route[len("/api/cut/"):]), self._body()))
            else:
                self._json({"error": "not found"}, 404)
        except drafts.Conflict as exc:
            self._json({"error": str(exc)}, 409)
        except KeyError as exc:
            self._json({"error": f"not found: {exc}"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def do_PUT(self) -> None:
        route = urlparse(self.path).path
        try:
            if route == "/api/schedule":
                self._json(self.api.reorder_schedule(self._body()))
            else:
                self._json({"error": "not found"}, 404)
        except drafts.Conflict as exc:
            self._json({"error": str(exc)}, 409)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        try:
            if route == "/api/plans/import":
                self._json(self.api.import_plans(self._body()), 201)
            elif route == "/api/plans":
                self._json(self.api.create_plan(self._body()), 201)
            elif route == "/api/drafts/generate":
                self._json(self.api.generate_drafts(self._body()), 202)
            elif route == "/api/drafts":
                self._json(self.api.create_draft(self._body()), 201)
            elif route.startswith("/api/draft/") and route.endswith("/export"):
                body = self._body()
                self._json(self.api.export_draft(
                    unquote(route[len("/api/draft/"):-len("/export")]), body,
                    body.get("editor")))
            elif route == "/api/segments":
                self._json(self.api.restore_segment(self._body()), 201)
            elif route == "/api/backup":
                self._json(self.api.backup())
            elif route == "/api/commands":
                self._json(self.api.run_command(self._body()), 202)
            elif route == "/api/captions":
                self._json(self.api.caption_timeline(self._body()))
            elif route == "/api/cuts":
                self._json(self.api.create_cut(self._body()), 201)
            elif route.endswith("/export") and route.startswith("/api/cut/"):
                slug = unquote(route[len("/api/cut/"):-len("/export")])
                self._json(self.api.export_cut(slug, self._body().get("editor")))
            elif route.endswith("/duplicate") and route.startswith("/api/cut/"):
                slug = unquote(route[len("/api/cut/"):-len("/duplicate")])
                self._json(self.api.duplicate_cut(slug, self._body()), 201)
            elif route.endswith("/snap") and route.startswith("/api/cut/"):
                slug = unquote(route[len("/api/cut/"):-len("/snap")])
                self._json(self.api.snap_cut(slug, self._body()))
            elif route.endswith("/render") and route.startswith("/api/cut/"):
                slug = unquote(route[len("/api/cut/"):-len("/render")])
                self._json(self.api.render_cut(slug, self._body()))
            elif route.endswith("/line") and route.startswith("/api/cut/"):
                slug = unquote(route[len("/api/cut/"):-len("/line")])
                self._json(self.api.add_line_to_cut(slug, self._body()))
            elif route.endswith("/shots") and route.startswith("/api/cut/"):
                slug = unquote(route[len("/api/cut/"):-len("/shots")])
                self._json(self.api.add_to_cut(slug, self._body()))
            else:
                self._json({"error": "not found"}, 404)
        except drafts.Conflict as exc:
            self._json({"error": str(exc)}, 409)
        except KeyError as exc:
            self._json({"error": f"not found: {exc}"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        try:
            if route.startswith("/api/plan/"):
                self._json(self.api.delete_plan(unquote(route[len("/api/plan/"):]), self._body()))
            elif route.startswith("/api/draft/"):
                self._json(self.api.delete_draft(unquote(route[len("/api/draft/"):]), self._body()))
            elif route.startswith("/api/segment/"):
                self._json(self.api.delete_segment(unquote(route[len("/api/segment/"):])))
            elif route.startswith("/api/cut/"):
                self._json(self.api.delete_cut(unquote(route[len("/api/cut/"):])))
            else:
                self._json({"error": "not found"}, 404)
        except drafts.Conflict as exc:
            self._json({"error": str(exc)}, 409)
        except KeyError as exc:
            self._json({"error": f"not found: {exc}"}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)


def serve(
    port: int = 8722,
    host: str = "127.0.0.1",
    autosave: float = AUTOSAVE_AFTER,
    on_message=print,
) -> ThreadingHTTPServer:
    """Localhost only. This process writes to the sidecars; do not expose it."""
    api = Api(autosave=Autosave(after=autosave, on_message=on_message))
    httpd = ThreadingHTTPServer((host, port), partial(Handler, api=api))
    # Reachable by whoever started it, so the flush on the way out is theirs
    # to make rather than something buried in a handler.
    httpd.autosave = api.autosave
    return httpd
