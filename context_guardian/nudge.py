"""Phase 2 - decide whether to nudge, and what to say.

Design decisions locked from the Phase 1 measurements (docs/phase1-notes.md):

1. **One message, never two.** 7 of the 8 genuine repeat-read events in the
   corpus fired while the session was already past the 200k warn line. The
   spec's `"fire_mode": "either"` would therefore double-nudge in the
   overwhelming majority of real cases. A single evaluator returns one
   decision with a severity.

2. **Repeat-reads escalate rather than compete.** When context is already
   high, the re-read file becomes the *specific detail* in the context
   message rather than a message of its own. It only stands alone below the
   warn threshold - one event in the corpus, and precisely the case the
   token threshold cannot see.

3. **Fire on entering a level, not per turn.** 60% of corpus turns sit above
   200k. Per-turn nudging would be continuous noise.

This module is deliberately free of I/O so the decision table can be tested
directly.
"""

from collections import namedtuple

WARN = "warn"
URGENT = "urgent"
REPEAT_READ = "repeat_read"

# Bumped whenever build_message() changes what it asks for. Outcomes must be
# grouped by this: pooling results from a message that no longer exists with
# the current one measures neither. Round 1 (v1) scored 17%, round 2 (v2)
# scored 50%+, and the pooled figure was 25% - a number describing nothing.
#   v1 - suggested delegating to a subagent / running /compact
#   v2 - asks for actions the recipient actually controls
MESSAGE_VERSION = "v2"

# Ordered most severe first - the evaluator returns the first that applies,
# which is what enforces "one message, never two".
SEVERITY_ORDER = (URGENT, WARN, REPEAT_READ)

Decision = namedtuple("Decision", "level subject context_tokens message")


def _fmt_tokens(n):
    """Render token counts the way a person reads them: 280k, 1.2M."""
    if n is None:
        return "unknown"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)


def _basename(path):
    if not path:
        return None
    return path.replace("\\", "/").rstrip("/").split("/")[-1]


def build_message(level, context_tokens, hot_file=None, hot_count=None,
                  warn_tokens=None, urgent_tokens=None,
                  suggest_delegation=False):
    """Compose the note injected as additionalContext.

    Rewritten after the first dogfooding round, which measured 22% acted-on
    against a 50% gate. The investigation (docs/phase4-results.md) found the
    problem was not phrasing but the *recommended action*:

      - "delegate to a subagent" was suggested in every message and happened
        ZERO times in 2,926 tool calls - because Claude Code instructs the
        model not to spawn agents unless the user asks for it. The advice was
        unactionable by construction, so `suggest_delegation` now defaults to
        off.
      - "/compact" is a user command. Claude cannot run it. Telling Claude to
        compact is telling it to do something it has no lever for.

    What Claude *can* actually do is (a) stop re-reading files already in
    context, and (b) tell the user, who does hold the /compact lever. So the
    messages now ask for exactly those, and nothing else.
    """
    ctx = _fmt_tokens(context_tokens)
    name = _basename(hot_file)

    if level == REPEAT_READ:
        # The only fully autonomous action available - and the most concrete.
        return (f"Context Guardian: you have read {name} {hot_count} times in "
                f"the last few reads. Its contents are already in this "
                f"conversation - re-reading spends context without adding "
                f"information. Refer back to what you already have unless you "
                f"have reason to believe the file changed.")

    if level == URGENT:
        head = (f"Context Guardian: this session is at ~{ctx} tokens, past the "
                f"urgent threshold of {_fmt_tokens(urgent_tokens)}.")
        ask = ("Only the user can run /compact, so tell them the context is "
               "getting heavy - briefly, at your next natural pause, without "
               "derailing what you are doing.")
    elif level == WARN:
        head = (f"Context Guardian: this session is at ~{ctx} tokens, past the "
                f"warn threshold of {_fmt_tokens(warn_tokens)}.")
        ask = ("Worth mentioning to the user at your next natural pause so they "
               "can decide whether to /compact - it is their command to run, "
               "not yours. Do not interrupt current work for it.")
    else:
        return None

    parts = [head]
    if name and hot_count:
        parts.append(f"{name} has also been read {hot_count} times recently and "
                     f"is already in context - do not re-read it.")
    parts.append(ask)
    if suggest_delegation:
        parts.append("If substantial exploration remains, a subagent would keep "
                     "it out of this context.")
    return " ".join(parts)


def evaluate(*, context_tokens, cfg, hot_file=None, hot_count=None,
             has_fired=lambda level, subject: False):
    """Return a Decision, or None if nothing should be said.

    `has_fired(level, subject)` reports whether this level has already
    nudged and not yet re-armed; keeping it a callback is what lets the
    decision table be tested without a database.
    """
    warn = cfg["context_warn_tokens"]
    urgent = cfg["context_urgent_tokens"]
    tokens = context_tokens or 0
    repeat_fires = bool(hot_file and hot_count
                        and hot_count >= cfg["repeat_read_threshold"])

    for level in SEVERITY_ORDER:
        if level == URGENT and tokens < urgent:
            continue
        if level == WARN and not (warn <= tokens < urgent):
            continue
        if level == REPEAT_READ and (tokens >= warn or not repeat_fires):
            continue

        subject = hot_file if level == REPEAT_READ else None
        if cfg.get("repeat_read_nudge_once_per_file", True) and level == REPEAT_READ:
            if has_fired(level, subject):
                return None
        elif has_fired(level, None):
            return None

        message = build_message(
            level, tokens,
            hot_file=hot_file if repeat_fires else None,
            hot_count=hot_count if repeat_fires else None,
            warn_tokens=warn, urgent_tokens=urgent,
            suggest_delegation=cfg.get("suggest_delegation", False),
        )
        return Decision(level=level, subject=subject, context_tokens=tokens,
                        message=message)

    return None


def levels_to_rearm(context_tokens, cfg):
    """Levels whose threshold the session has now dropped safely back below.

    The margin provides hysteresis: without it, a session hovering on the
    threshold would re-arm and re-fire on almost every prompt.
    """
    tokens = context_tokens or 0
    margin = cfg.get("rearm_margin_tokens", 0)
    out = []
    if tokens < cfg["context_warn_tokens"] - margin:
        out.append(WARN)
    if tokens < cfg["context_urgent_tokens"] - margin:
        out.append(URGENT)
    return out
