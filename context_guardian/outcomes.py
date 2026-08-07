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
from .nudge import MESSAGE_VERSION, _fmt_tokens

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


# Phrases that indicate Claude raised context with the user in its own words.
SURFACE_MARKERS = (
    "/compact", "compact the", "compacting", "context is getting",
    "context window", "running low on context", "context is heavy",
    "fresh session", "start a new session", "context guardian",
)


def surfaced_to_user(nudge, within_messages=6):
    """Did Claude actually tell the user the context was getting heavy?

    This is the action the rewritten message asks for, and it is invisible to
    a PostToolUse sensor - it happens in assistant *text*, not a tool call.
    So read the transcript directly and look at what Claude said after the
    nudge landed.

    Returns True/False, or None when it cannot be determined (no transcript
    recorded, e.g. nudges from before this was instrumented).
    """
    path = nudge["transcript_path"] if "transcript_path" in nudge.keys() else None
    if not path:
        return None
    path = Path(path)
    if not path.exists():
        return None

    import json

    after = nudge["timestamp"]
    seen = 0
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("isSidechain") or entry.get("type") != "assistant":
                    continue
                ts = entry.get("timestamp") or ""
                if ts <= after:
                    continue
                msg = entry.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                text = " ".join(b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text")
                if not text.strip():
                    continue
                seen += 1
                low = text.lower()
                if any(m in low for m in SURFACE_MARKERS):
                    return True
                if seen >= within_messages:
                    return False
    except OSError:
        return None
    return False if seen else None


def _nearest_tool_call_id(conn, session_id, timestamp):
    """Last tool call recorded at or before a nudge, i.e. where it landed."""
    row = conn.execute(
        """SELECT id FROM tool_calls
            WHERE session_id = ? AND timestamp <= ?
            ORDER BY id DESC LIMIT 1""",
        (session_id, timestamp),
    ).fetchone()
    return row["id"] if row else 0


# A repeat-read nudge succeeds when the file is NOT read again. That is an
# absence of evidence, so it needs a minimum amount of subsequent reading
# before "didn't happen" means anything - otherwise a session that simply
# ended scores as compliance.
MIN_READS_TO_JUDGE_COMPLIANCE = 5


def classify_repeat_read(conn, nudge, lookahead=25):
    """Did Claude stop re-reading the file it was nudged about?

    Scored separately because this nudge's success condition is the *absence*
    of an action. The generic classifier looked only for new events and so
    recorded every compliance as 'ignored' - inverting the result for the one
    instruction Claude can follow entirely on its own.
    """
    subject = (nudge["subject"] or "").replace("\\", "/").lower()
    if not subject:
        return "pending", None

    anchor = _nearest_tool_call_id(conn, nudge["session_id"], nudge["timestamp"])
    rows = conn.execute(
        """SELECT file_path FROM tool_calls
            WHERE session_id = ? AND id > ? AND tool_name = 'Read'
              AND file_path IS NOT NULL
            ORDER BY id LIMIT ?""",
        (nudge["session_id"], anchor, lookahead),
    ).fetchall()

    again = sum(1 for r in rows
                if (r["file_path"] or "").replace("\\", "/").lower() == subject)
    if again:
        return "ignored", f"re-read {again}x more"
    if len(rows) < MIN_READS_TO_JUDGE_COMPLIANCE:
        # Too little subsequent reading for the absence to mean anything.
        return "pending", None
    return "complied", f"no further reads in {len(rows)}"


def classify(conn, nudge, window, claimed_events=None):
    """Work out what happened after one nudge.

    `claimed_events` is a set of tool_call ids already credited to an earlier
    nudge. Without it, two nudges fired minutes apart both "see" the same
    later compaction and each claim it as their own success - which is
    exactly what happened in round 1, inflating 3 real compactions into 4
    recorded wins. A single event can only be caused by one nudge.
    """
    if nudge["level"] == "repeat_read":
        outcome, detail = classify_repeat_read(conn, nudge)
        return outcome, detail, None

    session_id = nudge["session_id"]
    anchor = _nearest_tool_call_id(conn, session_id, nudge["timestamp"])

    rows = conn.execute(
        """SELECT id, tool_name, is_sidechain, running_context_tokens
             FROM tool_calls
            WHERE session_id = ? AND id > ?
            ORDER BY id LIMIT ?""",
        (session_id, anchor, window),
    ).fetchall()

    if not rows:
        return "pending", None, None

    for r in rows:
        if r["tool_name"] in DELEGATION_TOOLS or r["is_sidechain"]:
            if claimed_events is None or r["id"] not in claimed_events:
                return "delegated", None, r["id"]

    before = nudge["context_tokens"] or 0
    running = before
    for r in rows:
        c = r["running_context_tokens"]
        if c is None:
            continue
        if before and c < before * (1 - COMPACTION_DROP):
            if claimed_events is not None and r["id"] in claimed_events:
                break  # an earlier nudge already owns this compaction
            return ("compacted",
                    f"{_fmt_tokens(before)} -> {_fmt_tokens(c)}", r["id"])
        running = c

    if len(rows) < window:
        return "pending", None, None
    return "ignored", None, None


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

    # Via db.connect so schema migrations (and the message_version backfill)
    # are applied before anything is read.
    from . import db as _db
    conn = _db.connect(db_path)
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

    ACTED = ("delegated", "compacted", "surfaced", "complied")

    # Group by message version. Pooling outcomes from a message that no
    # longer exists with the current one measures neither - it produced a
    # meaningless 25% when the two versions were 17% and 50%.
    groups = {}
    for n in nudges:
        version = (n["message_version"] if "message_version" in n.keys() else None)
        groups.setdefault(version or "v1 (pre-instrumentation)", []).append(n)

    claimed = set()
    latest_rate = latest_judged = None
    for version, group in groups.items():
        tally = Counter()
        print(f"\n--- message {version} --- ({len(group)} nudge(s))")
        print(f"{'when':<20} {'level':<12} {'context':>8}  outcome")
        print("-" * 72)
        for n in group:
            outcome, detail, event_id = classify(conn, n, window, claimed)
            if event_id is not None:
                claimed.add(event_id)
            # Telling the user is the action the current message asks for.
            if outcome == "ignored" and surfaced_to_user(n):
                outcome, detail = "surfaced", "raised it with the user"
            tally[outcome] += 1
            when = (n["timestamp"] or "")[:19].replace("T", " ")
            ctx = _fmt_tokens(n["context_tokens"])
            line = f"{when:<20} {n['level']:<12} {ctx:>8}  {outcome}"
            if detail:
                line += f" ({detail})"
            print(line)

        judged = sum(v for k, v in tally.items() if k != "pending")
        acted = sum(tally[k] for k in ACTED)
        print("\n  " + "   ".join(f"{k} {v}" for k, v in sorted(tally.items())))
        if judged:
            rate = acted / judged * 100
            print(f"  acted on: {acted}/{judged} ({rate:.0f}%)")
            latest_rate, latest_judged = rate, judged
        else:
            print("  nothing judged yet")

    # The gate applies to the CURRENT message only - older versions are
    # history, not evidence about what ships today.
    print("\n" + "=" * 72)
    print(f"Gate (pre-committed) - evaluated against message "
          f"{MESSAGE_VERSION} only:")
    if latest_judged is None or latest_judged < 5:
        n = latest_judged or 0
        print(f"  NOT YET - {n} judged nudge(s) on the current message, "
              f"need at least 5.")
    elif latest_rate >= 50:
        print(f"  PASS - {latest_rate:.0f}% acted on across {latest_judged} "
              f"judged nudge(s).")
        print( "         The message works. Note the sample size in any "
               "public claim.")
    else:
        print(f"  FAIL - only {latest_rate:.0f}% acted on. Rewrite the message "
              f"(nudge.py:build_message),\n         bump MESSAGE_VERSION, and "
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
