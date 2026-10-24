"""Chunk-offset merging.

Chunks overlap by CHUNK_OVERLAP seconds so the model never has a sentence cut
dead at a boundary, which means the same moment can arrive twice. These tests
pin down how that gets resolved.
"""

import pytest

from greels.analyze import merge_segments, to_segments
from greels.proxy import chunk_offsets
from greels.schema import Line, Segment
from greels.taxonomy import Taxonomy


def seg(start, end, title="x", lines=None):
    return Segment(id=f"clip#{start:08.2f}", start=start, end=end, title=title,
                   lines=lines or [])


# --- offsets -----------------------------------------------------------


def test_short_clip_is_not_chunked():
    assert chunk_offsets(35.0, 240) == []
    assert chunk_offsets(240.0, 240) == []


def test_long_clip_is_covered_end_to_end():
    offsets = chunk_offsets(1190.34, 240)
    assert offsets == [0.0, 240.0, 480.0, 720.0, 960.0]
    assert offsets[-1] + 240 + 5 >= 1190.34, "last chunk must reach the end"


def test_trailing_sliver_is_absorbed_by_the_previous_chunk():
    # 963s would otherwise leave a 3-second chunk shorter than the overlap.
    assert chunk_offsets(963.0, 240) == [0.0, 240.0, 480.0, 720.0]


# --- merging -----------------------------------------------------------


def test_offsets_are_applied_to_segments_and_their_lines():
    taxonomy = Taxonomy.load()
    raw = {"segments": [{"start": 10, "end": 20, "title": "t", "summary": "", "english": "",
                         "tags": ["person:grandpa"],
                         "lines": [{"start": 11, "end": 12, "kok": "a", "en": "b"}]}]}
    (result,) = to_segments(raw, offset=240.0, stem="clip", taxonomy=taxonomy)
    assert (result.start, result.end) == (250.0, 260.0)
    assert (result.lines[0].start, result.lines[0].end) == (251.0, 252.0)
    assert result.id == "clip#00250.00"


def test_unknown_tags_are_quarantined_not_dropped():
    taxonomy = Taxonomy.load()
    raw = {"segments": [{"start": 0, "end": 5, "title": "t", "summary": "", "english": "",
                         "tags": ["person:grandpa", "theme:llamas"], "lines": []}]}
    (result,) = to_segments(raw, 0.0, "clip", taxonomy)
    assert result.tags == ["person:grandpa"]
    assert result.proposed_tags == ["theme:llamas"]


def test_duplicate_from_the_overlap_is_dropped():
    kept = merge_segments([seg(230, 250, "whole"), seg(240, 250, "truncated copy")], 300)
    assert [s.title for s in kept] == ["whole"]


def test_partial_overlap_is_trimmed_rather_than_discarded():
    kept = merge_segments([seg(230, 245, "first"), seg(240, 300, "second")], 400)
    assert [s.title for s in kept] == ["first", "second"]
    assert kept[1].start == 245.0, "later segment starts where the earlier one ends"


def test_result_is_monotonic_and_disjoint():
    kept = merge_segments(
        [seg(100, 160), seg(0, 50), seg(155, 200), seg(45, 105), seg(240, 250)], 300
    )
    for a, b in zip(kept, kept[1:]):
        assert a.end <= b.start + 1e-9
        assert a.start < b.start


def test_segments_are_clamped_to_the_asset():
    (kept,) = merge_segments([seg(1180, 1300)], duration=1190.34)
    assert kept.end == pytest.approx(1190.34)


def test_slivers_are_dropped():
    assert merge_segments([seg(0, 0.4)], 100) == []


def test_ids_follow_a_trimmed_start():
    kept = merge_segments([seg(230, 245), seg(240, 300)], 400)
    assert kept[1].id == "clip#00245.00"


def test_lines_outside_a_trimmed_segment_are_removed_and_clipped():
    lines = [Line(start=241, end=244, en="dropped"), Line(start=246, end=260, en="kept")]
    kept = merge_segments([seg(230, 245, "first"), seg(240, 300, "second", lines)], 400)
    texts = [line.en for line in kept[1].lines]
    assert texts == ["kept"]
    assert kept[1].lines[0].start >= kept[1].start
