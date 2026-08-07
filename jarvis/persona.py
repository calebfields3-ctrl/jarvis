"""How Jarvis speaks.

Every user-facing sentence lives here rather than being scattered through the
logic. Two reasons that matters:

* The voice is swappable. ``JARVIS_PERSONA=plain`` gets you flat reporting;
  the default gets you the butler. Nothing in ``brain.py`` changes either way.
* The logic stays testable. ``brain.py`` computes numbers and decides what is
  true; the persona decides how to say it. Tests can assert on facts without
  matching prose, and on prose without recomputing facts.

The house style for the default persona: formal, precise, British, dry. He
addresses you as "sir", volunteers what he thinks you have missed, and is more
insistent about risk than about opportunity. The wit is occasional and
understated -- a persona that quips on every line stops being read.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol, Sequence


@dataclass
class GreetingContext:
    """Everything the greeting might mention. The persona picks what to use."""

    name: str
    hour: int
    first_session: bool
    gap: str | None
    expertise_label: str
    expertise_score: float
    lessons: int
    graded: int


# --------------------------------------------------------------------- base
class Persona:
    """Plain, factual reporting. The neutral baseline and the fallback."""

    key = "plain"
    address = ""

    def __init__(self, *, seed: int | None = None, address: str | None = None) -> None:
        self._rng = random.Random(seed) if seed is not None else random.Random()
        if address is not None:
            self.address = address

    # -- small helpers ---------------------------------------------------
    def _pick(self, options: Sequence[str]) -> str:
        return self._rng.choice(list(options))

    @property
    def _sir(self) -> str:
        """', sir' when the persona uses an honorific, else nothing."""
        return f", {self.address}" if self.address else ""

    @property
    def _Sir(self) -> str:
        """' sir' as a standalone address, else nothing."""
        return f" {self.address}" if self.address else ""

    @staticmethod
    def money(value: float, *, signed: bool = False) -> str:
        sign = "+" if signed and value >= 0 else ("-" if signed else "")
        return f"{sign}${abs(value):,.2f}" if signed else f"${value:,.2f}"

    # -- lifecycle -------------------------------------------------------
    def boot(self, watching: int, mode: str, wake_phrase: str) -> str:
        return (
            f"Watching {watching} symbols. I/O mode: {mode}.\n"
            f'Waiting for "{wake_phrase}"...  (Ctrl-C to stop)\n'
        )

    def greeting_open(self, ctx: GreetingContext) -> str:
        if ctx.hour < 12:
            part = "Good morning"
        elif ctx.hour < 18:
            part = "Good afternoon"
        else:
            part = "Good evening"
        return f"{part}, {ctx.name}. Jarvis online."

    def greeting_gap(self, ctx: GreetingContext) -> str | None:
        if ctx.first_session:
            return "This is our first session -- I'll remember it from here."
        return f"It's been {ctx.gap} since we last spoke." if ctx.gap else None

    def greeting_expertise(self, ctx: GreetingContext) -> str:
        return (
            f"My read on markets is {ctx.expertise_label} right now "
            f"(score {ctx.expertise_score:.2f}) -- {ctx.lessons} lessons held, "
            f"{ctx.graded} predictions graded."
        )

    def summoned(self, name: str) -> str:
        """The one line he says the instant the window appears.

        Spoken aloud before anything has been looked up, so it has to be
        short and it has to be instant -- a pause here reads as the wake word
        not working, and Caleb says it again.
        """
        return f"Yes, {name}?"

    def dismissed(self) -> str:
        return "Standing by."

    def shutdown(self) -> str:
        return "Jarvis offline. Memory saved."

    def acknowledge(self) -> str:
        return "Done."

    # -- portfolio -------------------------------------------------------
    def portfolio_empty(self) -> str:
        return (
            "Your portfolio is empty. Add a deposit and some trades and I'll "
            "start tracking performance from there:\n"
            "  jarvis deposit 10000\n"
            "  jarvis buy AAPL 10 185.50"
        )

    def portfolio_headline(self, total: float, positions: int) -> str:
        return f"Portfolio: {self.money(total)} across {positions} positions."

    def day_pnl(self, pnl: float, pct: float | None, prior_date: str) -> str:
        direction = "up" if pnl >= 0 else "down"
        suffix = f" ({pct:+.2f}%)" if pct is not None else ""
        return (
            f"Since the {prior_date} close you're {direction} "
            f"{self.money(abs(pnl))}{suffix}."
        )

    def no_prior_close(self) -> str:
        return (
            "I don't have yesterday's close on record yet, so no day-over-day "
            "number -- I'll mark today's close and have it for you tomorrow."
        )

    def best_position(self, symbol: str, pct: float, pnl: float) -> str:
        return f"Best position: {symbol} {pct:+.1f}% ({self.money(pnl, signed=True)} open)."

    def worst_position(self, symbol: str, pct: float, pnl: float) -> str:
        return f"Weakest: {symbol} {pct:+.1f}% ({self.money(pnl, signed=True)} open)."

    def performance(self, days: int, change: float, pct: float, wins: int, losses: int) -> str:
        return (
            f"Last {days} sessions on record: {self.money(change, signed=True)} "
            f"({pct:+.2f}%), {wins} up days vs {losses} down."
        )

    def stale_prices(self, symbols: Sequence[str]) -> str:
        return (
            f"Heads up: I couldn't get live prices for {', '.join(symbols)} -- "
            "those are valued at cost, so the total is approximate."
        )

    # -- market ----------------------------------------------------------
    def news_header(self) -> str:
        return "Overnight headlines worth your attention:"

    def scan_summary(self, watched: int, with_data: int, fired: int) -> str:
        return (
            f"Watched {watched} charts; {with_data} returned data; "
            f"{fired} setups fired."
        )

    def setups_header(self) -> str:
        return "Highest-conviction setups:"

    def no_setups(self) -> str:
        return "Nothing meets the bar right now. No trade is a position."

    def untested_marker(self) -> str:
        return "untested -- hypothesis"

    def alert(self, symbol: str, description: str, direction: str, confidence: float) -> str:
        return (
            f"Heads up -- {symbol}: {description} "
            f"({direction}, confidence {confidence:.0%})."
        )

    def breaking_news(self, headline: str, source: str) -> str:
        return f"Breaking: {headline} ({source})."

    def disclaimer(self) -> str:
        return (
            "This is analysis, not a recommendation -- position sizing and the "
            "decision are yours."
        )

    # -- risk ------------------------------------------------------------
    def blocked_preamble(self) -> str:
        return "I'm not going to hand you setups while you're blocked. Come back tomorrow."

    def watch_added(self, symbol: str, level: float, direction: str) -> str:
        return f"Watching {symbol} for a move {direction} {level:.2f}."

    def watch_triggered(self, symbol: str, level: float, direction: str, price: float) -> str:
        return f"{symbol} crossed {direction} {level:.2f} -- now {price:.2f}."

    # -- knowledge -------------------------------------------------------
    def knowledge_header(self) -> str:
        return "Here's what I've got on that:"

    def unknown_topic(self) -> str:
        return (
            "I don't have anything solid on that yet. I'm still building expertise -- "
            "ask me about your portfolio, a ticker on the watchlist, current setups, "
            "or what I've learned so far."
        )

    def too_vague(self) -> str:
        return (
            "Ask me something more specific and I'll tell you what I've learned "
            "about it -- a concept, a pattern, a ticker, or your portfolio."
        )

    def name_confirmed(self, name: str) -> str:
        return f"Got it -- {name} from now on."

    def name_recalled(self, name: str) -> str:
        return f"You're {name}. I don't forget that."


# ------------------------------------------------------------------- JARVIS
class JarvisPersona(Persona):
    """The butler. Formal, precise, British, and quietly protective.

    Two rules keep him from becoming a parody:

    * The numbers are never dressed up. Only the framing around them changes.
    * He is more forceful about risk than about opportunity. Enthusiasm for a
      setup is understated; objection to a bad decision is not.
    """

    key = "jarvis"
    address = "sir"

    # -- lifecycle -------------------------------------------------------
    def boot(self, watching: int, mode: str, wake_phrase: str) -> str:
        return (
            f"All systems online. {watching} instruments under observation.\n"
            f"Audio: {mode}.\n"
            f'I will be listening for "{wake_phrase}"{self._sir}.'
            "  (Ctrl-C to stand down)\n"
        )

    def greeting_open(self, ctx: GreetingContext) -> str:
        # He greets by name and says "sir" everywhere else. A butler who only
        # ever says "sir" forgets who he works for, and being greeted by name
        # is the point of having him at all.
        if ctx.hour < 5:
            return self._pick((
                f"You're up early, {ctx.name}. Or late. I've stopped guessing.",
                f"Good morning, {ctx.name}, though the hour hardly earns the word.",
            ))
        if ctx.hour < 12:
            part = "Good morning"
        elif ctx.hour < 18:
            part = "Good afternoon"
        else:
            part = "Good evening"
        return f"{part}, {ctx.name}."

    def greeting_gap(self, ctx: GreetingContext) -> str | None:
        if ctx.first_session:
            return (
                "We haven't spoken before. I'll remember everything from here -- "
                "you needn't repeat yourself."
            )
        if not ctx.gap:
            return None
        return self._pick((
            f"It has been {ctx.gap} since we last spoke. I've kept watch.",
            f"{ctx.gap.capitalize()} since your last visit. Nothing has gone unobserved.",
        ))

    def greeting_expertise(self, ctx: GreetingContext) -> str:
        # He is candid about how much he actually knows, and does not inflate it.
        if ctx.graded == 0:
            return (
                f"I hold {ctx.lessons} lessons but have graded nothing yet"
                f"{self._sir}, so treat my opinions as reading rather than "
                "experience. A run of `jarvis bootstrap` would fix that."
            )
        modesty = {
            "novice": "I would not yet trust my own judgement",
            "developing": "My judgement is improving but unproven",
            "competent": "I am becoming useful",
            "proficient": "I have a reasonable record now",
            "expert": "My record is, at last, respectable",
        }.get(ctx.expertise_label, "My judgement is developing")
        return (
            f"{modesty}{self._sir} -- {ctx.lessons} lessons held and "
            f"{ctx.graded:,} predictions graded, which puts me at "
            f"{ctx.expertise_label} ({ctx.expertise_score:.2f})."
        )

    def summoned(self, name: str) -> str:
        return self._pick((
            f"Hello{self._sir}. What can I do for you?",
            f"{self._Sir}. How can I help?",
            f"Yes{self._sir}?",
            f"At your service{self._sir}.",
        ))

    def dismissed(self) -> str:
        return self._pick((
            f"Very good{self._sir}. Standing by.",
            "Standing by. I'll be here.",
            f"As you wish{self._sir}.",
        ))

    def shutdown(self) -> str:
        return f"Powering down{self._sir}. Everything is saved."

    def acknowledge(self) -> str:
        return self._pick((f"Noted{self._sir}.", "Done.", f"Very good{self._sir}."))

    # -- portfolio -------------------------------------------------------
    def portfolio_empty(self) -> str:
        return (
            f"Your portfolio is empty{self._sir}. I can't report on what isn't "
            "there. Fund it and record a trade or two and I'll begin keeping "
            "score:\n"
            "  jarvis deposit 10000\n"
            "  jarvis buy AAPL 10 185.50"
        )

    def portfolio_headline(self, total: float, positions: int) -> str:
        if positions == 0:
            return f"The portfolio stands at {self.money(total)}, entirely in cash."
        holding = "position" if positions == 1 else "positions"
        return f"The portfolio stands at {self.money(total)} across {positions} {holding}."

    def day_pnl(self, pnl: float, pct: float | None, prior_date: str) -> str:
        suffix = f", or {abs(pct):.2f}%," if pct is not None else ""
        movement = "up" if pnl >= 0 else "down"
        return (
            f"You're {movement} {self.money(abs(pnl))}{suffix} against the "
            f"{prior_date} close."
        )

    def no_prior_close(self) -> str:
        return (
            "I've no prior close on record, so I can't give you a day-over-day "
            f"figure{self._sir}. I'll mark tonight's and have it for you tomorrow."
        )

    def best_position(self, symbol: str, pct: float, pnl: float) -> str:
        return (
            f"{symbol} is carrying you at {pct:+.1f}% "
            f"({self.money(pnl, signed=True)} open)."
        )

    def worst_position(self, symbol: str, pct: float, pnl: float) -> str:
        # The weakest holding is not necessarily a losing one. Calling a
        # position that is up 80% "least happy" reads as though he can't
        # read his own numbers.
        if pct < -5:
            lead = "I'd draw your attention to"
        elif pct < 0:
            lead = "Least happy is"
        else:
            lead = "Trailing the pack, though still green, is"
        return (
            f"{lead} {symbol} at {pct:+.1f}% "
            f"({self.money(pnl, signed=True)} open)."
        )

    def performance(self, days: int, change: float, pct: float, wins: int, losses: int) -> str:
        verdict = ""
        if losses > wins and change < 0:
            verdict = " Rather more down days than up, I'm afraid."
        elif change > 0 and wins > losses:
            verdict = " A respectable stretch."
        return (
            f"Over the last {days} sessions on record: "
            f"{self.money(change, signed=True)} ({pct:+.2f}%), "
            f"{wins} up days against {losses} down.{verdict}"
        )

    def stale_prices(self, symbols: Sequence[str]) -> str:
        return (
            f"A caveat{self._sir}: I couldn't price {', '.join(symbols)}. "
            "Those are held at cost, so the total is an estimate rather than a fact."
        )

    # -- market ----------------------------------------------------------
    def news_header(self) -> str:
        return "Overnight, a few items I thought you'd want:"

    def scan_summary(self, watched: int, with_data: int, fired: int) -> str:
        if fired == 0:
            return (
                f"I've swept {watched} charts and found nothing worth your "
                "attention. Occasionally that is the finding."
            )
        return (
            f"I've swept {watched} charts -- {with_data} returned data, and "
            f"{fired} setups triggered."
        )

    def setups_header(self) -> str:
        return "The ones I'd look at first:"

    def no_setups(self) -> str:
        return (
            f"Nothing clears the bar{self._sir}. I'd sooner offer you nothing "
            "than something mediocre."
        )

    def untested_marker(self) -> str:
        return "untested -- I've never graded this one"

    def alert(self, symbol: str, description: str, direction: str, confidence: float) -> str:
        return (
            f"If I may{self._sir} -- {symbol}. {description}. "
            f"That's a {direction} setup at {confidence:.0%} confidence."
        )

    def breaking_news(self, headline: str, source: str) -> str:
        return f"You'll want to see this{self._sir}: {headline} -- {source}."

    def disclaimer(self) -> str:
        return (
            "Analysis, not advice. The sizing and the decision remain yours"
            f"{self._sir} -- as they should."
        )

    # -- risk ------------------------------------------------------------
    def blocked_preamble(self) -> str:
        # The one place he is genuinely immovable.
        return (
            f"I'd rather not{self._sir}. You're past a limit you set yourself, "
            "and handing you a watchlist now would not be doing you a service. "
            "The setups will keep."
        )

    def watch_added(self, symbol: str, level: float, direction: str) -> str:
        return f"I'll keep an eye on {symbol} and tell you if it goes {direction} {level:.2f}."

    def watch_triggered(self, symbol: str, level: float, direction: str, price: float) -> str:
        return (
            f"{symbol} has gone {direction} {level:.2f}{self._sir} -- "
            f"it's at {price:.2f} now. You asked to know."
        )

    # -- knowledge -------------------------------------------------------
    def knowledge_header(self) -> str:
        return "What I have on that:"

    def unknown_topic(self) -> str:
        return (
            f"I've nothing reliable on that{self._sir}, and I'd rather say so "
            "than improvise. Ask me about the portfolio, a ticker on the "
            "watchlist, the current setups, or what I've actually learned."
        )

    def too_vague(self) -> str:
        return (
            f"You'll have to be more specific{self._sir} -- a concept, a "
            "pattern, a ticker, or the portfolio."
        )

    def name_confirmed(self, name: str) -> str:
        return f"Very good, {name}. I'll remember."

    def name_recalled(self, name: str) -> str:
        return f"You are {name}{self._sir}. I don't forget that sort of thing."


PERSONAS: dict[str, type[Persona]] = {
    "jarvis": JarvisPersona,
    "plain": Persona,
}


def build_persona(key: str = "jarvis", *, seed: int | None = None,
                  address: str | None = None) -> Persona:
    """Pick a persona by name, falling back to the butler."""
    cls = PERSONAS.get((key or "").strip().lower(), JarvisPersona)
    return cls(seed=seed, address=address)
