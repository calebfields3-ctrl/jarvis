"""Jarvis itself: the orchestrator that remembers, watches, studies and answers.

Lifecycle:

    jarvis = Jarvis()          # loads memory, seeds foundations, vets sources
    print(jarvis.wake())       # greeting: name, portfolio, yesterday's P/L
    jarvis.scan_market()       # sweep the watchlist for patterns
    jarvis.learn_cycle()       # study curated teachers, grade past calls
    print(jarvis.ask("..."))   # conversation
    jarvis.sleep()             # close the session
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .config import Config, get_config
from .learning.coach import CURRICULUM, MODULES_BY_KEY, TradingCoach, TrainingModule
from .learning.curriculum import seed_foundations, seed_pattern_hypotheses
from .learning.distill import RuleBasedDistiller, ingest_lessons
from .learning.evaluate import EvaluationReport, OutcomeEvaluator
from .learning.news import NewsMonitor
from .learning.social import SocialStudy
from .learning.sources import SourceRegistry
from .learning.web import WebResearcher
from .learning.youtube import YouTubeStudy
from .market.intraday import IntradaySignal, scan_intraday
from .market.patterns import Detection, scan_bars
from .market.provider import SyntheticProvider, YahooProvider, build_provider
from .market.session import SessionClock
from .market.tradingview import TradingViewScanner, load_universe
from .memory.store import Memory, utcnow
from .persona import GreetingContext, Persona, build_persona
from .portfolio.risk import RiskManager, RiskProfile
from .portfolio.tracker import PortfolioSnapshot, PortfolioTracker
from .safety.url_safety import URLSafetyChecker

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    """What one sweep of the watchlist found."""

    symbols_watched: int = 0
    symbols_with_data: int = 0
    detections: list[tuple[str, Detection, float]] = field(default_factory=list)
    recorded: int = 0
    tradingview_snapshots: int = 0
    top_movers: list[tuple[str, float]] = field(default_factory=list)
    unusual_volume: list[tuple[str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def best(self, n: int = 5) -> list[tuple[str, Detection, float]]:
        return sorted(self.detections, key=lambda d: d[2], reverse=True)[:n]


@dataclass
class DayTradeReport:
    """One intraday sweep, plus whether Caleb is even clear to trade."""

    clock: SessionClock
    symbols_scanned: int = 0
    setups: list[tuple[str, IntradaySignal, float]] = field(default_factory=list)
    recorded: int = 0
    status: Any = None                       # DayTradeStatus
    notes: list[str] = field(default_factory=list)

    def best(self, n: int = 5) -> list[tuple[str, IntradaySignal, float]]:
        return sorted(self.setups, key=lambda s: s[2], reverse=True)[:n]


class Jarvis:
    def __init__(
        self,
        config: Config | None = None,
        *,
        provider=None,
        memory: Memory | None = None,
        offline: bool = False,
        persona: Persona | None = None,
    ) -> None:
        self.config = config or get_config()
        self.memory = memory or Memory(self.config.db_path)
        self.voice = persona or build_persona(
            self.config.persona,
            address=self.config.address,
            use_name=self.config.use_name,
        )
        self.provider = provider or (
            SyntheticProvider() if offline else build_provider(prefer_live=True)
        )
        self.portfolio = PortfolioTracker(self.memory.db)
        self.safety = URLSafetyChecker(
            safe_browsing_key=self.config.safe_browsing_key,
            require_safe_browsing=self.config.require_safe_browsing,
            timeout=self.config.http_timeout,
        )
        self.universe = load_universe(self.config.universe_file)
        self.scanner = TradingViewScanner(
            batch_size=self.config.scan_batch_size,
            max_workers=max(2, self.config.scan_concurrency // 3),
            timeout=self.config.http_timeout,
        )
        self.distiller = RuleBasedDistiller()
        self.registry = SourceRegistry(self.memory.knowledge)
        self.youtube = YouTubeStudy(self.config.youtube_api_key, timeout=self.config.http_timeout)
        self.social = SocialStudy(self.config.home / "inbox")
        self.web = WebResearcher(
            self.safety,
            cse_key=self.config.google_cse_key,
            cse_id=self.config.google_cse_id,
            timeout=self.config.http_timeout,
        )
        self.news = NewsMonitor.from_file(
            self.config.news_feeds_file, self.safety, universe=self.universe,
            timeout=self.config.http_timeout,
        )
        self.evaluator = OutcomeEvaluator(
            self.provider,
            self.memory.signals,
            self.memory.knowledge,
            promotion_samples=self.config.lesson_promotion_samples,
            promotion_confidence=self.config.lesson_promotion_confidence,
        )
        self.risk = RiskManager(self.memory.db, RiskProfile())
        self.coach = TradingCoach(self.memory.training, self.risk)
        self.session_id: int | None = None
        self._bootstrap()

    # ------------------------------------------------------------ bootstrap
    def _bootstrap(self) -> None:
        """First-run setup. Cheap and idempotent on every later run."""
        profile = self.memory.profile
        if not profile.get("owner_name"):
            profile.name = self.config.owner_name
        if profile.get("foundations_seeded") != "1":
            seed_foundations(self.memory.knowledge, self.config.seed_knowledge_file)
            seed_pattern_hypotheses(self.memory.knowledge)
            profile.set("foundations_seeded", "1")
            profile.set("first_boot", utcnow().isoformat())
        # Re-vet sources every boot so edits to the curated file take effect.
        try:
            counts = self.registry.sync(self.config.curated_sources_file)
            log.info("source vetting: %s", counts)
        except Exception as exc:
            log.warning("source sync failed: %s", exc)

    # ---------------------------------------------------------------- wake
    def wake(self, *, channel: str = "text") -> str:
        """The 'hey Jarvis' response: greet Caleb, then brief him."""
        ctx = self._greeting_context()
        self.session_id = self.memory.sessions.start(channel=channel)

        lines = [self.voice.greeting_open(ctx)]
        opener = self.voice.greeting_gap(ctx)
        if opener:
            lines.append(opener)

        lines.append("")
        lines.extend(self.portfolio_summary().splitlines())

        headlines = self.memory.news.recent(limit=3, min_impact=0.6)
        if headlines:
            lines.append("")
            lines.append(self.voice.news_header())
            for item in headlines:
                tickers = f" [{item['tickers']}]" if item["tickers"] else ""
                lines.append(f"  - {item['headline']} ({item['source']}){tickers}")

        triggered = self.check_watches()
        if triggered:
            lines.append("")
            lines.extend(triggered)

        lines.append("")
        lines.append(self.voice.greeting_expertise(ctx))

        greeting = "\n".join(lines)
        self.memory.sessions.record(self.session_id, "jarvis", greeting)
        return greeting

    # ------------------------------------------------------ summoned by voice
    def first_time_today(self) -> bool:
        """Whether this is the first time Caleb has summoned him today.

        Uses the market's day, not UTC's. Summoning him at 8pm Eastern and
        again at 1am should be one day and two greetings, not the reverse.
        """
        return self.memory.profile.get("last_briefing_day") != self._market_day()

    def summoned(self) -> str:
        """What he says when the wake word pulls the window up.

        The first summon of the day gets the full briefing -- portfolio, what
        moved overnight, what he's watching, whether Caleb can trade. Every
        summon after that gets one line, because repeating the whole briefing
        every time someone says his name is how a useful thing becomes noise.
        """
        # Being summoned by voice is how a session begins now -- `wake` is
        # never called on that path, and without this nothing he says all day
        # would be recorded.
        if self.session_id is None:
            self.session_id = self.memory.sessions.start(channel="voice")

        name = self.spoken_name
        study = self._study_report()

        if not self.first_time_today():
            line = self.voice.summoned(name)
            # What he learned while out is worth saying even on a short
            # greeting -- it's the answer to "did you actually do it", and
            # sitting on it until tomorrow morning makes it worthless.
            if study:
                line = f"{line}\n\n{study}"
            self.memory.sessions.record(self.session_id, "jarvis", line)
            return line

        self.memory.profile.set("last_briefing_day", self._market_day())
        lines = [self.voice.greeting_open(self._greeting_context()), ""]
        if study:
            lines.extend([study, ""])
        try:
            lines.extend(self.premarket_brief().splitlines())
        except Exception as exc:
            log.warning("the daily briefing failed: %s", exc)
            lines.extend(self.portfolio_summary().splitlines())

        briefing = "\n".join(lines)
        self.memory.sessions.record(self.session_id, "jarvis", briefing)
        return briefing

    def _study_report(self) -> str:
        """What the background studying found, said once and then forgotten.

        Cleared after reporting so the same session isn't announced every
        time he's spoken to for the rest of the week.
        """
        from .learning.deepstudy import clear_progress, describe_progress, load_progress

        progress = load_progress(self.memory)
        if progress is None or not progress.cycles:
            return ""
        clear_progress(self.memory)
        return describe_progress(progress)

    @staticmethod
    def _market_day() -> str:
        from .portfolio.tracker import market_now

        return market_now().date().isoformat()

    @property
    def spoken_name(self) -> str:
        """His name as it should be *said*, which is not always how it's spelt.

        Text-to-speech gets plenty of real names wrong. ``JARVIS_SPOKEN_NAME``
        takes a phonetic respelling, so the name on screen stays correct while
        the one out of the speaker sounds right.
        """
        return self.config.spoken_name or self.memory.profile.name

    def _greeting_context(self) -> GreetingContext:
        previous = self.memory.sessions.last_session()
        level, score = self.memory.knowledge.expertise_level()
        stats = self.memory.knowledge.stats()
        return GreetingContext(
            name=self.spoken_name,
            hour=datetime.now().hour,
            first_session=not (previous and previous.get("started_at")),
            gap=(
                self._humanise_gap(previous["started_at"])
                if previous and previous.get("started_at") else None
            ),
            expertise_label=level,
            expertise_score=score,
            lessons=stats["total"],
            graded=stats["graded_predictions"],
        )

    def sleep(self, summary: str | None = None) -> None:
        if self.session_id is not None:
            self.memory.sessions.end(self.session_id, summary)
            self.session_id = None

    # ----------------------------------------------------------- portfolio
    def portfolio_summary(self) -> str:
        snap = self.snapshot()
        if not snap.positions and snap.cash == 0:
            return self.voice.portfolio_empty()

        lines = [self.voice.portfolio_headline(snap.total_value, len(snap.positions))]

        if snap.day_pnl is None:
            lines.append(self.voice.no_prior_close())
        else:
            lines.append(
                self.voice.day_pnl(snap.day_pnl, snap.day_pnl_pct, snap.prior_date)
            )

        best, worst = snap.best(), snap.worst()
        if best is not None and best.quantity:
            lines.append(
                self.voice.best_position(
                    best.symbol, best.unrealized_pct, best.unrealized_pnl
                )
            )
        if worst is not None and worst is not best:
            lines.append(
                self.voice.worst_position(
                    worst.symbol, worst.unrealized_pct, worst.unrealized_pnl
                )
            )

        perf = self.portfolio.performance(30)
        if perf.get("change") is not None:
            lines.append(
                self.voice.performance(
                    perf["days"], perf["change"], perf["change_pct"],
                    perf["win_days"], perf["loss_days"],
                )
            )

        if snap.stale_prices:
            lines.append(self.voice.stale_prices(snap.stale_prices[:5]))
        return "\n".join(lines)

    def snapshot(self) -> PortfolioSnapshot:
        return self.portfolio.snapshot(price_lookup=lambda syms: self.provider.prices(syms))

    def mark_close(self) -> PortfolioSnapshot:
        """Record today's close so tomorrow's greeting has a day-over-day number."""
        snap = self.snapshot()
        self.portfolio.close_day(snap)
        return snap

    # ---------------------------------------------------------------- scan
    def scan_market(self, symbols: Sequence[str] | None = None) -> ScanReport:
        """Sweep the watchlist: TradingView for live state, local engine for patterns."""
        # `symbols is None` means "use the watchlist"; an explicitly empty list
        # means exactly that, and must not silently expand to 677 symbols.
        watchlist = list(self.universe if symbols is None else symbols)
        report = ScanReport(symbols_watched=len(watchlist))
        if not watchlist:
            report.notes.append(
                f"Watchlist is empty -- expected tickers in {self.config.universe_file}"
            )
            return report
        if len(watchlist) < self.config.min_universe_size:
            report.notes.append(
                f"Watching {len(watchlist)} symbols, below the {self.config.min_universe_size} target."
            )

        # 1. TradingView's own view of every chart, batched concurrently.
        snapshots = self.scanner.scan(watchlist, interval="1d")
        report.tradingview_snapshots = len(snapshots)
        if snapshots:
            gainers, losers = self.scanner.movers(snapshots, top=5)
            report.top_movers = [(s.symbol, s.change_pct or 0) for s in gainers + losers]
            report.unusual_volume = [
                (s.symbol, s.relative_volume or 0)
                for s in self.scanner.unusual_volume(snapshots)[:10]
            ]
        elif self.scanner.last_error:
            report.notes.append(f"TradingView scanner unavailable: {self.scanner.last_error}")

        # 2. Own pattern engine over OHLCV -- this is what gets graded later.
        histories = self.provider.bulk_bars(watchlist, period="1y", interval="1d")
        report.symbols_with_data = len(histories)

        for symbol, bars in histories.items():
            if len(bars) < 60:
                continue
            for detection in scan_bars(bars):
                lesson = self.memory.knowledge.lesson_for_pattern(detection.pattern_key)
                # Conviction = what the detector saw, weighted by what this
                # pattern has actually been worth historically.
                confidence = round(
                    detection.strength * (0.4 + 0.6 * (lesson.confidence if lesson else 0.5)), 3
                )
                snap = snapshots.get(symbol)
                if snap and snap.recommendation in {"strong_buy", "buy"} and detection.direction == "long":
                    confidence = min(0.98, confidence + 0.05)
                elif snap and snap.recommendation in {"strong_sell", "sell"} and detection.direction == "short":
                    confidence = min(0.98, confidence + 0.05)

                report.detections.append((symbol, detection, confidence))
                signal_id = self.memory.signals.record(
                    symbol=symbol,
                    pattern_key=detection.pattern_key,
                    direction=detection.direction,
                    price=bars[-1].close,
                    horizon_bars=detection.horizon_bars,
                    features=detection.features,
                    lesson_id=lesson.id if lesson else None,
                    confidence=confidence,
                    detected_at=f"{bars[-1].ts} 00:00:00",
                )
                if signal_id:
                    report.recorded += 1
        return report

    def scan_briefing(self, report: ScanReport | None = None, *, top: int = 6) -> str:
        report = report or self.scan_market()
        lines = [
            self.voice.scan_summary(
                report.symbols_watched, report.symbols_with_data, len(report.detections)
            )
        ]
        best = report.best(top)
        if best:
            lines.append("")
            lines.append(self.voice.setups_header())
            for symbol, detection, confidence in best:
                lesson = self.memory.knowledge.lesson_for_pattern(detection.pattern_key)
                track = ""
                if lesson:
                    samples = lesson.support + lesson.refute
                    if samples:
                        rate = lesson.support / samples * 100
                        track = f" [{rate:.0f}% over {samples} graded]"
                    else:
                        track = f" [{self.voice.untested_marker()}]"
                lines.append(
                    f"  {symbol:<6} {detection.direction.upper():<5} "
                    f"{detection.pattern_key:<24} conf {confidence:.2f}{track}"
                )
                lines.append(f"         {detection.description}")
        if report.top_movers:
            movers = ", ".join(f"{s} {c:+.1f}%" for s, c in report.top_movers[:6])
            lines.append(f"\nBiggest movers: {movers}")
        if report.unusual_volume:
            vols = ", ".join(f"{s} {v:.1f}x" for s, v in report.unusual_volume[:5])
            lines.append(f"Unusual volume: {vols}")
        for note in report.notes:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    # -------------------------------------------------------------- watches
    def add_watch(
        self, symbol: str, level: float, direction: str, note: str | None = None
    ) -> str:
        """Standing instruction: tell me when this crosses that level."""
        symbol = symbol.upper()
        self.memory.watches.add(symbol, level, direction, note)
        return self.voice.watch_added(symbol, level, direction)

    def check_watches(self) -> list[str]:
        """Fire any watch whose level has been crossed. Each fires once."""
        active = self.memory.watches.active()
        if not active:
            return []
        symbols = sorted({w["symbol"] for w in active})
        try:
            prices = dict(self.provider.prices(symbols))
        except Exception as exc:
            log.debug("watch price lookup failed: %s", exc)
            return []
        return [
            self.voice.watch_triggered(
                watch["symbol"], watch["level"], watch["direction"], price
            )
            for watch, price in self.memory.watches.check(prices)
        ]

    def list_watches(self) -> str:
        watches = self.memory.watches.all()
        if not watches:
            return "Nothing on watch. Add one with `jarvis watch XOM below 105`."
        lines = []
        for watch in watches:
            if watch["active"]:
                lines.append(
                    f"  [watching] {watch['symbol']:<6} {watch['direction']:<5} "
                    f"{watch['level']:.2f}"
                    + (f"  -- {watch['note']}" if watch["note"] else "")
                )
            else:
                fired = (
                    f" at {watch['triggered_price']:.2f} on {watch['triggered_at'][:10]}"
                    if watch["triggered_at"] else " (cancelled)"
                )
                lines.append(
                    f"  [done]     {watch['symbol']:<6} {watch['direction']:<5} "
                    f"{watch['level']:.2f}{fired}"
                )
        return "\n".join(lines)

    # -------------------------------------------------------- premarket brief
    def premarket_brief(self, *, clock: SessionClock | None = None) -> str:
        """Everything worth knowing before the bell, in one read.

        Deliberately ordered by what can hurt you first: your open risk, then
        the news that could reprice it, then what you asked to be told, and
        only then the opportunities.
        """
        clock = clock or SessionClock.live()
        lines = [clock.describe(), ""]

        snap = self.snapshot()
        lines.extend(self.portfolio_summary().splitlines())

        status = self.risk.status(snap.total_value)
        remaining = status.day_trades_remaining
        if remaining is not None:
            lines.append("")
            lines.append(
                f"Day trades available today: {remaining} of 3 "
                f"(equity {self.voice.money(status.equity)}, below the "
                "$25,000 PDT minimum)."
            )
        if not status.can_trade:
            lines.append("")
            lines.append(status.describe())

        headlines = self.memory.news.recent(limit=5, min_impact=0.5)
        if headlines:
            lines.append("")
            lines.append(self.voice.news_header())
            for item in headlines:
                tickers = f" [{item['tickers']}]" if item["tickers"] else ""
                lines.append(f"  - {item['headline']} ({item['source']}){tickers}")

        held = {p.symbol for p in snap.positions}
        exposed = [
            item for item in self.memory.news.recent(limit=40, min_impact=0.3)
            if held & {t for t in (item["tickers"] or "").split(",") if t}
        ][:3]
        if exposed:
            lines.append("")
            lines.append("News touching positions you actually hold:")
            for item in exposed:
                lines.append(f"  - {item['headline']} [{item['tickers']}]")

        watching = self.memory.watches.active()
        if watching:
            lines.append("")
            lines.append("Still on watch:")
            for watch in watching:
                lines.append(
                    f"  {watch['symbol']} {watch['direction']} {watch['level']:.2f}"
                )

        triggered = self.check_watches()
        if triggered:
            lines.append("")
            lines.extend(triggered)
        return "\n".join(lines)

    # ----------------------------------------------------------- day trading
    def daytrade_scan(
        self,
        symbols: Sequence[str] | None = None,
        *,
        interval: str = "5m",
        clock: SessionClock | None = None,
        max_symbols: int = 120,
    ) -> DayTradeReport:
        """Sweep for intraday setups and check Caleb is clear to take one.

        Deliberately narrower than the daily scan: intraday data is far heavier
        per symbol, and a day trader watching 600 names is not watching any of
        them. Defaults to the most liquid slice of the watchlist.
        """
        clock = clock or SessionClock.live()
        watchlist = list(self.universe if symbols is None else symbols)[:max_symbols]
        report = DayTradeReport(clock=clock, symbols_scanned=len(watchlist))

        equity = self.snapshot().total_value
        report.status = self.risk.status(equity)
        report.notes.append(clock.describe())

        if not clock.phase.is_regular_hours:
            report.notes.append(
                "Outside regular hours -- intraday setups below are from the last "
                "session and are not live entries."
            )
        elif not clock.can_open_new_trades:
            report.notes.append(
                "Too close to the bell (or midday) to open a new day trade."
            )

        if not watchlist:
            report.notes.append("No symbols to scan.")
            return report

        histories = self.provider.bulk_bars(watchlist, period="1mo", interval=interval)
        for symbol, bars in histories.items():
            if len(bars) < 30:
                continue
            for signal in scan_intraday(bars):
                lesson = self.memory.knowledge.lesson_for_pattern(signal.pattern_key)
                confidence = round(
                    signal.strength * (0.4 + 0.6 * (lesson.confidence if lesson else 0.5)), 3
                )
                report.setups.append((symbol, signal, confidence))
                # Horizon in bars-to-close, so the call is graded on the same
                # session it was taken in -- a day trade that needs three days
                # to work was not a day trade.
                bars_left = max(1, int((signal.features.get("minutes_left", 60)) // 5) or 12)
                signal_id = self.memory.signals.record(
                    symbol=symbol,
                    pattern_key=signal.pattern_key,
                    direction=signal.direction,
                    price=signal.entry,
                    timeframe=interval,
                    horizon_bars=bars_left,
                    features={**signal.features, "stop": signal.stop,
                              "target": signal.target, "phase": signal.phase.value},
                    lesson_id=lesson.id if lesson else None,
                    confidence=confidence,
                    detected_at=bars[-1].ts,
                )
                if signal_id:
                    report.recorded += 1
        return report

    def daytrade_briefing(
        self, report: DayTradeReport | None = None, *, top: int = 5
    ) -> str:
        report = report or self.daytrade_scan()
        equity = report.status.equity if report.status else 0.0
        lines = [report.clock.describe(), ""]

        if report.status is not None:
            lines.append(report.status.describe())
            lines.append("")
            if not report.status.can_trade:
                lines.append(self.voice.blocked_preamble())
                return "\n".join(lines)

        lines.append(
            f"Scanned {report.symbols_scanned} charts intraday; "
            f"{len(report.setups)} actionable setups."
        )
        best = report.best(top)
        if not best:
            lines.append(self.voice.no_setups())
        for symbol, signal, confidence in best:
            lesson = self.memory.knowledge.lesson_for_pattern(signal.pattern_key)
            samples = (lesson.support + lesson.refute) if lesson else 0
            track = (
                f"{lesson.support / samples * 100:.0f}% over {samples} graded"
                if lesson and samples else self.voice.untested_marker()
            )
            plan = self.risk.plan_trade(
                symbol, signal.direction, signal.entry, signal.stop, signal.target,
                equity=equity,
            )
            lines.append("")
            lines.append(
                f"  {symbol} {signal.direction.upper()} -- {signal.pattern_key} "
                f"(conf {confidence:.2f}, {track})"
            )
            lines.append(f"    {signal.description}")
            lines.append(f"    {plan.describe().splitlines()[0] if plan.is_viable else ''}".rstrip())
            for line in plan.describe().splitlines()[1:]:
                lines.append(f"    {line.strip()}")
            if not plan.is_viable:
                lines.append(f"    no trade: {plan.rejected_reason}")
        return "\n".join(lines)

    def plan_trade(
        self, symbol: str, direction: str, entry: float, stop: float, target: float
    ):
        """Size a trade Caleb is considering, against live account equity."""
        return self.risk.plan_trade(
            symbol, direction, entry, stop, target, equity=self.snapshot().total_value
        )

    def trading_status(self):
        return self.risk.status(self.snapshot().total_value)

    # -------------------------------------------------------------- coaching
    def next_lesson(self) -> TrainingModule | None:
        return self.coach.next_module()

    def teach(self, module: TrainingModule | None = None) -> str:
        """Present the next module's teaching, then its quiz."""
        module = module or self.coach.next_module()
        if module is None:
            return (
                "You've passed every module in the curriculum. From here the "
                "training is your own journal -- run `jarvis review`."
            )
        lines = [f"Module {module.level}: {module.title}", ""]
        lines.extend(f"  - {point}" for point in module.teaching)
        lines.append("")
        lines.append(f"Quiz ({len(module.quiz)} questions, {module.pass_mark} to pass):")
        for index, question in enumerate(module.quiz, 1):
            lines.append(f"  {index}. {question.prompt}")
            for choice, option in enumerate(question.options):
                lines.append(f"       {chr(97 + choice)}) {option}")
        lines.append("")
        lines.append(f"Answer with: jarvis quiz {module.key} a b c")
        return "\n".join(lines)

    def submit_quiz(self, module_key: str, answers: Sequence[str]) -> str:
        module = MODULES_BY_KEY.get(module_key)
        if module is None:
            known = ", ".join(m.key for m in CURRICULUM)
            return f"No module called {module_key!r}. Available: {known}"
        indices = [
            (ord(a.strip().lower()[0]) - 97) if a.strip() else -1 for a in answers
        ]
        result = self.coach.grade_quiz(module, indices)

        lines = [
            f"{result['title']}: {result['score']}/{result['out_of']} "
            f"({'PASSED' if result['passed'] else 'not passed'}, "
            f"need {result['pass_mark']})",
            "",
        ]
        for item in result["detail"]:
            mark = "correct" if item["correct"] else "wrong"
            lines.append(f"  [{mark}] {item['prompt']}")
            if not item["correct"]:
                lines.append(f"      you said: {item['your_answer']}")
                lines.append(f"      answer:   {item['right_answer']}")
            lines.append(f"      {item['explanation']}")
        progress = self.coach.progress()
        lines.append("")
        lines.append(f"Progress: {progress['passed']}/{progress['total']} modules passed.")
        if progress["next"]:
            lines.append(f"Next up: {progress['next']}")
        return "\n".join(lines)

    def review_trades(self, limit: int = 100) -> str:
        return self.coach.review_journal(limit=limit).describe()

    # --------------------------------------------------------------- learn
    def learn_cycle(self, *, study_web: bool = True) -> dict[str, Any]:
        """One full pass of studying, distilling and grading."""
        result: dict[str, Any] = {}

        result["youtube"] = self.youtube.study_sources(self.memory.knowledge)
        result["social"] = self.social.study_sources(self.memory.knowledge)

        if study_web:
            topics = self._research_topics()
            web_stats = {"queries": 0, "pages_read": 0, "links_blocked": 0, "errors": []}
            for topic in topics:
                report = self.web.study(self.memory.knowledge, topic, max_pages=2)
                web_stats["queries"] += 1
                web_stats["pages_read"] += len(report.opened)
                web_stats["links_blocked"] += len(report.blocked)
                if report.error:
                    web_stats["errors"].append(report.error)
            result["web"] = web_stats

        result["distilled"] = self.distill_pending()
        result["news"] = self.news.monitor(self.memory.news)

        evaluation = self.evaluator.evaluate_open_signals()
        result["evaluation"] = {
            "graded": len(evaluation.graded),
            "hit_rate": round(evaluation.hit_rate, 3),
            "promoted": evaluation.promoted,
            "retired": evaluation.retired,
            "summary": evaluation.summary(),
        }
        result["expertise"] = self.memory.knowledge.expertise_level()
        return result

    def distill_pending(self, limit: int = 25) -> dict[str, int]:
        """Turn everything studied but not yet understood into lessons."""
        stats = {"items": 0, "lessons": 0}
        for item in self.memory.knowledge.unprocessed_content(limit):
            lessons = self.distiller.distill(item.get("body") or "", title=item.get("title"))
            source = (
                self.memory.knowledge.get_source(item["source_id"])
                if item.get("source_id") else None
            )
            written = ingest_lessons(
                self.memory.knowledge,
                lessons,
                source_id=item.get("source_id"),
                content_id=item["id"],
                source_credibility=float(source["credibility"]) if source else 0.4,
            )
            self.memory.knowledge.mark_processed(item["id"])
            stats["items"] += 1
            stats["lessons"] += written
        return stats

    def bootstrap_expertise(
        self, *, symbols: Sequence[str] | None = None, sample: int = 60, period: str = "2y"
    ) -> dict[str, Any]:
        """Replay history so the pattern hypotheses get a real track record fast.

        Without this Jarvis needs months of live scanning before any pattern has
        enough graded samples to be trusted. This walks the detectors forward
        through past bars -- never showing them a future bar -- and grades every
        signal on what followed.
        """
        pool = list(symbols or self.universe)
        if not pool:
            return {"error": "watchlist is empty"}
        chosen = pool if len(pool) <= sample else random.Random(11).sample(pool, sample)
        recorded = self.evaluator.backfill_from_history(chosen, scan_bars, period=period)
        report = self.evaluator.evaluate_open_signals(limit=100_000)
        return {
            "symbols_replayed": len(chosen),
            "signals_recorded": recorded,
            "signals_graded": len(report.graded),
            "hit_rate": round(report.hit_rate, 3),
            "promoted": sorted(set(report.promoted)),
            "retired": sorted(set(report.retired)),
            "expertise": self.memory.knowledge.expertise_level(),
        }

    def what_i_know(self, *, top: int = 12) -> str:
        stats = self.memory.knowledge.stats()
        level, score = self.memory.knowledge.expertise_level()
        lines = [
            f"Expertise: {level} ({score:.2f}).",
            f"{stats['total']} lessons -- " + ", ".join(
                f"{n} {tier}" for tier, n in sorted(stats["by_tier"].items())
            ),
            f"{stats['sources']} verified teachers, {stats['content_studied']} pieces studied, "
            f"{stats['graded_predictions']} predictions graded.",
        ]
        board = self.evaluator.pattern_scoreboard(min_samples=5)
        if board:
            lines.append("")
            lines.append("Measured pattern performance (walk-forward):")
            for row in board[:top]:
                lines.append(
                    f"  {row['pattern']:<24} {row['hit_rate'] * 100:5.1f}% over "
                    f"{row['samples']:>4} samples, edge {row['avg_edge_pct']:+.2f}% "
                    f"[{row['tier']}]"
                )
        else:
            lines.append(
                "\nNo pattern has enough graded samples yet. "
                "Run `jarvis bootstrap` to replay history and build a track record."
            )
        proven = self.memory.knowledge.lessons(tier="expertise", limit=5)
        if proven:
            lines.append("")
            lines.append("What I now consider proven:")
            for lesson in proven:
                lines.append(f"  - {lesson.claim} (conf {lesson.confidence:.2f})")
        return "\n".join(lines)

    def _research_topics(self) -> list[str]:
        """Pick what to go read about: weakest patterns first, then holdings."""
        topics: list[str] = []
        weak = [
            l for l in self.memory.knowledge.lessons(tier="hypothesis", limit=30)
            if l.pattern_key
        ]
        for lesson in weak[:2]:
            topics.append(f"{lesson.pattern_key.replace('_', ' ')} trading pattern reliability")
        for pos in self.portfolio.positions()[:2]:
            topics.append(f"{pos.symbol} stock analysis outlook")
        return topics or ["stock market technical analysis fundamentals"]

    # ----------------------------------------------------------------- ask
    def ask(self, question: str) -> str:
        """Conversational entry point. Routes intent, then answers from memory."""
        if self.session_id is None:
            self.session_id = self.memory.sessions.start()
        self.memory.sessions.record(self.session_id, "owner", question)
        answer = self._route(question.strip())
        self.memory.sessions.record(self.session_id, "jarvis", answer)
        return answer

    def _route(self, q: str) -> str:
        lowered = q.lower()

        # Setting the name has to be checked before asking for it, or
        # "call me Cal" is answered with the old name instead of applied.
        if lowered.startswith("my name is ") or lowered.startswith("call me "):
            name = q.split(" is ", 1)[-1] if " is " in lowered else q[len("call me "):]
            name = name.strip().rstrip(".")
            if name:
                self.memory.profile.name = name
                return self.voice.name_confirmed(name)

        if any(w in lowered for w in ("my name", "who am i", "call me")):
            return self.voice.name_recalled(self.memory.profile.name)

        if any(w in lowered for w in ("portfolio", "how am i doing", "p&l", "pnl", "p/l", "positions")):
            return self.portfolio_summary()

        if any(w in lowered for w in ("yesterday", "previous day", "last night", "overnight")):
            snap = self.snapshot()
            if snap.day_pnl is None:
                return (
                    "I don't have a prior close on record yet. Run `jarvis mark-close` at "
                    "the end of a session and I'll have the day-over-day number next time."
                )
            direction = "up" if snap.day_pnl >= 0 else "down"
            return (
                f"Against the {snap.prior_date} close you're {direction} "
                f"${abs(snap.day_pnl):,.2f} ({snap.day_pnl_pct:+.2f}%)."
            )

        # Self-status, but only when the question is about Jarvis itself.
        # "What do you know about breakouts" is a topic question, not this one.
        self_referential = any(
            w in lowered for w in ("what do you know", "how smart", "expertise", "learned")
        )
        if self_referential and " about " not in lowered:
            return self.what_i_know()

        if any(w in lowered for w in ("news", "headline", "happening")):
            items = self.memory.news.recent(limit=6, min_impact=0.4)
            if not items:
                return "Nothing material in the feeds yet. Run `jarvis news` to poll now."
            return "\n".join(
                f"[{i['impact']:.2f}] {i['headline']} -- {i['source']}" for i in items
            )

        if any(
            w in lowered
            for w in ("day trade", "daytrade", "day trading", "intraday", "scalp")
        ):
            if any(w in lowered for w in ("teach", "learn", "train", "how do i", "show me how")):
                return self.teach()
            return self.daytrade_briefing()

        watch_request = self._parse_watch_request(q)
        if watch_request:
            return self.add_watch(*watch_request)

        if any(w in lowered for w in ("what are you watching", "my watches", "on watch")):
            return self.list_watches()

        if any(w in lowered for w in ("brief me", "pre-market", "premarket", "before the open")):
            return self.premarket_brief()

        if any(w in lowered for w in ("can i trade", "am i allowed", "pdt", "pattern day")):
            return self.trading_status().describe()

        if any(w in lowered for w in ("review my trades", "how am i trading", "my mistakes", "journal")):
            return self.review_trades()

        if any(w in lowered for w in ("teach me", "train me", "next lesson", "quiz me")):
            return self.teach()

        if any(w in lowered for w in ("scan", "setups", "signals", "watchlist", "opportunit")):
            recent = self.memory.signals.recent(limit=8, min_confidence=0.5)
            if not recent:
                return "No recent high-conviction setups on file. Run `jarvis scan` for a fresh sweep."
            lines = ["Most recent high-conviction setups:"]
            for s in recent:
                lines.append(
                    f"  {s['symbol']:<6} {s['direction'].upper():<5} {s['pattern_key']:<24} "
                    f"conf {s['confidence']:.2f} @ {s['price_at_signal']:.2f} ({s['detected_at'][:10]})"
                )
            return "\n".join(lines)

        symbol = self._symbol_in(q)
        if symbol:
            return self.brief_symbol(symbol)

        return self._answer_from_knowledge(q)

    # English words that are also real tickers. Without this, "what is a stock"
    # resolves to A (Agilent) and every plain question becomes a chart request.
    _TICKER_HOMOGRAPHS = frozenset(
        {
            "A", "ALL", "AN", "ARE", "AS", "AT", "BE", "BIG", "BY", "CAN", "CAR",
            "DD", "DO", "F", "FOR", "GO", "HAS", "HE", "IT", "KEY", "LOW", "NOW",
            "ON", "ONE", "OR", "OUT", "SEE", "SO", "TO", "UP", "US", "WE", "PM",
            "AM", "IS", "IN", "OF", "MY", "ME", "NO", "IF", "TWO", "NEW", "OLD",
            "RUN", "BUY", "PAY", "WIN", "GAP", "MAX", "MIN", "NET", "WAY", "DAY",
        }
    )

    def _symbol_in(self, q: str) -> str | None:
        """Find a ticker in a question, without hijacking ordinary English."""
        universe = {s.upper() for s in self.universe}
        for raw in q.replace("?", " ").split():
            token = raw.strip(".,!:;()\"'")
            if not token:
                continue
            # A cashtag is unambiguous -- always honour it.
            if token.startswith("$"):
                candidate = token[1:].upper()
                if candidate in universe:
                    return candidate
                continue
            candidate = token.upper()
            if candidate not in universe:
                continue
            # Typed in caps: the user meant the ticker.
            if token.isupper():
                return candidate
            # Typed in lower case: only accept it if it can't be a normal word.
            if len(candidate) >= 3 and candidate not in self._TICKER_HOMOGRAPHS:
                return candidate
        return None

    def brief_symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        bars = self.provider.bars(symbol, "1y", "1d")
        lines = [f"{symbol}:"]
        if bars:
            last = bars[-1]
            change = (
                (last.close - bars[-2].close) / bars[-2].close * 100 if len(bars) > 1 else 0
            )
            lines.append(f"  Last {last.close:.2f} ({change:+.2f}% on the session).")
            detections = scan_bars(bars)
            if detections:
                lines.append("  Setups firing right now:")
                for d in detections:
                    lesson = self.memory.knowledge.lesson_for_pattern(d.pattern_key)
                    samples = (lesson.support + lesson.refute) if lesson else 0
                    track = (
                        f" -- {lesson.support / samples * 100:.0f}% over {samples} graded"
                        if lesson and samples else " -- untested"
                    )
                    lines.append(f"    {d.direction.upper()} {d.pattern_key}: {d.description}{track}")
            else:
                lines.append("  No pattern is firing on the daily right now.")
        else:
            lines.append("  No price history available.")

        notes = getattr(self.provider, "fundamental_notes", lambda s: [])(symbol)
        if notes:
            lines.append("  Fundamentals:")
            lines.extend(f"    - {n}" for n in notes)

        holding = next((p for p in self.portfolio.positions() if p.symbol == symbol), None)
        if holding and holding.quantity:
            # positions() returns the ledger view with no price attached; mark it
            # to the last bar so the open P/L isn't reported as a flat zero.
            if bars:
                holding.last_price = bars[-1].close
            open_pl = (
                f" ({holding.unrealized_pct:+.1f}% open)" if holding.last_price else ""
            )
            lines.append(
                f"  You hold {holding.quantity:g} at {holding.avg_cost:.2f}{open_pl}."
            )
        related = [
            n for n in self.memory.news.recent(limit=25, min_impact=0.3)
            if symbol in (n["tickers"] or "")
        ][:3]
        if related:
            lines.append("  Recent news:")
            lines.extend(f"    - {n['headline']} ({n['source']})" for n in related)

        lines.append(f"  {self.voice.disclaimer()}")
        return "\n".join(lines)

    # Question scaffolding that matches almost every lesson and so tells us
    # nothing about what was actually asked.
    _STOPWORDS = frozenset(
        {
            "what", "when", "where", "which", "whats", "does", "doesnt", "know",
            "about", "tell", "explain", "should", "would", "could", "there",
            "this", "that", "with", "from", "have", "will", "your", "youre",
            "mean", "means", "just", "really", "again", "much", "many", "into",
            "some", "them", "they", "then", "than", "like", "want", "need",
            # three-letter fillers -- the minimum term length is 3 so that real
            # jargon like "ETF", "P/E" and "RSI" survives.
            "the", "and", "for", "are", "was", "you", "how", "its", "did", "can",
            "who", "why", "get", "got", "has", "had", "but", "not", "any", "all",
            "out", "one", "two", "way", "use", "why", "yes", "now", "her", "his",
        }
    )

    def _answer_from_knowledge(self, q: str) -> str:
        """Fall back to what Jarvis has actually learned about the topic asked."""
        terms = self._content_terms(q)
        if not terms:
            return self.voice.too_vague()
        scored: list[tuple[int, Any]] = []
        for lesson in self.memory.knowledge.lessons(limit=400):
            # The pattern key carries the name traders actually use ("breakout"),
            # which often never appears in the claim's wording.
            key = (lesson.pattern_key or "").replace("_", " ")
            haystack = f"{lesson.topic} {key} {lesson.claim}".lower()
            overlap = sum(1 for t in terms if t in haystack)
            if overlap:
                scored.append((overlap, lesson))
        if not scored:
            return self.voice.unknown_topic()
        scored.sort(key=lambda pair: (pair[0], pair[1].confidence), reverse=True)
        lines = [self.voice.knowledge_header()]
        for _, lesson in scored[:4]:
            marker = {
                "foundation": "basics", "expertise": "proven",
                "working": "holding up", "hypothesis": "unproven",
            }.get(lesson.tier, lesson.tier)
            lines.append(f"  - {lesson.claim} ({marker}, conf {lesson.confidence:.2f})")
            samples = lesson.support + lesson.refute
            if samples:
                # For anything measurable, the track record is the real answer.
                lines.append(
                    f"      measured: {lesson.support / samples * 100:.0f}% correct "
                    f"over {samples} graded calls"
                )
        return "\n".join(lines)

    _WATCH_RX = re.compile(
        r"(?:watch|keep an eye on|alert me (?:on|about)|tell me (?:if|when))\s+"
        r"\$?([A-Za-z][A-Za-z.\-]{0,5})\b"
        r".{0,30}?\b(above|below|over|under|hits?|reaches|drops? to|falls? to)\b\s*"
        r"\$?(\d+(?:\.\d+)?)",
        re.I,
    )

    def _parse_watch_request(self, q: str) -> tuple[str, float, str] | None:
        """Pull "keep an eye on XOM below 105" out of natural speech."""
        match = self._WATCH_RX.search(q)
        if not match:
            return None
        symbol, word, level = match.group(1).upper(), match.group(2).lower(), match.group(3)
        if symbol not in {s.upper() for s in self.universe}:
            return None
        direction = "below" if word in {"below", "under", "drops to", "drop to",
                                        "falls to", "fall to"} else "above"
        return symbol, float(level), direction

    @classmethod
    def _content_terms(cls, q: str) -> set[str]:
        """Content words from a question, singularised so 'breakouts' finds 'breakout'."""
        terms: set[str] = set()
        for raw in q.lower().replace("?", " ").replace("'", "").split():
            word = raw.strip(".,!:;\"()")
            if len(word) < 3 or word in cls._STOPWORDS:
                continue
            # Crude but effective stemming for the plurals traders actually use.
            if word.endswith("ies") and len(word) > 5:
                word = word[:-3] + "y"
            elif word.endswith("es") and len(word) > 5 and not word.endswith("ses"):
                word = word[:-2]
            elif word.endswith("s") and not word.endswith("ss"):
                word = word[:-1]
            terms.add(word)
        return terms

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _time_greeting() -> str:
        hour = datetime.now().hour
        if hour < 12:
            return "Good morning"
        if hour < 18:
            return "Good afternoon"
        return "Good evening"

    @staticmethod
    def _humanise_gap(stamp: str) -> str | None:
        try:
            then = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
        delta = utcnow() - then
        if delta < timedelta(minutes=2):
            return None
        if delta < timedelta(hours=1):
            return f"{int(delta.total_seconds() // 60)} minutes"
        if delta < timedelta(days=1):
            return f"{int(delta.total_seconds() // 3600)} hours"
        return f"{delta.days} day{'s' if delta.days != 1 else ''}"
