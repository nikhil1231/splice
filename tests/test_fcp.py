"""Reading what Final Cut holds.

The pipeline's idea of "imported" is only worth anything if it comes from the
library rather than from our own optimism, so this covers the reader and, just
as importantly, what it does when it cannot read.
"""

import plistlib
import sqlite3

import pytest

from greels import fcp

# The two CoreData tables the reader touches, as Final Cut lays them out.
EVENT_SCHEMA = """
CREATE TABLE ZCOLLECTION (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZFLAGS INTEGER,
    ZCATALOG INTEGER, ZMETADATA INTEGER, ZIDENTIFIER VARCHAR, ZNAME VARCHAR,
    ZTYPE VARCHAR
);
CREATE TABLE ZCOLLECTIONMD (
    Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCOLLECTION INTEGER,
    ZIDENTIFIER VARCHAR, ZDICTIONARYDATA BLOB
);
"""


def archived(**fields) -> bytes:
    """An NSKeyedArchiver dictionary, as Final Cut stores one."""
    objects = ["$null", None]
    keys, values = [], []
    for key, value in fields.items():
        keys.append(plistlib.UID(len(objects)))
        objects.append(key)
        values.append(plistlib.UID(len(objects)))
        objects.append(value)
    class_uid = plistlib.UID(len(objects))
    objects.append({"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]})
    objects[1] = {"NS.keys": keys, "NS.objects": values, "$class": class_uid}

    return plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
    )


def make_event(bundle, event_name, clips, assets=None):
    """One event inside a .fcpbundle, holding these clip names."""
    event = bundle / event_name
    event.mkdir(parents=True)
    conn = sqlite3.connect(event / "CurrentVersion.fcpevent")
    conn.executescript(EVENT_SCHEMA)

    rows = [(fcp.CLIP_TYPE, {"displayName": n}) for n in clips]
    rows += [(fcp.ASSET_TYPE, dict(displayName=n, md5String=h))
             for n, h in (assets or {}).items()]
    for pk, (ztype, fields) in enumerate(rows, start=1):
        conn.execute("INSERT INTO ZCOLLECTION (Z_PK, ZTYPE) VALUES (?,?)", (pk, ztype))
        conn.execute(
            "INSERT INTO ZCOLLECTIONMD (Z_PK, ZCOLLECTION, ZDICTIONARYDATA) VALUES (?,?,?)",
            (pk, pk, archived(**fields)),
        )
    conn.commit()
    conn.close()
    return event


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    monkeypatch.delenv("GREELS_FCP_LIBRARY", raising=False)
    bundle = tmp_path / "Grandparents.fcpbundle"
    bundle.mkdir(parents=True)
    return bundle


def test_reads_the_clips_final_cut_holds(library):
    make_event(library, "Grandparents", ["lunch", "balcony"])
    assert fcp.imported_clips() == {"lunch": 1, "balcony": 1}


def test_every_event_in_the_bundle_counts(library):
    make_event(library, "Grandparents", ["lunch"])
    make_event(library, "Second Event", ["balcony"])
    assert fcp.imported_clips() == {"lunch": 1, "balcony": 1}


def test_a_second_import_is_the_same_clip_counted_twice(library):
    """Final Cut suffixes rather than replaces, and the count is what proves
    a re-offered clip actually landed."""
    make_event(library, "Grandparents", ["lunch", "lunch - a1"])
    assert fcp.imported_clips() == {"lunch": 2}


def test_duplicates_are_named_so_you_know_what_to_delete(library):
    make_event(library, "Grandparents", ["lunch", "lunch - a1", "balcony"])
    assert fcp.duplicate_names() == {"lunch": ["lunch", "lunch - a1"]}


def test_asset_hashes_are_read(library):
    make_event(library, "Grandparents", ["lunch"], assets={"lunch": "ABC123"})
    assert fcp.asset_hashes() == {"lunch": "ABC123"}


def test_no_library_is_not_an_empty_library(tmp_path, monkeypatch):
    """The difference decides whether every clip gets offered again."""
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    monkeypatch.delenv("GREELS_FCP_LIBRARY", raising=False)
    assert fcp.imported_clips() is None


def test_an_empty_library_is_readable_and_empty(library):
    make_event(library, "Grandparents", [])
    assert fcp.imported_clips() == {}


def test_an_unreadable_catalogue_reads_as_no_library(library):
    event = library / "Grandparents"
    event.mkdir(parents=True)
    (event / "CurrentVersion.fcpevent").write_bytes(b"not a database at all")
    assert fcp.imported_clips() is None


def test_one_corrupt_row_does_not_lose_the_others(library):
    event = make_event(library, "Grandparents", ["lunch", "balcony"])
    conn = sqlite3.connect(event / "CurrentVersion.fcpevent")
    conn.execute("INSERT INTO ZCOLLECTION (Z_PK, ZTYPE) VALUES (99, ?)", (fcp.CLIP_TYPE,))
    conn.execute(
        "INSERT INTO ZCOLLECTIONMD (Z_PK, ZCOLLECTION, ZDICTIONARYDATA) VALUES (99, 99, ?)",
        (b"garbage",),
    )
    conn.commit()
    conn.close()
    assert fcp.imported_clips() == {"lunch": 1, "balcony": 1}


def test_an_explicit_library_path_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path / "media"))
    elsewhere = tmp_path / "Somewhere.fcpbundle"
    elsewhere.mkdir(parents=True)
    make_event(elsewhere, "Grandparents", ["lunch"])
    monkeypatch.setenv("GREELS_FCP_LIBRARY", str(elsewhere))
    assert fcp.imported_clips() == {"lunch": 1}


def test_two_bundles_are_too_ambiguous_to_guess_between(library, tmp_path):
    (tmp_path / "Another.fcpbundle").mkdir()
    assert fcp.library_path() is None


@pytest.mark.parametrize(
    "name,expected",
    [("lunch", "lunch"), ("lunch - a1", "lunch"), ("lunch - a12", "lunch"),
     ("lunch - alpha", "lunch - alpha"), ("a1", "a1")],
)
def test_copy_suffix_stripping(name, expected):
    assert fcp.base_name(name) == expected
