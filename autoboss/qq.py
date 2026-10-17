"""QQ 通知 + 遥控：走 OneBot v11 协议。

小号用 NapCat / LLOneBot / Lagrange 这类 OneBot 实现登录，开一个**正向 WebSocket**
服务（默认 ws://127.0.0.1:3001）。程序这边只当 websocket 客户端，跟浏览器那边一样
用 `websockets` 库，不引入新依赖。

两个角色：

* `Notifier` —— 打招呼进程用。每投出一条、每次停机，给大号发一条私聊。
  每次发消息开一条短连接，发完就断：通知频率很低（几十秒一条），
  没必要为它维护长连接；NapCat 没开时也只是打印一句警告，**绝不影响投递本身**。
* `OneBot` —— `run.py bot` 常驻进程用。长连接收事件、回消息。
"""
from __future__ import annotations

import json
import time
import uuid

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect


class OneBot:
    """OneBot v11 正向 WebSocket 客户端（同一条连接上既收事件也发动作）。"""

    def __init__(self, ws_url: str, token: str = "", open_timeout: float = 5.0):
        self.ws_url = ws_url
        self.token = token
        self.open_timeout = open_timeout
        self.ws = None

    def __enter__(self):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        self.ws = connect(self.ws_url, additional_headers=headers,
                          open_timeout=self.open_timeout, max_size=None)
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None

    def send_private(self, user_id: int, text: str):
        """发私聊。不等回执 —— 发不出去对方也看不到，等回执没有意义。"""
        self.ws.send(json.dumps({
            "action": "send_private_msg",
            "params": {"user_id": int(user_id), "message": text, "auto_escape": True},
            "echo": uuid.uuid4().hex[:8],
        }, ensure_ascii=False))

    def events(self, timeout: float | None = None):
        """逐个产出上报事件（带 post_type 的）。动作回执、心跳以外的东西都丢掉。

        timeout 秒内没有任何数据就产出 None，让调用方有机会做点别的（比如看子进程）。
        """
        while True:
            try:
                raw = self.ws.recv(timeout=timeout)
            except TimeoutError:
                yield None
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("post_type") \
                    and data.get("meta_event_type") != "heartbeat":
                yield data


class Notifier:
    """给大号推送消息。没启用时所有方法都是空操作，调用方不用判断。"""

    def __init__(self, cfg):
        self.ws_url = str(cfg.get("qq.ws_url", "ws://127.0.0.1:3001"))
        self.token = cfg.env(str(cfg.get("qq.access_token_env", "ONEBOT_TOKEN")))
        # QQ 号属于个人信息：config 里没写就去 .env 读（owner_qq_env 指定变量名）
        owner_env = str(cfg.get("qq.owner_qq_env", "") or "")
        owner = cfg.get("qq.owner_qq", 0) or (cfg.env(owner_env) if owner_env else 0)
        self.owner = int(str(owner).strip() or 0)
        self.enabled = bool(cfg.get("qq.enabled", False)) and self.owner > 0
        self.notify_each = bool(cfg.get("qq.notify_each", True))
        self._warned_at = 0.0

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            with OneBot(self.ws_url, self.token) as bot:
                bot.send_private(self.owner, text)
                # 立刻断开的话，个别实现会在处理完动作之前就把连接收掉
                time.sleep(0.3)
            return True
        except (OSError, TimeoutError, ConnectionClosed, Exception) as exc:  # noqa: BLE001
            # NapCat 没开时每条都会失败，警告别刷屏：一分钟最多一句
            if time.time() - self._warned_at > 60:
                self._warned_at = time.time()
                print(f"  [qq] 通知发送失败（{type(exc).__name__}: {exc}）—— 不影响投递")
            return False

    def job_sent(self, title: str, company: str, salary: str, total: int, cap: int):
        if self.notify_each:
            self.send(f"✅ 已投递（今日第 {total} 条）\n{title}\n@ {company}"
                      + (f"\n💰 {salary}" if salary else ""))
