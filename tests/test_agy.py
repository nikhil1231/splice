"""The one call that costs money and time.

`agy` is stood in for. What is worth pinning down is the shape of the command
and, much more, what each way of failing is reported as: a forty-four minute
analysis that falls over should say "agy was denied the read_file tool", not
"KeyError".
"""

import json
import subprocess
from pathlib import Path

import pytest

from greels import agy, analyze


def _agy(monkeypatch, stdout="", stderr="", code=0):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)

    monkeypatch.setattr(agy.subprocess, "run", fake_run)
    return calls


GOOD = json.dumps({
    "status": "SUCCESS",
    "structured_output": {"segments": [{"start": 0, "end": 5, "title": "One"}]},
})


# --- the command -------------------------------------------------------


def test_it_asks_for_json_against_the_schema(monkeypatch):
    calls = _agy(monkeypatch, GOOD)
    analyze.run_agy("describe this", Path("/tmp/schema.json"))

    cmd = calls[0]
    assert cmd[cmd.index("-p") + 1] == "describe this"
    assert cmd[cmd.index("--json-schema") + 1] == "/tmp/schema.json"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == analyze.config.AGY_MODEL


def test_the_permission_flag_can_be_turned_off(monkeypatch):
    calls = _agy(monkeypatch, GOOD)
    monkeypatch.setattr(analyze.config, "AGY_SKIP_PERMISSIONS", False)
    analyze.run_agy("p", Path("s.json"))
    assert "--dangerously-skip-permissions" not in calls[0]


# --- what came back ----------------------------------------------------


def test_a_clean_answer_is_the_structured_part_of_it(monkeypatch):
    _agy(monkeypatch, GOOD)
    assert analyze.run_agy("p", Path("s.json"))["segments"][0]["title"] == "One"


def test_being_denied_the_tool_says_which_tool(monkeypatch):
    """It runs, it succeeds, and it never saw the video -- the failure that
    looks least like one."""
    _agy(monkeypatch, json.dumps({
        "status": "SUCCESS",
        "denied_actions": [{"action": "read_file"}, {"action": "read_file"}],
        "structured_output": {"segments": []},
    }))
    with pytest.raises(analyze.AnalysisError, match="read_file"):
        analyze.run_agy("p", Path("s.json"))


def test_a_status_that_is_not_success_carries_what_it_said(monkeypatch):
    _agy(monkeypatch, json.dumps({"status": "ERROR", "response": "quota exhausted"}))
    with pytest.raises(analyze.AnalysisError, match="quota exhausted"):
        analyze.run_agy("p", Path("s.json"))


def test_saying_nothing_at_all_carries_the_exit_code_and_stderr(monkeypatch):
    _agy(monkeypatch, "", "agy: command failed", code=127)
    with pytest.raises(analyze.AnalysisError, match="command failed"):
        analyze.run_agy("p", Path("s.json"))


def test_output_that_is_not_json_is_quoted_back(monkeypatch):
    _agy(monkeypatch, "Sorry, I cannot do that.")
    with pytest.raises(analyze.AnalysisError, match="not JSON"):
        analyze.run_agy("p", Path("s.json"))


def test_json_without_the_segments_is_refused(monkeypatch):
    _agy(monkeypatch, json.dumps({"status": "SUCCESS", "structured_output": {}}))
    with pytest.raises(analyze.AnalysisError, match="structured_output.segments"):
        analyze.run_agy("p", Path("s.json"))


def test_a_run_that_never_returns_is_a_failure_and_not_a_hang(monkeypatch):
    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, analyze.config.AGY_SUBPROCESS_TIMEOUT)

    monkeypatch.setattr(agy.subprocess, "run", timeout)
    with pytest.raises(analyze.AnalysisError, match="exceeded"):
        analyze.run_agy("p", Path("s.json"))
