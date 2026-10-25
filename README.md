# Splice

Splice helps you cut short videos out of a large collection of personal
footage. It sends each clip to Gemini, which splits it into moments and
returns a title, a summary, a transcript, an English translation and tags for
each one. You then search, review and plan in a local web app, and export clips,
rough cuts and captions to Final Cut Pro or DaVinci Resolve.

```
raw/*.MOV ─▶ work/proxies ─▶ work/chunks ─▶ agy ─▶ index/*.seg.json
                                                        │
                                          ┌─────────────┼──────────────┐
                                          ▼             ▼              ▼
                                 library.sqlite   <project>     srt/ +
                                   (search)         .fcpxml      cutlist.csv
                                          │
                                          ▼
                              index/reels/*.reel.json ─┬▶ export/reels/*.fcpxml
                                    (cuts)              │      (a timeline)
                                                        └▶ export/reels/*.mp4

            editor ──▶ Ep1.fcpxml ──▶ splice captions ──▶ Ep1.srt ──▶ editor
```

There are two kinds of output:

- **Material**: every clip, with its moments marked as ranges, imported into
  your editor's library.
- **Cuts**: a running order of trimmed shots, imported as a timeline you can
  keep editing.

`splice captions` goes the other way. It reads a timeline you finished in the
editor and writes subtitles for it.

## Requirements

- macOS. Proxies and renders use VideoToolbox, captions use fonts that ship
  with macOS, and exports are validated against the FCPXML DTDs inside Final
  Cut Pro. Other platforms are untested.
- [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`).
- `agy`, Google's Antigravity CLI, on your `PATH` and signed in. Splice runs it
  to call Gemini. Analysis and draft scripts are the only things that use the
  network.
- Final Cut Pro or DaVinci Resolve.

## Setup

```bash
uv sync
uv run splice init ~/Movies/family     # creates the project folder and splice.yaml
uv run splice doctor                   # checks ffmpeg, agy, the editor and the folders
```

Edit `splice.yaml`, put footage in `raw/`, then run `uv run splice sync`.

## The project

A project is one folder:

| Path | What it holds |
|---|---|
| `raw/` | Your footage. Subfolders are fine. |
| `splice.yaml` | What the footage is, the language spoken, who is in it, where backups go. |
| `taxonomy.yaml` | The tag vocabulary. `init` copies a starter set. |
| `prompts/` | Optional. Your own versions of the prompts. |
| `index/` | One `<clip>.seg.json` per clip, plus cuts, plans and drafts. |
| `work/`, `export/` | Proxies, caches and exported files. Safe to delete. |

### splice.yaml

```yaml
name: Family                  # Final Cut event / Resolve bin; also names family.fcpxml
footage: family interviews filmed on a phone, cut into short vertical videos
language: Portuguese          # what people speak; every line is also translated to English
transcription: ""             # how to write it down, if not just the language name
archive: ~/Documents/family-archive   # where `splice backup` copies your work
people:
  - id: grandma               # used as the tag person:grandma and as a speaker id
    name: Grandma
    describe: the older woman # given to the model so it can tell people apart
    colour: 1 0.85 0.4 1      # caption colour in Final Cut, "r g b a" from 0 to 1
  - id: both
    name: Both Grandparents
    word: grandparents        # prefix for clips renamed after this group
    speaker: false            # a tag only, never assigned a line
  - id: me
    name: Me
    camera: true              # the person filming; never used to name a clip
```

Every field is optional. Unknown fields are an error, so typos are caught.

Splice looks for the project in this order:

1. `SPLICE_ROOT`, if set.
2. The nearest folder at or above the current directory that contains a
   `splice.yaml`.
3. The `root:` in `~/.config/splice/config.yaml`, which `init` sets.

### Prompts

The prompts live in `src/splice/defaults/prompts/`:

- `analyze.md`: watch one clip and return its moments.
- `drafts.md`: propose short stories from the transcripts.

They use `{footage}`, `{spoken}`, `{written}` and `{speakers}`, which are filled
in from `splice.yaml`. To customise a prompt for one project, copy it into that
project's `prompts/` folder and edit the copy. Splice uses a project copy as-is.

Each analysed clip stores a fingerprint of the prompt and schema it was analysed
with. If you change the analysis prompt, the language or the people, the
fingerprint changes and the next `sync` re-analyses every clip. Changing
`taxonomy.yaml` does not.

The transcript fields in the sidecars are named `konkani` and `kok` for every
language. Renaming them would change every existing fingerprint. The browser
and the captions label them with the project's `language`.

## Daily use

```bash
uv run splice sync        # process everything new in raw/
uv run splice serve       # open http://127.0.0.1:8722
```

`sync` runs these steps in order:

1. Find new or changed clips in `raw/`.
2. Build small proxies and split long ones into chunks.
3. Analyse new clips with agy.
4. Re-apply the tag vocabulary.
5. Rename camera-numbered clips (`IMG_1234`) after their content.
6. Rebuild the search index.
7. Measure loudness and where speech is, and move subtitle timings onto the
   speech.
8. Check what your editor already holds, and write `export/incoming.fcpxml`
   with only the clips it doesn't have.
9. Back up to the archive.

Import `incoming.fcpxml` in Final Cut with **File ▸ Import ▸ XML**. Every step
is safe to repeat, and `sync --dry-run` shows what would happen without
changing anything.

Clips are matched by content, not filename. Copying the same footage again
under another name is detected and skipped. Renaming a clip in Finder is
picked up and the analysis is kept. Replacing a file with a different one
triggers re-analysis, and the old segments are kept until the new analysis
finishes.

The individual steps:

```bash
uv run splice scan / proxy / analyze -j 3 / rename / index / export all
uv run splice align --dry-run             # subtitle timings onto the speech
uv run splice thumbs                      # poster frames (otherwise built on demand)
uv run splice status                      # where each clip is in the pipeline
uv run splice status --editor resolve
uv run splice doctor                      # check the machine and the project
```

Analysis is slow: expect roughly two to three times a clip's length. agy fails
now and then. Each chunk is retried, and a failure is recorded in the sidecar
and retried on the next run.

## The browser

`uv run splice serve` starts a local server on 127.0.0.1. Edits are written
straight to the sidecars and the index is rebuilt. The header has **Sync raw**,
**Back up**, a **Run ▾** menu with the other commands, and a health indicator
that runs the same checks as `splice doctor`. While a command that writes is
running, edits are refused rather than lost.

| Page | What it is for |
|---|---|
| **Plans** | Ideas for videos. Each has a stage (idea, gathering, ready, shelved) and a pool of moments; tick the ones you'll use. |
| **Schedule** | Plans marked ready, in the order they'll go out. Mark each one Finished, then Posted. |
| **Segments** | Search everything. The selected moment opens beside the results so you can play it, fix its title, tags and transcript lines, and add it to a plan. |
| **Review** | Go through moments one at a time with the keyboard. |
| **Clips** | Watch a whole clip with its full transcript. Click a line to jump to it; shift-click a second line to make a shot from the range. |
| **Reels** | Draft scripts, suggested reels, your cuts, and caption files for timelines you edited yourself. |

Review keys: <kbd>J</kbd>/<kbd>K</kbd> move, <kbd>F</kbd> keep, <kbd>X</kbd>
reject, <kbd>S</kbd> seen, <kbd>,</kbd>/<kbd>.</kbd> slower/faster,
<kbd>E</kbd> edit. The default order puts moments tagged
`quality:reel_candidate` first.

Cut player keys: <kbd>Space</kbd> play, <kbd>I</kbd>/<kbd>O</kbd> set in/out,
<kbd>←</kbd>/<kbd>→</kbd> previous/next shot, <kbd>[</kbd>/<kbd>]</kbd> nudge
half a second.

Undo and redo (⌘Z / ⇧⌘Z) work on Plans, Schedule and Segments. Every page and
open item has its own URL.

### Moment states

Each moment's state is worked out from plans, cuts and its rating. It is never
stored.

| State | Meaning |
|---|---|
| Published | Chosen for a posted plan, or in a published cut |
| Used | In a cut |
| Planned | Ticked in a ready plan |
| Proposed | In a plan, but not ticked in a ready one |
| Rejected | Rated reject, and in no plan or cut |
| Seen | Reviewed, and in no plan or cut |
| Unseen | None of the above |

## Finding moments

```bash
uv run splice search "things that used to cost little"   # titles, summaries, tags, speech
uv run splice search --order promise
uv run splice search --tag person:grandma --tag tone:funny
uv run splice search --tag theme:money --unused --rating favorite
uv run splice lines "train ticket"                         # each time somebody says it
uv run splice related "train"                              # moments similar to this one
uv run splice reels                                        # suggested reels across clips
uv run splice tags                                         # the vocabulary and its coverage
```

`lines` searches transcript lines, in the original language and in English,
and returns timestamps. `related` scores pairs of moments by shared rare tags
and shared words. `reels` groups moments from at least three clips around one
tag or a pair of tags. Rejected moments are left out, and favourites come
first. None of these call a model.

## Tags

`taxonomy.yaml` is the controlled vocabulary. `fcp_label` is the name shown in
Final Cut's keyword list and in Resolve marker notes. The people in
`splice.yaml` are added as `person:` tags automatically.

Tags the model makes up go into `proposed_tags` and stay out of exports until
you accept them:

```bash
uv run splice taxonomy promote theme:exams                      # accept a proposed tag
uv run splice taxonomy alias theme:historical tone:historical   # fold it into an existing one
uv run splice tags --unused                                     # vocabulary nothing uses yet
uv run splice reclassify && uv run splice index
```

`sync` runs `reclassify` for you.

## Draft scripts

**Reels ▸ Draft scripts** asks Gemini for up to three short stories (30–90
seconds) from your transcripts. You can give it a brief, or pick the moments
to draw from. It reads the summaries first, then the full transcripts of the
moments it picked. The model only returns references to transcript lines.
Splice looks up the actual words and times, so quotes can't be invented.

Each draft lists its beats with a reason for each one. You can reorder, trim,
swap in an alternative take, play the sequence, and export it as a cut to
either editor. Drafts are saved in `index/drafts/`. `SPLICE_DRAFT_MODEL`
chooses the model and defaults to `SPLICE_AGY_MODEL`.

## Cuts

A cut is a running order of shots, each one a clip with in and out points.
Cuts are saved in `index/reels/<slug>.reel.json`.

```bash
uv run splice reel new "Money" --tag theme:money --rating favorite --order shot
uv run splice reel new "Trains" --from theme:travel     # from a suggested reel
uv run splice reel new "Every ticket" --said ticket     # one shot per time it's said
uv run splice reel add money "first job"                # a segment id or part of a title
uv run splice reel add money --search "old photo"
uv run splice reel add money --line "train ticket"      # one sentence as a shot
uv run splice reel trim money 2 --start 5 --end 40
uv run splice reel move money 3 1
uv run splice reel order money --by shot                # or long, short, clip
uv run splice reel snap money --dry-run                 # pull trims to where speech starts and ends
uv run splice reel duplicate money "Money, shorter"
uv run splice reel show money --lines
uv run splice reel unused --rating any -t theme:money   # good moments no cut uses
```

`reel show` also lists likely problems: the cut is long, a shot starts or ends
mid-sentence, a caption is too fast to read, or two shots in a row could be
one. These are suggestions, not errors.

Shots remember their clip and times, not a segment id, so re-analysing a clip
doesn't break a cut. A shot whose clip has gone missing stays in the cut and is
reported, but is left out of exports.

### Exporting a cut

```bash
uv run splice reel export money                     # Final Cut timeline
uv run splice reel export money --editor resolve    # Resolve timeline
uv run splice reel export money --editor resolve --send   # straight into the open Resolve project
```

This writes three files to `export/reels/`:

- `<slug>.fcpxml` (or `<slug>.resolve.fcpxml`): the timeline. Clip volumes are
  matched. In Final Cut the spoken lines arrive as captions.
- `<slug>.srt`: the captions, timed to the cut.
- `<slug>.txt`: the running order with what is said in each shot.

`--lang both` puts the original language above the English, and
`--no-captions` leaves captions out.

### Rendering a cut

```bash
uv run splice reel render money                     # export/reels/money.mp4
uv run splice reel render money --fit               # small enough to send by chat or email
uv run splice reel render money --lang both -o ~/Desktop/money.mp4
uv run splice reel render money --from proxy        # quick low-res preview
uv run splice reel render money --aspect 1:1        # crop to a square
uv run splice reel render money --bitrate 16M
uv run splice reel render --all --stale             # re-render every cut that changed
```

Renders keep the footage's shape unless you pass `--aspect` (9:16, 4:5,
1:1 or 16:9). Clip loudness is matched to −16 LUFS (`--no-level` turns
this off). Subtitles are included as a track you can turn off, or burned into
the picture with `--captions burn`. Burned captions are drawn with Pillow,
because Homebrew's ffmpeg has no text filters. A clip in a different aspect
ratio is placed over a blurred copy of itself; use `--fill pad` or
`--fill crop` instead if you prefer.

## Captioning a cut you edited yourself

Once you've finished a cut in the editor, export its XML and run:

```bash
uv run splice captions ~/Downloads/Ep1.fcpxmld              # writes Ep1.srt
uv run splice captions ~/Downloads/Ep1.fcpxmld --by-speaker # one .srt per speaker
uv run splice captions ~/Downloads/Ep1.fcpxmld --titles     # styled titles (Final Cut only)
uv run splice captions ~/Downloads/Ep1.fcpxmld --lang both --dry-run
uv run splice captions ~/Downloads/Ep1.fcpxml --editor resolve
```

Splice reads the timeline, finds which parts of which clips it uses, and
writes the matching transcript lines with the timeline's own timings. Footage
it doesn't know, such as music or title cards, is skipped. Both Final Cut's
`.fcpxmld` bundles and plain `.fcpxml` files work. You can also drop the file
on **Reels ▸ Caption a cut you made** in the browser.

- **Final Cut:** select the project, then **File ▸ Export XML**. Import the
  `.srt` with **File ▸ Import ▸ Captions**. `--titles` instead writes a copy
  of the project with the lines as styled titles (bold white text with an
  outline), which you import with **File ▸ Import ▸ XML**.
- **Resolve:** **Timelines ▸ Export ▸ FCPXML 1.10**. Import the `.srt` onto a
  subtitle track.

`--by-speaker` writes one file per speaker, so you can style each one as its
own subtitle track. Lines with no speaker go to `unattributed`.

## Final Cut or Resolve

Every command that exports takes `--editor fcp` (the default) or
`--editor resolve`, and `SPLICE_EDITOR` changes the default. The browser has
the same choice in its header.

| | Final Cut | Resolve |
|---|---|---|
| FCPXML version | 1.11 | 1.10 |
| Clips arrive as | an event | a timeline with every clip end to end |
| A moment is | a keyword range | a marker with a duration |
| Tags appear in | keyword collections | marker notes |
| Favourite / reject | a rating | not carried |
| Spoken lines | captions on the timeline | the `.srt` file |
| Re-exporting a clip | adds a second copy (`- a1`) | refreshes the existing clip |
| Splice can check what's imported | always | only while Resolve is running |

Final Cut files keep plain names (`incoming.fcpxml`), and Resolve files are
named `*.resolve.fcpxml`, so neither overwrites the other. Import Resolve
files with **File ▸ Import ▸ Timeline**.

### What has been imported

Splice reads the editor's own library to see which clips it holds: Final Cut's
`.fcpbundle` in the project folder, or Resolve's media pool through its
scripting API. A clip that was exported but never imported is offered again on
the next sync. If the editor can't be read, nothing is changed. Set
`SPLICE_FCP_LIBRARY` if your Final Cut library lives somewhere else.

To mark clips as imported by hand:

```bash
uv run splice imported                      # everything analysed so far
uv run splice imported --only lunch         # matching clips only
uv run splice imported --reset              # start over, e.g. after deleting the library
uv run splice imported --editor resolve
```

Changing a moment's boundaries, title, tags or rating puts its clip back into
`incoming.fcpxml`. Fixing a transcript or summary doesn't, because none of
that is in the export. Final Cut can't update a clip in place, so it imports a
second copy, and `sync` lists the old copies for you to delete.

## Renaming clips

```bash
uv run splice rename                                  # show proposed names
uv run splice rename --apply
uv run splice rename --apply -s IMG_1234=birthday_cake
```

Names come from the longest moment in the clip, prefixed with the person who
appears most. Only camera-style names (`IMG_1234`, `DSC_…`, bare UUIDs) are
renamed unless you pass `--all`. Renaming updates the sidecar, proxies, cuts
and plans that refer to the clip, and every previous name is kept so the
editor's copy can still be matched.

## Backup

The work that can't be regenerated is the sidecars (especially corrected
transcripts), cuts, plans and drafts. `splice backup` copies them, along with
`splice.yaml`, `taxonomy.yaml` and your prompts, into the folder named by
`archive:`. Make that folder a **private** git repository and commit after
backups. It contains everything that was said in your footage.

```bash
uv run splice backup             # copy into the archive
uv run splice backup --check     # show what would change
uv run splice backup --restore   # copy the archive back over index/
```

`sync` backs up when it finishes, and `serve` backs up 90 seconds after your
last edit (`--autosave 0` turns this off). A backup is refused if the project
folder is missing, if it would delete more than a third of the archive, or if
the archive belongs to a different project folder. `--force` overrides the
last two.

Before anything is overwritten or deleted, a copy goes to `index/.trash/`:

```bash
uv run splice trash list
uv run splice trash restore 20260905T1147
```

## Files

- `index/<clip>.seg.json`: the source of truth for a clip, meaning its
  moments, transcript lines, tags, rating and what has been delivered to each
  editor. Plain JSON, safe to edit by hand.
- `index/reels/`, `index/plans/`, `index/drafts/`: cuts, plans (and
  `schedule.json`, their posting order) and draft scripts.
- `index/library.sqlite`: the search index, rebuilt from the sidecars on every
  change. Safe to delete.
- `work/`, `export/`: proxies, chunks, thumbnails, audio caches and exported
  files. Safe to delete.
- `plans/schedule.json` next to `splice.yaml`: optional. A series outline the
  browser offers as starting plans.

Re-analysing a clip replaces the model's output but keeps the ratings and
statuses you set. Each one is moved to whichever new moment covers the old
one's midpoint.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SPLICE_ROOT` | (see above) | Project folder |
| `SPLICE_CONFIG` | `<root>/splice.yaml` | Settings file |
| `SPLICE_ARCHIVE` | `archive:` in splice.yaml | Backup destination |
| `SPLICE_EDITOR` | `fcp` | Default editor |
| `SPLICE_FCP_LIBRARY` | the `.fcpbundle` in the project folder | Final Cut library to read |
| `SPLICE_AGY_BIN` | `agy` | agy executable |
| `SPLICE_AGY_MODEL` | `gemini-3.1-pro-high` | Analysis model |
| `SPLICE_DRAFT_MODEL` | same as analysis | Draft script model |
| `SPLICE_AGY_TIMEOUT` | `20m` | Per-call timeout |
| `SPLICE_AGY_SKIP_PERMISSIONS` | `1` | Lets agy read the video file without prompting; set `0` if you have allow-rules |

Chunk length, proxy size and similar settings are constants in
`src/splice/config.py`.

## Development

```bash
uv run pytest -q
uvx ruff check --select F401,F841 src/ tests/
```

ffmpeg, agy and the editors are faked in the tests. Each test runs in its own
temporary project, copied from `tests/project/`.

## License

MIT
