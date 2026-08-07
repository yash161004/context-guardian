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
from context_guardian.outcomes import classify, classify_repeat_read  # noqa: E402

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
    outcome, detail, _ = classify(conn, n, window=15)
    assert outcome == "compacted"
    assert "280k" in detail and "40k" in detail


def test_one_compaction_cannot_be_claimed_by_two_nudges(conn):
    """Round-1 bug: nudges 7 minutes apart both credited the same later
    compaction, inflating 3 real events into 4 recorded wins."""
    add_call(conn, offset=0, tokens=266_000)
    first = add_nudge(conn, offset=1, tokens=266_000)
    add_call(conn, offset=2, tokens=399_000)
    second = add_nudge(conn, offset=3, tokens=399_000)
    add_call(conn, offset=4, tokens=60_000)          # the single compaction
    for i in range(15):
        add_call(conn, offset=5 + i, tokens=61_000)

    claimed = set()
    o1, _, e1 = classify(conn, first, window=15, claimed_events=claimed)
    claimed.add(e1)
    o2, _, e2 = classify(conn, second, window=15, claimed_events=claimed)

    assert o1 == "compacted"
    assert o2 != "compacted", "the same compaction was counted twice"


def test_delegation_is_also_only_credited_once(conn):
    add_call(conn, offset=0)
    first = add_nudge(conn, offset=1)
    second = add_nudge(conn, offset=2)
    add_call(conn, offset=3, tool="Task")
    for i in range(15):
        add_call(conn, offset=4 + i, tool="Edit")

    claimed = set()
    o1, _, e1 = classify(conn, first, window=15, claimed_events=claimed)
    claimed.add(e1)
    o2, _, _ = classify(conn, second, window=15, claimed_events=claimed)
    assert o1 == "delegated"
    assert o2 != "delegated"


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


def add_read(conn, path, session="s", offset=0):
    db.record_tool_call(
        conn, session_id=session, tool_name="Read", file_path=path,
        is_sidechain=False, timestamp=ts(offset), running_context_tokens=150_000)


def test_repeat_read_success_is_the_absence_of_a_re_read(conn):
    """This nudge succeeds by nothing happening. Scoring it with the generic
    classifier recorded every compliance as 'ignored' - inverting the result
    for the one instruction Claude can follow entirely on its own."""
    add_read(conn, "/proj/hot.py", offset=0)
    db.record_nudge(conn, session_id="s", level="repeat_read",
                    subject="/proj/hot.py", message="m", context_tokens=150_000,
                    timestamp=ts(1))
    n = conn.execute("SELECT * FROM nudges ORDER BY id DESC LIMIT 1").fetchone()
    for i in range(10):
        add_read(conn, f"/proj/other{i}.py", offset=2 + i)

    outcome, detail = classify_repeat_read(conn, n)
    assert outcome == "complied"
    assert "no further reads" in detail


def test_repeat_read_that_continues_is_ignored(conn):
    add_read(conn, "/proj/hot.py", offset=0)
    db.record_nudge(conn, session_id="s", level="repeat_read",
                    subject="/proj/hot.py", message="m", context_tokens=150_000,
                    timestamp=ts(1))
    n = conn.execute("SELECT * FROM nudges ORDER BY id DESC LIMIT 1").fetchone()
    for i in range(8):
        add_read(conn, f"/proj/other{i}.py", offset=2 + i)
    add_read(conn, "/proj/HOT.py", offset=20)      # same file, different case

    outcome, detail = classify_repeat_read(conn, n)
    assert outcome == "ignored"
    assert "re-read" in detail


def test_a_session_that_simply_stopped_is_not_scored_as_compliance(conn):
    """Guards the finding: 'no further reads' means nothing if there was no
    further reading at all."""
    add_read(conn, "/proj/hot.py", offset=0)
    db.record_nudge(conn, session_id="s", level="repeat_read",
                    subject="/proj/hot.py", message="m", context_tokens=150_000,
                    timestamp=ts(1))
    n = conn.execute("SELECT * FROM nudges ORDER BY id DESC LIMIT 1").fetchone()
    add_read(conn, "/proj/one.py", offset=2)       # only 1 read afterwards

    assert classify_repeat_read(conn, n)[0] == "pending"


def test_message_version_is_backfilled_from_the_message_text(tmp_path):
    """v1 messages recommended a subagent; v2 messages never do. The row
    carries its own evidence, which beats guessing from a commit date."""
    path = tmp_path / "state.db"
    c = db.connect(path)
    c.execute("""INSERT INTO nudges (session_id, level, timestamp, active, message)
                 VALUES ('s','warn','t',1,'... consider delegating to a subagent ...')""")
    c.execute("""INSERT INTO nudges (session_id, level, timestamp, active, message)
                 VALUES ('s','warn','t',1,'... their command to run, not yours ...')""")
    c.commit()
    c.close()

    c = db.connect(path)   # migration runs again
    versions = [r["message_version"] for r in
                c.execute("SELECT message_version FROM nudges ORDER BY id")]
    assert versions == ["v1", "v2"]
    c.close()


def test_backfill_runs_even_when_the_column_already_exists(tmp_path):
    """The bug this caused: the backfill was conditioned on having just added
    the column, so every database created by an earlier version kept NULLs
    forever and the report silently pooled both message versions."""
    path = tmp_path / "state.db"
    c = db.connect(path)
    c.execute("""INSERT INTO nudges (session_id, level, timestamp, active,
                                     message, message_version)
                 VALUES ('s','warn','t',1,'no keyword here', NULL)""")
    c.commit()
    c.close()

    c = db.connect(path)
    row = c.execute("SELECT message_version FROM nudges").fetchone()
    assert row["message_version"] == "v2"
    c.close()


def test_other_sessions_do_not_leak_into_the_outcome(conn):
    add_call(conn, session="s", offset=0)
    n = add_nudge(conn, session="s", offset=1)
    for i in range(15):
        add_call(conn, session="other", offset=2 + i, tool="Task")
    assert classify(conn, n, window=15)[0] == "pending"
