"""Deterministic risk engine.

This is the safety layer. Claude proposes; the risk engine disposes.
It converts a Decision into a concrete, size-limited Order (or rejects it),
enforcing hard caps that no amount of model confidence can override.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from .types import Account, Decision, Order, OrderType, Side


@dataclass
class RiskConfig:
    max_position_pct: float = 0.20      # max % of equity in one position
    risk_per_trade_pct: float = 0.01    # % of equity risked to the stop
    stop_loss_pct: float = 0.02         # default stop distance from entry
    take_profit_pct: float = 0.04       # default target distance
    max_daily_drawdown_pct: float = 0.05  # kill switch for the day
    max_open_positions: int = 3
    min_confidence: float = 0.55        # below this, no trade

    @staticmethod
    def profile(name: str) -> "RiskConfig":
        """Named risk profiles. Read the honest note before picking 'aggressive'.

        conservative — small size, tight daily halt. Survives long losing streaks.
        balanced     — the sane default.
        aggressive   — larger size + wider daily loss. HIGHER RISK OF RUIN, NOT a
                       higher expected return. On a small account a normal losing
                       streak here can take you toward zero, and -90% needs +900%
                       just to recover. Use only money you can lose entirely.
        """
        presets = {
            "conservative": dict(max_position_pct=0.10, risk_per_trade_pct=0.005,
                                 stop_loss_pct=0.015, take_profit_pct=0.03,
                                 max_daily_drawdown_pct=0.03, min_confidence=0.65),
            "balanced": dict(),  # the dataclass defaults
            "aggressive": dict(max_position_pct=0.35, risk_per_trade_pct=0.02,
                               stop_loss_pct=0.03, take_profit_pct=0.09,
                               max_daily_drawdown_pct=0.10, min_confidence=0.5),
        }
        if name not in presets:
            raise ValueError(f"unknown profile {name!r}; choose from {list(presets)}")
        return RiskConfig(**presets[name])


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None):
        self.cfg = config or RiskConfig()
        self._day_start_equity: float | None = None
        self._day: date | None = None
        self._halted = False

    def start_day(self, equity: float) -> None:
        """Begin a new session: reset the drawdown baseline and clear any halt."""
        self._day_start_equity = equity
        self._halted = False

    def roll_day(self, ts: datetime, equity: float) -> bool:
        """Start a new session when `ts` falls on a later UTC day.

        Call this once per bar, before the bar is marked, with the equity the
        account had going into it. A halt therefore lasts for the rest of the
        day it tripped on, whatever equity does in the meantime, and the next
        day starts fresh from that day's opening equity. Returns True on a roll.
        A timestamp without a timezone is taken to be UTC; feeds attach UTC,
        and the CSV loader rejects files that leave the timezone out.
        """
        day = (ts.astimezone(timezone.utc) if ts.tzinfo else ts).date()
        if self._day is None:
            self._day = day
            return False
        if day <= self._day:
            return False
        self._day = day
        self.start_day(equity)
        return True

    @property
    def halted(self) -> bool:
        return self._halted

    def check_kill_switch(self, account: Account) -> bool:
        """Halt trading if daily drawdown breached. Returns True if halted."""
        if self._day_start_equity is None:
            self._day_start_equity = account.equity
        dd = (self._day_start_equity - account.equity) / self._day_start_equity
        if dd >= self.cfg.max_daily_drawdown_pct:
            self._halted = True
        return self._halted

    def size_order(
        self, decision: Decision, account: Account, price: float
    ) -> tuple[Order | None, str]:
        """Turn a Decision into a risk-bounded Order, or reject with a reason."""
        if self._halted:
            return None, "halted: daily drawdown kill-switch active"
        if decision.veto or decision.side == Side.FLAT:
            return None, "no-trade: brain vetoed or flat"
        if decision.confidence < self.cfg.min_confidence:
            return None, (
                f"reject: confidence {decision.confidence:.2f} "
                f"< min {self.cfg.min_confidence:.2f}"
            )

        pos = account.position(decision.symbol)
        open_count = sum(1 for p in account.positions.values() if not p.is_flat)
        if pos.is_flat and open_count >= self.cfg.max_open_positions:
            return None, "reject: max open positions reached"

        # Position size from fixed-fractional risk: risk_amount / stop_distance.
        stop_dist = max(price * self.cfg.stop_loss_pct, 1e-9)
        risk_amount = account.equity * self.cfg.risk_per_trade_pct
        qty_by_risk = risk_amount / stop_dist

        # Cap by max position notional.
        max_notional = account.equity * self.cfg.max_position_pct
        qty_by_notional = max_notional / max(price, 1e-9)

        qty = min(qty_by_risk, qty_by_notional) * clamp(decision.size_hint, 0.0, 1.0)

        # Account for existing same-direction exposure so repeated signals
        # can't stack a position beyond the max-notional cap.
        same_dir = (pos.qty > 0 and decision.side == Side.BUY) or (
            pos.qty < 0 and decision.side == Side.SELL)
        if same_dir:
            remaining = qty_by_notional - abs(pos.qty)
            if remaining <= 0:
                return None, "reject: position already at max notional"
            qty = min(qty, remaining)
        if qty <= 0:
            return None, "reject: computed size is zero"

        if decision.side == Side.BUY:
            sl = price * (1 - self.cfg.stop_loss_pct)
            tp = price * (1 + self.cfg.take_profit_pct)
        else:  # SELL / short
            sl = price * (1 + self.cfg.stop_loss_pct)
            tp = price * (1 - self.cfg.take_profit_pct)

        order = Order(
            ts=decision.ts,
            symbol=decision.symbol,
            side=decision.side,
            qty=round(qty, 8),
            type=OrderType.MARKET,
            stop_loss=round(sl, 8),
            take_profit=round(tp, 8),
            tag=f"conf={decision.confidence:.2f}",
        )
        return order, "ok"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
