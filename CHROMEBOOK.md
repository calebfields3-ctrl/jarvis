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
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
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

If anything goes wrong it stops and tells you exactly what to do. **Running
`./setup.sh` again is always safe** — it skips whatever already worked.

**You'll know it worked** when you see green text saying `Jarvis is ready.`

---

## Part 4 — Say hello

```
jarvis wake
```

He should greet you by name and tell you your portfolio is empty. **That's it
working.**

Now try the real thing:

```
jarvis run
```

Type `hey jarvis` and press Enter. He'll greet you. From then on just type
normal sentences:

```
how's my portfolio
what setups are you seeing
teach me to day trade
tell me about NVDA
can I trade today
keep an eye on NVDA above 200
goodbye
```

To shut it down completely, hold **`Ctrl + C`**.

---

## Part 5 — Every time after this

When you close the Terminal and come back later, you have to point it at Jarvis
again. **Two lines, every time:**

```
cd ~/jarvis
source .venv/bin/activate
```

Then `jarvis wake`, or anything else.

### Make that automatic (optional but worth it)

Paste this **once** and you'll never type those two lines again — a new terminal
will start inside Jarvis's folder, ready to go:

```
echo 'cd ~/jarvis && source .venv/bin/activate' >> ~/.bashrc
```

Close the Terminal, open it again, and just type `jarvis wake`.

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
