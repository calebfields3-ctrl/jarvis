"""Command line interface.

``jarvis run`` is the real thing: background threads keep watching charts,
reading news and studying, while the foreground waits for "hey Jarvis". Every
other subcommand is a one-shot slice of the same machinery.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from datetime import datetime

from .brain import Jarvis
from .config import get_config
from .market.yahoo import quiet_yfinance
from .voice.io import VoiceChannel

BANNER = r"""
   _   _   ___  _   _ ___ ___
  | | /_\ | _ \\ \ / /_ _/ __|    self-learning trading assistant
 _| |/ _ \|   / \ V / | |\__ \    say "hey Jarvis" to begin
|__/_/ \_\_|_\  \_/ |___|___/
"""


# --------------------------------------------------------------- background
class BackgroundMonitors:
    """Keeps the watchlist, the news wire and the study loop running."""

    def __init__(self, jarvis: Jarvis, *, announce=None) -> None:
        self.jarvis = jarvis
        self.announce = announce or (lambda msg: None)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.last_scan: str | None = None
        self.last_news: str | None = None
        self.last_learn: str | None = None

    def _loop(self, name: str, interval: int, work) -> None:
        while not self._stop.is_set():
            try:
                work()
            except Exception as exc:
                logging.getLogger(__name__).warning("%s loop error: %s", name, exc)
            self._stop.wait(interval)

    def _scan(self) -> None:
        report = self.jarvis.scan_market()
        self.last_scan = (
            f"{len(report.detections)} setups across {report.symbols_with_data} charts"
        )
        urgent = [d for d in report.best(3) if d[2] >= 0.75]
        for symbol, detection, confidence in urgent:
            self.announce(
                self.jarvis.voice.alert(
                    symbol, detection.description, detection.direction, confidence
                )
            )
        # Standing instructions get priority over anything Jarvis noticed himself.
        for message in self.jarvis.check_watches():
            self.announce(message)

    def _news(self) -> None:
        stats = self.jarvis.news.monitor(self.jarvis.memory.news)
        self.last_news = f"{stats['stored']} new of {stats['seen']} headlines"
        for item in self.jarvis.memory.news.recent(limit=3, min_impact=0.8):
            self.announce(
                self.jarvis.voice.breaking_news(item["headline"], item["source"])
            )

    def _learn(self) -> None:
        result = self.jarvis.learn_cycle()
        self.last_learn = result["evaluation"]["summary"]

    def start(self) -> None:
        config = self.jarvis.config
        jobs = [
            ("scan", config.scan_interval_seconds, self._scan),
            ("news", config.news_interval_seconds, self._news),
            ("learn", config.learn_interval_seconds, self._learn),
        ]
        for name, interval, work in jobs:
            thread = threading.Thread(
                target=self._loop, args=(name, interval, work), name=f"jarvis-{name}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> str:
        return (
            f"scan: {self.last_scan or 'pending'} | "
            f"news: {self.last_news or 'pending'} | "
            f"study: {self.last_learn or 'pending'}"
        )


# --------------------------------------------------------------- subcommands
def cmd_run(args, jarvis: Jarvis) -> int:
    channel = VoiceChannel(jarvis.config)
    print(BANNER)
    print(jarvis.voice.boot(len(jarvis.universe), channel.mode, jarvis.config.wake_phrase))

    monitors = BackgroundMonitors(jarvis, announce=channel.say)
    if not args.no_monitors:
        monitors.start()

    try:
        while True:
            if not channel.wait_for_wake():
                break
            channel.say(jarvis.wake(channel="voice" if channel.is_voice else "text"))
            # Conversation stays open until the user dismisses Jarvis.
            while True:
                heard = channel.listen()
                if heard is None:
                    break
                lowered = heard.lower().strip()
                if lowered in {"stop", "exit", "quit", "goodbye", "bye", "sleep", "that's all"}:
                    channel.say(jarvis.voice.dismissed())
                    break
                if lowered in {"status", "monitor status"}:
                    channel.say(monitors.status())
                    continue
                channel.say(jarvis.ask(heard))
            jarvis.sleep()
    except KeyboardInterrupt:
        print()
    finally:
        monitors.stop()
        jarvis.sleep()
        jarvis.mark_close()
    print(jarvis.voice.shutdown())
    return 0


def cmd_wake(args, jarvis: Jarvis) -> int:
    print(jarvis.wake())
    jarvis.sleep()
    return 0


def cmd_ask(args, jarvis: Jarvis) -> int:
    print(jarvis.ask(" ".join(args.question)))
    jarvis.sleep()
    return 0


def cmd_scan(args, jarvis: Jarvis) -> int:
    symbols = args.symbols or None
    print(jarvis.scan_briefing(jarvis.scan_market(symbols), top=args.top))
    return 0


def cmd_brief(args, jarvis: Jarvis) -> int:
    print(jarvis.premarket_brief())
    return 0


def cmd_watch(args, jarvis: Jarvis) -> int:
    if args.cancel:
        cancelled = jarvis.memory.watches.cancel(args.cancel)
        print(f"Cancelled {cancelled} watch(es) on {args.cancel.upper()}.")
        return 0
    if not args.symbol:
        print(jarvis.list_watches())
        return 0
    if args.direction is None or args.level is None:
        print("Usage: jarvis watch SYMBOL {above|below} LEVEL")
        return 1
    print(jarvis.add_watch(args.symbol, args.level, args.direction, args.note))
    return 0


def cmd_daytrade(args, jarvis: Jarvis) -> int:
    symbols = args.symbols or None
    report = jarvis.daytrade_scan(symbols, interval=args.interval, max_symbols=args.max_symbols)
    print(jarvis.daytrade_briefing(report, top=args.top))
    return 0


def cmd_plan(args, jarvis: Jarvis) -> int:
    plan = jarvis.plan_trade(args.symbol, args.direction, args.entry, args.stop, args.target)
    print(plan.describe())
    status = jarvis.trading_status()
    if not status.can_trade:
        print()
        print(status.describe())
    return 0 if plan.is_viable else 1


def cmd_riskstatus(args, jarvis: Jarvis) -> int:
    print(jarvis.trading_status().describe())
    return 0


def cmd_train(args, jarvis: Jarvis) -> int:
    if args.module:
        from .learning.coach import MODULES_BY_KEY

        module = MODULES_BY_KEY.get(args.module)
        if module is None:
            print(f"No module called {args.module!r}.")
            return 1
        print(jarvis.teach(module))
        return 0
    progress = jarvis.coach.progress()
    print(f"Training: {progress['passed']}/{progress['total']} modules passed.")
    if progress["completed_modules"]:
        for title in progress["completed_modules"]:
            print(f"  [done] {title}")
    print()
    print(jarvis.teach())
    return 0


def cmd_quiz(args, jarvis: Jarvis) -> int:
    print(jarvis.submit_quiz(args.module, args.answers))
    return 0


def cmd_review(args, jarvis: Jarvis) -> int:
    print(jarvis.review_trades(limit=args.limit))
    return 0


def cmd_learn(args, jarvis: Jarvis) -> int:
    result = jarvis.learn_cycle(study_web=not args.no_web)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_news(args, jarvis: Jarvis) -> int:
    stats = jarvis.news.monitor(jarvis.memory.news, min_impact=args.min_impact)
    print(
        f"Polled {len(jarvis.news.feeds)} newsrooms: {stats['seen']} headlines, "
        f"{stats['material']} material, {stats['stored']} new."
    )
    for error in jarvis.news.last_errors[:5]:
        print(f"  feed issue: {error}")
    for item in jarvis.memory.news.recent(limit=args.top, min_impact=args.min_impact):
        tickers = f"  [{item['tickers']}]" if item["tickers"] else ""
        print(f"  [{item['impact']:.2f}] {item['headline']} -- {item['source']}{tickers}")
    return 0


def cmd_bootstrap(args, jarvis: Jarvis) -> int:
    print(
        f"Replaying {args.sample} symbols over {args.period} of history to build a "
        "track record. This takes a while."
    )
    result = jarvis.bootstrap_expertise(sample=args.sample, period=args.period)
    print(json.dumps(result, indent=2, default=str))
    print()
    print(jarvis.what_i_know())
    return 0


def cmd_know(args, jarvis: Jarvis) -> int:
    print(jarvis.what_i_know(top=args.top))
    return 0


def cmd_sources(args, jarvis: Jarvis) -> int:
    counts = jarvis.registry.sync(jarvis.config.curated_sources_file)
    print(f"Vetting: {counts}")
    for source in jarvis.memory.knowledge.trusted_sources():
        print(
            f"  [{source['verification']:<11}] {source['platform']:<10} "
            f"{source['handle']:<28} credibility {source['credibility']:.2f}"
        )
        if args.verbose and source["evidence"]:
            print(f"      {source['evidence']}")
    return 0


def cmd_buy(args, jarvis: Jarvis) -> int:
    jarvis.portfolio.record_trade(args.symbol, "buy", args.quantity, args.price, note=args.note)
    print(f"Recorded: bought {args.quantity:g} {args.symbol.upper()} at {args.price:.2f}.")
    print(jarvis.portfolio_summary())
    return 0


def cmd_sell(args, jarvis: Jarvis) -> int:
    jarvis.portfolio.record_trade(args.symbol, "sell", args.quantity, args.price, note=args.note)
    print(f"Recorded: sold {args.quantity:g} {args.symbol.upper()} at {args.price:.2f}.")
    print(jarvis.portfolio_summary())
    return 0


def cmd_deposit(args, jarvis: Jarvis) -> int:
    jarvis.portfolio.record_cash(args.amount, note=args.note)
    print(f"Cash adjusted by ${args.amount:,.2f}. Balance now ${jarvis.portfolio.cash():,.2f}.")
    return 0


def cmd_mark_close(args, jarvis: Jarvis) -> int:
    snap = jarvis.mark_close()
    print(
        f"Marked {snap.as_of.date()} close at ${snap.total_value:,.2f} "
        f"(cash ${snap.cash:,.2f}, positions ${snap.positions_value:,.2f})."
    )
    return 0


def cmd_status(args, jarvis: Jarvis) -> int:
    level, score = jarvis.memory.knowledge.expertise_level()
    stats = jarvis.memory.knowledge.stats()
    print(f"Owner            : {jarvis.memory.profile.name}")
    print(f"Memory           : {jarvis.config.db_path}")
    print(f"Sessions on file : {jarvis.memory.sessions.count()}")
    print(f"Watchlist        : {len(jarvis.universe)} symbols")
    print(f"Market data      : {getattr(jarvis.provider, 'name', type(jarvis.provider).__name__)}")
    print(f"Expertise        : {level} ({score:.2f})")
    print(f"Lessons          : {stats['total']} ({stats['by_tier']})")
    print(f"Verified teachers: {stats['sources']}")
    print(f"Graded calls     : {stats['graded_predictions']}")
    print(f"Safe Browsing    : {'configured' if jarvis.config.safe_browsing_key else 'not configured'}")
    print(f"Google search    : {'configured' if jarvis.config.google_cse_id else 'not configured'}")
    print(f"YouTube API      : {'configured' if jarvis.config.youtube_api_key else 'not configured'}")
    return 0


def cmd_history(args, jarvis: Jarvis) -> int:
    rows = jarvis.portfolio.history(args.days)
    if not rows:
        print("No end-of-day marks recorded yet. Run `jarvis mark-close`.")
        return 0
    print(f"{'date':<12}{'total':>14}{'day P/L':>14}{'day %':>9}")
    for row in rows:
        pnl = row["day_pnl"]
        pct = row["day_pnl_pct"]
        print(
            f"{row['as_of_date']:<12}{row['total_value']:>14,.2f}"
            f"{(f'{pnl:+,.2f}' if pnl is not None else '--'):>14}"
            f"{(f'{pct:+.2f}' if pct is not None else '--'):>9}"
        )
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis", description="Jarvis -- self-learning AI trading assistant"
    )
    parser.add_argument("--offline", action="store_true", help="use the synthetic market (no network)")
    parser.add_argument("--persona", choices=["jarvis", "plain"], help="override his voice")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="wake-word loop with background monitoring")
    run.add_argument("--no-monitors", action="store_true", help="skip background loops")
    run.set_defaults(func=cmd_run)

    sub.add_parser("wake", help="print the greeting once").set_defaults(func=cmd_wake)

    ask = sub.add_parser("ask", help="ask a question")
    ask.add_argument("question", nargs="+")
    ask.set_defaults(func=cmd_ask)

    scan = sub.add_parser("scan", help="sweep the watchlist for patterns")
    scan.add_argument("symbols", nargs="*", help="limit to these symbols")
    scan.add_argument("--top", type=int, default=8)
    scan.set_defaults(func=cmd_scan)

    sub.add_parser("brief", help="pre-market briefing").set_defaults(func=cmd_brief)

    watch = sub.add_parser("watch", help="standing alert on a price level")
    watch.add_argument("symbol", nargs="?")
    watch.add_argument("direction", nargs="?", choices=["above", "below"])
    watch.add_argument("level", nargs="?", type=float)
    watch.add_argument("--note")
    watch.add_argument("--cancel", metavar="SYMBOL", help="cancel watches on a symbol")
    watch.set_defaults(func=cmd_watch)

    day = sub.add_parser("daytrade", help="intraday setup scan with sizing and risk checks")
    day.add_argument("symbols", nargs="*")
    day.add_argument("--interval", default="5m", choices=["1m", "2m", "5m", "15m", "30m", "1h"])
    day.add_argument("--top", type=int, default=5)
    day.add_argument("--max-symbols", type=int, default=120)
    day.set_defaults(func=cmd_daytrade)

    plan = sub.add_parser("plan", help="size a trade from its stop")
    plan.add_argument("symbol")
    plan.add_argument("direction", choices=["long", "short"])
    plan.add_argument("entry", type=float)
    plan.add_argument("stop", type=float)
    plan.add_argument("target", type=float)
    plan.set_defaults(func=cmd_plan)

    sub.add_parser("risk", help="PDT rule and today's circuit breakers").set_defaults(
        func=cmd_riskstatus
    )

    train = sub.add_parser("train", help="day-trading curriculum")
    train.add_argument("module", nargs="?", help="jump to a specific module")
    train.set_defaults(func=cmd_train)

    quiz = sub.add_parser("quiz", help="answer a module's quiz, e.g. quiz risk_sizing a b c")
    quiz.add_argument("module")
    quiz.add_argument("answers", nargs="+")
    quiz.set_defaults(func=cmd_quiz)

    review = sub.add_parser("review", help="coach's review of your real trades")
    review.add_argument("--limit", type=int, default=100)
    review.set_defaults(func=cmd_review)

    learn = sub.add_parser("learn", help="one study + grading cycle")
    learn.add_argument("--no-web", action="store_true")
    learn.set_defaults(func=cmd_learn)

    news = sub.add_parser("news", help="poll global newsrooms now")
    news.add_argument("--min-impact", type=float, default=0.35)
    news.add_argument("--top", type=int, default=12)
    news.set_defaults(func=cmd_news)

    boot = sub.add_parser("bootstrap", help="replay history to build a track record")
    boot.add_argument("--sample", type=int, default=60, help="how many symbols to replay")
    boot.add_argument("--period", default="2y")
    boot.set_defaults(func=cmd_bootstrap)

    know = sub.add_parser("know", help="what Jarvis has learned and what it has proven")
    know.add_argument("--top", type=int, default=12)
    know.set_defaults(func=cmd_know)

    sources = sub.add_parser("sources", help="re-vet and list curated teachers")
    sources.add_argument("--verbose", action="store_true")
    sources.set_defaults(func=cmd_sources)

    buy = sub.add_parser("buy", help="record a buy")
    buy.add_argument("symbol")
    buy.add_argument("quantity", type=float)
    buy.add_argument("price", type=float)
    buy.add_argument("--note")
    buy.set_defaults(func=cmd_buy)

    sell = sub.add_parser("sell", help="record a sell")
    sell.add_argument("symbol")
    sell.add_argument("quantity", type=float)
    sell.add_argument("price", type=float)
    sell.add_argument("--note")
    sell.set_defaults(func=cmd_sell)

    dep = sub.add_parser("deposit", help="record a cash deposit or withdrawal")
    dep.add_argument("amount", type=float)
    dep.add_argument("--note")
    dep.set_defaults(func=cmd_deposit)

    sub.add_parser("mark-close", help="record today's close for tomorrow's P/L").set_defaults(
        func=cmd_mark_close
    )
    sub.add_parser("status", help="system and memory status").set_defaults(func=cmd_status)

    hist = sub.add_parser("history", help="equity history")
    hist.add_argument("--days", type=int, default=30)
    hist.set_defaults(func=cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Third-party network chatter is silenced unless you asked to see it.
    quiet_yfinance(not args.verbose)

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    config = get_config()
    if args.persona:
        config.persona = args.persona
    jarvis = Jarvis(config, offline=args.offline)
    try:
        return args.func(args, jarvis)
    finally:
        jarvis.memory.close()


if __name__ == "__main__":
    sys.exit(main())
