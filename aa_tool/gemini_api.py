"""Google Gemini API 翻譯後端。

與 :class:`aa_tool.gemini_web.GeminiWebSession` 介面對齊（``open`` / ``translate``
/ ``close`` 與同一組例外），讓 ``aa_auto_translate`` 的協調器能在「瀏覽器操控」
與「API」兩種後端間直接抽換。

多把 API 金鑰以 round-robin 輪換：每送一次請求就換下一把，把用量平均分散。

額度（429）處理採「冷卻 + RPD 偵測」：
  * 某把金鑰回 429 → 解析回應分辨「每分鐘速率上限(RPM)」與「每日配額(RPD)」，
    為該金鑰設定一段「冷卻時間」並換下一把；冷卻中的金鑰在輪換時跳過，
    避免每個 chunk 都重複試探已用盡的金鑰。
      - RPM：短冷卻（優先用伺服器建議的 retryDelay／Retry-After，否則 60 秒），
        稍後自動歸隊。
      - RPD：冷卻到下一個每日重置時刻（GMT+8 每天 15:00），log 顯示重置時間而非倒數；
        並把該金鑰的重置時間持久化（以金鑰指紋為索引，不存明文金鑰）。下次開啟（新
        session）載入時，已過重置時間者自動視為可用、未過者沿用剩餘冷卻。
  * 當所有金鑰都在冷卻中：若最近解除時間在可接受範圍內（多為 RPM）→ stop-aware
    等待後重試；若是長冷卻（RPD）或反覆全滿 → 丟 GeminiQuotaExceeded（協調器中止整批）。

只用標準庫 urllib，不引入第三方 SDK。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from .gemini_web import GeminiAborted, GeminiQuotaExceeded, GeminiWebError

# 可選模型（依使用者指定）。下拉選單與此清單一致。
API_MODELS = [
    "gemini-2.5-pro",
    "gemini-3.1-pro",
    "gemini-3-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]
DEFAULT_API_MODEL = API_MODELS[0]

# 特定模型的預設 thinking level。對應官方 SDK 的
# ThinkingConfig(thinking_level=...)；REST 對映 generationConfig.thinkingConfig.thinkingLevel。
# 3.5 Flash 預設 high（key 一律小寫比對）。
_DEFAULT_THINKING_LEVEL = {
    "gemini-3.5-flash": "high",
}

_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
             "models/{model}:generateContent")
_TIMEOUT = 300

# 額度冷卻參數
_RPM_COOLDOWN = 60.0          # 每分鐘速率上限的預設冷卻（無伺服器建議時）
# 所有金鑰皆冷卻時，最近解除時間若 ≤ 此秒數就等待後重試（多為 RPM）；超過則終止
_ALL_COOLDOWN_MAX_WAIT = 120.0
_MAX_WAIT_ROUNDS = 3          # 「全部冷卻→等待→重試」最多幾輪，避免反覆 ping-pong

# 伺服器暫時性錯誤（5xx，如 503 高負載）：等待後重試，不視為金鑰問題、不進冷卻
_TRANSIENT_HTTP = {500, 502, 503, 504}
_BUSY_RETRY_WAIT = 120.0     # 503 等暫時性錯誤的重試間隔（2 分鐘）
_MAX_BUSY_RETRIES = 5        # 暫時性錯誤最多重試幾次，仍失敗才跳過該話

# 從 retryDelay（"30s" / "1.5s"）抽秒數
_DURATION_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)

# 每日配額（RPD）重置時間：GMT+8 每天 15:00。冷卻長度＝距下一個重置時刻。
_RESET_TZ = timezone(timedelta(hours=8))
_RESET_HOUR = 15
# RPD 冷卻狀態持久化檔（只存金鑰雜湊→重置時間，不存明文金鑰）
_QUOTA_STATE_FILENAME = "aa_api_quota.json"


def _next_rpd_reset(now: datetime | None = None) -> datetime:
    """回傳下一個 RPD 重置時刻（GMT+8 15:00）。now 之後最近的那一個。"""
    now = (now or datetime.now(_RESET_TZ)).astimezone(_RESET_TZ)
    reset = now.replace(hour=_RESET_HOUR, minute=0, second=0, microsecond=0)
    if now >= reset:
        reset += timedelta(days=1)
    return reset


def _key_fingerprint(key: str) -> str:
    """金鑰指紋（sha256 前 16 碼），作為持久化索引；不外洩明文金鑰。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class GeminiServerBusy(GeminiWebError):
    """伺服器暫時無法服務（HTTP 5xx，如 503 高負載）——可等待後重試。"""


class GeminiApiSession:
    """以 Google Gemini API 進行翻譯，多金鑰輪換。"""

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        system_prompt: str = "",
        log: Callable[[str], None] | None = None,
        stop_event=None,
        base_dir: str | None = None,
    ) -> None:
        self._keys = [k.strip() for k in (api_keys or []) if k.strip()]
        self._model = (model or DEFAULT_API_MODEL).strip()
        self._system_prompt = system_prompt or ""
        self._log = log or (lambda m: print(f"[gemini_api] {m}"))
        self._stop_event = stop_event
        self._base_dir = base_dir
        self._idx = 0  # round-robin 游標
        # 每把金鑰的「冷卻到期」單調時鐘值（0 = 可用）；輪換時跳過未到期者
        self._cooldown_until = [0.0] * len(self._keys)
        # 每把金鑰的 RPD（每日配額）重置時刻（datetime，None=非 RPD 冷卻）；
        # 供 log 顯示與跨程式持久化用
        self._rpd_reset: list[datetime | None] = [None] * len(self._keys)

    # ── 生命週期（對齊 GeminiWebSession 介面） ──

    def open(self, *_a, **_kw) -> None:
        if not self._keys:
            raise GeminiWebError("API 模式但未設定任何 API 金鑰")
        self._load_quota_state()  # 載入上次未過重置時間的 RPD 冷卻；過期者自動清除
        self._log(f"API 後端就緒：模型 {self._model}，"
                  f"共 {len(self._keys)} 把金鑰輪換")

    def close(self) -> None:
        pass

    def start_new_session(self) -> None:
        """API 無對話狀態（每次請求獨立、金鑰已輪換），無需重開；保留同名介面。"""
        return

    def __enter__(self) -> "GeminiApiSession":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── 翻譯 ──

    def translate(self, prompt_text: str) -> str:
        """送一次請求並回傳譯文。

        金鑰輪換並跳過冷卻中的金鑰；遇 429 依 RPM/RPD 設定冷卻後換下一把。
        所有金鑰皆冷卻時：最近解除在可接受範圍（多為 RPM）→ 等待後重試；
        否則（RPD／反覆全滿）→ 丟 GeminiQuotaExceeded。
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
            # 繞一圈，只試「目前未冷卻」的金鑰
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
                    cooldown, reset_dt = self._cooldown_for(e)
                    self._cooldown_until[i] = self._now() + cooldown
                    if reset_dt is not None:
                        # RPD（每日配額）：顯示重置時間、持久化，本次執行不再用此金鑰
                        self._rpd_reset[i] = reset_dt
                        self._save_quota_state()
                        self._log(
                            f"  金鑰 #{i + 1} 已達每日配額(RPD)，額度將於 "
                            f"{reset_dt:%m/%d %H:%M}（GMT+8）重置，換下一把…")
                    else:
                        self._log(
                            f"  金鑰 #{i + 1} 達速率上限(RPM)，"
                            f"冷卻約 {int(cooldown)}s，換下一把…")
                    continue
                except GeminiServerBusy as e:
                    # 503 等暫時性錯誤：伺服器高負載，非金鑰問題、不進冷卻；
                    # 等待後重試整輪，超過上限才放棄（往外拋 → 協調器跳過該話）。
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
                    break  # 跳出 for，重新繞一圈重試
            if busy_wait:
                continue  # 已等待暫時性錯誤，重試整輪（不進入「全部冷卻」判斷）
            # 這一圈沒有任何金鑰可用（全部冷卻中）
            soonest = min(self._cooldown_until)
            wait = soonest - self._now()
            if wait > _ALL_COOLDOWN_MAX_WAIT or waits >= _MAX_WAIT_ROUNDS:
                if wait > _ALL_COOLDOWN_MAX_WAIT:
                    resets = [r for r in self._rpd_reset if r is not None]
                    when = (f"，最快將於 {min(resets):%m/%d %H:%M}（GMT+8）重置"
                            if resets else "")
                    raise GeminiQuotaExceeded(
                        f"所有 {n} 把金鑰皆達額度上限（含每日配額 RPD）{when}，終止")
                raise GeminiQuotaExceeded(
                    f"所有 {n} 把金鑰反覆達速率上限，終止（{last_quota}）")
            wait = max(1.0, wait) + 0.5
            self._log(f"  所有金鑰暫時冷卻中，等待約 {int(wait)}s 後重試…")
            if not self._sleep_with_stop(wait):
                raise GeminiAborted("等待金鑰冷卻時收到停止指令")
            waits += 1

    def _request(self, key: str, prompt_text: str) -> str:
        body: dict = {"contents": [{"parts": [{"text": prompt_text}]}]}
        if self._system_prompt.strip():
            body["systemInstruction"] = {
                "parts": [{"text": self._system_prompt}]}
        # 特定模型套用預設 thinking level（如 3.5 Flash → high）
        level = _DEFAULT_THINKING_LEVEL.get(self._model.lower())
        if level:
            body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": level}}
        url = _ENDPOINT.format(model=self._model) + f"?key={key}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 429:
                retry_after, is_daily = self._parse_quota_error(e, body)
                exc = GeminiQuotaExceeded("HTTP 429（額度上限）")
                # 供 _cooldown_for 判斷冷卻長度（RPM 用 retry_after，RPD 用長冷卻）
                exc.retry_after = retry_after  # type: ignore[attr-defined]
                exc.is_daily = is_daily        # type: ignore[attr-defined]
                raise exc from e
            if e.code in _TRANSIENT_HTTP:
                raise GeminiServerBusy(
                    f"HTTP {e.code}（伺服器忙碌/暫時無法服務）：{body[:200]}") from e
            raise GeminiWebError(f"API 錯誤 HTTP {e.code}：{body[:300]}") from e
        except urllib.error.URLError as e:
            raise GeminiWebError(f"API 連線失敗：{e}") from e
        return self._extract_text(payload)

    # ── 額度冷卻輔助 ──

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

    def _cooldown_for(self, exc: Exception) -> tuple[float, datetime | None]:
        """依 429 例外決定（冷卻秒數, RPD 重置時刻｜None）。

        RPD：冷卻到下一個 GMT+8 15:00 重置時刻（回傳該 datetime 供 log／持久化）；
        RPM：短冷卻（伺服器建議的 retryDelay，否則 60s），回傳 None。
        """
        if getattr(exc, "is_daily", False):
            reset_dt = _next_rpd_reset()
            secs = max(1.0, (reset_dt - datetime.now(_RESET_TZ)).total_seconds())
            return secs, reset_dt
        retry_after = getattr(exc, "retry_after", None)
        cd = retry_after if (retry_after and retry_after > 0) else _RPM_COOLDOWN
        return float(cd), None

    @staticmethod
    def _parse_quota_error(
        err: urllib.error.HTTPError, body: str) -> tuple[float | None, bool]:
        """解析 429 回應，回傳 (建議重試秒數 or None, 是否為每日配額 RPD)。

        判斷依據（任一命中即可，全程容錯，解析失敗回 (None, False)）：
          * `error.details[].QuotaFailure.violations[].quotaId/quotaMetric` 含 "PerDay" → RPD
          * `error.details[].RetryInfo.retryDelay`（如 "30s"）→ 建議重試秒數
          * HTTP `Retry-After` 標頭（秒）→ 建議重試秒數（次要）
        """
        retry_after: float | None = None
        is_daily = False
        # HTTP Retry-After 標頭（純秒數）
        try:
            ra = err.headers.get("Retry-After") if err.headers else None
            if ra and ra.strip().isdigit():
                retry_after = float(ra.strip())
        except Exception:
            pass
        try:
            data = json.loads(body) if body else {}
            details = (data.get("error", {}) or {}).get("details", []) or []
            for d in details:
                if not isinstance(d, dict):
                    continue
                dtype = str(d.get("@type", ""))
                if dtype.endswith("QuotaFailure"):
                    for v in d.get("violations", []) or []:
                        ident = (str(v.get("quotaId", ""))
                                 + " " + str(v.get("quotaMetric", ""))).lower()
                        if "perday" in ident or "per_day" in ident:
                            is_daily = True
                elif dtype.endswith("RetryInfo"):
                    m = _DURATION_RE.search(str(d.get("retryDelay", "")))
                    if m:
                        retry_after = float(m.group(1))
            # 部分回應只在 message 提到 per day
            msg = str((data.get("error", {}) or {}).get("message", "")).lower()
            if "per day" in msg or "perday" in msg or "daily" in msg:
                is_daily = True
        except Exception:
            pass
        return retry_after, is_daily

    # ── RPD 冷卻持久化（跨程式記住每日配額狀態，過了重置時間自動失效） ──

    def _quota_state_path(self) -> str | None:
        if not self._base_dir:
            return None
        return os.path.join(self._base_dir, _QUOTA_STATE_FILENAME)

    def _load_quota_state(self) -> None:
        """載入上次存的 RPD 冷卻：以金鑰指紋對映重置時間。

        重置時間「未過」→ 設回冷卻（換算成單調時鐘剩餘秒數）；「已過」→ 視為可用
        （自動重算），不載入。載入後重寫檔案以清掉過期項目。
        """
        path = self._quota_state_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        now = datetime.now(_RESET_TZ)
        loaded = 0
        for i, key in enumerate(self._keys):
            iso = data.get(_key_fingerprint(key))
            if not iso:
                continue
            try:
                reset_dt = datetime.fromisoformat(iso)
            except Exception:
                continue
            remaining = (reset_dt - now).total_seconds()
            if remaining > 0:  # 尚未到重置時間 → 仍在 RPD 冷卻
                self._cooldown_until[i] = self._now() + remaining
                self._rpd_reset[i] = reset_dt
                loaded += 1
                self._log(f"  金鑰 #{i + 1} 仍在每日配額冷卻中，"
                          f"額度將於 {reset_dt:%m/%d %H:%M}（GMT+8）重置")
            # remaining <= 0：已過重置時間 → 自動重算為可用，不載入
        if loaded < len([v for v in data.values() if v]):
            self._save_quota_state()  # 清掉過期／無關的項目

    def _save_quota_state(self) -> None:
        """把目前仍有效（未過重置時間）的 RPD 冷卻寫檔；空則刪檔。"""
        path = self._quota_state_path()
        if not path:
            return
        now = datetime.now(_RESET_TZ)
        state = {
            _key_fingerprint(self._keys[i]): r.isoformat()
            for i, r in enumerate(self._rpd_reset)
            if r is not None and (r - now).total_seconds() > 0
        }
        try:
            if state:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
            elif os.path.exists(path):
                os.remove(path)
        except Exception as e:  # noqa: BLE001 — 持久化失敗不影響翻譯
            self._log(f"  （RPD 冷卻狀態寫入失敗，不影響翻譯：{e}）")

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
