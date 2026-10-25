"""Segments -> FCPXML.

The point of this file: a 20-minute interview should land in the editor already
carved into named ranges, so one click returns moments from four different
source files.

What that looks like depends on who is being handed it, and `editors.py` holds
the differences. Final Cut gets an event of keyworded clips, which is what its
browser is for. Resolve has no keyword ranges and no importer for an event at
all, so it gets a stringout — every clip end to end on one timeline, each
segment a named marker carrying its tags in the note — and importing that is
what puts the sources in its media pool.

It links the originals in raw/, never the proxies — proxies exist only for the
model.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from dataclasses import dataclass, field

from . import config, editors, fcp, library
from .editors import Editor
from .fcptime import frame_duration, frames, from_frames, rational, snap
from .schema import ExportRecord, Sidecar
from .taxonomy import Taxonomy

FCPXML_VERSION = "1.11"

# Apple ships the schemas inside Final Cut. When it is installed we can hold the
# export to Apple's own definition instead of guessing.
DTD_DIR = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework"
    "/Versions/A/Resources"
)


def dtd_path(version: str = FCPXML_VERSION) -> Path | None:
    path = DTD_DIR / f"FCPXMLv{version.replace('.', '_')}.dtd"
    return path if path.exists() else None


def validate(document: Path, version: str = FCPXML_VERSION) -> str | None:
    """Check against Apple's DTD. Returns None if valid or unavailable.

    xmllint cannot resolve a DTD whose path contains spaces -- and Apple's does,
    twice -- so both files are staged somewhere clean first.
    """
    dtd = dtd_path(version)
    if dtd is None or not shutil.which("xmllint"):
        return None

    with tempfile.TemporaryDirectory() as tmp:
        staged_dtd = Path(tmp) / "fcpxml.dtd"
        staged_doc = Path(tmp) / "document.fcpxml"
        shutil.copyfile(dtd, staged_dtd)
        shutil.copyfile(document, staged_doc)
        proc = subprocess.run(
            ["xmllint", "--noout", "--dtdvalid", str(staged_dtd), str(staged_doc)],
            capture_output=True, text=True,
        )
    if proc.returncode == 0:
        return None
    return proc.stderr.strip() or f"xmllint exited {proc.returncode}"


def write_validated(
    tree: ET.ElementTree, dest: Path, version: str | None = None
) -> Path:
    """Write an FCPXML only if Apple's DTD accepts it.

    Staged through a temporary file beside the destination and moved into place
    at the end. Writing straight to `dest` and raising afterwards left an
    invalid file on disk -- and, worse, replaced a perfectly good one with it.
    `incoming.fcpxml` is promised to be safe to import whenever it exists, and
    that promise has to survive a failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tree.write(staged, encoding="utf-8", xml_declaration=True)
        # Final Cut wants the doctype; ElementTree cannot emit one.
        text = staged.read_text()
        if "<!DOCTYPE" not in text:
            staged.write_text(text.replace("?>\n", "?>\n<!DOCTYPE fcpxml>\n", 1))

        # A document read back from Final Cut declares its own version, and
        # 1.13 against the 1.11 DTD fails on elements that are simply newer.
        against = version or tree.getroot().get("version") or FCPXML_VERSION
        problem = validate(staged, against)
        if problem:
            raise ValueError(
                f"export failed Apple's FCPXML {against} DTD:\n{problem}"
            )
        staged.replace(dest)
    finally:
        staged.unlink(missing_ok=True)
    return dest


def _format_key(sc: Sidecar) -> tuple[str, int, int]:
    return (sc.asset.fps, sc.asset.width, sc.asset.height)


class Resources:
    """The `<resources>` block: one `<format>` per shape, one `<asset>` per clip.

    Shared by the keyword export and the reel timeline, which need exactly the
    same declarations and must number them the same way -- an id that means two
    things in one document is a file Final Cut silently mis-imports.
    """

    def __init__(self, root: ET.Element, raw_dir: Path | None = None):
        self.element = ET.SubElement(root, "resources")
        self.raw_dir = raw_dir or config.raw_dir()
        self._next = 1
        self.formats: dict[tuple[str, int, int], str] = {}
        self.assets: dict[str, str] = {}

    def _id(self) -> str:
        rid = f"r{self._next}"
        self._next += 1
        return rid

    def format_for(self, sc: Sidecar) -> str:
        """A `<format>` per distinct (fps, width, height).

        No `name`: these are portrait phone clips, which have no standard Apple
        format name.
        """
        key = _format_key(sc)
        if key not in self.formats:
            self.formats[key] = fid = self._id()
            ET.SubElement(
                self.element, "format", id=fid,
                frameDuration=frame_duration(sc.asset.fps),
                width=str(sc.asset.width), height=str(sc.asset.height),
                colorSpace="1-1-1 (Rec. 709)",
            )
        return self.formats[key]

    def asset_for(self, sc: Sidecar) -> str:
        if sc.asset.stem in self.assets:
            return self.assets[sc.asset.stem]
        fid = self.format_for(sc)
        self.assets[sc.asset.stem] = aid = self._id()
        attrs = {
            "id": aid,
            "name": sc.asset.stem,
            "start": "0s",
            "duration": rational(sc.asset.duration, sc.asset.fps),
            "hasVideo": "1",
            "format": fid,
        }
        if sc.asset.has_audio:
            attrs |= {"hasAudio": "1", "audioSources": "1", "audioChannels": "2"}
        asset = ET.SubElement(self.element, "asset", attrs)
        ET.SubElement(
            asset, "media-rep", kind="original-media",
            src=_file_url(self.raw_dir / sc.asset.file),
        )
        return aid

    def declare(self, sidecars: list[Sidecar]) -> None:
        """Formats first, then assets, so the block reads like Apple's own."""
        for sc in sidecars:
            self.format_for(sc)
        for sc in sidecars:
            self.asset_for(sc)


def new_document() -> tuple[ET.Element, Resources]:
    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    return root, Resources(root)


def finish(root: ET.Element) -> ET.ElementTree:
    ET.indent(root, space="    ")
    return ET.ElementTree(root)


def build_document(
    sidecars: list[Sidecar],
    taxonomy: Taxonomy,
    event_name: str | None = None,
    raw_dir: Path | None = None,
    editor: Editor | str | None = None,
) -> ET.ElementTree:
    which = editors.get(editor)
    event_name = event_name or config.event_name()
    root = ET.Element("fcpxml", version=which.fcpxml_version)
    resources = Resources(root, raw_dir)
    resources.declare(sidecars)

    library_el = ET.SubElement(root, "library")
    event = ET.SubElement(library_el, "event", name=event_name)
    # No clips means no stringout: a `<project>` whose `<sequence>` is empty is
    # a document Resolve imports as a timeline of nothing, which is worse than
    # the event it would otherwise be.
    parent = _stringout(event, sidecars, resources, event_name) if (
        which.needs_timeline and sidecars
    ) else event

    # On a spine every clip needs its own offset, and they are accumulated in
    # whole frames of the sequence's timebase: rounding each one's start
    # independently leaves one-frame gaps, which read as black flashes.
    seq_fps = _stringout_shape(sidecars).asset.fps if sidecars else "30/1"
    cursor = 0
    for sc in sidecars:
        length = max(1, frames(sc.asset.duration, seq_fps))
        on_spine = which.needs_timeline
        _append_clip(
            parent, sc, resources.assets[sc.asset.stem],
            resources.formats[_format_key(sc)], taxonomy, which,
            offset=from_frames(cursor, seq_fps) if on_spine else "0s",
            duration=from_frames(length, seq_fps) if on_spine else None,
        )
        cursor += length

    return finish(root)


def _stringout(
    event: ET.Element,
    sidecars: list[Sidecar],
    resources: Resources,
    name: str,
) -> ET.Element:
    """A `<spine>` for the clips to be laid on, end to end.

    Resolve's importer has one door -- `ImportTimelineFromFile`, and File >
    Import > Timeline behind the menus -- and it wants a timeline. An event
    full of clips is a document it reads without error and makes nothing out
    of, which is the worst failure available. The timeline is the vehicle: the
    importer brings the source clips into the media pool along with it, and the
    media pool is what the material export is actually for.

    The spine takes the shape that holds the most footage, the same rule a
    reel's timeline follows -- a single 720p landscape clip at the head of a
    drop of thirty portrait ones would otherwise make the whole stringout
    720p, and everything after it a letterbox.
    """
    shape = _stringout_shape(sidecars)
    fps = shape.asset.fps
    total = sum(max(1, frames(sc.asset.duration, fps)) for sc in sidecars)
    project = ET.SubElement(event, "project", name=f"{name} — new footage")
    sequence = ET.SubElement(
        project, "sequence",
        format=resources.formats[_format_key(shape)],
        duration=from_frames(total, fps),
        tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k",
    )
    return ET.SubElement(sequence, "spine")


def _stringout_shape(sidecars: list[Sidecar]) -> Sidecar:
    """Whichever shape holds the most seconds. Ties go to first appearance."""
    weight: dict[tuple, float] = {}
    first: dict[tuple, Sidecar] = {}
    for sc in sidecars:
        key = _format_key(sc)
        weight[key] = weight.get(key, 0.0) + sc.asset.duration
        first.setdefault(key, sc)
    return first[max(weight, key=lambda k: weight[k])]


def _marker_note(seg, taxonomy: Taxonomy) -> str:
    """What a marker says beyond its name, for an editor with no keywords.

    Resolve shows a marker's note in the Edit Index and in the marker popover,
    and it is searchable there. The tags go in it because without them a
    stringout is a hundred named ranges and no way to ask which are about
    money. The summary follows, clipped: a note long enough to need scrolling
    is one nobody reads.
    """
    labels = [taxonomy.label(t) for t in seg.tags]
    parts = [", ".join(dict.fromkeys(labels))] if labels else []
    if seg.summary:
        parts.append(seg.summary[:240])
    return " — ".join(p for p in parts if p)


def _append_clip(
    parent: ET.Element,
    sc: Sidecar,
    asset_id: str,
    format_id: str,
    taxonomy: Taxonomy,
    editor: Editor | None = None,
    offset: str = "0s",
    duration: str | None = None,
) -> ET.Element:
    which = editor or editors.FINAL_CUT
    fps = sc.asset.fps
    clip = ET.SubElement(
        parent,
        "asset-clip",
        name=sc.asset.stem,
        ref=asset_id,
        offset=offset,
        start="0s",
        # In a browser a clip's duration is its own. On a spine it is the
        # timeline's, and the two disagree by a few milliseconds whenever the
        # clip's frame rate is not the sequence's -- enough to leave the last
        # clip hanging past the end of the sequence it is inside.
        duration=duration or rational(sc.asset.duration, fps),
        format=format_id,
        audioRole="dialogue",
    )

    # Order matters to the DTD: <note> before the marker-item group.
    if sc.analysis.errors:
        note = ET.SubElement(clip, "note")
        note.text = "splice: " + "; ".join(sc.analysis.errors)[:500]

    clip_end = snap(sc.asset.duration, fps)
    for seg in sc.segments:
        start = snap(max(0.0, seg.start), fps)
        end = min(snap(seg.end, fps), clip_end)
        if end <= start:
            continue
        t_start, t_dur = rational(start, fps), rational(end - start, fps)

        labels = [taxonomy.label(t) for t in seg.tags]
        if labels and which.keyword_ranges:
            # One keyword element carrying several comma-separated values, so a
            # single range joins several keyword collections at once.
            ET.SubElement(
                clip, "keyword", start=t_start, duration=t_dur,
                value=", ".join(dict.fromkeys(labels)),
            )

        if seg.rating in ("favorite", "rejected") and which.ratings:
            ET.SubElement(
                clip, "rating", name=seg.title[:60],
                value="favorite" if seg.rating == "favorite" else "reject",
                start=t_start, duration=t_dur,
            )

        # A marker at each segment start makes titles readable on the
        # filmstrip. For an editor with no keyword ranges it is the whole of
        # what a segment becomes, so it takes the segment's length and carries
        # the tags in its note rather than being a point with a name.
        marker = {"start": t_start, "value": seg.title[:120]}
        if which.marker_ranges:
            marker["duration"] = t_dur
            note_text = _marker_note(seg, taxonomy)
            if note_text:
                marker["note"] = note_text
        else:
            marker["duration"] = frame_duration(fps)
        ET.SubElement(clip, "marker", **marker)

    return clip


def _file_url(path: Path) -> str:
    return "file://" + quote(str(path.resolve()), safe="/")


@dataclass
class Reconciliation:
    """What reading the editor's library changed, and what it revealed."""

    seen: bool = False                       # could we read the library at all
    confirmed: list[str] = field(default_factory=list)   # offers that landed
    withdrawn: list[str] = field(default_factory=list)   # offers that never did
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    in_library: int = 0
    editor: str = editors.FCP


def held_clips(editor: Editor | str | None = None) -> dict[str, int] | None:
    """What the editor holds: clip stem -> copies, or None if it cannot be read.

    The two readers are not alike. Final Cut's catalogue is a file, so it
    answers whether or not Final Cut is running; Resolve's answers only while
    Resolve is open. Both return None when they cannot see, and that is the
    contract every caller here is written against.
    """
    which = editors.get(editor)
    if which.name == editors.RESOLVE:
        from . import resolve

        return resolve.imported_clips()
    return fcp.imported_clips()


def held_duplicates(editor: Editor | str | None = None) -> dict[str, list[str]]:
    which = editors.get(editor)
    if which.name == editors.RESOLVE:
        from . import resolve

        return resolve.duplicate_names()
    return fcp.duplicate_names()


def _names_for(sidecar: Sidecar, editor: str = editors.FCP) -> list[str]:
    """Every name the editor might hold this clip under, most likely first."""
    candidates = [sidecar.delivery(editor).fcp_name, sidecar.asset.stem]
    candidates += reversed(sidecar.asset.previous_names)
    seen, out = set(), []
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _landed(record: ExportRecord, copies: int, which: Editor) -> bool:
    """Is an outstanding offer one the editor can be seen to have taken?

    Final Cut cannot update a clip from XML, so a re-offer arrives as a second
    copy and a count that has grown since the offer is the proof. Resolve
    matches a re-import against the path it already has and reuses the clip, so
    there is no count to grow: the clip being in the pool is the whole of what
    can be observed, and an outstanding offer for a clip that is there is taken
    as landed.

    That second rule believes something it did not watch happen, and the
    difference from the bug that lost eight clips is worth being precise about:
    there, a clip that was never imported counted as delivered and vanished
    from the queue. Here the clip is demonstrably in the pool. What is not
    verified is whether the new markers came with it -- and a stale marker
    costs a re-import, not a clip.
    """
    if not record.offered_at:
        return False
    if not record.imported_at:
        return True
    return copies > record.fcp_copies if which.duplicates_on_reimport else True


def preview(
    sidecars: list[Sidecar], editor: Editor | str | None = None
) -> Reconciliation | None:
    """What `reconcile` would change, without changing it.

    Same matching, no writes: `sync --dry-run` has to be able to say that eight
    clips are about to come back without being the thing that brings them back.
    """
    which = editors.get(editor)
    held = held_clips(which)
    if held is None:
        return None

    report = Reconciliation(
        seen=True, in_library=sum(held.values()), editor=which.name
    )
    report.duplicates = held_duplicates(which)
    for sidecar in sidecars:
        record = sidecar.delivery(which.name)
        name = next((n for n in _names_for(sidecar, which.name) if n in held), None)
        if name is None:
            if record.offered_at or record.imported_at:
                report.withdrawn.append(sidecar.asset.stem)
        elif not record.imported_at or _landed(record, held[name], which):
            report.confirmed.append(sidecar.asset.stem)
    return report


def projected_pending(
    sidecars: list[Sidecar], editor: Editor | str | None = None
) -> tuple[list[Sidecar], list[Sidecar]]:
    """`pending` as it would read after a reconcile, computed without writing."""
    which = editors.get(editor)
    report = preview(sidecars, which)
    if report is None:
        return pending(sidecars, which)

    withdrawn = set(report.withdrawn)
    fresh, changed = [], []
    for sidecar in sidecars:
        if not sidecar.segments:
            continue
        record = sidecar.delivery(which.name)
        if sidecar.asset.stem in withdrawn or not record.delivered:
            fresh.append(sidecar)
        elif record.imported_sha != sidecar.content_sha():
            changed.append(sidecar)
    return fresh, changed


def reconcile(
    sidecars: list[Sidecar], editor: Editor | str | None = None
) -> Reconciliation:
    """Correct each sidecar's import state against what the editor holds.

    Delivery is observed here and asserted nowhere else. A clip that was offered
    and never imported has its offer withdrawn and rejoins the queue; a clip the
    editor turns out to hold is recorded as delivered even if nothing here ever
    offered it.

    An unreadable library returns `seen=False` and changes nothing -- guessing
    in either direction is worse than trusting what the sidecars already say.
    That case is the normal one for Resolve, which can only be read while it is
    running.
    """
    which = editors.get(editor)
    held = held_clips(which)
    if held is None:
        return Reconciliation(seen=False, editor=which.name)

    report = Reconciliation(
        seen=True, in_library=sum(held.values()), editor=which.name
    )
    report.duplicates = held_duplicates(which)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for sidecar in sidecars:
        record = sidecar.delivery(which.name, attach=True)
        name = next((n for n in _names_for(sidecar, which.name) if n in held), None)

        if name is None:
            if record.offered_at or record.imported_at:
                # The editor does not have it, whatever we believed. This is the
                # case that lost eight clips: put it back in the queue.
                report.withdrawn.append(sidecar.asset.stem)
                sidecar.set_delivery(which.name, ExportRecord())
                sidecar.save(config.sidecar_path(sidecar.asset.stem))
            continue

        copies = held[name]
        if _landed(record, copies, which) or not record.imported_at:
            record.imported_at = stamp
            record.imported_sha = record.offered_sha or sidecar.content_sha()
            record.fcp_name = name
            record.fcp_copies = copies
            record.offered_at, record.offered_sha = None, ""
            report.confirmed.append(sidecar.asset.stem)
            sidecar.save(config.sidecar_path(sidecar.asset.stem))
        elif record.fcp_name != name or record.fcp_copies != copies:
            record.fcp_name, record.fcp_copies = name, copies
            sidecar.save(config.sidecar_path(sidecar.asset.stem))

    return report


def pending(
    sidecars: list[Sidecar], editor: Editor | str | None = None
) -> tuple[list[Sidecar], list[Sidecar]]:
    """(never delivered, delivered but changed since).

    Both belong in the next import. The second list is the awkward one -- Final
    Cut cannot update a clip from XML, so those arrive as a second copy that you
    delete afterwards -- so it stays a separate list worth reporting, not a
    silent addition. Resolve reuses the clip it has instead, and the re-offer
    reaches it as a fresh stringout rather than a second copy in the pool.
    """
    which = editors.get(editor)
    fresh, changed = [], []
    for sidecar in sidecars:
        if not sidecar.segments:
            continue
        record = sidecar.delivery(which.name)
        if not record.delivered:
            fresh.append(sidecar)
        elif record.imported_sha != sidecar.content_sha():
            changed.append(sidecar)
    return fresh, changed


def mark_offered(
    sidecars: list[Sidecar],
    held: dict[str, int] | None = None,
    editor: Editor | str | None = None,
) -> None:
    """Record that these went into an import file -- not that they arrived."""
    which = editors.get(editor)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for sidecar in sidecars:
        record = sidecar.delivery(which.name, attach=True)
        record.offered_at = stamp
        record.offered_sha = sidecar.content_sha()
        if held is not None:
            name = next((n for n in _names_for(sidecar, which.name) if n in held), "")
            record.fcp_copies = held.get(name, 0)
        sidecar.save(config.sidecar_path(sidecar.asset.stem))


def mark_exported(
    sidecars: list[Sidecar], editor: Editor | str | None = None
) -> None:
    """Assert delivery by hand, for a library splice cannot read."""
    which = editors.get(editor)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for sidecar in sidecars:
        sidecar.set_delivery(which.name, ExportRecord(
            imported_at=stamp,
            imported_sha=sidecar.content_sha(),
            fcp_name=sidecar.asset.stem,
            fcp_copies=1,
        ))
        sidecar.save(config.sidecar_path(sidecar.asset.stem))


def write(
    dest: Path | None = None,
    only: str | None = None,
    event_name: str | None = None,
    new_only: bool = False,
    mark: bool = False,
    sidecars: list[Sidecar] | None = None,
    editor: Editor | str | None = None,
) -> tuple[Path | None, int, int]:
    which = editors.get(editor)
    dest = dest or (config.export_dir() / f"{config.export_stem()}.fcpxml")
    taxonomy = Taxonomy.load()

    if sidecars is None:
        sidecars = [sc for sc in library.load_all() if sc.segments]
        if only:
            wanted = set(library.match([sc.asset.stem for sc in sidecars], only))
            sidecars = [sc for sc in sidecars if sc.asset.stem in wanted]
        if new_only:
            sidecars, _ = pending(sidecars, which)
    if not sidecars:
        return None, 0, 0

    write_validated(
        build_document(sidecars, taxonomy, event_name=event_name, editor=which), dest
    )

    if mark:
        mark_offered(sidecars, held_clips(which), which)

    return dest, len(sidecars), sum(len(sc.segments) for sc in sidecars)
