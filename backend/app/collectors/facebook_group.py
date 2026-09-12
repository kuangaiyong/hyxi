"""Facebook 公开小组采集器声明。

⚠️ Facebook 服务条款禁止自动化登录与抓取，账号存在被封风险。请用专用小号，
   不要复用任何有价值的账号。撞上两步验证或安全检查时脚本以退出码 3 交回给人，
   不做任何绕过尝试 —— 与项目既有的「不破验证码」姿态一致。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional
from urllib.parse import quote

from app.collectors.base import Collector
from app.config import settings

DEFAULT_BASE_URL = "https://www.facebook.com"
# 原帖链接只接受 http(s) 的站点地址（见 post_url）
_WEB_BASE = re.compile(r"^https?://[^/\s]", re.IGNORECASE)


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

    def post_url(self, source: Dict[str, Any], message_id: str) -> Optional[str]:
        """小组帖子的固定链接：`/groups/{gid}/permalink/{mid}/`。

        这是 Facebook 自己的规范形态 —— 用户手里那条
        `.../groups/2407063016436085/permalink/2494381381037581/` 就是它，
        末尾那串数字正是 posts 表里的 message_id。

        **不需要也不该做自动登录**：链接在用户自己的浏览器里打开，用的就是他本人的
        登录态；没登录时 Facebook 会自己带着这个目标走一遍登录再跳回原贴。把采集
        小号的会话递进用户浏览器是安全倒退（凭据只进不出），为省一次点击不值得。

        `base_url` 取自数据源参数而不是写死常量：本地 fixture 验证时它指向测试站点，
        写死会让链接指到真站上去。

        **这串会原样进 `<a :href>`，而 base_url / group_id 都是用户在数据源页填的**：
          · base_url 不是 http(s) 就不给链接 —— 「javascript:alert(1)//x」拼出来是一段
            合法脚本（后面全是注释），每条主贴的「🔗 原帖」都会变成它
          · 非字符串（手工构造的 API 请求能存进来）同样不给，不能在这里抛 —— 链接是
            逐条现算的，一条抛异常整页帖子列表就 500
          · group_id / message_id 按单个路径段编码，带 / ? # 的值冲不出自己那一段
        （以上三条都是发版前评审实测出来的）
        """
        params = source.get("params") or {}
        group_id = params.get("group_id")
        base = params.get("base_url") or DEFAULT_BASE_URL
        if not group_id or not message_id or not isinstance(base, str) \
                or not _WEB_BASE.match(base):
            return None
        gid, mid = quote(str(group_id), safe=""), quote(str(message_id), safe="")
        return f"{base.rstrip('/')}/groups/{gid}/permalink/{mid}/"

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
