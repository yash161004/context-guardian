"""Phase 1 sensor tests - green against the 6 Phase 0 transcripts.

The corpus lives outside the repo (it is the author's real session history),
so corpus-dependent tests skip cleanly when it is absent. Point
CONTEXT_GUARDIAN_CORPUS at a directory of .jsonl transcripts to run them
elsewhere.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from context_guardian import db, transcript  # noqa: E402
from context_guardian.config import DEFAULTS  # noqa: E402
from context_guardian.detector import RepeatReadDetector, is_scratchpad  # noqa: E402
from context_guardian.replay import replay_transcript  # noqa: E402

# The Phase 0 corpus: 6 transcripts, 3,306 main-session turns.
CORPUS_FILES = [
    "d--TrustMesh-TrustMesh/d89ba4e2-0de2-4f91-a7d5-20d553232b2d.jsonl",
    "D--fixtura/0dc10ab5-ebc5-4f25-b6ab-414d2ec30fa5.jsonl",
    "d--TrustMesh-TrustMesh/4ef64bd8-0c32-4040-b70b-b23b0a1237c7.jsonl",
    "d--TrustMesh-TrustMesh/4280f2e7-2a29-42cc-a2ea-1fc68c5efd53.jsonl",
    "d--TrustMesh-TrustMesh/5e8a9449-5549-404d-8bff-92043cd92daa.jsonl",
    "d--TrustMesh-TrustMesh/39ff5e29-7094-46f1-88b7-6cf853b05812.jsonl",
]

# Known-correct value from Phase 0's corrected analysis (the fixtura session).
# This is the regression check that the three Phase 0 extraction bugs stay fixed.
EXPECTED_CORPUS_PEAK = 663_026

# What the *sensor* can actually observe is slightly lower, and this gap is
# structural rather than a bug: a PostToolUse hook only ever samples at tool
# calls, and the 663,026-token peak occurred on a text-only assistant turn
# that issued no tools. The sensor's best view of that moment is the previous
# tool call, at 662,671 (0.05% low). Documented in docs/phase1-notes.md.
EXPECTED_SENSOR_OBSERVED_PEAK = 662_671

# Per-session peaks from the same corrected analysis.
EXPECTED_PEAKS = {
    "d89ba4e2-0de2-4f91-a7d5-20d553232b2d": 420_229,
    "0dc10ab5-ebc5-4f25-b6ab-414d2ec30fa5": 663_026,
    "4ef64bd8-0c32-4040-b70b-b23b0a1237c7": 520_481,
    "4280f2e7-2a29-42cc-a2ea-1fc68c5efd53": 388_610,
    "5e8a9449-5549-404d-8bff-92043cd92daa": 317_771,
    "39ff5e29-7094-46f1-88b7-6cf853b05812": 239_177,
}

# Phase 0 found 12 repeat-read events; 4 were scratchpad polling of
# tasks/*.output. The denylist must remove exactly those 4.
PHASE0_TOTAL_REPEAT_EVENTS = 12
PHASE0_SCRATCHPAD_FALSE_POSITIVES = 4
EXPECTED_REAL_REPEAT_EVENTS = (PHASE0_TOTAL_REPEAT_EVENTS
                               - PHASE0_SCRATCHPAD_FALSE_POSITIVES)

# Of those 8 genuine events, 7 fire while the session is ALREADY over the
# 200k warn threshold (263k-366k). This corrects the Phase 0 note, which
# claimed the two signals were decoupled - that read came from thinking in
# percentages (16-37% of a 1M window is 160k-370k, i.e. mostly above 200k)
# and from the excluded scratchpad events sitting at the low end.
EXPECTED_REPEAT_EVENTS_ABOVE_WARN = 7


def corpus_root():
    env = os.environ.get("CONTEXT_GUARDIAN_CORPUS")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "projects"


def corpus_paths():
    root = corpus_root()
    paths = [root / rel for rel in CORPUS_FILES]
    return [p for p in paths if p.exists()]


requires_corpus = pytest.mark.skipif(
    len(corpus_paths()) < len(CORPUS_FILES),
    reason="Phase 0 corpus not available (set CONTEXT_GUARDIAN_CORPUS)",
)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.db")
    yield c
    c.close()


@pytest.fixture
def replayed(conn):
    """Replay the whole corpus once; shared across corpus assertions."""
    results = {}
    for path in corpus_paths():
        results[path.stem] = replay_transcript(conn, path)
    return conn, results


# --------------------------------------------------------------------------
# Section 4, bullet 1 - peak context is the known-correct 663k
# --------------------------------------------------------------------------

@requires_corpus
def test_corpus_peak_context_matches_phase0(replayed):
    conn, results = replayed
    peak = max(r["peak_context_tokens"] for r in results.values())
    assert peak == EXPECTED_CORPUS_PEAK


@requires_corpus
def test_sensor_observed_peak_is_bounded_by_transcript_peak(replayed):
    """The sensor samples at tool calls, so its peak can trail the true one.

    Asserting the exact observed value pins the size of that blind spot: if a
    change to the extraction logic made it larger, this fails loudly rather
    than silently degrading the sensor's accuracy.
    """
    conn, _ = replayed
    row = conn.execute(
        "SELECT MAX(running_context_tokens) AS peak FROM tool_calls "
        "WHERE is_sidechain = 0"
    ).fetchone()
    assert row["peak"] == EXPECTED_SENSOR_OBSERVED_PEAK
    assert row["peak"] <= EXPECTED_CORPUS_PEAK
    # The blind spot must stay negligible relative to the thresholds.
    assert (EXPECTED_CORPUS_PEAK - row["peak"]) / EXPECTED_CORPUS_PEAK < 0.01


@requires_corpus
@pytest.mark.parametrize("session_id,expected", sorted(EXPECTED_PEAKS.items()))
def test_per_session_peak_matches_phase0(replayed, session_id, expected):
    _, results = replayed
    assert results[session_id]["peak_context_tokens"] == expected


# --------------------------------------------------------------------------
# Section 4, bullet 2 - never report >100% of a correctly detected window
# --------------------------------------------------------------------------

@requires_corpus
def test_no_row_exceeds_its_context_window(replayed):
    """Guards against Phase 0 Bug #1 recurring.

    Against a wrongly-hardcoded 200k window this corpus reported peaks of
    210-331%. Any row over 100% of its *detected* window means the
    denominator has drifted wrong again.
    """
    conn, _ = replayed
    bad = conn.execute(
        """SELECT session_id, running_context_tokens, context_window
             FROM tool_calls
            WHERE is_sidechain = 0
              AND context_window IS NOT NULL
              AND running_context_tokens > context_window"""
    ).fetchall()
    assert bad == [], f"{len(bad)} row(s) exceed their context window"


@requires_corpus
def test_windows_detected_as_1m(replayed):
    _, results = replayed
    for session_id, r in results.items():
        assert r["context_window"] == 1_000_000, session_id


def test_output_tokens_are_not_counted():
    """Phase 0 Bug #2 - output_tokens must not inflate the prompt size."""
    entry = {
        "type": "assistant",
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 500,
                "output_tokens": 9999,
            },
        },
    }
    assert transcript.extract_prompt_tokens(entry) == 1510


def test_sidechain_entries_are_flagged():
    """Phase 0 Bug #3 - subagent turns must be identifiable and excluded."""
    main = {"isSidechain": False, "message": {"usage": {"input_tokens": 1}}}
    side = {"isSidechain": True, "message": {"usage": {"input_tokens": 1}}}
    assert transcript.is_sidechain(main) is False
    assert transcript.is_sidechain(side) is True


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-haiku-4-5", 200_000),
    ("claude-haiku-4-5-20251001", 200_000),
    ("some-unknown-model", 1_000_000),
])
def test_context_window_detection(model, expected):
    assert transcript.context_window_for(model) == expected


def test_sidechain_rows_excluded_from_running_total(conn):
    """A subagent turn must not move the main session's running total."""
    from context_guardian.replay import replay_transcript as _rt  # noqa: F401

    db.record_tool_call(conn, session_id="s", tool_name="Read", file_path="a.py",
                        is_sidechain=False, timestamp="t",
                        running_context_tokens=100)
    db.record_tool_call(conn, session_id="s", tool_name="Read", file_path="b.py",
                        is_sidechain=True, timestamp="t",
                        running_context_tokens=None)
    assert db.last_running_context(conn, "s") == 100


# --------------------------------------------------------------------------
# Section 4, bullet 3 - exactly the Phase 0 repeat-reads, minus scratchpad
# --------------------------------------------------------------------------

@requires_corpus
def test_repeat_read_events_exclude_scratchpad_false_positives(replayed):
    _, results = replayed
    events = [e for r in results.values() for e in r["repeat_events"]]

    assert len(events) == EXPECTED_REAL_REPEAT_EVENTS, \
        f"expected {EXPECTED_REAL_REPEAT_EVENTS} real events, got: {events}"

    for e in events:
        assert not is_scratchpad(e["file_path"], DEFAULTS["scratchpad_path_patterns"])


@requires_corpus
def test_repeat_reads_mostly_co_occur_with_high_context(replayed):
    """Corrects the Phase 0 note: the two signals are NOT decoupled.

    7 of the 8 genuine repeat-read events fire while the session is already
    past the 200k warn threshold. This matters for Phase 2: a naive
    "fire on either signal" design would emit two nudges for one situation
    in the overwhelming majority of cases, so the nudge must dedupe.
    """
    _, results = replayed
    events = [e for r in results.values() for e in r["repeat_events"]]
    assert len(events) == EXPECTED_REAL_REPEAT_EVENTS

    above = [e for e in events
             if (e["running_context_tokens"] or 0) >= DEFAULTS["context_warn_tokens"]]
    assert len(above) == EXPECTED_REPEAT_EVENTS_ABOVE_WARN

    # The one low-context event is the reason repeat-reads still earn their
    # keep as a separate signal - it is invisible to the token threshold.
    below = [e for e in events if e not in above]
    assert len(below) == 1
    assert below[0]["running_context_tokens"] < DEFAULTS["context_warn_tokens"]


@pytest.mark.parametrize("path,expected", [
    (r"C:\Users\X\AppData\Local\Temp\claude\proj\sess\tasks\biiofvdb2.output", True),
    (r"C:\Users\X\AppData\Local\Temp\claude\proj\sess\scratchpad\run.log", True),
    ("/tmp/claude/proj/sess/tasks/abc.output", True),
    (r"D:\TrustMesh\TrustMesh\backend\app\session_manager.py", False),
    (r"D:\TrustMesh\TrustMesh\.claude\launch.json", False),
    ("/home/u/.claude/projects/foo/bar.jsonl", True),
])
def test_scratchpad_matching(path, expected):
    """`.claude/launch.json` is real project config, not scratchpad - it must
    NOT be excluded, or genuine re-reads of project config go unseen."""
    assert is_scratchpad(path, DEFAULTS["scratchpad_path_patterns"]) is expected


def test_scratchpad_reads_do_not_consume_window_slots():
    """A burst of task-polling must not flush genuine repeat-reads out of
    the rolling window and mask a real signal."""
    d = RepeatReadDetector(window=4, threshold=3,
                           scratchpad_patterns=DEFAULTS["scratchpad_path_patterns"])
    assert d.record("Read", "/proj/a.py") == 1
    for i in range(5):
        assert d.record("Read", f"/tmp/claude/s/tasks/t{i}.output") is None
    assert d.record("Read", "/proj/a.py") == 2
    assert d.record("Read", "/proj/a.py") == 3


def test_repeat_detector_normalises_windows_paths():
    d = RepeatReadDetector(window=10, threshold=3)
    assert d.record("Read", r"D:\App\Main.py") == 1
    assert d.record("Read", "d:/app/main.py") == 2
    assert d.fires(d.record("Read", r"D:\app\Main.PY")) is True


def test_non_read_tools_are_not_counted():
    d = RepeatReadDetector(window=10, threshold=3)
    assert d.record("Edit", "/proj/a.py") is None
    assert d.record("Write", "/proj/a.py") is None
    assert d.record("Read", "/proj/a.py") == 1


# --------------------------------------------------------------------------
# Section 4, bullet 4 - malformed input never crashes
# --------------------------------------------------------------------------

def test_malformed_jsonl_lines_are_skipped(tmp_path, capsys):
    t = tmp_path / "broken.jsonl"
    t.write_text(
        json.dumps({"message": {"model": "claude-opus-5",
                                "usage": {"input_tokens": 100,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}}) + "\n"
        + "{not valid json at all\n"
        + "\n"
        + '{"truncated": \n'
        + json.dumps({"message": {"model": "claude-opus-5",
                                  "usage": {"input_tokens": 250,
                                            "cache_read_input_tokens": 0,
                                            "cache_creation_input_tokens": 0}}}) + "\n",
        encoding="utf-8",
    )
    entries = list(transcript.iter_transcript(t))
    assert len(entries) == 2
    assert "unparseable" in capsys.readouterr().err


def test_replay_survives_malformed_transcript(conn, tmp_path):
    t = tmp_path / "broken.jsonl"
    t.write_text("{{{garbage\n" + json.dumps({
        "message": {
            "model": "claude-opus-5",
            "usage": {"input_tokens": 5, "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 0},
            "content": [{"type": "tool_use", "name": "Read",
                         "input": {"file_path": "/proj/a.py"}}],
        }
    }) + "\n", encoding="utf-8")

    result = replay_transcript(conn, t)
    assert result["rows"] == 1
    assert result["peak_context_tokens"] == 15


def test_latest_context_state_on_missing_file(tmp_path, capsys):
    tokens, model = transcript.latest_context_state(tmp_path / "nope.jsonl")
    assert (tokens, model) == (None, None)
    assert "could not tail" in capsys.readouterr().err


def test_latest_context_state_reads_tail(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = []
    for i in range(1, 51):
        lines.append(json.dumps({
            "message": {"model": "claude-opus-5",
                        "usage": {"input_tokens": i * 10,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}))
    # A trailing sidechain entry must be ignored in favour of the last
    # main-chain reading.
    lines.append(json.dumps({
        "isSidechain": True,
        "message": {"model": "claude-opus-5",
                    "usage": {"input_tokens": 999999,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}}}))
    t.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tokens, model = transcript.latest_context_state(t)
    assert tokens == 500
    assert model == "claude-opus-5"


# --------------------------------------------------------------------------
# Hook-level behaviour
# --------------------------------------------------------------------------

def run_hook_raw(raw_bytes, db_path, cwd=REPO_ROOT):
    """Drive the hook with exact bytes on stdin.

    Deliberately byte-level: the hook's stdin decoding is itself under test,
    so the harness must not quietly re-encode via the locale codec.
    """
    env = dict(os.environ, CONTEXT_GUARDIAN_DB=str(db_path))
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / "sensor.py")],
        input=raw_bytes, capture_output=True,
        env=env, cwd=str(cwd), timeout=30,
    )
    return proc


def run_hook(payload, db_path, cwd=REPO_ROOT):
    proc = run_hook_raw(json.dumps(payload).encode("utf-8"), db_path, cwd)

    class _R:
        returncode = proc.returncode
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
    return _R


def test_hook_writes_a_row_and_stays_silent(tmp_path):
    """Phase 1 is measure-only: exit 0, and nothing on stdout."""
    dbp = tmp_path / "state.db"
    transcript_file = tmp_path / "t.jsonl"
    transcript_file.write_text(json.dumps({
        "message": {"model": "claude-opus-5",
                    "usage": {"input_tokens": 1000,
                              "cache_read_input_tokens": 210_000,
                              "cache_creation_input_tokens": 0}}}) + "\n",
        encoding="utf-8")

    r = run_hook({
        "session_id": "sess-1",
        "transcript_path": str(transcript_file),
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/proj/a.py"},
        "tool_response": "ok",
    }, dbp)

    assert r.returncode == 0
    assert r.stdout == ""

    conn = db.connect(dbp)
    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row["session_id"] == "sess-1"
    assert row["tool_name"] == "Read"
    assert row["running_context_tokens"] == 211_000
    assert row["model"] == "claude-opus-5"
    assert row["context_window"] == 1_000_000
    assert row["repeat_read_count"] == 1
    conn.close()


def test_hook_flags_subagent_calls(tmp_path):
    """agent_id in the payload is the authoritative sidechain signal."""
    dbp = tmp_path / "state.db"
    r = run_hook({
        "session_id": "sess-2",
        "hook_event_name": "PostToolUse",
        "agent_id": "agent-abc",
        "agent_type": "Explore",
        "tool_name": "Read",
        "tool_input": {"file_path": "/proj/a.py"},
    }, dbp)
    assert r.returncode == 0

    conn = db.connect(dbp)
    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row["is_sidechain"] == 1
    assert row["running_context_tokens"] is None
    conn.close()


def test_hook_counts_repeat_reads_across_invocations(tmp_path):
    """The rolling window survives process boundaries via the DB."""
    dbp = tmp_path / "state.db"
    for _ in range(3):
        r = run_hook({
            "session_id": "sess-3",
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/proj/hot.py"},
        }, dbp)
        assert r.returncode == 0

    conn = db.connect(dbp)
    counts = [row["repeat_read_count"] for row in
              conn.execute("SELECT repeat_read_count FROM tool_calls ORDER BY id")]
    assert counts == [1, 2, 3]
    conn.close()


def test_hook_skips_scratchpad_reads(tmp_path):
    dbp = tmp_path / "state.db"
    for _ in range(4):
        run_hook({
            "session_id": "sess-4",
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/claude/p/s/tasks/x.output"},
        }, dbp)

    conn = db.connect(dbp)
    counts = [row["repeat_read_count"] for row in
              conn.execute("SELECT repeat_read_count FROM tool_calls ORDER BY id")]
    assert counts == [None, None, None, None]
    conn.close()


def test_hook_handles_bom_prefixed_stdin(tmp_path):
    """Some Windows shells prepend a UTF-8 BOM when piping to stdin; without
    stripping it, json.loads rejects every event on those setups."""
    dbp = tmp_path / "state.db"
    raw = json.dumps({
        "session_id": "sess-bom",
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/proj/a.py"},
    }).encode("utf-8-sig")  # utf-8 with a BOM

    r = run_hook_raw(raw, dbp)
    assert r.returncode == 0
    assert b"unparseable" not in r.stderr

    conn = db.connect(dbp)
    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row is not None and row["session_id"] == "sess-bom"
    conn.close()


def test_hook_handles_non_ascii_paths(tmp_path):
    """stdin must be decoded as UTF-8, not the locale codec.

    On a default Windows install sys.stdin.read() uses cp1252, which raises
    on these bytes - so this would break for anyone with a non-ASCII path.
    """
    dbp = tmp_path / "state.db"
    weird = "/proj/café/naïve_日本語.py"
    raw = json.dumps({
        "session_id": "sess-utf8",
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": weird},
    }, ensure_ascii=False).encode("utf-8")

    r = run_hook_raw(raw, dbp)
    assert r.returncode == 0
    assert b"unparseable" not in r.stderr

    conn = db.connect(dbp)
    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row is not None
    assert row["file_path"] == weird
    conn.close()


def test_hook_never_crashes_on_garbage_input(tmp_path):
    dbp = tmp_path / "state.db"
    for payload in [b"", b"   ", b"not json", b"[]", b"null",
                    b"\xff\xfe\x00garbage", b'{"tool_name": ']:
        r = run_hook_raw(payload, dbp)
        assert r.returncode == 0, f"crashed on {payload!r}: {r.stderr}"
        assert r.stdout == b""


def test_hook_handles_unreadable_transcript(tmp_path):
    """A missing transcript must still produce a row, not an exception."""
    dbp = tmp_path / "state.db"
    r = run_hook({
        "session_id": "sess-5",
        "transcript_path": str(tmp_path / "does-not-exist.jsonl"),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }, dbp)
    assert r.returncode == 0

    conn = db.connect(dbp)
    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row["tool_name"] == "Bash"
    assert row["running_context_tokens"] is None
    conn.close()
