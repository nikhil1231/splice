"""Editable story proposals, kept separately from decisions in saved cuts.

References survive re-analysis by using asset identity and source times. Quote
snapshots are evidence of what a proposal was based on, never new transcripts.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict, model_validator

from . import config, library, reel, trash
from .schema import Line, Sidecar


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token() -> str:
    return uuid.uuid4().hex


class Conflict(ValueError):
    pass


class Excerpt(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    stem: str
    fingerprint: str = ""
    segment_id: str = ""
    start: float = Field(ge=0)
    end: float
    snapshot: list[Line] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("an excerpt must end after it starts")
        return self


class Beat(Excerpt):
    id: str = Field(default_factory=token)
    role: str = ""
    note: str = ""
    alternatives: list[Excerpt] = Field(default_factory=list)


class Draft(BaseModel):
    version: Literal[1] = 1
    id: str = Field(default_factory=token)
    revision: str = Field(default_factory=token)
    title: str = Field(min_length=1, max_length=300)
    angle: str = ""
    brief: str = ""
    notes: str = ""
    beats: list[Beat] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


def directory():
    return config.index_dir() / "drafts"


def path_for(draft_id: str):
    # HTTP identifiers must never become arbitrary filesystem paths.
    if len(draft_id) != 32 or any(c not in "0123456789abcdef" for c in draft_id):
        raise ValueError("invalid draft id")
    return directory() / f"{draft_id}.draft.json"


def load(draft_id: str) -> Draft:
    path = path_for(draft_id)
    if not path.exists():
        raise KeyError(draft_id)
    return Draft.model_validate_json(path.read_text())


def load_all() -> list[Draft]:
    return sorted((Draft.model_validate_json(p.read_text())
                   for p in directory().glob("*.draft.json")),
                  key=lambda d: d.created_at, reverse=True)


def save(draft: Draft, expected: str | None = None) -> Draft:
    path = path_for(draft.id)
    if path.exists():
        current = load(draft.id)
        if current.revision != expected:
            raise Conflict("Draft changed in another window. Reload before saving.")
        trash.keep(path, reason="draft edited")
    elif expected is not None:
        raise Conflict("Draft was deleted. Reload before saving.")
    draft.revision = token()
    draft.updated_at = now()
    directory().mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".tmp")
    staged.write_text(draft.model_dump_json(indent=2) + "\n")
    staged.replace(path)
    return draft


def delete(draft_id: str, revision: str):
    draft = load(draft_id)
    if draft.revision != revision:
        raise Conflict("Draft changed in another window. Reload before deleting.")
    path = path_for(draft_id)
    trash.keep(path, reason="draft deleted")
    path.unlink()


def source(excerpt: Excerpt, sidecars: list[Sidecar]) -> Sidecar | None:
    if excerpt.fingerprint:
        return next((s for s in sidecars if s.asset.fingerprint == excerpt.fingerprint), None)
    return next((s for s in sidecars if excerpt.stem in
                 [s.asset.stem, s.asset.original_name, *s.asset.previous_names]), None)


def lines_for(sc: Sidecar, start: float, end: float) -> list[Line]:
    # Re-analysis can overlap segments. A caption still needs only one copy.
    unique = {(l.start, l.end, l.en, l.kok, l.speaker): l
              for s in sc.segments for l in s.lines if l.end > start and l.start < end}
    return sorted(unique.values(), key=lambda l: (l.start, l.end))


def capture(excerpt: Excerpt, sidecars: list[Sidecar]) -> Excerpt:
    sc = source(excerpt, sidecars)
    if sc is None:
        raise ValueError(f"source unavailable: {excerpt.stem}")
    if excerpt.end > sc.asset.duration:
        raise ValueError("excerpt runs past the end of its source")
    if any(s.rating == "rejected" and s.end > excerpt.start and s.start < excerpt.end
           for s in sc.segments):
        raise ValueError("excerpt includes a rejected moment")
    lines = lines_for(sc, excerpt.start, excerpt.end)
    if not lines:
        raise ValueError("excerpt has no timestamped dialogue")
    segment = next((s for s in sc.segments if s.end > excerpt.start and s.start < excerpt.end), None)
    return excerpt.model_copy(update={"stem": sc.asset.stem, "fingerprint": sc.asset.fingerprint,
                                      "segment_id": segment.id if segment else "",
                                      "snapshot": lines})


def describe(excerpt: Excerpt, sidecars: list[Sidecar]) -> dict:
    sc = source(excerpt, sidecars)
    out = excerpt.model_dump(mode="json")
    out.update(duration=excerpt.end - excerpt.start, problem="", warnings=[], lines=[],
               context=[], proxy=False, clip_duration=None)
    if not sc:
        out["problem"] = "Source footage is missing or has been replaced."
        out["lines"] = out["snapshot"]
        return out
    out["stem"] = sc.asset.stem
    out["clip_duration"] = sc.asset.duration
    out["proxy"] = library.proxy_for(sc.asset.stem).exists()
    if not (config.raw_dir() / sc.asset.file).exists():
        out["problem"] = "Original footage is missing."
    elif excerpt.end > sc.asset.duration:
        out["problem"] = "Excerpt runs past the end of the source."
    lines = lines_for(sc, excerpt.start, excerpt.end)
    out["lines"] = [l.model_dump(mode="json") for l in lines]
    if lines != excerpt.snapshot:
        out["warnings"].append("Source dialogue changed since this excerpt was selected.")
    segments = sorted(sc.segments, key=lambda s: s.start)
    matching = [i for i, s in enumerate(segments) if s.end > excerpt.start and s.start < excerpt.end]
    if matching:
        out["segment_id"] = segments[matching[0]].id
        neighbours = segments[max(0, min(matching) - 1):max(matching) + 2]
        out["context_start"] = min(excerpt.start, neighbours[0].start)
        out["context_end"] = min(sc.asset.duration, max(excerpt.end, neighbours[-1].end))
        out["context"] = [dict(l.model_dump(mode="json"), segment_id=s.id,
                               title=s.title, rejected=s.rating == "rejected")
                          for s in neighbours for l in s.lines]
        if any(segments[i].rating == "rejected" for i in matching):
            out["warnings"].append("This excerpt now includes a rejected moment.")
    else:
        out["warnings"].append("The original moment no longer exists in the transcript.")
    return out


def detail(draft: Draft, sidecars: list[Sidecar] | None = None) -> dict:
    sidecars = library.load_all() if sidecars is None else sidecars
    out = draft.model_dump(mode="json")
    out["beats"] = [describe(b, sidecars) | {
        "alternatives": [describe(a, sidecars) for a in b.alternatives]} for b in draft.beats]
    out["duration"] = sum(b.end - b.start for b in draft.beats)
    return out


def edited(body: dict, existing: Draft | None = None) -> Draft:
    fields = {k: v for k, v in body.items() if k in {"title", "angle", "brief", "notes", "beats"}}
    draft = Draft.model_validate((existing.model_dump() if existing else {}) | fields)
    sidecars = library.load_all()
    previous = {b.id: b for b in existing.beats} if existing else {}

    def keep_or_capture(e: Excerpt, old: Excerpt | None):
        identity = lambda x: (x.fingerprint or x.stem, x.start, x.end)
        if old and identity(e) == identity(old):
            return e.model_copy(update={"snapshot": old.snapshot})
        return capture(e, sidecars)

    beats = []
    seen = set()
    for b in draft.beats:
        if b.id in seen:
            raise ValueError("duplicate beat id")
        seen.add(b.id)
        old = previous.get(b.id)
        b = keep_or_capture(b, old)
        b.alternatives = [keep_or_capture(a, next((x for x in old.alternatives
                          if (x.fingerprint, x.stem, x.start, x.end) ==
                          (a.fingerprint, a.stem, a.start, a.end)), None) if old else None)
                          for a in b.alternatives]
        beats.append(b)
    draft.beats = beats
    return draft


def snapshot_cut(draft: Draft) -> reel.Reel:
    resolved = detail(draft)
    if not draft.beats:
        raise ValueError("nothing to export: the draft is empty")
    problems = [b["problem"] for b in resolved["beats"] if b["problem"]]
    if problems:
        raise ValueError("Cannot export incomplete draft: " + "; ".join(problems))
    return reel.create(draft.title, hook=draft.angle, notes=draft.notes,
                       shots=[reel.Shot(stem=b["stem"], start=b["start"], end=b["end"],
                                        segment_id=b["segment_id"], note=b["note"] or b["role"])
                              for b in resolved["beats"]])
