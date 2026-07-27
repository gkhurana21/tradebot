#!/usr/bin/env bash
# demo.sh — the whole tradebot system, end to end, in one run.
# Usage:  ./demo.sh
# Safe by default: only `compare` touches the Claude API, and only if you've set
# ANTHROPIC_API_KEY — and it's cost-guarded (haiku, max 50 calls, ~3 cents).
set -e
cd "$(dirname "$0")"

# --- API key handling (safe) ------------------------------------------------
# Priority: existing env var -> gitignored .env file -> hidden prompt.
# The key is never printed, never stored in shell history, and .env is
# gitignored + chmod 600, so it never lands in git.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "The Claude brain (step 9) needs an Anthropic API key."
  echo "Get one at https://console.anthropic.com  ->  API Keys."
  printf 'Paste your key now (input hidden), or press Enter to skip and run free: '
  read -rs KEY; echo
  # Strip any stray paste artifacts (ESC/control chars, spaces) — keep only the key.
  KEY=$(printf '%s' "$KEY" | LC_ALL=C tr -cd 'A-Za-z0-9_-')
  if [ -n "$KEY" ]; then
    export ANTHROPIC_API_KEY="$KEY"
    printf 'Save it to .env so you never re-enter it? (gitignored) [y/N] '
    read -r SAVE
    case "$SAVE" in
      y|Y) (umask 077; printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" > .env)
           echo "Saved to .env (chmod 600, gitignored)." ;;
    esac
  fi
  unset KEY
fi

# Activate the project venv (created during setup).
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "No .venv found — run:  python3 -m venv .venv && source .venv/bin/activate && pip install -e \".[llm]\""
  export PYTHONPATH=src
fi

run() { printf '\n\033[1;36m===== %s =====\033[0m\n' "$1"; shift; "$@"; }

run "1. Chart analysis (real BTC data)"        tradebot analyze --source coinbase --symbol BTC-USD
run "2. Single-symbol backtest (balanced)"     tradebot backtest --no-llm --profile balanced --bars 1500
run "3. Risk profiles: conservative vs aggressive"  bash -c '
  for p in conservative aggressive; do
    echo "-- $p --"; tradebot backtest --no-llm --profile "$p" --bars 1500 | grep Return
  done'
run "4. Multi-symbol portfolio (ensemble)"     tradebot portfolio --no-llm --bars 1200
run "5. Walk-forward optimizer (finds robust params)"  tradebot optimize --bars 2000 --folds 4
run "6. Monte Carlo: the \$1 -> \$1M reality check"    tradebot montecarlo
run "7. On-chain snapshot (live Ethereum RPC)"  tradebot onchain
run "8. Live paper loop (400 ticks, streams events)"   bash -c '
  tradebot live --iterations 400 --poll 0 --no-llm 2>&1 | grep -c "\"event\": \"fill\"" | xargs echo "paper fills:"'
run "9. Heuristic vs Claude brain (cost-guarded)"      tradebot compare --source coinbase --symbol BTC-USD
run "10. Build the HTML dashboard"             tradebot dashboard --out dashboard.html
run "11. Test suite"                           bash -c 'PYTHONPATH=src python3 tests/test_core.py | tail -1'

printf '\n\033[1;32mDone. Open dashboard.html in a browser to see the visual report.\033[0m\n'
printf 'Set ANTHROPIC_API_KEY to fill in the Claude column of step 9.\n'
