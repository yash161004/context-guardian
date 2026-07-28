"""Did the nudge actually change anything?

This is the only question that decides whether Context Guardian is a real
tool or a well-tested no-op. Everything else - thresholds, repeat-read
detection, rate limiting - has been measured against real transcripts. The
*wording* has not, and cannot be unit-tested: it either moves Claude
mid-session or it doesn't.

So measure it. For every nudge emitted, look at what Claude did next:

  delegated  - spawned a subagent (Task call, or sidechain rows appear)
  compacted  - context dropped sharply, i.e. /compact ran
  ignored    - neither, within the follow-up window
  pending    - not enough activity after it yet to say

    python -m context_guardian.outcomes

Run this after a few days of real use. If most nudges are `ignored`, the
message is wrong - that's a Phase 2 rewrite, not a reason to abandon the
detector.
"""

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .nudge import _fmt_tokens

# How many tool calls after a nudge still count as "in response to" it.
DEFAULT_FOLLOW_WINDOW = 15

# A drop of at least this fraction of context reads as a compaction rather
# than ordinary turn-to-turn variation.
COMPACTION_DROP = 0.30

DELEGATION_TOOLS = {"Task", "Agent"}


def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _nearest_tool_call_id(conn, session_id, timestamp):
    """Last tool call recorded at or before a nudge, i.e. where it landed."""
    row = conn.execute(
        """SELECT id FROM tool_calls
            WHERE session_id = ? AND timestamp <= ?
            ORDER BY id DESC LIMIT 1""",
        (session_id, timestamp),
    ).fetchone()
    return row["id"] if row else 0


def classify(conn, nudge, window):
    """Work out what happened after one nudge."""
    session_id = nudge["session_id"]
    anchor = _nearest_tool_call_id(conn, session_id, nudge["timestamp"])

    rows = conn.execute(
        """SELECT tool_name, is_sidechain, running_context_tokens
             FROM tool_calls
            WHERE session_id = ? AND id > ?
            ORDER BY id LIMIT ?""",
        (session_id, anchor, window),
    ).fetchall()

    if not rows:
        return "pending", None

    delegated = any(r["tool_name"] in DELEGATION_TOOLS or r["is_sidechain"]
                    for r in rows)
    if delegated:
        return "delegated", None

    before = nudge["context_tokens"] or 0
    after = [r["running_context_tokens"] for r in rows
             if r["running_context_tokens"] is not None]
    if before and after:
        lowest = min(after)
        if lowest < before * (1 - COMPACTION_DROP):
            return "compacted", f"{_fmt_tokens(before)} -> {_fmt_tokens(lowest)}"

    if len(rows) < window:
        return "pending", None
    return "ignored", None


def report_no_nudges(conn, cfg):
    """Explain a zero-nudge result at the moment it is read.

    This exists because of a specific, predictable misreading. After three
    days of real work, "0 nudges" *feels* like "the idea doesn't work" - and
    that instinct would kill a project whose detector is fine and whose
    thresholds are simply set for someone else's sessions. Saying so in a
    design doc is useless; the person needs to read it here, in the output,
    at the moment they draw the conclusion.
    """
    row = conn.execute(
        """SELECT MIN(timestamp) first, MAX(timestamp) last, COUNT(*) n,
                  MAX(running_context_tokens) peak
             FROM tool_calls WHERE is_sidechain = 0""").fetchone()

    if not row or not row["n"]:
        print("No nudges, and no tool calls recorded either.\n"
              "That is an install problem, not a result. Run:\n"
              "    python -m context_guardian.selfcheck")
        return

    days = None
    first, last = _parse_ts(row["first"]), _parse_ts(row["last"])
    if first and last:
        days = (last - first).total_seconds() / 86400

    peak = row["peak"] or 0
    warn = cfg["context_warn_tokens"]

    print(f"No nudges emitted.\n")
    print(f"  recording for   : {days:.1f} day(s)" if days is not None
          else "  recording for   : unknown")
    print(f"  tool calls seen : {row['n']:,}")
    print(f"  peak context    : {_fmt_tokens(peak)} tokens")
    print(f"  warn threshold  : {_fmt_tokens(warn)} tokens")

    if days is not None and days < 2.5:
        print("\nToo early to conclude anything - keep working normally.")
        return

    # Past the dogfooding window with nothing. This is the branch that gets
    # misread, so say the conclusion outright.
    print("\n" + "=" * 68)
    print("READ THIS BEFORE CONCLUDING THE IDEA DOESN'T WORK")
    print("=" * 68)
    print(
        "Zero nudges after a full dogfooding window does NOT mean the\n"
        "detector is broken or the premise is wrong. It means the THRESHOLD\n"
        "is set for someone else's sessions - it was derived from a 6-session\n"
        "corpus of unusually long runs, and your real work is shorter.\n"
    )
    if peak:
        pct = peak / warn * 100
        print(f"Your peak was {_fmt_tokens(peak)}, which is {pct:.0f}% of the "
              f"warn threshold.")
        suggested = max(50_000, int(peak * 0.75 / 10_000) * 10_000)
        print(f"\nThe fix is one number. Try:\n"
              f"    context_warn_tokens   : {suggested:,}\n"
              f"    context_urgent_tokens : {int(suggested * 1.75):,}\n"
              f"in ~/.claude/context-guardian/config.json, then keep working.")
    print("\nAbandon the project only if it nudges and Claude ignores it, and\n"
          "rewriting the message doesn't help. That is a different result\n"
          "from this one.")


def run(window=DEFAULT_FOLLOW_WINDOW):
    cfg = load_config()
    db_path = Path(cfg["db_path"])
    if not db_path.exists():
        print(f"No database at {db_path} - nothing recorded yet.")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        nudges = conn.execute(
            "SELECT * FROM nudges ORDER BY id").fetchall()
    except sqlite3.Error:
        print("No nudges table yet - nothing has nudged.")
        return 1

    print("Context Guardian - did the nudge change anything?\n")

    if not nudges:
        report_no_nudges(conn, cfg)
        conn.close()
        return 0

    tally = Counter()
    print(f"{'when':<20} {'level':<12} {'context':>8}  outcome")
    print("-" * 72)
    for n in nudges:
        outcome, detail = classify(conn, n, window)
        tally[outcome] += 1
        when = (n["timestamp"] or "")[:19].replace("T", " ")
        ctx = _fmt_tokens(n["context_tokens"])
        line = f"{when:<20} {n['level']:<12} {ctx:>8}  {outcome}"
        if detail:
            line += f" ({detail})"
        print(line)

    decided = tally["delegated"] + tally["compacted"] + tally["ignored"]
    print("\n" + "-" * 72)
    print(f"delegated {tally['delegated']}   compacted {tally['compacted']}   "
          f"ignored {tally['ignored']}   pending {tally['pending']}")

    if decided == 0:
        print("\nNo nudge has enough follow-up activity to judge yet.")
        conn.close()
        return 0

    acted = tally["delegated"] + tally["compacted"]
    rate = acted / decided * 100
    print(f"\nacted on: {acted}/{decided} ({rate:.0f}%)")

    # The pre-committed launch gate. Stated here rather than in a doc so the
    # criterion cannot drift after seeing the result.
    print("\nGate for launching (set before collecting this data):")
    if decided < 5:
        print(f"  NOT YET - {decided} judged nudge(s), need at least 5.")
    elif rate >= 50:
        print(f"  PASS - {rate:.0f}% acted on. The message works. Ship it.")
    else:
        print(f"  FAIL - only {rate:.0f}% acted on. The wording is the problem, "
              f"not the\n         detector. Rewrite the message "
              f"(context_guardian/nudge.py:build_message)\n         and "
              f"re-measure before launching.")

    conn.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=int, default=DEFAULT_FOLLOW_WINDOW,
                        help="tool calls after a nudge that count as a response "
                             f"(default: {DEFAULT_FOLLOW_WINDOW})")
    args = parser.parse_args(argv)
    return run(window=args.window)


if __name__ == "__main__":
    sys.exit(main())
