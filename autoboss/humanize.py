"""拟人化动作。

风控真正看的是**行为分布**，不是单次点击：鼠标是否瞬移、间隔是否恒定、
是否只在工作时间活动。所以这里的重点是引入受控的随机性。

注意别过度设计 —— 加一大堆假的"人类噪声"本身就是一种可识别的特征。
目标是"像人在用"，不是"像机器人假装是人"。
"""
from __future__ import annotations

import math
import random
import time


def sleep(sec: float):
    time.sleep(max(0.0, sec))


def pause(a: float, b: float):
    """闭区间随机停顿。"""
    time.sleep(random.uniform(a, b))


def think_pause(base: float = 0.6, spread: float = 1.4):
    """模拟"读完内容想一下"的停顿。"""
    time.sleep(base + random.expovariate(1.0 / spread))


def human_delay(min_sec: float, max_sec: float):
    """两次打招呼之间的间隔。

    用三角分布而不是均匀分布：真人更常落在中位数附近，
    均匀分布的"每个间隔都等概率"反而不自然。
    """
    mid = (min_sec + max_sec) / 2
    time.sleep(random.triangular(min_sec, max_sec, mid))


def move_and_click(page, wx: float, wy: float, viewport: tuple[int, int]):
    """带弧线的鼠标移动 + 点击。

    直接跳到目标点再 click 是最典型的机器人特征。
    这里用一条带随机曲率的二次贝塞尔曲线，分步移动过去。
    """
    w, h = viewport
    sx, sy = _last_pos()
    # 终点加 1~2px 抖动：真人不可能每次精确落在同一像素
    tx = wx + random.uniform(-1.5, 1.5)
    ty = wy + random.uniform(-1.5, 1.5)
    tx = min(max(tx, 1), w - 2)
    ty = min(max(ty, 1), h - 2)

    dist = math.hypot(tx - sx, ty - sy)
    steps = max(8, min(34, int(dist / 22)))

    # 控制点在中点附近随机偏移，形成自然弧度
    cx = (sx + tx) / 2 + random.uniform(-dist * 0.12, dist * 0.12)
    cy = (sy + ty) / 2 + random.uniform(-dist * 0.12, dist * 0.12)

    for i in range(1, steps + 1):
        t = i / steps
        # ease-out：接近目标时减速，符合手的运动
        e = 1 - (1 - t) ** 2.2
        x = (1 - e) ** 2 * sx + 2 * (1 - e) * e * cx + e ** 2 * tx
        y = (1 - e) ** 2 * sy + 2 * (1 - e) * e * cy + e ** 2 * ty
        page.mouse_move(x, y)
        time.sleep(random.uniform(0.006, 0.022))

    time.sleep(random.uniform(0.08, 0.28))  # 落点后的微停顿，再按下
    page.click(tx, ty)
    _set_last_pos(tx, ty)


# --- 光标位置的进程内记忆 ------------------------------------------------
# 让下一段移动从上一次落点出发，而不是每次都从屏幕角落瞬移过去。
_last = {"x": 0.0, "y": 0.0}


def _last_pos() -> tuple[float, float]:
    return _last["x"], _last["y"]


def _set_last_pos(x: float, y: float):
    _last["x"], _last["y"] = x, y
