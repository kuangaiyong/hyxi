"""Tweakers.net 论坛采集器声明"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional
from urllib.parse import quote

from app.collectors.base import Collector
from app.config import settings

DEFAULT_BASE_URL = "https://gathering.tweakers.net"
# 原帖链接只接受 http(s) 的站点地址（见 post_url）。`javascript:` 拼出来是一段合法脚本，
# 会原样进 `<a :href>` —— 与 facebook_group.py 同一条实测结论
_WEB_BASE = re.compile(r"^https?://[^/\s]", re.IGNORECASE)


class TweakersCollector(Collector):
    id = "tweakers"
    display_name = "Tweakers.net 论坛"
    script = "tweakers.js"
    needs_credentials = False
    incremental_strategy = "page"
    # 一个来源 = 一个 thread = **一条主题 + 按时间平铺的回复**。不是 Facebook 那种
    # 「许多主贴各自带评论」—— 出口的措辞与增量时下发的主题指纹都看这个
    thread_kind = "thread"
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

    def post_url(self, source: Dict[str, Any], message_id: str) -> Optional[str]:
        """单条楼层的固定链接：`/forum/list_message/{id}#{id}`。

        这是站点自己在用的形态（真站实测：楼层头部的日期就是一个指向它的
        `<a class="oldage" href="…/forum/list_message/85322114#85322114">`）。

        改造前 Tweakers 一条链接都没有（基类默认返回 None），结果页上主题卡因此没有
        「🔗 原帖」。**回复不挂链接**：那是 `results.py::_post_url` 按 `reply_level == 0`
        统一定的口径（Facebook 的评论 id 拼不出正确链接），这里不另开一条路。

        `base_url` 取自数据源参数而不是写死常量（本地 fixture 验证时要指向测试站点），
        并照抄 Facebook 那三条实测结论：非 http(s) 不给链接（`javascript:` 是一段合法脚本）、
        非字符串不给（手工构造的 API 请求能把它存成数字，抛异常就是整页 500）、
        路径段一律编码。
        """
        params = source.get("params") or {}
        base = params.get("base_url") or source.get("base_url") or DEFAULT_BASE_URL
        if not message_id or not isinstance(base, str) or not _WEB_BASE.match(base):
            return None
        mid = quote(str(message_id), safe="")
        return f"{base.rstrip('/')}/forum/list_message/{mid}#{mid}"

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
            # 主题（发起帖）的指纹，由 CollectorRunner 从库里查出来下发。增量跑从
            # max_page_number + 1 起抓、看不到第 1 页，脚本自己认不出主题；拿不到时脚本
            # 不猜（存量库的 140 条并列主贴就是这种状态），见 markTopicAndReplies()
            "topic_fingerprint": source.get("topic_fingerprint") or None,
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
