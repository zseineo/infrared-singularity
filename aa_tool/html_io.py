import re
import html


def read_html_pre_content(file_path: str) -> str | None:
    """讀取 HTML 檔案，提取 <pre> 區塊內容並 unescape，回傳純文字。"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(r'<pre>([\s\S]*?)</pre>', content, re.IGNORECASE)
    if match:
        return html.unescape(match.group(1))
    return None


def read_html_head(file_path: str) -> str | None:
    """讀取 HTML 檔案的 `<head>...</head>` 原始內容（含 `<head>` 標籤），供
    儲存時沿用。找不到或讀取失敗回傳 None。"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r'<head\b[^>]*>[\s\S]*?</head>', content, re.IGNORECASE)
    if m:
        return m.group(0)
    return None


def read_html_bg_color(file_path: str) -> str | None:
    """讀取 HTML body 的 background-color，回傳 #rrggbb 或 None。"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(
        r'body\s*\{[^}]*background-color\s*:\s*(#[0-9a-fA-F]{3,8}|\w+)',
        content)
    if m:
        return m.group(1)
    return None


_PRESERVED_SPAN_OPEN_RE = (
    r'<span style="(?:color:[^"]*|display:\s*none;?\s*)">'
)


def write_html_file(file_path: str, text_content: str,
                    bg_color: str = "#fff",
                    head_html: str | None = None) -> None:
    """將文字內容包裝為 HTML 並寫入檔案（保留 span 標籤）。

    預設將非白名單內容一律 `html.escape`，避免 AA 圖形中的 `<` `>` 被瀏覽器誤判
    為標籤。白名單僅限特例：上色用 `<span style="color:...">` 與隱藏用
    `<span style="display:none;">`（含少量空白變體）以及共用的 `</span>`。
    """
    parts = re.split(
        r'(' + _PRESERVED_SPAN_OPEN_RE + r'|</span>)', text_content)
    escaped_parts = []
    for part in parts:
        if re.match(_PRESERVED_SPAN_OPEN_RE, part) or part == '</span>':
            escaped_parts.append(part)
        else:
            escaped_parts.append(html.escape(part))
    escaped_content = ''.join(escaped_parts)

    if head_html:
        head_block = head_html
    else:
        head_block = f'''<head>
    <meta charset="UTF-8">
    <title>AA_Translated</title>
    <style>
        body {{ background-color: {bg_color}; color: #000; padding: 20px; }}
        pre {{
            font-family: 'MS PGothic', 'Meiryo', monospace;
            font-size: 16px;
            line-height: 1.2;
            white-space: pre;
            word-wrap: normal;
        }}
    </style>
</head>'''
    html_struct = f'''<!DOCTYPE html>
<html lang="zh-TW">
{head_block}
<body>
<pre>{escaped_content}</pre>
</body>
</html>'''
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(html_struct)
