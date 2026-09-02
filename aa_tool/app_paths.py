"""設定檔存放位置的單一真相來源。

**為什麼要有這個模組**：設定／暫存／金鑰過去散在程式根目錄，換版本時使用者得
自己認出六個檔名逐一複製，而且打包版（PyInstaller）解壓到新資料夾就等於全部
重來。改成集中存放於 ``%APPDATA%\\AATool\\``後：

* 更新程式（重新解壓、覆蓋 exe）不會動到設定，不必每次重設；
* 要手動轉移只需複製這一個資料夾；
* 原本 `settings_manager` / `secure_store` / `original_cache` / `gemini_api`
  都已經是「收 ``base_dir`` 參數」的純 I/O 模組，因此**只要把 base_dir 指到這裡
  即可**，不必改動各模組的讀寫邏輯。

**版本相容（新舊目錄都要能讀）**：`auto_migrate()` 在每次啟動時把程式根目錄
還存在、但新資料夾還沒有的受管檔案**複製**過來（原檔保留，舊版程式仍可用）。
因為是「逐檔補缺」而非「一次性旗標」，即使使用者中途又用舊版程式跑出新的設定
檔，下次啟動一樣會被接過來。
"""
from __future__ import annotations

import os
import shutil
import sys

#: `%APPDATA%` 底下的資料夾名稱
APP_DIR_NAME = "AATool"

#: 統一存放於 `data_dir()` 的檔案。`aa_settings_cache.json` 的 `.lock` / `.bak`
#: sidecar 由 `file_lock` / `settings_manager` 自行產生在同目錄，不必列出。
MANAGED_FILES = (
    "AA_Settings.json",        # 正則／過濾／術語表
    "aa_settings_cache.json",  # UI 狀態、自動翻譯參數、網址紀錄
    "aa_api_keys.dat",         # DPAPI 加密金鑰
    "aa_original_cache.json",  # 原文暫存
    "aa_api_quota.json",       # Gemini RPD 冷卻狀態
    "aa_crash.log",            # 閃退日誌
)

#: 「匯入舊版設定」可搬的檔案——排除 crash log：那是本機執行紀錄，
#: 匯入舊的只會蓋掉目前這次啟動的紀錄，對使用者沒有價值。
IMPORTABLE_FILES = tuple(f for f in MANAGED_FILES if f != "aa_crash.log")

#: 覆蓋匯入前自動留下的備份後綴（誤按「匯入」時還救得回來）
IMPORT_BACKUP_SUFFIX = ".before-import"

#: 進階／測試用：指定後改用該路徑當設定資料夾
ENV_OVERRIDE = "AATOOL_DATA_DIR"

_data_dir: str | None = None
_migrated = False


def app_root() -> str:
    """程式所在目錄。

    frozen（PyInstaller）時 ``__file__`` 指向 ``_internal/``，必須改用 exe 旁的
    目錄，否則會去 ``_internal/`` 找舊設定而漏掉真正的舊檔。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, APP_DIR_NAME)


def data_dir() -> str:
    """設定資料夾（必要時建立）。

    建不出來時**退回程式根目錄**而不是丟例外：設定存哪裡不該讓程式開不起來，
    退回後行為與舊版完全相同。結果會快取，同一次執行內路徑固定。
    """
    global _data_dir
    if _data_dir is None:
        d = (os.environ.get(ENV_OVERRIDE) or "").strip() or _default_data_dir()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = app_root()
        _data_dir = d
    return _data_dir


def path_for(name: str) -> str:
    """取得受管檔案在設定資料夾中的完整路徑。"""
    return os.path.join(data_dir(), name)


def list_settings_files(folder: str) -> list[str]:
    """列出該資料夾中「存在的」受管檔案名稱（用來判斷這層是不是設定資料夾）。"""
    if not folder:
        return []
    try:
        return [n for n in MANAGED_FILES
                if os.path.isfile(os.path.join(folder, n))]
    except OSError:
        return []


def detect_settings_dir(folder: str) -> str | None:
    """從使用者挑的資料夾找出真正放設定的那一層；找不到回 None。

    使用者可能挑到三種東西，都要能認：
    1. 舊版程式資料夾（v2.14 以前設定就在根目錄）；
    2. 備份下來的 ``AATool`` 資料夾本身；
    3. 上述 ``AATool`` 的**上一層**（例如整包備份的 Roaming 資料夾）。
    """
    if not folder or not os.path.isdir(folder):
        return None
    for cand in (folder, os.path.join(folder, APP_DIR_NAME)):
        if os.path.isdir(cand) and list_settings_files(cand):
            return cand
    return None


def copy_settings_from(src_dir: str, *, overwrite: bool,
                       names: tuple[str, ...] = MANAGED_FILES,
                       ) -> tuple[list[str], list[str]]:
    """把 `src_dir` 的受管檔案複製進設定資料夾。回傳 (已複製, 已略過)。

    `overwrite=False`（自動遷移）只補新資料夾缺少的檔案；`overwrite=True`
    （手動匯入舊版設定）會覆蓋，但先把現有檔複製成
    ``<檔名>.before-import`` 備份。來源與目的相同時直接不做事。
    """
    copied: list[str] = []
    skipped: list[str] = []
    dst_dir = data_dir()
    if not src_dir or not os.path.isdir(src_dir):
        return copied, skipped
    if os.path.normcase(os.path.abspath(src_dir)) == \
            os.path.normcase(os.path.abspath(dst_dir)):
        return copied, skipped
    for name in names:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dst_dir, name)
        exists = os.path.exists(dst)
        if exists and not overwrite:
            skipped.append(name)
            continue
        try:
            if exists:
                shutil.copy2(dst, dst + IMPORT_BACKUP_SUFFIX)
            shutil.copy2(src, dst)
            copied.append(name)
        except OSError:
            skipped.append(name)
    return copied, skipped


def auto_migrate() -> list[str]:
    """啟動時的舊設定接收：從程式根目錄**複製**缺少的受管檔案過來。

    採複製（不是搬移）是刻意的：根目錄原檔保留，同一份資料夾若還會用舊版程式
    開，舊版仍讀得到自己的設定。代價是兩邊之後會各自演進，故僅在新資料夾
    「還沒有該檔」時才補，不會反覆覆蓋。

    每個 process 只跑一次，回傳這次複製過來的檔名。
    """
    global _migrated
    if _migrated:
        return []
    _migrated = True
    copied, _ = copy_settings_from(app_root(), overwrite=False)
    return copied


def import_settings_from(folder: str) -> tuple[str | None, list[str]]:
    """「匯入舊版設定」：從使用者挑的資料夾覆蓋匯入。

    回傳 (實際來源資料夾, 已匯入的檔名)；資料夾內找不到任何設定檔時回 (None, [])。
    """
    src = detect_settings_dir(folder)
    if not src:
        return None, []
    copied, _ = copy_settings_from(src, overwrite=True, names=IMPORTABLE_FILES)
    return src, copied
