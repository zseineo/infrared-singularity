"""網頁版 Gemini 自動化模組。

用 Playwright 操控瀏覽器上的 Gemini Gem 進行翻譯，供 ``aa_auto_translate.py``
的自動化流程使用。本模組不依賴 PyQt，可獨立執行與測試。

⚠️ 維護提醒：Gemini 前端改版會使下方 DOM 選擇器失效，是本模組最主要的脆弱點。
所有選擇器集中在 :data:`DEFAULT_SELECTORS`，並可由 :class:`GeminiWebSession` 的
``selectors`` 參數（對應 ``AA_settings`` 的 ``gemini_selectors``）覆寫。
若自動化突然失敗且訊息指向「找不到元素」，第一步先檢查、更新這裡的選擇器。
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import random
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
    "reply_japanese": "retry",     # 譯文殘留大量日文（平假名比例過高，部分沒翻）
    "reply_ids": "retry",          # 譯文行號對不上（回成上一段的譯文、或上一段接在前面）
    "fetch_fail": "stop",          # 抓取網頁失敗
}

# 少數項目的「retry／stop」不是字面上的重試／中斷，UI 與 Log 改用這裡的說法。
ERROR_POLICY_CHOICE_LABELS: dict[str, dict[str, str]] = {
    # 以下各項的 stop ＝跳過這一話續下一話（不是中斷整批），retry ＝排進待補翻列表、
    # 之後再補翻（不是當場重送）。web_censored 的 retry v3.03 前是「當場重送＋拆段」。
    "web_censored": {"retry": "稍後重試", "stop": "跳過這一話"},
    "reply_format": {"retry": "稍後重試", "stop": "跳過這一話"},
    "reply_lines": {"retry": "稍後重試", "stop": "跳過這一話"},
    "reply_japanese": {"retry": "稍後重試", "stop": "跳過這一話"},
    "reply_ids": {"retry": "稍後重試", "stop": "跳過這一話"},
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

# 「延伸思考」（v3.05）：模型選單裡和模型並列的開關項目（gem-menu-item；開啟時帶
# selected class，模型按鈕變成兩行「Flash／延伸」，點了選單就關）。用這些字樣認選單
# 項目與模型按鈕的第二行（繁中「延伸思考」；其他語言介面的寫法是推測）。
_THINKING_RE = re.compile(r"思考|延伸|think|扩展|拡張", re.I)

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
# 頁面左下角的提示（Angular Material snack-bar，例：「發生錯誤 (1095)」）只停留幾秒。
# 等待生成時順便讀，讀到就寫進 Log；送出後還沒開始生成就跳錯誤 → 再等
# _TOAST_ERROR_GRACE 秒仍沒開始就當作沒送出，不再空等 _GEN_NOT_STARTED_TIMEOUT。
_TOAST_SEL = ("mat-snack-bar-container, simple-snack-bar, "
              "[class*='snack-bar'], [class*='snackbar']")
_TOAST_JS = ("(sel) => Array.from(document.querySelectorAll(sel))"
             ".filter(e => !(e.parentElement && e.parentElement.closest(sel)))"
             ".map(e => (e.innerText || '').trim()).filter(Boolean)")
_TOAST_ERROR_RE = re.compile(r'錯誤|错误|error|went wrong|出了點問題|出了点问题', re.I)
_TOAST_ERROR_GRACE = 5.0
# 開 Gem 後頁面網址須穩定停在 Gem 上這麼久才算開好；網路慢時 Gemini 會在
# domcontentloaded 之後才把 Gem 網址改導到 /app（沒套用 Gem 的一般對話）。
_GEM_URL_SETTLE = 5.0
_GEM_OPEN_RETRIES = 3
# Debug Log：頁面健康檢查（在頁面內跑一小段 JS）最多等幾毫秒；等不到＝頁面凍結
_HEALTH_TIMEOUT_MS = 5000
# Debug Log：等待生成期間每隔幾秒記一次進度快照
_DEBUG_GEN_EVERY = 15.0
# 頁面健康檢查：DOM 元素數、JS heap（Chrome 的 performance.memory）
_HEALTH_JS = ("({n: document.getElementsByTagName('*').length,"
              " heap: (performance.memory || {}).usedJSHeapSize || 0,"
              " limit: (performance.memory || {}).jsHeapSizeLimit || 0})")

# 頁面看得到的 Playwright 痕跡（v3.00 實測）：bounding_box()、locator.evaluate()、
# wait_for_function() 第一次呼叫時，會在頁面本身的 JS 環境註冊一批全域事件監聽器
# （事件名稱包含「__playwright_global_listeners_check__」），頁面腳本只要改寫
# addEventListener 就看得到。click／fill／inner_text／is_enabled／is_visible／
# get_attribute／鍵盤／截圖／page.evaluate 則不會。送出流程一律避開前三者：
# 元素位置改由獨立 JS 環境讀（_box_of），健康檢查改用 get_attribute 探測。
# 生成判定完成後，再多等這秒數才讀取回覆文字。
# 目的：避免串流尾端／DOM 尚未完全 render 時就讀走半截或舊內容
# （等同「按下複製鍵到實際取得內容之間的緩衝」）。
_POST_GEN_SETTLE = 3.0

# 填入內容後、按送出前的停頓秒數。使用者回報：不論哪種送出方式，填完立刻送出
# 很容易遇到「發生錯誤 (1095)」，推測頁面還沒處理完輸入內容（v2.99）。
_PRE_SEND_PAUSE = 0.5

# 擬人操作（send_method="human"，v3.00 實驗）。Debug Log 統計（30 次送出）：回覆被抽換成
# 「我是語言模型」的比例，新對話的第一則 8/12，同對話第 2、3 則 5/18；同一個瀏覽器
# 手動操作則不會被擋 → 推測與頁面上觀察得到的操作行為有關（瞬移點擊、焦點反覆切換、
# 載入後立刻送出、毫無間隔的節奏）。以下皆為 (最短, 最長) 秒數，每次隨機取值。
_HUMAN_WARMUP = (8.0, 14.0)       # 新對話載入完成後，至少過這麼久才開始操作
_HUMAN_GAP = (3.0, 7.0)           # 同對話上一則回覆完成後，至少過這麼久（閱讀時間）
_HUMAN_PRE_PASTE = (0.4, 0.9)     # 點輸入框後到按 Ctrl+V
_HUMAN_PASTE_PAUSE = (1.0, 2.5)   # 貼上後到移向送出鈕
_HUMAN_HOVER = (0.12, 0.3)        # 游標停在目標上到按下
_HUMAN_CLICK_HOLD = (0.07, 0.13)  # 按下到放開

# 頁面座標校正用的 mousemove 監聽器。一律在獨立 JS 環境（_iso_eval）安裝：頁面腳本看不到
# 這些變數，改寫 addEventListener 之類內建函式的偵測腳本也攔不到這次呼叫。
_MOVE_LISTENER_JS = (
    "(() => { if (!window.__aaLm) { window.__aaLm = 1; addEventListener('mousemove',"
    " e => { window.__aaMove = [e.clientX, e.clientY]; }, true); }"
    " window.__aaMove = null; return 0; })()")
_EDITOR_FOCUSED_JS = (
    "(() => { const a = document.activeElement; return !!(a && a.isContentEditable); })()")

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


# 填入輸入框的方式（連線設定「填入方式」）。實測（v2.71，1.6 萬字）：已有回覆的對話頁
# 逐字填入要 20～26 秒（慢機器會超過 Playwright 預設 30 秒而逾時），另兩種 0.1～0.3 秒。
INPUT_METHODS: dict[str, str] = {
    "fill": "逐字填入（原本做法，最保險但長文較慢）",
    "quill": "直接寫入編輯器（快，不碰剪貼簿）",
    "clipboard": "剪貼簿貼上（快，會暫時佔用剪貼簿，貼完還原）",
    # v2.99 的 os_paste（系統鍵盤貼上）v3.02 移除：擬人操作一律自己用系統鍵盤貼上，
    # 程式送出要全背景運作（不切前景），舊設定讀取時改成 clipboard
}
DEFAULT_INPUT_METHOD = "fill"

# 按送出的方式（連線設定「送出方式」）。實測（v2.79）：某些時段 Gemini 會把「程式
# 產生的點擊／按鍵」（Playwright 的 click、Enter，含先移動滑鼠、等待、視窗在前景）
# 一律回「我是語言模型，幫不上忙」；同一個瀏覽器、同一份內容改由真實滑鼠點擊送出
# （人手或 Windows 系統滑鼠）就正常。PostMessage 送按鍵／點擊也一樣被擋或送不出。
# v3.01：只把點擊換成系統滑鼠的「滑鼠點擊」os_click 仍有一定比例被擋，擬人操作實測
# 不會 → 移除 os_click，只留程式送出與擬人操作（舊設定讀取時改成 human）。
SEND_METHODS: dict[str, str] = {
    "program": "程式送出（原本做法，不影響滑鼠）",
    # v3.00 實驗（取代 v2.99 的 os_click_input「點框＋送出」）：整段「點輸入框→貼上→
    # 點送出」都用系統滑鼠／鍵盤、軌跡與節奏比照真人，期間瀏覽器維持在前景，見
    # GeminiWebSession._human_compose_and_send
    "human": "擬人操作（實驗；每次送出佔用滑鼠與前景數秒）",
}
DEFAULT_SEND_METHOD = "program"

# Gemini 輸入框是 Quill 編輯器，實例掛在 .ql-container 的 __quill（內部屬性，改版可能消失
# → 回 false 由呼叫端退回逐字填入）。以 'user' 來源設值，Gemini 才會當成使用者輸入。
_QUILL_SET_JS = """(el, text) => {
  const c = el.closest('.ql-container'); const q = c && c.__quill;
  if (!q) return false;
  q.setText(text + String.fromCharCode(10), 'user');
  q.setSelection(q.getLength(), 0, 'user');
  return true;
}"""


def _clip_lines(text: str) -> list[str]:
    """比對輸入框內容用：去掉空行與行首尾空白（編輯器會吃掉／補上空白行）。"""
    return [ln.strip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]


def _win_clipboard_get() -> str | None:
    """讀系統剪貼簿文字（Windows）；非 Windows、沒有文字或讀不到回 None。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    for _ in range(10):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        h = u32.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def _win_clipboard_set(text: str) -> bool:
    """寫系統剪貼簿文字（Windows）；成功回 True。"""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    for _ in range(10):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            return False
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        return bool(u32.SetClipboardData(13, h))  # 成功後記憶體歸系統管
    finally:
        u32.CloseClipboard()


# 系統滑鼠點擊後沒送出（瀏覽器原本不在前景）時最多點幾次
_OS_CLICK_TRIES = 3


def _win_input_types():
    """SendInput 用的 (INPUT, MOUSEINPUT, KEYBDINPUT) 結構（Windows）。"""
    import ctypes
    from ctypes import wintypes

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):   # 聯集要含三種，INPUT 的大小才會與系統一致
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    return _INPUT, _MOUSEINPUT, _KEYBDINPUT


def _send_ctrl_key(vk: int, human: bool = False) -> bool:
    """用 SendInput 按一下 Ctrl+<vk>（Windows 真實鍵盤事件，送往目前的前景視窗）。

    human=True 時各鍵之間的間隔隨機、比照真人（約 0.15～0.35 秒完成）。
    """
    if os.name != "nt":
        return False
    import ctypes
    u32 = ctypes.windll.user32
    _INPUT, _MOUSEINPUT, _KEYBDINPUT = _win_input_types()
    vk_ctrl, keyup = 0x11, 0x0002

    def _key(code: int, flags: int) -> bool:
        arr = (_INPUT * 1)()
        arr[0].type = 1                                       # INPUT_KEYBOARD
        arr[0].u.ki = _KEYBDINPUT(code, u32.MapVirtualKeyW(code, 0), flags, 0, 0)
        return u32.SendInput(1, arr, ctypes.sizeof(_INPUT)) == 1

    gaps = ((random.uniform(0.06, 0.14), random.uniform(0.05, 0.11),
             random.uniform(0.03, 0.09)) if human else (0.03, 0.05, 0.03))
    ok = _key(vk_ctrl, 0)
    time.sleep(gaps[0])
    ok = ok and _key(vk, 0)
    time.sleep(gaps[1])
    _key(vk, keyup)                   # 放開一定要送，避免 Ctrl／按鍵卡在按下狀態
    time.sleep(gaps[2])
    _key(vk_ctrl, keyup)
    return ok


def _send_ctrl_v(human: bool = False) -> bool:
    """用 SendInput 按一下 Ctrl+V。"""
    return _send_ctrl_key(0x56, human)


def _human_path(x0: int, y0: int, x1: int, y1: int,
                rnd: random.Random) -> tuple[list[tuple[int, int]], float]:
    """擬人滑鼠軌跡：微彎的三次貝茲曲線、頭尾慢中間快、帶一點抖動。

    回傳 (各點螢幕座標, 每步間隔秒數)；距離越遠越久（約 0.2～0.9 秒）。最後一點必為終點。
    """
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 3:
        return [(x1, y1)], 0.0
    dur = min(0.9, 0.2 + dist / 1800.0) * rnd.uniform(0.85, 1.2)
    steps = max(10, int(dur * 80))
    nx, ny = -(y1 - y0) / dist, (x1 - x0) / dist          # 垂直方向（讓路徑微彎）
    b1, b2 = rnd.uniform(-0.18, 0.18) * dist, rnd.uniform(-0.18, 0.18) * dist
    c1 = (x0 + (x1 - x0) * 0.3 + nx * b1, y0 + (y1 - y0) * 0.3 + ny * b1)
    c2 = (x0 + (x1 - x0) * 0.7 + nx * b2, y0 + (y1 - y0) * 0.7 + ny * b2)
    pts: list[tuple[int, int]] = []
    for i in range(1, steps + 1):
        t = i / steps
        s = t * t * (3 - 2 * t)                             # 頭尾慢、中間快
        a, b, c, d = (1 - s) ** 3, 3 * (1 - s) ** 2 * s, 3 * (1 - s) * s * s, s ** 3
        x = a * x0 + b * c1[0] + c * c2[0] + d * x1
        y = a * y0 + b * c1[1] + c * c2[1] + d * y1
        if i < steps:
            x += rnd.uniform(-0.7, 0.7)
            y += rnd.uniform(-0.7, 0.7)
        p = (round(x), round(y))
        if not pts or p != pts[-1]:
            pts.append(p)
    if pts[-1] != (x1, y1):
        pts.append((x1, y1))
    return pts, dur / steps


def _send_mouse_click_at(x: int, y: int, hold: float = 0.05) -> bool:
    """在螢幕座標 (x, y) 用 SendInput 點一下左鍵（Windows）；成功回 True。hold＝按住秒數。

    按下與放開各送一批「移到絕對座標＋按鍵」：同一批 SendInput 的事件不會被使用者的
    滑鼠移動插隊，所以就算使用者正在動滑鼠，按下與放開也都落在 (x, y)。
    座標以整個虛擬桌面正規化到 0～65535（多螢幕適用）。
    """
    if os.name != "nt":
        return False
    import ctypes
    u32 = ctypes.windll.user32
    _INPUT, _MOUSEINPUT, _KEYBDINPUT = _win_input_types()

    vx, vy = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)   # 虛擬桌面左上
    vw, vh = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)   # 虛擬桌面寬高
    if vw <= 1 or vh <= 1:
        return False
    nx = round((x - vx) * 65535 / (vw - 1))
    ny = round((y - vy) * 65535 / (vh - 1))
    move = 0x0001 | 0x8000 | 0x4000   # MOVE | ABSOLUTE | VIRTUALDESK

    def _batch(button_flag: int) -> bool:
        arr = (_INPUT * 2)()
        for k, flags in enumerate((move, button_flag)):
            arr[k].type = 0                                   # INPUT_MOUSE
            arr[k].u.mi = _MOUSEINPUT(nx if k == 0 else 0, ny if k == 0 else 0,
                                      0, flags, 0, 0)
        return u32.SendInput(2, arr, ctypes.sizeof(_INPUT)) == 2

    if not _batch(0x0002):            # LEFTDOWN
        return False
    time.sleep(hold)
    if not _batch(0x0004):            # LEFTUP：按下已送出，放開一定要送到
        u32.mouse_event(0x0004, 0, 0, 0, 0)
    return True


def brief_error(e: BaseException) -> str:
    """例外訊息的精簡版：Playwright 的錯誤會在第一行摘要後附「Call log:」與
    整段呼叫紀錄——`fill()` 逾時時連要填入的整話原文都在裡面，Log 會被灌上
    幾百行。去掉「Call log:」以後的部分；其他例外（沒有 Call log）原樣保留。"""
    msg = str(e)
    cut = msg.find('Call log:')
    if cut >= 0:
        msg = msg[:cut]
    return msg.strip() or type(e).__name__


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
        abort_event=None,
        log: Callable[[str], None] | None = None,
        debug=None,
        input_method: str = DEFAULT_INPUT_METHOD,
        send_method: str = DEFAULT_SEND_METHOD,
        extended_thinking: bool | None = None,
    ) -> None:
        """``debug``：`aa_tool.debug_log.DebugLog`（勾「Debug Log」時由協調器傳入），
        記錄每一步耗時、頁面健康度、瀏覽器事件與錯誤截圖；None＝不記。
        ``abort_event``：GUI「強制停止」時設定——等待生成中也立即中止（一般停止要等
        這一話生成完）。"""
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
        self.abort_event = abort_event
        # 合併使用者覆寫：覆寫值為「完整候選列表」，整項取代預設。
        self.selectors = dict(DEFAULT_SELECTORS)
        for key, val in (selectors or {}).items():
            if isinstance(val, list) and val:
                self.selectors[key] = val
            elif isinstance(val, str) and val:
                self.selectors[key] = [val]
        self.headless = headless
        self._log = log or (lambda m: print(f"[gemini_web] {m}"))
        self._debug = debug
        self.input_method = (input_method if input_method in INPUT_METHODS
                             else DEFAULT_INPUT_METHOD)
        self.send_method = (send_method if send_method in SEND_METHODS
                            else DEFAULT_SEND_METHOD)
        # 延伸思考：True＝每個新對話確認開啟、False＝確認關閉、None＝不動
        self.extended_thinking = extended_thinking
        self._channel_used = ""   # 實際啟動的瀏覽器（Debug Log 用）
        self._pw = None
        self._context = None
        self._page = None
        self._send_count = 0      # 當前 session 已送出次數
        self._session_index = 0   # session 序號（每開新對話 +1）
        # 系統滑鼠點擊：上次校正出的「估算座標 → 實際座標」修正量（實體像素）。
        # 視窗沒移動時下次直接套用，第一跳就對準，游標停在送出鈕上的時間最短。
        self._os_click_adjust = (0.0, 0.0)
        self._seen_toasts: set[str] = set()   # 這次送出已記過的頁面提示
        self._hwnd = 0              # 瀏覽器頂層視窗（_find_browser_hwnd 快取）
        self._matched_sel: dict[str, str] = {}   # role → _find 最近命中的選擇器（_box_of 用）
        self._cdp = None            # 獨立 JS 環境用的 CDP session（_iso_eval）
        self._iso_id = None         # 獨立 JS 環境的 executionContextId（頁面導向後失效）
        self._page_ready_at = 0.0   # 新對話頁面載入完成的時間（擬人操作的暖機基準）
        self._last_reply_at = 0.0   # 上一則回覆讀完的時間（擬人操作的閱讀間隔基準）

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
        if self._debug is not None:
            self._debug_attach()
        self._open_new_chat()
        self._ensure_logged_in(login_timeout)
        self._ensure_model()

    def open_for_manual_test(self, stop_event) -> None:
        """手動測試用：以同一個 profile、同樣的啟動參數開瀏覽器並進入 Gem，不做任何操作。

        讓使用者在「與自動翻譯完全相同的瀏覽器」裡手動送訊息對照（判斷被擋是瀏覽器
        本身的問題，還是自動化操作的問題）。阻塞到使用者關掉所有分頁、或 stop_event
        被設定為止，結束時關閉瀏覽器。
        """
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
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
            self._page.goto(self.gem_url, wait_until="domcontentloaded")
            while not stop_event.is_set():
                try:
                    if not self._context.pages:
                        break
                    self._context.pages[0].wait_for_timeout(500)
                except Exception:  # noqa: BLE001 — 瀏覽器被使用者關掉
                    break
        finally:
            self.close()

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
                self._channel_used = (_CHANNEL_LABELS[channel]
                                      + ("（沙箱）" if sandbox else "（無沙箱）"))
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
        self._dbg(f"── session #{self._session_index} 第 {self._send_count} 次送出："
                  f"{text.count(chr(10)) + 1} 行／{len(text)} 字｜{self._page.url}")
        self._health("送出前")
        try:
            with self._timed("找輸入框"):
                editor = self._require("input")
            if self.send_method == "human":
                prev_count = self._response_count()
                with self._timed("擬人操作（點輸入框、貼上、點送出）"):
                    self._human_compose_and_send(editor, text)
            else:
                with self._timed("點輸入框"):
                    self._click_input(editor)
                with self._timed(f"填入文字（{self.input_method}）"):
                    self._fill_input(editor, text)
                if _PRE_SEND_PAUSE > 0:
                    time.sleep(_PRE_SEND_PAUSE)
                prev_count = self._response_count()
                with self._timed(f"按送出（{self.send_method}）"):
                    self._send()
            t0 = time.time()
            started = self._wait_generation_done(prev_count)
            self._last_reply_at = time.time()
            self._dbg(f"等待生成結束：{time.time() - t0:.1f}s"
                      + ("" if started else "（沒開始生成）"))
            if not started:
                # 沒送出去：頁面上最後一則回覆是「上一段」的，不能當成這次的回覆
                self._debug_screenshot("not_started")
                return ""
            # 生成判定完成後再沉澱數秒，確保讀到的是完整最終回覆
            if _POST_GEN_SETTLE > 0:
                time.sleep(_POST_GEN_SETTLE)
            reply = self._latest_response_text()
        except Exception as e:
            if self._debug is not None:
                self._debug.exception("送出／等待回覆時發生例外", e)
                self._health("例外當下")
                self._debug_screenshot("error")
            raise
        self._dbg(f"回覆：{reply.count(chr(10)) + 1 if reply else 0} 行／{len(reply)} 字")
        self._health("回覆後")
        return reply

    # ── 填入輸入框 ──

    def _fill_input(self, editor, text: str) -> None:
        """依 ``input_method`` 把文字放進輸入框；快速方式失敗就退回逐字填入。

        快速方式填完一律比對內容（行為單位），不一致就清空改用逐字填入——
        避免 Gemini 改版或剪貼簿被搶用時送出錯誤內容。
        """
        method = self.input_method
        if method == "quill":
            try:
                sel = self._matched_sel.get("input")
                ok = bool(self._page.evaluate(
                    f"([sel, text]) => ({_QUILL_SET_JS})(document.querySelector(sel), text)",
                    [sel, text]) if sel else editor.evaluate(_QUILL_SET_JS, text))
            except Exception as e:  # noqa: BLE001
                ok = False
                self._dbg(f"直接寫入編輯器失敗：{brief_error(e)}")
            if ok and self._input_matches(editor, text, wait=2.0):
                return
            self._log("  （直接寫入編輯器沒成功，改用逐字填入）")
        elif method == "clipboard":
            if self._paste_via_clipboard(editor, text):
                return
            self._log("  （剪貼簿貼上沒成功，改用逐字填入）")
        else:
            editor.fill(text)
            return
        self._clear_input(editor)
        editor.fill(text)

    def _input_matches(self, editor, text: str, wait: float = 0.0) -> bool:
        """輸入框內容與 text 逐行一致（忽略空行）。wait>0 時最多等這麼久讓頁面處理完。"""
        want = _clip_lines(text)
        deadline = time.time() + wait
        while True:
            try:
                got = _clip_lines(editor.inner_text(timeout=5000))
            except Exception:  # noqa: BLE001
                got = []
            if got == want:
                return True
            if time.time() >= deadline:
                i = next((k for k, (a, b) in enumerate(zip(got, want)) if a != b),
                         min(len(got), len(want)))
                ga = len(got[i]) if i < len(got) else "-"
                wa = len(want[i]) if i < len(want) else "-"
                self._dbg(f"輸入框內容不一致：{len(got)} 行（應為 {len(want)} 行），"
                          f"第 {i + 1} 行起不同（該行 {ga} 字，應為 {wa} 字）")
                return False
            time.sleep(0.2)

    def _clear_input(self, editor) -> None:
        try:
            editor.click()
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Delete")
        except Exception as e:  # noqa: BLE001
            self._dbg(f"清空輸入框失敗：{brief_error(e)}")

    def _paste_via_clipboard(self, editor, text: str) -> bool:
        """暫借系統剪貼簿貼上：先備份、貼完（不論成敗）立刻還原。成功回 True。

        只支援 Windows（其他平台回 False → 逐字填入）。剪貼簿原本不是文字
        （例如圖片）時無法還原成原樣，只會清成空的——tooltip 已提醒。
        """
        if os.name != "nt":
            return False
        backup = _win_clipboard_get()
        try:
            if not _win_clipboard_set(text):
                self._dbg("寫入剪貼簿失敗")
                return False
            editor.click()
            self._page.keyboard.press("Control+V")
            return self._input_matches(editor, text, wait=15.0)
        except Exception as e:  # noqa: BLE001
            self._dbg(f"剪貼簿貼上失敗：{brief_error(e)}")
            return False
        finally:
            _win_clipboard_set(backup or "")

    def _os_paste_into(self, editor, text: str, human: bool = False) -> bool:
        """擬人操作用（瀏覽器已在前景時）暫借剪貼簿、用系統鍵盤 Ctrl+V 貼上並核對內容，貼完還原剪貼簿。

        輸入框沒有焦點先用程式點一下；裡面已經有字（上一則沒清乾淨、草稿被還原等）
        就先 Ctrl+A 全選，讓這次貼上直接取代掉。human=True 時按鍵節奏比照真人。
        """
        backup = _win_clipboard_get()
        try:
            if not _win_clipboard_set(text):
                self._dbg("寫入剪貼簿失敗")
                return False
            if not self._editor_focused():
                editor.click()
            try:
                leftover = (editor.inner_text(timeout=2000) or "").strip()
            except Exception:  # noqa: BLE001
                leftover = ""
            if leftover:
                self._dbg(f"系統鍵盤貼上：輸入框原本有 {len(leftover)} 字，先全選再貼上")
                _send_ctrl_key(0x41, human)                     # Ctrl+A
                time.sleep(0.1)
            if not _send_ctrl_v(human):
                self._dbg("系統鍵盤貼上：SendInput 送不出去")
                return False
            return self._input_matches(editor, text, wait=15.0)
        except Exception as e:  # noqa: BLE001
            self._dbg(f"系統鍵盤貼上失敗：{brief_error(e)}")
            return False
        finally:
            _win_clipboard_set(backup or "")

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
            with self._timed(f"開啟 Gem（第 {attempt} 次）"):
                self._page.goto(self.gem_url, wait_until="domcontentloaded")
            # 不是 Gem 網址（沒有 /gem/<id>）就無從檢查；登入頁交給 _ensure_logged_in
            t0 = time.time()
            ok = not want or self._on_login_page() or self._stays_on_gem(want)
            self._dbg(f"確認停在 Gem：{'是' if ok else '否'}（{time.time() - t0:.1f}s）"
                      f"｜{self._page.url}")
            if ok:
                break
            self._log(f"⚠️ 開啟 Gem 後被導到「{self._page.url}」（不是 Gem 對話，"
                      f"多半是網路慢），重新開啟（{attempt}/{_GEM_OPEN_RETRIES}）…")
        else:
            self._log("⚠️ 多次開啟仍被導離 Gem，請確認 Gem 網址能在瀏覽器正常開啟；"
                      "先照目前頁面繼續")
        self._send_count = 0
        self._session_index += 1
        self._page_ready_at = time.time()
        self._iso_id = None         # 頁面導向後舊的獨立 JS 環境已失效

    # ── Debug Log（self._debug 為 None 時全部不做事） ──

    def _dbg(self, msg: str) -> None:
        if self._debug is not None:
            self._debug.write(msg)

    @contextlib.contextmanager
    def _timed(self, label: str):
        """with 區塊計時：寫「label：N.Ns」，丟例外時寫「label：失敗（N.Ns）」。"""
        t0 = time.time()
        try:
            yield
        except BaseException:
            self._dbg(f"{label}：失敗（{time.time() - t0:.1f}s）")
            raise
        self._dbg(f"{label}：{time.time() - t0:.1f}s")

    def _health(self, tag: str) -> None:
        """頁面健康度：在頁面內跑一小段 JS 量回應延遲、DOM 元素數、JS heap。

        先用有逾時的 get_attribute 探測頁面有沒有回應（頁面凍結時最多等
        _HEALTH_TIMEOUT_MS 就記「沒回應」），有回應才在獨立 JS 環境量數字。
        v3.00 前用 wait_for_function，會在頁面留下 Playwright 監聽器（見 _HEALTH_JS 註解）。
        """
        if self._debug is None or self._page is None:
            return
        from aa_tool.debug_log import system_memory
        t0 = time.time()
        try:
            self._page.locator("html").get_attribute("lang", timeout=_HEALTH_TIMEOUT_MS)
            v = self._iso_eval(_HEALTH_JS)
            mb = 1024 * 1024
            heap = (f"JS heap {v['heap'] / mb:.0f}／{v['limit'] / mb:.0f} MB"
                    if v.get("heap") else "JS heap 讀不到")
            self._dbg(f"[健康] {tag}：回應 {(time.time() - t0) * 1000:.0f}ms、"
                      f"DOM {v['n']} 個元素、{heap}、回覆區塊 {self._response_count()} 個"
                      f"｜{system_memory()}")
        except Exception as e:  # noqa: BLE001 — 量不到本身就是答案
            self._dbg(f"[健康] {tag}：頁面 {_HEALTH_TIMEOUT_MS // 1000} 秒內沒有回應"
                      f"（{brief_error(e)}）——頁面可能凍結｜{system_memory()}")

    def _debug_screenshot(self, tag: str) -> None:
        if self._debug is None or self._page is None:
            return
        path = self._debug.screenshot_path(tag)
        try:
            self._page.screenshot(path=path, timeout=10000)
            self._dbg(f"截圖：{os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            self._dbg(f"截圖失敗：{brief_error(e)}")

    def _debug_attach(self) -> None:
        """記錄瀏覽器版本並掛上頁面事件：崩潰、JS 錯誤、主框架導向、關閉、新分頁。"""
        page, dbg = self._page, self._dbg
        try:
            ver = self._context.browser.version if self._context.browser else ""
        except Exception:  # noqa: BLE001
            ver = ""
        dbg(f"瀏覽器：{self._channel_used or '?'} {ver}｜headless={self.headless}"
            f"｜每 {self.max_per_session} 次送出換新對話｜要求模型 {self.required_model or '不限'}"
            f"｜填入方式 {self.input_method}")
        try:
            page.on("crash", lambda *_: dbg("💥 頁面崩潰（crash 事件）"))
            page.on("close", lambda *_: dbg("頁面被關閉（close 事件）"))
            page.on("pageerror", lambda err: dbg(f"頁面 JS 錯誤：{str(err)[:300]}"))
            page.on("console", lambda m: m.type == "error"
                    and dbg(f"console.error：{m.text[:300]}"))
            page.on("framenavigated", lambda f: f == page.main_frame
                    and dbg(f"頁面導向：{f.url}"))
            self._context.on("page", lambda p: dbg(f"開了新分頁：{p.url}"))
        except Exception as e:  # noqa: BLE001
            dbg(f"掛頁面事件失敗：{brief_error(e)}")

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
        """確認模型（`_check_model`）後，再把「延伸思考」調成設定的狀態。"""
        self._check_model()
        self._ensure_thinking()

    def _thinking_on(self) -> bool | None:
        """「延伸思考」是否開著：開著時模型按鈕第二行會出現「延伸」。讀不到按鈕回 None。"""
        picker = self._find("model_indicator")
        if picker is None:
            return None
        try:
            lines = [ln.strip() for ln in (picker.inner_text() or "").splitlines()
                     if ln.strip()]
        except Exception:  # noqa: BLE001
            return None
        return any(_THINKING_RE.search(ln) for ln in lines[1:])

    def _thinking_menu_item(self):
        """模型選單（已點開）裡的「延伸思考」項目；找不到回 None。"""
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
                    first = ((item.inner_text() or "").strip().splitlines() or [""])[0]
                except Exception:
                    continue
                if _THINKING_RE.search(first):
                    return item
        return None

    def _ensure_thinking(self) -> None:
        """依設定開／關「延伸思考」（v3.05）。狀態相符就不動；切換失敗只記 Log、不阻擋翻譯。"""
        want = self.extended_thinking
        if want is None:
            return
        cur = self._thinking_on()
        if cur is None:
            self._log("⚠️ 讀不到「延伸思考」狀態（找不到模型按鈕），略過")
            return
        if cur == want:
            self._dbg(f"延伸思考：已是{'開啟' if want else '關閉'}")
            return
        label = "開啟" if want else "關閉"
        picker = self._find("model_indicator")
        try:
            picker.click()
            self._page.wait_for_timeout(700)
            item = self._thinking_menu_item()
            if item is None:
                self._dismiss_menu()
                self._log(f"⚠️ 模型選單裡找不到「延伸思考」，無法{label}（目前模型可能不支援）")
                return
            item.click(timeout=_MODEL_CLICK_TIMEOUT_MS)
            self._page.wait_for_timeout(800)
        except Exception as e:  # noqa: BLE001 — 切不了只提醒，不中斷翻譯
            self._dismiss_menu()
            self._log(f"⚠️ {label}「延伸思考」失敗：{brief_error(e)}")
            return
        if self._thinking_on() == want:
            self._log(f"✅ 已{label}「延伸思考」")
        else:
            self._log(f"⚠️ 點了「延伸思考」但狀態沒有變成{label}，請在瀏覽器確認")

    def _check_model(self) -> None:
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
            self._log(f"⚠️ 重新整理頁面失敗：{brief_error(e)}；下次輪詢再試")
            return False
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._find("model_indicator") is not None:
                self._page.wait_for_timeout(400)  # 同 _ensure_model，等選單穩定
                return True
            self._sleep_with_stop(1.5)
        self._log("⚠️ 重新整理後 60 秒內未出現模型選單（可能被登出）；下次輪詢再試")
        return False

    def _check_abort(self) -> None:
        """強制停止：不等生成完，直接中止。"""
        if self.abort_event is not None and self.abort_event.is_set():
            raise GeminiAborted("使用者強制停止")

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
            return "fail", f"點不開模型選單：{brief_error(e)}"

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
        """讀取頁面上顯示的目前模型名稱（例如 '2.5 Pro'）。讀不到回空字串。

        開著「延伸思考」時按鈕是兩行（「Flash」「延伸」），合併成一行回傳（「Flash 延伸」）。
        """
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
                return " ".join(text.split())
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
        """回傳該 role 第一個命中且可見的 locator；找不到回 None。命中的選擇器記在 _matched_sel。"""
        for sel in self.selectors.get(role, []):
            try:
                loc = self._page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    self._matched_sel[role] = sel
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

    def _click_input(self, editor) -> None:
        """點輸入框（程式點擊；擬人操作另走 _human_compose_and_send）。"""
        editor.click()

    def _send(self) -> None:
        """程式送出（擬人操作另走 _human_compose_and_send，找不到瀏覽器視窗時也退回這裡）。"""
        self._click_send()

    def _wait_send_enabled(self, timeout: float = 15.0):
        """等送出鈕出現且可按（填入後要一點時間），回傳 locator；逾時回 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            btn = self._find("send")
            try:
                if btn is not None and btn.is_enabled():
                    return btn
            except Exception:
                pass
            time.sleep(0.5)
        return None

    def _sent_check(self) -> bool:
        """送出生效：出現停止鈕，或輸入框已被清空。"""
        if self._find("stop") is not None:
            return True
        editor = self._find("input")
        try:
            return editor is not None and not (editor.inner_text(timeout=1000) or "").strip()
        except Exception:
            return False

    @staticmethod
    def _still_enabled(btn) -> bool:
        """補點的前提：送出鈕仍可按（輸入框還有字）。已送出時多點會中斷生成。"""
        try:
            return btn.is_enabled()
        except Exception:
            return False

    def _editor_focused(self) -> bool:
        """焦點在可編輯區（Gemini 輸入框）裡。"""
        try:
            return bool(self._iso_eval(_EDITOR_FOCUSED_JS))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _activate_window(h) -> None:
        """切前景視窗（Windows）。

        **不可用「按一下 Alt」解除前景鎖定**：Chrome 會把焦點移到「設定與其他」選單鈕，
        還原時也會讓使用者原本的程式（VS Code 等）選到選單列。改成暫時共用目前前景
        視窗執行緒的輸入佇列，SetForegroundWindow 就不會被擋。
        """
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        if u32.IsIconic(h):
            u32.ShowWindow(h, 9)                 # SW_RESTORE
        fg_tid = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
        me = k32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != me and \
            bool(u32.AttachThreadInput(me, fg_tid, True))
        try:
            u32.SetForegroundWindow(h)
            u32.BringWindowToTop(h)
        finally:
            if attached:
                u32.AttachThreadInput(me, fg_tid, False)

    def _wait_page_focus(self, hwnd) -> bool:
        """把瀏覽器視窗切到前景，等頁面真的拿到焦點（document.hasFocus）；切不過去再切一次。"""
        page = self._page
        for _ in range(2):
            self._activate_window(hwnd)
            page.bring_to_front()
            end = time.time() + 1.0
            while time.time() < end:
                try:
                    if self._iso_eval("document.hasFocus()"):
                        return True
                except Exception:
                    pass
                time.sleep(0.02)
        return False

    def _find_browser_hwnd(self) -> int:
        """找這個 Playwright 瀏覽器的頂層視窗：暫時把分頁標題改成唯一字串再比對視窗標題。

        內建 Chromium／系統 Chrome／Edge 都適用，也不會找到使用者自己開的 Gemini 分頁。
        找到後快取（v3.00）：改標題頁面看得到，不必每次點擊都改一次。視窗失效才重找。
        """
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        if self._hwnd and u32.IsWindow(self._hwnd):
            return self._hwnd
        page = self._page
        token = f"aa-send-{os.getpid()}-{time.time_ns()}"
        old = page.evaluate("document.title")
        page.evaluate("t => { document.title = t; }", token)
        found: list[int] = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(h, _l):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(h, buf, 256)
            if buf.value.startswith(token):
                found.append(h)
                return False
            return True

        try:
            end = time.time() + 3
            while not found and time.time() < end:
                u32.EnumWindows(proc(_cb), 0)
                if not found:
                    time.sleep(0.1)
        finally:
            try:
                page.evaluate("t => { document.title = t; }", old)
            except Exception:
                pass
        self._hwnd = found[0] if found else 0
        return self._hwnd

    # ── 獨立 JS 環境與座標換算 ──

    def _iso_eval(self, expression: str):
        """在獨立 JS 環境（isolated world）執行 expression 並回傳結果（v3.00）。

        與頁面共用 DOM、不共用 JS 全域：我們加的變數與事件監聽器頁面腳本看不到，
        頁面若改寫 addEventListener 等內建函式也攔不到我們的呼叫（實測）。頁面導向後
        舊環境失效 → 自動重建；CDP 不可用時退回 page.evaluate（頁面本身的環境）。
        expression 必須是運算式（函式請寫成立即執行的形式）。
        """
        last: Exception | None = None
        for _ in range(2):
            try:
                if self._cdp is None:
                    self._cdp = self._context.new_cdp_session(self._page)
                if self._iso_id is None:
                    tree = self._cdp.send("Page.getFrameTree")
                    self._iso_id = self._cdp.send("Page.createIsolatedWorld", {
                        "frameId": tree["frameTree"]["frame"]["id"],
                        "worldName": "aa_tool"})["executionContextId"]
                    self._dbg(f"建立獨立 JS 環境（#{self._iso_id}）")
                r = self._cdp.send("Runtime.evaluate", {
                    "expression": expression, "contextId": self._iso_id,
                    "returnByValue": True})
                if "exceptionDetails" in r:
                    raise GeminiWebError(
                        (r["exceptionDetails"].get("exception") or {}).get("description")
                        or r["exceptionDetails"].get("text") or "JS 例外")
                return (r.get("result") or {}).get("value")
            except Exception as e:  # noqa: BLE001 — 多半是頁面導向後環境失效，重建一次
                last = e
                self._iso_id = None
        self._dbg(f"獨立 JS 環境執行失敗，改用頁面環境：{brief_error(last)}")
        return self._page.evaluate(expression)

    def _box_of(self, role: str, loc):
        """元素在頁面上的位置 {x, y, width, height}；讀不到回 None。

        以 _find 命中的選擇器在獨立 JS 環境讀 getBoundingClientRect——不用 Playwright 的
        bounding_box()（會在頁面留下監聽器，見 _HEALTH_JS 註解）。選擇器不是標準 CSS
        （使用者自訂的 Playwright 專用語法）或讀不到時才退回 bounding_box()。
        """
        sel = self._matched_sel.get(role)
        if sel:
            try:
                r = self._iso_eval(
                    "(() => { const el = document.querySelector(" + json.dumps(sel) + ");"
                    " if (!el) return null; const r = el.getBoundingClientRect();"
                    " return {x: r.x, y: r.y, width: r.width, height: r.height}; })()")
                if r and r.get("width") and r.get("height"):
                    return r
            except Exception:  # noqa: BLE001
                pass
        self._dbg(f"獨立環境讀不到「{role}」的位置，改用 Playwright（頁面看得到痕跡）")
        return loc.bounding_box()

    def _client_to_screen(self, cx: float, cy: float) -> tuple[float, float, float]:
        """頁面座標 → 螢幕座標的估算值（尚未套用 _os_click_adjust 修正量），回傳 (x, y, dpr)。"""
        geo = self._iso_eval(
            "({sx: screenX, sy: screenY, ow: outerWidth, oh: outerHeight,"
            " iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio})")
        dpr = geo["dpr"] or 1
        border = (geo["ow"] - geo["iw"]) / 2
        return ((geo["sx"] + border + cx) * dpr,
                (geo["sy"] + geo["oh"] - geo["ih"] - border + cy) * dpr, dpr)

    # ── 擬人操作（send_method="human"，v3.00 實驗） ──

    def _human_compose_and_send(self, editor, text: str) -> None:
        """點輸入框 → 貼上 → 點送出，全程比照真人操作。

        與 v3.00 前「滑鼠點擊」（只把點送出鈕換成系統滑鼠，v3.01 移除）的差別
        （都是針對 Debug Log 統計與「手動操作不會被擋」的推測）：
        - 節奏：新對話載入完成後至少等 ``_HUMAN_WARMUP``、同對話上一則回覆後至少等
          ``_HUMAN_GAP`` 才開始（背景等待，不佔用滑鼠）；各步之間隨機停頓。
        - 焦點：瀏覽器叫到前景一次，整段做完才還原——原本點框、貼上、點送出各切一次，
          頁面看到的是「取得焦點→失去焦點」反覆跳動。
        - 滑鼠：沿曲線、由快而慢移動（頁面收到一連串 mousemove），停一下再按、按住約
          0.1 秒；目標點在按鈕中央附近隨機。新對話第一則送出前先在對話區隨意移動兩三下。
        - 鍵盤：Ctrl+V 按鍵間隔比照真人。
        任一步失敗就改用一般做法補上（程式點擊／逐字填入／程式送出），並記入 Log。
        """
        import ctypes
        from ctypes import wintypes
        rnd = random.Random()
        first = self._send_count == 1
        lo, hi = _HUMAN_WARMUP if first else _HUMAN_GAP
        base = self._page_ready_at if first else self._last_reply_at
        wait = rnd.uniform(lo, hi) - (time.time() - base)
        if wait > 0:
            self._dbg(f"擬人操作：{'新對話載入後' if first else '上一則回覆後'}再等 {wait:.1f}s")
            self._sleep_with_stop(wait)
        hwnd = self._find_browser_hwnd() if os.name == "nt" else 0
        if not hwnd:
            self._log("  （擬人操作需要 Windows 並找得到瀏覽器視窗，這次改用一般方式送出）")
            editor.click()
            self._fill_input(editor, text)
            time.sleep(_PRE_SEND_PAUSE)
            self._send()
            return
        u32 = ctypes.windll.user32
        prev_fg = u32.GetForegroundWindow()
        orig = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(orig))
        t_hold = time.time()
        try:
            if not self._wait_page_focus(hwnd):
                self._dbg("擬人操作：瀏覽器視窗沒有取得焦點，照樣嘗試")
            self._iso_eval(_MOVE_LISTENER_JS)
            if first:
                self._human_wander(rnd)
            if not self._human_click(editor, "輸入框", rnd, self._editor_focused,
                                     lambda: True, role="input"):
                self._log("  （擬人操作：點不到輸入框，改用程式點擊）")
                editor.click()
            time.sleep(rnd.uniform(*_HUMAN_PRE_PASTE))
            if not self._os_paste_into(editor, text, human=True):
                self._log("  （擬人操作：系統鍵盤貼上沒成功，改用逐字填入）")
                self._clear_input(editor)
                editor.fill(text)
            time.sleep(rnd.uniform(*_HUMAN_PASTE_PAUSE))
            btn = self._wait_send_enabled()
            if btn is None or not self._human_click(
                    btn, "送出鈕", rnd, self._sent_check,
                    lambda: self._still_enabled(btn), role="send"):
                self._log("  （擬人操作：點送出鈕沒成功，改用程式送出）")
                self._click_send()
        finally:
            self._dbg(f"擬人操作：佔用滑鼠與前景 {time.time() - t_hold:.1f}s")
            u32.SetCursorPos(orig.x, orig.y)
            if prev_fg and prev_fg != hwnd:
                self._activate_window(prev_fg)

    def _human_move_to(self, x: float, y: float, rnd: random.Random,
                       before_last=None) -> bool:
        """系統滑鼠沿擬人軌跡移到螢幕座標 (x, y)；途中使用者動了滑鼠就停下、回 False。

        before_last：最後一步之前呼叫（校正用：先清掉頁面回報，最後一步的回報才是終點）。
        最後一步一定落在與前一步不同的位置——Chrome 對「位置沒變」的移動不發 mousemove，
        游標本來就在終點上時先偏開 2 像素再移回。
        """
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        cur = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(cur))
        pts, step = _human_path(cur.x, cur.y, int(x), int(y), rnd)
        if len(pts) < 2:
            pts = [(int(x) - 2, int(y) + 1), (int(x), int(y))]
            step = step or 0.03
        last = (cur.x, cur.y)
        for i, (px, py) in enumerate(pts):
            now = wintypes.POINT()
            u32.GetCursorPos(ctypes.byref(now))
            if (now.x, now.y) != last:
                return False           # 游標不在上一步放的位置＝使用者動了滑鼠
            if before_last is not None and i == len(pts) - 1:
                before_last()
            u32.SetCursorPos(px, py)
            u32.GetCursorPos(ctypes.byref(now))
            last = (now.x, now.y)      # 以實際位置為準（螢幕邊界會被夾住）
            if step:
                time.sleep(step)
        return True

    def _human_wander(self, rnd: random.Random) -> None:
        """新對話第一則送出前：滑鼠在對話區隨意移動兩三下（不點擊），像人在看頁面。"""
        try:
            vw, vh = self._iso_eval("[innerWidth, innerHeight]")
        except Exception:  # noqa: BLE001
            return
        for _ in range(rnd.randint(2, 3)):
            x, y, _dpr = self._client_to_screen(vw * rnd.uniform(0.3, 0.7),
                                                vh * rnd.uniform(0.2, 0.55))
            self._human_move_to(x + self._os_click_adjust[0],
                                y + self._os_click_adjust[1], rnd)
            time.sleep(rnd.uniform(0.3, 0.9))

    def _human_click(self, loc, label: str, rnd: random.Random, done, can_retry,
                     role: str) -> bool:
        """沿擬人軌跡移到 ``loc`` 上（中央附近隨機一點）、停一下再點；``done()`` 為生效判定。

        對位：以頁面收到的 mousemove 座標校正（DPI 縮放、視窗邊框不必自己算準），差太多就再小幅
        移動修正（像人在微調），最多 5 次；對不準或點了沒生效回 False。
        """
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        box = self._box_of(role, loc)
        if not box:
            return False
        tx = box["x"] + box["width"] * rnd.uniform(0.38, 0.62)
        ty = box["y"] + box["height"] * rnd.uniform(0.38, 0.62)
        ex0, ey0, dpr = self._client_to_screen(tx, ty)
        px, py = ex0 + self._os_click_adjust[0], ey0 + self._os_click_adjust[1]
        hit = False
        got = None
        for _ in range(5):
            # 最後一步前清掉回報：之後收到的就是游標停在終點時頁面看到的座標
            if not self._human_move_to(px, py, rnd, before_last=lambda: self._iso_eval(
                    "window.__aaMove = null")):
                self._dbg(f"擬人操作：移向{label}途中滑鼠被移動，稍等後重來")
                time.sleep(0.6)
                continue
            # 視窗剛叫到前景時頁面第一次回報可能要將近 1 秒（實測 0.75 秒）
            wait_end = time.time() + 1.0
            got = None
            while got is None and time.time() < wait_end:
                time.sleep(0.01)
                got = self._iso_eval("window.__aaMove")
            if not got:
                continue
            cur = wintypes.POINT()
            u32.GetCursorPos(ctypes.byref(cur))
            if (cur.x, cur.y) != (int(px), int(py)):
                continue
            ex, ey = tx - got[0], ty - got[1]
            if abs(ex) <= box["width"] / 3 and abs(ey) <= box["height"] / 3:
                hit = True
                break
            px += ex * dpr
            py += ey * dpr
        self._dbg(f"擬人操作：{'對準' if hit else '對不準'}{label}"
                  f"（頁面座標 {got}，目標 {tx:.0f},{ty:.0f}）")
        if not hit:
            return False
        self._os_click_adjust = (px - ex0, py - ey0)
        for n in range(1, _OS_CLICK_TRIES + 1):
            time.sleep(rnd.uniform(*_HUMAN_HOVER))
            if not _send_mouse_click_at(int(px), int(py),
                                        hold=rnd.uniform(*_HUMAN_CLICK_HOLD)):
                self._dbg("擬人操作：SendInput 送不出去")
                return False
            end = time.time() + 1.5
            while time.time() < end:
                if done():
                    if n > 1:
                        self._dbg(f"擬人操作：第 {n} 次點{label}才生效")
                    return True
                time.sleep(0.05)
            if not can_retry():
                break
            self._dbg(f"擬人操作：點{label}後沒有生效，再點一次")
        return False

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
        t_start = time.time()
        start_deadline = time.time() + _GEN_NOT_STARTED_TIMEOUT
        started = False
        self._seen_toasts = set()
        toast_err_at = None
        while time.time() < start_deadline:
            self._check_abort()
            if self._find("stop") is not None or self._response_count() > prev_count:
                started = True
                break
            if self._poll_toasts() and toast_err_at is None:
                toast_err_at = time.time()
                self._debug_screenshot("toast_error")
            if toast_err_at and time.time() - toast_err_at >= _TOAST_ERROR_GRACE:
                break
            time.sleep(0.3)
        self._dbg(f"開始生成：{'是' if started else '否'}（{time.time() - t_start:.1f}s）")
        if not started:
            if toast_err_at:
                self._log(f"⚠️ Gemini 跳出錯誤後沒有開始生成（目前頁面：{self._page.url}），"
                          "訊息沒送出去")
            else:
                self._log(f"⚠️ 送出後 {_GEN_NOT_STARTED_TIMEOUT}s 仍未開始生成"
                          f"（目前頁面：{self._page.url}），訊息可能沒送出去")
            return False

        # 2) 等待結束：停止鈕消失 + 回覆文字連續數次不變
        gen_deadline = time.time() + _GEN_TIMEOUT
        last_text = None
        stable = 0
        next_snap = time.time() + _DEBUG_GEN_EVERY
        while time.time() < gen_deadline:
            self._check_abort()
            self._poll_toasts()
            t_poll = time.time()
            generating = self._find("stop") is not None
            text = self._latest_response_text()
            if self._debug is not None:
                # 輪詢本身變慢（讀 DOM 要好幾秒）＝頁面開始卡
                poll = time.time() - t_poll
                if poll > 2 or time.time() >= next_snap:
                    next_snap = time.time() + _DEBUG_GEN_EVERY
                    self._dbg(f"生成中快照：停止鈕={'有' if generating else '無'}"
                              f"、回覆 {len(text)} 字、穩定 {stable}、"
                              f"這次讀取 DOM 花 {poll:.1f}s")
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

    def _poll_toasts(self) -> bool:
        """讀頁面左下角提示，新出現的寫進 Log。新提示含錯誤字樣時回 True。"""
        try:
            texts = self._iso_eval(f"({_TOAST_JS})({json.dumps(_TOAST_SEL)})")
        except Exception:  # noqa: BLE001 — 頁面導向中等情況讀不到就算了
            return False
        err = False
        for t in texts:
            t = " ".join(str(t).split())
            if t in self._seen_toasts:
                continue
            self._seen_toasts.add(t)
            self._log(f"⚠️ Gemini 頁面提示：「{t}」（目前頁面：{self._page.url}）")
            if _TOAST_ERROR_RE.search(t):
                err = True
        return err

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
