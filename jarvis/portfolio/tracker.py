"""Portfolio ledger, valuation and the previous-day P/L that opens every greeting.

Positions are derived from the trade ledger rather than stored, so the numbers
can never drift out of sync with history. Cost basis is average-cost; realised
P/L is booked at sell time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from ..memory.db import Database


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_cost: float
    realized_pnl: float = 0.0
    last_price: float | None = None

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def market_value(self) -> float:
        price = self.last_price if self.last_price is not None else self.avg_cost
        return self.quantity * price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealized_pct(self) -> float:
        return 0.0 if not self.cost_basis else self.unrealized_pnl / self.cost_basis * 100


@dataclass
class PortfolioSnapshot:
    as_of: datetime
    cash: float
    positions: list[Position]
    day_pnl: float | None = None
    day_pnl_pct: float | None = None
    prior_total: float | None = None
    prior_date: str | None = None
    stale_prices: list[str] = field(default_factory=list)

    @property
    def positions_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def total_value(self) -> float:
        return self.cash + self.positions_value

    @property
    def open_unrealized(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions)

    def best(self) -> Position | None:
        held = [p for p in self.positions if p.quantity]
        return max(held, key=lambda p: p.unrealized_pct, default=None)

    def worst(self) -> Position | None:
        held = [p for p in self.positions if p.quantity]
        return min(held, key=lambda p: p.unrealized_pct, default=None)


class PortfolioTracker:
    """Reads the ledger, values it, and remembers each day's close."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------- mutations
    def record_trade(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        *,
        fees: float = 0.0,
        executed_at: datetime | None = None,
        note: str | None = None,
    ) -> int:
        side = side.lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if quantity <= 0 or price < 0:
            raise ValueError("quantity must be positive and price non-negative")
        stamp = (executed_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
        return self.db.execute(
            "INSERT INTO trades(symbol, side, quantity, price, fees, executed_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol.upper(), side, quantity, price, fees, stamp, note),
        )

    def record_cash(
        self, amount: float, *, occurred_at: datetime | None = None, note: str | None = None
    ) -> int:
        stamp = (occurred_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
        return self.db.execute(
            "INSERT INTO cash_flows(amount, occurred_at, note) VALUES (?, ?, ?)",
            (amount, stamp, note),
        )

    # ------------------------------------------------------------ derivation
    def cash(self, as_of: datetime | None = None) -> float:
        clause, params = self._time_clause("occurred_at", as_of)
        deposits = float(
            self.db.scalar(f"SELECT COALESCE(SUM(amount), 0) FROM cash_flows{clause}", params)
            or 0.0
        )
        tclause, tparams = self._time_clause("executed_at", as_of)
        rows = self.db.query(
            f"SELECT side, quantity, price, fees FROM trades{tclause}", tparams
        )
        for r in rows:
            gross = r["quantity"] * r["price"]
            deposits += (-gross if r["side"] == "buy" else gross) - r["fees"]
        return round(deposits, 2)

    def positions(self, as_of: datetime | None = None) -> list[Position]:
        clause, params = self._time_clause("executed_at", as_of)
        rows = self.db.query(
            f"SELECT symbol, side, quantity, price, fees FROM trades{clause} "
            "ORDER BY executed_at, id",
            params,
        )
        book: dict[str, Position] = {}
        for r in rows:
            pos = book.setdefault(r["symbol"], Position(r["symbol"], 0.0, 0.0))
            qty, price = r["quantity"], r["price"]
            if r["side"] == "buy":
                total_cost = pos.cost_basis + qty * price + r["fees"]
                pos.quantity += qty
                pos.avg_cost = total_cost / pos.quantity if pos.quantity else 0.0
            else:
                sold = min(qty, pos.quantity)
                pos.realized_pnl += sold * (price - pos.avg_cost) - r["fees"]
                pos.quantity -= sold
                if pos.quantity <= 1e-9:
                    pos.quantity = 0.0
        return [p for p in book.values() if p.quantity > 0 or p.realized_pnl]

    @staticmethod
    def _time_clause(column: str, as_of: datetime | None) -> tuple[str, tuple]:
        if as_of is None:
            return "", ()
        return f" WHERE {column} <= ?", (as_of.strftime("%Y-%m-%d %H:%M:%S"),)

    # ------------------------------------------------------------ valuation
    def snapshot(
        self,
        price_lookup: Callable[[list[str]], Mapping[str, float]] | None = None,
        *,
        as_of: datetime | None = None,
    ) -> PortfolioSnapshot:
        """Value the book. ``price_lookup`` maps symbols -> last price.

        A symbol the lookup cannot price falls back to cost basis and is listed
        in ``stale_prices`` so the greeting can say so rather than quietly
        reporting a wrong number.
        """
        as_of = as_of or datetime.now(timezone.utc)
        positions = [p for p in self.positions(as_of) if p.quantity > 0]
        stale: list[str] = []
        if positions and price_lookup:
            symbols = [p.symbol for p in positions]
            try:
                prices = price_lookup(symbols) or {}
            except Exception:
                prices = {}
            for pos in positions:
                price = prices.get(pos.symbol)
                if price is None or price <= 0:
                    stale.append(pos.symbol)
                else:
                    pos.last_price = float(price)
        elif positions:
            stale = [p.symbol for p in positions]

        snap = PortfolioSnapshot(as_of=as_of, cash=self.cash(as_of), positions=positions,
                                 stale_prices=stale)
        prior = self.previous_snapshot(before=as_of.date())
        if prior:
            snap.prior_total = prior["total_value"]
            snap.prior_date = prior["as_of_date"]
            snap.day_pnl = round(snap.total_value - prior["total_value"], 2)
            if prior["total_value"]:
                snap.day_pnl_pct = round(snap.day_pnl / prior["total_value"] * 100, 2)
        return snap

    # ------------------------------------------------------- daily marks
    def close_day(
        self, snapshot: PortfolioSnapshot, *, as_of_date: date | None = None
    ) -> None:
        """Persist an end-of-day mark. Idempotent per date."""
        day = (as_of_date or snapshot.as_of.date()).isoformat()
        self.db.execute(
            """
            INSERT INTO equity_snapshots(as_of_date, cash, positions_value, total_value,
                                         day_pnl, day_pnl_pct)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_date) DO UPDATE SET
                cash = excluded.cash,
                positions_value = excluded.positions_value,
                total_value = excluded.total_value,
                day_pnl = excluded.day_pnl,
                day_pnl_pct = excluded.day_pnl_pct
            """,
            (
                day,
                round(snapshot.cash, 2),
                round(snapshot.positions_value, 2),
                round(snapshot.total_value, 2),
                snapshot.day_pnl,
                snapshot.day_pnl_pct,
            ),
        )

    def previous_snapshot(self, before: date | None = None) -> dict[str, Any] | None:
        before = before or datetime.now(timezone.utc).date()
        row = self.db.query_one(
            "SELECT * FROM equity_snapshots WHERE as_of_date < ? "
            "ORDER BY as_of_date DESC LIMIT 1",
            (before.isoformat(),),
        )
        return dict(row) if row else None

    def history(self, days: int = 30) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM equity_snapshots ORDER BY as_of_date DESC LIMIT ?", (days,)
        )
        return [dict(r) for r in reversed(rows)]

    def performance(self, days: int = 30) -> dict[str, Any]:
        hist = self.history(days)
        if len(hist) < 2:
            return {"days": len(hist), "change": None, "change_pct": None, "win_days": 0}
        start, end = hist[0]["total_value"], hist[-1]["total_value"]
        wins = sum(1 for h in hist if (h["day_pnl"] or 0) > 0)
        return {
            "days": len(hist),
            "change": round(end - start, 2),
            "change_pct": round((end - start) / start * 100, 2) if start else None,
            "win_days": wins,
            "loss_days": sum(1 for h in hist if (h["day_pnl"] or 0) < 0),
            "best_day": max((h["day_pnl"] or 0) for h in hist),
            "worst_day": min((h["day_pnl"] or 0) for h in hist),
        }

    def recent_trades(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM trades ORDER BY executed_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]
