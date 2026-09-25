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
import re
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


class GeminiBusyRetriesExhausted(GeminiWebError):
    """伺服器忙碌（5xx）／請求逾時，同一次請求連續重試達上限，這次先放棄。

    由 API 後端（`gemini_api`／`openai_api`）丟出。協調器據此把該話「暫時跳過」、
    放進待補翻列表，等下一次翻譯成功（伺服器已恢復）後再補翻，不中斷整批。
    """


class GeminiContentBlocked(GeminiWebError):
    """API 端的內容安全過濾擋下了這次請求或回應。

    例：Gemini `promptFeedback.blockReason: PROHIBITED_CONTENT`、候選回覆
    `finishReason: SAFETY`；OpenAI 相容 `finish_reason: content_filter`；
    Anthropic `stop_reason: refusal`。本質上等同「被審查」——重送同樣內容幾乎
    一定再被擋，協調器比照 `CensoredResponse` 跳過該話、續下一話，不中斷整批。
    """


class GeminiResponseTruncated(GeminiWebError):
    """API 回覆因輸出達模型上限而被截斷（不論是否已有部分文字）。

    例：Gemini `finishReason: MAX_TOKENS`、OpenAI 相容 `finish_reason: length`、
    Anthropic `stop_reason: max_tokens`。原因是這一話的輸出太長，重送同樣內容
    幾乎一定再被截斷 → 協調器跳過該話（不存半套翻譯）、續下一話，不中斷整批。
    """


# ── 自動翻譯進階設定：各種錯誤要「中斷」或「重試」（v2.41） ──
# 值為 "retry"／"stop"；沒設定的項目用這裡的預設（＝v2.40 以前的固定行為）。
# API 項目由兩個 API 後端（gemini_api／openai_api）套用：重試＝丟各自的「伺服器忙碌」
# 例外走等待重試、達上限後由協調器放進待補翻列表；中斷＝丟 GeminiWebError。
# web_stuck／fetch_fail 由協調器（aa_auto_translate）套用。
ERROR_POLICY_DEFAULTS: dict[str, str] = {
    "api_5xx": "retry",            # 伺服器忙碌（HTTP 500/502/503/504）
    "api_timeout": "retry",        # 回應逾時／連線逾時
    "api_conn_after_ok": "retry",  # 連線失敗／中途斷線／非 JSON（本批已成功翻譯過）
    "api_conn_first": "stop",      # 同上，但本批還沒成功翻譯過（多半是設定問題）
    "api_4xx": "stop",             # HTTP 4xx（429 額度另有冷卻邏輯，不在此列）
    "api_empty": "stop",           # 空回應（非安全過濾、非截斷）
    "web_stuck": "stop",           # 瀏覽器：Gemini 卡住（開新對話重送後仍無回應）
    "web_censored": "stop",        # 回覆被抽換成罐頭拒絕語／極短回覆（疑似被審查）
    "reply_format": "retry",       # 回覆不是 ID|譯文 格式（AI 沒照 prompt，回了摘要）
    "reply_lines": "retry",        # 譯文 ID 行數比送出的少太多（漏翻）
    "fetch_fail": "stop",          # 抓取網頁失敗
}

# 少數項目的「retry／stop」不是字面上的重試／中斷，UI 與 Log 改用這裡的說法。
ERROR_POLICY_CHOICE_LABELS: dict[str, dict[str, str]] = {
    # 這一項的 stop ＝跳過這一話續下一話（不是中斷整批），retry ＝開新對話重送、
    # 再不行就把該段對半拆開送（見 aa_auto_translate._send_chunk）。
    "web_censored": {"retry": "重送＋拆段", "stop": "跳過這一話"},
    # 這兩項的 retry ＝排進待補翻列表、之後再補翻（不是當場重送）。
    "reply_format": {"retry": "稍後重試", "stop": "跳過這一話"},
    "reply_lines": {"retry": "稍後重試", "stop": "跳過這一話"},
}


def policy_choice_label(key: str, value: str) -> str:
    """進階設定某項目某選項的顯示文字（未特別指定者用「重試」／「中斷」）。"""
    return ERROR_POLICY_CHOICE_LABELS.get(key, {}).get(
        value, "重試" if value == "retry" else "中斷")


def resolve_error_policy(policy) -> dict[str, str]:
    """設定值 → 完整的 {項目: "retry"|"stop"}；未知項目／值忽略，缺的補預設。"""
    out = dict(ERROR_POLICY_DEFAULTS)
    if isinstance(policy, dict):
        for k, v in policy.items():
            if k in out and v in ("retry", "stop"):
                out[k] = v
    return out


def policy_error(policy: dict, key: str, msg: str, *, busy_cls: type,
                 default_note: str = "") -> GeminiWebError:
    """依進階設定決定這個錯誤要丟哪種例外。

    重試 → ``busy_cls``（後端的「伺服器忙碌」例外，會等待重試）；中斷 → GeminiWebError
    （協調器中斷整批）。選的是預設值時附 ``default_note``（原本的說明），否則附
    「（進階設定：重試／中斷）」，讓 Log 看得出行為是被設定改過的。
    """
    choice = policy.get(key, ERROR_POLICY_DEFAULTS[key])
    note = (default_note if choice == ERROR_POLICY_DEFAULTS[key]
            else f"（進階設定：{'重試' if choice == 'retry' else '中斷'}）")
    return busy_cls(msg + note) if choice == "retry" else GeminiWebError(msg + note)


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
        # 簡體介面的送出鈕是「发送」，只列繁體會找不到而退回 Enter 送出
        "button[aria-label*='发送']",
        "button[aria-label*='發送']",
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
    # 簡體介面（使用者可能把 Gemini 語言設為簡體中文；字不同、比對不到會漏判）
    "已达使用上限",
    "已达上限",
    "用量已满",
    "请稍后再试",
    "升级即可继续",
]

DEFAULT_MAX_PER_SESSION = 3

# 生成完成判定：回覆文字連續 _STABLE_CHECKS 次（間隔 _POLL_INTERVAL 秒）不變即視為完成。
_POLL_INTERVAL = 1.0
_STABLE_CHECKS = 3
_GEN_TIMEOUT = 600          # 單次生成最長等待秒數
# 送出後過了這麼久仍沒開始生成（出現停止鈕或新回覆） → 視為「根本沒送出去」（頁面在填字後被導走、
# 文字被洗掉等），直接回空讓 translate() 開新對話重送，而不是空等 _GEN_TIMEOUT。
_GEN_NOT_STARTED_TIMEOUT = 60
# 開 Gem 後頁面網址須穩定停在 Gem 上這麼久才算開好；網路慢時 Gemini 會在
# domcontentloaded 之後才把 Gem 網址改導到 /app（沒套用 Gem 的一般對話）。
_GEM_URL_SETTLE = 5.0
_GEM_OPEN_RETRIES = 3
# 生成判定完成後，再多等這秒數才讀取回覆文字。
# 目的：避免串流尾端／DOM 尚未完全 render 時就讀走半截或舊內容
# （等同「按下複製鍵到實際取得內容之間的緩衝」）。
_POST_GEN_SETTLE = 3.0

# 自動切換模型失敗（選單選擇器失效）時，退回等使用者手動切換的最長秒數。
_MODEL_WAIT_TIMEOUT = 300   # 5 分鐘
_MODEL_WAIT_POLL = 3        # 每 3 秒重讀一次模型字串
_MODEL_REMIND_EVERY = 30    # 每 30 秒於 Log 提醒一次「請切換模型」
# 點選單項目的逾時：停用中的項目若用預設 30 秒會卡很久，縮短後改判為「模型不可用」。
_MODEL_CLICK_TIMEOUT_MS = 5000
# 點完之後確認指示器真的換過去的最長等待秒數。
_MODEL_SWITCH_CONFIRM = 3.0
# 開新對話後模型指示器比輸入框晚出現（實測晚 0.1～0.2 秒），讀不到時最多再等這麼久。
_MODEL_READ_TIMEOUT = 5.0
# 要求的模型額度已滿時，長時間等待額度恢復（每隔一段時間重試自動切換）。
_QUOTA_WAIT_TIMEOUT = 12 * 3600   # 最長等 12 小時
_QUOTA_POLL_INTERVAL = 600        # 每 10 分鐘重試一次自動切換
# 模型選單上「額度已滿／將於某時恢復」的字樣（命中代表該模型暫時不可用）。
# 繁體／簡體／英文都要涵蓋：使用者的 Gemini 介面語言不一定是繁體，只列繁體字樣
# 會在簡體介面完全比對不到（「用量額度將於…重設」→「用量额度将于…重置」），
# 導致額度已滿被誤判成「選單選擇器失效」而中止整批。
_QUOTA_RESET_PHRASES = [
    "用量額度將於", "額度將於", "額度已滿", "已達上限",
    "用量额度将于", "额度将于", "额度已满", "已达上限",
    "quota will reset", "available again", "resets ",
]
# 上面列不完的寫法（各語言／改版文案）再用寬鬆規則兜底：同一段文字裡同時出現
# 「額度／配額／quota／limit」與「將於／重設／恢復／reset」之類的字眼即視為額度訊息。
_QUOTA_RESET_RE = re.compile(
    r"(?:[額额]度|配[額额]|quota|limit)[^\n]{0,24}?"
    r"(?:將於|将于|重[設设置]|恢復|恢复|已[滿满]|用完|reset|renew|available again)"
    r"|(?:將於|将于)[^\n]{0,16}?重[設设置]",
    re.IGNORECASE,
)


def looks_quota_note(text: str) -> bool:
    """模型選單項目的文字看起來是否在說「這個模型額度已滿／某時才恢復」。"""
    if not text:
        return False
    low = text.lower()
    if any(p.lower() in low for p in _QUOTA_RESET_PHRASES):
        return True
    return bool(_QUOTA_RESET_RE.search(text))


_GEM_ID_RE = re.compile(r'/gem/([^/?#]+)')


def _gem_id(url: str) -> str:
    """網址中的 Gem 代號（``/gem/<id>`` 的 id）；不是 Gem 網址回空字串。

    只比對代號、不比整條路徑：同一個 Gem 可能帶 ``/u/1/`` 帳號前綴，
    送出後網址還會多一段對話 id。
    """
    m = _GEM_ID_RE.search(url or "")
    return m.group(1) if m else ""

# 啟動瀏覽器的候選，依序嘗試第一個能啟動的：
#   None    → Playwright 自帶的 Chromium（原本唯一的行為，已可用者不受影響）
#   chrome  → 系統安裝的 Google Chrome
#   msedge  → 系統安裝的 Microsoft Edge（Windows 必有）
# 打包版（PyInstaller）不含 Chromium 本體（約 400MB），故一定會退到後兩者。
_BROWSER_CHANNELS: list[str | None] = [None, "chrome", "msedge"]
_CHANNEL_LABELS = {
    None: "Playwright 內建 Chromium",
    "chrome": "系統 Google Chrome",
    "msedge": "系統 Microsoft Edge",
}


def _use_system_browsers_path(log: Callable[[str], None]) -> None:
    """讓打包版也能用使用者自行 ``playwright install`` 裝的瀏覽器。

    playwright 的 ``_impl/_transport.py`` 在偵測到 ``sys.frozen``（PyInstaller
    打包）時會強制 ``PLAYWRIGHT_BROWSERS_PATH=0``，只找 exe 內
    ``_internal/playwright/driver/package/.local-browsers``；本工具沒有把
    Chromium 打包進去，於是連使用者自己裝在 ``%LOCALAPPDATA%\\ms-playwright``
    的瀏覽器也一併看不到。這裡在該目錄存在時明確指過去（明確值會蓋過
    playwright 內部的 ``setdefault``）。

    未打包執行時這個值本來就是預設值，設了等於沒設；使用者已自行指定
    ``PLAYWRIGHT_BROWSERS_PATH`` 時一律尊重，不覆寫。
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return
    path = os.path.join(local_app_data, "ms-playwright")
    if os.path.isdir(path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
        log(f"瀏覽器路徑指向系統安裝位置：{path}")


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
        _use_system_browsers_path(self._log)
        self._pw = sync_playwright().start()
        try:
            self._context = self._launch_context()
        except Exception:
            self.close()   # 啟動失敗也要收掉已 start 的 playwright，否則留下孤兒 node 程序
            raise
        try:
            self._context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._open_new_chat()
        self._ensure_logged_in(login_timeout)
        self._ensure_model()

    def _launch_context(self):
        """依 ``_BROWSER_CHANNELS`` 順序啟動，回傳第一個成功的 persistent context。

        內建 Chromium 排第一，因此原本就能跑的環境行為完全不變；只有在
        Chromium 不存在（最常見：打包版）時才會退到系統的 Chrome／Edge。
        """
        failures: list[str] = []
        for channel in _BROWSER_CHANNELS:
            # Playwright 預設帶 --no-sandbox；內建 Chromium 會隱藏警示列，系統 Chrome／Edge
            # 則頂端常駐「不受支援的命令列標記：--no-sandbox」。系統瀏覽器先開沙箱啟動，
            # 失敗才退回原本的無沙箱（內建 Chromium 維持原行為）。
            for sandbox in ((True, False) if channel else (False,)):
                kwargs = {
                    "headless": self.headless,
                    "args": ["--disable-blink-features=AutomationControlled"],
                    "chromium_sandbox": sandbox,
                }
                if channel:
                    kwargs["channel"] = channel
                try:
                    context = self._pw.chromium.launch_persistent_context(
                        self.profile_dir, **kwargs)
                except Exception as e:
                    first_line = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
                    failures.append(f"{_CHANNEL_LABELS[channel]}"
                                    f"{'（沙箱）' if sandbox else ''}：{first_line}")
                    continue
                if channel:
                    self._log(f"未找到內建 Chromium，改用{_CHANNEL_LABELS[channel]}啟動")
                return context
        raise GeminiWebError(
            "無法啟動瀏覽器：找不到 Playwright 內建 Chromium，系統也沒有可用的 "
            "Google Chrome 或 Microsoft Edge。\n"
            "解法擇一：(1) 安裝 Google Chrome 或 Microsoft Edge；"
            "(2) 執行 pip install playwright 後再執行 playwright install chromium。\n"
            "嘗試紀錄：\n  - " + "\n  - ".join(failures))

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
        回覆後模型已不符 ``required_model``（額度用完被自動降級）→ 捨棄這次回覆，
        開新對話確認模型後重送一次；仍不符丟 :class:`GeminiModelMismatch`。
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
            self._log("⏳ Gemini 沒有回應（訊息沒送出，或卡住逾時），開新對話重試一次…")
            self._open_new_chat()
            self._ensure_logged_in(60)
            self._ensure_model()
            reply = self._send_and_collect(prompt_text)
            if not reply.strip():
                raise GeminiStuck(
                    "Gemini 沒有回應（訊息沒送出，或卡住逾時），重開新對話後仍然如此")
        self._check_quota(reply)
        # 額度用完時 Gemini 會在對話中途自動降級（例如 Flash → Flash-Lite），而模型
        # 只在開新對話時確認 → 每次回覆後再讀一次。降級後的這次回覆不採用：開新對話
        # （_ensure_model 會切回或等額度恢復）後重送。
        cur = self._downgraded_model()
        if cur:
            self._log(f"⚠️ 送出後模型變成「{cur}」，不符需求「{self.required_model}」"
                      "（可能額度用完被自動降級）→ 這次回覆不採用，開新對話確認模型後重送…")
            self.start_new_session()
            reply = self._send_and_collect(prompt_text)
            if not reply.strip():
                raise GeminiStuck("模型降級後開新對話重送，仍無回應")
            self._check_quota(reply)
            cur = self._downgraded_model()
            if cur:
                raise GeminiModelMismatch(
                    f"重送後模型仍為「{cur}」，不符需求「{self.required_model}」")
        return reply

    def _downgraded_model(self) -> str:
        """目前模型讀得到且不符 required_model 時回傳模型名，否則回空字串。"""
        req = self.required_model
        if not req or req == "any":
            return ""
        model = self._read_current_model()
        return model if model and not model_matches(model, req) else ""

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
        if not self._wait_generation_done(prev_count):
            # 沒送出去：頁面上最後一則回覆是「上一段」的，不能當成這次的回覆
            return ""
        # 生成判定完成後再沉澱數秒，確保讀到的是完整最終回覆
        if _POST_GEN_SETTLE > 0:
            time.sleep(_POST_GEN_SETTLE)
        return self._latest_response_text()

    # ── 對話管理 ──

    def start_new_session(self) -> None:
        """公開：強制開一個全新對話（重試前用，避免同一對話重複吐相同結果）。

        重開後重新確認登入與模型（與 session 輪替走同一套）。
        """
        if self._page is None:
            return
        self._open_new_chat()
        self._ensure_logged_in(60)
        self._ensure_model()

    def _open_new_chat(self) -> None:
        """重新導向 Gem URL 開啟全新對話，重置送出計數（不讀模型，呼叫端自行決定何時 log）。

        網路慢時（使用者回報：中國連線）Gemini 會在頁面載入後才把 Gem 網址改導到
        ``/app``：沒套用 Gem，而且已填入的文字會被洗掉、訊息沒送出。故開完要確認
        網址穩定停在 Gem 上，被導走就重開（最多 ``_GEM_OPEN_RETRIES`` 次）。
        """
        want = _gem_id(self.gem_url)
        for attempt in range(1, _GEM_OPEN_RETRIES + 1):
            self._page.goto(self.gem_url, wait_until="domcontentloaded")
            # 不是 Gem 網址（沒有 /gem/<id>）就無從檢查；登入頁交給 _ensure_logged_in
            if not want or self._on_login_page() or self._stays_on_gem(want):
                break
            self._log(f"⚠️ 開啟 Gem 後被導到「{self._page.url}」（不是 Gem 對話，"
                      f"多半是網路慢），重新開啟（{attempt}/{_GEM_OPEN_RETRIES}）…")
        else:
            self._log("⚠️ 多次開啟仍被導離 Gem，請確認 Gem 網址能在瀏覽器正常開啟；"
                      "先照目前頁面繼續")
        self._send_count = 0
        self._session_index += 1

    def _on_login_page(self) -> bool:
        url = (self._page.url or "").lower()
        return "accounts.google.com" in url or "signin" in url

    def _stays_on_gem(self, want: str) -> bool:
        """等輸入框出現（最多 30 秒），之後網址連續 ``_GEM_URL_SETTLE`` 秒都還在
        這個 Gem 上才回 True；期間任何時刻被導離就回 False。"""
        deadline = time.time() + 30
        while time.time() < deadline and self._find("input") is None:
            if _gem_id(self._page.url) != want:
                return False
            self._sleep_with_stop(0.5)
        settle_end = time.time() + _GEM_URL_SETTLE
        while True:
            if _gem_id(self._page.url) != want:
                return False
            if time.time() >= settle_end:
                return True
            self._sleep_with_stop(0.3)

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
        deadline = time.time() + _MODEL_READ_TIMEOUT
        while not model and time.time() < deadline:
            # 指示器還沒 render 就讀會讀到空、整個 session 略過檢查 → 多等一下
            self._sleep_with_stop(0.5)
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
        # 手動切換的 5 分鐘從「連續 fail 的第一次」起算：額度等待後重整頁面、
        # 選單一時沒載好而 fail 時，才不會因起點是 10 分鐘前而當場逾時。
        manual_deadline: float | None = None
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
                manual_deadline = None
                if not announced_quota:
                    self._log(
                        f"⏸️ 目前無法切換到「{req}」（{info}），"
                        f"多半是該模型額度已滿。將每 "
                        f"{_QUOTA_POLL_INTERVAL // 60} 分鐘重整頁面重試一次，"
                        f"最長等 {_QUOTA_WAIT_TIMEOUT // 3600} 小時；"
                        "若選單本來就沒有這個模型，請按停止並改「要求模型」設定。")
                    announced_quota = True
                while True:
                    if time.time() >= quota_deadline:
                        raise GeminiModelMismatch(
                            f"等待「{req}」額度恢復逾時（超過 "
                            f"{_QUOTA_WAIT_TIMEOUT // 3600} 小時）：{info}")
                    self._sleep_with_stop(_QUOTA_POLL_INTERVAL)
                    # 選單上的額度狀態可能只在頁面載入時取得，不重整會一直顯示
                    # 「額度已滿」。進入等待前一定剛開過新對話，重整不會遺失內容。
                    if self._reload_for_quota_poll():
                        break
                continue
            # status == "fail"：選單操作或選擇器失效 → 退回等使用者手動切換
            now = time.time()
            if manual_deadline is None:
                manual_deadline = now + _MODEL_WAIT_TIMEOUT
            if now - last_remind >= _MODEL_REMIND_EVERY:
                self._log(f"⚠️ 無法自動切換模型（{info or '選單選擇器可能失效'}），"
                          "請在瀏覽器手動切換到正確模型…")
                last_remind = now
            if now >= manual_deadline:
                raise GeminiModelMismatch(
                    f"無法自動切換到「{req}」，且等待手動切換逾時"
                    f"（{info or '選單選擇器可能失效'}）")
            self._sleep_with_stop(_MODEL_WAIT_POLL)
            cur = self._read_current_model()
            if cur and model_matches(cur, req):
                self._log(f"✅ 偵測到已切換為「{cur}」，繼續翻譯")
                return

    def _reload_for_quota_poll(self) -> bool:
        """額度等待中，重試自動切換前重新整理頁面，並等模型選單出現。

        重整失敗（網路中斷等）或 60 秒內模型選單沒出現（例如被登出）回 False，
        由呼叫端等下一個輪詢間隔再試。
        """
        self._log("🔄 重新整理頁面，檢查額度是否已恢復…")
        try:
            self._page.reload(wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001 — 任何重整失敗都等下一輪再試
            self._log(f"⚠️ 重新整理頁面失敗：{e}；下次輪詢再試")
            return False
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._find("model_indicator") is not None:
                self._page.wait_for_timeout(400)  # 同 _ensure_model，等選單穩定
                return True
            self._sleep_with_stop(1.5)
        self._log("⚠️ 重新整理後 60 秒內未出現模型選單（可能被登出）；下次輪詢再試")
        return False

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
          - 'ok'    成功選到，且指示器已確認換成符合的模型
          - 'quota' 該模型目前不可用（顯示額度已滿／項目被停用／選單裡根本沒有它
                    ／點了也換不過去）；info 為說明，呼叫端會等額度恢復後重試
          - 'fail'  選單打不開或讀不到任何項目（多半是選擇器失效）；info 為原因

        **'quota' 與 'fail' 的分野**：讀得到選單項目就代表選擇器沒失效，
        這時選不到要求的模型幾乎都是「該模型暫時不可用」（額度用完時 Gemini 會把
        該項目標成停用或直接不列出），要等額度恢復、不是叫使用者手動切換——
        v2.46 前一律歸成 'fail'，在簡體介面（額度字樣比對不到）會演變成
        「無法自動切換模型」洗版 5 分鐘後中止整批。
        """
        picker = self._find("model_indicator")
        if picker is None:
            return "fail", "找不到模型選單按鈕"
        try:
            picker.click()
            self._page.wait_for_timeout(700)
        except Exception as e:  # noqa: BLE001 — 點不開就是選擇器／版面問題
            return "fail", f"點不開模型選單：{e}"

        target = None
        target_txt = ""
        target_name = ""
        seen: list[str] = []          # 選單上讀到的所有項目首行（診斷用）
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
                seen.append(name)
                if target is None and model_matches(name, req):
                    target = item
                    target_txt = txt
                    target_name = name
            if seen:
                break

        menu_desc = "／".join(seen[:8]) if seen else ""
        if target is None:
            self._dismiss_menu()
            if not seen:
                return "fail", "選單打開了但讀不到任何模型項目"
            # 選單有東西、就是沒有要求的那個 → 多半是額度用完被下架
            return "quota", f"選單中沒有符合「{req}」的模型（目前有：{menu_desc}）"

        if looks_quota_note(target_txt):
            info = next(
                (ln.strip() for ln in target_txt.splitlines()
                 if looks_quota_note(ln)),
                "額度已滿")
            self._dismiss_menu()
            return "quota", info

        # 額度用完時該項目常被標成停用；直接點下去會卡到 Playwright 的
        # actionability 逾時（預設 30 秒）才丟例外，被誤判成選擇器失效。
        try:
            if target.is_disabled():
                self._dismiss_menu()
                return "quota", f"選單中的「{target_name or req}」目前不可選取"
        except Exception:
            pass

        try:
            target.click(timeout=_MODEL_CLICK_TIMEOUT_MS)
            self._page.wait_for_timeout(800)
        except Exception as e:  # noqa: BLE001 — 點不下去多半是該模型被停用
            self._dismiss_menu()
            return "quota", f"點不下去「{req}」這個項目（{type(e).__name__}）"

        # 點了不代表換得過去：額度用完時 Gemini 會把指示器彈回原本的模型。
        cur = self._read_current_model()
        deadline = time.time() + _MODEL_SWITCH_CONFIRM
        while cur and not model_matches(cur, req) and time.time() < deadline:
            self._sleep_with_stop(0.5)
            cur = self._read_current_model()
        if cur and not model_matches(cur, req):
            return "quota", f"點選後模型仍是「{cur}」，沒有換成「{req}」"
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
                ("pro", "flash", "ultra", "gemini", "2.5", "3.0", "3.1", "3.5", "3.6")
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

    def _wait_generation_done(self, prev_count: int) -> bool:
        """等待生成開始 → 結束 → 回覆文字穩定。

        回傳 False ＝ ``_GEN_NOT_STARTED_TIMEOUT`` 秒內根本沒開始生成（訊息多半沒送出去，
        例如填字後頁面被導走、文字被洗掉）；呼叫端應開新對話重送，不要空等 ``_GEN_TIMEOUT``。
        """
        # 1) 等待開始：出現停止鈕，或回覆數量增加
        start_deadline = time.time() + _GEN_NOT_STARTED_TIMEOUT
        started = False
        while time.time() < start_deadline:
            if self._find("stop") is not None or self._response_count() > prev_count:
                started = True
                break
            time.sleep(0.3)
        if not started:
            self._log(f"⚠️ 送出後 {_GEN_NOT_STARTED_TIMEOUT}s 仍未開始生成"
                      f"（目前頁面：{self._page.url}），訊息可能沒送出去")
            return False

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
                    return True
            else:
                stable = 0
            last_text = text
            time.sleep(_POLL_INTERVAL)
        self._log("⚠️ 等待生成逾時，改用目前已取得的回覆")
        return True

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
