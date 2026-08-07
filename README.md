# Context Guardian

**Every other context tool shows *you* a dashboard. This one tells *Claude*.**

A Claude Code plugin that watches your session's context and, when it gets
heavy, quietly tells Claude what it's seeing — so Claude can delegate to a
subagent or suggest `/compact` on its own, mid-session, without you having to
notice first.

```
Context Guardian: this session is at ~280k tokens of context, past the warn
threshold of 200k. session_manager.py has also been read 4 times recently.
Consider delegating further exploration to a subagent, or suggesting /compact
if the remaining work is large.
```

That message is injected alongside your prompt. You never see it. Claude does.

---

## The problem

Long sessions degrade before they break. Claude starts re-reading files it
already read, loses the thread of what you asked for, and the work gets worse
in ways that are obvious in hindsight and invisible in the moment. By the time
you think "why is it re-reading that?", you've already lost half an hour.

Existing tools show you a token counter. That doesn't help — you're busy
working, and it's *Claude* that needs to change what it's doing.

## Install

```bash
/plugin marketplace add yash161004/context-guardian
```

```bash
/plugin install context-guardian
```

Then check it's alive:

```bash
python3 -m context_guardian.selfcheck
```

The hooks resolve their interpreter by falling back through `python3` → `py`
→ `python`, so they work on macOS, Linux, and Windows without editing
anything. (Debian/Ubuntu frequently has no `python`; Windows ships a
`python3` stub that isn't Python at all.)

**No dependencies.** Python 3.9+ and the standard library. State lives in a
local SQLite file at `~/.claude/context-guardian/state.db` and never leaves
your machine.

<details>
<summary>Manual install, without the plugin system</summary>

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {"hooks": [{"type": "command",
                  "command": "python /path/to/context-guardian/hooks/sensor.py"}]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{"type": "command",
                  "command": "python /path/to/context-guardian/hooks/nudge.py"}]}
    ]
  }
}
```
</details>

## How it works

Two hooks:

- **`PostToolUse` → `sensor.py`** — after each tool call, reads your session
  transcript and records context size, which files were read, and how often.
  Writes to SQLite. Says nothing.
- **`UserPromptSubmit` → `nudge.py`** — when you submit a prompt, decides
  whether to attach a note for Claude. Usually it doesn't.

It nudges **once per threshold crossing**, not once per turn. It re-arms after
a `/compact` so a genuinely fresh climb can warn again.

### It never

- runs `/compact` or any other command on your behalf
- blocks, edits, or delays your prompt
- sends anything anywhere — no network calls, no telemetry
- reads your code (it counts tokens and file paths, not contents)

Suggest-only is the whole of v1, deliberately. A tool that acts on your
session has to earn that first.

## Configuration

`~/.claude/context-guardian/config.json`:

```json
{
  "context_warn_tokens": 200000,
  "context_urgent_tokens": 350000,
  "repeat_read_threshold": 3,
  "repeat_read_window": 10,
  "nudge_enabled": true
}
```

> **The default thresholds are derived from one person's 6-session corpus**
> of coding-heavy work on 1M-context models. They are a starting point, not a
> universal constant. **If you run a 200k-window model, lower them** — at the
> defaults they can barely fire before you're out of room entirely.

Turn it off without uninstalling: `"enabled": false`.

## Why absolute tokens, not a percentage

The first version of this used "75% of the context window", which is what
every similar tool does. Against 6 real transcripts it **fired zero times**.

The sessions were on 1M-context models, where 75% is 750,000 tokens — a
number normal work never reaches. Measured correctly, those sessions peaked
at 66%, 52%, 42%, 39%, 32%, 24%. All six sailed past 200k (roughly a *full*
older context window) at a consistent ~turn 200, then ran another 300–500
turns.

Percentage-of-window hides that completely. Absolute tokens don't.

The full analysis, including two other measurement bugs that inverted the
result, is in [`docs/phase0-findings.md`](docs/phase0-findings.md).

## Why re-reads are a signal

Same corpus: one file (`session_manager.py`) was read **7 times in a single
session**, and recurred across three separate sessions. That's the "why is it
re-reading this" moment, made countable.

One caveat that turned out to matter: **33% of the raw re-read events were
false positives** — Claude polling `tasks/*.output` to check whether a
background task had finished. That's correct behaviour, and it's exactly the
delegation this tool wants to encourage. Nagging about it would have made v1
feel broken on day one. Those paths are excluded by default.

## Something wrong?

Both hooks fail safe — a broken install looks exactly like a quiet session.
So don't assume silence means it's working:

```bash
python3 -m context_guardian.selfcheck
```

It reports whether rows are being recorded, when the last one landed, peak
context seen, and any nudges emitted — with a likely cause when something
looks off.

## Does it actually work?

Fair question, and a nudge only matters if Claude acts on it — so that was
measured rather than assumed, over six days of real work.

| | judged nudges | acted on |
|---|---:|---:|
| first message | 17 | **24%** |
| current message | 5 | **80%** |

**That second row is five nudges.** Small enough that one different outcome
moves it a long way, so treat it as "promising, early" rather than a
benchmark. It is reported here because the alternative — shipping with no
number at all — is worse.

The first message told Claude to delegate to a subagent or run `/compact`.
Across 3,580 tool calls it delegated **zero** times, because Claude Code
instructs it not to spawn agents unprompted — and `/compact` is a user
command Claude cannot run. Both suggestions were levers the reader didn't
have. The current message asks only for things it controls: stop re-reading
what's already in context, and tell you, since you hold `/compact`.

Full method, including three measurement bugs that were hiding the result,
in [`docs/phase4-results.md`](docs/phase4-results.md).

Measure it for yourself:

```bash
python3 -m context_guardian.outcomes
```

For every nudge, it reports what happened next — `delegated` (a subagent was
spawned), `compacted` (context dropped sharply), or `ignored`. If you're
seeing mostly `ignored`, the message needs rewriting, and an issue with your
numbers is genuinely useful to me.

## Development

```bash
python -m pytest tests/ -q
```

85 tests. The corpus-dependent ones skip unless you point
`CONTEXT_GUARDIAN_CORPUS` at a directory of transcripts; the rest run
anywhere.

Design notes and every place the implementation departed from the plan, with
the measurement that forced it:

- [`docs/phase0-findings.md`](docs/phase0-findings.md) — trigger validation
- [`docs/phase1-notes.md`](docs/phase1-notes.md) — the sensor
- [`docs/phase2-notes.md`](docs/phase2-notes.md) — the nudge
- [`docs/phase3-notes.md`](docs/phase3-notes.md) — packaging
- [`docs/phase4-gate.md`](docs/phase4-gate.md) — the launch gate, written
  before the data arrived

## Status

v1: free, solo, open source, and intended to stay that way. If it catches
something real for you, [open an issue](../../issues) and say so — that's the
signal that decides whether this goes further.

## License

MIT
