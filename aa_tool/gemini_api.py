"""Google Gemini API 翻譯後端。

與 :class:`aa_tool.gemini_web.GeminiWebSession` 介面對齊（``open`` / ``translate``
/ ``close`` 與同一組例外），讓 ``aa_auto_translate`` 的協調器能在「瀏覽器操控」
與「API」兩種後端間直接抽換。

多把 API 金鑰以 round-robin 輪換：每送一次請求就換下一把，把用量平均分散；
遇到 429（額度上限）會自動換下一把重試，整輪都滿才丟 GeminiQuotaExceeded。

只用標準庫 urllib，不引入第三方 SDK。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from .gemini_web import GeminiQuotaExceeded, GeminiWebError

# 可選模型（依使用者指定）。下拉選單與此清單一致。
API_MODELS = [
    "gemini-2.5-pro",
    "gemini-3.1-pro",
    "gemini-3-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]
DEFAULT_API_MODEL = API_MODELS[0]

_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
             "models/{model}:generateContent")
_TIMEOUT = 300


class GeminiApiSession:
    """以 Google Gemini API 進行翻譯，多金鑰輪換。"""

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        system_prompt: str = "",
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._keys = [k.strip() for k in (api_keys or []) if k.strip()]
        self._model = (model or DEFAULT_API_MODEL).strip()
        self._system_prompt = system_prompt or ""
        self._log = log or (lambda m: print(f"[gemini_api] {m}"))
        self._idx = 0  # round-robin 游標

    # ── 生命週期（對齊 GeminiWebSession 介面） ──

    def open(self, *_a, **_kw) -> None:
        if not self._keys:
            raise GeminiWebError("API 模式但未設定任何 API 金鑰")
        self._log(f"API 後端就緒：模型 {self._model}，"
                  f"共 {len(self._keys)} 把金鑰輪換")

    def close(self) -> None:
        pass

    def __enter__(self) -> "GeminiApiSession":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── 翻譯 ──

    def translate(self, prompt_text: str) -> str:
        """送一次請求並回傳譯文。金鑰輪換 + 429 自動換把重試。"""
        if not prompt_text.strip():
            return ""
        if not self._keys:
            raise GeminiWebError("API 模式但未設定任何 API 金鑰")
        last_quota: Exception | None = None
        for _ in range(len(self._keys)):
            key_no = (self._idx % len(self._keys)) + 1
            key = self._keys[self._idx % len(self._keys)]
            self._idx += 1
            self._log(f"API 送出（金鑰 #{key_no}/{len(self._keys)}，模型 {self._model}）")
            try:
                return self._request(key, prompt_text)
            except GeminiQuotaExceeded as e:
                last_quota = e
                self._log(f"  金鑰 #{key_no} 額度滿，換下一把…")
                continue
        raise GeminiQuotaExceeded(
            f"所有 {len(self._keys)} 把金鑰皆達額度上限（{last_quota}）")

    def _request(self, key: str, prompt_text: str) -> str:
        body: dict = {"contents": [{"parts": [{"text": prompt_text}]}]}
        if self._system_prompt.strip():
            body["systemInstruction"] = {
                "parts": [{"text": self._system_prompt}]}
        url = _ENDPOINT.format(model=self._model) + f"?key={key}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise GeminiQuotaExceeded("HTTP 429（額度上限）") from e
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise GeminiWebError(f"API 錯誤 HTTP {e.code}：{detail}") from e
        except urllib.error.URLError as e:
            raise GeminiWebError(f"API 連線失敗：{e}") from e
        return self._extract_text(payload)

    @staticmethod
    def _extract_text(payload: dict) -> str:
        cands = payload.get("candidates", [])
        if not cands:
            fb = payload.get("promptFeedback", {})
            raise GeminiWebError(f"API 無回應內容（可能被安全過濾：{fb}）")
        parts = cands[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise GeminiWebError("API 回應為空")
        return text
