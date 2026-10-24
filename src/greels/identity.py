"""Decide what a file in raw/ actually is.

`raw/` is a drop bin, and the thing you drop is not always new. It may be a clip
already in the archive under the name the camera gave it before `rename` renamed
it; a clip renamed in Finder rather than here; or a re-export that replaced a
file in place. Keying on the filename -- which is what this used to do -- reads
all three as new footage and pays for each with a proxy, a Gemini run and a
duplicate clip in Final Cut.

Identity is the fingerprint. This module compares what is on disk against what
the sidecars already know and returns the decision; `scan` carries it out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import config
from .schema import Sidecar


class Verdict(str, Enum):
    NEW = "new"                # never seen; scan it
    UNCHANGED = "unchanged"    # same bytes, same name; nothing to do
    ADOPTED = "adopted"        # same bytes, renamed elsewhere; follow it
    DUPLICATE = "duplicate"    # same bytes as a clip still sitting in raw/
    REPLACED = "replaced"      # same name, different bytes; re-probe


@dataclass
class Finding:
    path: Path
    verdict: Verdict
    stem: str
    # For ADOPTED and DUPLICATE: the stem this file's content is already known by.
    known_as: str = ""
    # The fingerprint as computed now, empty when the stored one was trusted.
    # `scan` re-probes whenever this differs from what the sidecar holds, which
    # is what backfills identity onto sidecars written before it existed.
    digest: str = ""

    @property
    def note(self) -> str:
        if self.verdict is Verdict.ADOPTED:
            return f"{self.known_as} -> {self.stem} (renamed outside greels)"
        if self.verdict is Verdict.DUPLICATE:
            return f"{self.stem} is the same footage as {self.known_as}"
        if self.verdict is Verdict.REPLACED:
            return f"{self.stem} was replaced on disk"
        return self.stem


@dataclass
class Survey:
    findings: list[Finding] = field(default_factory=list)

    def of(self, *verdicts: Verdict) -> list[Finding]:
        return [f for f in self.findings if f.verdict in verdicts]

    @property
    def duplicates(self) -> list[Finding]:
        return self.of(Verdict.DUPLICATE)

    @property
    def adopted(self) -> list[Finding]:
        return self.of(Verdict.ADOPTED)

    @property
    def replaced(self) -> list[Finding]:
        return self.of(Verdict.REPLACED)

    @property
    def skip_stems(self) -> set[str]:
        """Stems no later stage should touch: a duplicate is not its own clip."""
        return {f.stem for f in self.duplicates}


def fingerprint_map(sidecars: list[Sidecar]) -> dict[str, Sidecar]:
    """fingerprint -> sidecar, for the sidecars that have one.

    Built fresh from index/*.seg.json on every run rather than cached in a second
    store: the sidecars stay the only thing that has to be correct.
    """
    return {sc.asset.fingerprint: sc for sc in sidecars if sc.asset.fingerprint}


def _needs_rehash(sidecar: Sidecar, path: Path) -> bool:
    """A fingerprint is trusted until the file's size or mtime says otherwise."""
    if not sidecar.asset.fingerprint:
        return True
    stat = path.stat()
    return (
        sidecar.asset.size_bytes != stat.st_size
        or abs(sidecar.asset.mtime - stat.st_mtime) > 1e-6
    )


def survey(paths: list[Path], sidecars: list[Sidecar]) -> Survey:
    """Classify everything in raw/ against what the archive already knows."""
    from .probe import fingerprint

    by_stem = {sc.asset.stem: sc for sc in sidecars}
    by_fingerprint = fingerprint_map(sidecars)
    present = {p.stem for p in paths}
    survey = Survey()
    # A clip can only be adopted once; a second file with the same content is a
    # duplicate of it, not a second rename.
    adopted: set[str] = set()

    for path in paths:
        stem = path.stem
        known = by_stem.get(stem)

        if known is not None and not _needs_rehash(known, path):
            survey.findings.append(
                Finding(path, Verdict.UNCHANGED, stem, digest=known.asset.fingerprint)
            )
            continue

        digest = fingerprint(path)
        match = by_fingerprint.get(digest)

        if known is not None:
            # No stored fingerprint means this sidecar predates identity, not
            # that the footage changed. Treating the two the same would mark the
            # whole archive replaced and re-analyse it -- hours of Gemini time to
            # rebuild transcripts that were already correct.
            replaced = bool(known.asset.fingerprint) and known.asset.fingerprint != digest
            verdict = Verdict.REPLACED if replaced else Verdict.UNCHANGED
            survey.findings.append(Finding(path, verdict, stem, digest=digest))
            continue

        if match is None:
            survey.findings.append(Finding(path, Verdict.NEW, stem, digest=digest))
            continue

        # Content we hold, under a name we do not. Either the file moved, or
        # this is a second copy of a file that is still where it was.
        old = match.asset.stem
        gone = old not in present and not (config.raw_dir() / match.asset.file).exists()
        if gone and old not in adopted:
            adopted.add(old)
            survey.findings.append(
                Finding(path, Verdict.ADOPTED, stem, known_as=old, digest=digest)
            )
        else:
            survey.findings.append(
                Finding(path, Verdict.DUPLICATE, stem, known_as=old, digest=digest)
            )

    return survey


def adopt(finding: Finding) -> None:
    """Follow a rename that happened outside greels.

    The analysis is the expensive part and none of it depends on the filename,
    so this moves the sidecar, proxy, chunks and segment ids across and keeps
    every segment. Re-analysing here would burn Gemini time to arrive back at
    the transcript we already have.
    """
    from .rename import relocate

    relocate(finding.known_as, finding.stem, finding.path.name)


def duplicate_paths(survey: Survey) -> list[Path]:
    return [f.path for f in survey.duplicates]
