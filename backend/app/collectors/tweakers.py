"""Tweakers.net 论坛采集器声明"""

from __future__ import annotations

import os
from typing import Any, Dict

from app.collectors.base import Collector
from app.config import settings

DEFAULT_BASE_URL = "https://gathering.tweakers.net"


class TweakersCollector(Collector):
    id = "tweakers"
    display_name = "Tweakers.net 论坛"
    script = "tweakers.js"
    needs_credentials = False
    incremental_strategy = "page"
    param_fields = [
        {
            "name": "thread_id",
            "label": "帖子 ID",
            "type": "text",
            "required": True,
            "placeholder": "例如 2336074",
        },
    ]

    def output_path(self, source: Dict[str, Any]) -> str:
        thread_id = (source.get("params") or {}).get("thread_id")
        if not thread_id:
            raise ValueError("缺少 thread_id 参数")
        return os.path.join(settings.project_root, f"tweakers_thread_{thread_id}.json")

    def build_job(self, source: Dict[str, Any], output_path: str) -> Dict[str, Any]:
        params = source.get("params") or {}
        return {
            "source_id": source.get("id") or self.id,
            "collector_id": self.id,
            "mode": source.get("mode", "collect"),
            "params": {
                "thread_id": params.get("thread_id"),
                "start_page": params.get("start_page", 1),
                "headless": params.get("headless", True),
            },
            "incremental": params.get("incremental", True),
            "output_path": output_path,
            "state_file": source.get("state_file")
            or os.path.join(settings.project_root, ".scraper_state.json"),
            # base_url / pacing 只从 source 取，不从 params 取：params 里混着 LLM 给的值，
            # 让模型能改抓取目标和请求节奏等于把反爬纪律交给模型
            "base_url": source.get("base_url") or DEFAULT_BASE_URL,
            "pacing": source.get("pacing") or {"delay_min": 4000, "delay_max": 11000},
        }
