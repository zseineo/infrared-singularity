import json
import os
from dataclasses import dataclass, field

from .constants import (
    DEFAULT_BASE_REGEX, DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX,
    DEFAULT_BG_COLOR, DEFAULT_FG_COLOR,
)


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
    experimental_edit_tab: bool = False


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
            'filter': settings.filter_text,
            'glossary': settings.glossary,
            'glossary_temp': settings.glossary_temp,
            'base_regex': settings.base_regex,
            'invalid_regex': settings.invalid_regex,
            'symbol_regex': settings.symbol_regex,
        }
        try:
            with open(self.get_settings_file(), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("AA_Settings.json save failed:", e)

    def save_regex_to_settings(self, base: str, invalid: str, symbol: str) -> None:
        """僅更新 AA_Settings.json 中的三條正則表達式，保留其他欄位。"""
        settings_file = self.get_settings_file()
        data: dict = {}
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                pass
        data['base_regex'] = base
        data['invalid_regex'] = invalid
        data['symbol_regex'] = symbol
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
            cache.url_related_links = data.get('url_related_links', [])
            cache.current_url = data.get('current_url', '')
            cache.auto_copy = bool(data.get('auto_copy', False))
            cache.batch_folder = data.get('batch_folder', '')
            cache.experimental_edit_tab = bool(data.get('experimental_edit_tab', False))
        except Exception as e:
            print("Cache load failed:", e)
        return cache

    def save_cache(self, cache: AppCache) -> None:
        """將 AppCache 寫入 aa_settings_cache.json。"""
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
            'url_history': cache.url_history,
            'url_related_links': cache.url_related_links,
            'current_url': cache.current_url,
            'auto_copy': cache.auto_copy,
            'batch_folder': cache.batch_folder,
            'experimental_edit_tab': cache.experimental_edit_tab,
        }
        try:
            with open(self.get_cache_file(), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print("Cache save failed:", e)
