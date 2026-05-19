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

from aa_tool import constants, html_io, settings_manager, text_extraction
from aa_tool import translation_engine, url_fetcher
from aa_tool.gemini_web import GeminiQuotaExceeded, GeminiWebError, GeminiWebSession

# 單次送給 Gemini 的最大提取行數；超過則分段送出後合併。
# 800 是經驗值：實務上短於這個長度都不需分段；切點固定在「整行」邊界，
# 永遠不會把單一 ID 切開（chunks 由 lines 切片組成）。
MAX_LINES_PER_REQUEST = 800

# 審查偵測：原文一定行數以上，但回覆極短且幾乎沒有 ID|文 結構 → 視為被審查。
_CENSOR_REPLY_MAX_LINES = 4
_CENSOR_SOURCE_MIN_LINES = 4
_ID_LINE_RE = re.compile(r'^\s*\d+-\d+\s*\|')

_URL_CACHE_DIR = os.path.join(tempfile.gettempdir(), "aa_url_cache")


# ── 例外 ──

class ChapterError(RuntimeError):
    """單一話處理失敗（抓取/解析/提取/替換等）。"""


class StopRequested(RuntimeError):
    """使用者按下停止鈕，要求中止整批流程。"""


class CensoredResponse(RuntimeError):
    """偵測到 Gemini 回覆疑似被審查（極短且非翻譯格式），該話跳過。"""


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
        glossary=translation_engine.parse_glossary(glossary_text),
        author_name=c.author_name,
        author_only=c.author_only,
        korean_mode=c.korean_mode,
        experimental=c.experimental_extraction,
        pad_right_aa=c.pad_right_aa,
        glossary_avoid_aa=c.glossary_avoid_aa,
        work_title=c.doc_title,
    )


# ── 結果 ──

@dataclass
class AutoResult:
    """一次自動翻譯批次的總結。"""
    done: list = field(default_factory=list)        # [輸出檔路徑, ...]
    failed: list = field(default_factory=list)      # [(url, 原因), ...]
    quota_paused: bool = False                      # 是否因額度上限暫停
    pending_url: str = ""                           # 暫停／停止時未完成的話網址
    remaining: int = 0                              # 尚未處理的話數
    stopped: bool = False                           # 是否被使用者手動停止
    reached_end: bool = False                       # 是否因為沒有下一話而結束


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


def _safe_filename(page_title: str, index: int) -> str:
    """由頁面標題產生安全檔名，前綴序號以維持話數順序。"""
    title = (page_title or "").strip() or f"chapter_{index}"
    title = re.sub(r'[\\/:*?"<>|]', "_", title)
    title = title[:80].strip() or f"chapter_{index}"
    return f"{index:03d}_{title}.html"


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
    gem_url: str | None = None,
    profile_dir: str | None = None,
    headless: bool = False,
    max_per_session: int | None = None,
    until_last: bool = False,
    stop_event=None,
    progress: Callable[[str], None] | None = None,
) -> AutoResult:
    """從 ``start_url`` 起連續自動翻譯。

    count：要翻譯的「話數」（一話 ＝ 作品的一篇／一回，對應 HTML 一個檔案）。
        翻譯一話內部可能因內容過長而分多次送給 Gemini，但仍只算一話。
    until_last：為 True 時忽略 count，一路翻到沒有下一話為止。
    stop_event：threading.Event；設定後會在話與話之間（及分段之間）中止。
    progress：進度回呼（單一字串參數）；None 時印到 stdout。
    """
    log = progress or (lambda m: print(m))
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))

    cfg = load_config(base_dir)
    cache = settings_manager.SettingsManager(base_dir).load_cache()
    gem_url = gem_url or cache.gemini_gem_url
    if not gem_url:
        raise ValueError("未設定 Gem 網址（gemini_gem_url）")
    profile_dir = (profile_dir or cache.gemini_profile_dir
                   or os.path.join(tempfile.gettempdir(), "aa_gemini_profile"))
    os.makedirs(out_dir, exist_ok=True)

    total = _UNTIL_LAST_CAP if until_last else max(1, count)
    total_label = "最後一話" if until_last else str(total)

    def _stopping() -> bool:
        return stop_event is not None and stop_event.is_set()

    result = AutoResult()
    url = start_url
    session = GeminiWebSession(
        gem_url, profile_dir,
        max_per_session=(max_per_session
                         if max_per_session is not None
                         else cache.gemini_max_per_session),
        selectors=cache.gemini_selectors or None,
        headless=headless, log=log)

    try:
        log("開啟瀏覽器並登入 Gemini…")
        session.open()
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
                log(f"  ❌ {e} → 無法取得下一話，中斷。")
                break
            next_url = _next_chapter_url(nav_links)

            # 2) 提取 → 翻譯 → 替換 → 存檔
            try:
                extracted = _extract(source, display_title, cfg)
                n_lines = len([l for l in extracted.splitlines() if l.strip()])
                log(f"  提取 {n_lines} 行，開始翻譯…")
                translated = _translate(session, extracted, log, stop_event)
                warnings = text_extraction.validate_ai_text(translated)
                if warnings:
                    log("  ⚠️ 翻譯驗證警告：" + "  ".join(warnings)
                        + " → 重試一次（會再送一次 Gemini）")
                    translated = _translate(session, extracted, log, stop_event)
                log("  替換翻譯中…")
                result_text = translation_engine.apply_translation(
                    source, extracted, translated, cfg.glossary,
                    pad_right_aa=cfg.pad_right_aa,
                    symbol_regex_str=cfg.symbol_regex,
                    glossary_avoid_aa=cfg.glossary_avoid_aa)
                out_path = os.path.join(out_dir, _safe_filename(page_title, i))
                html_io.write_html_file(out_path, result_text)
                result.done.append(out_path)
                log(f"  ✅ 已存檔：{out_path}")
                url = next_url
            except CensoredResponse as e:
                result.failed.append((url, f"可能被審查：{e}"))
                log(f"  🚫 {e} → 跳過此話，繼續下一話。")
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
    finally:
        session.close()

    processed = len(result.done) + len(result.failed)
    result.remaining = 0 if until_last else max(0, total - processed)
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


# ── CLI ──

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="連續多話自動翻譯（操控網頁版 Gemini）")
    parser.add_argument("--url", required=True, help="起始話的網址")
    parser.add_argument("--count", type=int, default=1,
                        help="要連續翻譯的話數（一話 ＝ 作品一篇）")
    parser.add_argument("--until-last", action="store_true",
                        help="忽略 --count，一路翻到沒有下一話為止")
    parser.add_argument("--out", required=True, help="輸出 HTML 的資料夾")
    parser.add_argument("--gem-url", default=None,
                        help="Gemini Gem 網址（預設讀設定 gemini_gem_url）")
    parser.add_argument("--profile-dir", default=None,
                        help="Playwright 瀏覽器 profile 目錄")
    parser.add_argument("--max-per-session", type=int, default=None,
                        help="同一對話最多送幾次後開新對話（覆寫設定）")
    parser.add_argument("--headless", action="store_true",
                        help="無頭模式（首次登入請勿用，需看得到視窗手動登入）")
    args = parser.parse_args(argv)

    try:
        result = run_auto_translate(
            args.url, args.count, args.out,
            gem_url=args.gem_url, profile_dir=args.profile_dir,
            headless=args.headless, until_last=args.until_last,
            max_per_session=args.max_per_session)
    except (ValueError, GeminiWebError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0 if not result.failed and not result.quota_paused else 2


if __name__ == "__main__":
    sys.exit(main())
