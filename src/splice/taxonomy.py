"""The controlled tag vocabulary.

Tags the model invents are not rejected, just quarantined into
`proposed_tags`. They stay out of FCPXML until promoted, which keeps the Final
Cut keyword sidebar from fragmenting into near-duplicates.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import config

_SLUG = re.compile(r"[^a-z0-9]+")


def _save(path: Path, data: dict) -> None:
    """Write the vocabulary through a temporary name.

    It is a source file, hand-edited and backed up with the project, and it is
    what decides what every segment means. Half of it is worse than an old copy
    of it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(path.suffix + ".tmp")
    staged.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))
    staged.replace(path)


def _source(path: Path | None) -> Path:
    """The file to read: the one asked for, else the project's own, else the
    default vocabulary a new project starts from."""
    if path is not None:
        return path
    own = config.taxonomy_path()
    return own if own.is_file() else config.default_taxonomy_path()


def _editable(path: Path | None) -> tuple[Path, dict]:
    """(where a change is written, what it is written over). A project still
    on the default vocabulary gets its own copy on its first change, rather
    than a change to the copy every project shares."""
    target = path or config.taxonomy_path()
    source = target if target.is_file() else _source(None)
    return target, yaml.safe_load(source.read_text()) or {}


class Taxonomy:
    def __init__(
        self,
        namespaces: list[str],
        tags: dict[str, dict],
        aliases: dict[str, str] | None = None,
    ):
        self.namespaces = namespaces
        self.tags = tags
        # The other half of quarantine. Promoting theme:historical alongside the
        # tone:historical that already exists is exactly the fragmentation the
        # quarantine is meant to prevent, so a near-duplicate gets folded in
        # rather than accepted or thrown away.
        self.aliases = aliases or {}

    @classmethod
    def load(cls, path: Path | None = None) -> "Taxonomy":
        data = yaml.safe_load(_source(path).read_text()) or {}
        namespaces = list(data.get("namespaces", []))
        tags = dict(data.get("tags", {}) or {})
        # The people in splice.yaml are vocabulary whether or not anybody wrote
        # them in here too: one list of who is in the footage, not two.
        for person in config.project().people:
            tags.setdefault(f"person:{person.id}", {"fcp_label": person.label})
            if "person" not in namespaces:
                namespaces.insert(0, "person")
        return cls(namespaces, tags, data.get("aliases", {}) or {})

    def resolve(self, tag: str) -> str:
        """Fold a known near-duplicate onto its canonical tag."""
        return self.aliases.get(tag, tag)

    def alias(self, source: str, target: str, path: Path | None = None) -> str:
        source, target = normalise(source), normalise(target)
        if target not in self.tags:
            raise ValueError(f"{target} is not in the vocabulary")
        if source in self.tags:
            raise ValueError(f"{source} is already a real tag; remove it first")

        path, data = _editable(path)
        data.setdefault("aliases", {})[source] = target
        _save(path, data)
        self.aliases[source] = target
        return target

    def label(self, tag: str) -> str:
        entry = self.tags.get(tag) or {}
        if entry.get("fcp_label"):
            return entry["fcp_label"]
        # Fall back to a readable form of the value half.
        value = tag.split(":", 1)[-1]
        return value.replace("_", " ").title()

    def known(self, tag: str) -> bool:
        return tag in self.tags

    def split(self, tags: list[str]) -> tuple[list[str], list[str]]:
        """(known, proposed), each normalised and deduplicated in order."""
        known: list[str] = []
        proposed: list[str] = []
        for raw in tags:
            tag = self.resolve(normalise(raw))
            if not tag:
                continue
            bucket = known if self.known(tag) else proposed
            if tag not in bucket:
                bucket.append(tag)
        return known, proposed

    def prompt_block(self) -> str:
        """The vocabulary as shown to the model, grouped by namespace."""
        lines = []
        for ns in self.namespaces:
            values = [t.split(":", 1)[1] for t in self.tags if t.startswith(f"{ns}:")]
            if values:
                lines.append(f"- {ns}: {', '.join(values)}")
        return "\n".join(lines)

    def promote(self, tag: str, label: str | None = None, path: Path | None = None) -> str:
        tag = normalise(tag)
        if not tag or ":" not in tag:
            raise ValueError(f"tag must be namespace:value, got {tag!r}")
        if tag in self.tags:
            return tag
        label = label or tag.split(":", 1)[1].replace("_", " ").title()

        path, data = _editable(path)
        data.setdefault("tags", {})[tag] = {"fcp_label": label}
        ns = tag.split(":", 1)[0]
        if ns not in data.setdefault("namespaces", []):
            data["namespaces"].append(ns)
        _save(path, data)

        self.tags[tag] = {"fcp_label": label}
        if ns not in self.namespaces:
            self.namespaces.append(ns)
        return tag


def normalise(tag: str) -> str:
    """'Theme: Old Photos' -> 'theme:old_photos'. Bare tags get no namespace."""
    tag = (tag or "").strip().lower()
    if not tag:
        return ""
    if ":" in tag:
        ns, _, value = tag.partition(":")
        ns = _SLUG.sub("_", ns).strip("_")
        value = _SLUG.sub("_", value).strip("_")
        return f"{ns}:{value}" if ns and value else ""
    return _SLUG.sub("_", tag).strip("_")
