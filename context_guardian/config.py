"""Configuration loading for Context Guardian.

Nothing is hardcoded at the call sites - every threshold comes from here, so
Phase 2's nudge logic and the Phase 1 sensor read the same numbers.

The thresholds below are derived from a 6-session personal corpus (all
coding-heavy work on 1M-context frontier models). They are a defensible v1
default, NOT a universal constant - see README. Someone running a
200k-window model will want to lower them substantially, which is exactly
why they are config-overridable.
"""

import json
import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".claude" / "context-guardian"
CONFIG_PATH = CONFIG_DIR / "config.json"
DB_PATH = CONFIG_DIR / "state.db"

DEFAULTS = {
    "enabled": True,

    # Absolute token counts, NOT a percentage of the context window.
    # Phase 0 finding: percentage-of-window is the wrong shape entirely on
    # 1M-context models, where 75% (750k tokens) is never reached in a
    # normal working session.
    "context_warn_tokens": 200_000,
    "context_urgent_tokens": 350_000,

    # --- Phase 2: the nudge ---------------------------------------------
    # Suggest-only. v1 never triggers /compact itself; it tells Claude what
    # it is seeing and lets Claude decide. Earning trust in the detector
    # comes before taking actions on someone's behalf.
    "nudge_enabled": True,

    # A nudge fires once on *entering* a severity level, not once per turn.
    # In the Phase 0 corpus 60% of all turns sit above the 200k warn line,
    # so a per-turn check would nag continuously and get uninstalled.
    # The level re-arms only after context falls this far back below its
    # threshold, which stops flapping at the boundary.
    "rearm_margin_tokens": 20_000,

    # Repeat-read nudges are capped at one per file per session - the point
    # is to surface the pattern once, not to keep score.
    "repeat_read_nudge_once_per_file": True,

    # Hard ceiling per session, as a backstop against any rate-limiting bug
    # reaching the user as a stream of nudges.
    "max_nudges_per_session": 12,

    "repeat_read_window": 10,
    "repeat_read_threshold": 3,

    # What the rolling window counts. "reads" matches the Phase 0 analysis
    # that produced the threshold of 3; "tool_calls" is a wider window that
    # fires less often. See docs/phase1-notes.md for the measured difference.
    "repeat_read_window_counts": "reads",

    # Paths excluded from repeat-read counting. Phase 0 found 33% of
    # repeat-read events were the agent polling its own background-task
    # output files - correct behaviour, not confusion. Nagging about it is
    # the fastest way to get uninstalled.
    #
    # Patterns use fnmatch semantics against a lowercased, forward-slash
    # normalised path. Note that `*` crosses path separators here, so
    # `*/tasks/*.output` and `**/tasks/*.output` behave identically.
    "scratchpad_path_patterns": [
        "*/tasks/*.output",
        "*/scratchpad/*",
        "*/.claude/projects/*",
        "*/temp/claude/*",
        "*/tmp/claude/*",
    ],
}


def load_config(path=None):
    """Load config, falling back to DEFAULTS for any missing key.

    A broken or unreadable config file must never take the hook down - we
    warn on stderr and use defaults. The sensor failing closed would block
    the user's actual tool call, which is worse than losing a data point.
    """
    path = Path(path) if path else CONFIG_PATH
    cfg = dict(DEFAULTS)

    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
            else:
                print(f"[context-guardian] config at {path} is not a JSON object; "
                      f"using defaults", file=sys.stderr)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[context-guardian] could not read config at {path} ({e}); "
                  f"using defaults", file=sys.stderr)

    # Environment override, mainly for tests and for pointing a dogfooding
    # session at a throwaway database.
    env_db = os.environ.get("CONTEXT_GUARDIAN_DB")
    cfg["db_path"] = Path(env_db) if env_db else cfg.get("db_path", DB_PATH)

    return cfg


def write_example_config(dest):
    """Write a fully-populated example config (used to seed CONFIG_PATH)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    example = {k: v for k, v in DEFAULTS.items()}
    with dest.open("w", encoding="utf-8") as f:
        json.dump(example, f, indent=2)
        f.write("\n")
    return dest
