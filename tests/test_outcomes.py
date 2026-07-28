"""Tests for the nudge-outcome tracker.

The gate this feeds decides whether the project launches, so the
classification must not be able to flatter itself.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from context_guardian import db  # noqa: E402
from context_guardian.outcomes import classify  # noqa: E402

T0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def ts(offset_seconds):
    return (T0 + timedelta(seconds=offset_seconds)).isoformat()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.db")
    yield c
    c.close()


def add_call(conn, session="s", offset=0, tool="Read", sidechain=False,
             tokens=280_000):
    db.record_tool_call(
        conn, session_id=session, tool_name=tool, file_path="/a.py",
        is_sidechain=sidechain, timestamp=ts(offset),
        running_context_tokens=tokens)


def add_nudge(conn, session="s", offset=0, tokens=280_000, level="warn"):
    db.record_nudge(conn, session_id=session, level=level, message="m",
                    context_tokens=tokens, timestamp=ts(offset))
    return conn.execute("SELECT * FROM nudges ORDER BY id DESC LIMIT 1").fetchone()


def test_delegation_via_task_tool_is_detected(conn):
    add_call(conn, offset=0)
    n = add_nudge(conn, offset=1)
    add_call(conn, offset=2, tool="Task")
    assert classify(conn, n, window=15)[0] == "delegated"


def test_delegation_via_sidechain_rows_is_detected(conn):
    add_call(conn, offset=0)
    n = add_nudge(conn, offset=1)
    add_call(conn, offset=2, tool="Read", sidechain=True)
    assert classify(conn, n, window=15)[0] == "delegated"


def test_compaction_is_detected_from_a_context_drop(conn):
    add_call(conn, offset=0, tokens=280_000)
    n = add_nudge(conn, offset=1, tokens=280_000)
    add_call(conn, offset=2, tokens=40_000)
    outcome, detail = classify(conn, n, window=15)
    assert outcome == "compacted"
    assert "280k" in detail and "40k" in detail


def test_small_context_dip_is_not_a_compaction(conn):
    """Ordinary turn-to-turn variation must not be scored as success."""
    add_call(conn, offset=0, tokens=280_000)
    n = add_nudge(conn, offset=1, tokens=280_000)
    for i in range(15):
        add_call(conn, offset=2 + i, tokens=270_000)
    assert classify(conn, n, window=15)[0] == "ignored"


def test_ignored_when_window_passes_with_no_change(conn):
    add_call(conn, offset=0)
    n = add_nudge(conn, offset=1)
    for i in range(15):
        add_call(conn, offset=2 + i, tool="Edit")
    assert classify(conn, n, window=15)[0] == "ignored"


def test_pending_when_not_enough_activity_yet(conn):
    add_call(conn, offset=0)
    n = add_nudge(conn, offset=1)
    add_call(conn, offset=2, tool="Edit")
    assert classify(conn, n, window=15)[0] == "pending"


def test_pending_when_nothing_followed(conn):
    add_call(conn, offset=0)
    n = add_nudge(conn, offset=1)
    assert classify(conn, n, window=15)[0] == "pending"


def test_activity_before_the_nudge_is_not_credited(conn):
    """A subagent spawned BEFORE the nudge must not count as a response."""
    add_call(conn, offset=0, tool="Task")
    n = add_nudge(conn, offset=1)
    for i in range(15):
        add_call(conn, offset=2 + i, tool="Edit")
    assert classify(conn, n, window=15)[0] == "ignored"


def test_other_sessions_do_not_leak_into_the_outcome(conn):
    add_call(conn, session="s", offset=0)
    n = add_nudge(conn, session="s", offset=1)
    for i in range(15):
        add_call(conn, session="other", offset=2 + i, tool="Task")
    assert classify(conn, n, window=15)[0] == "pending"
