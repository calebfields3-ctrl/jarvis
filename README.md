# Jarvis

A self-learning AI trading assistant. Wakes on "hey Jarvis", greets Caleb by
name, opens with a portfolio summary including the previous day's P/L, and
remembers everything across sessions. It starts with foundational trading
knowledge and builds real expertise by studying curated sources — then grades
itself against what the market actually did.

```
$ jarvis run

   _   _   ___  _   _ ___ ___
  | | /_\ | _ \\ \ / /_ _/ __|    self-learning trading assistant
 _| |/ _ \|   / \ V / | |\__ \    say "hey Jarvis" to begin
|__/_/ \_\_|_\  \_/ |___|___/

Watching 677 symbols. I/O mode: porcupine/speech/pyttsx3.
Waiting for "hey jarvis"...

> hey jarvis

Good morning, Caleb. Jarvis online.
It's been 14 hours since we last spoke.

Portfolio: $100,303.12 across 3 positions.
Since the 2026-08-06 close you're up $1,504.55 (+1.52%).
Best position: NVDA +19.7% ($+2,324.15 open).
Weakest: XOM -3.1% ($-689.40 open).
Last 22 sessions on record: $+6,410.22 (+6.83%), 13 up days vs 9 down.

Overnight headlines worth your attention:
  - Fed signals slower pace of cuts after inflation print (Reuters) [SPY, TLT]

My read on markets is competent right now (score 0.49) -- 214 lessons held,
1,295 predictions graded.
```

---

## The core idea

Most "AI trading" tools are confident. Jarvis is *measured*. Every pattern it
spots is written down **before** the outcome is known — symbol, direction,
price, horizon. When the horizon passes, the call is graded against real bars
and folded back into the lesson that produced it.

So a pattern's journey looks like this:

```
hypothesis  ──►  working  ──►  expertise
 (unproven)     (5+ graded)   (20+ graded, >62% correct)
     │
     └──────►  retired  (20+ graded, <45% correct)
```

Nothing gets promoted for being persuasive. Only for being right. And when
Jarvis is asked about a pattern, it answers with its own record:

```
$ jarvis ask "what do you know about breakouts"

Here's what I've got on that:
  - A volatility squeeze resolving downward tends to continue downward. (holding up, conf 0.51)
      measured: 51% correct over 61 graded calls
  - A close above the 20-day high on heavy volume tends to keep running. (retired, conf 0.34)
      measured: 34% correct over 136 graded calls
```

That second line is Jarvis telling you a pattern it was taught **does not
work** in the market it watches. That is the whole point.

---

## Install

```bash
git clone <this repo> && cd jarvis
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"     # or: pip install -r requirements.txt
```

Nothing is required beyond PyYAML. Every integration below degrades gracefully:
no key means that capability reports itself as unavailable, and the rest keeps
running.

```bash
jarvis status        # what's configured, what isn't
jarvis --offline ...  # force the synthetic market, no network at all
```

---

## First run

```bash
jarvis deposit 50000
jarvis buy AAPL 100 182.30
jarvis buy NVDA 50 118.75

jarvis bootstrap          # replay 2 years of history to build a track record
jarvis wake               # the greeting
jarvis run                # the real thing: wake word + background monitoring
```

`jarvis bootstrap` matters. Without it, every pattern is an untested hypothesis
and Jarvis will tell you so. Bootstrapping walks the detectors forward through
past bars — never showing them a future bar — and grades every signal on what
followed. It takes a few seconds and gives Jarvis a thousand-plus honest
walk-forward samples to reason from.

Run `jarvis mark-close` at the end of each day (or let `jarvis run` do it on
exit) so tomorrow's greeting has a day-over-day number.

---

## What it does

### Persistent memory
One SQLite database at `~/.jarvis/jarvis.db`. It holds your name, every
session and its transcript, the full trade ledger, daily equity marks, every
lesson with its confidence and track record, every prediction, and every
graded outcome. Restart it and nothing is lost.

### Watching 500+ charts
`data/universe.txt` ships with 677 symbols. Each scan cycle does two things:

- **TradingView scanner** — batched, concurrent POSTs to the same screener
  endpoint TradingView's own stock screener uses. 677 symbols is a handful of
  round-trips, not 677. Gives live price, RSI, MACD, moving averages, relative
  volume and TradingView's own recommendation per chart.
- **Local pattern engine** — 9 detectors over raw OHLCV producing 17 pattern
  types (golden/death cross, 20-day breakouts and breakdowns, RSI reversals,
  engulfing candles, hammers, shooting stars, MACD crosses, volatility squeeze
  resolutions, gaps, volume-dryup bases). This is the half that gets graded,
  and it needs no vendor.

A full 677-symbol sweep runs in about 9 seconds.

### Learning from curated traders
Jarvis only studies accounts in `data/curated_sources.yaml`, and only after
each passes vetting. Evidence is scored — regulatory registration (0.30),
audited track record (0.28), positions published before the outcome (0.16),
institutional background (0.14), tenure (0.10). Scam language ("guaranteed
returns", "signals group", "98% win rate", "never loses") is an instant
rejection regardless of credentials or size.

**Follower count is worth 0.04, capped.** Reach is not evidence of skill, and
a system that weighted it would learn from whoever markets hardest.

- **YouTube** — transcripts of recent uploads from verified channels, via
  `youtube-transcript-api` and the YouTube Data API.
- **Instagram / TikTok** — neither offers a general read API for other
  people's posts, and scraping them violates their terms and breaks
  constantly. Jarvis does **not** scrape. It ingests through official APIs
  where you hold a token, the sanctioned oEmbed endpoints for specific post
  URLs, and a drop folder at `~/.jarvis/inbox/{instagram,tiktok}/` — save a
  caption as `.txt` with `@handle` on the first line, or drop your own data
  export as `.json`. The drop folder is the route that always works.
- **The web** — Google Programmable Search, then the safety gate (below)
  decides what may be opened.
- **Yahoo Finance** — OHLCV, prices, and the fundamentals Jarvis actually
  reasons about: P/E, margins, revenue growth, debt/equity, short interest,
  52-week range. Turned into plain sentences, not raw JSON.
- **Global news** — RSS from Reuters, AP, CNBC, MarketWatch, BBC, the
  Guardian, NPR, plus the Fed, SEC and BLS. Each headline is scored for market
  impact, tagged with tickers and sectors, and filed if material.

Studied content goes through a distiller that keeps sentences making a
*generalisable claim about market behaviour* and discards sponsor reads, hype,
calls to action and personal anecdotes. Extracted lessons enter as
`hypothesis` and are capped at 0.72 confidence — **no source, however
credible, can mint certainty.** Only outcomes do that.

### The safety gate
Jarvis opens a link only when it clears all three:

1. HTTPS, real registrable domain, no embedded credentials, no punycode
   homographs, no bare IPs.
2. Survives the heuristic screen — URL shorteners, brand lookalikes
   (`tradingview-premium.net`, `yahoo-finance-login.com`), direct binary
   downloads, scam-shaped paths.
3. Reputationally cleared — on the curated allowlist of exchanges, regulators,
   primary data sources and established newsrooms, **or** cleared by Google
   Safe Browsing.

Anything merely "probably fine" comes back `unknown` and is **not followed**.
Set `JARVIS_REQUIRE_SAFE_BROWSING=1` to demand an active Safe Browsing clear
for every URL, allowlist included.

### Day trading
Daily bars answer "should I own this for weeks". Day trading is a different
question on a different clock, so it gets its own engine.

**The clock gates everything.** `jarvis/market/session.py` knows the opening
drive (09:30-10:00), the morning trend, the midday chop, and power hour, in US
Eastern with holidays handled. A breakout at 09:45 and the same breakout at
12:30 are not the same trade, and Jarvis refuses to open a new day trade inside
the midday window or within 15 minutes of the bell.

**Six intraday detectors**, all VWAP- and opening-range aware: opening range
breakout/breakdown, VWAP reclaim/rejection, gap-and-go, failed-breakdown
reversal, momentum surge, power-hour trend. VWAP is anchored per session and
resets each day; relative volume compares against the *same minute* of prior
sessions, not a whole-day average.

Every intraday signal carries an entry, a stop and a target. Anything below
1.5R is dropped before you ever see it — a day trade risking a dollar to make
eighty cents loses money at a perfectly respectable hit rate.

**Sizing comes from the stop, never from a feeling:**

```
$ jarvis plan AAPL long 50 48 56
AAPL LONG 75 shares @ 50.00
  stop 48.00 | target 56.00 | 3.0R
  risking $150.00 on $3,750.00 of stock
```

**The PDT rule is tracked, not discovered the hard way.** Four day trades in
five rolling business days flags a US margin account and freezes it for 90 days
under $25,000. Jarvis counts every round trip and blocks you at three:

```
$ jarvis risk
Equity $20,030.00 is below the $25,000 PDT minimum. 3 day trades used since
2026-07-31; 0 left before the rule trips.
Today: 6 trades, $+30.00 realised, no losing streak.
STOP TRADING TODAY:
  - pattern day trader rule: 3 day trades since 2026-07-31 on $20,030.00 equity.
    You are at the limit -- one more within five business days flags the account
    and freezes it for 90 days.
```

Daily loss limit, trade count and consecutive-loss circuit breakers work the
same way. When you're blocked, `jarvis daytrade` will not show you setups at
all — handing a tilted trader a watchlist is how the bad day becomes a bad month.

### Training you to day trade
Seven modules, each gated behind a quiz, progress persisted:

1. What a day trade actually is
2. The shape of the trading day
3. Position sizing from the stop
4. The pattern day trader rule
5. VWAP and the opening range
6. The psychology that actually costs money
7. Measuring whether you actually have an edge

Risk and PDT modules require a perfect score — you don't get to be mostly right
about the rule that can freeze your account.

```
$ jarvis train                       # teach the next module + its quiz
$ jarvis quiz risk_sizing a b c      # answer it
```

**And then the part that actually changes behaviour** — `jarvis review` reads
your real closed trades and names what's costing you money, with your own
numbers:

```
$ jarvis review
Reviewed 8 closed trades.
Win rate 62% | avg win $88.00 | avg loss $733.33 | expectancy $-220.00 per trade
Expectancy is negative -- as it stands, trading more loses money faster.

!! You are cutting winners and holding losers
     Average win $88.00 against an average loss of $733.33 (0.12x). At a 62%
     win rate that is not sustainable.
     -> Set the target and stop before entry and let both work.
 ! A lot of your entries land in the midday chop
     4 of 8 timed entries fell between 12:00 and 14:00 ET.
```

It detects: cutting winners while holding losers, revenge sizing (scoped to
*within* a session — sizing up the next morning is a strategy call, not a
reflex), overnight drift on intended day trades, midday entries, overtrading,
and insufficient sample size.

### Voice
Wake word falls back in a defined order: Picovoice Porcupine (ships a built-in
"jarvis" keyword, fully on-device) → continuous speech recognition matching the
phrase → typing it at a prompt. Same for listening (SpeechRecognition → stdin)
and speaking (pyttsx3 → `say`/`espeak` → stdout). Headless boxes work fine;
they just type.

---

## Commands

| Command | What it does |
|---|---|
| `jarvis run` | Wake-word loop plus background scan / news / study threads |
| `jarvis wake` | Print the greeting once |
| `jarvis ask "..."` | One question |
| `jarvis scan [SYMBOLS]` | Sweep the watchlist for setups |
| `jarvis daytrade` | Intraday setups with sizing and risk checks |
| `jarvis plan SYM long E S T` | Size a trade from its stop |
| `jarvis risk` | PDT rule and today's circuit breakers |
| `jarvis train [module]` | Day-trading curriculum |
| `jarvis quiz MODULE a b c` | Answer a module's quiz |
| `jarvis review` | Coach's review of your real trades |
| `jarvis bootstrap` | Replay history to build a measured track record |
| `jarvis know` | Everything learned, and the pattern scoreboard |
| `jarvis learn` | One study + grading cycle |
| `jarvis news` | Poll global newsrooms now |
| `jarvis sources` | Re-vet and list curated teachers |
| `jarvis buy/sell/deposit` | Record trades and cash |
| `jarvis mark-close` | Record today's close for tomorrow's P/L |
| `jarvis history` | Equity curve |
| `jarvis status` | What's configured and what isn't |

---

## Configuration

All optional; all read from the environment.

| Variable | Purpose |
|---|---|
| `JARVIS_OWNER` | Name Jarvis greets you by (default `Caleb`) |
| `JARVIS_HOME` | Memory location (default `~/.jarvis`) |
| `JARVIS_DATA_DIR` | Where the editable data files live (auto-detected) |
| `JARVIS_WAKE_PHRASE` | Default `hey jarvis` |
| `JARVIS_VOICE` | `1` to enable microphone and speech |
| `PICOVOICE_ACCESS_KEY` | On-device wake word |
| `GOOGLE_SAFE_BROWSING_KEY` | URL threat verification |
| `JARVIS_REQUIRE_SAFE_BROWSING` | `1` to require an active clear for every URL |
| `GOOGLE_CSE_KEY` / `GOOGLE_CSE_ID` | Google Programmable Search |
| `YOUTUBE_API_KEY` | Channel discovery for curated teachers |
| `INSTAGRAM_ACCESS_TOKEN` / `TIKTOK_ACCESS_TOKEN` | Official social APIs |
| `JARVIS_SCAN_INTERVAL` | Seconds between sweeps (default 60) |
| `JARVIS_PROMOTION_SAMPLES` | Graded calls before a pattern can be trusted (default 20) |

Editable data files:

- `data/universe.txt` — the watchlist (677 symbols shipped)
- `data/curated_sources.yaml` — who Jarvis may learn from
- `data/news_feeds.yaml` — which newsrooms it monitors
- `data/seed_knowledge.yaml` — extra foundational lessons and house rules

---

## Tests

```bash
pytest -q      # 214 tests, ~6s, no network required
```

Coverage includes portfolio arithmetic (average cost, realised P/L, day-over-day
P/L), indicator correctness, every detector against hand-built bar sequences,
the safety gate's verdicts, trader vetting including scam disqualification, the
reinforcement loop (promotion, retirement, foundations never retiring), news
scoring and feed parsing, memory surviving restarts, the session clock and
intraday detectors, position sizing and the PDT rule, and every coaching
diagnosis against synthetic bad habits.

---

## Things you should know

**Jarvis does not trade.** It analyses, tracks and reports. There is no broker
integration and no order placement anywhere in the codebase. Every execution
decision is yours. Its output is decision support, not advice, and it says so
on every symbol briefing.

**Day trading is the hardest way to make money in markets.** The consistent
finding across regulator and academic studies is that most day traders lose,
and that losses concentrate among those trading most actively. Jarvis is built
to respect that: it caps position size, blocks you at your limits, refuses to
show setups when you're tilted, and reports negative expectancy plainly rather
than finding an encouraging way to phrase it. None of that makes day trading
safe. It makes the risks visible.

**The PDT rule here is US equities.** Thresholds and the definition of a day
trade differ by broker and jurisdiction; the $25,000 minimum and the
four-trades-in-five-business-days window are FINRA rules for US margin
accounts. Check your own broker's terms before relying on the count.

**The curated source list ships without individual traders.** Vouching that a
specific person is a proven, successful trader is a factual claim about a real
human being, and it is not mine to make on your behalf. The file ships with
exchanges, regulators and established newsrooms — whose credentials are matters
of public record — plus a documented template. Add the traders you trust, with
honest evidence tags. The vetting engine is the substance; the list is your data.

**Measured hit rates are honest but not a strategy.** Hit rate without average
win/loss size says nothing about profitability — the scoreboard reports
`avg_edge_pct` alongside it for exactly this reason. A pattern can be right 58%
of the time and still lose money. Jarvis knows this; it's in its foundations.

**Backtested edges decay.** A bootstrap on two years of history tells you what
worked in that regime. It is a starting prior, not a guarantee, which is why
live signals keep getting graded and lessons keep moving.

**TradingView has no public streaming API.** The scanner uses the same endpoint
the public stock screener does. It works, it is fast, and it is not a
contractual API — if it ever changes, the local pattern engine carries the load
unchanged, which is why the grading half deliberately depends on nothing but
OHLCV.
