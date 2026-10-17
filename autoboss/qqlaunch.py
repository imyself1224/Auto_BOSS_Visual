"""`run.py bot` 启动时把 QQ 小号挂起来：SnowLuma 没开就开 SnowLuma，QQ 没开就开 QQ。

SnowLuma 的工作方式是把 hook 注入**正在运行的** QQ.exe，所以两个都得在跑。
顺序无所谓（它会一直等 QQ.exe 出现），这里先开 SnowLuma 再开 QQ。

已经在跑的不会重复开：
* SnowLuma —— 看它的 WebUI 端口（默认 5099）有没有在监听；
* QQ —— 看有没有 QQ.exe 进程。

两个都开在**独立的窗口**里，不挂在 bot 进程下面：bot 退出、重启都不会把 QQ 带走，
SnowLuma 的控制台窗口还能直接看日志（首次启动的 WebUI 临时密码也印在那里）。

QQ 打开后仍要在 QQ 窗口里登录小号（SnowLuma 只支持扫码 / QQ 自己的快速登录），
登录完 SnowLuma 才会开出 OneBot 端口，bot 那边的重连循环会自己连上。
"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

from .config import PROJECT_ROOT

DEFAULT_QQ_PATH = r"C:\Program Files\Tencent\QQNT\QQ.exe"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def process_running(image: str) -> bool:
    """Windows 上按进程名查（tasklist），其它平台一律当作没在跑。"""
    if os.name != "nt":
        return False
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
        capture_output=True, text=True, errors="ignore",
    ).stdout
    return image.lower() in out.lower()


class QQLauncher:
    def __init__(self, cfg):
        self.enabled = bool(cfg.get("qq.auto_launch", False))
        # 相对路径按项目根目录算，默认就是 auto_boss/SnowLuma
        self.snowluma_dir = PROJECT_ROOT / str(cfg.get("qq.snowluma_dir", "SnowLuma") or "SnowLuma")
        self.webui_port = int(cfg.get("qq.snowluma_webui_port", 5099) or 5099)
        self.qq_path = str(cfg.get("qq.qq_path", "") or DEFAULT_QQ_PATH)
        # 测试里换成桩，保证测试不会真的开 QQ
        self.popen = subprocess.Popen
        self.port_open = port_open
        self.process_running = process_running

    def ensure(self) -> None:
        """按需拉起 SnowLuma 和 QQ。失败只打印提示，不抛异常 —— 手动开好也一样能用。"""
        if not self.enabled:
            return
        self._ensure_snowluma()
        self._ensure_qq()

    def _ensure_snowluma(self):
        if self.port_open(self.webui_port):
            print(f"SnowLuma 已在运行（WebUI http://127.0.0.1:{self.webui_port}）")
            return
        root = self.snowluma_dir
        launcher = root / "launcher.bat"
        if not launcher.is_file():
            print(f"  [qq] 找不到 {launcher}，跳过自动启动 SnowLuma")
            return
        env = dict(os.environ)
        # 发现 QQ.exe 就自动注入，不用去 WebUI 手动点 Load（环境变量优先于 runtime.json）
        env["SNOWLUMA_HOOK_AUTOLOAD"] = "1"
        try:
            self.popen(["cmd", "/c", str(launcher)], cwd=str(root), env=env,
                       creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except OSError as exc:
            print(f"  [qq] 启动 SnowLuma 失败（{exc}）")
            return
        print(f"已启动 SnowLuma（新窗口），WebUI http://127.0.0.1:{self.webui_port}")

    def _ensure_qq(self):
        if self.process_running("QQ.exe"):
            print("QQ 已在运行")
            return
        if not Path(self.qq_path).is_file():
            print(f"  [qq] 找不到 QQ：{self.qq_path}，请在 [qq].qq_path 里改成实际路径")
            return
        try:
            self.popen([self.qq_path], cwd=str(Path(self.qq_path).parent),
                       creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except OSError as exc:
            print(f"  [qq] 启动 QQ 失败（{exc}）")
            return
        print("已启动 QQ —— 请在 QQ 窗口里登录小号，登录后 bot 会自动连上")
