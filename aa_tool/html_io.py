import base64
import os
import re
import html


def _build_embed_font_face(font_path: str, family: str) -> str | None:
    """讀 TTF 檔，回傳 `@font-face { ... }` CSS 片段（Base64 內嵌 data URI）。

    用於儲存「離線手機可閱讀」的單檔 HTML：把字型完整塞進 HTML，
    瀏覽器不需要連網或載入外部資源即可顯示正確字型。

    讀檔失敗回傳 None；呼叫端應 fallback 為不嵌入。
    """
    try:
        with open(font_path, 'rb') as f:
            raw = f.read()
    except OSError:
        return None
    b64 = base64.b64encode(raw).decode('ascii')
    ext = os.path.splitext(font_path)[1].lower()
    # 標準 RFC 8081 MIME：font/ttf for TTF, font/otf for OTF
    if ext == '.otf':
        mime = 'font/otf'
        fmt = 'opentype'
    else:
        mime = 'font/ttf'
        fmt = 'truetype'
    return (
        f"@font-face {{\n"
        f"            font-family: '{family}';\n"
        f"            src: url(data:{mime};base64,{b64}) format('{fmt}');\n"
        f"        }}"
    )


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
                    head_html: str | None = None,
                    embed_font_path: str | None = None,
                    embed_font_family: str | None = None) -> None:
    """將文字內容包裝為 HTML 並寫入檔案（保留 span 標籤）。

    預設將非白名單內容一律 `html.escape`，避免 AA 圖形中的 `<` `>` 被瀏覽器誤判
    為標籤。白名單僅限特例：上色用 `<span style="color:...">` 與隱藏用
    `<span style="display:none;">`（含少量空白變體）以及共用的 `</span>`。

    Args:
        embed_font_path: 若指定 TTF/OTF 路徑，將字型 Base64 內嵌到 `<head>` 的
            `@font-face`，並把 `pre.font-family` 設為以該字型為首選。
            **此模式會覆寫 head_html**（強制產生新 head 確保字型確實內嵌）。
            產出的單一 HTML 檔不需任何外部資源，下載到手機本地直接打開亦可
            正確顯示，但檔案會增大約「字型檔大小 × 1.33」（Base64 overhead）。
        embed_font_family: 內嵌字型的 CSS family 名稱；未指定時取檔名（無副檔名）。
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

    embed_font_css = ""
    embed_family_for_pre = ""
    if embed_font_path and os.path.exists(embed_font_path):
        family = (embed_font_family
                  or os.path.splitext(os.path.basename(embed_font_path))[0])
        face = _build_embed_font_face(embed_font_path, family)
        if face:
            embed_font_css = f"        {face}\n"
            embed_family_for_pre = f"'{family}', "

    if head_html and not embed_font_css:
        # 沒要內嵌字型且檔上有自訂 head（例：使用者改過 CSS）→ 沿用原 head
        head_block = head_html
    else:
        # 內嵌字型模式：強制重產 head 以確保 @font-face 與 pre.font-family 一致；
        # 一般模式：沿用既有預設樣板。
        head_block = f'''<head>
    <meta charset="UTF-8">
    <title>AA_Translated</title>
    <style>
{embed_font_css}        body {{ background-color: {bg_color}; color: #000; padding: 20px; }}
        pre {{
            font-family: {embed_family_for_pre}'MS PGothic', 'Meiryo', monospace;
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
