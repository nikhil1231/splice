"""Relatedness between segments, and reel suggestions built on it.

Raw tag overlap is useless here: at 6.2 tags a segment the graph is 55% dense,
and the "closest" pairs are things that merely share person:nikhil, which sits
on 129 of 180 segments. Weighting each tag by rarity fixes that without any
model -- person:nikhil scores 0.33, object:coconut_scraper scores 5.19 -- and
turns 1,245 junk pairs into a few hundred real ones.

Tags alone are still not enough to say what a moment is *about*. The taxonomy
is 45 closed terms, and the things footage is actually about mostly are not in
it: "grandma feeding birds on the balcony" and "serving their favorite food to
a crow" are the same subject twice, and the only tags they share are
person:grandma and person:nikhil. Ranked on tags, the first one's neighbours
were five other things filmed on the balcony. The word "crow" is said eleven
times across the two of them and was never looked at. So relatedness reads the
prose and the speech as well -- see `Space.text_similarity`.

Everything here is computed from the SQLite index on demand. At this size that
is milliseconds, and it means nothing to invalidate.
"""

from __future__ import annotations

import itertools
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, index

# A reel wants enough material to be worth cutting but not a documentary.
REEL_MIN_SECONDS = 20.0
REEL_MAX_SECONDS = 100.0
REEL_MIN_CLIPS = 3
# Below this, a cluster is things that happen to share a background prop rather
# than a subject -- object:newspaper collecting every clip with a table in it.
REEL_MIN_COHERENCE = 0.15
SEGMENT_MIN_SECONDS = 3.0
# A single 113s moment is not a reel, it is a moment. It can still be cut down
# by hand, but a suggestion should not spend its whole budget on one.
SEGMENT_MAX_SECONDS = 60.0

# Namespaces that name a subject. A pair of these -- 1960s and marriage, both
# of them and television -- finds a sharper reel than either alone. A pair
# drawn from quality: or tone: does not: "quiet b-roll of daily life" describes
# how the footage looks, which is a mood board and not a reel with a point.
SUBJECT_NAMESPACES = ("person:", "theme:", "object:", "location:", "era:")
PAIR_MAX_SECONDS = 160.0

# The two cosines run over different vocabularies and do not land on the same
# scale: across all 17,287 cross-clip pairs the tag score's 90th percentile is
# 0.294 and the word score's is 0.060. Scaling a word score by that ratio --
# 4.9, call it 5 -- is what lets the two be compared at all. It is a unit
# conversion, not a claim that a word is worth five tags. Below about 3 the
# balcony beats the crow again; above about 8, two moments that merely both
# mention chapati outrank a real shared subject.
TEXT_WEIGHT = 5.0

# Three kinds of word that are never a subject: English function words, the
# names of the people who are in almost every frame (from the project, see
# `stopwords`), and the voice every summary is written in -- "Nikhil asks",
# "Grandma explains", "she recalls".
# Left in, those last ones cite `ask` and `explain` as the reason for a match
# 140 times across the archive, which says only that someone was interviewed.
#
# The list is by hand because it has to be: with 192 short documents, rarity
# cannot tell filler from subject. A document-frequency cutoff was tried first
# and was worse -- "each other" sits below any threshold that still keeps
# "chapati", and it pushed the crow out of the top six entirely.
STOPWORDS = frozenset("""
about after all also and any anything are away back because been before being bit
but came can come comes coming could did didn't does doing don't done down each
even every for from get gets getting give given giving goes going gone good got
had has have having her here hers herself him himself his how
i'll i'm into it's its itself just keep kept know knows let like little long look
looking looks made make makes making many may mean means might mine more most much
must myself never new next nor not now off oh okay once one only other
others our ours out over own put puts putting really right said same say saying
says see seeing seen she should show some something still such take taken takes
taking tell telling than that the their theirs them themselves then there these
they thing things think this those three through time together too two upon use
used very want wanted wants was way well went were what when where whether which
while who whom whose why will with within without would yeah yes yet you your
yours
actually again against almost already also although always another anyone
anyway both everyone everything maybe nothing perhaps probably someone
whatever whenever whoever
ask asked asking asks describe describes explain explained explaining explains
mention mentioned mentions recall recalled recalls remark remarks
""".split())


def stopwords() -> frozenset[str]:
    """The fixed list, plus the people in this project by id and by name."""
    names = set()
    for person in config.project().people:
        names.add(person.id.lower())
        names.update(WORD.findall(person.label.lower()))
    return STOPWORDS | names


# Two letters is never a subject, and a bare number is a timestamp or a count.
WORD = re.compile(r"[a-z']{3,}")

# A title is nine words that someone chose; a transcript is four hundred that
# nobody did. Without this the vector is whatever the conversation rambled
# about and the title barely registers.
FIELD_WEIGHTS = {"title": 3, "summary": 2, "english": 1}


@dataclass
class Segment:
    id: str
    stem: str
    start: float
    end: float
    title: str
    summary: str
    english: str = ""
    rating: str = "none"
    created: str = ""
    in_cuts: int = 0
    tags: set[str] = field(default_factory=set)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Reel:
    title: str
    tag: str
    segments: list[Segment]
    coherence: float
    score: float
    # The tag or tags that gathered this. `tag` is their joined form, which is
    # what `reels --tag` and `reel new --from` take.
    seeds: tuple[str, ...] = ()

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def clips(self) -> set[str]:
        return {s.stem for s in self.segments}


class Space:
    """Segments as IDF-weighted tag vectors."""

    def __init__(self, conn: sqlite3.Connection):
        self.segments: dict[str, Segment] = {}
        for row in conn.execute(
            "SELECT s.id, s.stem, s.start, s.end, s.title, s.summary, s.english, "
            "  s.rating, "
            "  a.created, "
            "  (SELECT count(*) FROM reel_shots r WHERE r.segment_id = s.id) AS in_cuts "
            "FROM segments s JOIN assets a ON a.stem = s.stem"
        ):
            self.segments[row["id"]] = Segment(
                id=row["id"], stem=row["stem"], start=row["start"], end=row["end"],
                title=row["title"] or "", summary=row["summary"] or "",
                english=row["english"] or "",
                rating=row["rating"] or "none", created=row["created"] or "",
                in_cuts=row["in_cuts"] or 0,
            )
        for row in conn.execute("SELECT segment_id, tag FROM segment_tags"):
            segment = self.segments.get(row["segment_id"])
            if segment:
                segment.tags.add(row["tag"])

        self.labels = {
            row["tag"]: row["label"] for row in conn.execute("SELECT tag, label FROM tags")
        }

        total = len(self.segments) or 1
        counts: dict[str, int] = {}
        for segment in self.segments.values():
            for tag in segment.tags:
                counts[tag] = counts.get(tag, 0) + 1
        self.doc_freq = counts
        self.idf = {tag: math.log(total / n) for tag, n in counts.items()}

        self._norm = {
            sid: math.sqrt(sum(self.idf[t] ** 2 for t in s.tags)) or 1.0
            for sid, s in self.segments.items()
        }
        self._text: dict[str, dict[str, float]] | None = None

    @classmethod
    def load(cls, db: Path | None = None) -> "Space":
        conn = index.connect(db)
        try:
            return cls(conn)
        finally:
            conn.close()

    def similarity(self, a: str, b: str, without: str | Iterable[str] | None = None) -> float:
        first, second = self.segments.get(a), self.segments.get(b)
        if not first or not second:
            return 0.0
        drop = {without} if isinstance(without, str) else set(without or ())
        left = first.tags - drop
        right = second.tags - drop
        shared = left & right
        if not shared:
            return 0.0
        norm_a = math.sqrt(sum(self.idf[t] ** 2 for t in left))
        norm_b = math.sqrt(sum(self.idf[t] ** 2 for t in right))
        if not norm_a or not norm_b:
            return 0.0
        return sum(self.idf[t] ** 2 for t in shared) / (norm_a * norm_b)

    # --- words ---------------------------------------------------------

    def _build_text(self) -> None:
        """Segments as IDF-weighted word vectors, same shape as the tag ones.

        Built on first use rather than in __init__: reel suggestions never ask
        for it, and every one of them loads a Space.
        """
        stop = stopwords()
        counted = {
            sid: Counter(
                word
                for field, weight in FIELD_WEIGHTS.items()
                for word in WORD.findall(getattr(s, field).lower()) * weight
                if word not in stop
            )
            for sid, s in self.segments.items()
        }
        vocabulary = Counter()
        for words in counted.values():
            vocabulary.update(words)

        def singular(word: str) -> str:
            """crows -> crow, but only where this archive says crow is a word.

            The two clips this was written for say "crows" in one and "crow" in
            the other, and matched on neither. Guessing at English morphology
            would fold "gas" to "ga"; asking the vocabulary costs nothing and
            cannot invent a word that is not already in the footage.
            """
            if not word.endswith("s"):
                return word
            for stem in (word[:-2], word[:-1]) if word.endswith("es") else (word[:-1],):
                if stem and vocabulary.get(stem, 0) >= 2:
                    return stem
            return word

        # Summed, not rebuilt from a dict comprehension: a segment that says
        # both "crow" and "crows" folds two entries onto one word, and the
        # comprehension quietly kept whichever came last.
        folded: dict[str, Counter] = {}
        for sid, words in counted.items():
            merged: Counter = Counter()
            for word, n in words.items():
                merged[singular(word)] += n
            folded[sid] = merged
        total = len(folded) or 1
        doc_freq = Counter()
        for words in folded.values():
            doc_freq.update(words.keys())

        text = {}
        for sid, words in folded.items():
            # A word said in exactly one segment can never match anything, and
            # keeping it only inflates the norm it is divided by.
            weighted = {
                w: (1 + math.log(n)) * math.log(total / doc_freq[w])
                for w, n in words.items() if doc_freq[w] > 1
            }
            norm = math.sqrt(sum(x * x for x in weighted.values())) or 1.0
            text[sid] = {w: x / norm for w, x in weighted.items()}
        self._text = text

    def _words(self) -> dict[str, dict[str, float]]:
        if self._text is None:
            self._build_text()
        assert self._text is not None
        return self._text

    def text_similarity(self, a: str, b: str) -> float:
        """How much the prose and the speech of two moments have in common."""
        vectors = self._words()
        first, second = vectors.get(a, {}), vectors.get(b, {})
        if len(second) < len(first):
            first, second = second, first
        return sum(x * second[w] for w, x in first.items() if w in second)

    def shared_words(self, a: str, b: str, limit: int = 3) -> list[str]:
        """The words that carried a text match, strongest first."""
        vectors = self._words()
        first, second = vectors.get(a, {}), vectors.get(b, {})
        ranked = sorted(
            ((x * second[w], w) for w, x in first.items() if w in second), reverse=True
        )
        return [w for _, w in ranked[:limit]]

    def related(
        self, segment_id: str, limit: int = 8, cross_clip: bool = True
    ) -> list[tuple[float, Segment, list[str]]]:
        """Nearest segments, with the tags and words that drove the match.

        Tags and words both, because either alone gets it wrong: the taxonomy
        has no word for a crow, and the speech has no word for the fact that
        two people are the same two people.
        """
        source = self.segments[segment_id]
        out = []
        for other in self.segments.values():
            if other.id == segment_id:
                continue
            if cross_clip and other.stem == source.stem:
                continue
            # Whichever signal is stronger, not their average. Averaging
            # halves a real match every time the other signal is silent, which
            # is most of the time -- it left the crow below four other things
            # filmed on the same balcony. Two moments are related if they share
            # a subject or if they share the words; they do not have to do both.
            by_tag = self.similarity(segment_id, other.id)
            by_word = TEXT_WEIGHT * self.text_similarity(segment_id, other.id)
            score = max(by_tag, by_word)

            if score <= 0:
                continue
            # Led by whichever signal won, because that is the answer to the
            # question the list is really asking. When the words found the
            # crow, opening with person:grandma reads like the reason, and it
            # is on 129 segments and is the reason for nothing.
            tags = sorted(source.tags & other.tags, key=lambda t: -self.idf[t])
            words = self.shared_words(segment_id, other.id)
            shared = words + tags if by_word > by_tag else tags + words
            out.append((score, other, shared[:4]))
        out.sort(key=lambda item: -item[0])
        # Ranked on the raw score and reported capped, so the number stays
        # readable as a strength between 0 and 1 without the cap deciding the
        # order. 167 of those 17,287 pairs come out above 1, and every one is
        # the same moment twice -- a story retold, a shot filmed again.
        return [(min(1.0, score), other, shared) for score, other, shared in out[:limit]]

    def find(self, query: str) -> list[Segment]:
        """Resolve a segment id, or a substring of a title."""
        if query in self.segments:
            return [self.segments[query]]
        needle = query.lower()
        return [
            s for s in self.segments.values()
            if needle in s.id.lower() or needle in s.title.lower()
        ]

    # --- reels ---------------------------------------------------------

    def subject_rate(self, tag: str, segments: list[Segment]) -> float:
        """How often a prop is actually talked about rather than just present.

        Coherence alone could not tell object:router from object:newspaper --
        0.26 against 0.27 -- yet the router is what those segments are about
        and the newspaper is lying on the table in the background. The give-away
        is language: four of five router segments name it, none of the newspaper
        ones do.

        Only applied to object: tags. A tone: or theme: segment has no reason to
        say "quiet" or "characteristic" out loud, so the same test would punish
        perfectly good clusters.
        """
        if not tag.startswith("object:") or not segments:
            return 1.0
        stems = [word[:5] for word in tag.split(":", 1)[1].split("_") if len(word) >= 4]
        if not stems:
            return 1.0
        hits = sum(
            any(stem in f"{s.title} {s.summary}".lower() for stem in stems) for s in segments
        )
        return hits / len(segments)

    def coherence(
        self, segments: list[Segment], without: str | Iterable[str] | None = None
    ) -> float:
        """How alike these are *ignoring the tag that gathered them*.

        Including the seed tag makes every cluster look coherent by
        construction, which is how object:newspaper -- six clips that merely
        have a newspaper lying on the table -- came out top-ranked.
        """
        if len(segments) < 2:
            return 0.0
        scores = [
            self.similarity(a.id, b.id, without=without)
            for i, a in enumerate(segments)
            for b in segments[i + 1:]
        ]
        return sum(scores) / len(scores)

    def reels(self, limit: int = 12) -> list[Reel]:
        """Tags that gather enough coherent material from enough clips.

        Coherence is what separates a subject from a backdrop: object:newspaper
        gathers seven segments across six clips, but they are only related by a
        newspaper lying on a table, and they do not resemble each other.

        Seeded by one tag and by pairs of subject tags. A pair is often the
        sharper reel -- "1960s and marriage" or "both of them and television"
        are subjects, where either tag alone is a heading.
        """
        candidates = [r for seed in self._seeds() if (r := self._cluster(seed))]
        candidates.sort(key=lambda r: -r.score)
        return self._dedupe(candidates, limit)

    def _seeds(self) -> list[tuple[str, ...]]:
        singles = [
            (tag,) for tag, count in self.doc_freq.items()
            if REEL_MIN_CLIPS <= count <= 20
        ]
        wide = sorted(
            tag for tag, count in self.doc_freq.items()
            if REEL_MIN_CLIPS <= count <= 40 and tag.startswith(SUBJECT_NAMESPACES)
        )
        pairs = [
            pair for pair in itertools.combinations(wide, 2)
            # Two people is a cast, not a subject; the rest of the namespaces
            # pair with themselves usefully (marriage and photos).
            if not all(t.startswith("person:") for t in pair)
        ]
        return singles + pairs

    def _cluster(self, seed: tuple[str, ...]) -> "Reel | None":
        members = [
            s for s in self.segments.values()
            if set(seed) <= s.tags
            and s.duration >= SEGMENT_MIN_SECONDS
            # You said no to this one. A suggestion that keeps proposing it
            # is asking you to make the same decision twice.
            and s.rating != "rejected"
        ]
        if len({s.stem for s in members}) < REEL_MIN_CLIPS:
            return None

        chosen = self._pick(members)
        clips = {s.stem for s in chosen}
        total = sum(s.duration for s in chosen)
        if len(clips) < REEL_MIN_CLIPS or total < REEL_MIN_SECONDS:
            return None
        # A pair gathers less, so it is allowed to gather it over more time --
        # the cut is where it gets shortened, not the suggestion.
        if len(seed) > 1 and total > PAIR_MAX_SECONDS:
            return None

        # Measured with every seed tag removed: including them makes any
        # cluster look coherent by construction.
        score_c = self.coherence(chosen, without=seed)
        if score_c < REEL_MIN_COHERENCE:
            return None
        subject = min(self.subject_rate(tag, chosen) for tag in seed)
        if subject < 0.5:
            return None  # a backdrop, not a subject

        rarity = sum(self.idf[tag] for tag in seed) / len(seed)
        return Reel(
            title=" · ".join(self.labels.get(tag, tag) for tag in seed),
            tag="+".join(seed),
            seeds=seed,
            # Proposed in the order the footage was taken. Alphabetical by
            # filename is not an order anything happened in.
            segments=sorted(chosen, key=lambda s: (s.created, s.stem, s.start)),
            coherence=score_c,
            # Rarity says the seed is specific; spread across clips is the
            # thing folders could never show you.
            score=score_c * rarity * subject * math.log(1 + len(clips)),
        )

    def _pick(self, members: list[Segment]) -> list[Segment]:
        """Fill a reel-length budget with the strongest material.

        Order of preference: what you marked a favourite, then what the model
        called a reel candidate, then rarity. A moment already in a cut goes
        last -- it is not disqualified, but a suggestion that only proposes
        what you have already used is telling you nothing.
        """
        ordered = sorted(
            members,
            key=lambda s: (
                s.rating != "favorite",
                "quality:reel_candidate" not in s.tags,
                s.in_cuts > 0,
                -sum(self.idf[t] for t in s.tags),
            ),
        )
        chosen: list[Segment] = []
        total = 0.0
        for segment in ordered:
            if segment.duration > SEGMENT_MAX_SECONDS and len(members) > 2:
                continue
            if total + segment.duration > REEL_MAX_SECONDS and chosen:
                continue
            chosen.append(segment)
            total += segment.duration
        return chosen

    def _dedupe(self, reels: list[Reel], limit: int) -> list[Reel]:
        """Drop a reel that is mostly another, already-stronger one."""
        kept: list[Reel] = []
        for reel in reels:
            ids = {s.id for s in reel.segments}
            if any(len(ids & {s.id for s in k.segments}) >= 0.6 * len(ids) for k in kept):
                continue
            kept.append(reel)
            if len(kept) >= limit:
                break
        return kept
