"""Build the queryable index from the sidecars.

SQLite is derived and disposable: every command rebuilds it from
`index/*.seg.json`, which stays the source of truth. That is what lets you
hand-correct a sidecar without fighting a second writable store, and it means
a schema change here is a rebuild rather than a migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config, library

# No WAL: nothing writes to this file incrementally. It is built whole and
# moved into place, and a write-ahead log would only be a second file that has
# to arrive at the same instant as the first.
SCHEMA = """
CREATE TABLE assets (
    stem        TEXT PRIMARY KEY,
    file        TEXT NOT NULL,
    duration    REAL NOT NULL,
    fps         TEXT NOT NULL,
    width       INTEGER, height INTEGER,
    has_audio   INTEGER,
    created     TEXT,
    size_bytes  INTEGER
);

CREATE TABLE segments (
    id          TEXT PRIMARY KEY,
    stem        TEXT NOT NULL REFERENCES assets(stem),
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    duration    REAL NOT NULL,
    title       TEXT,
    summary     TEXT,
    konkani     TEXT,
    english     TEXT,
    rating      TEXT,
    status      TEXT,
    -- Reserved so semantic search is an INSERT later, not a migration.
    embedding   BLOB
);
CREATE INDEX segments_stem ON segments(stem);
CREATE INDEX segments_status ON segments(status);

CREATE TABLE tags (
    tag         TEXT PRIMARY KEY,
    namespace   TEXT,
    value       TEXT,
    label       TEXT,
    proposed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE segment_tags (
    segment_id  TEXT NOT NULL REFERENCES segments(id),
    tag         TEXT NOT NULL REFERENCES tags(tag),
    PRIMARY KEY (segment_id, tag)
);

CREATE TABLE lines (
    segment_id  TEXT NOT NULL REFERENCES segments(id),
    seq         INTEGER NOT NULL,
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    kok         TEXT,
    en          TEXT,
    PRIMARY KEY (segment_id, seq)
);

-- The cuts, read from index/reels/*.reel.json. Derived like everything else
-- here: these used to be the one thing a rebuild had to carry across, which
-- made SQLite a second writable store for exactly one kind of row.
CREATE TABLE reels (
    slug        TEXT PRIMARY KEY,
    title       TEXT,
    status      TEXT,
    hook        TEXT,
    notes       TEXT,
    duration    REAL NOT NULL DEFAULT 0,
    updated_at  TEXT
);
CREATE TABLE reel_shots (
    reel_slug   TEXT NOT NULL REFERENCES reels(slug),
    position    INTEGER NOT NULL,
    stem        TEXT NOT NULL,
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    -- Where the shot came from. A cut does not depend on it: re-analysis
    -- renumbers segment ids, and a reel must survive that.
    segment_id  TEXT,
    note        TEXT,
    PRIMARY KEY (reel_slug, position)
);
CREATE INDEX reel_shots_segment ON reel_shots(segment_id);
CREATE INDEX reel_shots_stem ON reel_shots(stem);

-- Standalone rather than external-content, so tag text can be indexed
-- alongside the prose: searching "marriage" should reach a segment whose
-- speech only ever says "married", via its theme:marriage tag. The porter
-- stemmer covers the easier plural/tense cases.
CREATE VIRTUAL TABLE segments_fts USING fts5(
    segment_id UNINDEXED, title, summary, english, konkani, tags,
    tokenize = 'porter unicode61'
);

-- The same speech again, one utterance at a time. A segment is 28 seconds on
-- average and one of them is 108, so a hit on a segment says "somewhere in
-- the next two minutes he says this". A hit on a line says when.
CREATE VIRTUAL TABLE lines_fts USING fts5(
    segment_id UNINDEXED, seq UNINDEXED, en, kok,
    tokenize = 'porter unicode61'
);
"""


def _clear(db: Path) -> None:
    for path in (db, db.with_name(db.name + "-journal")):
        if path.exists():
            path.unlink()


def build(db: Path | None = None) -> tuple[int, int]:
    """Rebuild the index. Returns (assets, segments).

    Built beside the live file and moved over it, rather than deleted and
    written in place. Every write from the browser rebuilds this, and a read
    arriving while the old file was gone -- a search in one tab while a
    verdict lands in another -- found no database at all. The move is atomic,
    so a reader sees one index or the other and never the gap between them.
    """
    from .taxonomy import Taxonomy

    db = db or config.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    building = db.with_name(db.name + ".building")
    _clear(building)

    taxonomy = Taxonomy.load()
    conn = sqlite3.connect(building)
    conn.executescript(SCHEMA)

    sidecars = library.load_all()
    n_segments = 0
    seen_tags: set[str] = set()

    for sc in sidecars:
        conn.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
            (sc.asset.stem, sc.asset.file, sc.asset.duration, sc.asset.fps,
             sc.asset.width, sc.asset.height, int(sc.asset.has_audio),
             sc.asset.created, sc.asset.size_bytes),
        )
        for seg in sc.segments:
            conn.execute(
                "INSERT INTO segments (id,stem,start,end,duration,title,summary,"
                "konkani,english,rating,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (seg.id, sc.asset.stem, seg.start, seg.end, seg.duration, seg.title,
                 seg.summary, seg.konkani, seg.english, seg.rating, seg.status),
            )
            n_segments += 1

            for tag, proposed in [(t, 0) for t in seg.tags] + [(t, 1) for t in seg.proposed_tags]:
                if tag not in seen_tags:
                    ns, _, value = tag.partition(":")
                    conn.execute(
                        "INSERT INTO tags VALUES (?,?,?,?,?)",
                        (tag, ns, value or ns, taxonomy.label(tag), proposed),
                    )
                    seen_tags.add(tag)
                conn.execute(
                    "INSERT OR IGNORE INTO segment_tags VALUES (?,?)", (seg.id, tag)
                )

            for i, line in enumerate(seg.lines):
                conn.execute(
                    "INSERT INTO lines VALUES (?,?,?,?,?,?)",
                    (seg.id, i, line.start, line.end, line.kok, line.en),
                )

    conn.execute(
        "INSERT INTO segments_fts (segment_id, title, summary, english, konkani, tags) "
        "SELECT s.id, s.title, s.summary, s.english, s.konkani, "
        "  coalesce((SELECT group_concat(replace(t.value,'_',' ') || ' ' || t.label, ' ') "
        "            FROM segment_tags st JOIN tags t ON t.tag = st.tag "
        "            WHERE st.segment_id = s.id), '') "
        "FROM segments s"
    )
    conn.execute(
        "INSERT INTO lines_fts (segment_id, seq, en, kok) "
        "SELECT segment_id, seq, en, kok FROM lines"
    )
    _write_reels(conn)
    conn.commit()
    conn.close()

    building.replace(db)
    # Left by the versions of this that ran in WAL mode. A reader that opened
    # the new file next to an old log would be reading two different indexes.
    for suffix in ("-wal", "-shm"):
        stale = db.with_name(db.name + suffix)
        if stale.exists():
            stale.unlink()
    return len(sidecars), n_segments


def _write_reels(conn: sqlite3.Connection) -> None:
    """Index the cuts so a segment can say which reels already use it."""
    from . import reel as reels

    for reel in reels.load_all():
        conn.execute(
            "INSERT INTO reels VALUES (?,?,?,?,?,?,?)",
            (reel.slug, reel.title, reel.status, reel.hook, reel.notes,
             reel.duration, reel.updated_at),
        )
        for position, shot in enumerate(reel.shots):
            conn.execute(
                "INSERT INTO reel_shots VALUES (?,?,?,?,?,?,?)",
                (reel.slug, position, shot.stem, shot.start, shot.end,
                 shot.segment_id, shot.note),
            )


def connect(db: Path | None = None) -> sqlite3.Connection:
    db = db or config.db_path()
    if not db.exists():
        raise FileNotFoundError(f"no index at {db}; run `splice index` first")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


# How a set of moments comes back. Alphabetical by filename is the cheapest
# order and the only one that corresponds to nothing: `shot` is the afternoon
# as it happened, and `promise` puts the model's own opinion of what is worth
# using at the top -- which is what turns 192 moments into a shortlist you can
# get through before you lose interest.
ORDERS = {
    "clip": "s.stem, s.start",
    "shot": "COALESCE(a.created, 'zzzz'), s.stem, s.start",
    "promise": (
        "(SELECT 1 FROM segment_tags t WHERE t.segment_id = s.id "
        "AND t.tag = 'quality:reel_candidate') IS NULL, "
        "(SELECT 1 FROM segment_tags t WHERE t.segment_id = s.id "
        "AND t.tag LIKE 'tone:%') IS NULL, "
        "COALESCE(a.created, 'zzzz'), s.stem, s.start"
    ),
}


def search_lines(query: str, limit: int = 30, db: Path | None = None) -> list[dict]:
    """Individual utterances matching a query, in relevance order.

    The archive's own unit is `video + start + end + meaning`, and a line is
    the smallest one it has. Finding the moment somebody says a thing is the
    question you ask while assembling a cut -- "start where he says *sixty,
    seventy years ago*" -- and answering it to the nearest 108-second segment
    is not answering it.

    Ordered by relevance rather than by clip: with 1458 lines a phrase can
    turn up a dozen times, and the best match first is the only order worth
    having.
    """
    conn = connect(db)
    rows = [
        dict(r) for r in conn.execute(
            "SELECT l.segment_id, l.seq, l.start, l.end, l.en, l.kok, "
            "       s.stem, s.title, s.rating, a.duration AS clip_duration, "
            "       a.created AS shot_at "
            "FROM lines_fts f "
            "JOIN lines l ON l.segment_id = f.segment_id AND l.seq = f.seq "
            "JOIN segments s ON s.id = l.segment_id "
            "JOIN assets a ON a.stem = s.stem "
            "WHERE lines_fts MATCH ? ORDER BY f.rank LIMIT ?",
            (query, limit),
        )
    ]
    conn.close()
    return rows


def search(
    query: str = "",
    tags: list[str] | None = None,
    status: str | None = None,
    in_cut: bool | None = None,
    limit: int = 30,
    order: str = "clip",
    db: Path | None = None,
) -> list[dict]:
    conn = connect(db)
    where, params = [], []

    if query:
        # The transcript as well as the prose. A moment's `english` is a
        # translation of the whole of it, so most phrases are in there
        # somewhere -- but prose cannot say *when*, and the browser's
        # Said-out-loud list, which could, is gone. Matching the lines is what
        # lets a moment come back carrying the sentences that matched and the
        # second each one starts.
        where.append(
            "(s.id IN (SELECT segment_id FROM segments_fts WHERE segments_fts MATCH ?)"
            " OR s.id IN (SELECT segment_id FROM lines_fts WHERE lines_fts MATCH ?))"
        )
        params += [query, query]
    for tag in tags or []:
        where.append("s.id IN (SELECT segment_id FROM segment_tags WHERE tag = ?)")
        params.append(tag)
    if status:
        where.append("s.status = ?")
        params.append(status)
    if in_cut is not None:
        # "What good material have I not used yet?" is the question that stops
        # a moment being forgotten, and it is one join away.
        clause = "EXISTS" if in_cut else "NOT EXISTS"
        where.append(
            f"{clause} (SELECT 1 FROM reel_shots r WHERE r.segment_id = s.id)"
        )

    sql = (
        "SELECT s.*, (SELECT group_concat(tag, ' ') FROM segment_tags t "
        "WHERE t.segment_id = s.id) AS tags, "
        # Which cuts already use this moment. This is what reel_shots is
        # indexed by segment_id for: it turns "have I used this?" from one
        # request per row into part of the row.
        "(SELECT group_concat(DISTINCT r.reel_slug) FROM reel_shots r "
        "WHERE r.segment_id = s.id) AS reels, a.created AS shot_at "
        "FROM segments s JOIN assets a ON a.stem = s.stem"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    if order not in ORDERS:
        raise ValueError(f"order must be one of {', '.join(ORDERS)}")
    sql += f" ORDER BY {ORDERS[order]} LIMIT ?"
    params.append(limit)

    # Always present, so a caller can render "what matched" without asking
    # first whether anything was searched for.
    rows = [dict(r) | {"said": []} for r in conn.execute(sql, params)]
    if query:
        _attach_said(conn, rows, query)
    conn.close()
    return rows


def _attach_said(conn: sqlite3.Connection, rows: list[dict], query: str) -> None:
    """Hang the utterances that matched under the moments they were said in.

    In the order they were said, not in rank order: within one moment these
    are read as a fragment of the transcript, and three sentences shuffled by
    relevance read as three unrelated ones.

    A moment that matched only its prose or a tag comes back with nothing
    here, and that is the honest answer -- "married" reaches a moment through
    `theme:marriage` without anybody ever saying the word.
    """
    wanted = {row["id"]: row for row in rows}
    if not wanted:
        return
    for line in conn.execute(
        "SELECT l.segment_id, l.start, l.end, l.en, l.kok "
        "FROM lines_fts f JOIN lines l "
        "  ON l.segment_id = f.segment_id AND l.seq = f.seq "
        "WHERE lines_fts MATCH ? ORDER BY l.segment_id, l.start",
        (query,),
    ):
        row = wanted.get(line["segment_id"])
        if row is not None:
            row["said"].append(
                {key: line[key] for key in ("start", "end", "en", "kok")}
            )
