"""Repeat-read detection.

Phase 0 established this as an *independent* signal, not a co-factor on the
context trigger: all 12 events in the validation corpus fired at 16-37%
context, nowhere near the token thresholds. The two signals do not co-occur,
so they are evaluated separately.
"""

from collections import deque
from fnmatch import fnmatch


def normalise_path(file_path):
    """Lowercased, forward-slash form used for both matching and counting.

    Windows paths arrive with backslashes and inconsistent case; without
    this, `D:\\App\\main.py` and `d:/app/main.py` count as different files
    and the detector silently under-reports.
    """
    if not file_path:
        return None
    return file_path.replace("\\", "/").lower()


def is_scratchpad(file_path, patterns):
    """True if this path is agent scratchpad / background-task output.

    Phase 0: 4 of 12 repeat-read events were the agent polling
    `tasks/*.output` files to see whether a delegated background task had
    finished. That is the workflow this project wants to *encourage*.
    Counting it as confusion would make v1 nag about the exact behaviour it
    is trying to promote.
    """
    norm = normalise_path(file_path)
    if not norm:
        return False
    return any(fnmatch(norm, p.lower()) for p in patterns)


class RepeatReadDetector:
    """Rolling-window counter for repeated reads of the same file.

    `window_counts` selects what the window is measured in:
      - "reads"      : last N Read calls (matches the Phase 0 analysis that
                       produced threshold=3)
      - "tool_calls" : last N tool calls of any kind (wider in wall-clock
                       terms, so it fires less often)
    """

    def __init__(self, window=10, threshold=3, scratchpad_patterns=(),
                 window_counts="reads"):
        self.window = window
        self.threshold = threshold
        self.scratchpad_patterns = list(scratchpad_patterns)
        self.window_counts = window_counts
        self._history = deque(maxlen=window)

    def record(self, tool_name, file_path, read_tools=("Read",)):
        """Record a tool call; return the repeat count for this file.

        Returns None for calls that are not counted (non-read tools, reads
        without a path, and excluded scratchpad paths).
        """
        is_read = tool_name in read_tools and bool(file_path)
        norm = normalise_path(file_path) if is_read else None

        if is_read and is_scratchpad(file_path, self.scratchpad_patterns):
            # Excluded from counting entirely - it must not occupy a window
            # slot either, or a burst of task-polling would flush genuine
            # repeat-reads out of the window and mask a real signal.
            return None

        if self.window_counts == "tool_calls":
            self._history.append(norm)  # None for non-read calls
        elif norm is not None:
            self._history.append(norm)

        if norm is None:
            return None
        return list(self._history).count(norm)

    def fires(self, count):
        return count is not None and count >= self.threshold
