"""跨平台 sidecar 檔案鎖（advisory），用於保護 aa_settings_cache.json 的讀-合-寫。

使用方式：

    from aa_tool.file_lock import locked_file
    with locked_file(cache_file + '.lock', timeout=5.0):
        # 讀檔、合併、原子寫檔
        ...

Windows 使用 `msvcrt.locking()`，POSIX 使用 `fcntl.flock()`。
鎖加在 sidecar 檔案（`<path>`）上，避免與 `os.replace()` 原子寫入互斥
（replace 會更換 inode，若鎖在目標檔上會失效）。
"""
from __future__ import annotations

import contextlib
import os
import time
from typing import Iterator

try:
    import msvcrt  # type: ignore[import-not-found]
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

try:
    import fcntl  # type: ignore[import-not-found]
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


@contextlib.contextmanager
def locked_file(lock_path: str, timeout: float = 5.0,
                poll_interval: float = 0.05) -> Iterator[None]:
    """取得 sidecar 檔案的獨占鎖；逾時則放行（避免死等卡死主執行緒）。"""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                if _HAS_MSVCRT:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif _HAS_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_interval)
        yield
    finally:
        if acquired:
            try:
                if _HAS_MSVCRT:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                    except OSError:
                        pass
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                elif _HAS_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
