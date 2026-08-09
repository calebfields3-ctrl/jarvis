# Getting Jarvis running

Ten minutes, start to finish. Everything below has been run end to end.

---

## 1. Prerequisites

You need **Python 3.10 or newer** and **git**. Check:

```bash
python3 --version     # must be 3.10+
git --version
```

If Python is missing or too old:

- **macOS** — `brew install python@3.12 git`
  (no Homebrew? install it from https://brew.sh first)
- **Windows** — install from https://python.org/downloads,
  **tick "Add Python to PATH"** on the first screen. Then use `python`
  instead of `python3` in every command below.
- **Linux (Debian/Ubuntu)** — `sudo apt install python3 python3-venv git`

---

## 2. Get the code and install

```bash
git clone https://github.com/calebfields3-ctrl/jarvis.git
cd jarvis
git checkout claude/jarvis-ai-trading-assistant-2dv4oy

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[all]"
```

That last line takes a minute or two — it pulls in pandas and yfinance.

Then set the key that gives him a mind:

```bash
export ANTHROPIC_API_KEY=sk-ant-...    # console.anthropic.com -> API keys
```

Without it he falls back to matching your words against a list of things he
knows how to do. That still works — it's also what runs when the network is
down — but it's a shadow of the real thing. A few dollars a month covers
normal use.

On Linux you also want the window toolkit, which isn't a pip package:

```bash
sudo apt install -y python3-tk       # macOS and Windows already have it
```

**You'll know it worked when:**

```bash
jarvis status
```

prints your name, the memory path, and `Watchlist: 677 symbols`.

> **The `source .venv/bin/activate` line matters.** You need to run it in
> every new terminal window before `jarvis` is on your PATH. If you get
> `command not found: jarvis`, that's what you forgot.

---

## 3. First run — four commands

```bash
jarvis bootstrap        # takes ~30s; builds his track record from history
jarvis deposit 25000    # your starting cash (use your real number)
jarvis buy AAPL 10 182.30   # any positions you already hold: SYMBOL QTY PRICE
jarvis                  # open him
```

**`jarvis bootstrap` is not optional.** Without it every pattern he knows is
an untested hypothesis and he'll tell you so on every setup. Bootstrapping
replays two years of history through his detectors — never showing them a
future bar — and grades every signal on what actually followed. That's what
turns "I read that breakouts work" into "breakouts hit 34% over 136 samples,
so I've retired that one."

A dark window opens with a glowing ring, and he greets you by name with your
portfolio. Say **"hey Jarvis"** or type in the box at the bottom.

The ring is the interface: brighter and faster when he's listening, ticking
over while he works, pulsing when he speaks, amber when he needs your say-so
before doing something. When that happens a box asks you first — nothing
destructive runs without you clicking yes, and a short list of things he
refuses outright no matter what you click.

---

## 4. The daily rhythm

```bash
jarvis brief            # before the open: risk, news, watches, day trades left
jarvis daytrade         # intraday setups, each sized to your stop
jarvis scan             # swing setups across all 677 symbols
jarvis risk             # am I clear to trade right now?
jarvis mark-close       # at the end of the day, so tomorrow has a P/L number
```

Record trades as you make them so his numbers stay honest:

```bash
jarvis buy NVDA 20 118.75
jarvis sell NVDA 20 121.40
```

Then, weekly:

```bash
jarvis review           # he reads your real trades and names your bad habits
jarvis train            # the next module of the day-trading curriculum
```

---

## 5. The full experience

```bash
jarvis
```

That's it — no subcommand. Window, wake word, and the full agent behind it.
Close the window to stop him.

He has real control of the machine he's running on: a shell, your files, and
the ability to open pages in your browser. So you can ask him things no router
would ever have an answer for — "what's eating my disk", "make me a folder for
this month's trade journal", "pull up NVDA's chart". Everything he does is
written to `~/.jarvis/audit.jsonl`; ask him for the audit trail any time.

Prefer the terminal? `jarvis start --no-window`. Want the older text-only loop
with background monitoring threads? `jarvis run`.

Ask him things in plain English:

```
hey jarvis
how's my portfolio
what setups are you seeing
keep an eye on XOM below 105
tell me about NVDA
can i trade today
teach me to day trade
review my trades
goodbye
```

---

## 6. Optional: voice

Everything works typed. For actual speech:

```bash
pip install SpeechRecognition pyttsx3
```

Then microphone input and spoken replies. For the true always-listening
"hey jarvis" wake word (fully on-device):

```bash
pip install pvporcupine pyaudio
export PICOVOICE_ACCESS_KEY=your_free_key   # from console.picovoice.ai
export JARVIS_VOICE=1
jarvis run
```

**`pyaudio` is the one that commonly fails to install**, because it needs
system audio headers:

- **macOS** — `brew install portaudio` then retry
- **Debian/Ubuntu** — `sudo apt install portaudio19-dev python3-pyaudio`
- **Windows** — usually installs fine; if not, `pip install pipwin && pipwin install pyaudio`

If any of it fails, skip it. He drops back to typed input automatically and
tells you which mode he's in on startup.

---

## 7. Optional: API keys

None are required. Each one just switches on a capability that otherwise
reports itself as unavailable.

| Key | Unlocks | Where |
|---|---|---|
| `GOOGLE_SAFE_BROWSING_KEY` | URL threat verification before he opens links | console.cloud.google.com → Safe Browsing API |
| `GOOGLE_CSE_KEY` + `GOOGLE_CSE_ID` | Web research via Google | programmablesearchengine.google.com |
| `YOUTUBE_API_KEY` | Finding new videos from curated traders | console.cloud.google.com → YouTube Data API v3 |
| `PICOVOICE_ACCESS_KEY` | On-device "hey jarvis" wake word | console.picovoice.ai |

Set them in your shell profile so they persist:

```bash
echo 'export YOUTUBE_API_KEY=...' >> ~/.zshrc   # or ~/.bashrc
```

Check what he can see: `jarvis status`

---

## 8. Making him yours

```bash
export JARVIS_ADDRESS=boss        # he says "boss" instead of "sir"
export JARVIS_ADDRESS=            # drops the honorific entirely
export JARVIS_PERSONA=plain       # flat factual reporting, no butler
export JARVIS_OWNER="Cal"         # or just say "call me Cal" to him
```

Edit these files directly — he reloads them on every start:

- `data/universe.txt` — the 677 symbols he watches
- `data/curated_sources.yaml` — **who he's allowed to learn from.** Ships with
  exchanges and newsrooms only; add the individual traders you trust, with
  honest evidence tags. Run `jarvis sources` to see who passed vetting.
- `data/news_feeds.yaml` — which newsrooms he monitors

---

## Troubleshooting

**`command not found: jarvis`**
You didn't activate the venv. `source .venv/bin/activate` (Windows:
`.venv\Scripts\activate`), in every new terminal.

**`Market data: synthetic` in `jarvis status`**
He couldn't reach Yahoo Finance, so he fell back to his offline simulator.
Check your internet. Everything still works, but the prices aren't real.

**Setups all say "untested"**
Run `jarvis bootstrap`.

**`No module named jarvis`**
You're outside the project directory or the venv isn't active. `cd` back to
the `jarvis` folder and activate.

**Want to start completely over**
`rm -rf ~/.jarvis` wipes all memory — profile, trades, lessons, everything.

**Something looks wrong and you want the detail**
`jarvis --verbose <command>` shows the network and library errors he normally
keeps to himself.

---

## Two things to know before you trade

**He never places an order.** There is no broker connection anywhere in the
code. He analyses, sizes and warns; every execution decision is yours.

**Day trading is the hardest way to make money in markets.** Most day traders
lose, and losses concentrate among the most active. He's built to respect
that — he caps size, blocks you at your limits, and reports negative
expectancy plainly rather than finding an encouraging way to phrase it. That
makes the risks visible. It doesn't make them go away.
