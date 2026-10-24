"""Calling a command function directly, which is what everything here does.

`greels reel new` is a plain function with typer Options as defaults, so a
test -- or `sync` -- can call it. The trap is that an option you do not pass
does not arrive as its default: it arrives as a `typer.models.OptionInfo`,
which is an object, and every object is truthy. Adding an option to a command
therefore silently changes what every existing caller does, and the symptom
turns up somewhere else entirely -- the first time this happened, a new
`--line` on `reel add` surfaced as a sqlite error inside a full-text search.

This checks every such call in the codebase and the tests, so the next option
added to a command fails here, by name, instead of there.
"""

import ast
import inspect
from pathlib import Path

import pytest
import typer

from greels import cli, cli_reel

MODULES = {"cli": cli, "cli_reel": cli_reel}
ROOT = Path(__file__).resolve().parents[1]
# The files a bare `scan(...)` could mean a command in.
OWN = {"cli.py": "cli", "cli_reel.py": "cli_reel"}


def _options_of(module) -> dict[str, set[str]]:
    """Each command in a module, and the options a caller has to pass by hand."""
    out = {}
    for name, value in vars(module).items():
        if not callable(value) or isinstance(value, type):
            continue
        if getattr(value, "__module__", "") != module.__name__:
            continue
        try:
            params = inspect.signature(value).parameters
        except (TypeError, ValueError):
            continue
        wanted = {
            key for key, param in params.items()
            if isinstance(param.default, typer.models.OptionInfo)
        }
        if wanted:
            out[name] = wanted
    return out


OPTIONS = {alias: _options_of(module) for alias, module in MODULES.items()}


def _command_calls(path: Path):
    """Calls that reach a command: `cli.scan(...)`, or a bare `scan(...)` in
    the module that defines it. Anything else named `search` is not this."""
    own = OWN.get(path.name)
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func, alias, name = node.func, None, None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in OPTIONS:
                alias, name = func.value.id, func.attr
        elif isinstance(func, ast.Name) and own:
            alias, name = own, func.id
        if alias and name in OPTIONS[alias]:
            yield name, OPTIONS[alias][name], {kw.arg for kw in node.keywords}, node.lineno


SOURCES = sorted(
    list((ROOT / "src" / "greels").glob("*.py")) + list((ROOT / "tests").glob("*.py"))
)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_every_option_of_a_command_is_passed_when_it_is_called(path):
    misses = []
    for name, wanted, passed, line in _command_calls(path):
        if None in passed:  # **kwargs: assume the caller knows
            continue
        missing = wanted - passed
        if missing:
            misses.append(f"{path.name}:{line} {name}() without {sorted(missing)}")
    assert not misses, (
        "an unpassed typer default arrives as an OptionInfo, which is truthy:\n  "
        + "\n  ".join(misses)
    )
