"""Backtest loop: wires a Market, an OrderBook and one Strategy together.

Each step:
  1. fair value advances (the move informed flow already knew about)
  2. background liquidity cancels and re-quotes around the new fair; it
     always rejoins at the back of a level, so MM orders that held their
     price keep their queue priority over it
  3. the MM's quote instruction from `latency` steps ago arrives at the
     exchange and rests through OrderBook.replace: same price keeps queue
     position, a new price joins the back of that level. The quote decided
     THIS step (from this step's state) only rests `latency` steps from
     now; until then flow keeps trading against the old resting quote.
     latency=0 means the decision rests the same step, as before.
  4. counterparty flow arrives as marketable limit orders and consumes
     resting liquidity strictly in price-time order; the MM only fills
     after everything ahead of it at that price is gone
  5. MM fills update inventory and cash, PnL is marked to fair

PnL attribution:
  spread capture     signed edge versus fair at the moment of each fill
  adverse selection  the next step fair move on size traded against
                     informed counterparties (they knew it was coming)
  stale cost         the part of spread capture lost to fair moving between
                     the quote DECISION and the fill. This is where getting
                     picked off on a stale quote shows up. Adverse selection
                     only measures the NEXT move after a fill, so a pick off
                     on a move that already happened lands here instead.
                     Small but nonzero at latency 0 (a quote still rests for
                     one step), then dominates as latency grows.

The strategy never sees counterparty kind. Attribution is done here, where
the backtest knows which flow order produced each fill.
"""

from collections import deque

import numpy as np

from engine.order_book import OrderBook

MM = "mm"
FLOW = "flow"
BG = "bg"


class Backtest:
    def __init__(self, market, strategy, steps=2000,
                 maker_rebate=0.0, taker_fee=0.0,
                 vol_window=30, vol_guess=0.05, latency=0):
        self.market = market
        self.strategy = strategy
        self.steps = steps
        self.maker_rebate = maker_rebate   # credit per unit on our passive fills
        self.taker_fee = taker_fee         # charged per unit when we aggress
        self.vol_window = vol_window
        self.vol_guess = vol_guess         # used until enough moves observed
        self.latency = latency             # steps between deciding and resting

    def run(self):
        book = OrderBook()
        acct = {"inventory": 0, "cash": 0.0, "spread_capture": 0.0,
                "adverse_selection": 0.0, "stale_cost": 0.0, "rebates": 0.0,
                "fills": 0, "pending_informed": 0,
                "queue_sum": 0.0, "queue_fills": 0}
        quotes_offered = 0
        pending = deque()   # (rest_at_step, decision_fair, bid_px, bid_sz, ask_px, ask_sz)
        decision_fair = {"buy": None, "sell": None}   # fair when the live quote was decided
        moves = []
        pnl_hist, inv_hist, fair_hist = [], [], []

        def apply_mm_fill(f, fair, kind=None, taker=False, queue_pos=None):
            signed = f.size if f.buy_agent == MM else -f.size
            acct["inventory"] += signed
            acct["cash"] -= signed * f.price
            if taker:
                acct["cash"] -= self.taker_fee * f.size
            else:
                credit = self.maker_rebate * f.size
                acct["cash"] += credit
                acct["rebates"] += credit
            # positive when we buy below fair or sell above fair
            acct["spread_capture"] += signed * (fair - f.price)
            side = "buy" if f.buy_agent == MM else "sell"
            if decision_fair[side] is not None:
                acct["stale_cost"] += signed * (fair - decision_fair[side])
            if kind == "informed":
                acct["pending_informed"] += signed
            if queue_pos is not None:
                acct["queue_sum"] += queue_pos
                acct["queue_fills"] += 1
            acct["fills"] += 1

        for t in range(self.steps):
            fair_prev = self.market.fair
            fair = self.market.step()
            move = fair - fair_prev
            moves.append(move)

            # informed flow from last step traded ahead of this move
            acct["adverse_selection"] += acct["pending_informed"] * move
            acct["pending_informed"] = 0

            if len(moves) >= 5:
                vol_est = float(np.std(moves[-self.vol_window:]))
            else:
                vol_est = self.vol_guess

            # background quoter refreshes first (it is faster than us); it may
            # cross an MM quote left stale by the fair value move
            book.cancel_agent(BG)
            mm_fills = []
            for side, px, sz in self.market.generate_background():
                for f in book.add_limit(BG, side, px, sz):
                    if MM in (f.buy_agent, f.sell_agent):
                        apply_mm_fill(f, fair)   # we were passive
                        mm_fills.append(f)

            state = {
                "fair_est": fair,
                "inventory": acct["inventory"],
                "cash": acct["cash"],
                "vol_est": vol_est,
                "time_left": (self.steps - t) / self.steps,
                "book": book,
                "tick": self.market.tick,
                "queue_ahead_bid": book.queue_ahead(MM, "buy"),
                "queue_ahead_ask": book.queue_ahead(MM, "sell"),
            }
            # the decision made now joins the wire; the instruction that
            # actually rests this step was decided `latency` steps ago
            pending.append((t + self.latency, fair) + self.strategy.quote(state))
            while pending and pending[0][0] <= t:
                _, dec_fair, bid_px, bid_sz, ask_px, ask_sz = pending.popleft()
                if bid_sz > 0 and ask_sz > 0 and ask_px <= bid_px:
                    ask_px = bid_px + self.market.tick   # never cross ourselves
                decision_fair["buy"] = dec_fair if bid_sz > 0 else None
                decision_fair["sell"] = dec_fair if ask_sz > 0 else None
                quotes_offered += (1 if bid_sz > 0 else 0) + (1 if ask_sz > 0 else 0)
                for side, px, sz in (("buy", bid_px, bid_sz), ("sell", ask_px, ask_sz)):
                    for f in book.replace(MM, side, px, int(sz)):
                        if MM in (f.buy_agent, f.sell_agent):
                            apply_mm_fill(f, fair, taker=True)   # we crossed someone
                            mm_fills.append(f)

            # queue ahead of our quotes as they stand for this step, used to
            # attribute an average queue position to the fills below
            q_bid = book.queue_ahead(MM, "buy")
            q_ask = book.queue_ahead(MM, "sell")

            for order in self.market.generate_flow():
                fills = book.add_limit(FLOW, order.side, order.limit_price, order.size)
                book.cancel_agent(FLOW)   # flow is immediate or cancel
                for f in fills:
                    if MM not in (f.buy_agent, f.sell_agent):
                        continue
                    qp = q_bid if f.buy_agent == MM else q_ask
                    apply_mm_fill(f, fair, kind=order.kind,
                                  queue_pos=qp if qp is not None else 0)
                    mm_fills.append(f)
            if mm_fills:
                self.strategy.on_fills(mm_fills, state)

            pnl_hist.append(acct["cash"] + acct["inventory"] * fair)
            inv_hist.append(acct["inventory"])
            fair_hist.append(fair)

        return {
            "name": self.strategy.name,
            "history": {
                "pnl": np.array(pnl_hist),
                "inventory": np.array(inv_hist),
                "fair": np.array(fair_hist),
            },
            "fills": acct["fills"],
            "quotes_offered": quotes_offered,
            "queue_sum": acct["queue_sum"],
            "queue_fills": acct["queue_fills"],
            "spread_capture": acct["spread_capture"],
            "adverse_selection": acct["adverse_selection"],
            "stale_cost": acct["stale_cost"],
            "rebates": acct["rebates"],
        }
