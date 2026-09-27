"""自動翻譯的 Debug Log：勾選後每次執行寫一份詳細記錄檔，供回報問題時附上。

使用者回報的狀況（瀏覽器卡住、Timeout 30000ms…）開發端常重現不出來，光看面板
Log 也看不出是哪一步卡住、頁面是變慢還是當掉。這裡記的是面板 Log 之外的細節：
每一步的時間點與耗時、頁面健康度（回應延遲／DOM 元素數／JS 記憶體）、系統可用
記憶體、瀏覽器事件（崩潰、JS 錯誤、導向），以及錯誤的完整 traceback 與截圖。

**不記**：API 金鑰、送出／回覆的全文（只記行數與長度）。

檔案放在設定資料夾的 ``debug_logs/``，只保留最新 ``_KEEP`` 份（連同截圖）。
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
import threading
import time
import traceback

from aa_tool import app_paths

DIR_NAME = "debug_logs"
_KEEP = 20                 # 保留最新幾份 log（截圖依附在同一次執行，一起清）
_MAX_EXC_CHARS = 8000      # 單筆例外訊息上限（Playwright 的 Call log 會含整話原文）


def debug_dir(base_dir: str | None = None) -> str:
    return os.path.join(base_dir or app_paths.data_dir(), DIR_NAME)


def system_memory() -> str:
    """系統實體記憶體「可用／總量」；非 Windows 或讀不到回空字串。"""
    if sys.platform != "win32":
        return ""

    class _MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    st = _MemStatus()
    st.dwLength = ctypes.sizeof(_MemStatus)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return ""
    except Exception:  # noqa: BLE001 — 只是輔助資訊
        return ""
    gb = 1024 ** 3
    return (f"系統記憶體可用 {st.ullAvailPhys / gb:.1f}／{st.ullTotalPhys / gb:.1f} GB"
            f"（使用率 {st.dwMemoryLoad}%）")


class DebugLog:
    """一次執行一份檔案；每筆即時 flush（程式卡死或被強制關掉時也留得下來）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.dir = os.path.dirname(path)
        self._stem = os.path.splitext(os.path.basename(path))[0]
        self._lock = threading.Lock()
        self._shots = 0
        self._f = open(path, "w", encoding="utf-8", newline="")

    def write(self, msg: str) -> None:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
        line = f"[{stamp}] {msg}"
        with self._lock:
            if self._f.closed:
                return
            self._f.write(line.replace("\n", "\n    ") + "\n")
            self._f.flush()

    def exception(self, title: str, e: BaseException) -> None:
        """完整例外（含 traceback 與 Playwright 的 Call log），過長截斷。"""
        text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        if len(text) > _MAX_EXC_CHARS:
            text = text[:_MAX_EXC_CHARS] + f"\n…（以下省略 {len(text) - _MAX_EXC_CHARS} 字）"
        self.write(f"‼ {title}\n{text}")

    def screenshot_path(self, tag: str) -> str:
        self._shots += 1
        return os.path.join(self.dir, f"{self._stem}_{self._shots:02d}_{tag}.png")

    def close(self) -> None:
        with self._lock:
            if not self._f.closed:
                self._f.close()


def _prune(folder: str) -> None:
    """只留最新 _KEEP 份 log；被刪掉的那幾次執行的截圖一併刪。"""
    try:
        logs = sorted((n for n in os.listdir(folder) if n.endswith(".log")), reverse=True)
    except OSError:
        return
    for old in logs[_KEEP:]:
        stem = old[:-4]
        for n in os.listdir(folder):
            if n == old or (n.startswith(stem + "_") and n.endswith(".png")):
                try:
                    os.remove(os.path.join(folder, n))
                except OSError:
                    pass


def start(base_dir: str | None = None) -> DebugLog:
    """建立新的一份 Debug Log（檔名含時間），寫入系統資訊表頭。"""
    folder = debug_dir(base_dir)
    os.makedirs(folder, exist_ok=True)
    _prune(folder)
    name = time.strftime("auto_translate_%Y%m%d_%H%M%S.log")
    dlog = DebugLog(os.path.join(folder, name))
    try:
        from importlib.metadata import version
        pw_ver = version("playwright")
    except Exception:  # noqa: BLE001 — 打包版可能讀不到套件資訊
        pw_ver = "?"
    dlog.write(f"OS：{platform.platform()}｜Python {platform.python_version()}"
               f"｜{'打包版' if getattr(sys, 'frozen', False) else '原始碼執行'}"
               f"｜Playwright {pw_ver}")
    mem = system_memory()
    if mem:
        dlog.write(mem)
    return dlog
