"""浏览器层：启动带 CDP 调试口的真实浏览器，用原生 CDP websocket 驱动。

为什么不用 Playwright / Selenium
--------------------------------
1. **指纹**。Playwright 连上 CDP 后会往每个页面注入自己的 utility script，
   这是很显眼的自动化特征；BOSS 这类站点会据此判定。原生 CDP 不注入任何东西。
2. **target 归属明确**。Playwright 的 page 对象和 `new_cdp_session()` 拿到的
   session 可能落在**不同的 target** 上 —— 实测出现过 page.url 是 BOSS、
   而 CDP 命令打到了另一个标签页的情况，导致截图和实际操作对不上。
   原生 CDP 一个 target 一个 websocket，不存在这种歧义。
3. **可控的域（domain）**。我们只调用确实需要的命令，
   **不调用 `Runtime.enable` / `Page.enable` / `Debugger.enable`**。
   这几个 enable 正是最常见的 CDP 检测点（比如用 console.log 的 getter 探针）。
   `Runtime.evaluate` 不需要先 enable，所以画网格不受影响。

为什么用真实浏览器而不是无头
---------------------------
登录态需要你手动扫码，登录凭证存在独立 profile 里；真实浏览器 + 真实用户操作
的事件流，也比无头环境自然得多。
"""
from __future__ import annotations

import base64
import json
import os
import random
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

# 自动探测顺序：Windows 自带 Edge 优先，其次 Chrome。
_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_LAUNCH_FLAGS = [
    "--remote-allow-origins=*",          # 新版 Chromium 的 CDP 握手要求
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,OptimizationHints",
    "--disable-popup-blocking",
]

# CDP 消息体可能很大（截图 base64），给足上限。
_WS_MAX_SIZE = 96 * 1024 * 1024


def find_browser(explicit: str = "") -> str:
    if explicit:
        if not Path(explicit).exists():
            raise SystemExit(f"配置里的浏览器路径不存在：{explicit}")
        return explicit
    for path in _CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit(
        "没找到 Edge 或 Chrome。请在 config.toml 的 [browser].executable_path 里\n"
        "手动填写浏览器可执行文件路径。"
    )


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


# ===========================================================================
# 断线重连后**允许自动重试**的命令。
#
# 只包含只读/幂等操作。像 Input.dispatchMouseEvent 这种绝不重试 ——
# 万一第一次其实已经送达了，重试就会变成一次重复点击。
# 宁可让这一步失败被记进日志，也好过稀里糊涂点两下。
_RETRY_SAFE = frozenset({
    "Page.captureScreenshot",
    "Page.getLayoutMetrics",
    "Page.getNavigationHistory",
    "Runtime.evaluate",
    "Storage.getCookies",
    "Network.getCookies",
})


class CDPConnection:
    """到一个 target 的 CDP websocket 连接，带断线重连。"""

    def __init__(self, ws_url: str, timeout: float = 30.0, reconnect=None):
        self.ws_url = ws_url
        self.timeout = timeout
        # reconnect 是个无参可调用对象，返回新的 ws_url（target 还在的话）。
        # 由 BrowserSession 提供，用来在 target 没变时原地重连。
        self._reconnect = reconnect
        self._stack: ExitStack | None = None
        self._ws = None
        self._id = 0
        self.reconnects = 0
        self._open()

    def _open(self):
        self._stack = ExitStack()
        self._ws = self._stack.enter_context(
            connect(self.ws_url, max_size=_WS_MAX_SIZE, open_timeout=self.timeout)
        )

    def _reopen(self, tries: int = 3) -> bool:
        """断线后重连。多试几次：浏览器刚断开时 /json/list 和 websocket
        往往要过一两秒才恢复，只试一次就放弃，会把一次瞬断变成整轮崩溃。"""
        try:
            if self._stack:
                self._stack.close()
        except Exception:
            pass
        for attempt in range(tries):
            if attempt:
                time.sleep(1.5 * attempt)
            url = self.ws_url
            if self._reconnect is not None:
                try:
                    resolved = self._reconnect()
                except Exception:
                    resolved = None
                if not resolved:
                    continue        # target 暂时没列出来，等一下再看
                url = resolved
            self.ws_url = url
            try:
                self._open()
                self.reconnects += 1
                return True
            except Exception:
                continue
        return False

    def _send_once(self, method: str, params: dict | None, timeout: float | None) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

        limit = timeout or self.timeout
        deadline = time.time() + limit
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError(f"CDP 命令超时：{method}")
            try:
                raw = self._ws.recv(timeout=remain)
            except TimeoutError:
                raise TimeoutError(f"CDP 命令超时：{method}") from None
            msg = json.loads(raw)
            if msg.get("id") != mid:
                continue        # 事件推送，忽略
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(f"CDP {method} 失败：{err.get('message', err)}")
            return msg.get("result", {})

    def send(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        try:
            return self._send_once(method, params, timeout)
        except ConnectionClosed as exc:
            if method not in _RETRY_SAFE:
                raise RuntimeError(
                    f"CDP 连接中断（{method}）。该命令有副作用，不自动重试，"
                    f"以免重复操作。请重跑 `python run.py greet`。"
                ) from exc
            if not self._reopen():
                raise RuntimeError(
                    f"CDP 连接中断且无法重连（{method}）：{exc}。"
                    "目标标签页可能已被关闭，重跑即可。"
                ) from exc
            print("[browser] CDP 连接中断，已重连，正在重试…")
            return self._send_once(method, params, timeout)

    def close(self):
        try:
            if self._stack:
                self._stack.close()
        except Exception:
            pass


# ===========================================================================
class Page:
    """绑定到某个 page target 的高层操作。"""

    def __init__(self, conn: CDPConnection, target_id: str, http_base: str):
        self.conn = conn
        self.target_id = target_id
        self.http_base = http_base
        self._vp: tuple[int, int] | None = None

    # --- 视口 -------------------------------------------------------
    def viewport(self, refresh: bool = True) -> tuple[int, int]:
        """返回 CSS 像素下的视口宽高。

        注意用 `window.innerWidth` 而不是 `Page.getLayoutMetrics` 的
        `cssLayoutViewport`：后者不含滚动条宽度，而截图是**含**滚动条那条区域的。
        网格 canvas 也是按 innerWidth 铺的，三者必须用同一个基准，
        否则坐标会整体偏掉一个滚动条宽（约 16px）。
        """
        if self._vp is not None and not refresh:
            return self._vp
        res = self.evaluate("window.innerWidth + ',' + window.innerHeight")
        w, h = str(res).split(",")
        self._vp = (int(float(w)), int(float(h)))
        return self._vp

    # --- 截图 -------------------------------------------------------
    def focus(self):
        try:
            self.conn.send("Page.bringToFront", timeout=10)
        except Exception:
            pass

    def screenshot(self) -> bytes:
        """截取当前视口，PNG 字节。"""
        self.focus()
        res = self.conn.send("Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": False,
            # 必须用 True：实测 fromSurface=False 时 clip 参数会被**静默忽略**，
            # 局部放大截图会退化成整屏截图，两段式定位就完全失效了。
            "fromSurface": True,
        }, timeout=45)
        return base64.b64decode(res["data"])

    def screenshot_clip(self, x: float, y: float, w: float, h: float, scale: float = 3.0) -> bytes:
        """放大截取一小块区域，用于两段式定位的第二段。

        返回图尺寸 = (w × scale, h × scale)。
        """
        self.focus()
        res = self.conn.send("Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": False,
            "fromSurface": True,
            "clip": {"x": float(x), "y": float(y), "width": float(w),
                     "height": float(h), "scale": float(scale)},
        }, timeout=45)
        return base64.b64decode(res["data"])

    # --- 输入原语（CDP 产生可信事件）-------------------------------
    def mouse_move(self, x: float, y: float):
        self.conn.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "buttons": 0,
        })

    def click(self, x: float, y: float):
        self.conn.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "buttons": 1, "clickCount": 1,
        })
        time.sleep(random.uniform(0.06, 0.14))
        self.conn.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "buttons": 0, "clickCount": 1,
        })

    def type_text(self, text: str, min_delay: float = 0.04, max_delay: float = 0.12):
        """逐字符输入。

        不用 Input.insertText：那是瞬时整段插入，输入节奏和真人差异极大，
        且可能不触发前端框架的 input 事件。
        """
        for ch in text:
            self.conn.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "text": ch, "unmodifiedText": ch,
            })
            self.conn.send("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch})
            pause = random.uniform(min_delay, max_delay)
            if ch in "，。！？,.!?~":
                pause += random.uniform(0.05, 0.18)
            time.sleep(pause)

    def press_key(self, key: str):
        keymap = {
            "Enter": ("\r", 13), "Escape": ("", 27), "Tab": ("\t", 9),
            "Backspace": ("", 8), "ArrowDown": ("", 40), "ArrowUp": ("", 38),
        }
        text, code = keymap.get(key, ("", 0))
        self.conn.send("Input.dispatchKeyEvent", {
            "type": "rawKeyDown", "key": key, "windowsVirtualKeyCode": code,
        })
        if text:
            self.conn.send("Input.dispatchKeyEvent", {
                "type": "char", "text": text, "key": key,
            })
        self.conn.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": key, "windowsVirtualKeyCode": code,
        })

    def _scroll_top(self) -> float:
        """当前文档滚动位置，用来校验滚动是否真的生效。"""
        try:
            return float(self.evaluate("Math.round(window.scrollY)"))
        except Exception:
            return -1.0

    def scroll(self, dy: int, x: float | None = None, y: float | None = None,
               tries: int = 4) -> float:
        """滚动页面。dy > 0 向下。返回实际滚动的像素数。

        两个实测踩到的坑：

        1. `Input.dispatchMouseEvent` 的 `mouseWheel` 在当前 Edge 上**完全不生效**
            (scrollY 恒为 0)，所以改用 `Input.synthesizeScrollGesture`。
        2. 该手势在渲染器"预热"之前会被**静默丢弃** —— 实测同一个调用
           前两次没有任何效果，第三次才生效。输入管线在没接收过真实输入事件前
           处于空闲状态，合成手势会被扔掉。

        所以这里不假设一次就能成：先发一个 mouseMoved 唤醒输入管线，
        滚动后**读回 scrollY 校验**，没动就重试。
        """
        w, h = self.viewport()
        cx = int(x if x is not None else w / 2)
        cy = int(y if y is not None else h / 2)

        before = self._scroll_top()
        for attempt in range(tries):
            # 唤醒输入管线：没有这一步，首次滚动大概率被丢弃
            try:
                self.mouse_move(cx, cy)
            except Exception:
                pass
            time.sleep(0.06)

            try:
                self.conn.send("Input.synthesizeScrollGesture", {
                    "x": cx, "y": cy,
                    # 符号：yDistance 为负表示向下滚动
                    "xDistance": 0, "yDistance": -int(dy),
                    "gestureSourceType": "mouse",
                    "preventFling": True,       # 关掉惯性，位移可预期
                    "speed": random.randint(800, 1200),
                }, timeout=30)
            except Exception:
                # 老版本 Chromium 没这个命令：退回 JS 滚动
                # （滚动不涉及 isTrusted 校验，风险远低于 JS 注入点击）
                self.evaluate(f"window.scrollBy(0, {int(dy)})")
                time.sleep(0.4)
                return self._scroll_top() - before

            time.sleep(0.45)
            after = self._scroll_top()
            if after != before:
                return after - before

        return 0.0

    # --- 导航 / 求值 / Cookie ---------------------------------------
    def navigate(self, url: str, settle: float = 3.0):
        self.conn.send("Page.navigate", {"url": url}, timeout=45)
        time.sleep(settle)
        self._vp = None
        return self.current_url()

    def current_url(self) -> str:
        """从导航历史读当前 URL。

        比缓存 target 上的 url 字段可靠 —— 单页应用的路由切换不会更新那个字段，
        但会进导航历史。
        """
        try:
            hist = self.conn.send("Page.getNavigationHistory", timeout=10)
            idx = hist.get("currentIndex", 0)
            entries = hist.get("entries") or []
            if 0 <= idx < len(entries):
                return entries[idx].get("url", "")
        except Exception:
            pass
        return self._target_url()

    def _target_url(self) -> str:
        try:
            for t in list_targets(self.http_base):
                if t.get("id") == self.target_id:
                    return t.get("url", "")
        except Exception:
            pass
        return ""

    def title(self) -> str:
        try:
            hist = self.conn.send("Page.getNavigationHistory", timeout=10)
            idx = hist.get("currentIndex", 0)
            entries = hist.get("entries") or []
            if 0 <= idx < len(entries):
                return entries[idx].get("title", "")
        except Exception:
            pass
        return ""

    def evaluate(self, expression: str, timeout: float = 20.0):
        """执行 JS。

        注意：**不调用 `Runtime.enable`**。`Runtime.evaluate` 本身不需要 enable，
        而 enable 恰恰是最容易被探测到的 CDP 特征。
        """
        res = self.conn.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
        }, timeout=timeout)
        if "exceptionDetails" in res:
            det = res["exceptionDetails"]
            raise RuntimeError(f"页面 JS 执行异常：{det.get('text')}")
        return (res.get("result") or {}).get("value")

    def find_text_boxes(self, text: str, prefix: bool = False
                        ) -> list[tuple[float, float, float, float]]:
        """找出页面上**文字恰好等于 text**（`prefix=True` 时为"以 text 开头"）
        且当前可见、没被遮挡的元素，
        返回它们的 CSS 像素外框 (left, top, width, height)。

        只用来给**固定界面文案**（筛选栏按钮、下拉选项）取几何位置 ——
        这类文字没有字体加密，按文字查比让模型估坐标准得多：
        实测模型能把「求职类型」读对，外框却稳定往右偏 30~60 个归一化单位，
        点下去开的是隔壁「薪资待遇」。
        只读不写；点击仍然走 CDP 的可信鼠标事件。
        """
        expr = """(function(t, pre){
  var out=[], W=innerWidth, H=innerHeight;
  var it=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), n;
  while((n=it.nextNode())){
    var v=(n.nodeValue||'').trim();
    if(pre ? v.indexOf(t)!==0 : v!==t) continue;
    var el=n.parentElement; if(!el) continue;
    var r=el.getBoundingClientRect();
    if(r.width<=0||r.height<=0||r.right<=0||r.bottom<=0||r.left>=W||r.top>=H) continue;
    var hit=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
    if(!hit||!(hit===el||el.contains(hit)||hit.contains(el))) continue;
    out.push([r.left,r.top,r.width,r.height]);
  }
  return JSON.stringify(out);
})(%s, %s)""" % (json.dumps(text), "true" if prefix else "false")
        try:
            boxes = json.loads(self.evaluate(expr) or "[]")
        except Exception:
            return []
        return [tuple(float(v) for v in b) for b in boxes if len(b) == 4]

    def text_markers(self, markers) -> list[str]:
        """页面可见文字里出现了 markers 中的哪些（document.body.innerText 子串匹配）。"""
        expr = ("(function(ms){var t=document.body?document.body.innerText:'';"
                "return JSON.stringify(ms.filter(function(m){return t.indexOf(m)>=0;}));})(%s)"
                % json.dumps(list(markers), ensure_ascii=False))
        try:
            return list(json.loads(self.evaluate(expr) or "[]"))
        except Exception:
            return []

    def cookies(self) -> dict[str, str]:
        try:
            res = self.conn.send("Storage.getCookies", timeout=15)
        except Exception:
            return {}
        out: dict[str, str] = {}
        for c in res.get("cookies", []):
            if "zhipin.com" in (c.get("domain") or ""):
                out[c["name"]] = c["value"]
        return out

    def is_logged_in(self) -> bool:
        """wt2 是 BOSS 的登录凭证 cookie，存在即视为已登录。"""
        return "wt2" in self.cookies()

    def close(self):
        self.conn.close()


# ===========================================================================
#  target 管理（走 HTTP 端点）
# ===========================================================================
def list_targets(http_base: str, timeout: float = 10.0) -> list[dict]:
    url = f"{http_base}/json/list"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def page_targets(http_base: str) -> list[dict]:
    try:
        return [t for t in list_targets(http_base) if t.get("type") == "page"]
    except Exception:
        return []


# ===========================================================================
class BrowserSession:
    """负责把浏览器拉起来、连上去、用完断开。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.port = int(cfg.get("browser.cdp_port", 9222))
        self.profile = cfg.user_data_dir
        self.exe = find_browser(str(cfg.get("browser.executable_path", "")))
        self.http_base = f"http://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------
    def launch(self):
        """端口没开就拉起浏览器；已开着就直接复用。"""
        self.profile.mkdir(parents=True, exist_ok=True)

        if port_open(self.port):
            print(f"[browser] 复用已在运行的调试端口 {self.port}")
            return

        w = int(self.cfg.get("browser.viewport_width", 1600))
        h = int(self.cfg.get("browser.viewport_height", 900))
        args = [
            self.exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            f"--window-size={w},{h}",
            "--window-position=80,60",
            *_LAUNCH_FLAGS,
        ]
        print(f"[browser] 启动 {Path(self.exe).name}，profile={self.profile}")
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

        deadline = time.time() + 30
        while time.time() < deadline:
            if port_open(self.port):
                return
            time.sleep(0.4)
        raise SystemExit(
            f"浏览器已启动但调试端口 {self.port} 一直没响应。\n"
            "常见原因：该 profile 已被另一个浏览器实例占用。\n"
            f"请先关掉所有使用 {self.profile} 的窗口再试。"
        )

    # ------------------------------------------------------------------
    def start(self) -> Page:
        self.launch()
        targets = page_targets(self.http_base)
        if not targets:
            raise SystemExit(
                "调试端口上没有可用的标签页。请手动在该浏览器窗口里新建一个标签页后重试。"
            )
        self._page = self._attach(targets[-1])
        return self._page

    def _attach(self, target: dict) -> Page:
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise SystemExit(f"target {target.get('id')} 没有 websocket 地址")
        target_id = target["id"]
        http_base = self.http_base

        def resolve_ws_url():
            """断线后按 target id 重新找地址 —— 重连后 ws_url 可能已经变了。"""
            for t in page_targets(http_base):
                if t.get("id") == target_id:
                    return t.get("webSocketDebuggerUrl")
            return None

        conn = CDPConnection(ws_url, reconnect=resolve_ws_url)
        page = Page(conn, target_id, http_base)
        page.focus()
        return page

    # ------------------------------------------------------------------
    def sync_page(self, current: Page | None) -> Page:
        """动作之后可能开了新标签页，这里重新对齐。

        只在「当前 target 没了」或「出现了别的 zhipin 标签页」时才切换，
        避免在两个标签之间来回横跳。
        """
        targets = page_targets(self.http_base)
        if not targets:
            if current is not None:
                return current
            raise SystemExit("找不到可用的标签页")

        if current is not None:
            still_there = any(t.get("id") == current.target_id for t in targets)
            if still_there:
                others = [t for t in targets
                          if t.get("id") != current.target_id
                          and "zhipin.com" in (t.get("url") or "")]
                if not others:
                    return current
                target = others[-1]
            else:
                zhipin = [t for t in targets if "zhipin.com" in (t.get("url") or "")]
                target = (zhipin or targets)[-1]
        else:
            zhipin = [t for t in targets if "zhipin.com" in (t.get("url") or "")]
            target = (zhipin or targets)[-1]

        if current is not None and target.get("id") == current.target_id:
            return current

        current.close() if current else None
        print(f"[browser] 切换标签页：{str(target.get('url'))[:90]}")
        self._page = self._attach(target)
        return self._page

    # ------------------------------------------------------------------
    def stop(self):
        """只断开 CDP，不关浏览器 —— 方便你人工接管处理验证码。"""
        if self._page is not None:
            self._page.close()
            self._page = None
