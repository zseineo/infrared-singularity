"""閱讀順序編號：依各檔內文第一個投稿標頭的日期，替作品資料夾的檔名加上「NNN_」前綴。

作品的短篇／幕間／小ネタ常沒有話數，卻是夾在某幾話之間發表的；檔名排序會把它們
擠到一邊。投稿標頭（``2011/04/21(木) 00:35:17 ID:xxx``）是發表當下由伺服器產生、
翻譯不會動到的，拿它排序就是作者發表的順序。編號直接放在檔名開頭，任何地方
（檔案總管、本工具的檔案列表、其他看圖／閱讀軟體）照檔名排序都對。

- 已有前綴的檔案重排時先去掉舊前綴再編，重跑結果一致。
- 讀不到日期的檔案排在最後（依原檔名）。
- 檔名比對（同名檔判定、從資料夾讀話數）一律先 ``strip_order_prefix``。
"""
from __future__ import annotations

import os
import re

from . import original_cache
from .html_io import read_html_pre_content

ORDER_PREFIX_RE = re.compile(r'^\d{3,}_')
_DATE_RE = re.compile(
    r'(\d{4})/(\d{1,2})/(\d{1,2})\([^)\s]+\)\s*(\d{1,2}):(\d{2}):(\d{2})')
_EXTS = ('.html', '.htm')
_TMP_SUFFIX = '.__reorder_tmp'


def strip_order_prefix(name: str) -> str:
    """去掉檔名開頭的閱讀順序編號（``012_作品 第5話.html`` → ``作品 第5話.html``）。"""
    return ORDER_PREFIX_RE.sub('', name, count=1)


def find_existing(folder: str, filename: str) -> str | None:
    """資料夾裡去掉編號後與 ``filename`` 同名（不分大小寫）的檔案路徑；沒有回 None。"""
    want = filename.casefold()
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for n in names:
        if strip_order_prefix(n).casefold() == want:
            return os.path.join(folder, n)
    return None


def taken_names(folder: str) -> set[str]:
    """資料夾裡去掉編號後的檔名（casefold），供同名序號判定。"""
    try:
        return {strip_order_prefix(n).casefold() for n in os.listdir(folder)}
    except OSError:
        return set()


def post_date(path: str) -> tuple | None:
    """檔案內文第一個投稿標頭的日期時間（可直接比較大小的 tuple）；讀不到回 None。"""
    try:
        try:
            text = read_html_pre_content(path)
        except UnicodeDecodeError:
            text = None
        if text is None:
            with open(path, encoding='utf-8', errors='replace') as f:
                text = f.read()
    except OSError:
        return None
    m = _DATE_RE.search(original_cache.compute_fingerprint(text) or '')
    return tuple(int(x) for x in m.groups()) if m else None


def plan_order(folder: str) -> tuple[list[tuple[str, str]], list[str]]:
    """算出整個資料夾的新檔名。

    回傳 ``(renames, undated)``：renames 為全部 HTML 檔依閱讀順序的
    ``(目前檔名, 新檔名)``（包含不必改名的）；undated 為讀不到日期的目前檔名。
    """
    dated, undated = [], []
    for n in os.listdir(folder):
        p = os.path.join(folder, n)
        if not n.lower().endswith(_EXTS) or not os.path.isfile(p):
            continue
        base = strip_order_prefix(n)
        d = post_date(p)
        # 同日期時比不含副檔名的檔名：重翻多存的「X-2.html」排在「X.html」之後
        key = os.path.splitext(base)[0].casefold()
        if d is None:
            undated.append((key, n, base))
        else:
            dated.append((d, key, n, base))
    dated.sort()
    undated.sort()
    order = [(n, base) for _d, _k, n, base in dated] + [(n, base) for _k, n, base in undated]
    width = max(3, len(str(len(order))))
    renames = [(n, f"{i:0{width}d}_{base}") for i, (n, base) in enumerate(order, 1)]
    return renames, [n for _k, n, _b in undated]


def apply_order(folder: str, renames: list[tuple[str, str]]) -> dict[str, str]:
    """依 ``plan_order`` 的結果改名，回傳 ``{舊完整路徑: 新完整路徑}``（只含有改名的）。

    先全部改成暫存名再改成最終名，避免 A→B、B→C 這種互換時撞名。
    中途失敗（檔案被其他程式鎖住等）時，還在暫存名的檔案改回原名再丟出例外；
    已改成新名的維持新名（內容不受影響，重跑即可補齊）。
    """
    moves = [(old, new) for old, new in renames if old != new]
    staged: list[tuple[str, str, str]] = []
    done: dict[str, str] = {}
    try:
        for old, new in moves:
            tmp = os.path.join(folder, old + _TMP_SUFFIX)
            os.replace(os.path.join(folder, old), tmp)
            staged.append((tmp, old, new))
        while staged:
            tmp, old, new = staged[0]
            dst = os.path.join(folder, new)
            os.replace(tmp, dst)
            staged.pop(0)
            done[os.path.join(folder, old)] = dst
    except OSError:
        for tmp, old, _new in staged:
            try:
                os.replace(tmp, os.path.join(folder, old))
            except OSError:
                pass
        raise
    return done


def renumber_folder(folder: str) -> tuple[dict[str, str], list[str]]:
    """``plan_order`` ＋ ``apply_order``，回傳 ``(改名對照, 讀不到日期的檔名)``。"""
    renames, undated = plan_order(folder)
    return apply_order(folder, renames), undated
