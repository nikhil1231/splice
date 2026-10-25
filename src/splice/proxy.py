"""Build small proxies and time-sliced chunks for analysis.

Both matter. A 132MB/7min transcode took 642s to analyse and came back with a
single generic paragraph; a 1.7MB/35s proxy took 56s and came back with real
per-topic segments. Proxies buy throughput, chunks buy temporal resolution.

Proxies are for the model only. FCPXML always links the original in raw/.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config
from .probe import duration_of


class EncodeError(RuntimeError):
    pass


# Most of this footage is 1080p HEVC, where software decode dominates the cost:
# 2m45s of software decode against 16s hardware for the same 120s slice, byte
# for byte the same output. Not every file will accept it, so fall back.
HWACCEL = ["-hwaccel", "videotoolbox"]


def _run(cmd: list[str], *, allow_hwaccel_fallback: bool = False) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return
    if allow_hwaccel_fallback and HWACCEL[0] in cmd:
        i = cmd.index(HWACCEL[0])
        _run(cmd[:i] + cmd[i + 2:])
        return
    tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
    raise EncodeError(f"{' '.join(cmd[:2])} failed:\n{tail}")


def build_proxy(source: Path, dest: Path, force: bool = False) -> Path:
    """480p / 15fps / mono AAC. Roughly 3-5 MB per minute."""
    if dest.exists() and not force and dest.stat().st_mtime >= source.stat().st_mtime:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    _run(
        [
            "ffmpeg", "-y", "-nostdin", "-v", "error",
            *HWACCEL,
            "-i", str(source),
            # `fps` before `scale`, not `-r` after it. As an output option the
            # rate change happens past the filter chain, so the scaler resizes
            # all thirty frames of every second and fifteen of them are then
            # thrown away. Dropping first halves its work: a 17-minute 1080p
            # HEVC interview went 186.5s -> 160.9s here, same 480p15 out, same
            # size to within 0.06%. It ends two frames sooner -- the filter and
            # the output option round the last of 15,718 differently -- which
            # is 0.13s of tail the model does not see and nothing measures
            # against, since segment times come from the original's duration.
            "-vf", f"fps={config.PROXY_FPS},scale=-2:{config.PROXY_HEIGHT}",
            "-c:v", "libx264", "-crf", str(config.PROXY_CRF), "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            # A keyframe exactly where `build_chunks` will cut, so the cut can
            # be a stream copy instead of a second encode. x264 would otherwise
            # place them every 16.7s and a copy would snap backwards to one,
            # which moves every timestamp in the chunk.
            "-force_key_frames", f"expr:gte(t,n_forced*{config.CHUNK_SECONDS})",
            "-ac", "1", "-ar", str(config.PROXY_AUDIO_RATE),
            "-c:a", "aac", "-b:a", config.PROXY_AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(tmp),
        ],
        allow_hwaccel_fallback=True,
    )
    tmp.replace(dest)
    return dest


def keyframes_in(path: Path) -> list[float]:
    """Every keyframe time in a file, read from the container index.

    Packets rather than frames: `-show_frames` decodes the video to answer,
    which on a 25-minute proxy is slower than the encode this exists to avoid.
    The packet index is already written down -- 0.11s for the same file.
    """
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        stamp, _, flags = line.partition(",")
        if "K" in flags:
            try:
                out.append(float(stamp))
            except ValueError:
                continue
    return sorted(out)


# Half a frame at 15fps. A chunk copied from a keyframe one frame early is a
# chunk whose every timestamp is 0.067s out, which is a caption on the wrong
# syllable by the time it reaches Final Cut; anything but an exact hit
# re-encodes instead.
KEYFRAME_TOLERANCE = 0.033


def chunk_offsets(duration: float, chunk_seconds: int) -> list[float]:
    """Start offsets covering `duration`. Empty when one pass will do."""
    if duration <= chunk_seconds:
        return []
    offsets, t = [], 0.0
    while t < duration:
        offsets.append(t)
        t += chunk_seconds
    # A sliver at the end gets folded into the previous chunk's overlap.
    if len(offsets) > 1 and duration - offsets[-1] < config.CHUNK_OVERLAP:
        offsets.pop()
    return offsets


def build_chunks(
    proxy: Path,
    stem: str,
    chunk_seconds: int = config.CHUNK_SECONDS,
    force: bool = False,
) -> list[tuple[float, Path]]:
    """Slice a proxy into overlapping chunks named `<stem>__<offset>.mp4`.

    The trailing CHUNK_OVERLAP seconds give the model enough context not to cut
    a sentence dead at the boundary; the merge step resolves the duplicates.

    The proxy is already 480p/15fps at the quality analysis reads, so a chunk
    of it is a copy of some of its packets and not a second encode -- 5.04s
    down to 0.10s per chunk on a 25-minute interview. That needs a keyframe
    exactly at the offset, which `build_proxy` puts there. A proxy built
    before it did, or cut at a `chunk_seconds` it was not built for, has none,
    and those chunks are encoded the old way rather than silently landing a
    few frames from where they claim to start.
    """
    duration = duration_of(proxy)
    offsets = chunk_offsets(duration, chunk_seconds)
    if not offsets:
        return []

    out_dir = config.chunk_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[float, Path]] = []
    keyframes: list[float] | None = None

    for offset in offsets:
        dest = out_dir / f"{stem}__{int(round(offset)):05d}.mp4"
        length = min(chunk_seconds + config.CHUNK_OVERLAP, duration - offset)
        if not (dest.exists() and not force and dest.stat().st_mtime >= proxy.stat().st_mtime):
            # Read once per proxy, and only when there is something to cut.
            if keyframes is None:
                keyframes = keyframes_in(proxy)
            exact = any(abs(k - offset) <= KEYFRAME_TOLERANCE for k in keyframes)
            tmp = dest.with_suffix(".tmp.mp4")
            _run(
                [
                    "ffmpeg", "-y", "-nostdin", "-v", "error",
                    "-ss", f"{offset:.3f}", "-i", str(proxy), "-t", f"{length:.3f}",
                    *(["-c", "copy"] if exact else [
                        "-c:v", "libx264", "-crf", str(config.PROXY_CRF),
                        "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", config.PROXY_AUDIO_BITRATE,
                    ]),
                    "-movflags", "+faststart",
                    str(tmp),
                ]
            )
            tmp.replace(dest)
        results.append((offset, dest))

    return results


def clear_chunks(stem: str) -> None:
    for path in config.chunk_dir().glob(f"{stem}__*.mp4"):
        path.unlink()
