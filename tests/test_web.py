"""The browser page, which nothing else here tests.

There is no JavaScript test setup and this is not one. It is the cheapest
possible guard on a file that is edited constantly and whose failures are
silent: a syntax error in `index.html` breaks the whole UI, the Python suite
stays green, and you find out by opening a blank page.

Skipped where there is no node, which is most machines that are not this one.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "src" / "greels" / "web" / "index.html"
SCRIPT = re.compile(r"<script>(.*)</script>", re.S)
STYLE = re.compile(r"<style>(.*)</style>", re.S)


def _script() -> str:
    found = SCRIPT.search(PAGE.read_text())
    assert found, "index.html has no <script> block"
    return found.group(1)


@pytest.mark.skipif(not shutil.which("node"), reason="no node on this machine")
def test_the_page_script_parses(tmp_path):
    scratch = tmp_path / "page.js"
    scratch.write_text(_script())
    done = subprocess.run(["node", "--check", str(scratch)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_every_style_the_page_uses_is_one_it_defines():
    """A class named in the JS and nowhere in the CSS is either dead or a
    typo, and both look the same from here."""
    page = PAGE.read_text()
    css = STYLE.search(page).group(1)
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    # Written as `class: 'row line'`, `class: 'btn primary'`, and so on.
    used = {
        name
        for literal in re.findall(r"class: '([^']*)'", _script())
        for name in literal.split()
        if name and not name.startswith("$")
    }
    assert used - defined == set()


def test_the_server_serves_an_address_for_every_tab_and_no_other():
    """The tabs are listed twice, once in the page and once in the server,
    and a tab added to one alone reloads into a 404."""
    from greels.server import PAGES

    tabs = re.search(r"const TABS = \[([^\]]*)\]", _script()).group(1)
    assert {"/" + t.lower() for t in re.findall(r"'([^']+)'", tabs)} == PAGES
