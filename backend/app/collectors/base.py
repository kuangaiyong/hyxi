"""采集器声明基类。

每个来源在这里只做纯声明（脚本名、表单字段、增量策略、输出位置），站点知识全部留在
对应的 Node 脚本里。Python 侧不含任何站点分支 —— 这是「新增一个来源 = 一个 Node 脚本
加十几行声明」的前提。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from app.config import settings


class Collector:
    id: str = ""
    display_name: str = ""
    script: str = ""                    # 相对 collectors/ 的脚本文件名
    needs_credentials: bool = False
    param_fields: List[Dict[str, Any]] = []
    incremental_strategy: str = "page"  # "page" | "watermark"
    # 不进「新增数据源」的采集器下拉框。留给没有真实站点、只服务于本地 fixture 的采集器；
    # get_collector() 照样能解析出来，已注册的数据源和回归测试不受影响
    internal: bool = False

    def script_path(self) -> str:
        return os.path.join(settings.project_root, "collectors", self.script)

    def output_path(self, source: Dict[str, Any]) -> str:
        """采集结果的落盘位置。全项目唯一的文件名来源，调用方不再自己拼。"""
        raise NotImplementedError

    def build_job(self, source: Dict[str, Any], output_path: str) -> Dict[str, Any]:
        """构造传给 Node 脚本的 job —— 唯一允许每个来源各不相同的地方，纯数据无逻辑。"""
        raise NotImplementedError

    def normalize(self, raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把脚本输出拍平成统一的 post 数组。"""
        return raw.get("posts", [])
