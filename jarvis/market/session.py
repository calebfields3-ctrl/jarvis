"""The trading clock.

On a daily chart the time of day is irrelevant. On a five-minute chart it is
most of the information: the same breakout means different things at 09:35, at
noon, and at 15:55. Every intraday detector in ``intraday.py`` asks this module
what part of the session a bar belongs to before deciding anything.

Times are US/Eastern, which is the market's clock regardless of where you are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Iterable, Sequence

try:  # Python 3.9+ stdlib; falls back to a fixed offset if tzdata is absent.
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - minimal containers ship no tzdata
    EASTERN = timezone(timedelta(hours=-5), "EST")


# Regular US equity hours.
PREMARKET_OPEN = time(4, 0)
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
AFTERHOURS_CLOSE = time(20, 0)


class Phase(str, Enum):
    """Where in the day a bar sits. Ordered as the session runs."""

    CLOSED = "closed"
    PREMARKET = "premarket"
    OPENING = "opening"        # 09:30-10:00, the highest-volatility window
    MORNING = "morning"        # 10:00-12:00, trends established at the open
    MIDDAY = "midday"          # 12:00-14:00, thin and chop-prone
    AFTERNOON = "afternoon"    # 14:00-15:00, positioning for the close
    POWER_HOUR = "power_hour"  # 15:00-16:00, institutional rebalancing
    AFTERHOURS = "afterhours"

    @property
    def is_regular_hours(self) -> bool:
        return self in {
            Phase.OPENING, Phase.MORNING, Phase.MIDDAY,
            Phase.AFTERNOON, Phase.POWER_HOUR,
        }

    @property
    def is_tradeable(self) -> bool:
        """Phases a day trader should be willing to take a new entry in.

        Midday is excluded deliberately: thin volume produces breakouts that
        do not follow through, and it is where undisciplined traders give back
        the morning.
        """
        return self in {Phase.OPENING, Phase.MORNING, Phase.AFTERNOON, Phase.POWER_HOUR}


# US market holidays. Weekends are handled separately; this covers the fixed
# and observed closures that actually remove a trading day.
_FIXED_HOLIDAYS_MMDD = {(1, 1), (6, 19), (7, 4), (12, 25)}


def parse_bar_time(ts: str) -> datetime:
    """Read a bar timestamp.

    Daily bars carry ``YYYY-MM-DD`` and are anchored to the close; intraday bars
    carry a full timestamp. Both come back tz-aware in Eastern so downstream
    comparisons never mix naive and aware datetimes.
    """
    text = ts.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[: len(text)], fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            parsed = parsed.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=EASTERN)
        return parsed.astimezone(EASTERN)
    raise ValueError(f"unrecognised bar timestamp: {ts!r}")


def phase_at(moment: datetime | str) -> Phase:
    """Which part of the session a moment falls in."""
    if isinstance(moment, str):
        moment = parse_bar_time(moment)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    moment = moment.astimezone(EASTERN)

    if not is_trading_day(moment.date()):
        return Phase.CLOSED

    clock = moment.time()
    if clock < PREMARKET_OPEN:
        return Phase.CLOSED
    if clock < MARKET_OPEN:
        return Phase.PREMARKET
    if clock < time(10, 0):
        return Phase.OPENING
    if clock < time(12, 0):
        return Phase.MORNING
    if clock < time(14, 0):
        return Phase.MIDDAY
    if clock < time(15, 0):
        return Phase.AFTERNOON
    if clock < MARKET_CLOSE:
        return Phase.POWER_HOUR
    if clock < AFTERHOURS_CLOSE:
        return Phase.AFTERHOURS
    return Phase.CLOSED


def is_trading_day(day: date) -> bool:
    """Weekday and not a fixed/observed US market holiday."""
    if day.weekday() >= 5:
        return False
    if (day.month, day.day) in _FIXED_HOLIDAYS_MMDD:
        return False
    # A holiday landing at a weekend is observed on the adjacent weekday, and
    # the market is closed that day: Saturday holidays move to the Friday
    # before, Sunday holidays to the Monday after.
    if day.weekday() == 4 and ((day.month, day.day + 1) in _FIXED_HOLIDAYS_MMDD):
        return False
    if day.weekday() == 0 and ((day.month, day.day - 1) in _FIXED_HOLIDAYS_MMDD):
        return False
    # Thanksgiving: fourth Thursday of November.
    if day.month == 11 and day.weekday() == 3 and 22 <= day.day <= 28:
        return False
    return True


def minutes_into_session(moment: datetime | str) -> float | None:
    """Minutes since the opening bell; None outside regular hours."""
    if isinstance(moment, str):
        moment = parse_bar_time(moment)
    moment = moment.astimezone(EASTERN)
    if not phase_at(moment).is_regular_hours:
        return None
    open_at = moment.replace(
        hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0
    )
    return (moment - open_at).total_seconds() / 60


def minutes_to_close(moment: datetime | str) -> float | None:
    """Minutes until the closing bell; None outside regular hours."""
    if isinstance(moment, str):
        moment = parse_bar_time(moment)
    moment = moment.astimezone(EASTERN)
    if not phase_at(moment).is_regular_hours:
        return None
    close_at = moment.replace(
        hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0
    )
    return (close_at - moment).total_seconds() / 60


def session_date(moment: datetime | str) -> date:
    if isinstance(moment, str):
        moment = parse_bar_time(moment)
    return moment.astimezone(EASTERN).date()


def group_by_session(bars: Sequence) -> dict[date, list]:
    """Split a run of intraday bars into trading days, in order."""
    sessions: dict[date, list] = {}
    for bar in bars:
        sessions.setdefault(session_date(bar.ts), []).append(bar)
    return sessions


def regular_hours_only(bars: Sequence) -> list:
    """Drop pre- and post-market bars.

    Extended-hours prints come from thin books and routinely mark highs and
    lows that no regular-hours order could ever have filled at, which quietly
    corrupts opening-range and VWAP calculations.
    """
    return [b for b in bars if phase_at(b.ts).is_regular_hours]


@dataclass(frozen=True)
class SessionClock:
    """A fixed 'now' so scans and tests reason about the same moment."""

    now: datetime

    @classmethod
    def live(cls) -> "SessionClock":
        return cls(datetime.now(tz=EASTERN))

    @classmethod
    def at(cls, moment: datetime | str) -> "SessionClock":
        if isinstance(moment, str):
            moment = parse_bar_time(moment)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=EASTERN)
        return cls(moment.astimezone(EASTERN))

    @property
    def phase(self) -> Phase:
        return phase_at(self.now)

    @property
    def can_open_new_trades(self) -> bool:
        """Late in the day a new day trade cannot resolve before the bell."""
        remaining = minutes_to_close(self.now)
        return self.phase.is_tradeable and (remaining is None or remaining >= 15)

    def describe(self) -> str:
        phase = self.phase
        stamp = self.now.strftime("%H:%M")
        if phase is Phase.CLOSED:
            return f"Market closed ({stamp} ET)."
        if phase is Phase.PREMARKET:
            return f"Pre-market, {stamp} ET -- thin books, gaps are not confirmed yet."
        if phase is Phase.AFTERHOURS:
            return f"After hours, {stamp} ET -- prints here rarely hold to tomorrow's open."
        remaining = minutes_to_close(self.now) or 0
        labels = {
            Phase.OPENING: "Opening drive",
            Phase.MORNING: "Morning trend",
            Phase.MIDDAY: "Midday chop -- the worst hours to force a trade",
            Phase.AFTERNOON: "Afternoon positioning",
            Phase.POWER_HOUR: "Power hour",
        }
        return f"{labels[phase]}, {stamp} ET, {remaining:.0f} minutes to the close."
