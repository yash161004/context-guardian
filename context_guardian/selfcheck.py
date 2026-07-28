"""Is Context Guardian actually alive?

Both hooks are built to fail safe, which means they also fail *silently* -
a broken install looks exactly like a quiet session. Two real bugs during
development (a BOM on stdin, and locale-decoded stdin) presented as nothing
at all rather than as errors.

So: never assume no news is good news. Run this.

    python -m context_guardian.selfcheck
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CONFIG_PATH, load_config
from .nudge import _fmt_tokens

OK = "  ok  "
WARN_ = " warn "
FAIL = " fail "


def _line(status, text):
    print(f"[{status}] {text}")


def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def run(hours=24):
    print("Context Guardian - self check\n")
    problems = 0

    # --- config ---------------------------------------------------------
    cfg = load_config()
    if CONFIG_PATH.exists():
        _line(OK, f"config found at {CONFIG_PATH}")
    else:
        _line(WARN_, f"no config at {CONFIG_PATH} (using built-in defaults)")
    if not cfg.get("enabled", True):
        _line(WARN_, "'enabled' is false - the sensor records nothing")
        problems += 1
    if not cfg.get("nudge_enabled", True):
        _line(WARN_, "'nudge_enabled' is false - no nudges will be emitted")
    _line(OK, f"thresholds: warn {_fmt_tokens(cfg['context_warn_tokens'])}, "
              f"urgent {_fmt_tokens(cfg['context_urgent_tokens'])}, "
              f"repeat {cfg['repeat_read_threshold']}x in "
              f"{cfg['repeat_read_window']} reads")

    # --- database -------------------------------------------------------
    db_path = Path(cfg["db_path"])
    if not db_path.exists():
        _line(FAIL, f"no database at {db_path}")
        print("\n  The sensor has never written a row. Most likely the "
              "PostToolUse hook\n  is not installed, or the python on PATH "
              "cannot run it. Check:\n"
              "    - hooks are registered (plugin installed, or entries in "
              "settings.json)\n"
              "    - the command's interpreter exists (python3 vs python)\n"
              "    - run a tool call in a session, then re-run this check")
        return 1

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        _line(FAIL, f"cannot open database: {e}")
        return 1

    _line(OK, f"database at {db_path}")

    try:
        total = conn.execute("SELECT COUNT(*) n FROM tool_calls").fetchone()["n"]
    except sqlite3.Error as e:
        _line(FAIL, f"schema problem: {e}")
        return 1

    if total == 0:
        _line(FAIL, "database exists but holds no tool calls")
        problems += 1
    else:
        _line(OK, f"{total:,} tool call(s) recorded")

    # --- recency: the actual liveness question ---------------------------
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = conn.execute(
        "SELECT timestamp FROM tool_calls ORDER BY id DESC LIMIT 500").fetchall()
    recent = sum(1 for r in rows
                 if (_parse_ts(r["timestamp"]) or datetime.min.replace(
                     tzinfo=timezone.utc)) >= cutoff)
    if recent:
        _line(OK, f"{recent} tool call(s) in the last {hours}h - sensor is live")
    elif total:
        last = _parse_ts(rows[0]["timestamp"]) if rows else None
        when = last.isoformat(timespec="seconds") if last else "unknown"
        _line(WARN_, f"nothing recorded in the last {hours}h (last: {when})")
        problems += 1

    # --- sessions and nudges --------------------------------------------
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) n FROM tool_calls").fetchone()["n"]
    _line(OK, f"{sessions} session(s) seen")

    peak = conn.execute(
        "SELECT MAX(running_context_tokens) p FROM tool_calls "
        "WHERE is_sidechain = 0").fetchone()["p"]
    if peak:
        _line(OK, f"peak context observed: {_fmt_tokens(peak)} tokens")
    else:
        _line(WARN_, "no context size ever recorded - the sensor may not be "
                     "able to read your transcripts")
        problems += 1

    try:
        nudges = conn.execute("SELECT COUNT(*) n FROM nudges").fetchone()["n"]
        by_level = conn.execute(
            "SELECT level, COUNT(*) n FROM nudges GROUP BY level").fetchall()
    except sqlite3.Error:
        _line(WARN_, "nudges table missing - run any prompt to create it")
        nudges, by_level = 0, []

    if nudges:
        detail = ", ".join(f"{r['level']}: {r['n']}" for r in by_level)
        _line(OK, f"{nudges} nudge(s) emitted ({detail})")
        last = conn.execute(
            "SELECT timestamp, level, message FROM nudges "
            "ORDER BY id DESC LIMIT 1").fetchone()
        print(f"\n  most recent nudge ({last['level']}, {last['timestamp']}):")
        print(f"    {last['message']}")
    elif peak and peak >= cfg["context_warn_tokens"]:
        _line(WARN_, "context has passed the warn threshold but nothing has "
                     "nudged yet")
        print("    Expected right after installing - the nudge fires on your "
              "next prompt.\n"
              "    If it persists across several prompts, check that the "
              "UserPromptSubmit\n    hook is registered.")
    else:
        _line(OK, "no nudges yet (no session has crossed a threshold)")

    conn.close()

    print()
    if problems:
        print(f"{problems} thing(s) worth looking at.")
        return 1
    print("All good.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24,
                        help="how recent counts as 'live' (default: 24)")
    args = parser.parse_args(argv)
    return run(hours=args.hours)


if __name__ == "__main__":
    sys.exit(main())
