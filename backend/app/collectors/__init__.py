"""采集器注册表"""

from typing import Dict, List

from app.collectors.base import Collector
from app.collectors.facebook_group import FacebookGroupCollector
from app.collectors.group_feed import GroupFeedCollector
from app.collectors.tweakers import TweakersCollector

_REGISTRY: Dict[str, Collector] = {
    c.id: c for c in (TweakersCollector(), GroupFeedCollector(), FacebookGroupCollector())
}


def get_collector(collector_id: str) -> Collector:
    collector = _REGISTRY.get(collector_id)
    if collector is None:
        raise ValueError(f"未知采集器: {collector_id}")
    return collector


def all_collectors() -> List[Collector]:
    return list(_REGISTRY.values())


__all__ = ["Collector", "get_collector", "all_collectors"]
