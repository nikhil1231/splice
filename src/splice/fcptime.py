"""Rational time arithmetic for FCPXML.

Final Cut rejects a file whose times are not exact frame multiples, so every
value is computed in whole frames and only then written as a rational. Floats
never touch the output.

Timescale convention follows Apple's own files: 29.97 fps is written over
30000, and integer rates are scaled by 100 (30 fps -> 100/3000s) rather than
reduced to 1/30s.
"""

from __future__ import annotations

from fractions import Fraction


def timebase(fps: str | Fraction) -> tuple[int, int]:
    """(frame_ticks, timescale) such that one frame == frame_ticks/timescale s."""
    rate = Fraction(fps)
    if rate <= 0:
        rate = Fraction(30, 1)
    num, den = rate.numerator, rate.denominator
    if den == 1:
        return 100, num * 100
    return den, num


def frames(seconds: float, fps: str | Fraction) -> int:
    rate = Fraction(fps)
    if rate <= 0:
        rate = Fraction(30, 1)
    return max(0, round(Fraction(seconds).limit_denominator(1000000) * rate))


def rational(seconds: float, fps: str | Fraction) -> str:
    """Seconds -> a frame-aligned FCPXML time string."""
    return from_frames(frames(seconds, fps), fps)


def from_frames(n: int, fps: str | Fraction) -> str:
    """A whole frame count -> an FCPXML time string.

    A timeline is laid out by accumulating frames, not seconds: rounding each
    clip's start independently leaves gaps and overlaps a frame wide, which
    Final Cut shows as black flashes between shots.
    """
    ticks, scale = timebase(fps)
    return "0s" if n == 0 else f"{n * ticks}/{scale}s"


def frame_duration(fps: str | Fraction) -> str:
    ticks, scale = timebase(fps)
    return f"{ticks}/{scale}s"


def snap(seconds: float, fps: str | Fraction) -> float:
    """The frame-aligned value `rational` will actually encode."""
    ticks, scale = timebase(fps)
    return frames(seconds, fps) * ticks / scale
