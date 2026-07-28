"""The zero-nudge report.

This is the branch most likely to be misread as project failure, so its
wording is tested like behaviour rather than left to chance.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from context_guardian import db  # noqa: E402
from context_guardian.config import DEFAULTS  # noqa: E402
from context_guardian.outcomes import report_no_nudges  # noqa: E402

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.db")
    yield c
    c.close()


def seed(conn, days_span, peak, n=50):
    start = NOW - timedelta(days=days_span)
    for i in range(n):
        ts = (start + timedelta(seconds=i * (days_span * 86400 / max(n - 1, 1))))
        db.record_tool_call(
            conn, session_id="s", tool_name="Read", file_path="/a.py",
            is_sidechain=False, timestamp=ts.isoformat(),
            running_context_tokens=peak if i == n - 1 else peak // 2)


def test_early_in_the_window_it_says_too_early(conn, capsys):
    seed(conn, days_span=1.0, peak=90_000)
    report_no_nudges(conn, DEFAULTS)
    out = capsys.readouterr().out
    assert "Too early" in out
    assert "READ THIS" not in out


def test_after_the_window_it_states_the_conclusion_outright(conn, capsys):
    """The whole point: don't make the user infer it."""
    seed(conn, days_span=3.2, peak=90_000)
    report_no_nudges(conn, DEFAULTS)
    out = capsys.readouterr().out

    assert "READ THIS BEFORE CONCLUDING THE IDEA DOESN'T WORK" in out
    assert "does NOT mean" in out
    assert "THRESHOLD" in out
    # and it must say what to do about it, not just reassure
    assert "context_warn_tokens" in out


def test_it_suggests_a_threshold_derived_from_observed_peak(conn, capsys):
    seed(conn, days_span=3.2, peak=120_000)
    report_no_nudges(conn, DEFAULTS)
    out = capsys.readouterr().out

    assert "120k" in out                       # reports the real peak
    assert "90,000" in out                     # 75% of 120k, rounded to 10k
    assert "157,500" in out or "157,000" in out  # urgent scaled from it


def test_it_distinguishes_an_install_problem_from_a_result(conn, capsys):
    """No tool calls at all is a broken install, not a finding."""
    report_no_nudges(conn, DEFAULTS)
    out = capsys.readouterr().out
    assert "install problem" in out
    assert "selfcheck" in out
    assert "READ THIS" not in out


def test_it_names_the_only_result_that_justifies_abandoning(conn, capsys):
    seed(conn, days_span=3.2, peak=90_000)
    report_no_nudges(conn, DEFAULTS)
    out = capsys.readouterr().out
    assert "Abandon the project only if" in out
    assert "ignores it" in out
