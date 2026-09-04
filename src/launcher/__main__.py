"""Entry point（守护/客户端分离——docker 模型）。

- ``python -m src.launcher --daemon``：监督树唯一持久属主——LauncherApi +
  monitor + ProcessManager，setsid 自活（终端关闭不亡）；SIGTERM/SIGINT
  优雅关停全树。
- ``python -m src.launcher``（默认）：瘦客户端 shell——探活 :9421，活着
  即连上同一棵树；死了自动拉起守护再连。``quit`` 只关 shell（目标照跑），
  ``shutdown`` 动词才全树关停。

契约 components/launcher.yaml（Runs: python -m src.launcher；Binds :9421）。
配置面：launcher 单写者（守护侧）——conf/launcher.conf.json（监督
参数）+ conf/guilds.conf.json（频道清单，薄钉字段，缺省空表）。
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time

from .client import LauncherClient
from .process_manager import ProcessManager

CONFIG_PATH = os.path.join("conf", "launcher.conf.json")
GUILDS_PATH = os.path.join("conf", "guilds.conf.json")

SPAWN_WAIT_SEC = 15.0


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def run_daemon(port):
    """守护模式：API + monitor + ProcessManager；信号驱动优雅退出。"""
    from .api import LauncherApi

    config = _load_json(CONFIG_PATH, {})
    guilds = _load_json(GUILDS_PATH, {}).get("guilds", [])
    pm = ProcessManager(config)

    done = threading.Event()

    def _on_signal(signum, frame):
        done.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    api = LauncherApi(
        pm,
        config=config,
        config_path=CONFIG_PATH,
        guilds=guilds,
        port=port,
        shutdown_callback=lambda: done.set(),
    )
    threading.Thread(target=api.serve_forever, daemon=True).start()

    while not done.is_set():
        try:
            pm.monitor()
        except Exception:  # noqa: BLE001 —— 监视线兜底：单轮异常不许拖死守护
            pass
        done.wait(1.0)

    pm.graceful_shutdown()
    api.stop()


def _spawn_daemon(port):
    """拉起守护子进程（新会话——本 shell 退出不牵连）。"""
    log_dir = os.path.join("log", "launcher")
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "daemon.log"), "a")
    subprocess.Popen(
        [
            sys.executable, "-m", "src.launcher", "--daemon",
            "--port", str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _wait_daemon(client, timeout=SPAWN_WAIT_SEC):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.alive():
            return True
        time.sleep(0.25)
    return False


def run_client_shell(port):
    """客户端模式：探活 → （死则拉起 + 等待）→ 远程 Dispatcher + Shell。"""
    from .commands import Dispatcher
    from .shell import Shell

    client = LauncherClient(port)
    if not client.alive():
        print("launcher daemon not running on :{0} — spawning...".format(port))
        _spawn_daemon(port)
        if not _wait_daemon(client):
            print(
                "daemon failed to start (see log/launcher/daemon.log);"
                " exiting."
            )
            return 1
        print("daemon up on :{0}".format(port))

    config = client.config_get()
    dispatcher = Dispatcher(None, config, CONFIG_PATH, remote=client)
    shell = Shell(client, config, CONFIG_PATH, dispatcher)
    shell.run()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Prometheus launcher: supervision daemon + client shell",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override launcher_port from conf/launcher.conf.json (default 9421)",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as the supervision daemon (targets keep running after"
             " client shells exit)",
    )
    args = parser.parse_args(argv)

    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.chdir(project_root)

    if not args.daemon:
        err_log = os.path.join("log", "launcher", "stderr.log")
        os.makedirs(os.path.dirname(err_log), exist_ok=True)
        sys.stderr = open(err_log, "a")

    config = _load_json(CONFIG_PATH, {})
    port = args.port if args.port is not None else config.get("launcher_port", 9421)

    if args.daemon:
        run_daemon(port)
        return 0
    return run_client_shell(port)


if __name__ == "__main__":
    sys.exit(main())
