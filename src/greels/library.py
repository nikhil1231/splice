"""Discovery of source footage and access to the sidecar store."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from . import config
from .schema import Sidecar


def discover_raw() -> list[Path]:
    """Source clips in raw/, one per stem.

    raw/ is a drop bin, so this recurses: a folder of clips copied straight off
    a phone is picked up as-is. Where the same stem appears with several
    extensions, SUFFIX_PRIORITY keeps the original over any downscale.
    """
    root = config.raw_dir()
    if not root.is_dir():
        return []

    by_stem: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if set(path.relative_to(root).parts[:-1]) & config.RAW_SKIP_DIRS:
            continue
        if path.suffix.lower() not in config.VIDEO_SUFFIXES:
            continue

        current = by_stem.get(path.stem)
        if current is None or _rank(path) < _rank(current):
            by_stem[path.stem] = path

    return [by_stem[stem] for stem in sorted(by_stem)]


def _rank(path: Path) -> int:
    suffix = path.suffix.lower()
    return (
        config.SUFFIX_PRIORITY.index(suffix)
        if suffix in config.SUFFIX_PRIORITY
        else len(config.SUFFIX_PRIORITY)
    )


def match(stems: list[str], pattern: str | None) -> list[str]:
    """Filter stems by a glob. A bare substring is treated as *sub*."""
    if not pattern:
        return stems
    if not any(ch in pattern for ch in "*?["):
        pattern = f"*{pattern}*"
    return [s for s in stems if fnmatch.fnmatch(s, pattern)]


def sidecars() -> list[Path]:
    root = config.index_dir()
    return sorted(root.glob("*.seg.json")) if root.is_dir() else []


def load_sidecar(stem: str) -> Sidecar | None:
    path = config.sidecar_path(stem)
    return Sidecar.load(path) if path.exists() else None


def load_all() -> list[Sidecar]:
    out = []
    for path in sidecars():
        try:
            out.append(Sidecar.load(path))
        except Exception as exc:  # a hand-edited sidecar should not kill a run
            print(f"  ! skipping malformed {path.name}: {exc}")
    return out


def proxy_for(stem: str) -> Path:
    return config.proxy_dir() / f"{stem}.mp4"


def chunks_for(stem: str) -> list[tuple[float, Path]]:
    """Chunks of a proxy as (offset_seconds, path), in order."""
    out = []
    for path in sorted(config.chunk_dir().glob(f"{stem}__*.mp4")):
        try:
            offset = float(path.stem.rsplit("__", 1)[1])
        except (IndexError, ValueError):
            continue
        out.append((offset, path))
    return sorted(out)
