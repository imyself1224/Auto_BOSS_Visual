"""QQ 通知 + 遥控：走 OneBot v11 协议。

小号用 NapCat / LLOneBot / Lagrange 这类 OneBot 实现登录，开一个**正向 WebSocket**
服务（默认 ws://127.0.0.1:3001）。程序这边只当 websocket 客户端，跟浏览器那边一样
用 `websockets` 库，不引入新依赖。

两个角色：

* `Notifier` —— 打招呼进程用。每投出一条、每次停机，给大号发一条私聊。
  每次发消息开一条短连接，发完就断：通知频率很低（几十秒一条），
  没必要为它维护长连接；NapCat 没开时也只是打印一句警告，**绝不影响投递本身**。
  断网 / OneBot 掉线时发不出去的消息会存进 `logs/qq_outbox/`，
  连上后自动补发，不会丢。
* `OneBot` —— `run.py bot` 常驻进程用。长连接收事件、回消息。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

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

    def send_private(self, user_id: int, message) -> str:
        """发私聊，返回 echo。默认不等回执；Notifier 会拿 echo 去等发送结果。

        `message` 可以是纯文本，也可以是 OneBot 消息段数组（文本 + 图片），
        数组时**不能**带 `auto_escape`，否则图片段会被当成转义文本。
        """
        echo = uuid.uuid4().hex[:8]
        params = {"user_id": int(user_id), "message": message}
        if isinstance(message, str):
            params["auto_escape"] = True
        self.ws.send(json.dumps({
            "action": "send_private_msg",
            "params": params,
            "echo": echo,
        }, ensure_ascii=False))
        return echo

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
    """给大号推送消息。没启用时所有方法都是空操作，调用方不用判断。

    发不出去（断网 / OneBot 掉线）时消息存到 ``logs/qq_outbox/``，
    下次发送成功或 bot 重新连上时再补发 —— 断网期间发生的事不会丢。
    """

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
        self.outbox_dir = cfg.logs_dir / "qq_outbox"
        self._send_lock = threading.Lock()

    # --- 发送 / 补发 ---------------------------------------------------
    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        with self._send_lock:
            # 先把断网期间攒下的发完，尽量保持先后顺序
            if self._flush_locked() and self._send_now(text):
                return True
            self._enqueue(text)
            return False

    def flush(self) -> bool:
        """重新连上 QQ 后补发断网期间攒下的消息。"""
        if not self.enabled:
            return False
        with self._send_lock:
            return self._flush_locked()

    def _send_now(self, text: str) -> bool:
        try:
            with OneBot(self.ws_url, self.token) as bot:
                echo = bot.send_private(self.owner, text)
                # 等 OneBot 的回执：连不上 QQ 服务器时本地 WS 照样通，
                # 光看 send() 没抛异常会误以为发出去了（断网就是这样）。
                self._await_echo(bot, echo)
            return True
        except Exception as exc:  # noqa: BLE001
            # NapCat 没开时每条都会失败，警告别刷屏：一分钟最多一句
            if time.time() - self._warned_at > 60:
                self._warned_at = time.time()
                print(f"  [qq] 通知发送失败（{type(exc).__name__}: {exc}）"
                      "—— 已存进 logs/qq_outbox，连上后补发")
            return False

    def _await_echo(self, bot: OneBot, echo: str, timeout: float = 4.0):
        """等这条动作的回执，retcode 非 0 视为没送达。

        有的实现不回动作回执：超时就当成功，宁可漏存也不要重复补发。
        """
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                raw = bot.ws.recv(timeout=remaining)
            except TimeoutError:
                return
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("echo") != echo:
                continue   # 别的动作回执 / 事件，跟这条无关
            status = str(data.get("status") or "").lower()
            retcode = data.get("retcode")
            if status == "failed" or (retcode is not None and int(retcode) not in (0, 1)):
                raise RuntimeError(
                    f"OneBot 返回失败：status={status!r} retcode={retcode!r} "
                    f"{data.get('message') or data.get('wording') or ''}".strip())
            return

    def _flush_locked(self) -> bool:
        """补发 outbox。返回 False 表示还没连通，队列里还有东西。"""
        try:
            files = sorted(self.outbox_dir.glob("*.json"))
        except OSError:
            return True
        for path in files:
            try:
                text = str(json.loads(path.read_text(encoding="utf-8")).get("text", ""))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)   # 坏文件别把队列卡死
                continue
            if not self._send_now(text):
                return False
            path.unlink(missing_ok=True)
        return True

    def _enqueue(self, text: str) -> None:
        """断网时落到本地。同一条连续失败只存一份，免得崩溃循环把队列刷爆。"""
        try:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            files = sorted(self.outbox_dir.glob("*.json"))
            if files:
                try:
                    last = json.loads(files[-1].read_text(encoding="utf-8")).get("text")
                    if last == text:
                        return
                except (OSError, ValueError):
                    pass
            # 别无限攒：只留最近 200 条
            for old in files[:-200]:
                old.unlink(missing_ok=True)
            name = (f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_"
                    f"{uuid.uuid4().hex[:6]}.json")
            (self.outbox_dir / name).write_text(
                json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError as exc:
            print(f"  [qq] 通知发不出去，也存不进 outbox（{exc}）")

    def job_sent(self, title: str, company: str, salary: str, total: int, cap: int):
        if self.notify_each:
            self.send(f"✅ 已投递（今日第 {total} 条）\n{title}\n@ {company}"
                      + (f"\n💰 {salary}" if salary else ""))
