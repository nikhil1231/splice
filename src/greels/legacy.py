"""Read the old hand-driven video_logs.txt.

Its minute-bucket prose is not good enough to edit from, but it is a real
record of what is in each clip. Feeding it back as a hint means the new pass
starts from what you already found rather than rediscovering it.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config

_HEADER = re.compile(r"^##\s+(?P<file>\S+?)\s*(?:\(Original:\s*(?P<orig>[^)]+)\))?\s*$")


def parse(path: Path | None = None) -> dict[str, dict[str, str]]:
    """{stem: {'original': 'IMG_6272.MOV', 'prose': '- **00:00...'}}"""
    path = path or config.legacy_log_path()
    if not path.exists():
        return {}

    out: dict[str, dict[str, str]] = {}
    stem: str | None = None
    body: list[str] = []

    def flush() -> None:
        if stem:
            out[stem]["prose"] = "\n".join(body).strip()

    for line in path.read_text().splitlines():
        header = _HEADER.match(line)
        if header:
            flush()
            stem = Path(header.group("file")).stem
            body = []
            out[stem] = {"original": (header.group("orig") or "").strip(), "prose": ""}
        elif stem:
            body.append(line)
    flush()
    return out
