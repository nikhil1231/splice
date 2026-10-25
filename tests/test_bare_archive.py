"""Every command, on an archive that has not been indexed yet.

The first run of anything here is on an empty media root, and so is the run
after somebody deletes `library.sqlite` -- which is safe to do, because it is
derived. Neither should produce a stack trace.
"""

import pytest
import typer

from splice import cli, cli_reel


@pytest.fixture
def bare(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    monkeypatch.delenv("SPLICE_FCP_LIBRARY", raising=False)
    return tmp_path


def _exits(fn, *args, **kwargs):
    with pytest.raises(typer.Exit):
        fn(*args, **kwargs)


def test_search_says_to_build_one(bare, capsys):
    _exits(cli.search, "money", tag=[], status=None, rating=None, unused=False,
           used=False, order="clip", limit=30)
    assert "no index at" in capsys.readouterr().out


def test_lines_says_to_build_one(bare, capsys):
    _exits(cli.lines, "money", limit=20, lang="en")
    assert "no index at" in capsys.readouterr().out


def test_suggestions_say_to_build_one(bare, capsys):
    _exits(cli.reels, limit=10, tag=None)
    assert "no index at" in capsys.readouterr().out


def test_related_says_to_build_one(bare, capsys):
    _exits(cli.related, "hero", limit=8, same_clip=False)
    assert "no index at" in capsys.readouterr().out


def test_seeding_a_cut_from_a_suggestion_says_to_build_one(bare, capsys):
    _exits(cli_reel.reel_new, "X", from_tag="era:1960s", segment=[], said=None,
           search="", tag=[], rating="", limit=40, order="found", hook="")
    assert "no index at" in capsys.readouterr().out


def test_the_ones_that_do_not_need_an_index_still_work(bare, capsys):
    cli.status(editor=None)
    cli_reel.reel_list()
    out = capsys.readouterr().out
    assert "0 found" in out and "no reels yet" in out
