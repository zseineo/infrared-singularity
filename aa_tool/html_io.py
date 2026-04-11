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


def write_html_file(file_path: str, text_content: str) -> None:
    """將文字內容包裝為 HTML 並寫入檔案（保留 span 標籤）。"""
    parts = re.split(r'(<span style="color:[^"]*">|</span>)', text_content)
    escaped_parts = []
    for part in parts:
        if re.match(r'<span style="color:[^"]*">', part) or part == '</span>':
            escaped_parts.append(part)
        else:
            escaped_parts.append(html.escape(part))
    escaped_content = ''.join(escaped_parts)

    html_struct = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AA_Translated</title>
    <style>
        body {{
            background-color: #fff;
            color: #000;
            padding: 10px;
            margin: 0;
        }}
        pre {{
            font-family: 'MS PGothic', 'Meiryo', 'Hiragino Kaku Gothic Pro', monospace;
            font-size: 16px;
            line-height: 1.2;
            white-space: pre;
            word-wrap: normal;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        @media (max-width: 768px) {{
            body {{ padding: 5px; }}
            pre {{
                font-size: 10px;
                line-height: 1.15;
            }}
        }}
    </style>
</head>
<body>
<pre>{escaped_content}</pre>
</body>
</html>'''
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(html_struct)
