# Phase 4 — Launch Gate

**Decision made 2026-07-28, before any outcome data existed.** Written down
first precisely so the criterion cannot be quietly relaxed once the numbers
arrive.

---

## The decision: do not launch yet

The repo is public and installable. The posts are not going out for **3 real
working days**.

Not because the code is unfinished — 94 tests pass and every threshold is
measured against real sessions. Because **the one variable that decides
whether this project is real has never been observed**: does Claude actually
*act* on the nudge?

Everything else was validated against transcripts. The wording cannot be.
It either moves Claude mid-session or it doesn't, and no test can tell you
which.

Launching first would mean answering the first question anyone asks —
*"did it help you?"* — with a shrug. That is the exact failure mode the
roadmap's ground rule exists to prevent.

## Why this isn't just "wait"

Three days of using it and going on memory would produce a vibe, not a
result. So the deciding variable is now instrumented:

```bash
python -m context_guardian.outcomes
```

For every nudge emitted, it classifies what Claude did next:

| outcome | meaning |
|---|---|
| `delegated` | spawned a subagent — a `Task` call, or sidechain rows appeared |
| `compacted` | context dropped ≥30% — `/compact` ran |
| `ignored` | neither, within 15 follow-up tool calls |
| `pending` | not enough activity yet to judge |

Deliberately conservative: activity *before* the nudge is never credited,
other sessions never leak in, and an ordinary context dip is not scored as a
compaction. It should be hard for this to flatter itself.

## The gate

Pre-committed, and printed by the tool itself so it is checked against the
same rule every time:

- **Fewer than 5 judged nudges** → keep dogfooding, not enough signal.
- **≥50% acted on** → the message works. Launch: `awesome-claude-plugins` PR,
  r/ClaudeAI, X.
- **<50% acted on** → **the wording is the problem, not the detector.**
  Rewrite `build_message()` in `context_guardian/nudge.py`, re-measure, and
  do not launch until it passes.
- **Zero nudges in 3 days** → the thresholds are wrong for real workflow,
  not just for the validation corpus. Re-tune before launching.

The third branch matters most. A low rate would be easy to misread as "the
idea doesn't work" and abandon. It almost certainly means the detector is
right and the sentence is bad — a Phase 2 fix measured in minutes, not a
reason to bin the project.

## What launching looks like once it passes

In order:

1. **The before/after clip for the README.** Currently the biggest gap. It
   needs a genuine nudge followed by a genuine delegation — capture it, do
   not stage it.
2. PR to `awesome-claude-plugins`.
3. One r/ClaudeAI post, one X post. Real clip, no hype copy.

## Still standing: the v1 → v2 gate

Unchanged from the roadmap. Before any team/paid work begins, one of:

- 50+ organic installs/stars, **or**
- 3+ people independently reporting it caught something real, **or**
- an unprompted *"does this work for my team?"*

None of those within ~3–4 weeks of launch is also a result: ship v1 as a
portfolio piece and move on.
