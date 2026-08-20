"""連續多話自動翻譯協調器。

把原本的人工五步流程 ——「提取原文 → 貼到網頁版 Gemini → 貼回翻譯 →
替換並存檔 → 按下一話」—— 串成全自動流程，連續翻譯 N 話。

翻譯這一步由 :mod:`aa_tool.gemini_web` 操控網頁版 Gemini 的 Gem 完成；
其餘步驟全部重用 ``aa_tool/`` 內既有的純函式。

用法（CLI）::

    python aa_auto_translate.py --url <起始網址> --count 5 --out <輸出資料夾>

也可由 aa_main_qt 的 GUI 按鈕呼叫 :func:`run_auto_translate`。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from aa_tool import constants, html_io, original_cache, settings_manager
from aa_tool import text_extraction, translation_engine, url_fetcher
from aa_tool.gemini_web import (
    GeminiAborted, GeminiModelMismatch, GeminiQuotaExceeded, GeminiWebError,
    GeminiWebSession,
)

# 單次送給 Gemini 的最大提取行數；超過則分段送出後合併。
# 800 是經驗值：實務上短於這個長度都不需分段；切點固定在「整行」邊界，
# 永遠不會把單一 ID 切開（chunks 由 lines 切片組成）。
MAX_LINES_PER_REQUEST = 800

# 審查偵測：原文一定行數以上，但回覆極短且幾乎沒有 ID|文 結構 → 視為被審查。
_CENSOR_REPLY_MAX_LINES = 4
_CENSOR_SOURCE_MIN_LINES = 4
_ID_LINE_RE = re.compile(r'^\s*\d+-\d+\s*\|')

# 未翻譯偵測：可比對的 ID 中，譯文與原文「完全相同」的比例 ≥ 此值 → 視為沒翻譯。
_UNTRANSLATED_RATIO = 0.9
# 可比對 ID 數少於此值時不做未翻譯判定（樣本太少容易誤判，交給其他檢查）。
_UNTRANSLATED_MIN_IDS = 3

_URL_CACHE_DIR = os.path.join(tempfile.gettempdir(), "aa_url_cache")


# ── 例外 ──

class ChapterError(RuntimeError):
    """單一話處理失敗（抓取/解析/提取/替換等）。"""


class StopRequested(RuntimeError):
    """使用者按下停止鈕，要求中止整批流程。"""


class CensoredResponse(RuntimeError):
    """偵測到 Gemini 回覆疑似被審查（極短且非翻譯格式），該話跳過。"""


class UntranslatedResponse(RuntimeError):
    """偵測到回覆與原文幾乎一致（疑似沒翻譯，只是把原文吐回來），該話跳過。"""


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
    quota_paused: bool = False                      # 是否因額度上限暫停
    pending_url: str = ""                           # 暫停／停止時未完成的話網址
    next_url: str = ""                              # 自然跑滿話數後的下一話續接網址
    remaining: int = 0                              # 尚未處理的話數
    stopped: bool = False                           # 是否被使用者手動停止
    reached_end: bool = False                       # 是否因為沒有下一話而結束
    model_mismatch: bool = False                    # 是否因模型與要求不符而中止


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

def _fetch_and_parse(url: str, cfg: AutoConfig) -> tuple[str, list, str, str]:
    """抓網頁並解析，回傳 (帶標題前綴的完整 source, 關聯連結, 頁面標題, display_title)。

    為使 ID 行號與手動流程一致：手動流程在網址讀取成功後會在文字前面
    prepend ``display_title + "\\n\\n"``（見 aa_main_qt.py 約 1741-1744 行），
    導致所有行號 +2。自動流程也得照辦，否則同一句話會被指派到不同行號的 ID。
    """
    page_html = _read_url_cache(url)
    if page_html is None:
        try:
            page_html = url_fetcher.fetch_url(url)
        except Exception as e:
            raise ChapterError(f"抓取網頁失敗：{e}") from e
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
    """檢查回覆是否疑似被審查。

    判定條件：原文一定行數以上（避免短句誤判），但回覆極短（≤4 行）且幾乎
    沒有 ``ID|文`` 結構 → 視為被審查。被審查時 Gemini 通常回一兩句
    「無法協助」「不便回應」之類的拒絕語。
    """
    sent = [l for l in extracted_chunk.split("\n") if l.strip()]
    reply_lines = [l for l in reply.split("\n") if l.strip()]
    if len(sent) < _CENSOR_SOURCE_MIN_LINES:
        return False
    if len(reply_lines) > _CENSOR_REPLY_MAX_LINES:
        return False
    matched = sum(1 for l in reply_lines if _ID_LINE_RE.match(l))
    return matched <= 1


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


def _translate(session: GeminiWebSession, extracted: str,
               log: Callable[[str], None], stop_event=None) -> str:
    """送 Gemini 翻譯；行數過多時分段送出後合併。

    GeminiQuotaExceeded 直接往外拋（呼叫端暫停整批）。
    每段送出前檢查 stop_event，已設定則丟 StopRequested。
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
        chunk_text = "\n".join(chunk)
        reply = session.translate(chunk_text)
        if _looks_censored(chunk_text, reply):
            raise CensoredResponse(
                f"分段 {idx}/{len(chunks)} 回覆極短且非翻譯格式（疑似被審查）")
        parts.append(reply.strip())
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

    供面板「檔名預覽」即時顯示真正會落地的檔名用。
    - `allow_network=False`：只吃本地 URL 快取，沒命中回 None（不卡網路，
      適合面板開啟時的即時預覽）。
    - fetch/parse 失敗或內文為空一律回 None（預覽不該丟例外）。
    """
    if not url:
        return None
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(base_dir)
    if fetch_auto_fill_title is None:
        fetch_auto_fill_title = settings_manager.SettingsManager(
            base_dir).load_cache().fetch_auto_fill_title
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
    path = compute_chapter_filename(
        out_dir or ".", doc_title=doc_title,
        fetch_auto_fill_title=fetch_auto_fill_title,
        source=source, page_title=page_title, fallback_index=1)
    return os.path.basename(path)


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


def _next_chapter_url(nav_links: list) -> str:
    """從關聯連結找「下一話」的 URL（邏輯同 aa_main_qt._fetch_adjacent_chapter）。"""
    if not nav_links:
        return ""
    cur = next((i for i, lk in enumerate(nav_links)
                if lk.get("is_current")), -1)
    if cur < 0 or cur + 1 >= len(nav_links):
        return ""
    return nav_links[cur + 1].get("url") or ""


# ── 主流程 ──

# until_last 模式下的安全上限，避免關聯連結異常時無限迴圈。
_UNTIL_LAST_CAP = 9999


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
    url_list: list[str] | None = None,
    append_mode: bool | None = None,
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
    append_mode：對應主畫面「加入翻譯」（True，保留原文、翻譯附在原文之後）／「替換
        翻譯」（False）。None 時讀 cache 的 auto_translate_append_mode（預設 False）。
    url_list：手動網址清單（一行一個）。非空時**整批完全照清單跑**——第一行即第一話，
        `start_url` 參數本次忽略，下一話也不再從關聯記事推導。供「關聯記事尚未支援」
        的站台臨時使用。話數仍受 count 限制（取 min(count, 清單長度)）；until_last
        為 True 時跑完整份清單。清單模式下單話抓取失敗不會中斷整批（下一話網址已知），
        改為跳過該話續下一話。
    stop_event：threading.Event；設定後會在話與話之間（及分段之間）中止。
    progress：進度回呼（單一字串參數）；None 時印到 stdout。
    print_summary：是否在結束時透過 `log` 印出 `_print_summary` 總結；
        GUI 端會自行印更完整的版本，故傳 False 避免面板 log 出現兩份總結。
    """
    log = progress or (lambda m: print(m))
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))

    cfg = load_config(base_dir)
    sm = settings_manager.SettingsManager(base_dir)
    cache = sm.load_cache()
    backend = (backend or cache.translate_backend or "browser").lower()
    if fetch_auto_fill_title is None:
        fetch_auto_fill_title = cache.fetch_auto_fill_title
    if append_mode is None:
        append_mode = getattr(cache, "auto_translate_append_mode", False)
    os.makedirs(out_dir, exist_ok=True)

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
        if provider == "gemini":
            session = gemini_api.GeminiApiSession(
                keys, cache.gemini_api_model,
                system_prompt=api_system_prompt, log=log,
                stop_event=stop_event, base_dir=base_dir,
                timeout=api_timeout)
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
                timeout=api_timeout)
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
        for i in range(1, total + 1):
            if _stopping():
                result.stopped = True
                log("⏹️ 已收到停止指令，中止。")
                break
            if not url:
                result.reached_end = True
                log("✅ 沒有下一話了，已翻到最後一話。")
                break
            log(f"=== 第 {i}/{total_label} 話：{url} ===")

            # 1) 抓取＋解析（失敗則無法得知下一話 → 中斷整批）
            try:
                source, nav_links, page_title, display_title = _fetch_and_parse(
                    url, cfg)
            except ChapterError as e:
                result.failed.append((url, str(e)))
                if urls:
                    # 清單模式：下一話網址已知（不靠這一話的關聯記事），可續跑
                    log(f"  ❌ {e} → 跳過此話，繼續下一話。")
                    url = urls[i] if i < len(urls) else ""
                    continue
                result.pending_url = url  # 這一話未完成 → 供 GUI 回填起始網址接續
                log(f"  ❌ {e} → 無法取得下一話，中斷。")
                break
            # 讀過的網址寫入讀取紀錄（與手動流程一致）
            _record_url_history(sm, url, page_title, nav_links, source, log)
            # 清單模式下一話直接取清單的下一筆（i 為 1-based，故下一筆是 urls[i]）
            if urls:
                next_url = urls[i] if i < len(urls) else ""
            else:
                next_url = _next_chapter_url(nav_links)

            # 1.5) 若已有同名檔則跳過（重跑批次時略過已完成的話、省 API 額度）。
            #      檢查用「不含碰撞序號」的檔名主體，因此判定的是「這一話本身」是否
            #      已產出過，而非湊巧撞名的其他話。
            if skip_existing:
                name_base = compute_chapter_name_base(
                    doc_title=doc_title,
                    fetch_auto_fill_title=fetch_auto_fill_title,
                    source=source, page_title=page_title, fallback_index=i)
                existing = os.path.join(out_dir, f"{name_base}.html")
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
                translated = _translate(session, extracted, log, stop_event)
                warnings = text_extraction.validate_ai_text(translated)
                untranslated = _looks_untranslated(extracted, translated)
                if warnings or untranslated:
                    reason = ("回覆與原文幾乎一致（疑似未翻譯）" if untranslated
                              else "翻譯驗證警告：" + "  ".join(warnings))
                    log(f"  ⚠️ {reason} → 重試一次（再送一次 Gemini）")
                    # 未翻譯時：同一對話重送很可能吐相同結果，先開新對話再重試
                    if untranslated:
                        log("  （未翻譯：先開新對話再重試，避免重複相同結果）")
                        session.start_new_session()
                    translated = _translate(session, extracted, log, stop_event)
                    if _looks_untranslated(extracted, translated):
                        raise UntranslatedResponse("重試（已換新對話）後仍與原文幾乎一致")
                log("  加入翻譯中…" if append_mode else "  替換翻譯中…")
                result_text = translation_engine.apply_translation(
                    source, extracted, translated, cfg.glossary,
                    append_mode=append_mode,
                    pad_right_aa=cfg.pad_right_aa,
                    symbol_regex_str=cfg.symbol_regex,
                    glossary_avoid_aa=cfg.glossary_avoid_aa)
                out_path = compute_chapter_filename(
                    out_dir,
                    doc_title=doc_title,
                    fetch_auto_fill_title=fetch_auto_fill_title,
                    source=source, page_title=page_title,
                    fallback_index=i)
                html_io.write_html_file(out_path, result_text)
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
                result.failed.append((url, f"可能被審查：{e}"))
                log(f"  🚫 {e} → 跳過此話，繼續下一話。")
                url = next_url
            except UntranslatedResponse as e:
                result.failed.append((url, f"疑似未翻譯：{e}"))
                log(f"  ⚠️ {e} → 跳過此話（不存檔），繼續下一話。")
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
            except Exception as e:  # noqa: BLE001 — 單話任何錯誤都不該中斷整批
                result.failed.append((url, str(e)))
                log(f"  ❌ 失敗：{e} → 跳過此話，繼續下一話。")
                url = next_url
        else:
            # 迴圈跑滿設定話數而自然結束（非 break）→ url 為下一話續接網址，
            # 供 GUI 把它帶回「起始網址」直接接續下一批（已是最後一話時 url 為空）。
            if url:
                result.next_url = url
                log(f"▶ 已達設定話數；下一話接續網址：{url}")
    finally:
        session.close()

    processed = len(result.done) + len(result.failed) + len(result.skipped)
    result.remaining = 0 if until_last else max(0, total - processed)
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
            log(f"  ❌ {u} — {reason}")
    if result.skipped:
        log(f"跳過（已存在同名檔）：{len(result.skipped)} 話")
        for u, fn in result.skipped:
            log(f"  ⏭️ {u} — {fn}")
    if result.quota_paused:
        log("⏸️ 因 Gemini 額度上限暫停。")
        log(f"   待額度恢復後，用此網址當 --url 接續：{result.pending_url}")
        if result.remaining > 0:
            log(f"   尚餘約 {result.remaining} 話未翻。")
    if result.stopped:
        log("⏹️ 已被手動停止。")
        if result.pending_url:
            log(f"   要接續，可用此網址當 --url：{result.pending_url}")
    if result.reached_end:
        log("🏁 已翻到最後一話。")
    if result.model_mismatch:
        log("🛑 因模型與要求不符而中止整批。請在 Gemini 切換到正確模型後重跑。")


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
    parser.add_argument("--append", action="store_true",
                        help="加入翻譯模式（保留原文、翻譯附在原文之後）；預設為替換翻譯")
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
