"""SQLite storage: schema, migrations and connection handling.

Jarvis keeps one database under ``~/.jarvis/jarvis.db``. It holds the owner
profile, every session, the portfolio ledger, everything Jarvis has learned,
and the outcome of every prediction it has made. Nothing about Jarvis's
knowledge lives in memory only -- restart it and it picks up where it left off.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# v3 added watches; v2 added training_attempts. Every change so far has been purely additive, so
# `CREATE TABLE IF NOT EXISTS` upgrades an existing database in place.
SCHEMA_VERSION = 3

SCHEMA = """
-- Owner profile: name, preferences, anything Jarvis should remember about you.
CREATE TABLE IF NOT EXISTS profile (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per wake-up. Lets Jarvis say "last time we spoke...".
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT,
    channel     TEXT NOT NULL DEFAULT 'text',
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS utterances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL CHECK (role IN ('owner', 'jarvis')),
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_utterances_session ON utterances(session_id);

-- ---------------------------------------------------------------- portfolio
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    REAL NOT NULL,
    price       REAL NOT NULL,
    fees        REAL NOT NULL DEFAULT 0,
    executed_at TEXT NOT NULL DEFAULT (datetime('now')),
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(executed_at);

CREATE TABLE IF NOT EXISTS cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    amount      REAL NOT NULL,          -- positive deposit, negative withdrawal
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    note        TEXT
);

-- End-of-day mark. The previous day's P/L in the greeting comes from here.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    as_of_date    TEXT PRIMARY KEY,     -- YYYY-MM-DD
    cash          REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_value   REAL NOT NULL,
    day_pnl       REAL,
    day_pnl_pct   REAL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ----------------------------------------------------------------- learning
-- A curated teacher: a trader whose track record has been verified.
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,        -- youtube | instagram | tiktok | web | news
    handle        TEXT NOT NULL,
    display_name  TEXT,
    url           TEXT,
    verification  TEXT NOT NULL DEFAULT 'unverified',
                  -- verified | provisional | unverified | rejected
    credibility   REAL NOT NULL DEFAULT 0.5,   -- 0..1, moves with outcomes
    evidence      TEXT,                 -- why we believe they're real & proven
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (platform, handle)
);

CREATE TABLE IF NOT EXISTS content_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER REFERENCES sources(id),
    platform     TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    url          TEXT,
    title        TEXT,
    published_at TEXT,
    ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    body         TEXT,                  -- transcript / article text
    processed    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_content_processed ON content_items(processed);

-- The unit of knowledge. Seeded with foundations, grown by study, and
-- re-weighted every time a prediction it produced is graded.
CREATE TABLE IF NOT EXISTS lessons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    topic          TEXT NOT NULL,
    claim          TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'concept',
                   -- concept | pattern | rule | risk | fundamental
    pattern_key    TEXT,                -- links a lesson to a detector
    tier           TEXT NOT NULL DEFAULT 'foundation',
                   -- foundation | hypothesis | working | expertise | retired
    confidence     REAL NOT NULL DEFAULT 0.5,
    support        INTEGER NOT NULL DEFAULT 0,
    refute         INTEGER NOT NULL DEFAULT 0,
    source_id      INTEGER REFERENCES sources(id),
    content_id     INTEGER REFERENCES content_items(id),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (topic, claim)
);
CREATE INDEX IF NOT EXISTS idx_lessons_pattern ON lessons(pattern_key);
CREATE INDEX IF NOT EXISTS idx_lessons_tier ON lessons(tier);

-- ------------------------------------------------------------ market watch
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    pattern_key    TEXT NOT NULL,
    timeframe      TEXT NOT NULL DEFAULT '1d',
    direction      TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    detected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    price_at_signal REAL NOT NULL,
    horizon_bars   INTEGER NOT NULL DEFAULT 5,
    features       TEXT,                -- JSON blob of the detector's inputs
    lesson_id      INTEGER REFERENCES lessons(id),
    confidence     REAL NOT NULL DEFAULT 0.5,
    status         TEXT NOT NULL DEFAULT 'open',  -- open | graded | expired
    UNIQUE (symbol, pattern_key, timeframe, detected_at)
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

-- The feedback that makes the learning real: what actually happened next.
CREATE TABLE IF NOT EXISTS outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    INTEGER NOT NULL UNIQUE REFERENCES signals(id),
    evaluated_at TEXT NOT NULL DEFAULT (datetime('now')),
    exit_price   REAL NOT NULL,
    return_pct   REAL NOT NULL,
    hit          INTEGER NOT NULL       -- 1 the move went the predicted way
);

CREATE TABLE IF NOT EXISTS news_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    headline     TEXT NOT NULL,
    url          TEXT,
    published_at TEXT,
    seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    summary      TEXT,
    tickers      TEXT,                  -- comma separated
    impact       REAL NOT NULL DEFAULT 0,
    UNIQUE (source, headline)
);
CREATE INDEX IF NOT EXISTS idx_news_seen ON news_items(seen_at);

-- Cache of safety verdicts so a URL is only judged once per window.
CREATE TABLE IF NOT EXISTS url_verdicts (
    url         TEXT PRIMARY KEY,
    verdict     TEXT NOT NULL,          -- safe | unsafe | unknown
    reasons     TEXT,
    checked_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------- training
-- Caleb's progress through the day-trading curriculum. One row per attempt,
-- so improvement over time is visible rather than overwritten.
CREATE TABLE IF NOT EXISTS training_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    module_key   TEXT NOT NULL,
    score        INTEGER NOT NULL,
    out_of       INTEGER NOT NULL,
    passed       INTEGER NOT NULL,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_training_module ON training_attempts(module_key);

-- Standing instructions: "keep an eye on XOM below 105". Jarvis checks these
-- on every scan cycle and speaks up once when one triggers.
CREATE TABLE IF NOT EXISTS watches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    level        REAL NOT NULL,
    direction    TEXT NOT NULL CHECK (direction IN ('above', 'below')),
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    triggered_at TEXT,
    triggered_price REAL,
    active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (symbol, level, direction)
);
CREATE INDEX IF NOT EXISTS idx_watches_active ON watches(active);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """Thin SQLite wrapper with a per-thread connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._ensure_schema()

    # ------------------------------------------------------------- plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        with self.write() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction. Commits on success, rolls back on error."""
        with self._write_lock:
            conn = self.conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # --------------------------------------------------------------- reads
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple = ()):
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # -------------------------------------------------------------- writes
    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.write() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def executemany(self, sql: str, rows) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self.write() as conn:
            cur = conn.executemany(sql, rows)
            return cur.rowcount

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
