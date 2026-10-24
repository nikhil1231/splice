"""Segment data model and the JSON Schema handed to `agy --json-schema`.

The sidecar `<stem>.seg.json` is the source of truth. SQLite is a rebuilt
index, so this module is the only place the shape is defined.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import config

Status = Literal["indexed", "reviewed", "planned", "used", "published"]
Rating = Literal["none", "favorite", "rejected"]


def speakers() -> tuple[str, ...]:
    """Who a line can be attributed to, as bare taxonomy values, from the
    project's people. Anything else -- a name the model invented, "the older
    woman" -- is nobody, because a caption coloured for the wrong person is
    worse than one left in the default colour."""
    return config.project().speakers


class Line(BaseModel):
    """One subtitle-sized utterance. Absolute seconds within the asset."""

    start: float
    end: float
    kok: str = ""
    en: str = ""
    # Who says it, as a bare taxonomy value -- "grandma", not "person:grandma".
    # Empty means nobody decided, which is different from nobody knowing: a
    # caption with no speaker is drawn in the default colour rather than
    # guessed at. Only 7% of lines sit in a segment naming exactly one person,
    # so the tags cannot supply this and the model has to.
    speaker: str = ""


class Segment(BaseModel):
    id: str = ""
    start: float
    end: float
    title: str
    summary: str = ""
    konkani: str = ""
    english: str = ""
    lines: list[Line] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    proposed_tags: list[str] = Field(default_factory=list)
    rating: Rating = "none"
    status: Status = "indexed"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Asset(BaseModel):
    file: str
    stem: str
    # What the camera called it, kept when a clip is renamed. The only link
    # back to the phone's own numbering.
    original_name: str | None = None
    # Content identity. A filename is not identity: the same clip re-copied off
    # the phone lands under whatever name the camera gave it, and a clip renamed
    # outside this tool changes name without changing at all. Both are the same
    # footage, and this is what says so.
    fingerprint: str = ""
    # Guards the fingerprint: it is only recomputed when one of these moved, so
    # a steady-state sync hashes nothing.
    mtime: float = 0.0
    # Every stem this clip has been known by, oldest first -- including names it
    # was given outside this tool. Final Cut may still hold it under one of them.
    previous_names: list[str] = Field(default_factory=list)
    duration: float = 0.0
    fps: str = "30/1"  # verbatim r_frame_rate; FCPXML needs the exact rational
    width: int = 0
    height: int = 0
    has_audio: bool = True
    created: str | None = None
    size_bytes: int = 0


class AnalysisRun(BaseModel):
    model: str = ""
    run_at: str = ""
    chunks: int = 0
    chunk_seconds: int = 0
    prompt_sha: str = ""
    errors: list[str] = Field(default_factory=list)


class ExportRecord(BaseModel):
    """What an editor has been offered, and what it demonstrably holds.

    These are two different facts and conflating them lost eight clips: writing
    `incoming.fcpxml` used to stamp a clip as delivered, so an import that never
    happened took the clip out of the pipeline for good. Offering is something we
    do; importing is something we can only observe, by reading the library.

    The `fcp_` field names predate there being a second editor to deliver to.
    They are not renamed because every sidecar in the archive -- and every
    backup of one in `archive/` -- already spells them that way, and a rename
    would rewrite 192 files to say what they already say. For Resolve they mean
    the media pool's name for the clip and how many copies of it are in there.
    """

    # Written when the clip goes into incoming.fcpxml.
    offered_at: str | None = None
    offered_sha: str = ""
    # Written only when Final Cut is seen to hold it.
    imported_at: str | None = None
    imported_sha: str = ""
    # The name Final Cut knows it by, which stops matching the stem the moment
    # the clip is renamed here.
    fcp_name: str = ""
    # Copies Final Cut held when the offer was made. Final Cut cannot update a
    # clip from XML, so a re-offer arrives as a second copy -- and a count that
    # has grown is the proof the import landed.
    fcp_copies: int = 0

    # Superseded by the four fields above. Accepted so the sidecars written
    # before the split -- and their backups in archive/ -- still load; migrated
    # on the way in, then corrected against the library by `reconcile`.
    fcpxml_at: str | None = None
    fcpxml_sha: str = ""

    @model_validator(mode="after")
    def _migrate_legacy(self) -> "ExportRecord":
        if self.fcpxml_at and not (self.imported_at or self.offered_at):
            self.imported_at = self.fcpxml_at
            self.imported_sha = self.fcpxml_sha
            self.fcpxml_at, self.fcpxml_sha = None, ""
        return self

    @property
    def delivered(self) -> bool:
        """The editor is known to hold some version of this clip."""
        return bool(self.imported_at)


class Sidecar(BaseModel):
    asset: Asset
    analysis: AnalysisRun = Field(default_factory=AnalysisRun)
    exported: ExportRecord = Field(default_factory=ExportRecord)
    # One record per editor that is not Final Cut. Final Cut keeps `exported`,
    # where it has always been: a clip in Final Cut's library is not in
    # Resolve's media pool and the two have to be tracked apart, but moving the
    # older fact into a dict would rewrite every sidecar in the archive to
    # relocate a value none of them changed.
    deliveries: dict[str, ExportRecord] = Field(default_factory=dict)
    segments: list[Segment] = Field(default_factory=list)

    def delivery(self, editor: str = "fcp", attach: bool = False) -> ExportRecord:
        """This clip's delivery record for one editor.

        `attach` is the difference between asking and intending to write. A
        caller that is only reading -- `pending`, `status`, the browser's
        badge -- gets a detached blank for an editor this clip has never been
        offered to, so asking the question does not put an empty record in
        every sidecar in the archive. `reconcile` and `mark_offered` mutate
        what they are given and then save, so they attach.
        """
        if editor == "fcp":
            return self.exported
        if attach:
            return self.deliveries.setdefault(editor, ExportRecord())
        return self.deliveries.get(editor) or ExportRecord()

    def set_delivery(self, editor: str, record: ExportRecord) -> None:
        """Replace a record outright -- which is how an offer is withdrawn."""
        if editor == "fcp":
            self.exported = record
        else:
            self.deliveries[editor] = record

    def content_sha(self) -> str:
        """Fingerprint of what an export would contain for this clip."""
        payload = json.dumps(
            [
                [round(s.start, 3), round(s.end, 3), s.title, sorted(s.tags), s.rating]
                for s in self.segments
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @classmethod
    def load(cls, path: Path) -> "Sidecar":
        return cls.model_validate_json(path.read_text())

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)


# --- The schema agy enforces -------------------------------------------
#
# Hand-written and deliberately flat: pydantic's generated schema uses
# $defs/$ref, which structured-output backends handle inconsistently.
# Timestamps here are relative to the *chunk*, not the asset.

def agy_schema() -> dict:
    """The schema, with the project's language and people written into its
    descriptions. Rendered the same way from the same greels.yaml every time,
    because it is half of the fingerprint that decides what is re-analysed."""
    p = config.project()
    who = f"{', '.join(p.speakers)}, or unknown" if p.speakers else "unknown"
    example = ", ".join(([f"person:{p.people[0].id}"] if p.people else []) + ["theme:money"])
    return {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {
                            "type": "number",
                            "description": "Seconds from the start of THIS clip.",
                        },
                        "end": {
                            "type": "number",
                            "description": "Seconds from the start of THIS clip.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Short specific name for the moment.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "One or two sentences in English.",
                        },
                        "konkani": {
                            "type": "string",
                            "description": f"Verbatim {p.written} spoken in this segment.",
                        },
                        "english": {
                            "type": "string",
                            "description": f"English translation of the {p.spoken}.",
                        },
                        "lines": {
                            "type": "array",
                            "description": "Utterance-level timings for subtitles.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "start": {"type": "number"},
                                    "end": {"type": "number"},
                                    "kok": {"type": "string"},
                                    "en": {"type": "string"},
                                    "speaker": {
                                        "type": "string",
                                        "description": f"Who says this line: {who}.",
                                    },
                                },
                                # `speaker` is required or the model simply omits
                                # it: the first run came back with every line
                                # attributed to nobody at all.
                                "required": ["start", "end", "en", "speaker"],
                            },
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": f"Prefixed tags, e.g. {example}.",
                        },
                    },
                    "required": ["start", "end", "title", "summary", "english", "tags"],
                },
            }
        },
        "required": ["segments"],
    }


def write_agy_schema(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(agy_schema(), indent=2))
    return path
