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


# 便携包的数据目录名。挂在**包的同级**而不是包内 —— 包目录带版本号，装在里面的话
# 每升一次级就是一个全新的空目录，LLM 配置、数据源、任务、舆情结论全部要重来一遍。
EXTERNAL_DATA_DIRNAME = "HYXi-数据"


def data_dir() -> str:
    """数据目录。源码态在 `backend/data`，便携包在**包的同级目录**。

        C:\\HYXi\\
        ├── HYXi-1.8.1-win64\\     ← 旧版本，升级后可直接删
        ├── HYXi-1.8.2-win64\\     ← 新版本，启动即接上数据
        └── HYXi-数据\\             ← 与版本无关

    放同级而不是用户目录，是为了保住「免安装、整个目录拷走就能换机器、删目录即卸载」——
    数据一旦躺进 `%LOCALAPPDATA%`，这三条就同时不成立了。

    **包内已有 `data/` 时一律沿用它**：老版本的数据就在那儿，升上来的用户重启一次
    不能凭空变成空库。新解压的包里没有这个目录（ZIP 不含 data/，已核实），所以这条
    只对既有安装生效。
    """
    root = project_root()
    if not is_frozen():
        return os.path.join(root, "backend", "data")
    legacy = os.path.join(root, "data")
    if os.path.isdir(legacy):
        return legacy
    return os.path.join(os.path.dirname(root), EXTERNAL_DATA_DIRNAME)


def env_file() -> str:
    """`.env` 的位置 —— **必须跟着数据走**。

    里面的 `TWEAKERS_SECRET_KEY` 是数据源密码的加密密钥：它和 `hyxi.db` 分开了，
    库里的密文就再也解不开，界面只会说「与保存时的密钥不一致，请重新录入凭据」。

    **判据必须和 `data_dir()` 咬死**：只有数据也还留在包里（legacy 布局）时才认包根那份。
    改成「包根有 .env 就用它」会开一个静默毁数据的口子 —— 用户照《使用说明》在包根手写
    一个 `.env` 开局域网访问，它就会遮住数据目录里那份，而 `ensure_env_file()` 见它没有
    `TWEAKERS_SECRET_KEY=` 便补上一把**新**密钥；此后 `settings.secret_key` 是新的，
    而库里的凭据是用旧密钥加密的，界面只会说「与保存时的密钥不一致，请重新录入凭据」。
    """
    root = project_root()
    if not is_frozen():
        return os.path.join(root, ".env")
    data = data_dir()
    if os.path.normcase(data) == os.path.normcase(os.path.join(root, "data")):
        return os.path.join(root, ".env")     # legacy 布局：数据和密钥都还在包里
    return os.path.join(data, ".env")
