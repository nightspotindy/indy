"""SQLite persistence for nightspot — the session state machine and the
memory bank that visitors deposit into and are gifted from.

WAL mode, one connection per thread (thread-local). On init a lightweight
migration runs PRAGMA table_info over each table and ALTER TABLE ADD COLUMN
for anything missing, so a database created by an older version of the code
never crashes a newer version.
"""
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join("data", "nightspot.db")

SEED_SESSION_ID = "seed"
SEED_MEMORY = "I offer you the memory of a yellow rose seen at sunset."

_local = threading.local()

# Desired schema. Migration adds any of these columns that an existing table
# is missing. Order matters only for fresh CREATE TABLE.
_SCHEMA = {
    "memories": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("session_id", "TEXT"),
        ("category", "TEXT"),
        ("kind", "TEXT"),
        ("body", "TEXT"),
        ("created_at", "REAL"),
        ("times_given", "INTEGER DEFAULT 0"),
    ],
    "sessions": [
        ("id", "TEXT PRIMARY KEY"),
        ("state", "TEXT"),
        ("takes", "INTEGER DEFAULT 0"),
        ("path", "TEXT"),
        ("deposit_id", "INTEGER"),
        ("gift_id", "INTEGER"),
        ("created_at", "REAL"),
    ],
    "questions": [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("session_id", "TEXT UNIQUE"),
        ("body", "TEXT"),
        ("created_at", "REAL"),
    ],
}


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init() -> None:
    """Create tables if absent, then migrate to add any missing columns."""
    c = _conn()
    for table, cols in _SCHEMA.items():
        coldefs = ", ".join("{} {}".format(name, typ) for name, typ in cols)
        c.execute("CREATE TABLE IF NOT EXISTS {} ({})".format(table, coldefs))
    # Migration: add columns that older databases lack.
    for table, cols in _SCHEMA.items():
        existing = {row["name"] for row in c.execute(
            "PRAGMA table_info({})".format(table))}
        for name, typ in cols:
            if name not in existing:
                # PRIMARY KEY / UNIQUE can't be added via ALTER; strip them.
                addtyp = typ.replace("PRIMARY KEY AUTOINCREMENT", "") \
                            .replace("PRIMARY KEY", "") \
                            .replace("UNIQUE", "").strip()
                c.execute("ALTER TABLE {} ADD COLUMN {} {}".format(
                    table, name, addtyp))
    c.commit()
    _seed()


def _seed() -> None:
    """Seed the bank with one memory under session_id 'seed'."""
    c = _conn()
    row = c.execute(
        "SELECT id FROM memories WHERE session_id=?", (SEED_SESSION_ID,)
    ).fetchone()
    if row is None:
        c.execute(
            "INSERT INTO memories (session_id, category, kind, body, "
            "created_at, times_given) VALUES (?,?,?,?,?,0)",
            (SEED_SESSION_ID, "memory", "text", SEED_MEMORY, time.time()),
        )
        c.commit()


# --- sessions ---------------------------------------------------------------

def create_session(session_id: str, state: str = "capture") -> None:
    c = _conn()
    c.execute(
        "INSERT OR REPLACE INTO sessions (id, state, takes, path, deposit_id, "
        "gift_id, created_at) VALUES (?,?,0,NULL,NULL,NULL,?)",
        (session_id, state, time.time()),
    )
    c.commit()


def get_session(session_id: str) -> Optional[sqlite3.Row]:
    c = _conn()
    return c.execute(
        "SELECT * FROM sessions WHERE id=?", (session_id,)
    ).fetchone()


def update_session(session_id: str, **fields: Any) -> None:
    if not fields:
        return
    c = _conn()
    cols = ", ".join("{}=?".format(k) for k in fields)
    vals = list(fields.values())
    vals.append(session_id)
    c.execute("UPDATE sessions SET {} WHERE id=?".format(cols), vals)
    c.commit()


# --- memories (the bank) ----------------------------------------------------

def add_memory(session_id: str, category: str, kind: str, body: str) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO memories (session_id, category, kind, body, created_at, "
        "times_given) VALUES (?,?,?,?,?,0)",
        (session_id, category, kind, body, time.time()),
    )
    c.commit()
    return int(cur.lastrowid)


def get_memory(memory_id: int) -> Optional[sqlite3.Row]:
    c = _conn()
    return c.execute(
        "SELECT * FROM memories WHERE id=?", (memory_id,)
    ).fetchone()


def pick_gift(session_id: str, category: str) -> Optional[sqlite3.Row]:
    """Choose one deposit to gift: same category, least-circulated first,
    never the visitor's own. Falls back to any category if theirs is empty."""
    c = _conn()
    row = c.execute(
        "SELECT * FROM memories WHERE category=? AND session_id!=? "
        "ORDER BY times_given ASC, id ASC LIMIT 1",
        (category, session_id),
    ).fetchone()
    if row is not None:
        return row
    return c.execute(
        "SELECT * FROM memories WHERE session_id!=? "
        "ORDER BY times_given ASC, id ASC LIMIT 1",
        (session_id,),
    ).fetchone()


def mark_given(memory_id: int) -> None:
    c = _conn()
    c.execute(
        "UPDATE memories SET times_given = times_given + 1 WHERE id=?",
        (memory_id,),
    )
    c.commit()


# --- questions --------------------------------------------------------------

def add_question(session_id: str, body: str) -> bool:
    """Record one question per session. Returns False if this session has
    already asked (UNIQUE constraint on session_id)."""
    c = _conn()
    try:
        c.execute(
            "INSERT INTO questions (session_id, body, created_at) "
            "VALUES (?,?,?)",
            (session_id, body, time.time()),
        )
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_question(session_id: str) -> Optional[sqlite3.Row]:
    c = _conn()
    return c.execute(
        "SELECT * FROM questions WHERE session_id=?", (session_id,)
    ).fetchone()
