#!/usr/bin/env python3
"""Context Guardian - Phase 2 nudge (UserPromptSubmit hook).

Suggest-only. This hook tells *Claude* what the sensor is seeing and lets
Claude decide what to do about it. It never runs /compact, never blocks a
prompt, and never addresses the user directly - nudging the model rather
than the human is the entire point of the project.

CRITICAL - stdout discipline:
Claude Code treats any plain stdout from a UserPromptSubmit hook as context
to inject. A stray print would therefore land in the model's context on
every single prompt. This script writes to stdout exactly once, only when
it has decided to nudge, and only a well-formed JSON object. All diagnostics
go to stderr.

The hook also has a 30-second timeout (shorter than other events, because
the user is sat waiting on their prompt), so it does one tail-read and two
indexed queries and gets out.

Install (in ~/.claude/settings.json):

    {
      "hooks": {
        "UserPromptSubmit": [
          {
            "hooks": [
              {"type": "command", "command": "python /path/to/hooks/nudge.py"}
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_guardian import db, nudge as nudge_logic, transcript  # noqa: E402
from context_guardian.config import load_config  # noqa: E402


def emit(additional_context):
    """Write the one and only thing this hook is allowed to put on stdout."""
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }, sys.stdout)


def main():
    try:
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
    if not isinstance(payload, dict):
        return 0

    try:
        cfg = load_config()
        if not (cfg.get("enabled", True) and cfg.get("nudge_enabled", True)):
            return 0

        session_id = payload.get("session_id") or "unknown"

        # Freshest reading available: the transcript itself. Falls back to
        # whatever the sensor last recorded if the async transcript write
        # has not landed yet.
        context_tokens = None
        transcript_path = payload.get("transcript_path")
        if transcript_path:
            context_tokens, _ = transcript.latest_context_state(transcript_path)

        conn = db.connect(cfg["db_path"])
        if context_tokens is None:
            context_tokens = db.last_running_context(conn, session_id)

        if context_tokens is None:
            # Nothing measured yet - stay silent rather than guess.
            conn.close()
            return 0

        # Re-arm any level the session has dropped back below (e.g. after a
        # /compact) before deciding, so a genuine second crossing can fire.
        db.rearm_levels(conn, session_id,
                        nudge_logic.levels_to_rearm(context_tokens, cfg))

        hot_file = hot_count = None
        hot = db.hottest_recent_read(conn, session_id,
                                     window=cfg["repeat_read_window"] * 3,
                                     threshold=cfg["repeat_read_threshold"])
        if hot:
            hot_file, hot_count = hot

        if db.count_nudges(conn, session_id) >= cfg.get("max_nudges_per_session", 12):
            conn.close()
            return 0

        decision = nudge_logic.evaluate(
            context_tokens=context_tokens,
            cfg=cfg,
            hot_file=hot_file,
            hot_count=hot_count,
            has_fired=lambda level, subject: db.has_active_nudge(
                conn, session_id, level, subject),
        )

        if decision is None:
            conn.close()
            return 0

        db.record_nudge(
            conn,
            session_id=session_id,
            level=decision.level,
            subject=decision.subject,
            context_tokens=decision.context_tokens,
            message=decision.message,
            timestamp=datetime.now(timezone.utc).isoformat(),
            # Recorded so the outcome tracker can later check whether Claude
            # actually raised it with the user - the one action the new
            # message asks for, and one a PostToolUse sensor cannot see.
            transcript_path=transcript_path,
            message_version=nudge_logic.MESSAGE_VERSION,
        )
        conn.close()

        emit(decision.message)

    except Exception as e:
        # Never block or corrupt a prompt. Nothing has been written to
        # stdout at this point unless emit() already succeeded, so failing
        # here leaves the prompt exactly as the user typed it.
        print(f"[context-guardian] nudge error ({type(e).__name__}: {e}); "
              f"staying silent", file=sys.stderr)
        if os.environ.get("CONTEXT_GUARDIAN_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
