#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""掃描 AA 圖行，列出尚未納入 DEFAULT_SYMBOL_REGEX 的高頻「裝飾字元」候選。

用途
----
`DEFAULT_SYMBOL_REGEX`（aa_tool/constants.py）是「AA 圖裝飾符號」的字元集，
被提取演算法用來判斷某段內容的 AA 噪聲密度。這份清單建立得早、很少更新；
本工具讓你**不需動用 AI** 就能找出該補哪些字元。

工作方式
--------
1. 掃描指定檔案（預設 testcase/base.txt + testcase/failcase.txt；也可自行
   傳入更多 .txt / .html 路徑）。
2. 用啟發式判斷「這行是不是 AA 圖行」：去掉空白後，日文內文字元（平假名 /
   片假名 / 漢字 / 英數字）佔比 < 30%，且非空白字元 ≥ 5 個 → 視為 AA 行。
3. 對 AA 行裡的每個字元，若它**不是**日文內文字元、**不是**全/半形空白、
   且**還沒被 `DEFAULT_SYMBOL_REGEX` 命中**，就計入頻率統計。
4. 印出依頻率排序的候選清單 + 一行「可直接貼回 constants.py」的新 regex。

你只要：跑 `python scan_aa_symbols.py` → 看清單 → 把確實是 AA 裝飾的字元
（如 `∨ ∧ ⌒ ≧ ≦` 之類；**不要**把真實漢字如 `心 刃 辷` 加進去）挑出來 →
照印出來的範例改 `aa_tool/constants.py` 的 `DEFAULT_SYMBOL_REGEX` 即可。

注意：半形片假名（ｦ-ﾟ）由 `text_extraction._HALFWIDTH_KANA_CHARS` 另外處理，
不需也不該加進 symbol_regex，本工具會自動把它們排除在候選之外。
"""
from __future__ import annotations

import collections
import os
import re
import sys

# 讓 `from aa_tool.constants import ...` 在任何 cwd 下都能 import
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from aa_tool.constants import DEFAULT_SYMBOL_REGEX  # noqa: E402

_SYM_RE = re.compile(DEFAULT_SYMBOL_REGEX)
# 半形片假名（U+FF66–U+FF9F）— 另由 text_extraction._HALFWIDTH_KANA_CHARS 處理
_HALFWIDTH_KANA = set(chr(c) for c in range(0xFF66, 0xFFA0))
_SPACES = {' ', '　', '\t', '\n', '\r'}


def _is_jp_content(ch: str) -> bool:
    """日文內文字元：平假名 / 片假名 / CJK 漢字 / 半全形英數字。"""
    cp = ord(ch)
    if 0x3040 <= cp <= 0x30FF:        # 平假名 + 片假名
        return True
    if 0x4E00 <= cp <= 0x9FFF:        # CJK 漢字
        return True
    if ch.isalnum():                   # 英數字（含全形）
        return True
    return False


def _is_aa_line(line: str) -> bool:
    non_space = [c for c in line if c not in _SPACES]
    if len(non_space) < 5:
        return False
    jp = sum(1 for c in non_space if _is_jp_content(c))
    return jp / len(non_space) < 0.30


def _scan(paths: list[str]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for path in paths:
        if not os.path.exists(path):
            print(f"(略過：找不到檔案 {path})", file=sys.stderr)
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not _is_aa_line(line):
                    continue
                for ch in line:
                    if ch in _SPACES:
                        continue
                    if _is_jp_content(ch):
                        continue
                    if ch in _HALFWIDTH_KANA:
                        continue          # 已另外處理
                    if _SYM_RE.match(ch):
                        continue          # 已在 DEFAULT_SYMBOL_REGEX 中
                    counter[ch] += 1
    return counter


def _print_report(counter: collections.Counter, top: int = 40) -> None:
    if not counter:
        print("沒有發現新的候選字元 — DEFAULT_SYMBOL_REGEX 已涵蓋掃到的 AA 行。")
        return
    print(f"# 出現於 AA 圖行、但不在 DEFAULT_SYMBOL_REGEX 中的字元（依頻率排序，前 {top}）：")
    print("#   （挑出確實是 AA 裝飾的；勿加真實漢字 / 真實標點）")
    new_chars: list[str] = []
    for ch, cnt in counter.most_common(top):
        print(f"  {ch!r:>8}  U+{ord(ch):04X}  ×{cnt}")
        if cnt >= 2:                       # 出現 ≥2 次的當作建議候選
            new_chars.append(ch)
    print()
    if new_chars:
        # 印出「假如全加進去」的新 regex，方便挑選後修改
        # DEFAULT_SYMBOL_REGEX 形如 "[....]"，把新字元插在 ] 前
        old = DEFAULT_SYMBOL_REGEX
        if old.endswith("]"):
            # 把候選字元做最小化跳脫（regex 字元類內需跳脫的：] \ ^ -）
            esc = "".join(
                ("\\" + c if c in "]\\^-" else c) for c in "".join(new_chars))
            suggested = old[:-1] + esc + "]"
        else:
            suggested = old + " (無法自動拼接，請手動加入)"
        print("# 若決定把上面 ×≥2 的候選全部加入，DEFAULT_SYMBOL_REGEX 會變成：")
        print(f"DEFAULT_SYMBOL_REGEX = r\"{suggested}\"")
        print()
        print("# 實務上：複製這行 → 刪掉你覺得不該加的字元 → 貼回 aa_tool/constants.py")


def main() -> None:
    args = sys.argv[1:]
    if args:
        paths = args
    else:
        paths = [
            os.path.join(_HERE, "testcase", "base.txt"),
            os.path.join(_HERE, "testcase", "failcase.txt"),
        ]
    counter = _scan(paths)
    _print_report(counter)


if __name__ == "__main__":
    main()
