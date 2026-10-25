"""Give camera-numbered clips names that mean something.

`IMG_8768.MOV` tells you nothing in Final Cut's browser sitting next to
`grandpa_young_hero_photo_1961`. Once a clip is analysed there is enough
information to name it, so this renames the original and drags everything that
refers to it along: sidecar, proxy, chunks and segment ids.

The camera's own number is kept in `asset.original_name` -- it is the only link
back to what the phone called the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import config, library
from .schema import Sidecar

# Clips a camera named, as opposed to ones you named. A clip saved out of a
# chat or a share sheet arrives as a bare UUID instead, and says just as
# little: 6fa891f7-26a0-458f-b942-6cd50fc630d3 sat unnamed through a sync
# that named the thirty-seven IMG_ clips beside it.
CAMERA_NAMED = re.compile(
    r"^((IMG|DSC|MVI|VID|MOV|DJI|GX|GOPR|PXL)[_-]?\d+"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.I,
)
_SLUG = re.compile(r"[^a-z0-9]+")

def person_words() -> dict[str, str]:
    """`person:<id>` -> the word a clip about them is named with, for everyone
    in the project except whoever holds the camera: they are in every clip, so
    naming one after them says nothing."""
    return {f"person:{p.id}": p.filename_word
            for p in config.project().people if not p.camera}


def group_words() -> set[str]:
    """Words for more than one person (`grandparents` for person:both). A name
    already holding one is about the group, and gains no single name."""
    return {p.filename_word for p in config.project().people if not p.speaker}


class RenameError(RuntimeError):
    pass


@dataclass
class Plan:
    stem: str
    new_stem: str
    source: Path
    reason: str = ""

    @property
    def dest(self) -> Path:
        return self.source.with_name(self.new_stem + self.source.suffix)


@dataclass
class Skip:
    """A clip that cannot be named yet, and why.

    One un-analysed clip used to raise out of `build_plans` and abort naming for
    every other clip in the batch -- which, inside `sync`, meant a single failed
    analysis quietly left the whole archive camera-numbered.
    """

    stem: str
    reason: str


def slug(text: str, limit: int = 60) -> str:
    return _SLUG.sub("_", text.lower()).strip("_")[:limit].rstrip("_")


def is_camera_named(stem: str) -> bool:
    return bool(CAMERA_NAMED.match(stem))


def propose(sidecar: Sidecar) -> str:
    """A name derived from the analysis.

    The longest segment is the one the clip is really about; a short opening
    question makes a poor filename. The dominant person is prefixed when the
    title does not already name them, so clips group together when sorted.
    """
    if not sidecar.segments:
        raise RenameError(f"{sidecar.asset.stem} has no segments yet; analyse it first")

    best = max(sidecar.segments, key=lambda s: s.duration)
    name = slug(best.title)

    words = person_words()
    counts: dict[str, int] = {}
    for segment in sidecar.segments:
        for tag in segment.tags:
            if tag in words:
                counts[tag] = counts.get(tag, 0) + 1
    if counts:
        person = words[max(counts, key=lambda t: counts[t])]
        if person not in name and not any(g in name for g in group_words()):
            name = f"{person}_{name}"

    return name or sidecar.asset.stem


def build_plans(
    sidecars: list[Sidecar],
    explicit: dict[str, str] | None = None,
    only_camera_named: bool = True,
) -> tuple[list[Plan], list[Skip]]:
    """(what can be renamed, what cannot and why).

    Only an unmatched `--set` is fatal -- that is a typo you want to hear about.
    Everything else is per-clip: a clip with no analysis to name it from, or a
    name already taken, is skipped and reported while the rest proceed.
    """
    explicit = {k: slug(v) for k, v in (explicit or {}).items()}
    raw = config.raw_dir()
    existing = {p.stem for p in library.discover_raw()}
    plans: list[Plan] = []
    skips: list[Skip] = []
    claimed: set[str] = set()

    unknown = set(explicit) - {sc.asset.stem for sc in sidecars}
    if unknown:
        raise RenameError(f"no such clip(s): {', '.join(sorted(unknown))}")

    for sidecar in sidecars:
        stem = sidecar.asset.stem
        if stem in explicit:
            new_stem, reason = explicit[stem], "given"
        elif only_camera_named and not is_camera_named(stem):
            continue
        elif not sidecar.segments:
            skips.append(Skip(stem, "not analysed yet"))
            continue
        else:
            new_stem, reason = propose(sidecar), "from analysis"

        if not new_stem or new_stem == stem:
            continue
        if new_stem in (existing - {stem}) or new_stem in claimed:
            skips.append(Skip(stem, f"{new_stem} collides with an existing clip"))
            continue

        source = raw / sidecar.asset.file
        if not source.exists():
            skips.append(Skip(stem, f"{source.name} is missing from raw/"))
            continue

        claimed.add(new_stem)
        plans.append(Plan(stem=stem, new_stem=new_stem, source=source, reason=reason))

    return plans, skips


def relocate(old_stem: str, new_stem: str, new_file: str) -> None:
    """Carry everything that points at a clip over to its new name.

    Split out from `apply` because the raw file is not always ours to move: a
    clip renamed in Finder has already moved, and `scan` adopts that rename by
    calling this with the file where it now sits. Both paths must drag the
    sidecar, proxy, chunks and segment ids along identically.
    """
    proxy = library.proxy_for(old_stem)
    if proxy.exists():
        proxy.rename(library.proxy_for(new_stem))
    for _, chunk in library.chunks_for(old_stem):
        suffix = chunk.stem.split("__", 1)[1]
        chunk.rename(chunk.with_name(f"{new_stem}__{suffix}.mp4"))

    sidecar_path = config.sidecar_path(old_stem)
    if sidecar_path.exists():
        sidecar = Sidecar.load(sidecar_path)
        sidecar.asset.original_name = sidecar.asset.original_name or sidecar.asset.file
        # Final Cut may hold this clip under any name it has had, so the trail
        # has to survive -- it is what `reconcile` matches on.
        if old_stem not in sidecar.asset.previous_names:
            sidecar.asset.previous_names.append(old_stem)
        # A name it has come back to is its current name, not a former one.
        sidecar.asset.previous_names = [
            n for n in sidecar.asset.previous_names if n != new_stem
        ]
        sidecar.asset.file = new_file
        sidecar.asset.stem = new_stem
        for segment in sidecar.segments:
            segment.id = f"{new_stem}#{segment.id.split('#', 1)[1]}"
        sidecar.save(config.sidecar_path(new_stem))
        if config.sidecar_path(new_stem) != sidecar_path:
            sidecar_path.unlink()

    _follow_in_reels(old_stem, new_stem)
    _follow_in_plans(old_stem, new_stem)

    # Subtitle files carry the old stem in their names; they are regenerated by
    # `export srt`, so drop the stale ones rather than leave two sets.
    for stale in (config.export_dir() / "srt").glob(f"{old_stem}__*.srt"):
        stale.unlink()


def _follow_in_reels(old_stem: str, new_stem: str) -> int:
    """Point any cut that used this clip at its new name.

    A shot is `(clip, in, out)`, so a rename is the one thing that can break
    one: the range is still right and the clip it names no longer exists.
    Everything else a reel depends on survives on its own.
    """
    from . import reel as store

    touched = 0
    for reel in store.load_all():
        moved = False
        for shot in reel.shots:
            if shot.stem != old_stem:
                continue
            shot.stem = new_stem
            if shot.segment_id.startswith(f"{old_stem}#"):
                shot.segment_id = f"{new_stem}#{shot.segment_id.split('#', 1)[1]}"
            moved = True
        if moved:
            reel.save()
            touched += 1
    return touched


def _follow_in_plans(old_stem: str, new_stem: str) -> int:
    """Point any plan moment on this clip at its new name.

    A plan finds its moments again by `(stem, in, out)`, the same as a shot,
    and a rename was the one thing that could still lose them: the sidecar,
    the proxy and every cut moved, and the plans kept a stem that no longer
    existed. grandpa_building_the_house_for_rs_13_000 was in four plans the
    day its speaker turned out not to be Grandpa. A plan is the only record of
    what was published, so this saves through `plans.save`, revision-checked
    and with its trash copy, rather than writing around it.
    """
    from . import plans as store

    touched = 0
    for plan in store.load_all():
        moved = False
        for moment in plan.moments:
            if moment.stem == old_stem or moment.id.startswith(f"{old_stem}#"):
                moment.stem = new_stem
                moment.id = f"{new_stem}#{moment.id.split('#', 1)[1]}"
                moved = True
        if moved:
            store.save(plan, plan.revision)
            touched += 1
    return touched


def apply(plan: Plan) -> None:
    """Move the original and everything that points at it."""
    if plan.dest.exists():
        raise RenameError(f"{plan.dest.name} already exists")

    plan.source.rename(plan.dest)
    relocate(plan.stem, plan.new_stem, plan.dest.name)
