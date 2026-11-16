"""程式自動更新：查 GitHub Release 最新版 → 下載 zip → SHA256 驗證 → 安全解壓 → 關閉後替換。

流程（只在使用者按「檢查更新」時才連網）：

1. ``check_latest()`` 讀 ``releases/latest``，找 ``aa_main_qt_v<版本>.zip`` 與同名
   ``.sha256`` 兩個附件；版本比目前新才算有更新（不會降版）。
2. ``prepare_update()`` 下載 zip 與 .sha256 到暫存資料夾，比對 SHA256（GitHub API
   附件的 ``digest`` 有值時也要一致），通過才解壓（擋 ``..``／絕對路徑、限制解壓總量）。
3. 執行中的 exe 不能覆寫自己，所以由「新版的 exe」來換檔：``launch_apply()`` 以
   ``aa_main_qt.exe --apply-update <程式資料夾> <舊程式 PID>`` 啟動暫存裡的新版後，
   舊程式自行關閉；新版 ``run_apply()`` 等舊程式結束 → 把程式資料夾裡要換的項目先
   改名到備份資料夾 → 複製新檔 → 成功就刪備份、失敗就還原，最後重新開啟程式資料夾的
   ``aa_main_qt.exe``。結果寫進設定資料夾的 ``aa_update_result.json``，下次啟動顯示。

只換新版 zip 裡有的項目（兩個 exe 與 ``_internal``）；程式資料夾裡的其他檔案（舊版留在
根目錄的設定等）不動。設定資料夾（``%APPDATA%\\AATool``）本來就不在程式資料夾裡。

安全：只連 ``https``、只接受 GitHub 的網域（含轉址後的下載網址）、固定 repo、
附件大小上限、SHA256 比對、解壓防 zip slip。只用標準庫。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable

from . import app_paths, net_proxy

REPO = "zseineo/infrared-singularity"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
EXE_NAME = "aa_main_qt.exe"
ASSET_RE = re.compile(r"^aa_main_qt_v(\d+(?:\.\d+)*)\.zip$")
RESULT_FILE = "aa_update_result.json"
APPLY_ARG = "--apply-update"

_MAX_ZIP_BYTES = 1024 ** 3            # 附件上限 1 GB（目前約 100 MB）
_MAX_UNZIP_BYTES = 3 * 1024 ** 3      # 解壓總量上限 3 GB（擋壓縮炸彈）
_ALLOWED_HOSTS = ("github.com", "api.github.com")
_ALLOWED_HOST_SUFFIX = ".githubusercontent.com"   # 附件實際下載點（轉址後）
_WAIT_EXIT_SECONDS = 90               # 等舊程式關閉的上限


class UpdateError(Exception):
    """更新流程中可以直接顯示給使用者的錯誤。"""


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    zip_name: str
    zip_url: str
    zip_size: int
    zip_digest: str      # GitHub 算的 "sha256:…"；舊附件可能沒有（空字串）
    sha_url: str


def parse_version(v: str) -> tuple[int, ...]:
    """「v3.27」「3.27」→ (3, 27)；數字逐段比，3.10 > 3.9。認不出來回 ()。"""
    m = re.match(r"^v?(\d+(?:\.\d+)*)$", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a) and bool(b) and a > b


def _check_url(url: str) -> None:
    """只允許 https 與 GitHub 的網域。"""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not (
            host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_HOST_SUFFIX)):
        raise UpdateError(f"拒絕連到非 GitHub 的網址：{url[:120]}")


def _open(url: str, timeout: float, accept: str = ""):
    _check_url(url)
    headers = {"User-Agent": net_proxy.USER_AGENT}
    if accept:
        headers["Accept"] = accept
    resp = net_proxy.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
    _check_url(resp.geturl())   # 轉址後的實際網址也要是 GitHub
    return resp


def check_latest(timeout: float = 20) -> ReleaseInfo:
    """讀 GitHub 最新 Release；缺 zip 或 .sha256 附件時丟 UpdateError。"""
    try:
        with _open(LATEST_API, timeout, "application/vnd.github+json") as r:
            data = json.loads(r.read().decode("utf-8"))
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"無法連到 GitHub 檢查更新：{e}") from None
    tag = str(data.get("tag_name") or "")
    assets = {a.get("name", ""): a for a in data.get("assets") or []}
    zip_asset = next((a for n, a in assets.items() if ASSET_RE.match(n)), None)
    if zip_asset is None:
        raise UpdateError(f"最新版 {tag} 沒有附 zip 安裝檔，無法自動更新")
    zip_name = zip_asset["name"]
    version = ASSET_RE.match(zip_name).group(1)
    sha_asset = assets.get(zip_name + ".sha256")
    if sha_asset is None:
        raise UpdateError(f"最新版 {tag} 沒有附 SHA256 檔，為了安全不自動更新")
    return ReleaseInfo(
        version=version, tag=tag, notes=str(data.get("body") or ""),
        zip_name=zip_name, zip_url=zip_asset["browser_download_url"],
        zip_size=int(zip_asset.get("size") or 0),
        zip_digest=str(zip_asset.get("digest") or ""),
        sha_url=sha_asset["browser_download_url"])


def _download(url: str, dest: str, expected_size: int = 0,
              progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> None:
    part = dest + ".part"
    got = 0
    with _open(url, 60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or expected_size or 0)
        if total > _MAX_ZIP_BYTES:
            raise UpdateError(f"更新檔大得不合理（{total} bytes），停止下載")
        while True:
            if cancelled is not None and cancelled():
                raise UpdateError("已取消下載")
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            got += len(chunk)
            if got > _MAX_ZIP_BYTES:
                raise UpdateError("更新檔超過大小上限，停止下載")
            f.write(chunk)
            if progress is not None:
                progress(got, total)
    if expected_size and got != expected_size:
        raise UpdateError(f"下載不完整（{got}／{expected_size} bytes）")
    os.replace(part, dest)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sha256_file(text: str) -> str:
    """``sha256sum`` 格式（「<64 位十六進位>  檔名」）取出雜湊；找不到回空字串。"""
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    return m.group(1).lower() if m else ""


def safe_extract(zip_path: str, dest: str) -> None:
    """解壓到 dest；任何項目落在 dest 以外（``..``、絕對路徑、磁碟代號）就整個拒絕。"""
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        total = 0
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            target = os.path.realpath(os.path.join(root, name))
            if (name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                    or os.path.commonpath([root, target]) != root):
                raise UpdateError(f"更新檔含有不安全的路徑：{info.filename}")
            total += info.file_size
            if total > _MAX_UNZIP_BYTES:
                raise UpdateError("更新檔解壓後過大，拒絕解壓")
        z.extractall(root)


def find_package_root(folder: str) -> str:
    """解壓結果裡含 aa_main_qt.exe 的資料夾（zip 頂層或下一層）；找不到丟 UpdateError。"""
    if os.path.isfile(os.path.join(folder, EXE_NAME)):
        return folder
    for n in sorted(os.listdir(folder)):
        p = os.path.join(folder, n)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, EXE_NAME)):
            return p
    raise UpdateError(f"更新檔裡找不到 {EXE_NAME}")


def work_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "AATool_update")


def prepare_update(info: ReleaseInfo,
                   progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> str:
    """下載、驗證、解壓；回傳新版程式資料夾（含 aa_main_qt.exe）。"""
    base = os.path.join(work_dir(), info.version)
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    zip_path = os.path.join(base, info.zip_name)
    sha_path = zip_path + ".sha256"
    try:
        _download(info.sha_url, sha_path)
        _download(info.zip_url, zip_path, info.zip_size, progress, cancelled)
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"下載更新檔失敗：{e}") from None
    with open(sha_path, encoding="utf-8", errors="replace") as f:
        expected = parse_sha256_file(f.read())
    if not expected:
        raise UpdateError("SHA256 檔格式不對，為了安全不更新")
    actual = sha256_of(zip_path)
    if actual != expected:
        raise UpdateError("SHA256 不符：檔案可能損毀或被竄改，已停止更新")
    if info.zip_digest.lower().startswith("sha256:") \
            and info.zip_digest.split(":", 1)[1].lower() != actual:
        raise UpdateError("SHA256 與 GitHub 記錄的不符：檔案可能被竄改，已停止更新")
    extract = os.path.join(base, "pkg")
    try:
        safe_extract(zip_path, extract)
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"解壓更新檔失敗：{e}") from None
    try:
        os.remove(zip_path)   # 驗證過、解壓完就不需要了（約 100 MB）
    except OSError:
        pass
    return find_package_root(extract)


def launch_apply(pkg_root: str, app_dir: str) -> None:
    """啟動新版 exe 換檔（呼叫端隨後必須結束程式，新版會等這個 PID 結束）。"""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [os.path.join(pkg_root, EXE_NAME), APPLY_ARG, app_dir, str(os.getpid())],
        cwd=pkg_root, creationflags=flags, close_fds=True)


# ── 換檔（由新版 exe 以 --apply-update 執行） ──

def _wait_pid_exit(pid: int, timeout: float) -> bool:
    if os.name != "nt":
        end = time.time() + timeout
        while time.time() < end:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.5)
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if not h:
        return True                               # 已經不在了
    try:
        return k32.WaitForSingleObject(h, int(timeout * 1000)) == 0
    finally:
        k32.CloseHandle(h)


def _retry(fn, tries: int = 10, wait: float = 0.5):
    """防毒掃描等造成的短暫佔用：重試幾次。"""
    for i in range(tries):
        try:
            return fn()
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(wait)


def apply_files(pkg_root: str, app_dir: str,
                on_step: Callable[[str], None] | None = None) -> None:
    """把 pkg_root 的每個項目換進 app_dir；任何一步失敗都還原成原狀再丟 UpdateError。"""
    items = sorted(os.listdir(pkg_root))
    backup = os.path.join(app_dir, f"_update_backup_{int(time.time())}")
    os.makedirs(backup)
    moved: list[str] = []
    placed: list[str] = []
    try:
        for n in items:
            src = os.path.join(app_dir, n)
            if os.path.lexists(src):
                if on_step:
                    on_step(f"備份 {n}")
                try:
                    _retry(lambda s=src, d=os.path.join(backup, n): os.replace(s, d))
                except OSError as e:
                    raise UpdateError(
                        f"無法移動「{n}」（{e.strerror or e}）：可能還有程式開著"
                        "（例如單獨開啟的網址讀取視窗），請關閉後再更新") from None
                moved.append(n)
        for n in items:
            if on_step:
                on_step(f"複製 {n}")
            s, d = os.path.join(pkg_root, n), os.path.join(app_dir, n)
            placed.append(n)
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
    except Exception as e:  # noqa: BLE001 — 還原後統一丟 UpdateError
        for n in placed:
            p = os.path.join(app_dir, n)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else _silent_remove(p)
        for n in moved:
            try:
                _retry(lambda n=n: os.replace(os.path.join(backup, n),
                                              os.path.join(app_dir, n)))
            except OSError:
                pass
        shutil.rmtree(backup, ignore_errors=True)
        if isinstance(e, UpdateError):
            raise
        raise UpdateError(f"複製新檔失敗，已還原舊版：{e}") from None
    shutil.rmtree(backup, ignore_errors=True)


def _silent_remove(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def write_result(ok: bool, version: str, message: str) -> None:
    try:
        with open(app_paths.path_for(RESULT_FILE), "w", encoding="utf-8") as f:
            json.dump({"ok": ok, "version": version, "message": message}, f,
                      ensure_ascii=False)
    except OSError:
        pass


def pop_result() -> dict | None:
    """讀出上次更新的結果並刪掉（啟動時呼叫）；順便清掉暫存的更新檔。"""
    path = app_paths.path_for(RESULT_FILE)
    result = None
    try:
        with open(path, encoding="utf-8") as f:
            result = json.load(f)
    except (OSError, ValueError):
        pass
    _silent_remove(path)
    if result is not None:
        shutil.rmtree(work_dir(), ignore_errors=True)
    return result


def run_apply(app_dir: str, pid: int, version: str,
              on_step: Callable[[str], None] | None = None) -> bool:
    """新版 exe 的 --apply-update 入口：等舊程式結束 → 換檔 → 寫結果 → 重開程式。"""
    pkg_root = os.path.dirname(os.path.abspath(sys.executable))
    ok, msg = False, ""
    if on_step:
        on_step("等待舊版程式關閉…")
    if not _wait_pid_exit(pid, _WAIT_EXIT_SECONDS):
        msg = "舊版程式沒有關閉，未更新"
    elif os.path.normcase(os.path.abspath(app_dir)) == os.path.normcase(pkg_root):
        msg = "更新來源與程式資料夾相同，未更新"
    elif not os.path.isfile(os.path.join(app_dir, EXE_NAME)):
        msg = f"程式資料夾裡找不到 {EXE_NAME}，未更新"
    else:
        try:
            apply_files(pkg_root, app_dir, on_step)
            ok, msg = True, f"已更新到 v{version}"
        except UpdateError as e:
            msg = str(e)
        except Exception as e:  # noqa: BLE001
            msg = f"更新失敗：{e}"
    write_result(ok, version, msg)
    exe = os.path.join(app_dir, EXE_NAME)
    if os.path.isfile(exe):
        flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        subprocess.Popen([exe], cwd=app_dir, creationflags=flags, close_fds=True)
    return ok
