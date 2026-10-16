"""DeepSeek API 客户端 —— 纯标准库实现，不依赖 openai / requests。

覆盖两类调用：
  * chat()   —— 纯文本，用来根据 JD + 个人背景生成招呼语
  * vision() —— 多模态，把截图交给 deepseek-flash 做视觉定位

踩过的坑（都已在代码里处理）：
  1. 非流式请求服务端会先返回若干**空行**做 keep-alive，不能假定首字节是 '{'。
  2. **图片只能出现在 user 消息里**，放进 system/assistant 会 400。
  3. response_format=json_object 时，prompt 里必须出现 "json" 字样，否则 400。
  4. 429/500/503 可重试；400/401/402/422 重试没意义。
"""
from __future__ import annotations

import base64
import json
import random
import time
import urllib.error
import urllib.request

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def extract_json(text: str) -> dict:
    """从模型回复里抠出 JSON 对象。

    模型偶尔会裹上 ```json 围栏或在前后加客套话，这里全部容错。
    用括号配平扫描而不是正则，避免嵌套对象被截断。
    """
    if not text:
        raise DeepSeekError("模型返回了空内容")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        # 去掉首行围栏与结尾围栏
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 括号配平扫描：找到第一个完整的 {...}
    start = cleaned.find("{")
    if start == -1:
        raise DeepSeekError(f"回复里找不到 JSON 对象：{text[:300]}")
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise DeepSeekError(f"JSON 解析失败：{exc}") from exc
    raise DeepSeekError(f"JSON 括号不闭合：{text[:300]}")


class DeepSeek:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout: int = 120,
        max_retries: int = 4,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.call_count = 0
        self.total_tokens = 0

    # ------------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                # 带上 UA，某些网关会对空 UA 直接 403
                "User-Agent": "auto-boss/1.0",
            },
            method="POST",
        )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                # 坑 1：前面可能有 keep-alive 空行
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    raise DeepSeekError("服务端返回空响应体")
                result = json.loads(text)
                self.call_count += 1
                usage = result.get("usage") or {}
                self.total_tokens += usage.get("total_tokens", 0)
                return result

            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code in RETRYABLE_STATUS and attempt < self.max_retries - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    last_exc = exc
                    continue
                raise DeepSeekError(
                    self._friendly(exc.code, body), status=exc.code, body=body
                ) from exc

            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue

        raise DeepSeekError(f"请求失败，已重试 {self.max_retries} 次：{last_exc}")

    @staticmethod
    def _friendly(code: int, body: str) -> str:
        hints = {
            400: "请求格式错误",
            401: "API key 无效",
            402: "账户余额不足",
            422: "参数不合法",
            429: "触发并发限流",
            500: "服务端错误",
            503: "服务端过载",
        }
        return f"DeepSeek 返回 {code}（{hints.get(code, '未知')}）：{body[:400]}"

    # ------------------------------------------------------------------
    def chat(
        self,
        prompt: str,
        system: str | None = None,
        model: str = "deepseek-flash",
        max_tokens: int = 512,
        temperature: float = 1.0,
        json_mode: bool = False,
        thinking: bool = False,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        # 思维模式下 temperature 会被忽略，关了才有效
        if not thinking:
            payload["temperature"] = temperature

        if json_mode:
            if "json" not in (prompt + (system or "")).lower():
                raise DeepSeekError("开启 json_mode 时 prompt 里必须出现 'json' 字样")
            payload["response_format"] = {"type": "json_object"}

        result = self._post(payload)
        return self._content(result)

    def vision(
        self,
        prompt: str,
        images: list[bytes],
        system: str | None = None,
        model: str = "deepseek-flash",
        max_tokens: int = 1024,
        json_mode: bool = False,
        detail: str | None = None,
        thinking: bool = False,
    ) -> str:
        """images: PNG 字节列表。

        注意坑 2：图片块只能放在 user 消息里。
        所以 system 走 system 消息，图片统一挂在最后一条 user 消息上。
        """
        if not images:
            raise DeepSeekError("vision() 至少需要一张图片")

        content: list[dict] = [{"type": "text", "text": prompt}]
        for png in images:
            b64 = base64.b64encode(png).decode("ascii")
            url_obj: dict = {"url": f"data:image/png;base64,{b64}"}
            if detail:
                url_obj["detail"] = detail
            content.append({"type": "image_url", "image_url": url_obj})

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if json_mode:
            if "json" not in (prompt + (system or "")).lower():
                raise DeepSeekError("开启 json_mode 时 prompt 里必须出现 'json' 字样")
            payload["response_format"] = {"type": "json_object"}

        result = self._post(payload)
        return self._content(result)

    @staticmethod
    def _content(result: dict) -> str:
        try:
            choice = result["choices"][0]
        except (KeyError, IndexError) as exc:
            raise DeepSeekError(f"响应结构异常：{json.dumps(result, ensure_ascii=False)[:400]}") from exc

        finish = choice.get("finish_reason")
        text = (choice.get("message") or {}).get("content") or ""
        if not text.strip():
            if finish == "length":
                raise DeepSeekError("输出被 max_tokens 截断，请调大 max_tokens")
            raise DeepSeekError(f"模型返回空内容（finish_reason={finish}）")
        return text
