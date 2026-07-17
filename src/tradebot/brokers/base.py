"""Broker adapter interface.

Every execution venue — the paper engine, MetaTrader 5, a crypto exchange
REST API — implements this same interface. The rest of the system never
knows which one is behind it, so we build/backtest on paper and flip to
live by swapping the adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.types import Account, Fill, Order


class BrokerAdapter(ABC):
    @abstractmethod
    def account(self) -> Account:
        """Current cash, equity, and open positions."""

    @abstractmethod
    def price(self, symbol: str) -> float:
        """Latest mark price for a symbol."""

    @abstractmethod
    def submit(self, order: Order) -> Fill:
        """Execute an order, return the resulting fill."""

    @abstractmethod
    def close(self, symbol: str) -> Fill | None:
        """Flatten a symbol's position. None if already flat."""

    def name(self) -> str:
        return self.__class__.__name__
