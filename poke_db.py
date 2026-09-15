"""SQLite access, plus the migration step.

`CREATE TABLE IF NOT EXISTS` creates tables but never alters them, so a
database made before a column existed silently lacks it and every query
mentioning that column fails at runtime. `migrate()` closes that gap by adding
missing columns on boot.

Additive only: it adds columns, never drops or retypes them. Anything more than
that needs a real migration tool, and this project is nowhere near needing one.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("DRAFT_DB", "draft.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "database_schema.sql"

# table -> column -> the DDL fragment to add it with.
# Kept in step with database_schema.sql by the test in tests/test_schema.py.
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "season": {
        "format_key": "TEXT",
        "tier_snapshot": "TEXT",
        "pool_label": "TEXT",
        "created_at": "TEXT",
    },
    "format": {
        "ladder": "TEXT NOT NULL DEFAULT 'singles'",
        "species_banned": "INTEGER NOT NULL DEFAULT 0",
        "showdown_name": "TEXT",
    },
    "draft": {
        "pick_seconds": "INTEGER NOT NULL DEFAULT 120",
        "clock_started_at": "TEXT",
    },
    "pool_entry": {
        "showdown_id": "TEXT NOT NULL DEFAULT ''",
        "stats": "TEXT NOT NULL DEFAULT '{}'",
        "artwork_url": "TEXT",
        "tier": "TEXT",
        "banned": "INTEGER NOT NULL DEFAULT 0",
    },
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        # FastAPI runs sync endpoints in a threadpool and does not guarantee
        # that a dependency's cleanup runs on the thread that opened the
        # connection. Fails under uvicorn but not under TestClient.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE around a block, so concurrent writers serialize."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any expected column the table is missing. Returns what it added."""
    applied: list[str] = []
    for table, wanted in EXPECTED_COLUMNS.items():
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue   # brand new database; the schema script already made it
        present = columns(conn, table)
        for column, ddl in wanted.items():
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                applied.append(f"{table}.{column}")
    return applied


def rebuild_draft_pick(conn: sqlite3.Connection) -> bool:
    """Drop NOT NULL from draft_pick.pool_entry_id on an older database.

    SQLite cannot relax a column constraint in place, so this is the standard
    rebuild: make the new table, copy the rows, swap the names. Only runs when
    the old constraint is actually present, so it is a no-op on every boot
    after the first.

    A forfeited pick stores NULL here, and the old schema rejects that — the
    draft would fail at the exact moment a team ran out of time, which is the
    worst possible moment to discover a migration was skipped.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='draft_pick'"
    ).fetchone():
        return False
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(draft_pick)")}
    entry = columns.get("pool_entry_id")
    if entry is None or not entry["notnull"]:
        return False

    conn.executescript("""
        CREATE TABLE draft_pick_new (
            id            INTEGER PRIMARY KEY,
            season_id     INTEGER NOT NULL REFERENCES season(id) ON DELETE CASCADE,
            pick_no       INTEGER NOT NULL,
            round_no      INTEGER NOT NULL,
            team_id       INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
            pool_entry_id INTEGER REFERENCES pool_entry(id) ON DELETE CASCADE,
            cost_paid     INTEGER NOT NULL,
            picked_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (season_id, pick_no),
            UNIQUE (season_id, pool_entry_id)
        );
        INSERT INTO draft_pick_new SELECT * FROM draft_pick;
        DROP TABLE draft_pick;
        ALTER TABLE draft_pick_new RENAME TO draft_pick;
        CREATE INDEX IF NOT EXISTS idx_draft_pick_season ON draft_pick(season_id);
        CREATE INDEX IF NOT EXISTS idx_draft_pick_team ON draft_pick(team_id);
    """)
    return True


def init_db() -> list[str]:
    """Create tables, then patch up any older database. Safe on every boot."""
    conn = connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        applied = migrate(conn)
        if rebuild_draft_pick(conn):
            applied.append("draft_pick.pool_entry_id (nullable)")
        conn.commit()
        return applied
    finally:
        conn.close()