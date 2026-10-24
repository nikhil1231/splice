"""greels — index family footage and hand it to an editor."""

from __future__ import annotations

from pathlib import Path

import typer

from . import cli_reel, config, editors, identity, library
from .schema import Sidecar

app = typer.Typer(
    add_completion=False,
    help="Analyse footage with agy (Gemini) and hand it to Final Cut or Resolve.",
    no_args_is_help=True,
)
taxonomy_app = typer.Typer(help="Inspect and grow the controlled vocabulary.", no_args_is_help=True)
export_app = typer.Typer(help="Write editor-facing files.", no_args_is_help=True)
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(export_app, name="export")
app.add_typer(cli_reel.reel_app, name="reel")

ONLY = typer.Option(None, "--only", "-o", help="Glob or substring over clip names.")


def main() -> None:
    """The `greels` command. A missing project is a sentence, not a traceback:
    it is the first thing anybody new to this sees."""
    try:
        app()
    except config.NoProject as exc:
        typer.echo(f"! {exc}", err=True)
        raise SystemExit(1)


@app.command()
def init(
    folder: Path = typer.Argument(
        ..., help="Where the footage, and everything made from it, will live."
    ),
    default: bool = typer.Option(
        True, "--default/--no-default",
        help="Make this the project greels uses when run from anywhere else.",
    ),
):
    """Start a project: a folder for the footage and a greels.yaml describing it."""
    import shutil

    import yaml

    folder = folder.expanduser().resolve()
    (folder / "raw").mkdir(parents=True, exist_ok=True)
    (folder / "index").mkdir(exist_ok=True)
    settings = folder / config.PROJECT_FILE
    # Never over an existing one: it is somebody's description of their family.
    if settings.exists():
        typer.echo(f"{settings} already exists; left as it is")
    else:
        shutil.copyfile(config.DEFAULTS_DIR / config.PROJECT_FILE, settings)
        typer.echo(f"wrote {settings}")
    vocabulary = folder / "taxonomy.yaml"
    if not vocabulary.exists():
        shutil.copyfile(config.default_taxonomy_path(), vocabulary)
        typer.echo(f"wrote {vocabulary}")

    if default:
        path = config.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        data["root"] = str(folder)
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        typer.echo(f"greels now uses {folder} by default ({path})")

    typer.echo(
        "\nnext:\n"
        f"  1. fill in {settings}: what the footage is, its language, who is in it\n"
        f"  2. put footage in {folder / 'raw'}\n"
        "  3. uv run greels doctor, then uv run greels sync\n"
        f"\nthe default prompts are in {config.DEFAULTS_DIR / 'prompts'}; copy one "
        f"into {folder / 'prompts'} to change it for this project"
    )


def _needs_index(exc: FileNotFoundError) -> "typer.Exit":
    """Turn a missing index into a sentence rather than a traceback.

    Every one of these is one `greels index` away, and the message the index
    raises already says so. What it must not do is arrive as a stack trace on
    somebody's first run.
    """
    typer.echo(f"! {exc}")
    return typer.Exit(1)


def _hms(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600:d}:{total % 3600 // 60:02d}:{total % 60:02d}"


@app.command()
def scan(
    force: bool = typer.Option(False, help="Re-probe every clip, fingerprint included."),
) -> identity.Survey:
    """Probe raw/ and create or refresh the asset block of each sidecar.

    Identity here is the clip's content, not its filename, so a clip re-copied
    off the phone under its camera number is recognised as one you already have
    rather than analysed and imported a second time.
    """
    from .probe import ProbeError, probe_asset

    paths = library.discover_raw()
    if not paths:
        typer.echo(f"no video found under {config.raw_dir()}")
        return identity.Survey()

    sidecars = library.load_all()
    survey = identity.survey(paths, sidecars)

    for finding in survey.adopted:
        identity.adopt(finding)
    if survey.adopted:
        sidecars = library.load_all()

    known = {sc.asset.stem: sc for sc in sidecars}
    created = updated = 0
    total = 0.0

    for finding in survey.findings:
        if finding.verdict is identity.Verdict.DUPLICATE:
            continue
        if finding.verdict is identity.Verdict.UNCHANGED and not force:
            existing = known.get(finding.stem)
            # An unchanged clip still needs a probe if its sidecar has yet to
            # record the fingerprint -- that is how identity is backfilled onto
            # sidecars written before it existed.
            if existing is not None and existing.asset.fingerprint == finding.digest:
                total += existing.asset.duration
                continue

        try:
            asset = probe_asset(finding.path)
        except ProbeError as exc:
            typer.echo(f"  ! {exc}")
            continue
        total += asset.duration

        existing = known.get(finding.stem)
        if existing is not None:
            asset.original_name = existing.asset.original_name
            asset.previous_names = existing.asset.previous_names
            existing.asset = asset  # keep segments; only the probe is refreshed
            if finding.verdict is identity.Verdict.REPLACED:
                # The footage under this name is not the footage that was
                # analysed. Segments are kept -- they may be hand-corrected and
                # are irreplaceable -- but the analysis no longer matches, so it
                # has to run again.
                existing.analysis.prompt_sha = ""
            existing.save(config.sidecar_path(finding.stem))
            updated += 1
        else:
            Sidecar(asset=asset).save(config.sidecar_path(finding.stem))
            created += 1

    counted = len(paths) - len(survey.duplicates)
    typer.echo(
        f"{counted} clips, {_hms(total)} of footage "
        f"({created} new, {updated} refreshed) -> {config.index_dir()}"
    )
    _report_survey(survey)
    return survey


def _report_survey(survey: identity.Survey) -> None:
    for finding in survey.adopted:
        typer.echo(f"  ~ adopted rename: {finding.note}")
    for finding in survey.replaced:
        typer.echo(f"  ! {finding.note}; it will be re-analysed")
    if survey.duplicates:
        typer.echo(
            f"\n{len(survey.duplicates)} file(s) in raw/ are footage you already have. "
            "They are left where they are and otherwise ignored:"
        )
        for finding in survey.duplicates:
            typer.echo(f"  = {finding.path.name:32} same as {finding.known_as}")


@app.command()
def proxy(
    only: str = ONLY,
    force: bool = typer.Option(False, help="Rebuild proxies that already exist."),
    chunk_seconds: int = typer.Option(config.CHUNK_SECONDS, help="Slice length."),
):
    """Build the small proxies and chunks that analysis reads."""
    from .proxy import EncodeError, build_chunks, build_proxy, clear_chunks

    # Duplicates never get a sidecar, so they are already out of this list.
    sidecars = _selected(only, strict=False)
    for sc in sidecars:
        stem = sc.asset.stem
        # Chunks are named by offset, so shortening the slice leaves the old,
        # longer ones behind and analysis reads both sets.
        if sc.analysis.chunks > 1 and sc.analysis.chunk_seconds not in (0, chunk_seconds):
            clear_chunks(stem)
        try:
            path = build_proxy(config.raw_dir() / sc.asset.file, library.proxy_for(stem), force)
            chunks = build_chunks(path, stem, chunk_seconds=chunk_seconds, force=force)
        except EncodeError as exc:
            typer.echo(f"  ! {stem}: {exc}")
            continue
        size = path.stat().st_size / 1e6
        note = f", {len(chunks)} chunks" if chunks else ""
        typer.echo(f"  {stem:52} {size:6.1f} MB{note}")


@app.command()
def analyze(
    only: str = ONLY,
    force: bool = typer.Option(False, help="Re-analyse clips that already have segments."),
    jobs: int = typer.Option(config.DEFAULT_JOBS, "--jobs", "-j", help="Concurrent agy calls."),
    chunk_seconds: int = typer.Option(config.CHUNK_SECONDS, help="Slice length."),
):
    """Run agy over each clip and write segments into its sidecar."""
    from .analyze import analyze_assets, legacy_prose_for, prompt_sha, schema_file
    from .proxy import EncodeError
    from .taxonomy import Taxonomy

    taxonomy = Taxonomy.load()
    schema = schema_file()
    sha = prompt_sha()
    sidecars = _selected(only, strict=False)

    todo = [
        sc for sc in sidecars
        if force or not sc.segments or sc.analysis.prompt_sha != sha or sc.analysis.errors
    ]
    skipped = len(sidecars) - len(todo)
    if skipped:
        typer.echo(f"{skipped} already analysed and current; use --force to redo")
    if not todo:
        return

    previous = {sc.asset.stem: sc for sc in todo}
    total = sum(sc.asset.duration for sc in todo)
    typer.echo(
        f"analysing {len(todo)} clip(s), {_hms(total)} of footage, {jobs} at a time"
    )

    done = failed = 0

    def report(result) -> None:
        nonlocal done, failed
        done += 1
        stem = result.asset.stem
        if not result.segments:
            failed += 1
            typer.echo(f"[{done}/{len(todo)}] {stem}: FAILED — {'; '.join(result.analysis.errors)[:200]}")
            return

        # Analysis owns timing and meaning; your review state does not come
        # from the model, so carry it across a re-run.
        _carry_review_state(previous[stem], result)
        result.save(config.sidecar_path(stem))

        proposed = sorted({t for s in result.segments for t in s.proposed_tags})
        typer.echo(
            f"[{done}/{len(todo)}] {stem[:44]:46} {len(result.segments):3d} segments"
            f" / {result.analysis.chunks} chunk(s)"
            + ("  [partial]" if result.analysis.errors else "")
        )
        if proposed:
            typer.echo(f"{'':56} proposed: {', '.join(proposed)}")

    try:
        analyze_assets(
            [sc.asset for sc in todo], taxonomy, schema,
            legacy_prose={sc.asset.stem: legacy_prose_for(sc.asset.stem) for sc in todo},
            jobs=jobs, chunk_seconds=chunk_seconds, on_done=report,
        )
    except EncodeError as exc:
        typer.echo(f"! proxy build failed: {exc}")
        raise typer.Exit(1)

    if failed:
        typer.echo(f"\n{failed} clip(s) failed; rerun `greels analyze` to retry just those")


def _carry_review_state(old: Sidecar, new: Sidecar) -> None:
    """Re-apply rating/status to whichever new segment covers the old midpoint.

    And keep the record of what each editor was given. A re-analysis builds a
    fresh sidecar, and with it an empty delivery record, so the next sync found
    the clip in the library, took it for a new arrival and confirmed it against
    the *new* segments: grandpa_full_life_story_interview and two others were
    re-timed and Final Cut kept the old, wrong keyword ranges without a word.
    Carried over, the old import no longer matches and the clip is offered
    again as the second copy it has to be.

    Every editor's record, not only Final Cut's -- dropping the others would
    reproduce exactly that failure for whichever editor was not the one this
    was first written for.
    """
    new.exported = old.exported.model_copy(deep=True)
    new.deliveries = {
        name: record.model_copy(deep=True)
        for name, record in old.deliveries.items()
    }
    for previous in old.segments:
        if previous.rating == "none" and previous.status == "indexed":
            continue
        midpoint = (previous.start + previous.end) / 2
        for segment in new.segments:
            if segment.start <= midpoint <= segment.end:
                segment.rating = previous.rating
                segment.status = previous.status
                break


EDITOR = typer.Option(
    None, "--editor", "-e",
    help=f"Which editor to hand this to: {editors.names_for_help()}. "
         "Defaults to $GREELS_EDITOR, or Final Cut.",
)


def _suffix(which) -> str:
    """What marks a file as one editor's rather than the other's.

    Final Cut's files keep the names they have always had -- `incoming.fcpxml`
    is in the README, in the browser and in six months of muscle memory.
    Resolve's are marked, because the two documents are not interchangeable and
    one overwriting the other would break the promise `incoming.fcpxml` makes:
    that if the file is there, importing it is always safe.
    """
    return "" if which.name == editors.FCP else f".{which.name}"


def incoming_path(which) -> Path:
    return config.export_dir() / f"incoming{_suffix(which)}.fcpxml"


def _editor(name: str | None):
    """The editor a command was told to use, or a sentence and an exit.

    A typo must not fall back to Final Cut: the two documents are different
    files and a silent fallback turns `--editor resolv` into an import that
    quietly does nothing.
    """
    try:
        return editors.get(name)
    except editors.Unknown as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)


@app.command()
def sync(
    jobs: int = typer.Option(config.DEFAULT_JOBS, "--jobs", "-j", help="Concurrent agy calls."),
    editor: str = EDITOR,
    rename_new: bool = typer.Option(
        True, "--rename/--no-rename", help="Name camera-numbered clips from their analysis."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Say what a sync would do, and change nothing."
    ),
):
    """Drop clips in raw/, run this, import the file it names. The whole loop.

    Every step is safe to repeat. Clips are identified by their content, so
    re-dropping footage you already have costs nothing; and what the editor
    holds is read from its own library rather than assumed, so a clip you were
    offered and never imported comes back next time instead of vanishing.

    `--editor resolve` hands the same loop to Resolve instead. Delivery state
    is kept per editor, so switching does not re-offer the archive to the one
    you were already using.

    It ends by aligning the new subtitles to the speech and mirroring the
    archive into the repo, because both are things you would otherwise have to
    remember on the evening you are least likely to.
    """
    from .export_fcpxml import pending, reconcile, write as write_fcpxml
    from .rename import RenameError, apply as do_rename, build_plans

    which = _editor(editor)
    if dry_run:
        _dry_run(jobs, which)
        return

    scan(force=False)
    proxy(only=None, force=False, chunk_seconds=config.CHUNK_SECONDS)
    analyze(only=None, force=False, jobs=jobs, chunk_seconds=config.CHUNK_SECONDS)

    # Vocabulary you grew since the last run only reaches the editor once the
    # sidecars are re-split against it, so this belongs in the loop rather than
    # in a command you have to remember.
    reclassify()

    if rename_new:
        plans, skips = build_plans(library.load_all())
        renamed = []
        for plan in plans:
            try:
                do_rename(plan)
                renamed.append(plan)
            except RenameError as exc:
                typer.echo(f"  ! {plan.stem}: {exc}")
        if renamed:
            typer.echo(f"\nnamed {len(renamed)} clip(s):")
            for plan in renamed:
                typer.echo(f"  {plan.stem:22} -> {plan.new_stem}")
        for skip in skips:
            typer.echo(f"  . {skip.stem:22} not named: {skip.reason}")

    index_cmd()
    listened = library.load_all()
    _listen(listened)

    # Here, because `_listen` has just measured where the talking is and the
    # exports below are written from the line timings. The model estimates
    # those and is about half a second out, which is a caption arriving before
    # the mouth moves. It changes no segment boundary, title, tag or rating,
    # so nothing it touches is re-offered to the editor as a second copy.
    #
    # Guarded, because `align` on its own refuses an archive with no sidecars
    # in it -- which is the right answer to someone typing it, and the wrong
    # one for a sync of an empty drop bin, which is not an error.
    if listened:
        align(only=None, window=None, dry_run=False)

    analysed = [sc for sc in library.load_all() if sc.segments]
    # reconcile corrects these in place as well as on disk, so `pending` below
    # reads the state the library actually justifies.
    _report_reconcile(reconcile(analysed, which), editor=which)
    fresh, changed = pending(analysed, which)

    export_srt_cmd(only=None, lang="en", dest=None)
    export_csv_cmd(only=None, dest=None)
    write_fcpxml(
        dest=config.export_dir() / f"{config.export_stem()}{_suffix(which)}.fcpxml",
        editor=which,
    )

    typer.echo("")
    incoming = incoming_path(which)
    outgoing = fresh + changed
    if outgoing:
        _, clips, segments = write_fcpxml(
            dest=incoming, sidecars=outgoing, mark=True, editor=which
        )
        typer.echo(
            f"{clips} clip(s), {segments} "
            f"{'keyword ranges' if which.keyword_ranges else 'marked ranges'} "
            f"-> {incoming}"
        )
        typer.echo(f"  {which.import_menu} in {which.label}.")
        if changed and which.duplicates_on_reimport:
            # Final Cut has no way to update a clip from XML. These arrive as a
            # second copy rather than replacing the first, so say so before the
            # import rather than leaving it to be discovered afterwards.
            typer.echo(
                f"\n  {len(changed)} of them changed since you imported them and will "
                "arrive as a SECOND copy named '<clip> - a1'. Delete the older copy "
                "in Final Cut afterwards:"
            )
            for sc in changed:
                typer.echo(f"    {sc.asset.stem}")
        elif changed:
            # Resolve reuses the media pool clip it already has, so nothing is
            # duplicated -- what arrives is a fresh stringout carrying the
            # markers these clips now have.
            typer.echo(
                f"\n  {len(changed)} of them changed since you imported them. "
                "Resolve keeps the clip it has and the new timeline carries "
                "their markers:"
            )
            for sc in changed:
                typer.echo(f"    {sc.asset.stem}")
    else:
        # Never leave a stale file behind: its existence has to mean "import
        # this", or one day it gets imported twice.
        incoming.unlink(missing_ok=True)
        typer.echo("nothing new to import")

    # A cut is the one thing here nothing can regenerate, and the steps above
    # are exactly what breaks one: a clip renamed, replaced or gone.
    _report_reels(library.load_all(), only_problems=True)

    # Last, because the run has just made the sidecars that cost hours of agy
    # and it is the only moment they exist nowhere else. The browser's copy of
    # this loop is mirrored by the server's autosave ninety seconds later; a
    # run from a terminal had nothing doing it, and eleven transcripts sat
    # outside git for two days before anybody opened the page.
    _mirror_into_repo()


def _mirror_into_repo() -> None:
    """Back up, reporting a refusal rather than raising past the caller.

    A refusal means the media root does not look like itself -- unmounted, or
    the wrong one -- and that is worth a sentence at the end of a sync, not a
    stack trace over the import instructions it just printed.

    A sync of a project that names no archive does not back up at all: the
    archive mirrors one media root and deletes what that root does not have,
    so an unasked-for backup of a scratch `GREELS_ROOT` into somebody's real
    one is how the transcripts go. A scratch root has no greels.yaml, and so
    no archive.
    """
    from . import backup as store

    try:
        added, updated, removed = store.backup()
    except store.Refused as exc:
        typer.echo(f"\n! not backed up: {exc}")
        return
    if added or updated or removed:
        typer.echo(
            f"\nbacked up into archive/: {added} added, {updated} updated, "
            f"{removed} removed. Commit it."
        )


def _lines_off_the_speech(sidecars: list[Sidecar]) -> int:
    """How many subtitle lines `align` would move.

    Lives in `health` now, so the sync dry run, `doctor` and the browser's
    header cannot end up reporting three different numbers for it.
    """
    from . import health

    return health.lines_off_the_speech(sidecars)


def _listen(sidecars: list[Sidecar]) -> None:
    """Measure each new clip's level and where the talking is.

    Both are cheap -- under a second a clip, off the proxy, cached against it
    -- and both are wanted the moment you start cutting: one matches the
    levels in a render, the other decides where a trim lands. Doing it here
    means the first cut of the evening does not wait for ffmpeg sixty times.
    """
    from . import loudness, speech

    stems = [sc.asset.stem for sc in sidecars]
    if not stems:
        return
    heard, _ = speech.measure_all(stems)
    measured, _ = loudness.measure_all(stems)
    typer.echo(f"listened to {heard} clip(s); levels for {measured}")


def _report_reconcile(report, would: bool = False, editor=None) -> None:
    which = editor or editors.get(getattr(report, "editor", None) or None)
    if not report.seen:
        extra = (
            " Resolve can only be read while it is running with the project "
            "open." if which.name == editors.RESOLVE else ""
        )
        typer.echo(
            f"\n! could not read the {which.label} library, so import state is "
            "whatever the sidecars already said. `greels imported "
            f"--editor {which.name}` sets it by hand.{extra}"
        )
        return

    typer.echo(f"\n{which.label} holds {report.in_library} clip(s)")
    if report.confirmed:
        verb = "would be confirmed" if would else "confirmed since last run"
        typer.echo(f"  + {len(report.confirmed)} import(s) {verb}")
    if report.withdrawn:
        verb = "would go back in" if would else "are back in"
        typer.echo(
            f"  - {len(report.withdrawn)} clip(s) were offered but never imported; "
            f"they {verb} the queue:"
        )
        for stem in report.withdrawn:
            typer.echo(f"      {stem}")
    if report.duplicates:
        typer.echo(
            f"  ! {which.label} holds more than one copy of "
            f"{len(report.duplicates)} clip(s):"
        )
        for base, names in sorted(report.duplicates.items()):
            typer.echo(f"      {', '.join(names)}  (keep the newest, delete the rest)")


def _dry_run(jobs: int, which=None) -> None:
    """What a sync would do, without doing any of it."""
    from .analyze import prompt_sha
    from .export_fcpxml import preview, projected_pending
    from .export_srt import filename as srt_filename
    from .rename import build_plans

    paths = library.discover_raw()
    sidecars = library.load_all()
    survey = identity.survey(paths, sidecars)

    typer.echo(f"{len(paths)} file(s) in {config.raw_dir()}")
    for verdict in identity.Verdict:
        found = survey.of(verdict)
        if found:
            typer.echo(f"  {verdict.value:10} {len(found)}")
    _report_survey(survey)

    sha = prompt_sha()
    todo = [
        sc for sc in sidecars
        if not sc.segments or sc.analysis.prompt_sha != sha or sc.analysis.errors
    ]
    new = len(survey.of(identity.Verdict.NEW))
    typer.echo(f"\nwould analyse {len(todo) + new} clip(s) at -j {jobs}")

    plans, skips = build_plans(sidecars)
    for plan in plans:
        typer.echo(f"  would name {plan.stem:22} -> {plan.new_stem}")
    pending_analysis = {sc.asset.stem for sc in todo}
    for skip in skips:
        # A clip this run is about to analyse gets named from that analysis;
        # it only reads as unnameable because the analysis has not happened yet.
        if skip.stem in pending_analysis and skip.reason == "not analysed yet":
            typer.echo(f"  would name {skip.stem:22} once it has been analysed")
        else:
            typer.echo(f"  would skip {skip.stem:22} ({skip.reason})")

    which = which or editors.default()
    analysed = [sc for sc in sidecars if sc.segments]
    report = preview(analysed, which)
    if report is None:
        typer.echo(
            f"\ncannot read the {which.label} library; import state would be "
            "left as-is"
        )
    else:
        _report_reconcile(report, would=True, editor=which)

    fresh, changed = projected_pending(analysed, which)
    typer.echo(
        f"\nwould offer {len(fresh) + len(changed)} clip(s): "
        f"{len(fresh)} never delivered, {len(changed)} changed since import"
    )
    for sc in fresh:
        typer.echo(f"    {sc.asset.stem}")
    for sc in changed:
        note = " (arrives as a second copy)" if which.duplicates_on_reimport else ""
        typer.echo(f"    {sc.asset.stem}{note}")

    live = {
        srt_filename(sc, seg)
        for sc in sidecars for seg in sc.segments
    }
    srt_dir = config.export_dir() / "srt"
    orphans = [p for p in srt_dir.glob("*.srt") if p.name not in live] if srt_dir.is_dir() else []
    if orphans:
        typer.echo(f"\nwould delete {len(orphans)} orphaned .srt file(s)")

    _report_reels(library.load_all(), only_problems=True)

    # Both of these are the last two steps of a real run, and the browser opens
    # with this dry run before spending an hour of agy. Counted off the warm
    # speech cache and the backup's own comparison, so neither costs anything.
    off = _lines_off_the_speech(sidecars)
    if off:
        typer.echo(f"\nwould move {off} line timing(s) onto the speech")
    try:
        from . import backup as store

        fresh_files, _, differing = store.drift()
        if fresh_files or differing:
            typer.echo(
                f"would back up {len(fresh_files)} new and {len(differing)} changed "
                "file(s) into archive/"
            )
    except Exception as exc:
        typer.echo(f"would try to back up, but: {exc}")

    typer.echo("\nnothing was changed; drop --dry-run to run it")


@app.command()
def imported(
    only: str = ONLY,
    reset: bool = typer.Option(
        False, "--reset", help="Forget what the editor has, e.g. after deleting the library."
    ),
    editor: str = EDITOR,
):
    """Record by hand that the editor already holds these clips.

    `sync` reads the editor's own library, so this is only needed when it
    cannot -- a Final Cut library kept somewhere greels cannot see, or a
    version whose catalogue it fails to read. For Resolve it is needed far more
    often, because Resolve answers only while it is running with the project
    open, and a sync at midnight cannot see a thing.

    Setting `GREELS_FCP_LIBRARY` is usually the better answer for Final Cut.
    For Resolve the better answer is to have it open.
    """
    from .export_fcpxml import mark_exported
    from .schema import ExportRecord

    which = _editor(editor)
    sidecars = [sc for sc in _selected(only) if sc.segments]
    if reset:
        for sidecar in sidecars:
            sidecar.set_delivery(which.name, ExportRecord())
            sidecar.save(config.sidecar_path(sidecar.asset.stem))
        typer.echo(
            f"forgot {which.label} import state for {len(sidecars)} clip(s); "
            "all will be offered again"
        )
        return

    mark_exported(sidecars, which)
    typer.echo(f"marked {len(sidecars)} clip(s) as already in {which.label}")


@app.command(name="rename")
def rename_cmd(
    only: str = ONLY,
    set_: list[str] = typer.Option(
        [], "--set", "-s", help="Exact rename, OLD=NEW (repeatable)."
    ),
    apply_: bool = typer.Option(False, "--apply", help="Actually rename; otherwise dry run."),
    all_: bool = typer.Option(
        False, "--all", help="Also propose names for clips you already named."
    ),
):
    """Give camera-numbered clips names derived from their analysis.

    Renames the original in raw/ and everything pointing at it: sidecar,
    proxy, chunks and segment ids. Dry run unless --apply.
    """
    from .rename import RenameError, apply as do_rename, build_plans

    explicit = {}
    for item in set_:
        if "=" not in item:
            typer.echo(f"! --set expects OLD=NEW, got {item!r}")
            raise typer.Exit(1)
        old, new = item.split("=", 1)
        explicit[old.strip()] = new.strip()

    sidecars = _selected(only)
    try:
        plans, skips = build_plans(sidecars, explicit, only_camera_named=not all_)
    except RenameError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)

    for skip in skips:
        typer.echo(f"  . {skip.stem:24} skipped: {skip.reason}")
    if not plans:
        typer.echo("nothing to rename")
        return

    for plan in plans:
        typer.echo(f"  {plan.stem:24} -> {plan.new_stem:46} ({plan.reason})")

    if not apply_:
        typer.echo(f"\n{len(plans)} clip(s) would be renamed; re-run with --apply")
        return

    for plan in plans:
        try:
            do_rename(plan)
        except RenameError as exc:
            typer.echo(f"! {plan.stem}: {exc}")
            raise typer.Exit(1)
    typer.echo(f"\nrenamed {len(plans)} clip(s); run `greels index` and `greels export all`")


@app.command()
def backup(
    restore: bool = typer.Option(False, "--restore", help="Copy the archive's copies back."),
    check: bool = typer.Option(False, "--check", help="Show drift without writing."),
    force: bool = typer.Option(
        False, "--force", help="Mirror even when it would delete most of the backup."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before restoring."),
):
    """Keep a copy of the sidecars, cuts and project files in the archive."""
    from . import backup as store

    try:
        store.archive_root()
    except store.NoArchive as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)

    if check:
        new, orphaned, changed = store.drift()
        for name in new:
            typer.echo(f"  + {name}")
        for name in changed:
            typer.echo(f"  M {name}")
        for name in orphaned:
            typer.echo(f"  - {name} (backed up but no longer live)")
        typer.echo(
            "backup is current" if not (new or orphaned or changed)
            else f"\n{len(new)} new, {len(changed)} changed, {len(orphaned)} stale"
        )
        return

    if restore:
        # Restoring writes the repo's copy over a live one, and a live sidecar
        # may hold a correction that was never backed up. Say what will be
        # overwritten before doing it.
        _, only_saved, differing = store.drift()
        if differing and not yes:
            typer.echo(f"{len(differing)} live file(s) differ from the backup and "
                       "would be overwritten:")
            for name in differing[:10]:
                typer.echo(f"  {name}")
            if len(differing) > 10:
                typer.echo(f"  ... and {len(differing) - 10} more")
            typer.confirm("overwrite them with the backed-up copies?", abort=True)
        try:
            restored, skipped = store.restore()
        except FileNotFoundError as exc:
            typer.echo(f"! {exc}")
            raise typer.Exit(1)
        typer.echo(f"restored {restored} file(s), {skipped} already identical")
        typer.echo("run `greels index` to rebuild the search index")
        return

    try:
        added, updated, removed = store.backup(force=force)
    except store.NoArchive as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    except store.Refused as exc:
        typer.echo(f"! {exc}")
        typer.echo("  nothing was changed. `greels backup --force` overrides this.")
        raise typer.Exit(1)
    typer.echo(
        f"{added} added, {updated} updated, {removed} removed -> {store.archive_root()}"
    )
    typer.echo("commit the archive to version them")


trash_app = typer.Typer(
    help="Copies of anything a write was about to destroy.", no_args_is_help=True
)
app.add_typer(trash_app, name="trash")


@trash_app.command("list")
def trash_list(limit: int = typer.Option(20, "--limit", "-n")):
    """What has been kept, newest first."""
    from . import trash

    kept = trash.entries()
    if not kept:
        typer.echo(f"nothing kept  ({trash.trash_dir()})")
        return
    for entry in kept[:limit]:
        note = trash.why(entry)
        typer.echo(
            f"{entry.when:%Y-%m-%d %H:%M}  {entry.original[:44]:46} {note}"
        )
        typer.echo(f"{'':18}  {entry.name}")
    typer.echo(f"\n{len(kept)} copy(ies) in {trash.trash_dir()}")
    typer.echo("  greels trash restore <name>   puts one back")


@trash_app.command("restore")
def trash_restore(
    name: str = typer.Argument(..., help="A name from `greels trash list`, or part of one."),
):
    """Put a kept copy back over the live file.

    Whatever it displaces is itself kept, so restoring the wrong one costs
    nothing.
    """
    from . import trash

    matches = [e for e in trash.entries() if name in e.name]
    if not matches:
        typer.echo(f"nothing kept matching {name!r}")
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(f"{len(matches)} copies match {name!r}:")
        for entry in matches[:10]:
            typer.echo(f"  {entry.name}")
        typer.echo("\nname one exactly")
        raise typer.Exit(1)

    destination = trash.restore(matches[0])
    typer.echo(f"restored {matches[0].name}\n  -> {destination}")
    typer.echo("run `greels index` to rebuild the search index")


@trash_app.command("empty")
def trash_empty(yes: bool = typer.Option(False, "--yes", "-y")):
    """Throw the kept copies away."""
    from . import trash

    kept = trash.entries()
    if not kept:
        typer.echo("nothing to empty")
        return
    if not yes:
        typer.confirm(f"delete {len(kept)} kept copy(ies)?", abort=True)
    typer.echo(f"deleted {trash.empty()} file(s)")


@app.command()
def thumbs(
    only: str = ONLY,
    force: bool = typer.Option(False, help="Rebuild frames that already exist."),
    prune: bool = typer.Option(True, "--prune/--no-prune", help="Drop orphaned frames."),
):
    """Build the poster frames the browser shows.

    Built on demand as you browse anyway; this is for when you would rather
    wait once than a tenth of a second at a time.
    """
    from . import thumbs as store

    sidecars = _selected(only, strict=False)
    built, skipped, failed = store.build_all(sidecars, force=force)
    typer.echo(
        f"{built} built, {skipped} already there"
        + (f", {failed} had no proxy to take a frame from" if failed else "")
        + f" -> {store.thumb_dir()}"
    )
    if prune and not only:
        dropped = store.prune()
        if dropped:
            typer.echo(f"{dropped} frame(s) for segments that no longer exist, removed")


@app.command(name="index")
def index_cmd():
    """Rebuild library.sqlite from the sidecars."""
    from .index import build

    assets, segments = build()
    typer.echo(f"indexed {segments} segments across {assets} clips -> {config.db_path()}")


# Two quotes and a count, not the whole transcript of a 108-second moment.
# `lines` is the command for every hit; this one is a list of moments that
# says why each is in it.
_SAID_SHOWN = 2


@app.command()
def search(
    query: str = typer.Argument(
        "", help="Full-text query over titles, summaries, tags and what is said."
    ),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Require this tag (repeatable)."),
    status: str = typer.Option(None, help="Filter by workflow status."),
    rating: str = typer.Option(None, "--rating", help="favorite | rejected | none."),
    unused: bool = typer.Option(
        False, "--unused", help="Only moments no cut has claimed yet."
    ),
    used: bool = typer.Option(False, "--used", help="Only moments already in a cut."),
    order: str = typer.Option(
        "clip", "--order",
        help="clip (by filename) | shot (when it was taken) | promise (worth watching first).",
    ),
    limit: int = typer.Option(30, "--limit", "-n"),
):
    """Find moments across every clip.

    The same filters the browser's facets apply, so a query you refine there
    can be handed straight to `greels reel add --search`.
    """
    from .index import search as run

    if unused and used:
        typer.echo("! --unused and --used ask for opposite things")
        raise typer.Exit(1)

    try:
        rows = run(
            query=query, tags=list(tag), status=status, order=order,
            in_cut=True if used else False if unused else None, limit=limit,
        )
    except ValueError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    except FileNotFoundError as exc:
        raise _needs_index(exc)
    if rating:
        rows = [r for r in rows if r["rating"] == rating]
    if not rows:
        typer.echo("no matches")
        return
    for row in rows:
        mark = " *" if row["rating"] == "favorite" else " x" if row["rating"] == "rejected" else "  "
        typer.echo(
            f"{row['stem'][:38]:40} {_hms(row['start'])}-{_hms(row['end'])}{mark} {row['title']}"
        )
        # Where the words are, when it was the transcript that matched. A
        # moment averages 28 seconds and one of them runs 108, so a row on its
        # own says "somewhere in the next two minutes he says this".
        for said in row["said"][:_SAID_SHOWN]:
            typer.echo(
                f"{'':40} {_hms(said['start'])}  {(said['en'] or said['kok'])[:70]}"
            )
        if len(row["said"]) > _SAID_SHOWN:
            # Counted rather than dropped quietly: a phrase said nine times is
            # the reason `lines` exists, and this row should say so.
            typer.echo(
                f"{'':40} and {len(row['said']) - _SAID_SHOWN} more here — "
                f"`greels lines` lists them"
            )
        detail = row["tags"] or ""
        if row["reels"]:
            detail += f"   [in {row['reels']}]"
        if detail:
            typer.echo(f"{'':40} {detail}")
    typer.echo(
        f"\n{len(rows)} match(es)" if len(rows) < limit
        else f"\nthe first {limit}; -n asks for more"
    )


@app.command()
def lines(
    query: str = typer.Argument(..., help="A phrase somebody says."),
    limit: int = typer.Option(20, "--limit", "-n"),
    lang: str = typer.Option("en", "--lang", help="Show en | kok | both."),
):
    """Find the moment somebody says a thing, to the second.

    `search` finds moments; a moment here averages 28 seconds and one of them
    runs 108, so a hit on one says "somewhere in the next two minutes". This
    says when. It is the question you ask while assembling a cut -- *start
    where he says sixty, seventy years ago* -- and the reason the two
    twenty-minute interviews were unusable before.
    """
    from .index import search_lines
    from .render import LANGS

    if lang not in LANGS:
        typer.echo(f"! --lang must be one of {', '.join(LANGS)}")
        raise typer.Exit(1)
    try:
        rows = search_lines(query, limit=limit)
    except FileNotFoundError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    if not rows:
        typer.echo("nothing said matches that")
        return

    for row in rows:
        typer.echo(
            f"{row['stem'][:34]:36} {_hms(row['start'])}  "
            f"{(row['en'] if lang != 'kok' else row['kok']) or ''}"
        )
        if lang in ("kok", "both") and row["kok"] and lang == "both":
            typer.echo(f"{'':36} {'':7}  {row['kok']}")
    typer.echo(
        f"\n{len(rows)} line(s)" if len(rows) < limit
        else f"\nthe first {limit}; -n asks for more"
    )
    typer.echo("  greels reel add <slug> --line '<phrase>'   puts one in a cut")


@app.command()
def related(
    query: str = typer.Argument(..., help="Segment id, or part of a segment title."),
    limit: int = typer.Option(8, "--limit", "-n"),
    same_clip: bool = typer.Option(
        False, "--same-clip", help="Include moments from the same source file."
    ),
):
    """Moments that resemble this one, across the whole archive."""
    from .similarity import Space

    try:
        space = Space.load()
    except FileNotFoundError as exc:
        raise _needs_index(exc)
    matches = space.find(query)
    if not matches:
        typer.echo(f"no segment matching {query!r}")
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(f"{len(matches)} segments match {query!r}:")
        for segment in matches[:12]:
            typer.echo(f"  {segment.id:52} {segment.title}")
        typer.echo("\nnarrow the query, or pass a full segment id")
        raise typer.Exit(1)

    source = matches[0]
    typer.echo(f"{source.title}")
    typer.echo(f"  {source.stem}  {_hms(source.start)}-{_hms(source.end)}")
    typer.echo("")

    hits = space.related(source.id, limit=limit, cross_clip=not same_clip)
    if not hits:
        typer.echo("nothing related")
        return
    for score, segment, shared in hits:
        # A rejected moment is still related; it is just not one to reach for.
        mark = " (rejected)" if segment.rating == "rejected" else ""
        typer.echo(
            f"{score:.2f}  {segment.title[:46]:48} "
            f"{_hms(segment.start)}-{_hms(segment.end)}{mark}"
        )
        typer.echo(f"      {segment.stem[:44]:46} via {', '.join(shared)}")


@app.command()
def tags(
    namespace: str = typer.Option(None, "--namespace", "-N", help="Only this namespace."),
    sort: str = typer.Option("signal", "--sort", help="signal | count | name"),
    unused: bool = typer.Option(False, "--unused", help="Only vocabulary nothing uses."),
):
    """Every tag, with how much of the archive it actually covers.

    `signal` is how much a tag narrows things down: a tag on almost every
    segment tells you nothing and scores near zero, a rare one scores high.
    It is what drives `related` and `reels`.
    """
    from .taxonomy import Taxonomy

    taxonomy = Taxonomy.load()
    counts: dict[str, tuple[int, int]] = {}
    signal: dict[str, float] = {}
    proposed: dict[str, int] = {}

    indexed = True
    try:
        from .similarity import Space

        space = Space.load()
        signal = space.idf
        for tag in space.idf:
            members = [s for s in space.segments.values() if tag in s.tags]
            counts[tag] = (len(members), len({s.stem for s in members}))
    except FileNotFoundError:
        indexed = False
        typer.echo("(no index yet -- run `greels index` for usage counts)\n")

    for sidecar in library.load_all():
        for segment in sidecar.segments:
            for tag in segment.proposed_tags:
                proposed[tag] = proposed.get(tag, 0) + 1

    names = sorted(set(taxonomy.tags) | set(counts))
    if namespace:
        names = [t for t in names if t.startswith(f"{namespace.rstrip(':')}:")]
    if unused:
        if not indexed:
            typer.echo("! --unused needs an index; run `greels index` first")
            raise typer.Exit(1)
        names = [t for t in names if not counts.get(t, (0, 0))[0]]

    keys = {
        "count": lambda t: (-counts.get(t, (0, 0))[0], t),
        "name": lambda t: t,
        "signal": lambda t: (-signal.get(t, 0.0), t),
    }
    if sort not in keys:
        typer.echo(f"! --sort must be one of {', '.join(keys)}")
        raise typer.Exit(1)

    shown = 0
    for group in sorted({t.split(":", 1)[0] for t in names}):
        members = sorted([t for t in names if t.startswith(f"{group}:")], key=keys[sort])
        if not members:
            continue
        typer.echo(f"\n{group.upper()}")
        for tag in members:
            segments, clips = counts.get(tag, (0, 0))
            label = taxonomy.label(tag)
            if not segments:
                typer.echo(f"  {tag:30} {label:22} {'unused' if indexed else '-'}")
            else:
                typer.echo(
                    f"  {tag:30} {label:22} {segments:4d} segment{'s' if segments != 1 else ' '}"
                    f" /{clips:3d} clip{'s' if clips != 1 else ' '}"
                    f"   signal {signal.get(tag, 0.0):.2f}"
                )
            shown += 1

    if taxonomy.aliases and not unused and not namespace:
        typer.echo("\nALIASES  (folded in at reclassify time)")
        for source, target in sorted(taxonomy.aliases.items()):
            typer.echo(f"  {source:30} -> {target}")

    if proposed and not unused:
        typer.echo("\nPROPOSED  (quarantined, not exported to Final Cut)")
        for tag, n in sorted(proposed.items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {tag:30} x{n}")

    summary = f"\n{shown} shown  |  {len(taxonomy.tags)} in vocabulary"
    if indexed:
        idle = [t for t in taxonomy.tags if not counts.get(t, (0, 0))[0]]
        summary += f", {len(idle)} unused"
    typer.echo(summary)


@app.command()
def reels(
    limit: int = typer.Option(10, "--limit", "-n", help="How many to suggest."),
    tag: str = typer.Option(None, "--tag", "-t", help="Show one suggestion in full."),
):
    """Suggest reels: coherent material gathered from several source files."""
    from .similarity import Space

    try:
        space = Space.load()
    except FileNotFoundError as exc:
        raise _needs_index(exc)
    suggestions = space.reels(limit=40 if tag else limit)
    if tag:
        suggestions = [r for r in suggestions if r.tag == tag or r.title.lower() == tag.lower()]
        if not suggestions:
            typer.echo(f"no reel suggestion for {tag!r}; run `greels reels` to see them")
            raise typer.Exit(1)

    for reel in suggestions:
        typer.echo(
            f"\n{reel.title.upper()}  —  {len(reel.segments)} moments / "
            f"{len(reel.clips)} clips / {reel.duration:.0f}s"
        )
        typer.echo(f"  {reel.tag}   coherence {reel.coherence:.2f}")
        for segment in reel.segments:
            star = "*" if "quality:reel_candidate" in segment.tags else " "
            typer.echo(
                f"  {star} {segment.duration:5.0f}s  {segment.title[:44]:46} "
                f"{segment.stem[:30]} {_hms(segment.start)}"
            )
    if not tag and suggestions:
        typer.echo("\n* = tagged reel_candidate.  greels reels --tag <tag> for one in full")


@app.command()
def serve(
    port: int = typer.Option(8722, "--port", "-p"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser."),
    autosave: float = typer.Option(
        None, "--autosave",
        help="Seconds after the last edit to mirror into archive/. 0 turns it off.",
    ),
):
    """Browse and edit the archive in a browser. Localhost only."""
    import webbrowser

    from .server import AUTOSAVE_AFTER, serve as make_server

    if not config.db_path().exists():
        typer.echo("no index yet; building one")
        index_cmd()

    url = f"http://127.0.0.1:{port}/"
    delay = AUTOSAVE_AFTER if autosave is None else autosave
    try:
        httpd = make_server(port=port, autosave=delay, on_message=typer.echo)
    except OSError as exc:
        typer.echo(f"! cannot listen on {port}: {exc}")
        raise typer.Exit(1)

    typer.echo(f"greels on {url}   (ctrl-c to stop)")
    typer.echo("edits write straight back to the sidecars")
    typer.echo(
        f"they are mirrored into archive/ {delay:.0f}s after you stop typing, "
        "and on the way out" if delay else
        "autosave is off; run `greels backup` before you commit"
    )
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nstopped")
    finally:
        # An evening of corrections should not be lost to quitting.
        httpd.autosave.stop()
        httpd.server_close()


@app.command()
def align(
    only: str = ONLY,
    window: float = typer.Option(
        None, "--window", help="How far a line may move, in seconds."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would move."),
):
    """Move the subtitle timings onto the speech they describe.

    The model estimates line times rather than measuring them, and it shows:
    0.5, 4.0, 10.0, 12.5. Half a second out is enough for a caption to appear
    before the mouth moves, which is the thing everyone notices and nobody
    can name.

    `silencedetect` can hear where each utterance begins. This snaps every
    line to the nearest one, within a second, and leaves alone any line with
    no onset near it. About a fifth of them move, by half a second.

    **Only the lines.** A segment's own boundaries are left exactly where they
    are: segment ids are derived from their start time, so moving one
    renumbers it, which costs every reel that names it its provenance and
    every poster frame its picture. The gain is not worth that.
    """
    from . import index, speech, trash

    limit = window if window is not None else speech.WINDOW
    cache = {}
    total_moved = total_lines = touched = 0

    for chosen in _selected(only):
        stem = chosen.asset.stem
        runs = speech.runs_for(stem, cache)
        if not runs:
            continue
        # Re-read immediately before changing it. `_selected` loaded every
        # sidecar at the start, and by the time this reaches the sixtieth the
        # first is seconds old -- long enough for a correction typed in the
        # browser to be written over by a copy from before it.
        sidecar = library.load_sidecar(stem) or chosen
        moved = 0
        for segment in sidecar.segments:
            ordered = sorted(segment.lines, key=lambda l: (l.start, l.end))
            snapped = speech.snap_lines(runs, ordered, limit)
            for line, (start, end) in zip(ordered, snapped):
                total_lines += 1
                if abs(start - line.start) > 0.01 or abs(end - line.end) > 0.01:
                    moved += 1
                    line.start, line.end = start, end
            segment.lines = ordered
        if not moved:
            continue
        total_moved += moved
        touched += 1
        typer.echo(f"  {stem:52} {moved:3d} line(s)")
        if not dry_run:
            trash.keep(config.sidecar_path(stem), reason=f"aligned {stem} to speech")
            sidecar.save(config.sidecar_path(stem))

    if not total_moved:
        typer.echo("every line already sits on the speech it describes")
        return
    verb = "would move" if dry_run else "moved"
    typer.echo(f"\n{total_moved} of {total_lines} line(s) {verb}, across {touched} clip(s)")
    if dry_run:
        typer.echo("  nothing was changed; drop --dry-run to do it")
        return
    speech._save_cache(cache)
    index.build()
    typer.echo("  a copy of each sidecar as it was is in index/.trash")


@app.command()
def doctor(editor: str = EDITOR):
    """Check that everything this depends on is where it should be.

    `status` says where the footage has got to. This says whether the machine
    around it still works -- which is the question after a laptop change, an
    OS upgrade, or a drive that did not mount.

    The checks themselves live in `health`, because the browser draws the same
    ones in its header. A warning only a terminal shows is a warning nobody
    sees on an evening spent in the browser.
    """
    from . import health

    report = health.checkup(_editor(editor))
    marks = {health.OK: "ok  ", health.NOTE: "note", health.PROBLEM: "FAIL"}

    section = None
    for check in report.checks:
        if check.section != section:
            # A blank line between sections but not above the first one, which
            # is what this printed before the checks moved out of here.
            typer.echo(f"\n{check.section}" if section else check.section)
            section = check.section
        typer.echo(f"  {marks[check.level]}  {check.label:34} {check.detail}")

    problems, notes = len(report.problems), len(report.notes)
    typer.echo(f"\nvocabulary : {report.vocabulary} tags")
    typer.echo(
        "\neverything is in order" if not (problems or notes)
        else f"\n{problems} problem(s), {notes} thing(s) worth knowing"
    )
    if problems:
        raise typer.Exit(1)


@app.command()
def status(editor: str = EDITOR):
    """Where every clip currently sits in the pipeline."""
    from .taxonomy import Taxonomy

    paths = library.discover_raw()
    loaded = library.load_all()
    sidecars = {sc.asset.stem: sc for sc in loaded}
    taxonomy = Taxonomy.load()
    survey = identity.survey(paths, loaded)

    scanned = analysed = partial = 0
    proxied = chunked = 0
    duration = segments = 0.0
    proposed: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_rating: dict[str, int] = {}
    judged = 0
    unanalysed: list[str] = []

    for path in paths:
        stem = path.stem
        sc = sidecars.get(stem)
        if library.proxy_for(stem).exists():
            proxied += 1
        if library.chunks_for(stem):
            chunked += 1
        if not sc:
            continue
        scanned += 1
        duration += sc.asset.duration
        if sc.segments:
            analysed += 1
            segments += len(sc.segments)
            if sc.analysis.errors:
                partial += 1
            for seg in sc.segments:
                by_status[seg.status] = by_status.get(seg.status, 0) + 1
                by_rating[seg.rating] = by_rating.get(seg.rating, 0) + 1
                if seg.rating != "none" or seg.status != "indexed":
                    judged += 1
                for tag in seg.proposed_tags:
                    proposed[tag] = proposed.get(tag, 0) + 1
        else:
            unanalysed.append(stem)

    typer.echo(f"media root : {config.media_root()}")
    typer.echo(f"clips      : {len(paths)} found, {_hms(duration)} of footage")
    typer.echo(f"scanned    : {scanned}/{len(paths)}")
    typer.echo(f"proxied    : {proxied}/{len(paths)}  ({chunked} chunked)")
    typer.echo(f"analysed   : {analysed}/{len(paths)}" + (f"  ({partial} partial)" if partial else ""))

    from .export_fcpxml import held_clips

    which = _editor(editor)
    label = f"in {which.short.lower()}"
    held = held_clips(which)
    if held is None:
        typer.echo(f"{label}: cannot read the library")
    else:
        delivered = sum(1 for sc in loaded if sc.delivery(which.name).delivered)
        typer.echo(
            f"{label}: {sum(held.values())} clip(s), {delivered} tracked here"
        )
    if survey.duplicates:
        typer.echo(f"duplicates : {len(survey.duplicates)} file(s) in raw/ are footage you already have")
    typer.echo(f"segments   : {int(segments)}")
    if by_status:
        typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    if segments:
        # How far the review pass has got, which is what everything downstream
        # is waiting on: the favourites-first ordering in suggestions, `reel
        # unused`, and not being offered again a moment you turned down.
        # Judged the same way Review counts it -- a verdict or a "seen".
        typer.echo(
            f"  judged   : {judged} of {int(segments)}"
            f"  ({by_rating.get('favorite', 0)} kept, "
            f"{by_rating.get('rejected', 0)} rejected)"
        )
        _report_states(loaded)

    _report_reels(loaded)
    if unanalysed:
        preview = ", ".join(unanalysed[:6]) + (" ..." if len(unanalysed) > 6 else "")
        typer.echo(f"awaiting analysis: {preview}")
    if proposed:
        typer.echo(f"\nproposed tags ({len(proposed)}) awaiting review:")
        for tag, count in sorted(proposed.items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {tag:34} x{count}   -> greels taxonomy promote {tag}")
    typer.echo(f"\nvocabulary : {len(taxonomy.tags)} tags")


def _report_states(sidecars: list[Sidecar]) -> None:
    """How much of the footage has gone somewhere, in the browser's words.

    The same derivation the badges use, so "used" here and a green disc there
    cannot mean two different things.
    """
    from . import plans, reel as store

    found = plans.load_all()
    if not found and not store.load_all():
        return
    live = {s.id: s.model_dump() | {"stem": sc.asset.stem} for sc in sidecars for s in sc.segments}
    plans.reattach(found, live)
    tally = plans.counts(plans.segment_states(found, store.load_all(), live.values()))
    typer.echo("  state    : " + "  ".join(
        f"{name}={tally[name]}" for name in reversed(plans.STATES) if tally[name]))
    if found:
        stages: dict[str, int] = {}
        for plan in found:
            stages[plan.stage] = stages.get(plan.stage, 0) + 1
        posted = sum(1 for plan in found if plan.posted)
        finished = sum(1 for plan in found if plan.finished and not plan.posted)
        typer.echo(f"plans      : {len(found)}  "
                   + "  ".join(f"{s}={stages[s]}" for s in plans.STAGES if s in stages)
                   + (f"  finished={finished}" if finished else "")
                   + (f"  posted={posted}" if posted else ""))


def _report_reels(sidecars: list[Sidecar], only_problems: bool = False) -> None:
    """The cuts, and anything in them that has come loose.

    Worth surfacing here rather than only in `reel show`: a shot comes loose
    because of something that happened elsewhere -- a clip deleted, a file
    replaced -- so you find out while looking at the archive, not when you go
    to export.

    `sidecars` must be *every* clip, not just the analysed ones: a shot lifted
    from a clip that was later re-scanned and not yet re-analysed would
    otherwise be reported as having no clip at all.
    """
    from . import reel as store

    reels = store.load_all()
    if not reels:
        return

    if not only_problems:
        total = sum(r.duration for r in reels)
        by_state: dict[str, int] = {}
        for reel in reels:
            by_state[reel.status] = by_state.get(reel.status, 0) + 1
        stale = sum(1 for r in reels if r.stale)
        unrendered = sum(1 for r in reels if r.render_stale or not r.rendered_at)
        typer.echo(
            f"reels      : {len(reels)}, {_hms(total)} cut  "
            + "  ".join(f"{k}={v}" for k, v in sorted(by_state.items()))
            + (f"  ({stale} changed since export)" if stale else "")
            + (f"  ({unrendered} not rendered since)" if unrendered else "")
        )

    loose = [
        (reel.slug, problem)
        for reel in reels
        for problem in store.resolve(reel, sidecars).problems
    ]
    if loose and only_problems:
        typer.echo(f"\n{len(loose)} shot(s) in your cuts came loose:")
    for slug, problem in loose:
        typer.echo(f"  ! {slug}: {problem}")
    if loose and only_problems:
        typer.echo("  they stay in the running order and are left out of an export")


@taxonomy_app.command("promote")
def taxonomy_promote(
    tag: str = typer.Argument(..., help="e.g. theme:exams"),
    label: str = typer.Option(None, help="Final Cut keyword; defaults from the tag."),
):
    """Move a proposed tag into the vocabulary so it reaches Final Cut."""
    from .taxonomy import Taxonomy

    taxonomy = Taxonomy.load()
    try:
        promoted = taxonomy.promote(tag, label)
    except ValueError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    typer.echo(f"promoted {promoted} -> \"{taxonomy.label(promoted)}\"")
    typer.echo("run `greels reclassify` to move it out of proposed_tags, then `greels index`")


@taxonomy_app.command("alias")
def taxonomy_alias(
    source: str = typer.Argument(..., help="The near-duplicate, e.g. theme:historical"),
    target: str = typer.Argument(..., help="The tag it should fold into, e.g. tone:historical"),
):
    """Fold a proposed near-duplicate into an existing tag."""
    from .taxonomy import Taxonomy

    taxonomy = Taxonomy.load()
    try:
        resolved = taxonomy.alias(source, target)
    except ValueError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    typer.echo(f"{source} now folds into {resolved}")
    typer.echo("run `greels reclassify`, then `greels index`")


@app.command()
def reclassify():
    """Re-split every sidecar's tags against the current vocabulary."""
    from .taxonomy import Taxonomy

    taxonomy = Taxonomy.load()
    moved = 0
    for sc in library.load_all():
        changed = False
        for seg in sc.segments:
            known, proposed = taxonomy.split(seg.tags + seg.proposed_tags)
            if known != seg.tags or proposed != seg.proposed_tags:
                moved += len(known) - len(seg.tags)
                seg.tags, seg.proposed_tags = known, proposed
                changed = True
        if changed:
            sc.save(config.sidecar_path(sc.asset.stem))
    typer.echo(f"{moved} tag assignment(s) promoted out of quarantine")


@app.command()
def captions(
    project: Path = typer.Argument(..., help="An FCPXML exported from the editor."),
    lang: str = typer.Option("en", "--lang", help="Subtitle language: en | kok | both."),
    dest: Path = typer.Option(None, "--dest", help="Where to write the .srt."),
    by_speaker: bool = typer.Option(
        False, "--by-speaker",
        help="One .srt per voice, so an editor that imports .srt can colour "
             "each speaker by styling its track.",
    ),
    titles: bool = typer.Option(
        False, "--titles",
        help="Write the project back with the lines as styled titles, "
             "instead of an .srt Final Cut draws in a black box.",
    ),
    editor: str = EDITOR,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the captions without writing them."
    ),
):
    """Caption a cut you assembled by hand, in either editor.

    `reel export` captions a running order greels itself decided. This is for
    the other direction: once the cut has been argued with -- b-roll cut in, a
    line lifted, music under it -- its timings no longer resemble the reel's,
    and the translations still have to land on it.

    The document is read the same way whichever program wrote it. What
    `--editor` changes is what you are told to do with the result, because the
    menus are not the same: Final Cut imports an .srt with **File > Import >
    Captions**, Resolve with a right-click on the subtitle track. Nothing here
    writes to a library either way: the .srt lands next to the .xml, and
    importing it is additive, so the music and the trims are not round-tripped
    through anything.
    """
    from . import export_reel, import_fcpxml, render as engine

    if lang not in engine.LANGS:
        typer.echo(f"! --lang must be one of {', '.join(engine.LANGS)}")
        raise typer.Exit(1)
    which = _editor(editor)
    if titles and which.name != editors.FCP:
        # The styled titles are built on Final Cut's own `Basic Title.moti`,
        # which is a file inside Final Cut. Resolve resolves that reference to
        # nothing, so the export would import as a project full of empty
        # generators -- worse than not offering it.
        typer.echo(
            f"! --titles is Final Cut's: the styled title is built on its own "
            f"Basic Title template, which {which.label} cannot resolve.\n"
            f"  the .srt goes in as a subtitle track you can style once: "
            f"{which.captions_menu}"
        )
        raise typer.Exit(1)
    if not project.exists():
        typer.echo(f"! no such file: {project}")
        raise typer.Exit(1)

    try:
        cut = import_fcpxml.read(project)
    except import_fcpxml.Unreadable as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)

    typer.echo(
        f"{cut.project}: {len(cut.shots)} shot(s) off {len(cut.reel.clips)} clip(s), "
        f"{cut.duration:.1f}s"
    )
    for note in cut.skipped:
        typer.echo(f"  - {note}: not in the archive, so nothing to say for it")
    for problem in cut.problems:
        typer.echo(f"  ! {problem}")

    body = export_reel.srt(cut, lang=lang)
    if not body:
        typer.echo("nothing is said in this cut -- no captions to write")
        return
    count = body.count(" --> ")

    if dry_run:
        typer.echo(body)
        typer.echo(f"{count} caption(s); nothing written")
        return

    if titles:
        from . import export_titles

        out = dest or project.with_name(f"{project.stem} captions.fcpxml")
        try:
            written, made = export_titles.write(project, dest=out, lang=lang)
        except ValueError as exc:  # the DTD refused it
            typer.echo(f"! {exc}")
            raise typer.Exit(1)
        typer.echo(f"{made} title(s) -> {written}")
        typer.echo("import with File > Import > XML; it arrives as a second "
                   "project called '... + captions', so the one you have is safe")
        return

    if by_speaker:
        # An .srt has nowhere to put a speaker, so the split *is* the speaker:
        # one file per voice, imported as its own track and styled once.
        parts = export_reel.srt_by_speaker(cut, lang=lang)
        folder = dest or project.parent
        folder.mkdir(parents=True, exist_ok=True)
        for who, part in sorted(parts.items()):
            where = folder / f"{project.stem}.{who}.srt"
            where.write_text(part)
            typer.echo(f"{part.count(' --> '):3d} caption(s) -> {where}")
        typer.echo("import each as its own caption track, and colour the track")
        return

    out = dest or project.with_suffix(".srt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    typer.echo(f"{count} caption(s) -> {out}")
    typer.echo(f"import with {which.captions_menu}, with the project open")


@export_app.command("fcpxml")
def export_fcpxml_cmd(
    only: str = ONLY,
    dest: Path = typer.Option(None, help="Output path."),
    event: str = typer.Option(
        None, help="Final Cut event / Resolve bin name. Defaults to the project's name."
    ),
    new: bool = typer.Option(
        False, "--new", help="Only clips the editor has not been given yet."
    ),
    editor: str = EDITOR,
):
    """Write the editor's library import."""
    from .export_fcpxml import write

    which = _editor(editor)
    if new and dest is None:
        dest = incoming_path(which)
    try:
        path, clips, segments = write(
            dest=dest, only=only, event_name=event, new_only=new, mark=new,
            editor=which,
        )
    except ValueError as exc:
        typer.echo(f"! {exc}")
        raise typer.Exit(1)
    if path is None:
        typer.echo("nothing to export")
        return
    ranges = "keyword ranges" if which.keyword_ranges else "marked ranges"
    typer.echo(f"{clips} clips, {segments} {ranges} -> {path}")
    typer.echo(f"import with {which.import_menu} in {which.label}")


@export_app.command("srt")
def export_srt_cmd(
    only: str = ONLY,
    lang: str = typer.Option("en", help="en | kok | both"),
    dest: Path = typer.Option(None, help="Output directory."),
):
    """Write one subtitle file per segment, timed from the segment's start."""
    from .export_srt import write

    path, count, pruned = write(dest_dir=dest, only=only, lang=lang)
    note = f", {pruned} orphan(s) removed" if pruned else ""
    typer.echo(f"{count} subtitle file(s){note} -> {path}")
    return path, count, pruned


@export_app.command("csv")
def export_csv_cmd(only: str = ONLY, dest: Path = typer.Option(None, help="Output path.")):
    """Write the flat cut list."""
    from .export_csv import write

    path, rows = write(dest=dest, only=only)
    typer.echo(f"{rows} row(s) -> {path}")


@export_app.command("all")
def export_all(only: str = ONLY, editor: str = EDITOR):
    """Write the FCPXML, subtitles and cut list in one go."""
    which = _editor(editor)
    export_fcpxml_cmd(
        only=only, dest=config.export_dir() / f"{config.export_stem()}{_suffix(which)}.fcpxml",
        event=None, new=False, editor=which.name,
    )
    export_srt_cmd(only=only, lang="en", dest=None)
    export_csv_cmd(only=only, dest=None)


def _selected(only: str | None, strict: bool = True) -> list[Sidecar]:
    """The sidecars a command should act on.

    `strict` is what separates a command you typed -- where an empty result is a
    mistake worth stopping for -- from a step inside `sync`, where an archive
    with nothing in it yet is simply the first run.
    """
    sidecars = library.load_all()
    if not sidecars:
        if not strict:
            return []
        typer.echo("no sidecars yet; run `greels scan` first")
        raise typer.Exit(1)
    if only:
        wanted = set(library.match([sc.asset.stem for sc in sidecars], only))
        sidecars = [sc for sc in sidecars if sc.asset.stem in wanted]
        if not sidecars and strict:
            typer.echo(f"nothing matched {only!r}")
            raise typer.Exit(1)
    return sidecars


if __name__ == "__main__":
    app()
