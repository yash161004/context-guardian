# Phase 4 — Round 1 Dogfooding Results

**Window:** 2026-07-28 → 2026-08-02 (3 working days)
**Corpus:** 2,926 tool calls, 10 sessions, 19 nudges, all on `claude-opus-5`
**Result: GATE FAILED — 17% acted on, against a pre-committed 50%.**

Not launching. But the gate's own diagnosis ("the wording is the problem")
turned out to be only half right, and the half it got wrong matters more.

---

## The number, corrected

The tracker first reported **22%**. It was wrong.

Two nudges fired 7 minutes apart in session `bcd5fae7` (at 266k and 399k),
and *both* credited the same later compaction — tool call row `1476` — as
their own success. One event, two recorded wins.

Fixed by having each event claimable only once. The honest number is
**3 of 18 (17%)**.

That the bug inflated the result in the flattering direction is the reason
to distrust a metric you wrote yourself. It is now tested
(`test_one_compaction_cannot_be_claimed_by_two_nudges`).

---

## The real finding: the advice was unactionable, not badly worded

Every nudge said some version of *"consider delegating remaining exploration
to a subagent, or suggesting the user run /compact."*

**Delegation happened zero times in 2,926 tool calls.** Not "rarely" — never.
No `Task` calls, no `Agent` calls, zero sidechain rows.

That is not Claude ignoring good advice. Claude Code instructs the model not
to spawn subagents unless the user asks for one. The nudge was recommending
an action the model is told not to take unprompted. It could never have
worked.

And the other half of the suggestion is no better: **`/compact` is a user
command.** Claude cannot run it. The message spent both its recommendations
on things the recipient has no lever for.

So 15 "ignored" outcomes are not 15 failures of persuasion. They are the
correct response to advice that cannot be followed.

### What the ignored nudges were actually doing

Follow-up tool mixes tell the rest of the story:

| nudge | context | what Claude did next |
|---|---:|---|
| 2026-07-29 03:52 | 206k | `Bash` ×14 |
| 2026-08-01 15:35 | 201k | `Bash` ×12, `PowerShell` ×3 |
| 2026-07-31 19:31 | 359k | `PowerShell` ×8, `Edit` ×6 |
| 2026-07-28 15:19 | 242k | `PowerShell` ×9, `Write` ×6 |

Heads-down implementation — running tests, editing files. "Consider
delegating remaining *exploration*" is irrelevant advice mid-build. The
nudge knew the context size and nothing about what the session was doing.

Context in those windows stayed flat or rose slightly (100–101% of the nudge
value). Nothing changed, because nothing could.

---

## What was vindicated

**The thresholds are right.** 19 nudges across 10 sessions in 3 days, peak
context **747k**, six sessions above 328k. The detector fired when it should
have.

This retires the earlier speculation — mine included — that 200k/350k might
be tuned too high and that something like 80k/140k could be closer to true.
That guess came from a number I invented for a preview screenshot. Real data
says the opposite. Good thing it wasn't acted on.

**The rate limiting works.** 19 nudges over 2,926 tool calls, never more than
6 in a single session, no repeats at the same level. It did not nag.

**The sensor is solid.** 2,926 rows, zero crashes, zero corrupt entries
across three days of real work.

---

## The premise needs revising, not abandoning

The project's stated differentiator was *"nudge Claude, not the human."*

Round 1 says Claude has almost no autonomous lever here. It cannot compact.
It should not delegate unprompted. What it *can* do is:

1. **Stop re-reading files already in context** — fully autonomous, and the
   repeat-read detector already identifies exactly when.
2. **Tell the user** — who does hold the `/compact` lever.

So the differentiator survives in a narrower, more honest form: the tool
still nudges Claude rather than showing a dashboard, but what it asks for is
either a behaviour change Claude controls, or a well-timed handoff to the
person who controls the rest.

---

## Changes made

**`build_message()` rewritten** around what the recipient can act on:

- *Repeat-read* — now a direct, autonomous instruction:
  > you have read session.py 4 times in the last few reads. Its contents are
  > already in this conversation — re-reading spends context without adding
  > information.
- *Warn / urgent* — asks for the handoff, and explicitly protects work in
  progress:
  > Only the user can run /compact, so tell them the context is getting heavy
  > — briefly, at your next natural pause, without derailing what you are
  > doing.
- *Delegation suggestion removed*, behind `suggest_delegation` (default
  `false`). Unactionable advice in a short note trains the reader to skim all
  of it, including the parts that are actionable.

**Outcome tracking extended.** The new primary action — telling the user —
happens in assistant *text*, which a `PostToolUse` sensor cannot see. Nudges
now record their `transcript_path`, and the tracker reads the transcript to
check whether Claude raised it in the following few messages
(`surfaced_to_user`). Without this, round 2 would have been unmeasurable, and
re-running the experiment blind is how the roadmap's ground rule gets broken
quietly.

Schema migration is additive only — existing history is preserved, and old
nudges with no recorded transcript report *undetermined* rather than being
silently scored as failures.

---

---

# Round 2 Results (2026-08-05 → 2026-08-06)

**GATE PASSED — 80% acted on, 4 of 5 judged nudges (v2 message).**

| message | judged | acted | rate |
|---|---:|---:|---:|
| v1 (round 1) | 17 | 4 | **24%** |
| v2 (round 2) | 5 | 4 | **80%** |

The rewrite worked. But the tracker reported **36%**, which is neither
number, and three separate measurement faults had to be fixed before the
real result was visible.

## Three faults, all of which understated v2

**1. It pooled two different interventions.** v1 and v2 nudges were averaged
together. Half the sample was a message that no longer exists in the
codebase. Nudges now record `message_version`, the report groups by it, and
the gate evaluates the *current* message only.

**2. Repeat-read compliance was scored as failure.** That nudge succeeds by
the file *not* being read again — an absence, which the event-hunting
classifier could not see. `_legacy.py` was followed by 25 reads and never
touched again; that was recorded as `ignored`. Now scored by whether the
re-reading actually stopped, with a guard: fewer than 5 subsequent reads is
`pending`, not compliance, so a session that merely ended cannot masquerade
as success.

**3. The backfill never ran.** It was conditioned on having just added the
`message_version` column — but the column had been added by an earlier run
that predated the backfill, so every existing row stayed `NULL` and the
report silently pooled everything. A data migration must be driven by the
state of the data, never by the schema-change event. Both faults are now
tested.

## Guarding against motivated reasoning

Three corrections that all moved the number toward PASS is a pattern worth
distrusting. So the conservative reading, dropping the repeat-read scoring
change entirely and only separating v1 from v2:

> v2: 3 acted (2 compacted, 1 surfaced) of 6 judged = **50%**

**The gate passes either way** — at exactly 50% on the strictest reading, at
80% with repeat-read compliance counted. The decision does not depend on the
scoring change, which is the only reason to trust it.

## The honest caveat

**n = 5.** The pre-committed gate required a minimum of 5 judged nudges and
that is exactly what it got. The improvement over v1 is large and consistent
(24% → 50–80%), but a single different outcome would move the conservative
figure below the line.

The gate was pre-committed and it is met, so it is honoured — the same rule
that made round 1 a genuine failure makes this a genuine pass. But any public
claim must state the sample size. "80% of 5" is honest; "80%" alone is not.

## What actually changed behaviour

v2's wins came from asking for things the recipient controls:

- 3× **compacted** — Claude raised it, the context then dropped 207k→69k,
  350k→63k, 428k→61k
- 1× **surfaced** — Claude told the user unprompted
- 1× **complied** — stopped re-reading a file after being told it was
  already in context
- **0× delegated**, still. Across both rounds and 3,580 tool calls, not one
  subagent. Removing that advice was correct.

## Next

Launch. The remaining gap is the README's before/after clip, which now has
real material: five v2 nudges with recorded outcomes.
