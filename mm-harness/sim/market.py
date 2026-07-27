"""Simulated market: a latent fair value plus counterparty order flow.

The fair value is a random walk with optional mean reversion. Counterparty
flow arrives as marketable limit orders (an order crosses if the MM quote is
within the trader's price tolerance, so wider quotes really do get fewer
fills). Flow splits into:

  noise traders     random side, no information
  informed traders  trade in the direction fair value is about to move,
                    which is exactly adverse selection for the MM

There is also background liquidity: competing passive quoters that rest
size around fair every step. They are what makes queue position real. Each
one cancels and re-adds every step, so they always join the back of a
level, while an MM that holds its price keeps its spot in front. The
competitive landscape is a regime, not a constant: quoter count, presence
probability, size range and the half spread range are all tunable, and
each quoter draws a fresh half spread from the range every step.

Flow and background generation never look at the order book, so two
backtests with the same seed see the identical market regardless of
strategy behavior.
"""

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class FlowOrder:
    side: str          # "buy" or "sell"
    size: int
    kind: str          # "noise" or "informed"
    limit_price: float  # worst price the trader will accept


class Market:
    def __init__(self, seed=0, fair=100.0, vol=0.04, mean_reversion=0.01,
                 flow_rate=6.0, informed_frac=0.2, tick=0.01,
                 patience=0.08, min_size=1, max_size=3,
                 bg_quoters=2, bg_prob=0.7, bg_half_range=(0.05, 0.10),
                 bg_min=2, bg_max=8):
        self.rng = np.random.default_rng(seed)
        self.fair = fair
        self.anchor = fair
        self.vol = vol
        self.mean_reversion = mean_reversion
        self.flow_rate = flow_rate
        self.informed_frac = informed_frac
        self.tick = tick
        self.patience = patience     # mean price tolerance beyond fair
        self.min_size = min_size
        self.max_size = max_size
        self.bg_quoters = bg_quoters       # number of background quoters
        self.bg_prob = bg_prob             # chance per quoter per side per step
        self.bg_half_range = bg_half_range  # each quoter draws its half spread here
        self.bg_min = bg_min
        self.bg_max = bg_max
        # the next fair value move is drawn one step ahead so that informed
        # traders can trade on it before it happens
        self._pending_move = self._draw_move()

    def _draw_move(self):
        pull = self.mean_reversion * (self.anchor - self.fair)
        return pull + self.vol * float(self.rng.standard_normal())

    def step(self):
        """Advance fair value by the move informed traders already acted on,
        then draw the next pending move. Returns the new fair value."""
        self.fair += self._pending_move
        self._pending_move = self._draw_move()
        return self.fair

    def generate_flow(self):
        """Draw this step's counterparty orders. Informed traders pick the
        side of the pending (not yet realized) fair value move."""
        orders = []
        n = int(self.rng.poisson(self.flow_rate))
        for _ in range(n):
            size = int(self.rng.integers(self.min_size, self.max_size + 1))
            tolerance = float(self.rng.exponential(self.patience))
            if self.rng.random() < self.informed_frac:
                kind = "informed"
                side = "buy" if self._pending_move > 0 else "sell"
            else:
                kind = "noise"
                side = "buy" if self.rng.random() < 0.5 else "sell"
            if side == "buy":
                limit = self.fair + tolerance
            else:
                limit = self.fair - tolerance
            orders.append(FlowOrder(side, size, kind, limit))
        return orders

    def generate_background(self):
        """Draw this step's background liquidity as (side, price, size)
        tuples. Each quoter independently draws presence, a half spread from
        bg_half_range and a size per side; prices round away from fair to
        the tick grid."""
        quotes = []
        lo, hi = self.bg_half_range
        for _ in range(self.bg_quoters):
            for side in ("buy", "sell"):
                if self.rng.random() < self.bg_prob:
                    half = float(self.rng.uniform(lo, hi))
                    size = int(self.rng.integers(self.bg_min, self.bg_max + 1))
                    if side == "buy":
                        px = math.floor((self.fair - half) / self.tick + 1e-9) * self.tick
                    else:
                        px = math.ceil((self.fair + half) / self.tick - 1e-9) * self.tick
                    quotes.append((side, px, size))
        return quotes
