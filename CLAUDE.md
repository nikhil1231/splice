# Working on Splice

The README is the user guide and describes what the tool does. If a change
makes a sentence in it untrue, fix the sentence in the same change. The reasons
behind the design are in the code comments and docstrings, so keep those
current too.

## What must not be lost

- `index/*.seg.json` — hours of Gemini time, and hand-corrected transcripts
  that cannot be produced again at all.
- `index/reels/*.reel.json` — a running order and its trims are a decision;
  nothing regenerates a decision.
- `index/plans/*.plan.json` and `index/plans/schedule.json` — a plan's stage,
  the takes ticked for its edit, whether it was posted and the order the ready
  ones go out in are the only record of which moments were published.

Everything else is derived and safe to delete: `library.sqlite`, `work/`,
`export/`. A segment's state (unseen … published) is derived too, by
`plans.segment_states`, and is never stored. Before any write that destroys
one of the files above, call `trash.keep()`. `backup` mirrors them, and the
project's own `splice.yaml`, `taxonomy.yaml` and prompts, into the archive its
`splice.yaml` names -- a private repo of its own, never this one -- and the
server does that on its own 90 seconds after the last edit. A scratch
`SPLICE_ROOT` has no splice.yaml and so no archive, and an archive refuses a
media root other than the one in its manifest.

Never edit the user's actual archive to test something. Build the command, run
it with `--dry-run`, and leave the decision to them. If a check needs a cut,
make a scratch one and delete it afterwards.

## The shape of the thing

Nothing in `src/` knows about any one family. What the footage is, its
language, who is in it and where it is backed up are `config.project()`, read
from the project's `splice.yaml`; the prompts and starting vocabulary are in
`src/splice/defaults/`, and a project's own copies win. The tests' cast (a
family of four and a camera, speaking Konkani) is `tests/project/`, copied
fresh for every test by `conftest.py` -- which also points `SPLICE_ROOT`,
`XDG_CONFIG_HOME` and the archive away from whoever is running them.

The analysis fingerprint (`analyze.prompt_sha`) is what decides which clips are
re-analysed. Anything that changes the rendered prompt or `schema.agy_schema()`
for an existing project offers its whole archive for hours of re-analysis;
`test_the_cast_renders_the_schema_exactly_as_it_was_before_projects` pins it.

`config` (paths) → `probe`/`identity` → `proxy` → `analyze` → `schema.Sidecar`
→ `index` (SQLite, thrown away and rebuilt) → `search`/`similarity` →
`reel.Reel` → `export_reel`/`render`.

`editors.py` sits across the delivery end of that: Final Cut and Resolve are
two importers with different capabilities, not one with two names, and every
place that writes a document, reads a library or prints a menu path asks it
which. `fcp.py` and `resolve.py` are the two readers, and both answer `None`
for "cannot see", which every caller is written against.

Three rules the design rests on:

- **The sidecars are the source of truth.** SQLite is rebuilt from them on
  every write. Never store anything in SQLite that nothing regenerates.
- **A shot is `(clip, in, out)`**, not a segment id — segment ids are derived
  from their start time, so re-analysis renumbers them.
- **Delivery is observed, never asserted, and it is per editor.** A clip in
  Final Cut's library is not a clip in Resolve's media pool. Writing a file is
  an offer; only reading the editor's own catalogue confirms an import.

## The maintainer's machine

`ffmpeg` here has **no libass and no freetype**: no `subtitles`, no
`drawtext`. Captions are drawn with Pillow in `captions.py` and composited as
overlays. Check `ffmpeg -filters` before proposing anything that puts text on
a frame. VideoToolbox encoders are present and much faster than x264; the
software path is a fallback, not a default.

Both editors are installed. Apple ships every FCPXML DTD from 1.0 to 1.13
inside Final Cut, so a Resolve export at 1.10 is still validated against
Apple's schema. Resolve is 20.0, and its scripting module imports — but
`scriptapp('Resolve')` returns `None` unless Resolve is running, which is the
case the tests stand in for. Do not open the user's Resolve to check
something; fake the media pool the way `tests/test_resolve.py` does.

## Style

- Comments and docstrings say **why**, and name the thing that went wrong. Not
  "cache the result" but "a 26 ms blip is the detector's ramp-up, not an
  utterance." If a constant has a value, the comment says where the value came
  from — a measurement, not a guess.
- Commit messages are prose in the same voice: what was wrong, what it now
  does, and what was deliberately not done. Present tense, no bullet-point
  changelogs unless there really are several separate things.
- Tests are named as sentences about behaviour
  (`test_a_loose_shot_is_left_out_and_said_out_loud`), and their docstrings
  carry the reason. ffmpeg and agy are stood in for; what is tested is the
  decision taken before the subprocess runs.
- Prefer measuring to asserting. Several claims in the README are numbers that
  were run; if you add one, run it.

## Traps that have bitten

- A typer command is a plain function, and an option you do not pass arrives
  as an `OptionInfo`, which is truthy. `tests/test_command_calls.py` enforces
  that every caller passes every option — add options to a command and that
  test tells you who to update.
- A looped image input (`-loop 1`) is infinite, and an overlay whose main
  input ends against one does not end.
- Two things called `notes` in one payload: the reel has its own field, and
  the critique used to overwrite it.
- **Load late, save soon.** Anything that loads a sidecar or a cut, does slow
  work, and then saves what it loaded will write over whatever happened in
  between — a render takes a minute and does not block the browser, an export
  writes three files, `align` walks sixty sidecars. Re-read immediately before
  saving, and stamp only the fields you own.

## Checks

```bash
uv run pytest -q          # ~930 tests, about 11 seconds
uv run splice doctor      # whether the machine around the archive still works
uvx ruff check --select F401,F841 src/ tests/
```

`pytest | tail` hides the exit status — check it separately before committing.
