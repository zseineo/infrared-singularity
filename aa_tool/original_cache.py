"""原文暫存 (``aa_original_cache.json``) 的共用 I/O。

由 `aa_main_qt`（手動流程）與 `aa_auto_translate`（自動流程）共用。
索引使用「投稿標頭指紋」（日期 + 時間.毫秒 + 作者 ID），由伺服器產生、
翻譯過程不會動到，可作為跨檔名命名的穩定備援索引。

設計重點：
- 純 I/O，無 GUI 依賴；可在 CLI 自動翻譯流程內被呼叫。
- 寫入採「原子寫」(`temp + os.replace`)，多執行緒／多程序同時寫不容易壞。
- 上限裁切以「時間戳最新的 N 筆」保留，避免長期執行後檔案膨脹。
"""
from __future__ import annotations

import json
import os
import re
import time

from .html_io import read_html_pre_content

# 投稿標頭指紋：日期 + 時間.毫秒 + 作者 ID，例：
#   "2023/04/02(日) 20:54:38.52 ID:5UkYdPSV"
_AUTHOR_FP_FULL_RE = re.compile(
    r'\d{4}/\d{1,2}/\d{1,2}\([^)\s]+\)\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?'
    r'\s*ID:[A-Za-z0-9+/]+'
)
# fallback：無 ID 的老格式（5ch 早期），只取日期 + 時間
_AUTHOR_FP_DATE_RE = re.compile(
    r'\d{4}/\d{1,2}/\d{1,2}\([^)\s]+\)\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?'
)

CACHE_FILENAME = 'aa_original_cache.json'
DEFAULT_LIMIT = 50


def compute_fingerprint(text: str) -> str | None:
    """從文字中抽出第一個投稿標頭指紋；找不到回 None。"""
    if not text:
        return None
    m = _AUTHOR_FP_FULL_RE.search(text)
    if not m:
        m = _AUTHOR_FP_DATE_RE.search(text)
    if not m:
        return None
    return re.sub(r'\s+', ' ', m.group(0)).strip()


def _cache_path(base_dir: str) -> str:
    return os.path.join(base_dir, CACHE_FILENAME)


def load_data(base_dir: str) -> dict:
    p = _cache_path(base_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_data(base_dir: str, data: dict) -> None:
    """原子寫：先寫暫存檔再 ``os.replace``，減少同寫衝突。"""
    target = _cache_path(base_dir)
    tmp = target + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError:
        pass


def save_entry(
    base_dir: str,
    original_text: str,
    *,
    extracted: str = "",
    translation: str = "",
    limit: int = DEFAULT_LIMIT,
) -> str | None:
    """寫入暫存；回傳寫入的 fingerprint key，或 None（沒指紋而略過）。

    `limit`：條目上限，超過時以時間戳保留最新 N 筆。
    """
    if not original_text:
        return None
    key = compute_fingerprint(original_text)
    if not key:
        return None
    data = load_data(base_dir)
    entry: dict = {'text': original_text, 'ts': time.time()}
    if extracted:
        entry['extracted'] = extracted
    if translation:
        entry['translation'] = translation
    data[key] = entry
    if len(data) > limit:
        ordered = sorted(
            data.items(), key=lambda kv: kv[1].get('ts', 0), reverse=True)
        data = dict(ordered[:limit])
    save_data(base_dir, data)
    return key


def load_entry_for_html(base_dir: str, html_file_path: str) -> dict | None:
    """依 html 檔的 ``<pre>`` 算指紋，從暫存找對應 entry。

    相容舊版以檔名 basename 為 key、entry 內存 `author_key` 作備援的格式：
    直接以指紋查不到時，掃 values 比對 `author_key` 或重算指紋。
    """
    if not html_file_path:
        return None
    try:
        pre = read_html_pre_content(html_file_path)
    except OSError:
        return None
    if not pre:
        return None
    target_fp = compute_fingerprint(pre)
    if not target_fp:
        return None
    data = load_data(base_dir)
    entry = data.get(target_fp)
    if isinstance(entry, dict) and isinstance(entry.get('text'), str) and entry['text']:
        return entry
    for cached_entry in data.values():
        if not isinstance(cached_entry, dict):
            continue
        if not isinstance(cached_entry.get('text'), str) or not cached_entry['text']:
            continue
        entry_fp = (cached_entry.get('author_key')
                    or compute_fingerprint(cached_entry['text']))
        if entry_fp == target_fp:
            return cached_entry
    return None


def load_text_for_html(base_dir: str, html_file_path: str) -> str | None:
    """便利函式：只回 entry 的 ``text``，找不到回 None。"""
    entry = load_entry_for_html(base_dir, html_file_path)
    return entry['text'] if entry else None
