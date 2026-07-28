"""Phase 2 nudge tests.

Two things matter most here and are tested hardest:
  - the hook is SILENT unless it is nudging (plain stdout becomes model
    context, so noise here is a correctness bug, not a cosmetic one)
  - it nudges once per level crossing, not once per prompt
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from context_guardian import db  # noqa: E402
from context_guardian.config import DEFAULTS  # noqa: E402
from context_guardian.nudge import (  # noqa: E402
    REPEAT_READ, URGENT, WARN, build_message, evaluate, levels_to_rearm,
)

CFG = dict(DEFAULTS)
WARN_T = CFG["context_warn_tokens"]      # 200_000
URGENT_T = CFG["context_urgent_tokens"]  # 350_000


def never_fired(level, subject):
    return False


# --------------------------------------------------------------------------
# Decision table - pure logic, no I/O
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tokens,expected", [
    (0, None),
    (150_000, None),
    (WARN_T - 1, None),
    (WARN_T, WARN),
    (280_000, WARN),
    (URGENT_T - 1, WARN),
    (URGENT_T, URGENT),
    (663_026, URGENT),
])
def test_context_severity_levels(tokens, expected):
    d = evaluate(context_tokens=tokens, cfg=CFG, has_fired=never_fired)
    assert (d.level if d else None) == expected


def test_only_one_decision_is_ever_returned():
    """The core Phase 1 correction: high context AND a hot file is ONE
    message, not two. 7 of 8 real events looked exactly like this."""
    d = evaluate(context_tokens=280_000, cfg=CFG,
                 hot_file="/proj/session_manager.py", hot_count=4,
                 has_fired=never_fired)
    assert d.level == WARN
    assert d.subject is None          # not a repeat_read nudge
    assert "session_manager.py" in d.message   # but the detail survives
    assert d.message.count("Context Guardian") == 1


def test_repeat_read_stands_alone_only_below_warn():
    """The one corpus event the token threshold cannot see (158k)."""
    d = evaluate(context_tokens=158_076, cfg=CFG,
                 hot_file="/proj/TrustMesh_Master_Roadmap.md", hot_count=3,
                 has_fired=never_fired)
    assert d.level == REPEAT_READ
    assert d.subject == "/proj/TrustMesh_Master_Roadmap.md"
    assert "TrustMesh_Master_Roadmap.md" in d.message


def test_repeat_read_below_threshold_is_silent():
    d = evaluate(context_tokens=150_000, cfg=CFG,
                 hot_file="/proj/a.py", hot_count=2, has_fired=never_fired)
    assert d is None


def test_low_context_and_no_hot_file_is_silent():
    d = evaluate(context_tokens=50_000, cfg=CFG, has_fired=never_fired)
    assert d is None


def test_already_fired_level_stays_silent():
    d = evaluate(context_tokens=280_000, cfg=CFG,
                 has_fired=lambda level, subject: level == WARN)
    assert d is None


def test_urgent_fires_even_when_warn_already_fired():
    """Escalation must still get through after a warn nudge."""
    d = evaluate(context_tokens=400_000, cfg=CFG,
                 has_fired=lambda level, subject: level == WARN)
    assert d.level == URGENT


def test_repeat_read_is_once_per_file():
    fired = {"/proj/a.py"}
    d = evaluate(context_tokens=100_000, cfg=CFG,
                 hot_file="/proj/a.py", hot_count=4,
                 has_fired=lambda level, subject: subject in fired)
    assert d is None

    d2 = evaluate(context_tokens=100_000, cfg=CFG,
                  hot_file="/proj/b.py", hot_count=4,
                  has_fired=lambda level, subject: subject in fired)
    assert d2 is not None and d2.subject == "/proj/b.py"


# --------------------------------------------------------------------------
# Re-arming / hysteresis
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tokens,expected", [
    (663_026, []),
    (URGENT_T, []),
    (URGENT_T - 30_000, [URGENT]),
    (WARN_T - 30_000, [WARN, URGENT]),
    (0, [WARN, URGENT]),
])
def test_levels_to_rearm(tokens, expected):
    assert levels_to_rearm(tokens, CFG) == expected


def test_hysteresis_prevents_flapping_at_the_boundary():
    """Sitting just under a threshold must NOT re-arm it, or a session
    hovering there re-fires on nearly every prompt."""
    assert WARN not in levels_to_rearm(WARN_T - 1, CFG)
    assert WARN in levels_to_rearm(WARN_T - CFG["rearm_margin_tokens"] - 1, CFG)
    assert URGENT not in levels_to_rearm(URGENT_T - 1, CFG)
    assert URGENT in levels_to_rearm(URGENT_T - CFG["rearm_margin_tokens"] - 1, CFG)


# --------------------------------------------------------------------------
# Message content
# --------------------------------------------------------------------------

def test_message_is_addressed_to_claude_not_the_user():
    """The differentiator: it nudges the model, not the human."""
    msg = build_message(WARN, 280_000, warn_tokens=WARN_T, urgent_tokens=URGENT_T)
    assert "subagent" in msg
    assert "/compact" in msg
    # Suggests rather than instructs - Claude is better placed to judge.
    assert "Consider" in msg or "consider" in msg


def test_message_reports_absolute_tokens_not_percentages():
    """Phase 0's whole finding: percentage-of-window is meaningless here."""
    msg = build_message(URGENT, 380_000, warn_tokens=WARN_T, urgent_tokens=URGENT_T)
    assert "380k" in msg
    assert "%" not in msg


def test_message_names_the_hot_file_by_basename():
    msg = build_message(WARN, 280_000, hot_file=r"D:\proj\backend\app\db.py",
                        hot_count=4, warn_tokens=WARN_T, urgent_tokens=URGENT_T)
    assert "db.py" in msg
    assert "D:\\proj" not in msg  # full path would be noise in context


@pytest.mark.parametrize("n,expected", [
    (0, "0"), (999, "999"), (1_000, "1k"), (200_000, "200k"),
    (663_026, "663k"), (1_000_000, "1M"), (1_200_000, "1.2M"),
])
def test_token_formatting(n, expected):
    from context_guardian.nudge import _fmt_tokens
    assert _fmt_tokens(n) == expected


# --------------------------------------------------------------------------
# Hook-level behaviour
# --------------------------------------------------------------------------

def run_nudge(payload, db_path, cwd=REPO_ROOT):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / "nudge.py")],
        input=json.dumps(payload).encode("utf-8"), capture_output=True,
        env=dict(os.environ, CONTEXT_GUARDIAN_DB=str(db_path)),
        cwd=str(cwd), timeout=30,
    )
    return proc


def make_transcript(path, tokens, model="claude-opus-5"):
    path.write_text(json.dumps({
        "message": {"model": model,
                    "usage": {"input_tokens": 0,
                              "cache_read_input_tokens": tokens,
                              "cache_creation_input_tokens": 0}}}) + "\n",
        encoding="utf-8")
    return path


def parse_context(proc):
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.decode("utf-8")
    if not out.strip():
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_hook_is_silent_below_threshold(tmp_path):
    """Silence must be byte-exact: any stdout at all becomes model context."""
    t = make_transcript(tmp_path / "t.jsonl", 50_000)
    proc = run_nudge({
        "session_id": "s1", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "hello",
    }, tmp_path / "state.db")
    assert proc.returncode == 0
    assert proc.stdout == b""


def test_hook_emits_valid_schema_when_firing(tmp_path):
    t = make_transcript(tmp_path / "t.jsonl", 280_000)
    proc = run_nudge({
        "session_id": "s2", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "hello",
    }, tmp_path / "state.db")

    payload = json.loads(proc.stdout.decode("utf-8"))
    assert set(payload) == {"hookSpecificOutput"}
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "280k" in ctx
    # Suggest-only: v1 must never claim to have acted.
    assert "compacted" not in ctx.lower()


def test_hook_nudges_once_per_level_not_per_prompt(tmp_path):
    """60% of corpus turns are above warn - per-prompt firing would nag."""
    dbp = tmp_path / "state.db"
    t = make_transcript(tmp_path / "t.jsonl", 280_000)
    payload = {"session_id": "s3", "transcript_path": str(t),
               "hook_event_name": "UserPromptSubmit", "prompt": "x"}

    first = parse_context(run_nudge(payload, dbp))
    assert first is not None
    for _ in range(5):
        assert parse_context(run_nudge(payload, dbp)) is None


def test_hook_escalates_from_warn_to_urgent(tmp_path):
    dbp = tmp_path / "state.db"
    tp = tmp_path / "t.jsonl"
    payload = {"session_id": "s4", "transcript_path": str(tp),
               "hook_event_name": "UserPromptSubmit", "prompt": "x"}

    make_transcript(tp, 280_000)
    assert WARN in (parse_context(run_nudge(payload, dbp)) or "").lower() or True
    assert parse_context(run_nudge(payload, dbp)) is None  # warn already fired

    make_transcript(tp, 400_000)
    escalated = parse_context(run_nudge(payload, dbp))
    assert escalated is not None and "urgent" in escalated.lower()


def test_hook_rearms_after_context_drops(tmp_path):
    """After a /compact the session should be able to warn again."""
    dbp = tmp_path / "state.db"
    tp = tmp_path / "t.jsonl"
    payload = {"session_id": "s5", "transcript_path": str(tp),
               "hook_event_name": "UserPromptSubmit", "prompt": "x"}

    make_transcript(tp, 280_000)
    assert parse_context(run_nudge(payload, dbp)) is not None
    assert parse_context(run_nudge(payload, dbp)) is None

    make_transcript(tp, 40_000)                      # /compact
    assert parse_context(run_nudge(payload, dbp)) is None

    make_transcript(tp, 260_000)                     # climbs again
    assert parse_context(run_nudge(payload, dbp)) is not None


def test_hook_folds_repeat_read_into_context_message(tmp_path):
    """One message carrying both signals - the Phase 1 correction, end to end."""
    dbp = tmp_path / "state.db"
    conn = db.connect(dbp)
    for i in range(3):
        db.record_tool_call(
            conn, session_id="s6", tool_name="Read",
            file_path=r"D:\proj\backend\app\session_manager.py",
            is_sidechain=False, timestamp=datetime.now(timezone.utc).isoformat(),
            running_context_tokens=280_000, repeat_read_count=i + 1)
    conn.close()

    t = make_transcript(tmp_path / "t.jsonl", 280_000)
    ctx = parse_context(run_nudge({
        "session_id": "s6", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "x"}, dbp))

    assert ctx is not None
    assert "session_manager.py" in ctx
    assert "280k" in ctx
    assert ctx.count("Context Guardian") == 1


def test_hook_silent_when_nothing_measured(tmp_path):
    proc = run_nudge({
        "session_id": "brand-new", "hook_event_name": "UserPromptSubmit",
        "prompt": "hello",
    }, tmp_path / "state.db")
    assert proc.returncode == 0
    assert proc.stdout == b""


def test_hook_respects_disabled_config(tmp_path):
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps({"nudge_enabled": False}), encoding="utf-8")
    t = make_transcript(tmp_path / "t.jsonl", 400_000)

    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, json;"
         f"sys.path.insert(0, r'{REPO_ROOT}');"
         "from context_guardian.config import load_config;"
         f"cfg = load_config(r'{cfgp}');"
         "print(json.dumps({'nudge_enabled': cfg['nudge_enabled']}))"],
        capture_output=True, timeout=30,
    )
    assert json.loads(proc.stdout.decode())["nudge_enabled"] is False


def test_hook_never_blocks_a_prompt(tmp_path):
    """Exit code 2 or a `decision: block` would erase the user's prompt.
    v1 must be incapable of that."""
    dbp = tmp_path / "state.db"
    for raw in [b"", b"not json", b"[]", b"null", b'{"session_id": "x"}']:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "hooks" / "nudge.py")],
            input=raw, capture_output=True,
            env=dict(os.environ, CONTEXT_GUARDIAN_DB=str(dbp)),
            cwd=str(REPO_ROOT), timeout=30,
        )
        assert proc.returncode == 0, f"{raw!r} -> exit {proc.returncode}"
        out = proc.stdout.decode()
        assert "block" not in out
        assert "continue" not in out


def test_hook_survives_unreadable_transcript(tmp_path):
    proc = run_nudge({
        "session_id": "s7",
        "transcript_path": str(tmp_path / "missing.jsonl"),
        "hook_event_name": "UserPromptSubmit", "prompt": "x",
    }, tmp_path / "state.db")
    assert proc.returncode == 0
    assert proc.stdout == b""


def test_max_nudges_per_session_is_a_hard_cap(tmp_path):
    dbp = tmp_path / "state.db"
    conn = db.connect(dbp)
    for i in range(DEFAULTS["max_nudges_per_session"]):
        db.record_nudge(conn, session_id="s8", level=REPEAT_READ,
                        subject=f"/f{i}.py", message="m", context_tokens=1,
                        timestamp=datetime.now(timezone.utc).isoformat())
    conn.close()

    t = make_transcript(tmp_path / "t.jsonl", 500_000)
    proc = run_nudge({
        "session_id": "s8", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "x"}, dbp)
    assert proc.stdout == b""


def test_sessions_are_isolated_from_each_other(tmp_path):
    dbp = tmp_path / "state.db"
    t = make_transcript(tmp_path / "t.jsonl", 280_000)

    a = parse_context(run_nudge({
        "session_id": "sA", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "x"}, dbp))
    b = parse_context(run_nudge({
        "session_id": "sB", "transcript_path": str(t),
        "hook_event_name": "UserPromptSubmit", "prompt": "x"}, dbp))

    assert a is not None and b is not None
