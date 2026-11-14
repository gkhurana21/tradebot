"""Per-stream paper portfolio: cash + positions, mark-to-market, kill switch.

Fill price convention: the close of the newly completed bar. The cost model's
slippage_bps absorbs the gap between that close and a realistic fill. All
fills, positions, and state are persisted through Store so the daemon can
crash-resume.
"""

from __future__ import annotations

from bot.models import EquitySnapshot, PaperFill, Position
from bot.store import Store


class PaperPortfolio:
    def __init__(self, stream: str, universe: str, store: Store,
                 start_equity: float, kill_switch_dd: float,
                 cost_per_side: float):
        self.stream = stream
        self.universe = universe
        self.store = store
        self.cost_per_side = cost_per_side
        self.kill_switch_dd = kill_switch_dd

        state = store.load_stream_state(stream)
        if state is None:
            self.cash = start_equity
            self.high_water_mark = start_equity
            self.halted = False
            self.last_bar_ts = 0
        else:
            self.cash = state["cash_usd"]
            self.high_water_mark = state["high_water_mark"]
            self.halted = state["halted"]
            self.last_bar_ts = state["last_bar_ts"]
        self.positions = store.load_positions(stream)

    # ── accounting ────────────────────────────────────────────────────────
    def equity(self, prices: dict[str, float]) -> float:
        value = self.cash
        for sym, pos in self.positions.items():
            value += pos.units * prices[sym]
        return value

    def gross_exposure(self, prices: dict[str, float]) -> float:
        eq = self.equity(prices)
        if eq <= 0:
            return 0.0
        notional = sum(abs(p.units) * prices[s]
                       for s, p in self.positions.items())
        return notional / eq

    # ── trading ──────────────────────────────────────────────────────────
    def rebalance(self, target_weights: dict[str, float],
                  prices: dict[str, float], ts: int,
                  reason: str = "rebalance") -> list[PaperFill]:
        if self.halted:
            return []
        eq = self.equity(prices)
        fills: list[PaperFill] = []
        for sym, w in target_weights.items():
            if sym not in prices:
                continue
            price = prices[sym]
            current_units = self.positions.get(
                sym, Position(self.stream, sym, 0.0, 0.0)).units
            target_units = (w * eq) / price
            delta = target_units - current_units
            notional = abs(delta) * price
            if notional < 1.0:            # ignore dust rebalances
                continue
            fills.append(self._fill(sym, ts, price, delta, notional, reason))
        self._persist_state(ts)
        return fills

    def _fill(self, sym: str, ts: int, price: float, delta: float,
              notional: float, reason: str) -> PaperFill:
        cost = notional * self.cost_per_side
        self.cash -= delta * price + cost
        pos = self.positions.get(sym, Position(self.stream, sym, 0.0, 0.0))
        new_units = pos.units + delta
        if abs(new_units) < 1e-12:
            pos.units, pos.avg_price = 0.0, 0.0
        elif pos.units == 0 or (pos.units > 0) != (new_units > 0):
            pos.units, pos.avg_price = new_units, price   # fresh/flipped lot
        elif abs(new_units) > abs(pos.units):              # adding
            pos.avg_price = ((abs(pos.units) * pos.avg_price
                              + abs(delta) * price) / abs(new_units))
            pos.units = new_units
        else:                                              # reducing
            pos.units = new_units
        self.positions[sym] = pos
        if pos.is_flat:
            del self.positions[sym]
        self.store.save_position(
            Position(self.stream, sym, pos.units if not pos.is_flat else 0.0,
                     pos.avg_price))
        fill = PaperFill(self.stream, self.universe, sym, ts, price, delta,
                         notional, cost, reason)
        return self.store.insert_fill(fill)

    # ── mark to market + kill switch ─────────────────────────────────────
    def snapshot(self, prices: dict[str, float], ts: int) -> EquitySnapshot:
        eq = self.equity(prices)
        self.high_water_mark = max(self.high_water_mark, eq)
        snap = EquitySnapshot(self.stream, ts, eq, self.cash,
                              self.gross_exposure(prices),
                              len(self.positions))
        self.store.insert_equity(snap)

        if (not self.halted
                and eq < self.high_water_mark * (1 - self.kill_switch_dd)):
            self._liquidate(prices, ts)
            self.halted = True
        self._persist_state(ts)
        return snap

    def _liquidate(self, prices: dict[str, float], ts: int) -> None:
        for sym in list(self.positions):
            pos = self.positions[sym]
            price = prices.get(sym, pos.avg_price)
            notional = abs(pos.units) * price
            self._fill(sym, ts, price, -pos.units, notional, "kill_switch")

    def _persist_state(self, ts: int) -> None:
        self.last_bar_ts = max(self.last_bar_ts, ts)
        self.store.save_stream_state(self.stream, self.cash,
                                     self.high_water_mark, self.halted,
                                     self.last_bar_ts)
