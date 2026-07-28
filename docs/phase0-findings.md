# Phase 0 — Trigger Validation Findings

**Date:** 2026-07-28
**Script:** [`notebooks/phase0_validation.py`](../notebooks/phase0_validation.py)
**Corpus:** 6 real transcripts, 3,306 main-session assistant turns
(5 × TrustMesh, 1 × fixtura), 0.5–4 MB each.

---

## Headline: the roadmap's trigger does not fire. Not once.

The proposed context trigger (**≥75% of a 200,000-token window**) fired in
**0 of 6** sessions once measured correctly. Peak context across the whole
corpus was 66.3%; the median session peaked at ~40%.

This trips the Phase 0 gate as written:

> **Gate:** If the heuristic doesn't line up with your marked moments,
> adjust the trigger definition here — don't proceed until it does.

Do not start Phase 1 against the current trigger definition. A `PostToolUse`
hook built to this spec would be a no-op in every session in this corpus.

---

## Why the original number was wrong

Three measurement bugs, each of which alone would have produced a
misleading result. All are fixed in the committed script.

### 1. Wrong denominator — the big one

The sessions ran on **`claude-opus-5`, `claude-opus-4-8`, and
`claude-sonnet-5`** — all **1M-context** models. The script hardcoded
200,000.

Against a 200k window the data reads as "83% of turns are over threshold,
peak 331%" — apparently catastrophic. Against the real 1M window the same
data reads "never crosses 75%, peak 66%." Same tokens, opposite conclusion.

The script now auto-detects the window from the `model` field per transcript
and takes the **smallest** window across models seen (sessions do switch
models mid-run — `d89ba4e2` used three).

### 2. `output_tokens` was double-counted

Original: `input + output + cache_read + cache_creation`.

The context sent to the model at request time is
`input_tokens + cache_read_input_tokens + cache_creation_input_tokens` —
cached and uncached prompt tokens are disjoint parts of one prompt, so they
sum. `output_tokens` is the *response*; it lands in the *next* turn's input.
Adding it inflates every reading.

### 3. Sidechain turns were mixed in

Transcript entries carry `isSidechain`. Subagent (Task tool) turns run in
their **own** context window, so folding their usage into the main
session's running total produces meaningless sawtooth. Now excluded.

*(Note: this corpus happens to contain 0 sidechain entries, so it changed
nothing here — but it would corrupt any session that delegates, which is
precisely the behaviour Context Guardian exists to encourage.)*

---

## What the corrected data actually shows

### Context usage — absolute tokens, % of turns at/over

| transcript | turns | peak | ≥150k | ≥200k | ≥250k | ≥300k | ≥400k | ≥500k |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `d89ba4e2` | 707 | 420,229 | 83% | 74% | 67% | 56% | 4% | 0% |
| `0dc10ab5` | 533 | 663,026 | 80% | 67% | 52% | 37% | 32% | 32% |
| `4ef64bd8` | 699 | 520,481 | 82% | 71% | 64% | 52% | 30% | 3% |
| `4280f2e7` | 501 | 388,610 | 76% | 58% | 39% | 20% | 0% | 0% |
| `5e8a9449` | 421 | 317,771 | 63% | 46% | 22% | 6% | 0% | 0% |
| `39ff5e29` | 445 | 239,177 | 58% | 26% | 0% | 0% | 0% | 0% |
| **all** | **3,306** | — | **75%** | **60%** | **45%** | **33%** | **12%** | **6%** |

First turn at/over 200k: turns 176, 179, 202, 213, 227, 331 — remarkably
consistent, ~**turn 200** regardless of project.

**Read:** every one of these sessions blew straight past what used to be a
full 200k context window and kept going for another 300–500 turns. The
pain is real; the *percentage-of-window* framing is what fails to see it.
On a 1M model, 75% is 750k tokens — a threshold a normal working session
simply never reaches.

### Repeat-reads — the trigger that actually fires

12 events across the corpus (same file ≥3× within a 10-read window), at
context levels of **16–37%**.

> **⚠️ Corrected in Phase 1 — see [phase1-notes.md](phase1-notes.md).**
> This section originally concluded the repeat-read signal was "completely
> decoupled from context %". That was wrong, and the error was reasoning in
> percentages: 16–37% of a **1M** window is 160k–370k tokens, most of which
> sits *above* the 200k warn threshold, not below it. Once the 4 scratchpad
> false positives (which are the low-context ones) are excluded, **7 of the
> 8 genuine events fire while the session is already over 200k**. The two
> signals substantially overlap. The revised trigger's `"fire_mode":
> "either"` therefore needs de-duplication in Phase 2.

| session | events | files |
|---|---:|---|
| `d89ba4e2` | 5 | `tasks/*.output` ×3, `test_auth_dependencies.py` ×2 |
| `4ef64bd8` | 4 | `db.py` ×2, `session_manager.py` ×2 |
| `4280f2e7` | 1 | `session_manager.py` |
| `5e8a9449` | 1 | `TrustMesh_Master_Roadmap.md` |
| `39ff5e29` | 1 | `tasks/*.output` |
| `0dc10ab5` | 0 | — |

Worst offender overall: `backend/app/session_manager.py`, read **7×** in one
session — and it recurs across three separate sessions. That is exactly the
"why is it re-reading this" pattern the project is named for.

### False positive found: subagent task-output polling

**4 of the 12 events (33%) are on
`…\Temp\claude\<project>\<session>\tasks\*.output` files.** That is Claude
*polling a background task for completion* — correct, intentional behaviour,
not confusion. A v1 that nags about it will feel broken on first contact.

**Phase 1 must exclude the agent's own scratchpad/task-output paths from the
repeat-read detector.** This is the single most concrete thing this exercise
produced.

---

## Proposed revised trigger definition

Replace percentage-of-window with an **absolute token floor**, and promote
repeat-reads from a co-signal to an independent one.

```jsonc
{
  "context_trigger": {
    "mode": "absolute",          // NOT % of window — see finding #1
    "warn_tokens": 200000,       // fires ~turn 200; all 6/6 sessions
    "urgent_tokens": 350000      // fires in 4/6; the genuinely long ones
  },
  "repeat_read_trigger": {
    "window": 10,                // last 10 Read calls
    "threshold": 3,              // same path ≥3× in window
    "exclude_globs": [
      "**/Temp/claude/**/tasks/*.output",
      "**/.claude/**"
    ]
  },
  "fire_mode": "either"          // independent; they do not co-occur
}
```

Rationale for each number:

- **200k warn** — fires in 6/6 sessions at a consistent ~turn 200, and has
  a natural meaning: "you have now exceeded a classic full context window."
  Covers 60% of all turns, which is too noisy to nag on every turn — the
  nudge must be **rate-limited** (see open questions).
- **350k urgent** — separates the four genuinely long sessions from the two
  moderate ones. ≥400k would miss `4280f2e7`/`5e8a9449` entirely; ≥500k
  fires in only 2/6.
- **Repeat-read unchanged at 3-in-10** — it produced 8 plausible-signal
  events after excluding task-output polling. Loosening to 2 would flood;
  the whole-session tallies show many files hit 2×, which is normal.
  *(Phase 1 note: the window must count Read calls, not all tool calls —
  measured in [phase1-notes.md](phase1-notes.md).)*

---

## The one step I could not do for you

The roadmap's Phase 0 has a step that is inherently yours:

> Manually mark the points in those transcripts where the session *felt*
> like it was degrading.

I can tell you **where the triggers fire**; I cannot tell you whether those
are the moments you remember things going sideways. The numbers above are
only half the gate. Concretely, please check:

1. **`0dc10ab5` (fixtura, peaked 663k, 0 repeat-reads)** — did this session
   feel bad? If yes, repeat-reads missed it entirely and context volume is
   the only signal that would have caught it. If it felt *fine*, then 663k
   of context is not itself a problem and the context trigger is weaker
   than assumed.
2. **`session_manager.py` re-reads across three sessions** — was that
   thrashing, or legitimate repeated reference to a central file?
3. **~turn 200 / 200k tokens** — does that line up with when you typically
   start feeling the need to `/compact`?

If (1) says the big session felt fine and (2) says the re-reads were
legitimate, the honest conclusion is that the pain is sharper in *theory*
than in this corpus — which is itself a Phase 0 result worth having before
spending 6 days on Phases 1–3.

---

## Recommendation

**Do not proceed to Phase 1 yet.** Two options:

- **A (preferred):** Answer the three questions above. If they line up,
  lock the revised definition and start Phase 1 with the absolute-token
  trigger and the task-output exclusion baked in.
- **B:** If they don't line up, the corpus is telling you the trigger is
  looking at the wrong thing. Candidate signals worth testing before
  writing any hook: turn count since last user message, tool-call error
  rate, edit-then-revert churn on the same file, ratio of Read calls to
  Edit calls.

Either way, the deliverable this phase promised — a validated trigger
definition with exact thresholds — is now blocked on your recall, not on
more code.
