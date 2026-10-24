"""Building the prompt, and deciding when a clip has to be analysed again.

Analysis is the expensive step -- the twenty-minute interview took forty-four
minutes of wall clock -- so the two things worth pinning down are that the
prompt actually assembles, and that the fingerprint which decides whether to
re-run it changes for the right reasons and no others.
"""

from pathlib import Path

import pytest

from greels import analyze
from greels.taxonomy import Taxonomy


@pytest.fixture
def taxonomy():
    return Taxonomy.load()


# --- the prompt --------------------------------------------------------


def test_nothing_is_left_for_the_model_to_wonder_about(taxonomy):
    """A placeholder that survives goes to Gemini verbatim."""
    built = analyze.build_prompt(Path("/raw/a.MOV"), taxonomy)
    for placeholder in ("{video_path}", "{chunk_note}", "{relative_note}",
                        "{taxonomy}", "{legacy_hint}"):
        assert placeholder not in built
    assert "/raw/a.MOV" in built


def test_a_whole_clip_is_told_it_is_whole(taxonomy):
    built = analyze.build_prompt(Path("/raw/a.MOV"), taxonomy)
    assert "in full" in built
    assert "part 1 of" not in built


def test_a_chunk_is_told_where_it_sits_and_to_count_from_zero(taxonomy):
    """Getting this wrong puts every timestamp of a twenty-minute interview
    out by the chunk's offset."""
    built = analyze.build_prompt(Path("/raw/a.MOV"), taxonomy,
                                 chunk_index=2, chunk_count=5, chunk_offset=480)
    assert "part 3 of 5" in built
    assert "00:08:00" in built
    assert "start of THIS file" in built


def test_the_old_notes_come_with_a_warning_not_to_copy_the_times(taxonomy):
    built = analyze.build_prompt(Path("/raw/a.MOV"), taxonomy,
                                 legacy_prose="- **00:00** Grandma frying fish.")
    assert "frying fish" in built
    assert "unreliable minute buckets" in built


def test_no_old_notes_leaves_no_empty_heading(taxonomy):
    assert "earlier, rougher pass" not in analyze.build_prompt(
        Path("/raw/a.MOV"), taxonomy
    )


def test_the_vocabulary_is_in_there(taxonomy):
    """As namespaces and their values, indented under the bullet the prompt
    introduces them with."""
    built = analyze.build_prompt(Path("/raw/a.MOV"), taxonomy)
    assert "  - theme: " in built and "money" in built
    assert "  - quality: " in built


# --- when to run it again ----------------------------------------------


def test_the_same_prompt_is_the_same_fingerprint():
    """What makes it change, and what must not, is pinned down in
    test_pipeline.py -- this is only that it is stable."""
    assert analyze.prompt_sha() == analyze.prompt_sha()


# --- when agy falls over -----------------------------------------------


def test_a_transient_failure_is_retried(monkeypatch):
    """agy fails in bursts; a failure that survives the retries is recorded in
    the sidecar rather than swallowed."""
    monkeypatch.setattr(analyze.config, "AGY_RETRIES", 2)
    monkeypatch.setattr(analyze, "_stagger", lambda: None)
    tries = []

    def flaky(prompt, schema_path):
        tries.append(1)
        if len(tries) < 3:
            raise analyze.AnalysisError("agy fell over")
        return {"segments": []}

    monkeypatch.setattr(analyze, "run_agy", flaky)
    assert analyze._call_with_retry("p", Path("s.json"), "a") == {"segments": []}
    assert len(tries) == 3


def test_a_failure_that_survives_the_retries_is_raised_with_its_reason(monkeypatch):
    monkeypatch.setattr(analyze.config, "AGY_RETRIES", 1)
    monkeypatch.setattr(analyze, "_stagger", lambda: None)

    def always(prompt, schema_path):
        raise analyze.AnalysisError("quota exhausted")

    monkeypatch.setattr(analyze, "run_agy", always)
    with pytest.raises(analyze.AnalysisError, match="quota exhausted"):
        analyze._call_with_retry("p", Path("s.json"), "chunk 2")
