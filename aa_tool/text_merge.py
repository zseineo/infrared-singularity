"""逐行三方合併 — 純邏輯，供批次搜尋「只撤銷替換、保留之後的修改」與位置追蹤使用。

每次替換只記兩份文字：before（沒有替換）與 after（有替換），兩者行數相同、逐行對應
（替換不會增減行）。復原＝把「after → before」的差異套到檔案目前的內容上：
沒動過的行直接換回；同一行另有修改時逐字合併；改到被替換的字本身則保留並回報。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from difflib import SequenceMatcher

_SIMILAR = 0.6      # 兩行多相似才視為「同一行被修改過」
_ALIGN_CELLS = 4096 # 區塊（舊行數×新行數）在這以內才做最佳配對


def _key(line: str) -> str:
    # QTextEdit 存檔時 NBSP 會變成半形空白，比對時視為相同
    return line.replace('\u00a0', ' ')


def _similarity(a: str, b: str) -> float:
    sm = SequenceMatcher(None, a, b, False)
    return sm.ratio() if sm.quick_ratio() >= _SIMILAR else 0.0


def _char_edits(a: str, b: str) -> list[tuple[int, int, str]]:
    """把 a 變成 b 的片段 (起, 迄, 新文字)，位置是 a 的索引。"""
    return [(i1, i2, b[j1:j2]) for tag, i1, i2, j1, j2
            in SequenceMatcher(None, a, b, False).get_opcodes() if tag != 'equal']


def _align_block(old: list[str], new: list[str]) -> dict[int, int]:
    """一小段之內，保持順序、讓總相似度最高的逐行配對：{new 索引: old 索引}。

    不用 difflib：兩行一模一樣、其中一行被編輯時，它會把另一行配過來（只認完全相同）。
    """
    # 相同的頭尾不必算相似度（長檔的絕大部分都在這裡結束）
    head, limit = 0, min(len(old), len(new))
    while head < limit and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < limit - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    mapping = {i: i for i in range(head)}
    for k in range(1, tail + 1):
        mapping[len(new) - k] = len(old) - k
    middle = _best_pairs(old[head:len(old) - tail], new[head:len(new) - tail])
    mapping.update({head + j: head + i for j, i in middle.items()})
    return mapping


def _best_pairs(old: list[str], new: list[str]) -> dict[int, int]:
    """頭尾都不同的一小段：動態規劃求最佳配對。"""
    n, m = len(old), len(new)
    if not n or not m:
        return {}
    if n * m > _ALIGN_CELLS:
        if n == m:
            return {i: i for i in range(n)
                    if old[i] == new[i] or _similarity(old[i], new[i]) >= _SIMILAR}
        # 太大：只認完全相同的行，其餘視為無法對應（呼叫端會保留現狀）
        return {j + k: i + k
                for i, j, size in SequenceMatcher(None, old, new).get_matching_blocks()
                for k in range(size)}
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    paired = [[False] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(score[i - 1][j], score[i][j - 1])
            x, y = old[i - 1], new[j - 1]
            similar = 1.0 if x == y else _similarity(x, y) * 0.99
            if similar >= _SIMILAR * 0.99 and score[i - 1][j - 1] + similar >= best:
                best = score[i - 1][j - 1] + similar
                paired[i][j] = True
            score[i][j] = best
    mapping: dict[int, int] = {}
    i, j = n, m
    while i and j:
        if paired[i][j]:
            mapping[j - 1] = i - 1
            i, j = i - 1, j - 1
        elif score[i - 1][j] >= score[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return mapping


def map_lines(old: list[str], new: list[str]) -> dict[int, int]:
    """找出 new 的各行對應 old 的哪一行：{new 行號: old 行號}。

    頭尾相同的行直接對應；中間以「兩邊都只出現一次的行」當錨點切成小段，再各自配對。
    """
    a, b = [_key(x) for x in old], [_key(x) for x in new]
    head, limit = 0, min(len(a), len(b))
    while head < limit and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < limit - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1

    mapping = {i: i for i in range(head)}
    for k in range(1, tail + 1):
        mapping[len(b) - k] = len(a) - k

    end_a, end_b = len(a) - tail, len(b) - tail
    count_a, count_b = Counter(a[head:end_a]), Counter(b[head:end_b])
    unique = {x for x, n in count_a.items() if n == 1 and count_b.get(x) == 1}
    at_a = [i for i in range(head, end_a) if a[i] in unique]
    at_b = [j for j in range(head, end_b) if b[j] in unique]
    matcher = SequenceMatcher(None, [a[i] for i in at_a], [b[j] for j in at_b], False)
    anchors = [(at_a[i + k], at_b[j + k])
               for i, j, size in matcher.get_matching_blocks() for k in range(size)]

    from_a, from_b = head, head
    for i, j in anchors + [(end_a, end_b)]:
        for q, p in _align_block(a[from_a:i], b[from_b:j]).items():
            mapping[from_b + q] = from_a + p
        if i < end_a:
            mapping[j] = i
        from_a, from_b = i + 1, j + 1
    return mapping


def _merge_line(before: str, after: str, current: str,
                words: Iterable[str]) -> str | None:
    """單行：撤銷 after 裡的替換、保留 current 的其他修改；互相重疊時回傳 None。"""
    if current == after:
        return before
    if current == before:
        return current
    reverts = _char_edits(after, before)
    edits = _char_edits(after, current)
    for start, end, _ in reverts:
        # 被替換進去的整個詞都算替換範圍：改到詞裡與原詞相同的那個字也是衝突
        low, high = start, end
        for word in words:
            at = after.find(word) if word else -1
            while 0 <= at <= end:
                if at + len(word) >= start:
                    low, high = min(low, at), max(high, at + len(word))
                at = after.find(word, at + 1)
        for a, b, _ in edits:
            if start == end and a <= start <= b:
                return None
            if a == b and low < a < high:
                return None
            if a < b and max(a, low) < min(b, high):
                return None
    result = after
    for start, end, text in sorted(reverts + edits, key=lambda e: e[:2], reverse=True):
        result = result[:start] + text + result[end:]
    return result


def revert_changes(before: str, after: str, current: str,
                   words: Iterable[str] = ()) -> tuple[str, int]:
    """把 before → after 的替換從 *current* 撤銷，保留 current 的其他修改。

    Args:
        words: 替換進去的文字，用來判斷之後的修改是否改到了替換本身

    Returns:
        (結果, 因為之後被改過而無法撤銷的行數)。結果的行數與 current 相同。
    """
    if current == after:
        return before, 0
    b_lines, a_lines, c_lines = before.split('\n'), after.split('\n'), current.split('\n')
    if len(b_lines) != len(a_lines):
        return current, 1
    mapping = map_lines(a_lines, c_lines)
    words = tuple(words)
    reached: set[int] = set()
    kept = 0
    out = list(c_lines)
    for j, i in mapping.items():
        reached.add(i)
        if b_lines[i] == a_lines[i]:
            continue
        merged = _merge_line(b_lines[i], a_lines[i], c_lines[j], words)
        if merged is None:
            kept += 1
        else:
            out[j] = merged
    # 有替換、但之後被刪掉或改到認不出來的行
    kept += sum(1 for i, (b, a) in enumerate(zip(b_lines, a_lines))
                if b != a and i not in reached)
    return '\n'.join(out), kept


def map_span(old: str, new: str, start: int, end: int) -> int | None:
    """old[start:end] 原封不動留在 new 的哪個位置；已被改動回傳 None。"""
    if old == new or _key(old) == _key(new):
        return start
    for a, b, size in SequenceMatcher(None, old, new, False).get_matching_blocks():
        if size and a <= start and end <= a + size:
            return b + (start - a)
    return None
