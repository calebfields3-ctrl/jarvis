"""Day-trading risk management.

The part beginners skip and blow up over. Three jobs:

1. **Size the position from the stop**, not from a gut feeling about how much
   to spend. Risk a fixed fraction of the account per trade and let the stop
   distance decide the share count.
2. **Track the pattern day trader rule.** Four or more day trades in five
   rolling business days flags a US margin account as a PDT and requires
   $25,000 minimum equity. Getting flagged under that threshold freezes the
   account for 90 days. Jarvis counts every day trade so this never arrives
   as a surprise.
3. **Enforce the daily circuit breakers** -- max loss, max trades, max
   consecutive losers -- because the trades taken after a bad morning are
   reliably the worst ones of the week.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from ..market.session import EASTERN, is_trading_day, parse_bar_time
from ..memory.db import Database

# FINRA thresholds. These are regulation, not preference.
PDT_TRADE_LIMIT = 3            # a 4th day trade in the window triggers the rule
PDT_WINDOW_BUSINESS_DAYS = 5
PDT_EQUITY_MINIMUM = 25_000.0


@dataclass
class RiskProfile:
    """The rules Caleb trades by. Conservative defaults on purpose."""

    account_risk_pct: float = 0.5        # risk per trade, % of account equity
    max_position_pct: float = 20.0       # cap on any one position, % of equity
    daily_loss_limit_pct: float = 2.0    # stop trading for the day past this
    max_trades_per_day: int = 6
    max_consecutive_losses: int = 3
    min_reward_risk: float = 1.5

    def validate(self) -> None:
        if not 0 < self.account_risk_pct <= 5:
            raise ValueError("account_risk_pct must be between 0 and 5")
        if self.daily_loss_limit_pct <= 0:
            raise ValueError("daily_loss_limit_pct must be positive")


@dataclass
class PositionPlan:
    """A fully specified trade: size, stop, target, and what it can cost."""

    symbol: str
    direction: str
    entry: float
    stop: float
    target: float
    shares: int
    risk_amount: float
    position_value: float
    reward_risk: float
    warnings: list[str] = field(default_factory=list)
    rejected_reason: str | None = None

    @property
    def is_viable(self) -> bool:
        return self.rejected_reason is None and self.shares > 0

    def describe(self) -> str:
        if not self.is_viable:
            return f"{self.symbol}: no trade -- {self.rejected_reason}"
        lines = [
            f"{self.symbol} {self.direction.upper()} {self.shares} shares "
            f"@ {self.entry:.2f}",
            f"  stop {self.stop:.2f} | target {self.target:.2f} | {self.reward_risk:.1f}R",
            f"  risking ${self.risk_amount:,.2f} on ${self.position_value:,.2f} of stock",
        ]
        lines.extend(f"  warning: {w}" for w in self.warnings)
        return "\n".join(lines)


@dataclass
class DayTradeStatus:
    """Where Caleb stands against the PDT rule and today's circuit breakers."""

    equity: float
    day_trades_in_window: int
    window_start: date
    trades_today: int
    realized_today: float
    consecutive_losses: int
    blocked_reasons: list[str] = field(default_factory=list)

    @property
    def is_pdt_restricted(self) -> bool:
        """True when another day trade would trip the rule under $25k."""
        return (
            self.equity < PDT_EQUITY_MINIMUM
            and self.day_trades_in_window >= PDT_TRADE_LIMIT
        )

    @property
    def day_trades_remaining(self) -> int | None:
        """How many more day trades are safe. None once equity clears $25k."""
        if self.equity >= PDT_EQUITY_MINIMUM:
            return None
        return max(0, PDT_TRADE_LIMIT - self.day_trades_in_window)

    @property
    def can_trade(self) -> bool:
        return not self.blocked_reasons

    def describe(self) -> str:
        lines = []
        remaining = self.day_trades_remaining
        if remaining is None:
            lines.append(
                f"Equity ${self.equity:,.2f} is above the $25,000 PDT minimum -- "
                "day trades are unrestricted."
            )
        else:
            lines.append(
                f"Equity ${self.equity:,.2f} is below the $25,000 PDT minimum. "
                f"{self.day_trades_in_window} day trades used since "
                f"{self.window_start}; {remaining} left before the rule trips."
            )
        streak = (
            "no losing streak" if self.consecutive_losses == 0
            else f"{self.consecutive_losses} "
                 f"loss{'es' if self.consecutive_losses != 1 else ''} in a row"
        )
        lines.append(
            f"Today: {self.trades_today} trades, "
            f"${self.realized_today:+,.2f} realised, {streak}."
        )
        if self.blocked_reasons:
            lines.append("STOP TRADING TODAY:")
            lines.extend(f"  - {r}" for r in self.blocked_reasons)
        return "\n".join(lines)


class RiskManager:
    """Sizes trades and polices the day's limits."""

    def __init__(self, db: Database, profile: RiskProfile | None = None) -> None:
        self.db = db
        self.profile = profile or RiskProfile()
        self.profile.validate()

    # ------------------------------------------------------------- sizing
    def plan_trade(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target: float,
        *,
        equity: float,
    ) -> PositionPlan:
        """Turn a setup into a share count, or explain why it is not a trade."""
        warnings: list[str] = []

        def reject(reason: str) -> PositionPlan:
            return PositionPlan(
                symbol=symbol.upper(), direction=direction, entry=entry, stop=stop,
                target=target, shares=0, risk_amount=0.0, position_value=0.0,
                reward_risk=0.0, warnings=warnings, rejected_reason=reason,
            )

        if entry <= 0 or equity <= 0:
            return reject("entry price and account equity must be positive")

        # The stop has to be on the losing side of the entry, or it is not a stop.
        if direction == "long" and stop >= entry:
            return reject("a long's stop must sit below the entry")
        if direction == "short" and stop <= entry:
            return reject("a short's stop must sit above the entry")

        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return reject("stop equals entry -- there is no defined risk")

        reward_risk = abs(target - entry) / risk_per_share
        if reward_risk < self.profile.min_reward_risk:
            return reject(
                f"{reward_risk:.1f}R is below your {self.profile.min_reward_risk:.1f}R "
                "minimum -- the maths does not work even at a good hit rate"
            )

        risk_budget = equity * self.profile.account_risk_pct / 100
        shares = int(risk_budget // risk_per_share)
        if shares < 1:
            return reject(
                f"the stop is ${risk_per_share:.2f} wide -- one share risks more than "
                f"your ${risk_budget:,.2f} per-trade budget"
            )

        position_value = shares * entry
        max_value = equity * self.profile.max_position_pct / 100
        if position_value > max_value:
            shares = int(max_value // entry)
            if shares < 1:
                return reject(
                    f"a single share costs ${entry:,.2f}, above your "
                    f"${max_value:,.2f} position cap"
                )
            position_value = shares * entry
            warnings.append(
                f"size capped by the {self.profile.max_position_pct:.0f}% position "
                f"limit -- risking less than the full budget"
            )

        # Stop distance as a share of entry: a very wide stop means the trade
        # needs a big move just to hit target, and a very tight one gets taken
        # out by ordinary noise.
        stop_pct = risk_per_share / entry * 100
        if stop_pct > 5:
            warnings.append(f"{stop_pct:.1f}% stop is wide for a day trade")
        elif stop_pct < 0.2:
            warnings.append(f"{stop_pct:.2f}% stop is inside normal noise -- expect to be shaken out")

        return PositionPlan(
            symbol=symbol.upper(), direction=direction, entry=entry, stop=stop,
            target=target, shares=shares, risk_amount=round(shares * risk_per_share, 2),
            position_value=round(position_value, 2), reward_risk=round(reward_risk, 2),
            warnings=warnings,
        )

    # ---------------------------------------------------------- PDT rule
    def count_day_trades(self, as_of: date | None = None) -> tuple[int, date]:
        """Day trades in the rolling five-business-day window.

        A day trade is a buy and a sell of the same symbol on the same session.
        Counted as the number of matched round trips, which is how a broker
        counts them.
        """
        as_of = as_of or datetime.now(tz=EASTERN).date()
        window_start = _business_days_back(as_of, PDT_WINDOW_BUSINESS_DAYS - 1)

        rows = self.db.query(
            "SELECT symbol, side, quantity, DATE(executed_at) AS day FROM trades "
            "WHERE DATE(executed_at) >= ? AND DATE(executed_at) <= ? "
            "ORDER BY executed_at, id",
            (window_start.isoformat(), as_of.isoformat()),
        )
        by_day_symbol: dict[tuple[str, str], dict[str, float]] = {}
        for row in rows:
            key = (row["day"], row["symbol"])
            bucket = by_day_symbol.setdefault(key, {"buy": 0.0, "sell": 0.0})
            bucket[row["side"]] += row["quantity"]

        day_trades = 0
        for bucket in by_day_symbol.values():
            # Each matched round trip in a session is one day trade.
            if bucket["buy"] > 0 and bucket["sell"] > 0:
                day_trades += 1
        return day_trades, window_start

    def status(self, equity: float, *, as_of: date | None = None) -> DayTradeStatus:
        """Everything that could stop Caleb taking the next trade."""
        as_of = as_of or datetime.now(tz=EASTERN).date()
        day_trades, window_start = self.count_day_trades(as_of)

        trades_today = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM trades WHERE DATE(executed_at) = ?",
                (as_of.isoformat(),),
            ) or 0
        )
        realized_today = self._realized_pnl_on(as_of)
        consecutive = self._consecutive_losses()

        blocked: list[str] = []
        if equity < PDT_EQUITY_MINIMUM and day_trades >= PDT_TRADE_LIMIT:
            consequence = (
                "You are at the limit -- one more within five business days flags "
                "the account and freezes it for 90 days."
                if day_trades == PDT_TRADE_LIMIT
                else "You are past the limit. Expect the account to be flagged and "
                     "restricted for 90 days."
            )
            blocked.append(
                f"pattern day trader rule: {day_trades} day trades since "
                f"{window_start} on ${equity:,.2f} equity. {consequence}"
            )
        loss_limit = -abs(equity * self.profile.daily_loss_limit_pct / 100)
        # An unfunded account has a zero limit, and 0.0 <= -0.0 is True in
        # Python -- which would report a flat day as a blown loss limit.
        if loss_limit < 0 and realized_today <= loss_limit:
            blocked.append(
                f"daily loss limit hit: ${realized_today:,.2f} against a "
                f"${loss_limit:,.2f} limit"
            )
        if trades_today >= self.profile.max_trades_per_day:
            blocked.append(
                f"trade count limit: {trades_today} today, max "
                f"{self.profile.max_trades_per_day}"
            )
        if consecutive >= self.profile.max_consecutive_losses:
            blocked.append(
                f"{consecutive} losing trades in a row -- step away before the next one"
            )

        return DayTradeStatus(
            equity=equity,
            day_trades_in_window=day_trades,
            window_start=window_start,
            trades_today=trades_today,
            realized_today=round(realized_today, 2),
            consecutive_losses=consecutive,
            blocked_reasons=blocked,
        )

    # ------------------------------------------------------------ history
    def _realized_pnl_on(self, day: date) -> float:
        """Realised P/L from round trips closed on a given session."""
        rows = self.db.query(
            "SELECT symbol, side, quantity, price, fees FROM trades "
            "WHERE DATE(executed_at) = ? ORDER BY executed_at, id",
            (day.isoformat(),),
        )
        books: dict[str, list[tuple[float, float]]] = {}
        realized = 0.0
        for row in rows:
            symbol, qty, price = row["symbol"], row["quantity"], row["price"]
            lots = books.setdefault(symbol, [])
            if row["side"] == "buy":
                lots.append((qty, price))
                realized -= row["fees"]
            else:
                remaining = qty
                realized -= row["fees"]
                while remaining > 0 and lots:
                    lot_qty, lot_price = lots[0]
                    matched = min(remaining, lot_qty)
                    realized += matched * (price - lot_price)
                    remaining -= matched
                    if matched >= lot_qty:
                        lots.pop(0)
                    else:
                        lots[0] = (lot_qty - matched, lot_price)
        return realized

    def _consecutive_losses(self, lookback: int = 20) -> int:
        """Losing round trips in a row, counting back from the most recent."""
        results = self.closed_trades(limit=lookback)
        streak = 0
        for trade in results:
            if trade["pnl"] < 0:
                streak += 1
            else:
                break
        return streak

    def closed_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """Completed round trips, newest first, with P/L and R multiple.

        Matched FIFO per symbol across the whole ledger, which is what the
        journal review reasons about.
        """
        rows = self.db.query(
            "SELECT symbol, side, quantity, price, fees, executed_at, note "
            "FROM trades ORDER BY executed_at, id"
        )
        books: dict[str, list[dict]] = {}
        closed: list[dict[str, Any]] = []
        for row in rows:
            symbol = row["symbol"]
            lots = books.setdefault(symbol, [])
            if row["side"] == "buy":
                lots.append({"qty": row["quantity"], "price": row["price"],
                             "opened_at": row["executed_at"]})
            else:
                remaining = row["quantity"]
                while remaining > 0 and lots:
                    lot = lots[0]
                    matched = min(remaining, lot["qty"])
                    pnl = matched * (row["price"] - lot["price"]) - row["fees"]
                    closed.append({
                        "symbol": symbol,
                        "quantity": matched,
                        "entry": lot["price"],
                        "exit": row["price"],
                        "opened_at": lot["opened_at"],
                        "closed_at": row["executed_at"],
                        "pnl": round(pnl, 2),
                        "return_pct": round(
                            (row["price"] - lot["price"]) / lot["price"] * 100, 3
                        ) if lot["price"] else 0.0,
                        "same_session": str(lot["opened_at"])[:10] == str(row["executed_at"])[:10],
                        "note": row["note"],
                    })
                    remaining -= matched
                    lot["qty"] -= matched
                    if lot["qty"] <= 1e-9:
                        lots.pop(0)
        closed.sort(key=lambda t: t["closed_at"], reverse=True)
        return closed[:limit]


def _business_days_back(from_day: date, count: int) -> date:
    """Walk back N trading days, skipping weekends and market holidays."""
    day = from_day
    stepped = 0
    while stepped < count:
        day -= timedelta(days=1)
        if is_trading_day(day):
            stepped += 1
    return day
