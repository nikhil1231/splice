# Splice

Turns a pile of family footage into something you can cut short videos from.
It watches every clip with Gemini, splits it into named moments with
transcripts, translations and tags, lets you search, review and plan in a
browser, and hands the result to Final Cut Pro or DaVinci Resolve as clips,
rough cuts and captions.

It was built for one archive -- hours of Konkani-speaking grandparents in Goa,
filmed on a phone and cut into Instagram reels -- and that archive is the
example all through this README: *Grandma*, *Grandpa*, the person holding the
camera asking the questions, the romanised Konkani above the English. Yours is
described in a `splice.yaml` instead: what the footage is, the language spoken,
and who is in it. See [Your project](#your-project).

The useful unit here is not a file, it is `video + start + end + meaning +
state`. `agy` (Gemini) does the analysis, because it can hear speech in
languages most transcription cannot. Draft scripts use a second,
transcript-only Gemini pass to propose stories; search, tag suggestions,
trimming, and exports remain deterministic.

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
                                    (the cuts)          │      (a timeline)
                                                        └▶ export/reels/*.mp4
                                                            (something to watch)

              the editor ──▶ Ep1.fcpxml ──▶ splice captions ──▶ Ep1.srt ──▶ back
                (the cut you actually made, captioned in its own running order)
```

The archive half hands the editor *material*: whole clips carved into named
ranges. The reel half hands it a *cut*: a spine of trimmed shots that opens as
a rough edit on the timeline. The last arrow goes the other way: once the cut
has been argued with, `captions` reads the finished timeline back and writes
the subtitles for *it*.

**The editor is Final Cut or DaVinci Resolve**, and which one is a flag —
`--editor resolve`, or the pair of buttons in the browser's header. The two
are not the same file with a different name on it, and what differs is in
[Final Cut or Resolve](#final-cut-or-resolve).

## An evening of it

The loop this is all for, once the footage is in:

```bash
uv run splice serve
```

1. **Review**, most promising first, at 1.5×. <kbd>F</kbd> keeps, <kbd>X</kbd>
   rejects, <kbd>E</kbd> opens it for fixing without leaving the pass. It says
   how much footage is left rather than how many moments, which is the number
   that tells you whether to carry on.
2. **Reels ▸ Draft scripts**: generate candidates with an optional brief or
   selected moments, read the proposed dialogue, and watch its context.
   Suggestions, searches, and manually assembled cuts remain available.
3. Drag the dialogue beats into your order and trim with <kbd>I</kbd> /
   <kbd>O</kbd> while they play. **Save draft** keeps your changes.
4. **Export to Final Cut** — or **Export to Resolve**, the button beside it —
   creates a fresh saved-cut snapshot and an editable timeline with matched
   levels. Import the XML in whichever you pressed.
5. Finish the rhythm and visual editing there. The saved snapshot also
   offers **Render one to send** for family feedback and transcript corrections.

Saved edits are mirrored into the project's archive a minute and a half later.
Commit that; it is the only copy of the transcripts and the cuts that git can
give you back.

## Setup

```bash
brew install ffmpeg
uv sync
uv run splice init ~/Movies/my-family    # a folder for the footage, and a splice.yaml
uv run splice doctor                     # what is missing
```

`uv sync` brings Pillow, which is here for one thing: Homebrew's ffmpeg cannot
draw text, so burnt-in captions are drawn as pictures and composited.

`agy` -- Google's Antigravity command line, which runs Gemini headless -- must
be on PATH and signed in; it is what watches the footage and writes the
transcripts. Nothing else here talks to a network.

Splice was written on a Mac and is most at home there: hardware encoding is
VideoToolbox, the caption fonts are the ones macOS ships, and exports are
checked against the FCPXML DTDs inside Final Cut. Resolve support exists so it
can run elsewhere, but that has not been finished.

## Your project

A project is one folder: the footage goes in `raw/`, everything Splice makes
goes in `index/`, `work/` and `export/`, and a `splice.yaml` says what the
footage is. `splice init` writes a commented one; the parts that matter:

```yaml
name: Grandparents        # the Final Cut event / Resolve bin, and grandparents.fcpxml
footage: family footage of Konkani-speaking grandparents in Goa, being cut into short Instagram reels
language: Konkani         # what people speak; every line is also translated into English
transcription: romanised Konkani
archive: ~/Documents/grandparents-archive   # where the backup goes: a private git repo
people:
  - id: grandma           # becomes person:grandma, and a speaker a line can be given
    name: Grandma
    describe: the older woman            # told to the model, to tell people apart
    colour: 1 0.85 0.4 1                 # her caption colour in Final Cut
  - id: both
    name: Both Grandparents
    word: grandparents                   # a clip about them is named grandparents_...
    speaker: false                       # a tag, never a voice
  - id: nikhil
    name: Nikhil
    camera: true                         # behind the camera; never what a clip is named after
```

Splice finds the project from `SPLICE_ROOT` if it is set, else from the nearest
folder above where it was started that holds a `splice.yaml`, else from the
default `splice init` records in `~/.config/splice/config.yaml` -- so `serve`
can be started from anywhere.

Beside `splice.yaml` sit the project's own **`taxonomy.yaml`**, the vocabulary
of tags (`init` copies a starting one), and optionally a **`prompts/`** folder.
The prompts Splice sends are `analyze.md` (watch a clip, return its moments)
and `drafts.md` (propose stories from transcripts), in `src/splice/defaults/`.
They fill `{footage}`, `{written}`, `{spoken}` and `{speakers}` from
`splice.yaml`. To change one for this project, copy it into `prompts/` and edit
the copy.

What the model was asked is fingerprinted into every sidecar, and `analyze`
re-runs any clip whose fingerprint no longer matches -- so editing the analysis
prompt, the language or the people offers the whole archive for re-analysis on
the next `sync`. Growing the vocabulary does not.

The transcript fields are still called `konkani` and `kok` in the sidecars and
the schema, whatever language they hold: renaming them would change the
fingerprint of every clip already analysed. The browser and the captions call
them by the project's `language`.

## Final Cut or Resolve

Everything that hands work over takes `--editor fcp` or `--editor resolve`,
and `SPLICE_EDITOR` sets which one a bare command means. The browser has the
same choice as a pair of buttons in the header, remembered there the way the
review speed is — it is a preference about how you work, not a fact about the
archive, so it is not in the files and an address does not carry it. Final
Cut is the default, because every sidecar here already has a Final Cut
delivery record and changing what a bare `sync` means would offer the whole
archive to Resolve on the next run.

A name that is neither stops the command rather than falling back. The point
of the flag is that the two documents differ, and a fallback turns `--editor
resolv` into an import that quietly does nothing.

**The differences are facts about two importers, not preferences:**

| | Final Cut | Resolve |
|---|---|---|
| FCPXML version | 1.11 | **1.10** |
| material arrives as | an event of clips | a **stringout** timeline |
| a segment is | a keyword range | a **marker** with a duration |
| its tags land in | keyword collections | the **marker note** |
| favorite / reject | a `<rating>` range | nothing |
| the spoken lines | captions on the timeline | the **`.srt`** beside it |
| a re-offered clip | a second copy, `- a1` | the same clip, refreshed |
| Splice can read it | always | **only while it is running** |

Taking those in turn, because each one cost something to find out:

- **Resolve does not speak 1.11.** Its own scripting README — Resolve 20,
  `Developer/Scripting/README.txt` — lists `EXPORT_FCPXML_1_8`, `_1_9` and
  `_1_10` and nothing newer, and records 1.3 to 1.7 as dropped in 18.1. So
  1.10 is the newest version both programs speak. Apple ships that schema too,
  so Resolve's documents are still validated against Apple's DTD before they
  are written; Blackmagic publishes nothing to check against.
- **Resolve's importer reads timelines, not events.** `ImportTimelineFromFile`
  is the only door in the scripting API and File ▸ Import ▸ Timeline the only
  one in the menus. An `<event>` full of clips is a document Resolve reads
  without error and makes nothing out of — no clips, no complaint, which is
  the worst failure available. So the material export for Resolve is a
  stringout: every new clip laid end to end on one timeline. The timeline is
  the vehicle; importing it brings the sources into the media pool, which is
  where the material was always headed.
- **Keyword ranges are Final Cut's alone, and this is the one real loss.**
  Clicking "Money" in Final Cut's sidebar returns sub-clip ranges from four
  different source files, and Resolve has nothing that does that. A marker is
  what it has: a name, a note, a colour and a length, listed and filterable in
  the Edit Index. So a segment goes over as a marker spanning the segment,
  with its tags and summary in the note, where they stay searchable. The
  sidebar was the fallback anyway — searching the transcripts here is what
  stopped anybody scrubbing clips in Final Cut.
- **Captions do not survive the trip.** Final Cut lifts `<caption>` elements
  off the timeline into its caption editor; Resolve ignores them. The `.srt`
  written beside the timeline is how the lines get there, and it arrives as a
  real subtitle track Resolve will style — which is better than the black slab
  Final Cut draws, and the reason `captions --titles` exists at all. So the
  Resolve timeline carries a marker at the head of each shot instead, because
  without either it arrives as twelve near-identical portrait clips of the
  same room with nothing saying which is which.
- **Delivery state is kept per editor.** A clip in Final Cut's library is not
  a clip in Resolve's media pool. Sharing one record would mark the whole
  archive as delivered to Resolve the moment the editor was switched, which is
  the bug that lost eight clips with the sign flipped. Final Cut's record
  stays where it has always been, in the sidecar's `exported`; the others live
  beside it under `deliveries`.
- **What "the import landed" means differs.** Final Cut cannot update a clip
  from XML, so a re-offer arrives as a second copy and the count growing is
  the proof. Resolve matches on the file path and reuses the clip it has, so
  there is no count to grow: an outstanding offer for a clip that is in the
  pool is taken as landed. That believes something it did not watch happen,
  and the limit is worth stating — the clip is demonstrably there; what is
  unverified is whether the new markers came with it, and a stale marker costs
  a re-import rather than a clip.
- **Resolve can only be read while it is running.** Final Cut's catalogue is a
  file, so it answers at three in the morning with Final Cut shut. Resolve's
  answer comes through the scripting API, which needs Resolve open with a
  project in it — and which Blackmagic ships as a Studio feature. An
  unreachable editor changes no import state, which is the same rule an
  unreadable Final Cut library has always followed, so a sync while Resolve is
  closed is inert rather than wrong. `splice imported --editor resolve` sets
  it by hand, and is needed far more often than its Final Cut equivalent.

**What is measured and what is reasoned**, since the two are not the same and
this file is the design record. The version ceiling is measured: Resolve 20's
own `Developer/Scripting/README.txt` is on this disk and lists what it lists,
and every document Splice writes is put through Apple's DTD at that version
before it lands, which is a real check that really runs. The importer's
behaviour is reasoned from Blackmagic's documented API — `ImportTimelineFromFile`
is the only entry point it publishes, and `<keyword>`, `<rating>` and
`<caption>` have no counterpart in anything the API exposes. That reasoning
has not been settled by importing a file into Resolve, because doing so would
mean making a project in somebody's real Resolve to find out. The tests fake
the media pool instead. If an import ever behaves differently from what this
section says, the section is the thing that was wrong.

The two editors' files never overwrite each other. Final Cut keeps the plain
names — `incoming.fcpxml`, `export/reels/<slug>.fcpxml` — and Resolve's are
marked: `incoming.resolve.fcpxml`, `<slug>.resolve.fcpxml`. `incoming.fcpxml`
promises that if the file is there, importing it is safe, and one editor's
document written over the other's breaks that promise without changing
anything you could see.

One thing goes the other way, and it is the reason Resolve is worth the trip:

```bash
uv run splice reel export 1961 --editor resolve --send
```

`--send` puts the timeline into the open Resolve project through the scripting
API — no file to find, no import dialog. Final Cut has no equivalent, so it is
refused there rather than writing the file and looking like it sent something.
When Resolve is shut, which is the ordinary case, it says so and leaves the
written timeline where it is: it is a convenience on top of a file, not
instead of one.

## Usage

Chuck clips in `raw/`, then:

```bash
uv run splice sync
```

That is the whole loop: scan, proxy, analyse whatever is new, reclassify against
the current vocabulary, name any camera-numbered clips, reindex, listen to each
new clip for its level and where the talking is, **move the new subtitles onto
that speech**, check what
the editor actually holds, write `export/incoming.fcpxml` containing **only
what the editor does not already have**, and **back the archive up**. Import that with **File ▸ Import ▸ XML**.
Run it again with nothing new and it says so, and deletes `incoming.fcpxml` — if
that file exists, it is always safe to import.

`sync --editor resolve` runs the same loop for Resolve, writes
`incoming.resolve.fcpxml` instead, and tells you to use **File ▸ Import ▸
Timeline**. The delivery state it keeps is Resolve's own, so the two do not
interfere.

The last two used to be `align` and `backup`, typed afterwards from memory. A
sync is exactly when both are wanted and exactly when neither gets typed: the
subtitles it has just written are the ones sitting half a second off the
speech, and the sidecars it has just made are, for that moment, the only copy
of hours of agy. Eleven of them sat outside git for two days. `align` moves
the lines only — no segment boundary, title, tag or rating changes, so nothing
it touches is re-offered to Final Cut as a second copy. A project with no
`archive:` does not back up at all, and an archive refuses a media root other
than the one that filled it: a backup mirrors one media root and deletes what
that root does not have.

Every step is safe to repeat, and nothing falls out between runs:

- **Clips are identified by their content, not their filename.** Re-copying
  footage you already have — under the camera's name, after `rename` gave it a
  real one — is recognised and skipped. No second proxy, no second analysis, no
  second clip in Final Cut.
- **What the editor holds is read, not assumed.** `sync` reads Final Cut's own
  catalogue, or Resolve's media pool through its scripting API. A clip you were
  offered and never imported comes back next time instead of disappearing. An
  editor that cannot be read — which for Resolve means one that is not
  running — changes no import state at all.

`sync --dry-run` says what a run would do and changes nothing.

The browser has the whole loop as a button: **Sync raw** in the header, beside
**Back up**. It opens with the dry run, so what an hour of agy would be spent
on is on screen before it is spent, and the run itself is the second click; its
output is the same lines the terminal gets, arriving as they are printed.
**Run ▾** beside it has the rest of Splice — align, rename, the editor import
of what is new, the exports, the two reports — each with its own dry run
where the command has one, and each carrying the sentence that says what it
does, because `reclassify` and `align` mean nothing from the name and the wrong
guess costs an evening.

Last in the header is the **health light** — `✓`, `⚠ 5` or `✕ 1`. It is
`splice doctor`, which checks the machine around the archive rather than the
footage in it: ffmpeg, agy, Apple's FCPXML DTD — which validates both editors'
documents — whether Resolve is installed and whether it is answering, the room
left on the disk, a
sidecar whose footage has gone, an index behind the sidecars, subtitles off
the speech, transcripts not yet mirrored, a cut with a shot that came loose.
All of that already existed and it existed in a terminal, which on an evening
spent in the browser is nowhere: a drive that did not mount was something you
found out by remembering to look. Clicking it lists whatever is not `ok`, and
each one that has a fix offers it — *Align the subtitles →*, *Rebuild the
index →*, *Back up now →* — because "12 line timings sit off the speech" does
not tell anybody it means `align`. **Run the whole check** at the foot is the
full report, the fine things included. It reads 52 ms off this archive and is
asked for on load and after every command, so a fix shows as fixed. The
terminal and the browser draw one list; neither can reassure you about an
archive the other is complaining about.

One runs at a time, and while one that writes is going the browser **refuses
edits and says why** rather than queueing them: `reclassify` inside a sync loads
every sidecar and writes back the ones it changed, so a correction saved mid-run
would be written over, and a hand-corrected transcript is the one thing here
that nothing regenerates. A refused save keeps what you typed and says to try it
again in a moment. Dry runs, the exports and the two reports change nothing, and
so refuse nothing.

To look at what came out — and fix it:

```bash
uv run splice serve          # http://127.0.0.1:8722
```

Browse plans, segments, clips, tags, reel suggestions and your cuts; edit a title,
summary, translation, tags, rating, whether it was seen **or any individual subtitle line**
and it writes straight back to the sidecar and reindexes. The lines matter
because they are what a caption is made of: when a cut says a caption goes by
faster than it can be read, the fix is a shorter translation, and that is the
box to type it in. Correcting one updates the segment's prose to match, unless
the same save edited the prose by hand. Localhost only, since it writes to your
files.

Every page has an address, and so does whatever is open on it: a reload, or a
link kept for later, comes back to the same place. `/segments?q=coconut&seg=…`
is that search with that moment in the pane, `/plans?moment=…&plan=…` is the
side panel open on a plan's moment, `/review?seg=…` is where a review pass had
got to, and `/clips?clip=…` and `/reels?mode=cuts&cut=…` are an open clip and
an open cut. Back and Forward step through what you opened, not through what
you typed: a search, a filter and each moment judged in a review pass replace
the address rather than adding to the history, so Back leaves the page instead
of undoing it one keystroke at a time. Back past a moment with an unsaved
correction in it asks first, the same as clicking another row. The review
speed and order, and the stage filter a plain `/` opens with, stay in the
browser: they are preferences, and a link should not change them.

**↶ and ↷** in the header undo and redo on Plans, Schedule and Segments, and
so do ⌘Z and ⇧⌘Z, or Ctrl+Z and Ctrl+Shift+Z (Ctrl+Y redoes too). Each page has its
own history, so an undo always changes something on the page you are looking
at. What can be undone: a moment added to, taken out of or moved in a plan; a
✓; a stage; a new, duplicated or deleted plan; a plan's title or description;
a drag or Alt+arrow on Schedule; Ready, Finished and Posted; a saved
correction on Segments or in the side panel on Plans, and a Keep or Reject
there; a deleted segment; and the shortlist. The button's tooltip names the
step it will take. In a text box, ⌘Z stays the box's own and undoes a
keystroke at a time. Leaving the box makes the whole edit one step for the
page.

Undo makes the same saves a click does, so a plan another window has changed
is refused and reloaded as usual, and the page then forgets its history for
Plans and Schedule, because that history describes plans that are no longer
there. A deleted plan comes back under its own id, with its date, its ticks
and its place on the schedule. A stage change undone back to Ready returns the
plan to its old place too, not to the end. A deleted segment comes back from the page's copy
of it, not from the trash. Restoring the trash copy would replace the whole
sidecar and lose every correction made to the clip since. The segment goes
back into the plans and the shortlist it was taken out of. A saved correction
is undone field by field. The server answers each save with the patch that
reverses it, which includes the prose a corrected line rewrote. Undo sends
that patch, and the answer to it is the redo. The history lasts until the
page is reloaded, and holds the last 100 steps on each page.

**Plans** is what it opens on, and it is the one page here that is not about
the footage. Everything else describes what was shot; this describes the reels
it might become. A list scrolling downwards, one block each: a title, a
sentence about what it is for, and the moments that might go in it — the same
chips the shortlist is made of, dragged into the order you think they go in,
with the count and the running time underneath so you can see whether it is
reel-length yet.

It goes in front of Segments because the question it answers is the one you
arrive with. Segments answers *what do I have*, which is the question you ask
once you already know what you are looking for.

Each plan has a **stage**, the pill beside its title: *idea*, *gathering*,
*ready* or *shelved*. A stage's badge wears the colour of the state it gives
its ticked moments, except gathering. An idea and a plan being gathered for
both only propose their moments, and with one amber ring on both the page
could not say which plans were being worked on. So an idea is an empty amber
ring, gathering a teal one a quarter filled, and ready a blue ring half
filled. The chips under the plan are a pool of material and
alternate takes, not the reel, so each one has a **✓**: chosen for the edit,
or not. The strip above the plans filters by stage and remembers the choice.
The plans are kept in stage order, furthest along first: ready, gathering,
idea, shelved. In the order they were thought of, the one reel nearly ready
sat under forty-one ideas. A plan whose stage changes moves at once to the end
of its new group, and the page says where it went. That order is saved in the
plan files, not worked out in the browser, so a reload, another tab and the
backup all see it. A new plan goes last among the ideas. The ready plans are
in the order they go out on **Schedule**, posted reels included, and a drag
there moves them here too. So a plan made ready lands where the Schedule puts
it, after every reel already placed.

Renumbering writes only the plans' `order`. Their revisions do not move, so a
tab with other plans open is not refused at its next keystroke for a change
nobody made to them, and no trash copy is kept of an order, which the stages
decide. An archive from before this is put in order by its first save.

**Duplicate**, beside **Delete plan**, makes a new idea from a plan: the same
title with *(copy)* on the end, the same description, strand, target and
moments, and the title box ready to rename it. It copies the words and the
pool, not the decisions. It starts as an idea with no take ticked, because a
take ticked in two plans is flagged as a contradiction. It is never finished
or posted, because it did not go out. It has no week or slot, because the
outline never placed it.

Plans have no numbers and no dates. Planning and scheduling are separate, and
the order reels go out in belongs to **Schedule**. The September outline came
with a week and a slot on every idea. That turned the page of ideas into a
timetable, and nothing on it could be moved without rewriting the ideas.

**Schedule** is the page next to Plans, and every plan set to Ready appears
there as a tile. A tile shows the takes chosen for the reel, its strand, how
long the chosen takes run against its target, and how far it has got:
**Ready**, **Finished** or **Posted**, one step at a time. Ready has its takes
chosen. Finished has been cut and is waiting to go out. Without that middle
step, the reel due on Friday and the one nobody had opened Final Cut for
looked the same. The count above the board says how many are at each.
Drag tiles into the order they go out in; Alt+↑ and Alt+↓ do the same from the
keyboard. The numbers down the left belong to the page, not the tiles, and
stay put while a tile moves past them: *third* is a place in the running
order, not a property of whichever reel is in it. A ready plan's card on Plans
links to its place, *#3 on the schedule →*.

A posted tile is dimmed and keeps its number: it cannot be dragged, and the
others move around it, because episode 2 went out as episode 2. A finished
tile still moves, since a cut can go out later than planned. Posting changes
the moments; finishing does not, because nothing has gone out and the cut says
for itself which shots it used. A posted reel's chosen moments become
**published** and its unchosen takes are free again; that is how the page
knows which moments were actually used, so a posted reel of thirteen takes
that used four marks four. Un-posting leaves a reel finished. A finished or
posted plan cannot leave Ready. Set it back to Ready on Schedule first, and
the stage menu on Plans says so rather than refusing silently. Its card on
Plans says *· finished* or *· posted* beside the stage. A plan leaving Ready
leaves the schedule. A plan that becomes ready joins after every
reel already placed; among plans that have never been placed, the outline's
week and slot decide the order.

Clicking a moment in a plan opens it in a **side panel** instead of taking you
to Segments. The panel shows the clip playing with its captions, the title
(saved when you leave the box), Keep / Reject, the ✓ for this plan, the
summary, what is said (click a line to play from there), the tags, the other
plans and cuts that hold it, and a few related moments with **+ Add**. `[` and
`]` step through the plan's moments in order, `t` ticks the one you are on,
and Escape closes it. Fixing captions is still Segments' job, and **Open in
Segments →** at the foot goes there. So does **↗** in the panel's header,
beside the close button. It is a link, so ⌘-click opens the moment in a tab
of its own and leaves the plan open here.

**+ Add to plan ▾** in the panel's foot is the same menu as on Segments: every
plan, ticked where it already holds the moment, and choosing one puts it in or
takes it out. *Also in* and the count on the button change where they stand,
and the clip keeps playing. **New plan with this moment** adds the new card to
the list behind the panel, with its title box ready to type in.

### States

Every moment has one state, worked out from the plans, the cuts, and its own
rating and status. Nothing stores the state: all of those are already files,
and a stored copy would only be a second answer that could disagree. The
plans, the Segments rows and filter, the editor and `splice status` all draw
the same seven:

| | State | What puts a moment there |
|---|---|---|
| ✓ filled disc, purple | **Published** | chosen for a reel posted on Schedule, or a shot in a published cut |
| ● filled disc, green | **Used** | a shot in a cut |
| ◐ half ring, blue | **Planned** | chosen for a plan that is ready |
| ◌ dashed ring, amber | **Proposed** | in an idea or gathering plan, or in a ready plan without its ✓ |
| ⊘ struck ring, red | **Rejected** | rated reject, and no plan or cut claims it |
| ⊙ ring and dot, slate | **Seen** | reviewed or kept, and no plan or cut claims it |
| ○ empty ring, grey | **Unseen** | none of the above |

When two things claim a moment, the one furthest along wins. A shelved plan
claims nothing. A posted reel lets go of the takes it did not use, so they are
free for the next idea. ★ marks a moment rated keep. A red ring round the
badge flags a contradiction: a rejected moment still in a plan, or one take
chosen for two plans. Its tooltip names the plans. The icon carries the state
on its own, so the badges still read without the colours.

The hand-set `planned`, `used` and `published` statuses are no longer offered
in the editor, because the state replaced them. An old sidecar that says one
keeps it, and it counts as seen.

**Segments is the whole screen**: the search results down the left, and the
moment you picked open beside them — the clip playing, the title, the tags,
the rating and the captions — rather than a list with an editor over the top
of it. Fixing twenty moments used to be twenty opens and twenty closes. The
prose — which the search reads alongside the lines themselves — and whether it
has been seen are folded away under *Summary, prose and whether it was seen*, because a corrected line
already rewrites the prose and the only segments that need it typed by hand are
the ones where the model wrote it differently from its own lines.

A save sends only the fields you actually retyped. That page can sit open all
evening while a review pass lands verdicts on the same sidecar, and stamping
every field on the way out would put an hour-old copy of them back.

**+ Shortlist**, on an open moment, puts it in a strip across the top of the
page — a thumbnail, a title, a cross to take it off again, and clicking one
opens it. It is the thought you have at the twelfth moment and have lost by
the fifteenth: *those four might go together*. There is no running order in it
and nothing to name; it is a scratch pad, and **Move the shortlist into a new
plan** is how it stops being one — which empties the strip, because that is
what makes room for the next thought.

**+ Add to plan**, beside it, skips the strip and goes straight into a plan by
name, or starts one. The button says how many plans a moment is already in,
because that is the question you ask before opening the menu, not after.
Each segment row and its open editor also show the titles of the plans holding
it as clickable labels, each with its stage and a ✓ if the moment is chosen for that
plan's edit. A moment in several plans lists each one; an unassigned moment
says **Not in a plan**. Labels and badges repaint in place as plans change, so
the player does not restart. On the open moment — the Segments pane, and the
**Also in** list in the side panel on Plans — hovering a plan's label shows a
**×** that takes the moment out of that plan. The rows in the list don't have
one, because a column of crosses is a column of targets for a stray click.

The September trip has a starting outline in `plans/schedule.json`: 42 video
ideas with titles, descriptions, strands, target lengths and source segments.
It was written against the 477 segments processed then, across 139 clips, and
used 470 of them. When three of those clips were re-timed, its ids were
re-pointed at the new segments. Eight of its moments landed in a segment a
neighbouring moment already had, and were merged into it, so it now names
462. These are
pools of material and alternate takes, not finished cuts; the time beneath
each plan is source footage, distinct from its target length. The outline
also suggests a week and slot for each idea. Those only decide where a plan
first lands on Schedule, and nothing on Plans shows them. The Plans page
shows live coverage, including anything still unassigned. No additional
filming is required by this first outline.

Plans are files, `index/plans/<id>.plan.json`, one per plan. They used to live
in the browser's `localStorage`, which was fine while a plan was only an idea.
Once a reel can be posted, its plan is the only record of what went out, and
clearing site data must not be how that record is lost. The first browser to
open this page moves its plans into the archive, once. If that browser never
loaded the September outline, it starts from `plans/schedule.json`, and only
when the library contains matching segments. One that loaded the outline and
then deleted every plan keeps its deletions. The server refuses a second
import, so another tab still holding the old copy cannot bring back a plan
deleted since. The old `localStorage` entry is left alone as a spare copy, and
nothing writes back to `plans/schedule.json`.

A plan saves as you type it, 700 ms after the last key. Stages, ticks and
moved moments save at once. Each save carries a revision. A save made from a
copy another tab has already changed is refused, and the page reloads the plan
and says which change to make again. Deleting a plan puts a copy in
`index/.trash/`. Typing saves often, so the trash keeps one copy of a plan per
ten minutes rather than one per save; otherwise one description would push
corrected transcripts out of the 300 the trash holds.

The running order is one more file, `index/plans/schedule.json`, holding the
plan ids in order and a revision. A drag writes that file and nothing else, so
reordering never changes a plan's revision under a Plans tab left open. A
reorder from a tab holding an older schedule is refused and reloaded, and so
is one that no longer names exactly the ready plans.

So there are three things here and they cost different amounts. A shortlist is
a line of `localStorage`. A plan is a small file with a name, a reason, a
stage and a pool of takes. It has a revision and a trash copy, and `backup`
mirrors it into the archive's `plans/`. A cut is a running order with trims, and it
is what gets exported. When a plan's chosen moments turn out to be a real
running order, `reel new` is what makes it one.

A plan's moments remember their clip and range as well as their id. Segment ids
are derived from start times, so re-running `analyze` renumbers them. A moment
whose id has gone is found again by the segment now covering its old midpoint,
and the card says *1 found again after re-analysis*. That is the same rule
`analyze` uses to carry a rating over. Only a moment whose footage has gone
says *no longer in the index*, with the title it remembered still on screen.

Every moment carries a poster frame, taken out of its proxy a beat in — the
first frame of a segment is often the tail of the previous shot. They are built
on demand as you scroll, ~130 ms and 8 KB each, or all at once with `splice
thumbs`.

It plays the 480p proxies analysis already built — enough to find a trim point,
and the only thing here small enough to scrub over HTTP. **Reels ▸ My cuts**
plays a whole cut back to back, with the line being spoken underneath, and each
shot's in and out points can be set from wherever the playhead is. That is not
an attempt to be an editor; it is what stops every trim being a round trip
through one.

**Said out loud** is the other half of search: a command of its own, and, in
the browser, part of the one search box. A moment here averages 28 seconds and
one of them runs 108, so a hit on a moment tells you "somewhere in the next two
minutes he says this". A hit on a *line* tells you when:

```
$ uv run splice lines "7 rupees"
grandpa_full_life_story_interview   0:05:35  At that time, for 7 rupees, I went to Bombay.
```

That is five and a half minutes into a twenty-minute interview, and it is the
question you actually ask while assembling a cut. The Konkani is indexed
alongside the English, so a phrase you remember the sound of finds it too, and
hits come back best-match-first because "rupees" is said nine times.

A line is also the finest thing you can put in a cut:

```bash
uv run splice reel add money --line "7 rupees"
uv run splice reel new "Every rupee" --said rupees   # a supercut
```

`--said` makes one shot per time anybody says it, each cut to its own
sentence, in the order the footage was taken. "Every time he mentions money,
back to back" is a reel format, and that is the whole of making one — five
shots, 34 seconds, from a phrase.

which adds a shot trimmed to that sentence, snapped to where the sentence
actually begins and ends, with the sentence itself as the shot's note — so it
names itself in the running order and in the editor's browser. An ambiguous
phrase is refused with the list rather than resolved to the first hit: a cut is
a decision, and quietly picking one of nine "rupees" is not one you made.

In the browser this is not a second list any more. Segments searches the
transcript alongside the prose and the tags — so a phrase that is only ever
said, and never written into a summary, finds its moment — and a moment that
matched something said quotes those sentences under it, timecoded: the first
three, then a count of the rest. Clicking one opens the moment with the player
already on that sentence rather than ninety seconds before it. The tab that
used to do this was a second place to type the same query; the only thing it
knew that the moment list did not was *when*, and that is on the row now.
`splice search` prints the same quotes, two of them, under each match.

Opening a clip gives you the whole thing to **watch and read at once**: the
footage at the top, every moment below it with what was said, and any line
clickable to jump there. The two life-story interviews are twenty minutes each,
and finding the good bits of one used to mean scrubbing it in an editor with no
idea what was being said where. **Whole clip ↗**, in the header of an open
moment, is the way there from Segments: it opens the clip on Clips with the
player on that moment's first frame and its transcript scrolled to it, so what
came just before is a scroll up rather than a search. It is a link to
`/clips?clip=…`, so ⌘-click opens it in a tab of its own, and one Back returns
to the moment with anything half-typed in it still there.

The transcript is also **selectable**. Click a line, shift-click a later one,
and the run between them becomes a shot — padded, snapped to where the first
sentence starts and the last one ends, with the text as its note. Those
interviews have moments 113 seconds long, which is a paragraph rather than a
shot, and 215 lines you can now read instead of scrub. That is the shape this
half of the archive always wanted: you find the good bits of a twenty-minute
interview by reading it.

**Review** is the tab that does the sorting: one moment at a time with the
footage playing, its transcript following along, and a keyboard — <kbd>J</kbd> /
<kbd>K</kbd> to move, <kbd>F</kbd> to keep, <kbd>X</kbd> to reject,
<kbd>S</kbd> for seen, <kbd>,</kbd> and <kbd>.</kbd> to slow down and speed up,
and <kbd>E</kbd> to fix what the moment says. <kbd>E</kbd> opens the same
editor Segments has, in place of the transcript and with the footage still
playing beside it rather than behind a dialog; <kbd>Esc</kbd> puts the pass
back. A verdict moves on
by itself, and a pass resumes at the first moment nothing has been said about —
coming back to a half-done pass at segment 1 and pressing <kbd>J</kbd> forty
times is the fastest way to stop doing the pass. 192 segments is more
than anyone will click through a dialog at a time, and nothing becomes a reel
until the keeps have been separated from the rest. It reviews whatever the
facets on the left select, so "everything untagged" or "all the favourites" is a
pass rather than a different screen.

Two things decide whether a pass gets finished, and both are on that screen.
It says how much **footage** is left rather than how many moments — 192 to go
tells you nothing, `1:31:10 left at 1×` tells you it is an evening. And it
plays at up to 2×, which turns that evening into forty-five minutes; the speed
is remembered, because how fast you can follow Konkani is a fact about you
rather than about this sitting.

It also starts at the end worth starting from. **Most promising first** puts
the moments the model tagged `quality:reel_candidate` at the top, then anything
it gave a `tone:`, then the rest, in the order the footage was taken. Those
tags are the only opinion in the archive about which moments are good, and 49
of these 192 carry the first one — so a pass that ignores them opens with three
silent b-roll shots of a cupboard, and one that uses them has you looking at
usable material inside a minute. It is a heuristic and it is not a verdict:
the point is to reach a shortlist before you lose interest, not to be told
what to keep. **The order it was shot** and **By clip name** are the other two;
alphabetical by filename, which is what this used to do, is the only one of the
three that corresponds to nothing.

The individual steps are still there when you want them:

```bash
uv run splice scan / proxy / analyze -j 3 / rename / index / export all
uv run splice thumbs                      # poster frames, otherwise built lazily
uv run splice align --dry-run             # subtitle timings, onto the speech
uv run splice status                      # where the footage has got to
uv run splice status --editor resolve     # ... as far as Resolve is concerned
uv run splice doctor                      # whether the machine around it still works
```

All of those but `scan` and `proxy` are in the browser's **Run ▾** menu as
well — those two are what `sync` opens with and are no use on their own — so
the terminal is for the runs that take an argument.

Finding things:

```bash
uv run splice search "things that used to cost little"   # prose, tags, speech
uv run splice search --order promise      # what is worth watching first
uv run splice lines "7 rupees"            # every time somebody says it
uv run splice search --tag person:grandma --tag tone:funny
uv run splice search --tag theme:money --unused --rating favorite
uv run splice related "7 train"          # moments like this one, anywhere
uv run splice reels                       # suggested reels across clips
uv run splice tags                        # the vocabulary and what it covers
```

Making one:

```bash
uv run splice reel new "1961" --from era:1960s   # seed from a suggestion
uv run splice reel show 1961
uv run splice reel export 1961                   # -> a Final Cut timeline
uv run splice reel export 1961 -e resolve        # -> one Resolve can open
uv run splice captions ~/Downloads/Ep1.fcpxmld   # <- subtitles for the cut you made
```

## Adding clips later

Drop, sync, import. There is nothing to remember and no per-clip bookkeeping.

Final Cut has no way to update a clip from XML — importing the same clip twice
gives you two clips — so `sync` has to know what it already holds. It finds that
out by reading the library's own catalogue
(`Grandparents.fcpbundle/<Event>/CurrentVersion.fcpevent`), rather than by
trusting its own record of what it handed over. That distinction matters: an
export that was written and never imported used to count as delivered, which
quietly took eight clips out of the pipeline for good. Now `incoming.fcpxml`
holds everything Final Cut cannot be seen to have, so failing to import one is
harmless — the next sync offers it again.

Resolve's equivalent is its media pool, read through the scripting API, and it
differs in two ways that are worth knowing before they surprise you. It will
not duplicate a clip it already has, so a re-offer refreshes the timeline
rather than leaving a second copy to delete. And it answers only while Resolve
is running with a project open, so most syncs cannot see it at all — which
changes nothing, because an editor that cannot be read leaves import state
exactly as the sidecars have it.

Set `SPLICE_FCP_LIBRARY` if the Final Cut library lives somewhere other than
the media root, or if you keep more than one bundle there. If the editor
cannot be read at all, `sync` says so and leaves import state alone; `splice
imported` then sets it by hand:

```bash
uv run splice imported                      # everything analysed so far
uv run splice imported --only lunch_conv    # or just some
uv run splice imported --reset              # deleted the library, start over
uv run splice imported --editor resolve     # the same, for the media pool
```

**A clip you already imported and then changed** — by hand or by
`analyze --force` — goes back into
`incoming.fcpxml`, because what the editor holds for it is stale. Final Cut
cannot replace it, so it arrives as a second copy named `<clip> - a1` — `sync`
names these before you import and lists the older copies to delete afterwards.
Resolve keeps the clip it has and the fresh stringout carries the markers the
clip now deserves, so there is nothing to delete and `sync` does not pretend
there is.

"Changed" means what an export would actually carry: a segment's **boundaries,
title, tags or rating**. Correcting a translation or a summary does not re-offer
the clip, because none of that reaches the editor — an evening spent fixing
Konkani in the browser costs you no re-imports.

Adding a tag to the project's `taxonomy.yaml` deliberately does **not** invalidate past
analyses — that used to mark the whole archive stale and trigger hours of
re-analysis. `sync` runs `reclassify` for you, so a promoted or aliased tag
reaches the editor on the next run. `analyze --force` is for when you really do
want a re-run.

## Duplicates, renames and replacements

`raw/` is a drop bin, and what you drop is not always new. `scan` compares
content, not names:

- **The same footage again**, under any name, while the original is still there
  — reported as a duplicate and otherwise ignored. Nothing is moved or deleted.
- **A clip renamed in Finder** rather than by `splice rename` — the rename is
  adopted: sidecar, proxy, chunks and segment ids follow it, and the analysis is
  kept. Every name a clip has had is remembered, because Final Cut may still
  hold it under one of them.
- **A file replaced in place** — noticed even when the new cut is the same
  length, and queued for re-analysis. Its segments are kept until the new
  analysis lands, since a hand-corrected transcript is not regenerable.

## Naming

A clip the camera named tells you nothing in Final Cut's browser next to
`grandpa_young_hero_photo_1961`. Once analysed there is enough to fix that:

```bash
uv run splice rename                                   # propose, dry run
uv run splice rename --apply                           # take the proposals
uv run splice rename --apply -s IMG_8768=grandpa_guessing_echo_show
```

It moves the original and everything pointing at it — sidecar, proxy, chunks,
segment ids, **the shots in any cut that used it and the moments in any
plan that holds it** — and drops stale `.srt`
files carrying the old name. A rename is the one thing that can break a reel:
its trims stay right while the clip they name stops existing. Clips you
named yourself are left alone unless you pass `--all`; a bare UUID, which is
what a clip saved out of a chat arrives as, counts as a camera's name. The camera's own number
survives in `asset.original_name`, and every name the clip has had is kept in
`asset.previous_names`, which is how a clip renamed after you imported it is
still matched against what Final Cut holds.

A clip that cannot be named — its analysis failed, or the name is already taken
— is skipped and reported. It used to raise, which inside `sync` left every
other clip camera-numbered too.

Proposals come from the longest segment, not the first: a short opening
question ("Do you know what this is?") makes a poor filename. `sync` reindexes
and re-exports for you; after a bare `rename --apply`, run `splice index` and
`splice export all`.

## Related moments and reel suggestions

`splice related <title fragment>` finds moments resembling one you name;
`splice reels` proposes reels assembled from several source files.

Neither involves a model. Both start from the tags, but **not from raw tag
overlap** — that does not work here. At 6.2 tags a segment, 87% of all segment pairs share
at least one, and more than half of those share nothing rarer than
`person:nikhil`, which sits on 139 of 192 segments. So raw overlap ends up
ranking by how heavily tagged a moment is: its strongest pair is simply the two
most-tagged moments in the archive.

Weighting each tag by rarity (`person:nikhil` 0.32, `object:router` 3.18,
`object:coconut_scraper` 5.26) puts the crow story next to the twelfth-day
feast instead.

### What tags cannot say

Rarity fixes the ranking. It does not fix the vocabulary, and for `related`
that turned out to be the bigger problem. The taxonomy is 45 closed terms and
the thing a moment is *about* is usually not one of them. Standing on
"Grandma feeds crows and pigeons chapati", the obvious neighbour is "Serving
their favourite food to a crow" — and on tags they share `person:grandma` and
`person:nikhil` and nothing else, because there is no `object:crow` and there
never will be. What came back instead were five other things filmed on the
balcony, ranked by `location:balcony`. The room, not the subject.

The word "crow" is said eleven times across those two clips, and nothing was
reading it. So `related` scores a pair twice — once on rarity-weighted tags,
once on rarity-weighted **words** from the title, the summary and the English
speech — and takes whichever is stronger. Not the average: averaging halves a
real match every time the other signal is silent, which is most of the time,
and it left the crow below the balcony again. Two moments are related if they
share a subject **or** if they share the words; they need not do both.

Three things make the word score work, and it is worth nothing without them:

- **A hand-written stoplist.** With 192 short documents, rarity cannot tell
  filler from subject. A document-frequency cutoff was tried first and was
  worse: "each other" sits below any threshold that still keeps "chapati". The
  list also drops `grandma`, `grandpa` and `nikhil` — they are in almost every
  frame — and the voice every summary is written in, `asks`, `explains`,
  `recalls`. Left in, those cite "ask" and "explain" as the reason for a match
  140 times across the archive, which says only that someone was interviewed.
- **Folding a plural onto its singular, but only where the archive already
  uses the singular.** One clip says "crows" and the other says "crow", and on
  surface forms they matched on neither. Stripping a trailing `s` blindly
  turns "gas" into "ga"; asking the vocabulary cannot invent a word that is
  not already in the footage.
- **Weighting the fields.** A title is nine words someone chose and a
  transcript is four hundred that nobody did, so title counts 3, summary 2,
  speech 1. Unweighted, the vector is whatever the conversation rambled about.

The two scores are cosines over different vocabularies and do not land on the
same scale: across all 17,287 cross-clip pairs the tag score's 90th percentile
is 0.294 and the word score's is 0.060, so a word score is multiplied by 4.9 —
call it 5 — before the comparison. That is a unit conversion, not a claim that
a word is worth five tags. Below about 3 the balcony wins again; above about 8
two moments that merely both mention chapati outrank a real shared subject.
167 of those pairs then score above 1 and are reported as 1.00; every one of
them is the same moment twice, a story retold or a shot filmed again.

167 of the 192 segments come out with a different nearest neighbour than they
had on tags alone, which is the size of the thing that was missing.

Building the word vectors for the whole archive takes 5 ms, and it is done on
first use — `reels` never asks for it.

`reels` is unchanged and still runs on tags alone. A suggestion is seeded by a
tag, and a tag is what a person can accept or reject; a cluster gathered by a
word is a cluster nobody can name.

Suggestions are seeded by one tag and by **pairs** of subject tags. A pair is
often the sharper reel — "1960s · Marriage", "Both of them · Television",
"Daily life · Food" are subjects, where either tag alone is a heading. Only
`person:`, `theme:`, `object:`, `location:` and `era:` pair up: a pair drawn
from `quality:` or `tone:` gives you "quiet b-roll of daily life", which
describes how the footage looks and is a mood board rather than a reel with a
point. Two people is a cast, not a subject, so `person:` does not pair with
itself.

A suggested reel needs three things: material from **at least three different
clips** (one clip's worth is not a discovery), **coherence measured with the
seed tag removed** (otherwise every cluster looks coherent by construction),
and for `object:` tags, that someone actually **talks about** the thing (every
seed has to pass, for a pair).
That last test is what separates `object:router` from `object:newspaper` —
they scored 0.26 and 0.27 on coherence, but the router is the subject of its
segments and the newspaper is just lying on the table. It is not applied to
`tone:` or `theme:` tags, which have no reason to be named out loud.

Suggestions know what you have already decided. A moment you **rejected** in
Review is not proposed again — a suggestion that keeps offering it is asking
you to make the same decision twice. A **favourite** is taken first, then a
`quality:reel_candidate`, then rarity; a moment already in a cut goes last,
since a suggestion made only of what you have used tells you nothing. A single
moment longer than a minute is left out, because that is a moment, not a reel.

They are proposed in the order the footage was taken. Alphabetical by filename
is not an order anything happened in.

Everything is computed on demand from `library.sqlite`. Nothing is cached, so
nothing goes stale.

## Draft scripts

**Reels ▸ Draft scripts** is the default Reels view. **Generate drafts** finds
up to three distinct stories, aiming for 30–90 seconds each. Leave the brief
and selection empty to explore the library, give it an angle, or use **Choose
source moments** to constrain its material. **Blank draft** starts a manual
script. Generating again makes new candidates and preserves existing drafts.

The local server runs `agy` with your existing sign-in. It first reads the
summaries and proposes stories, then reads the complete timestamped dialogue of
the moments it picked plus neighbouring context. This archive's 192 summaries
are 72 KB and go in one prompt; the limit is not the model's context but
`ARG_MAX`, because the prompt reaches `agy` as a single argument. It never
reanalyses the video. `SPLICE_DRAFT_MODEL` overrides the drafting model; by default it uses
`SPLICE_AGY_MODEL`. No direct model API key is needed. Drafting sends the existing
text to Gemini, so it is a model request even though the browser and files are local.

The model selects supplied segment and line references. Python supplies the
actual quotes and times, rejects unknown references, and excludes rejected
moments. A single clip is enough, and a long answer can yield a short excerpt.
Favourites and already-used material are labelled for discovery. Fewer than
three candidates is fine when the material supports fewer stories. A library
that outgrows one prompt is read in batches instead, and the candidates each
batch returns are then reduced to three; a reduction that declines to choose
between them keeps three rather than reporting an empty archive. When
discovery really does find nothing, the message repeats the reason it gave.
Generation reports its stage, retries transient model failures, and offers
**Retry generation** on failure. A running job survives a browser reload, but a server restart
interrupts it; saved drafts survive both. A selected story whose full dialogue
exceeds the prompt budget asks for fewer moments rather than silently dropping context.

Each draft shows its angle, ordered dialogue beats, the reason for each beat,
and optional alternative excerpts. English appears first, with romanised
Konkani expandable. **Watch context** plays the surrounding and neighbouring
moments; click a transcript line to play there, or shift-click another to make
that run the excerpt. **Correct transcript** opens the existing source editor.
Editorial notes are separate and never rewrite somebody's words.

Drag beats, use the up/down controls, remove or add excerpts, choose an
alternative, or trim with numeric in/out fields, **Mark in/out**, or the player
keys: Space, arrows, I/O, and brackets. **Play draft** runs the chosen sequence.
Proxy loading can leave a beat at joins; judge the final rhythm in the editor.

Use **Save draft** after editing. Unsaved changes stay in this browser session
while switching drafts, and leaving the page warns before discarding them.
Concurrent saves use a revision token: an older browser cannot overwrite a newer
edit. **Reload saved draft** lets you pick up source corrections or resolve a
conflict. The saved dialogue snapshot is retained so later transcript changes
can be flagged. Missing or replaced footage stays visible and blocks an
incomplete export; source renames resolve by fingerprint or filename history.

**Export to Final Cut** and **Export to Resolve** each save pending edits,
create a new saved cut, and write its FCPXML, SRT, and running-order text using
the existing exporter. Each press creates a fresh snapshot; editing the draft
later does not alter that cut or any project in either editor. The page then
names the menu to import it with, which is not the same one in both. The
browser does not add music, transitions, or visual polish. Those decisions
remain in the editor.

Drafts live in `index/drafts/<id>.draft.json`, with a schema version, revision,
brief, generation provenance, beats, alternatives, notes, and dialogue snapshots.
They are backed up under the archive's `drafts/` and use the existing trash/restore
workflow. They never enter the cuts index or count as used footage until exported.
The HTTP surface is `GET/POST /api/drafts`, `GET/PATCH/DELETE /api/draft/<id>`,
`POST /api/drafts/generate`, `GET /api/drafts/generation`, and
`POST /api/draft/<id>/export`. Updates, deletes and exports require the current
`revision`; a conflicting request returns HTTP 409.

## Making a reel

A suggestion is a proposal that changes whenever the vocabulary does. A **cut**
is a decision, and it is the only thing here nothing can regenerate.

```bash
uv run splice reel new "1961" --from era:1960s   # from a suggestion
uv run splice reel new "Money" --tag theme:money --rating favorite --order shot
uv run splice reel add 1961 "young hero"         # a segment id or part of a title
uv run splice reel add 1961 --search "old photo" # or everything a query finds
uv run splice reel trim 1961 2 --start 5 --end 40
uv run splice reel move 1961 3 1
uv run splice reel order 1961 --by shot           # or long, short, clip
uv run splice reel duplicate 1961 "1961, shorter" # try one without losing this
uv run splice reel show 1961 --lines
uv run splice reel export 1961                    # -> a Final Cut timeline
uv run splice reel export 1961 --editor resolve   # -> one Resolve can open
uv run splice reel render 1961                    # -> an mp4 you can watch
```

A search is usually the quickest way in: `--tag theme:money --rating favorite`
is a running order you can then argue with, which beats starting from nothing.
`--order shot` puts the moments in the order the footage was taken, which is
what makes a set of them read as an afternoon rather than a list.

`export` writes three files into `export/reels/`:

- **`<slug>.fcpxml`**, or **`<slug>.resolve.fcpxml`** — an FCPXML `<project>`.
  Import it and it arrives as a timeline you can open and keep editing, not as
  loose clips — **with each clip's volume already set** so the edit does not
  open with a fader ride. Neither editor has a limiter on the way in, so those
  volumes are pulled down together by however much the worst one would have
  clipped: quieter than the −16 LUFS a render lands on, and matched, which is
  the part that matters. Final Cut's also has **the spoken lines already on it
  as captions**; Resolve's has a marker at the head of each shot instead, and
  the lines go in as the `.srt` below. Both are validated against Apple's DTD
  before they are written.
- **`<slug>.srt`** — the same captions as a sidecar file, rebased onto the cut.
  The per-segment subtitles are no use once a moment has been trimmed and
  moved; these are.
- **`<slug>.txt`** — the running order, with what is said in each shot. Worth
  reading before you open Final Cut at all.

Captions rather than titles, in Final Cut: they land in its caption editor,
carry a language, and burn in or export as a sidecar on the way out — where
titles would be seventeen text generators to reposition by hand. `--lang both`
puts the romanised Konkani above the English; `--no-captions` leaves them out.
Resolve discards `<caption>` on the way in, so it is not offered one; the
`.srt` is better there anyway, since Resolve imports it as a subtitle track it
will style rather than as a black slab.

The line timings are the model's estimates, not measurements, so they land
within a second rather than on the word — a caption pass is nudging, not
typing. Two estimates can also overlap, and Final Cut refuses captions that sit
on top of each other, so each one is clipped to where the next begins. The
`.srt` is clipped the same way, because a subtitle that overlaps the next one
is a subtitle that flickers wherever it is played.

The UI is the better place for the assembly itself. **Reels ▸ My cuts** gives
you drag-to-reorder, **Duplicate** — which is the move a long cut asks for,
since finding the 100 seconds means throwing eight things away and wanting two
of them back — an Export button, and a player: press **Play the cut** and
the shots run back to back off their proxies, with the line being spoken shown
underneath.

Trimming is play, mark in, mark out, next — four keys, done a hundred times, so
it has four keys: <kbd>Space</kbd> plays, <kbd>I</kbd> and <kbd>O</kbd> set the
in and out points of whatever is on screen to the playhead, <kbd>←</kbd> and
<kbd>→</kbd> move between shots, and <kbd>[</kbd> / <kbd>]</kbd> nudge by half a
second (hold shift to go the other way). The **⇤ in** and **out ⇥** buttons on
each row do the same thing with the mouse.

The moment's dialogue sits under the player and each line plays from itself, so
"start where he says *sixty, seventy years ago*" is a click and an <kbd>I</kbd>.
**Shift-click a second line and the run between them becomes the shot** — which
is how a 113-second answer becomes the twenty seconds that matter, by reading
it rather than by scrubbing it. It shows the whole moment, not only what the
shot currently holds: the line you want is usually just outside the trim, and
those are shown faint.

Getting a moment *into* a cut in the browser is the clip transcript's job, and
only its: shift-click a run of lines and it becomes a shot, cut to where the
first sentence starts and the last one ends. Neither Segments nor Review offers
it any more. Those two are for deciding what is worth keeping and for fixing
what it says, and a pick-a-cut-and-press-add on either of them was a running
order being assembled a moment at a time by somebody who had not decided on one
yet. Everything else goes through `reel new` and `reel add`, which take a
search and give you an order to argue with.

There is a beat at each join while the next proxy loads, so this shows you the
running order and the performances, not the finished rhythm. The editor is
still where you judge that.

Two things a cut has to survive, and does:

- **Re-analysis.** Segment ids are derived from their own start time, so
  changing the prompt renumbers them. A shot is therefore `(clip, in, out)`,
  with the segment kept only as provenance — a moved boundary reattaches by
  midpoint, and a deleted one costs you a title, not a shot.
- **A shot coming loose.** If the clip is gone, or the file was replaced by a
  shorter one, the shot stays in the cut and is *reported* — by `reel show`, by
  `status`, and by `sync`, which is where you would find out, since the things
  that break a cut all happen there. It is left out of the exported timeline —
  a document referring to a clip Final Cut cannot resolve is refused outright,
  taking the good shots with it — but it is never silently dropped from the
  running order.

`reel show` and `reel export` also read the cut back to you — the things you
would notice on a second viewing and not on the first:

```
· this cut runs 5:49; a reel that gets watched to the end is usually under 100s
· shot 2 opens 0.8s after this one started talking; `reel snap` moves it back to the word
· shot 3 cuts 0.9s before they stop talking
· shot 3 has 1 caption going by faster than 26 characters a second
· shot 8 runs straight on from shot 7 in the same clip; one shot would cut the same
```

The last two come from the audio, and only where a snap would actually fix
them. Coming in over the middle of a long answer is an edit somebody made;
clipping the first syllable of one is the model's boundary showing through,
and pointing at a button that would not move it is worse than saying nothing.

None of it is a rule and nothing is blocked by it. A cut that breaks all of
them can be the right cut; these are just the observations that are cheap to
make and easy to miss.

### Snapping a cut onto speech

```bash
uv run splice reel snap 1961 --dry-run    # what would move
uv run splice reel snap 1961              # move it
```

Every shot in a cut starts life on a segment boundary, and those are the
model's estimates — round numbers, good to about a second. Opening half a word
in is the defect you notice first and cannot name, and it is on every shot you
have not hand-trimmed.

The audio knows where it should be. `silencedetect` over a proxy takes about a
twentieth of a second and comes back with every pause; the gaps between the
pauses are the utterances. `snap` moves each in-point to the start of the one
it lands in or near, and each out-point to where that sentence actually
finishes — or back off the room tone, when it was sitting in a pause. Nothing
moves further than 1.2 s, which is the model's own error bar: past that it
would be snapping to the wrong pause. A b-roll shot of a cupboard has no
utterance near it and is left exactly where it is.

On the three shots of `1961` it moves two, by 1.05 s and 0.30 s. It is not a
transcript aligner and does not pretend to be one — it cannot tell you where
the third word begins, only where somebody started and stopped talking, which
are the two places a trim wants to be. The cut as it was goes to
`index/.trash` first, and **Snap to speech** in the browser does the same
thing.

Trims belong to the reel, not to the segment. Taking four seconds out of the
middle of a moment is an edit, not a correction, and the archive goes on saying
what was actually said and when.

Positions on the timeline accumulate in whole **sequence** frames while each
shot's in-point stays in its own source's — rounding each offset from seconds
independently leaves one-frame gaps, which Final Cut renders as black flashes
between shots. This footage is 30 fps with some 29.97 and one 24 fps clip, so
that is a real case rather than a hypothetical one.

## Watching one back

```bash
uv run splice reel render 1961              # -> export/reels/1961.mp4
uv run splice reel render --all --stale     # everything that has moved since
```

The browser plays a cut shot by shot with a beat at each join, which is enough
to judge a running order and not enough to judge a rhythm. `render` is the same
cut as one file — the shots end to end off the originals, the spoken lines
along as a subtitle track, 1080×1920 in about a fifth of its own running time.
70 seconds of reel took 15 s and came out at 51 MB.

That closes the one loop that used to need an editor open for a question you
ask constantly. It also makes a cut something you can *send* — which for footage
nobody else has seen is the more useful half: the people in it are the only
ones who can tell you whose wedding that was.

**Levels are matched between clips.** Sixty clips shot across several
afternoons at whatever distance the conversation happened to be span about 12
dB, and a cut that jumps in level at every join sounds broken in a way the
pictures never do. Each clip is measured once (EBU R128 integrated loudness,
off its proxy, under a second, cached in `work/loudness.json`) and gets a fixed
gain toward −16 LUFS, with a limiter on the mix so a lift cannot clip. The
three clips in `1961` were 4 dB apart and come out at −15.5 LUFS. `--no-level`
leaves them as they were recorded.

Subtitles come as a *track* by default: they play in QuickTime, VLC, Safari
and on a phone, and can be turned off, which is how you check whether a cut
reads without them.

**`--captions burn` draws them onto the picture**, which is what you want the
moment the file leaves this machine — WhatsApp re-encodes, Photos ignores the
track, and the person who can tell you whose wedding that was gets a video in
a language they will not read with no captions at all. The ffmpeg here was
built without libass and without freetype, so there is no `subtitles` filter
and no `drawtext`; each caption is instead drawn to a small transparent PNG
and composited only while its line is being said. It costs nothing — a 70
second reel renders in the same 12 seconds either way — and it means the
captions look like captions rather than like a .srt: wrapped to the frame, on
a scrim, above the sixth of a vertical video that Instagram covers with its
own buttons. `--lang both` puts the romanised Konkani above the English in a
lighter weight.

```bash
uv run splice reel render 1961 --from proxy       # 270x480, about a second
uv run splice reel render 1961 --aspect 1:1       # 1080 out of the middle
uv run splice reel render 1961 --bitrate 16M      # a master rather than a send
uv run splice reel render money --fit             # small enough to send
uv run splice reel render 1961 --lang both -o ~/Desktop/for-mum.mp4
```

`--fit` is the other half of drawing the captions on. Five minutes at the
bitrate a phone shoots comes to a quarter of a gigabyte, which mail and most
chat apps refuse; `--fit` lowers it only as far as it has to — `money` goes
from 253 MB to 67, and `1961`, already small, is left alone. **Render one to
send** in the browser does both without being asked, because a render meant to
leave the machine that cannot leave it is not one.

**Reels ▸ My cuts** has both as buttons — **Render a video** to watch it back
with the subtitles as a track, **Render one to send** with them drawn on and
the Konkani above the English. Press one,
watch a progress bar, and the finished cut appears in the page with its
captions on it — which is the shortest path from "I think this works" to
knowing. The captions there are generated from the cut as it stands rather
than read out of the file, so they are right after a trim and only wrong after
a re-render, which is the safer way round. One render runs at a time: two
encodes at once is not twice the work, it is both of them at half speed and a
browser that has stopped playing proxies.

A reframe is fitted *inside* the source frame rather than around it, so
`--aspect 1:1` on vertical footage is 1080×1080 taken out of the middle, not a
1920×1920 upscale of the same pixels. The one landscape clip in the archive
gets a blurred copy of itself behind it rather than black bars; `--fill pad`
or `--fill crop` if that is wrong.

Shot lengths are rounded to whole frames of the same sequence timebase the
FCPXML export uses, so the draft and the timeline are the same length and cut
in the same places. A render written to `export/reels/` is recorded on the
cut, so `reel list` and `status` say when the file on disk is older than the
cut it came from — separately from the timeline, because the two go stale
independently.

## Captioning a cut you made yourself

`reel export` puts the spoken lines on a running order Splice decided. This is
the other direction, and it is the one a finished episode actually needs: by
the time a cut is done it has been argued with — b-roll cut in, a line lifted,
eight shots taken off one clip, music laid under it — and it shares no timings
with the reel it started from. The first real episode here is 33.5 s against
the 78.7 s of the reel behind it.

```bash
uv run splice captions ~/Downloads/Ep1.fcpxmld          # -> ~/Downloads/Ep1.srt
uv run splice captions ~/Downloads/Ep1.fcpxmld --titles # -> styled, as a project
uv run splice captions ~/Downloads/Ep1.fcpxmld --by-speaker   # -> one .srt a voice
uv run splice captions ~/Downloads/Ep1.fcpxmld --lang both --dry-run
uv run splice captions ~/Downloads/Ep1.fcpxml --editor resolve
```

**Which editor wrote the file is not asked, because it does not need to be.**
The reader walks both dialects — Resolve's timelines begin at an hour rather
than at zero, and its shots are a `<clip>` wrapping a `<video>` where Final
Cut writes a bare `<asset-clip>`, and both are shapes this already handled.
`--editor` changes only what you are told to do with the `.srt` afterwards,
because the two menus have nothing in common.

It does say so when the file is the wrong *format*. Resolve's export menu puts
Final Cut Pro 7 XML one line above FCPXML and OTIO and DRT beside it, and all
three get picked by mistake; "not an FCPXML document" and "syntax error: line
1, column 0" are both true and both send you looking for a corrupt file rather
than back to the menu, so each is named for what it is.

`--by-speaker` is for the editors that cannot colour a line. An `.srt` is a
timecode and a line of text and has nowhere to put a speaker, so CapCut,
Resolve and the rest can colour a *track* but not a line. One file per voice
makes that the answer: import them as separate caption tracks and style each
once. Timings stay absolute, so the tracks lie on top of each other exactly as
they did in one file, and lines nobody has attributed go to `unattributed`
rather than being dropped or guessed at.

In Final Cut: select the **project** — not the event, which has no timeline in
it — then **File ▸ Export XML**. Run this on the result and import the `.srt`
with **File ▸ Import ▸ Captions**, with the project open. In Resolve:
right-click the timeline in the media pool, **Timelines ▸ Export ▸ FCPXML
1.10**, and bring the `.srt` back with a right-click on the subtitle track.
Or drop either onto **Reels ▸ Caption a cut you made** in the browser, which
shows the running order it read and what each caption will say before it hands
the file over — one .srt a voice, since that is what the editors downstream of
this can actually colour. The styled titles are a command-line thing:
`--titles`, and Final Cut's alone.

Since 10.6.6 what Final Cut writes is a **`.fcpxmld` bundle** — a folder with
`Info.fcpxml` inside it that Finder draws as a single file. Both that and a
plain `.fcpxml` are accepted; in the browser it arrives as a directory entry
rather than a file, which is why dropping one used to do nothing at all.
Resolve writes a plain file and needs none of this.

### Making them look like a reel

Final Cut draws an imported caption as white type on a solid black slab, and
offers almost nothing to change about it. That is the *format*, not a setting:
Apple's own DTD puts `backgroundColor` on a `text-style` and marks it "for
captions", while `font`, `strokeColor`, `shadowColor` and `strokeWidth` belong
to a title. A caption gets a box. The look everyone means by "like a reel" —
heavy type, white, a hard black outline, a soft shadow, sitting clear of
Instagram's furniture — is a **title**.

```bash
uv run splice captions ~/Downloads/Ep1.fcpxmld --titles
```

That writes `Ep1 captions.fcpxml`: **the project Final Cut exported, with the
lines added to it as styled titles**. It is not rebuilt from a model of the cut
— the document is parsed, the titles are inserted into that tree, and it is
printed back out, so the colour, the crops, the volume curves, the keywords and
the music survive because nothing re-interprets them. Import it with **File ▸
Import ▸ XML** and it arrives as a second project called `Ep1 + captions`,
sitting next to the one you have, which is untouched. **Download styled
titles** in the browser does the same thing.

Each title is anchored to the shot it sits over, on a lane above everything
already there, and Apple's DTD is what checks the result — it caught a real
one, since an anchored item has to come *before* the keyword ranges on a clip
and a title appended after them is a document Final Cut refuses outright,
taking the whole import with it.

This is Final Cut's alone, and asking for it with `--editor resolve` is
refused rather than obeyed: the styled title is built on Final Cut's own
`Basic Title` template, which is a file inside the application, and Resolve
resolves that reference to nothing. The export would import as a project full
of empty generators — which looks exactly like it worked. The `.srt` is the
answer there, and a better one, since Resolve styles a subtitle track properly.

To change the look, select the titles in Final Cut and use the Text inspector:
it restyles all of them at once, which beats arguing with a `Style` dataclass
here. Sizes are set for a 1080×1920 frame — 68 pt Helvetica Neue Bold, a −9
outside stroke, and 520 px below centre, which is about a quarter of the way up
and clears the caption and buttons Instagram draws over the bottom sixth.

**Nothing writes to the `.fcpbundle`, and the edit is never round-tripped.**
Importing captions is additive, so the music, the trims and the lanes are not
handed back to Final Cut to re-interpret. Writing into the library was the
other option and is how you corrupt one — it is undocumented CoreData behind
`NSKeyedArchiver`, and Final Cut is usually open.

Reading the library directly was tried first, and it *nearly* works. The clips
and their source ranges are in
`Grandparents.fcpbundle/<Event>/<Project>/CurrentVersion.fcpevent`, and the
reconstructed spine summed to 33.533 s — exactly the `mediaRange` the sequence
records for itself, which is a checksum rather than a coincidence. But the
parent/child table is an unordered many-to-many with no sort column, so the
*running order* is not in there, and an episode is nothing but a running order.
`File ▸ Export XML` states every `offset` outright, along with the gaps, the
lanes and the retimes that the bundle only implies. One menu command beats a
reconstruction that can be quietly wrong.

What comes back from the reader is a `reel.Resolved` — the same thing a cut
resolves to — so the `.srt`, the browser's WebVTT and the running order are the
existing code rather than a second implementation of it. That makes the round
trip a test: export a cut, read it back, and the captions have to be the same
ones, to the frame.

Five things it handles because Final Cut writes all five and Splice never
does — and every one of them was found by running it on a real export rather
than by thinking about it. The hand-built tests all passed while the reader was
producing 74 captions for a 16-caption cut.

Resolve's dialect needed none of them. Its timelines begin at 01:00:00:00
rather than at zero, and a shot is a `<clip>` wrapping a `<video>` — the first
is already handled by reading the sequence's `tcStart` as the origin, and the
second is the first item on the list below. There are tests standing on that
now rather than an assumption.

- **A shot is the `<clip>` wrapper, not the `<video>` inside it.** The wrapper
  carries the trim; the inner element spans the whole asset. Reading the inner
  one gives you the entire clip, every line in it, at an offset that can be
  negative.
- **Detached audio is not a second shot.** Detaching a shot's sound, or
  splitting it to ride a level, leaves the same words on an audio-only
  component anchored to the picture. The picture wins — but an audio-only piece
  with no picture of its own, a J-cut or a voiceover, is the only place those
  words are said and is kept.
- **A picture-only cutaway says nothing.** A `<clip>` holding a `<video>` and
  no audio is not in the mix, so captioning it puts words on screen nobody is
  saying and cuts the sentence underneath in half.
- **An identity `timeMap` is not a retime.** Final Cut writes one for any clip
  that has been near the retime editor, and both of Ep1's are identity.
  Dividing the map's value span by the clip's duration turned one into a 14×
  speed-up and dragged nine lines into a two-second shot. `timept` is a
  mapping — local time to source time — and has to be read as one.
- **A connected clip's offset is in its parent's *source* time**, not the
  timeline's, and a **gap** is a real spine element that pushes what follows it
  along.

On Ep1 the result is 9 shots over 33.533 s and 16 captions — and 33.533 s is
what the Final Cut library independently records as the sequence's own
`mediaRange`, which is the closest thing to an oracle available here.

Footage the archive has never seen — the music, a title card, anything imported
straight into Final Cut — is named and left alone. Every finished cut has some,
so it is the normal case rather than a fault.

## Backup

Four kinds of files hold work worth keeping: **sidecars** with analysed and
hand-corrected dialogue, **draft scripts** with proposed stories and personal
edits, **reels** with chosen running orders and trims, and **plans** with
their stages, the takes chosen for each and the order they go out in.
Regeneration cannot recover those decisions.

They are JSON mirrored into the folder `splice.yaml` names as its `archive:`,
where git can keep the history of transcript corrections, draft revisions,
reordered cuts and what was published. The project's own `splice.yaml`,
`taxonomy.yaml`, prompts and plan seed go under its `project/`, since nothing
else versions them either. Make it a git repository of its own and keep it
**private**: it is everything anybody in the footage said. It belongs nowhere
near the code, which is why it is not in this repository.

```bash
uv run splice backup           # mirror sidecars, drafts, reels, plans and the project into the archive
uv run splice backup --check   # what would change
uv run splice backup --restore # copy them back over index/
```

The archive's `manifest.json` names the media root it was taken from, and a
backup from any other root is refused unless forced: two projects pointed at
one archive would prune each other's transcripts.

You rarely have to. `sync` ends with one, and `serve` mirrors into the archive
90 seconds after you stop typing and again on the way out, so the only thing
left to remember is the commit — and the health light says when that is due. A
burst of review keystrokes is one backup rather than sixty, a refusal is
printed rather than raised, and `--autosave 0` turns it off. The UI still has
the button.

Between them those cover the two ways a sidecar gets made. The autosave only
ever covered one: it starts its clock on an *edit in the browser*, so a sync
run from a terminal made eleven transcripts and mirrored none of them, and
they sat outside git until somebody next opened the page — two days, as it
turned out.

`backup` covers the gap between commits; `index/.trash/` covers the gap between
backups. Anything a write is about to destroy goes there first — a segment you
delete, a transcript you overwrite, a draft, cut or plan you throw away, a live
file a `--restore` displaces:

```bash
uv run splice trash list
uv run splice trash restore 20260905T1147   # puts one back
```

Not every write: a rating or a status is a keystroke in Review, it happens
hundreds of times an hour, and losing one costs a keystroke. What is kept is
prose, tags and whole files — the things that were expensive to make. Restoring
keeps whatever it displaces, so restoring the wrong copy costs nothing either.

## Where state lives

`index/<stem>.seg.json` is the **source of truth** — one per clip, hand-editable,
diffable. `index/reels/<slug>.reel.json` is the same for a cut. `library.sqlite`
is derived and thrown away on every `index` run, so correcting a bad timestamp
means editing the JSON, not fighting a second writable store.

The reels used to be the one exception — rows carried across a rebuild because
nothing regenerated them, which made SQLite a second writable store for exactly
one kind of row. They are files now, and the rebuild re-reads them like
everything else.

`index/plans/<id>.plan.json` is a plan: its stage, whether it was finished and posted, and the
moments in its pool with their clip, range and whether each is chosen.
`index/plans/schedule.json` is the order the ready ones go out in. A segment's *state* is not
stored anywhere. It is worked out from the plans, the cuts and the sidecar
whenever it is asked for, which takes about 5 ms for 477 moments and 42 plans.

The shortlist is the one thing left in the browser's `localStorage`, and
clearing site data loses it. A starting series outline, if a project has one,
is kept beside `splice.yaml` in `plans/schedule.json` and is only read.

Re-running `analyze` overwrites the model's output but **keeps** any `rating` or
`status` you set, re-attached to whichever new segment covers the old midpoint.

Each sidecar carries the clip's `fingerprint` — a hash of the ends of the file
plus its exact size — which is what makes identity survive a rename and makes a
re-drop free to detect. It is recomputed only when the file's size or mtime
moves, so a steady-state sync reads none of the footage.

Delivery state lives there too, and splits in two: `offered_at` is something
this tool did, `imported_at` is something it observed in the editor's own
library. Nothing writes `imported_at` on the strength of having written a file.
There is one record per editor — Final Cut's under `exported`, where it has
always been, and any others under `deliveries` — because a clip in Final Cut's
library is not a clip in Resolve's media pool.

## Tags

The project's `taxonomy.yaml` is the controlled vocabulary, and the people in
`splice.yaml` are in it whether listed there or not; `fcp_label` is what shows up in
Final Cut's keyword sidebar, and in Resolve's marker notes, which is the
nearest thing it has. Tags the model invents are not discarded, they go to
`proposed_tags` and stay out of the FCPXML until you accept them:

```bash
uv run splice tags                          # what each tag covers, and what is quarantined
uv run splice taxonomy promote theme:exams  # accept it
uv run splice taxonomy alias theme:historical tone:historical   # or fold it in
uv run splice reclassify && uv run splice index
```

Promoting is not the only answer. A proposed tag that duplicates one you have —
`theme:historical` next to `tone:historical` — should be *aliased*, which folds
it in at reclassify time. Promoting it is the fragmentation this is meant to
stop.

A segment already in a cut says so, in the list and in Review — that is what
`reel_shots` is indexed by segment id for, so "have I used this?" is part of
the row rather than a request per row. The **State** list on Segments turns
that into the question that stops a moment being forgotten: *unseen*, *seen*
and *rejected* are the moments no plan or cut has claimed.

A state, rating or tag filter stays on when you leave Segments and come back.
The legend on Plans sets one on its way there. So every filter narrowing the
list sits above the results as a chip with a ×. A search that comes back empty
only because of the filters says how many moments match without them, with a
button to show them all.

```bash
uv run splice reel unused                 # favourites no cut has claimed
uv run splice reel unused --rating any -t theme:money
```

A cut also remembers what it last handed over — its title, and each shot's clip,
in point, out point and note — so `reel list` and `status` say when one has
changed since its export. The cut also records which editor its timeline was
last written for, since the two go to different filenames and the timestamp
alone would not say which. A corrected translation does not count: the captions
in the exported timeline are then out of date, but they are sitting in Final
Cut's caption editor where fixing one is easier than importing a second
timeline — and in Resolve they were never on the timeline at all, only in the
`.srt`, which is regenerated whole. That matters because neither editor can
update a project from XML any more than it can a clip: a re-export arrives as a
second timeline, not an update.

`splice tags` is the view worth checking now and then: it shows each tag's reach
(segments and clips) and its **signal** — how much it narrows things down.
`person:nikhil` covers 139 of 192 segments and scores 0.32; `object:router`
covers 8 and scores 3.18. Low-signal tags are near-useless for finding
anything, which is why `related` and `reels` weight by it. `--unused` shows
vocabulary nothing has earned yet.

That quarantine is what stops the sidebar fragmenting into money / finance /
cost / rupees.

## Things worth knowing

- **Proxies and chunking are not optimisations.** Handing `agy` a 132 MB / 7 min
  file took 642 s and came back with one generic paragraph. A 0.9 MB proxy of
  the same clip takes ~60 s and comes back with real per-topic segments. Long
  inputs get sampled sparsely, so `CHUNK_SECONDS` (240) is what buys temporal
  resolution. Drop it to 120 if an interview still reads thin.
- **Building them costs two things, and one of them was waste.** The proxy
  drops to 15 fps *inside* the filter chain and ahead of the scale, rather
  than with an output `-r` behind it: as an output option the rate change
  happens past the scaler, so half the frames were resized and then thrown
  away. A 17-minute 1080p HEVC interview went 186.5 s → 160.9 s, same 480p15
  out, same size to within 0.06%. Chunking is then a **stream copy** and not a
  second encode — the proxy is already at the quality analysis reads — which
  is 5.04 s → 0.10 s a chunk, 25 s → 0.5 s for that interview. That needs a
  keyframe exactly at each cut, which the proxy now forces at multiples of
  `CHUNK_SECONDS`; x264 would place them every 16.7 s and a copy would snap
  backwards to one, moving every timestamp in the chunk. A proxy built before
  this, or cut at a `chunk_seconds` it was not built for, has no such keyframe
  and is encoded the old way rather than silently landing somewhere other than
  where it says.
- **It is not fast.** The 20-minute grandpa interview took 44 minutes wall clock
  at `-j 5` — and that included every one of its five chunks failing its first
  attempt and succeeding on retry. Budget hours, not minutes, for a full
  backfill, and run it in the background.
- **agy fails transiently, in bursts.** Launching several processes in the same
  instant reliably burned an attempt on all of them. `LAUNCH_STAGGER` spaces the
  starts out and `AGY_RETRIES` covers the rest; a failure that survives both is
  recorded in the sidecar and retried on the next run.
- **Proxy encoding uses VideoToolbox.** This footage is 1080p HEVC, where
  software decode dominates: 45 s against 16 s for the same slice, byte-identical
  output. It falls back to software if a file will not decode on the GPU.
- **Subtitle timings are estimated, not measured** — until you measure them.
  The model returns suspiciously round line times: 0.5, 4.0, 10.0, 12.5. Half a
  second out is enough for a caption to appear before the mouth moves.
  `splice align` snaps every line to the nearest utterance the audio actually
  contains, within a second; across this archive that moves 285 of 1458 lines,
  by half a second each. It leaves segment *boundaries* alone on purpose:
  segment ids are derived from their start time, so moving one renumbers it,
  costing every reel that names it its provenance and every moment its poster
  frame. Run it with `--dry-run` first, and the sidecars it changes go to
  `index/.trash` on the way. `sync` runs it for you, right after the step that
  measures where the talking is and before the exports that are written from
  those timings; it changes no boundary, title, tag or rating, so a clip it
  touches is not re-offered to the editor.
- **Timestamps sometimes come back as a clock.** Asked for seconds, Gemini now
  and then answers a whole chunk in minutes and seconds: a line from 57 to 100
  that means 0:57 to 1:00. Merged as seconds, the lines past the end of the
  file are dropped and the next chunk's real minutes are discarded as
  duplicates — `grandpa_nana_drew_the_house_plan` lost its last forty seconds,
  and the seventeen-minute house tour lost two minutes from the middle. A
  chunk whose timestamps past a minute never show :60 to :99, and either run
  past the end of the file or are too many for that to be chance, is converted
  before the merge. A chunk that mixes the two is left alone. The first run with
  the check converted one of five chunks of the house tour.
- **FCPXML links the originals in `raw/`**, never the proxies. Every time is
  computed in whole frames, because Final Cut rejects a file that is not
  frame-aligned. Every export is validated against Apple's own FCPXML DTD
  (shipped inside Final Cut) and the export fails rather than writing a file
  the editor would reject. Resolve's documents are checked against the same
  DTD at 1.10, which is Apple's schema and the newest version both programs
  speak; Blackmagic publishes nothing of its own to check against, so a
  machine with no Final Cut on it writes both editors' exports unchecked —
  which `doctor` says out loud.
- **`analyze` is resumable.** It skips clips that already have segments from the
  current prompt, and records per-chunk failures in the sidecar rather than
  swallowing them, so re-running retries only what broke.
- **`raw/` is a drop bin.** Put anything in it — loose clips, or a whole folder
  copied off a phone — and `splice scan` picks it up; discovery recurses. If the
  same clip lands as both `X.MOV` and `X.mp4`, the original wins over the
  downscale. Non-video files are ignored, so stills can sit alongside.

## Later

`segments.embedding` is reserved so semantic search is an insert rather than a
migration.

Embeddings would earn their place on the queries tags cannot express — "things
that used to cost surprisingly little" spans segments with no tag in common.
"More like this" no longer needs them either: the words in the prose and the
speech cover the case that broke it, because two moments about a crow both say
crow. What words cannot cover is two moments about a crow where one of them
only ever says "bird". That is the query embeddings would be for.

A network graph of the archive is the one thing worth **not** building: at 55%
density it renders as a hairball, and the visually strongest edges are the
least meaningful ones.

Two others, measured and dropped:

- **Holding a caption on screen past the end of its line**, so a fast one has
  time to be read. Standard subtitling practice, and worth nothing here: the
  talking is continuous, the model's line ends already run up against the next
  line's start, and across 126 captions in two reels exactly one had any room
  to grow — by half a second.
- **Finding recurring words across clips**, to suggest supercuts the tags
  cannot express. Conversational speech is function words: the top of that
  list is "don't", "right", "meaning", "yeah". The tags are a curated version
  of the same idea and they already work.

  Half of this came back — see "What tags cannot say". What was wrong was not
  the words, it was ranking them by how often they recur; weighted by rarity,
  against a stoplist, and used to score a pair rather than to seed a cluster,
  they find what the taxonomy has no term for. Seeding a supercut is still not
  something they can do: a cluster gathered by a word is a cluster nobody can
  name, accept or reject.
