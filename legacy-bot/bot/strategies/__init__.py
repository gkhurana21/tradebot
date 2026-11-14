from bot.strategies.base import Strategy
from bot.strategies.meanrev import MeanRev
from bot.strategies.null_random import NullRandom
from bot.strategies.tsmom import TSMom
from bot.strategies.xsmom import XSMom

REGISTRY: dict[str, type[Strategy]] = {
    TSMom.name: TSMom,
    XSMom.name: XSMom,
    MeanRev.name: MeanRev,
    NullRandom.name: NullRandom,
}


def build_strategy(name: str, cfg: dict, universe: str) -> Strategy:
    params = cfg["strategies"][name]
    universe_cfg = cfg["universes"][universe]
    return REGISTRY[name](params, universe, universe_cfg)


def enabled_strategies(cfg: dict) -> list[str]:
    return [n for n, p in cfg["strategies"].items() if p.get("enabled")]
