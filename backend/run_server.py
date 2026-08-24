"""便携包的启动入口 —— Nuitka 编译的就是这个文件。

源码开发态**不走这里**（那条路是 `start.ps1` 起 uvicorn + Vite 两个进程），所以这里
只管打包态需要、而开发态天然成立的那些事：密钥自动生成、依赖自检、单进程起服务。

五件事的顺序是锁死的，别调换：
  1. 先算包根
  2. 接管旧版本的数据（必须在生成 .env 之前 —— 否则先生成一把**新**密钥，
     旧 .env 因为「已存在」不再被复制，数据搬过来了密码却全部解不开）
  3. 生成 .env
  4. 再 import app.config（`settings` 是 import 期实例化并当场读 .env 的）
  5. 起飞前自检，然后 uvicorn.run + 开浏览器
"""

import os
import shutil
import socket
import sqlite3
import sys
import threading
import warnings
import webbrowser
from pathlib import Path

# pydantic 会为 model_name 这个字段名警告两次「conflict with protected namespace」。
# 对开发者是噪音，对使用者是吓人的东西 —— 一个双击启动的人看到满屏 UserWarning
# 会以为程序坏了。这只在打包态的启动路径上屏蔽，源码态照常暴露出来
warnings.filterwarnings("ignore", message=".*protected namespace.*")

from app.paths import data_dir, env_file, is_frozen, project_root  # noqa: E402


def _line(text: str = "") -> None:
    print(text, flush=True)


# ===== 0. 升级时把旧版本的数据接过来 =====

# 「用户在这个数据目录里做过事没有」看这几张表：表名 → 超过多少行才算数。
# **sources 的门槛是 1 而不是 0**：首启时 seed_default_sources() 会自动补一条
# Tweakers 源，门槛给 0 的话每个刚建出来的空目录看起来都是有数据的，而那正是
# 要识别出来的那一种。给 1 则「用户自己加过源」仍然拦得住 —— 只配了数据源、
# 还没来得及配 LLM 的目录不该被旧包顶掉。
_USER_DATA_TABLES = {
    "app_config": 0, "credentials": 0, "schedules": 0, "tasks": 0, "posts": 0,
    "sources": 1,
}


def _has_user_data(data_path: str) -> bool:
    """这个数据目录里有没有用户自己配过、跑过的东西。

    判据不能是「目录在不在」：用户下载新版本后往往先双击试一下，那一下就把外部
    数据目录连同空库一起建出来了；他发现要重配、回去接着用旧版本，等再来试新版本时
    「目录已存在」就把接管永久短路掉了 —— 配置一条都带不过来，还看不到任何原因
    （用户实测报过）。「先试试新的、发现不行退回旧的」正是最自然的升级姿势。

    **读不动一律当作有数据**：宁可不接管（用户还能自己复制），也不能拿旧数据去覆盖
    一个可能装着东西的目录。
    """
    db = os.path.join(data_path, "hyxi.db")
    if not os.path.exists(db):
        return False
    try:
        # as_uri() 而不是 f"file:{db}"：路径里带个 # 就会被当成 URI 的 fragment
        # 而把后半截截掉，连不上就落进下面那个 except，表现成「怎么都接不上」
        uri = Path(db).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            have = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table, floor in _USER_DATA_TABLES.items():
                if table not in have:
                    continue
                # 只数到门槛+1 就够，posts 上千行时不必全表扫
                n = conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} LIMIT {floor + 1})"
                ).fetchone()[0]
                if n > floor:
                    return True
        finally:
            conn.close()
    except Exception:
        return True
    return False


def _previous_install(root: str):
    """旁边那个装着数据的旧版本包。没有就返回 None。

    包目录带版本号，升级就是解压出一个全新的空目录 —— 用户过去每升一次级，LLM 配置、
    数据源、跑过的任务和舆情结论就全部要重来一遍。数据现在挂在包的同级目录，但**第一次**
    升上来的时候它还在旧包肚子里，得去把它接出来。

    判据是「同级目录里有装着用户数据的 data/」，多个就取 hyxi.db 最近改过的那个 ——
    那才是用户真正在用的那份，按目录名比版本号会在改过名字的目录上判错。

    **光有个空库不算数**：旁边摆着一个从没用过的旧包时，接管会把空库复制过来，
    而复制完 target 仍然是空的 —— 下次启动照做一遍，每次还打一条「已接管它的
    配置与数据」的假日志。
    """
    parent = os.path.dirname(root)
    found = []
    try:
        entries = os.listdir(parent)
    except OSError:
        return None
    for name in entries:
        candidate = os.path.join(parent, name)
        if os.path.normcase(candidate) == os.path.normcase(root) or not os.path.isdir(candidate):
            continue
        data = os.path.join(candidate, "data")
        if _has_user_data(data):
            found.append((os.path.getmtime(os.path.join(data, "hyxi.db")), candidate))
    return max(found)[1] if found else None


def adopt_previous_install(root: str) -> None:
    """外部数据目录里还没有用户的东西时，把旧版本包里的 data 和 .env 复制过来。

    判据是 `_has_user_data()` 而不是「目录在不在」—— 理由见那个函数。目录已经建出来
    但还是个空壳（用户先双击试了一下）时照样接管，把空壳改名让路即可。

    **复制而不是移动**：旧包留在原地照样能跑，用户想回退就回退，确认没问题再自己删。
    数据量在几 MB 到几十 MB 这个量级（库 + 配图），一次性的开销可以接受，而且搬完之后
    数据就在包外了，以后每次升级都不用再复制。

    **先搬进暂存目录、齐了再整体改名**，不直接往 target 里写。`shutil.copytree` 是攒到
    最后才抛 `shutil.Error` 的 —— 中途一个文件读不动（杀软锁着某张图、旧实例还开着某个
    session 文件），半份数据就留在 target 上了：`.env` 那一步在同一个 try 里没执行到，
    密钥被当成缺失重新生成，库里的凭据全部解不开，而那半份数据看着又像是接管成功了。

    整段是尽力而为：接不过来只是回到「从头配一遍」，不该拦住启动。
    """
    if not is_frozen():
        return                                  # 只服务便携包。源码态跑到这里会去扫仓库
    target = data_dir()                         # 的父目录，把隔壁项目的 data 拷进来
    if _has_user_data(target):
        return                                  # 已经有用户自己的东西了，别覆盖
    old = _previous_install(root)
    if not old:
        return
    staging = target + ".partial"
    retired = target + ".unused"
    moved = False
    try:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(os.path.join(old, "data"), staging)
        old_env = os.path.join(old, ".env")
        if os.path.exists(old_env) and not os.path.exists(os.path.join(staging, ".env")):
            # 密钥必须一起搬，否则库里的数据源密码全部解不开
            shutil.copy2(old_env, os.path.join(staging, ".env"))
        if os.path.exists(target):
            # 试跑那一下留下的空壳。先改名让路而不是直接删 —— 万一下一步没成，
            # 还挪得回去（它里面那份 .env 就是当前生效的密钥）
            shutil.rmtree(retired, ignore_errors=True)
            os.rename(target, retired)
            moved = True
        os.rename(staging, target)              # 到这一步才对外可见
        shutil.rmtree(retired, ignore_errors=True)
        _line(f"  检测到旧版本 {os.path.basename(old)}，已接管它的配置与数据")
        _line("  确认新版本一切正常后，旧文件夹就可以删了")
    except Exception as e:
        # 留下半份比什么都没有更糟：数据不全，而 .env 那一步在同一个 try 里没走到，
        # 密钥会被当成缺失重新生成 —— 看着像接管成功了，凭据却全部解不开
        shutil.rmtree(staging, ignore_errors=True)
        if moved and not os.path.exists(target):
            try:
                os.rename(retired, target)      # 让路的那份挪回来，别让它凭空消失
            except OSError:
                # 锁还没放开。说清楚东西在哪，别让用户以为数据目录凭空没了
                _line(f"         原数据目录暂时叫「{os.path.basename(retired)}」，"
                      f"手工改回「{os.path.basename(target)}」即可")
        _line(f"  [警告] 旧版本数据没能接过来: {e}")
        _line("         本次按全新安装启动；下次启动会再试一次")
        _line(f"         也可自己把 {os.path.join(old, 'data')} 整个复制成 {target}")


# ===== 1. 密钥 =====

def ensure_env_file(env_path: str) -> None:
    """没有 .env 就生成一份，只写加密密钥。

    收的是 **.env 的完整路径**（调用方给 `paths.env_file()`）而不是包根 ——
    写成内部现算的话，传进来的目录就成了一个不起作用的形参，
    而它真实写入的位置靠全局状态决定（实测踩过：测试传临时目录，它去动了仓库根的 .env）。

    **不生成 API_KEY**：便携包绑 127.0.0.1，按项目既定姿态「未设 API_KEY 时不鉴权」正合适；
    自动生成一把反而要求用户先去前端粘贴一次才能用，凭空多一道坎。想开局域网访问的
    做法写在《使用说明》里。

    **SECRET_KEY 必须有**：缺了它后端会拒绝保存数据源凭据并返回 400（绝不降级成明文落库），
    用户会卡在「数据源」页而且看不出为什么。

    已有 .env 只补缺失的那一行，绝不覆盖 —— 换掉 SECRET_KEY 会让已录入的凭据全部解不开。
    """
    from cryptography.fernet import Fernet

    # .env 现在住在数据目录里，那个目录首次启动时还不存在
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    existing = ""
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            existing = f.read()
        if any(l.strip().startswith("TWEAKERS_SECRET_KEY=") and l.strip() != "TWEAKERS_SECRET_KEY="
               for l in existing.splitlines()):
            return
        _line("  .env 里缺加密密钥，补一行（不动其余内容）")
    else:
        _line("  首次启动，生成 .env（本机专属加密密钥）")

    key = Fernet.generate_key().decode()
    with open(env_path, "a" if existing else "w", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        if not existing:
            f.write("# HYXi 本机配置。SECRET_KEY 用于加密数据源密码，换掉它\n"
                    "# 已保存的凭据就全部解不开了，需要重新录入。\n")
        f.write(f"TWEAKERS_SECRET_KEY={key}\n")


# ===== 3. 自检 =====

def preflight(root: str, host: str, port: int) -> bool:
    """能不能起飞。返回 False 表示缺的是硬依赖，不该硬撑着启动。"""
    from app.config import resolve_node_executable

    ok = True

    # 校验**实际会被用到的那个** node，而不是写死包内路径 —— 采集子进程走的就是它
    node_exe = resolve_node_executable()
    if os.path.isabs(node_exe) and os.path.exists(node_exe):
        _line(f"  [OK]   Node 运行时  {node_exe}")
    elif not os.path.isabs(node_exe) and shutil.which(node_exe):
        _line(f"  [OK]   Node 运行时  {shutil.which(node_exe)}（来自 PATH）")
    else:
        _line(f"  [失败] 找不到 Node 运行时（期望 {os.path.join(root, 'node', 'node.exe')}）")
        _line("         包被拆散了？请重新解压完整的压缩包，不要只拷贝其中几个文件夹")
        ok = False

    if os.path.exists(os.path.join(root, "node_modules", "playwright", "package.json")):
        _line("  [OK]   采集依赖 playwright")
    else:
        _line("  [失败] 找不到 node_modules\\playwright，同样是包不完整")
        ok = False

    # Chrome 缺失**不拦启动**：配 LLM、翻历史数据、导报告都不需要它，只有采集需要
    chrome = find_chrome()
    if chrome:
        _line(f"  [OK]   Google Chrome  {chrome}")
    else:
        _line("  [警告] 没有检测到 Google Chrome。")
        _line("         采集脚本用真实 Chrome 启动（Playwright 自带的 chromium 不满足），")
        _line("         缺它只影响「采集」，其余功能照常。请到 https://www.google.cn/chrome/ 安装。")

    if port_in_use(host, port):
        _line(f"  [失败] 端口 {port} 已被占用 —— HYXi 可能已经在运行了，")
        _line(f"         先看看浏览器里是不是已经开着 http://{host}:{port}")
        ok = False
    else:
        _line(f"  [OK]   端口 {port} 空闲")

    return ok


def find_chrome():
    """真实 Chrome 的安装位置。注册表优先，找不到再看默认目录。"""
    try:
        import winreg
        for root_key in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"):
                try:
                    with winreg.OpenKey(root_key, sub) as k:
                        path = winreg.QueryValue(k, None)
                    if path and os.path.exists(path):
                        return path
                except OSError:
                    continue
    except ImportError:
        pass
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(path):
            return path
    return None


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


# ===== 4. 起服务 =====

def main() -> int:
    root = project_root()
    _line("HYXi 舆情分析平台")
    _line(f"程序目录: {root}")
    _line()

    _line("==> 准备配置")
    # 顺序锁死：先接管旧版本的数据，再补密钥 —— 反过来会先生成一把**新**密钥，
    # 旧 .env 因为「已存在」不再被复制，于是数据搬过来了、密码却全部解不开
    adopt_previous_install(root)
    env_path = env_file()
    try:
        ensure_env_file(env_path)
    except Exception as e:
        _line(f"  [失败] 生成 .env 失败: {e}")
        _line(f"         写不进 {os.path.dirname(env_path)}")
        _line("         该位置可能是只读的，请把整个文件夹解压到有写权限的地方")
        return 1
    _line(f"  数据目录: {data_dir()}")

    # .env 必须在这一行之前就位
    from app.config import settings

    # 用户按《使用说明》开局域网访问时 host 是 0.0.0.0，那是个「监听所有网卡」的写法，
    # 不是能往浏览器里粘的地址 —— 显示和打开都得换回本机地址
    local_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::", "") else settings.host

    _line()
    _line("==> 自检")
    if not preflight(root, local_host, settings.port):
        _line()
        _line("启动中止。")
        return 1

    url = f"http://{local_host}:{settings.port}"
    _line()
    _line("==> 启动服务")
    _line(f"    地址: {url}")
    if local_host != settings.host:
        _line(f"    （监听 {settings.host}，同一局域网内可用本机 IP 访问）")
    _line(f"    关闭这个窗口即停止服务。数据都在 {data_dir()}")
    _line()

    # 浏览器要等服务真的能应答再开，否则用户看到的是一张连接失败页。
    # HYXI_NO_BROWSER 供自动化验收用 —— 否则每验收一次就往操作者脸上弹一个窗口
    if not os.environ.get("HYXI_NO_BROWSER"):
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    import uvicorn
    from main import app

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")

    # 收尾的话也得由这里说。启动器是**纯 ASCII** 的 —— cmd.exe 按读取时生效的代码页
    # 解析 .bat，chcp 之后再遇到中文会把多字节序列拆断、整行碎成不存在的命令
    # （实测报出 "'锛夎蛋涓嶅埌杩欓噷銆?REM' 不是内部或外部命令"）。中文一律从这里出，
    # 控制台此时已被启动器切到 65001，UTF-8 显示正常
    _line()
    _line(f"服务已停止。数据都保存在 {data_dir()}，升级不会丢。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
