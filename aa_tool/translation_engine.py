import re

from .constants import BORDER_CHARS


def parse_glossary(glossary_str: str) -> dict[str, str]:
    """將 '日文=中文' 格式的術語表字串解析為 dict。"""
    glossary: dict[str, str] = {}
    for line in glossary_str.split('\n'):
        parts = line.split('=', 1)
        if len(parts) == 2 and parts[0].strip():
            glossary[parts[0].strip()] = parts[1].strip()
    return glossary


def _replace_with_padding(
    line: str,
    original: str,
    translated: str,
    padded_translated: str,
) -> str:
    """在一行中執行替換，依邊框字元判斷是否需要補空白。"""
    if original not in line:
        return line
    try:
        pattern = re.compile(re.escape(original))

        def repl(m):
            rest = m.string[m.end():]
            if any(c in rest for c in BORDER_CHARS):
                return padded_translated
            return translated

        return pattern.sub(repl, line)
    except Exception:
        return line.replace(original, translated)


def apply_glossary_to_text(text: str, glossary: dict[str, str]) -> str:
    """對任意文本套用術語表（含 Auto-Padding 與邊框判定）。

    術語依長度遞減排序後逐條替換，避免短詞覆蓋長詞。
    """
    sorted_glossary = sorted(glossary.items(), key=lambda x: len(x[0]), reverse=True)
    lines = text.split('\n')

    for jp_term, tw_term in sorted_glossary:
        len_diff = len(jp_term) - len(tw_term)
        padded_tw = tw_term + ('　' * len_diff if len_diff > 0 else '')

        for i in range(len(lines)):
            lines[i] = _replace_with_padding(lines[i], jp_term, tw_term, padded_tw)

    return '\n'.join(lines)


def apply_translation(
    source: str,
    extracted: str,
    translated: str,
    glossary: dict[str, str],
) -> str:
    """執行翻譯替換：將提取的原文替換為翻譯文，套用術語表並自動補全形空白。

    Args:
        source: 帶有 AA 圖的原始全文
        extracted: 提取結果（'ID|原文' 格式，每行一條）
        translated: AI 翻譯結果（'ID|翻譯文' 格式，每行一條）
        glossary: 術語表 dict

    Returns:
        替換後的完整文本
    """
    # 解析映射表
    orig_map: dict[str, str] = {}
    for line in extracted.split('\n'):
        if '|' in line:
            parts = line.split('|', 1)
            orig_map[parts[0].strip()] = parts[1].strip()

    trans_map: dict[str, str] = {}
    for line in translated.split('\n'):
        if '|' in line:
            parts = line.split('|', 1)
            trans_map[parts[0].strip()] = parts[1].strip()

    # 長度優先排序（最長原文先替換）
    valid_ids = [k for k in trans_map.keys() if k in orig_map]
    valid_ids.sort(key=lambda k: len(orig_map[k]), reverse=True)

    sorted_glossary = sorted(glossary.items(), key=lambda x: len(x[0]), reverse=True)

    # 逐條替換
    for _id in valid_ids:
        trans_text = trans_map[_id]
        original = orig_map[_id]
        final_translated = trans_text

        # 對翻譯文套用術語表
        for jp_term, tw_term in sorted_glossary:
            final_translated = final_translated.replace(jp_term, tw_term)

        len_diff = len(original) - len(final_translated)
        padded = final_translated + ('　' * len_diff if len_diff > 0 else '')

        source_lines = source.split('\n')
        for i in range(len(source_lines)):
            source_lines[i] = _replace_with_padding(
                source_lines[i], original, final_translated, padded
            )
        source = '\n'.join(source_lines)

    # 全域術語覆蓋：未被提取的原文部分也套用術語表
    source = apply_glossary_to_text(source, glossary)

    return source
