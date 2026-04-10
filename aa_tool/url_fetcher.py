"""網頁抓取與 HTML 解析 — 純 I/O 與純邏輯，不依賴任何 UI 框架。

支援的網域：
  - 預設格式（article div + relate_dl）
  - himanatokiniyaruo.com（dt[id] / dd 結構 + related-entries）
  - blog.fc2.com（ently_text div + relate_dl，含 web.archive.org 封存版）
"""
from __future__ import annotations

import gzip
import html
import re
import urllib.request
from urllib.parse import urljoin, urlparse

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
#  共用輔助
# ════════════════════════════════════════════════════════════════

def _extract_dt_dd_posts(container_html: str) -> list[str]:
    """從含有 <dt>/<dd> 的 HTML 片段提取貼文列表。

    回傳格式：['dt文字\n內文行1\n內文行2', ...]
    """
    posts = re.finditer(
        r'<dt(?:\s[^>]*)?>(.+?)</dt>\s*<dd(?:\s[^>]*)?>(.*?)(?=<dt|</dl>|$)',
        container_html, re.DOTALL,
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

    return lines_out


# ════════════════════════════════════════════════════════════════
#  解析器：預設格式
# ════════════════════════════════════════════════════════════════

def _parse_default(page_html: str, base_url: str) -> tuple[str | None, list[dict], str]:
    """解析預設格式：<div class="article"> + <dl class="relate_dl">。"""
    m = re.search(r'<div\s+class="article">', page_html)
    if not m:
        return None, [], ""

    start = m.start()
    m_relate = re.search(r'<dl\s+class="relate_dl">', page_html[start + 1:])
    m2 = re.search(r'<div\s+class="article">', page_html[start + 1:])
    candidates = []
    if m_relate:
        candidates.append(start + 1 + m_relate.start())
    if m2:
        candidates.append(start + 1 + m2.start())
    end = min(candidates) if candidates else len(page_html)
    article_html = page_html[start:end]

    lines_out = _extract_dt_dd_posts(article_html)
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

    return text_content or None, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：himanatokiniyaruo.com
# ════════════════════════════════════════════════════════════════

def _parse_himanatokiniyaruo(page_html: str, base_url: str) -> tuple[str | None, list[dict], str]:
    """解析 himanatokiniyaruo.com 格式。

    內文：<dt id="N"> ... </dt><dd> ... </dd> 結構
    標題：<h2><a class="kjax" ...>TITLE</a></h2>
    關聯：<div class="related-entries"> ... </div>
    """
    # ── 標題 ──
    title_m = re.search(
        r'<h2[^>]*>\s*<a[^>]+class="kjax"[^>]*>([^<]+)</a>\s*</h2>',
        page_html,
    )
    if title_m:
        page_title = html.unescape(title_m.group(1)).strip()
    else:
        # fallback: <title> タグ
        t_m = re.search(r'<title>([^<]+)</title>', page_html)
        page_title = html.unescape(t_m.group(1)).strip() if t_m else ""

    # ── 內文：找第一個 <dt id="數字"> 的所在區段 ──
    first_dt = re.search(r'<dt\s+id="\d+"', page_html)
    if not first_dt:
        return None, [], page_title

    # 往前找最近的開標籤容器（最多往前 2000 字元）
    search_start = max(0, first_dt.start() - 2000)
    prefix = page_html[search_start:first_dt.start()]

    # 找內文區塊結束點：related div 或 related-entries
    end_m = re.search(
        r'<div\s+class="related[^"]*"',
        page_html[first_dt.start():],
    )
    content_end = first_dt.start() + end_m.start() if end_m else len(page_html)
    content_html = page_html[first_dt.start():content_end]

    lines_out = _extract_dt_dd_posts(content_html)
    text_content = '\n\n'.join(lines_out) if lines_out else None

    # ── 關聯連結 ──
    nav_links: list[dict] = []
    related_m = re.search(
        r'<div\s+class="related-entries">(.*?)</div>',
        page_html, re.DOTALL,
    )
    if related_m:
        related_html = related_m.group(1)
        for a_m in re.finditer(
            r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
            related_html, re.DOTALL,
        ):
            href = urljoin(base_url, a_m.group(1))
            title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
            if not title:
                continue
            is_current = href.rstrip('/') == base_url.rstrip('/')
            nav_links.append({'title': title, 'url': href, 'is_current': is_current})

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：FC2 Blog
# ════════════════════════════════════════════════════════════════

def _parse_fc2blog(page_html: str, base_url: str) -> tuple[str | None, list[dict], str]:
    """解析 FC2 Blog 格式（含 web.archive.org 封存版）。

    內文：<div class="ently_text">，截止於 fc2button-clap div 或 relate_dl。
    標題：<title>...</title>
    關聯：<dl class="relate_dl ..."> 中的 <li class="relate_li">
    """
    # ── 標題 ──
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""

    # ── 內文 ──
    ently_m = re.search(r'<div\s+class="ently_text">', page_html)
    if not ently_m:
        return None, [], page_title

    content_start = ently_m.end()
    end_m = re.search(
        r'<div\s+class="fc2button-clap[^"]*"|<dl\s+class="relate_dl|<!--/ently_text-->',
        page_html[content_start:],
    )
    content_end = content_start + end_m.start() if end_m else len(page_html)
    content_html = page_html[content_start:content_end]

    # 移除 <a> 連結整段（含導覽文字如 Next/Back/前往目錄）
    content_html = re.sub(r'<a\s[^>]*>.*?</a>', '', content_html, flags=re.DOTALL)
    content_text = re.sub(r'<br\s*/?>', '\n', content_html)
    content_text = re.sub(r'<[^>]+>', '', content_text)
    content_text = html.unescape(content_text)

    lines = content_text.split('\n')
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text_content = '\n'.join(lines) if lines else None

    # ── 關聯連結 ──
    nav_links: list[dict] = []
    relate_m = re.search(r'<dl\s+class="relate_dl[^"]*">(.*?)</dl>', page_html, re.DOTALL)
    if relate_m:
        relate_html = relate_m.group(1)
        for li in re.finditer(
            r'<li\s+class="(relate_li(?:_nolink)?)"[^>]*>(.*?)</li>',
            relate_html, re.DOTALL,
        ):
            li_class = li.group(1)
            li_inner = li.group(2)
            if li_class == 'relate_li':
                a_m = re.search(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', li_inner, re.DOTALL)
                if a_m:
                    href = urljoin(base_url, a_m.group(1))
                    title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
                    nav_links.append({'title': title, 'url': href, 'is_current': False})
            else:
                title = html.unescape(re.sub(r'<[^>]+>', '', li_inner)).strip()
                nav_links.append({'title': title, 'url': None, 'is_current': True})
        nav_links.reverse()

    return text_content or None, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  公開入口
# ════════════════════════════════════════════════════════════════

# 網域 → 解析函式的對應表
_DOMAIN_PARSERS: dict[str, callable] = {
    'himanatokiniyaruo.com': _parse_himanatokiniyaruo,
    'blog.fc2.com': _parse_fc2blog,
}


def _resolve_domain(base_url: str) -> str:
    """從 URL 提取有效網域（自動處理 web.archive.org 封存 URL）。"""
    domain = urlparse(base_url).netloc.lower()
    domain = re.sub(r'^www\.', '', domain)

    # web.archive.org 封存網址 → 從路徑提取原始網域
    if domain == 'web.archive.org':
        orig_m = re.search(r'/web/\d+/(https?://[^/\s]+)', base_url)
        if orig_m:
            orig_domain = urlparse(orig_m.group(1)).netloc.lower()
            return re.sub(r'^www\.', '', orig_domain)

    return domain


def parse_page_html(
    page_html: str,
    base_url: str,
) -> tuple[str | None, list[dict], str]:
    """解析頁面 HTML，回傳 (文本內容, 關聯連結列表, 頁面標題)。

    依據 base_url 的網域自動選擇對應的解析器；
    若無匹配則使用預設解析器。
    支援 web.archive.org 封存 URL 的網域識別。

    - 文本內容：提取的純文字，找不到時為 ``None``
    - 關聯連結：``[{'title': ..., 'url': ... or None, 'is_current': bool}, ...]``
    - 頁面標題：清理後的標題字串
    """
    domain = _resolve_domain(base_url)

    for key, parser in _DOMAIN_PARSERS.items():
        if key in domain:
            return parser(page_html, base_url)

    return _parse_default(page_html, base_url)
