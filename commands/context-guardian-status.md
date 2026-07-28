---
description: Check whether Context Guardian is alive and what it has recorded
---

Run Context Guardian's self check and report the result to the user:

```bash
python3 -m context_guardian.selfcheck
```

Run it from the plugin's own directory (`${CLAUDE_PLUGIN_ROOT}`), or with
that directory on `PYTHONPATH`. On Windows, use `python` instead of
`python3`.

Summarise the output plainly: whether the sensor is recording, the peak
context observed, and any nudges emitted. If the check reports problems,
explain the likely cause from its output rather than restating the raw text.
