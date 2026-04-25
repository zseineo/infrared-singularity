"""啟動期錯誤日誌：捕捉 C++ 層 segfault、Python 未捕捉例外與 Qt 訊息。

用法（於 `aa_main_qt.py` 的 `main()` 最前面呼叫一次）：
    from aa_tool.crash_logger import install_crash_logger
    install_crash_logger()

日誌寫到專案根目錄的 `aa_crash.log`（append 模式，保留跨次啟動紀錄）。
閃退時 `faulthandler` 會直接把 C 堆疊寫入檔案，故事後可打開檢視。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import time
import traceback

_LOG_PATH: str | None = None
_LOG_FILE = None  # 保留 handle 給 faulthandler 使用；不可關閉


def _log_path() -> str:
    global _LOG_PATH
    if _LOG_PATH is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _LOG_PATH = os.path.join(root, "aa_crash.log")
    return _LOG_PATH


def _append(msg: str) -> None:
    try:
        with open(_log_path(), 'a', encoding='utf-8') as f:
            f.write(msg)
    except OSError:
        pass


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    header = f"\n[{ts}] Uncaught Python exception:\n"
    body = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _append(header + body)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _qt_message_handler(mode, context, message) -> None:
    try:
        from PyQt6.QtCore import QtMsgType
        label = {
            QtMsgType.QtDebugMsg: 'DEBUG',
            QtMsgType.QtInfoMsg: 'INFO',
            QtMsgType.QtWarningMsg: 'WARNING',
            QtMsgType.QtCriticalMsg: 'CRITICAL',
            QtMsgType.QtFatalMsg: 'FATAL',
        }.get(mode, str(mode))
    except Exception:
        label = str(mode)
    if label in ('DEBUG', 'INFO'):
        return  # 省噪音
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    ctx = ''
    try:
        if context is not None and context.file:
            ctx = f' ({context.file}:{context.line})'
    except Exception:
        pass
    _append(f"[{ts}] Qt {label}: {message}{ctx}\n")


def install_crash_logger() -> None:
    """安裝所有錯誤攔截器；可重複呼叫安全。"""
    global _LOG_FILE
    path = _log_path()
    # 啟動標記
    _append(f"\n=== Session start {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(pid={os.getpid()}) ===\n")

    # 1) C 層崩潰：faulthandler 需要持續開啟的檔案 handle
    try:
        if _LOG_FILE is None:
            _LOG_FILE = open(path, 'a', encoding='utf-8', buffering=1)
        faulthandler.enable(file=_LOG_FILE, all_threads=True)
    except Exception as e:
        _append(f"[init] faulthandler.enable failed: {e}\n")

    # 2) 未捕捉的 Python 例外
    sys.excepthook = _excepthook

    # 3) Qt 的 warning/critical/fatal
    try:
        from PyQt6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_qt_message_handler)
    except Exception as e:
        _append(f"[init] qInstallMessageHandler failed: {e}\n")
