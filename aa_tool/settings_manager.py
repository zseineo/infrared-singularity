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
    - current 中的新 key（不在 existing 內）：依出現順序 append 到末端。
    - 空行 / 不含等號的行：原樣保留（依「先 existing 後 current 新增」順序）。

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

    out_lines: list[str] = []
    used_keys: set[str] = set()
    seen_extra: set[str] = set()
    for key, line, has_eq in parse_lines(existing):
        if has_eq:
            if key in cur_map:
                out_lines.append(cur_map[key])
                used_keys.add(key)
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)
            seen_extra.add(line)

    for key in cur_keys_order:
        if key not in used_keys:
            out_lines.append(cur_map[key])
    for line in cur_extra_lines:
        if line not in seen_extra:
            out_lines.append(line)
            seen_extra.add(line)

    return "\n".join(out_lines)


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
    last_open_dir: str = ""
    editor_bg_color: str = "#ffffff"
    work_history_limit: int = 10
    fetch_history_limit: int = 50
    original_cache_limit: int = 50
    glossary_auto_search: bool = True
    diff_save_mode: bool = False
    editor_default_wysiwyg: bool = False
    # 儲存 HTML 時是否把字型 Base64 內嵌到 <head>（離線手機可正確顯示，
    # 代價是檔案會增大約 1MB+）；先固定嵌入 fonts/monapo.ttf。
    embed_font_in_html: bool = False
    embed_font_name: str = "monapo"


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
            cache.last_open_dir = data.get('last_open_dir', '')
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
            cache.diff_save_mode = bool(data.get(
                'diff_save_mode', cache.diff_save_mode))
            cache.embed_font_in_html = bool(data.get(
                'embed_font_in_html', cache.embed_font_in_html))
            cache.editor_default_wysiwyg = bool(data.get(
                'editor_default_wysiwyg', cache.editor_default_wysiwyg))
            cache.embed_font_name = str(data.get(
                'embed_font_name', cache.embed_font_name))
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
                'last_open_dir': cache.last_open_dir,
                'editor_bg_color': cache.editor_bg_color,
                'work_history_limit': cache.work_history_limit,
                'fetch_history_limit': cache.fetch_history_limit,
                'original_cache_limit': cache.original_cache_limit,
                'glossary_auto_search': cache.glossary_auto_search,
                'diff_save_mode': cache.diff_save_mode,
                'embed_font_in_html': cache.embed_font_in_html,
                'editor_default_wysiwyg': cache.editor_default_wysiwyg,
                'embed_font_name': cache.embed_font_name,
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

    def append_url_history(self, entry: dict, max_items: int = 50) -> None:
        """新增一筆 URL 歷史（讀檔 → 以 url 去重 → append 於末端 → 寫回）。

        採 newest-last 慣例，與 aa_url_fetch_qt.py 既有 `reversed(url_history)` 顯示一致。
        """
        url = (entry or {}).get('url', '')
        if not url:
            return
        cache_file = self.get_cache_file()
        with locked_file(cache_file + '.lock'):
            data = self._read_cache_raw()
            hist = data.get('url_history', []) or []
            hist = [h for h in hist
                    if isinstance(h, dict) and h.get('url') != url]
            hist.append(dict(entry))
            data['url_history'] = hist[-max_items:]
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
