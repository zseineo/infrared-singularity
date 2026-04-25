import re

from .constants import DEFAULT_BASE_REGEX, DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX


def _compile_regex(pattern: str, default: str) -> re.Pattern:
    """嘗試編譯正則，失敗時使用預設值。"""
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(default)


def _compile_custom_filters(filter_str: str) -> list[re.Pattern]:
    """將使用者輸入的過濾規則（每行一條正則）編譯為 list。"""
    regexes = []
    for line in filter_str.split('\n'):
        line = line.strip()
        if line:
            try:
                regexes.append(re.compile(line))
            except re.error:
                pass
    return regexes


def _postprocess_text(text: str) -> str:
    """提取後的字串後處理（去除非內文字元）。"""
    # 若被邊框符號 │ 或 | 包起來，則只取邊框內的文字
    match = re.search(r'[│\|](.*)[│\|]', text)
    if match:
        text = match.group(1).strip(' \t　.')

    # 移除開頭非平假名、片假名、漢字、英文、數字的部分
    text = re.sub(
        r'^([^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFa-zA-ZＡ-Ｚａ-ｚ0-9０-９]+)',
        '', text
    ).strip()

    # 移除結尾的骰子格式 【數字D數字:數字】
    text = re.sub(r'【\d+D\d+:\d+】$', '', text).strip()
    # 移除開頭的「數字＋點」格式（全形半形皆可），如 １．、1.、３.
    text = re.sub(r'^[0-9０-９]+[.．]', '', text).strip()
    # 若句尾包含特定符號，則將其刪除
    text = text.rstrip('|｜(（').strip()

    return text


# 2ch/5ch 發文者行：形如「4402 ： ◆GESU1/dEaE ： 2021/05/06(木) 23:19:36 ID:nGcM5Umt」
# 特徵：含 ID:xxxxxx（trip code），這組標記對發文者行幾乎是唯一判別
_POSTER_LINE_RE = re.compile(r'ID:[A-Za-z0-9+/.]{6,}')


_BRACKET_PAIRS = {
    '【': '】', '】': '【',
    '「': '」', '」': '「',
    '\u201c': '\u201d', '\u201d': '\u201c',  # ""
    '\u2018': '\u2019', '\u2019': '\u2018',  # ''
    '『': '』', '』': '『',
    '（': '）', '）': '（',
    '(': ')', ')': '(',
}
_OPEN_BRACKETS = set('【「\u201c\u2018『（(')
_CLOSE_BRACKETS = set('】」\u201d\u2019』）)')


def _complete_brackets(text: str, source_line: str) -> str:
    """檢查 text 中的括號是否成對，視需要補上缺少的括號。

    對每個未配對的括號，在原始行中定位 text 的位置，
    再檢查緊鄰的相鄰字元是否恰好是該缺少的括號：
    - 缺少左括號（有未配對的右括號）→ 檢查 text 前一個字元
    - 缺少右括號（有未配對的左括號）→ 檢查 text 後一個字元
    有就補上，沒有則維持原樣。
    """
    # 用 stack 找未配對的括號
    stack: list[tuple[str, int]] = []  # (bracket_char, index)
    for i, ch in enumerate(text):
        if ch in _OPEN_BRACKETS:
            stack.append((ch, i))
        elif ch in _CLOSE_BRACKETS:
            expected_open = _BRACKET_PAIRS[ch]
            if stack and stack[-1][0] == expected_open:
                stack.pop()
            else:
                stack.append((ch, i))

    # 在 source_line 中定位 text
    pos = source_line.find(text)
    if pos < 0:
        return text  # 無法定位，維持原樣

    result = text
    for bracket, _idx in stack:
        if bracket in _CLOSE_BRACKETS:
            # 有未配對的右括號 → 缺少左括號 → 檢查 text 前一個字元
            if pos > 0 and source_line[pos - 1] == _BRACKET_PAIRS[bracket]:
                result = _BRACKET_PAIRS[bracket] + result
                pos -= 1  # 補上後 result 在 source_line 中的起始位置前移
        else:
            # 有未配對的左括號 → 缺少右括號 → 檢查 text 後一個字元
            end = pos + len(result)
            if end < len(source_line) and source_line[end] == _BRACKET_PAIRS[bracket]:
                result = result + _BRACKET_PAIRS[bracket]

    # 額外處理：整個括號在 text 外（提取時被丟掉）的情況
    # 檢查 source_line 中緊鄰 text 的前/後字元是否為括號，有就補回
    if pos > 0:
        prev = source_line[pos - 1]
        if prev in _OPEN_BRACKETS and not result.startswith(prev):
            result = prev + result
            pos -= 1
    end = pos + len(result)
    if end < len(source_line):
        nxt = source_line[end]
        if nxt in _CLOSE_BRACKETS and not result.endswith(nxt):
            result = result + nxt

    return result


def extract_text(
    source: str,
    base_regex_str: str,
    invalid_regex_str: str,
    symbol_regex_str: str,
    filter_str: str,
    skip_title: str = "",
    author_name: str = "",
) -> dict[str, int]:
    """從原始文本中提取日文文字片段。

    Args:
        skip_title: URL 讀取來源的標題文字；若 source 的第一個非空行內容等於
            此字串，整行跳過（不提取）。空字串表示不套用此規則。
        author_name: 作者名稱；提取結果中若有文字等於此名稱則剔除。
            空字串表示不套用此規則。

    Returns:
        dict[str, int]: {提取文字: 來源行號}，保持插入順序。
    """
    base_regex = _compile_regex(base_regex_str, DEFAULT_BASE_REGEX)
    invalid_regex = _compile_regex(invalid_regex_str, DEFAULT_INVALID_REGEX)
    symbol_regex = _compile_regex(symbol_regex_str, DEFAULT_SYMBOL_REGEX)
    custom_regexes = _compile_custom_filters(filter_str)

    lines = source.split('\n')
    extracted_set: dict[str, int] = {}

    # 定位「第一個非空行」的行號（供 skip_title 比對用）
    title_line_num = 0
    if skip_title.strip():
        target = skip_title.strip()
        for idx, line in enumerate(lines, 1):
            if line.strip():
                if line.strip() == target:
                    title_line_num = idx
                break

    # 作者名稱通常含開頭符號（如「◆Hr94QM5gdI」），而提取後處理會去掉符號
    # 只剩英數字 trip code。因此從作者名稱抽出最長的英數字串（長度 ≥ 6）作為
    # 過濾鍵；找不到則退回原字串完全比對。
    author_target = author_name.strip()
    author_tripcode = ""
    if author_target:
        runs = re.findall(r'[A-Za-z0-9]{6,}', author_target)
        if runs:
            author_tripcode = max(runs, key=len)

    for line_num, line in enumerate(lines, 1):
        if line_num == title_line_num:
            continue
        if _POSTER_LINE_RE.search(line):
            continue
        chunks = re.split(r'[ 　]{2,}', line)

        for chunk in chunks:
            if not chunk.strip():
                continue

            chunk_str = str(chunk)
            symbol_count = len(symbol_regex.findall(chunk_str))
            if symbol_count > len(chunk_str) * 0.5:
                continue

            matches = base_regex.findall(chunk)
            for text in matches:
                text = text.strip()
                if len(text) < 3:
                    continue

                if invalid_regex.match(text):
                    continue

                text = _postprocess_text(text)

                if len(text) <= 2:
                    continue

                if author_target:
                    stripped = text.strip()
                    if stripped == author_target or (
                        author_tripcode and stripped == author_tripcode
                    ):
                        continue

                text = _complete_brackets(text, line)

                # 自訂過濾規則：在提取流程結束後對最終結果做過濾
                if any(reg.search(text) for reg in custom_regexes):
                    continue

                if text and text not in extracted_set:
                    extracted_set[text] = line_num

    return extracted_set


def format_extraction_output(extracted_set: dict[str, int]) -> str:
    """將提取結果格式化為 '001-1|text' 格式。"""
    line_sub_index: dict[int, int] = {}
    output = ""
    for text, ln in extracted_set.items():
        sub = line_sub_index.get(ln, 1)
        line_sub_index[ln] = sub + 1
        output += f"{ln:03d}-{sub}|{text}\n"
    return output


def analyze_extraction(
    text: str,
    base_regex_str: str,
    invalid_regex_str: str,
    symbol_regex_str: str,
    filter_str: str,
) -> str:
    """對輸入文字逐步分析提取流程，回傳分析報告字串。"""
    base_regex = _compile_regex(base_regex_str, DEFAULT_BASE_REGEX)
    invalid_regex = _compile_regex(invalid_regex_str, DEFAULT_INVALID_REGEX)
    symbol_regex = _compile_regex(symbol_regex_str, DEFAULT_SYMBOL_REGEX)
    custom_regexes = _compile_custom_filters(filter_str)

    report: list[str] = []
    lines = text.split('\n')

    for line_idx, line in enumerate(lines, 1):
        if not line.strip():
            continue
        report.append(f"--- 開始分析字串 (第 {line_idx} 行) ---")
        report.append(f"原始字串: '{line}'")

        if _POSTER_LINE_RE.search(line):
            report.append("  -> ❌ 整行剔除：判定為發文者行（含 ID:xxxxxx trip code）。")
            report.append("\n" + "=" * 40 + "\n")
            continue

        chunks = re.split(r'[ 　]{2,}', line)
        report.append(f"[步驟 1] 藉由連續兩個以上空白進行分割，分割出 {len(chunks)} 個區塊:")
        for i, chunk in enumerate(chunks):
            report.append(f"  區塊 {i+1}: '{chunk}'")

        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                report.append(f"\n[區塊 {i+1} 分析] '{chunk}'")
                report.append("  -> ❌ 剔除：區塊為空字串或純空白。")
                continue

            report.append(f"\n[區塊 {i+1} 分析] '{chunk}'")
            chunk_str = str(chunk)

            symbol_count = len(symbol_regex.findall(chunk_str))
            symbol_ratio = symbol_count / len(chunk_str) if len(chunk_str) > 0 else 0
            report.append(
                f"  [步驟 2] 判斷 AA 符號比例 "
                f"(符號數: {symbol_count}, 總字元數: {len(chunk_str)}, 比例: {symbol_ratio:.2f})"
            )

            if symbol_ratio > 0.5:
                report.append("  -> ❌ 剔除：符號比例超過 50%，判定為 AA 圖案。")
                continue

            matches = base_regex.findall(chunk)
            report.append(f"  [步驟 3] 執行 Base Regex 匹配: 找到 {len(matches)} 個可能詞彙")
            if not matches:
                report.append("  -> ❌ 剔除：無法匹配出任何文字。")
                continue

            for j, match_text in enumerate(matches):
                report.append(f"\n  >> 對 [結果 {j+1}] '{match_text}' 進行進階檢驗:")
                t = match_text.strip()

                if len(t) < 3:
                    report.append("    -> ❌ 剔除：去除前後空白後，長度小於 3 字元。")
                    continue
                else:
                    report.append(f"    - 長度檢驗通過 (長度: {len(t)})")

                if invalid_regex.match(t):
                    report.append("    -> ❌ 剔除：全句符合無意義符號組合正則 (Invalid Regex)。")
                    continue
                else:
                    report.append("    - 無意義符號組合檢驗通過")

                original_t = t
                t = _postprocess_text(t)
                report.append(f"    [步驟 4] 後處理解析 (去除非內文字元): '{original_t}' => '{t}'")

                if len(t) <= 2:
                    report.append("    -> ❌ 剔除：經過後處理後，剩餘文字長度 <= 2 字元。")
                    continue
                else:
                    report.append(f"    - 最終長度檢驗通過 (長度: {len(t)})")

                filtered_by_custom = False
                for reg in custom_regexes:
                    if reg.search(t):
                        report.append(f"    -> ❌ 剔除：命中自訂濾網正則表達式 ({reg.pattern})（對最終提取結果過濾）。")
                        filtered_by_custom = True
                        break
                if filtered_by_custom:
                    continue
                else:
                    report.append("    - 自訂過濾清單檢驗通過（對最終提取結果）")

                report.append(f"    -> ✅ 成功提取最終文字: '{t}'")
        report.append("\n" + "=" * 40 + "\n")

    return "\n".join(report)


def validate_ai_text(ai_content: str) -> list[str]:
    """驗證 AI 翻譯結果格式，回傳警告訊息列表（空 = 格式正確）。"""
    if not ai_content.strip():
        return []

    ai_lines = [l for l in ai_content.split('\n') if l.strip()]
    warnings: list[str] = []

    id_pattern = re.compile(r'\d{2,4}-\d+\|')
    multi_id_lines = []
    for i, line in enumerate(ai_lines, 1):
        ids_found = id_pattern.findall(line)
        if len(ids_found) >= 2:
            multi_id_lines.append(str(i))
    if multi_id_lines:
        warnings.append(f"⚠ 第 {','.join(multi_id_lines)} 行含有多個ID")

    return warnings


_KANJI_DIGITS = {
    '〇': 0, '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '百': 100, '千': 1000,
}


def _kanji_to_int(text: str) -> int | None:
    """將漢數字字串轉為整數。支援「八」「十二」「百二十三」等格式。"""
    if not text:
        return None
    # 先嘗試直接轉阿拉伯數字
    if text.isdigit():
        return int(text)

    result = 0
    current = 0
    for ch in text:
        val = _KANJI_DIGITS.get(ch)
        if val is None:
            return None
        if val >= 10:
            # 十百千 — 乘算單位
            if current == 0:
                current = 1
            result += current * val
            current = 0
        else:
            current = val
    result += current
    return result if result > 0 or text == '〇' or text == '零' else None


def check_chapter_number(text_first_lines: str) -> str | None:
    """掃描文字的前幾行，尋找話數格式，回傳章節號碼字串。

    支援格式：第N話、番外編N、その N（阿拉伯數字＋漢數字＋全角數字）。

    Returns:
        章節號碼字串或 None。
    """
    result = get_chapter_display(text_first_lines)
    if result:
        return result[0]
    return None


def get_chapter_display(text_first_lines: str) -> tuple[str, str] | None:
    """掃描文字的前幾行，尋找話數格式。

    Returns:
        (number_str, display_label) 元組，例如 ("8", "第8話")、("3", "番外編3")、
        ("216", "その216")，或 None。
    """
    # 第N話 — 阿拉伯數字
    match = re.search(r'第\s*(\d+)\s*話', text_first_lines)
    if match:
        n = str(int(match.group(1)))
        return (n, f"第{n}話")

    # 第N話 — 漢數字
    match = re.search(r'第\s*([〇零一二三四五六七八九十百千]+)\s*話', text_first_lines)
    if match:
        num = _kanji_to_int(match.group(1))
        if num is not None:
            return (str(num), f"第{num}話")

    # 番外編 N
    match = re.search(r'番外編\s*(\d+)', text_first_lines)
    if not match:
        match = re.search(r'番外編\s*([〇零一二三四五六七八九十百千]+)', text_first_lines)
    if match:
        num = _kanji_to_int(match.group(1))
        if num is not None:
            return (str(num), f"番外編{num}")

    # その N（全角・半角数字）— FC2 Blog タイトル形式
    match = re.search(r'その\s*([０-９\d]+)', text_first_lines)
    if match:
        num_str = match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        n = str(int(num_str))
        return (n, f"その{n}")

    return None


# 已知的站名前綴 — 出現在標題最前方時應被忽略
_SITE_NAME_PREFIXES = [
    '安価でやるお！',
    'やる夫達のいる日常',
    'やる夫まとめくす',
    'やる夫短編集',
]

_SITE_PREFIX_RE = re.compile(
    r'^(?:' + '|'.join(re.escape(s) for s in _SITE_NAME_PREFIXES) + r')\s*'
)


def extract_work_title(title: str) -> str:
    """從頁面標題中去除站名前綴，回傳作品名稱部分。"""
    return _SITE_PREFIX_RE.sub('', title).strip()
