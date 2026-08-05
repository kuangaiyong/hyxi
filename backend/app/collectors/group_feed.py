"""「主贴 + 嵌套评论」型信息流采集器声明。

站点自身没有页码（无限滚动 / 批次加载），增量只能靠时间水位线，所以
incremental_strategy 是 watermark 而不是 page。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from app.collectors.base import Collector
from app.config import settings


class GroupFeedCollector(Collector):
    id = "group_feed"
    display_name = "公开小组信息流"
    script = "group_feed.js"
    needs_credentials = False
    incremental_strategy = "watermark"
    # 没有默认站点（base_url 必填），真实场景下用户填不出可用地址 —— 真实版本是
    # facebook_group，这个只服务于 fixture 站点，不进界面
    internal = True
    param_fields = [
        {
            "name": "group_id",
            "label": "小组 ID",
            "type": "text",
            "required": True,
            "placeholder": "例如 2407063016436085",
        },
        {
            "name": "base_url",
            "label": "站点地址",
            "type": "text",
            "required": True,
            "placeholder": "例如 https://www.example.com",
        },
    ]

    def legacy_output_path(self, source: Dict[str, Any]) -> Optional[str]:
        group_id = (source.get("params") or {}).get("group_id")
        if not group_id:
            return None
        return os.path.join(settings.project_root, f"group_feed_{group_id}.json")

    def build_job(self, source: Dict[str, Any], output_path: str) -> Dict[str, Any]:
        params = source.get("params") or {}
        return {
            "source_id": source.get("id") or self.id,
            "collector_id": self.id,
            "mode": source.get("mode", "collect"),
            "params": {
                "group_id": params.get("group_id"),
                "start_page": params.get("start_page", 1),
                "headless": params.get("headless", True),
            },
            "incremental": params.get("incremental", True),
            # 增量去重的锚点由 Python 下发。脚本不再读旧落盘文件
            "known_fingerprints": source.get("known_fingerprints") or [],
            "output_path": output_path,
            "state_file": source.get("state_file")
            or os.path.join(settings.data_dir, "sessions", f"{source.get('id', self.id)}.json"),
            # 不像 Tweakers 有固定站点，这里的 base_url 必须由用户在数据源页填。
            # 它取自 source.params（界面录入并经 _validate_params 过滤），不是 LLM 给的 ——
            # collect 步骤只把 source_id 交给编排层，模型碰不到任何平台参数
            "base_url": (params.get("base_url") or "").rstrip("/"),
            "pacing": source.get("pacing") or {"delay_min": 4000, "delay_max": 11000},
        }
