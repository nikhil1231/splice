"""A cut -> one video file you can watch.

The browser plays a cut shot by shot off the proxies, with a beat at each join
while the next one loads. That is enough to judge a running order and a
performance, and not enough to judge a rhythm -- so "does this work?" has meant
an import into Final Cut, which is a slow way to ask the question you ask most
often.

This renders the cut instead: the shots concatenated with no gap, the spoken
lines carried along as subtitles, one file. It is not an edit -- no
transitions, no music, no colour, and Final Cut is still where those happen.
It is the answer to *watch this back*, and to *send it to my mother and ask
who that is*, which for footage nobody else has seen is the more valuable of
the two.

Three things worth knowing:

**Shot lengths are rounded to whole frames of the same sequence timebase the
FCPXML export uses**, so the draft and the timeline are the same length and cut
in the same places. A draft that runs two seconds shorter than the thing it is
standing in for is worse than no draft.

**The originals are the default source, not the proxies.** A proxy is 270x480
and built for a model to look at. This is built for a person, and the
originals are already 9:16 -- the shape a reel is posted in -- so there is
nothing to reframe and no reason to watch a thumbnail. `--from proxy` is there
for when speed matters more, and is honest about being a scratch copy.

**Captions come as a subtitle track by default, and can be drawn on.** A
track plays in QuickTime, VLC, Safari and on a phone and can be turned off,
which is what you want while deciding whether a cut reads without them; it
does not survive being sent to anyone, which is what `--captions burn` is for.
This ffmpeg cannot draw text, so a burnt caption is a picture composited over
the frame -- see `captions.py`.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from . import config, export_reel, library
from .fcptime import frames
from .reel import Reel, Resolved, load, resolve

# Fill an aspect mismatch with a blurred copy of the shot rather than black
# bars. Only one clip in this archive is landscape, so this is rare -- but a
# single letterboxed shot in a vertical reel reads as a mistake, and the blur
# reads as a choice.
FILLS = ("blur", "pad", "crop")
SOURCES = ("raw", "proxy")
CAPTIONS = ("soft", "burn", "none")
LANGS = ("en", "kok", "both")
ASPECTS: dict[str, tuple[int, int] | None] = {
    "source": None,
    "9:16": (9, 16),
    "4:5": (4, 5),
    "1:1": (1, 1),
    "16:9": (16, 9),
}

BLUR_SIGMA = 24
# The hardware encoder has no CRF, so the bitrate is derived from the frame
# size: 1080x1920 at 30 fps comes to ~5.6 Mbit, which is about what a phone
# shoots and small enough to send. Raise it with --bitrate for a master.
BITRATE_BITS_PER_PIXEL = 0.09
# Where a burnt-in caption sits, as a share of the frame height measured from
# the bottom. Instagram covers the bottom sixth of a reel with its own caption
# and buttons; this clears it.
CAPTION_BASELINE = 0.20
# A caption is a still: it needs a frame when it appears, not thirty a second.
CARD_FPS = 2
SOFTWARE_CRF = 20
SOFTWARE_PRESET = "veryfast"
# Where the limiter sits when a level match has lifted anything, as a linear
# amplitude: -1 dBFS. See loudness.py for why a lift needs one.
CEILING = "0.891"


# Past this a file stops being something you can hand to someone: mail and
# most chat apps refuse it, and the ones that accept it re-encode. It is not a
# hard limit anywhere in particular, which is why this is a remark and not a
# rule.
SENDABLE_BYTES = 100_000_000


class RenderError(RuntimeError):
    pass


def out_dir() -> Path:
    return config.export_dir() / "reels"


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


@dataclass
class Target:
    """The shape and timebase every shot is conformed to."""

    width: int
    height: int
    fps: str

    @property
    def aspect(self) -> float:
        return self.width / self.height


def target_for(
    resolved: Resolved,
    aspect: str = "source",
    source: str = "raw",
    height: int | None = None,
) -> Target:
    """What to render into.

    The shape follows whichever clip holds the most screen time -- the same
    rule the FCPXML export uses to pick a sequence format, and for the same
    reason: a reel should not become 720p because one 720p shot opens it.
    """
    if aspect not in ASPECTS:
        raise RenderError(f"aspect must be one of {', '.join(ASPECTS)}")
    shape = export_reel.sequence_format(resolved)
    if shape is None:
        raise RenderError("no usable shots to render")

    base_h = shape.asset.height or 1920
    base_w = shape.asset.width or 1080
    if source == "proxy":
        # The proxy is 480 tall whatever the original was; rendering the draft
        # any larger is upscaling a thumbnail.
        base_w = base_w * config.PROXY_HEIGHT / base_h
        base_h = config.PROXY_HEIGHT

    ratio = ASPECTS[aspect]
    if ratio is None:
        out_h, out_w = base_h, base_w
    else:
        # A different shape is fitted *inside* the source frame rather than
        # around it: asking a vertical clip for a square should give you
        # 1080x1080 out of the middle of it, not a 1920x1920 upscale of the
        # same pixels. An explicit --height overrides that, since resizing on
        # purpose is a different request from reframing.
        wide = ratio[0] / ratio[1]
        out_h = min(base_h, base_w / wide)
        out_w = out_h * wide
    if height:
        out_w, out_h = out_w * height / out_h, height
    fps = str(config.PROXY_FPS) if source == "proxy" else shape.asset.fps
    return Target(width=_even(out_w), height=_even(out_h), fps=fps)


def source_path(shot, source: str) -> Path | None:
    """The file a shot is cut from: the camera original, or its proxy."""
    if not shot.sidecar:
        return None
    if source == "proxy":
        path = library.proxy_for(shot.sidecar.asset.stem)
    else:
        path = config.raw_dir() / shot.sidecar.asset.file
    return path if path.exists() else None


@dataclass
class Cut:
    """One shot as it will be rendered: where from, how long, how loud."""

    shot: object            # ResolvedShot
    path: Path
    duration: float         # snapped to whole frames of the target timebase
    gain_db: float = 0.0

    @property
    def start(self) -> float:
        return self.shot.start


def cuts_for(
    resolved: Resolved,
    target: Target,
    source: str = "raw",
    gains: dict[str, float] | None = None,
) -> tuple[list[Cut], list[str]]:
    """The renderable shots, and what was left out of the render.

    A shot `resolve` has already reported as loose is skipped rather than
    guessed at -- and so is one whose source file is missing, which for
    `--from proxy` is an ordinary state of affairs rather than damage.
    """
    rate = Fraction(target.fps)
    out: list[Cut] = []
    skipped: list[str] = []
    for shot in resolved.shots:
        if shot.problem:
            skipped.append(f"shot {shot.index + 1}: {shot.problem}")
            continue
        path = source_path(shot, source)
        if path is None:
            what = "proxy" if source == "proxy" else "source file"
            skipped.append(f"shot {shot.index + 1}: no {what} for {shot.shot.stem}")
            continue
        length = max(1, frames(shot.duration, rate)) / rate
        out.append(Cut(
            shot=shot, path=path, duration=float(length),
            gain_db=(gains or {}).get(shot.shot.stem, 0.0),
        ))
    return out, skipped


# --- the filter graph --------------------------------------------------


def _video_chain(index: int, cut: Cut, target: Target, fill: str) -> str:
    """Conform one shot to the target shape.

    Emitted in the cheap form when the shot is already the right shape, which
    for this archive is almost always -- everything but one clip was shot
    vertically on the same phone.
    """
    label, w, h = f"v{index}", target.width, target.height
    asset = cut.shot.sidecar.asset
    same_shape = (
        asset.width and asset.height
        and abs(asset.width / asset.height - target.aspect) < 0.01
    )
    tail = f"fps={target.fps},setsar=1,format=yuv420p[{label}]"

    if same_shape or fill == "crop":
        return (
            f"[{index}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},{tail}"
        )
    if fill == "pad":
        return (
            f"[{index}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:-1:-1:color=black,{tail}"
        )
    return (
        f"[{index}:v]split=2[bg{index}][fg{index}];"
        f"[bg{index}]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma={BLUR_SIGMA}[bb{index}];"
        f"[fg{index}]scale={w}:{h}:force_original_aspect_ratio=decrease[ff{index}];"
        f"[bb{index}][ff{index}]overlay=(W-w)/2:(H-h)/2,{tail}"
    )


def _audio_chain(index: int, cut: Cut) -> str:
    """One shot's audio, at a common rate and layout so concat will take it.

    `volume` is a static gain measured once per clip, not a compressor: two
    clips recorded on different afternoons differ by several dB, and a cut that
    jumps in level between shots sounds broken in a way the pictures do not.
    """
    gain = f"volume={cut.gain_db:.1f}dB," if abs(cut.gain_db) >= 0.1 else ""
    return (
        f"[{index}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
        f"channel_layouts=stereo,{gain}asetpts=PTS-STARTPTS[a{index}]"
    )


def bitrate_for(target: Target, duration: float = 0.0, fit: bool = False) -> str:
    """What to hand the hardware encoder, in kbit.

    `fit` is for a render meant to leave this machine: five minutes at the
    bitrate a phone shoots comes to a quarter of a gigabyte, which mail and
    most chat apps refuse. Lowered only as far as it has to be -- a short reel
    is already small and is left alone.
    """
    rate = float(Fraction(target.fps))
    bits = target.width * target.height * rate * BITRATE_BITS_PER_PIXEL
    if fit and duration > 0:
        room = SENDABLE_BYTES * 0.66 * 8 / duration - 160_000
        bits = min(bits, max(500_000, room))
    return f"{int(bits / 1000)}k"


def _encoder_args(target: Target, encoder: str, bitrate: str | None,
                  duration: float = 0.0, fit: bool = False) -> list[str]:
    """Hardware by default: 70 s of 1080x1920 in 12 s against minutes of x264.

    libx264 is the fallback rather than the default -- it is where a file that
    will not encode on the GPU ends up, and where a `--bitrate` is ignored in
    favour of a constant quality, since that is what software encoding is good
    at and hardware cannot do at all.
    """
    if encoder == "sw":
        return ["-c:v", "libx264", "-crf", str(SOFTWARE_CRF),
                "-preset", SOFTWARE_PRESET]
    return ["-c:v", "h264_videotoolbox",
            "-b:v", bitrate or bitrate_for(target, duration, fit)]


@dataclass
class Plan:
    """Everything about a render that can be decided without running one."""

    args: list[str]
    dest: Path
    target: Target
    cuts: list[Cut] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    subtitles: Path | None = None

    @property
    def duration(self) -> float:
        return sum(cut.duration for cut in self.cuts)


def build_plan(
    resolved: Resolved,
    dest: Path,
    *,
    source: str = "raw",
    aspect: str = "source",
    fill: str = "blur",
    height: int | None = None,
    encoder: str = "hw",
    subtitles: Path | None = None,
    burn: bool = False,
    cards: list | None = None,
    lang: str = "en",
    bitrate: str | None = None,
    fit: bool = False,
    gains: dict[str, float] | None = None,
) -> Plan:
    """The ffmpeg invocation for a cut, without running it.

    One input per shot, seeking before `-i` so ffmpeg skips to the shot rather
    than decoding up to it, and one filter chain each into a single `concat`.
    Concatenating in the filter graph rather than by demuxer is what makes a
    join gapless across sources that do not share a frame rate or a shape.
    """
    if fill not in FILLS:
        raise RenderError(f"fill must be one of {', '.join(FILLS)}")
    target = target_for(resolved, aspect=aspect, source=source, height=height)
    cuts, skipped = cuts_for(resolved, target, source=source, gains=gains)
    if not cuts:
        raise RenderError("nothing to render: no shot resolves to a file on disk")

    # Inputs first and output options after, in that order: ffmpeg reads its
    # arguments positionally, and a `-map` written before an `-i` is taken as
    # an option of the input that follows it.
    inputs: list[str] = []
    graph: list[str] = []
    silent: list[int] = []

    for i, cut in enumerate(cuts):
        inputs += ["-ss", f"{cut.start:.3f}", "-t", f"{cut.duration:.3f}",
                   "-i", str(cut.path)]
        graph.append(_video_chain(i, cut, target, fill))
        if cut.shot.sidecar.asset.has_audio:
            graph.append(_audio_chain(i, cut))
        else:
            silent.append(i)

    # A clip with no audio track still owes the concat one, or every shot after
    # it slides forward against the picture.
    for slot, i in enumerate(silent):
        inputs += ["-f", "lavfi", "-t", f"{cuts[i].duration:.3f}",
                   "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append(f"[{len(cuts) + slot}:a]asetpts=PTS-STARTPTS[a{i}]")

    pairs = "".join(f"[v{i}][a{i}]" for i in range(len(cuts)))
    graph.append(f"{pairs}concat=n={len(cuts)}:v=1:a=1[vout][acat]")

    # A per-clip gain can push a peak past full scale -- speech sits far below
    # its own peaks, so the clip that needs the most lift is often the one
    # already peaking. The limiter is what makes the lift safe; without it,
    # matching levels would mean matching the quietest clip.
    if any(abs(cut.gain_db) >= 0.1 for cut in cuts):
        graph.append(f"[acat]alimiter=limit={CEILING}:level=disabled[aout]")
    else:
        graph.append("[acat]anull[aout]")

    # Burnt-in captions are pictures: this ffmpeg has no libass and no
    # drawtext, and a soft track does not survive being sent to anyone. Each
    # card is a looped still, composited only while its line is being said --
    # a timeline-enabled overlay costs nothing on the frames where it is off.
    video_out = "[vout]"
    burnt = list(cards or []) if burn else []
    for i, card in enumerate(burnt):
        slot = len(cuts) + len(silent) + i
        # Bounded and slow. A looped image is an infinite stream, and an
        # overlay whose main input ends against one does not end -- it repeats
        # the last frame for as long as the loop goes on, which for 28 cards
        # was a 70-second reel still writing at 2 GB. `-t` ends each card when
        # its line does, and 2 fps is 15x fewer frames to composite than the
        # timeline's; a still needs no more than that.
        inputs += [
            "-loop", "1", "-framerate", str(CARD_FPS),
            "-t", f"{card.end:.3f}", "-i", str(card.path),
        ]
        nxt = f"[vb{i}]"
        graph.append(
            f"{video_out}[{slot}:v]overlay=x=(W-w)/2:"
            f"y=H*{1 - CAPTION_BASELINE:.3f}-h:eof_action=pass:"
            f"enable='between(t,{card.start:.3f},{card.end:.3f})'{nxt}"
        )
        video_out = nxt

    subtitle_index = len(cuts) + len(silent) + len(burnt)
    if subtitles and not burn:
        inputs += ["-i", str(subtitles)]

    args = ["ffmpeg", "-y", "-nostdin", "-v", "error", *inputs]
    args += ["-filter_complex", ";".join(graph), "-map", video_out, "-map", "[aout]"]
    if subtitles and not burn:
        args += ["-map", f"{subtitle_index}:s", "-c:s", "mov_text",
                 "-metadata:s:s:0", f"language={'kok' if lang == 'kok' else 'eng'}"]

    args += _encoder_args(
        target, encoder, bitrate, sum(cut.duration for cut in cuts), fit
    )
    args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000",
             "-movflags", "+faststart", str(dest)]
    return Plan(args=args, dest=dest, target=target, cuts=cuts,
                skipped=skipped, subtitles=subtitles)


def args_inputs(args: list[str]) -> list[str]:
    """The `-i` values in an argument list, in order."""
    return [args[i + 1] for i, a in enumerate(args) if a == "-i"]


# --- running one -------------------------------------------------------


@dataclass
class Result:
    path: Path
    plan: Plan
    size_bytes: int

    @property
    def duration(self) -> float:
        return self.plan.duration

    def too_big_to_send(self) -> str:
        """A remark, and the bitrate that would fix it. Empty when it fits."""
        if self.size_bytes <= SENDABLE_BYTES or not self.duration:
            return ""
        # Two thirds of the ceiling, so it is comfortably under rather than
        # exactly on it, and rounded to something you would type.
        want = SENDABLE_BYTES * 0.66 * 8 / self.duration / 1_000_000
        return (
            f"{self.size_bytes / 1e6:.0f} MB is more than most ways of sending "
            f"a video will take; --bitrate {max(1, round(want)):d}M "
            f"is about {SENDABLE_BYTES * 0.66 / 1e6:.0f} MB"
        )


def _check_room(resolved, dest: Path, **shape) -> None:
    """Refuse a render the disk cannot hold, before spending a minute on it.

    Roughly: bitrate times length, plus the audio, plus a margin. This drive
    is 95% full, and a render that runs out of space halfway leaves a
    plausible-looking temporary file and nothing said about why it stopped.
    """
    try:
        target = target_for(resolved, aspect=shape.get("aspect", "source"),
                            source=shape.get("source", "raw"),
                            height=shape.get("height"))
    except RenderError:
        return  # nothing to render; the real error comes later and is clearer
    rate = shape.get("bitrate") or bitrate_for(
        target, resolved.duration, shape.get("fit", False)
    )
    try:
        bits = float(rate.rstrip("kKmM")) * (1e6 if rate[-1] in "mM" else 1e3)
    except (ValueError, IndexError):
        return
    wanted = (bits + 160_000) / 8 * resolved.duration * 1.1
    free = shutil.disk_usage(dest.parent).free
    if wanted > free:
        raise RenderError(
            f"this would need about {wanted / 1e9:.1f} GB and there is "
            f"{free / 1e9:.1f} GB free on that disk; --bitrate would make it "
            "smaller, or render somewhere else with --out"
        )


def _run(args: list[str], on_progress=None) -> None:
    if on_progress is None:
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise RenderError(f"ffmpeg failed:\n{tail}")
    _run_watched(args, on_progress)


def _run_watched(args: list[str], on_progress) -> None:
    """The same run, reporting how far in it has got.

    `-progress pipe:1` emits `key=value` lines every second, which is the only
    way to know how a render is doing from outside it -- and a render is the
    one thing here long enough that a button pressed in the browser needs to
    say something while it happens.

    stderr is merged into the same pipe rather than read separately: a failure
    can print more than a pipe buffer holds, and a second pipe nobody is
    draining is a deadlock waiting for a bad file.
    """
    args = [args[0], "-progress", "pipe:1", "-nostats", *args[1:]]
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1,
    )
    tail: deque[str] = deque(maxlen=8)
    for line in proc.stdout:
        line = line.strip()
        # `out_time_us` only. ffmpeg also emits `out_time_ms`, whose value is
        # microseconds too -- a long-standing misnomer -- and reading a field
        # that is wrong about its own units is asking to be a thousand times
        # out the day somebody corrects it.
        if line.startswith("out_time_us="):
            try:
                on_progress(int(line.split("=", 1)[1]) / 1_000_000)
            except ValueError:
                pass
        elif "=" not in line and line:
            tail.append(line)
    if proc.wait() != 0:
        raise RenderError("ffmpeg failed:\n" + "\n".join(tail))


def render(
    reel: Reel,
    *,
    dest: Path | None = None,
    source: str = "raw",
    aspect: str = "source",
    fill: str = "blur",
    height: int | None = None,
    lang: str = "en",
    captions: str = "soft",
    encoder: str = "hw",
    bitrate: str | None = None,
    fit: bool = False,
    sidecars: list | None = None,
    gains: dict[str, float] | None = None,
    resolved: Resolved | None = None,
    on_progress=None,
) -> Result:
    """Render a cut, and leave nothing behind if it fails.

    Written to a temporary name and moved into place, like every other write
    here: a half-written mp4 that looks like a finished render is worse than
    none, because you would watch it and believe the cut ends there.
    """
    # Checked here rather than by the callers, and before anything says it is
    # rendering: a `--captions burnt` that quietly wrote a soft track would be
    # found out at the far end, by somebody watching a video with no captions.
    for value, allowed, what in (
        (source, SOURCES, "--from"), (captions, CAPTIONS, "--captions"),
        (lang, LANGS, "--lang"), (aspect, tuple(ASPECTS), "--aspect"),
        (fill, FILLS, "--fill"),
    ):
        if value not in allowed:
            raise RenderError(f"{what} must be one of {', '.join(allowed)}")

    resolved = resolved or resolve(reel, sidecars)
    dest = dest or (out_dir() / f"{reel.slug}.mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _check_room(resolved, dest, source=source, aspect=aspect, height=height,
                bitrate=bitrate, fit=fit)

    subtitle_path = None
    if captions != "none":
        body = export_reel.srt(resolved, lang=lang)
        if body:
            work = config.media_root() / "work" / "render"
            work.mkdir(parents=True, exist_ok=True)
            subtitle_path = work / f"{reel.slug}.{lang}.srt"
            subtitle_path.write_text(body)

    burn = captions == "burn"
    cards = []
    if burn:
        from . import captions as cards_for

        if not cards_for.available():
            raise RenderError(
                "captions cannot be drawn: no font, or Pillow is not installed.\n"
                "  --captions soft embeds them as a track instead."
            )
        target = target_for(resolved, aspect=aspect, source=source, height=height)
        cards = cards_for.cards_for(
            export_reel.caption_lines(resolved), target.width, target.height, lang
        )

    plan = build_plan(
        resolved, dest.with_suffix(".tmp.mp4"), source=source, aspect=aspect,
        fill=fill, height=height, encoder=encoder, subtitles=subtitle_path,
        burn=burn, cards=cards, lang=lang, bitrate=bitrate, fit=fit, gains=gains,
    )
    try:
        _run(plan.args, on_progress)
    except RenderError:
        if encoder == "hw":
            # Not every file decodes or encodes on the GPU; the same fallback
            # the proxy builder makes, for the same reason.
            plan = build_plan(
                resolved, dest.with_suffix(".tmp.mp4"), source=source,
                aspect=aspect, fill=fill, height=height, encoder="sw",
                subtitles=subtitle_path, burn=burn, cards=cards, lang=lang,
                bitrate=bitrate, fit=fit, gains=gains,
            )
            _run(plan.args, on_progress)
        else:
            raise
    plan.dest.replace(dest)
    plan.dest = dest

    # Recorded only now, with the file on disk -- the same rule the FCPXML
    # export follows: writing one is the only thing that justifies saying one
    # happened. Skipped when the render went somewhere the reel does not own.
    #
    # Written onto the cut as it stands *now*, not onto the copy this render
    # started from. A render takes a minute and the browser is not blocked
    # during it, so a trim made while it ran would otherwise be overwritten by
    # the version being rendered -- and the sha recorded is still that of what
    # was actually rendered, so the file correctly reports itself as behind.
    if dest == out_dir() / f"{reel.slug}.mp4":
        current = load(reel.slug) or reel
        current.rendered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        current.rendered_sha = reel.content_sha()
        current.rendered_captions = captions
        current.save()

    return Result(path=dest, plan=plan, size_bytes=dest.stat().st_size)
