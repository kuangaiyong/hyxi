"""Facebook 公开小组采集器声明。

⚠️ Facebook 服务条款禁止自动化登录与抓取，账号存在被封风险。请用专用小号，
   不要复用任何有价值的账号。撞上两步验证或安全检查时脚本以退出码 3 交回给人，
   不做任何绕过尝试 —— 与项目既有的「不破验证码」姿态一致。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from app.collectors.base import Collector
from app.config import settings

DEFAULT_BASE_URL = "https://www.facebook.com"


class FacebookGroupCollector(Collector):
    id = "facebook_group"
    display_name = "Facebook 公开小组"
    script = "facebook_group.js"
    needs_credentials = True
    incremental_strategy = "watermark"
    param_fields = [
        {
            "name": "group_id",
            "label": "小组 ID",
            "type": "text",
            "required": True,
            "placeholder": "例如 2407063016436085",
        },
        {
            "name": "max_batches",
            "label": "每轮最多滚动批次",
            "type": "number",
            "required": False,
            "placeholder": "默认 10。信息流没有总页数，只能给个上限",
        },
        {
            "name": "base_url",
            "label": "站点地址（可选）",
            "type": "text",
            "required": False,
            "placeholder": f"留空即 {DEFAULT_BASE_URL}；本地验证时才填",
        },
    ]

    def legacy_output_path(self, source: Dict[str, Any]) -> Optional[str]:
        group_id = (source.get("params") or {}).get("group_id")
        if not group_id:
            return None
        return os.path.join(settings.project_root, f"facebook_group_{group_id}.json")

    def session_path(self, source: Dict[str, Any]) -> str:
        """会话按 source 隔离而不是按 collector —— 同一个采集器可能挂两个账号"""
        return os.path.join(
            settings.data_dir, "sessions", f"{source.get('id', self.id)}.json"
        )

    def build_job(self, source: Dict[str, Any], output_path: str) -> Dict[str, Any]:
        params = source.get("params") or {}
        return {
            "source_id": source.get("id") or self.id,
            "collector_id": self.id,
            "mode": source.get("mode", "collect"),
            "params": {
                "group_id": params.get("group_id"),
                "max_batches": int(params.get("max_batches") or 10),
                "start_page": params.get("start_page", 1),
                "headless": params.get("headless", True),
                "manual_login_timeout_ms": source.get("manual_login_timeout_ms"),
            },
            "incremental": params.get("incremental", True),
            # 增量去重的锚点由 Python 下发。脚本不再读旧落盘文件 —— 那份文件已经不存在了
            "known_fingerprints": source.get("known_fingerprints") or [],
            "output_path": output_path,
            "state_file": source.get("state_file") or self.session_path(source),
            # 正文图落盘根目录。脚本在下面按 source_id 分子目录，images 字段存
            # 相对这里的路径，供 /api/v1/media/{path} 回读。
            # 允许 source 覆盖（同 state_file），否则测试会往真实 data 目录里写图
            "media_dir": source.get("media_dir") or os.path.join(settings.data_dir, "media"),
            # 凭据不在 job 里 —— 只走子进程环境变量（见 CollectorRunner._child_env）
            "base_url": (params.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
            "pacing": source.get("pacing") or {"delay_min": 4000, "delay_max": 11000},
        }
