"""SQLite state for Context Guardian.

One row per tool call, flat by design - session-level views come from
`GROUP BY session_id`, not a second table. Phase 1 needs to be right, not
normalised.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id             TEXT    NOT NULL,
    tool_name              TEXT,
    file_path              TEXT,
    is_sidechain           INTEGER NOT NULL DEFAULT 0,
    timestamp              TEXT    NOT NULL,
    model                  TEXT,
    context_window         INTEGER,
    prompt_tokens          INTEGER,
    running_context_tokens INTEGER,
    repeat_read_count      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session
    ON tool_calls (session_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_file
    ON tool_calls (session_id, file_path);

-- Phase 2. One row per nudge actually emitted, which is what makes
-- "fire once on entering a level" possible across processes.
CREATE TABLE IF NOT EXISTS nudges (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT    NOT NULL,
    level          TEXT    NOT NULL,   -- 'warn' | 'urgent' | 'repeat_read'
    subject        TEXT,               -- file path, for repeat_read nudges
    context_tokens INTEGER,
    timestamp      TEXT    NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    message        TEXT
);

CREATE INDEX IF NOT EXISTS idx_nudges_session
    ON nudges (session_id, level, active);
"""


def connect(db_path):
    """Open (creating if needed) the state database."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL keeps the hook from blocking when several sessions run at once -
    # a realistic case, since people keep multiple Claude Code windows open.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Additive column migrations for databases created by older versions.

    Kept deliberately dumb - add-column-if-missing only. A sensor that
    destroys someone's collected history to reshape a table is not a trade
    worth making.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(nudges)")}
    for column, ddl in (("transcript_path", "TEXT"),
                        ("message_version", "TEXT")):
        if column not in have:
            conn.execute(f"ALTER TABLE nudges ADD COLUMN {column} {ddl}")

    # Backfill from the stored message text rather than by date: every v1
    # message recommended a subagent and no v2 message does, so the row
    # carries its own evidence - better than guessing from a commit date.
    #
    # Driven by the data (rows still NULL), NOT by "did we just add the
    # column". Conditioning a backfill on the schema-change event silently
    # skips every database where the column was added by an earlier version
    # that had no backfill - which is exactly what happened here.
    conn.execute(
        """UPDATE nudges SET message_version =
               CASE WHEN message LIKE '%subagent%' THEN 'v1' ELSE 'v2' END
             WHERE message_version IS NULL""")


def record_tool_call(conn, *, session_id, tool_name, file_path, is_sidechain,
                     timestamp, model=None, context_window=None,
                     prompt_tokens=None, running_context_tokens=None,
                     repeat_read_count=None, commit=True):
    """Insert one tool-call observation.

    The live hook commits every row (one row per process, durability matters).
    Bulk replay passes commit=False and commits once at the end.
    """
    conn.execute(
        """
        INSERT INTO tool_calls (
            session_id, tool_name, file_path, is_sidechain, timestamp,
            model, context_window, prompt_tokens, running_context_tokens,
            repeat_read_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, tool_name, file_path, 1 if is_sidechain else 0, timestamp,
         model, context_window, prompt_tokens, running_context_tokens,
         repeat_read_count),
    )
    if commit:
        conn.commit()


def last_running_context(conn, session_id):
    """Most recent known main-chain context size for a session.

    Used when the transcript tail yields nothing (e.g. the async write has
    not landed yet) so the sensor carries the previous reading forward
    rather than recording a spurious drop to zero.
    """
    row = conn.execute(
        """
        SELECT running_context_tokens
          FROM tool_calls
         WHERE session_id = ?
           AND is_sidechain = 0
           AND running_context_tokens IS NOT NULL
         ORDER BY id DESC
         LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return row["running_context_tokens"] if row else None


def has_active_nudge(conn, session_id, level, subject=None):
    """True if this level (or level+subject) has already fired and not re-armed."""
    if subject is None:
        row = conn.execute(
            """SELECT 1 FROM nudges
                WHERE session_id = ? AND level = ? AND active = 1 LIMIT 1""",
            (session_id, level),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT 1 FROM nudges
                WHERE session_id = ? AND level = ? AND subject = ? AND active = 1
                LIMIT 1""",
            (session_id, level, subject),
        ).fetchone()
    return row is not None


def record_nudge(conn, *, session_id, level, message, subject=None,
                 context_tokens=None, timestamp, transcript_path=None,
                 message_version=None):
    conn.execute(
        """INSERT INTO nudges
               (session_id, level, subject, context_tokens, timestamp,
                active, message, transcript_path, message_version)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (session_id, level, subject, context_tokens, timestamp, message,
         transcript_path, message_version),
    )
    conn.commit()


def rearm_levels(conn, session_id, levels):
    """Clear the fired flag so a level can nudge again on the next crossing."""
    if not levels:
        return 0
    marks = ",".join("?" for _ in levels)
    cur = conn.execute(
        f"""UPDATE nudges SET active = 0
             WHERE session_id = ? AND active = 1 AND level IN ({marks})""",
        (session_id, *levels),
    )
    conn.commit()
    return cur.rowcount


def count_nudges(conn, session_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM nudges WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row["n"] if row else 0


def hottest_recent_read(conn, session_id, window, threshold):
    """The file most recently seen tripping the repeat-read threshold.

    Scoped to the last `window` counted reads so a file that was hammered
    early in a long session does not keep resurfacing hours later.
    """
    rows = conn.execute(
        """SELECT file_path, repeat_read_count
             FROM tool_calls
            WHERE session_id = ? AND is_sidechain = 0
              AND repeat_read_count IS NOT NULL
            ORDER BY id DESC
            LIMIT ?""",
        (session_id, window),
    ).fetchall()
    best = None
    for row in rows:
        if row["repeat_read_count"] >= threshold:
            if best is None or row["repeat_read_count"] > best[1]:
                best = (row["file_path"], row["repeat_read_count"])
    return best


def session_summary(conn, session_id=None):
    """Per-session rollup, for eyeballing state during dogfooding."""
    sql = """
        SELECT session_id,
               COUNT(*)                        AS tool_calls,
               MAX(running_context_tokens)     AS peak_context_tokens,
               MAX(context_window)             AS context_window,
               SUM(CASE WHEN repeat_read_count >= 3 THEN 1 ELSE 0 END)
                                               AS repeat_read_events,
               MIN(timestamp)                  AS started,
               MAX(timestamp)                  AS last_seen
          FROM tool_calls
         WHERE is_sidechain = 0
    """
    params = ()
    if session_id:
        sql += " AND session_id = ?"
        params = (session_id,)
    sql += " GROUP BY session_id ORDER BY last_seen DESC"
    return conn.execute(sql, params).fetchall()
