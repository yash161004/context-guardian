#!/usr/bin/env python3
"""
Phase 0 - Validate the trigger for Context Guardian.

Reads one or more Claude Code transcript JSONL files
(~/.claude/projects/.../*.jsonl) and, for each turn, computes:

  - running context/token usage % (from cache_read + cache_creation +
    input token fields on assistant messages, relative to a
    configurable context window size)
  - a "repeat-read" counter: how many times the same file path has
    been Read within the last N Read calls (default: window=10, count>2)

It then prints a per-file report so you can eyeball it against the
points in the session where things *felt* like they were degrading,
and reports where the proposed trigger (context% >= THRESHOLD_PCT
and/or repeat_reads >= REPEAT_THRESHOLD) would have fired.

USAGE:
    python phase0_validation.py /path/to/transcript1.jsonl [transcript2.jsonl ...]

    # tune thresholds without editing the file:
    python phase0_validation.py --context-threshold 75 --repeat-window 10 \
        --repeat-threshold 3 /path/to/transcript.jsonl

    # full per-turn table (very long on real sessions):
    python phase0_validation.py --verbose /path/to/transcript.jsonl

This script does NOT modify anything - it's read-only analysis to
validate the trigger definition before Phase 1 builds the real hook.
"""

import argparse
import json
import sys
from collections import deque, Counter
from pathlib import Path

# Context window size in tokens, per model. This matters enormously for
# what "% full" means - the same 400k-token prompt is "over the limit" on a
# 200k model and "comfortably mid-session" on a 1M model. Getting this wrong
# invalidates the entire trigger.
MODEL_CONTEXT_WINDOWS = {
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
FALLBACK_CONTEXT_WINDOW = 1_000_000


def detect_context_window(models_seen):
    """Pick the context window from the models actually used in the session.

    Sessions can switch models mid-run, so we take the *smallest* window of
    any model seen: that is the binding constraint, and it is the conservative
    choice for a trigger meant to warn early.
    """
    windows = [MODEL_CONTEXT_WINDOWS[m] for m in models_seen if m in MODEL_CONTEXT_WINDOWS]
    return min(windows) if windows else FALLBACK_CONTEXT_WINDOW


def load_transcript(path: Path):
    """Yield parsed JSON objects from a Claude Code transcript JSONL file.
    Skips lines that fail to parse rather than crashing the whole run,
    since these files can contain occasional malformed/partial lines.
    """
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  [warn] {path.name}:{line_no} failed to parse ({e}); skipping",
                      file=sys.stderr)


def is_sidechain(entry):
    """Subagent (Task tool) turns run in their own separate context window.
    Their usage numbers say nothing about the main session's context
    pressure, so they must be excluded or the running % sawtooths.
    """
    return bool(entry.get("isSidechain"))


def extract_token_usage(entry):
    """Pull the prompt size from an assistant message entry, if present.

    Claude Code records `usage` on each assistant message. The size of the
    context actually sent to the model for that turn is:

        input_tokens + cache_read_input_tokens + cache_creation_input_tokens

    Cached and uncached prompt tokens are disjoint parts of the same prompt,
    so they sum. `output_tokens` is deliberately NOT included - it is the
    response, not the context window state at request time (it lands in the
    *next* turn's input). Including it double-counts.

    Returns None if this entry carries no usage info.
    """
    msg = entry.get("message", entry)
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not usage:
        return None

    input_tokens = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

    return input_tokens + cache_read + cache_creation


def extract_read_file_path(entry):
    """If this entry is a tool_use for a Read tool, return the file path
    it read. Returns None otherwise.
    """
    msg = entry.get("message", entry)
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return None

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "Read":
            file_path = (block.get("input") or {}).get("file_path")
            if file_path:
                return file_path
    return None


def collapse_runs(triggers):
    """Collapse consecutive trigger turns into (start, end, count) runs.
    Once a session is over the context threshold it usually stays there for
    hundreds of turns; listing each one buries the signal.
    """
    runs = []
    for t in triggers:
        if runs and t["turn"] == runs[-1]["end_turn"] + 1:
            runs[-1]["end_turn"] = t["turn"]
            runs[-1]["end_line"] = t["line_no"]
            runs[-1]["count"] += 1
            runs[-1]["peak_pct"] = max(runs[-1]["peak_pct"], t["context_pct"])
        else:
            runs.append({
                "start_turn": t["turn"], "end_turn": t["turn"],
                "start_line": t["line_no"], "end_line": t["line_no"],
                "count": 1, "peak_pct": t["context_pct"],
            })
    return runs


def analyze(path: Path, context_window, repeat_window: int,
            repeat_threshold: int, context_threshold_pct: float,
            verbose: bool = False):
    print(f"\n{'=' * 70}")
    print(f"=== {path.name}")
    print(f"{'=' * 70}")

    entries = list(load_transcript(path))

    # Detect which model(s) the session actually ran on, so the "% full"
    # figure is relative to the real window rather than a guess.
    models_seen = Counter()
    for _, entry in entries:
        if is_sidechain(entry):
            continue
        msg = entry.get("message")
        if isinstance(msg, dict) and msg.get("model"):
            models_seen[msg["model"]] += 1

    if context_window is None:
        context_window = detect_context_window(models_seen)
        source = "auto-detected"
    else:
        source = "user-specified"
    model_desc = ", ".join(f"{m} x{c}" for m, c in models_seen.most_common()) or "unknown"
    print(f"models: {model_desc}")
    print(f"context window: {context_window:,} tokens ({source})")

    read_history = deque(maxlen=repeat_window)
    read_totals = Counter()
    running_tokens = 0
    turn = 0
    peak_tokens = 0
    context_triggers = []
    repeat_events = []
    first_context_cross = None
    sidechain_skipped = 0

    for line_no, entry in entries:
        if is_sidechain(entry):
            sidechain_skipped += 1
            continue

        tokens = extract_token_usage(entry)
        if tokens is not None:
            # usage is a per-turn snapshot of prompt size, not cumulative
            running_tokens = tokens
            peak_tokens = max(peak_tokens, tokens)
            turn += 1
        context_pct = (running_tokens / context_window) * 100 if context_window else 0

        file_path = extract_read_file_path(entry)
        repeat_reads = 0
        if file_path:
            read_history.append(file_path)
            read_totals[file_path] += 1
            repeat_reads = list(read_history).count(file_path)
            if repeat_reads >= repeat_threshold:
                repeat_events.append({
                    "line_no": line_no, "turn": turn, "context_pct": context_pct,
                    "file_path": file_path, "repeat_reads": repeat_reads,
                })

        if tokens is not None and context_pct >= context_threshold_pct:
            context_triggers.append({
                "line_no": line_no, "turn": turn, "context_pct": context_pct,
            })
            if first_context_cross is None:
                first_context_cross = (turn, line_no, context_pct)

        if verbose and (tokens is not None or file_path):
            detail = f"turn {turn:>4} line {line_no:>5} | context {context_pct:5.1f}%"
            if file_path:
                detail += f" | read: {file_path} (x{repeat_reads} in window)"
            fired = (context_pct >= context_threshold_pct) or (repeat_reads >= repeat_threshold)
            print(detail + ("  <-- TRIGGER" if fired else ""))

    # ---- report -------------------------------------------------------
    print(f"\nassistant turns (main session): {turn}"
          f"   sidechain entries skipped: {sidechain_skipped}")
    print(f"peak context: {peak_tokens:,} tokens "
          f"({peak_tokens / context_window * 100:.1f}% of {context_window:,})")

    print(f"\n-- context trigger (>= {context_threshold_pct}%) --")
    if first_context_cross:
        t, ln, pct = first_context_cross
        print(f"  first crossing: turn {t}/{turn} ({t / turn * 100:.0f}% of the way "
              f"into the session), line {ln}, at {pct:.1f}%")
        print(f"  {len(context_triggers)} of {turn} turns above threshold "
              f"({len(context_triggers) / turn * 100:.0f}% of session)")
        runs = collapse_runs(context_triggers)
        print(f"  {len(runs)} contiguous run(s):")
        for r in runs[:10]:
            print(f"    turns {r['start_turn']}-{r['end_turn']} "
                  f"(lines {r['start_line']}-{r['end_line']}), "
                  f"{r['count']} turns, peak {r['peak_pct']:.1f}%")
        if len(runs) > 10:
            print(f"    ... and {len(runs) - 10} more run(s)")
    else:
        print("  never crossed")

    print(f"\n-- repeat-read trigger (same file >= {repeat_threshold}x "
          f"within last {repeat_window} reads) --")
    if repeat_events:
        print(f"  {len(repeat_events)} event(s):")
        for ev in repeat_events[:15]:
            print(f"    turn {ev['turn']:>4} line {ev['line_no']:>5} | "
                  f"context {ev['context_pct']:5.1f}% | x{ev['repeat_reads']} | "
                  f"{ev['file_path']}")
        if len(repeat_events) > 15:
            print(f"    ... and {len(repeat_events) - 15} more")
    else:
        print("  never fired")

    hot = [(f, c) for f, c in read_totals.most_common(5) if c > 1]
    if hot:
        print("\n-- most re-read files (whole session, no window) --")
        for f, c in hot:
            print(f"    {c:>3}x  {f}")

    return {
        "turns": turn,
        "peak_tokens": peak_tokens,
        "context_window": context_window,
        "context_triggers": context_triggers,
        "repeat_events": repeat_events,
        "first_context_cross": first_context_cross,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("transcripts", nargs="+", type=Path,
                        help="Path(s) to .jsonl transcript files")
    parser.add_argument("--context-window", type=int, default=None,
                        help="Model context window in tokens. Default: auto-detect "
                             "from the model recorded in each transcript.")
    parser.add_argument("--context-threshold", type=float, default=75.0,
                        help="Context %% at which the trigger should fire (default: 75.0)")
    parser.add_argument("--repeat-window", type=int, default=10,
                        help="Number of recent Read calls to consider for repeat-read detection (default: 10)")
    parser.add_argument("--repeat-threshold", type=int, default=3,
                        help="Number of repeat reads of the same file within the window to trigger (default: 3)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print the full per-turn table (very long)")
    args = parser.parse_args()

    results = {}
    for transcript_path in args.transcripts:
        if not transcript_path.exists():
            print(f"[error] file not found: {transcript_path}", file=sys.stderr)
            continue
        results[transcript_path.name] = analyze(
            transcript_path,
            context_window=args.context_window,
            repeat_window=args.repeat_window,
            repeat_threshold=args.repeat_threshold,
            context_threshold_pct=args.context_threshold,
            verbose=args.verbose,
        )

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'transcript':<20} {'turns':>6} {'window':>10} {'peak tok':>10} "
          f"{'peak%':>7} {'1st>thr':>9} {'rpt':>5}")
    for name, r in results.items():
        peak_pct = r["peak_tokens"] / r["context_window"] * 100
        cross = f"turn {r['first_context_cross'][0]}" if r["first_context_cross"] else "never"
        print(f"{name[:20]:<20} {r['turns']:>6} {r['context_window']:>10,} "
              f"{r['peak_tokens']:>10,} {peak_pct:>6.1f}% {cross:>9} "
              f"{len(r['repeat_events']):>5}")

    print(
        "\nNext step: compare these trigger points against the moments "
        "you remember the session degrading. If they line up, lock in "
        "these thresholds for Phase 1. If not, adjust --context-threshold "
        "/ --repeat-window / --repeat-threshold and re-run until they do."
    )


if __name__ == "__main__":
    main()
