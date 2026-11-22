"""對話框對齊演算法 — 純邏輯，不依賴任何 UI 框架。

所有字寬量測透過 FontMeasurer 協議完成。
支援五種對話框類型：
  - normal:    普通框（￣/＿）
  - shout:     吶喊框（_人/⌒Y）
  - slash:     斜線框（＼─|／）
  - box:       方框（┌─┐ / │ / └─┘，亦支援粗體 ┏━┓ / ┃ / ┗━┛）
  - kanji_box: 漢字框（ミ川彡 / 三 / 彡川ミ）

五種框共用同一套流程，差別只在各自的邊框樣式與邊線字元：
  1. 以上框左角的 x 座標為錨點，找左角落在同一位置的下框與各列左邊線
     （不可用「行內第一個／最後一個同字元」猜：左側 AA 與台詞裡都會有同樣的字元）
  2. 由台詞右端算出框寬，重建上下邊框
  3. 各列只改「台詞右端～右邊線」之間的填白，把右邊線放到同一個絕對 x 座標

不變式：除了邊框橫線與填白，不刪除、不改動任何可見字元（含框右側的 AA 與上色標籤）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .font_measure import FontMeasurer


# ════════════════════════════════════════════════════════════════
#  共用常數
# ════════════════════════════════════════════════════════════════

RIGHT_BORDER_CHARS = '│｜|〉》>）)ノﾉ＼ヽ｝}］]'

# 「對齊上一行」用的填白字元：半形空白 + 全形空白 + 半形點
_PAD_CHARS = ' \u3000.'

# 對話框用的空白：另含 AA 作者微調用的 U+2000–U+200B 各種窄空白與 NBSP
_SPACE = ' \u3000\u00a0\u2000-\u200b'
_SPACE_RE = re.compile(f'[{_SPACE}]')
# 台詞右端的填白。半形點只有「前面已經有空白」時才算填白，否則是台詞的刪節號（好吧...）
_TRAILING_PAD_RE = re.compile(f'(?<![{_SPACE}])[{_SPACE}][{_SPACE}.]*$')

_MAX_BOX_LINES = 30     # 上框到下框最多相隔幾行

# 以下距離都是 MS PGothic 12pt 下的 px，使用時乘上 _scale(m) 換算到目前的字級
_REFERENCE_SPACE = 10.625  # 該字級下全形空白的寬度
_LEFT_TOLERANCE = 24.0  # 同一個框各列左邊線的 x 容許誤差
_RIGHT_TOLERANCE = 32.0
_EXACT_TOLERANCE = 8.0   # 幾乎正好在右角下方＝即使後面接著漢字圖案也視為右邊線
_SELECTED_TOLERANCE = 56.0  # 使用者自己選取時，左邊線可以歪得多一些

# 框內是否有台詞：用來和 AA 圖裡長得像框的圖形（視窗、招牌、積木字）區分
_TEXT_RE = re.compile(
    r'(?![ニ三二川人ミハﾆ])[\u3041-\u30fa\u3400-\u9fff\uac00-\ud7a3\uff10-\uff5a]'
    r'|(?:(?![ﾆﾊ])[ｦ-ﾝ][ﾞﾟ]?){2}'  # 半形片假名的擬聲詞（ｼﾞﾜｯ）
    r'|[A-Za-z0-9]{2}|[！？…♪♡♥]'
)
# 連續兩個漢字＝台詞（排除 AA 常拿來當圖案的漢字）
_IDEOGRAPH_PAIR_RE = re.compile(r'(?:(?![三二川人乂王工彡从芹芯云圭])[\u3400-\u9fff]){2}')
# AA 圖的筆畫字元：框內過半都是這些＝框著一張圖，不是對話框
_ART_RE = re.compile(r"[:;/\\|{}<>=_`´',^~＿￣／＼≧≦≪≫ﾆニ-]")
# 另一個框的邊框片段
_BORDER_PIECE_RE = re.compile(r'人[_＿]+人|⌒[YＹ]⌒|──\||￣{3}|＿{3}')
_WIDE_GAP_RE = re.compile(f'[{_SPACE}]{{2,}}')

# 不佔顯示寬度的標記：上色／隱藏 span（隱藏的內容一併略過）
_MARKUP_RE = re.compile(
    r'<span style="display:\s*none;?\s*">.*?</span>'
    r'|<span\s+style="(?:color:[^"]*|display:\s*none;?\s*)">'
    r'|</span>'
)


# ════════════════════════════════════════════════════════════════
#  各框型的樣式
# ════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class _Style:
    top_re: re.Pattern[str]
    bot_re: re.Pattern[str]
    lefts: str               # 內容列左邊線可用的字元
    rights: str              # 內容列右邊線可用的字元
    left_tolerance: float = _LEFT_TOLERANCE
    right_offset: float = 0  # 右邊線右緣相對於上框右緣的位置（取自作者手排原文的中位數）
    mirrored: str = ''       # 這些左邊線會左右鏡射地內縮／外凸（斜線框、≫≪ 鋸齒框）
    ends_line: bool = False  # 「對話框(全)」時上下框必須位於行尾，避免改到 AA 圖中的小方塊


_GAP = f'[{_SPACE}.]'

_STYLES: dict[str, _Style] = {
    'shout': _Style(
        top_re=re.compile(
            r'(?:[､、＼][_＿]+人|[_＿]+人(?=(?:[_＿]+人){2}))(?:[_＿]+(?:[,，.][_＿]*)?人+)*'
            rf'(?:[_＿]*{_GAP}{{0,3}}人(?!人))?[_＿]*[,，､、／]?'),
        bot_re=re.compile(
            rf'／?(?:⌒[YＹ]+)+⌒?(?:{_GAP}{{0,3}}[YＹ](?![YＹ]))?＼?'),
        lefts='）)＞>》≫≪',
        rights='（(＜<《≪≫＞>',
        right_offset=-1,
        mirrored='≫≪',
    ),
    'slash': _Style(
        top_re=re.compile(rf'＼─\|(?:──\|)*─?{_GAP}{{0,3}}／'),
        bot_re=re.compile(rf'／─\|(?:──\|)*─?{_GAP}{{0,3}}＼'),
        lefts='│|｜─―━—',
        rights='│|｜─―━—',
        mirrored='│|｜─―━—',
        left_tolerance=36.0,  # │ 比 ＼ 內縮
    ),
    'box': _Style(
        top_re=re.compile(rf'[┌┏][─━]+[{_SPACE}]*[┐┓]'),
        bot_re=re.compile(rf'[└┗][─━]+[{_SPACE}]*[┘┛]'),
        lefts='│┃|｜',
        rights='│┃|｜',
        ends_line=True,
    ),
    'kanji_box': _Style(
        top_re=re.compile(rf'ミ川+[{_SPACE}]*彡'),
        bot_re=re.compile(rf'彡川+[{_SPACE}]*ミ'),
        lefts='三',
        rights='三',
        ends_line=True,
    ),
    # 普通框的上下框角落變體太多，由 _find_normal_border() 判斷
    'normal': _Style(
        top_re=re.compile('￣{3,}'),
        bot_re=re.compile('＿{3,}'),
        lefts='|│｜',
        rights='|│｜',
        right_offset=-2,
    ),
}


@dataclass(frozen=True)
class _Border:
    start: int  # 左角落的字元位置
    end: int    # 右角落之後的字元位置
    fill: tuple[int, int] = (0, 0)  # 普通框橫線的範圍


@dataclass(frozen=True)
class _Row:
    line: int
    text: str
    left: int | None     # 左邊線的字元位置
    right: int | None    # 右邊線的字元位置
    content_end: int     # 台詞右端（不含填白）


@dataclass(frozen=True)
class _Box:
    kind: str
    top_line: int
    bot_line: int
    ordinal: int         # 同一行有多個上框時是第幾個
    left_x: float
    top: _Border
    bot: _Border | None  # 普通框允許只選到上框
    rows: list[_Row]


# ════════════════════════════════════════════════════════════════
#  內部輔助：可見文字與原始行的對應
# ════════════════════════════════════════════════════════════════

def _scale(m: FontMeasurer) -> float:
    """目前字級相對於 12pt 的倍率。"""
    return m.measure('　') / _REFERENCE_SPACE


def _visible(line: str) -> str:
    """回傳實際顯示出來的文字（去掉 span 標記）。"""
    return _MARKUP_RE.sub('', line) if '<' in line else line


def _replace_visible(line: str, start: int, end: int, new: str) -> str:
    """把可見文字的 [start, end) 換成 *new*，範圍內的標記原樣保留。"""
    if '<' not in line:
        return line[:start] + new + line[end:]

    positions: list[int] = []  # 第 i 個可見字元在原始行中的位置
    pos = 0
    for tag in _MARKUP_RE.finditer(line):
        positions.extend(range(pos, tag.start()))
        pos = tag.end()
    positions.extend(range(pos, len(line)))

    removed = set(positions[start:end])
    if start < end:
        at = positions[start]
    elif start:
        at = positions[start - 1] + 1
    else:
        at = 0
    tail = ''.join(c for i, c in enumerate(line[at:], at) if i not in removed)
    return line[:at] + new + tail


# ════════════════════════════════════════════════════════════════
#  內部輔助：補空白
# ════════════════════════════════════════════════════════════════

def _pad_to_width(text: str, target_width: float, m: FontMeasurer) -> str:
    """用全形空白再半形空白將 *text* 補至最接近 *target_width* 的寬度。

    最後做一次 snap：若再多一個半形空白比現狀更接近 target，就補上。
    """
    while m.measure(text + '　') <= target_width:
        text += '　'
    while m.measure(text + ' ') <= target_width:
        text += ' '
    if (m.measure(text + ' ') - target_width) < (target_width - m.measure(text)):
        text += ' '
    return text


def _make_padding(width: float, m: FontMeasurer) -> str:
    """組出最接近 *width* 的填白（誤差通常 < 1px）。

    全形／半形空白寬度互質，調整半形個數即可湊出幾乎任意寬度；
    半形穿插在全形之間，避免連續半形空白貼到留言板時被壓成一格。
    """
    if width <= 0:
        return ''
    full, half = m.measure('　'), m.measure(' ')
    best = (float('inf'), 0, 0)
    for n_half in range(11):
        n_full = round((width - n_half * half) / full)
        if n_full < 0:
            break
        error = abs(n_full * full + n_half * half - width)
        if error < best[0]:
            best = (error, n_full, n_half)
        if error <= 0.75 * _scale(m):
            break
    _, n_full, n_half = best
    paired = min(n_full, n_half)
    return '　 ' * paired + '　' * (n_full - paired) + ' ' * (n_half - paired)


# ════════════════════════════════════════════════════════════════
#  內部輔助：解析邊框與內容列
# ════════════════════════════════════════════════════════════════

def _is_safe_normal_border(left_part: str, right_part: str, char: str) -> bool:
    """安全性檢查：確認邊框角落字元符合已知模式，避免誤判 AA 圖案。"""
    lp_end = left_part.rstrip(' \u3000')
    last_c = lp_end[-1] if lp_end else ''
    if char == '￣':
        return last_c in ("´", "'") and any(c in right_part for c in '｀ヽﾍ')
    return last_c in ('乂', 'ヽ', '丶') and any(c in right_part for c in 'ノﾉ')


def _find_normal_border(text: str, is_top: bool, strict: bool) -> _Border | None:
    """找普通框的上框（￣）或下框（＿）：右側只能剩 4 個以內的角落字元。"""
    char = '￣' if is_top else '＿'
    if char * 3 not in text:
        return None
    pattern = _STYLES['normal'].top_re if is_top else _STYLES['normal'].bot_re
    run = list(pattern.finditer(text))[-1]
    right = text[run.end():]
    corner = right.strip(' \u3000')
    if len(corner) > 4:
        return None
    if strict and not _is_safe_normal_border(text[:run.start()], right, char):
        return None

    start = len(text[:run.start()].rstrip(' \u3000'))
    if start:
        start -= 1
        # 上框左角是兩個字（f´）時，錨點在 f
        if is_top and start and text[start] in "´'" and text[start - 1] in 'fｆr,/／γ':
            start -= 1
    end = run.end()
    if corner:
        end += right.index(corner) + len(corner)
    return _Border(start, end, run.span())


def _find_borders(text: str, kind: str, is_top: bool, strict: bool) -> list[_Border]:
    if kind == 'normal':
        border = _find_normal_border(text, is_top, strict)
        return [border] if border else []

    style = _STYLES[kind]
    found: list[_Border] = []
    for match in (style.top_re if is_top else style.bot_re).finditer(text):
        if strict:
            if style.ends_line and _SPACE_RE.sub('', text[match.end():]):
                continue
            if kind == 'shout':
                count = match.group().count('人' if is_top else '⌒')
                if count < 3 or (is_top and match.group()[0] not in '､、＼'):
                    continue
        found.append(_Border(match.start(), match.end()))
    return found


def _find_left(text: str, kind: str, left_x: float, tolerance: float,
               m: FontMeasurer) -> int | None:
    """找 x 座標最接近上框左角的左邊線。"""
    best: tuple[float, int] | None = None
    tolerance *= _scale(m)
    for i, char in enumerate(text):
        if char not in _STYLES[kind].lefts:
            continue
        x = m.measure(text[:i])
        if x > left_x + tolerance:
            break
        distance = abs(x - left_x)
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, i)
    return best[1] if best else None


def _find_right(text: str, kind: str, left: int, right_x: float,
                m: FontMeasurer) -> int | None:
    """找右邊線。翻譯後台詞長短改變，右邊線不一定還在上框右角下方。"""
    allowed = _STYLES[kind].rights
    if kind == 'slash':
        # 斜線框左右必同類：│ 對 │、― 對 ―（台詞裡常有 ───）
        vertical = '│|｜'
        allowed = vertical if text[left] in vertical else '─―━—'

    last = len(text.rstrip(' \u3000\u00a0\r_＿,.')) - 1
    while last > left and _SPACE_RE.match(text[last]):
        last -= 1

    candidates: list[int] = []
    for i in range(left + 1, last + 1):
        char = text[i]
        if char not in allowed:
            continue
        if char in '（(':
            # 後面有成對右括號且中間沒有寬間隔＝台詞裡的括號
            close = text.find('）' if char == '（' else ')', i + 1)
            if close != -1 and not _WIDE_GAP_RE.search(text[i:close]):
                continue
        if i != last:
            # 行中的候選：前面要是填白，後面不能還有台詞
            if not (_SPACE_RE.match(text[i - 1]) or text[i - 1] == '.'
                    or text.startswith('（_', i)):  # 吶喊框右邊線的寫法，可緊貼台詞
                continue
            following = _WIDE_GAP_RE.split(text[i + 1:], maxsplit=1)[0]
            if (_IDEOGRAPH_PAIR_RE.search(following)
                    and abs(m.measure(text[:i + 1]) - right_x) > _EXACT_TOLERANCE * _scale(m)):
                continue
        candidates.append(i)

    if not candidates:
        if (kind == 'normal' and last > left
                and text[last] in RIGHT_BORDER_CHARS
                and _SPACE_RE.match(text[last - 1])):
            return last
        return None

    near = [i for i in candidates
            if abs(m.measure(text[:i + 1]) - right_x) <= _RIGHT_TOLERANCE * _scale(m)]
    if not near:
        return candidates[0]
    chosen = min(near, key=lambda i: abs(m.measure(text[:i + 1]) - right_x))
    # 框緊貼 AA 牆（「| |」）時，靠內側的那條才是框線
    for i in reversed(candidates):
        if i < chosen and not _SPACE_RE.sub('', text[i + 1:chosen]).strip('.'):
            chosen = i
    return chosen


def _content_end(text: str, left: int, right: int | None) -> int:
    """台詞右端的字元位置。"""
    inner = text[left + 1:right]
    if right is not None:
        inner = inner.rstrip(',')  # 緊貼右邊線的半形逗號是填白
    if not inner.strip(' \u3000.'):
        return left + 1
    pad = _TRAILING_PAD_RE.search(inner)
    return left + 1 + (pad.start() if pad else len(inner))


def _is_open_frame(box: _Box, m: FontMeasurer) -> bool:
    """只有上下框、沒有左右邊線的旁白框（┏━┓／┗━┛ 中間純文字）。"""
    if box.kind != 'box':
        return False
    for row in box.rows:
        if row.left is not None:
            return False
        content = row.text.lstrip(' \u3000\u00a0.')
        if content and (m.measure(row.text[:len(row.text) - len(content)])
                        < box.left_x - _LEFT_TOLERANCE * _scale(m)):
            return False
    return True


def _parse_rows(texts: list[str], kind: str, first: int, stop: int,
                left_x: float, right_x: float, m: FontMeasurer,
                tolerance: float | None = None) -> list[_Row]:
    if tolerance is None:
        tolerance = _STYLES[kind].left_tolerance
    rows: list[_Row] = []
    for i in range(first, stop):
        text = texts[i]
        left = _find_left(text, kind, left_x, tolerance, m)
        if left is None:
            rows.append(_Row(i, text, None, None, 0))
            continue
        right = _find_right(text, kind, left, right_x, m)
        rows.append(_Row(i, text, left, right, _content_end(text, left, right)))
    return rows


def _has_unknown_width(row: _Row) -> bool:
    """這列沒有右邊線、後面又接了別的東西（AA、另一個框），無從得知台詞到哪裡結束。"""
    if row.left is None or row.right is not None:
        return False
    inner = row.text[row.left + 1:row.content_end].strip()
    return bool(_WIDE_GAP_RE.search(inner) or _BORDER_PIECE_RE.search(inner))


def _is_dialogue_box(box: _Box, texts: list[str], m: FontMeasurer) -> bool:
    """「對話框(全)」的把關：沒把握是對話框就不動，寧可漏修也不能改壞 AA。"""
    rows = box.rows
    if not rows or any(row.left is None for row in rows):
        return False

    closed = sum(row.right is not None for row in rows)
    if closed and any(_has_unknown_width(row) for row in rows):
        return False
    if box.kind == 'normal':
        if 0 < closed < len(rows) / 2:
            return False
    elif closed < ceil(len(rows) / 2):
        return False

    # 上下框不受翻譯影響，寬度應該相近
    if box.bot is not None:
        top = texts[box.top_line][box.top.start:box.top.end]
        bot = texts[box.bot_line][box.bot.start:box.bot.end]
        top_w, bot_w = m.measure(top), m.measure(bot)
        if abs(top_w - bot_w) > max(48.0 * _scale(m), top_w * 0.2):
            return False

    has_text = False
    ink = ''
    for row in rows:
        inner = row.text[row.left + 1:row.content_end]
        ink += _SPACE_RE.sub('', inner)
        if box.kind == 'box' and re.search('[│┃┌┐└┘┏┓┗┛]', inner):
            return False  # 巢狀框、表格、積木字
        has_text = has_text or bool(_TEXT_RE.search(inner))
    return has_text and len(_ART_RE.findall(ink)) < len(ink) * 0.4


# ════════════════════════════════════════════════════════════════
#  對話框偵測
# ════════════════════════════════════════════════════════════════

def detect_all_boxes(all_lines: list[str], m: FontMeasurer,
                     strict: bool = True) -> list[_Box]:
    """找出所有對話框，依左角 x 座標由左到右排序。

    Args:
        strict: True＝掃描全文，只收有把握的框；
                False＝使用者已選取範圍，角落變體、空框也接受。
    """
    texts = [_visible(line) for line in all_lines]
    scale = _scale(m)
    boxes: list[_Box] = []

    for kind in _STYLES:
        bot_tolerance = _LEFT_TOLERANCE if strict else _SELECTED_TOLERANCE
        row_tolerance = _STYLES[kind].left_tolerance if strict else _SELECTED_TOLERANCE
        if not strict and kind == 'normal' and boxes:
            break  # 普通框的寬鬆規則只在選取範圍內沒有其他框型時使用
        tops = {i: found for i, text in enumerate(texts)
                if (found := _find_borders(text, kind, True, strict))}
        if not tops:
            continue
        bots = {i: found for i, text in enumerate(texts)
                if (found := _find_borders(text, kind, False, strict))}

        covered = -1  # 寬鬆模式下普通框已涵蓋到第幾行
        for top_line, found_tops in tops.items():
            if not strict and kind == 'normal' and top_line <= covered:
                continue
            for ordinal, top in enumerate(found_tops):
                text = texts[top_line]
                left_x = m.measure(text[:top.start])
                right_x = m.measure(text[:top.end])

                bot_line, bot = -1, None
                for j in range(top_line + 1,
                               min(top_line + _MAX_BOX_LINES + 1, len(texts))):
                    for border in bots.get(j, ()):
                        x = m.measure(texts[j][:border.start])
                        if abs(x - left_x) <= bot_tolerance * scale:
                            bot_line, bot = j, border
                            break
                    if bot is not None:
                        break
                    # 同一位置又出現一個上框：這個上框沒有自己的下框
                    if strict and any(
                        abs(m.measure(texts[j][:t.start]) - left_x) <= _LEFT_TOLERANCE * scale
                        for t in tops.get(j, ())
                    ):
                        break
                if bot is None:
                    if strict or kind != 'normal':
                        continue
                    bot_line = len(texts)

                rows = _parse_rows(texts, kind, top_line + 1, bot_line,
                                   left_x, right_x, m, row_tolerance)
                box = _Box(kind, top_line, bot_line, ordinal, left_x, top, bot, rows)
                if strict:
                    if not _is_dialogue_box(box, texts, m):
                        continue
                elif (rows and all(row.left is None for row in rows)
                      and not _is_open_frame(box, m)):
                    continue
                elif bot is None and all(row.right is None for row in rows):
                    continue
                elif kind == 'box' and any(
                    re.search('[┌┐└┘┏┓┗┛]', row.text[row.left + 1:row.content_end])
                    for row in rows if row.left is not None
                ):
                    continue  # 積木字、巢狀框：即使選取了也不能拉寬
                boxes.append(box)
                covered = bot_line

    boxes.sort(key=lambda box: (box.left_x, box.top_line))
    return boxes


# ════════════════════════════════════════════════════════════════
#  對話框重建
# ════════════════════════════════════════════════════════════════

def _build_top(text: str, box: _Box, width: float,
               m: FontMeasurer) -> tuple[str, int]:
    """組出寬度不小於 *width* 的上框，回傳 (上框, 橫線單位數)。"""
    old = text[box.top.start:box.top.end]
    minimum = 1
    if box.kind == 'normal':
        a, b = box.top.fill
        head, unit, tail = text[box.top.start:a], text[a], text[b:box.top.end]
        minimum = 3
    elif box.kind == 'slash':
        head, unit, tail = '＼─|', '──|', '─／'
    elif box.kind == 'shout':
        # 沿用作者自己的節距（_人 或 __人）與頭尾字元
        first, last = old.index('人'), old.rindex('人')
        repeated = re.match(r'[_＿]+人', old[first + 1:])
        unit = repeated.group() if repeated else '_人'
        head, tail = old[:first + 1], old[last + 1:]
        minimum = 2
    else:
        head, unit, tail = old[0], old[1], old[-1]

    count = max(minimum,
                ceil((width - m.measure(head + tail)) / m.measure(unit) - 0.01))
    return head + unit * count + tail, count


def _build_bottom(text: str, box: _Box, count: int, width: float,
                  m: FontMeasurer) -> str:
    """組出與上框對稱的下框。"""
    assert box.bot is not None
    old = text[box.bot.start:box.bot.end]
    if box.kind == 'normal':
        a, b = box.bot.fill
        return text[box.bot.start:a] + text[a] * count + text[b:box.bot.end]
    if box.kind == 'slash':
        return '／─|' + '──|' * count + '─＼'
    if box.kind != 'shout':
        return old[0] + old[1] * count + old[-1]

    # 吶喊框下框的節距與上框不同，改取最接近上框寬度的個數
    head = '／' if old.startswith('／') else ''
    tail = '＼' if old.endswith('＼') else ''
    body = old[len(head):len(old) - len(tail)]
    unit = body[:2]
    last = '⌒' if body.endswith('⌒') else '⌒Ｙ'
    n = max(2, round((width - m.measure(head + last + tail)) / m.measure(unit)))
    return head + unit * n + last + tail


def _right_offsets(box: _Box, m: FontMeasurer) -> dict[int, float]:
    """各列右邊線右緣相對於上框右緣的位置。"""
    style = _STYLES[box.kind]
    insets: dict[str, list[float]] = {}
    for row in box.rows:
        if row.left is not None and row.text[row.left] in style.mirrored:
            insets.setdefault(row.text[row.left], []).append(
                m.measure(row.text[:row.left]) - box.left_x)
    mirrored = {char: -median(xs) for char, xs in insets.items()}
    default = style.right_offset * _scale(m)
    return {row.line: mirrored.get(row.text[row.left], default)
            for row in box.rows if row.left is not None}


def _has_right_side_art(box: _Box, top_text: str, bot_text: str) -> bool:
    """框的右側是否還有別的東西（AA、另一個框）。"""
    tails = [top_text[box.top.end:]]
    if box.bot is not None:
        tails.append(bot_text[box.bot.end:])
    tails.extend(row.text[row.right + 1:] for row in box.rows if row.right is not None)
    return any(_SPACE_RE.sub('', tail).strip('_＿,.\r') for tail in tails)


def _rebuild_box(lines: list[str], box: _Box, m: FontMeasurer) -> None:
    """就地重建 *lines* 中的一個對話框。"""
    top_text = _visible(lines[box.top_line])
    bot_text = _visible(lines[box.bot_line]) if box.bot is not None else ''
    old_right = m.measure(top_text[:box.top.end])
    offsets = _right_offsets(box, m)
    margin = 2 * m.measure('　')

    any_closed = any(row.right is not None for row in box.rows)
    needed = [
        m.measure(row.text[:row.content_end]) + margin
        + m.measure(row.text[row.left if row.right is None else row.right])
        - offsets[row.line]
        for row in box.rows
        if row.left is not None and row.content_end > row.left + 1
        and not _has_unknown_width(row)
    ]
    if box.rows and _is_open_frame(box, m):
        # 旁白框沒有邊線可對齊，只需確保橫線蓋得住最長的一行
        needed = [m.measure(text) + margin for row in box.rows
                  if (text := row.text.rstrip(' \u3000\u00a0\r'))]
    target = max(needed, default=old_right)

    # 一般會依台詞縮放。但右側有 AA（縮了會把整片圖拉歪）或根本沒有右邊線
    # （開放式的框，寬度是作者自己定的）時只放寬、不縮窄
    keep_width = not needed
    if not any_closed or _has_right_side_art(box, top_text, bot_text):
        keep_width = keep_width or target <= old_right

    right_x = old_right
    if not keep_width:
        new_top, count = _build_top(top_text, box, target - box.left_x, m)
        right_x = m.measure(top_text[:box.top.start] + new_top)
        lines[box.top_line] = _replace_visible(
            lines[box.top_line], box.top.start, box.top.end, new_top)
        if box.bot is not None:
            width = right_x - m.measure(bot_text[:box.bot.start])
            new_bot = _build_bottom(bot_text, box, count, width, m)
            lines[box.bot_line] = _replace_visible(
                lines[box.bot_line], box.bot.start, box.bot.end, new_bot)

    for row in box.rows:
        if row.left is None or row.right is None:
            continue
        used = m.measure(row.text[:row.content_end] + row.text[row.right])
        padding = _make_padding(right_x + offsets[row.line] - used, m)
        lines[row.line] = _replace_visible(
            lines[row.line], row.content_end, row.right, padding)


def _adjust_boxes(lines: list[str], m: FontMeasurer, strict: bool) -> int:
    """就地修正所有對話框，回傳處理的框數。

    並排的框必須由左到右：左框修完，右框各列的 x 座標才會歸位（修之前甚至歪到
    偵測不出來），所以每輪只處理「列範圍內沒有更靠左待修框」的框，修完重新偵測，
    直到沒有新的框為止。
    """
    done: set[tuple[str, int, int]] = set()
    while True:
        claimed: set[int] = set()
        batch: list[_Box] = []
        for box in detect_all_boxes(lines, m, strict):
            key = (box.kind, box.top_line, box.ordinal)
            if key in done:
                continue
            span = range(box.top_line, box.bot_line + 1)
            if claimed.isdisjoint(span):
                batch.append(box)
                done.add(key)
            claimed.update(span)
        for box in batch:
            _rebuild_box(lines, box, m)
        if not batch:
            return len(done)


# ════════════════════════════════════════════════════════════════
#  高階 API
# ════════════════════════════════════════════════════════════════

def adjust_bubble(selected_text: str, m: FontMeasurer) -> str | None:
    """對使用者選取的文本進行對話框修正。

    自動偵測框型，回傳修正後的文本（行數不變）。
    錯誤以 str 回傳，開頭為 '⚠️'。
    """
    lines = selected_text.split('\n')
    # 先用嚴格規則（選取範圍夾帶 AA 時才不會認錯框），找不到才放寬
    if _adjust_boxes(lines, m, strict=True) or _adjust_boxes(lines, m, strict=False):
        return '\n'.join(lines)
    return '⚠️ 選取的範圍找不到完整的對話框（上下框與左邊線），請確認選取範圍！'


def adjust_all_bubbles(text: str, m: FontMeasurer) -> tuple[str, int]:
    """掃描全文並自動對齊所有對話框。

    Returns:
        (修正後的完整文本, 成功修正的對話框數量)
    """
    all_lines = text.split('\n')
    count = _adjust_boxes(all_lines, m, strict=True)
    return '\n'.join(all_lines), count


# ════════════════════════════════════════════════════════════════
#  高階 API：對齊上一行
# ════════════════════════════════════════════════════════════════

def align_to_prev_line(
    prev_line_text: str,
    current_line_text: str,
    col_idx: int,
    m: FontMeasurer,
) -> tuple[str, int] | None:
    """將目前行游標後方的符號對齊到上一行末端。

    Args:
        prev_line_text: 上一行文字（已去除尾部空白）
        current_line_text: 目前行完整文字
        col_idx: 游標在目前行中的位置（column index）
        m: 字型量測器

    Returns:
        (修正後的目前行文字, 新游標 column) 或 None 表示無法對齊。
    """
    if not prev_line_text:
        return None

    # 找游標後第一個非空白字元
    target_col = -1
    for i in range(col_idx, len(current_line_text)):
        if current_line_text[i] not in [' ', '　']:
            target_col = i
            break
    if target_col == -1:
        return None

    selected_text = current_line_text[target_col:]
    prev_content = prev_line_text.rstrip(_PAD_CHARS) or prev_line_text
    if len(prev_content) > 1:
        target_width = m.measure(prev_content[:-1])
    else:
        target_width = m.measure(prev_content)

    text_before = current_line_text[:target_col]
    stripped_before = text_before.rstrip(' \u3000')

    if m.measure(stripped_before) >= target_width:
        res_prefix = stripped_before
    else:
        res_prefix = _pad_to_width(stripped_before, target_width, m)

    new_col = len(res_prefix) + 1
    return res_prefix + ' ' + selected_text, new_col
