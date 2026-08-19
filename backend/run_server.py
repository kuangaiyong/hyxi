"""便携包的启动入口 —— Nuitka 编译的就是这个文件。

源码开发态**不走这里**（那条路是 `start.ps1` 起 uvicorn + Vite 两个进程），所以这里
只管打包态需要、而开发态天然成立的那些事：密钥自动生成、依赖自检、单进程起服务。

四件事的顺序是锁死的，别调换：
  1. 先算包根、生成 .env
  2. 再 import app.config（`settings` 是 import 期实例化并当场读 .env 的）
  3. 起飞前自检
  4. uvicorn.run + 开浏览器
"""

import os
import shutil
import socket
import sys
import threading
import warnings
import webbrowser

# pydantic 会为 model_name 这个字段名警告两次「conflict with protected namespace」。
# 对开发者是噪音，对使用者是吓人的东西 —— 一个双击启动的人看到满屏 UserWarning
# 会以为程序坏了。这只在打包态的启动路径上屏蔽，源码态照常暴露出来
warnings.filterwarnings("ignore", message=".*protected namespace.*")

from app.paths import project_root  # noqa: E402


def _line(text: str = "") -> None:
    print(text, flush=True)


# ===== 1. 密钥 =====

def ensure_env_file(root: str) -> None:
    """没有 .env 就生成一份，只写加密密钥。

    **不生成 API_KEY**：便携包绑 127.0.0.1，按项目既定姿态「未设 API_KEY 时不鉴权」正合适；
    自动生成一把反而要求用户先去前端粘贴一次才能用，凭空多一道坎。想开局域网访问的
    做法写在《使用说明》里。

    **SECRET_KEY 必须有**：缺了它后端会拒绝保存数据源凭据并返回 400（绝不降级成明文落库），
    用户会卡在「数据源」页而且看不出为什么。

    已有 .env 只补缺失的那一行，绝不覆盖 —— 换掉 SECRET_KEY 会让已录入的凭据全部解不开。
    """
    from cryptography.fernet import Fernet

    env_path = os.path.join(root, ".env")
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
    try:
        ensure_env_file(root)
    except Exception as e:
        _line(f"  [失败] 生成 .env 失败: {e}")
        _line("         程序目录可能是只读的，请把整个文件夹解压到有写权限的位置")
        return 1

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
    _line("    关闭这个窗口即停止服务。数据都在程序目录的 data 文件夹里。")
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
    _line("服务已停止。数据都保存在程序目录的 data 文件夹里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
