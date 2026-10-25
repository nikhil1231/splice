"""Captions drawn as pictures.

Pillow is real here -- it is a pure function from text to pixels and standing
it in would test nothing. What is worth checking is the arithmetic around it:
that a line too long for the frame is broken rather than run off the edge,
that the same words are only ever drawn once, and that asking for a language
gets that language.
"""

import pytest

from splice import captions


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLICE_ROOT", str(tmp_path))
    return tmp_path


def _lines(*rows):
    return list(rows)


pytestmark = pytest.mark.skipif(
    not captions.available(), reason="no font or no Pillow on this machine"
)


# --- what gets drawn ---------------------------------------------------


def test_a_card_is_the_box_and_not_the_whole_frame():
    """Positioning is the overlay's job; drawing a 1080x1920 transparent PNG
    per caption would be eight megabytes each."""
    card, = captions.cards_for(
        _lines((0.0, 2.0, "Are you married?", "")), 1080, 1920
    )
    assert card.width < 1080
    assert card.height < 250
    assert card.path.stat().st_size < 50_000


def test_a_line_too_long_for_the_frame_wraps_rather_than_running_off():
    long = "Two years before that, and if it is sixty three then it would " \
           "have to be sixty one, which is what everybody said at the time."
    short, = captions.cards_for(_lines((0.0, 2.0, "Yes.", "")), 1080, 1920)
    wrapped, = captions.cards_for(_lines((0.0, 2.0, long, "")), 1080, 1920)
    assert wrapped.width <= 1080
    assert wrapped.height > short.height


def test_a_single_unwrappable_word_is_broken_rather_than_overflowing():
    card, = captions.cards_for(_lines((0.0, 2.0, "a" * 400, "")), 1080, 1920)
    assert card.width <= 1080


def test_both_languages_make_a_taller_card_than_one():
    one, = captions.cards_for(_lines((0.0, 2.0, "Yes.", "Hoi.")), 1080, 1920)
    two, = captions.cards_for(
        _lines((0.0, 2.0, "Yes.", "Hoi.")), 1080, 1920, lang="both"
    )
    assert two.height > one.height


def test_konkani_alone_is_drawn_when_it_is_asked_for():
    en, = captions.cards_for(_lines((0.0, 2.0, "Yes.", "Hoi.")), 1080, 1920)
    kok, = captions.cards_for(
        _lines((0.0, 2.0, "Yes.", "Hoi.")), 1080, 1920, lang="kok"
    )
    assert en.path != kok.path


def test_a_line_with_nothing_in_it_gets_no_card():
    assert captions.cards_for(_lines((0.0, 2.0, "  ", "")), 1080, 1920) == []


def test_a_line_that_lasts_no_time_gets_no_card():
    assert captions.cards_for(_lines((4.0, 4.0, "Yes.", "")), 1080, 1920) == []


def test_a_missing_translation_falls_back_to_the_other_language():
    card, = captions.cards_for(_lines((0.0, 2.0, "", "Hoi.")), 1080, 1920)
    assert card.width > 0


# --- drawing once ------------------------------------------------------


def test_the_same_words_are_only_drawn_once():
    first, = captions.cards_for(_lines((0.0, 2.0, "Yes.", "")), 1080, 1920)
    written = first.path.stat().st_mtime_ns
    again, = captions.cards_for(_lines((9.0, 11.0, "Yes.", "")), 1080, 1920)
    assert again.path == first.path
    assert again.path.stat().st_mtime_ns == written
    # ...and the times still belong to the line, not to the picture.
    assert (again.start, again.end) == (9.0, 11.0)


def test_a_different_frame_size_is_a_different_card():
    big, = captions.cards_for(_lines((0.0, 2.0, "Yes.", "")), 1080, 1920)
    small, = captions.cards_for(_lines((0.0, 2.0, "Yes.", "")), 270, 480)
    assert big.path != small.path
    assert small.height < big.height


def test_cards_come_back_in_time_order_whatever_order_they_arrive_in():
    cards = captions.cards_for(_lines(
        (9.0, 11.0, "Later.", ""),
        (1.0, 3.0, "Earlier.", ""),
    ), 1080, 1920)
    assert [c.start for c in cards] == [1.0, 9.0]
