# Phase 2 — The Nudge

**Status:** built. 85 tests green (43 new), verified end-to-end against real
transcripts.
**Deliverables:** `hooks/nudge.py`, `context_guardian/nudge.py`,
`tests/test_nudge.py`, config keys in `config.example.json`.

This is the differentiator: every other tool in this space shows a human a
dashboard. This one tells *Claude* what it is seeing, mid-session, and lets
Claude decide what to do about it.

---

## The three calls, and what the data said

All three were locked before building, from the Phase 1 measurements.

### 1. One message, never two

Phase 1 found **7 of 8 genuine repeat-read events fire while the session is
already past the 200k warn line**. The original spec's `"fire_mode":
"either"` would therefore have emitted two nudges for one situation in
almost every real case.

Replaced with a single evaluator that walks severity in order
(`urgent → warn → repeat_read`) and returns the **first** match. Repeat-reads
become the *specific detail inside* the context message rather than a
message of their own.

Real output, from session `4ef64bd8` at the moment `session_manager.py` was
being re-read:

> Context Guardian: this session is at ~280k tokens of context, past the warn
> threshold of 200k. session_manager.py has also been read 4 times recently.
> Consider delegating further exploration to a subagent, or suggesting
> /compact if the remaining work is large.

One message. Both signals. `test_only_one_decision_is_ever_returned` pins it.

### 2. Repeat-reads escalate, but can still stand alone

They only fire as their own nudge **below** the warn threshold. In the corpus
that is exactly one event — `TrustMesh_Master_Roadmap.md` at 158k — and it is
precisely the case the token threshold cannot see. That single event is why
the standalone path stays rather than being deleted.

### 3. Fire on entering a level, not per prompt

60% of corpus turns sit above 200k. A per-prompt check would nudge
continuously and get the plugin uninstalled in week one.

A nudge is recorded in a new `nudges` table and marked `active`. The level
re-arms only after context falls `rearm_margin_tokens` (20k) back below its
threshold — hysteresis, so a session hovering at 199k doesn't re-fire on
every prompt. After a `/compact` the level genuinely re-arms and can warn
again, which `test_hook_rearms_after_context_drops` walks through end to end.

Escalation still gets through: `warn` having fired never suppresses
`urgent`.

---

## The stdout hazard

`UserPromptSubmit` treats **plain stdout as context to inject**. A stray
`print()` in this hook would land in the model's context on every single
prompt of every session.

So `hooks/nudge.py` writes to stdout exactly once, only when nudging, and
only a well-formed JSON object. Every diagnostic goes to stderr. Silence is
asserted byte-exactly (`assert proc.stdout == b""`) rather than by parsing,
because "empty" is the actual requirement.

The event also carries a **30-second timeout** (shorter than other hooks —
the user is sat waiting on their prompt). The hook does one tail-read and
three indexed queries.

---

## v1 cannot block a prompt

`UserPromptSubmit` can reject a prompt outright via exit code 2 or
`decision: "block"` — the prompt is erased and never reaches Claude. A
context-management tool has no business doing that.

`test_hook_never_blocks_a_prompt` feeds the hook empty input, malformed
JSON, `[]`, `null`, and a payload with no transcript, and asserts exit 0
with neither `block` nor `continue` in the output. The catch-all handler
stays silent on any unexpected failure, so a bug here degrades to "no nudge"
rather than "lost prompt".

Same for scope discipline: nothing in this phase runs `/compact`. The
message *suggests*; Claude decides. Auto-compaction stays out until v1.1 at
the earliest, and only once the detector has earned trust in the wild.

---

## Message design

- **Absolute tokens, never percentages** — the Phase 0 finding, carried
  through to the wording. `test_message_reports_absolute_tokens_not_percentages`
  asserts no `%` appears.
- **Basename, not full path** — `session_manager.py`, not
  `D:\TrustMesh\TrustMesh\backend\app\session_manager.py`. Full paths are
  noise in a context note.
- **Suggests, never instructs** — "Consider delegating…". Claude has far
  more information about whether delegating makes sense right now than a
  hook counting tokens does.
- **Addressed to Claude, not the user.** No `systemMessage` is set, so
  nothing interrupts the human. That asymmetry is the product.

---

## Schema addition

```sql
CREATE TABLE nudges (
    id, session_id, level, subject, context_tokens,
    timestamp, active, message
);
```

`active` is what makes "once per level crossing" work across processes —
each hook invocation is a fresh process with no memory of the last.
`subject` holds the file path for repeat-read nudges, which is how
"once per file per session" is enforced.

`max_nudges_per_session` (12) is a backstop: if the rate-limiting logic ever
regresses, the blast radius is 12 messages, not a stream.

---

## Test coverage

43 new tests. The decision table is pure (no I/O), so every severity
boundary is asserted directly; the hook tests then drive the real script as
a subprocess.

| Concern | Test |
|---|---|
| Severity boundaries | `test_context_severity_levels` (8 cases around 200k/350k) |
| One message, not two | `test_only_one_decision_is_ever_returned` |
| Standalone repeat-read below warn | `test_repeat_read_stands_alone_only_below_warn` |
| Once per crossing | `test_hook_nudges_once_per_level_not_per_prompt` |
| Escalation survives | `test_urgent_fires_even_when_warn_already_fired` |
| Re-arm after /compact | `test_hook_rearms_after_context_drops` |
| Hysteresis | `test_hysteresis_prevents_flapping_at_the_boundary` |
| Byte-exact silence | `test_hook_is_silent_below_threshold` |
| Never blocks a prompt | `test_hook_never_blocks_a_prompt` |
| Session isolation | `test_sessions_are_isolated_from_each_other` |

```bash
python -m pytest tests/ -q
```

---

## Install

Both hooks, in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {"hooks": [{"type": "command",
                  "command": "python D:/Context Gaurdian/hooks/sensor.py"}]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{"type": "command",
                  "command": "python D:/Context Gaurdian/hooks/nudge.py"}]}
    ]
  }
}
```

Phase 3 replaces this with a `plugin.json` manifest so it installs in one
step instead.

---

## Carried into Phase 3

- **Dogfooding is the only real test of the message.** The thresholds are
  measurably right; whether the *wording* actually changes Claude's
  behaviour mid-session is unknown and cannot be unit-tested. Watch for
  whether a nudge is acted on or ignored.
- **The silent-failure problem from Phase 1 still applies.** Both hooks fail
  safe, which means they also fail quietly. The README should ship a
  `--self-check` or equivalent so someone can confirm the thing is alive
  rather than assuming no news is good news.
- **`max_nudges_per_session` at 12 is a guess**, unlike the token
  thresholds. Real sessions will say whether it is ever reached.
