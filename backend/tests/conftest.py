"""pytest 会话级护栏 —— 在任何测试模块 import app 之前生效。

一、数据目录默认指向本进程自己的临时目录。
   以前「测试不写真实库」靠的是 test_api.py 恰好排在最前面、setup_class 先改 data_dir 再 import
   orchestrator；单跑某个类、或并行打乱顺序时 orchestrator 一导入就对真实库 init_db / 迁移 / 归并。
   tasks_dir / exports_dir 是 Settings 类定义时从真实数据目录算好的默认值，只盖 data_dir 管不到，一并指走。
   环境变量无条件覆盖：测试永远不该用真实数据目录，哪怕外面的 shell 里设了。

二、标记（见 CLAUDE.md「测试」一节的快慢分道与并行命令）：
   - browser：起真 Chrome 的类（Playwright）。快道 `-m "not browser"`
   - xdist_group：定义了 setup_class 的类整类留在同一个进程（--dist loadgroup），类级状态照串行时的样子；
     其余用例逐条分发
"""

import logging
import os
import shutil
import tempfile
import time

import pytest

SESSION_DIR_PREFIX = "hyxi-test-data-"
# 超过这么久没动过的会话目录算上次删不掉的残留。全量最长一小时，给足两倍，并行的别的 worker 目录还新、不会被误删
STALE_SESSION_SECONDS = 2 * 3600


def close_file_handlers_under(path):
    """关掉写在 path 里的日志文件句柄。应用的滚动日志落在会话临时目录里，收尾时还开着 ——
    Windows 上删不掉打开中的文件，rmtree 的错误又被 ignore_errors 吞了，目录就一直留着（实测攒了 21 个）"""
    root = os.path.normcase(os.path.realpath(path))
    loggers = [logging.getLogger()] + [l for l in logging.Logger.manager.loggerDict.values()
                                       if isinstance(l, logging.Logger)]
    for logger in loggers:
        for handler in list(logger.handlers):
            filename = getattr(handler, "baseFilename", "")
            if filename and os.path.normcase(os.path.realpath(filename)).startswith(root + os.sep):
                logger.removeHandler(handler)
                handler.close()


def remove_stale_session_dirs(root, max_age_seconds=STALE_SESSION_SECONDS):
    """清掉以前的会话留下、删不掉的临时数据目录。只动本前缀、且很久没动过的"""
    cutoff = time.time() - max_age_seconds
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name.startswith(SESSION_DIR_PREFIX) and os.path.isdir(path):
            try:
                if os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


remove_stale_session_dirs(tempfile.gettempdir())
_SESSION_DATA = tempfile.mkdtemp(prefix=SESSION_DIR_PREFIX)
for _var, _sub in (("TWEAKERS_DATA_DIR", ""), ("TWEAKERS_TASKS_DIR", "tasks"), ("TWEAKERS_EXPORTS_DIR", "exports")):
    _path = os.path.join(_SESSION_DATA, _sub) if _sub else _SESSION_DATA
    os.makedirs(_path, exist_ok=True)
    os.environ[_var] = _path

# 起真 Chrome 的类。漏标只影响快道快不快，不影响全量和按影响面选测（它们不看这个标记）
BROWSER_CLASSES = {
    "TestFacebookLoginEndToEnd",
    "TestSessionStateReflectsReality",
    "TestTweakersCollectorGoldenEndToEnd",
    "TestTweakersThreadAndQuotesEndToEnd",
    "TestTweakersIncrementalTopicEndToEnd",
    "TestGroupFeedCollectorEndToEnd",
    "TestThreadDialogExtractionEndToEnd",
}


def pytest_configure(config):
    config.addinivalue_line("markers", "browser: 起真 Chrome 的用例（慢道）")
    # 没装 pytest-xdist 时也要认得这个标记，否则每条都是一个 PytestUnknownMarkWarning
    config.addinivalue_line("markers", "xdist_group(name): 并行时同组用例留在同一个进程")


# tryfirst：pytest-xdist 在 worker 里按 xdist_group 标记给 nodeid 加分组后缀的钩子注册得比 conftest 晚、
# 按后注册先执行的顺序会先跑 —— 那时标记还没打上，分组整个不生效（实测 TestAPIEndpointsEndToEnd 被拆到两个进程）
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.cls is None:
            continue
        if item.cls.__name__ in BROWSER_CLASSES:
            item.add_marker(pytest.mark.browser)
        if "setup_class" in vars(item.cls) or "teardown_class" in vars(item.cls):
            item.add_marker(pytest.mark.xdist_group(f"{item.path.name}::{item.cls.__name__}"))


def pytest_sessionfinish(session, exitstatus):
    close_file_handlers_under(_SESSION_DATA)
    shutil.rmtree(_SESSION_DATA, ignore_errors=True)
