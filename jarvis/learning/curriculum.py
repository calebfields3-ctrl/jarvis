"""What Jarvis knows on day one, and how that baseline is planted.

Jarvis starts as a competent beginner, not an expert: it knows what a stock is,
what a bid/ask spread means, that position sizing exists. These seed lessons are
tier ``foundation`` -- they are never retired by the outcome loop, because they
are definitions rather than predictions. Everything above this line has to be
learned and then earned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .sources import _load_yaml

# The floor. Deliberately shallow -- concepts, not edges.
FOUNDATIONS: list[tuple[str, str]] = [
    # --- what the instruments are
    ("instruments", "A share of stock is a fractional ownership claim on a company's assets and future earnings."),
    ("instruments", "A stock's price is set by what buyers and sellers agree to transact at, not by the company."),
    ("instruments", "An ETF holds a basket of assets and trades like a single stock."),
    ("instruments", "An index like the S&P 500 measures a group of stocks; you trade products that track it, not the index itself."),
    # --- how trading works mechanically
    ("mechanics", "The bid is the highest price a buyer will pay; the ask is the lowest a seller will accept; the gap between them is the spread."),
    ("mechanics", "A market order fills immediately at whatever price is available; a limit order only fills at your price or better."),
    ("mechanics", "A stop order becomes a market order once the stop price trades, so its fill price is not guaranteed."),
    ("mechanics", "Liquidity is how much size can trade without moving the price; thin stocks have wide spreads and worse fills."),
    ("mechanics", "Volume is the number of shares traded; it shows how much conviction is behind a price move."),
    ("mechanics", "Regular US market hours are 9:30am to 4:00pm Eastern; pre- and post-market trading is thinner and gappier."),
    ("mechanics", "Settlement, fees and slippage all reduce realised returns relative to the price on the chart."),
    # --- reading a chart
    ("charts", "A candlestick shows open, high, low and close for one time period."),
    ("charts", "A moving average smooths price to show trend direction; price above a rising average is an uptrend."),
    ("charts", "Support is a price where buyers have previously stepped in; resistance is where sellers have."),
    ("charts", "Higher highs and higher lows define an uptrend; lower highs and lower lows define a downtrend."),
    ("charts", "A timeframe is a choice, not a truth: the same chart can be an uptrend daily and a downtrend hourly."),
    # --- indicators, stated honestly
    ("indicators", "RSI measures the speed of recent gains against losses on a 0-100 scale; above 70 is called overbought and below 30 oversold."),
    ("indicators", "Overbought does not mean 'about to fall' -- strong trends stay overbought for a long time."),
    ("indicators", "MACD compares two moving averages to show momentum shifts."),
    ("indicators", "Indicators are derived from price, so they lag price; they describe, they do not predict."),
    # --- fundamentals
    ("fundamentals", "Earnings per share is profit divided by shares outstanding."),
    ("fundamentals", "The price-to-earnings ratio compares price to earnings; it is only meaningful against peers and history."),
    ("fundamentals", "Revenue growth, margins and debt load describe the business behind the ticker."),
    ("fundamentals", "Companies report earnings quarterly, and prices often move sharply on the report."),
    ("fundamentals", "Market capitalisation is share price times shares outstanding -- the market's price for the whole company."),
    # --- risk, which beginners underweight
    ("risk", "Position sizing decides how much a single bad trade can cost you, and it matters more than entry timing."),
    ("risk", "Risking a small fixed fraction of the account per trade is what keeps a losing streak survivable."),
    ("risk", "Diversification reduces the damage from any one position being wrong."),
    ("risk", "Leverage multiplies losses exactly as much as it multiplies gains."),
    ("risk", "An exit plan defined before entry is what separates a trade from a hope."),
    ("risk", "Past performance does not guarantee future results, and backtested edges decay."),
    # --- market structure and context
    ("macro", "Interest rates set by central banks affect the discount applied to future company earnings."),
    ("macro", "Inflation data, employment reports and central bank meetings are scheduled events that move whole markets."),
    ("macro", "Most stocks move substantially with the broad market; sector and index direction is context for every single-name trade."),
    ("macro", "Breaking news can reprice an asset faster than any chart pattern resolves."),
    # --- epistemics: how Jarvis is supposed to think
    ("method", "A pattern is a hypothesis about what usually happens next, and it is only worth what its measured hit rate says."),
    ("method", "A signal must be recorded before the outcome is known, otherwise the track record is worthless."),
    ("method", "Correlation found by searching many combinations is often noise; more data is the only cure."),
    ("method", "A confident source is not an accurate source; track outcomes, not conviction."),
]


def seed_foundations(knowledge_store, extra_file: Path | str | None = None) -> int:
    """Plant the baseline. Idempotent -- re-running does not duplicate lessons."""
    added = 0
    for topic, claim in FOUNDATIONS:
        knowledge_store.add_lesson(
            topic=topic, claim=claim, kind="concept",
            tier="foundation", confidence=0.9,
        )
        added += 1

    if extra_file:
        for topic, entries in (_load_yaml(Path(extra_file)) or {}).items():
            for entry in entries or []:
                claim = entry.get("claim") if isinstance(entry, dict) else entry
                if not claim:
                    continue
                knowledge_store.add_lesson(
                    topic=topic,
                    claim=str(claim),
                    kind=(entry.get("kind") if isinstance(entry, dict) else None) or "concept",
                    pattern_key=(entry.get("pattern_key") if isinstance(entry, dict) else None),
                    tier="foundation",
                    confidence=0.85,
                )
                added += 1
    return added


def seed_pattern_hypotheses(knowledge_store) -> int:
    """Give every detector a lesson to attach outcomes to.

    These start as *hypotheses* at deliberately low confidence. Jarvis has heard
    that a golden cross is bullish; it has not yet seen whether that is true in
    the markets it actually watches. Its own graded outcomes decide.
    """
    from ..market.intraday import INTRADAY_PATTERN_KEYS
    from ..market.patterns import PATTERN_KEYS

    descriptions = {
        # intraday -- day trading setups, on the same evidential footing
        "opening_range_breakout": "A break above the first 30 minutes' high tends to continue for the session.",
        "opening_range_breakdown": "A break below the first 30 minutes' low tends to continue for the session.",
        "vwap_reclaim": "Reclaiming VWAP after trading below it tends to lead to further upside.",
        "vwap_rejection": "Failing at VWAP from below tends to lead to further downside.",
        "gap_and_go_long": "A stock that gaps up and makes session highs early tends to keep running.",
        "gap_and_go_short": "A stock that gaps down and makes session lows early tends to keep falling.",
        "failed_breakdown_reversal": "Undercutting the session low and reclaiming it traps shorts and tends to squeeze.",
        "momentum_surge_long": "A high-volume upside thrust tends to see continuation within the session.",
        "momentum_surge_short": "A high-volume downside thrust tends to see continuation within the session.",
        "power_hour_trend_long": "Holding above VWAP at session highs after 15:00 tends to close strong.",
        "power_hour_trend_short": "Pinned below VWAP at session lows after 15:00 tends to close weak.",
        "golden_cross": "A 50-day crossing above the 200-day tends to precede continued strength.",
        "death_cross": "A 50-day crossing below the 200-day tends to precede continued weakness.",
        "rsi_oversold_reversal": "Price turning up out of RSI oversold tends to bounce over the next week.",
        "rsi_overbought_reversal": "Price rolling over from RSI overbought tends to fade over the next week.",
        "breakout_20d_high": "A close above the 20-day high on heavy volume tends to keep running.",
        "breakdown_20d_low": "A close below the 20-day low on heavy volume tends to keep falling.",
        "bullish_engulfing": "A bullish engulfing candle after a decline tends to mark a short-term low.",
        "bearish_engulfing": "A bearish engulfing candle after a rally tends to mark a short-term high.",
        "hammer": "A hammer after a pullback tends to be followed by a bounce.",
        "shooting_star": "A shooting star after a run tends to be followed by a pullback.",
        "macd_bull_cross": "MACD crossing above its signal line tends to precede upside momentum.",
        "macd_bear_cross": "MACD crossing below its signal line tends to precede downside momentum.",
        "squeeze_breakout_up": "A volatility squeeze resolving upward tends to continue upward.",
        "squeeze_breakout_down": "A volatility squeeze resolving downward tends to continue downward.",
        "gap_up_hold": "A stock that gaps up and holds the gap tends to keep drifting higher.",
        "gap_down_continue": "A stock that gaps down and keeps selling tends to keep drifting lower.",
        "volume_dryup_base": "A tight base on drying volume above the 50-day tends to resolve upward.",
    }
    count = 0
    for key in PATTERN_KEYS:
        knowledge_store.add_lesson(
            topic="patterns",
            claim=descriptions.get(key, f"The {key} pattern has predictive value."),
            kind="pattern",
            pattern_key=key,
            tier="hypothesis",
            confidence=0.5,   # a coin flip until the outcomes say otherwise
        )
        count += 1
    for key in INTRADAY_PATTERN_KEYS:
        knowledge_store.add_lesson(
            topic="intraday",
            claim=descriptions.get(key, f"The {key} intraday setup has predictive value."),
            kind="pattern",
            pattern_key=key,
            tier="hypothesis",
            confidence=0.5,
        )
        count += 1
    return count
