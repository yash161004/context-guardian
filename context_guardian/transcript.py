"""Reading and interpreting Claude Code transcript JSONL files.

This is where all three Phase 0 measurement bugs are fixed, and it is the
only module that should know the transcript's on-disk shape.
"""

import json
import sys
from pathlib import Path

# Context window per model. Phase 1 triggers on absolute tokens, so this is
# not used for thresholding - it is recorded alongside the token counts so
# Phase 4 can reason about real users on a mix of window sizes, and so the
# sensor can sanity-check that it never reports >100% of a real window.
MODEL_CONTEXT_WINDOWS = {
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
FALLBACK_CONTEXT_WINDOW = 1_000_000

# Tools whose file_path we treat as a "read" for repeat-read detection.
READ_TOOLS = {"Read"}


def context_window_for(model):
    """Window size for a model name, with a safe fallback for unknown models."""
    if not model:
        return FALLBACK_CONTEXT_WINDOW
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]
    # Unknown/future model ids: match on prefix before guessing.
    for known, window in MODEL_CONTEXT_WINDOWS.items():
        if model.startswith(known):
            return window
    return FALLBACK_CONTEXT_WINDOW


def iter_transcript(path, warn=True):
    """Yield (line_no, entry) for each parseable line of a transcript.

    Malformed lines are skipped with a stderr warning rather than raising -
    a sensor that crashes blocks the user's real tool call.
    """
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield line_no, json.loads(line)
                except json.JSONDecodeError as e:
                    if warn:
                        print(f"[context-guardian] {path.name}:{line_no} "
                              f"unparseable ({e}); skipping", file=sys.stderr)
    except OSError as e:
        if warn:
            print(f"[context-guardian] could not read transcript {path} ({e})",
                  file=sys.stderr)


def is_sidechain(entry):
    """True for subagent turns.

    Subagents run in their own context window, so their usage says nothing
    about the main session's context pressure. Folding them in was Phase 0
    Bug #3 - and it would corrupt precisely the delegating sessions this
    tool exists to encourage.
    """
    return bool(entry.get("isSidechain"))


def extract_prompt_tokens(entry):
    """Size of the prompt sent to the model for this turn, or None.

        input_tokens + cache_read_input_tokens + cache_creation_input_tokens

    Cached and uncached prompt tokens are disjoint parts of one prompt, so
    they sum. `output_tokens` is deliberately excluded: it is the response
    being generated, and it arrives as *input* on the next turn. Including
    it double-counts (Phase 0 Bug #2).
    """
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    return ((usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0))


def extract_model(entry):
    msg = entry.get("message")
    if isinstance(msg, dict):
        model = msg.get("model")
        # Claude Code writes "<synthetic>" for locally-generated messages
        # (e.g. interrupt notices); they carry no real model identity.
        if model and not model.startswith("<"):
            return model
    return None


def extract_tool_uses(entry):
    """Yield (tool_name, tool_input) for each tool_use block in an entry."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name"), (block.get("input") or {})


def extract_read_path(entry):
    """File path read by this entry, if it is a Read tool call."""
    for name, tool_input in extract_tool_uses(entry):
        if name in READ_TOOLS:
            fp = tool_input.get("file_path")
            if fp:
                return fp
    return None


def latest_context_state(path, tail_bytes=512_000):
    """Most recent main-chain (prompt_tokens, model) in a transcript.

    Reads only the tail of the file rather than parsing it whole: the sensor
    runs on *every* tool call, and these transcripts reach several MB. Doing
    a full parse each time would add real latency to every tool the user runs.

    Returns (None, None) if no usable entry is found in the tail.

    Note: Claude Code writes the transcript asynchronously, so it may lag the
    in-memory conversation by a turn. For threshold detection at the 200k/350k
    scale a one-turn lag is immaterial, but it does mean this is a
    near-real-time reading, not an exact one.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # discard the partial first line
            chunk = f.read()
    except OSError as e:
        print(f"[context-guardian] could not tail transcript {path} ({e})",
              file=sys.stderr)
        return None, None

    tokens = model = None
    for raw in reversed(chunk.splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if is_sidechain(entry):
            continue
        t = extract_prompt_tokens(entry)
        if t is not None:
            tokens = t
            model = extract_model(entry)
            break

    return tokens, model
