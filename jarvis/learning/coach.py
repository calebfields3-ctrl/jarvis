"""Day-trading training for Caleb.

Two halves, because learning to trade needs both:

**The curriculum** -- ordered modules from "what actually is a day trade" up to
session structure, VWAP, sizing and the PDT rule. Each module has a short
lesson and a quiz that has to be passed before the next one unlocks. Progress
persists, so a module passed in March is still passed in July.

**The journal review** -- the part that actually changes behaviour. Jarvis
reads Caleb's real closed trades and names the specific patterns costing him
money: cutting winners while letting losers run, sizing up after a loss,
trading the midday chop, holding day trades overnight. Every finding is drawn
from his own ledger, with the numbers attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import Any, Iterable, Sequence

from ..market.session import EASTERN, Phase, phase_at


# --------------------------------------------------------------- curriculum
@dataclass(frozen=True)
class QuizQuestion:
    prompt: str
    options: tuple[str, ...]
    answer_index: int
    explanation: str

    @property
    def answer(self) -> str:
        return self.options[self.answer_index]


@dataclass(frozen=True)
class TrainingModule:
    key: str
    title: str
    level: int                    # 1 basics -> 4 advanced
    teaching: tuple[str, ...]
    quiz: tuple[QuizQuestion, ...]
    requires: tuple[str, ...] = ()

    @property
    def pass_mark(self) -> int:
        """Most of the quiz, rounded up. Risk modules demand everything."""
        if self.key.startswith("risk") or self.key == "pdt_rule":
            return len(self.quiz)
        return max(1, (len(self.quiz) * 2 + 2) // 3)


CURRICULUM: tuple[TrainingModule, ...] = (
    TrainingModule(
        key="what_is_day_trading",
        title="What a day trade actually is",
        level=1,
        teaching=(
            "A day trade is opening and closing the same security within one "
            "trading session. The position does not exist overnight.",
            "Day trading is not a different kind of investing -- it is a different "
            "job. Returns come from many small edges repeated, not from being "
            "right about a company.",
            "The costs are real and constant: spread, commission, slippage and "
            "taxes are paid on every round trip, win or lose.",
            "Most people who day trade lose money. The ones who do not treat it "
            "as a process with rules, not as a series of opinions.",
        ),
        quiz=(
            QuizQuestion(
                "What makes a trade a 'day trade'?",
                ("Holding it for less than a week",
                 "Opening and closing the same security in one session",
                 "Trading with leverage",
                 "Trading a volatile stock"),
                1,
                "The defining feature is that the position opens and closes in the "
                "same session, so it carries no overnight risk.",
            ),
            QuizQuestion(
                "Where do day trading returns come from?",
                ("Correctly predicting a company's future",
                 "Holding through volatility until you are proven right",
                 "Repeating a small statistical edge many times",
                 "Finding stocks nobody else has noticed"),
                2,
                "A day trader is running a process with a small edge repeated often. "
                "Any single trade is close to noise.",
            ),
            QuizQuestion(
                "Which cost applies to every single round trip?",
                ("Only commission", "Only taxes",
                 "Spread, commission, slippage and taxes",
                 "None if the trade is profitable"),
                2,
                "All of them are paid whether the trade wins or loses, which is why "
                "a marginal edge disappears once costs are included.",
            ),
        ),
    ),
    TrainingModule(
        key="session_structure",
        title="The shape of the trading day",
        level=1,
        requires=("what_is_day_trading",),
        teaching=(
            "09:30-10:00 is the opening drive: the highest volume and volatility "
            "of the day, and where most of the day's range is often set.",
            "10:00-12:00 carries the trend established at the open, usually with "
            "cleaner follow-through than the first fifteen minutes.",
            "12:00-14:00 is midday: volume drains out, ranges compress, and "
            "breakouts stop following through. Most avoidable losses live here.",
            "15:00-16:00 is power hour, when institutional rebalancing returns "
            "volume and moves can accelerate into the bell.",
            "The clock is information. The same chart pattern is a different "
            "trade at 09:45 than it is at 12:30.",
        ),
        quiz=(
            QuizQuestion(
                "Which window is generally the worst for taking new breakouts?",
                ("09:30-10:00", "10:00-12:00", "12:00-14:00", "15:00-16:00"),
                2,
                "Midday volume drains out, so breakouts lack the participation "
                "needed to follow through.",
            ),
            QuizQuestion(
                "Why does the time of day change the meaning of a pattern?",
                ("It does not -- price is price",
                 "Volume and participation vary through the session",
                 "Exchanges change the fee structure",
                 "Because algorithms only run in the morning"),
                1,
                "A breakout needs participation to continue. Volume varies enormously "
                "across the session, so identical patterns have different odds.",
            ),
        ),
    ),
    TrainingModule(
        key="risk_sizing",
        title="Position sizing from the stop",
        level=2,
        requires=("session_structure",),
        teaching=(
            "Decide the stop first, then the size. The stop defines the risk per "
            "share; the size is arithmetic from there.",
            "Risk a small fixed fraction of the account per trade -- commonly "
            "0.5% to 1%. Shares = (account x risk%) / (entry - stop).",
            "Sizing this way means a wide stop gives you a small position and a "
            "tight stop gives you a larger one, for identical dollar risk.",
            "Never size by 'how much do I want to make'. That inverts the whole "
            "calculation and is how accounts die.",
            "A run of losers is normal. Fixed fractional sizing is what makes a "
            "normal losing streak survivable rather than terminal.",
        ),
        quiz=(
            QuizQuestion(
                "Account is $30,000, risking 1% per trade. Entry $50, stop $48. "
                "How many shares?",
                ("150 shares", "300 shares", "600 shares", "60 shares"),
                0,
                "$30,000 x 1% = $300 of risk. Risk per share is $50 - $48 = $2. "
                "$300 / $2 = 150 shares.",
            ),
            QuizQuestion(
                "Your stop is very wide. What happens to your position size?",
                ("It gets bigger", "It gets smaller",
                 "It stays the same", "Size does not depend on the stop"),
                1,
                "Dollar risk is held constant, so a wider stop means fewer shares.",
            ),
            QuizQuestion(
                "What should be decided first?",
                ("How many shares to buy", "How much profit you want",
                 "Where the stop goes", "How much buying power you have"),
                2,
                "The stop defines the risk. Everything else is derived from it.",
            ),
        ),
    ),
    TrainingModule(
        key="pdt_rule",
        title="The pattern day trader rule",
        level=2,
        requires=("risk_sizing",),
        teaching=(
            "In a US margin account, four or more day trades within five rolling "
            "business days makes you a 'pattern day trader'.",
            "A pattern day trader must maintain $25,000 in account equity. Being "
            "flagged below that threshold restricts the account for 90 days.",
            "The window rolls -- it is any five consecutive business days, not a "
            "calendar week, so Monday's trade still counts on Friday.",
            "Under $25,000 you get three day trades per rolling five-day window. "
            "Plan which setups are worth spending one on.",
            "A cash account has no PDT rule, but settlement means funds from a "
            "sale are not reusable for one business day.",
        ),
        quiz=(
            QuizQuestion(
                "How many day trades in five business days trigger the PDT rule?",
                ("2 or more", "3 or more", "4 or more", "10 or more"),
                2,
                "Four or more day trades within five rolling business days flags the "
                "account as a pattern day trader.",
            ),
            QuizQuestion(
                "What equity must a flagged pattern day trader maintain?",
                ("$2,000", "$10,000", "$25,000", "$100,000"),
                2,
                "$25,000 is the FINRA minimum for a flagged pattern day trader.",
            ),
            QuizQuestion(
                "Is the five-day window a calendar week?",
                ("Yes, it resets every Monday",
                 "No, it rolls across any five consecutive business days",
                 "Yes, it resets on the 1st of the month",
                 "It resets after every profitable trade"),
                1,
                "The window rolls continuously, so a trade from four days ago still "
                "counts against you today.",
            ),
        ),
    ),
    TrainingModule(
        key="vwap_and_levels",
        title="VWAP and the opening range",
        level=3,
        requires=("session_structure",),
        teaching=(
            "VWAP is volume-weighted average price, anchored to the session open. "
            "Institutions benchmark their fills against it.",
            "Because it is the benchmark, VWAP acts as intraday support and "
            "resistance far more reliably than a plain moving average.",
            "Above VWAP with it rising is intraday strength; below it and falling "
            "is weakness. Crossing it is a change of control.",
            "The opening range -- the high and low of the first 30 minutes -- "
            "frames the rest of the day. Breaks of it are the classic day trade.",
            "Levels work because many participants watch them, not because of "
            "anything intrinsic. That is also why they fail when volume is thin.",
        ),
        quiz=(
            QuizQuestion(
                "What does VWAP measure?",
                ("The average of the day's high and low",
                 "Volume-weighted average price since the session open",
                 "A 20-period moving average",
                 "The price with the most volume traded"),
                1,
                "It is the running average price weighted by volume, anchored to "
                "the session open.",
            ),
            QuizQuestion(
                "Why does VWAP tend to act as support and resistance?",
                ("It is a mathematical law",
                 "Institutions benchmark execution against it",
                 "Exchanges enforce it",
                 "It always sits at the day's midpoint"),
                1,
                "Large orders are judged against VWAP, so real buying and selling "
                "clusters around it.",
            ),
            QuizQuestion(
                "The opening range is the high and low of which period?",
                ("The whole morning", "The first 30 minutes",
                 "Pre-market", "The previous day"),
                1,
                "Conventionally the first 30 minutes, which frames the day's levels.",
            ),
        ),
    ),
    TrainingModule(
        key="discipline",
        title="The psychology that actually costs money",
        level=3,
        requires=("risk_sizing",),
        teaching=(
            "Revenge trading -- taking a bigger position right after a loss to win "
            "it back -- is the single most expensive habit in day trading.",
            "Cutting winners early while holding losers is the natural human "
            "instinct and it is exactly backwards. It produces a high win rate "
            "and a shrinking account.",
            "A daily loss limit is not weakness. Once you are down, judgement is "
            "measurably worse, and the next trade is statistically your worst.",
            "Overtrading is what boredom looks like on a P/L statement. If the "
            "setup is not one of your defined ones, there is no trade.",
            "The plan is made before the market opens, when you are calm. During "
            "the session you execute it; you do not renegotiate it.",
        ),
        quiz=(
            QuizQuestion(
                "You take a loss and immediately double your size on the next trade. "
                "What is this?",
                ("Sound averaging", "Revenge trading",
                 "Scaling in", "Proper risk management"),
                1,
                "Increasing size to recover a loss is revenge trading, and it is the "
                "most reliable way to turn a bad day into a catastrophic one.",
            ),
            QuizQuestion(
                "Why is a high win rate not enough on its own?",
                ("It always means you are trading well",
                 "Small wins and large losses can still lose money overall",
                 "Win rate is irrelevant",
                 "Brokers penalise high win rates"),
                1,
                "If average losses are larger than average wins, a 70% win rate can "
                "still be a losing strategy.",
            ),
            QuizQuestion(
                "When should the trading plan be made?",
                ("During the trade, as things develop",
                 "After the first loss",
                 "Before the session, while calm",
                 "At the end of the day"),
                2,
                "Plans made under pressure are not plans. Decide before the open and "
                "execute during the session.",
            ),
        ),
    ),
    TrainingModule(
        key="expectancy",
        title="Measuring whether you actually have an edge",
        level=4,
        requires=("discipline", "risk_sizing"),
        teaching=(
            "Expectancy = (win rate x average win) - (loss rate x average loss). "
            "Positive expectancy is the only definition of an edge.",
            "Express results in R multiples -- profit divided by the amount you "
            "risked. A +2R trade is the same result whatever the position size.",
            "Twenty trades tells you almost nothing. A hundred starts to be "
            "informative. Judge the process over samples, not over sessions.",
            "Journal every trade with the setup, the plan and whether you followed "
            "it. A loss that followed the plan is a cost of business; a win that "
            "broke it is a problem.",
            "If expectancy is negative, trading more will lose money faster. Size "
            "up only after the process is demonstrably positive.",
        ),
        quiz=(
            QuizQuestion(
                "You win 40% of trades. Wins average +3R, losses average -1R. "
                "What is expectancy per trade?",
                ("-0.2R", "+0.2R", "+1.2R", "+2.0R"),
                1,
                "(0.4 x 3) - (0.6 x 1) = 1.2 - 0.6 = +0.2R per trade. A 40% win "
                "rate is highly profitable when wins are three times losses.",
            ),
            QuizQuestion(
                "What does an R multiple express?",
                ("Return as a percentage of the account",
                 "Profit divided by the amount risked",
                 "The number of shares traded",
                 "Risk relative to the market"),
                1,
                "R normalises every result against what was risked, so trades of "
                "different sizes are comparable.",
            ),
            QuizQuestion(
                "Your expectancy is negative. What should you do?",
                ("Trade bigger to recover faster",
                 "Trade more often to increase samples",
                 "Stop and fix the process before risking more",
                 "Switch to a more volatile stock"),
                2,
                "Negative expectancy means more trading loses money faster. The "
                "process has to change first.",
            ),
        ),
    ),
)

MODULES_BY_KEY = {module.key: module for module in CURRICULUM}


# ------------------------------------------------------------ journal review
@dataclass
class Finding:
    """One diagnosed habit, with the numbers from Caleb's own ledger."""

    code: str
    severity: str            # 'critical' | 'warning' | 'note' | 'good'
    headline: str
    evidence: str
    fix: str

    def describe(self) -> str:
        marker = {"critical": "!!", "warning": " !", "note": "  ", "good": " +"}[self.severity]
        return f"{marker} {self.headline}\n     {self.evidence}\n     -> {self.fix}"


@dataclass
class JournalReview:
    trades_reviewed: int
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None
    findings: list[Finding] = field(default_factory=list)

    def describe(self) -> str:
        if not self.trades_reviewed:
            return (
                "No closed round trips to review yet. Record some trades with "
                "`jarvis buy` and `jarvis sell` and I'll analyse them."
            )
        lines = [f"Reviewed {self.trades_reviewed} closed trades."]
        if self.win_rate is not None:
            lines.append(
                f"Win rate {self.win_rate * 100:.0f}% | "
                f"avg win ${self.avg_win:,.2f} | avg loss ${self.avg_loss:,.2f} | "
                f"expectancy ${self.expectancy:,.2f} per trade"
            )
        if self.expectancy is not None and self.expectancy < 0:
            lines.append(
                "Expectancy is negative -- as it stands, trading more loses money "
                "faster. Fix the process before adding size."
            )
        if self.findings:
            lines.append("")
            lines.extend(f.describe() for f in self.findings)
        return "\n".join(lines)


class TradingCoach:
    """Runs the curriculum and reviews the trade journal."""

    def __init__(self, training_store, risk_manager) -> None:
        self.training = training_store
        self.risk = risk_manager

    # ------------------------------------------------------------ curriculum
    def next_module(self) -> TrainingModule | None:
        """The next module whose prerequisites are all passed."""
        passed = self.training.passed_modules()
        for module in CURRICULUM:
            if module.key in passed:
                continue
            if all(req in passed for req in module.requires):
                return module
        return None

    def progress(self) -> dict[str, Any]:
        passed = self.training.passed_modules()
        return {
            "passed": len(passed),
            "total": len(CURRICULUM),
            "completed_modules": [MODULES_BY_KEY[k].title for k in passed if k in MODULES_BY_KEY],
            "next": (self.next_module().title if self.next_module() else None),
            "certified": len(passed) == len(CURRICULUM),
        }

    def grade_quiz(self, module: TrainingModule, answers: Sequence[int]) -> dict[str, Any]:
        """Mark a quiz attempt and record it."""
        correct = 0
        detail = []
        for question, given in zip(module.quiz, answers):
            hit = given == question.answer_index
            correct += int(hit)
            detail.append({
                "prompt": question.prompt,
                "correct": hit,
                "your_answer": (
                    question.options[given] if 0 <= given < len(question.options) else "(none)"
                ),
                "right_answer": question.answer,
                "explanation": question.explanation,
            })
        passed = correct >= module.pass_mark
        self.training.record_attempt(module.key, correct, len(module.quiz), passed)
        return {
            "module": module.key,
            "title": module.title,
            "score": correct,
            "out_of": len(module.quiz),
            "pass_mark": module.pass_mark,
            "passed": passed,
            "detail": detail,
        }

    # -------------------------------------------------------------- review
    def review_journal(self, *, limit: int = 100) -> JournalReview:
        """Diagnose habits from real closed trades."""
        trades = self.risk.closed_trades(limit=limit)
        if not trades:
            return JournalReview(0, None, None, None, None)

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        win_rate = len(wins) / len(trades)
        avg_win = mean(t["pnl"] for t in wins) if wins else 0.0
        avg_loss = abs(mean(t["pnl"] for t in losses)) if losses else 0.0
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

        findings: list[Finding] = []
        findings += self._check_win_loss_asymmetry(win_rate, avg_win, avg_loss, len(trades))
        findings += self._check_revenge_sizing(trades)
        findings += self._check_overnight_holds(trades)
        findings += self._check_midday_entries(trades)
        findings += self._check_overtrading(trades)
        findings += self._check_sample_size(len(trades))

        severity_order = {"critical": 0, "warning": 1, "note": 2, "good": 3}
        findings.sort(key=lambda f: severity_order[f.severity])

        return JournalReview(
            trades_reviewed=len(trades),
            win_rate=round(win_rate, 3),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            expectancy=round(expectancy, 2),
            findings=findings,
        )

    def _check_win_loss_asymmetry(
        self, win_rate: float, avg_win: float, avg_loss: float, n: int
    ) -> list[Finding]:
        if n < 5 or not avg_loss or not avg_win:
            return []
        ratio = avg_win / avg_loss
        if ratio < 0.8:
            return [Finding(
                code="cut_winners_hold_losers",
                severity="critical",
                headline="You are cutting winners and holding losers",
                evidence=(
                    f"Average win ${avg_win:,.2f} against an average loss of "
                    f"${avg_loss:,.2f} ({ratio:.2f}x). At a {win_rate * 100:.0f}% "
                    "win rate that is not sustainable."
                ),
                fix=(
                    "Set the target and stop before entry and let both work. A trade "
                    "closed early because it felt uncomfortable is not a plan."
                ),
            )]
        if ratio >= 1.8:
            return [Finding(
                code="good_asymmetry",
                severity="good",
                headline="Your winners are meaningfully bigger than your losers",
                evidence=f"Average win ${avg_win:,.2f} vs average loss ${avg_loss:,.2f} ({ratio:.2f}x).",
                fix="Keep doing exactly this -- it is what makes a sub-50% win rate profitable.",
            )]
        return []

    def _check_revenge_sizing(self, trades: Sequence[dict]) -> list[Finding]:
        """Position value jumping after a loss, within the same session.

        Scoped to a single session on purpose: sizing up the morning after a bad
        day is a (defensible) strategy decision, whereas sizing up ten minutes
        after a loss is the emotional reflex that empties accounts.
        """
        by_session: dict[str, list[dict]] = {}
        for trade in trades:
            by_session.setdefault(str(trade["opened_at"])[:10], []).append(trade)

        incidents = 0
        for session_trades in by_session.values():
            ordered = sorted(session_trades, key=lambda t: t["opened_at"])
            for previous, current in zip(ordered, ordered[1:]):
                if previous["pnl"] >= 0:
                    continue
                prior_value = previous["quantity"] * previous["entry"]
                next_value = current["quantity"] * current["entry"]
                if prior_value > 0 and next_value > prior_value * 1.5:
                    incidents += 1
        if incidents >= 2:
            return [Finding(
                code="revenge_sizing",
                severity="critical",
                headline="You size up after losing trades",
                evidence=(
                    f"{incidents} times you followed a loss with a position at least "
                    "50% larger than the one that just lost."
                ),
                fix=(
                    "Fix your risk per trade as a percentage of equity so size cannot "
                    "respond to the last result. Consider a hard stop for the day after "
                    "two consecutive losses."
                ),
            )]
        return []

    def _check_overnight_holds(self, trades: Sequence[dict]) -> list[Finding]:
        intended_day_trades = [t for t in trades if not t["same_session"]]
        if len(trades) >= 5 and len(intended_day_trades) / len(trades) > 0.4:
            return [Finding(
                code="overnight_drift",
                severity="warning",
                headline="Many positions are being carried overnight",
                evidence=(
                    f"{len(intended_day_trades)} of {len(trades)} closed trades were "
                    "held past the session they opened in."
                ),
                fix=(
                    "Decide before entry whether a trade is a day trade or a swing. "
                    "Holding a losing day trade overnight is usually avoidance, not a "
                    "change of thesis -- and it adds gap risk you never sized for."
                ),
            )]
        return []

    def _check_midday_entries(self, trades: Sequence[dict]) -> list[Finding]:
        midday = 0
        timed = 0
        for trade in trades:
            try:
                phase = phase_at(str(trade["opened_at"]))
            except ValueError:
                continue
            if phase.is_regular_hours:
                timed += 1
                if phase is Phase.MIDDAY:
                    midday += 1
        if timed >= 8 and midday / timed > 0.3:
            return [Finding(
                code="midday_entries",
                severity="warning",
                headline="A lot of your entries land in the midday chop",
                evidence=f"{midday} of {timed} timed entries fell between 12:00 and 14:00 ET.",
                fix=(
                    "Midday volume drains out and breakouts stop following through. "
                    "Take the break, and come back for the afternoon session."
                ),
            )]
        return []

    def _check_overtrading(self, trades: Sequence[dict]) -> list[Finding]:
        by_day: dict[str, int] = {}
        for trade in trades:
            by_day[str(trade["opened_at"])[:10]] = by_day.get(str(trade["opened_at"])[:10], 0) + 1
        if not by_day:
            return []
        busiest = max(by_day.values())
        average = mean(by_day.values())
        limit = self.risk.profile.max_trades_per_day
        if busiest > limit:
            return [Finding(
                code="overtrading",
                severity="warning",
                headline="Some sessions run well past your trade limit",
                evidence=(
                    f"Busiest session had {busiest} trades against your limit of "
                    f"{limit}; you average {average:.1f} per active day."
                ),
                fix=(
                    "More trades is not more edge -- it is more cost. Cap the count "
                    "and only take your defined setups."
                ),
            )]
        return []

    def _check_sample_size(self, n: int) -> list[Finding]:
        if n < 30:
            return [Finding(
                code="small_sample",
                severity="note",
                headline="Too few trades to judge the process yet",
                evidence=f"{n} closed trades. Below about 100 the numbers are mostly noise.",
                fix="Keep the sizing small and the journal complete while the sample builds.",
            )]
        return []
