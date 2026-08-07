#!/usr/bin/env bash
#
# One-command setup for Jarvis.
#
#   ./setup.sh
#
# Safe to run again if something goes wrong -- it skips whatever is already
# done. Every failure prints what broke and what to do about it, rather than a
# stack trace, because the person running this may be new to a terminal.

set -u

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; OFF='\033[0m'

say()  { printf "${GREEN}==>${OFF} %s\n" "$1"; }
warn() { printf "${YELLOW}[!]${OFF} %s\n" "$1"; }
die()  { printf "\n${RED}Setup stopped.${OFF}\n  %s\n\n" "$1" >&2; exit 1; }

cd "$(dirname "$0")" || die "Could not find the Jarvis folder."

# ---------------------------------------------------------------- 1. Python
say "Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
    die "Python 3 is not installed. Run this first, then try again:
    sudo apt update && sudo apt install -y python3 python3-venv python3-pip git"
fi

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)' 2>/dev/null || echo 0)
if [ "$PY_OK" != "1" ]; then
    die "Your Python is $(python3 --version 2>&1 | cut -d" " -f2), but Jarvis needs 3.10 or newer.
    On Debian or a Chromebook, try:  sudo apt update && sudo apt install -y python3"
fi
say "Python $(python3 --version 2>&1 | cut -d' ' -f2) -- good."

# ------------------------------------------------------- 2. Virtual environment
# A venv keeps Jarvis's packages separate from the system Python, so nothing
# here can break anything else on the machine.
if [ ! -d .venv ]; then
    say "Creating the virtual environment (this keeps Jarvis self-contained)..."
    python3 -m venv .venv 2>/dev/null || die "Could not create the virtual environment.
    The python3-venv package is probably missing. Run:
    sudo apt update && sudo apt install -y python3-venv"
else
    say "Virtual environment already exists -- reusing it."
fi

# shellcheck disable=SC1091
. .venv/bin/activate || die "Could not activate the virtual environment. Try deleting the .venv folder and running this again."

# ------------------------------------------------------------- 3. Install
say "Installing Jarvis and his dependencies. This takes a few minutes..."
python -m pip install --quiet --upgrade pip >/dev/null 2>&1

if python -m pip install --quiet -e ".[all]" 2>/dev/null; then
    say "Installed with live market data support."
else
    warn "The full install failed -- most likely no internet, or pandas could not build."
    warn "Falling back to the core install. Jarvis still runs, using simulated prices."
    python -m pip install --quiet -e . || die "Even the core install failed. Check your internet connection and run ./setup.sh again."
fi

command -v jarvis >/dev/null 2>&1 || die "Jarvis installed but is not on the PATH. Try closing the terminal, reopening it, and running ./setup.sh again."

# ------------------------------------------------------------- 4. Bootstrap
# Without a track record he reports every setup as untested, which is a poor
# and misleading first impression.
if jarvis status 2>/dev/null | grep -q "Graded calls     : 0"; then
    say "Building his track record from historical data (about 30 seconds)..."
    jarvis bootstrap --sample 30 >/dev/null 2>&1 || warn "Bootstrap did not finish. Run 'jarvis bootstrap' yourself later."
else
    say "Track record already built -- skipping."
fi

# ------------------------------------------------------- 5. Auto-activation
# Without this, the very next thing people type is `jarvis wake` and they get
# "command not found", because the venv is not active in their shell. Telling
# them to activate it is not enough -- it needs to just work.
ACTIVATE_LINE="cd $(pwd) && source .venv/bin/activate"
if [ -f "$HOME/.bashrc" ] && grep -Fq "$ACTIVATE_LINE" "$HOME/.bashrc" 2>/dev/null; then
    say "New terminals already start inside Jarvis -- nothing to do."
else
    printf "# Added by Jarvis setup.sh -- start new shells ready to go\n%s\n" \
        "$ACTIVATE_LINE" >> "$HOME/.bashrc"
    say "New terminals will now start with Jarvis ready."
fi

# ---------------------------------------------------------------- 6. Done
printf "\n${GREEN}${BOLD}Jarvis is ready.${OFF}\n\n"
printf "  ${BOLD}Run this one line now${OFF} (just this once -- new terminals do it for you):\n\n"
printf "    ${BOLD}source .venv/bin/activate${OFF}\n\n"
printf "  Then try:\n\n"
printf "    ${BOLD}jarvis wake${OFF}       he says hello\n"
printf "    ${BOLD}jarvis run${OFF}        the full thing -- then type: hey jarvis\n"
printf "    ${BOLD}jarvis brief${OFF}      pre-market briefing\n"
printf "    ${BOLD}jarvis train${OFF}      he teaches you to day trade\n\n"
printf "  Add your money and positions when you are ready:\n\n"
printf "    ${BOLD}jarvis deposit 500${OFF}\n"
printf "    ${BOLD}jarvis buy AAPL 2 182.30${OFF}\n\n"
