"""页面叠加层：网格标尺 + 标记点。

为什么要在**页面里**画网格，而不是用图像库在截图后处理：

1. 零依赖 —— 不用装 Pillow。
2. **坐标准确**。canvas 固定定位在视口 (0,0)，尺寸取 window.innerWidth/Height，
   所以网格和截图像素是同一套坐标系，不存在缩放/DPR 换算误差。
3. 刻度直接标注**归一化值 0–1000**，模型读出来的数就是要用的数。

覆盖层带 `pointer-events:none`，且每次截图前注入、截完立即移除，
不会干扰点击，也不会长期留在页面里。
"""
from __future__ import annotations

OVERLAY_ID = "__autoboss_overlay__"

# 注入网格。刻度用归一化单位（0-1000）标注，方便模型直接读数。
_GRID_JS = """
(() => {
  const ID = %(id)r;
  const prev = document.getElementById(ID);
  if (prev) prev.remove();

  const w = window.innerWidth, h = window.innerHeight;
  const c = document.createElement('canvas');
  c.id = ID;
  c.width = w; c.height = h;
  c.style.cssText = 'position:fixed;left:0;top:0;width:' + w + 'px;height:' + h +
                    'px;z-index:2147483647;pointer-events:none;';
  const g = c.getContext('2d');
  const N = 1000;

  // 竖向线 + 顶部读数
  g.textBaseline = 'top';
  g.font = 'bold 12px monospace';
  for (let i = 0; i <= 10; i++) {
    const n = i * 100;
    const x = Math.round(n / N * w);
    g.strokeStyle = (i === 5) ? 'rgba(255,45,45,0.75)' : 'rgba(0,110,255,0.42)';
    g.lineWidth = (i === 5) ? 1.6 : 1;
    g.beginPath(); g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, h); g.stroke();
    g.fillStyle = 'rgba(255,45,45,0.95)';
    g.strokeStyle = 'rgba(255,255,255,0.9)';
    g.lineWidth = 3;
    g.strokeText(n, x + 3, 3);
    g.fillText(n, x + 3, 3);
  }

  // 横向线 + 左侧读数
  for (let i = 0; i <= 10; i++) {
    const n = i * 100;
    const y = Math.round(n / N * h);
    g.strokeStyle = (i === 5) ? 'rgba(255,45,45,0.75)' : 'rgba(0,110,255,0.42)';
    g.lineWidth = (i === 5) ? 1.6 : 1;
    g.beginPath(); g.moveTo(0, y + 0.5); g.lineTo(w, y + 0.5); g.stroke();
    g.fillStyle = 'rgba(255,45,45,0.95)';
    g.strokeStyle = 'rgba(255,255,255,0.9)';
    g.lineWidth = 3;
    g.strokeText(n, 3, y + 3);
    g.fillText(n, 3, y + 3);
  }

  document.documentElement.appendChild(c);
  return true;
})()
"""

_CLEAR_JS = """
(() => {
  const el = document.getElementById(%(id)r);
  if (el) el.remove();
  return true;
})()
"""

# 在已注入的覆盖层上画标记（十字准星 + 序号 + 归一化坐标文字）。
# 用于复核 AI 的判断，以及存档截图时一眼看出点了哪儿。
_MARK_JS = """
(() => {
  const el = document.getElementById(%(id)r);
  if (!el) return false;
  const w = window.innerWidth, h = window.innerHeight;
  const g = el.getContext('2d');
  g.lineWidth = 2.5;
  for (const m of %(marks)s) {
    const x = m.x / 1000 * w, y = m.y / 1000 * h;
    const color = m.color || '#ff2d2d';
    g.strokeStyle = color;
    g.beginPath();
    g.moveTo(x - 18, y); g.lineTo(x + 18, y);
    g.moveTo(x, y - 18); g.lineTo(x, y + 18);
    g.stroke();
    g.beginPath(); g.arc(x, y, 13, 0, Math.PI * 2); g.stroke();
    if (m.label) {
      g.font = 'bold 13px monospace';
      g.fillStyle = color;
      g.strokeStyle = 'rgba(255,255,255,0.95)';
      g.lineWidth = 3.5;
      g.textBaseline = 'bottom';
      g.strokeText(m.label, x + 16, y - 14);
      g.fillText(m.label, x + 16, y - 14);
    }
  }
  return true;
})()
"""


# 在某个区域内画**加密网格**，区域外压暗。
#
# 这是两段式定位的第二段：全屏截图会被服务端缩到约 1300px 等效尺寸，
# 一个小按钮在上面只剩几个像素，模型没法精确读数。
# 把目标区域单独裁出来放大，并在里面画上更细的网格 ——
# 关键是刻度依然标注**全局归一化值**，所以模型报出的坐标不需要任何反算。
_REGION_JS = """
(() => {
  const ID = %(id)r;
  const prev = document.getElementById(ID);
  if (prev) prev.remove();

  const w = window.innerWidth, h = window.innerHeight;
  const N = 1000;
  const b = %(bbox)s;                      // 归一化 [x1, y1, x2, y2]
  const x1 = b[0] / N * w, y1 = b[1] / N * h;
  const x2 = b[2] / N * w, y2 = b[3] / N * h;

  const c = document.createElement('canvas');
  c.id = ID;
  c.width = w; c.height = h;
  c.style.cssText = 'position:fixed;left:0;top:0;width:' + w + 'px;height:' + h +
                    'px;z-index:2147483647;pointer-events:none;';
  const g = c.getContext('2d');

  // 区域外压暗，把注意力收拢到目标区域
  g.fillStyle = 'rgba(0,0,0,0.55)';
  g.fillRect(0, 0, w, y1);
  g.fillRect(0, y2, w, h - y2);
  g.fillRect(0, y1, x1, y2 - y1);
  g.fillRect(x2, y1, w - x2, y2 - y1);

  // 区域边框
  g.strokeStyle = 'rgba(255,45,45,0.95)';
  g.lineWidth = 2;
  g.strokeRect(x1 + 0.5, y1 + 0.5, x2 - x1, y2 - y1);

  // 区域内的加密刻度线，标签是**全局**归一化值
  const div = %(div)s;
  g.font = 'bold 11px monospace';
  g.textBaseline = 'top';
  for (let i = 1; i < div; i++) {
    const gn = b[0] + (b[2] - b[0]) * i / div;
    const x = Math.round(gn / N * w);
    g.strokeStyle = 'rgba(0,200,255,0.75)';
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(x + 0.5, y1); g.lineTo(x + 0.5, y2); g.stroke();
    g.fillStyle = 'rgba(0,235,255,1)';
    g.strokeStyle = 'rgba(0,0,0,0.85)';
    g.lineWidth = 3;
    g.strokeText(Math.round(gn), x + 2, y1 + 2);
    g.fillText(Math.round(gn), x + 2, y1 + 2);
  }
  for (let i = 1; i < div; i++) {
    const gn = b[1] + (b[3] - b[1]) * i / div;
    const y = Math.round(gn / N * h);
    g.strokeStyle = 'rgba(0,200,255,0.75)';
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(x1, y + 0.5); g.lineTo(x2, y + 0.5); g.stroke();
    g.fillStyle = 'rgba(0,235,255,1)';
    g.strokeStyle = 'rgba(0,0,0,0.85)';
    g.lineWidth = 3;
    g.strokeText(Math.round(gn), x1 + 2, y + 2);
    g.fillText(Math.round(gn), x1 + 2, y + 2);
  }

  document.documentElement.appendChild(c);
  return true;
})()
"""


def install(page) -> None:
    """注入全局网格覆盖层。"""
    page.evaluate(_GRID_JS % {"id": OVERLAY_ID})


def install_region(page, bbox_norm: list[float], divisions: int = 10) -> None:
    """在指定归一化区域内注入加密网格，区域外压暗。"""
    import json
    page.evaluate(_REGION_JS % {
        "id": OVERLAY_ID,
        "bbox": json.dumps([round(float(v), 1) for v in bbox_norm]),
        "div": max(4, min(20, int(divisions))),
    })


def clear(page) -> None:
    """移除覆盖层。截图后一定要调用，否则会挡住真实页面。"""
    try:
        page.evaluate(_CLEAR_JS % {"id": OVERLAY_ID})
    except Exception:
        # 页面导航走了会抛异常，这里无所谓
        pass


def mark(page, marks: list[dict]) -> None:
    """在覆盖层上打标记。marks: [{"x": 归一化, "y": 归一化, "label": str}]"""
    import json
    page.evaluate(_MARK_JS % {"id": OVERLAY_ID, "marks": json.dumps(marks, ensure_ascii=False)})


def norm_to_css(nx: float, ny: float, viewport: tuple[int, int]) -> tuple[float, float]:
    """归一化坐标 (0-1000) → 视口 CSS 像素坐标。"""
    w, h = viewport
    return nx / 1000.0 * w, ny / 1000.0 * h


def css_to_norm(x: float, y: float, viewport: tuple[int, int]) -> tuple[float, float]:
    w, h = viewport
    return x / w * 1000.0, y / h * 1000.0
