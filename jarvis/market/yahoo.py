"""Yahoo Finance: prices, OHLCV history and the fundamentals Jarvis studies.

``yfinance`` is optional. Without it (or without a network) every call returns
empty and callers degrade to whatever they have cached -- Jarvis stays usable
offline, it just says less.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .patterns import Bar

log = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    import yfinance as _yf
except Exception:  # pragma: no cover
    _yf = None


def yfinance_available() -> bool:
    return _yf is not None


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class YahooClient:
    """Cached, failure-tolerant Yahoo Finance access."""

    def __init__(self, *, price_ttl: int = 60, bars_ttl: int = 900, fund_ttl: int = 21600):
        self.price_ttl = price_ttl
        self.bars_ttl = bars_ttl
        self.fund_ttl = fund_ttl
        self._cache: dict[str, _CacheEntry] = {}

    # ------------------------------------------------------------- caching
    def _get(self, key: str):
        entry = self._cache.get(key)
        if entry and entry.expires_at > time.time():
            return entry.value
        self._cache.pop(key, None)
        return None

    def _put(self, key: str, value: Any, ttl: int) -> None:
        self._cache[key] = _CacheEntry(value, time.time() + ttl)

    # --------------------------------------------------------------- bars
    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> list[Bar]:
        key = f"bars:{symbol}:{period}:{interval}"
        cached = self._get(key)
        if cached is not None:
            return cached
        if _yf is None:
            return []
        try:
            frame = _yf.Ticker(symbol).history(
                period=period, interval=interval, auto_adjust=False
            )
        except Exception as exc:
            log.debug("yahoo history failed for %s: %s", symbol, exc)
            return []
        bars: list[Bar] = []
        for ts, row in frame.iterrows():
            try:
                bars.append(
                    Bar(
                        ts=str(ts.date() if hasattr(ts, "date") else ts),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=float(row.get("Volume", 0) or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        bars = [b for b in bars if b.close == b.close and b.close > 0]  # drop NaN
        self._put(key, bars, self.bars_ttl)
        return bars

    def bulk_history(
        self, symbols: Sequence[str], period: str = "6mo", interval: str = "1d"
    ) -> dict[str, list[Bar]]:
        """One batched download for many symbols -- far kinder to Yahoo than N calls."""
        if _yf is None or not symbols:
            return {}
        out: dict[str, list[Bar]] = {}
        pending = []
        for sym in symbols:
            cached = self._get(f"bars:{sym}:{period}:{interval}")
            if cached is not None:
                out[sym] = cached
            else:
                pending.append(sym)
        if not pending:
            return out
        try:
            data = _yf.download(
                tickers=" ".join(pending),
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
        except Exception as exc:
            log.debug("yahoo bulk download failed: %s", exc)
            return out
        for sym in pending:
            try:
                frame = data[sym] if len(pending) > 1 else data
            except Exception:
                continue
            bars: list[Bar] = []
            for ts, row in frame.iterrows():
                try:
                    close = float(row["Close"])
                except (KeyError, TypeError, ValueError):
                    continue
                if close != close or close <= 0:
                    continue
                bars.append(
                    Bar(
                        ts=str(ts.date() if hasattr(ts, "date") else ts),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=close,
                        volume=float(row.get("Volume", 0) or 0),
                    )
                )
            if bars:
                out[sym] = bars
                self._put(f"bars:{sym}:{period}:{interval}", bars, self.bars_ttl)
        return out

    # -------------------------------------------------------------- prices
    def prices(self, symbols: Sequence[str]) -> dict[str, float]:
        symbols = [s.upper() for s in symbols]
        out: dict[str, float] = {}
        missing: list[str] = []
        for sym in symbols:
            cached = self._get(f"px:{sym}")
            if cached is not None:
                out[sym] = cached
            else:
                missing.append(sym)
        if not missing or _yf is None:
            return out
        try:
            data = _yf.download(
                tickers=" ".join(missing), period="5d", interval="1d",
                group_by="ticker", auto_adjust=False, threads=True, progress=False,
            )
        except Exception as exc:
            log.debug("yahoo price fetch failed: %s", exc)
            return out
        for sym in missing:
            try:
                frame = data[sym] if len(missing) > 1 else data
                price = float(frame["Close"].dropna().iloc[-1])
            except Exception:
                continue
            if price > 0:
                out[sym] = price
                self._put(f"px:{sym}", price, self.price_ttl)
        return out

    def price(self, symbol: str) -> float | None:
        return self.prices([symbol]).get(symbol.upper())

    # -------------------------------------------------------- fundamentals
    def fundamentals(self, symbol: str) -> dict[str, Any]:
        """The Yahoo fundamentals Jarvis actually reasons about."""
        key = f"fund:{symbol}"
        cached = self._get(key)
        if cached is not None:
            return cached
        if _yf is None:
            return {}
        try:
            info = _yf.Ticker(symbol).info or {}
        except Exception as exc:
            log.debug("yahoo fundamentals failed for %s: %s", symbol, exc)
            return {}
        wanted = {
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": info.get("marketCap"),
            "pe_trailing": info.get("trailingPE"),
            "pe_forward": info.get("forwardPE"),
            "peg": info.get("pegRatio"),
            "price_to_book": info.get("priceToBook"),
            "profit_margin": info.get("profitMargins"),
            "operating_margin": info.get("operatingMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "debt_to_equity": info.get("debtToEquity"),
            "free_cashflow": info.get("freeCashflow"),
            "dividend_yield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "short_percent_float": info.get("shortPercentOfFloat"),
            "recommendation": info.get("recommendationKey"),
            "target_mean": info.get("targetMeanPrice"),
            "fifty_two_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_low": info.get("fiftyTwoWeekLow"),
            "avg_volume": info.get("averageVolume"),
        }
        cleaned = {k: v for k, v in wanted.items() if v is not None}
        self._put(key, cleaned, self.fund_ttl)
        return cleaned

    def fundamental_notes(self, symbol: str) -> list[str]:
        """Turn raw fundamentals into the sentences a trader would actually say."""
        f = self.fundamentals(symbol)
        if not f:
            return []
        notes: list[str] = []
        pe = f.get("pe_trailing")
        if isinstance(pe, (int, float)):
            if pe > 60:
                notes.append(f"trades at {pe:.0f}x trailing earnings -- priced for growth")
            elif pe < 12:
                notes.append(f"trades at {pe:.0f}x trailing earnings -- value territory")
        margin = f.get("profit_margin")
        if isinstance(margin, (int, float)):
            if margin > 0.20:
                notes.append(f"net margin {margin * 100:.0f}% is strong")
            elif margin < 0:
                notes.append("currently unprofitable on a net basis")
        growth = f.get("revenue_growth")
        if isinstance(growth, (int, float)):
            if growth > 0.20:
                notes.append(f"revenue growing {growth * 100:.0f}% year over year")
            elif growth < 0:
                notes.append(f"revenue shrinking {abs(growth) * 100:.0f}% year over year")
        de = f.get("debt_to_equity")
        if isinstance(de, (int, float)) and de > 200:
            notes.append(f"debt/equity of {de:.0f} is heavy -- rate sensitive")
        short = f.get("short_percent_float")
        if isinstance(short, (int, float)) and short > 0.15:
            notes.append(f"{short * 100:.0f}% of float short -- squeeze risk both ways")
        hi, lo = f.get("fifty_two_high"), f.get("fifty_two_low")
        if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and lo > 0:
            notes.append(f"52-week range {lo:.2f} to {hi:.2f}")
        return notes
