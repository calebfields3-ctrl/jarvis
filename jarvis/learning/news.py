"""Continuous monitoring of global news for events that move markets.

Pulls RSS/Atom from major international newsrooms and wire services, scores
each headline for market impact, extracts the tickers and sectors it touches,
and files anything material into memory. Feeds are read directly (they are
publisher-provided machine endpoints), but every feed host still has to sit on
the safety allowlist.

``feedparser`` is used when installed; otherwise a small built-in RSS/Atom
parser handles the standard shapes, so news monitoring works with no
third-party packages at all.
"""

from __future__ import annotations

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..safety.url_safety import URLSafetyChecker
from .sources import _load_yaml

log = logging.getLogger(__name__)

# Event language weighted by how hard it historically moves prices.
IMPACT_TERMS: dict[str, float] = {
    # monetary policy / macro -- moves everything at once
    "rate hike": 0.9, "rate cut": 0.9, "interest rate": 0.75, "federal reserve": 0.8,
    "fed chair": 0.75, "fomc": 0.85, "inflation": 0.7, "cpi": 0.75, "ppi": 0.6,
    "jobs report": 0.7, "nonfarm payroll": 0.75, "unemployment": 0.6, "gdp": 0.6,
    "recession": 0.8, "central bank": 0.7, "yield curve": 0.6, "treasury yield": 0.65,
    "stimulus": 0.6, "default": 0.8, "debt ceiling": 0.75,
    # company events
    "earnings": 0.6, "guidance": 0.6, "profit warning": 0.8, "beats estimates": 0.6,
    "misses estimates": 0.65, "bankruptcy": 0.85, "merger": 0.7, "acquisition": 0.7,
    "buyout": 0.65, "ipo": 0.5, "layoffs": 0.55, "recall": 0.55, "ceo": 0.45,
    "dividend": 0.4, "buyback": 0.45, "delisting": 0.7, "short seller": 0.6,
    # regulation / legal
    "sec charges": 0.75, "investigation": 0.6, "lawsuit": 0.5, "antitrust": 0.65,
    "sanctions": 0.7, "tariff": 0.75, "trade war": 0.75, "regulation": 0.5,
    # geopolitics / shocks
    "war": 0.8, "invasion": 0.85, "attack": 0.7, "opec": 0.7, "oil supply": 0.7,
    "pandemic": 0.8, "outbreak": 0.6, "earthquake": 0.5, "strike": 0.5,
    "cyberattack": 0.6, "outage": 0.5, "coup": 0.7, "election": 0.55,
}

URGENCY_MARKERS = ("breaking", "urgent", "just in", "alert", "developing")

SECTOR_HINTS: dict[str, tuple[str, ...]] = {
    "technology": ("chip", "semiconductor", "software", "ai ", "cloud", "smartphone"),
    "energy": ("oil", "crude", "opec", "gas", "refinery", "barrel"),
    "financials": ("bank", "lender", "credit", "mortgage", "insurer"),
    "healthcare": ("fda", "drug", "trial", "vaccine", "biotech", "pharma"),
    "consumer": ("retail", "consumer", "spending", "restaurant", "apparel"),
    "industrials": ("airline", "aerospace", "manufactur", "freight", "shipping"),
}

# Uppercase words that look like tickers but are not.
_TICKER_STOPWORDS = frozenset(
    {
        "CEO", "CFO", "COO", "CTO", "USA", "US", "UK", "EU", "UN", "GDP", "CPI",
        "PPI", "FED", "ECB", "IMF", "OPEC", "SEC", "FDA", "FBI", "IPO", "ETF",
        "AI", "EV", "API", "NEWS", "LIVE", "WATCH", "AND", "THE", "FOR", "WITH",
        "NYSE", "NASDAQ", "Q1", "Q2", "Q3", "Q4", "M&A", "IT", "OK", "TV",
    }
)

_TICKER_RX = re.compile(r"\b\(?([A-Z]{1,5})\)?(?=[\s,.:)]|$)")
_CASHTAG_RX = re.compile(r"\$([A-Z]{1,5})\b")


@dataclass
class NewsItem:
    source: str
    headline: str
    url: str | None
    published_at: str | None
    summary: str
    impact: float
    tickers: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)

    @property
    def is_material(self) -> bool:
        return self.impact >= 0.45


class NewsMonitor:
    """Reads global newsrooms on a loop and files what matters."""

    def __init__(
        self,
        feeds: Sequence[dict],
        safety: URLSafetyChecker,
        *,
        timeout: int = 20,
        universe: Iterable[str] | None = None,
    ) -> None:
        self.feeds = list(feeds)
        self.safety = safety
        self.timeout = timeout
        self.universe = {s.upper() for s in (universe or [])}
        self.last_errors: list[str] = []

    # --------------------------------------------------------------- feeds
    @classmethod
    def from_file(
        cls,
        path: Path | str,
        safety: URLSafetyChecker,
        *,
        universe: Iterable[str] | None = None,
        **kwargs,
    ) -> "NewsMonitor":
        raw = _load_yaml(Path(path)) or {}
        feeds: list[dict] = []
        for region, entries in raw.items():
            for entry in entries or []:
                if isinstance(entry, dict) and entry.get("url"):
                    feeds.append({**entry, "region": entry.get("region", region)})
        return cls(feeds, safety, universe=universe, **kwargs)

    def _fetch_feed(self, url: str) -> list[dict]:
        verdict = self.safety.check(url)
        if not verdict.is_safe:
            self.last_errors.append(f"feed refused by safety gate: {url} ({verdict})")
            return []
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except Exception as exc:
            self.last_errors.append(f"{url}: {exc}")
            log.debug("feed fetch failed %s: %s", url, exc)
            return []
        return parse_feed(raw)

    # -------------------------------------------------------------- public
    def poll(self) -> list[NewsItem]:
        """One sweep across every configured newsroom."""
        self.last_errors = []
        items: list[NewsItem] = []
        for feed in self.feeds:
            name = feed.get("name") or feed.get("url", "unknown")
            for entry in self._fetch_feed(feed["url"]):
                items.append(self.assess(name, entry))
        items.sort(key=lambda i: i.impact, reverse=True)
        return items

    def assess(self, source: str, entry: dict) -> NewsItem:
        headline = (entry.get("title") or "").strip()
        summary = (entry.get("summary") or "").strip()
        text = f"{headline}. {summary}".lower()

        impact = 0.0
        for term, weight in IMPACT_TERMS.items():
            if term in text:
                impact = max(impact, weight)
        if any(marker in text for marker in URGENCY_MARKERS):
            impact = min(1.0, impact + 0.12)

        tickers = self.extract_tickers(f"{headline} {summary}")
        if tickers:
            impact = min(1.0, impact + 0.08)

        sectors = [
            sector for sector, hints in SECTOR_HINTS.items()
            if any(h in text for h in hints)
        ]

        return NewsItem(
            source=source,
            headline=headline,
            url=entry.get("link"),
            published_at=entry.get("published"),
            summary=summary[:600],
            impact=round(impact, 3),
            tickers=tickers,
            sectors=sectors,
        )

    def extract_tickers(self, text: str) -> list[str]:
        """Cashtags always count; bare uppercase only if it's on the watchlist."""
        found: list[str] = []
        for symbol in _CASHTAG_RX.findall(text):
            if symbol not in found:
                found.append(symbol)
        if self.universe:
            for symbol in _TICKER_RX.findall(text):
                if (
                    symbol in self.universe
                    and symbol not in _TICKER_STOPWORDS
                    and symbol not in found
                ):
                    found.append(symbol)
        return found[:8]

    def monitor(self, news_store, *, min_impact: float = 0.35) -> dict[str, int]:
        """Poll every newsroom and persist the material headlines."""
        stats = {"seen": 0, "stored": 0, "material": 0, "feed_errors": 0}
        for item in self.poll():
            stats["seen"] += 1
            if item.impact < min_impact or not item.headline:
                continue
            stats["material"] += 1
            stored = news_store.add(
                source=item.source,
                headline=item.headline,
                url=item.url,
                published_at=item.published_at,
                summary=item.summary,
                tickers=item.tickers,
                impact=item.impact,
            )
            if stored:
                stats["stored"] += 1
        stats["feed_errors"] = len(self.last_errors)
        return stats


# ------------------------------------------------------------------ parsing
def parse_feed(raw: bytes) -> list[dict]:
    """Parse RSS or Atom. Uses feedparser when present, else stdlib XML."""
    try:
        import feedparser  # type: ignore

        parsed = feedparser.parse(raw)
        return [
            {
                "title": e.get("title", ""),
                "link": e.get("link"),
                "summary": e.get("summary", "") or e.get("description", ""),
                "published": e.get("published") or e.get("updated"),
            }
            for e in parsed.entries
        ]
    except ImportError:
        pass
    except Exception as exc:
        log.debug("feedparser failed, falling back: %s", exc)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log.debug("feed XML parse failed: %s", exc)
        return []

    atom = "{http://www.w3.org/2005/Atom}"
    entries: list[dict] = []

    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in {"item", "entry"}:
            continue
        get = lambda name: _first_text(item, name, atom)  # noqa: E731
        link = get("link")
        if not link:
            link_el = item.find(f"{atom}link")
            if link_el is not None:
                link = link_el.attrib.get("href")
        entries.append(
            {
                "title": get("title") or "",
                "link": link,
                "summary": get("summary") or get("description") or get("content") or "",
                "published": get("pubDate") or get("published") or get("updated"),
            }
        )
    return entries


def _first_text(element: ET.Element, name: str, atom_ns: str) -> str | None:
    for candidate in (name, f"{atom_ns}{name}"):
        found = element.find(candidate)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return None
