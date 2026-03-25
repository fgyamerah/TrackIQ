"""
SQLite database layer — all pipeline state and logging lives here.
"""
import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import config

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath         TEXT    NOT NULL,       -- final path after organization
    original_path    TEXT,                   -- path before organization (inbox location)
    original_meta    TEXT,                   -- JSON: tags snapshot before sanitization
    cleaned_meta     TEXT,                   -- JSON: tags snapshot after sanitization
    actions          TEXT,                   -- JSON list of action strings performed
    created_at       TEXT    NOT NULL,
    rolled_back      INTEGER NOT NULL DEFAULT 0,
    rolled_back_at   TEXT,
    rollback_note    TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_filepath ON track_history(filepath);

CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath        TEXT    NOT NULL UNIQUE,
    filename        TEXT    NOT NULL,
    artist          TEXT,
    title           TEXT,
    genre           TEXT,
    bpm             REAL,
    key_musical     TEXT,
    key_camelot     TEXT,
    duration_sec    REAL,
    bitrate_kbps    INTEGER,
    filesize_bytes  INTEGER,
    status          TEXT    NOT NULL DEFAULT 'pending',
    error_msg       TEXT,
    processed_at    TEXT,
    pipeline_ver    TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT    NOT NULL,
    dry_run         INTEGER NOT NULL DEFAULT 0,
    inbox_count     INTEGER DEFAULT 0,
    processed       INTEGER DEFAULT 0,
    rejected        INTEGER DEFAULT 0,
    duplicates      INTEGER DEFAULT 0,
    unsorted        INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    duration_sec    REAL
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES pipeline_runs(id),
    original        TEXT    NOT NULL,
    duplicate       TEXT    NOT NULL,
    reason          TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_status   ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_filepath ON tracks(filepath);
CREATE INDEX IF NOT EXISTS idx_dupes_run       ON duplicate_groups(run_id);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Track operations
# ---------------------------------------------------------------------------
def upsert_track(filepath: str, **kwargs: Any) -> None:
    """Insert or update a track record. filepath is the unique key."""
    kwargs["filepath"]     = filepath
    kwargs["filename"]     = Path(filepath).name
    kwargs.setdefault("processed_at", _now())
    kwargs.setdefault("pipeline_ver", config.PIPELINE_VERSION)

    cols         = list(kwargs.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates      = ", ".join(
        f"{c}=excluded.{c}" for c in cols if c != "filepath"
    )
    sql = (
        f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({placeholders})"
        f" ON CONFLICT(filepath) DO UPDATE SET {updates}"
    )
    with get_conn() as conn:
        conn.execute(sql, list(kwargs.values()))


def get_track(filepath: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tracks WHERE filepath=?", (filepath,)
        ).fetchone()


def is_processed(filepath: str) -> bool:
    """Return True only if this track completed the pipeline successfully."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM tracks WHERE filepath=?", (filepath,)
        ).fetchone()
        return row is not None and row["status"] == "ok"


def mark_status(filepath: str, status: str, error_msg: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracks SET status=?, error_msg=?, processed_at=? WHERE filepath=?",
            (status, error_msg, _now(), filepath),
        )


def get_tracks_by_status(status: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tracks WHERE status=?", (status,)
        ).fetchall()


def get_all_ok_tracks():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tracks WHERE status='ok' ORDER BY artist, title"
        ).fetchall()


# ---------------------------------------------------------------------------
# Pipeline run operations
# ---------------------------------------------------------------------------
def start_run(dry_run: bool) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (run_at, dry_run) VALUES (?, ?)",
            (_now(), int(dry_run)),
        )
        return cur.lastrowid


def finish_run(run_id: int, **stats: Any) -> None:
    if not stats:
        return
    cols = ", ".join(f"{k}=?" for k in stats)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE pipeline_runs SET {cols} WHERE id=?",
            list(stats.values()) + [run_id],
        )


# ---------------------------------------------------------------------------
# Duplicate operations
# ---------------------------------------------------------------------------
def log_duplicate(run_id: int, original: str, duplicate: str, reason: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO duplicate_groups (run_id, original, duplicate, reason)"
            " VALUES (?, ?, ?, ?)",
            (run_id, original, duplicate, reason),
        )


def get_unresolved_duplicates(run_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM duplicate_groups WHERE run_id=? AND resolved=0",
            (run_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Track history operations
# ---------------------------------------------------------------------------
def save_track_history(
    filepath: str,
    original_path: str,
    original_meta: dict,
    actions: list,
) -> int:
    """
    Insert a history record immediately after a file is organized.

    Args:
        filepath:      Final library path (after move).
        original_path: Original inbox path (before move).
        original_meta: Dict of tag values captured before sanitization.
        actions:       List of action strings, e.g. ['organized', 'sanitized'].

    Returns the new history row ID.
    """
    import json
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO track_history "
            "(filepath, original_path, original_meta, actions, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                filepath,
                original_path,
                json.dumps(original_meta, ensure_ascii=False),
                json.dumps(actions),
                _now(),
            ),
        )
        return cur.lastrowid


def update_track_history_cleaned(filepath: str, cleaned_meta: dict) -> None:
    """
    Update the cleaned_meta field on the most recent history record for filepath.
    Called by the sanitizer after it has written sanitized tags.
    """
    import json
    with get_conn() as conn:
        conn.execute(
            "UPDATE track_history SET cleaned_meta=? "
            "WHERE filepath=? AND id=(SELECT MAX(id) FROM track_history WHERE filepath=?)",
            (json.dumps(cleaned_meta, ensure_ascii=False), filepath, filepath),
        )


def get_track_history(filepath: Optional[str] = None, include_rolled_back: bool = False):
    """
    Return history records, optionally filtered by filepath and rollback status.
    """
    with get_conn() as conn:
        if filepath:
            sql = "SELECT * FROM track_history WHERE filepath=?"
            args: list = [filepath]
        else:
            sql = "SELECT * FROM track_history WHERE 1=1"
            args = []
        if not include_rolled_back:
            sql += " AND rolled_back=0"
        sql += " ORDER BY created_at DESC"
        return conn.execute(sql, args).fetchall()


def get_history_by_id(history_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM track_history WHERE id=?", (history_id,)
        ).fetchone()


def mark_rolled_back(history_id: int, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE track_history SET rolled_back=1, rolled_back_at=?, rollback_note=? WHERE id=?",
            (_now(), note, history_id),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
