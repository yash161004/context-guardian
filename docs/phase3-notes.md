# Phase 3 — Package and Ship

**Status:** packaged, validated, committed locally. Dogfooding has started.
**Deliverables:** `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
`hooks/hooks.json`, `README.md`, `LICENSE` (MIT), `context_guardian/selfcheck.py`,
`commands/context-guardian-status.md`, git repo.

`claude plugin validate . --strict` passes.

---

## What I decided, and why

### The manifest lives at `.claude-plugin/plugin.json`, not the repo root

Easy thing to get wrong — the roadmap said "`plugin.json`". The actual
discovery path is `.claude-plugin/plugin.json`, with hooks declared either
inline or, as here, at `hooks/hooks.json`. Plugin-relative paths use
`${CLAUDE_PLUGIN_ROOT}`.

### Explicit version `0.1.0`, not commit-SHA versioning

Omitting `version` makes Claude Code use the git commit SHA, so users get
every commit. That's right for a plugin under active internal development
and wrong for one about to be posted publicly — it would ship half-finished
work to strangers the moment it's pushed.

Pinned to `0.1.0`. **The cost is that it must be bumped for users to receive
anything**; pushing commits alone does nothing. Worth it for a launch
artifact, and `0.x` sets honest expectations.

### `python3` in the plugin, `python` in the local install

There is no single interpreter name that works everywhere: Debian/Ubuntu
generally have no `python`, and Windows generally has no `python3`. A static
JSON command string can't branch.

Split by audience rather than picking a loser:
- `hooks/hooks.json` (strangers, mostly macOS/Linux) → `python3`
- the local dogfooding install in `settings.json` (Windows) → `python`

The README calls out the one-line Windows change. This is the roughest edge
in the install and a prime candidate for the first bug report — which is
useful Phase 4 signal in itself.

### A self-check command, because both hooks fail silently

Flagged at the end of Phase 1 and again in Phase 2, now built:

```bash
python3 -m context_guardian.selfcheck
```

The design constraint that makes this necessary: both hooks are built never
to crash a session, so a completely broken install is indistinguishable from
a quiet one. Two real bugs during development (a BOM on stdin, and
locale-decoded stdin) presented as *nothing at all*.

It reports config, database reachability, whether rows landed in the last
24h, peak context, and nudges emitted — with a diagnosis attached to each
failure rather than a bare red mark. It caught its own uninstalled state
correctly on first run.

One wording fix during testing: it originally claimed "no nudge emitted — is
the hook installed?" immediately after install, which is a false alarm, since
the nudge only fires on the *next* prompt. Now says so.

Also exposed as `/context-guardian-status` via `commands/`.

---

## Dogfooding is live

The hooks are installed in `~/.claude/settings.json` (backed up first to
`settings.json.bak-<timestamp>`, all pre-existing keys preserved).

First real reading, taken from the session that built this:

```
[  ok  ] 2 tool call(s) in the last 24h - sensor is live
[  ok  ] peak context observed: 202k tokens
```

The session writing Context Guardian crossed Context Guardian's own warn
threshold. That is the intended 2–3 working days of dogfooding beginning, and
it is the only QA budget this project gets before strangers see it.

**To uninstall:** delete the two entries under `hooks` in
`~/.claude/settings.json`, or restore the `.bak-` file alongside it.

### ⚠️ Do not also `/plugin install` on this machine

The hooks are currently registered manually in `settings.json` for
dogfooding. Installing the published plugin *as well* would register a
second copy of both hooks, so every tool call would be recorded twice and
nudge rate-limiting would be evaluated against doubled rows.

Before testing the plugin install path on this machine, remove the two
`settings.json` entries first. Strangers are unaffected — they only ever have
one copy.

---

## Deliberately not done

*(Superseded — the repo is now live at
https://github.com/yash161004/context-guardian, pushed on explicit
instruction. The GitHub account is `yash161004`, so the README install line
and the manifest `homepage`/`repository` fields were corrected before the
push. Committed files were scanned for credentials first; clean.)*

**No before/after GIF.** The roadmap asks for one in the README and it is the
single highest-value thing still missing — it needs a real session that
actually nudged, which is what dogfooding is about to produce. Capture it
from a genuine nudge rather than staging one.

---

## Carried into Phase 4

- **The wording is still untested.** Thresholds are measured; whether the
  message actually changes Claude's behaviour mid-session cannot be
  unit-tested and only dogfooding will show.
- **`max_nudges_per_session: 12` is a guess**, unlike the token thresholds.
- **The thresholds are one person's corpus.** The README says so explicitly.
  Anyone on a 200k-window model needs to lower them, and if that turns out to
  be most users, the default is wrong and Phase 0 should be re-run on donated
  transcripts.
- **The v1 → v2 gate stands.** 50+ organic installs, or 3+ people
  independently reporting it caught something real, or an unprompted "does
  this work for my team?". None of those inside ~3–4 weeks means ship it as a
  portfolio piece and move on.
