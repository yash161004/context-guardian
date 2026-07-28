"""Offline replay of a transcript through the sensor's logic.

The live hook is driven by PostToolUse events; the tests need to drive the
same measurement logic over the Phase 0 corpus. This module is that bridge,
so tests exercise the real detector and the real schema rather than a
parallel reimplementation that could drift.
"""

from datetime import datetime, timezone
from pathlib import Path

from . import db, transcript
from .config import DEFAULTS
from .detector import RepeatReadDetector, is_scratchpad


def replay_transcript(conn, path, cfg=None, session_id=None):
    """Replay one transcript file into the state DB.

    Walks the transcript in order, tracking the main-chain context size and
    the rolling read-window exactly as the live sensor does, and writes one
    row per tool call.

    Returns a summary dict for assertions.
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    path = Path(path)
    session_id = session_id or path.stem

    detector = RepeatReadDetector(
        window=cfg["repeat_read_window"],
        threshold=cfg["repeat_read_threshold"],
        scratchpad_patterns=cfg["scratchpad_path_patterns"],
        window_counts=cfg["repeat_read_window_counts"],
    )

    running = None
    model = None
    peak = 0
    rows = 0
    repeat_events = []
    skipped_sidechain = 0
    ts = datetime.now(timezone.utc).isoformat()

    for _, entry in transcript.iter_transcript(path):
        sidechain = transcript.is_sidechain(entry)
        if sidechain:
            skipped_sidechain += 1

        prompt_tokens = transcript.extract_prompt_tokens(entry)
        entry_model = transcript.extract_model(entry)
        if entry_model:
            model = entry_model

        if prompt_tokens is not None and not sidechain:
            running = prompt_tokens
            peak = max(peak, prompt_tokens)

        window = transcript.context_window_for(model) if model else None

        for tool_name, tool_input in transcript.extract_tool_uses(entry):
            file_path = tool_input.get("file_path")

            repeat_count = None
            if (tool_name in transcript.READ_TOOLS and file_path
                    and not sidechain):
                if not is_scratchpad(file_path, cfg["scratchpad_path_patterns"]):
                    repeat_count = detector.record(
                        tool_name, file_path, tuple(transcript.READ_TOOLS))
                    if detector.fires(repeat_count):
                        repeat_events.append({
                            "file_path": file_path,
                            "count": repeat_count,
                            "running_context_tokens": running,
                        })
            elif cfg["repeat_read_window_counts"] == "tool_calls" and not sidechain:
                detector.record(tool_name, None, tuple(transcript.READ_TOOLS))

            db.record_tool_call(
                conn,
                session_id=session_id,
                tool_name=tool_name,
                file_path=file_path,
                is_sidechain=sidechain,
                timestamp=ts,
                model=model,
                context_window=window,
                prompt_tokens=prompt_tokens,
                running_context_tokens=None if sidechain else running,
                repeat_read_count=repeat_count,
                commit=False,
            )
            rows += 1

    conn.commit()
    return {
        "session_id": session_id,
        "rows": rows,
        "peak_context_tokens": peak,
        "repeat_events": repeat_events,
        "skipped_sidechain": skipped_sidechain,
        "model": model,
        "context_window": transcript.context_window_for(model) if model else None,
    }
