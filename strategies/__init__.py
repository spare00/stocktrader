from strategies.base import Strategy
from strategies.gap_and_go import GapAndGoStrategy
from strategies.opening_impulse import OpeningImpulseStrategy
from strategies.spike import SpikeStrategy


STRATEGY_REGISTRY = {
    "gap_and_go": GapAndGoStrategy,
    "spike": SpikeStrategy,
    "opening_impulse": OpeningImpulseStrategy,
}


def build_strategies(settings):
    strategies = []
    for name in settings.strategy_names:
        try:
            strategy_cls = STRATEGY_REGISTRY[name]
        except KeyError as exc:
            raise ValueError(f"Unknown strategy: {name}") from exc
        strategies.append(strategy_cls(settings))
    return strategies
