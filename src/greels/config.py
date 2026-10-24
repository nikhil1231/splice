"""Paths, the project, and tunables.

A *project* is one archive: a media root holding the footage and everything
derived from it, and a `greels.yaml` in it saying what that footage is -- who
is in it, what they speak, where the backup goes. The code knows nothing about
any one family; that file does, and `greels init` writes a starting one.

Which project is found, first match wins:

1. `GREELS_ROOT`, for tests, scripts and a second archive;
2. the nearest directory above the current one holding a `greels.yaml`, the
   way git finds a repository;
3. `root:` in the user's own `~/.config/greels/config.yaml`, which `greels
   init` fills in, so the browser can be started from anywhere.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PACKAGE_DIR = Path(__file__).resolve().parent
# The prompts and vocabulary a project starts from. A project's own copy, when
# it has one, wins; these are what is used until it does.
DEFAULTS_DIR = PACKAGE_DIR / "defaults"
PROJECT_FILE = "greels.yaml"


class NoProject(RuntimeError):
    """Nothing says where the footage is. Guessing a folder is how a first run
    scatters raw/ and index/ through whatever directory it was started in."""


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / "greels" / "config.yaml"


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise NoProject(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise NoProject(f"{path} should be a mapping of settings")
    return data


def default_root() -> Path | None:
    """The project `greels init` last made the default, if any."""
    path = user_config_path()
    if not path.is_file():
        return None
    root = _read_yaml(path).get("root")
    return Path(root).expanduser() if root else None


def media_root() -> Path:
    override = os.environ.get("GREELS_ROOT")
    if override:
        return Path(override).expanduser()
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / PROJECT_FILE).is_file():
            return candidate
    root = default_root()
    if root is not None:
        return root
    raise NoProject(
        "No greels project found. Run `greels init <folder>` to make one, "
        "run greels from inside a folder holding a greels.yaml, or set GREELS_ROOT."
    )


def raw_dir() -> Path:
    return media_root() / "raw"


def proxy_dir() -> Path:
    return media_root() / "work" / "proxies"


def chunk_dir() -> Path:
    return media_root() / "work" / "chunks"


def index_dir() -> Path:
    return media_root() / "index"


def export_dir() -> Path:
    return media_root() / "export"


def db_path() -> Path:
    return index_dir() / "library.sqlite"


def sidecar_path(stem: str) -> Path:
    return index_dir() / f"{stem}.seg.json"


def fcp_library() -> Path | None:
    """An explicit .fcpbundle to read import state from, if one is configured.

    Unset is the normal case: the single bundle sitting in the media root is
    found by discovery. This is the escape hatch for a library kept elsewhere.
    """
    override = os.environ.get("GREELS_FCP_LIBRARY")
    return Path(override).expanduser() if override else None


# --- The project -------------------------------------------------------


class Person(BaseModel):
    """Somebody the footage is about, or a group of them.

    The id is what tags (`person:<id>`) and line speakers are written as, so
    renaming one in here does not rename it in the sidecars.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str = ""
    # Told to the model, so it can tell people apart: "the older woman".
    describe: str = ""
    # A Final Cut title colour, "r g b a" from 0 to 1. Unset is white.
    colour: str = ""
    # Behind the camera and asking the questions: usually off-screen, never
    # the subject a clip is named after.
    camera: bool = False
    # False for a group tag like person:both, which tags a moment but never
    # says a line.
    speaker: bool = True
    # The word a clip named after them starts with. Defaults to the id.
    word: str = ""

    @property
    def label(self) -> str:
        return self.name or self.id.replace("_", " ").title()

    @property
    def filename_word(self) -> str:
        return self.word or self.id


class Project(BaseModel):
    """What one archive is. Every field has a default, so an empty greels.yaml
    -- or none at all -- is a project about nobody in particular."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The Final Cut event and Resolve bin the archive is delivered into.
    name: str = "Footage"
    # One phrase for the model: what this is and what it is for.
    footage: str = "personal video footage, being cut into short vertical videos"
    # The language people speak in it. Captions and the browser call the
    # original lines by this name; empty calls them "Original".
    language: str = ""
    # How that language is written down, if not just its name: "romanised
    # Konkani" for a language whose own script nobody will read.
    transcription: str = ""
    people: list[Person] = Field(default_factory=list)
    # Where `greels backup` mirrors the irreplaceable files. Relative paths
    # are from the media root. Unset means no backup is taken.
    archive: str = ""

    @property
    def speakers(self) -> tuple[str, ...]:
        """Who a line can be attributed to, in the order the file lists them."""
        return tuple(p.id for p in self.people if p.speaker)

    @property
    def language_label(self) -> str:
        return self.language or "Original"

    @property
    def spoken(self) -> str:
        """The language, as a sentence names it."""
        return self.language or "the original language"

    @property
    def written(self) -> str:
        """How the original speech is to be transcribed."""
        return self.transcription or self.spoken


def project_path() -> Path:
    """The greels.yaml in use. `GREELS_CONFIG` names one directly, which is how
    the tests stand in a cast of their own without a media root to hold it."""
    override = os.environ.get("GREELS_CONFIG")
    if override:
        return Path(override).expanduser()
    return media_root() / PROJECT_FILE


def project_dir() -> Path:
    """Where the project's own taxonomy and prompts sit, beside greels.yaml."""
    return project_path().parent


@lru_cache(maxsize=8)
def _load_project(path: str, stamp: int) -> Project:
    try:
        return Project.model_validate(_read_yaml(Path(path)))
    except ValidationError as exc:
        raise NoProject(f"{path} has a setting greels does not understand:\n{exc}") from None


def project() -> Project:
    """The project, re-read whenever greels.yaml changes on disk.

    Asked for once per transcript line in places, so it is cached on the
    file's modification time rather than parsed every time.
    """
    path = project_path()
    try:
        stamp = path.stat().st_mtime_ns
    except FileNotFoundError:
        return Project()
    return _load_project(str(path), stamp)


def event_name() -> str:
    return project().name


def export_stem() -> str:
    """The library import's file name, from the project's: Grandparents makes
    grandparents.fcpxml, which is the name it had before there were projects."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", event_name().lower()).strip("_") or "library"


def archive_dir() -> Path | None:
    """Where the backup goes: `GREELS_ARCHIVE`, else the project's `archive:`."""
    override = os.environ.get("GREELS_ARCHIVE")
    if override:
        return Path(override).expanduser()
    configured = project().archive
    if not configured:
        return None
    path = Path(configured).expanduser()
    return path if path.is_absolute() else media_root() / path


def taxonomy_path() -> Path:
    """The project's vocabulary. Written to by `taxonomy promote`, so this is
    always the project's own path even before the file exists."""
    return project_dir() / "taxonomy.yaml"


def default_taxonomy_path() -> Path:
    return DEFAULTS_DIR / "taxonomy.yaml"


def prompt_path(name: str = "analyze.md") -> Path:
    """The project's own copy of a prompt if it has one, else the default."""
    own = project_dir() / "prompts" / name
    return own if own.is_file() else DEFAULTS_DIR / "prompts" / name


def speaker_instructions(p: Project | None = None) -> str:
    """How the model is told who can say a line, from the project's people."""
    p = p or project()
    if not p.speakers:
        return ("always `unknown` -- nobody has been named for this footage, so "
                "no line can be attributed.")
    ids = ", ".join(f"`{s}`" for s in p.speakers)
    said = [f"one of {ids}, or `unknown`."]
    for person in p.people:
        if person.speaker and person.describe:
            said.append(f"`{person.id}` is {person.describe}.")
    for person in p.people:
        if person.camera:
            said.append(f"{person.label} is the one holding the camera, so is "
                        "usually off-screen and usually the one asking.")
    said.append("Use `unknown` rather than guessing when two people talk over "
                "each other or you genuinely cannot tell -- a caption coloured "
                "for the wrong person is worse than one left in the default "
                "colour.")
    return " ".join(said)


def fill_prompt(template: str, p: Project | None = None) -> str:
    """Put the project into a prompt: what the footage is, its language, who
    is in it. A project's own prompt with none of these placeholders comes back
    unchanged -- which is what keeps an archive's analysis fingerprint, and so
    its hours of transcripts, from going stale when the prompt moved here."""
    p = p or project()
    return (
        template.replace("{footage}", p.footage)
        .replace("{spoken}", p.spoken)
        .replace("{written}", p.written)
        .replace("{speakers}", speaker_instructions(p))
    )


def schedule_seed_path() -> Path:
    """A series plan written outside greels, offered to the browser as a
    starting point for the plans board."""
    return project_dir() / "plans" / "schedule.json"


LEGACY_LOG_PATH_NAME = "video_logs.txt"


def legacy_log_path() -> Path:
    return media_root() / LEGACY_LOG_PATH_NAME


# --- Media -------------------------------------------------------------

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v"}

# raw/ is a drop bin: anything you put in it, including whole folders straight
# off a phone, gets picked up. Add a directory name here only to make discovery
# ignore it.
RAW_SKIP_DIRS: set[str] = set()

# When raw/ holds both X.MOV and X.mp4 of the same clip -- which a phone export
# will happily produce -- the .mp4 is the downscale. Prefer the original:
# FCPXML must link the real camera master, not a 640x360 copy.
SUFFIX_PRIORITY = [".mov", ".mp4", ".m4v"]

# Proxy encode. ~3-5 MB/min, versus the 132MB/7min transcode that took 642s.
PROXY_HEIGHT = 480
PROXY_FPS = 15
PROXY_CRF = 30
PROXY_AUDIO_RATE = 32000
PROXY_AUDIO_BITRATE = "64k"

# Analysis is sparse over long inputs, so slice. See plan: if segments come
# back thin, drop this to 120 before changing anything else.
CHUNK_SECONDS = 240
CHUNK_OVERLAP = 5.0

# --- agy ---------------------------------------------------------------

AGY_BIN = os.environ.get("GREELS_AGY_BIN", "agy")
AGY_MODEL = os.environ.get("GREELS_AGY_MODEL", "gemini-3.1-pro-high")
DRAFT_MODEL = os.environ.get("GREELS_DRAFT_MODEL", AGY_MODEL)
# The prompt is a single argv string to agy, so the ceiling is ARG_MAX and not
# the model's context. Measured here: one argument of 1,043,707 bytes execs, and
# 400,000 still execs beside a 500 KB environment. All 192 summaries are 72 KB
# and every transcript line in the archive is 185 KB, so a whole library now goes
# in one prompt. At 48,000 it took two, and two batches of candidates have to be
# reduced to three -- which is a step that could throw a good story away.
DRAFT_PROMPT_CHARS = 400000
AGY_TIMEOUT = os.environ.get("GREELS_AGY_TIMEOUT", "20m")
# Wall-clock ceiling for one subprocess, generously above AGY_TIMEOUT.
AGY_SUBPROCESS_TIMEOUT = 1500
AGY_RETRIES = 2
# agy auto-denies read_file in headless mode and a project-local
# .antigravity/settings.json is not consulted, so the flag is the only way in
# short of editing the user's global agy config. Set to 0 if allow-rules exist.
AGY_SKIP_PERMISSIONS = os.environ.get("GREELS_AGY_SKIP_PERMISSIONS", "1") != "0"
DEFAULT_JOBS = 3

