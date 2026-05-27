import json
import os
from dataclasses import dataclass, field

from .constants import (
    DEFAULT_BASE_REGEX, DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX,
    DEFAULT_BG_COLOR, DEFAULT_FG_COLOR,
)
from .file_lock import locked_file


def merge_glossary_diff(existing: str, current: str) -> str:
    """以等號左側為 key 合併兩段術語表文字。

    - 檔案中已有的 key：若 current 也有 → 用 current 的整行覆蓋；否則保留檔上行。
    - current 中的新 key（不在 existing 內）：依出現順序 prepend 到**最上面**
      （新加入的用語顯示在頂端，配合 `_save_glossary_entry` 的置頂行為）。
    - 空行 / 不含等號的行：原樣保留（existing 在前，current 新增的接在末端）。

    Key 比對與 `parse_glossary()` / `_check_glossary_duplicates()` 一致：
    走 `decode_glossary_term`（無引號 → strip；`"..."` → 保留外圍空白）。
    所以 `term=val` 和 `term = val`、` term=val ` 都會被視為同一條規則；
    `" Trooper "=…` 與 `Trooper=…` 則視為**不同** key（前者保留外圍空白）。
    """
    from .translation_engine import decode_glossary_term

    def parse_lines(text: str):
        for raw in (text or "").splitlines():
            line = raw.rstrip('\r')
            if '=' in line:
                key = decode_glossary_term(line.split('=', 1)[0])
                yield key, line, True
            else:
                yield None, line, False

    cur_map: dict[str, str] = {}
    cur_keys_order: list[str] = []
    cur_extra_lines: list[str] = []
    for key, line, has_eq in parse_lines(current):
        if has_eq:
            if key not in cur_map:
                cur_keys_order.append(key)
            cur_map[key] = line
        else:
            cur_extra_lines.append(line)

    existing_keys: set[str] = set()
    existing_out: list[str] = []
    seen_extra: set[str] = set()
    for key, line, has_eq in parse_lines(existing):
        if has_eq:
            existing_keys.add(key)
            existing_out.append(cur_map[key] if key in cur_map else line)
        else:
            existing_out.append(line)
            seen_extra.add(line)

    # current 中的新 key 置頂；existing 既有行維持原順序接在後面
    new_keys_out = [cur_map[key] for key in cur_keys_order
                    if key not in existing_keys]
    extra_out: list[str] = []
    for line in cur_extra_lines:
        if line not in seen_extra:
            extra_out.append(line)
            seen_extra.add(line)

    return "\n".join(new_keys_out + existing_out + extra_out)


def merge_filter_diff(existing: str, current: str) -> str:
    """以整行為 key 合併自訂過濾規則。檔上行先保留，current 中的新行 append。"""
    out_lines: list[str] = []
    seen: set[str] = set()
    for raw in (existing or "").splitlines():
        line = raw.rstrip('\r')
        out_lines.append(line)
        seen.add(line)
    for raw in (current or "").splitlines():
        line = raw.rstrip('\r')
        if line not in seen:
            out_lines.append(line)
            seen.add(line)
    return "\n".join(out_lines)


@dataclass
class AppSettings:
    """AA_Settings.json 對應的資料結構。"""
    base_regex: str = DEFAULT_BASE_REGEX
    invalid_regex: str = DEFAULT_INVALID_REGEX
    symbol_regex: str = DEFAULT_SYMBOL_REGEX
    filter_text: str = ""
    glossary: str = ""
    glossary_temp: str = ""


@dataclass
class AppCache:
    """aa_settings_cache.json 對應的資料結構。"""
    source_text: str = ""
    filter_text: str = ""
    glossary_text: str = ""
    glossary_text_temp: str = ""
    doc_title: str = ""
    doc_num: str = "1"
    bg_color: str = DEFAULT_BG_COLOR
    fg_color: str = DEFAULT_FG_COLOR
    preview_text: str = ""
    url_history: list = field(default_factory=list)
    url_related_links: list = field(default_factory=list)
    current_url: str = ""
    auto_copy: bool = False
    batch_folder: str = ""
    author_name: str = ""
    author_only: bool = False
    work_history: list = field(default_factory=list)
    editor_font_family: str = "MS PGothic"
    editor_font_size: int = 12
    editor_line_height: int = 120
    last_open_dir: str = ""
    # 最後在編輯器中開啟過的 HTML 檔案絕對路徑；首頁「📂 檔案列表」浮層
    # 以此檔的所在資料夾列出相鄰檔案（依檔名排序、前後各 10 個）。
    last_opened_file: str = ""
    editor_bg_color: str = "#ffffff"
    work_history_limit: int = 10
    fetch_history_limit: int = 50
    original_cache_limit: int = 50
    glossary_auto_search: bool = True
    glossary_translation_only: bool = False
    diff_save_mode: bool = False
    editor_default_wysiwyg: bool = False
    # 編輯器內按複製（Ctrl+C／右鍵）時，是否自動把複製內容填入全文替換的原文框
    editor_copy_to_replace: bool = False
    # 編輯器全文替換勾選「存入術語」時，是否同步加入批次搜尋的「快速替換」面板
    glossary_sync_to_batch_quick: bool = False
    # 另存新檔時是否把字型 Base64 內嵌到 <head>（離線手機可正確顯示，
    # 代價是檔案會增大約 1MB+）；覆蓋舊檔（Ctrl+S、批次搜尋）時不寫入。
    embed_font_in_html: bool = False
    embed_font_name: str = "monapo"
    # 編輯器右側「局部重套用」面板（Alt+4）的持久化狀態：
    # - side_panel_width：面板寬度（px）；0 表示沿用預設 splitter 比例
    # - side_auto_scroll：「自動捲動」勾選框狀態
    side_panel_width: int = 0
    side_auto_scroll: bool = False
    # 編輯器「用語集」面板（Alt+5）寬度（px）；0 表示沿用預設 splitter 比例
    glossary_panel_width: int = 0
    # 韓文提取模式：開啟後 base_regex 改用 DEFAULT_BASE_REGEX_KO
    korean_mode: bool = False
    # 實驗性日文提取演算法：錨點掃描 + 邊界擴展 + 分數制過濾。
    # 目的：減少 AA 圖中夾雜少量合法字元時的誤提取。預設關閉，由設定 dialog 切換。
    experimental_extraction: bool = False
    # 實驗性：替換翻譯時，若被替換原文的「右側」殘留內容像 AA 圖（沿用實驗提取
    # 演算法的 AA 噪聲判定）且譯文較短，則於譯文後補等量全形空白。預設關閉。
    pad_right_aa: bool = False
    # 實驗性：套用術語表時，略過疑似落在 AA 圖上的命中，避免術語表把 AA 圖中
    # 剛好等於某術語 key 的片假名碎片誤替換掉。預設關閉。
    glossary_avoid_aa: bool = False
    # 套用術語表時，原文 key 的平假名↔片假名互換變體也一併套用（例：術語
    # `ライザ=萊莎` 同時等同 `らいざ=萊莎`）。預設關閉。
    glossary_kana_fold: bool = False
    # 提取日文後，若提取出的文字與術語表中某條術語的「原文」完全相同（含空白
    # 一字不差），則從提取結果中剔除——避免把已會被全文替換的術語再列出來翻譯。
    # 影響：主畫面提取、自動翻譯 _extract、單字假名提取 三條路徑。預設關閉。
    glossary_skip_extract: bool = False
    # 「存入術語」（編輯器全文替換、批次搜尋加入術語等）後，是否自動把一般術語表
    # 永久寫入 AA_Settings.json（依「僅儲存差異」設定合併/覆蓋）。預設關閉。
    glossary_auto_persist: bool = False
    # 「補空白」按鈕在每個字元之間插入的全形空白數量（1~3）。
    pad_space_count: int = 2
    # 網址讀取成功且有正確辨識標題時，自動把標題填入作品名稱框。
    fetch_auto_fill_title: bool = False
    # ── 自動翻譯（aa_auto_translate.py / gemini_web.py）──
    # gemini_gem_url：使用者固定用來翻譯 AA 的 Gemini Gem 網址。
    # gemini_profile_dir：Playwright 持久化瀏覽器 profile 目錄（空 = 用 %TEMP% 預設）。
    # gemini_max_per_session：同一對話 session 最多翻譯幾次，超過自動開新對話。
    # gemini_selectors：DOM 選擇器覆寫（Gemini 改版時可在此手動修正）。
    # auto_translate_out_dir：自動翻譯輸出資料夾（記住上次選擇）。
    gemini_gem_url: str = ""
    gemini_profile_dir: str = ""
    gemini_max_per_session: int = 3
    gemini_selectors: dict = field(default_factory=dict)
    auto_translate_out_dir: str = ""
    auto_translate_count: int = 5
    auto_translate_until_last: bool = False
    # 翻譯後端：'browser'（操控網頁版 Gemini）或 'api'（Google Gemini API）。
    translate_backend: str = "browser"
    # API 模式用的模型 id（見 aa_tool.gemini_api.API_MODELS）。
    gemini_api_model: str = "gemini-2.5-pro"
    # API 模式的系統指令（翻譯人設／要求）；金鑰另存於加密檔 aa_api_keys.dat。
    gemini_api_system_prompt: str = ""
    # 瀏覽器模式是否使用 Gem（內建人設）：True 時瀏覽器模式「不送出」翻譯 Prompt，
    # 只有 API 模式才送。預設 True（多數使用者用 Gem）。
    browser_use_gem: bool = True
    # 要求的 Gemini 模型：pro / flash / flash-lite / any。
    # 翻譯中若偵測到模型與此不符，整批自動中止（讀不到模型字串時不阻擋，會在 Log 警告）。
    gemini_required_model: str = "pro"


class SettingsManager:
    """管理 AA_Settings.json 與 aa_settings_cache.json 的讀寫。

    純 I/O 層，不碰任何 UI widget。
    """

    def __init__(self, base_dir: str):
        self._base_dir = base_dir

    def get_settings_file(self) -> str:
        return os.path.join(self._base_dir, 'AA_Settings.json')

    def get_cache_file(self) -> str:
        return os.path.join(self._base_dir, 'aa_settings_cache.json')

    # ── AA_Settings.json ──

    def load_settings(self) -> AppSettings:
        """讀取 AA_Settings.json，回傳 AppSettings。找不到時回傳預設值。"""
        settings_file = self.get_settings_file()
        settings = AppSettings()
        if not os.path.exists(settings_file):
            return settings
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            settings.base_regex = data.get('base_regex', settings.base_regex)
            settings.invalid_regex = data.get('invalid_regex', settings.invalid_regex)
            settings.symbol_regex = data.get('symbol_regex', settings.symbol_regex)
            settings.filter_text = data.get('filter', '')
            settings.glossary = data.get('glossary', '')
            settings.glossary_temp = data.get('glossary_temp', '')
        except Exception as e:
            print("AA_Settings.json load failed:", e)
        return settings

    def save_settings(self, settings: AppSettings) -> None:
        """將 AppSettings 寫入 AA_Settings.json。"""
        data = {
            'base_regex': settings.base_regex,
            'invalid_regex': settings.invalid_regex,
            'symbol_regex': settings.symbol_regex,
            'filter': settings.filter_text,
            'glossary': settings.glossary,
            'glossary_temp': settings.glossary_temp,
        }
        try:
            with open(self.get_settings_file(), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("AA_Settings.json save failed:", e)

    def save_regex_to_settings(self, base: str, invalid: str, symbol: str) -> None:
        """僅更新 AA_Settings.json 中的三條正則表達式，保留其他欄位。

        為維持「三條 regex 在前、filter/glossary 在後」的固定順序，
        這裡用全新 dict 重建後寫入；其他未知欄位則 append 在尾端，避免被丟棄。
        """
        settings_file = self.get_settings_file()
        existing: dict = {}
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                pass
        ordered = {
            'base_regex': base,
            'invalid_regex': invalid,
            'symbol_regex': symbol,
            'filter': existing.get('filter', ''),
            'glossary': existing.get('glossary', ''),
            'glossary_temp': existing.get('glossary_temp', ''),
        }
        for k, v in existing.items():
            if k not in ordered:
                ordered[k] = v
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(ordered, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("AA_Settings.json regex save failed:", e)

    # ── aa_settings_cache.json ──

    def load_cache(self) -> AppCache:
        """讀取 aa_settings_cache.json，回傳 AppCache。找不到時回傳預設值。"""
        cache_file = self.get_cache_file()
        cache = AppCache()
        if not os.path.exists(cache_file):
            return cache
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cache.source_text = data.get('source_text', '').rstrip('\n')
            cache.filter_text = data.get('filter_text', '').rstrip('\n')
            cache.glossary_text = data.get('glossary_text', '').rstrip('\n')
            cache.glossary_text_temp = data.get('glossary_text_temp', '').rstrip('\n')
            cache.doc_title = data.get('doc_title', '')
            cache.doc_num = data.get('doc_num', '1')
            cache.bg_color = data.get('bg_color', DEFAULT_BG_COLOR)
            cache.fg_color = data.get('fg_color', DEFAULT_FG_COLOR)
            cache.preview_text = data.get('preview_text', '')
            cache.url_history = data.get('url_history', [])
            cache.current_url = data.get('current_url', '')
            raw_rel = data.get('url_related_links', [])
            if isinstance(raw_rel, dict):
                cache.url_related_links = list(raw_rel.get(cache.current_url, []))
            elif isinstance(raw_rel, list):
                # 舊格式：平鋪 list 即為目前 current_url 的連結
                cache.url_related_links = raw_rel
            else:
                cache.url_related_links = []
            cache.auto_copy = bool(data.get('auto_copy', False))
            cache.batch_folder = data.get('batch_folder', '')
            cache.author_name = data.get('author_name', '')
            cache.author_only = bool(data.get('author_only', False))
            cache.work_history = data.get('work_history', []) or []
            cache.editor_font_family = data.get(
                'editor_font_family', cache.editor_font_family)
            try:
                cache.editor_font_size = int(data.get(
                    'editor_font_size', cache.editor_font_size))
            except (TypeError, ValueError):
                pass
            try:
                cache.editor_line_height = int(data.get(
                    'editor_line_height', cache.editor_line_height))
            except (TypeError, ValueError):
                pass
            cache.last_open_dir = data.get('last_open_dir', '')
            cache.last_opened_file = data.get('last_opened_file', '')
            cache.editor_bg_color = data.get(
                'editor_bg_color', cache.editor_bg_color)
            try:
                cache.work_history_limit = int(data.get(
                    'work_history_limit', cache.work_history_limit))
            except (TypeError, ValueError):
                pass
            try:
                cache.fetch_history_limit = int(data.get(
                    'fetch_history_limit', cache.fetch_history_limit))
            except (TypeError, ValueError):
                pass
            try:
                # 舊版只有 fetch_history_limit 共用；若新欄位缺失則沿用舊值
                cache.original_cache_limit = int(data.get(
                    'original_cache_limit', cache.fetch_history_limit))
            except (TypeError, ValueError):
                pass
            cache.glossary_auto_search = bool(data.get(
                'glossary_auto_search', cache.glossary_auto_search))
            cache.glossary_translation_only = bool(data.get(
                'glossary_translation_only', cache.glossary_translation_only))
            cache.diff_save_mode = bool(data.get(
                'diff_save_mode', cache.diff_save_mode))
            cache.embed_font_in_html = bool(data.get(
                'embed_font_in_html', cache.embed_font_in_html))
            cache.editor_default_wysiwyg = bool(data.get(
                'editor_default_wysiwyg', cache.editor_default_wysiwyg))
            cache.editor_copy_to_replace = bool(data.get(
                'editor_copy_to_replace', cache.editor_copy_to_replace))
            cache.glossary_sync_to_batch_quick = bool(data.get(
                'glossary_sync_to_batch_quick',
                cache.glossary_sync_to_batch_quick))
            cache.embed_font_name = str(data.get(
                'embed_font_name', cache.embed_font_name))
            try:
                cache.side_panel_width = int(data.get(
                    'side_panel_width', cache.side_panel_width) or 0)
            except (TypeError, ValueError):
                pass
            cache.side_auto_scroll = bool(data.get(
                'side_auto_scroll', cache.side_auto_scroll))
            try:
                cache.glossary_panel_width = int(data.get(
                    'glossary_panel_width', cache.glossary_panel_width) or 0)
            except (TypeError, ValueError):
                pass
            cache.korean_mode = bool(data.get(
                'korean_mode', cache.korean_mode))
            cache.experimental_extraction = bool(data.get(
                'experimental_extraction', cache.experimental_extraction))
            cache.pad_right_aa = bool(data.get(
                'pad_right_aa', cache.pad_right_aa))
            cache.glossary_avoid_aa = bool(data.get(
                'glossary_avoid_aa', cache.glossary_avoid_aa))
            cache.glossary_kana_fold = bool(data.get(
                'glossary_kana_fold', cache.glossary_kana_fold))
            cache.glossary_skip_extract = bool(data.get(
                'glossary_skip_extract', cache.glossary_skip_extract))
            cache.glossary_auto_persist = bool(data.get(
                'glossary_auto_persist', cache.glossary_auto_persist))
            cache.fetch_auto_fill_title = bool(data.get(
                'fetch_auto_fill_title', cache.fetch_auto_fill_title))
            cache.gemini_gem_url = str(data.get(
                'gemini_gem_url', cache.gemini_gem_url))
            cache.gemini_profile_dir = str(data.get(
                'gemini_profile_dir', cache.gemini_profile_dir))
            try:
                cache.gemini_max_per_session = int(data.get(
                    'gemini_max_per_session', cache.gemini_max_per_session))
            except (TypeError, ValueError):
                pass
            sel = data.get('gemini_selectors', cache.gemini_selectors)
            cache.gemini_selectors = sel if isinstance(sel, dict) else {}
            cache.auto_translate_out_dir = str(data.get(
                'auto_translate_out_dir', cache.auto_translate_out_dir))
            try:
                cache.auto_translate_count = max(1, int(data.get(
                    'auto_translate_count', cache.auto_translate_count)))
            except (TypeError, ValueError):
                pass
            cache.auto_translate_until_last = bool(data.get(
                'auto_translate_until_last', cache.auto_translate_until_last))
            cache.gemini_required_model = str(data.get(
                'gemini_required_model', cache.gemini_required_model) or "pro")
            cache.translate_backend = str(data.get(
                'translate_backend', cache.translate_backend) or "browser")
            cache.gemini_api_model = str(data.get(
                'gemini_api_model', cache.gemini_api_model)
                or "gemini-2.5-pro")
            cache.gemini_api_system_prompt = str(data.get(
                'gemini_api_system_prompt', cache.gemini_api_system_prompt))
            cache.browser_use_gem = bool(data.get(
                'browser_use_gem', cache.browser_use_gem))
            try:
                v = int(data.get('pad_space_count', cache.pad_space_count))
                if v in (1, 2, 3):
                    cache.pad_space_count = v
            except (TypeError, ValueError):
                pass
        except Exception as e:
            print("Cache load failed:", e)
        return cache

    def save_cache(self, cache: AppCache) -> None:
        """將 AppCache 寫入 aa_settings_cache.json。

        多程序安全：使用 sidecar 鎖 + 原子寫（temp + os.replace）。
        - url_history / work_history：由 append_url_history / append_work_history
          在事件觸發時直接 append，這裡**保留檔上值**，不讓 in-memory 可能過時
          的版本覆蓋其他程序的新增。
        - url_related_links：以 dict[url → links] 儲存；僅更新 current_url 對
          應的 entry，其他 URL 的連結原樣保留。
        """
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            existing = self._read_cache_raw()

            # 歷史類：保留檔上值
            url_hist = existing.get('url_history', []) or []
            work_hist = existing.get('work_history', []) or []

            # url_related_links：維持 dict 格式
            rel_map = existing.get('url_related_links', {})
            if isinstance(rel_map, list):
                # 舊格式遷移：視為既有 current_url 的連結
                old_cur = existing.get('current_url', '')
                rel_map = {old_cur: rel_map} if (old_cur and rel_map) else {}
            elif not isinstance(rel_map, dict):
                rel_map = {}
            if cache.current_url and cache.url_related_links:
                rel_map[cache.current_url] = list(cache.url_related_links)

            data = {
                'source_text': cache.source_text,
                'filter_text': cache.filter_text,
                'glossary_text': cache.glossary_text,
                'glossary_text_temp': cache.glossary_text_temp,
                'doc_title': cache.doc_title,
                'doc_num': cache.doc_num,
                'bg_color': cache.bg_color,
                'fg_color': cache.fg_color,
                'preview_text': cache.preview_text,
                'url_history': url_hist,
                'url_related_links': rel_map,
                'current_url': cache.current_url,
                'auto_copy': cache.auto_copy,
                'batch_folder': cache.batch_folder,
                'author_name': cache.author_name,
                'author_only': cache.author_only,
                'work_history': work_hist,
                'editor_font_family': cache.editor_font_family,
                'editor_font_size': cache.editor_font_size,
                'editor_line_height': cache.editor_line_height,
                'last_open_dir': cache.last_open_dir,
                'last_opened_file': cache.last_opened_file,
                'editor_bg_color': cache.editor_bg_color,
                'work_history_limit': cache.work_history_limit,
                'fetch_history_limit': cache.fetch_history_limit,
                'original_cache_limit': cache.original_cache_limit,
                'glossary_auto_search': cache.glossary_auto_search,
                'glossary_translation_only': cache.glossary_translation_only,
                'diff_save_mode': cache.diff_save_mode,
                'embed_font_in_html': cache.embed_font_in_html,
                'editor_default_wysiwyg': cache.editor_default_wysiwyg,
                'editor_copy_to_replace': cache.editor_copy_to_replace,
                'glossary_sync_to_batch_quick':
                    cache.glossary_sync_to_batch_quick,
                'embed_font_name': cache.embed_font_name,
                'side_panel_width': cache.side_panel_width,
                'side_auto_scroll': cache.side_auto_scroll,
                'glossary_panel_width': cache.glossary_panel_width,
                'korean_mode': cache.korean_mode,
                'experimental_extraction': cache.experimental_extraction,
                'pad_right_aa': cache.pad_right_aa,
                'glossary_avoid_aa': cache.glossary_avoid_aa,
                'glossary_kana_fold': cache.glossary_kana_fold,
                'glossary_skip_extract': cache.glossary_skip_extract,
                'glossary_auto_persist': cache.glossary_auto_persist,
                'pad_space_count': cache.pad_space_count,
                'fetch_auto_fill_title': cache.fetch_auto_fill_title,
                'gemini_gem_url': cache.gemini_gem_url,
                'gemini_profile_dir': cache.gemini_profile_dir,
                'gemini_max_per_session': cache.gemini_max_per_session,
                'gemini_selectors': cache.gemini_selectors,
                'auto_translate_out_dir': cache.auto_translate_out_dir,
                'auto_translate_count': cache.auto_translate_count,
                'auto_translate_until_last': cache.auto_translate_until_last,
                'gemini_required_model': cache.gemini_required_model,
                'translate_backend': cache.translate_backend,
                'gemini_api_model': cache.gemini_api_model,
                'gemini_api_system_prompt': cache.gemini_api_system_prompt,
                'browser_use_gem': cache.browser_use_gem,
            }
            self._atomic_write_json(cache_file, data)

    # ── 細粒度更新 helpers（多程序安全，不動非目標欄位）──

    def _read_cache_raw(self) -> dict:
        """讀原始 JSON；檔案不存在或損毀回傳空 dict。呼叫端需自行持有鎖。"""
        cache_file = self.get_cache_file()
        if not os.path.exists(cache_file):
            return {}
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _atomic_write_json(self, path: str, data: dict) -> None:
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            print("Cache atomic write failed:", e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def append_url_history(self, entry: dict) -> None:
        """新增一筆 URL 歷史（讀檔 → 以 url 去重 → append 於末端 → 寫回）。

        採 newest-last 慣例，與 aa_url_fetch_qt.py 既有 `reversed(url_history)` 顯示一致。
        紀錄無上限；清除時才依設定保留最近 N 筆。
        """
        url = (entry or {}).get('url', '')
        if not url:
            return
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            data = self._read_cache_raw()
            hist = data.get('url_history', []) or []
            # 保留舊條目戳印的 work_title / author（由另存新檔/翻譯並儲存時寫入），
            # 若新 entry 未提供則沿用，避免重抓同一 URL 把戳印洗掉。
            new_entry = dict(entry)
            for h in hist:
                if isinstance(h, dict) and h.get('url') == url:
                    if 'work_title' not in new_entry and h.get('work_title'):
                        new_entry['work_title'] = h['work_title']
                    if 'author' not in new_entry and h.get('author'):
                        new_entry['author'] = h['author']
                    if 'fingerprint' not in new_entry and h.get('fingerprint'):
                        new_entry['fingerprint'] = h['fingerprint']
                    break
            hist = [h for h in hist
                    if isinstance(h, dict) and h.get('url') != url]
            hist.append(new_entry)
            data['url_history'] = hist
            self._atomic_write_json(cache_file, data)

    def stamp_url_history_meta(self, url: str, work_title: str,
                               author: str) -> None:
        """就地更新 url_history 中對應 url 條目的 work_title / author 欄位。

        - 不改順序、不新增、不去重；找不到對應 url 則略過。
        - 觸發點：EditWindow 另存新檔成功、apply_translation_and_save 成功時。
        - 覆寫策略：直接覆蓋既有戳印值。
        """
        if not url:
            return
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            data = self._read_cache_raw()
            hist = data.get('url_history', []) or []
            changed = False
            for h in hist:
                if isinstance(h, dict) and h.get('url') == url:
                    h['work_title'] = work_title or ''
                    h['author'] = author or ''
                    changed = True
                    break
            if changed:
                data['url_history'] = hist
                self._atomic_write_json(cache_file, data)

    def append_work_history(self, entry: dict, max_items: int = 10) -> None:
        """新增一筆作品/作者歷史（以 (title, author) 去重 prepend）。"""
        title = (entry or {}).get('title', '')
        author = (entry or {}).get('author', '')
        if not title and not author:
            return
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            data = self._read_cache_raw()
            hist = data.get('work_history', []) or []
            hist = [h for h in hist if isinstance(h, dict)
                    and not (h.get('title', '') == title
                             and h.get('author', '') == author)]
            hist.insert(0, dict(entry))
            data['work_history'] = hist[:max_items]
            self._atomic_write_json(cache_file, data)

    def update_url_related_links(self, url: str, links: list) -> None:
        """更新指定 URL 的相關連結。不同 URL 的連結各自保留。"""
        if not url:
            return
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            data = self._read_cache_raw()
            rel = data.get('url_related_links', {})
            if isinstance(rel, list):
                old_cur = data.get('current_url', '')
                rel = {old_cur: rel} if (old_cur and rel) else {}
            elif not isinstance(rel, dict):
                rel = {}
            rel[url] = list(links) if links else []
            data['url_related_links'] = rel
            self._atomic_write_json(cache_file, data)

    def peek_shared_state(self, current_url: str = '') -> dict:
        """輕量讀取多程序共享欄位。用於 mtime 觸發的即時刷新。

        不取鎖：原子寫入保證讀到的不會是半截檔案，且只回傳要刷新的欄位，
        不會覆蓋編輯器中正在編輯的文字。
        """
        data = self._read_cache_raw()
        rel = data.get('url_related_links', {})
        if isinstance(rel, dict):
            rel_links = list(rel.get(current_url, [])) if current_url else []
        elif isinstance(rel, list):
            # 舊格式：當 current_url 與檔上一致才用
            rel_links = (rel if current_url
                         and current_url == data.get('current_url', '') else [])
        else:
            rel_links = []
        return {
            'url_history': data.get('url_history', []) or [],
            'work_history': data.get('work_history', []) or [],
            'url_related_links': rel_links,
        }

    def clear_url_history(self) -> None:
        """清空 URL 歷史（保留其他欄位）。"""
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            data = self._read_cache_raw()
            data['url_history'] = []
            self._atomic_write_json(cache_file, data)

    def clear_url_history_keep_n(self, keep_n: int) -> int:
        """清除 URL 歷史，保留最近 keep_n 筆（keep_n=0 表示全清）。回傳清除後的筆數。"""
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            data = self._read_cache_raw()
            hist = data.get('url_history', []) or []
            if keep_n > 0:
                hist = hist[-keep_n:]
            else:
                hist = []
            data['url_history'] = hist
            self._atomic_write_json(cache_file, data)
            return len(hist)
