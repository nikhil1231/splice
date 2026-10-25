"""On-demand story discovery followed by source-constrained dialogue selection."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import agy, config, drafts, library, reel


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Story(Strict):
    title: str
    angle: str
    segments: list[str] = Field(min_length=1, max_length=12)


class Discovery(Strict):
    stories: list[Story] = Field(max_length=3)
    reason: str = ""


class Reference(Strict):
    segment_id: str
    first_line: int = Field(ge=0, strict=True)
    last_line: int = Field(ge=0, strict=True)


class Pick(Reference):
    role: str
    alternatives: list[Reference] = Field(default_factory=list, max_length=3)


class Script(Strict):
    title: str
    angle: str
    beats: list[Pick] = Field(min_length=1, max_length=30)


class Scripts(Strict):
    scripts: list[Script] = Field(max_length=1)
    reason: str = ""


def template():
    return config.fill_prompt(config.prompt_path("drafts.md").read_text())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def flat_schema(model):
    # agy's Gemini schema path expects inline objects, like the analysis schema.
    raw = model.model_json_schema()
    definitions = raw.get("$defs", {})

    def expand(node):
        if isinstance(node, list):
            return [expand(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return expand(definitions[node["$ref"].rsplit("/", 1)[-1]])
        return {k: expand(v) for k, v in node.items() if k not in {"$defs", "default"}}
    return expand(raw)


def ask(instruction, data, schema, key):
    prompt = template() + "\n" + instruction + "\nDATA:\n" + encoded(data)
    if len(prompt.encode()) > config.DRAFT_PROMPT_CHARS:
        raise ValueError("Selected dialogue exceeds the drafting budget. Choose fewer moments.")
    with tempfile.TemporaryDirectory(prefix="splice-draft-") as work:
        path = Path(work) / "schema.json"
        path.write_text(json.dumps(flat_schema(schema)))
        for attempt in range(config.AGY_RETRIES + 1):
            try:
                raw = agy.run_structured(prompt, path, model=config.DRAFT_MODEL,
                                         allow_tools=False, required_key=key)
                return schema.model_validate(raw)
            except (agy.AnalysisError, ValueError):
                if attempt == config.AGY_RETRIES:
                    raise


def batches(rows, overhead=8000):
    budget = config.DRAFT_PROMPT_CHARS - len(template().encode()) - overhead
    batch, size = [], 0
    for row in rows:
        length = len(encoded(row).encode()) + 1
        if length > budget:
            raise ValueError("One moment exceeds the discovery budget; select a smaller source.")
        if batch and size + length > budget:
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += length
    if batch:
        yield batch


def reference(ref: Reference, eligible):
    if ref.segment_id not in eligible:
        raise ValueError(f"Model selected an unknown or unselectable moment: {ref.segment_id}")
    sc, s = eligible[ref.segment_id]
    if not 0 <= ref.first_line <= ref.last_line < len(s.lines):
        raise ValueError("Model selected an invalid dialogue range")
    lines = s.lines[ref.first_line:ref.last_line + 1]
    if any(l.end <= l.start or l.start < 0 or l.end > sc.asset.duration for l in lines):
        raise ValueError("Selected dialogue has invalid source timings; correct the transcript first")
    if any(b.start < a.start for a, b in zip(lines, lines[1:])):
        raise ValueError("Selected transcript lines are out of time order")
    return drafts.capture(drafts.Excerpt(stem=sc.asset.stem, fingerprint=sc.asset.fingerprint,
                                        segment_id=s.id, start=lines[0].start,
                                        end=max(l.end for l in lines)), [sc])


def generate(brief="", selected=None, progress=lambda message: None):
    if not isinstance(brief, str) or len(brief) > 3000:
        raise ValueError("Keep the creative brief under 3,000 characters")
    if selected is not None and (not isinstance(selected, list) or
                                 any(not isinstance(s, str) for s in selected)):
        raise ValueError("selected moments must be a list of segment ids")
    sidecars = library.load_all()
    eligible = {s.id: (sc, s) for sc in sidecars for s in sc.segments
                if s.rating != "rejected" and s.lines}
    if selected:
        missing = set(selected) - eligible.keys()
        if missing:
            raise ValueError("Selected moments are missing, rejected, or have no dialogue: " + ", ".join(sorted(missing)))
        eligible = {sid: eligible[sid] for sid in selected}
    if not eligible:
        raise ValueError("No eligible timestamped dialogue. Analyse or select some footage first.")
    cuts = reel.load_all()
    rows = []
    for sid, (sc, s) in eligible.items():
        used = any(shot.stem in [sc.asset.stem, *sc.asset.previous_names] and
                   shot.end > s.start and shot.start < s.end
                   for cut in cuts for shot in cut.shots)
        rows.append(dict(id=sid, title=s.title, summary=s.summary, tags=s.tags,
                         duration=s.duration, favourite=s.rating == "favorite", already_used=used))
    groups = list(batches(rows))
    stories, said = [], []
    for i, group in enumerate(groups):
        progress(f"Finding stories · batch {i + 1}/{len(groups)}")
        found = ask("Find up to three distinct candidate stories. Use only these segment ids.",
                    dict(brief=brief, moments=group), Discovery, "stories")
        allowed = {r["id"] for r in group}
        if any(set(s.segments) - allowed for s in found.stories):
            raise ValueError("Discovery returned a source it was not given")
        stories.extend(found.stories)
        if not found.stories and found.reason:
            said.append(found.reason)
    # Reduce in bounded groups so a large library never becomes one huge prompt.
    while len(stories) > 3:
        reduced = []
        for group in batches([s.model_dump() for s in stories]):
            progress("Choosing distinct story candidates")
            found = ask("Select up to three strongest distinct stories from these proposals.",
                        dict(brief=brief, proposals=group), Discovery, "stories")
            allowed = {sid for s in group for sid in s["segments"]}
            if any(set(s.segments) - allowed for s in found.stories):
                raise ValueError("Story selection returned an unknown source")
            reduced.extend(found.stories)
        if not reduced:
            # Selecting is not vetoing. Discovery had already judged these worth
            # proposing; a reducer that answers "these all cover the same ground"
            # was throwing all six away and reporting an empty archive, on a
            # library whose 192 moments always take two batches to read.
            stories = stories[:3]
            break
        if len(reduced) >= len(stories):
            raise ValueError("Discovery could not reduce its candidates within the prompt budget")
        stories = reduced
    if not stories:
        raise ValueError("No supported story candidates found. Try a broader brief or "
                         "different moments." + ("" if not said else " The model said: "
                                                 + " ".join(said)))
    result = []
    for i, story in enumerate(stories):
        progress(f"Drafting dialogue · candidate {i + 1}/{len(stories)}")
        chosen = {sid: eligible[sid] for sid in story.segments}
        material, context = [], {}
        for sid, (sc, s) in chosen.items():
            material.append(dict(segment_id=sid, title=s.title, selectable=True,
                                 lines=[dict(index=j, **l.model_dump()) for j, l in enumerate(s.lines)]))
            ordered = sorted(sc.segments, key=lambda x: x.start)
            at = ordered.index(s)
            for neighbour in ordered[max(0, at - 1):at + 2]:
                if neighbour.id not in chosen:
                    context[neighbour.id] = dict(title=neighbour.title, selectable=False,
                        lines=[l.model_dump() for l in neighbour.lines])
        response = ask("Write one draft script for this angle, using only selectable references. "
                       "Aim for 30–90 seconds. Return fewer beats if the material needs it.",
                       dict(brief=brief, story=story.model_dump(), material=material,
                            context=list(context.values())), Scripts, "scripts")
        for script in response.scripts:
            beats = []
            for pick in script.beats:
                excerpt = reference(pick, chosen)
                beats.append(drafts.Beat(**excerpt.model_dump(), role=pick.role,
                    alternatives=[reference(a, chosen) for a in pick.alternatives]))
            result.append(drafts.Draft(title=script.title, angle=script.angle, brief=brief,
                beats=beats, provenance=dict(model=config.DRAFT_MODEL, generated_at=drafts.now(),
                    prompt_sha=hashlib.sha256(template().encode()).hexdigest()[:12],
                    selected=selected or [], story=story.model_dump())))
    if not result:
        raise ValueError("The selected stories have insufficient dialogue. Try a different brief.")
    return result


class Jobs:
    """One generation at a time; opening the page again reconnects to its status."""
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None

    def status(self):
        with self.lock:
            return dict(self.job) if self.job else None

    def start(self, body, persist):
        with self.lock:
            if self.job and self.job["state"] == "running":
                return dict(self.job)
            self.job = dict(id=drafts.token(), state="running", message="Starting discovery",
                            error="", drafts=[], request=body)

        def progress(message):
            with self.lock:
                self.job["message"] = message

        def run():
            try:
                found = generate(body.get("brief", ""), body.get("selected"), progress)
                ids = persist(found)
                with self.lock:
                    self.job.update(state="done", message=f"{len(ids)} drafts ready", drafts=ids)
            except Exception as exc:
                with self.lock:
                    self.job.update(state="failed", message="Draft generation failed", error=str(exc))

        threading.Thread(target=run, daemon=True, name="draft-generation").start()
        return self.status()
