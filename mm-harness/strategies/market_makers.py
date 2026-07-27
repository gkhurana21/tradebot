"""Market making strategies, ordered as a ladder of increasing risk control.

Every strategy sees only the state dict (the interface seam):

  fair_est         current fair value estimate
  inventory        signed position in units
  cash             cash balance
  vol_est          rolling estimate of per step fair value vol
  time_left        fraction of the session remaining, 1.0 down to 0.0
  book             the OrderBook (for best_bid / best_ask / depth if wanted)
  tick             minimum price increment
  queue_ahead_bid  resting size that fills before our bid does (better
                   priced plus earlier at our price), None if no resting bid
  queue_ahead_ask  same for our ask

Queue semantics: re-quoting the same price keeps our queue position;
quoting a new price joins the back of that level, and moving away then
coming back means the old spot is gone.

quote(state) returns (bid_px, bid_sz, ask_px, ask_sz). A size of 0 means
do not quote that side. on_fills(fills, state) is called after our quotes
trade; fills use agent name "mm" for us and "flow" for counterparties.
"""

import math

MM_AGENT = "mm"


def round_down(px, tick):
    return math.floor(px / tick + 1e-9) * tick


def round_up(px, tick):
    return math.ceil(px / tick - 1e-9) * tick


class Strategy:
    name = "base"

    def quote(self, state):
        raise NotImplementedError

    def on_fills(self, fills, state):
        pass


class SymmetricMM(Strategy):
    """Rung 1: fixed half-spread around the fair estimate. No risk control
    at all, inventory drifts wherever the flow pushes it."""

    name = "Symmetric"

    def __init__(self, half_spread=0.05, size=4):
        self.half_spread = half_spread
        self.size = size

    def quote(self, state):
        tick = state["tick"]
        mid = state["fair_est"]
        bid = round_down(mid - self.half_spread, tick)
        ask = round_up(mid + self.half_spread, tick)
        return bid, self.size, ask, self.size


class InventorySkewMM(SymmetricMM):
    """Rung 2: shift both quotes against inventory so fills mean revert the
    position, and stop quoting a side entirely past max_inventory."""

    name = "InvSkew"

    def __init__(self, half_spread=0.05, size=4, skew=0.02, max_inventory=12):
        super().__init__(half_spread, size)
        self.skew = skew
        self.max_inventory = max_inventory

    def center(self, state):
        # long inventory pushes both quotes down: easier to sell, harder to buy
        return state["fair_est"] - state["inventory"] * self.skew

    def half(self, state):
        return self.half_spread

    def quote(self, state):
        tick = state["tick"]
        inv = state["inventory"]
        center = self.center(state)
        half = self.half(state)
        bid = round_down(center - half, tick)
        ask = round_up(center + half, tick)
        bid_sz = 0 if inv >= self.max_inventory else self.size
        ask_sz = 0 if inv <= -self.max_inventory else self.size
        return bid, bid_sz, ask, ask_sz


class VolScaledMM(InventorySkewMM):
    """Rung 3: everything in rung 2, plus widen the spread when realized vol
    is high. High vol means each fill carries more mark to market risk."""

    name = "VolScaled"

    def __init__(self, base_half=0.02, vol_mult=1.0, size=4,
                 skew=0.02, max_inventory=12):
        super().__init__(base_half, size, skew, max_inventory)
        self.vol_mult = vol_mult

    def half(self, state):
        return self.half_spread + self.vol_mult * state["vol_est"]


class AvellanedaStoikovMM(Strategy):
    """Rung 4: Avellaneda-Stoikov closed form. With s = fair, q = inventory,
    tau = time remaining and sigma^2 the price variance over the horizon:

      reservation price  r = s - q * gamma * sigma^2 * tau
      optimal spread     gamma * sigma^2 * tau + (2 / gamma) * ln(1 + gamma / k)

    gamma is risk aversion, k is the flow decay constant (how fast fill
    probability drops as you quote further away). Inventory skew and spread
    both fall out of one utility argument instead of two hand tuned knobs.
    """

    name = "AvellStoikov"

    def __init__(self, gamma=0.3, k=10.0, horizon=30, size=4, max_inventory=12,
                 tau_floor=0.3):
        self.gamma = gamma
        self.k = k
        self.horizon = horizon   # steps used to scale per step vol to horizon variance
        self.size = size
        self.max_inventory = max_inventory
        # pure AS lets spread and skew collapse as tau goes to 0, which gets
        # the MM run over late in the session; floor tau to keep both alive
        self.tau_floor = tau_floor

    def reservation_and_half(self, state):
        s = state["fair_est"]
        q = state["inventory"]
        tau = max(state["time_left"], self.tau_floor)
        var = (state["vol_est"] ** 2) * self.horizon * tau
        r = s - q * self.gamma * var
        spread = self.gamma * var + (2.0 / self.gamma) * math.log(1.0 + self.gamma / self.k)
        return r, 0.5 * spread

    def quote(self, state):
        tick = state["tick"]
        inv = state["inventory"]
        r, half = self.reservation_and_half(state)
        bid = round_down(r - half, tick)
        ask = round_up(r + half, tick)
        bid_sz = 0 if inv >= self.max_inventory else self.size
        ask_sz = 0 if inv <= -self.max_inventory else self.size
        return bid, bid_sz, ask, ask_sz


class AdverseSelectionMM(AvellanedaStoikovMM):
    """Rung 5: rung 4 plus a toxicity guard. Track decayed fill pressure per
    side; when one side keeps getting hit (we are being picked off), widen
    that side and shrink its size until the pressure decays away.

    The guard only reacts to one sided pressure. Balanced two sided flow is
    healthy noise and should not widen anything.
    """

    name = "AdvSelGuard"

    def __init__(self, gamma=0.3, k=10.0, horizon=30, size=4, max_inventory=12,
                 tau_floor=0.3, tox_decay=0.85, tox_widen=0.03, tox_shrink=0.25):
        super().__init__(gamma, k, horizon, size, max_inventory, tau_floor)
        self.tox_decay = tox_decay     # per step decay of fill pressure
        self.tox_widen = tox_widen     # extra width per unit of net pressure
        self.tox_shrink = tox_shrink   # size shrink per unit of net pressure
        self.tox_bid = 0.0
        self.tox_ask = 0.0

    def on_fills(self, fills, state):
        for f in fills:
            if f.buy_agent == MM_AGENT:
                self.tox_bid += f.size    # our bid was hit
            elif f.sell_agent == MM_AGENT:
                self.tox_ask += f.size    # our ask was lifted

    def quote(self, state):
        self.tox_bid *= self.tox_decay
        self.tox_ask *= self.tox_decay
        tick = state["tick"]
        bid, bid_sz, ask, ask_sz = super().quote(state)
        net = self.tox_bid - self.tox_ask
        bid_pressure = max(0.0, net)
        ask_pressure = max(0.0, -net)
        bid = round_down(bid - self.tox_widen * bid_pressure, tick)
        ask = round_up(ask + self.tox_widen * ask_pressure, tick)
        if bid_sz > 0:
            bid_sz = max(1, round(bid_sz / (1.0 + self.tox_shrink * bid_pressure)))
        if ask_sz > 0:
            ask_sz = max(1, round(ask_sz / (1.0 + self.tox_shrink * ask_pressure)))
        return bid, bid_sz, ask, ask_sz


class QueueAwareMM(InventorySkewMM):
    """Queue tactics on top of rung 2's risk control. Joining a deep queue
    means waiting behind everyone at that price; improving by one tick buys
    front of queue priority at the cost of one tick of edge. So:

      queue ahead is deep     improve the best price by one tick
      queue ahead is shallow  join the level and take the better edge
      already at the front    hold the price to keep the spot

    Expect a higher fill rate at slightly worse prices than InvSkew: less
    spread captured per fill, more fills.
    """

    name = "QueueAware"

    def __init__(self, half_spread=0.05, size=4, skew=0.02, max_inventory=12,
                 deep_queue=5, min_edge=0.02, tactic_max_inv=6):
        super().__init__(half_spread, size, skew, max_inventory)
        self.deep_queue = deep_queue          # queue size worth paying a tick to jump
        self.min_edge = min_edge              # never quote closer to fair than this
        self.tactic_max_inv = tactic_max_inv  # no chasing fills that grow our position

    def _my_resting_price(self, book_side):
        for o in book_side:
            if o.agent == MM_AGENT:
                return o.price
        return None

    def quote(self, state):
        bid, bid_sz, ask, ask_sz = super().quote(state)
        inv = state["inventory"]
        if bid_sz > 0 and inv < self.tactic_max_inv:
            bid = self._tactical_bid(state, bid)
        if ask_sz > 0 and inv > -self.tactic_max_inv:
            ask = self._tactical_ask(state, ask)
        return bid, bid_sz, ask, ask_sz

    def _tactical_bid(self, state, target):
        book, tick, fair = state["book"], state["tick"], state["fair_est"]
        cap = round_down(fair - self.min_edge, tick)   # tightest bid allowed
        best = book.best_bid
        if best is None:
            return min(target, cap)
        mine = self._my_resting_price(book.bids)
        if (mine is not None and state["queue_ahead_bid"] == 0
                and target <= mine <= cap):
            return mine                        # front of the queue, hold the spot
        if target > best:
            return min(target, cap)            # our target already beats the queue
        others = book.depth_at("buy", best, exclude_agent=MM_AGENT)
        if others == 0:
            return min(target, cap)
        if others >= self.deep_queue:
            return min(best + tick, cap)       # deep queue: pay a tick, jump it
        return min(best, cap)                  # shallow queue: join it

    def _tactical_ask(self, state, target):
        book, tick, fair = state["book"], state["tick"], state["fair_est"]
        cap = round_up(fair + self.min_edge, tick)     # tightest ask allowed
        best = book.best_ask
        if best is None:
            return max(target, cap)
        mine = self._my_resting_price(book.asks)
        if (mine is not None and state["queue_ahead_ask"] == 0
                and cap <= mine <= target):
            return mine                        # front of the queue, hold the spot
        if target < best:
            return max(target, cap)            # our target already beats the queue
        others = book.depth_at("sell", best, exclude_agent=MM_AGENT)
        if others == 0:
            return max(target, cap)
        if others >= self.deep_queue:
            return max(best - tick, cap)       # deep queue: pay a tick, jump it
        return max(best, cap)                  # shallow queue: join it


# Instantiate fresh for every run, some strategies carry state between steps.
LADDER = [SymmetricMM, InventorySkewMM, VolScaledMM,
          AvellanedaStoikovMM, AdverseSelectionMM, QueueAwareMM]
