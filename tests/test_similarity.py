"""Relatedness and reel suggestions.

The failures these pin down are the ones the real archive produced: a tag
graph so dense it was meaningless, and a cluster built on a newspaper lying
on a table.
"""

import pytest

from greels import config, index, similarity
from greels.schema import Asset, Segment, Sidecar


def seg(sid, start, title, tags, summary="", end=None):
    return Segment(id=sid, start=start, end=end if end is not None else start + 30,
                   title=title, summary=summary, tags=tags)


@pytest.fixture
def space(tmp_path, monkeypatch):
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))

    def clip(stem, segments):
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=600, fps="30"),
            segments=segments,
        ).save(config.sidecar_path(stem))

    # person:nikhil is on everything, as it is in the real archive; the rare
    # tags are what should actually drive similarity.
    common = ["person:nikhil", "location:home"]
    clip("a", [
        seg("a#0", 0, "Grandma guesses it is a flour machine",
            common + ["object:router", "theme:technology"], "She holds the router."),
        seg("a#60", 60, "Lunch at the table", common + ["theme:food"]),
    ])
    clip("b", [
        seg("b#0", 0, "Inserting a SIM card into the router",
            common + ["object:router", "theme:technology"], "The router takes a SIM."),
    ])
    clip("c", [
        seg("c#0", 0, "Router lights on", common + ["object:router"], "The router blinks."),
    ])
    clip("d", [
        # Not location:home, so that tag is common but not universal -- a tag on
        # literally every segment has zero idf and is tested separately below.
        seg("d#0", 0, "Serving fodi and dal",
            ["person:nikhil", "location:kitchen", "theme:food", "theme:cooking"]),
    ])
    index.build()
    return similarity.Space.load()


def test_a_tag_on_every_segment_contributes_nothing(space):
    """person:nikhil is on 129 of 180 real segments. A tag on all of them
    carries no information, and two segments sharing only such tags are not
    related in any useful sense."""
    assert space.idf["person:nikhil"] == 0.0


def test_common_tags_are_worth_little_and_rare_tags_a_lot(space):
    assert space.idf["location:home"] < space.idf["object:router"]
    assert space.idf["theme:cooking"] > space.idf["location:home"]


def test_related_prefers_a_shared_rare_tag_over_shared_common_ones(space):
    hits = space.related("a#0")
    assert hits[0][1].id in {"b#0", "c#0"}
    assert "object:router" in hits[0][2]


def test_related_excludes_the_same_clip_by_default(space):
    assert all(s.stem != "a" for _, s, _ in space.related("a#0"))
    assert any(s.stem == "a" for _, s, _ in space.related("a#0", cross_clip=False))


def test_similarity_is_symmetric_and_bounded(space):
    for a in space.segments:
        for b in space.segments:
            score = space.similarity(a, b)
            assert 0.0 <= score <= 1.0 + 1e-9
            assert score == pytest.approx(space.similarity(b, a))


def test_similarity_can_ignore_a_tag(space):
    with_tag = space.similarity("a#0", "c#0")
    without = space.similarity("a#0", "c#0", without="object:router")
    assert without < with_tag


def test_find_resolves_an_id_or_a_title_fragment(space):
    assert [s.id for s in space.find("a#0")] == ["a#0"]
    assert [s.id for s in space.find("flour machine")] == ["a#0"]
    assert space.find("no such thing") == []


# --- the newspaper problem ---------------------------------------------


def test_subject_rate_separates_a_prop_from_a_backdrop(space):
    """object:router and object:newspaper scored 0.26 and 0.27 on coherence.
    What tells them apart is whether anyone talks about the thing."""
    about = [seg("x#0", 0, "Router lights on", ["object:router"], "The router blinks.")]
    behind = [
        seg("y#0", 0, "Gathering power adapters", ["object:newspaper"], "Picks up adapters."),
        seg("y#1", 30, "Looking for the AirPod", ["object:newspaper"], "Searches the sofa."),
    ]
    assert space.subject_rate("object:router", about) == 1.0
    assert space.subject_rate("object:newspaper", behind) == 0.0


def test_subject_rate_is_not_applied_to_abstract_tags(space):
    """A quiet clip has no reason to say "quiet", so the same test would
    wrongly discard a perfectly good B-roll cluster."""
    quiet = [seg("z#0", 0, "Grandma on the balcony", ["tone:quiet"], "Silent shot.")]
    assert space.subject_rate("tone:quiet", quiet) == 1.0
    assert space.subject_rate("theme:marriage", quiet) == 1.0


def test_reels_gather_material_from_several_clips(space):
    reels = space.reels()
    router = next((r for r in reels if r.tag == "object:router"), None)
    assert router is not None
    assert len(router.clips) >= 3
    assert router.duration >= similarity.REEL_MIN_SECONDS


def test_a_reel_never_exceeds_the_length_budget(space):
    for reel in space.reels():
        assert reel.duration <= similarity.REEL_MAX_SECONDS


def test_coherence_of_unrelated_segments_is_low(space):
    unrelated = [space.segments["a#60"], space.segments["c#0"]]
    assert space.coherence(unrelated) < space.coherence(
        [space.segments["a#0"], space.segments["b#0"]]
    )


# --- the tags listing --------------------------------------------------


def test_tag_usage_counts_segments_and_clips_separately(space):
    """A tag on six segments of one clip is a very different thing from one
    spread over six clips -- that spread is what reels are built on."""
    router = [s for s in space.segments.values() if "object:router" in s.tags]
    assert len(router) == 3
    assert len({s.stem for s in router}) == 3

    food = [s for s in space.segments.values() if "theme:food" in s.tags]
    assert len(food) == 2
    assert len({s.stem for s in food}) == 2


def test_every_used_tag_has_a_signal(space):
    for tag in space.doc_freq:
        assert tag in space.idf
        assert space.idf[tag] >= 0.0


# --- what a suggestion should already know -----------------------------


@pytest.fixture
def opinionated(tmp_path, monkeypatch):
    """A little archive where some verdicts have been given."""
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))

    def clip(stem, segments, created):
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=600, fps="30",
                        created=created),
            segments=segments,
        ).save(config.sidecar_path(stem))

    # theme:technology is what these have in common once the seed tag is taken
    # away, which is what coherence is measured on.
    common = ["person:nikhil", "theme:technology"]
    clip("late", [seg("late#0", 0, "Router lights on",
                      common + ["object:router"], "The router blinks.", end=10)],
         "2026-03-01T00:00:00Z")
    clip("early", [seg("early#0", 0, "Plugging in the router",
                       common + ["object:router"], "He plugs the router in.", end=10)],
         "2026-01-01T00:00:00Z")
    clip("middle", [
        seg("middle#0", 0, "Grandma blesses the router",
            common + ["object:router"], "She blesses the router.", end=10),
        seg("middle#100", 100, "A long look at the router",
            common + ["object:router"], "The router again.", end=280),
    ], "2026-02-01T00:00:00Z")
    clip("extra", [seg("extra#0", 0, "The router on the table",
                       common + ["object:router"], "The router sits there.", end=10)],
         "2026-02-15T00:00:00Z")
    # Something with none of those tags, so they are not universal and so
    # carry information.
    clip("elsewhere", [
        seg("elsewhere#0", 0, "Grandma cooking fish",
            ["person:grandma", "theme:cooking"], "Fish in the pan.", end=40),
        seg("elsewhere#60", 60, "Serving rice",
            ["person:grandma", "theme:food"], "Rice on a plate.", end=100),
    ], "2026-01-15T00:00:00Z")
    return lambda: (index.build(), similarity.Space.load())[1]


def _router(space):
    """The suggestion built from the router clips.

    Found by its material rather than its seed tag: several tags gather exactly
    the same segments here, and which of them ends up naming the cluster is
    beside the point.
    """
    return next(
        r for r in space.reels(limit=10)
        if r.clips & {"early", "middle", "late", "extra"} and not r.clips & {"elsewhere"}
    )


def test_a_suggestion_runs_in_the_order_the_footage_was_taken(opinionated):
    """Alphabetical by filename is not an order anything happened in."""
    space = opinionated()
    assert [s.stem for s in _router(space).segments] == [
        "early", "middle", "extra", "late"]


def test_a_moment_you_rejected_is_not_proposed_again(opinionated):
    """A suggestion that keeps offering it asks you to decide twice."""
    from greels import library

    sidecar = library.load_sidecar("middle")
    sidecar.segments[0].rating = "rejected"
    sidecar.save(config.sidecar_path("middle"))

    space = opinionated()
    assert "middle#0" not in {s.id for s in _router(space).segments}


def test_a_favourite_is_taken_before_anything_else(opinionated):
    from greels import library

    sidecar = library.load_sidecar("late")
    sidecar.segments[0].rating = "favorite"
    sidecar.save(config.sidecar_path("late"))

    space = opinionated()
    # The budget is not tight here, so what this pins is the picking order
    # rather than what survives it.
    picked = space._pick([s for s in space.segments.values() if "object:router" in s.tags])
    assert picked[0].id == "late#0"


def test_one_long_moment_does_not_become_the_whole_suggestion(opinionated):
    """A single 180s segment is a moment, not a reel."""
    space = opinionated()
    assert "middle#100" not in {s.id for s in _router(space).segments}


@pytest.fixture
def paired(tmp_path, monkeypatch):
    """An archive where a pair of tags names something neither tag does.

    theme:photos gathers the wedding photos and the holiday ones; era:1960s
    gathers the wedding photos and a story with no photos in it. Only together
    do they name "the wedding photographs".
    """
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))

    def clip(stem, segments):
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=600, fps="30",
                        created="2026-01-01T00:00:00Z"),
            segments=segments,
        ).save(config.sidecar_path(stem))

    # No single tag isolates the weddings, and the four of them do not have
    # identical tags -- otherwise removing both seeds would leave the cluster
    # looking perfectly coherent, which is the thing being guarded against.
    for n in range(4):
        clip(f"w{n}", [seg(f"w{n}#0", 0, f"A wedding photograph {n}",
                           ["theme:photos", "era:1960s",
                            "tone:sweet" if n < 2 else "tone:funny"], end=20)])
    for n in range(4):
        clip(f"h{n}", [seg(f"h{n}#0", 0, f"A holiday photograph {n}",
                           ["theme:photos", "location:goa"], end=20)])
    for n in range(4):
        clip(f"s{n}", [seg(f"s{n}#0", 0, f"A story from then {n}",
                           ["era:1960s", "theme:work"], end=20)])
    index.build()
    return similarity.Space.load()


def test_a_pair_of_subject_tags_can_seed_a_suggestion(paired):
    """Neither tag alone gathers only the wedding photographs."""
    paired_reel = next(
        r for r in paired.reels(limit=30) if r.seeds == ("era:1960s", "theme:photos")
    )
    assert {s.stem for s in paired_reel.segments} == {"w0", "w1", "w2", "w3"}


def test_a_pair_is_named_by_both_its_tags(paired):
    reel = next(r for r in paired.reels(limit=30) if len(r.seeds) == 2)
    assert reel.tag == "+".join(reel.seeds)
    assert " · " in reel.title


def test_coherence_for_a_pair_drops_both_seeds(paired):
    """Leaving either in makes the cluster look coherent by construction."""
    members = [s for s in paired.segments.values() if s.stem.startswith("w")]
    whole = paired.coherence(members)
    one_seed = paired.coherence(members, without={"era:1960s"})
    both_seeds = paired.coherence(members, without={"era:1960s", "theme:photos"})
    assert both_seeds < one_seed < whole


def test_two_people_are_a_cast_not_a_subject(paired):
    for reel in paired.reels(limit=40):
        assert not all(t.startswith("person:") for t in reel.seeds) or len(reel.seeds) == 1


# --- words ---------------------------------------------------------------
#
# The failure these pin down is the one the real archive produced: two moments
# that are plainly the same subject, sharing nothing but the people in them.


@pytest.fixture
def spoken(tmp_path, monkeypatch):
    """The crow, reduced. Two moments are plainly about feeding a crow, and the
    taxonomy has no word for a crow: on tags they share only the people who are
    in everything, and the three other things shot on the balcony beat them."""
    monkeypatch.setenv("GREELS_ROOT", str(tmp_path))
    both = ["person:nikhil", "person:grandma"]

    def clip(stem, segments):
        Sidecar(
            asset=Asset(file=f"{stem}.MOV", stem=stem, duration=600, fps="30"),
            segments=segments,
        ).save(config.sidecar_path(stem))

    def spoken_seg(sid, start, title, tags, summary, english):
        s = seg(sid, start, title, tags, summary)
        s.english = english
        return s

    balcony = ["location:balcony", "theme:daily_life"]
    # The proportions of the real clip: the crows are said eleven times and
    # the chapati twice, so a moment that only shares the chapati must not win.
    clip("birds", [spoken_seg(
        "birds#0", 0, "Grandma feeds the crows", both + balcony,
        "She brings chapati out to the crows on the balcony.",
        "I'll give it to the crows. The crows fight. Two crows ran away. "
        "See, that crow came. The crows eat and leave. Other crows come. "
        "The pigeons are not crows. That crow wants the chapati.")])
    clip("offering", [
        spoken_seg(
            "offering#0", 0, "Serving the favourite food",
            both + ["location:home", "theme:religion"],
            "The food is offered to a crow on a leaf.",
            "We serve it on a leaf and the crow takes it. "
            "That is how it reaches them."),
        spoken_seg(
            "offering#60", 60, "The twelfth day", both + ["location:home",
                                                         "theme:religion"],
            "The mourning period runs twelve days.",
            "For twelve days nobody cooks the favourite dishes."),
    ])
    clip("view", [spoken_seg(
        "view#0", 0, "The view from the balcony", both + balcony,
        "The complex and the pool below.",
        "Look at the pool. Nice breeze today. The trees are lush.")])
    clip("goodbye", [spoken_seg(
        "goodbye#0", 0, "Waving from the balcony", both + balcony,
        "They wave as the taxi pulls away.",
        "Wave properly. The taxi is here already.")])
    clip("tree", [spoken_seg(
        "tree#0", 0, "Trees over the pool", both + balcony,
        "A long look at the trees and the pool.",
        "Those trees were small once. Now they are over the pool.")])
    clip("tea", [spoken_seg(
        "tea#0", 0, "Tea and chapati", both + ["location:kitchen", "theme:food"],
        "Chapati straight off the pan, with tea.",
        "Eat the chapati while it is hot. The tea is going cold. "
        "Pour the tea out and I will make it again.")])
    clip("cold", [spoken_seg(
        "cold#0", 0, "Tea going cold", both + ["location:kitchen", "theme:food"],
        "The tea sits until it is cold.",
        "Nobody drank the tea. It is cold now.")])
    index.build()
    return similarity.Space.load()


def test_the_word_everything_is_about_beats_the_room_it_was_filmed_in(spoken):
    """The complaint this was written for: standing on the bird-feeding moment,
    the top of the list was five other things shot on the same balcony."""
    top = spoken.related("birds#0")[0]
    assert top[1].id == "offering#0"
    assert "crow" in top[2]


def test_a_plural_matches_its_singular(spoken):
    """One clip says "crows" and the other says "crow", and matching on the
    surface forms found nothing between them."""
    assert spoken.text_similarity("birds#0", "offering#0") > 0


def test_a_word_nothing_else_says_cannot_fold_to_a_non_word(spoken):
    """Stripping a trailing s off anything would turn "gas" into "ga" and
    "chapatis" into "chapati" only by luck. The vocabulary decides."""
    assert "chapati" in spoken._words()["birds#0"]
    assert "chapatis" not in spoken._words()["birds#0"]
    assert "chapat" not in spoken._words()["birds#0"]


def test_a_match_says_which_side_of_it_did_the_work(spoken):
    """`via` leads with the words when the words won. Opening with
    person:grandma reads like the reason, and it is the reason for nothing."""
    _, _, via = spoken.related("birds#0")[0]
    assert via[0] == "crow"


def test_filler_is_not_a_reason_two_moments_are_related(spoken):
    """Conversational speech is mostly function words -- this idea was dropped
    once because the top of a raw word count is "don't", "right", "yeah"."""
    assert not {"the", "and", "look", "come", "already"} & set(spoken._words()["view#0"])


def test_relatedness_is_still_bounded(spoken):
    for sid in spoken.segments:
        assert all(0 < score <= 1.0 for score, _, _ in spoken.related(sid))
