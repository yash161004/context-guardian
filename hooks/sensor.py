#!/usr/bin/env python3
"""Context Guardian - Phase 1 sensor (PostToolUse hook).

Measures only. Writes nothing to stdout, returns no `additionalContext`,
and never blocks a tool call. Acting on what it measures is Phase 2.

IMPORTANT - how this gets its numbers:
The PostToolUse hook payload does NOT include token usage or context size.
It provides session_id, transcript_path, cwd, tool_name, tool_input,
tool_response, tool_use_id (and agent_id/agent_type inside a subagent).
So the sensor takes the tool identity from stdin and reads the *context
size* from the transcript at `transcript_path`.

Because Claude Code writes that transcript asynchronously, the reading can
lag the live conversation by a turn. At the 200k/350k thresholds this is
immaterial, but it does make this near-real-time rather than exact.

Install (in ~/.claude/settings.json):

    {
      "hooks": {
        "PostToolUse": [
          {
            "hooks": [
              {"type": "command", "command": "python /path/to/hooks/sensor.py"}
            ]
          }
        ]
      }
    }
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a standalone script from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_guardian import db, transcript  # noqa: E402
from context_guardian.config import load_config  # noqa: E402
from context_guardian.detector import RepeatReadDetector, is_scratchpad  # noqa: E402

# The rolling window must survive across hook invocations - each tool call
# is a separate process - so it is rebuilt from the database rather than
# held in memory.
READ_TOOLS = ("Read",)


def rebuild_window(conn, session_id, cfg):
    """Reconstruct the rolling read-window for this session from the DB.

    Each hook invocation is a fresh process, so there is no in-memory state
    to carry over. Replaying the last N relevant rows is cheap and keeps the
    window semantics identical to the offline replay used in the tests.
    """
    window = cfg["repeat_read_window"]
    if cfg["repeat_read_window_counts"] == "tool_calls":
        rows = conn.execute(
            """SELECT tool_name, file_path FROM tool_calls
                WHERE session_id = ? AND is_sidechain = 0
                ORDER BY id DESC LIMIT ?""",
            (session_id, window),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT tool_name, file_path FROM tool_calls
                WHERE session_id = ? AND is_sidechain = 0
                  AND file_path IS NOT NULL AND tool_name IN ('Read')
                ORDER BY id DESC LIMIT ?""",
            (session_id, window),
        ).fetchall()

    detector = RepeatReadDetector(
        window=window,
        threshold=cfg["repeat_read_threshold"],
        scratchpad_patterns=cfg["scratchpad_path_patterns"],
        window_counts=cfg["repeat_read_window_counts"],
    )
    for row in reversed(rows):
        detector.record(row["tool_name"], row["file_path"], READ_TOOLS)
    return detector


def main():
    try:
        # Read bytes and decode UTF-8 explicitly rather than using
        # sys.stdin.read(), which decodes with the *locale* encoding - cp1252
        # on a default Windows install. A payload containing any non-ASCII
        # character (an accented file path, say) would otherwise raise or
        # silently mangle on exactly the platform this was developed on.
        # utf-8-sig also strips a BOM, which some Windows shells prepend.
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    except Exception:
        return 0
    if not raw.strip():
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[context-guardian] unparseable hook payload ({e}); skipping",
              file=sys.stderr)
        return 0

    try:
        cfg = load_config()
        if not cfg.get("enabled", True):
            return 0

        session_id = payload.get("session_id") or "unknown"
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}
        file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None

        # The hook tells us directly when it is firing inside a subagent -
        # more reliable than inferring it from the transcript.
        sidechain = bool(payload.get("agent_id") or payload.get("agent_type"))

        conn = db.connect(cfg["db_path"])

        prompt_tokens = model = None
        running = None
        transcript_path = payload.get("transcript_path")
        if transcript_path and not sidechain:
            prompt_tokens, model = transcript.latest_context_state(transcript_path)

        if prompt_tokens is not None:
            running = prompt_tokens
        elif not sidechain:
            # Transcript write may not have landed yet; carry the last known
            # value forward instead of recording a false drop to zero.
            running = db.last_running_context(conn, session_id)

        context_window = transcript.context_window_for(model) if model else None

        repeat_count = None
        if tool_name in READ_TOOLS and file_path and not sidechain:
            if is_scratchpad(file_path, cfg["scratchpad_path_patterns"]):
                repeat_count = None
            else:
                detector = rebuild_window(conn, session_id, cfg)
                repeat_count = detector.record(tool_name, file_path, READ_TOOLS)

        db.record_tool_call(
            conn,
            session_id=session_id,
            tool_name=tool_name,
            file_path=file_path,
            is_sidechain=sidechain,
            timestamp=datetime.now(timezone.utc).isoformat(),
            model=model,
            context_window=context_window,
            prompt_tokens=prompt_tokens,
            running_context_tokens=running,
            repeat_read_count=repeat_count,
        )
        conn.close()

    except Exception as e:
        # Absolute last line of defence. A sensor that breaks someone's
        # session is far worse than a sensor that misses a data point, so
        # every unexpected failure degrades to a stderr warning.
        print(f"[context-guardian] sensor error ({type(e).__name__}: {e}); "
              f"skipping this event", file=sys.stderr)
        if os.environ.get("CONTEXT_GUARDIAN_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
