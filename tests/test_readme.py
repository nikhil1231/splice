"""The README is the specification, so it should not lie about the CLI.

Every `uv run splice ...` line in it is resolved against the real command tree
and its real options. Nothing is executed -- half of these write files -- but a
renamed flag or a command that moved under a different group fails here, which
is the only place it would ever be noticed.
"""

import re
import shlex
from pathlib import Path

import pytest
import typer.main

from splice import cli

README = Path(__file__).resolve().parents[1] / "README.md"
LINE = re.compile(r"^\s*uv run splice (.+)$", re.M)


def _invocations() -> list[str]:
    out = []
    for body in LINE.findall(README.read_text()):
        # `splice scan / proxy / analyze -j 3` is a list of commands, not one.
        for part in body.split(" / "):
            part = part.split("#", 1)[0].strip()
            if part:
                out.append(part)
    return out


INVOCATIONS = _invocations()


def test_the_readme_actually_shows_some_commands():
    assert len(INVOCATIONS) > 20


@pytest.mark.parametrize("invocation", INVOCATIONS, ids=lambda s: s[:40])
def test_the_readme_names_a_command_that_exists(invocation):
    command = typer.main.get_command(cli.app)
    tokens = shlex.split(invocation)

    path = []
    while tokens and hasattr(command, "commands"):
        name = tokens[0]
        if name.startswith("-"):
            break
        child = command.commands.get(name)
        assert child is not None, (
            f"`splice {' '.join(path + [name])}` is in the README and not in the CLI"
        )
        command, path = child, path + [name]
        tokens = tokens[1:]

    assert path, f"`splice {invocation}` names no command"

    known = {opt for param in command.params for opt in param.opts + param.secondary_opts}
    for token in tokens:
        if not token.startswith("-"):
            continue
        flag = token.split("=", 1)[0]
        assert flag in known, (
            f"`splice {' '.join(path)}` has no {flag}; the README says it does"
        )
