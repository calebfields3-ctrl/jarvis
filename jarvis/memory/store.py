"""Typed accessors over the SQLite store.

Grouped by concern: who the owner is, what happened in past sessions, what
Jarvis has learned, and what it predicted. Everything here is synchronous and
cheap -- callers can hit it from the scan loop without worrying.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .db import Database


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------- profile
class ProfileStore:
    """Long-term facts about the owner. This is what makes Jarvis know Caleb."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        value = self.db.scalar("SELECT value FROM profile WHERE key = ?", (key,))
        return default if value is None else value

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO profile(key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, str(value)),
        )

    def all(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.db.query("SELECT key, value FROM profile")}

    @property
    def name(self) -> str:
        return self.get("owner_name", "there") or "there"

    @name.setter
    def name(self, value: str) -> None:
        self.set("owner_name", value)


# -------------------------------------------------------------------- sessions
class SessionStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def start(self, channel: str = "text") -> int:
        return self.db.execute(
            "INSERT INTO sessions(channel) VALUES (?)", (channel,)
        )

    def end(self, session_id: int, summary: str | None = None) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_at = datetime('now'), summary = ? WHERE id = ?",
            (summary, session_id),
        )

    def record(self, session_id: int, role: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO utterances(session_id, role, text) VALUES (?, ?, ?)",
            (session_id, role, text),
        )

    def last_session(self, before_id: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM sessions WHERE ended_at IS NOT NULL"
        params: tuple = ()
        if before_id is not None:
            sql += " AND id < ?"
            params = (before_id,)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.db.query_one(sql, params)
        return dict(row) if row else None

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM sessions") or 0)

    def recent_utterances(self, session_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT role, text, created_at FROM utterances WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [dict(r) for r in reversed(rows)]


# ------------------------------------------------------------------- knowledge
@dataclass
class Lesson:
    id: int
    topic: str
    claim: str
    kind: str
    pattern_key: str | None
    tier: str
    confidence: float
    support: int
    refute: int

    @classmethod
    def from_row(cls, row) -> "Lesson":
        return cls(
            id=row["id"],
            topic=row["topic"],
            claim=row["claim"],
            kind=row["kind"],
            pattern_key=row["pattern_key"],
            tier=row["tier"],
            confidence=row["confidence"],
            support=row["support"],
            refute=row["refute"],
        )


class KnowledgeStore:
    """Lessons plus the sources they came from.

    Confidence is a Beta posterior mean over the lesson's own track record:
    ``(support + prior_a) / (support + refute + prior_a + prior_b)``. A lesson
    read in a video starts near its source's credibility and then moves only as
    real outcomes come in, so a persuasive video cannot manufacture certainty.
    """

    PRIOR_STRENGTH = 4.0

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- sources --------------------------------------------------------
    def upsert_source(
        self,
        platform: str,
        handle: str,
        display_name: str | None = None,
        url: str | None = None,
        verification: str = "unverified",
        credibility: float = 0.5,
        evidence: str | None = None,
    ) -> int:
        self.db.execute(
            """
            INSERT INTO sources(platform, handle, display_name, url, verification,
                                credibility, evidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, handle) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, sources.display_name),
                url          = COALESCE(excluded.url, sources.url),
                verification = excluded.verification,
                evidence     = COALESCE(excluded.evidence, sources.evidence)
            """,
            (platform, handle, display_name, url, verification, credibility, evidence),
        )
        return int(
            self.db.scalar(
                "SELECT id FROM sources WHERE platform = ? AND handle = ?",
                (platform, handle),
            )
        )

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return dict(row) if row else None

    def trusted_sources(self, platform: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources WHERE verification IN ('verified', 'provisional')"
        params: tuple = ()
        if platform:
            sql += " AND platform = ?"
            params = (platform,)
        sql += " ORDER BY credibility DESC"
        return [dict(r) for r in self.db.query(sql, params)]

    def adjust_source_credibility(self, source_id: int, delta: float) -> None:
        self.db.execute(
            "UPDATE sources SET credibility = MAX(0.05, MIN(0.99, credibility + ?)) "
            "WHERE id = ?",
            (delta, source_id),
        )

    # -- content --------------------------------------------------------
    def add_content(
        self,
        platform: str,
        external_id: str,
        *,
        source_id: int | None = None,
        url: str | None = None,
        title: str | None = None,
        published_at: str | None = None,
        body: str | None = None,
    ) -> int | None:
        """Store a piece of study material. Returns None if already ingested."""
        existing = self.db.scalar(
            "SELECT id FROM content_items WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        )
        if existing is not None:
            return None
        return self.db.execute(
            """
            INSERT INTO content_items(source_id, platform, external_id, url, title,
                                      published_at, body)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source_id, platform, external_id, url, title, published_at, body),
        )

    def unprocessed_content(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM content_items WHERE processed = 0 ORDER BY id LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def mark_processed(self, content_id: int) -> None:
        self.db.execute(
            "UPDATE content_items SET processed = 1 WHERE id = ?", (content_id,)
        )

    # -- lessons --------------------------------------------------------
    def add_lesson(
        self,
        topic: str,
        claim: str,
        *,
        kind: str = "concept",
        pattern_key: str | None = None,
        tier: str = "hypothesis",
        confidence: float = 0.5,
        source_id: int | None = None,
        content_id: int | None = None,
    ) -> int:
        existing = self.db.query_one(
            "SELECT id FROM lessons WHERE topic = ? AND claim = ?", (topic, claim)
        )
        if existing:
            # Seeing the same claim from another teacher is corroboration, not
            # a new lesson: nudge confidence rather than duplicating.
            self.db.execute(
                "UPDATE lessons SET confidence = MIN(0.95, confidence + 0.02), "
                "updated_at = datetime('now') WHERE id = ?",
                (existing["id"],),
            )
            return int(existing["id"])
        return self.db.execute(
            """
            INSERT INTO lessons(topic, claim, kind, pattern_key, tier, confidence,
                                source_id, content_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (topic, claim, kind, pattern_key, tier, confidence, source_id, content_id),
        )

    def get_lesson(self, lesson_id: int) -> Lesson | None:
        row = self.db.query_one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
        return Lesson.from_row(row) if row else None

    def lesson_for_pattern(self, pattern_key: str) -> Lesson | None:
        """Best lesson behind a detector.

        Retired lessons are still returned, ranked last. That is deliberate: a
        retired pattern must keep accumulating outcomes so it can recover if the
        regime changes, and its low confidence already damps any signal it
        produces. Dropping it here would silently reset it to a neutral 0.5.
        """
        row = self.db.query_one(
            "SELECT * FROM lessons WHERE pattern_key = ? "
            "ORDER BY (tier = 'retired') ASC, confidence DESC LIMIT 1",
            (pattern_key,),
        )
        return Lesson.from_row(row) if row else None

    def lessons(
        self, tier: str | None = None, topic: str | None = None, limit: int = 50
    ) -> list[Lesson]:
        sql = "SELECT * FROM lessons WHERE 1=1"
        params: list[Any] = []
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        if topic:
            sql += " AND topic = ?"
            params.append(topic)
        sql += " ORDER BY confidence DESC, id LIMIT ?"
        params.append(limit)
        return [Lesson.from_row(r) for r in self.db.query(sql, tuple(params))]

    def reinforce(
        self, lesson_id: int, *, hit: bool, promotion_samples: int, promotion_conf: float
    ) -> Lesson | None:
        """Fold one graded outcome into a lesson and re-tier it."""
        row = self.db.query_one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
        if row is None:
            return None
        support = row["support"] + (1 if hit else 0)
        refute = row["refute"] + (0 if hit else 1)
        # Prior pulls toward the confidence the lesson was born with, weighted
        # by PRIOR_STRENGTH pseudo-observations.
        prior_a = self.PRIOR_STRENGTH * row["confidence"]
        prior_b = self.PRIOR_STRENGTH * (1 - row["confidence"])
        confidence = (support + prior_a) / (support + refute + prior_a + prior_b)

        samples = support + refute
        tier = row["tier"]
        if tier != "foundation":
            if samples >= promotion_samples and confidence >= promotion_conf:
                tier = "expertise"
            elif samples >= promotion_samples and confidence < 0.45:
                tier = "retired"
            elif samples >= max(5, promotion_samples // 4):
                tier = "working"
            else:
                tier = "hypothesis"

        self.db.execute(
            "UPDATE lessons SET support = ?, refute = ?, confidence = ?, tier = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (support, refute, round(confidence, 4), tier, lesson_id),
        )
        if row["source_id"]:
            self.adjust_source_credibility(row["source_id"], 0.01 if hit else -0.015)
        return self.get_lesson(lesson_id)

    def stats(self) -> dict[str, Any]:
        tiers = {
            r["tier"]: r["n"]
            for r in self.db.query("SELECT tier, COUNT(*) n FROM lessons GROUP BY tier")
        }
        return {
            "total": int(self.db.scalar("SELECT COUNT(*) FROM lessons") or 0),
            "by_tier": tiers,
            "sources": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM sources WHERE verification = 'verified'"
                )
                or 0
            ),
            "content_studied": int(
                self.db.scalar("SELECT COUNT(*) FROM content_items WHERE processed = 1")
                or 0
            ),
            "graded_predictions": int(
                self.db.scalar("SELECT COUNT(*) FROM outcomes") or 0
            ),
        }

    def expertise_level(self) -> tuple[str, float]:
        """A single readable answer to "how good is Jarvis now?".

        Driven by how much has been *validated*, not how much has been read or
        graded. Grading volume only earns a small credibility floor -- running a
        lot of tests is not the same as having found something that works, so it
        is capped well below what proven lessons contribute.
        """
        graded = int(self.db.scalar("SELECT COUNT(*) FROM outcomes") or 0)
        expert = int(
            self.db.scalar("SELECT COUNT(*) FROM lessons WHERE tier = 'expertise'") or 0
        )
        working = int(
            self.db.scalar("SELECT COUNT(*) FROM lessons WHERE tier = 'working'") or 0
        )
        validated = (expert * 3 + working) / 45          # up to ~0.80
        evidence_floor = min(graded, 1000) / 5000        # up to 0.20
        score = min(1.0, validated + evidence_floor)
        if score < 0.15:
            label = "novice"
        elif score < 0.35:
            label = "developing"
        elif score < 0.6:
            label = "competent"
        elif score < 0.82:
            label = "proficient"
        else:
            label = "expert"
        return label, round(score, 3)


# --------------------------------------------------------------------- signals
class SignalStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        symbol: str,
        pattern_key: str,
        direction: str,
        price: float,
        *,
        timeframe: str = "1d",
        horizon_bars: int = 5,
        features: dict | None = None,
        lesson_id: int | None = None,
        confidence: float = 0.5,
        detected_at: str | None = None,
    ) -> int | None:
        detected_at = detected_at or iso(utcnow())
        try:
            return self.db.execute(
                """
                INSERT INTO signals(symbol, pattern_key, timeframe, direction,
                                    detected_at, price_at_signal, horizon_bars,
                                    features, lesson_id, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    pattern_key,
                    timeframe,
                    direction,
                    detected_at,
                    price,
                    horizon_bars,
                    json.dumps(features or {}),
                    lesson_id,
                    confidence,
                ),
            )
        except Exception:
            # UNIQUE clash: the same pattern on the same bar. Not a new signal.
            return None

    def open_signals(self, older_than_days: float = 0.0) -> list[dict[str, Any]]:
        cutoff = iso(utcnow() - timedelta(days=older_than_days))
        rows = self.db.query(
            "SELECT * FROM signals WHERE status = 'open' AND detected_at <= ? "
            "ORDER BY detected_at",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    def grade(self, signal_id: int, exit_price: float, return_pct: float, hit: bool) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO outcomes(signal_id, exit_price, return_pct, hit) "
                "VALUES (?, ?, ?, ?)",
                (signal_id, exit_price, return_pct, 1 if hit else 0),
            )
            conn.execute(
                "UPDATE signals SET status = 'graded' WHERE id = ?", (signal_id,)
            )

    def recent(self, limit: int = 10, min_confidence: float = 0.0) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM signals WHERE confidence >= ? ORDER BY detected_at DESC, id DESC "
            "LIMIT ?",
            (min_confidence, limit),
        )
        return [dict(r) for r in rows]

    def hit_rate(self, pattern_key: str | None = None) -> tuple[int, float]:
        sql = (
            "SELECT COUNT(*) n, COALESCE(AVG(o.hit), 0) rate FROM outcomes o "
            "JOIN signals s ON s.id = o.signal_id"
        )
        params: tuple = ()
        if pattern_key:
            sql += " WHERE s.pattern_key = ?"
            params = (pattern_key,)
        row = self.db.query_one(sql, params)
        return (int(row["n"]), float(row["rate"])) if row else (0, 0.0)


# ------------------------------------------------------------------------ news
class NewsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(
        self,
        source: str,
        headline: str,
        *,
        url: str | None = None,
        published_at: str | None = None,
        summary: str | None = None,
        tickers: Sequence[str] = (),
        impact: float = 0.0,
    ) -> int | None:
        exists = self.db.scalar(
            "SELECT id FROM news_items WHERE source = ? AND headline = ?",
            (source, headline),
        )
        if exists is not None:
            return None
        return self.db.execute(
            """
            INSERT INTO news_items(source, headline, url, published_at, summary,
                                   tickers, impact)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source, headline, url, published_at, summary, ",".join(tickers), impact),
        )

    def recent(self, limit: int = 10, min_impact: float = 0.0) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM news_items WHERE impact >= ? ORDER BY seen_at DESC LIMIT ?",
            (min_impact, limit),
        )
        return [dict(r) for r in rows]

    def since(self, when: datetime, min_impact: float = 0.0) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM news_items WHERE seen_at >= ? AND impact >= ? "
            "ORDER BY impact DESC, seen_at DESC",
            (iso(when), min_impact),
        )
        return [dict(r) for r in rows]


# -------------------------------------------------------------------- training
class TrainingStore:
    """Caleb's progress through the day-trading curriculum."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record_attempt(
        self, module_key: str, score: int, out_of: int, passed: bool
    ) -> int:
        return self.db.execute(
            "INSERT INTO training_attempts(module_key, score, out_of, passed) "
            "VALUES (?, ?, ?, ?)",
            (module_key, score, out_of, 1 if passed else 0),
        )

    def passed_modules(self) -> set[str]:
        """Modules passed at least once. A pass is not lost by a later bad attempt."""
        rows = self.db.query(
            "SELECT DISTINCT module_key FROM training_attempts WHERE passed = 1"
        )
        return {r["module_key"] for r in rows}

    def attempts(self, module_key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM training_attempts"
        params: tuple = ()
        if module_key:
            sql += " WHERE module_key = ?"
            params = (module_key,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self.db.query(sql, params + (limit,))
        return [dict(r) for r in rows]

    def best_score(self, module_key: str) -> tuple[int, int] | None:
        row = self.db.query_one(
            "SELECT MAX(score) AS best, out_of FROM training_attempts "
            "WHERE module_key = ?",
            (module_key,),
        )
        if row is None or row["best"] is None:
            return None
        return int(row["best"]), int(row["out_of"])


# ------------------------------------------------------------------- container
class Memory:
    """Everything Jarvis remembers, in one handle."""

    def __init__(self, db_path) -> None:
        self.db = Database(db_path)
        self.profile = ProfileStore(self.db)
        self.sessions = SessionStore(self.db)
        self.knowledge = KnowledgeStore(self.db)
        self.signals = SignalStore(self.db)
        self.news = NewsStore(self.db)
        self.training = TrainingStore(self.db)

    def close(self) -> None:
        self.db.close()
