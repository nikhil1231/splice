"""Copy the hand-made files into the archive, where git keeps their history.

Two things here cannot be regenerated. The **sidecars** cost hours of Gemini
time and, once a transcript has been hand-corrected, cannot be produced again
at all. The **reels** were never generated in the first place -- a running
order and its trims are a decision, and nothing in this pipeline can make that
decision twice the same way. The **plans** and their **schedule** join them
once a reel is posted: they are then the only record of which moments went
out, and in what order.

All of it is small JSON, so version control is the obvious home, and a diff of a
transcript correction or a re-ordered cut is worth having. The archive is a
directory the project names (`archive:` in splice.yaml), meant to be a git
repository of its own and kept private: it is the footage's words, not code.
The project's own splice.yaml, vocabulary and prompts are mirrored there too,
under project/, because they are just as hand-made.
"""

from __future__ import annotations

import filecmp
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, trash

class Refused(RuntimeError):
    """A mirror that would have destroyed the copy it exists to protect."""


class NoArchive(Refused):
    """Nowhere to back up to. A refusal like the others, so every caller that
    already says "not backed up" says this too, instead of crashing."""


def archive_root() -> Path:
    """Where the mirror lives: `SPLICE_ARCHIVE`, else the project's `archive:`.

    A mirror belongs to one media root: it deletes what its source does not
    have, so a scratch `SPLICE_ROOT` mirrored over the real archive is how you
    lose it. A scratch root has no splice.yaml and so no archive, and the
    manifest's `media_root` stops one that borrowed somebody else's.

    Read at call time, not bound at import: a default argument would capture
    the path of whichever project was current when the module loaded.
    """
    root = config.archive_dir()
    if root is None:
        raise NoArchive(
            "no archive is set: add `archive: <folder>` to "
            f"{config.project_path()} (ideally a private git repository)"
        )
    return root


@dataclass(frozen=True)
class Set:
    """One mirrored directory: where it lives, where it is kept, what matches."""

    name: str
    live: Path
    saved: Path
    pattern: str


def sets() -> list[Set]:
    from .reel import reel_dir
    from .drafts import directory
    from . import plans

    root = archive_root()
    project = config.project_dir()
    kept = root / "project"
    return [
        Set("sidecars", config.index_dir(), root / "sidecars", "*.seg.json"),
        Set("reels", reel_dir(), root / "reels", "*.reel.json"),
        Set("drafts", directory(), root / "drafts", "*.draft.json"),
        Set("plans", plans.directory(), root / "plans", "*.plan.json"),
        # Beside the plans it orders. The patterns do not overlap, so neither
        # mirror counts the other's file as one to delete.
        Set("schedule", plans.directory(), root / "plans", "schedule.json"),
        # The project itself: what the footage is, its vocabulary, any prompt
        # it rewrote and the series plan it was seeded with. Hand-made, and in
        # the media root where nothing else versions them.
        Set("config", project, kept, config.PROJECT_FILE),
        Set("taxonomy", project, kept, "taxonomy.yaml"),
        Set("prompts", project / "prompts", kept / "prompts", "*.md"),
        Set("seed", project / "plans", kept / "plans", "schedule.json"),
    ]


def _copy(source: Path, target: Path) -> None:
    """Copy through a temporary name, so an interruption cannot leave half a
    transcript where a whole one was. This is the copy of the two things here
    that nothing can regenerate; it is worth the extra rename."""
    staged = target.with_suffix(target.suffix + ".tmp")
    try:
        shutil.copy2(source, staged)
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


# A backup deletes what is no longer live, which is right when a clip was
# renamed and catastrophic when the live directory merely could not be read.
# Above this share of the archive, deletion stops being tidying.
PRUNE_CEILING = 0.34


def _mirror(group: Set, force: bool = False) -> tuple[int, int, int]:
    group.saved.mkdir(parents=True, exist_ok=True)
    sources = sorted(group.live.glob(group.pattern)) if group.live.is_dir() else []
    saved = sorted(group.saved.glob(group.pattern))

    # The media root is a separate volume. Unmounted, it is not an archive with
    # nothing in it -- it is an archive nobody can see, and mirroring it would
    # delete every copy of transcripts that took hours to produce. Refuse.
    if saved and not sources:
        if not group.live.is_dir():
            # No amount of insistence makes this a good idea: there is nothing
            # to mirror, only something to lose.
            raise Refused(
                f"{group.live} does not exist, and {len(saved)} {group.name} are "
                f"backed up. Refusing to mirror an unreadable source over them."
            )
        if not force:
            raise Refused(
                f"{group.live} holds no {group.name} but {len(saved)} are backed "
                f"up. Refusing to empty the backup."
            )

    doomed = {p.name for p in saved} - {p.name for p in sources}
    if saved and len(doomed) > PRUNE_CEILING * len(saved) and not force:
        raise Refused(
            f"mirroring would delete {len(doomed)} of {len(saved)} backed-up "
            f"{group.name}: {', '.join(sorted(doomed)[:5])}"
            + (" ..." if len(doomed) > 5 else "")
            + ". That is more than a rename or two; check the media root is the "
            "one you think it is."
        )

    added = updated = 0
    for path in sources:
        target = group.saved / path.name
        if not target.exists():
            _copy(path, target)
            added += 1
        elif not filecmp.cmp(path, target, shallow=False):
            _copy(path, target)
            updated += 1

    # A clip renamed or deleted should not linger in the backup.
    for stale in doomed:
        (group.saved / stale).unlink()
    return added, updated, len(doomed)


def backup(dest: Path | None = None, force: bool = False) -> tuple[int, int, int]:
    """Mirror the sidecars and the reels into the repo.

    Returns (added, updated, removed) across both, since the caller only ever
    wants to know whether anything moved. Raises `Refused` rather than mirror a
    source it cannot properly see over copies that cannot be regenerated.
    """
    # Resolved here rather than as a default argument: a default binds the
    # module global once, at import, so anything that later points ARCHIVE
    # somewhere else gets the old path and mirrors over the wrong archive.
    groups = sets() if dest is None else [
        Set("sidecars", config.index_dir(), dest, "*.seg.json")
    ]
    if dest is None:
        _check_owner(force)

    added = updated = removed = 0
    for group in groups:
        a, u, r = _mirror(group, force=force)
        added, updated, removed = added + a, updated + u, removed + r

    _write_manifest()
    return added, updated, removed


def _check_owner(force: bool) -> None:
    """Refuse to mirror one media root over an archive another one wrote.

    The manifest says which root it was taken from. Mirroring deletes what the
    source lacks, so a second project pointed at the same archive -- a copied
    splice.yaml is all it takes -- would prune the first one's transcripts.
    """
    manifest = archive_root() / "manifest.json"
    # An unmounted root is its own refusal, made by the mirror with a better
    # sentence than "this is somebody else's archive".
    if force or not manifest.is_file() or not config.media_root().is_dir():
        return
    try:
        owner = json.loads(manifest.read_text()).get("media_root")
    except (OSError, ValueError):
        return
    here = str(config.media_root())
    if owner and Path(owner).expanduser().resolve() != Path(here).expanduser().resolve():
        raise Refused(
            f"{archive_root()} is the archive of {owner}, not of {here}. "
            "Point this project at an archive of its own."
        )


def _write_manifest() -> None:
    from .reel import reel_dir

    sources = sorted(config.index_dir().glob("*.seg.json")) if config.index_dir().is_dir() else []
    reels = sorted(reel_dir().glob("*.reel.json")) if reel_dir().is_dir() else []
    from .drafts import directory
    draft_files = list(directory().glob("*.draft.json"))
    from . import plans
    plan_files = list(plans.directory().glob("*.plan.json"))
    manifest = archive_root() / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "backed_up_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "media_root": str(config.media_root()),
                "clips": len(sources),
                "segments": sum(
                    len(json.loads(p.read_text()).get("segments", [])) for p in sources
                ),
                "reels": len(reels),
                "drafts": len(draft_files),
                "plans": len(plan_files),
                "shots": sum(
                    len(json.loads(p.read_text()).get("shots", [])) for p in reels
                ),
            },
            indent=2,
        )
        + "\n"
    )


def restore(source: Path | None = None) -> tuple[int, int]:
    """Copy the repo's copies back over the live directories. (restored, skipped)."""
    groups = sets() if source is None else [
        Set("sidecars", config.index_dir(), source, "*.seg.json")
    ]
    if not any(g.saved.is_dir() for g in groups):
        raise FileNotFoundError(f"no backup at {source or archive_root()}")

    restored = skipped = 0
    for group in groups:
        if not group.saved.is_dir():
            continue
        group.live.mkdir(parents=True, exist_ok=True)
        for path in sorted(group.saved.glob(group.pattern)):
            target = group.live / path.name
            if target.exists() and filecmp.cmp(path, target, shallow=False):
                skipped += 1
                continue
            # The live file may hold a correction that was never backed up.
            trash.keep(target, reason=f"overwritten by restoring {path.name}")
            _copy(path, target)
            restored += 1
    return restored, skipped


def drift() -> tuple[list[str], list[str], list[str]]:
    """(only live, only backed up, differing) -- what a backup would change."""
    fresh: list[str] = []
    stale: list[str] = []
    differing: list[str] = []
    for group in sets():
        live = {p.name for p in group.live.glob(group.pattern)} if group.live.is_dir() else set()
        saved = {p.name for p in group.saved.glob(group.pattern)} if group.saved.is_dir() else set()
        fresh += sorted(live - saved)
        stale += sorted(saved - live)
        differing += [
            name for name in sorted(live & saved)
            if not filecmp.cmp(group.live / name, group.saved / name, shallow=False)
        ]
    return fresh, stale, differing
