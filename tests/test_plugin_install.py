"""The shipped plugin's install path.

Written after discovering that the published `hooks.json` was silently broken
on Windows: `python3` there is a Microsoft Store stub that exits 9009 without
running anything, so the hook never fired - and because both hooks fail safe,
that is indistinguishable from "no nudges yet".

These tests execute the command strings exactly as Claude Code would.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"


def hook_commands():
    spec = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    for event, matchers in spec["hooks"].items():
        for matcher in matchers:
            for hook in matcher["hooks"]:
                yield event, hook["command"]


def test_manifest_points_at_the_hooks_file():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["hooks"] == "./hooks/hooks.json"
    assert HOOKS_JSON.exists()


def test_both_events_are_registered():
    events = {event for event, _ in hook_commands()}
    assert events == {"PostToolUse", "UserPromptSubmit"}


@pytest.mark.parametrize("event,command", list(hook_commands()))
def test_command_falls_back_across_interpreter_names(event, command):
    """No single interpreter name works everywhere - Debian/Ubuntu often has
    no `python`, Windows `python3` is a non-functional stub."""
    assert "||" in command, f"{event} has no interpreter fallback"
    for name in ("python3", "py", "python"):
        assert name in command
    assert command.index("python3") < command.index(" py "), \
        "python3 must be tried first so macOS/Linux never falls through"
    assert "${CLAUDE_PLUGIN_ROOT}" in command


@pytest.mark.parametrize("event,command", list(hook_commands()))
def test_command_actually_runs_on_this_machine(event, command, tmp_path):
    """The test that would have caught the shipped breakage.

    Runs the real command string through a shell, exactly as Claude Code
    does, and asserts the hook did its job rather than merely exiting 0 -
    a stub that never runs also 'succeeds' by exit code alone.
    """
    real = command.replace("${CLAUDE_PLUGIN_ROOT}",
                           str(REPO_ROOT).replace("\\", "/"))
    db_path = tmp_path / "state.db"

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({
        "message": {"model": "claude-opus-5",
                    "usage": {"input_tokens": 0,
                              "cache_read_input_tokens": 400_000,
                              "cache_creation_input_tokens": 0}}}) + "\n",
        encoding="utf-8")

    payload = {
        "session_id": "install-test",
        "transcript_path": str(transcript),
        "hook_event_name": event,
        "prompt": "hello",
        "tool_name": "Read",
        "tool_input": {"file_path": "/proj/a.py"},
    }

    proc = subprocess.run(
        real, shell=True, input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        env=dict(os.environ, CONTEXT_GUARDIAN_DB=str(db_path)),
        cwd=str(REPO_ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    if event == "PostToolUse":
        assert db_path.exists(), "sensor produced no database - it never ran"
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        n = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        conn.close()
        assert n == 1, "sensor ran but recorded nothing"
    else:
        # 400k is past the urgent threshold, so it must nudge - and stdout
        # must be nothing but the JSON, since plain stdout becomes context.
        out = proc.stdout.decode("utf-8").strip()
        assert out, "nudge produced no output at 400k context - it never ran"
        parsed = json.loads(out)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "urgent" in parsed["hookSpecificOutput"]["additionalContext"].lower()


def test_no_interpreter_noise_reaches_stdout(tmp_path):
    """A failed interpreter attempt must not pollute stdout.

    UserPromptSubmit treats plain stdout as context to inject, so a stub's
    complaint landing there would be fed to the model on every prompt.
    """
    command = next(c for e, c in hook_commands() if e == "UserPromptSubmit")
    real = command.replace("${CLAUDE_PLUGIN_ROOT}",
                           str(REPO_ROOT).replace("\\", "/"))
    proc = subprocess.run(
        real, shell=True, input=b"", capture_output=True,
        env=dict(os.environ, CONTEXT_GUARDIAN_DB=str(tmp_path / "s.db")),
        cwd=str(REPO_ROOT), timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout == b"", (
        f"stdout polluted by interpreter resolution: {proc.stdout!r}")
