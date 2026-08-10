"""OpenAI／Anthropic 相容的 API 翻譯後端。

與 :class:`aa_tool.gemini_api.GeminiApiSession` 及
:class:`aa_tool.gemini_web.GeminiWebSession` 介面對齊（``open`` / ``translate``
/ ``close`` / ``start_new_session`` 與同一組例外），讓 ``aa_auto_translate`` 的
協調器能在多種後端間直接抽換。

一支類別 :class:`ChatApiSession` 以 ``scheme`` 切換兩種請求／回應格式：

* ``"openai"``    → ``POST {base_url}/chat/completions``，``Authorization: Bearer``，
  body ``{model, messages}``，回應取 ``choices[0].message.content``。
  涵蓋 **OpenAI(GPT)／DeepSeek／自定義（OpenAI 相容端點）**——三者只差
  ``base_url`` 與 ``model``。
* ``"anthropic"`` → ``POST {base_url}/messages``，``x-api-key`` + ``anthropic-version``，
  body ``{model, max_tokens, system?, messages}``，回應取 ``content[].text``。
  對應 **Claude**。

多把金鑰以 round-robin 輪換；429 依 ``Retry-After`` 設定短冷卻後換下一把，
全部冷卻中則等待或丟 :class:`GeminiQuotaExceeded`；5xx 視為伺服器暫時性錯誤，
等待後重試整輪。與 Gemini 後端不同，這裡不處理「每日配額(RPD)固定重置時刻」
——OpenAI／Anthropic 的額度多為滾動視窗，交由伺服器的 ``Retry-After`` 主導。

只用標準庫 urllib，不引入任何第三方 SDK。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from .gemini_web import GeminiAborted, GeminiQuotaExceeded, GeminiWebError

# ── 供應商註冊表 ──
# scheme：請求／回應格式；base_url：預設端點（custom 由使用者填）；
# models：下拉建議值（可自行輸入其他 model id，故清單非窮舉）。
# 註：gemini 走 aa_tool.gemini_api.GeminiApiSession（另一組配額邏輯），此處僅列於
# API_PROVIDERS 供 UI 一致呈現，實際建立 session 由協調器依 scheme 分派。
API_PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Gemini (Google)",
        "scheme": "gemini",
        "base_url": "",
        "models": [],  # 見 aa_tool.gemini_api.API_MODELS
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "scheme": "openai",
        "base_url": "https://api.openai.com/v1",
        # GPT-5.6 家族：terra（智慧/成本平衡，翻譯量大預設）、sol（旗艦）、luna（省成本）
        "models": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"],
    },
    "claude": {
        "label": "Claude (Anthropic)",
        "scheme": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "scheme": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
    },
    "custom": {
        "label": "自定義 (OpenAI 相容)",
        "scheme": "openai",
        "base_url": "",
        "models": [],
    },
}

_TIMEOUT = 300
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_MAX_TOKENS = 32000  # Anthropic 必填的輸出上限（避免長章節被截斷）

# 額度冷卻參數（與 gemini_api 對齊語意，但無 RPD 每日重置）
_RPM_COOLDOWN = 60.0
_ALL_COOLDOWN_MAX_WAIT = 120.0
_MAX_WAIT_ROUNDS = 3

# 伺服器暫時性錯誤（5xx）：等待後重試整輪，不視為金鑰問題、不進冷卻
_TRANSIENT_HTTP = {500, 502, 503, 504}
_BUSY_RETRY_WAIT = 120.0
_MAX_BUSY_RETRIES = 5


class _ServerBusy(GeminiWebError):
    """伺服器暫時無法服務（HTTP 5xx）——可等待後重試（僅內部使用）。"""


def provider_scheme(provider: str) -> str:
    """回傳供應商的請求格式（gemini／openai／anthropic）；未知則視為 openai。"""
    return API_PROVIDERS.get(provider, {}).get("scheme", "openai")


def default_model(provider: str) -> str:
    """回傳供應商的建議預設模型（清單第一項）；無則空字串。"""
    models = API_PROVIDERS.get(provider, {}).get("models", [])
    return models[0] if models else ""


class ChatApiSession:
    """以 OpenAI／Anthropic 相容 API 進行翻譯，多金鑰輪換。"""

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        scheme: str = "openai",
        base_url: str = "",
        system_prompt: str = "",
        log: Callable[[str], None] | None = None,
        stop_event=None,
    ) -> None:
        self._keys = [k.strip() for k in (api_keys or []) if k.strip()]
        self._model = (model or "").strip()
        self._scheme = scheme if scheme in ("openai", "anthropic") else "openai"
        self._base_url = (base_url or "").strip().rstrip("/")
        self._system_prompt = system_prompt or ""
        self._log = log or (lambda m: print(f"[openai_api] {m}"))
        self._stop_event = stop_event
        self._idx = 0  # round-robin 游標
        self._cooldown_until = [0.0] * len(self._keys)

    # ── 生命週期（對齊其他後端介面） ──

    def open(self, *_a, **_kw) -> None:
        if not self._keys:
            raise GeminiWebError("API 模式但未設定任何 API 金鑰")
        if not self._base_url:
            raise GeminiWebError("未設定 API 端點（base_url）")
        if not self._model:
            raise GeminiWebError("未設定 API 模型")
        self._log(f"API 後端就緒：{self._scheme} @ {self._base_url}，"
                  f"模型 {self._model}，共 {len(self._keys)} 把金鑰輪換")

    def close(self) -> None:
        pass

    def start_new_session(self) -> None:
        """API 無對話狀態（每次請求獨立、金鑰已輪換），無需重開；保留同名介面。"""
        return

    def __enter__(self) -> "ChatApiSession":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── 翻譯 ──

    def translate(self, prompt_text: str) -> str:
        """送一次請求並回傳譯文。

        金鑰輪換並跳過冷卻中的金鑰；遇 429 依 Retry-After 冷卻後換下一把。
        所有金鑰皆冷卻時：最近解除在可接受範圍→等待後重試；否則丟
        :class:`GeminiQuotaExceeded`。5xx 由伺服器暫時性錯誤流程等待重試。
        """
        if not prompt_text.strip():
            return ""
        if not self._keys:
            raise GeminiWebError("API 模式但未設定任何 API 金鑰")
        n = len(self._keys)
        last_quota: Exception | None = None
        waits = 0
        busy_retries = 0
        while True:
            busy_wait = False
            for _ in range(n):
                i = self._idx % n
                self._idx += 1
                if self._cooldown_until[i] > self._now():
                    continue  # 冷卻中，跳過
                self._log(f"API 送出（金鑰 #{i + 1}/{n}，模型 {self._model}）")
                try:
                    return self._request(self._keys[i], prompt_text)
                except GeminiQuotaExceeded as e:
                    last_quota = e
                    cd = getattr(e, "retry_after", None) or _RPM_COOLDOWN
                    self._cooldown_until[i] = self._now() + cd
                    self._log(f"  金鑰 #{i + 1} 達速率上限(429)，"
                              f"冷卻約 {int(cd)}s，換下一把…")
                    continue
                except _ServerBusy as e:
                    busy_retries += 1
                    if busy_retries > _MAX_BUSY_RETRIES:
                        raise GeminiWebError(
                            f"伺服器暫時性錯誤重試 {_MAX_BUSY_RETRIES} 次仍失敗，"
                            f"跳過此話：{e}") from e
                    self._log(
                        f"  伺服器忙碌/暫時無法服務（{e}）→ {int(_BUSY_RETRY_WAIT)}s "
                        f"後重試（第 {busy_retries}/{_MAX_BUSY_RETRIES} 次）…")
                    if not self._sleep_with_stop(_BUSY_RETRY_WAIT):
                        raise GeminiAborted("等待伺服器恢復(5xx)時收到停止指令")
                    busy_wait = True
                    break
            if busy_wait:
                continue
            # 這一圈沒有任何金鑰可用（全部冷卻中）
            soonest = min(self._cooldown_until)
            wait = soonest - self._now()
            if wait > _ALL_COOLDOWN_MAX_WAIT or waits >= _MAX_WAIT_ROUNDS:
                raise GeminiQuotaExceeded(
                    f"所有 {n} 把金鑰反覆達速率上限，終止（{last_quota}）")
            wait = max(1.0, wait) + 0.5
            self._log(f"  所有金鑰暫時冷卻中，等待約 {int(wait)}s 後重試…")
            if not self._sleep_with_stop(wait):
                raise GeminiAborted("等待金鑰冷卻時收到停止指令")
            waits += 1

    # ── 請求組裝／送出 ──

    def _request(self, key: str, prompt_text: str) -> str:
        url, headers, body = self._build_request(key, prompt_text)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 429:
                exc = GeminiQuotaExceeded("HTTP 429（額度/速率上限）")
                exc.retry_after = self._parse_retry_after(e)  # type: ignore[attr-defined]
                raise exc from e
            if e.code in _TRANSIENT_HTTP:
                raise _ServerBusy(
                    f"HTTP {e.code}（伺服器忙碌/暫時無法服務）：{err_body[:200]}") from e
            raise GeminiWebError(f"API 錯誤 HTTP {e.code}：{err_body[:300]}") from e
        except urllib.error.URLError as e:
            raise GeminiWebError(f"API 連線失敗：{e}") from e
        return self._extract_text(payload)

    def _build_request(self, key: str, prompt_text: str) -> tuple[str, dict, dict]:
        """依 scheme 組出 (url, headers, body)。"""
        if self._scheme == "anthropic":
            url = f"{self._base_url}/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": key,
                "anthropic-version": _ANTHROPIC_VERSION,
            }
            body: dict = {
                "model": self._model,
                "max_tokens": _ANTHROPIC_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt_text}],
            }
            if self._system_prompt.strip():
                body["system"] = self._system_prompt
            return url, headers, body
        # openai（含 DeepSeek／自定義）
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
        messages = []
        if self._system_prompt.strip():
            messages.append({"role": "system", "content": self._system_prompt})
        messages.append({"role": "user", "content": prompt_text})
        return url, headers, {"model": self._model, "messages": messages}

    def _extract_text(self, payload: dict) -> str:
        """依 scheme 從回應取出譯文；取不到／被拒則丟 GeminiWebError。"""
        if self._scheme == "anthropic":
            blocks = payload.get("content", []) or []
            text = "".join(
                b.get("text", "") for b in blocks
                if isinstance(b, dict) and b.get("type", "text") == "text").strip()
            if not text:
                stop = payload.get("stop_reason", "")
                raise GeminiWebError(f"API 回應為空（stop_reason={stop}）")
            return text
        # openai
        choices = payload.get("choices", []) or []
        if not choices:
            raise GeminiWebError(f"API 無回應內容：{str(payload)[:200]}")
        msg = choices[0].get("message", {}) or {}
        text = (msg.get("content") or "").strip()
        if not text:
            finish = choices[0].get("finish_reason", "")
            raise GeminiWebError(f"API 回應為空（finish_reason={finish}）")
        return text

    @staticmethod
    def _parse_retry_after(err: urllib.error.HTTPError) -> float | None:
        """從 429 的 ``Retry-After`` 標頭抽秒數（純數字）；無則 None。"""
        try:
            ra = err.headers.get("Retry-After") if err.headers else None
            if ra and ra.strip().replace(".", "", 1).isdigit():
                return float(ra.strip())
        except Exception:
            pass
        return None

    # ── 輔助 ──

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _sleep_with_stop(self, seconds: float) -> bool:
        """睡 ``seconds`` 秒，期間每 0.5s 檢查 stop_event。回傳 True=睡滿、False=被停止。"""
        if self._stop_event is None:
            time.sleep(max(0.0, seconds))
            return True
        end = self._now() + seconds
        while True:
            remaining = end - self._now()
            if remaining <= 0:
                return True
            if self._stop_event.is_set():
                return False
            time.sleep(min(0.5, remaining))
