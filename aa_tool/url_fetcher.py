"""網頁抓取與 HTML 解析 — 純 I/O 與純邏輯，不依賴任何 UI 框架。"""
from __future__ import annotations

import gzip
import html
import re
import urllib.request
from urllib.parse import urljoin

# ════════════════════════════════════════════════════════════════
#  HTTP 請求
# ════════════════════════════════════════════════════════════════

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Encoding': 'gzip, deflate',
}

_ENCODINGS = ['utf-8', 'cp932', 'euc-jp', 'shift_jis']


def fetch_url(url: str, *, timeout: int = 20) -> str:
    """發送 HTTP GET 請求，回傳解碼後的 HTML 字串。

    自動處理 gzip 壓縮與日文常見編碼（utf-8 → cp932 → euc-jp → shift_jis）。
    """
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw_bytes = resp.read()
        if resp.headers.get('Content-Encoding') == 'gzip':
            page_bytes = gzip.decompress(raw_bytes)
        else:
            page_bytes = raw_bytes

    for enc in _ENCODINGS:
        try:
            return page_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return page_bytes.decode('utf-8', errors='replace')


# ════════════════════════════════════════════════════════════════
#  HTML 解析
# ════════════════════════════════════════════════════════════════

def parse_page_html(
    page_html: str,
    base_url: str,
) -> tuple[str | None, list[dict], str]:
    """解析頁面 HTML，回傳 (文本內容, 關聯連結列表, 頁面標題)。

    - 文本內容：從 ``<div class="article">`` 區塊提取的純文字。
      找不到時回傳 ``(None, [], "")``.
    - 關聯連結：``[{'title': ..., 'url': ... or None, 'is_current': bool}, ...]``
    - 頁面標題：清理後的 ``<title>`` 內容。
    """
    m = re.search(r'<div\s+class="article">', page_html)
    if not m:
        return None, [], ""

    start = m.start()
    # 結束邊界：relate_dl（關聯記事）或第二個 div.article，取較前者
    m_relate = re.search(r'<dl\s+class="relate_dl">', page_html[start + 1:])
    m2 = re.search(r'<div\s+class="article">', page_html[start + 1:])
    candidates = []
    if m_relate:
        candidates.append(start + 1 + m_relate.start())
    if m2:
        candidates.append(start + 1 + m2.start())
    end = min(candidates) if candidates else len(page_html)
    article_html = page_html[start:end]

    posts = re.finditer(
        r'<dt(?:\s[^>]*)?>(.+?)</dt>\s*<dd(?:\s[^>]*)?>(.*?)(?=<dt|</dl>)',
        article_html, re.DOTALL,
    )

    lines_out: list[str] = []
    for post in posts:
        dt_content = post.group(1)
        dt_text = re.sub(r'<[^>]+>', '', dt_content)
        dt_text = html.unescape(dt_text).strip()

        dd_content = post.group(2)
        dd_text = re.sub(r'<br\s*/?>', '\n', dd_content)
        dd_text = re.sub(r'<[^>]+>', '', dd_text)
        dd_text = html.unescape(dd_text)
        dd_lines = dd_text.split('\n')
        while dd_lines and not dd_lines[-1].strip():
            dd_lines.pop()
        while dd_lines and not dd_lines[0].strip():
            dd_lines.pop(0)
        if dd_lines:
            lines_out.append(dt_text + '\n' + '\n'.join(dd_lines))

    text_content = '\n\n'.join(lines_out)

    # 頁面標題
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""
    page_title = re.sub(r'^.*?まとめ\S*\s+', '', page_title)

    # 關聯連結
    nav_links: list[dict] = []
    relate_m = re.search(r'<dl\s+class="relate_dl">(.*?)</dl>', page_html, re.DOTALL)
    if relate_m:
        relate_html = relate_m.group(1)
        for li in re.finditer(
            r'<li\s+class="(relate_li(?:_nolink)?)"[^>]*>(.*?)</li>',
            relate_html, re.DOTALL,
        ):
            li_class = li.group(1)
            li_inner = li.group(2)
            if li_class == 'relate_li':
                a_m = re.search(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', li_inner)
                if a_m:
                    href = urljoin(base_url, a_m.group(1))
                    title = html.unescape(a_m.group(2)).strip()
                    nav_links.append({'title': title, 'url': href, 'is_current': False})
            else:
                title = html.unescape(re.sub(r'<[^>]+>', '', li_inner)).strip()
                nav_links.append({'title': title, 'url': None, 'is_current': True})
        nav_links.reverse()

    return text_content, nav_links, page_title
