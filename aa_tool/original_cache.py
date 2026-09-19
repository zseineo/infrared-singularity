"""原文暫存的共用 I/O（原文＋提取結果＋填入翻譯）。

由 `aa_main_qt`（手動流程）與 `aa_auto_translate`（自動流程）共用。
索引使用「投稿標頭指紋」（日期 + 時間.毫秒 + 作者 ID），由伺服器產生、
翻譯過程不會動到，可作為跨檔名的穩定索引——**存檔時的檔名與之後改的檔名
都不影響命中**。實測 7222 個已存檔的 HTML，指紋算得出來的有 7220 個（100%）。

**儲存格式（v2.51 起）：一筆一檔**
``<base_dir>/originals/<指紋 slug>-<雜湊>.json.gz``，每筆約 30～40 KB（gzip）。

v2.50 以前是單一 ``aa_original_cache.json``：一筆平均 163 KB，每次存檔都要把
整包讀進來再整包寫回去（實測 300 筆＝84 MB），因此上限只能設幾百筆——使用者
7236 個已存檔的檔案裡只有 4% 對得回原文。改成一筆一檔後，存檔只寫自己那一個
小檔、開檔只讀要用的那一個，上限才有可能拉到數千筆。舊格式會在第一次使用時
自動搬進新資料夾（見 `migrate_legacy`），原檔保留不動。

設計重點：
- 純 I/O，無 GUI 依賴；可在 CLI 自動翻譯流程內被呼叫。
- 寫入採「原子寫」(`temp + os.replace`)，多執行緒／多程序同時寫不容易壞。
- 上限裁切以「檔案 mtime 最新的 N 筆」保留；`limit <= 0` ＝不限制。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time

from .html_io import read_html_pre_content

# 投稿標頭指紋：日期 + 時間.毫秒 + 作者 ID，例：
#   "2023/04/02(日) 20:54:38.52 ID:5UkYdPSV"
# 涵蓋常見的各種標頭寫法（名前欄有無作者名、有無 ◆trip 都不影響）：
#   モルフォ ◆Zd66W2lR/c ： 2026/07/26(日) 17:50:44 ID:YttDRKKY
#    ◆rdCVKFIl72 ： 2023/08/18(金) 18:56:34 ID:x0xJW6xU
#   123 名前： ◆e8w1Y2fOxM[] 投稿日：2015/05/27(水) 19:32:03 ID:Ru3wlsPY
# 刻意不用 ◆trip 當索引：**有相當比例的貼文沒有 trip**（實測 7222 個檔中
# 只有 95% 取得出 trip，指紋則是 100%），而且同一作者同一天的多話會撞在一起。
_AUTHOR_FP_FULL_RE = re.compile(
    r'\d{4}/\d{1,2}/\d{1,2}\([^)\s]+\)\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?'
    r'\s*ID:[A-Za-z0-9+/]+'
)
# fallback：無 ID 的老格式（5ch 早期），只取日期 + 時間
_AUTHOR_FP_DATE_RE = re.compile(
    r'\d{4}/\d{1,2}/\d{1,2}\([^)\s]+\)\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?'
)

#: 舊格式（單一 JSON）的檔名；仍用於搬移與「清除暫存」
CACHE_FILENAME = 'aa_original_cache.json'
#: 新格式的資料夾名稱
STORE_DIRNAME = 'originals'
#: 搬移完成的標記檔（放在新資料夾內）
_MIGRATED_MARK = '.migrated'

DEFAULT_LIMIT = 5000


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


# ── 路徑 ──

def store_dir(base_dir: str) -> str:
    return os.path.join(base_dir, STORE_DIRNAME)


def _legacy_path(base_dir: str) -> str:
    return os.path.join(base_dir, CACHE_FILENAME)


_UNSAFE_RE = re.compile(r'[^0-9A-Za-z]+')


def entry_filename(fingerprint: str) -> str:
    """指紋 → 檔名。可讀的 slug ＋ 指紋雜湊前 8 碼。

    指紋含 ``/ : ( )`` 與可能出現在 ID 裡的 ``/ +``，不能直接當檔名；slug 會把
    這些字換成 ``_``，因此不同指紋可能 slug 相同（例如 ID 只差一個 ``/`` 與
    ``+``），故一律附上原指紋的雜湊前 8 碼作為區辨，查詢時直接算得出來、
    不需要索引檔。
    """
    slug = _UNSAFE_RE.sub('_', fingerprint).strip('_')[:60]
    digest = hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:8]
    return f"{slug}-{digest}.json.gz"


def entry_path(base_dir: str, fingerprint: str) -> str:
    return os.path.join(store_dir(base_dir), entry_filename(fingerprint))


# ── 讀寫 ──

def load_entry(base_dir: str, fingerprint: str) -> dict | None:
    """讀取單筆；檔案不存在或壞掉回 None。"""
    if not fingerprint:
        return None
    path = entry_path(base_dir, fingerprint)
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError, EOFError):
        return None
    return entry if isinstance(entry, dict) else None


def save_entry(
    base_dir: str,
    original_text: str,
    *,
    extracted: str = "",
    translation: str = "",
    limit: int = DEFAULT_LIMIT,
) -> str | None:
    """寫入暫存；回傳寫入的 fingerprint，或 None（沒指紋而略過）。

    `limit`：條目上限，超過時以檔案 mtime 保留最新 N 筆；`<= 0` ＝不限制。
    """
    if not original_text:
        return None
    key = compute_fingerprint(original_text)
    if not key:
        return None
    migrate_legacy(base_dir)
    entry: dict = {'fp': key, 'text': original_text, 'ts': time.time()}
    # 沒帶到的欄位沿用既有值：存檔路徑不一定每次都拿得到提取／翻譯（例如開舊檔
    # 後直接在編輯器存檔），整筆覆蓋會把之前存好的那兩欄洗掉。
    old = load_entry(base_dir, key) or {}
    for field, value in (('extracted', extracted), ('translation', translation)):
        value = value or old.get(field) or ""
        if value:
            entry[field] = value
    if _write_entry(base_dir, key, entry):
        _prune(base_dir, limit)
    return key


def _write_entry(base_dir: str, fingerprint: str, entry: dict) -> bool:
    """原子寫單筆（先寫 .tmp 再 os.replace）。成功回 True。"""
    path = entry_path(base_dir, fingerprint)
    tmp = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(tmp, 'wt', encoding='utf-8') as f:
            json.dump(entry, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _entry_files(base_dir: str) -> list[os.DirEntry]:
    try:
        return [e for e in os.scandir(store_dir(base_dir))
                if e.is_file() and e.name.endswith('.json.gz')]
    except OSError:
        return []


def _prune(base_dir: str, limit: int) -> None:
    """超過上限時刪掉最舊的（依 mtime）。`limit <= 0` ＝不限制。"""
    if limit is None or limit <= 0:
        return
    files = _entry_files(base_dir)
    if len(files) <= limit:
        return
    try:
        files.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        return
    for e in files[limit:]:
        try:
            os.remove(e.path)
        except OSError:
            pass


def load_entry_for_html(base_dir: str, html_file_path: str) -> dict | None:
    """依 html 檔的 ``<pre>`` 算指紋，從暫存找對應 entry。

    指紋取自投稿標頭（伺服器產生），翻譯不會改到它，所以譯文檔也算得出
    與原文相同的指紋；檔名怎麼改都不影響。
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
    migrate_legacy(base_dir)
    entry = load_entry(base_dir, target_fp)
    if isinstance(entry, dict) and isinstance(entry.get('text'), str) and entry['text']:
        return entry
    return None


def load_text_for_html(base_dir: str, html_file_path: str) -> str | None:
    """便利函式：只回 entry 的 ``text``，找不到回 None。"""
    entry = load_entry_for_html(base_dir, html_file_path)
    return entry['text'] if entry else None


# ── 舊格式搬移／統計／清除 ──

def migrate_legacy(base_dir: str) -> int:
    """把舊的單一 ``aa_original_cache.json`` 搬進新資料夾，回傳搬了幾筆。

    只做一次（新資料夾內留 `.migrated` 標記）；**舊檔保留不動**，使用者確認
    沒問題後可自行在設定的「清除暫存」一併刪除。舊格式的 key 可能是指紋、也
    可能是更舊版的檔名（entry 內另存 `author_key`），兩種都以「重算 entry 內
    text 的指紋」為準，算不出來才退回用 key 本身。
    """
    sdir = store_dir(base_dir)
    mark = os.path.join(sdir, _MIGRATED_MARK)
    if os.path.exists(mark):
        return 0
    legacy = _legacy_path(base_dir)
    moved = 0
    if os.path.exists(legacy):
        try:
            with open(legacy, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            for key, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                text = entry.get('text')
                if not isinstance(text, str) or not text:
                    continue
                fp = (compute_fingerprint(text) or entry.get('author_key')
                      or key)
                if not fp or os.path.exists(entry_path(base_dir, fp)):
                    continue
                new_entry = dict(entry)
                new_entry['fp'] = fp
                new_entry.setdefault('ts', time.time())
                if _write_entry(base_dir, fp, new_entry):
                    moved += 1
    try:
        os.makedirs(sdir, exist_ok=True)
        with open(mark, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
    except OSError:
        pass
    return moved


def store_stats(base_dir: str) -> tuple[int, int]:
    """回傳 (筆數, 總位元組)；含舊格式單一 JSON 的大小（若還在）。"""
    files = _entry_files(base_dir)
    total = 0
    for e in files:
        try:
            total += e.stat().st_size
        except OSError:
            pass
    legacy = _legacy_path(base_dir)
    try:
        total += os.path.getsize(legacy)
    except OSError:
        pass
    return len(files), total


def clear_store(base_dir: str) -> None:
    """清空所有原文暫存（含舊格式的單一 JSON）。"""
    for e in _entry_files(base_dir):
        try:
            os.remove(e.path)
        except OSError:
            pass
    legacy = _legacy_path(base_dir)
    try:
        if os.path.exists(legacy):
            os.remove(legacy)
    except OSError:
        pass
    # 舊檔刪掉後不必再搬移；標記留著避免下次又去找
    try:
        os.makedirs(store_dir(base_dir), exist_ok=True)
        with open(os.path.join(store_dir(base_dir), _MIGRATED_MARK),
                  'w', encoding='utf-8') as f:
            f.write(str(time.time()))
    except OSError:
        pass
