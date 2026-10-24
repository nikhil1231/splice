"""Index rebuild and concept search."""

import pytest

from greels import config, index, library as library_module
from greels.schema import Asset, Line, Segment, Sidecar
from greels.taxonomy import Taxonomy, normalise


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    asset = Asset(file="a.MOV", stem="a", duration=40, fps="30", width=1080, height=1920)
    Sidecar(
        asset=asset,
        segments=[
            Segment(
                id="a#0", start=0, end=27, title="Chat about a traveling videographer",
                summary="Grandma asks whether he is married.",
                english="Is he married? No, he isn't married yet.",
                tags=["person:grandma", "theme:marriage"],
                proposed_tags=["theme:llamas"],
                lines=[Line(start=0, end=2, kok="Lagn zalato?", en="Is he married?")],
            ),
            Segment(id="a#27", start=27, end=40, title="Coconut scraper",
                    tags=["object:coconut_scraper"], rating="favorite", status="used"),
        ],
    ).save(config.sidecar_path("a"))
    index.build()
    return tmp_path


def test_rebuild_counts(library):
    assert index.build() == (1, 2)


def test_search_reaches_a_concept_through_its_tag(library):
    # The speech only ever says "married"; the tag carries "marriage".
    titles = [row["title"] for row in index.search("marriage")]
    assert titles == ["Chat about a traveling videographer"]


def test_search_stems_word_forms(library):
    assert index.search("travelling") or index.search("traveling")


def test_search_by_tag(library):
    assert [r["title"] for r in index.search(tags=["object:coconut_scraper"])] == ["Coconut scraper"]


def test_search_by_status(library):
    assert [r["title"] for r in index.search(status="used")] == ["Coconut scraper"]


def test_proposed_tags_are_recorded_but_flagged(library):
    conn = index.connect()
    rows = dict(conn.execute("SELECT tag, proposed FROM tags").fetchall())
    assert rows["theme:llamas"] == 1
    assert rows["theme:marriage"] == 0


def test_lines_survive_the_round_trip(library):
    conn = index.connect()
    assert conn.execute("SELECT kok FROM lines").fetchone()[0] == "Lagn zalato?"


def test_a_cut_survives_a_rebuild(library):
    """The reel files are the source of truth, so a rebuild re-reads them.

    These rows used to be the one thing `build` had to carry across, which made
    SQLite a second writable store for exactly one kind of row.
    """
    from greels import reel as store

    store.create("What things used to cost", shots=[
        store.Shot(stem="a", start=0.0, end=27.0, segment_id="a#0"),
    ])

    index.build()
    conn = index.connect()
    assert conn.execute("SELECT title FROM reels").fetchone()[0] == "What things used to cost"
    assert conn.execute("SELECT stem FROM reel_shots").fetchone()[0] == "a"


# --- vocabulary --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("Theme: Old Photos", "theme:old_photos"), ("person:Grandpa", "person:grandpa"),
     ("  FUNNY ", "funny"), ("", ""), (":", "")],
)
def test_tag_normalisation(raw, expected):
    assert normalise(raw) == expected


def test_promote_moves_a_tag_into_the_vocabulary(tmp_path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text("namespaces: [theme]\ntags:\n  theme:money: {fcp_label: Money}\n")
    taxonomy = Taxonomy.load(path)
    assert not taxonomy.known("theme:exams")

    taxonomy.promote("theme:exams", path=path)
    assert taxonomy.known("theme:exams")
    assert Taxonomy.load(path).label("theme:exams") == "Exams"


def test_promote_rejects_a_tag_without_a_namespace(tmp_path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text("namespaces: []\ntags: {}\n")
    with pytest.raises(ValueError):
        Taxonomy.load(path).promote("exams", path=path)


def test_alias_folds_a_near_duplicate_instead_of_fragmenting(tmp_path):
    """theme:historical alongside tone:historical is the exact split the
    quarantine exists to catch; it should merge, not be promoted."""
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        "namespaces: [tone]\ntags:\n  tone:historical: {fcp_label: Historical}\n"
    )
    taxonomy = Taxonomy.load(path)
    taxonomy.alias("theme:historical", "tone:historical", path=path)

    known, proposed = Taxonomy.load(path).split(["theme:historical"])
    assert known == ["tone:historical"]
    assert proposed == []


def test_alias_target_must_already_exist(tmp_path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text("namespaces: []\ntags: {}\n")
    with pytest.raises(ValueError, match="not in the vocabulary"):
        Taxonomy.load(path).alias("theme:history", "tone:historical", path=path)


def test_a_real_tag_cannot_be_aliased_away(tmp_path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        "namespaces: [tone, theme]\ntags:\n"
        "  tone:historical: {fcp_label: Historical}\n"
        "  theme:money: {fcp_label: Money}\n"
    )
    with pytest.raises(ValueError, match="already a real tag"):
        Taxonomy.load(path).alias("theme:money", "tone:historical", path=path)


def test_aliasing_does_not_duplicate_an_existing_tag(tmp_path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        "namespaces: [tone]\ntags:\n  tone:historical: {fcp_label: Historical}\n"
        "aliases:\n  theme:historical: tone:historical\n"
    )
    known, _ = Taxonomy.load(path).split(["tone:historical", "theme:historical"])
    assert known == ["tone:historical"]


def test_label_falls_back_for_a_tag_outside_the_vocabulary(tmp_path):
    """`greels tags` lists tags in use as well as tags defined, so it must be
    able to name one that has not been promoted yet."""
    path = tmp_path / "taxonomy.yaml"
    path.write_text("namespaces: [object]\ntags: {}\n")
    assert Taxonomy.load(path).label("object:digital_diary") == "Digital Diary"


# --- which end to start from -----------------------------------------------


@pytest.fixture
def three_clips(tmp_path, monkeypatch):
    """Three clips whose alphabetical order is the reverse of their shot order,
    and whose best material was shot last."""
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    for stem, created, tags in (
        ("a_broll", "2026-01-03", ["quality:broll", "quality:silent"]),
        ("b_talk", "2026-01-02", ["tone:funny"]),
        ("c_gold", "2026-01-01", ["quality:reel_candidate"]),
    ):
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=30, fps="30",
                        created=created),
            segments=[Segment(id=f"{stem}#0", start=0, end=10, title=stem, tags=tags)],
        ).save(config.sidecar_path(stem))
    index.build()
    return tmp_path


def _stems(**kwargs):
    return [row["stem"] for row in index.search(limit=10, **kwargs)]


def test_by_clip_is_alphabetical_which_is_an_order_nothing_happened_in(three_clips):
    assert _stems(order="clip") == ["a_broll", "b_talk", "c_gold"]


def test_by_shot_is_the_afternoon_as_it_happened(three_clips):
    assert _stems(order="shot") == ["c_gold", "b_talk", "a_broll"]


def test_promise_puts_what_the_model_thought_worth_using_first(three_clips):
    """The tone and quality tags are the only opinion in the archive about
    which moments are good; a pass that ignores them opens with b-roll."""
    assert _stems(order="promise") == ["c_gold", "b_talk", "a_broll"]


def test_promise_beats_the_shot_order_it_falls_back_to(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    for stem, created, tags in (
        ("early_broll", "2026-01-01", ["quality:broll"]),
        ("late_gold", "2026-01-09", ["quality:reel_candidate"]),
    ):
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=30, fps="30",
                        created=created),
            segments=[Segment(id=f"{stem}#0", start=0, end=10, title=stem, tags=tags)],
        ).save(config.sidecar_path(stem))
    index.build()
    assert _stems(order="shot") == ["early_broll", "late_gold"]
    assert _stems(order="promise") == ["late_gold", "early_broll"]


def test_an_unknown_order_is_refused_rather_than_silently_alphabetical(three_clips):
    with pytest.raises(ValueError):
        index.search(order="whatever")


# --- finding the moment somebody says a thing ------------------------------


@pytest.fixture
def interview(tmp_path, monkeypatch):
    """One long clip, the shape the two real interviews are: a couple of
    two-minute segments with a dozen utterances inside each."""
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    Sidecar(
        asset=Asset(file="i.MOV", stem="i", duration=1200, fps="30"),
        segments=[
            Segment(id="i#00320.00", start=320, end=428, title="The train ticket",
                    english="For a student, 14 rupees. For 7 rupees I went to Bombay.",
                    lines=[
                        Line(start=326, end=330, en="For a student, 14 rupees."),
                        Line(start=335, end=341, en="For 7 rupees I went to Bombay.",
                             kok="Satt rupya, Bombay gelo."),
                    ]),
            Segment(id="i#00726.00", start=726, end=840, title="Pigmy deposits",
                    english="Daily, 5 rupees, 1 rupee, like that.",
                    lines=[Line(start=735, end=739, en="Daily, 5 rupees, like that.")]),
        ],
    ).save(config.sidecar_path("i"))
    index.build()
    return tmp_path


def test_a_line_hit_says_when_not_only_where(interview):
    """A hit on the segment says 'somewhere in the next two minutes'."""
    hit, = index.search_lines("Bombay")
    assert hit["start"] == 335
    assert hit["stem"] == "i"
    assert hit["segment_id"] == "i#00320.00"
    assert hit["title"] == "The train ticket"


def test_the_konkani_is_searchable_too(interview):
    assert index.search_lines("gelo")[0]["en"] == "For 7 rupees I went to Bombay."


def test_a_common_word_comes_back_best_match_first(interview):
    """With 1458 lines a phrase turns up a dozen times; clip order is no use."""
    hits = index.search_lines("rupees")
    assert len(hits) == 3
    assert hits[0]["en"] != hits[1]["en"]


def test_a_phrase_nobody_says_finds_nothing_rather_than_everything(interview):
    assert index.search_lines("helicopter") == []


def test_a_search_finds_a_moment_by_something_said_in_it(interview):
    """The browser's Said-out-loud tab is gone, so the one search has to reach
    the transcript. Only the line carries the Konkani here -- the segment's
    own prose is English -- and searching it used to find nothing."""
    assert [row["id"] for row in index.search("gelo")] == ["i#00320.00"]


def test_a_moment_carries_the_lines_that_matched(interview):
    """A moment averages 28 seconds and this one runs 108, so "it is in here
    somewhere" is not the answer. The row says when."""
    row, = index.search("Bombay")
    assert [(l["start"], l["en"]) for l in row["said"]] == [
        (335, "For 7 rupees I went to Bombay.")
    ]


def test_the_lines_that_matched_come_back_in_the_order_they_were_said(interview):
    """Within one moment these read as a fragment of the transcript, and three
    sentences shuffled by relevance read as three unrelated ones."""
    row = next(r for r in index.search("rupees") if r["id"] == "i#00320.00")
    assert [l["start"] for l in row["said"]] == [326, 335]


def test_a_moment_reached_through_its_tag_quotes_nothing(library):
    """The word reaches this through `theme:marriage`; nobody says it aloud,
    and quoting a line under the row would be a lie about why it is there."""
    row, = index.search("marriage")
    assert row["said"] == []


def test_the_line_index_is_rebuilt_with_everything_else(interview):
    """It is derived like the rest; a stale one would point at old timings."""
    sidecar = library_module.load_sidecar("i")
    sidecar.segments[0].lines[1].en = "For 7 rupees I went to Panjim."
    sidecar.segments[0].lines[1].kok = "Satt rupya, Panjim gelo."
    sidecar.save(config.sidecar_path("i"))
    index.build()
    assert index.search_lines("Bombay") == []
    assert index.search_lines("Panjim")[0]["start"] == 335


# --- rebuilt without a gap -------------------------------------------------


def test_a_rebuild_never_leaves_the_index_missing(library):
    """Every write from the browser rebuilds this, and a search arriving in
    another tab while the old file was gone found no database at all."""
    import threading

    from greels import config

    seen = []
    stop = threading.Event()

    def read():
        while not stop.is_set():
            seen.append(config.db_path().exists())

    watcher = threading.Thread(target=read, daemon=True)
    watcher.start()
    for _ in range(8):
        index.build()
    stop.set()
    watcher.join(2)

    assert seen and all(seen)


def test_a_rebuild_leaves_nothing_beside_the_index(library):
    """A second file that has to arrive at the same instant as the first is a
    second file that can be read against the wrong one."""
    from greels import config

    index.build()
    beside = [
        p.name for p in config.index_dir().iterdir()
        if p.name.startswith("library.sqlite") and p.name != "library.sqlite"
    ]
    assert beside == []


def test_a_stale_write_ahead_log_is_cleared_out(library):
    """Left by the versions of this that ran in WAL mode."""
    from greels import config

    (config.index_dir() / "library.sqlite-wal").write_bytes(b"from before")
    index.build()
    assert not (config.index_dir() / "library.sqlite-wal").exists()
