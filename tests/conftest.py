"""Every test runs inside a project of its own.

splice finds its project from the environment, the working directory and the
user's ~/.config -- all of which, left alone, would point a test at whoever is
running it's real footage. So each test gets a fresh copy of tests/project
(a splice.yaml naming a cast, and a vocabulary), an empty media root unless it
sets its own, no archive unless it names one, and an empty ~/.config.
"""

import shutil
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent / "project"


@pytest.fixture(autouse=True)
def _own_project(tmp_path_factory, monkeypatch):
    here = tmp_path_factory.mktemp("project")
    for item in PROJECT.iterdir():
        shutil.copy2(item, here / item.name)
    monkeypatch.setenv("SPLICE_CONFIG", str(here / "splice.yaml"))
    monkeypatch.setenv("SPLICE_ROOT", str(here / "media"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(here / "xdg"))
    monkeypatch.delenv("SPLICE_ARCHIVE", raising=False)
    return here
