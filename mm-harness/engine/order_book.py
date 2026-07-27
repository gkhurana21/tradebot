"""Limit order book with price-time (FIFO) priority.

Order and Fill are part of the interface seam. Strategies and diagnostics
depend only on these shapes, never on the book internals, so the sim can
later be swapped for a real exchange adapter that produces the same shapes.
"""

from dataclasses import dataclass


@dataclass
class Order:
    order_id: int
    agent: str
    side: str        # "buy" or "sell"
    price: float
    size: int
    timestamp: int   # event sequence number, older = higher priority at a price


@dataclass
class Fill:
    price: float
    size: int
    buy_agent: str
    sell_agent: str
    aggressor: str   # side of the incoming order that caused the fill
    timestamp: int


class OrderBook:
    """Book holds resting limit orders. Incoming orders match best price first,
    then oldest order at that price (FIFO). Fills happen at the resting price.
    """

    def __init__(self):
        self.bids = []   # sorted best first: highest price, then oldest
        self.asks = []   # sorted best first: lowest price, then oldest
        self._next_id = 0
        self._clock = 0

    # ---- quotes -----------------------------------------------------------

    @property
    def best_bid(self):
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self):
        return self.asks[0].price if self.asks else None

    @property
    def mid(self):
        if self.bids and self.asks:
            return 0.5 * (self.bids[0].price + self.asks[0].price)
        return None

    @property
    def spread(self):
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return None

    # ---- order entry ------------------------------------------------------

    def add_limit(self, agent, side, price, size):
        """Add a limit order. Matches against the opposite side while it
        crosses, then rests any remainder. Returns the list of Fills.
        """
        self._clock += 1
        fills, remaining = self._match(agent, side, size, limit_price=price)
        if remaining > 0:
            order = Order(self._next_id, agent, side, price, remaining, self._clock)
            self._next_id += 1
            self._insert(order)
        return fills

    def add_market(self, agent, side, size):
        """Add a market order. Matches as far as the book allows and any
        unfilled remainder is discarded. Returns the list of Fills.
        """
        self._clock += 1
        fills, _ = self._match(agent, side, size, limit_price=None)
        return fills

    def cancel_agent(self, agent):
        """Remove every resting order belonging to agent."""
        self.bids = [o for o in self.bids if o.agent != agent]
        self.asks = [o for o in self.asks if o.agent != agent]

    def replace(self, agent, side, price, size):
        """Re-quote one side with real exchange queue semantics:

          same price as our resting order  -> keep the queue position
          different price                  -> cancel, join the back of the
                                              new level (the old spot is lost
                                              even if we come back later)

        Sizing down trims from the back of our own orders, keeping the front
        one's priority. Sizing up leaves the resting order alone and adds the
        extra size as a new order at the back of the level, exactly like
        sending an additional order on a real exchange. Returns any Fills in
        case the new price crosses the opposite side.
        """
        book_side = self.bids if side == "buy" else self.asks
        mine = [o for o in book_side if o.agent == agent]
        if size <= 0:
            for o in mine:
                book_side.remove(o)
            return []
        if mine and abs(mine[0].price - price) < 1e-9:
            total = sum(o.size for o in mine)
            if size <= total:
                need_cut = total - size
                for o in reversed(mine):   # trim newest first, keep the front spot
                    cut = min(need_cut, o.size)
                    o.size -= cut
                    need_cut -= cut
                    if o.size == 0:
                        book_side.remove(o)
                    if need_cut == 0:
                        break
                return []
            return self.add_limit(agent, side, price, size - total)
        for o in mine:
            book_side.remove(o)
        return self.add_limit(agent, side, price, size)

    # ---- queue inspection --------------------------------------------------

    def queue_ahead(self, agent, side):
        """Total resting size that must trade before agent's first order on
        this side fills: better priced orders plus earlier orders at the same
        price. Returns None if the agent has no resting order on this side."""
        book_side = self.bids if side == "buy" else self.asks
        ahead = 0
        for o in book_side:   # already in strict priority order
            if o.agent == agent:
                return ahead
            ahead += o.size
        return None

    def depth_at(self, side, price, exclude_agent=None):
        """Total resting size at one price level, optionally excluding one
        agent's own orders."""
        book_side = self.bids if side == "buy" else self.asks
        return sum(o.size for o in book_side
                   if abs(o.price - price) < 1e-9 and o.agent != exclude_agent)

    # ---- internals --------------------------------------------------------

    def _match(self, agent, side, size, limit_price):
        book_side = self.asks if side == "buy" else self.bids
        fills = []
        while size > 0 and book_side:
            resting = book_side[0]
            if limit_price is not None:
                if side == "buy" and resting.price > limit_price:
                    break
                if side == "sell" and resting.price < limit_price:
                    break
            traded = min(size, resting.size)
            buy_agent = agent if side == "buy" else resting.agent
            sell_agent = resting.agent if side == "buy" else agent
            fills.append(Fill(resting.price, traded, buy_agent, sell_agent,
                              aggressor=side, timestamp=self._clock))
            size -= traded
            resting.size -= traded
            if resting.size == 0:
                book_side.pop(0)
        return fills, size

    def _insert(self, order):
        if order.side == "buy":
            book_side = self.bids
            better = lambda new, old: new.price > old.price
        else:
            book_side = self.asks
            better = lambda new, old: new.price < old.price
        # linear scan is fine, the book only ever holds a handful of orders
        idx = len(book_side)
        for i, resting in enumerate(book_side):
            if better(order, resting):
                idx = i
                break
        book_side.insert(idx, order)
