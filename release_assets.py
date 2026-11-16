"""發佈用：把 PyInstaller 輸出打成 zip、算 SHA256，上傳到 GitHub Release。

程式內「檢查更新」（aa_tool/updater.py）只認這兩個附件：
    aa_main_qt_v<版本>.zip          （頂層一個 aa_main_qt_v<版本>/ 資料夾）
    aa_main_qt_v<版本>.zip.sha256   （sha256sum 格式：「<雜湊>  <檔名>」）

用法（先 pyinstaller aa_main_qt.spec 產生 dist/aa_main_qt_v<版本>/）：
    py release_assets.py              # 打包＋算雜湊＋上傳到 tag v<版本>（Release 不存在就建立）
    py release_assets.py --no-upload  # 只產生檔案
版本號讀 aa_main_qt.py 的 APP_VERSION。上傳用 gh CLI。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))


def app_version() -> str:
    with open(os.path.join(ROOT, "aa_main_qt.py"), encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        sys.exit("讀不到 APP_VERSION")
    return m.group(1)


def make_zip(src_dir: str, zip_path: str) -> None:
    top = os.path.basename(src_dir)
    tmp = zip_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, _dirs, files in os.walk(src_dir):
            for n in sorted(files):
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
                z.write(full, f"{top}/{rel}")
    os.replace(tmp, zip_path)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-upload", action="store_true", help="只產生 zip 與 .sha256，不上傳")
    args = ap.parse_args()

    ver = app_version()
    name = f"aa_main_qt_v{ver}"
    src = os.path.join(ROOT, "dist", name)
    if not os.path.isfile(os.path.join(src, "aa_main_qt.exe")):
        sys.exit(f"找不到 {src}\\aa_main_qt.exe，請先 pyinstaller aa_main_qt.spec")
    zip_path = os.path.join(ROOT, "dist", name + ".zip")
    sha_path = zip_path + ".sha256"
    print(f"打包 {src} → {zip_path}")
    make_zip(src, zip_path)
    digest = sha256_of(zip_path)
    with open(sha_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest}  {os.path.basename(zip_path)}\n")
    print(f"SHA256 {digest}（{os.path.getsize(zip_path) / 1024 / 1024:.1f} MB）")
    if args.no_upload:
        return
    tag = f"v{ver}"
    if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode != 0:
        print(f"Release {tag} 不存在，建立中…")
        subprocess.run(["gh", "release", "create", tag, "--title", tag,
                        "--generate-notes"], check=True)
    subprocess.run(["gh", "release", "upload", tag, zip_path, sha_path, "--clobber"],
                   check=True)
    print(f"已上傳到 {tag}")


if __name__ == "__main__":
    main()
