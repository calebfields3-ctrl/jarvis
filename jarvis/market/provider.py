"""Market data providers.

Two implementations behind one interface:

``YahooProvider``     live OHLCV, prices and fundamentals from Yahoo Finance.
``SyntheticProvider`` deterministic, seeded price series for offline work.

The synthetic one is not a toy stub. It generates regime-switching random walks
(trend / chop / shock) with volume that reacts to price, which is enough for the
pattern detectors to fire realistically and for the learning loop to be
exercised and unit-tested without touching the network.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta
from typing import Mapping, Protocol, Sequence

from .patterns import Bar
from .yahoo import YahooClient, yfinance_available


class MarketDataProvider(Protocol):
    def bars(self, symbol: str, period: str = "1y", interval: str = "1d") -> list[Bar]: ...

    def bulk_bars(
        self, symbols: Sequence[str], period: str = "6mo", interval: str = "1d"
    ) -> dict[str, list[Bar]]: ...

    def prices(self, symbols: Sequence[str]) -> Mapping[str, float]: ...

    def fundamentals(self, symbol: str) -> dict: ...


class YahooProvider:
    """Live provider. Returns empty structures when Yahoo is unreachable."""

    name = "yahoo"

    def __init__(self, client: YahooClient | None = None) -> None:
        self.client = client or YahooClient()

    def available(self) -> bool:
        return yfinance_available()

    def bars(self, symbol: str, period: str = "1y", interval: str = "1d") -> list[Bar]:
        return self.client.history(symbol, period, interval)

    def bulk_bars(
        self, symbols: Sequence[str], period: str = "6mo", interval: str = "1d"
    ) -> dict[str, list[Bar]]:
        return self.client.bulk_history(symbols, period, interval)

    def prices(self, symbols: Sequence[str]) -> Mapping[str, float]:
        return self.client.prices(symbols)

    def fundamentals(self, symbol: str) -> dict:
        return self.client.fundamentals(symbol)

    def fundamental_notes(self, symbol: str) -> list[str]:
        return self.client.fundamental_notes(symbol)


class SyntheticProvider:
    """Deterministic offline market. Same symbol -> same history, always."""

    name = "synthetic"

    def __init__(self, *, seed: int = 7, end: date | None = None) -> None:
        self.seed = seed
        self.end = end or date.today()
        self._cache: dict[tuple[str, int], list[Bar]] = {}

    # -- deterministic per-symbol RNG -----------------------------------
    def _rng(self, symbol: str) -> random.Random:
        digest = hashlib.sha256(f"{symbol}:{self.seed}".encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    @staticmethod
    def _periods_to_days(period: str) -> int:
        table = {"1mo": 30, "3mo": 90, "6mo": 182, "1y": 365, "2y": 730, "5y": 1825}
        return table.get(period, 365)

    # -- intraday ---------------------------------------------------------
    _INTRADAY_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
                         "60m": 60, "1h": 60}

    def _intraday_bars(self, symbol: str, period: str, interval: str) -> list[Bar]:
        """Minute bars with a realistic intraday volume smile.

        Volume is U-shaped across the session (heavy at the open and into the
        close, thin at midday) and volatility follows it, because that shape is
        what the intraday detectors key off. A flat random walk would make the
        opening-range and VWAP logic look like it works when it doesn't.
        """
        from datetime import datetime, time as _time, timedelta as _td

        from .session import EASTERN, is_trading_day

        step = self._INTRADAY_MINUTES.get(interval, 5)
        sessions = max(2, min(self._periods_to_days(period), 30) * 5 // 7)
        rng = self._rng(f"{symbol}:{interval}")
        price = rng.uniform(12, 400)
        base_volume = rng.uniform(2e4, 8e5) * step

        # Walk back to the first session, skipping non-trading days.
        day = self.end
        trading_days: list = []
        while len(trading_days) < sessions:
            if is_trading_day(day):
                trading_days.append(day)
            day -= _td(days=1)
        trading_days.reverse()

        bars: list[Bar] = []
        for session_day in trading_days:
            # Overnight gap, then the session proper.
            price *= 1 + rng.gauss(0, 0.008)
            drift = rng.gauss(0, 0.0004)
            clock = datetime.combine(session_day, _time(9, 30), tzinfo=EASTERN)
            close_at = datetime.combine(session_day, _time(16, 0), tzinfo=EASTERN)
            total_minutes = (close_at - clock).total_seconds() / 60

            while clock < close_at:
                elapsed = (clock - datetime.combine(
                    session_day, _time(9, 30), tzinfo=EASTERN
                )).total_seconds() / 60
                # U-shape: 1.0 at the extremes, ~0.25 at midday.
                position = elapsed / total_minutes
                smile = 0.25 + 0.75 * (2 * position - 1) ** 2
                sigma = 0.0011 * (0.5 + smile)

                ret = rng.gauss(drift, sigma)
                open_price = price
                close = max(0.5, price * (1 + ret))
                wick = abs(rng.gauss(0, sigma * 0.6))
                high = max(open_price, close) * (1 + wick)
                low = max(0.4, min(open_price, close) * (1 - abs(rng.gauss(0, sigma * 0.6))))
                volume = base_volume * smile * rng.uniform(0.5, 1.6)

                bars.append(Bar(
                    ts=clock.strftime("%Y-%m-%d %H:%M:%S"),
                    open=round(open_price, 4), high=round(high, 4),
                    low=round(low, 4), close=round(close, 4), volume=round(volume),
                ))
                price = close
                clock += _td(minutes=step)
        return bars

    def bars(self, symbol: str, period: str = "1y", interval: str = "1d") -> list[Bar]:
        symbol = symbol.upper()
        if interval in self._INTRADAY_MINUTES:
            key = (f"{symbol}:{interval}", 0)
            if key not in self._cache:
                self._cache[key] = self._intraday_bars(symbol, period, interval)
            return self._cache[key]
        n_days = self._periods_to_days(period)
        n_bars = max(60, int(n_days * 5 / 7))  # trading days only
        key = (symbol, n_bars)
        if key in self._cache:
            return self._cache[key]

        rng = self._rng(symbol)
        price = rng.uniform(12, 400)
        base_vol = rng.uniform(4e5, 6e7)
        # Regime machine: 0 trend-up, 1 trend-down, 2 chop
        regime = rng.choice([0, 1, 2])
        regime_left = rng.randint(15, 60)
        drift_by_regime = {0: 0.0011, 1: -0.0010, 2: 0.0}
        vol_by_regime = {0: 0.014, 1: 0.019, 2: 0.011}

        bars: list[Bar] = []
        day = self.end - timedelta(days=int(n_bars * 7 / 5) + 5)
        for _ in range(n_bars):
            day += timedelta(days=1)
            while day.weekday() >= 5:  # skip weekends
                day += timedelta(days=1)

            regime_left -= 1
            if regime_left <= 0:
                regime = rng.choices([0, 1, 2], weights=[0.4, 0.25, 0.35])[0]
                regime_left = rng.randint(15, 60)

            drift = drift_by_regime[regime]
            sigma = vol_by_regime[regime]
            shock = 0.0
            if rng.random() < 0.012:  # earnings-style gap
                shock = rng.gauss(0, 0.06)

            ret = rng.gauss(drift, sigma) + shock
            open_price = price
            close = max(0.5, price * (1 + ret))
            wick = abs(rng.gauss(0, sigma * 0.7))
            high = max(open_price, close) * (1 + wick)
            low = min(open_price, close) * (1 - abs(rng.gauss(0, sigma * 0.7)))
            low = max(0.4, min(low, min(open_price, close)))

            vol_kick = 1 + min(3.0, abs(ret) / max(sigma, 1e-6) * 0.6)
            volume = base_vol * vol_kick * rng.uniform(0.6, 1.4)

            bars.append(
                Bar(
                    ts=day.isoformat(),
                    open=round(open_price, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close, 4),
                    volume=round(volume),
                )
            )
            price = close

        self._cache[key] = bars
        return bars

    def bulk_bars(
        self, symbols: Sequence[str], period: str = "6mo", interval: str = "1d"
    ) -> dict[str, list[Bar]]:
        return {s.upper(): self.bars(s, period, interval) for s in symbols}

    def prices(self, symbols: Sequence[str]) -> Mapping[str, float]:
        return {s.upper(): self.bars(s, "3mo")[-1].close for s in symbols}

    def fundamentals(self, symbol: str) -> dict:
        rng = self._rng(symbol + ":fund")
        sectors = [
            "Technology", "Healthcare", "Financial Services", "Energy",
            "Consumer Cyclical", "Industrials", "Utilities",
        ]
        return {
            "name": f"{symbol.upper()} Inc.",
            "sector": rng.choice(sectors),
            "market_cap": int(rng.uniform(5e8, 2.5e12)),
            "pe_trailing": round(rng.uniform(6, 85), 2),
            "profit_margin": round(rng.uniform(-0.12, 0.34), 4),
            "revenue_growth": round(rng.uniform(-0.25, 0.55), 4),
            "debt_to_equity": round(rng.uniform(5, 260), 1),
            "beta": round(rng.uniform(0.4, 2.3), 2),
            "synthetic": True,
        }

    def fundamental_notes(self, symbol: str) -> list[str]:
        return ["(synthetic data -- offline mode, not real fundamentals)"]


def build_provider(prefer_live: bool = True) -> MarketDataProvider:
    """Pick the live provider when it can actually reach Yahoo, else go offline.

    The probe is one cheap request; a blocked network or a missing yfinance
    both land on the synthetic provider so nothing downstream has to care.
    """
    if prefer_live and yfinance_available():
        provider = YahooProvider()
        try:
            if provider.prices(["SPY"]):
                return provider
        except Exception:
            pass
    return SyntheticProvider()
