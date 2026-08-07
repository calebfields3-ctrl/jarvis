"""TradingView scanner client.

TradingView has no public streaming API, so watching 500+ charts "at once" is
done the way TradingView's own screener does it: batched POSTs to the scanner
endpoint, each returning a full technical snapshot for up to a few hundred
symbols. Batches run concurrently, so a 500-symbol sweep is a handful of
round-trips rather than 500.

If the endpoint is unreachable the scanner returns nothing and the pipeline
falls back to Yahoo OHLCV + the local pattern engine, which needs no vendor.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

SCANNER_URL = "https://scanner.tradingview.com/{screener}/scan"

# The columns Jarvis reads off every chart it watches.
COLUMNS = [
    "close", "change", "change_abs", "volume", "average_volume_10d_calc",
    "relative_volume_10d_calc", "RSI", "MACD.macd", "MACD.signal",
    "SMA20", "SMA50", "SMA200", "EMA20",
    "BB.upper", "BB.lower", "ATR", "ADX",
    "high", "low", "open", "market_cap_basic",
    "Recommend.All", "Recommend.MA", "Recommend.Other",
    "price_52_week_high", "price_52_week_low",
]

INTERVAL_SUFFIX = {
    "1m": "|1", "5m": "|5", "15m": "|15", "30m": "|30",
    "1h": "|60", "2h": "|120", "4h": "|240",
    "1d": "", "1W": "|1W", "1M": "|1M",
}


@dataclass
class ChartSnapshot:
    """One chart's live state, as TradingView sees it."""

    symbol: str
    exchange: str
    interval: str
    values: dict[str, Any]

    def get(self, column: str, default: Any = None) -> Any:
        value = self.values.get(column, default)
        return default if value is None else value

    @property
    def close(self) -> float | None:
        v = self.values.get("close")
        return float(v) if isinstance(v, (int, float)) else None

    @property
    def change_pct(self) -> float | None:
        v = self.values.get("change")
        return float(v) if isinstance(v, (int, float)) else None

    @property
    def recommendation(self) -> str:
        """TradingView's own summary, bucketed the way its widget does."""
        r = self.values.get("Recommend.All")
        if not isinstance(r, (int, float)):
            return "unknown"
        if r >= 0.5:
            return "strong_buy"
        if r >= 0.1:
            return "buy"
        if r > -0.1:
            return "neutral"
        if r > -0.5:
            return "sell"
        return "strong_sell"

    @property
    def relative_volume(self) -> float | None:
        v = self.values.get("relative_volume_10d_calc")
        return float(v) if isinstance(v, (int, float)) else None


class TradingViewScanner:
    """Batched, concurrent screener over an arbitrarily large watchlist."""

    def __init__(
        self,
        *,
        screener: str = "america",
        exchange: str = "NASDAQ",
        batch_size: int = 100,
        max_workers: int = 8,
        timeout: int = 20,
    ) -> None:
        self.screener = screener
        self.exchange = exchange
        self.batch_size = max(1, batch_size)
        self.max_workers = max(1, max_workers)
        self.timeout = timeout
        self.last_error: str | None = None

    # ------------------------------------------------------------ internals
    @staticmethod
    def _qualify(symbol: str, exchange: str) -> str:
        return symbol if ":" in symbol else f"{exchange}:{symbol.upper()}"

    def _post(self, payload: dict) -> dict | None:
        request = urllib.request.Request(
            SCANNER_URL.format(screener=self.screener),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)
            log.debug("tradingview scan batch failed: %s", exc)
            return None
        except json.JSONDecodeError as exc:
            self.last_error = f"bad JSON from scanner: {exc}"
            return None

    def _scan_batch(self, symbols: Sequence[str], interval: str) -> list[ChartSnapshot]:
        suffix = INTERVAL_SUFFIX.get(interval, "")
        payload = {
            "symbols": {
                "tickers": [self._qualify(s, self.exchange) for s in symbols],
                "query": {"types": []},
            },
            "columns": [c + suffix for c in COLUMNS],
        }
        body = self._post(payload)
        if not body or "data" not in body:
            return []
        out: list[ChartSnapshot] = []
        for entry in body.get("data") or []:
            ticker = entry.get("s", "")
            exchange, _, symbol = ticker.partition(":")
            values = dict(zip(COLUMNS, entry.get("d") or []))
            out.append(ChartSnapshot(symbol or ticker, exchange, interval, values))
        return out

    # --------------------------------------------------------------- public
    def scan(self, symbols: Sequence[str], interval: str = "1d") -> dict[str, ChartSnapshot]:
        """Snapshot every symbol given. Runs batches concurrently."""
        symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        if not symbols:
            return {}
        batches = [
            symbols[i : i + self.batch_size]
            for i in range(0, len(symbols), self.batch_size)
        ]
        results: dict[str, ChartSnapshot] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(batches))) as pool:
            futures = {pool.submit(self._scan_batch, b, interval): b for b in batches}
            for future in as_completed(futures):
                try:
                    for snap in future.result():
                        results[snap.symbol] = snap
                except Exception as exc:  # one bad batch must not sink the sweep
                    log.debug("scan batch raised: %s", exc)
        return results

    def movers(
        self, snapshots: dict[str, ChartSnapshot], *, top: int = 10
    ) -> tuple[list[ChartSnapshot], list[ChartSnapshot]]:
        ranked = sorted(
            (s for s in snapshots.values() if s.change_pct is not None),
            key=lambda s: s.change_pct,
            reverse=True,
        )
        return ranked[:top], list(reversed(ranked[-top:]))

    def unusual_volume(
        self, snapshots: dict[str, ChartSnapshot], *, threshold: float = 2.0
    ) -> list[ChartSnapshot]:
        hits = [
            s for s in snapshots.values()
            if (s.relative_volume or 0) >= threshold
        ]
        return sorted(hits, key=lambda s: s.relative_volume or 0, reverse=True)


def load_universe(path) -> list[str]:
    """Read the watchlist file: one ticker per line, ``#`` comments allowed."""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []
    symbols: list[str] = []
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line and line not in seen:
            seen.add(line)
            symbols.append(line)
    return symbols
