"""Whether the machine around the archive still works.

`status` says where the footage has got to. This says whether the things it
depends on are where they should be -- the question after a laptop change, an
OS upgrade, or a drive that did not mount.

It produces checks rather than printing them, because there are two places
that ask. `splice doctor` renders them as lines; the browser draws the ones
that are not `ok` in its header, which is where they are actually read: the
evenings happen in the browser, and a warning only a terminal shows is a
warning nobody sees. A drive that did not mount should not have to be noticed
by remembering to run something.

Each check names the command that would address it where one exists, so the
browser can offer the fix beside the complaint rather than leaving you to work
out that "line timings sit off the speech" means `align`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config, library
from .schema import Sidecar

# What a check is worth. `problem` is the only one that makes `doctor` exit
# non-zero, and the only one the browser interrupts anybody for.
OK = "ok"
NOTE = "note"
PROBLEM = "problem"


@dataclass(frozen=True)
class Check:
    section: str
    label: str
    detail: str
    level: str = OK
    # A command in the browser's Run menu that would deal with this, if one
    # would. Names, not sentences: the menu already carries the sentence.
    fix: str = ""

    def payload(self) -> dict:
        return {"section": self.section, "label": self.label,
                "detail": self.detail, "level": self.level, "fix": self.fix}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    vocabulary: int = 0

    def add(self, section, label, detail, level=OK, fix="") -> None:
        self.checks.append(Check(section, label, detail, level, fix))

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.level == PROBLEM]

    @property
    def notes(self) -> list[Check]:
        return [c for c in self.checks if c.level == NOTE]

    def payload(self) -> dict:
        return {
            "checks": [c.payload() for c in self.checks],
            "problems": len(self.problems),
            "notes": len(self.notes),
            "vocabulary": self.vocabulary,
        }


# Proxies, poster frames and renders all land on this disk and a render is a
# couple of hundred megabytes. Worth saying before it is the thing that stops
# a run halfway. A note and not a failure: a disk filling up is not a broken
# machine, and `doctor` exits non-zero only for those. The browser shows notes
# too, which is the point of this being somewhere other than a terminal.
SPACE_NOTE_GB = 10


def checkup(editor=None) -> Report:
    """Every check, in the order `doctor` has always printed them.

    `editor` decides which program's library is the one being asked about, and
    nothing else: the tools, the disk and the cuts are the same question
    whichever editor the footage is headed for.
    """
    from . import backup as store, editors, export_fcpxml, reel as reels
    from . import captions
    from .taxonomy import Taxonomy

    which = editors.get(editor)
    report = Report()

    for tool, why in (
        ("ffmpeg", "proxies, chunks and poster frames"),
        ("ffprobe", "durations and frame rates"),
    ):
        path = shutil.which(tool)
        if path:
            report.add("tools", tool, path)
        else:
            report.add("tools", tool, f"not on PATH; needed for {why}", PROBLEM)

    agy = shutil.which(config.AGY_BIN)
    report.add("tools", config.AGY_BIN, agy) if agy else report.add(
        "tools", config.AGY_BIN,
        "not on PATH; everything but `analyze` still works", NOTE,
    )

    if shutil.which("xmllint"):
        report.add("tools", "xmllint", "exports are checked before they are written")
    else:
        report.add("tools", "xmllint",
                   "missing, so FCPXML cannot be validated before writing", NOTE)

    font = captions.font_path()
    if captions.available():
        report.add("tools", "captions", f"burnt in with {Path(font).stem}")
    elif font is None:
        report.add("tools", "captions",
                   "no usable font; `--captions burn` cannot draw them", NOTE)
    else:
        report.add("tools", "captions",
                   "Pillow is not installed; `--captions burn` cannot draw them", NOTE)

    # Apple ships the schemas inside Final Cut, and they are what validates
    # *both* editors' documents -- Resolve's included, since 1.10 is Apple's
    # own version and Blackmagic publishes nothing to check against. So a
    # machine with no Final Cut on it writes Resolve exports unchecked, which
    # is worth saying out loud on the machine this archive is moving to.
    dtd = export_fcpxml.dtd_path(which.fcpxml_version)
    label = f"Apple's FCPXML {which.fcpxml_version} DTD"
    if dtd:
        report.add("tools", label, str(dtd))
    else:
        report.add("tools", label,
                   "Final Cut not installed; exports go out unchecked", NOTE)

    if which.name == editors.RESOLVE:
        from . import resolve as bridge

        if not bridge.installed():
            report.add("tools", "Resolve scripting API",
                       "not found; splice cannot see the media pool or send a "
                       "timeline. `splice imported --editor resolve` sets "
                       "delivery by hand", NOTE)
        elif bridge.connect() is None:
            # The ordinary case rather than a fault: Resolve answers only while
            # it is running, and nobody leaves it open to browse an archive.
            report.add("tools", "Resolve scripting API",
                       "installed; Resolve is not running, so its media pool "
                       "cannot be read right now")
        else:
            report.add("tools", "Resolve scripting API", "connected")

    project = config.project()
    settings = config.project_path()
    who = ", ".join(p.label for p in project.people) or "nobody named yet"
    if settings.is_file():
        report.add("project", project.name,
                   f"{project.language_label}; {who} ({settings})")
    else:
        report.add("project", project.name,
                   f"no {settings.name} at {settings.parent}, so nobody is named "
                   "and the footage is described generically", NOTE)

    root = config.media_root()
    if not root.is_dir():
        report.add("storage", "media root",
                   f"{root} is not there -- an unmounted volume?", PROBLEM)
    else:
        report.add("storage", "media root", str(root))
        for name, path in (
            ("raw/", config.raw_dir()), ("index/", config.index_dir()),
            ("proxies", config.proxy_dir()),
        ):
            if path.is_dir():
                report.add("storage", name, str(path))
            else:
                report.add("storage", name, f"{path} does not exist yet", NOTE)

        free = shutil.disk_usage(root).free / 1e9
        detail = f"{free:.0f} GB free where the footage lives"
        report.add("storage", "space", detail,
                   OK if free > SPACE_NOTE_GB else NOTE)

    sidecars = library.load_all() if config.index_dir().is_dir() else []
    clips = library.discover_raw()
    missing = [
        sc.asset.stem for sc in sidecars
        if not (config.raw_dir() / sc.asset.file).exists()
    ]
    if missing:
        report.add("storage", "footage",
                   f"{len(missing)} sidecar(s) name a file raw/ does not have: "
                   + ", ".join(missing[:3]), NOTE)
    elif sidecars:
        report.add("storage", "footage",
                   f"{len(sidecars)} sidecar(s), all pointing at a file in raw/")
    if len(clips) > len(sidecars):
        report.add("storage", "scanning",
                   f"{len(clips) - len(sidecars)} clip(s) in raw/ have no sidecar; "
                   "run `splice scan`", NOTE, fix="sync")

    if not config.db_path().exists():
        report.add("derived", "index", "no library.sqlite; run `splice index`",
                   NOTE, fix="index")
    else:
        try:
            from .index import connect

            conn = connect()
            indexed = conn.execute("SELECT count(*) FROM segments").fetchone()[0]
            conn.close()
            live = sum(len(sc.segments) for sc in sidecars)
            if indexed != live:
                report.add("derived", "index",
                           f"holds {indexed} segments, the sidecars have {live}; "
                           "run `splice index`", NOTE, fix="index")
            else:
                report.add("derived", "index", f"{indexed} segments, current")
        except Exception as exc:
            report.add("derived", "index", f"unreadable: {exc}", PROBLEM, fix="index")

    try:
        off = lines_off_the_speech(sidecars)
        if off:
            report.add("derived", "subtitles",
                       f"{off} line timing(s) sit off the speech; `splice align`",
                       NOTE, fix="align")
        else:
            report.add("derived", "subtitles",
                       "every line sits on the speech it describes")
    except Exception as exc:
        report.add("derived", "subtitles", f"could not be checked: {exc}", NOTE)

    try:
        fresh, only_saved, differing = store.drift()
        if fresh or differing or only_saved:
            # The one thing here that nothing regenerates, sitting in exactly
            # one place. Worth more than a note once it is transcripts.
            report.add("derived", "backup",
                       f"{len(fresh)} new, {len(differing)} changed, "
                       f"{len(only_saved)} stale; run `splice backup`", NOTE,
                       fix="backup")
        else:
            report.add("derived", "backup", "current")
    except store.NoArchive as exc:
        # Unset is a choice a scratch project makes on purpose, so it is said
        # rather than counted against the machine.
        report.add("derived", "backup", f"not taken: {exc}", NOTE)
    except Exception as exc:
        report.add("derived", "backup", str(exc), PROBLEM)

    name = f"{which.short.lower()} library"
    held = export_fcpxml.held_clips(which)
    if held is None:
        report.add("delivery and cuts", name,
                   "cannot be read; import state stays as the sidecars say", NOTE)
    else:
        report.add("delivery and cuts", name, f"{sum(held.values())} clip(s)")

    cuts = reels.load_all()
    loose = [(r.slug, p) for r in cuts for p in reels.resolve(r, sidecars).problems]
    if not cuts:
        report.add("delivery and cuts", "cuts", "none yet")
    elif loose:
        for slug, problem in loose:
            report.add("delivery and cuts", "cut " + slug, problem, PROBLEM)
    else:
        report.add("delivery and cuts", "cuts",
                   f"{len(cuts)}, every shot attached")

    report.vocabulary = len(Taxonomy.load().tags)
    return report


def lines_off_the_speech(sidecars: list[Sidecar]) -> int:
    """How many subtitle lines `align` would move. Reads the cached runs only.

    Cheap on a warm cache and worth saying: the timings are the model's
    estimates, and half a second is enough for a caption to appear before the
    mouth moves.
    """
    from . import speech

    cache = speech._load_cache()
    n = 0
    for sidecar in sidecars:
        entry = cache.get(sidecar.asset.stem)
        runs = entry.get("runs") if entry else None
        if not runs:
            continue
        for segment in sidecar.segments:
            ordered = sorted(segment.lines, key=lambda l: (l.start, l.end))
            for line, (start, end) in zip(ordered, speech.snap_lines(runs, ordered)):
                if abs(start - line.start) > 0.01 or abs(end - line.end) > 0.01:
                    n += 1
    return n
