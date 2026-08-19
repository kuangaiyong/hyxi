"""包根路径解析 —— 唯一的一份。

单独成模块是因为它有两个使用者，而且**先后顺序被锁死**：`run_server.py` 必须先算出包根、
把 `.env` 生成出来，才能 import `app.config`（`settings` 是 import 期实例化并当场读 `.env`
的，反过来就读不到刚生成的密钥）。import 一次 `app.config` 就已经晚了，所以这两个函数
不能住在那里；各写一份又必然会漂移，而这条路径错了会连锁带偏数据、密钥、采集脚本
和 playwright 四个位置。
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是不是打包成可执行文件在跑。

    Nuitka 编译后会注入 `__compiled__`，PyInstaller 设 `sys.frozen` —— 两个都认，
    将来 Nuitka 构建不通、换回 PyInstaller 时这里不用再改一次。
    """
    return "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


def project_root() -> str:
    """源码态是仓库根，打包态是便携包根目录（`app\\hyxi.exe` 的上一级）。

    冻结态要过一遍 `realpath`：`sys.executable` 可能是 8.3 短路径，实测双击启动器
    拿到的是 `C:\\HYXI-B~1\\HYXI-1~1.1-W`。它指向的目录是对的，但会原样显示在
    「程序目录」那行上，而用户正要照着它去资源管理器里找 data 文件夹 —— 短路径
    既看不懂也不好粘。
    """
    if is_frozen():
        return os.path.realpath(str(Path(sys.executable).parent.parent))
    return str(Path(__file__).parent.parent.parent)


def data_dir() -> str:
    """便携包里没有 `backend/` 这一层，数据就放在包根下 —— 整个目录拷走就是全部家当。"""
    root = project_root()
    return os.path.join(root, "data") if is_frozen() else os.path.join(root, "backend", "data")
