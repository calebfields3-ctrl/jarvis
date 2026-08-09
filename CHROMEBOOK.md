# Installing Jarvis on a Chromebook

Written for a first computer. Nothing is assumed. Take it one step at a time —
the whole thing is about 20 minutes, and most of that is waiting.

---

## Before you start: two things about Chromebooks

**1. Chromebooks have a whole Linux computer hidden inside them.** It's switched
off out of the box. Turning it on is Part 1 below. It's an official Google
feature, it's safe, and it can't break the rest of your Chromebook — it runs in
its own sealed box.

**2. Copy and paste work differently in the Terminal.** Everywhere else on the
computer it's `Ctrl + C` and `Ctrl + V`. In the Terminal app it's:

- **Copy:** `Ctrl + Shift + C`
- **Paste:** `Ctrl + Shift + V`

If pasting seems to do nothing, this is why. You can also **right-click** (or
tap with two fingers on the trackpad) to paste.

---

## Part 1 — Turn on Linux

1. Click the **clock** in the bottom-right corner of the screen.
2. Click the **gear icon** (⚙️) to open Settings.
3. In the left-hand list, scroll down and click **About ChromeOS**.
   - On some Chromebooks it's under **Advanced** → **Developers** instead.
4. Find **Linux development environment** and click **Set up** or **Turn on**.
5. It asks for a username — type `caleb` (lowercase, no spaces).
6. It asks for disk size — the default is fine. If it lets you choose, pick
   **at least 10 GB**.
7. Click **Install** and wait. **This takes 5–10 minutes.** It's downloading a
   full Linux system. Leave it alone; go do something else.

When it finishes, a black window opens by itself. **That's the Terminal.** It's
where everything from here happens.

> ### If you can't find "Linux development environment"
>
> Two possible reasons:
>
> - **Your Chromebook is managed by a school or workplace.** Administrators can
>   block Linux. If your Chromebook was given to you by a school, this is the
>   most likely explanation, and you can't turn it on yourself.
> - **Your Chromebook is too old** (roughly pre-2019) or is a very low-end model.
>
> Either way you're not stuck — skip to **"If Linux isn't available"** at the
> bottom of this page. Jarvis will still run.

---

## Part 2 — Install the tools

The Terminal window should be open. If you closed it, find **Terminal** in your
app launcher (the circle at the bottom-left of the screen, then search for it).

Copy this line, click into the Terminal, paste with **`Ctrl + Shift + V`**, and
press **Enter**:

```
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip python3-tk
```

**What to expect:**

- It asks for a password. **Just press Enter** — you never set one, so it's
  blank. If it does ask properly, use your Google account password.
- A wall of text scrolls past for a minute or two. That's normal.
- It's finished when the scrolling stops and you see a line ending in `$` with a
  blinking cursor.

> `sudo` means "do this as the administrator". `apt` is how Linux installs
> programs. You're installing git (to download code) and Python (the language
> Jarvis is written in).

---

## Part 3 — Download and install Jarvis

Three lines. Paste each one, press Enter, and **wait for it to finish before
pasting the next**.

**Line 1** — download the code:

```
git clone https://github.com/calebfields3-ctrl/jarvis.git ~/jarvis
```

**Line 2** — go into the folder and switch to the right version:

```
cd ~/jarvis && git checkout claude/jarvis-ai-trading-assistant-2dv4oy
```

**Line 3** — set everything up:

```
./setup.sh
```

That last one does all the real work and takes a few minutes. It checks your
Python, builds a sealed environment, installs everything, and then spends about
30 seconds building Jarvis's track record from historical market data.

**Partway through it asks for an API key.** This is what gives him a mind
instead of a lookup table. **The free one works** — see *Getting the key*
below. You can press Enter to skip it; he still runs, just less cleverly, and
you can add it later with `jarvis key`.

If anything goes wrong it stops and tells you exactly what to do. **Running
`./setup.sh` again is always safe** — it skips whatever already worked.

**You'll know it worked** when you see green text saying `Jarvis is ready.`

---

## Getting the key

Two options. **You only need one.**

### Free — Google Gemini (no card, no payment)

1. In Chrome, go to **aistudio.google.com**
2. Sign in with the Google account already on your Chromebook
3. Click **Get API key** → **Create API key**
4. Copy it. It starts with `AIza`

That's it. No card, no trial clock, no parental permission needed. There's a
daily limit on how much you can use, which for one person talking to Jarvis is
generous.

### Paid — Anthropic Claude (needs a card, and a grown-up's yes)

1. **console.anthropic.com** → sign up
2. **Billing** → add **$5** of credit
3. **API keys** → **Create key**. It starts with `sk-`

Better answers than the free one, and about a dollar a month in practice. But
it needs a card attached, so that's a conversation to have with your parents
rather than a thing to work around.

### Either way

```
jarvis key
```

Paste whichever key you have. **He works out which is which himself** and
checks it actually works before saving it. Pasting into the Terminal shows
nothing on screen — that's on purpose. Just paste and press Enter.

---

## Part 4 — Say hello

```
jarvis
```

That's the whole command. **A browser tab opens with Jarvis in it** — dark,
with a glowing reactor in the middle.

**Now say "hey Jarvis" out loud.** The reactor speeds up and brightens while
he's listening, and he answers: *"Hello, sir. What can I do for you?"*

You can also type in the box at the bottom of the tab. Both work, always.

He calls you **sir**, not by name — the computer voice mangles most real names,
and hearing yours said wrong every single time gets old fast. If you'd rather
he used it, see *Being called by your name* below.

**The first time each day** he also gives you the morning rundown without being
asked — your portfolio, what moved overnight, anything he's watching, and
whether you're clear to day trade. After that he just says hello, because
hearing the whole briefing every time you say his name gets old fast.

Then just talk:

```
how's my portfolio
what setups are you seeing
what's eating my disk space
open NVDA's chart
teach me to day trade
can I trade today
```

You can also just type at the `>` prompt any time — talking and typing both
work, always.

**You'll see what he does.** Every time he touches your computer it prints a
line, like `· run_command  du -sh ~/Downloads`. That's him working, live.

**When he wants to do something that could break things** — delete a file,
install something — he stops and asks:

```
  Jarvis wants to: run: rm old-notes.txt
  'rm' can delete, install, or reach the network
  Allow it? [y/N]
```

Type `y` and Enter to allow it. Anything else means no. A few things he won't
do at all even if you say yes, and he'll tell you which.

To stop him, hold **Ctrl + C**.

**If he says he's using built-in routing**, he has no API key and can only
answer finance questions. That is almost certainly why he says "I don't know"
to ordinary sentences. Fix it in one command:

```
jarvis key
```

Paste your key when it asks. It checks the key actually works before saving it.

**If he sounds robotic**, his good voice didn't install. Run:

```
jarvis voice --install
```

That downloads a British male neural voice (about 60 MB, once). `jarvis voice`
on its own tells you what's missing, and `jarvis voice --test` makes him say
something so you can hear it.

> **If "hey Jarvis" does nothing**, two things to check. First, ChromeOS has
> to give Linux the microphone: **Settings → Linux → Microphone**, turn it on,
> then restart Linux. Second, run `./setup.sh` again — it installs the
> microphone packages and tells you if they failed. Typing works regardless.

---

## Being called by your name

He says "sir" by default. To have him use your name instead, paste this once:

```
echo 'export JARVIS_USE_NAME=1' >> ~/.bashrc
```

If he pronounces it wrong, spell it out the way it *sounds*:

```
echo 'export JARVIS_SPOKEN_NAME=Kayleb' >> ~/.bashrc
```

Close the Terminal and reopen it. Your name still shows correctly on screen —
only what he says out loud changes.

**Prefer no window?** `echo 'export JARVIS_WINDOW=0' >> ~/.bashrc` keeps him in
the Terminal. Everything works the same either way.

---

## Part 5 — Every time after this

When you close the Terminal and come back later, you have to point it at Jarvis
again. **Two lines, every time:**

```
cd ~/jarvis
source .venv/bin/activate
```

Then `jarvis`, or anything else.

### Make that automatic (optional but worth it)

Paste this **once** and you'll never type those two lines again — a new terminal
will start inside Jarvis's folder, ready to go:

```
echo 'cd ~/jarvis && source .venv/bin/activate' >> ~/.bashrc
```

Close the Terminal, open it again, and just type `jarvis`.

---

## Adding your real money and trades

Jarvis only knows what you tell him. Nothing connects to a broker — **he can
never place a trade**, so nothing here moves real money. You're keeping a record
so his numbers about *you* are honest.

```
jarvis deposit 500
jarvis buy AAPL 2 182.30
jarvis sell AAPL 2 186.10
jarvis mark-close
```

That last one records the day's closing value, which is how he has a
"yesterday's profit and loss" number to greet you with tomorrow. Run it at the
end of each day.

---

## Things that will probably go wrong

**`command not found: jarvis`**
You skipped Part 5. Run `cd ~/jarvis` then `source .venv/bin/activate`.

**`Permission denied` when running `./setup.sh`**
Run `chmod +x setup.sh` once, then try again.

**Pasting doesn't work**
Use `Ctrl + Shift + V`, not `Ctrl + V`. Or right-click.

**`Market data: synthetic` when you run `jarvis status`**
He couldn't reach Yahoo Finance, so he's using simulated prices. Everything
works; the numbers just aren't real. Check your Wi-Fi.

**Every setup says "untested"**
Run `jarvis bootstrap`.

**"hey Jarvis" doesn't do anything**
The microphone packages aren't installed, or ChromeOS hasn't given the Linux
container mic access (Settings → Linux → Microphone). Typing works regardless.

**He says he's using built-in routing**
No API key. Run `./setup.sh` again and paste one when it asks.

**He refuses something and you think he's wrong**
Some things are refused permanently and no amount of asking changes it — that
list is in `jarvis/safety/permissions.py` and it's short. Everything else just
needs you to click yes. `jarvis` → ask him to show you the audit trail to see
exactly what he's done and what was blocked.

**You want to wipe everything and start fresh**
`rm -rf ~/jarvis/.venv` then `./setup.sh` reinstalls.
`rm -rf ~/.jarvis` erases his memory — your name, trades, everything he learned.

**Something failed and you want the real error**
Add `--verbose`, like `jarvis --verbose wake`. It shows the technical detail he
normally keeps to himself.

---

## If Linux isn't available on your Chromebook

Use **GitHub Codespaces** instead. It runs Jarvis on Google's… actually on
Microsoft's computers, and shows you the screen in your Chrome browser. Free for
about 60 hours a month, and this repo is already set up to install itself.

1. In Chrome, go to `github.com/calebfields3-ctrl/jarvis`
2. Sign in.
3. Click the branch button near the top (it says `main`) and choose
   **`claude/jarvis-ai-trading-assistant-2dv4oy`**.
4. Click the green **Code** button → **Codespaces** tab → **Create codespace on
   branch**.
5. Wait 3–5 minutes while it builds and installs itself.
6. When the editor appears, find the black panel at the bottom — that's the
   terminal. Type `jarvis wake`.

Everything else in this guide works the same from there. Remember to **stop**
the codespace when you're done (Code → Codespaces → ⋯ → Stop) so it doesn't
use up your free hours sitting idle.

---

## What he can and can't reach

He has real control of the Linux container — a shell, your files in there, and
the ability to open pages in Chrome. He does not have ChromeOS. He can't read
your Gmail, click your tabs, or see your Downloads folder, because the Linux
container is a sealed box and that's the whole reason it's safe to turn on.
That's a hardware fact, not a setting.

Inside his box, three things sit between him and anything destructive:

1. A short list of things he will never do, with or without your permission.
2. Anything that deletes, installs, or reaches the network stops and asks you.
3. He's confined to your home folder, and `~/.ssh`, `~/.aws` and the system
   folders are off limits even inside it.

Everything he does is written to `~/.jarvis/audit.jsonl`. Ask him for the audit
trail any time.

---

## Two things worth saying plainly

**Jarvis never places a trade.** There's no broker connection anywhere in the
code. He analyses, sizes positions, and warns you. Every decision to actually
buy or sell is yours, made in your own brokerage account.

**Day trading is the hardest way to make money in markets.** Most people who try
it lose money, and the losses concentrate among those trading most actively.
He's built to respect that — he caps your position sizes, blocks you when you
hit your own limits, and tells you plainly when your results are losing money
rather than finding an encouraging way to phrase it. That makes the risks
visible. It doesn't make them go away. Start with small amounts you can afford
to lose while you learn.
