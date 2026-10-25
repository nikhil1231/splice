"""Which editor the archive is being handed to, and what it can actually read.

splice was written for Final Cut and its vocabulary shows it: keyword ranges,
an event, a `.fcpxmld` bundle. Resolve is the same pipeline aimed at a
different program, and the differences are not cosmetic -- a document written
for one and handed to the other does not import worse, it imports empty:

- **Resolve's FCPXML importer reads timelines, not events.** `ImportTimelineFromFile`
  is the only door in the scripting API, and File > Import > Timeline is the
  only one in the menus. An `<event>` full of `<asset-clip>`s and no
  `<project>` gives Resolve nothing to make a timeline out of. So the material
  export for Resolve is a stringout: every clip laid end to end on one
  timeline, which is the vehicle -- importing it puts the sources in the media
  pool, which is the actual destination.
- **Resolve does not speak FCPXML 1.11.** Its own scripting README (Resolve 20,
  `Developer/Scripting/README.txt`) lists `EXPORT_FCPXML_1_8`, `_1_9` and
  `_1_10` and nothing newer, and records 1.3-1.7 as dropped in 18.1. 1.10 is
  the newest version both programs speak, so that is what Resolve gets.
- **`<keyword>` and `<rating>` are Final Cut's alone.** Resolve has no keyword
  ranges and no favorite/reject on a range of a clip. Markers are what it has,
  and a marker carries a name, a note, a colour and a duration -- so a segment
  arrives as a named range in the Edit Index instead of a blue bar in the
  browser. That is the one real loss in the move, and it is stated here rather
  than discovered at import time.
- **Captions do not survive the trip.** Final Cut takes `<caption>` elements
  off the timeline and puts them in its caption editor. Resolve ignores them,
  so the `.srt` beside the timeline is how the lines get there -- and Resolve
  imports an .srt as a real subtitle track it will style, which is better than
  the black slab Final Cut draws.

Everything that writes a document, reads one back, or asks what the editor
already holds takes an editor name and asks here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

FCP = "fcp"
RESOLVE = "resolve"


@dataclass(frozen=True)
class Editor:
    """One editor, and the shape of what it can be handed."""

    name: str
    label: str
    # The label without the brand's second word, for the places that are a
    # column of aligned text: "in final cut" and "in resolve" line up where
    # "in davinci resolve" pushes the whole column six characters right.
    short: str
    # The newest FCPXML both splice and this editor speak.
    fcpxml_version: str
    # Does it understand a `<keyword>` range on a clip? Only Final Cut does.
    keyword_ranges: bool
    # Does it take `<caption>` off the timeline, or does the .srt do that job?
    clip_captions: bool
    # Does a `<rating>` range mean anything to it?
    ratings: bool
    # Does its importer want a `<project>`, or is a browser full of clips enough?
    needs_timeline: bool
    # Does re-importing a clip it already has leave a second copy?
    #
    # Final Cut does, and that copy count is the proof an import landed. Resolve
    # matches on the file path and reuses the clip it has, so presence in the
    # media pool is all there is to observe -- which is why `reconcile` has two
    # rules rather than one.
    duplicates_on_reimport: bool
    # What the user is told to do with the file. These are menu paths, and they
    # differ enough between the two that a generic sentence would be wrong for
    # both.
    import_menu: str
    captions_menu: str
    export_menu: str

    @property
    def marker_ranges(self) -> bool:
        """Should a segment be a marker with a duration rather than a point?

        Final Cut already has the range as a keyword, and a range marker on top
        of it is clutter on the filmstrip. Resolve has nothing else, so the
        marker has to carry the length.
        """
        return not self.keyword_ranges


FINAL_CUT = Editor(
    name=FCP,
    label="Final Cut Pro",
    short="Final Cut",
    # Apple's own current version, and the DTD splice validates against.
    fcpxml_version="1.11",
    keyword_ranges=True,
    clip_captions=True,
    ratings=True,
    needs_timeline=False,
    duplicates_on_reimport=True,
    import_menu="File > Import > XML",
    captions_menu="File > Import > Captions",
    export_menu="select the project, then File > Export XML",
)

DAVINCI_RESOLVE = Editor(
    name=RESOLVE,
    label="DaVinci Resolve",
    short="Resolve",
    fcpxml_version="1.10",
    keyword_ranges=False,
    clip_captions=False,
    ratings=False,
    needs_timeline=True,
    duplicates_on_reimport=False,
    import_menu="File > Import > Timeline",
    captions_menu="the Subtitle track's right-click > Import Subtitle",
    export_menu="right-click the timeline > Timelines > Export > FCPXML 1.10",
)

ALL = (FINAL_CUT, DAVINCI_RESOLVE)
NAMES = tuple(e.name for e in ALL)

# What a person types, and what they meant. `fcpx` and `davinci` are what the
# two programs are actually called out loud; the rest are what a tired hand
# types at eleven at night.
ALIASES = {
    "fcp": FCP, "fcpx": FCP, "finalcut": FCP, "final-cut": FCP, "final_cut": FCP,
    "fcpxml": FCP, "apple": FCP,
    "resolve": RESOLVE, "davinci": RESOLVE, "davinci-resolve": RESOLVE,
    "davinciresolve": RESOLVE, "dvr": RESOLVE, "blackmagic": RESOLVE,
}


class Unknown(ValueError):
    """A name that is neither editor. Carries the list rather than a guess."""


def get(name: str | Editor | None = None) -> Editor:
    """The editor a name means, or the default when nothing was asked for.

    An unrecognised name raises rather than falling back to Final Cut: writing
    a Final Cut document for somebody who typed `--editor resolv` and telling
    them it worked is the kind of silence this archive has been bitten by.
    """
    if isinstance(name, Editor):
        return name
    if name is None or not str(name).strip():
        return default()
    key = str(name).strip().lower().replace(" ", "")
    resolved = ALIASES.get(key)
    if resolved is None:
        raise Unknown(f"no such editor {name!r}; try one of {', '.join(NAMES)}")
    return FINAL_CUT if resolved == FCP else DAVINCI_RESOLVE


def default() -> Editor:
    """The editor to use when a command was not told one.

    `SPLICE_EDITOR` decides, and Final Cut wins the tie: every sidecar in this
    archive carries a Final Cut delivery record, and changing what a bare
    `splice sync` means would offer all of it to Resolve on the next run.
    """
    configured = os.environ.get("SPLICE_EDITOR", "").strip()
    if not configured:
        return FINAL_CUT
    try:
        return get(configured)
    except Unknown:
        # An unreadable preference is not worth stopping a sync for, and the
        # warning belongs where the run is reported rather than here.
        return FINAL_CUT


def names_for_help() -> str:
    return " | ".join(NAMES)
