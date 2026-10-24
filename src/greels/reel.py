"""A reel: an ordered cut assembled from moments across several clips.

Everything upstream of this file describes footage. This is the first thing
that describes a *video* -- which shots, in which order, trimmed to where.

Two decisions worth knowing about.

**A shot is `(clip, in, out)`, not a segment id.** Segment ids are derived from
their own start time (`grandpa_old_money_box#00000.00`), so re-running
`analyze` after a prompt change renumbers them and would silently empty every
reel that pointed at them. A cut cannot depend on that. The segment a shot was
lifted from is kept as provenance -- it is what `show` prints and what the
subtitles come from -- but the cut itself survives the segment's disappearance.

**A reel is a file, not a row.** `index/reels/<slug>.reel.json`, alongside the
sidecars, hand-editable and diffable, backed up into the repo by `backup`. The
SQLite tables are rebuilt from these like everything else, which retires the
one thing `index` used to have to carry across a rebuild.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import config, library
from .schema import Line, Segment, Sidecar

Status = Literal["planned", "cut", "published"]

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str, limit: int = 48) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:limit].strip("-") or "reel"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Shot(BaseModel):
    """One cut on the timeline: a range of one source clip."""

    stem: str
    start: float
    end: float
    # Where this came from. Provenance only -- the shot stands without it.
    segment_id: str = ""
    note: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Reel(BaseModel):
    slug: str
    title: str
    status: Status = "planned"
    # The idea the reel is for: what it is about, or the opening line.
    hook: str = ""
    notes: str = ""
    shots: list[Shot] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    # What was handed to the editor, and what it was. Neither editor can update
    # a project from XML any more than it can a clip, so a re-export arrives as
    # a second timeline -- worth knowing before you write one.
    exported_at: str | None = None
    exported_sha: str = ""
    # And which editor it went to. The two documents are different files with
    # different names, so this is the only thing that says which of them the
    # timestamp above belongs to. Blank on every cut exported before there was
    # a second editor, which means Final Cut.
    exported_editor: str = ""
    # And what was last rendered to a file you can watch. A separate pair,
    # because these go stale independently: a trim you made after exporting is
    # in the render and not in the timeline, and knowing which of the two you
    # are looking at is the whole point of recording it.
    rendered_at: str | None = None
    rendered_sha: str = ""
    # Which kind of captions the file on disk has. The two renders overwrite
    # each other, and a player that lays a subtitle track over a video with
    # the captions already drawn on shows both.
    rendered_captions: str = ""

    @property
    def duration(self) -> float:
        return sum(shot.duration for shot in self.shots)

    def content_sha(self) -> str:
        """Fingerprint of what an export of this cut would contain."""
        payload = json.dumps(
            [self.title] + [
                [s.stem, round(s.start, 3), round(s.end, 3), s.note] for s in self.shots
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def stale(self) -> bool:
        """Exported once, and changed since."""
        return bool(self.exported_at) and self.exported_sha != self.content_sha()

    @property
    def render_stale(self) -> bool:
        """Rendered once, and changed since."""
        return bool(self.rendered_at) and self.rendered_sha != self.content_sha()

    @property
    def clips(self) -> set[str]:
        return {shot.stem for shot in self.shots}

    def save(self, path: Path | None = None) -> Path:
        path = path or path_for(self.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2) + "\n")
        tmp.replace(path)
        return path


# --- the store ---------------------------------------------------------


def reel_dir() -> Path:
    return config.index_dir() / "reels"


def path_for(slug: str) -> Path:
    return reel_dir() / f"{slug}.reel.json"


def exists(slug: str) -> bool:
    return path_for(slug).exists()


def load(slug: str) -> Reel | None:
    path = path_for(slug)
    return Reel.model_validate_json(path.read_text()) if path.exists() else None


def load_all() -> list[Reel]:
    out = []
    for path in sorted(reel_dir().glob("*.reel.json")) if reel_dir().is_dir() else []:
        try:
            out.append(Reel.model_validate_json(path.read_text()))
        except Exception as exc:  # a hand-edited reel should not kill a run
            print(f"  ! skipping malformed {path.name}: {exc}")
    return out


def unique_slug(title: str) -> str:
    """A slug nothing already holds, so `new` never overwrites a cut."""
    base = slugify(title)
    slug, n = base, 2
    while exists(slug):
        slug, n = f"{base}-{n}", n + 1
    return slug


def create(title: str, shots: list[Shot] | None = None, **fields) -> Reel:
    reel = Reel(slug=unique_slug(title), title=title, shots=shots or [], **fields)
    reel.save()
    return reel


def delete(slug: str) -> bool:
    path = path_for(slug)
    if not path.exists():
        return False
    path.unlink()
    return True


def rename(reel: Reel, title: str) -> Reel:
    """Retitle in place. The slug is the identity and does not move with it."""
    reel.title = title
    reel.save()
    return reel


# --- resolving a reel against the footage ------------------------------


@dataclass
class ResolvedShot:
    shot: Shot
    index: int
    sidecar: Sidecar | None
    segment: Segment | None
    # Position on the reel's own timeline, seconds from its start.
    offset: float
    problem: str | None = None

    @property
    def start(self) -> float:
        return self.shot.start

    @property
    def end(self) -> float:
        return self.shot.end

    @property
    def duration(self) -> float:
        return self.shot.duration

    @property
    def title(self) -> str:
        if self.shot.note:
            return self.shot.note
        return self.segment.title if self.segment else self.shot.stem

    @property
    def lines(self) -> list[Line]:
        """Every line spoken inside this shot, whichever segment holds it.

        A shot is not required to sit inside one segment: widen the in point by
        five seconds and it reaches back into the moment before. Reading only
        `self.segment` dropped whatever was said in that overlap -- from the
        captions, from the reel's .srt, and from the running order.
        """
        if not self.sidecar:
            return []
        spoken = [
            line
            for segment in self.sidecar.segments
            for line in segment.lines
            if line.end > self.shot.start and line.start < self.shot.end
        ]
        # Segments overlap at their edges, so the same utterance can arrive
        # twice; and they are not necessarily in time order.
        seen: set[tuple] = set()
        out = []
        for line in sorted(spoken, key=lambda l: (l.start, l.end)):
            key = (round(line.start, 2), round(line.end, 2), line.en, line.kok)
            if key not in seen:
                seen.add(key)
                out.append(line)
        return out


    @property
    def moment_lines(self) -> list[Line]:
        """Everything said in the moment this shot was lifted from.

        Not the same as `lines`, which is what the shot currently holds and is
        what the captions and the .srt are made of. This is for trimming: the
        line you want is usually just outside the trim -- "start where he says
        *sixty, seventy years ago*" is a line the shot does not yet contain --
        and you cannot click a line that is not on the screen.
        """
        if not self.sidecar:
            return []
        spoken = list(self.segment.lines) if self.segment else []
        spoken += [
            line
            for segment in self.sidecar.segments
            for line in segment.lines
            if line.end > self.shot.start and line.start < self.shot.end
        ]
        seen: set[tuple] = set()
        out = []
        for line in sorted(spoken, key=lambda l: (l.start, l.end)):
            key = (round(line.start, 2), round(line.end, 2), line.en, line.kok)
            if key not in seen:
                seen.add(key)
                out.append(line)
        return out


@dataclass
class Resolved:
    reel: Reel
    shots: list[ResolvedShot]

    @property
    def problems(self) -> list[str]:
        return [f"shot {s.index + 1}: {s.problem}" for s in self.shots if s.problem]

    @property
    def sidecars(self) -> list[Sidecar]:
        """One per distinct source clip, in first-appearance order."""
        out: dict[str, Sidecar] = {}
        for shot in self.shots:
            if shot.sidecar and shot.sidecar.asset.stem not in out:
                out[shot.sidecar.asset.stem] = shot.sidecar
        return list(out.values())

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.shots)


def resolve(reel: Reel, sidecars: list[Sidecar] | None = None) -> Resolved:
    """Attach each shot to its footage, and say so when it cannot be.

    A shot that has come loose -- its clip deleted, or its range now past the
    end of a replaced file -- is reported rather than dropped. Losing a shot
    quietly is how a cut you spent an evening on turns into a shorter one.
    """
    by_stem = {sc.asset.stem: sc for sc in (sidecars or library.load_all())}
    out: list[ResolvedShot] = []
    offset = 0.0

    for i, shot in enumerate(reel.shots):
        sidecar = by_stem.get(shot.stem)
        segment = None
        problem = None

        if sidecar is None:
            problem = f"no clip named {shot.stem}"
        else:
            segment = next(
                (s for s in sidecar.segments if s.id == shot.segment_id), None
            )
            if segment is None:
                segment = _covering(sidecar, shot)
            if shot.end > sidecar.asset.duration + 0.05:
                problem = (
                    f"{shot.stem} is {sidecar.asset.duration:.1f}s long, "
                    f"shot runs to {shot.end:.1f}s"
                )
            elif shot.duration <= 0:
                problem = "empty range"

        out.append(
            ResolvedShot(
                shot=shot, index=i, sidecar=sidecar, segment=segment,
                offset=offset, problem=problem,
            )
        )
        offset += shot.duration

    return Resolved(reel=reel, shots=out)


def _covering(sidecar: Sidecar, shot: Shot) -> Segment | None:
    """The segment a shot sits in, when its recorded id no longer exists.

    Re-analysis renumbers segments, so a reel cut before it points at ids that
    are gone. The midpoint still lands in whatever now covers that moment --
    the same rule `analyze` uses to carry ratings across a re-run.
    """
    middle = (shot.start + shot.end) / 2
    return next(
        (s for s in sidecar.segments if s.start <= middle < s.end), None
    )


# --- reading a cut back to yourself ------------------------------------
#
# None of this is a rule. A reel that breaks all of them can be the right reel;
# these are the things you would notice on a second viewing and not on the
# first, which is exactly what is worth saying out loud.

# A vertical reel holds attention for about this long. Instagram allows far
# more; the ones people watch to the end do not use it.
LONG_REEL = 100.0
SHORT_REEL = 12.0
# Below this a shot reads as a flash rather than a moment.
FLASH = 1.2
# One shot taking this much of the reel means it is that shot, plus trimmings.
# Only worth saying from three shots up: in a two-shot cut, one of them is
# always about half.
DOMINANT = 0.55
# Two shots from one clip this close together were one shot until you cut it.
NEEDLESS_CUT = 0.4
# Characters a second a caption can be read at. Broadcast subtitling works to
# 15-17; this is deliberately slack, so what it catches is a line nobody could
# read rather than one that is merely brisk.
READING_RATE = 26


@dataclass
class Note:
    """One observation. The text continues from "this cut" or "shot 3", which
    is how both callers print it -- so it starts with a verb."""

    text: str
    shot: int | None = None


# A boundary this far inside an utterance is a clipped word. Below it is a
# breath, which is what a cut is supposed to sound like.
MID_UTTERANCE = 0.35
# And past this it is not a mistake, it is an edit: somebody deliberately came
# in over the middle of a sentence. It is also further than `reel snap` will
# move anything, so saying so would be pointing at a button that does nothing.
SNAPPABLE = 1.2


def critique(
    resolved: Resolved,
    speech: dict[str, list] | None = None,
    window: float = SNAPPABLE,
) -> list[Note]:
    """What you would notice about this cut on a second viewing.

    `speech` is the runs of speech per clip, when the caller has them. Passed
    in rather than measured here: this module knows about a cut and about the
    footage it names, and nothing about ffmpeg.
    """
    notes: list[Note] = []
    shots = [s for s in resolved.shots if not s.problem]
    total = sum(s.duration for s in shots)
    if not shots:
        return notes

    if total > LONG_REEL:
        notes.append(Note(
            f"runs {int(total // 60)}:{int(total % 60):02d}; a reel that gets watched "
            f"to the end is usually under {int(LONG_REEL)}s"
        ))
    elif total < SHORT_REEL:
        notes.append(Note(f"has only {total:.0f}s of material"))

    if len({s.shot.stem for s in shots}) == 1:
        notes.append(Note(
            "takes every shot from one clip; that is a trim, not a reel"
        ))

    for shot in shots:
        if shot.duration < FLASH:
            notes.append(Note(
                f"{shot.duration:.1f}s reads as a flash, not a moment", shot.index
            ))
        if total and shot.duration > DOMINANT * total and len(shots) > 2:
            notes.append(Note(
                f"is {shot.duration / total:.0%} of the reel; the rest is trimmings",
                shot.index,
            ))

    # Only where a snap would actually move it. Coming in over the middle of a
    # long answer is an edit somebody made; clipping the first syllable of one
    # is the model's boundary showing through, and that is the one worth
    # pointing at.
    for shot in shots if speech else []:
        runs = speech.get(shot.shot.stem) or []
        late = next(
            (r for r in runs if MID_UTTERANCE < shot.start - r[0] <= window), None
        )
        if late:
            notes.append(Note(
                f"opens {shot.start - late[0]:.1f}s after this one started talking; "
                "`reel snap` moves it back to the word",
                shot.index,
            ))
        early = next(
            (r for r in runs if MID_UTTERANCE < r[1] - shot.end <= window), None
        )
        if early:
            notes.append(Note(
                f"cuts {early[1] - shot.end:.1f}s before they stop talking",
                shot.index,
            ))

    # A caption nobody can read is worse than no caption: it is on screen, and
    # the viewer is trying. The fix is not a longer shot -- the speech is the
    # speech -- it is a shorter translation, which is an edit you can make.
    racing: dict[int, int] = {}
    for shot in shots:
        for line in shot.lines:
            if not line.en or line.end <= line.start:
                continue
            if len(line.en) / (line.end - line.start) > READING_RATE:
                racing[shot.index] = racing.get(shot.index, 0) + 1
    for index, count in racing.items():
        notes.append(Note(
            f"has {count} caption(s) going by faster than {READING_RATE} "
            "characters a second; a shorter translation is the only thing "
            "that fixes that",
            index,
        ))

    for earlier, later in zip(shots, shots[1:]):
        if earlier.shot.stem != later.shot.stem:
            continue
        if abs(later.start - earlier.end) < NEEDLESS_CUT:
            notes.append(Note(
                f"runs straight on from shot {earlier.index + 1} in the same clip; "
                "one shot would cut the same",
                later.index,
            ))

    # Whatever is about the cut as a whole first, then the shots in order.
    # These are read as a list and a list that jumps about is read as noise.
    notes.sort(key=lambda note: (note.shot is not None, note.shot or 0))
    return notes


def shot_from_segment(sidecar: Sidecar, segment: Segment) -> Shot:
    return Shot(
        stem=sidecar.asset.stem,
        start=segment.start,
        end=min(segment.end, sidecar.asset.duration),
        segment_id=segment.id,
    )


# A quote needs a breath either side of it, or it starts on the speaker's
# first consonant and ends on their last.
LINE_PAD = 0.4


def shot_from_line(sidecar: Sidecar, segment: Segment, line: Line,
                   pad: float = LINE_PAD) -> Shot:
    """A shot cut to one spoken line, with the line itself as its note.

    The note is what `show` prints and what names the clip in Final Cut, and
    for a shot that exists to carry one sentence, that sentence is its name.
    """
    return Shot(
        stem=sidecar.asset.stem,
        start=max(0.0, line.start - pad),
        end=min(line.end + pad, sidecar.asset.duration),
        segment_id=segment.id,
        note=(line.en or line.kok or "")[:120],
    )
