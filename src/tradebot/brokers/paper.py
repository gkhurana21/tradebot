"""In-memory paper-trading broker.

Simulates fills with configurable fees and slippage, tracks cash/positions,
and marks equity to the latest price. Used for backtests and paper-live runs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..core.metrics import Trade
from ..core.types import Account, Fill, Order, Position, Side
from .base import BrokerAdapter


class _OpenLot:
    """Tracks the currently-open round trip for a symbol."""
    __slots__ = ("side", "entry_ts", "entry_price", "qty", "realized", "fees")

    def __init__(self, side, ts, price, qty, fee):
        self.side = side
        self.entry_ts = ts
        self.entry_price = price
        self.qty = qty
        self.realized = 0.0
        self.fees = fee


class PaperBroker(BrokerAdapter):
    def __init__(
        self,
        starting_cash: float = 10_000.0,
        fee_pct: float = 0.001,      # 10 bps taker fee
        slippage_pct: float = 0.0005,  # 5 bps adverse slippage
    ):
        self._acct = Account(cash=starting_cash, equity=starting_cash)
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self._marks: dict[str, float] = {}
        self._marks_ts: dict[str, datetime] = {}
        self.fills: list[Fill] = []
        self.trades: list[Trade] = []          # completed round trips
        self._open: dict[str, _OpenLot] = {}   # symbol -> open lot

    # --- market data plumbing (backtester feeds marks in) -----------------
    def mark(self, symbol: str, price: float, ts: datetime | None = None) -> None:
        self._marks[symbol] = price
        if ts is not None:
            self._marks_ts[symbol] = ts
        self._revalue()

    def price(self, symbol: str) -> float:
        return self._marks[symbol]

    # --- BrokerAdapter -----------------------------------------------------
    def account(self) -> Account:
        return self._acct

    def submit(self, order: Order) -> Fill:
        px = self._marks[order.symbol]
        # Adverse slippage: buys fill higher, sells fill lower.
        if order.side == Side.BUY:
            fill_px = px * (1 + self.slippage_pct)
        else:
            fill_px = px * (1 - self.slippage_pct)
        fee = abs(order.qty) * fill_px * self.fee_pct
        signed = order.qty if order.side == Side.BUY else -order.qty
        self._apply(order.symbol, signed, fill_px, fee, order.stop_loss,
                    order.take_profit, order.tag or "", order.ts)
        fill = Fill(order.ts, order.symbol, order.side, order.qty, fill_px,
                    fee, order.tag)
        self.fills.append(fill)
        return fill

    def close(self, symbol: str, reason: str = "close") -> Fill | None:
        pos = self._acct.position(symbol)
        if pos.is_flat:
            return None
        px = self._marks[symbol]
        side = Side.SELL if pos.qty > 0 else Side.BUY
        if side == Side.BUY:
            fill_px = px * (1 + self.slippage_pct)
        else:
            fill_px = px * (1 - self.slippage_pct)
        qty = abs(pos.qty)
        fee = qty * fill_px * self.fee_pct
        ts = self._marks_ts.get(symbol, _now())
        self._apply(symbol, -pos.qty, fill_px, fee, None, None, reason, ts)
        fill = Fill(ts, symbol, side, qty, fill_px, fee, reason)
        self.fills.append(fill)
        return fill

    # --- internals ---------------------------------------------------------
    def _apply(self, symbol, signed_qty, price, fee, sl, tp, tag, ts) -> None:
        pos = self._acct.position(symbol)
        new_qty = pos.qty + signed_qty
        realized = 0.0
        # Realize P&L on the portion that reduces/closes the position.
        if pos.qty != 0 and (pos.qty > 0) != (signed_qty > 0):
            closing = min(abs(signed_qty), abs(pos.qty))
            realized = closing * (price - pos.avg_price) * (1 if pos.qty > 0 else -1)
            self._acct.cash += realized
        # Update average price when adding in the same direction.
        if pos.qty == 0 or (pos.qty > 0) == (signed_qty > 0):
            total = pos.avg_price * abs(pos.qty) + price * abs(signed_qty)
            pos.avg_price = total / max(abs(new_qty), 1e-12) if new_qty != 0 else 0.0
        pos.qty = new_qty if abs(new_qty) > 1e-12 else 0.0

        self._track_trade(symbol, signed_qty, price, fee, realized, tag, ts, pos)

        if pos.is_flat:
            pos.avg_price = 0.0
            pos.stop_loss = pos.take_profit = None
        else:
            pos.stop_loss, pos.take_profit = sl, tp
        self._acct.cash -= fee
        self._revalue()

    def _track_trade(self, symbol, signed_qty, price, fee, realized, tag, ts, pos):
        """Build round-trip Trade records as positions open and close."""
        lot = self._open.get(symbol)
        if lot is None and not pos.is_flat:
            # New position opened.
            side = Side.BUY if signed_qty > 0 else Side.SELL
            self._open[symbol] = _OpenLot(side, ts, price, abs(signed_qty), fee)
        elif lot is not None:
            lot.fees += fee
            lot.realized += realized
            if pos.is_flat:
                # Round trip complete.
                self.trades.append(Trade(
                    symbol=symbol, side=lot.side, entry_ts=lot.entry_ts,
                    exit_ts=ts, entry_price=lot.entry_price, exit_price=price,
                    qty=lot.qty, pnl=lot.realized - lot.fees, fees=lot.fees,
                    reason_exit=tag,
                ))
                del self._open[symbol]
            elif (pos.qty > 0) == (lot.side == Side.BUY):
                # Added in same direction: grow the lot.
                lot.qty += abs(signed_qty)

    def _revalue(self) -> None:
        equity = self._acct.cash
        for sym, pos in self._acct.positions.items():
            if not pos.is_flat and sym in self._marks:
                equity += pos.unrealized(self._marks[sym])
        self._acct.equity = equity


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
