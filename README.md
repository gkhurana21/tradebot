# tradebot — a Claude-driven trading system

An automated trading system where **Claude is the analysis brain** and a
**deterministic risk engine** has final say over every order. Built
paper-first on macOS, with an adapter architecture so execution venues
(paper → crypto REST → MetaTrader 5) are interchangeable.

> ⚠️ **Not financial advice. Trading risks real money.** This is a software
> project. Run it in paper mode. Nothing here executes live trades until you
> wire a live adapter and flip it on yourself — with your own eyes on the risk.

## The loop

```
data feed → strategy (signals) → Claude brain (decision) → risk engine (gate + size) → broker → logs
                                        │                          │
                                  proposes only            hard limits, kill switch,
                                                            final veto
```

Every component is swappable behind an interface, and a backtest runs the
**exact same code path** a live run would.

## Run it now (zero dependencies, offline)

```bash
python3 scripts/run_backtest.py                 # synthetic data, heuristic brain
python3 scripts/run_backtest.py --verbose       # see every decision
python3 tests/test_core.py                       # 8 tests on the money logic
```

Use real crypto data (needs internet, no API key):

```bash
python3 scripts/run_backtest.py --source coinbase --symbol ETH-USD
```

Enable the **real Claude brain**:

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python3 scripts/run_backtest.py                 # brain mode -> "LLM (Claude)"
```

## Layout

| Path | Role |
|------|------|
| `core/types.py` | Shared dataclasses (Candle, Signal, Decision, Order, Position, Account) |
| `core/strategy.py` | EMA/RSI trend, Bollinger mean-reversion, + agreement **Ensemble** |
| `core/risk.py` | Risk engine: sizing, hard caps, exposure-aware, kill switch |
| `core/metrics.py` | Sharpe, Sortino, Calmar, profit factor, expectancy, trade ledger |
| `brokers/base.py` | `BrokerAdapter` interface |
| `brokers/paper.py` | Paper broker with fees, slippage, round-trip trade tracking |
| `brain/brain.py` | Claude brain — on-chain context aware, optional live **web-search** |
| `data/feed.py` | Synthetic / Coinbase / CSV candle feeds |
| `data/onchain.py` | On-chain signals via public RPC (gas, stablecoin supply) |
| `backtest/engine.py` | Single-symbol event-driven backtester |
| `backtest/portfolio.py` | Multi-symbol portfolio backtester (shared account/risk) |
| `backtest/optimize.py` | Walk-forward parameter search (out-of-sample scored) |
| `backtest/dashboard.py` | Self-contained HTML dashboard (equity curve, tiles, trades) |
| `live/engine.py` | Live trading loop — same code path as backtest, JSON events |
| `live/webhook.py` | TradingView webhook receiver (stdlib, shared-secret auth) |
| `cli.py` | Unified `python -m tradebot` entry point |

## Unified CLI

Everything is one command (`PYTHONPATH=src python3 -m tradebot <cmd>`):

```bash
python3 -m tradebot analyze --source coinbase --symbol BTC-USD   # read the chart: trend, S/R, patterns
python3 -m tradebot backtest --symbol BTC-USD --profile balanced --verbose
python3 -m tradebot portfolio --onchain            # 3-symbol ensemble + on-chain context
python3 -m tradebot optimize --bars 2000 --folds 4 # walk-forward parameter search
python3 -m tradebot live --iterations 400 --poll 0 # live paper loop (streams JSON events)
python3 -m tradebot webhook --secret YOURSECRET    # TradingView alert receiver -> live trader
python3 -m tradebot dashboard --out dashboard.html # HTML report
python3 -m tradebot onchain                        # live on-chain snapshot
```

### Chart pattern & trend analysis

`core/patterns.py` reads a candle window like a trader would: trend direction +
strength (regression + ADX), support/resistance from clustered swing pivots,
candlestick patterns (engulfing, hammer, shooting star, doji), and chart patterns
(range breakout, double top/bottom). The `PatternStrategy` trades pattern+trend
confluence, and the full read is attached to every signal for the brain to weigh.

### The "$1 → $1M" question, answered with a simulator

`python3 -m tradebot montecarlo` runs thousands of simulated traders so you can
see the real odds instead of guessing. What it shows:

| Assumed edge | Careful 5% sizing | Result |
|---|---|---|
| 50% (no edge) | yes | ~0% reach $1M, bleed toward $0 |
| 51% (thin edge) | yes | ~0% reach $1M |
| 55% @1:1 net of fees | yes | ~87–92% reach $1M — but ~3,700 trades (years) |
| 55% edge | **aggressive 50%/trade** | median ends at **$0.02** — overbetting ruins you *with* an edge |

**The compounding isn't the impossible part — a durable edge is.** If you can
prove a real ~55%/1:1 edge *net of costs, out-of-sample, over thousands of
trades*, careful fixed-fractional compounding can work. Almost no retail strategy
has that edge net of fees and slippage. The whole game is honestly hunting for
(and validating) even a small real edge — which is what the backtester, the
walk-forward optimizer, and the analysis engine are for.

### Risk profiles — read this honestly

`--profile conservative|balanced|aggressive`. Same signals, three risk levels.
On identical data, aggressive turned a **-2.8%** result into **-10.5%** with a
13.8% drawdown. **Aggressive raises the risk of ruin; it does not raise expected
return.** On a small account, a normal losing streak at high risk-per-trade can
take you toward zero — and -90% needs +900% just to get back. There is no setting
that produces "insane returns from minimum capital." Anything claiming to is a way
to lose money. Size risk to what you can afford to lose entirely.

### TradingView integration

Point a Pine Script alert's webhook at `http://<host>:8787/webhook` with a JSON message:

```json
{"secret": "YOURSECRET", "symbol": "BTC-USD", "side": "buy",
 "strength": 0.8, "reason": "supertrend flip", "price": {{close}}}
```

The receiver authenticates the secret, converts it to a `Signal`, and routes it
through the **same brain + risk engine** as everything else — a TradingView alert
still can't move money on its own.

Enable **news-aware** decisions (Claude web-searches headlines before deciding):

```python
from tradebot.brain.brain import Brain
brain = Brain(news_aware=True)   # needs ANTHROPIC_API_KEY + `pip install anthropic`
```

## Safety design

- The brain **only proposes**. It cannot place an order.
- The risk engine converts proposals to bounded orders and can reject/veto.
- Hard limits: max position %, risk-per-trade, stop-loss, max open positions,
  min confidence, and a daily-drawdown **kill switch** that halts trading.

## Roadmap

- [x] Adapter architecture + paper broker + risk engine
- [x] EMA/RSI strategy + Claude brain (with offline fallback)
- [x] Event-driven backtester + tests (11 passing)
- [x] Pro metrics: Sharpe, Sortino, Calmar, profit factor, trade ledger
- [x] Second strategy (Bollinger) + agreement ensemble
- [x] Multi-symbol portfolio backtester
- [x] On-chain signals (gas, stablecoin supply) via public RPC
- [x] News-aware brain (Claude web-search) + on-chain context
- [x] HTML dashboard (equity curve, stat tiles, trade log)
- [x] TradingView webhook receiver (Pine alerts → signals)
- [x] Live paper loop (real-time feed, JSON event stream)
- [x] Walk-forward optimization (out-of-sample validated)
- [x] Unified CLI (`python -m tradebot`)
- [ ] MetaTrader 5 adapter (Windows VPS) for forex
- [ ] SQLite persistence + regime detection
- [ ] Prove Claude brain edge vs heuristic on real data
```
