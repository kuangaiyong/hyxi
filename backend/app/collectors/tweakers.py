"""Tweakers.net 论坛采集器声明"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

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
        {
            # 由用户在数据源页填写，不是 LLM 能碰的东西 —— collect 步骤只把 source_id
            # 交给编排层，模型拿不到任何平台参数。留空即官方站点
            "name": "base_url",
            "label": "站点地址（可选）",
            "type": "text",
            "required": False,
            "placeholder": f"留空即 {DEFAULT_BASE_URL}；自建镜像或本地验证时才填",
        },
    ]

    def legacy_output_path(self, source: Dict[str, Any]) -> Optional[str]:
        thread_id = (source.get("params") or {}).get("thread_id")
        if not thread_id:
            return None
        return os.path.join(settings.project_root, f"tweakers_thread_{thread_id}.json")

    def build_job(self, source: Dict[str, Any], output_path: str) -> Dict[str, Any]:
        params = source.get("params") or {}
        # 续抓点由 Python 从 posts 表算出来。脚本不再读旧落盘文件 ——
        # 那份文件已经不存在了
        start_page = params.get("start_page", 1)
        if params.get("incremental", True):
            resume = source.get("max_page_number") or 0
            if resume >= start_page:
                start_page = resume + 1
        return {
            "source_id": source.get("id") or self.id,
            "collector_id": self.id,
            "mode": source.get("mode", "collect"),
            "params": {
                "thread_id": params.get("thread_id"),
                "start_page": start_page,
                "headless": params.get("headless", True),
            },
            "incremental": params.get("incremental", True),
            "known_fingerprints": source.get("known_fingerprints") or [],
            "output_path": output_path,
            "state_file": source.get("state_file")
            or os.path.join(settings.project_root, ".scraper_state.json"),
            # base_url 来自用户在界面上录入的 source，pacing 完全不可配 ——
            # 请求节奏是反爬纪律，谁都不能改
            "base_url": (source.get("base_url") or params.get("base_url")
                         or DEFAULT_BASE_URL).rstrip("/"),
            # 正文图落盘根目录。脚本在下面按 source_id 分子目录，images 字段存
            # 相对这里的路径，供 /api/v1/media/{path} 回读。
            # 允许 source 覆盖（同 state_file），否则测试会往真实 data 目录里写图
            "media_dir": source.get("media_dir") or os.path.join(settings.data_dir, "media"),
            "pacing": source.get("pacing") or {"delay_min": 4000, "delay_max": 11000},
        }
