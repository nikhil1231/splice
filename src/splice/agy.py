"""Structured JSON transport shared by video analysis and transcript drafting."""

import json
import subprocess
from pathlib import Path

from . import config


class AnalysisError(RuntimeError):
    pass


def run_structured(
    prompt: str, schema_path: Path, *, model: str | None = None,
    allow_tools: bool = True, required_key: str = "segments",
) -> dict:
    """One `agy` call. Raises AnalysisError on anything but a clean SUCCESS."""
    cmd = [
        config.AGY_BIN,
        "-p", prompt,
        "--json-schema", str(schema_path),
        "--output-format", "json",
        "--model", model or config.AGY_MODEL,
        "--print-timeout", config.AGY_TIMEOUT,
    ]
    if allow_tools and config.AGY_SKIP_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.AGY_SUBPROCESS_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError(f"agy exceeded {config.AGY_SUBPROCESS_TIMEOUT}s") from exc

    stdout = proc.stdout.strip()
    if not stdout:
        raise AnalysisError(
            f"agy produced no output (exit {proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise AnalysisError(f"agy output was not JSON: {stdout[:400]}") from None

    denied = payload.get("denied_actions") or []
    if denied:
        names = ", ".join(sorted({d.get("action", "?") for d in denied}))
        raise AnalysisError(
            f"agy was denied the {names} tool, so it never read the video. "
            "Allow it in agy's settings.json or leave SPLICE_AGY_SKIP_PERMISSIONS on."
        )

    if payload.get("status") != "SUCCESS":
        raise AnalysisError(
            f"agy status={payload.get('status')}: {str(payload.get('response'))[:400]}"
        )

    structured = payload.get("structured_output")
    if not isinstance(structured, dict) or required_key not in structured:
        raise AnalysisError(f"agy returned no structured_output.{required_key}")
    return structured

