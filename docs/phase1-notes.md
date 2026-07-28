# Phase 1 — Sensor Implementation Notes

**Status:** built, 40 tests green against the 6 Phase 0 transcripts.
**Deliverables:** `hooks/sensor.py`, `context_guardian/{db,transcript,detector,config,replay}.py`,
`tests/test_sensor.py`, `config.example.json`.

This records where the implementation had to depart from the Phase 1 spec,
and why. Everything here is a measured result, not a preference.

---

## 1. The hook payload has no token usage — architecture change

**Spec 1/2.1 assumed** the sensor computes `prompt_tokens` from the
tool-call JSON on stdin.

**It can't.** The documented `PostToolUse` payload is:

```
session_id, transcript_path, cwd, permission_mode, hook_event_name,
prompt_id, effort, tool_name, tool_input, tool_response, tool_use_id
(+ agent_id, agent_type when firing inside a subagent)
```

There is no usage, token, or context field anywhere in it.

**What was built instead:** the hook takes *tool identity* from stdin and
reads *context size* from the transcript at `transcript_path`. Two
consequences:

- **The reading can lag by a turn.** Claude Code writes the transcript
  asynchronously and the docs state it "may not yet include the current
  turn's most recent messages." At 200k/350k thresholds a one-turn lag is
  immaterial, but this is near-real-time, not exact.
- **Tail-read, not full parse.** The sensor runs on *every* tool call and
  these transcripts reach 4 MB. `latest_context_state()` seeks to the last
  512 KB and scans backwards for the most recent main-chain usage record.
  A full parse per tool call would add real latency to everything the user
  does.

**Bonus from the same doc:** `agent_id`/`agent_type` are present in the
payload when the hook fires inside a subagent. That is a more reliable
sidechain signal than inferring `isSidechain` from the transcript, and it
is what `sensor.py` uses. (`transcript.is_sidechain()` still reads the
field, for the offline replay path.)

---

## 2. ⚠️ The Phase 0 "independent signals" claim was wrong

**Spec 2.2 states** repeat-reads fire "completely decoupled from context
volume (all 12 events at 16–37% context, nowhere near the token thresholds
above)."

**That is incorrect, and it came from my own Phase 0 write-up.** The error
was reasoning in percentages: 16–37% of a **1M** window is 160k–370k
tokens. The warn threshold is 200k. Most of that range is *above* it.

Measured, after applying the scratchpad denylist:

| session | context at firing | file |
|---|---:|---|
| `d89ba4e2` | 360,971 | `test_auth_dependencies.py` |
| `d89ba4e2` | 366,373 | `test_auth_dependencies.py` |
| `4ef64bd8` | 263,333 | `db.py` |
| `4ef64bd8` | 268,089 | `db.py` |
| `4ef64bd8` | 278,483 | `session_manager.py` |
| `4ef64bd8` | 279,890 | `session_manager.py` |
| `4280f2e7` | 327,284 | `session_manager.py` |
| `5e8a9449` | **158,076** | `TrustMesh_Master_Roadmap.md` |

**7 of 8 genuine repeat-read events fire while the session is already past
the 200k warn threshold.** The 4 excluded scratchpad events were the
low-context ones (16–23%) dragging the original range down.

Two implications, both for Phase 2:

- **`"fire_mode": "either"` will double-nudge.** In 7 of 8 real cases both
  signals are true simultaneously. Phase 2 must emit *one* message, not one
  per signal.
- **Repeat-reads still earn their place**, but as a *severity escalator*
  rather than an independent detector — with one genuine exception
  (`5e8a9449` at 158k) that the token threshold alone would miss. Keep the
  signal; stop describing it as orthogonal.

Locked in as `test_repeat_reads_mostly_co_occur_with_high_context`, so a
future change that alters this relationship fails loudly.

---

## 3. The rolling window must count reads, not tool calls

**Spec 2.2 says** "Window: last 10 tool calls (rolling)". Phase 0's
threshold of 3 was derived over the last 10 **Read** calls. Measured
difference across the corpus:

| mode | window 10 | window 20 | window 30 |
|---|---:|---:|---:|
| `reads` | **8** | 11 | 14 |
| `tool_calls` | 3 | 4 | 5 |

Reads are roughly 1 in 5 tool calls in this corpus (218 Reads against ~1,000
path-carrying calls, before counting Bash/Grep/Glob). A 10-*tool-call*
window is therefore about a 2-read window — it can barely reach a threshold
of 3, and it drops the signal to 0.5 events per session.

**Default is `"reads"`**, exposed as `repeat_read_window_counts` so the
other mode is one config edit away. Taking the spec literally here would
have shipped a near-dead detector.

---

## 4. The sensor's observed peak is 662,671, not 663,026

**Spec 4 asserts** peak `running_context_tokens` lands at 663k.

It lands at **662,671** — 355 tokens (0.05%) below the transcript's true
663,026 peak. The reason is structural, not a bug: **the 663,026-token turn
was text-only and issued no tool calls**, so a `PostToolUse` hook has no
invocation at which to observe it.

A tool-call-driven sensor *samples*; it does not watch continuously. The
test pins the exact observed value (so any regression that widens the blind
spot fails) and separately asserts the gap stays under 1%. The transcript
peak of 663,026 is still asserted at the extraction layer, which is where
the Phase 0 bug-fix regression check actually belongs.

Phase 2 adds a `UserPromptSubmit` hook, which gives a second sampling point
at a different part of the turn and will narrow this further.

---

## 5. Scratchpad denylist — seed list

Scanned all 218 Read paths across the corpus for polling patterns beyond
`tasks/*.output` (spec 2.2 asked for this). Found:

- `tasks/*.output` — 14 distinct paths, confirmed dominant
- `scratchpad/*.log` — 1 path, same `Temp/claude/<project>/<session>/` tree

Shipped seed list:

```
*/tasks/*.output
*/scratchpad/*
*/.claude/projects/*
*/temp/claude/*
*/tmp/claude/*
```

**Deliberately NOT excluded: `.claude/launch.json`.** It matched a naive
`**/.claude/**` pattern, but it is real project configuration living in the
repo, not agent scratchpad. Excluding it would blind the detector to genuine
re-reads of project config. (It peaked at 2 reads/session, so it never trips
the threshold anyway.) `*/.claude/projects/*` is narrowly scoped to
transcript files only.

Patterns use `fnmatch` semantics against a lowercased, forward-slash path,
where `*` crosses separators — so `*/tasks/*.output` and `**/tasks/*.output`
behave identically. Windows paths are normalised before matching *and*
before counting, or `D:\App\Main.py` and `d:/app/main.py` would count as
two different files and silently under-report.

One subtlety worth keeping: excluded paths do **not** consume a window slot.
Otherwise a burst of task-polling would flush genuine repeat-reads out of
the 10-slot window and mask a real signal.

---

## 5b. Two encoding bugs found by smoke-testing on Windows

Both were caught by running the hook for real rather than only through the
test harness, and both would have silently dropped events in the field:

- **BOM on stdin.** Some Windows shells prepend a UTF-8 BOM when piping.
  `json.loads` rejects it outright, so every event would have been dropped
  on those setups — while the hook still exited 0, making it look healthy.
- **Locale-decoded stdin.** `sys.stdin.read()` decodes with the *locale*
  encoding, which is cp1252 on a default Windows install. Any payload
  containing a non-ASCII file path would raise or mangle. The hook now reads
  `sys.stdin.buffer` and decodes `utf-8-sig` explicitly, which fixes both.

Worth noting the failure mode: the sensor's own "never crash" guarantee
meant these bugs presented as *silence*, not as errors. A measure-only tool
that fails safe also fails quietly — Phase 3 dogfooding should include a
"has this actually recorded anything lately?" check rather than assuming
no news is good news.

---

## 6. Schema note

`running_context_tokens` is described in spec 3 as "cumulative for this
session". It is implemented as **the most recent main-chain prompt size**,
which is what makes the 663k assertion in spec 4 coherent — `prompt_tokens`
is already a full snapshot of context, so literally accumulating it would
produce millions. Columns added beyond the spec: `id` (needs a stable
ordering for window reconstruction), `model` and `context_window` (spec 2.1
asked for the model to be logged for Phase 4).

The rolling window is rebuilt from SQLite on every invocation, because each
hook call is a separate process with no in-memory state to inherit.

---

## 7. Test coverage against spec section 4

| Spec requirement | Test |
|---|---|
| Peak lands at 663k | `test_corpus_peak_context_matches_phase0` (extraction layer, exact) + `test_sensor_observed_peak_is_bounded_by_transcript_peak` (sensor layer, 662,671) |
| No row >100% of a detected window | `test_no_row_exceeds_its_context_window`, `test_windows_detected_as_1m` |
| Phase 0 events minus 4 scratchpad FPs | `test_repeat_read_events_exclude_scratchpad_false_positives` (asserts exactly 8) |
| No crash on malformed JSONL | `test_malformed_jsonl_lines_are_skipped`, `test_replay_survives_malformed_transcript`, `test_hook_never_crashes_on_garbage_input` |

Plus per-session peak regression for all 6 sessions, the three Phase 0 bug
fixes pinned individually, cross-process window continuity, and the
measure-only guarantee (`stdout` empty, exit 0).

Corpus tests skip cleanly when the transcripts are absent — they are real
session history and are not in the repo. Set `CONTEXT_GUARDIAN_CORPUS` to
point elsewhere.

```bash
python -m pytest tests/test_sensor.py -q
```

---

## Open question carried into Phase 2

Given finding #2, "context is high" and "the agent is re-reading the same
file" are mostly the *same* moment, not two. Before building the nudge,
decide whether repeat-reads are worth surfacing as their own message at all,
or whether they are better used to make the context nudge more specific —
i.e. *"context at 280k, and `session_manager.py` has been re-read 4 times"*
as one message rather than two. The corpus favours the second reading.
