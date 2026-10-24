"""Read what DaVinci Resolve actually holds, and hand it a timeline.

The companion to `fcp.py`, and it has a harder job. A Final Cut library is a
folder full of files, so its catalogue can be read at three in the morning with
Final Cut shut. Resolve keeps its projects in a library it does not document,
and the documented way in is the scripting API -- which answers only while
Resolve is running with a project open, and which Blackmagic ships as a
Studio feature.

So `imported_clips` here returns `None` far more often than Final Cut's does,
and that is not a degradation to work around: everything downstream already
treats `None` as "trust what the sidecars say and change nothing", which is
exactly right for an editor nobody has opened yet. `greels imported --editor
resolve` is the escape hatch, the same as it always was.

Nothing here writes to a project unless it is asked to. `import_timeline` is
the one call that does, and it only ever adds: a new timeline in the current
project, never a replacement of one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Where Blackmagic installs the scripting module and the library behind it.
# The Windows entries are not speculative -- they are what Resolve's own
# `Developer/Scripting/README.txt` lists, and they are the ones that will
# matter, because this archive is moving to Windows.
PLATFORM_PATHS = {
    "darwin": (
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve"
        "/Developer/Scripting",
        "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries"
        "/Fusion/fusionscript.so",
    ),
    "win32": (
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer"
        r"\Scripting",
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    ),
    "linux": (
        "/opt/resolve/Developer/Scripting",
        "/opt/resolve/libs/Fusion/fusionscript.so",
    ),
}


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def api_dir() -> Path | None:
    """The Scripting folder holding `Modules/DaVinciResolveScript.py`."""
    override = os.environ.get("RESOLVE_SCRIPT_API")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    path = Path(PLATFORM_PATHS[_platform_key()][0])
    return path if path.is_dir() else None


def library_file() -> Path | None:
    """`fusionscript`, the shared library the module talks to Resolve through."""
    override = os.environ.get("RESOLVE_SCRIPT_LIB")
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None
    path = Path(PLATFORM_PATHS[_platform_key()][1])
    return path if path.exists() else None


def installed() -> bool:
    """Is there a Resolve here to talk to at all?

    Separate from `connect()` succeeding, and `doctor` needs the distinction:
    "Resolve is not installed" and "Resolve is installed but not running" have
    different answers, and only the second one is fixed by opening it.
    """
    return api_dir() is not None and library_file() is not None


def _module():
    """Import Blackmagic's module, putting its folder on the path if it is not.

    Imported late and by hand rather than at the top of the file, because this
    module has to be importable -- by the tests, and by a machine with no
    Resolve on it -- without any of this existing.
    """
    api = api_dir()
    if api is None or library_file() is None:
        return None
    modules = str(api / "Modules")
    if modules not in sys.path:
        sys.path.append(modules)
    # The module reads these out of the environment when it loads the library,
    # so setting them here is what makes an import work in a shell that never
    # sourced Blackmagic's snippet.
    os.environ.setdefault("RESOLVE_SCRIPT_API", str(api))
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", str(library_file()))
    try:
        import DaVinciResolveScript  # type: ignore

        return DaVinciResolveScript
    except Exception:  # a missing library, a Python it was not built for
        return None


def connect():
    """The running Resolve, or None.

    `scriptapp` returns None rather than raising when Resolve is not running,
    which is the common case and not an error worth a traceback.
    """
    module = _module()
    if module is None:
        return None
    try:
        return module.scriptapp("Resolve")
    except Exception:
        return None


def _media_pool(app=None):
    app = app or connect()
    if app is None:
        return None
    try:
        project = app.GetProjectManager().GetCurrentProject()
        return project.GetMediaPool() if project else None
    except Exception:
        return None


def base_name(name: str) -> str:
    """'lunch.MOV' -> 'lunch'.

    Resolve names a media pool item after the file, extension and all, where
    Final Cut uses the clip name. Stripping it is what lets one set of stems
    be matched against either editor.
    """
    return Path(str(name)).stem


def _walk(folder, seen: set[int] | None = None) -> list:
    """Every clip in a folder and everything under it.

    The media pool is a tree and the material could be filed anywhere in it by
    the time somebody has been editing for a week -- asking only the root
    folder would report a pool full of clips as empty.
    """
    seen = set() if seen is None else seen
    out = []
    try:
        out.extend(folder.GetClipList() or [])
        for child in folder.GetSubFolderList() or []:
            key = id(child)
            if key in seen:
                continue
            seen.add(key)
            out.extend(_walk(child, seen))
    except Exception:
        pass
    return out


def imported_clips(app=None) -> dict[str, int] | None:
    """Clip stem -> how many copies the media pool holds, or None if unreadable.

    `None` and `{}` mean different things here exactly as they do for Final
    Cut: an empty pool is grounds for offering every clip, an unreachable
    Resolve is grounds for trusting whatever the sidecars already say.
    """
    pool = _media_pool(app)
    if pool is None:
        return None
    try:
        root = pool.GetRootFolder()
    except Exception:
        return None
    if root is None:
        return None

    counts: dict[str, int] = {}
    for clip in _walk(root):
        try:
            name = clip.GetName()
        except Exception:
            continue
        if isinstance(name, str) and name:
            key = base_name(name)
            counts[key] = counts.get(key, 0) + 1
    return counts


def duplicate_names(app=None) -> dict[str, list[str]]:
    """Base name -> every name the pool holds for it, where it holds more than one.

    Usually empty, and that is the point: Resolve matches a re-import against
    the file path it already has and reuses the clip instead of making a second
    one. Final Cut's `- a1` copies have no equivalent here. Kept so the sync
    report is the same shape for both editors, and because a pool really can
    end up with two entries for one file -- the same footage copied to two
    paths, which is worth being told about.
    """
    pool = _media_pool(app)
    if pool is None:
        return {}
    try:
        root = pool.GetRootFolder()
    except Exception:
        return {}
    if root is None:
        return {}

    grouped: dict[str, list[str]] = {}
    for clip in _walk(root):
        try:
            name = clip.GetName()
        except Exception:
            continue
        if isinstance(name, str) and name:
            grouped.setdefault(base_name(name), []).append(name)
    return {k: sorted(v) for k, v in grouped.items() if len(v) > 1}


class NotRunning(RuntimeError):
    """Resolve could not be reached, so nothing was handed to it."""


def import_timeline(path: Path, name: str | None = None) -> str:
    """Put a written timeline into the open project. Returns the timeline's name.

    The whole reason Resolve is worth the port: the file handshake is optional.
    `reel export --editor resolve --send` writes the FCPXML and then puts it on
    the timeline list, so "does this work?" stops being a trip through Finder.

    It only ever adds. `ImportTimelineFromFile` makes a new timeline and leaves
    every existing one alone, and `importSourceClips` brings the raw files into
    the media pool if they are not already there.
    """
    pool = _media_pool()
    if pool is None:
        raise NotRunning(
            "Resolve is not running, or has no project open — open the project "
            "you want this in, then run it again"
        )
    options = {"importSourceClips": True}
    if name:
        options["timelineName"] = name
    try:
        timeline = pool.ImportTimelineFromFile(str(Path(path).resolve()), options)
    except Exception as exc:  # the API raises nothing useful; say what we tried
        raise NotRunning(f"Resolve refused {path.name}: {exc}") from exc
    if not timeline:
        raise NotRunning(
            f"Resolve read {path.name} and made no timeline out of it — check "
            "that the media in raw/ is where the XML says it is"
        )
    try:
        return timeline.GetName()
    except Exception:
        return name or Path(path).stem
