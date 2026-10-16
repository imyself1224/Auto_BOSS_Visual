"""视觉定位层：截图 → 叠加网格 → 问模型 → 拿到归一化坐标。

把「怎么让模型给准坐标」这件事全部收敛在这里，
上层 agent 只管调用并拿到 {'x': int, 'y': int} 这样的结果。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import overlay, prompts
from .deepseek import DeepSeekError, extract_json


class Vision:
    def __init__(self, client, cfg, store, shots_dir: Path, run_id: str, verbose: bool = True):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.shots_dir = shots_dir / run_id
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.verbose = verbose
        self.model = str(cfg.get("deepseek.vision_model", "deepseek-flash"))
        self._seq = 0
        self.shot_count = 0

    # ------------------------------------------------------------------
    def _save(self, png: bytes, tag: str) -> str:
        self._seq += 1
        name = f"{self._seq:03d}_{tag}.png"
        path = self.shots_dir / name
        path.write_bytes(png)
        self.shot_count += 1
        return str(path.relative_to(self.shots_dir.parent.parent))

    def _log(self, msg: str):
        if self.verbose:
            print(f"    [vision] {msg}")

    # ------------------------------------------------------------------
    def look(self, page, prompt: str, tag: str = "look", max_tokens: int = 1500) -> dict:
        """看一屏：注入全局网格 → 截图 → 问模型 → 返回解析后的 JSON。"""
        overlay.install(page)
        try:
            png = page.screenshot()
        finally:
            overlay.clear(page)     # 必须移除，否则会盖住真实页面挡住后续点击
        shot = self._save(png, tag)

        try:
            text = self.client.vision(
                prompt,
                [png],
                system=prompts.VISION_SYSTEM,
                model=self.model,
                max_tokens=max_tokens,
                json_mode=True,
            )
            data = extract_json(text)
        except DeepSeekError as exc:
            self.store.log("error", f"视觉调用失败（{tag}）：{exc}", shot)
            raise

        self.store.log("vision", f"{tag} → {json.dumps(data, ensure_ascii=False)[:500]}", shot)
        return data

    # ------------------------------------------------------------------
    def zoom(self, page, bbox_norm: list[float], prompt: str, tag: str = "zoom",
             pad_norm: float = 12.0, scale: float = 3.0, max_tokens: int = 900) -> dict:
        """两段式定位第二段：把候选区域裁出来放大，再问一次。

        bbox_norm 是全局归一化 [x1, y1, x2, y2]。
        返回值里的坐标同样是**全局**归一化值 —— 因为加密网格的刻度标注的就是全局值，
        所以不需要做任何反算，这也正是选这个方案的原因。
        """
        vw, vh = page.viewport()

        x1 = max(0.0, bbox_norm[0] - pad_norm)
        y1 = max(0.0, bbox_norm[1] - pad_norm)
        x2 = min(1000.0, bbox_norm[2] + pad_norm)
        y2 = min(1000.0, bbox_norm[3] + pad_norm)
        if x2 - x1 < 20 or y2 - y1 < 20:
            x1, y1, x2, y2 = 0.0, 0.0, 1000.0, 1000.0   # 区域太小就直接退化成全屏

        overlay.install_region(page, [x1, y1, x2, y2], divisions=10)
        try:
            png = page.screenshot_clip(
                x1 / 1000 * vw, y1 / 1000 * vh,
                (x2 - x1) / 1000 * vw, (y2 - y1) / 1000 * vh,
                scale=scale,
            )
        finally:
            overlay.clear(page)
        shot = self._save(png, tag)

        try:
            text = self.client.vision(
                prompts.ZOOM_PREFIX + prompt,
                [png],
                system=prompts.VISION_SYSTEM,
                model=self.model,
                max_tokens=max_tokens,
                json_mode=True,
            )
            data = extract_json(text)
        except DeepSeekError as exc:
            self.store.log("error", f"局部放大调用失败（{tag}）：{exc}", shot)
            raise

        self.store.log("vision", f"{tag}(zoom {[round(v) for v in (x1, y1, x2, y2)]}) → "
                                 f"{json.dumps(data, ensure_ascii=False)[:400]}", shot)
        # 把**实际**裁剪框带回去。注意不是调用方传进来的那个 ——
        # 这里还加过 pad_norm 并夹到 0-1000，换算局部坐标要用实际值。
        data["_bbox"] = [x1, y1, x2, y2]
        return data

    # ------------------------------------------------------------------
    def look_and_pick(self, page, prompt: str, tag: str, min_confidence: float = 0.55):
        """看一屏，返回 (模型原始返回 dict, 坐标或 None)。

        和 locate 的区别：**定位失败时也把原始返回给你**。
        筛选器那一步要用它 —— 点开的下拉对不对，只能靠模型报的
        `open_menu_for` 判断，而"下拉开错了"的典型症状恰恰是选项定位失败，
        这时候 locate 直接返回 None，把线索丢了。
        """
        data = self.look(page, prompt, tag=tag)
        return data, self._pick_click(data, min_confidence)

    def _zoom_pick_to_global(self, pick, bbox, tag: str = "") -> tuple[int, int]:
        """把局部放大图的返回坐标换算成全局归一化值。

        实测踩到的：模型在放大图里**有时报的是裁剪图内部的相对坐标**，
        完全无视网格上标着的全局刻度 —— 尽管 ZOOM_PREFIX 反复强调那是全局值。
        症状很隐蔽：它把 text_on_target 说对了，坐标却掉到裁剪框外面。
        真实一例：`zoom [313,117,477,281] → click {x:70, y:500}`，
        当成全局值用就会点到十万八千里之外（那次点到了左侧职位卡上，
        所以筛选下拉一个都没弹出来）。

        判据：落在框内 = 全局值，直接用；落在框外 = 按局部值换算回来。
        换算后仍越界就说明是垃圾，原样返回，交给上层的结果校验去挡。
        """
        if not bbox:
            return pick
        x1, y1, x2, y2 = (float(v) for v in bbox)
        nx, ny = int(pick[0]), int(pick[1])
        if x1 <= nx <= x2 and y1 <= ny <= y2:
            return nx, ny                    # 框内，当全局值用
        gx = x1 + nx / 1000.0 * (x2 - x1)
        gy = y1 + ny / 1000.0 * (y2 - y1)
        if 0 <= gx <= 1000 and 0 <= gy <= 1000:
            self._log(f"{tag}: 复核坐标 {pick} 在裁剪框外，"
                      f"按局部值换算 → ({gx:.0f}, {gy:.0f})")
            return int(round(gx)), int(round(gy))
        return nx, ny

    def locate(self, page, prompt: str, tag: str, refine: bool = True,
               min_confidence: float = 0.55,
               force_refine: bool = False,
               refine_pad: tuple[float, float] = (70.0, 70.0)) -> dict | None:
        """定位一个目标，必要时自动做第二段放大复核。

        返回 {"x": ..., "y": ..., "confidence": ..., "raw": {...}}，找不到返回 None。

        `force_refine=True`：即使置信度很高也做一次局部放大复核。
        给「一排挤在一起的小目标」用 —— 典型是 BOSS 的筛选栏，「求职类型」
        和「薪资待遇」中心只差半个按钮宽，模型会**自信地**点偏（conf ≥ 0.8），
        正常路径下就会跳过复核，把错坐标用出去。

        `refine_pad=(dx, dy)`：复核裁剪框的半宽/半高。默认正方形 ±70。
        横向一排的工具栏要传**又宽又矮**的值（如 (200, 45)）——
        否则正确的那个按钮会被裁掉半边，模型读不全文字就只能继续认错
        （实见：±70 的框把「求职类型」切得只剩「类型」两个字）。
        """
        first = self.look(page, prompt, tag=f"{tag}_1")
        pick = self._pick_click(first, min_confidence)
        if pick is None:
            return None

        conf = float(first.get("confidence") or 0)
        if not refine:
            return {"x": pick[0], "y": pick[1], "confidence": conf, "raw": first}

        # 第一段信心不高 → 做局部放大再问一次，比直接重试全屏有效得多
        if conf >= 0.8 and not force_refine:
            return {"x": pick[0], "y": pick[1], "confidence": conf, "raw": first}

        dx, dy = refine_pad
        try:
            second = self.zoom(
                page,
                [pick[0] - dx, pick[1] - dy, pick[0] + dx, pick[1] + dy],
                prompt,
                tag=f"{tag}_2",
            )
            pick2 = self._pick_click(second, min_confidence)
            if pick2 is not None:
                pick2 = self._zoom_pick_to_global(pick2, second.get("_bbox"), tag)
                conf2 = float(second.get("confidence") or 0)
                # 常规路径要求复核结果不低于第一段才采纳。
                # force_refine 时放宽：第一段是"自信地错"（conf 0.9+），
                # 若还要求 conf2 更高，正确的复核结果永远赢不了，等于白跑。
                if conf2 >= conf or (force_refine and conf2 >= min_confidence):
                    self._log(f"{tag}: 局部放大复核 {conf:.2f} → {conf2:.2f}")
                    return {"x": pick2[0], "y": pick2[1], "confidence": conf2, "raw": second}
        except DeepSeekError as exc:
            self._log(f"{tag}: 复核失败，沿用第一段结果（{exc}）")

        return {"x": pick[0], "y": pick[1], "confidence": conf, "raw": first}

    @staticmethod
    def _pick_click(data: dict, min_confidence: float) -> tuple[int, int] | None:
        """从模型返回里挑出可用的坐标，键名容忍几种常见写法。"""
        if data.get("found") is False:
            return None
        conf = float(data.get("confidence") or 0)
        if conf < min_confidence:
            return None

        for key in ("click", "point", "target", "center", "coordinates", "coord"):
            val = data.get(key)
            if isinstance(val, dict):
                x, y = val.get("x"), val.get("y")
            elif isinstance(val, (list, tuple)) and len(val) >= 2:
                x, y = val[0], val[1]
            else:
                continue
            if x is None or y is None:
                continue
            try:
                nx, ny = int(round(float(x))), int(round(float(y)))
            except (TypeError, ValueError):
                continue
            if 0 <= nx <= 1000 and 0 <= ny <= 1000:
                return nx, ny
        return None

    # ------------------------------------------------------------------
    def annotate(self, page, marks: list[dict], tag: str) -> str:
        """在图上打标记并存档 —— 用于事后复核 AI 到底点了哪儿。"""
        overlay.install(page)
        try:
            overlay.mark(page, marks)
            png = page.screenshot()
        finally:
            overlay.clear(page)
        return self._save(png, tag)
