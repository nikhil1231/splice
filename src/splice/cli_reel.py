"""`splice reel` -- assemble, trim and export a cut.

Everything else in this CLI is about the archive. This is the part you use
while actually making something: take a suggestion or a search, put the
moments in an order, trim them, and hand Final Cut a timeline.

Shots are addressed by their position in the cut, 1-based, because that is how
you think about a running order. Segment ids work too where a position would
be ambiguous.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from . import editors, library, reel as store
from .reel import Reel, Shot
from .schema import Line, Segment, Sidecar

reel_app = typer.Typer(
    help="Assemble reels and export them as Final Cut timelines.", no_args_is_help=True
)


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60:d}:{total % 60:02d}"


def _load(slug: str) -> Reel:
    reel = store.load(slug)
    if reel is None:
        known = [r.slug for r in store.load_all()]
        hint = f"  have: {', '.join(known)}" if known else "  none yet -- `splice reel new`"
        typer.echo(f"no reel {slug!r}\n{hint}")
        raise typer.Exit(1)
    return reel


def _matches(query: str, sidecars: list[Sidecar]) -> list[tuple[Sidecar, Segment]]:
    """Segments a query names: an exact id, else a substring of id or title.

    Deliberately reads the sidecars rather than the SQLite index -- building a
    cut should not require a rebuild to be current.
    """
    exact = [
        (sc, seg) for sc in sidecars for seg in sc.segments if seg.id == query
    ]
    if exact:
        return exact
    needle = query.lower()
    return [
        (sc, seg)
        for sc in sidecars
        for seg in sc.segments
        if needle in seg.id.lower() or needle in seg.title.lower()
    ]


def _resolve_one(query: str, sidecars: list[Sidecar]) -> tuple[Sidecar, Segment]:
    found = _matches(query, sidecars)
    if not found:
        typer.echo(f"no segment matching {query!r}")
        raise typer.Exit(1)
    if len(found) > 1:
        typer.echo(f"{len(found)} segments match {query!r}:")
        for sc, seg in found[:12]:
            typer.echo(f"  {seg.id:52} {seg.title}")
        typer.echo("\nnarrow it, or pass a full segment id")
        raise typer.Exit(1)
    return found[0]


def _from_phrase(phrase: str, sidecars: list[Sidecar], limit: int = 40) -> list[Shot]:
    """Every line matching a phrase, one shot each, in the order they happen.

    A supercut: every time somebody mentions money, back to back. In the order
    the footage was taken rather than by relevance -- a running order is a
    sequence of things that happened, and relevance is not one.
    """
    from .index import search_lines

    try:
        hits = search_lines(phrase, limit=limit)
    except FileNotFoundError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    if not hits:
        typer.echo(f"! nothing said matches {phrase!r}")
        raise typer.Exit(1)

    by_stem = {sc.asset.stem: sc for sc in sidecars}
    when = {sc.asset.stem: (sc.asset.created or "") for sc in sidecars}
    hits.sort(key=lambda h: (when.get(h["stem"], ""), h["stem"], h["start"]))

    out: list[Shot] = []
    for hit in hits:
        sidecar = by_stem.get(hit["stem"])
        segment = next(
            (s for s in sidecar.segments if s.id == hit["segment_id"]), None
        ) if sidecar else None
        if sidecar is None or segment is None:
            continue
        out.append(_cut_to_line(sidecar, segment, hit))
    if len(hits) >= limit:
        typer.echo(
            f"“{phrase}” is said at least {len(out)} time(s); this is the first "
            f"{limit} — raise --limit or quote more of the sentence"
        )
    else:
        typer.echo(f"“{phrase}” is said {len(out)} time(s)")
    return out


def _cut_to_line(sidecar: Sidecar, segment: Segment, hit: dict) -> Shot:
    """One line hit as a shot, snapped to where the sentence really runs."""
    from . import speech

    spoken = Line(start=hit["start"], end=hit["end"],
                  en=hit["en"] or "", kok=hit["kok"] or "")
    shot = store.shot_from_line(sidecar, segment, spoken)
    start, end = speech.snap_range(
        speech.runs_for(sidecar.asset.stem), shot.start, shot.end
    )
    return shot.model_copy(update={
        "start": start, "end": min(end, sidecar.asset.duration),
    })


def _shot_from_phrase(phrase: str, sidecars: list[Sidecar]) -> Shot:
    """The one line matching a phrase, as a shot cut to it.

    Refuses an ambiguous phrase rather than guessing at the first hit: a cut
    is a decision, and quietly picking one of nine places somebody says
    "rupees" is not one you made.
    """
    from .index import search_lines

    try:
        hits = search_lines(phrase, limit=12)
    except FileNotFoundError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    if not hits:
        typer.echo(f"nothing said matches {phrase!r}")
        raise typer.Exit(1)
    if len(hits) > 1:
        typer.echo(f"{len(hits)} lines match {phrase!r}:")
        for hit in hits:
            typer.echo(f"  {hit['stem'][:32]:34} {_clock(hit['start']):>7}  {hit['en']}")
        typer.echo("\nnarrow it -- quote more of the sentence")
        raise typer.Exit(1)

    hit = hits[0]
    sidecar = next((sc for sc in sidecars if sc.asset.stem == hit["stem"]), None)
    segment = next(
        (s for s in sidecar.segments if s.id == hit["segment_id"]), None
    ) if sidecar else None
    if sidecar is None or segment is None:
        typer.echo(f"! {hit['stem']} is in the index but not in the sidecars; run `splice index`")
        raise typer.Exit(1)
    return _cut_to_line(sidecar, segment, hit)


def _position(reel: Reel, ref: str) -> int:
    """A 1-based position, or a segment id, resolved to an index into shots."""
    if ref.isdigit():
        n = int(ref)
        if not 1 <= n <= len(reel.shots):
            typer.echo(f"! shot {n} is outside 1..{len(reel.shots)}")
            raise typer.Exit(1)
        return n - 1
    hits = [i for i, s in enumerate(reel.shots) if s.segment_id == ref]
    if len(hits) == 1:
        return hits[0]
    typer.echo(f"! no single shot for {ref!r}; use its position (1..{len(reel.shots)})")
    raise typer.Exit(1)


# --- commands ----------------------------------------------------------


@reel_app.command("list")
def reel_list():
    """Every reel, with its length and where it has got to."""
    reels = store.load_all()
    if not reels:
        typer.echo("no reels yet\n  splice reels                       to see suggestions")
        typer.echo("  splice reel new 'Title' --from <tag>  to start one from a suggestion")
        return
    for reel in reels:
        typer.echo(
            f"{reel.slug[:34]:36} {_clock(reel.duration):>6}  "
            f"{len(reel.shots):2d} shots / {len(reel.clips):2d} clips  "
            f"{reel.status:9} {_delivery(reel):24} {reel.title}"
        )
    typer.echo(f"\n{len(reels)} reel(s)")


def _delivery(reel: Reel) -> str:
    """What has been handed over, and whether the cut has moved since.

    Two separate facts: the timeline and the video file go stale
    independently, and a trim made after exporting is in one and not the
    other. Saying "changed" without saying changed *against what* is how you
    re-export the thing that was already current.
    """
    parts = []
    if reel.exported_at:
        # Which editor, because the two go to different files and a bare
        # "exported" no longer says which one is sitting on disk. Blank on
        # every cut exported before there was a second editor, which means
        # Final Cut and is left unsaid rather than guessed at.
        where = ""
        if reel.exported_editor and reel.exported_editor != editors.FCP:
            where = " " + editors.get(reel.exported_editor).short.lower()
        parts.append(("export stale" if reel.stale else "exported") + where)
    if reel.rendered_at:
        parts.append("render stale" if reel.render_stale else "rendered")
    return ", ".join(parts)


SEARCH = typer.Option("", "--search", "-q", help="Full-text query, as `splice search`.")
TAG = typer.Option([], "--tag", "-t", help="Require this tag (repeatable).")
RATING = typer.Option("", "--rating", help="favorite | rejected | none.")
LIMIT = typer.Option(40, "--limit", "-n", help="Most a query may contribute.")
ORDER = typer.Option(
    "found", "--order",
    help="found (as the search returned them) | shot (by when the footage was taken) | long",
)


@reel_app.command("new")
def reel_new(
    title: str = typer.Argument(..., help="What the reel is called."),
    from_tag: str = typer.Option(
        None, "--from", "-f", help="Seed from a `splice reels` suggestion, by tag."
    ),
    segment: list[str] = typer.Option(
        [], "--segment", "-s", help="Seed with this segment (repeatable)."
    ),
    said: str = typer.Option(
        None, "--said",
        help="Every time somebody says this, one shot each. A supercut.",
    ),
    search: str = SEARCH,
    tag: list[str] = TAG,
    rating: str = RATING,
    limit: int = LIMIT,
    order: str = ORDER,
    hook: str = typer.Option("", "--hook", help="The idea, or the opening line."),
):
    """Start a cut -- empty, from a suggestion, from a search, or from segments.

    A search is the quickest way in: `--tag theme:money --rating favorite` is a
    running order you can then argue with, which is a better starting point
    than a blank one.

    `--said` is the other one: every time anybody says a phrase, each as its
    own shot cut to the sentence. "Every time he mentions rupees" is a reel
    format, and it is one command.
    """
    sidecars = library.load_all()
    shots: list[Shot] = []

    if from_tag:
        shots += _from_suggestion(from_tag, sidecars)
    for query in segment:
        sc, seg = _resolve_one(query, sidecars)
        shots.append(store.shot_from_segment(sc, seg))
    if said:
        shots += _from_phrase(said, sidecars, limit)
    if search or tag or rating:
        shots += _from_query(search, list(tag), rating, limit, order, sidecars)

    reel = store.create(title, shots=_dedupe(shots), hook=hook)
    typer.echo(f"created {reel.slug}  ({len(reel.shots)} shots, {_clock(reel.duration)})")
    if reel.shots:
        _print_shots(reel)
    typer.echo(f"\n  splice reel show {reel.slug}")


def _dedupe(shots: list[Shot]) -> list[Shot]:
    """One shot per range. A query and a suggestion routinely overlap."""
    seen: set[tuple] = set()
    out = []
    for shot in shots:
        key = (shot.stem, round(shot.start, 2), round(shot.end, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(shot)
    return out


def _from_query(
    search: str, tags: list[str], rating: str, limit: int, order: str,
    sidecars: list[Sidecar],
) -> list[Shot]:
    from .index import search as run

    try:
        rows = run(query=search, tags=tags, limit=max(limit, 200))
    except FileNotFoundError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    if rating:
        rows = [r for r in rows if r["rating"] == rating]
    if not rows:
        typer.echo("! that query matches nothing")
        raise typer.Exit(1)

    by_stem = {sc.asset.stem: sc for sc in sidecars}
    picked: list[tuple[Sidecar, object]] = []
    for row in rows:
        sc = by_stem.get(row["stem"])
        seg = next((s for s in sc.segments if s.id == row["id"]), None) if sc else None
        if seg:
            picked.append((sc, seg))

    if order == "shot":
        # The order the footage happened in, which is what makes a set of
        # moments read as an afternoon rather than a list.
        picked.sort(key=lambda pair: (pair[0].asset.created or "", pair[1].start))
    elif order == "long":
        picked.sort(key=lambda pair: -pair[1].duration)
    elif order != "found":
        typer.echo("! --order must be one of: found, shot, long")
        raise typer.Exit(1)

    return [store.shot_from_segment(sc, seg) for sc, seg in picked[:limit]]


def _from_suggestion(tag: str, sidecars: list[Sidecar]) -> list[Shot]:
    from .cli import _needs_index
    from .similarity import Space

    try:
        space = Space.load()
    except FileNotFoundError as exc:
        raise _needs_index(exc)
    suggestions = space.reels(limit=60)
    match = next(
        (r for r in suggestions if r.tag == tag or r.title.lower() == tag.lower()), None
    )
    if match is None:
        typer.echo(f"no suggestion for {tag!r}; `splice reels` lists them")
        raise typer.Exit(1)

    by_stem = {sc.asset.stem: sc for sc in sidecars}
    shots = []
    for candidate in match.segments:
        sc = by_stem.get(candidate.stem)
        if sc is None:
            continue
        seg = next((s for s in sc.segments if s.id == candidate.id), None)
        if seg:
            shots.append(store.shot_from_segment(sc, seg))
    return shots


@reel_app.command("show")
def reel_show(
    slug: str = typer.Argument(..., help="Which reel."),
    lines: bool = typer.Option(False, "--lines", "-l", help="Include the dialogue."),
):
    """The running order, with timings and anything that has come loose."""
    reel = _load(slug)
    resolved = store.resolve(reel)

    typer.echo(f"{reel.title}   [{reel.status}]")
    if reel.stale:
        typer.echo(
            f"  changed since you exported it on {reel.exported_at[:10]} -- "
            "a re-export arrives as a SECOND timeline, not an update"
        )
    elif reel.exported_at:
        typer.echo(f"  exported {reel.exported_at[:10]}, unchanged since")
    if reel.hook:
        typer.echo(f"  hook: {reel.hook}")
    typer.echo(
        f"  {len(reel.shots)} shots / {len(reel.clips)} clips / "
        f"{_clock(resolved.duration)}"
    )
    typer.echo("")
    _print_shots(reel, resolved=resolved, lines=lines)

    if reel.notes:
        typer.echo(f"\nnotes: {reel.notes}")
    if resolved.problems:
        typer.echo("")
        for problem in resolved.problems:
            typer.echo(f"  ! {problem}")
    _print_notes(resolved)


def _print_notes(resolved) -> None:
    """Things you would notice on a second viewing, said before the first."""
    from . import speech

    heard = speech.runs_map(s.shot.stem for s in resolved.shots if s.sidecar)
    notes = store.critique(resolved, heard)
    if not notes:
        return
    typer.echo("")
    for note in notes:
        where = f"shot {note.shot + 1}" if note.shot is not None else "this cut"
        typer.echo(f"  · {where} {note.text}")


def _print_shots(reel: Reel, resolved=None, lines: bool = False) -> None:
    resolved = resolved or store.resolve(reel)
    for shot in resolved.shots:
        flag = "!" if shot.problem else " "
        typer.echo(
            f"{flag}{shot.index + 1:2d}. [{_clock(shot.offset)}] {shot.duration:5.1f}s  "
            f"{shot.title[:42]:44} {shot.shot.stem[:28]:30} "
            f"{shot.start:6.1f}-{shot.end:.1f}"
        )
        if shot.problem:
            typer.echo(f"       {shot.problem}")
        if lines:
            for line in shot.lines:
                if line.en:
                    typer.echo(f"       “{line.en}”")


@reel_app.command("add")
def reel_add(
    slug: str = typer.Argument(...),
    query: list[str] = typer.Argument(None, help="Segment ids, or parts of their titles."),
    at: int = typer.Option(None, "--at", help="Insert before this position, 1-based."),
    line: str = typer.Option(
        None, "--line", "-l",
        help="A phrase somebody says: adds a shot cut to that line alone.",
    ),
    search: str = SEARCH,
    tag: list[str] = TAG,
    rating: str = RATING,
    limit: int = LIMIT,
    order: str = ORDER,
):
    """Put moments into the cut, by name, by search, or by something said.

    `--line` is the finest of the three: it adds a shot trimmed to one
    utterance, snapped to where that utterance actually begins and ends, with
    the sentence itself as the shot's note. "Put the seven-rupees line in the
    money reel" is one command rather than an add and four trims.
    """
    reel = _load(slug)
    sidecars = library.load_all()
    new: list[Shot] = []
    for one in query or []:
        sc, seg = _resolve_one(one, sidecars)
        new.append(store.shot_from_segment(sc, seg))
    if line:
        new.append(_shot_from_phrase(line, sidecars))
    if search or tag or rating:
        new += _from_query(search, list(tag), rating, limit, order, sidecars)
    if not new:
        typer.echo("name some moments, or pass --line / --search / --tag / --rating")
        raise typer.Exit(1)

    where = len(reel.shots) if at is None else max(0, min(at - 1, len(reel.shots)))
    reel.shots[where:where] = new
    before = len(reel.shots)
    reel.shots = _dedupe(reel.shots)
    reel.save()
    dropped = before - len(reel.shots)
    typer.echo(
        f"{slug}: +{len(new) - dropped} shot(s), now {len(reel.shots)} / "
        f"{_clock(reel.duration)}" + (f"  ({dropped} already in the cut)" if dropped else "")
    )
    _print_shots(reel)


@reel_app.command("rm")
def reel_rm(
    slug: str = typer.Argument(...),
    ref: list[str] = typer.Argument(..., help="Shot positions, or segment ids."),
):
    """Take shots out of the cut."""
    reel = _load(slug)
    # Resolve every reference against the *current* order before removing any,
    # so `rm 2 3` does not silently delete 2 and 4.
    doomed = sorted({_position(reel, r) for r in ref}, reverse=True)
    for i in doomed:
        reel.shots.pop(i)
    reel.save()
    typer.echo(f"{slug}: -{len(doomed)} shot(s), now {len(reel.shots)} / {_clock(reel.duration)}")
    _print_shots(reel)


@reel_app.command("move")
def reel_move(
    slug: str = typer.Argument(...),
    ref: str = typer.Argument(..., help="The shot to move: position or segment id."),
    to: int = typer.Argument(..., help="Its new position, 1-based."),
):
    """Reorder the cut."""
    reel = _load(slug)
    i = _position(reel, ref)
    shot = reel.shots.pop(i)
    reel.shots.insert(max(0, min(to - 1, len(reel.shots))), shot)
    reel.save()
    _print_shots(reel)


@reel_app.command("trim")
def reel_trim(
    slug: str = typer.Argument(...),
    ref: str = typer.Argument(..., help="Which shot: position or segment id."),
    start: float = typer.Option(None, "--start", help="Absolute in-point, seconds into the clip."),
    end: float = typer.Option(None, "--end", help="Absolute out-point."),
    head: float = typer.Option(None, "--head", help="Move the in-point by this much (+ later)."),
    tail: float = typer.Option(None, "--tail", help="Move the out-point by this much (+ later)."),
):
    """Tighten a shot. Trims are the reel's, not the segment's.

    The archive keeps saying what was said and when; a cut is free to take
    four seconds out of the middle of it without that being a correction.
    """
    reel = _load(slug)
    i = _position(reel, ref)
    shot = reel.shots[i]

    new_start = shot.start if start is None else start
    new_end = shot.end if end is None else end
    if head is not None:
        new_start += head
    if tail is not None:
        new_end += tail

    sidecar = library.load_sidecar(shot.stem)
    limit = sidecar.asset.duration if sidecar else max(new_end, 0.1)
    # Leave room for the shot itself: an in point pushed to the last frame
    # would otherwise take the out point past the end of the clip.
    new_start = min(max(0.0, new_start), max(0.0, limit - 0.1))
    new_end = min(max(new_start + 0.1, new_end), limit)

    reel.shots[i] = shot.model_copy(update={"start": new_start, "end": new_end})
    reel.save()
    typer.echo(
        f"shot {i + 1}: {shot.start:.1f}-{shot.end:.1f} -> "
        f"{new_start:.1f}-{new_end:.1f}  ({new_end - new_start:.1f}s)"
    )
    typer.echo(f"{slug} is now {_clock(reel.duration)}")


@reel_app.command("snap")
def reel_snap(
    slug: str = typer.Argument(...),
    window: float = typer.Option(
        None, "--window", help="How far a boundary may move, in seconds."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would move."),
):
    """Pull every shot to where somebody actually starts and stops talking.

    A shot's in-point is wherever the model said the moment began, and those
    are estimates -- round numbers, good to about a second. That is fine for
    finding a moment and wrong for opening on one: half a word in is the
    defect you notice first and cannot name.

    The audio knows. This moves each end to the nearest edge of an utterance,
    within a second, and leaves alone anything that is already there.
    """
    from . import speech, trash

    reel = _load(slug)
    limit = window if window is not None else speech.WINDOW
    # Listen first, then read the cut again: measuring a cold clip takes a
    # tenth of a second each, and a cut loaded before that and saved after it
    # would write over anything typed in the browser in between.
    heard = speech.runs_map(shot.stem for shot in reel.shots)
    reel = _load(slug)

    moves: list[tuple[int, float, float, float, float]] = []
    updated = []

    for i, shot in enumerate(reel.shots):
        runs = heard.get(shot.stem) or speech.runs_for(shot.stem)
        start, end = speech.snap_range(runs, shot.start, shot.end, limit)
        sidecar = library.load_sidecar(shot.stem)
        if sidecar:
            end = min(end, sidecar.asset.duration)
        if abs(start - shot.start) > 0.01 or abs(end - shot.end) > 0.01:
            moves.append((i, shot.start, start, shot.end, end))
            shot = shot.model_copy(update={"start": start, "end": end})
        updated.append(shot)

    if not moves:
        typer.echo(f"{slug}: every shot already starts and ends on speech")
        return
    for i, was_start, start, was_end, end in moves:
        typer.echo(
            f"{i + 1:3d}. {was_start:8.2f}-{was_end:.2f} -> {start:8.2f}-{end:.2f}"
            f"   ({start - was_start:+.2f} / {end - was_end:+.2f})"
        )
    before = reel.duration
    if dry_run:
        typer.echo(f"\n{len(moves)} shot(s) would move; nothing was changed")
        return

    trash.keep(store.path_for(slug), reason=f"snapped {slug} to speech")
    reel.shots = updated
    reel.save()
    typer.echo(
        f"\n{len(moves)} shot(s) moved; {_clock(before)} -> {_clock(reel.duration)}"
        f"\n  a copy of the cut as it was is in index/.trash"
    )


@reel_app.command("note")
def reel_note(
    slug: str = typer.Argument(...),
    ref: str = typer.Argument(...),
    text: str = typer.Argument(..., help="What this shot is for. Also names it in Final Cut."),
):
    """Say what a shot is doing there."""
    reel = _load(slug)
    i = _position(reel, ref)
    reel.shots[i] = reel.shots[i].model_copy(update={"note": text})
    reel.save()
    typer.echo(f"shot {i + 1}: {text}")


@reel_app.command("set")
def reel_set(
    slug: str = typer.Argument(...),
    title: str = typer.Option(None, "--title"),
    status: str = typer.Option(None, "--status", help="planned | cut | published"),
    hook: str = typer.Option(None, "--hook"),
    notes: str = typer.Option(None, "--notes"),
):
    """Change the reel itself rather than its shots."""
    reel = _load(slug)
    fields = {k: v for k, v in
              {"title": title, "status": status, "hook": hook, "notes": notes}.items()
              if v is not None}
    if not fields:
        typer.echo("nothing to set")
        raise typer.Exit(1)
    updated = reel.model_copy(update=fields)
    Reel.model_validate(updated.model_dump())  # reject a bad status
    updated.save()
    typer.echo(f"{slug}: " + ", ".join(f"{k}={v!r}" for k, v in fields.items()))


@reel_app.command("export")
def reel_export(
    slug: str = typer.Argument(None, help="Which reel. Omit with --all."),
    every: bool = typer.Option(False, "--all", help="Export every reel."),
    stale: bool = typer.Option(
        False, "--stale", help="With --all, only the cuts that changed since their export."
    ),
    lang: str = typer.Option("en", "--lang", help="Subtitle language: en | kok | both."),
    captions: bool = typer.Option(
        True, "--captions/--no-captions",
        help="Anchor the spoken lines to the timeline as Final Cut captions.",
    ),
    level: bool = typer.Option(
        True, "--level/--no-level",
        help="Carry the level match onto the timeline as a per-clip volume.",
    ),
    editor: str = typer.Option(
        None, "--editor", "-e",
        help=f"Which editor to write it for: {editors.names_for_help()}. "
             "Defaults to $SPLICE_EDITOR, or Final Cut.",
    ),
    send: bool = typer.Option(
        False, "--send",
        help="Put it straight into the open Resolve project, no file to find.",
    ),
):
    """Write the editor's timeline, its captions and a readable running order.

    Import the `.fcpxml`: it arrives as a project you can open and keep
    editing, not as loose clips. `--editor resolve` writes the document
    Resolve can open, and `--send` skips the import and puts it on the
    timeline list itself.
    """
    from . import export_reel, render as engine

    if lang not in engine.LANGS:
        typer.echo(f"! --lang must be one of {', '.join(engine.LANGS)}")
        raise typer.Exit(1)
    try:
        which = editors.get(editor)
    except editors.Unknown as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    if send and which.name != editors.RESOLVE:
        # Final Cut has no scripting door of this kind. Saying so beats writing
        # the file and letting `--send` look like it did something.
        typer.echo("! --send only works for Resolve; Final Cut has no way in")
        raise typer.Exit(1)

    reels = store.load_all() if every else [_load(slug)] if slug else []
    if stale:
        reels = [r for r in reels if r.stale or not r.exported_at]
        if not reels:
            typer.echo("every cut is as you last exported it")
            return
    if not reels:
        typer.echo("name a reel, or pass --all")
        raise typer.Exit(1)

    sidecars = library.load_all()
    for reel in reels:
        if not reel.shots:
            typer.echo(f"{reel.slug}: no shots, skipped")
            continue
        gains = {}
        if level:
            from . import loudness

            # Neither editor has a limiter of its own on the way in.
            gains = loudness.gains_for(
                store.resolve(reel, sidecars), ceiling=loudness.CEILING_DB
            )
        try:
            path, resolved = export_reel.write(
                reel, lang=lang, sidecars=sidecars, captions=captions,
                gains=gains, editor=which,
            )
        except ValueError as exc:
            typer.echo(f"! {reel.slug}: {exc}")
            continue
        typer.echo(
            f"{reel.slug}: {len(resolved.shots)} shots / {_clock(resolved.duration)} "
            f"-> {path.parent}/{path.name}, {reel.slug}.{{srt,txt}}"
        )
        for problem in resolved.problems:
            typer.echo(f"  ! {problem} (left out of the timeline)")
        _print_notes(resolved)

        if send:
            from . import resolve as bridge

            try:
                made = bridge.import_timeline(path, name=reel.title)
            except bridge.NotRunning as exc:
                # The file is written and valid either way, so this is a
                # warning and not a failure: the import is still one drag away.
                typer.echo(f"  ! {exc}")
                typer.echo(f"    the timeline is written: {path}")
            else:
                typer.echo(f"  -> sent to Resolve as {made!r}")

    if send:
        return

    typer.echo(f"\nimport it with {which.import_menu} in {which.label}")
    if which.clip_captions:
        typer.echo(
            "  it arrives as a project, with the spoken lines on it as captions."
            "\n  if Final Cut will not take it, `--no-captions` writes the same"
            " timeline without them and the .srt is still beside it."
        )
    else:
        typer.echo(
            "  it arrives as a project with a marker at the head of each shot."
            f"\n  the lines are the .srt beside it: {which.captions_menu}."
        )


@reel_app.command("render")
def reel_render(
    slug: str = typer.Argument(None, help="Which reel. Omit with --all."),
    every: bool = typer.Option(False, "--all", help="Render every reel."),
    stale: bool = typer.Option(
        False, "--stale", help="With --all, only the cuts that changed since their render."
    ),
    source: str = typer.Option(
        "raw", "--from", help="raw (the originals) | proxy (fast and small)."
    ),
    aspect: str = typer.Option(
        "source", "--aspect", help="source | 9:16 | 4:5 | 1:1 | 16:9."
    ),
    fill: str = typer.Option(
        "blur", "--fill", help="How a mismatched shot fills the frame: blur | pad | crop."
    ),
    height: int = typer.Option(None, "--height", help="Output height in pixels."),
    lang: str = typer.Option("en", "--lang", help="Subtitle language: en | kok | both."),
    captions: str = typer.Option(
        "soft", "--captions",
        help="soft (a track you can turn off) | burn (drawn on, for sending) | none.",
    ),
    bitrate: str = typer.Option(None, "--bitrate", help="e.g. 12M for a master."),
    fit: bool = typer.Option(
        False, "--fit", help="Lower the bitrate until the file is one you can send."
    ),
    level: bool = typer.Option(
        True, "--level/--no-level",
        help="Match the level between clips recorded on different days.",
    ),
    out: str = typer.Option(None, "--out", "-o", help="Write here instead."),
):
    """Render the cut as one video file you can watch, and send to someone.

    The browser plays a cut with a beat at every join, and Final Cut is a slow
    way to ask "does this work?". This is the same cut as one file: the shots
    end to end, the levels matched, the spoken lines along as subtitles.
    """
    from . import render as engine

    for value, allowed, what in (
        (source, engine.SOURCES, "--from"), (captions, engine.CAPTIONS, "--captions"),
        (lang, engine.LANGS, "--lang"), (aspect, tuple(engine.ASPECTS), "--aspect"),
        (fill, engine.FILLS, "--fill"),
    ):
        if value not in allowed:
            typer.echo(f"! {what} must be one of {', '.join(allowed)}")
            raise typer.Exit(1)

    reels = store.load_all() if every else [_load(slug)] if slug else []
    if stale:
        reels = [r for r in reels if r.render_stale or not r.rendered_at]
        if not reels:
            typer.echo("every cut is as you last rendered it")
            return
    if not reels:
        typer.echo("name a reel, or pass --all")
        raise typer.Exit(1)
    if out and len(reels) > 1:
        typer.echo("! --out names one file; render one reel at a time with it")
        raise typer.Exit(1)

    sidecars = library.load_all()
    for reel in reels:
        if not reel.shots:
            typer.echo(f"{reel.slug}: no shots, skipped")
            continue
        resolved = store.resolve(reel, sidecars)
        gains = {}
        if level:
            from . import loudness

            gains = loudness.gains_for(resolved)
        typer.echo(f"{reel.slug}: rendering {_clock(reel.duration)} ...")
        # A 5:50 cut takes over a minute, and a minute of nothing looks like a
        # hang. One line, rewritten in place.
        total = max(reel.duration, 0.1)
        def tick(seconds: float, total: float = total) -> None:
            typer.echo(
                f"\r  {min(1.0, seconds / total):>4.0%}  {_clock(seconds)}"
                f" of {_clock(total)}   ",
                nl=False,
            )

        try:
            result = engine.render(
                reel, dest=Path(out) if out else None, source=source, aspect=aspect,
                fill=fill, height=height, lang=lang, captions=captions,
                bitrate=bitrate, fit=fit, gains=gains, resolved=resolved,
                on_progress=tick if sys.stdout.isatty() else None,
            )
        except engine.RenderError as exc:
            typer.echo(f"\r! {reel.slug}: {exc}          ")
            continue
        if sys.stdout.isatty():
            typer.echo("\r" + " " * 40 + "\r", nl=False)
        target = result.plan.target
        typer.echo(
            f"  {result.path}  {target.width}x{target.height} "
            f"{_clock(result.duration)}  {result.size_bytes / 1e6:.0f} MB"
        )
        for problem in result.plan.skipped:
            typer.echo(f"  ! {problem} (left out of the render)")
        if result.too_big_to_send():
            typer.echo(f"  · {result.too_big_to_send()}")
        _print_notes(resolved)

    if captions == "soft":
        typer.echo("\nsubtitles are a track, not burnt in -- turn them on in the player.")


@reel_app.command("order")
def reel_order(
    slug: str = typer.Argument(...),
    by: str = typer.Option(
        "shot", "--by",
        help="shot (when the footage was taken) | long | short | clip",
    ),
):
    """Re-sort a cut. Somewhere to start from, not somewhere to stop."""
    reel = _load(slug)
    sidecars = {sc.asset.stem: sc for sc in library.load_all()}

    def taken(shot: Shot) -> tuple:
        sc = sidecars.get(shot.stem)
        return ((sc.asset.created or "") if sc else "", shot.start)

    keys = {
        "shot": taken,
        "clip": lambda s: (s.stem, s.start),
        "long": lambda s: -s.duration,
        "short": lambda s: s.duration,
    }
    if by not in keys:
        typer.echo(f"! --by must be one of {', '.join(keys)}")
        raise typer.Exit(1)

    reel.shots.sort(key=keys[by])
    reel.save()
    _print_shots(reel)


@reel_app.command("duplicate")
def reel_duplicate(
    slug: str = typer.Argument(...),
    title: str = typer.Argument(None, help="What to call the copy."),
):
    """Copy a cut so you can try a shorter one without losing this one."""
    reel = _load(slug)
    copy = store.create(
        title or f"{reel.title} (again)",
        shots=[shot.model_copy(deep=True) for shot in reel.shots],
        hook=reel.hook,
        notes=reel.notes,
    )
    typer.echo(f"{copy.slug}: a copy of {slug}, {len(copy.shots)} shots / {_clock(copy.duration)}")


@reel_app.command("unused")
def reel_unused(
    rating: str = typer.Option("favorite", "--rating", help="favorite | none | any."),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Only within these tags."),
    limit: int = typer.Option(40, "--limit", "-n"),
):
    """Good material no cut has claimed yet.

    The question that stops a moment being forgotten: everything you marked
    worth keeping, minus everything already in a running order.
    """
    from .index import search as run

    try:
        rows = run(query="", tags=list(tag), in_cut=False, limit=max(limit, 400))
    except FileNotFoundError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    if rating != "any":
        rows = [r for r in rows if r["rating"] == rating]
    if not rows:
        typer.echo("nothing left over -- every one of those is in a cut")
        return

    for row in rows[:limit]:
        typer.echo(
            f"{row['duration']:5.0f}s  {(row['title'] or '')[:44]:46} "
            f"{row['stem'][:30]:32} {row['id']}"
        )
    typer.echo(f"\n{len(rows)} unused" + (f", showing {limit}" if len(rows) > limit else ""))
    typer.echo("  splice reel add <slug> --rating favorite   puts them all in one")


@reel_app.command("delete")
def reel_delete(
    slug: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask."),
):
    """Throw a cut away."""
    reel = _load(slug)
    if not yes:
        typer.confirm(
            f"delete {reel.slug} ({len(reel.shots)} shots, {_clock(reel.duration)})?",
            abort=True,
        )
    from . import trash

    kept = trash.keep(store.path_for(slug), reason=f"deleted the cut {slug}")
    store.delete(slug)
    typer.echo(f"deleted {slug}")
    if kept:
        typer.echo(f"  a copy is in {kept.parent};  splice trash restore {kept.name}")
