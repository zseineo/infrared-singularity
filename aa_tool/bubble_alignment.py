"""對話框對齊演算法 — 純邏輯，不依賴任何 UI 框架。

所有字寬量測透過 FontMeasurer 協議完成。
支援四種對話框類型：
  - normal: 普通框（￣/＿）
  - shout:  吶喊框（_人/⌒Y）
  - slash:  斜線框（＼─|／）
  - box:    方框（┌─┐ / │ / └─┘）
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .font_measure import FontMeasurer


# ════════════════════════════════════════════════════════════════
#  共用常數
# ════════════════════════════════════════════════════════════════

SHOUT_DELIM_PAIRS = [('）', '（'), ('＞', '＜'), ('>', '<'), ('》', '《')]
SLASH_DELIM_CHARS = ['│', '─']
RIGHT_BORDER_CHARS = '│｜|〉》>）)ノﾉ＼ヽ｝}］]'

# Padding 字元：半形空白 + 全形空白 + 半形點，皆視為對齊用填白
_PAD_CHARS = ' \u3000.'


def _content_width(text: str, m: 'FontMeasurer') -> float:
    """量測「內容寬度」：從右端剝除 padding 字元後再量測。"""
    return m.measure(text.rstrip(_PAD_CHARS))


# ════════════════════════════════════════════════════════════════
#  內部輔助：解析行結構
# ════════════════════════════════════════════════════════════════

def _parse_shout_lines(box_lines: list[str]) -> list[dict]:
    """將吶喊框的各行解析為結構化字典。"""
    re_top = re.compile(r'[､、]?[_＿]+(?:人[_＿]*)+')
    re_bot = re.compile(r'(?:⌒[YＹ]){2,}')
    parsed: list[dict] = []

    for sl in box_lines:
        m_t = re_top.search(sl)
        m_b = re_bot.search(sl)
        if m_t and not m_b:
            parsed.append({
                'type': 'top',
                'prefix': sl[:m_t.start()],
                'bubble': sl[m_t.start():].rstrip(),
                'orig': sl,
            })
        elif m_b:
            parsed.append({
                'type': 'bot',
                'prefix': sl[:m_b.start()],
                'bubble': sl[m_b.start():].rstrip(),
                'orig': sl,
            })
        else:
            stripped = sl.rstrip(' \u3000\r\n')
            found = False
            for lc, rc in SHOUT_DELIM_PAIRS:
                if not stripped.endswith(rc):
                    continue
                rc_pos = len(stripped) - 1
                lc_pos = stripped.rfind(lc, 0, rc_pos)
                if lc_pos == -1:
                    continue
                inner = re.sub(r',+$', '', stripped[lc_pos + 1:rc_pos])
                parsed.append({
                    'type': 'content',
                    'prefix': sl[:lc_pos],
                    'left_char': lc,
                    'right_char': rc,
                    'inner': inner,
                    'orig': sl,
                })
                found = True
                break
            if not found:
                parsed.append({'type': 'other', 'orig': sl})

    return parsed


def _parse_slash_lines(box_lines: list[str]) -> list[dict]:
    """將斜線框的各行解析為結構化字典。"""
    parsed: list[dict] = []
    for sl in box_lines:
        mt = re.search(r'＼─\|(?:──\|)+─?／', sl)
        mb = re.search(r'／─\|(?:──\|)+─?＼', sl)
        if mt:
            parsed.append({
                'type': 'top',
                'prefix': sl[:mt.start()],
                'bubble': sl[mt.start():mt.end()],
                'orig': sl,
            })
        elif mb:
            parsed.append({
                'type': 'bot',
                'prefix': sl[:mb.start()],
                'bubble': sl[mb.start():mb.end()],
                'orig': sl,
            })
        else:
            stripped = sl.rstrip(' \u3000\r\n')
            found = False
            for dc in SLASH_DELIM_CHARS:
                if not stripped.endswith(dc):
                    continue
                rc_pos = len(stripped) - 1
                lc_pos = stripped.rfind(dc, 0, rc_pos)
                if lc_pos == -1:
                    continue
                inner = re.sub(r',+$', '', stripped[lc_pos + 1:rc_pos])
                parsed.append({
                    'type': 'content',
                    'prefix': sl[:lc_pos],
                    'left_char': dc,
                    'right_char': dc,
                    'inner': inner,
                    'orig': sl,
                })
                found = True
                break
            if not found:
                parsed.append({'type': 'other', 'orig': sl})
    return parsed


def _parse_normal_lines(box_lines: list[str]) -> list[dict]:
    """將普通對話框的各行解析為結構化字典。"""
    parsed: list[dict] = []
    for bl in box_lines:
        if not bl:
            parsed.append({'type': 'orig', 'orig': bl})
            continue
        bl_n = bl.rstrip('\r\n \u3000')
        matched_border = False
        for bc in ['￣', '＿']:
            bm_list = list(re.finditer(f'({re.escape(bc)}{{3,}})', bl_n))
            if not bm_list:
                continue
            for bm in reversed(bm_list):
                rp = bl_n[bm.end():]
                if len(rp.strip(' \u3000')) <= 4:
                    lp = bl_n[:bm.start()]
                    parsed.append({
                        'type': 'border',
                        'left': lp,
                        'char': bc,
                        'right': rp,
                        'orig': bl_n,
                    })
                    matched_border = True
                    break
            if matched_border:
                break
        if matched_border:
            continue

        cm = re.search(r'^(.*?[^ \u3000.])([ \u3000.]+)([^ \u3000.]{1,3})$', bl_n)
        if cm:
            rc = cm.group(3)
            if any(c in rc for c in RIGHT_BORDER_CHARS):
                lt = re.sub(r',+$', '', cm.group(1))
                parsed.append({
                    'type': 'content',
                    'left': lt,
                    'char': ' ',
                    'right_padding': cm.group(2),
                    'right': rc,
                    'orig': bl_n,
                })
                continue
        lt = re.sub(r',+$', '', bl_n)
        parsed.append({
            'type': 'content',
            'left': lt,
            'char': ' ',
            'right_padding': '',
            'right': '',
            'orig': bl_n,
        })
    return parsed


# ════════════════════════════════════════════════════════════════
#  內部輔助：補空白至目標寬度
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


# ════════════════════════════════════════════════════════════════
#  三種框型的共用處理函式
# ════════════════════════════════════════════════════════════════

def process_shout(box_lines: list[str], m: FontMeasurer) -> list[str] | None:
    """處理吶喊框，回傳對齊後的各行。失敗回傳 None。"""
    parsed = _parse_shout_lines(box_lines)

    # 原始邊框寬度
    obw = 0
    for ps in parsed:
        if ps['type'] in ('top', 'bot') and 'bubble' in ps:
            obw = m.measure(ps['bubble'])
            break
    if obw == 0:
        return None

    # 內容所需最小寬度
    mcw = 0
    for ps in parsed:
        if ps['type'] == 'content':
            inner_stripped = ps['inner'].rstrip(_PAD_CHARS)
            needed = m.measure(
                ps['left_char'] + inner_stripped + '　' + ps['right_char']
            )
            if needed > mcw:
                mcw = needed
    tw = (mcw + m.measure('　')) if mcw > 0 else obw

    # 重建
    result: list[str] = []
    for ps in parsed:
        if ps['type'] == 'top':
            bubble = ps['bubble']
            corner = bubble[0] if bubble and bubble[0] in '､、' else ''
            res = corner + '_'
            while m.measure(res + '_人') <= tw:
                res += '_人'
            result.append(ps['prefix'] + res)
        elif ps['type'] == 'bot':
            res = ''
            while m.measure(res + '⌒Y') <= tw:
                res += '⌒Y'
            if res.endswith('Y'):
                res = res[:-1] + 'Ｙ'
            result.append(ps['prefix'] + res)
        elif ps['type'] == 'content':
            lc, rc = ps['left_char'], ps['right_char']
            inner = ps['inner'].rstrip(_PAD_CHARS)
            pw = m.measure(lc) + m.measure(rc)
            tiw = max(tw - pw, 0.0)
            padded = _pad_to_width(inner, tiw, m)
            result.append(ps['prefix'] + lc + padded + rc)
        else:
            result.append(ps['orig'])
    return result


def process_slash(box_lines: list[str], m: FontMeasurer) -> list[str] | None:
    """處理斜線框，回傳對齊後的各行。失敗回傳 None。"""
    parsed = _parse_slash_lines(box_lines)

    obw = 0
    for ps in parsed:
        if ps['type'] in ('top', 'bot') and 'bubble' in ps:
            obw = m.measure(ps['bubble'])
            break
    if obw == 0:
        return None

    mcw = 0
    for ps in parsed:
        if ps['type'] == 'content':
            inner_stripped = ps['inner'].rstrip(_PAD_CHARS)
            needed = m.measure(
                ps['left_char'] + inner_stripped + '　' + ps['right_char']
            )
            if needed > mcw:
                mcw = needed
    tw = (mcw + m.measure('　')) if mcw > 0 else obw

    result: list[str] = []
    for ps in parsed:
        if ps['type'] == 'top':
            res = '＼─|'
            while m.measure(res + '──|' + '─／') <= tw:
                res += '──|'
            res += '─／'
            result.append(ps['prefix'] + res)
        elif ps['type'] == 'bot':
            res = '／─|'
            while m.measure(res + '──|' + '─＼') <= tw:
                res += '──|'
            res += '─＼'
            result.append(ps['prefix'] + res)
        elif ps['type'] == 'content':
            lc, rc = ps['left_char'], ps['right_char']
            inner = ps['inner'].rstrip(_PAD_CHARS)
            pw = m.measure(lc) + m.measure(rc)
            tiw = max(tw - pw, 0.0)
            padded = _pad_to_width(inner, tiw, m)
            result.append(ps['prefix'] + lc + padded + rc)
        else:
            result.append(ps['orig'])
    return result


def process_normal(box_lines: list[str], m: FontMeasurer) -> list[str] | None:
    """處理普通對話框，回傳對齊後的各行。失敗回傳 None。

    目標寬計算：邊框左側錨點 + 對話框內側內容最大寬 + 邊框右側。
    對話框內側寬度只量 content 行裡「最右邊 |/│/｜ 之後到末尾非 padding」的部分。
    這個量會自然包含框內 leading pad（例如 `　 `），因此不需要再額外加全形空白餘裕；
    若再加會多出 1-2 個 ￣ 變成不必要的延伸/不夠縮減。

    上下邊框 (`f´…￣` / `乂…＿`) 用相同的 ￣/＿ 數量重建（以 top 為基準算 ref_n，
    再套用到所有 border），維持對話框視覺對稱；各 border 自身的 right (`｀ヽ` 2 fw vs
    `ノ` 1 fw) 不同會讓總寬不同 0.5-1 fw，但符合手寫慣例。
    """
    parsed = _parse_normal_lines(box_lines)
    fw_w = m.measure('　')

    # 邊框錨點（取第一個 border 行）：整行寬度、左側（`AA + pad + f´`）、右側（`｀ヽ`）
    obw = 0.0
    border_left_w = 0.0
    border_right_w = 0.0
    top_border = None
    for p in parsed:
        if p['type'] == 'border':
            obw = m.measure(str(p['orig']))
            border_left_w = m.measure(str(p['left']))
            border_right_w = m.measure(str(p['right']))
            top_border = p
            break

    # 對話框內側最大寬（從 content 行最右邊的 |/│/｜ 之後量起）
    max_inner_w = 0.0
    for p in parsed:
        if p['type'] == 'content' and p.get('right'):
            left = str(p.get('left', ''))
            pos = max(left.rfind('|'), left.rfind('│'), left.rfind('｜'))
            if pos >= 0:
                inner = left[pos + 1:].rstrip(_PAD_CHARS)
                w = m.measure(inner)
                if w > max_inner_w:
                    max_inner_w = w
    if max_inner_w <= 0 and obw <= 0:
        return None

    tw = (border_left_w + max_inner_w + border_right_w) if max_inner_w > 0 else obw

    # 從 top border 算出 ref_n（￣ 數量），再套用到所有 border 維持對稱
    ref_n = 0
    if top_border is not None and max_inner_w > 0:
        pc = str(top_border['char'])
        right = str(top_border['right'])
        target_left_w = tw - m.measure(right)
        res = str(top_border['left'])
        while m.measure(res + pc) <= target_left_w:
            res += pc
            ref_n += 1
        if (m.measure(res + pc) - target_left_w) < (target_left_w - m.measure(res)):
            ref_n += 1

    new_box: list[str] = []
    for p in parsed:
        if p['type'] == 'border':
            pc = str(p['char'])
            right = str(p['right'])
            if max_inner_w > 0:
                # 使用 top 的 ref_n 數量套到 top/bot 兩邊邊框
                res = str(p['left']) + (pc * ref_n)
            else:
                # 無 content：保持原長
                target_left_w = tw - m.measure(right)
                res = str(p['left'])
                while m.measure(res + pc) <= target_left_w:
                    res += pc
                if (m.measure(res + pc) - target_left_w) < (target_left_w - m.measure(res)):
                    res += pc
            new_box.append(res + right)
        elif p['type'] == 'content':
            if p.get('right'):
                right = str(p['right'])
                target_left_w = tw - m.measure(right)
                left_stripped = str(p['left']).rstrip(_PAD_CHARS)
                padded = _pad_to_width(left_stripped, target_left_w, m)
                new_box.append(padded + right)
            else:
                new_box.append(str(p.get('left', p.get('orig', ''))))
        else:
            new_box.append(str(p.get('orig', '')))
    return new_box


_BOX_TOP_RE = re.compile(r'^(.*?)(┌)(─+)(┐)\s*$')
_BOX_BOT_RE = re.compile(r'^(.*?)(└)(─+)(┘)\s*$')
_BOX_CONTENT_RE = re.compile(r'^(.*?)(│)(.*)(│)\s*$')


def _parse_box_lines(box_lines: list[str]) -> list[dict]:
    """將方框框的各行解析為結構化字典。"""
    parsed: list[dict] = []
    for sl in box_lines:
        sl_n = sl.rstrip('\r\n')
        mt = _BOX_TOP_RE.match(sl_n)
        mb = _BOX_BOT_RE.match(sl_n)
        mc = _BOX_CONTENT_RE.match(sl_n)
        if mt:
            parsed.append({
                'type': 'top',
                'prefix': mt.group(1),
                'dashes': mt.group(3),
                'orig': sl_n,
            })
        elif mb:
            parsed.append({
                'type': 'bot',
                'prefix': mb.group(1),
                'dashes': mb.group(3),
                'orig': sl_n,
            })
        elif mc:
            inner = re.sub(r',+$', '', mc.group(3))
            parsed.append({
                'type': 'content',
                'prefix': mc.group(1),
                'inner': inner,
                'orig': sl_n,
            })
        else:
            parsed.append({'type': 'other', 'orig': sl_n})
    return parsed


def process_box(box_lines: list[str], m: FontMeasurer) -> list[str] | None:
    """處理方框框（┌─┐/│/└─┘），回傳對齊後的各行。失敗回傳 None。"""
    parsed = _parse_box_lines(box_lines)

    # 原始邊框寬度（由第一個 top 的 ┌─...─┐ 量測）
    obw = 0
    prefix = ''
    for ps in parsed:
        if ps['type'] == 'top':
            obw = m.measure('┌' + ps['dashes'] + '┐')
            prefix = ps['prefix']
            break
    if obw == 0:
        return None

    # 內容所需最小寬度
    mcw = 0
    for ps in parsed:
        if ps['type'] == 'content':
            needed = m.measure('│' + ps['inner'].rstrip(_PAD_CHARS) + '　│')
            if needed > mcw:
                mcw = needed
    tw = (mcw + m.measure('　')) if mcw > 0 else obw

    # 計算邊框需要幾個 ─
    dash_w = m.measure('─')
    lr_w = m.measure('┌') + m.measure('┐')
    inner_target = max(tw - lr_w, dash_w)

    # 重建
    result: list[str] = []
    for ps in parsed:
        if ps['type'] == 'top':
            dashes = ''
            while m.measure(dashes + '─') <= inner_target:
                dashes += '─'
            # snap：若再多一個更接近目標則補上
            d1 = inner_target - m.measure(dashes)
            d2 = m.measure(dashes + '─') - inner_target
            if d2 < d1:
                dashes += '─'
            result.append(ps['prefix'] + '┌' + dashes + '┐')
        elif ps['type'] == 'bot':
            dashes = ''
            while m.measure(dashes + '─') <= inner_target:
                dashes += '─'
            d1 = inner_target - m.measure(dashes)
            d2 = m.measure(dashes + '─') - inner_target
            if d2 < d1:
                dashes += '─'
            result.append(ps['prefix'] + '└' + dashes + '┘')
        elif ps['type'] == 'content':
            inner = ps['inner'].rstrip(_PAD_CHARS)
            side_w = m.measure('│') + m.measure('│')
            tiw = max(tw - side_w, 0.0)
            padded = _pad_to_width(inner, tiw, m)
            result.append(ps['prefix'] + '│' + padded + '│')
        else:
            result.append(ps['orig'])
    return result


# ════════════════════════════════════════════════════════════════
#  高階 API：單選對話框修正
# ════════════════════════════════════════════════════════════════

def adjust_bubble(selected_text: str, m: FontMeasurer) -> str | None:
    """對使用者選取的文本進行對話框修正。

    自動偵測框型（吶喊/斜線/普通），回傳修正後的文本。
    回傳 None 表示選取範圍無法辨識為有效對話框。

    錯誤以 str 回傳，開頭為 '⚠️'。
    """
    lines = selected_text.split('\n')

    # ── 偵測吶喊框 ──
    has_top = any('_人' in ln for ln in lines)
    has_bot = any('⌒Y' in ln or '⌒Ｙ' in ln for ln in lines)
    if has_top and has_bot:
        result = process_shout(lines, m)
        if result is None:
            return '⚠️ 無法計算吶喊框寬度！'
        return '\n'.join(result)

    # ── 偵測斜線框 ──
    has_slash_top = any(re.search(r'＼─\|(?:──\|){2,}', ln) for ln in lines)
    has_slash_bot = any(re.search(r'／─\|(?:──\|){2,}', ln) for ln in lines)
    if has_slash_top and has_slash_bot:
        result = process_slash(lines, m)
        if result is None:
            return '⚠️ 無法計算斜線框寬度！'
        return '\n'.join(result)

    # ── 方框（┌─┐） ──
    has_box_top = any(_BOX_TOP_RE.match(ln.rstrip('\r\n')) for ln in lines)
    has_box_bot = any(_BOX_BOT_RE.match(ln.rstrip('\r\n')) for ln in lines)
    if has_box_top and has_box_bot:
        result = process_box(lines, m)
        if result is None:
            return '⚠️ 無法計算方框寬度！'
        return '\n'.join(result)

    # ── 普通對話框 ──
    # 先檢查是否有標準邊界
    has_border = False
    for line in lines:
        line_n = line.rstrip('\r\n \u3000')
        for char in ['￣', '＿', '─', '-', '=']:
            matches = list(re.finditer(f'({re.escape(char)}{{3,}})', line_n))
            if matches:
                for match in reversed(matches):
                    right_part = line_n[match.end():]
                    if len(right_part.strip(' \u3000')) <= 4:
                        has_border = True
                        break
            if has_border:
                break
        if has_border:
            break

    if not has_border:
        return '⚠️ 選取的範圍沒有標準對話框邊界 (￣ 或 ＿)，請確認選取範圍！'

    result = process_normal(lines, m)
    if result is None:
        return '⚠️ 無法計算對話框寬度！'
    return '\n'.join(result)


# ════════════════════════════════════════════════════════════════
#  高階 API：全文對話框偵測與修正
# ════════════════════════════════════════════════════════════════

def _is_safe_normal_border(left_part: str, right_part: str, char: str) -> bool:
    """安全性檢查：確認邊框角落字元符合已知模式，避免誤判 AA 圖案。"""
    rp_clean = right_part.strip(' \u3000')
    if len(rp_clean) > 4:
        return False
    lp_end = left_part.rstrip(' \u3000')
    last_c = lp_end[-1] if lp_end else ''
    if char == '￣':
        if last_c not in "´'":
            return False
        if not any(c in rp_clean for c in '｀ヽﾍ'):
            return False
    elif char == '＿':
        if last_c not in '乂ヽ丶':
            return False
        if not any(c in rp_clean for c in 'ノﾉ'):
            return False
    else:
        return False
    return True


def detect_all_boxes(all_lines: list[str]) -> list[tuple[int, int, str]]:
    """掃描全文，找出所有獨立對話框的範圍。

    Returns:
        list of (top_line_idx, bot_line_idx, box_type)
        box_type: 'shout' | 'slash' | 'normal'
    """
    all_boxes: list[tuple[int, int, str]] = []
    used_lines: set[int] = set()

    # ── 偵測 A: 吶喊框 ──
    re_shout_top = re.compile(r'[､、][_＿]+(?:人[_＿]*){3,}')
    re_shout_bot = re.compile(r'(?:⌒[YＹ]){3,}')
    s_tops: list[int] = []
    s_bots: list[int] = []
    for i, ln in enumerate(all_lines):
        if re_shout_top.search(ln):
            s_tops.append(i)
        if re_shout_bot.search(ln):
            s_bots.append(i)
    for ti in s_tops:
        if ti in used_lines:
            continue
        for bi in s_bots:
            if bi <= ti or bi in used_lines:
                continue
            if bi - ti > 30:
                break
            all_boxes.append((ti, bi, 'shout'))
            for k in range(ti, bi + 1):
                used_lines.add(k)
            break

    # ── 偵測 B: 斜線框 ──
    re_slash_top = re.compile(r'＼─\|(?:──\|){2,}─?／')
    re_slash_bot = re.compile(r'／─\|(?:──\|){2,}─?＼')
    for i, ln in enumerate(all_lines):
        if i in used_lines:
            continue
        if not re_slash_top.search(ln):
            continue
        for j in range(i + 1, min(i + 31, len(all_lines))):
            if j in used_lines:
                continue
            if re_slash_bot.search(all_lines[j]):
                all_boxes.append((i, j, 'slash'))
                for k in range(i, j + 1):
                    used_lines.add(k)
                break

    # ── 偵測 C: 方框（┌─┐） ──
    for i, ln in enumerate(all_lines):
        if i in used_lines:
            continue
        if not _BOX_TOP_RE.match(ln.rstrip('\r\n')):
            continue
        for j in range(i + 1, min(i + 31, len(all_lines))):
            if j in used_lines:
                continue
            if _BOX_BOT_RE.match(all_lines[j].rstrip('\r\n')):
                all_boxes.append((i, j, 'box'))
                for k in range(i, j + 1):
                    used_lines.add(k)
                break

    # ── 偵測 D: 普通對話框 ──
    n_borders: list[dict] = []
    for i, line in enumerate(all_lines):
        if i in used_lines:
            continue
        line_n = line.rstrip('\r\n \u3000')
        for char, btype in [('￣', 'top'), ('＿', 'bot')]:
            matches = list(re.finditer(f'({re.escape(char)}{{3,}})', line_n))
            if not matches:
                continue
            for mt in reversed(matches):
                rp = line_n[mt.end():]
                if len(rp.strip(' \u3000')) <= 4:
                    lp = line_n[:mt.start()]
                    if _is_safe_normal_border(lp, rp, char):
                        n_borders.append({
                            'line': i, 'btype': btype,
                            'left': lp, 'char': char, 'right': rp, 'orig': line_n,
                        })
                    break
            break

    n_tops = [b for b in n_borders if b['btype'] == 'top']
    n_bots = [b for b in n_borders if b['btype'] == 'bot']
    for top in n_tops:
        ti = top['line']
        if ti in used_lines:
            continue
        for bot in n_bots:
            bi = bot['line']
            if bi <= ti or bi in used_lines:
                continue
            if bi - ti > 30:
                break
            has_inner = any(
                t['line'] > ti and t['line'] < bi and t['line'] not in used_lines
                for t in n_tops if t is not top
            )
            if has_inner:
                break
            all_boxes.append((ti, bi, 'normal'))
            for k in range(ti, bi + 1):
                used_lines.add(k)
            break

    all_boxes.sort(key=lambda x: x[0])
    return all_boxes


def adjust_all_bubbles(text: str, m: FontMeasurer) -> tuple[str, int]:
    """掃描全文並自動對齊所有對話框。

    Returns:
        (修正後的完整文本, 成功修正的對話框數量)
    """
    all_lines = text.split('\n')
    all_boxes = detect_all_boxes(all_lines)

    count = 0
    for top_idx, bot_idx, box_type in reversed(all_boxes):
        box_lines = all_lines[top_idx:bot_idx + 1]
        if box_type == 'shout':
            result = process_shout(box_lines, m)
        elif box_type == 'slash':
            result = process_slash(box_lines, m)
        elif box_type == 'box':
            result = process_box(box_lines, m)
        else:
            result = process_normal(box_lines, m)
        if result is not None:
            all_lines[top_idx:bot_idx + 1] = result
            count += 1

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
