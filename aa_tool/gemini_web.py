"""網頁版 Gemini 自動化模組。

用 Playwright 操控瀏覽器上的 Gemini Gem 進行翻譯，供 ``aa_auto_translate.py``
的自動化流程使用。本模組不依賴 PyQt，可獨立執行與測試。

⚠️ 維護提醒：Gemini 前端改版會使下方 DOM 選擇器失效，是本模組最主要的脆弱點。
所有選擇器集中在 :data:`DEFAULT_SELECTORS`，並可由 :class:`GeminiWebSession` 的
``selectors`` 參數（對應 ``AA_settings`` 的 ``gemini_selectors``）覆寫。
若自動化突然失敗且訊息指向「找不到元素」，第一步先檢查、更新這裡的選擇器。
"""
from __future__ import annotations

import os
import time
from typing import Callable


class GeminiWebError(RuntimeError):
    """gemini_web 模組的通用錯誤。"""


class GeminiNotLoggedIn(GeminiWebError):
    """偵測到瀏覽器未登入 Google 帳號（且等待手動登入逾時）。"""


class GeminiQuotaExceeded(GeminiWebError):
    """撞到 Gemini 用量額度上限（例如 3.1 Pro 模型額度用盡）。

    這不是「翻譯失敗」，呼叫端應暫停整批流程、保留已完成進度，
    待額度恢復後再續跑。
    """


class GeminiStuck(GeminiWebError):
    """Gemini 卡住超過 10 分鐘仍無回應；通常重開對話可解。"""


class GeminiModelMismatch(GeminiWebError):
    """偵測到目前模型與 ``required_model`` 不符，且等待重選逾時。"""


class GeminiAborted(GeminiWebError):
    """等待過程中收到外部 stop_event，使用者主動中止。"""


def model_matches(detected: str, required: str) -> bool:
    """判斷讀到的模型字串是否符合要求。

    required 可選值：
      - ``""`` / ``"any"`` → 不檢查，永遠視為符合
      - ``"pro"``        → 字串含 ``pro`` 且不含 ``flash``
      - ``"flash"``      → 字串含 ``flash`` 但不含 ``lite``
      - ``"flash-lite"`` → 字串含 ``flash`` 且含 ``lite``

    讀不到模型字串（``detected`` 為空）時回 True（不阻擋，但會在 Log 顯示警告）；
    這是為了避免 Gemini 改版讀不到指示元素時把使用者整批鎖死。
    """
    if not required or required == "any":
        return True
    if not detected:
        return True  # 讀不到不阻擋，由 Log 警告提示使用者
    d = detected.lower()
    if required == "pro":
        return "pro" in d and "flash" not in d
    if required == "flash-lite":
        return "flash" in d and "lite" in d
    if required == "flash":
        return "flash" in d and "lite" not in d
    return True  # 未知 required 值 → 不阻擋


# ── DOM 選擇器（集中管理，Gemini 改版時改這裡）──
# 每一項為「候選 selector 列表」，依序嘗試，取第一個命中的元素。
DEFAULT_SELECTORS: dict[str, list[str]] = {
    # 輸入框（contenteditable）
    "input": [
        "div.ql-editor[contenteditable='true']",
        "rich-textarea .ql-editor",
        "div[contenteditable='true'][role='textbox']",
    ],
    # 送出按鈕
    "send": [
        "button.send-button",
        "button[aria-label*='Send']",
        "button[aria-label*='傳送']",
        "button[aria-label*='送出']",
        "button[mattooltip*='Send']",
    ],
    # 「停止生成」按鈕（出現＝正在生成）
    "stop": [
        "button[aria-label*='Stop']",
        "button[aria-label*='停止']",
        "button.stop",
    ],
    # 模型回覆區塊（取頁面上最後一個）
    "response": [
        "message-content.model-response-text",
        ".model-response-text",
        "model-response",
    ],
    # 目前模型指示器（用來在 Log 顯示「正在使用哪個模型」供使用者確認，例如 2.5 Pro）。
    # Gemini 2025 改版後預設模型選項消失，需要這條線索讓使用者確認確實是 Pro。
    "model_indicator": [
        "bard-mode-switcher button",
        "bard-mode-switcher",
        "button[data-test-id*='model']",
        "button[aria-label*='model']",
        "button[aria-label*='模型']",
        "[class*='model-switcher'] button",
        "[class*='mode-switcher']",
    ],
    # 模型選單項目（點開模型指示器後出現的清單），用於自動切換模型。
    "model_menu_item": [
        "[role='menuitemradio']",
        "[role='menuitem']",
        "mat-option",
        ".mat-mdc-menu-item",
        "button.bard-mode-list-button",
        "[class*='mode-list'] button",
    ],
}

# 額度上限訊息關鍵字（命中即視為撞額度，全小寫比對）。
# 刻意只收「具體片語」，不收「額度」「升級」等單詞 —— 那些會出現在 Gemini 常駐
# UI 或正常譯文中，會造成誤判。Gemini 改版若改了上限訊息文案，請在此補上新片語。
QUOTA_PHRASES = [
    "you've reached your limit",
    "you have reached your limit",
    "reached your limit",
    "limit for gemini",
    "limit for 2.5 pro",
    "limit for 3.1 pro",
    "upgrade to continue",
    "you've hit your limit",
    "已達使用上限",
    "已達上限",
    "用量已滿",
    "已用完",
    "請稍後再試",
    "升級即可繼續",
]

DEFAULT_MAX_PER_SESSION = 3

# 生成完成判定：回覆文字連續 _STABLE_CHECKS 次（間隔 _POLL_INTERVAL 秒）不變即視為完成。
_POLL_INTERVAL = 1.0
_STABLE_CHECKS = 3
_GEN_TIMEOUT = 600          # 單次生成最長等待秒數
_GEN_START_TIMEOUT = 25     # 送出後等待「開始生成」的最長秒數
# 生成判定完成後，再多等這秒數才讀取回覆文字。
# 目的：避免串流尾端／DOM 尚未完全 render 時就讀走半截或舊內容
# （等同「按下複製鍵到實際取得內容之間的緩衝」）。
_POST_GEN_SETTLE = 3.0

# 自動切換模型失敗（選單選擇器失效）時，退回等使用者手動切換的最長秒數。
_MODEL_WAIT_TIMEOUT = 300   # 5 分鐘
_MODEL_WAIT_POLL = 3        # 每 3 秒重讀一次模型字串
_MODEL_REMIND_EVERY = 30    # 每 30 秒於 Log 提醒一次「請切換模型」
# 要求的模型額度已滿時，長時間等待額度恢復（每隔一段時間重試自動切換）。
_QUOTA_WAIT_TIMEOUT = 12 * 3600   # 最長等 12 小時
_QUOTA_POLL_INTERVAL = 600        # 每 10 分鐘重試一次自動切換
# 模型選單上「額度已滿／將於某時恢復」的字樣（命中代表該模型暫時不可用）。
_QUOTA_RESET_PHRASES = [
    "用量額度將於", "額度將於", "額度已滿", "已達上限",
    "quota will reset", "available again", "resets ",
]


class GeminiWebSession:
    """管理一個持久化瀏覽器，操控 Gemini Gem 進行翻譯。

    使用方式::

        with GeminiWebSession(gem_url, profile_dir) as s:
            reply = s.translate("001-1|こんにちは")

    持久化 profile 讓 Google 登入狀態長期保留：第一次跑會開啟瀏覽器視窗，
    需手動登入一次，之後同一 ``profile_dir`` 不必再登入。
    """

    def __init__(
        self,
        gem_url: str,
        profile_dir: str,
        *,
        max_per_session: int = DEFAULT_MAX_PER_SESSION,
        selectors: dict | None = None,
        headless: bool = False,
        required_model: str = "",
        prepend_prompt: str = "",
        stop_event=None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if not gem_url:
            raise GeminiWebError("未提供 Gem 網址（gem_url）")
        self.gem_url = gem_url
        self.profile_dir = profile_dir
        self.max_per_session = max(1, int(max_per_session or DEFAULT_MAX_PER_SESSION))
        self.required_model = (required_model or "").strip().lower()
        # 翻譯 prompt：非空時，會附加在「每個新對話的第一則訊息」最前面，
        # 等於把指令放在對話開頭（後續同對話的分段靠 Gemini 自身上下文即可）。
        self.prepend_prompt = (prepend_prompt or "").strip()
        self.stop_event = stop_event  # 由協調器傳入，模型等待時用來中止
        # 合併使用者覆寫：覆寫值為「完整候選列表」，整項取代預設。
        self.selectors = dict(DEFAULT_SELECTORS)
        for key, val in (selectors or {}).items():
            if isinstance(val, list) and val:
                self.selectors[key] = val
            elif isinstance(val, str) and val:
                self.selectors[key] = [val]
        self.headless = headless
        self._log = log or (lambda m: print(f"[gemini_web] {m}"))
        self._pw = None
        self._context = None
        self._page = None
        self._send_count = 0      # 當前 session 已送出次數
        self._session_index = 0   # session 序號（每開新對話 +1）

    # ── 生命週期 ──

    def open(self, login_timeout: int = 300) -> None:
        """啟動瀏覽器、開啟 Gem 並確認已登入。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise GeminiWebError(
                "未安裝 playwright，請執行：pip install playwright "
                "並接著 playwright install chromium") from e
        if self.profile_dir:
            os.makedirs(self.profile_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            self._context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._open_new_chat()
        self._ensure_logged_in(login_timeout)
        self._ensure_model()

    def close(self) -> None:
        """關閉瀏覽器與 Playwright。"""
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:
                pass
        self._context = self._page = self._pw = None

    def __enter__(self) -> "GeminiWebSession":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 翻譯 ──

    def translate(self, prompt_text: str) -> str:
        """送出一段文字給 Gem，回傳生成完成後的最新回覆純文字。

        達到 ``max_per_session`` 時自動開啟新對話再送出（計數歸零）。
        偵測到額度上限時丟 :class:`GeminiQuotaExceeded`。
        若 10 分鐘無回應，自動重開一個新對話再送一次；仍無回應丟
        :class:`GeminiStuck`。
        """
        if self._page is None:
            raise GeminiWebError("session 尚未 open()")
        if not prompt_text.strip():
            return ""

        if self._send_count >= self.max_per_session:
            self._log(f"已達 session 上限（{self.max_per_session} 次），開啟新對話")
            self._open_new_chat()
            self._ensure_logged_in(60)
            self._ensure_model()

        reply = self._send_and_collect(prompt_text)
        if not reply.strip():
            self._log(
                f"⏳ Gemini 卡住超過 {_GEN_TIMEOUT}s 無回應，開新對話重試一次…")
            self._open_new_chat()
            self._ensure_logged_in(60)
            self._ensure_model()
            reply = self._send_and_collect(prompt_text)
            if not reply.strip():
                raise GeminiStuck(
                    f"Gemini 卡住超過 {_GEN_TIMEOUT}s 且重開新對話後仍無回應")
        self._check_quota(reply)
        return reply

    def _send_and_collect(self, prompt_text: str) -> str:
        """填入 → 送出 → 等待生成完成 → 取最新回覆。內部計數已遞增。"""
        # 新對話的第一則訊息把 prompt 附在最前面（後續分段靠對話上下文）
        first_in_chat = (self._send_count == 0)
        self._send_count += 1
        self._log(f"session #{self._session_index} 第 "
                  f"{self._send_count}/{self.max_per_session} 次送出")
        text = prompt_text
        if self.prepend_prompt and first_in_chat:
            text = self.prepend_prompt + "\n\n" + prompt_text
            self._log("  （已在對話開頭附加翻譯 prompt）")
        editor = self._require("input")
        editor.click()
        editor.fill(text)
        prev_count = self._response_count()
        self._click_send()
        self._wait_generation_done(prev_count)
        # 生成判定完成後再沉澱數秒，確保讀到的是完整最終回覆
        if _POST_GEN_SETTLE > 0:
            time.sleep(_POST_GEN_SETTLE)
        return self._latest_response_text()

    # ── 內部：對話管理 ──

    def _open_new_chat(self) -> None:
        """重新導向 Gem URL 開啟全新對話，重置送出計數（不讀模型，呼叫端自行決定何時 log）。"""
        self._page.goto(self.gem_url, wait_until="domcontentloaded")
        self._send_count = 0
        self._session_index += 1

    def _ensure_model(self) -> None:
        """確認目前模型符合 ``required_model``；不符時嘗試自動從選單切換。

        - 符合 / 不檢查 / 讀不到模型 → 只 log，不阻擋。
        - 不符 → 自動點開模型選單選到要求的模型。
        - 要求的模型「額度已滿」→ 長時間輪詢等額度恢復後再自動切換（可按停止中止）。
        - 選單選擇器失效 → 退回等使用者手動切換（短逾時）。
        """
        req = self.required_model
        try:
            self._page.wait_for_timeout(400)
        except Exception:
            pass
        model = self._read_current_model()
        if not req or req == "any":
            self._log(f"目前模型：{model or '(讀不到)'}")
            return
        if not model:
            self._log("⚠️ 無法讀取模型名稱（Gemini 可能改版），略過模型檢查")
            return
        if model_matches(model, req):
            self._log(f"目前使用模型：{model}（符合需求：{req}）")
            return
        self._log(f"目前模型「{model}」不符需求「{req}」，嘗試自動切換…")
        self._switch_model_with_wait(req)

    def _switch_model_with_wait(self, req: str) -> None:
        """自動切換到 req；額度滿則長等、選單失效則退回等手動切換。"""
        quota_deadline = time.time() + _QUOTA_WAIT_TIMEOUT
        manual_deadline = time.time() + _MODEL_WAIT_TIMEOUT
        announced_quota = False
        last_remind = 0.0
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise GeminiAborted("使用者於切換模型期間中止")
            status, info = self._try_select_model(req)
            if status == "ok":
                cur = self._read_current_model()
                self._log(f"✅ 已自動切換到「{cur or req}」，繼續翻譯")
                return
            if status == "quota":
                if not announced_quota:
                    self._log(
                        f"⏸️ 「{req}」額度已滿（{info}）。將等額度恢復後自動切換，"
                        f"最長等 {_QUOTA_WAIT_TIMEOUT // 3600} 小時；可按停止中止。")
                    announced_quota = True
                if time.time() >= quota_deadline:
                    raise GeminiModelMismatch(
                        f"等待「{req}」額度恢復逾時（超過 "
                        f"{_QUOTA_WAIT_TIMEOUT // 3600} 小時）")
                self._sleep_with_stop(_QUOTA_POLL_INTERVAL)
                continue
            # status == "fail"：選單操作或選擇器失效 → 退回等使用者手動切換
            now = time.time()
            if now - last_remind >= _MODEL_REMIND_EVERY:
                self._log("⚠️ 無法自動切換模型（選單選擇器可能失效），"
                          "請在瀏覽器手動切換到正確模型…")
                last_remind = now
            if now >= manual_deadline:
                raise GeminiModelMismatch(
                    f"無法自動切換到「{req}」，且等待手動切換逾時")
            self._sleep_with_stop(_MODEL_WAIT_POLL)
            cur = self._read_current_model()
            if cur and model_matches(cur, req):
                self._log(f"✅ 偵測到已切換為「{cur}」，繼續翻譯")
                return

    def _sleep_with_stop(self, seconds: float) -> None:
        """可被 stop_event 中斷的睡眠（每秒檢查一次）。"""
        end = time.time() + seconds
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                return
            if self.stop_event is not None and self.stop_event.is_set():
                raise GeminiAborted("使用者中止")
            time.sleep(min(1.0, remaining))

    def _dismiss_menu(self) -> None:
        try:
            self._page.keyboard.press("Escape")
        except Exception:
            pass

    def _try_select_model(self, req: str) -> tuple[str, str]:
        """點開模型選單、找符合 req 的項目並點選。

        回傳 (status, info)：
          - 'ok'    成功選到（且未顯示額度滿）
          - 'quota' 找到該模型但顯示額度已滿；info 為額度恢復時間那行
          - 'fail'  找不到選單／項目，或點選失敗
        """
        picker = self._find("model_indicator")
        if picker is None:
            return "fail", ""
        try:
            picker.click()
            self._page.wait_for_timeout(700)
        except Exception:
            return "fail", ""

        target = None
        target_txt = ""
        for sel in self.selectors.get("model_menu_item", []):
            try:
                loc = self._page.locator(sel)
                count = loc.count()
            except Exception:
                continue
            for i in range(count):
                try:
                    item = loc.nth(i)
                    if not item.is_visible():
                        continue
                    txt = (item.inner_text() or "").strip()
                except Exception:
                    continue
                if not txt:
                    continue
                name = txt.splitlines()[0].strip()  # 首行＝模型名
                if model_matches(name, req):
                    target = item
                    target_txt = txt
                    break
            if target is not None:
                break

        if target is None:
            self._dismiss_menu()
            return "fail", ""

        low = target_txt.lower()
        if any(p.lower() in low for p in _QUOTA_RESET_PHRASES):
            info = next(
                (ln.strip() for ln in target_txt.splitlines()
                 if any(p.lower() in ln.lower() for p in _QUOTA_RESET_PHRASES)),
                "額度已滿")
            self._dismiss_menu()
            return "quota", info

        try:
            target.click()
            self._page.wait_for_timeout(800)
        except Exception:
            self._dismiss_menu()
            return "fail", ""
        return "ok", ""

    def _read_current_model(self) -> str:
        """讀取頁面上顯示的目前模型名稱（例如 '2.5 Pro'）。讀不到回空字串。"""
        for sel in self.selectors.get("model_indicator", []):
            try:
                loc = self._page.locator(sel)
                if loc.count() == 0:
                    continue
                text = (loc.first.inner_text() or "").strip()
            except Exception:
                continue
            # 模型字串通常很短（如 "2.5 Pro"、"Gemini 2.5 Pro"），過長視為命中錯元素。
            if 1 <= len(text) <= 60 and any(
                kw in text.lower() for kw in
                ("pro", "flash", "ultra", "gemini", "2.5", "3.0", "3.1")
            ):
                return text
        return ""

    def _ensure_logged_in(self, timeout: int) -> None:
        """輪詢等待輸入框出現；逾時且仍在登入頁則丟 GeminiNotLoggedIn。"""
        deadline = time.time() + timeout
        prompted = False
        while time.time() < deadline:
            if self._find("input") is not None:
                return
            url = (self._page.url or "").lower()
            if ("accounts.google.com" in url or "signin" in url) and not prompted:
                self._log("⚠️ 偵測到未登入 Google，請在彈出的瀏覽器視窗手動登入…")
                prompted = True
            time.sleep(1.5)
        if self._find("input") is not None:
            return
        raise GeminiNotLoggedIn(
            "等待登入逾時：請先在瀏覽器手動登入 Google 並進入 Gem 頁面，"
            "登入狀態會記在 profile 目錄，下次不需重登。")

    # ── 內部：元素定位 ──

    def _find(self, role: str):
        """回傳該 role 第一個命中且可見的 locator；找不到回 None。"""
        for sel in self.selectors.get(role, []):
            try:
                loc = self._page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first
            except Exception:
                continue
        return None

    def _require(self, role: str):
        loc = self._find(role)
        if loc is None:
            raise GeminiWebError(
                f"找不到「{role}」元素 — Gemini 可能已改版。"
                f"請更新 gemini_web.DEFAULT_SELECTORS['{role}'] "
                f"或設定檔的 gemini_selectors。")
        return loc

    def _click_send(self) -> None:
        deadline = time.time() + 15
        while time.time() < deadline:
            btn = self._find("send")
            if btn is not None:
                try:
                    if btn.is_enabled():
                        btn.click()
                        return
                except Exception:
                    pass
            time.sleep(0.5)
        # 後備：直接按 Enter 送出
        self._log("找不到可用的送出按鈕，改用 Enter 送出")
        self._page.keyboard.press("Enter")

    def _response_count(self) -> int:
        for sel in self.selectors.get("response", []):
            try:
                n = self._page.locator(sel).count()
                if n:
                    return n
            except Exception:
                continue
        return 0

    def _latest_response_text(self) -> str:
        for sel in self.selectors.get("response", []):
            try:
                loc = self._page.locator(sel)
                if loc.count() > 0:
                    return (loc.last.inner_text() or "").strip()
            except Exception:
                continue
        return ""

    # ── 內部：等待生成完成 ──

    def _wait_generation_done(self, prev_count: int) -> None:
        """等待生成開始 → 結束 → 回覆文字穩定。"""
        # 1) 等待開始：出現停止鈕，或回覆數量增加
        start_deadline = time.time() + _GEN_START_TIMEOUT
        while time.time() < start_deadline:
            if self._find("stop") is not None or self._response_count() > prev_count:
                break
            time.sleep(0.3)

        # 2) 等待結束：停止鈕消失 + 回覆文字連續數次不變
        gen_deadline = time.time() + _GEN_TIMEOUT
        last_text = None
        stable = 0
        while time.time() < gen_deadline:
            generating = self._find("stop") is not None
            text = self._latest_response_text()
            if not generating and text and text == last_text:
                stable += 1
                if stable >= _STABLE_CHECKS:
                    return
            else:
                stable = 0
            last_text = text
            time.sleep(_POLL_INTERVAL)
        self._log("⚠️ 等待生成逾時，改用目前已取得的回覆")

    # ── 內部：額度偵測 ──

    def _check_quota(self, reply: str) -> None:
        """檢查回覆是否含額度上限訊息，命中則丟 GeminiQuotaExceeded。

        只掃描模型回覆本身（不掃整頁），避免把 Gemini 常駐 UI 的
        「升級」「額度」等字樣誤判成撞上限。
        """
        text = reply.lower()
        for phrase in QUOTA_PHRASES:
            if phrase in text:
                raise GeminiQuotaExceeded(f"偵測到額度上限訊息：「{phrase}」")
