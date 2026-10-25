"""The flat cut list.

The fallback for editors that cannot read FCPXML, written on every `sync`, and
until now the only output nothing checked. What matters is that a spreadsheet
can sort it: one row per moment, times in two forms, and no newline inside a
cell.
"""

import csv

import pytest

from splice import config, export_csv, reel as store
from splice.reel import Reel, Shot
from splice.schema import Asset, Segment, Sidecar


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    Sidecar(
        asset=Asset(file="a.MOV", stem="a", duration=120),
        segments=[
            Segment(id="a#00000.00", start=0, end=90.5, title="The money box",
                    summary="He opens it.", english="One line.\nAnd another.",
                    konkani="Ek.\nDon.", tags=["theme:money"],
                    proposed_tags=["theme:exams"], rating="favorite"),
            Segment(id="a#00090.50", start=90.5, end=100, title="After"),
        ],
    ).save(config.sidecar_path("a"))
    Sidecar(
        asset=Asset(file="b.MOV", stem="b", duration=30),
        segments=[Segment(id="b#0", start=0, end=30, title="Elsewhere")],
    ).save(config.sidecar_path("b"))
    return tmp_path


def _rows(**kwargs):
    path, count = export_csv.write(**kwargs)
    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == count
    return rows


def test_a_row_for_every_moment(archive):
    assert [row["segment_id"] for row in _rows()] == [
        "a#00000.00", "a#00090.50", "b#0",
    ]


def test_times_come_in_both_forms(archive):
    row = _rows()[0]
    assert (row["start_tc"], row["end_tc"]) == ("00:00:00.00", "00:01:30.50")
    assert (row["start_s"], row["end_s"], row["duration_s"]) == ("0.00", "90.50", "90.50")


def test_no_cell_holds_a_newline(archive):
    """A spreadsheet will take one and then not sort."""
    row = _rows()[0]
    assert row["english"] == "One line. / And another."
    assert row["konkani"] == "Ek. / Don."
    assert not any("\n" in value for value in row.values())


def test_the_verdict_and_the_quarantined_tags_are_carried(archive):
    row = _rows()[0]
    assert row["rating"] == "favorite" and row["status"] == "indexed"
    assert row["tags"] == "theme:money"
    assert row["proposed_tags"] == "theme:exams"


def test_a_row_says_which_cuts_have_claimed_it(archive):
    """So a sorted sheet shows what is already spoken for."""
    Reel(slug="money", title="Money", shots=[
        Shot(stem="a", start=0, end=90.5, segment_id="a#00000.00"),
    ]).save(store.path_for("money"))

    rows = {row["segment_id"]: row["reels"] for row in _rows()}
    assert rows["a#00000.00"] == "money"
    assert rows["b#0"] == ""


def test_only_narrows_it_to_one_clip(archive):
    assert {row["stem"] for row in _rows(only="b")} == {"b"}


def test_it_is_written_where_it_is_asked_for(archive, tmp_path):
    path, _ = export_csv.write(dest=tmp_path / "deeper" / "list.csv")
    assert path.exists() and path.name == "list.csv"
