"""Captions drawn as pictures, because this ffmpeg cannot draw text.

The ffmpeg on this machine was built without libass and without freetype:
there is no `subtitles` filter and no `drawtext`. A soft subtitle track is
fine for watching a draft back yourself -- QuickTime and Safari show it -- and
useless for the thing a draft is actually for. **Sending a cut to the people in
it strips them.** WhatsApp re-encodes, Photos ignores the track, and the
person who can tell you whose wedding that was gets a silent-looking video in
a language they will not read.

So each caption is rendered here to a small transparent PNG and composited by
ffmpeg with `overlay ... enable='between(t,a,b)'`. Rendering the box rather
than the whole frame keeps the images to a few kilobytes; positioning is left
to the overlay, which is what it is for.

It also means the captions look like captions rather than like a .srt: a
readable weight at a readable size, wrapped to the frame, on a scrim dark
enough to survive a white sari against a window, and sitting above the part of
a vertical video that Instagram covers with its own furniture.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import config

# Tried in order; the first that exists wins. Arial Bold is on every Mac, and
# a caption is one of the few places where the boring choice is the right one.
FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
)

# As a share of the frame height. 1080x1920 gives ~46 px, which is about what
# a phone caption is; smaller reads as a subtitle on a television.
FONT_SCALE = 0.024
LINE_SPACING = 1.28
# The caption box, as shares of the frame.
SIDE_MARGIN = 0.06
PAD_X, PAD_Y = 0.022, 0.012
# Where the box sits, measured from the bottom. Instagram puts its caption and
# buttons over the bottom sixth of a reel; this clears it.
BASELINE = 0.20
SCRIM = (0, 0, 0, 168)
INK = (255, 255, 255, 255)
# The romanised Konkani, when both are asked for.
FAINT = (255, 255, 255, 190)
RADIUS = 0.012


class CaptionError(RuntimeError):
    pass


def font_path() -> str | None:
    return next((f for f in FONTS if Path(f).exists()), None)


def available() -> bool:
    """Whether captions can be drawn at all: a font, and something to draw."""
    if font_path() is None:
        return False
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def cache_dir() -> Path:
    return config.media_root() / "work" / "captions"


@dataclass
class Card:
    """One caption, drawn and ready to composite."""

    path: Path
    start: float
    end: float
    width: int
    height: int


def _wrap(draw, text: str, font, limit: float) -> list[str]:
    """Break a line to fit the frame, on words, greedily.

    `textwrap` alone counts characters, which for a proportional font puts
    "Illinois" and "WWWWWWWW" on the same footing. This measures.
    """
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and draw.textlength(trial, font=font) > limit:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    # A single word wider than the frame is not wrappable on spaces; break it
    # rather than let it run off the edge.
    out: list[str] = []
    for line in lines or [""]:
        while draw.textlength(line, font=font) > limit and len(line) > 1:
            cut = max(1, int(len(line) * limit / max(1.0, draw.textlength(line, font=font))))
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out


def render_card(texts: list[str], width: int, height: int, dest: Path) -> tuple[int, int]:
    """Draw one caption box. Returns its pixel size."""
    from PIL import Image, ImageDraw, ImageFont

    path = font_path()
    if path is None:
        raise CaptionError("no usable font on this machine")

    size = max(14, int(height * FONT_SCALE))
    font = ImageFont.truetype(path, size)
    small = ImageFont.truetype(path, max(12, int(size * 0.82)))
    pad_x, pad_y = int(width * PAD_X), int(height * PAD_Y)
    limit = width * (1 - 2 * SIDE_MARGIN) - 2 * pad_x

    scratch = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    rows: list[tuple[str, object, tuple]] = []
    for i, text in enumerate(texts):
        face = font if i == len(texts) - 1 else small
        colour = INK if i == len(texts) - 1 else FAINT
        for line in _wrap(scratch, text, face, limit):
            rows.append((line, face, colour))

    step = int(size * LINE_SPACING)
    box_w = int(max(
        (scratch.textlength(line, font=face) for line, face, _ in rows), default=1
    )) + 2 * pad_x
    box_h = step * len(rows) + 2 * pad_y

    card = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (0, 0, box_w - 1, box_h - 1), radius=int(height * RADIUS), fill=SCRIM
    )
    y = pad_y
    for line, face, colour in rows:
        w = draw.textlength(line, font=face)
        draw.text(((box_w - w) / 2, y), line, font=face, fill=colour)
        y += step

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.png")
    card.save(tmp)
    tmp.replace(dest)
    return box_w, box_h


def _key(texts: list[str], width: int, height: int) -> str:
    payload = "\x00".join(texts) + f"|{width}x{height}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def cards_for(lines, width: int, height: int, lang: str = "en") -> list[Card]:
    """A card per caption, cached by what it says and how big the frame is.

    `lines` are (start, end, en, kok) in the reel's own time -- already
    rebased, clipped and de-overlapped by whoever assembled the cut.

    The cache is content-addressed and never invalidated: a card is 3 KB and
    says exactly what it says, so a corrected translation leaves an orphan
    rather than a wrong picture. `work/` is derived and safe to delete.
    """
    from .export_reel import caption_texts

    out: list[Card] = []
    for start, end, en, kok in sorted(lines, key=lambda line: (line[0], line[1])):
        texts = caption_texts(en, kok, lang)
        if not texts or end <= start:
            continue

        dest = cache_dir() / f"{_key(texts, width, height)}.png"
        if dest.exists():
            from PIL import Image

            with Image.open(dest) as img:
                size = img.size
        else:
            size = render_card(texts, width, height, dest)
        out.append(Card(path=dest, start=start, end=end, width=size[0], height=size[1]))
    return out
