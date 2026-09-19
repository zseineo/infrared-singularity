"""連續多話自動翻譯協調器。

把原本的人工五步流程 ——「提取原文 → 貼到網頁版 Gemini → 貼回翻譯 →
替換並存檔 → 按下一話」—— 串成全自動流程，連續翻譯 N 話。

翻譯這一步由 :mod:`aa_tool.gemini_web` 操控網頁版 Gemini 的 Gem 完成；
其餘步驟全部重用 ``aa_tool/`` 內既有的純函式。

用法（CLI）::

    python aa_auto_translate.py --url <起始網址> --count 5 --out <輸出資料夾>

    # 依作品名在輸出資料夾下開一層子資料夾存放（整批共用同一個）
    python aa_auto_translate.py --url <起始網址> --count 5 --out <輸出資料夾> \
        --group-by-series

也可由 aa_main_qt 的 GUI 按鈕呼叫 :func:`run_auto_translate`。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from aa_tool import app_paths, constants, html_io, original_cache
from aa_tool import settings_manager
from aa_tool import text_extraction, translation_engine, url_fetcher
from aa_tool.gemini_web import (
    ERROR_POLICY_DEFAULTS, policy_choice_label,
    GeminiAborted, GeminiBusyRetriesExhausted,
    GeminiContentBlocked, GeminiModelMismatch, GeminiQuotaExceeded,
    GeminiResponseTruncated, GeminiStuck, GeminiWebError, GeminiWebSession,
    resolve_error_policy,
)

# 單次送給 Gemini 的最大提取行數；超過則分段送出後合併。
# 800 是經驗值：實務上短於這個長度都不需分段；切點固定在「整行」邊界，
# 永遠不會把單一 ID 切開（chunks 由 lines 切片組成）。
MAX_LINES_PER_REQUEST = 800

# 審查偵測：原文一定行數以上，但回覆極短且幾乎沒有 ID|文 結構 → 視為被審查。
_CENSOR_REPLY_MAX_LINES = 4
_CENSOR_SOURCE_MIN_LINES = 4
_ID_LINE_RE = re.compile(r'^\s*\d+-\d+\s*\|')

# 罐頭拒絕語（v2.48）。實際回報：生成到快完成時，伺服器端把整段回覆抽換成
# 「大規模言語モデルとして私はまだ学習中であり、そちらには対応できません。」
# 這類一句話拒絕。**只有在回覆幾乎沒有 ID|文 結構時才會拿來比對**（正常譯文
# 每行都是 `ID|文`，劇情裡出現同樣字眼不會誤判），所以片語可以列得寬一點。
_REFUSAL_RE = re.compile(
    "|".join([
        "大規模言語モデル", "言語モデルとして", "まだ学習中",
        "対応できません", "お答えできません", "お手伝いできません",
        "大型語言模型", "大型语言模型", "語言模型", "语言模型",
        "還在學習", "还在学习", "無法協助", "无法协助", "無法回應", "无法回应",
        "i'm a language model", "i am a language model",
        "large language model", "as an ai",
        "i can't help with that", "i cannot help with that",
        "i'm not able to help", "i'm unable to",
    ]),
    re.IGNORECASE,
)

# 罐頭拒絕／極短回覆的重送策略（v2.48）：伺服器端的輸出過濾多半有隨機性，
# 換個對話重送常常就過了；連兩次不行才懷疑是「這段輸出太長」，對半拆開送。
_CENSOR_RETRIES = 2            # 同一段最多再重送幾次（每次都先開新對話）
_CENSOR_RETRY_WAIT = 20.0      # 重送前先等幾秒（連續送出容易再被攔）
_CENSOR_HALF_RETRIES = 1       # 對半拆之後，每半段最多再重送幾次
_CENSOR_SPLIT_MIN_LINES = 40   # 少於這個行數就不再拆（拆了也無濟於事）

# 回覆格式檢查（v2.50）：AI 有時不理會 prompt，改回一篇內容摘要（「やる夫スレの
# ログデータですね。登場人物…」）。這種回覆 apply_translation 一行也替換不到，會
# 存出一份沒翻譯的檔案，所以要當成翻譯失敗。
# ① 格式：回覆中符合 `ID|文` 的行數佔比低於此值 → 視為根本不是譯文。
_FORMAT_MIN_RATIO = 0.5
# ② 行數：譯文的 ID 行數 ÷ 送出行數 低於此值 → 視為漏翻太多（少數行被 AI 合併或
#    漏掉屬常見，抓 0.8 讓正常翻譯不會誤判）。
_LINE_KEEP_MIN_RATIO = 0.8
# 兩項檢查的送出行數下限：太短的段落本來就容易整合成幾行，不做判定。
_REPLY_CHECK_MIN_LINES = 5
# 選「稍後重試」時，同一話最多排進待補翻列表幾次；超過就認賠跳過。
# 沒有這個上限的話，若某一話每次都被回摘要，補翻階段（新的話都跑完後）會在
# 這一話上無限重試——伺服器忙碌那條路每次要等 35 分鐘，這條卻是馬上重送。
_MALFORMED_MAX_RETRIES = 3

# 未翻譯偵測：可比對的 ID 中，譯文與原文「完全相同」的比例 ≥ 此值 → 視為沒翻譯。
_UNTRANSLATED_RATIO = 0.9
# 可比對 ID 數少於此值時不做未翻譯判定（樣本太少容易誤判，交給其他檢查）。
_UNTRANSLATED_MIN_IDS = 3

_URL_CACHE_DIR = os.path.join(tempfile.gettempdir(), "aa_url_cache")

# 存檔後的 HTML 小於此大小即視為異常（實際一話不可能這麼小）：刪檔、記失敗、續下一話。
_MIN_OUTPUT_BYTES = 5 * 1024

# 進階設定「抓取網頁失敗＝重試」時的重抓節奏（與 API 伺服器忙碌重試一致）：
# 每次等 90 秒，每滿 5 次多等 10 分鐘，最多 10 次；仍失敗才中斷整批。
_FETCH_RETRY_WAIT = 90.0
_FETCH_LONG_WAIT = 600.0
_FETCH_LONG_WAIT_EVERY = 5
_FETCH_MAX_RETRIES = 10

# 進階設定各項目的中文名稱（Log 顯示「與預設不同」的項目用；面板另有完整說明）
ERROR_POLICY_LABELS = {
    "api_5xx": "API 伺服器忙碌（5xx）",
    "api_timeout": "API 逾時",
    "api_conn_after_ok": "API 連線中斷（已成功過）",
    "api_conn_first": "API 連線失敗（還沒成功過）",
    "api_4xx": "API HTTP 4xx 錯誤",
    "api_empty": "API 空回應",
    "web_stuck": "瀏覽器 Gemini 卡住",
    "web_censored": "回覆被換成拒絕語",
    "reply_format": "回覆格式不符",
    "reply_lines": "譯文行數少太多",
    "fetch_fail": "抓取網頁失敗",
}


# ── 例外 ──

class ChapterError(RuntimeError):
    """單一話處理失敗（抓取/解析/提取/替換等）。"""


class FetchFailed(ChapterError):
    """抓取網頁本身失敗（連線／HTTP），可依進階設定重抓；解析失敗不在此列。"""


class StopRequested(RuntimeError):
    """使用者按下停止鈕，要求中止整批流程。"""


class CensoredResponse(RuntimeError):
    """偵測到 Gemini 回覆疑似被審查（極短且非翻譯格式），該話跳過。"""


class UntranslatedResponse(RuntimeError):
    """偵測到回覆與原文幾乎一致（疑似沒翻譯，只是把原文吐回來），該話跳過。"""


class MalformedResponse(RuntimeError):
    """回覆不是可用的譯文：格式不符（不是 ``ID|譯文``）或行數少太多。

    典型情況是 AI 不理會 prompt，改成回一篇「這是やる夫スレ的記錄，重點整理如下…」
    的內容摘要。這種回覆丟進 `apply_translation` 一行也替換不到，會存出一份沒翻譯
    的檔案，所以必須當成翻譯失敗、不存檔（進階設定可選跳過該話或排進補翻列表）。
    """


class OutputTooSmall(RuntimeError):
    """存檔後檔案小於 `_MIN_OUTPUT_BYTES`（實際一話不可能這麼小）→ 已刪檔，該話跳過。"""


class OutputKeywordHit(RuntimeError):
    """譯文出現使用者設定的關鍵字，且動作為「停止」或「跳過」（皆不存檔）。

    `action` 為 "stop"／"skip"；「暫停」不丟例外（先存檔再原地等使用者按繼續）。
    """

    def __init__(self, action: str, message: str) -> None:
        super().__init__(message)
        self.action = action


# ── 設定載入 ──

@dataclass
class AutoConfig:
    """從 AA_Settings.json / aa_settings_cache.json 載入的提取＋替換參數。"""
    base_regex: str
    invalid_regex: str
    symbol_regex: str
    filter_text: str
    glossary: dict
    author_name: str
    author_only: bool
    korean_mode: bool
    experimental: bool
    pad_right_aa: bool
    glossary_avoid_aa: bool
    glossary_skip_extract: bool
    work_title: str


def load_config(base_dir: str) -> AutoConfig:
    """載入提取與替換所需的設定，與 aa_main_qt 的行為一致。"""
    sm = settings_manager.SettingsManager(base_dir)
    s = sm.load_settings()
    c = sm.load_cache()
    glossary_text = "\n".join(p for p in [s.glossary, s.glossary_temp] if p)
    # 韓文模式改用韓文字元集（對齊 aa_main_qt._active_base_regex）
    base = constants.DEFAULT_BASE_REGEX_KO if c.korean_mode else s.base_regex
    return AutoConfig(
        base_regex=base,
        invalid_regex=s.invalid_regex,
        symbol_regex=s.symbol_regex,
        filter_text=s.filter_text,
        glossary=translation_engine.parse_glossary(
            glossary_text, kana_fold=c.glossary_kana_fold),
        author_name=c.author_name,
        author_only=c.author_only,
        korean_mode=c.korean_mode,
        experimental=c.experimental_extraction,
        pad_right_aa=c.pad_right_aa,
        glossary_avoid_aa=c.glossary_avoid_aa,
        glossary_skip_extract=c.glossary_skip_extract,
        work_title=c.doc_title,
    )


# ── 結果 ──

@dataclass
class AutoResult:
    """一次自動翻譯批次的總結。"""
    done: list = field(default_factory=list)        # [輸出檔路徑, ...]
    failed: list = field(default_factory=list)      # [(url, 原因), ...]
    skipped: list = field(default_factory=list)     # [(url, 檔名), ...] 已存在同名檔而跳過
    filtered: list = field(default_factory=list)    # [(url, 頁面標題), ...] 標題不含過濾文字而跳過
    title_filter_stop: str = ""                     # 連續太多話不符標題過濾而中止時的說明
    quota_paused: bool = False                      # 是否因額度上限暫停
    pending_url: str = ""                           # 暫停／停止時未完成的話網址
    next_url: str = ""                              # 自然跑滿話數後的下一話續接網址
    remaining: int = 0                              # 尚未處理的話數
    stopped: bool = False                           # 是否被使用者手動停止
    reached_end: bool = False                       # 是否因為沒有下一話而結束
    model_mismatch: bool = False                    # 是否因模型與要求不符而中止
    keyword_stop: str = ""                          # 譯文出現「停止」關鍵字而中止時的說明
    titles: dict = field(default_factory=dict)      # {網址: 該網址讀取到的名稱（頁面標題）}


# ── URL 快取（沿用 aa_main_qt 的 %TEMP%/aa_url_cache/<md5>.html 格式）──

def _url_cache_path(url: str) -> str:
    h = hashlib.md5(url.encode("utf-8")).hexdigest()
    return os.path.join(_URL_CACHE_DIR, f"{h}.html")


def _read_url_cache(url: str) -> str | None:
    path = _url_cache_path(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _write_url_cache(url: str, page_html: str) -> None:
    try:
        os.makedirs(_URL_CACHE_DIR, exist_ok=True)
        with open(_url_cache_path(url), "w", encoding="utf-8") as f:
            f.write(page_html)
    except OSError:
        pass


# ── 流程步驟 ──

def _fetch_and_parse(url: str, cfg: AutoConfig, *,
                     skip_cache: bool = False) -> tuple[str, list, str, str]:
    """抓網頁並解析，回傳 (帶標題前綴的完整 source, 關聯連結, 頁面標題, display_title)。

    為使 ID 行號與手動流程一致：手動流程在網址讀取成功後會在文字前面
    prepend ``display_title + "\\n\\n"``（見 aa_main_qt.py 約 1741-1744 行），
    導致所有行號 +2。自動流程也得照辦，否則同一句話會被指派到不同行號的 ID。

    skip_cache：True 時不吃 %TEMP%/aa_url_cache 的內容，一律重新上網抓（抓到仍
        回寫暫存）。對應主畫面「不讀暫存」開關。
    """
    page_html = None if skip_cache else _read_url_cache(url)
    if page_html is None:
        try:
            page_html = url_fetcher.fetch_url(url)
        except Exception as e:
            # 連線層失敗時 url_fetcher 會附上自動診斷（DNS／TCP／TLS／憑證簽發者
            # ／對照站台／Proxy），一併帶進訊息讓它出現在面板 Log，回報者不必
            # 自己跑任何指令就看得到原因。
            detail = getattr(e, "diagnosis", None)
            msg = f"抓取網頁失敗：{e}"
            if detail:
                msg += chr(10) + chr(10).join(detail)
            raise FetchFailed(msg) from e
        _write_url_cache(url, page_html)
    try:
        text_content, nav_links, page_title = url_fetcher.parse_page_html(
            page_html, url,
            author_name=cfg.author_name, author_only=cfg.author_only)
    except Exception as e:
        raise ChapterError(f"解析頁面失敗：{e}") from e
    if not text_content or not text_content.strip():
        raise ChapterError("找不到內文（解析後內容為空）")
    display_title = (text_extraction.extract_work_title(page_title)
                     if page_title else "")
    source = (display_title + "\n\n" + text_content
              if display_title else text_content)
    return source, nav_links, page_title, display_title


def _extract(source: str, display_title: str, cfg: AutoConfig) -> str:
    """提取原文，回傳 'ID|原文' 格式字串。"""
    extracted_list = text_extraction.extract_text(
        source, cfg.base_regex, cfg.invalid_regex, cfg.symbol_regex,
        cfg.filter_text, skip_title=display_title, author_name=cfg.author_name,
        korean_mode=cfg.korean_mode, experimental=cfg.experimental,
        work_title=cfg.work_title)
    single = text_extraction.extract_single_kana(source, cfg.filter_text)
    seen = set(extracted_list)
    for item in single:
        if item not in seen:
            extracted_list.append(item)
            seen.add(item)
    if cfg.glossary_skip_extract and cfg.glossary:
        glossary_keys = set(cfg.glossary.keys())
        extracted_list = [
            item for item in extracted_list if item[0] not in glossary_keys]
    extracted = text_extraction.format_extraction_output(extracted_list)
    if not extracted.strip():
        raise ChapterError("提取結果為空（沒有可翻譯的文字）")
    return extracted


def _looks_censored(extracted_chunk: str, reply: str) -> bool:
    """檢查回覆是否疑似被審查（含伺服器端把回覆抽換成罐頭拒絕語）。

    先看有沒有 ``ID|文`` 結構：有兩行以上就是正常譯文，一律不判定（這道前置
    條件讓下面兩種判定都不會誤殺正常翻譯，即使劇情裡剛好有「対応できません」
    之類的台詞）。接著：

    - 回覆含罐頭拒絕語（`_REFUSAL_RE`）→ 不看行數一律視為被審查（v2.48；
      伺服器有時會在拒絕語前後多加幾行說明，原本的「≤4 行」會漏掉）。
    - 否則沿用舊規則：原文 ≥ `_CENSOR_SOURCE_MIN_LINES` 行但回覆 ≤
      `_CENSOR_REPLY_MAX_LINES` 行 → 視為被審查。
    """
    sent = [l for l in extracted_chunk.split("\n") if l.strip()]
    reply_lines = [l for l in reply.split("\n") if l.strip()]
    matched = sum(1 for l in reply_lines if _ID_LINE_RE.match(l))
    if matched > 1:
        return False
    if _REFUSAL_RE.search(reply or ""):
        return True
    if len(sent) < _CENSOR_SOURCE_MIN_LINES:
        return False
    return len(reply_lines) <= _CENSOR_REPLY_MAX_LINES


def _count_id_lines(text: str) -> tuple[int, int]:
    """回傳 (符合 ``ID|文`` 格式的行數, 非空白行數)。"""
    lines = [l for l in (text or "").split("\n") if l.strip()]
    return sum(1 for l in lines if _ID_LINE_RE.match(l)), len(lines)


def _format_ratio(reply: str) -> float:
    """回覆中「``ID|譯文`` 格式」的行數佔比（0～1）；沒有任何非空行回 0。"""
    matched, total = _count_id_lines(reply)
    return matched / total if total else 0.0


def _line_keep_ratio(sent: str, reply: str) -> float:
    """譯文的 ID 行數 ÷ 送出的行數（0～1）；送出為空回 1（無從判斷，不擋）。"""
    matched, _ = _count_id_lines(reply)
    total_sent = len([l for l in (sent or "").split("\n") if l.strip()])
    return matched / total_sent if total_sent else 1.0


def _parse_id_map(text: str) -> dict[str, str]:
    """把 'ID|文字' 每行解析成 {ID: 文字}。"""
    out: dict[str, str] = {}
    for line in text.split("\n"):
        if "|" in line:
            k, v = line.split("|", 1)
            out[k.strip()] = v.strip()
    return out


def _looks_untranslated(extracted: str, translated: str) -> bool:
    """回覆是否「幾乎等於原文」（疑似沒翻譯，只把原文吐回來）。

    以 ID 對齊比對：可比對的 ID 中，譯文與原文完全相同的比例 ≥
    `_UNTRANSLATED_RATIO` 即視為未翻譯。可比對 ID 太少則不判定（回 False）。
    """
    orig = _parse_id_map(extracted)
    trans = _parse_id_map(translated)
    common = [k for k in orig if k in trans]
    if len(common) < _UNTRANSLATED_MIN_IDS:
        return False
    same = sum(1 for k in common if orig[k] == trans[k])
    return same / len(common) >= _UNTRANSLATED_RATIO


def parse_mask_words(text: str) -> list[str]:
    """過濾詞清單原始文字 → 詞列表（一行一個、去空白與重複；長的排前面）。

    長詞優先替換，避免短詞先把長詞的一部分換掉而漏換（例如同時有「殺」「殺す」）。
    """
    words = {ln.strip() for ln in (text or "").splitlines() if ln.strip()}
    return sorted(words, key=len, reverse=True)


def mask_words(extracted: str, words: list[str]) -> tuple[str, int]:
    """把 'ID|原文' 各行原文部分出現的過濾詞換成等長的 ○，回傳 (新文字, 替換處數)。

    只動 ``|`` 右側的原文，ID 不動（ID 對齊與未翻譯偵測都靠它）。只影響送給 AI
    的文字——替換回原文件時仍用未遮蔽的提取結果定位（見 run_auto_translate）。
    """
    if not words:
        return extracted, 0
    total = 0
    out: list[str] = []
    for line in extracted.split("\n"):
        if "|" in line:
            head, body = line.split("|", 1)
            for w in words:
                n = body.count(w)
                if n:
                    body = body.replace(w, "○" * len(w))
                    total += n
            line = head + "|" + body
        out.append(line)
    return "\n".join(out), total


# 譯文關鍵字檢查的動作：pause＝先存檔、原地等使用者按繼續；stop＝不存檔、結束整批；
# skip＝不存檔、記入失敗、續下一話。
OUTPUT_KEYWORD_ACTIONS = {"pause": "暫停", "stop": "停止", "skip": "跳過"}
# 同一話命中多個動作時的優先順序：停止 > 跳過 > 暫停（跳過＝「這話不要存」，
# 必須優先於會先存檔的暫停）。
_OUTPUT_KEYWORD_PRIORITY = ("stop", "skip", "pause")


def parse_output_keyword_rules(rules) -> list[tuple[str, str]]:
    """設定值 [{"word": 詞, "action": 動作}, ...] → [(詞, 動作), ...]。

    去掉空詞與首尾空白；同一個詞重複時以後面的設定為準；未知動作視為 pause。
    """
    out: dict[str, str] = {}
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        word = str(r.get("word", "")).strip()
        if not word:
            continue
        action = str(r.get("action", "pause"))
        out[word] = action if action in OUTPUT_KEYWORD_ACTIONS else "pause"
    return list(out.items())


def find_output_keywords(translated: str,
                         rules: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """找出譯文中出現的關鍵字，回傳 [(詞, 動作, 第一個命中的那一行), ...]。

    直接比對文字（非正則），整份回覆逐行找（含 AI 回的拒絕語等非 ID 行）。
    """
    lines = translated.split("\n")
    hits: list[tuple[str, str, str]] = []
    for word, action in rules:
        line = next((ln for ln in lines if word in ln), None)
        if line is not None:
            hits.append((word, action, line.strip()))
    return hits


def _keyword_hits_text(hits: list[tuple[str, str, str]]) -> str:
    """命中清單 → 「「詞」（動作）」以頓號串接。"""
    return "、".join(f"「{w}」（{OUTPUT_KEYWORD_ACTIONS[a]}）" for w, a, _ in hits)


def _send_chunk(session: GeminiWebSession, chunk_lines: list[str], label: str,
                log: Callable[[str], None], stop_event, stuck_retry: bool,
                *, retries: int, allow_split: bool) -> str:
    """送出一段並確認拿到的是譯文；被吞成罐頭拒絕就重送，再不行就對半拆。

    伺服器端的輸出過濾（回覆生成到一半被整段抽換成「大規模言語モデルとして…
    対応できません」）**有隨機性**，而且被吞的那則回覆會留在對話脈絡裡影響後續，
    所以每次重送前都 `start_new_session()` 開新對話並等 `_CENSOR_RETRY_WAIT` 秒。
    重送 `retries` 次都不行，才改判「這段輸出太長容易被攔」，對半拆成兩段分別送
    （合起來仍是同一段的完整譯文）。全部失敗才丟 `CensoredResponse`（跳過該話）。

    最壞情況的送出次數：(1+_CENSOR_RETRIES) + 2×(1+_CENSOR_HALF_RETRIES)。

    `retries=0, allow_split=False`（進階設定「回覆被換成拒絕語＝跳過這一話」，
    **預設**）時完全不重送，命中就丟 `CensoredResponse`＝v2.47 以前的行為。
    """
    text = "\n".join(chunk_lines)
    for attempt in range(1, retries + 2):
        if stop_event is not None and stop_event.is_set():
            raise StopRequested()
        try:
            reply = session.translate(text)
        except GeminiStuck as e:
            if not stuck_retry:
                raise
            raise GeminiBusyRetriesExhausted(f"{e}（進階設定：重試）") from e
        if not _looks_censored(text, reply):
            if attempt > 1:
                log(f"  ✅ {label} 重送後取得正常譯文。")
            return reply.strip()
        first = (reply.strip().splitlines() or [""])[0][:60]
        log(f"  🚫 {label} 的回覆被抽換成拒絕語／極短回覆"
            f"（第 {attempt} 次）：{first}")
        if attempt > retries:
            break
        log(f"  🔁 開新對話後等 {int(_CENSOR_RETRY_WAIT)} 秒再重送一次"
            "（被吞的回覆會留在對話脈絡裡，同一個對話重送多半一樣）…")
        session.start_new_session()
        if stop_event is not None:
            if stop_event.wait(_CENSOR_RETRY_WAIT):
                raise StopRequested()
        else:
            time.sleep(_CENSOR_RETRY_WAIT)

    if allow_split and len(chunk_lines) >= _CENSOR_SPLIT_MIN_LINES:
        mid = len(chunk_lines) // 2
        log(f"  ✂️ 重送都被擋 → 改成對半拆（{mid} + {len(chunk_lines) - mid} 行）"
            "分開送：回覆愈長愈容易在快完成時被攔掉。")
        session.start_new_session()
        first_half = _send_chunk(
            session, chunk_lines[:mid], f"{label} 前半", log, stop_event,
            stuck_retry, retries=_CENSOR_HALF_RETRIES, allow_split=False)
        second_half = _send_chunk(
            session, chunk_lines[mid:], f"{label} 後半", log, stop_event,
            stuck_retry, retries=_CENSOR_HALF_RETRIES, allow_split=False)
        return (first_half + "\n" + second_half).strip()

    tried = (f"；已開新對話重送 {retries} 次"
             + ("＋對半拆開送" if allow_split else "") + "仍相同") if retries else ""
    raise CensoredResponse(
        f"{label} 回覆極短且非翻譯格式（疑似被審查）{tried}")


def _check_reply_usable(sent: str, reply: str, policy: dict,
                        log: Callable[[str], None],
                        retries_done: int = 0) -> None:
    """譯文能不能用：格式是不是 ``ID|譯文``、行數有沒有少太多。不能用就丟例外。

    依進階設定決定丟哪一種（兩項各自獨立設定）：
      - 「跳過這一話」→ `MalformedResponse`：記入失敗清單、不存檔、續下一話。
      - 「稍後重試」→ `GeminiBusyRetriesExhausted`：排進待補翻列表，等下一話翻譯
        成功（代表 AI 恢復正常）後再補翻這一話——隔一段時間再試比當場重送有意義，
        因為 AI 不照 prompt 多半是整個對話已經歪掉。

    送出行數少於 `_REPLY_CHECK_MIN_LINES` 時兩項都不判定（樣本太少容易誤判）。
    """
    sent_lines = len([l for l in (sent or "").split("\n") if l.strip()])
    if sent_lines < _REPLY_CHECK_MIN_LINES:
        return

    def _fail(key: str, msg: str) -> None:
        if (policy.get(key, ERROR_POLICY_DEFAULTS[key]) == "retry"
                and retries_done < _MALFORMED_MAX_RETRIES):
            log(f"  ⚠️ {msg} → 之後再補翻這一話（進階設定：稍後重試）")
            raise GeminiBusyRetriesExhausted(msg + "（進階設定：稍後重試）")
        if retries_done:
            msg += f"（已補翻重試 {retries_done} 次仍相同）"
        raise MalformedResponse(msg)

    matched, total = _count_id_lines(reply)
    ratio = matched / total if total else 0.0
    if ratio < _FORMAT_MIN_RATIO:
        _fail("reply_format",
              f"回覆不是「ID|譯文」格式（{total} 行中只有 {matched} 行符合，"
              "疑似 AI 沒照 prompt、改回了內容摘要）")

    keep = matched / sent_lines
    if keep < _LINE_KEEP_MIN_RATIO:
        _fail("reply_lines",
              f"譯文行數比原文少太多（送出 {sent_lines} 行、回來只有 {matched} 行＝"
              f"{keep:.0%}，低於 {_LINE_KEEP_MIN_RATIO:.0%}）")


def _translate(session: GeminiWebSession, extracted: str,
               log: Callable[[str], None], stop_event=None,
               stuck_retry: bool = False, censor_retry: bool = False) -> str:
    """送 Gemini 翻譯；行數過多時分段送出後合併。

    GeminiQuotaExceeded 直接往外拋（呼叫端暫停整批）。
    每段送出前檢查 stop_event，已設定則丟 StopRequested。
    stuck_retry：進階設定「瀏覽器 Gemini 卡住＝重試」時為 True——把 GeminiStuck
    轉成 GeminiBusyRetriesExhausted，讓協調器暫時跳過該話、之後補翻（卡住本身已
    等過 10 分鐘並開新對話重送過一次）；False 時照舊往外拋（中斷整批）。
    """
    lines = [l for l in extracted.split("\n") if l.strip()]
    chunks = [lines[i:i + MAX_LINES_PER_REQUEST]
              for i in range(0, len(lines), MAX_LINES_PER_REQUEST)]
    parts: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        if stop_event is not None and stop_event.is_set():
            raise StopRequested()
        if len(chunks) > 1:
            log(f"  翻譯分段 {idx}/{len(chunks)}（{len(chunk)} 行）")
        parts.append(_send_chunk(
            session, chunk, f"分段 {idx}/{len(chunks)}", log, stop_event,
            stuck_retry,
            retries=_CENSOR_RETRIES if censor_retry else 0,
            allow_split=censor_retry))
    return "\n".join(parts)


_INVALID_FN_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize(text: str) -> str:
    return _INVALID_FN_CHARS.sub("_", (text or "").strip())[:80].strip()


def _unique_path(out_dir: str, name_base: str) -> str:
    """同名衝突時加 dash 序號（``name_base``→``name_base-2``→``name_base-3``…）後回傳完整路徑。

    第一個檔案不加任何後綴（例：``Title_34.html``），第二個起以 ``-N`` 區分
    （例：``Title_34-2.html``），這樣與作品中常見的「34、34-2」並列習慣一致。
    """
    path = os.path.join(out_dir, f"{name_base}.html")
    if not os.path.exists(path):
        return path
    i = 2
    while True:
        candidate = os.path.join(out_dir, f"{name_base}-{i}.html")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def compute_chapter_name_base(
    *,
    doc_title: str,
    fetch_auto_fill_title: bool,
    source: str,
    page_title: str,
    fallback_index: int,
) -> str:
    """算這一話的檔名主體（不含 ``.html`` 副檔名、也不含同名衝突序號）。

    命名規則與主畫面 ``_prepare_translation`` 一致：
    - ``fetch_auto_fill_title=True``（自動填入模式）：title 由 ``extract_work_title``
      從每話的 ``page_title`` 萃取；話數欄位被視為空，檔名只用 title。
    - ``fetch_auto_fill_title=False``（手動模式）：title 用使用者填的 ``doc_title``；
      話數由 ``check_chapter_number`` 從每話原文偵測（找不到時不附加），主體為
      ``{title}_{num}`` 或 ``{title}``。

    「跳過已存在同名檔」的判定與實際存檔的檔名計算共用此主體，避免兩處命名規則走鐘。
    """
    if fetch_auto_fill_title:
        auto_title = (text_extraction.extract_work_title(page_title)
                      if page_title else "")
        title = auto_title or page_title or f"chapter_{fallback_index}"
        num = ""
    else:
        title = (doc_title or "").strip() or "未命名"
        detected = text_extraction.check_chapter_number((source or "")[:200])
        num = str(detected) if detected is not None else ""
    safe_title = _sanitize(title) or f"chapter_{fallback_index}"
    safe_num = _sanitize(num)
    return f"{safe_title}_{safe_num}" if safe_num else safe_title


def compute_series_folder_name(
    *,
    doc_title: str,
    fetch_auto_fill_title: bool,
    page_title: str,
) -> str:
    """算「依作品名分資料夾」要用的資料夾名（已 sanitize），算不出時回空字串。

    - 手動模式（``fetch_auto_fill_title=False``）：直接用使用者填的 ``doc_title``，
      不做任何猜測——那本來就是作品名稱欄位。
    - 自動模式：由 ``page_title`` 經 ``extract_series_folder_name`` 收斂成作品名
      主體（**去掉話數**）。注意不可改用 ``extract_work_title``：它保留話數，
      每話結果都不同，會變成一話一個資料夾。

    整批只算一次（見 ``run_auto_translate``），故同一批的 N 話必落在同一資料夾。
    """
    if fetch_auto_fill_title:
        name = text_extraction.extract_series_folder_name(page_title or "")
    else:
        name = (doc_title or "").strip()
    return _sanitize(name)


def compute_chapter_filename(
    out_dir: str,
    *,
    doc_title: str,
    fetch_auto_fill_title: bool,
    source: str,
    page_title: str,
    fallback_index: int,
) -> str:
    """每話實際落地檔名（含碰撞序號），命名主體見 ``compute_chapter_name_base``。

    同名衝突一律加序號 ``-2``、``-3`` 等（見 ``_unique_path``）。
    """
    name_base = compute_chapter_name_base(
        doc_title=doc_title, fetch_auto_fill_title=fetch_auto_fill_title,
        source=source, page_title=page_title, fallback_index=fallback_index)
    return _unique_path(out_dir, name_base)


def _preview_fetch_source(
    url: str, base_dir: str, allow_network: bool,
) -> tuple[str, str] | None:
    """預覽用的抓取＋解析，回傳 ``(source, page_title)``，失敗一律回 None。

    - `allow_network=False`：只吃本地 URL 快取，沒命中回 None（不卡網路）。
    - fetch/parse 失敗或內文為空皆回 None（預覽不該丟例外）。

    `preview_first_filename`（檔名）與 `preview_series_folder`（資料夾名）共用，
    面板重算一次時兩者走同一份快取、不會抓兩次網頁。
    """
    if not url:
        return None
    cfg = load_config(base_dir)
    page_html = _read_url_cache(url)
    if page_html is None:
        if not allow_network:
            return None
        try:
            page_html = url_fetcher.fetch_url(url)
        except Exception:
            return None
        _write_url_cache(url, page_html)
    try:
        text_content, _nav, page_title = url_fetcher.parse_page_html(
            page_html, url,
            author_name=cfg.author_name, author_only=cfg.author_only)
    except Exception:
        return None
    if not text_content or not text_content.strip():
        return None
    display_title = (text_extraction.extract_work_title(page_title)
                     if page_title else "")
    source = (display_title + "\n\n" + text_content
              if display_title else text_content)
    return source, page_title


def preview_first_filename(
    out_dir: str,
    url: str,
    *,
    base_dir: str | None = None,
    doc_title: str = "",
    fetch_auto_fill_title: bool | None = None,
    allow_network: bool = True,
) -> str | None:
    """試算 ``url`` 這一話實際會寫入的檔名（含碰撞序號），不翻譯、不寫檔。

    供面板「檔名」列即時顯示真正會落地的檔名用。
    - `allow_network=False`：只吃本地 URL 快取，沒命中回 None（不卡網路，
      適合面板開啟時的即時預覽）。
    - fetch/parse 失敗或內文為空一律回 None（預覽不該丟例外）。
    """
    if not url:
        return None
    base_dir = base_dir or app_paths.data_dir()
    if fetch_auto_fill_title is None:
        fetch_auto_fill_title = settings_manager.SettingsManager(
            base_dir).load_cache().fetch_auto_fill_title
    fetched = _preview_fetch_source(url, base_dir, allow_network)
    if fetched is None:
        return None
    source, page_title = fetched
    path = compute_chapter_filename(
        out_dir or ".", doc_title=doc_title,
        fetch_auto_fill_title=fetch_auto_fill_title,
        source=source, page_title=page_title, fallback_index=1)
    return os.path.basename(path)


def preview_series_folder(
    url: str,
    *,
    base_dir: str | None = None,
    doc_title: str = "",
    fetch_auto_fill_title: bool | None = None,
    allow_network: bool = True,
) -> str | None:
    """試算「依作品名分資料夾」會用的資料夾名，不翻譯、不建資料夾。

    供面板「作品資料夾」唯讀欄位顯示用，開始時把算出的名稱帶給協調器。
    手動模式不必抓網頁（直接用 doc_title）；自動模式的取名規則與實跑完全一致
    （同走 ``compute_series_folder_name``）。算不出時回 None。
    """
    base_dir = base_dir or app_paths.data_dir()
    if fetch_auto_fill_title is None:
        fetch_auto_fill_title = settings_manager.SettingsManager(
            base_dir).load_cache().fetch_auto_fill_title
    if not fetch_auto_fill_title:
        # 手動模式：作品名稱就是使用者填的，不必上網
        return compute_series_folder_name(
            doc_title=doc_title, fetch_auto_fill_title=False,
            page_title="") or None
    fetched = _preview_fetch_source(url, base_dir, allow_network)
    if fetched is None:
        return None
    _source, page_title = fetched
    return compute_series_folder_name(
        doc_title=doc_title, fetch_auto_fill_title=True,
        page_title=page_title) or None


def _record_url_history(sm, url: str, page_title: str, nav_links: list,
                        source: str, log: Callable[[str], None]) -> None:
    """把讀過的網址寫入讀取紀錄（與手動流程一致）。

    沿用多程序安全的 `append_url_history` / `update_url_related_links`：
    主程式的 1.5 秒檔案監看會自動把新紀錄刷新到 UI。失敗只記 log，不中斷翻譯。
    """
    try:
        entry: dict = {"url": url, "title": page_title or url}
        fp = original_cache.compute_fingerprint(source)
        if fp:
            entry["fingerprint"] = fp
        # work_title / author / 既有 fingerprint 由 append_url_history 自動沿用
        sm.append_url_history(entry)
        sm.update_url_related_links(url, nav_links)
    except Exception as e:  # noqa: BLE001 — 紀錄寫入失敗不該影響翻譯
        log(f"  ⚠️ 寫入網址讀取紀錄失敗（不影響翻譯）：{e}")


def format_url_with_title(url: str, titles: 'dict | None') -> str:
    """總結行用的「網址（名稱）」格式；沒有名稱時只回網址。

    `titles` 為 `AutoResult.titles`（網址 → 該網址讀取到的頁面標題）。總結列出的是
    「沒有被翻譯的網址」，光看網址（如 `?p=8210`）分不出是哪一話，故一併帶出名稱。
    """
    title = ((titles or {}).get(url) or "").strip()
    return f"{url}（{title}）" if title and title != url else url


def _next_chapter_url(nav_links: list) -> 'tuple[str, str]':
    """從關聯連結找「下一話」，回傳 `(URL, 標題)`（邏輯同 aa_main_qt._fetch_adjacent_chapter）。

    標題取自關聯連結本身，讓「還沒抓過的下一話」在總結裡也顯示得出名稱。
    """
    if not nav_links:
        return "", ""
    cur = next((i for i, lk in enumerate(nav_links)
                if lk.get("is_current")), -1)
    if cur < 0 or cur + 1 >= len(nav_links):
        return "", ""
    nxt = nav_links[cur + 1]
    return (nxt.get("url") or ""), (nxt.get("title") or "")


def _fill_titles_from_history(result: AutoResult, url_history: list) -> None:
    """總結前補上仍然缺名稱的網址：從讀取紀錄（本次啟動時載入的快照）回查。

    涵蓋「這次根本沒抓成功（抓取階段就 ChapterError）」或清單模式中未跑到的網址 ——
    只要以前讀過就有名稱可顯示；沒讀過就維持只顯示網址。
    """
    wanted = {u for u, _ in result.failed} | {u for u, _ in result.skipped}
    wanted |= {result.pending_url, result.next_url}
    wanted = {u for u in wanted if u and u not in result.titles}
    if not wanted:
        return
    for entry in url_history or []:
        u = entry.get("url")
        if u in wanted:
            title = (entry.get("title") or "").strip()
            if title:
                result.titles[u] = title


# ── 主流程 ──

# until_last 模式下的安全上限，避免關聯連結異常時無限迴圈。
_UNTIL_LAST_CAP = 9999

# 標題過濾：連續這麼多話都不符就中止（跳過的話不計話數，過濾文字打錯時才不會
# 一路抓到最後，關聯連結成環時也不會無限迴圈）。
_TITLE_FILTER_MAX_CONSECUTIVE = 50


def title_matches(page_title: str, title_filter: str) -> bool:
    """標題過濾：頁面標題含過濾文字（不分大小寫）才算符合；過濾文字空白＝不過濾。"""
    needle = (title_filter or "").strip().casefold()
    return not needle or needle in (page_title or "").casefold()


def run_auto_translate(
    start_url: str,
    count: int,
    out_dir: str,
    *,
    base_dir: str | None = None,
    backend: str | None = None,
    gem_url: str | None = None,
    profile_dir: str | None = None,
    headless: bool = False,
    max_per_session: int | None = None,
    required_model: str = "",
    doc_title: str = "",
    fetch_auto_fill_title: bool | None = None,
    until_last: bool = False,
    skip_existing: bool = False,
    skip_cache: bool = False,
    title_filter: str = "",
    group_by_series: bool | None = None,
    series_folder: str = "",
    url_list: list[str] | None = None,
    append_mode: bool | None = None,
    mask_words_enabled: bool | None = None,
    mask_word_list: str | None = None,
    output_keywords_enabled: bool | None = None,
    output_keyword_rules: list | None = None,
    on_pause: Callable[[str], None] | None = None,
    resume_event=None,
    error_policy: dict | None = None,
    stop_event=None,
    progress: Callable[[str], None] | None = None,
    print_summary: bool = True,
) -> AutoResult:
    """從 ``start_url`` 起連續自動翻譯。

    count：要翻譯的「話數」（一話 ＝ 作品的一篇／一回，對應 HTML 一個檔案）。
        翻譯一話內部可能因內容過長而分多次送給 Gemini，但仍只算一話。
    until_last：為 True 時忽略 count，一路翻到沒有下一話為止。
    skip_existing：為 True 時，翻譯前先算好這一話的檔名，若輸出資料夾已有同名檔
        （不計碰撞序號）就跳過該話、直接抓下一話——適合批次中斷後重跑略過已完成的話。
    skip_cache：為 True 時每一話都重新上網抓，不吃 %TEMP%/aa_url_cache 的內容
        （抓到仍回寫暫存）。對應主畫面「不讀暫存」開關，GUI 由該開關帶入。
    title_filter：標題過濾文字。非空時，抓到的頁面標題（page_title）不含此文字
        （不分大小寫）的話直接跳過、讀下一話；跳過的話**不計入 count**，也不參與
        作品資料夾名的決定。連續 `_TITLE_FILTER_MAX_CONSECUTIVE` 話都不符就中止整批
        （`title_filter_stop`、`pending_url`）。
    group_by_series：為 True 時在 ``out_dir`` 底下依作品名開一層子資料夾，整批的
        HTML 都寫進去（已存在就直接沿用）。None 時讀 cache 的
        auto_translate_group_by_series（預設 False）。**資料夾名整批只決定一次**
        （手動模式用 doc_title；自動模式用第一話 page_title 收斂出的作品名主體），
        之後的話一律沿用，故不會發生「一話一個資料夾」。算不出名字時退回 out_dir。
    series_folder：明確指定的作品資料夾名（GUI 面板算好的；CLI 可用 --series-folder 指定）。
        非空時直接採用，不再從標題推算——面板顯示什麼就存到哪，所見即所得。
    append_mode：對應主畫面「加入翻譯」（True，保留原文、翻譯附在原文之後）／「替換
        翻譯」（False）。None 時讀 cache 的 auto_translate_append_mode（預設 False）。
    mask_words_enabled／mask_word_list：替換過濾詞——送給 AI 前把清單裡的詞（一行
        一個，非正則）換成等長的 ○，降低被審查擋下的機率；替換回原文件時仍用原本
        的提取結果定位，所以只有譯文裡會出現 ○。None 時讀 cache 的
        auto_translate_mask_words／auto_translate_mask_word_list。
    output_keywords_enabled／output_keyword_rules：譯文關鍵字檢查——翻譯回來的譯文
        出現規則裡的詞（直接比對文字）時，依該詞設定的動作處理：「停止」＝不存檔、
        中止整批（`keyword_stop`、`pending_url`）；「跳過」＝不存檔、記入失敗、續下一話；
        「暫停」＝照常存檔後呼叫 `on_pause(說明)`，再等 `resume_event` 被設定才繼續
        （等待中按停止＝手動停止）。規則格式 [{"word": 詞, "action": pause|stop|skip}]。
        None 時讀 cache 的 auto_translate_output_kw／auto_translate_output_kw_rules。
    on_pause／resume_event：「暫停」用的 UI 回呼與 threading.Event；未提供時（CLI）
        暫停一律視為停止。
    error_policy：進階設定——各種錯誤要「中斷」或「重試」（{項目: "stop"|"retry"}，
        項目見 gemini_web.ERROR_POLICY_DEFAULTS；缺的項目用預設＝v2.40 以前的固定
        行為）。API 項目交給 API 後端套用；web_stuck（重試＝暫時跳過放進待補翻）與
        fetch_fail（重試＝等待後重抓，達上限仍中斷）由本函式套用。None 時讀 cache 的
        auto_translate_error_policy。
    url_list：手動網址清單（一行一個）。非空時**整批完全照清單跑**——第一行即第一話，
        `start_url` 參數本次忽略，下一話也不再從關聯記事推導。供「關聯記事尚未支援」
        的站台臨時使用。話數仍受 count 限制（取 min(count, 清單長度)）；until_last
        為 True 時跑完整份清單。單話抓取失敗時與一般模式相同，中斷整批並設
        pending_url（v2.27 前會跳過該話續跑）。
    stop_event：threading.Event；設定後會在話與話之間（及分段之間）中止。
    progress：進度回呼（單一字串參數）；None 時印到 stdout。
    print_summary：是否在結束時透過 `log` 印出 `_print_summary` 總結；
        GUI 端會自行印更完整的版本，故傳 False 避免面板 log 出現兩份總結。
    """
    log = progress or (lambda m: print(m))
    # CLI 直接執行時也要接收程式根目錄的舊設定（GUI 端啟動時已做過，重複呼叫無副作用）
    app_paths.auto_migrate()
    base_dir = base_dir or app_paths.data_dir()

    cfg = load_config(base_dir)
    sm = settings_manager.SettingsManager(base_dir)
    cache = sm.load_cache()
    backend = (backend or cache.translate_backend or "browser").lower()
    # 抓網頁用的代理：CLI 直接執行時也要套用（GUI 端載入設定時已套過一次）
    url_fetcher.set_fetch_proxy(getattr(cache, "fetch_proxy_url", "") or "")
    if fetch_auto_fill_title is None:
        fetch_auto_fill_title = cache.fetch_auto_fill_title
    if append_mode is None:
        append_mode = getattr(cache, "auto_translate_append_mode", False)
    if group_by_series is None:
        group_by_series = getattr(
            cache, "auto_translate_group_by_series", False)
    if mask_words_enabled is None:
        mask_words_enabled = getattr(cache, "auto_translate_mask_words", False)
    if mask_word_list is None:
        mask_word_list = getattr(cache, "auto_translate_mask_word_list", "")
    words_to_mask = parse_mask_words(mask_word_list) if mask_words_enabled else []
    if mask_words_enabled:
        log(f"🔒 替換過濾詞：開啟（清單 {len(words_to_mask)} 個詞）"
            + ("" if words_to_mask else "；清單是空的，本次不會替換任何字"))
    if output_keywords_enabled is None:
        output_keywords_enabled = getattr(cache, "auto_translate_output_kw", False)
    if output_keyword_rules is None:
        output_keyword_rules = getattr(cache, "auto_translate_output_kw_rules", [])
    kw_rules = (parse_output_keyword_rules(output_keyword_rules)
                if output_keywords_enabled else [])
    if output_keywords_enabled:
        log(f"🔎 譯文關鍵字檢查：開啟（{len(kw_rules)} 個關鍵字）"
            + ("" if kw_rules else "；沒有設定任何關鍵字，本次不會檢查"))
    title_filter = (title_filter or "").strip()
    if title_filter:
        log(f"🔤 標題過濾：只翻標題含「{title_filter}」的話（不符的跳過，不計話數）")
    if skip_cache:
        log("🌐 不讀暫存：每一話都重新上網抓取（不吃本機網頁暫存）")
    if error_policy is None:
        error_policy = getattr(cache, "auto_translate_error_policy", {})
    policy = resolve_error_policy(error_policy)
    changed = [f"{ERROR_POLICY_LABELS[k]}→{policy_choice_label(k, v)}"
               for k, v in policy.items() if v != ERROR_POLICY_DEFAULTS[k]]
    if changed:
        log("⚙ 進階設定（與預設不同）：" + "、".join(changed))
    os.makedirs(out_dir, exist_ok=True)

    # ── 依作品名分資料夾 ──
    # effective_out_dir 是這批實際落檔的資料夾：group_by_series 關閉時就是
    # out_dir 本身。開啟時整批只決定一次（見 _decide_series_dir），之後所有話
    # 沿用同一個資料夾——「跳過已存在同名檔」的判定與存檔都吃這個變數，兩者
    # 必然一致，不會出現「檔案已在子資料夾卻又重譯一份」。
    effective_out_dir = out_dir
    series_dir_decided = not group_by_series

    def _decide_series_dir(page_title: str) -> None:
        """用第一話的標題定下這批的作品資料夾（只會生效一次）。"""
        nonlocal effective_out_dir, series_dir_decided
        if series_dir_decided:
            return
        series_dir_decided = True
        name = _sanitize(series_folder) or compute_series_folder_name(
            doc_title=doc_title, fetch_auto_fill_title=fetch_auto_fill_title,
            page_title=page_title)
        if not name:
            log("  ⚠️ 無法從標題判斷作品名稱 → 這批直接存進輸出資料夾（不分子資料夾）。")
            return
        target = os.path.join(out_dir, name)
        existed = os.path.isdir(target)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            log(f"  ⚠️ 無法建立作品資料夾「{name}」（{e}）→ 改存進輸出資料夾。")
            return
        effective_out_dir = target
        log(f"  📁 作品資料夾：{name}（{'沿用既有' if existed else '新建'}）"
            f"；本批各話都存進這裡。")

    # 手動網址清單：非空時第一行即第一話，整批照清單順序跑（不看關聯記事）
    urls = [u.strip() for u in (url_list or []) if u.strip()]
    if urls:
        start_url = urls[0]
        total = len(urls) if until_last else min(max(1, count), len(urls))
    else:
        total = _UNTIL_LAST_CAP if until_last else max(1, count)
    total_label = str(total) if urls else (
        "最後一話" if until_last else str(total))
    if urls:
        log(f"📋 使用手動網址清單（共 {len(urls)} 個網址，本批跑 {total} 話）；"
            f"「起始網址」欄位本次忽略，下一話不看關聯記事。")

    def _stopping() -> bool:
        return stop_event is not None and stop_event.is_set()

    result = AutoResult()
    url = start_url

    # ── 依後端建立翻譯 session（兩者皆提供 open / translate / close） ──
    if backend == "api":
        from aa_tool import gemini_api, openai_api, secure_store
        provider = (getattr(cache, "api_provider", "gemini") or "gemini").lower()
        keys = secure_store.load_keys(base_dir, provider)
        if not keys:
            raise ValueError(
                f"API 模式（{provider}）但未設定任何 API 金鑰（請到「連線設定」輸入）")
        # API 模式：兩段 prompt 都送出（合併為系統指令，空段自動略過）
        api_prompts = [getattr(cache, "gemini_api_only_prompt", "") or "",
                       cache.gemini_api_system_prompt or ""]
        api_system_prompt = "\n\n".join(p.strip() for p in api_prompts if p.strip())
        api_timeout = int(getattr(cache, "api_timeout", 0) or 0)
        api_proxy = getattr(cache, "api_proxy_url", "") or ""  # 與抓網頁分開設定
        if provider == "gemini":
            session = gemini_api.GeminiApiSession(
                keys, cache.gemini_api_model,
                system_prompt=api_system_prompt, log=log,
                stop_event=stop_event, base_dir=base_dir,
                timeout=api_timeout, proxy=api_proxy, error_policy=policy)
            open_log = f"使用 Gemini API（模型 {cache.gemini_api_model}）…"
        else:
            meta = openai_api.API_PROVIDERS.get(provider, {})
            model = ((getattr(cache, "api_models", {}) or {}).get(provider)
                     or openai_api.default_model(provider))
            base_url = (getattr(cache, "api_custom_base_url", "") if provider == "custom"
                        else meta.get("base_url", ""))
            session = openai_api.ChatApiSession(
                keys, model,
                scheme=meta.get("scheme", "openai"), base_url=base_url,
                system_prompt=api_system_prompt, log=log, stop_event=stop_event,
                timeout=api_timeout, proxy=api_proxy, error_policy=policy)
            open_log = f"使用 {meta.get('label', provider)} API（模型 {model}）…"
    else:
        gem_url = gem_url or cache.gemini_gem_url
        if not gem_url:
            raise ValueError("未設定 Gem 網址（gemini_gem_url）")
        profile_dir = (profile_dir or cache.gemini_profile_dir
                       or os.path.join(tempfile.gettempdir(),
                                       "aa_gemini_profile"))
        session = GeminiWebSession(
            gem_url, profile_dir,
            max_per_session=(max_per_session
                             if max_per_session is not None
                             else cache.gemini_max_per_session),
            selectors=cache.gemini_selectors or None,
            required_model=required_model or cache.gemini_required_model,
            # 使用 Gem（內建人設）時，瀏覽器模式不送 prompt；否則才附加。
            prepend_prompt=("" if cache.browser_use_gem
                            else cache.gemini_api_system_prompt),
            stop_event=stop_event,
            headless=headless, log=log)
        open_log = "開啟瀏覽器並登入 Gemini…"

    # 待補翻列表（v2.30）：伺服器忙碌／逾時連續重試達上限（後端丟
    # GeminiBusyRetriesExhausted）的話先暫時跳過、放進這裡，等下一次翻譯成功
    # （＝伺服器已恢復）後依序補翻；新的話都跑完後則持續補翻到清空（v2.31）。
    # 補翻不佔話數。元素為
    # (網址, source, display_title, page_title, 話序號)，補翻時不必重抓網頁。
    deferred: list = []

    def _record_failed(ch_url: str, retrying: bool, reason: str) -> None:
        """記一話失敗；若是補翻中的話，順便移出待補翻列表（已有結論）。"""
        result.failed.append((ch_url, reason))
        if retrying:
            deferred.pop(0)

    def _wait_for_resume(message: str) -> bool:
        """譯文關鍵字「暫停」：通知 UI 後原地等使用者按繼續。回 False＝按了停止。"""
        if on_pause is None or resume_event is None:
            log("  （目前的執行方式無法暫停等待 → 視為停止）")
            return False
        resume_event.clear()
        on_pause(message)
        while not resume_event.wait(0.5):
            if _stopping():
                return False
        log("  ▶ 已按繼續，接著翻譯下一話。")
        return True

    stuck_retry = policy["web_stuck"] == "retry"
    censor_retry = policy["web_censored"] == "retry"
    # {網址: 因「回覆無法使用」而排進待補翻列表的次數}，上限 _MALFORMED_MAX_RETRIES
    malformed_tries: dict[str, int] = {}

    def _fetch_with_retry(ch_url: str) -> tuple[str, list, str, str]:
        """抓取＋解析；進階設定「抓取網頁失敗＝重試」時等待後重抓（解析失敗不重試）。

        重抓達上限仍失敗 → 丟最後一次的 FetchFailed（呼叫端中斷整批）；等待中按停止
        → StopRequested。
        """
        attempt = 0
        while True:
            try:
                return _fetch_and_parse(ch_url, cfg, skip_cache=skip_cache)
            except FetchFailed as e:
                if policy["fetch_fail"] != "retry":
                    raise
                if attempt >= _FETCH_MAX_RETRIES:
                    lines = str(e).split("\n")
                    lines[0] += f"（已重抓 {attempt} 次仍失敗）"
                    raise FetchFailed("\n".join(lines)) from e
                attempt += 1
                long_wait = attempt % _FETCH_LONG_WAIT_EVERY == 0
                wait = _FETCH_RETRY_WAIT + (_FETCH_LONG_WAIT if long_wait else 0.0)
                log(f"  ⏳ {str(e).splitlines()[0]} → {int(wait)}s 後重抓"
                    f"（第 {attempt}/{_FETCH_MAX_RETRIES} 次，進階設定：重試）…")
                if stop_event is not None:
                    if stop_event.wait(wait):
                        raise StopRequested() from e
                else:
                    time.sleep(wait)

    try:
        log(open_log)
        try:
            session.open()
        except GeminiModelMismatch as e:
            result.model_mismatch = True
            log(f"🛑 啟動時模型不符且等待逾時（{e}），未開始翻譯。")
            return result
        except GeminiAborted as e:
            result.stopped = True
            log(f"⏹️ {e}，未開始翻譯。")
            return result
        retry_ready = False   # 上一話翻譯成功 → 下一輪先補翻待補翻列表
        drain_logged = False  # 「新的話已跑完、開始清空列表」的提示只印一次
        i = 0                 # 已開始處理的「新」話數（補翻、標題過濾跳過的不計）
        list_pos = 0          # 清單模式：已讀到清單第幾個網址（標題過濾跳過的也算）
        filtered_run = 0      # 連續幾話不符標題過濾
        while True:
            if _stopping():
                result.stopped = True
                log("⏹️ 已收到停止指令，中止。")
                break
            # 新的話都跑完了（跑滿話數或沒有下一話）
            no_more_new = i >= total or not url
            # 這一輪先補翻的時機：上一話翻譯成功（伺服器已恢復），或新的話已跑完
            # ——後者持續補翻到列表清空為止（v2.31；要中止就按停止）。
            retrying = bool(deferred) and (retry_ready or no_more_new)
            retry_ready = False
            if no_more_new and retrying and not drain_logged:
                drain_logged = True
                log(f"🔁 新的話已跑完，繼續補翻待補翻列表剩下的 {len(deferred)} 話，"
                    "直到全部完成（要中止請按停止）…")
            if not retrying and no_more_new:
                if i >= total:
                    # 跑滿設定話數而結束 → url 為下一話續接網址，供 GUI 把它帶回
                    # 「起始網址」直接接續下一批（已是最後一話時 url 為空）。
                    if url:
                        result.next_url = url
                        log(f"▶ 已達設定話數；下一話接續網址：{url}")
                else:
                    result.reached_end = True
                    log("✅ 沒有下一話了，已翻到最後一話。")
                break
            if retrying:
                ch_url, source, display_title, page_title, ch_index = deferred[0]
                next_url = url  # 補翻不推進「新的話」：補完照原本進度續跑
                log("=== 補翻先前暫時跳過的話："
                    f"{format_url_with_title(ch_url, result.titles)} ===")
            else:
                i += 1
                ch_url, ch_index = url, i
                log(f"=== 第 {i}/{total_label} 話：{url} ===")

                # 1) 抓取＋解析（失敗則無法得知下一話 → 中斷整批）
                try:
                    source, nav_links, page_title, display_title = _fetch_with_retry(
                        url)
                except StopRequested:
                    result.stopped = True
                    result.pending_url = url
                    log("⏹️ 已收到停止指令，中止（此話未完成）。")
                    break
                except ChapterError as e:
                    # 抓取／解析失敗一律中斷整批（v2.27：清單模式原本會跳過續跑，現在也
                    # 中斷——「跳過某一話」只保留給 AI 端的疑似審查／疑似未翻譯，抓取層
                    # 的失敗不默默漏話）。訊息可能含多行診斷：Log 印完整版，失敗清單／
                    # 總結只留第一行。
                    result.failed.append((url, str(e).split(chr(10))[0]))
                    result.pending_url = url  # 這一話未完成 → 供 GUI 回填起始網址接續
                    log(f"  ❌ {e} → 中斷整批（此話未完成，可用它當起始網址接續）。")
                    if urls:
                        log("  （網址清單模式：清單中這一話之後的網址都還沒處理；"
                            "要接續請把清單改成從這一話開始。）")
                    break
                # 讀過的網址寫入讀取紀錄（與手動流程一致）
                _record_url_history(sm, url, page_title, nav_links, source, log)
                # 記下這一話的名稱，供總結顯示（失敗／跳過／接續的網址才分得出是哪一話）
                if page_title:
                    result.titles[url] = page_title
                # 清單模式下一話直接取清單的下一筆（list_pos 為已讀網址數，故下一筆
                # 是 urls[list_pos]；標題過濾跳過的話不計 i，所以不能用 i 當索引）
                if urls:
                    list_pos += 1
                    next_url = urls[list_pos] if list_pos < len(urls) else ""
                else:
                    next_url, next_title = _next_chapter_url(nav_links)
                    if next_url and next_title:
                        result.titles.setdefault(next_url, next_title)

                # 1.3) 標題過濾：標題不含過濾文字 → 跳過、讀下一話，不計話數。
                #      放在決定作品資料夾之前，別的作品的標題才不會被拿去當資料夾名。
                if not title_matches(page_title, title_filter):
                    i -= 1
                    filtered_run += 1
                    result.filtered.append((url, page_title))
                    log(f"  ⏭️ 標題「{page_title or '（讀不到標題）'}」不含"
                        f"「{title_filter}」→ 跳過此話（不計話數），讀下一話。")
                    url = next_url
                    if url and filtered_run >= _TITLE_FILTER_MAX_CONSECUTIVE:
                        result.title_filter_stop = (
                            f"連續 {filtered_run} 話標題都不含「{title_filter}」")
                        result.pending_url = url
                        log(f"  🛑 {result.title_filter_stop} → 中止整批"
                            "（請確認標題過濾文字；下一話可用來當起始網址接續）。")
                        break
                    continue
                filtered_run = 0
                # 1.4) 依作品名分資料夾：用第一話的標題定一次，之後各話沿用。
                #      放在跳過判定之前，確保「已存在同名檔」看的是子資料夾。
                _decide_series_dir(page_title)

                # 1.5) 若已有同名檔則跳過（重跑批次時略過已完成的話、省 API 額度）。
                #      檢查用「不含碰撞序號」的檔名主體，因此判定的是「這一話本身」是否
                #      已產出過，而非湊巧撞名的其他話。
                if skip_existing:
                    name_base = compute_chapter_name_base(
                        doc_title=doc_title,
                        fetch_auto_fill_title=fetch_auto_fill_title,
                        source=source, page_title=page_title, fallback_index=i)
                    existing = os.path.join(effective_out_dir, f"{name_base}.html")
                    if os.path.exists(existing):
                        result.skipped.append((url, f"{name_base}.html"))
                        log(f"  ⏭️ 已存在同名檔「{name_base}.html」→ 跳過此話，續下一話。")
                        url = next_url
                        continue

            # 2) 提取 → 翻譯 → 替換 → 存檔
            try:
                extracted = _extract(source, display_title, cfg)
                n_lines = len([l for l in extracted.splitlines() if l.strip()])
                log(f"  提取 {n_lines} 行，開始翻譯…")
                # 送給 AI 的是遮蔽過濾詞後的版本；未翻譯偵測也跟它比（AI 看到的就是
                # 這份）。替換回原文件仍用未遮蔽的 extracted 定位。
                to_send, n_masked = mask_words(extracted, words_to_mask)
                if n_masked:
                    log(f"  🔒 已把 {n_masked} 處過濾詞換成 ○")
                translated = _translate(session, to_send, log, stop_event,
                                        stuck_retry=stuck_retry,
                                        censor_retry=censor_retry)
                warnings = text_extraction.validate_ai_text(translated)
                untranslated = _looks_untranslated(to_send, translated)
                if warnings or untranslated:
                    reason = ("回覆與原文幾乎一致（疑似未翻譯）" if untranslated
                              else "翻譯驗證警告：" + "  ".join(warnings))
                    log(f"  ⚠️ {reason} → 重試一次（再送一次 Gemini）")
                    # 未翻譯時：同一對話重送很可能吐相同結果，先開新對話再重試
                    if untranslated:
                        log("  （未翻譯：先開新對話再重試，避免重複相同結果）")
                        session.start_new_session()
                    translated = _translate(session, to_send, log, stop_event,
                                            stuck_retry=stuck_retry,
                                            censor_retry=censor_retry)
                    if _looks_untranslated(to_send, translated):
                        raise UntranslatedResponse("重試（已換新對話）後仍與原文幾乎一致")
                try:
                    _check_reply_usable(to_send, translated, policy, log,
                                        malformed_tries.get(ch_url, 0))
                except GeminiBusyRetriesExhausted:
                    malformed_tries[ch_url] = malformed_tries.get(ch_url, 0) + 1
                    raise
                # 譯文關鍵字檢查：停止／跳過在存檔前處理（不存檔），暫停等存檔後再等
                kw_hits = find_output_keywords(translated, kw_rules) if kw_rules else []
                kw_action = next((a for a in _OUTPUT_KEYWORD_PRIORITY
                                  if any(h[1] == a for h in kw_hits)), None)
                if kw_hits:
                    log(f"  🔎 譯文出現關鍵字：{_keyword_hits_text(kw_hits)}")
                    for w, _a, line in kw_hits:
                        log(f"      「{w}」：{line[:100]}")
                if kw_action in ("stop", "skip"):
                    words = "、".join(f"「{w}」" for w, a, _ in kw_hits if a == kw_action)
                    raise OutputKeywordHit(kw_action, f"譯文出現關鍵字{words}")
                log("  加入翻譯中…" if append_mode else "  替換翻譯中…")
                result_text = translation_engine.apply_translation(
                    source, extracted, translated, cfg.glossary,
                    append_mode=append_mode,
                    pad_right_aa=cfg.pad_right_aa,
                    symbol_regex_str=cfg.symbol_regex,
                    glossary_avoid_aa=cfg.glossary_avoid_aa)
                out_path = compute_chapter_filename(
                    effective_out_dir,
                    doc_title=doc_title,
                    fetch_auto_fill_title=fetch_auto_fill_title,
                    source=source, page_title=page_title,
                    fallback_index=ch_index)
                html_io.write_html_file(out_path, result_text)
                size = os.path.getsize(out_path)
                if size < _MIN_OUTPUT_BYTES:
                    # 一話的 HTML 實際上不可能這麼小 → 多半是內容缺損，留著反而
                    # 會讓「已存在同名檔則跳過」把它當成已完成。刪掉後跳過此話。
                    try:
                        os.remove(out_path)
                        removed = "已刪除"
                    except OSError as e:
                        removed = f"刪除失敗（{e}），請手動刪除"
                    raise OutputTooSmall(
                        f"存檔後只有 {size / 1024:.1f} KB（小於 "
                        f"{_MIN_OUTPUT_BYTES // 1024} KB），{removed}："
                        f"{os.path.basename(out_path)}")
                # 同步把原文（含 display_title 前綴）以「投稿指紋」存進
                # aa_original_cache.json — 與手動流程一致，使 EditWindow
                # 的「比對原文」模式能對得回 source。
                try:
                    original_cache.save_entry(
                        base_dir, source,
                        extracted=extracted, translation=translated,
                        limit=cache.original_cache_limit)
                except Exception as e:  # noqa: BLE001 — 暫存寫失敗不該中斷整批
                    log(f"  ⚠️ 原文暫存寫入失敗（不影響存檔）：{e}")
                result.done.append(out_path)
                log(f"  ✅ 已存檔：{out_path}")
                if retrying:
                    deferred.pop(0)
                retry_ready = True  # 翻譯成功＝伺服器正常 → 下一輪先補翻待補翻列表
                url = next_url
                if kw_action == "pause":
                    words = "、".join(f"「{w}」" for w, a, _ in kw_hits if a == "pause")
                    log(f"  ⏸️ 譯文出現關鍵字{words} → 已存檔並暫停，"
                        "確認後按上方橫幅的「▶ 繼續」翻下一話（或按停止結束）。")
                    if not _wait_for_resume(
                            f"譯文出現關鍵字{words}，已存檔並暫停："
                            f"{os.path.basename(out_path)}"):
                        # 這話已存檔 → 接續網址是下一話（補翻中則是下一個新話）
                        result.stopped = True
                        result.pending_url = url
                        log("⏹️ 暫停中按了停止，中止整批（此話已存檔）。")
                        break
            except ChapterError as e:
                # 只會是 `_extract` 的「提取結果為空」＝這一話沒有可翻譯的文字
                # （純 AA／圖片話）。不是工具故障，記錄後跳過續跑，不中斷整批。
                _record_failed(ch_url, retrying, str(e).split(chr(10))[0])
                log(f"  ⏭️ {e} → 跳過此話，繼續下一話。")
                url = next_url
            except GeminiModelMismatch as e:
                result.model_mismatch = True
                result.pending_url = url
                log(f"🛑 模型不符且等待逾時（{e}），中止整批。")
                break
            except GeminiAborted as e:
                result.stopped = True
                result.pending_url = url
                log(f"⏹️ {e}，中止整批。")
                break
            except CensoredResponse as e:
                _record_failed(ch_url, retrying, f"可能被審查：{e}")
                log(f"  🚫 {e} → 跳過此話，繼續下一話。")
                url = next_url
            except GeminiContentBlocked as e:
                # API 端安全過濾擋下（如 blockReason: PROHIBITED_CONTENT）＝被審查，
                # 重送幾乎一定再被擋 → 比照 CensoredResponse 跳過該話、續下一話。
                _record_failed(ch_url, retrying, f"可能被審查：{e}")
                log(f"  🚫 {e} → 跳過此話，繼續下一話。")
                url = next_url
            except GeminiResponseTruncated as e:
                # API 輸出達模型上限被截斷＝這一話內容太長，重送一樣會被截斷 →
                # 跳過該話（不存半套翻譯）、續下一話；不放進待補翻列表。
                _record_failed(ch_url, retrying, f"回應被截斷：{e}")
                log(f"  ✂️ {e} → 跳過此話（不存檔），繼續下一話。")
                url = next_url
            except UntranslatedResponse as e:
                _record_failed(ch_url, retrying, f"疑似未翻譯：{e}")
                log(f"  ⚠️ {e} → 跳過此話（不存檔），繼續下一話。")
                url = next_url
            except MalformedResponse as e:
                # 回覆不是譯文（AI 回了摘要／漏翻太多）→ 不存檔。進階設定選
                # 「稍後重試」時不會走到這裡（改丟 GeminiBusyRetriesExhausted
                # 排進待補翻列表）。
                _record_failed(ch_url, retrying, f"回覆無法使用：{e}")
                log(f"  ⚠️ {e} → 跳過此話（不存檔），繼續下一話。")
                url = next_url
            except OutputTooSmall as e:
                _record_failed(ch_url, retrying, f"檔案過小：{e}")
                log(f"  🗑️ {e} → 跳過此話，繼續下一話。")
                url = next_url
            except OutputKeywordHit as e:
                if e.action == "skip":
                    _record_failed(ch_url, retrying, f"{e}（跳過，不存檔）")
                    log(f"  ⏭️ {e} → 跳過此話（不存檔），繼續下一話。")
                    url = next_url
                else:  # stop
                    # 補翻中的話也移出列表並記失敗（原因寫明關鍵字，不會被誤列成
                    # 「伺服器忙碌未能補翻」）；pending_url 同其他中斷類錯誤
                    _record_failed(ch_url, retrying, f"{e}（停止，不存檔）")
                    result.keyword_stop = str(e)
                    result.pending_url = url
                    log(f"  🛑 {e} → 不存檔，中止整批"
                        + ("。" if retrying else "（此話未完成，可用它當起始網址接續）。"))
                    break
            except GeminiBusyRetriesExhausted as e:
                # 伺服器忙碌／逾時重試達上限：外部狀況，不中斷整批也不就此放棄 →
                # 暫時跳過、放進待補翻列表，下一次翻譯成功（伺服器恢復）後再補翻。
                if retrying and no_more_new:
                    # 清空列表階段：排到列表最後、先補下一話，避免某一話（例如
                    # 太長而每次都逾時）卡住其他話。只有一話時就是原地再試。
                    deferred.append(deferred.pop(0))
                    log(f"  ⏳ {e} → 伺服器仍未恢復，這話排到待補翻列表最後"
                        f"（共 {len(deferred)} 話），稍後再試。")
                elif retrying:
                    log(f"  ⏳ {e} → 伺服器仍未恢復，這話留在待補翻列表"
                        f"（共 {len(deferred)} 話），下一次翻譯成功後再試。")
                else:
                    deferred.append(
                        (ch_url, source, display_title, page_title, ch_index))
                    log(f"  ⏳ {e} → 暫時跳過此話，加入待補翻列表"
                        f"（共 {len(deferred)} 話），下一次翻譯成功後再補翻。")
                url = next_url
            except StopRequested:
                result.stopped = True
                result.pending_url = url
                log("⏹️ 已收到停止指令，中止（此話未完成）。")
                break
            except GeminiQuotaExceeded as e:
                result.quota_paused = True
                result.pending_url = url
                log(f"  ⏸️ {e}")
                log("  撞到 Gemini 額度上限，暫停。已完成的話皆已存檔。")
                break
            except Exception as e:  # noqa: BLE001 — 未預期錯誤：記錄後中斷整批
                # v2.27：原本是「跳過此話續跑」，改為中斷。非 5xx、非安全過濾的
                # API 錯誤（4xx、空回應等）、寫檔失敗、程式錯誤等都走這裡，繼續跑
                # 下去往往整批都失敗，停下來讓使用者處理比默默漏話好。
                # （安全過濾擋下另走上面的 GeminiContentBlocked，比照審查跳過；
                # 輸出被截斷走 GeminiResponseTruncated 跳過；連線失敗／中途斷線／
                # 非 JSON 回應在本批成功翻譯過之後由 API 後端當成暫時性錯誤重試，
                # 只有第一次送出就失敗〔多半是設定問題〕才會到這裡。）
                # 補翻中的話出錯時，pending_url 仍是「下一個新的話」（url），
                # 補翻的那話記在失敗清單。
                _record_failed(ch_url, retrying, str(e))
                result.pending_url = url
                log(f"  ❌ 失敗：{e} → 中斷整批"
                    + ("。" if retrying else "（此話未完成，可用它當起始網址接續）。"))
                break
    finally:
        session.close()

    # 仍留在待補翻列表的話＝暫時跳過後始終沒補翻成功 → 列入失敗，總結才看得到
    for d_url, *_ in deferred:
        result.failed.append(
            (d_url, "伺服器忙碌／逾時，重試達上限而暫時跳過，之後未能補翻成功"))

    processed = len(result.done) + len(result.failed) + len(result.skipped)
    result.remaining = 0 if until_last else max(0, total - processed)
    _fill_titles_from_history(result, cache.url_history)
    if print_summary:
        _print_summary(result, log)
    return result


def _print_summary(result: AutoResult, log: Callable[[str], None]) -> None:
    log("──────── 自動翻譯總結 ────────")
    log(f"成功：{len(result.done)} 話")
    for p in result.done:
        log(f"  ✅ {p}")
    if result.failed:
        log(f"失敗：{len(result.failed)} 話")
        for u, reason in result.failed:
            log(f"  ❌ {format_url_with_title(u, result.titles)} — {reason}")
    if result.skipped:
        log(f"跳過（已存在同名檔）：{len(result.skipped)} 話")
        for u, fn in result.skipped:
            log(f"  ⏭️ {format_url_with_title(u, result.titles)} — {fn}")
    if result.filtered:
        log(f"跳過（標題不符過濾）：{len(result.filtered)} 話")
        for u, _t in result.filtered:
            log(f"  ⏭️ {format_url_with_title(u, result.titles)}")
    if result.title_filter_stop:
        log(f"🛑 {result.title_filter_stop}，已中止整批。")
        if result.pending_url:
            log("   要接續，可用此網址當 --url："
                + format_url_with_title(result.pending_url, result.titles))
    if result.quota_paused:
        log("⏸️ 因 Gemini 額度上限暫停。")
        log("   待額度恢復後，用此網址當 --url 接續："
            + format_url_with_title(result.pending_url, result.titles))
        if result.remaining > 0:
            log(f"   尚餘約 {result.remaining} 話未翻。")
    if result.stopped:
        log("⏹️ 已被手動停止。")
        if result.pending_url:
            log("   要接續，可用此網址當 --url："
                + format_url_with_title(result.pending_url, result.titles))
    if result.reached_end:
        log("🏁 已翻到最後一話。")
    if result.model_mismatch:
        log("🛑 因模型與要求不符而中止整批。請在 Gemini 切換到正確模型後重跑。")
    if result.keyword_stop:
        log(f"🛑 {result.keyword_stop}（停止），已中止整批。")
        if result.pending_url:
            log("   要接續，可用此網址當 --url："
                + format_url_with_title(result.pending_url, result.titles))


# ── CLI ──

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="連續多話自動翻譯（操控網頁版 Gemini）")
    parser.add_argument("--url", required=True, help="起始話的網址")
    parser.add_argument("--count", type=int, default=1,
                        help="要連續翻譯的話數（一話 ＝ 作品一篇）")
    parser.add_argument("--until-last", action="store_true",
                        help="忽略 --count，一路翻到沒有下一話為止")
    parser.add_argument("--skip-existing", action="store_true",
                        help="輸出資料夾已有同名檔時跳過該話（重跑批次略過已完成的話）")
    parser.add_argument("--skip-cache", action="store_true",
                        help="不讀本機網頁暫存，每一話都重新上網抓")
    parser.add_argument("--title-filter", default="",
                        help="只翻頁面標題含此文字的話（不符的跳過、不計話數）")
    parser.add_argument("--append", action="store_true",
                        help="加入翻譯模式（保留原文、翻譯附在原文之後）；預設為替換翻譯")
    parser.add_argument("--group-by-series", action="store_true",
                        help="在輸出資料夾下依作品名開子資料夾存放（整批同一個）")
    parser.add_argument("--series-folder", default="",
                        help="指定作品資料夾名（不指定則從第一話標題推算）")
    parser.add_argument("--out", required=True, help="輸出 HTML 的資料夾")
    parser.add_argument("--gem-url", default=None,
                        help="Gemini Gem 網址（預設讀設定 gemini_gem_url）")
    parser.add_argument("--profile-dir", default=None,
                        help="Playwright 瀏覽器 profile 目錄")
    parser.add_argument("--backend", default=None,
                        choices=["browser", "api"],
                        help="翻譯後端：browser（操控網頁）或 api（Gemini API）")
    parser.add_argument("--max-per-session", type=int, default=None,
                        help="同一對話最多送幾次後開新對話（覆寫設定）")
    parser.add_argument("--required-model", default="",
                        help="要求的模型（pro／flash／flash-lite／any，預設讀設定）")
    parser.add_argument("--doc-title", default="",
                        help="作品名稱（手動模式檔名前綴）；預設 '未命名'")
    parser.add_argument("--auto-fill-title", action="store_true",
                        help="檔名從每話 page_title 自動萃取（覆寫 fetch_auto_fill_title 設定）")
    parser.add_argument("--headless", action="store_true",
                        help="無頭模式（首次登入請勿用，需看得到視窗手動登入）")
    args = parser.parse_args(argv)

    try:
        result = run_auto_translate(
            args.url, args.count, args.out,
            backend=args.backend,
            gem_url=args.gem_url, profile_dir=args.profile_dir,
            headless=args.headless, until_last=args.until_last,
            skip_existing=args.skip_existing,
            skip_cache=args.skip_cache,
            title_filter=args.title_filter,
            group_by_series=(True if args.group_by_series else None),
            series_folder=args.series_folder,
            append_mode=(True if args.append else None),
            max_per_session=args.max_per_session,
            required_model=args.required_model,
            doc_title=args.doc_title,
            fetch_auto_fill_title=(True if args.auto_fill_title else None))
    except (ValueError, GeminiWebError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0 if not result.failed and not result.quota_paused else 2


if __name__ == "__main__":
    sys.exit(main())
