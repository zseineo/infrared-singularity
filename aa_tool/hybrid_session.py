"""「瀏覽器＋API」翻譯方式（v3.34）：瀏覽器優先，瀏覽器額度用完改用 API，到了恢復時間切回瀏覽器。

包一個 :class:`aa_tool.gemini_web.GeminiWebSession`（須以 ``quota_fallback=True`` 建立：
額度用完時丟帶 ``reset_at`` 的 :class:`GeminiQuotaExceeded`，不原地長等）與一個 API session
（``GeminiApiSession``／``ChatApiSession``），對協調器提供同一組
``open``／``translate``／``close``／``start_new_session`` 介面，協調器不必知道底下是哪個。

- 瀏覽器額度用完（開新對話時要求的模型不可選、回覆含額度訊息、對話中途被降級後切不回去）
  → 記下恢復時刻（選單／回覆的額度字樣解析不到時用 ``_RETRY_UNKNOWN``），這一話與之後改用 API。
- 用 API 時每次翻譯前看時間，到了就開新對話試瀏覽器；還是不行就更新恢復時刻、繼續用 API。
- API 也用完（所有金鑰冷卻／每日配額）：瀏覽器的恢復時刻在 ``_MAX_WAIT_FOR_WEB`` 內就等到那時
  切回瀏覽器，否則照原本行為往外拋（協調器暫停整批）。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from .gemini_web import GeminiAborted, GeminiQuotaExceeded

_RETRY_UNKNOWN = 20 * 60          # 不知道恢復時間時，隔多久再試瀏覽器
_MAX_WAIT_FOR_WEB = 12 * 3600     # API 也用完時，最多等瀏覽器恢復多久


class HybridSession:
    def __init__(self, web, api, *, log: Callable[[str], None] | None = None,
                 stop_event=None) -> None:
        self._web = web
        self._api = api
        self._log = log or (lambda m: print(f"[hybrid] {m}"))
        self._stop_event = stop_event
        self.mode = "web"            # "web" | "api"：目前用哪個
        self._web_retry_at = 0.0     # 用 API 時，到這個時刻（epoch）再試瀏覽器

    # ── 生命週期 ──

    def open(self, *a, **kw) -> None:
        self._api.open()             # 先開 API：金鑰／端點設定有錯，開始前就報出來
        try:
            self._web.open(*a, **kw)
        except GeminiQuotaExceeded as e:
            self._to_api(e)

    def close(self) -> None:
        for s in (self._web, self._api):
            try:
                s.close()
            except Exception:  # noqa: BLE001 — 收尾不影響結果
                pass

    def __enter__(self) -> "HybridSession":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def start_new_session(self) -> None:
        if self.mode == "web":
            try:
                self._web.start_new_session()
            except GeminiQuotaExceeded as e:
                self._to_api(e)
        else:
            self._api.start_new_session()

    # ── 翻譯 ──

    def translate(self, prompt_text: str) -> str:
        if self.mode == "api" and time.time() >= self._web_retry_at:
            self._try_back_to_web()
        if self.mode == "web":
            try:
                return self._web.translate(prompt_text)
            except GeminiQuotaExceeded as e:
                self._to_api(e)
        try:
            return self._api.translate(prompt_text)
        except GeminiQuotaExceeded as e:
            return self._wait_either(prompt_text, e)

    def _wait_either(self, prompt_text: str, api_exc: GeminiQuotaExceeded) -> str:
        """API 也用完：每到瀏覽器預定恢復的時間就試瀏覽器，不行再順便試一次 API，
        輪流等到其中一邊恢復；超過 _MAX_WAIT_FOR_WEB 才往外拋（協調器暫停整批）。

        v3.34 只等一次——讀不到恢復時間（20 分鐘後試）或瀏覽器實際比標示的晚恢復時，
        第一次試失敗就整批暫停。
        """
        deadline = time.time() + _MAX_WAIT_FOR_WEB
        while True:
            if self._web_retry_at > deadline:
                raise api_exc
            self._log(f"⏸️ API 額度也用完了（{api_exc}），等到 {_fmt(self._web_retry_at)} "
                      "再試瀏覽器（可按停止中斷）…")
            self._sleep(self._web_retry_at - time.time())
            self._try_back_to_web()
            if self.mode == "web":
                try:
                    return self._web.translate(prompt_text)
                except GeminiQuotaExceeded as e:
                    self._to_api(e)
            try:   # 瀏覽器還不行：API 可能已經恢復了（每日配額重置、冷卻結束）
                return self._api.translate(prompt_text)
            except GeminiQuotaExceeded as e:
                api_exc = e

    def _to_api(self, e: GeminiQuotaExceeded) -> None:
        now = time.time()
        reset = getattr(e, "reset_at", None)
        if reset and reset > now:
            self._web_retry_at = reset + 60      # 多等一分鐘，避開剛好卡在恢復那一刻
            when = f"預計 {_fmt(reset)} 恢復，屆時切回瀏覽器"
        else:
            self._web_retry_at = now + _RETRY_UNKNOWN
            when = f"不知道何時恢復，{_RETRY_UNKNOWN // 60} 分鐘後再試瀏覽器"
        if self.mode != "api":
            self._log(f"🔀 瀏覽器額度用完（{e}），改用 API 翻譯；{when}")
        else:
            self._log(f"🔀 瀏覽器仍不能用（{e}），繼續用 API；{when}")
        self.mode = "api"

    def _try_back_to_web(self) -> None:
        self._log("🔀 到了瀏覽器額度恢復的時間，開新對話試著切回瀏覽器…")
        try:
            self._web.start_new_session()     # 開新對話＋確認模型；仍不能用會丟額度例外
        except GeminiQuotaExceeded as e:
            self._to_api(e)
            return
        self.mode = "web"
        self._log("✅ 已切回瀏覽器翻譯")

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_event is not None and self._stop_event.is_set():
                raise GeminiAborted("使用者於等待瀏覽器額度恢復時中止")
            time.sleep(min(1.0, end - time.time()))


def _fmt(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%m/%d %H:%M")
