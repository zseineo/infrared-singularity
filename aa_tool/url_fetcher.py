"""網頁抓取與 HTML 解析 — 純 I/O 與純邏輯，不依賴任何 UI 框架。

支援的網域：
  - 預設格式（article div + relate_dl）
  - himanatokiniyaruo.com（dt[id] / dd 結構 + related-entries）
  - blog.fc2.com（ently_text div + relate_dl，含 web.archive.org 封存版）
  - yaruobook.jp（author-res-dt / author-res 結構 + relatedPostsWrap）
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

def _normalize_color_tags(text: str) -> str:
    """將各種顏色標籤統一為 <span style="color:VALUE"> 格式。

    支援：
    - <span style="...color:VALUE..."> （含混合屬性）
    - <font color="VALUE"> / </font>
    """
    # <font color="..."> → <span style="color:...">
    text = re.sub(
        r'<font\s+color="([^"]+)"[^>]*>',
        lambda m: f'<span style="color:{m.group(1)}">',
        text,
    )
    text = text.replace('</font>', '</span>')

    # <span style="...color:VALUE..."> → <span style="color:VALUE">
    def _repl(m: re.Match) -> str:
        style = m.group(1)
        cm = re.search(r'color\s*:\s*([^;"]+)', style)
        if cm:
            return f'<span style="color:{cm.group(1).strip()}">'
        return ''
    text = re.sub(r'<span\s+style="([^"]*color[^"]*)">', _repl, text)

    return text


_BLACK_SPAN_RE = re.compile(
    r'<span\s+style="color:\s*(?:#0{3,6}|black)\s*">(.*?)</span>',
    re.DOTALL,
)


def _strip_tags_keep_color(text: str) -> str:
    """移除 HTML 標籤，但保留 <span style="color:..."> 和 </span>。

    黑色（#000, #000000, black）的 span 會被移除（只保留內文）。
    """
    text = _normalize_color_tags(text)
    text = _BLACK_SPAN_RE.sub(r'\1', text)
    return re.sub(r'<(?!span\s+style="color:|/span>)[^>]+>', '', text)


def _strip_all_tags(text: str) -> str:
    """移除所有 HTML 標籤（不保留任何 span）。"""
    return re.sub(r'<[^>]+>', '', text)


_OPEN_COLOR_RE = re.compile(r'<span\s+style="color:[^"]*">')
_CLOSE_COLOR_RE = re.compile(r'</span>')


def _cleanup_unmatched_spans(text: str) -> str:
    """移除不配對的 color span 開/閉標籤。

    處理跨 dt/dd 邊界的 span：
    - 開標籤在作者 dd 中，閉標籤在下一個 dt 中被移除 → 孤兒開標籤
    - 閉標籤在 dd 中，開標籤在 dt 中被移除 → 孤兒閉標籤
    """
    opens = list(_OPEN_COLOR_RE.finditer(text))
    closes = list(_CLOSE_COLOR_RE.finditer(text))

    # 從左到右配對，找出孤兒
    events: list[tuple[int, int, str]] = []  # (pos, end, kind)
    for m in opens:
        events.append((m.start(), m.end(), 'open'))
    for m in closes:
        events.append((m.start(), m.end(), 'close'))
    events.sort(key=lambda e: e[0])

    remove_ranges: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = []  # open tag (start, end)

    for pos, end, kind in events:
        if kind == 'open':
            stack.append((pos, end))
        else:
            if stack:
                stack.pop()
            else:
                # 孤兒 </span>
                remove_ranges.append((pos, end))

    # 剩餘未關閉的 <span>
    for pos, end in stack:
        remove_ranges.append((pos, end))

    if not remove_ranges:
        return text

    # 從後往前移除
    result = list(text)
    for start, end in sorted(remove_ranges, reverse=True):
        result[start:end] = []
    return ''.join(result)


# 貼文標頭行 — 用於在非 dt/dd 結構中分割貼文
_POST_HEADER_RE = re.compile(r'^\s*\d+\s*(?:名前|Name)\s*[：:]')
_COLOR_SPAN_RE = re.compile(r'<span\s+style="color:[^"]*">|</span>')

# 從標頭提取投稿者名稱：「N 名前：NAME[...]」→ NAME
_POSTER_NAME_RE = re.compile(r'(?:名前|Name)\s*[：:]\s*(.+?)(?:\[|投稿日|$)')

# 替代格式：「N ： NAME ： YYYY/MM/DD...」→ NAME（yaruobook.jp 等無「名前」關鍵字的站點）
_POSTER_NAME_RE_ALT = re.compile(r'^\s*\d+\s*[：:]\s*(.+?)\s*[：:]\s*\d{4}[/年]')


def _is_author_post(header_text: str, author_name: str) -> bool:
    """判斷貼文標頭是否屬於指定作者。

    依序嘗試兩種標頭格式提取投稿者名稱：
    1.「N 名前：POSTER_NAME[...]」（5ch 類型）
    2.「N ： POSTER_NAME ： YYYY/MM/DD...」（yaruobook.jp 類型）
    名稱比對時會將連續空白正規化為單一空白，避免 HTML 多空白差異造成失配。
    """
    if not author_name:
        return False
    m = _POSTER_NAME_RE.search(header_text)
    if not m:
        m = _POSTER_NAME_RE_ALT.search(header_text)
    if not m:
        return author_name in header_text  # fallback
    poster_name = re.sub(r'\s+', ' ', m.group(1).strip())
    target = re.sub(r'\s+', ' ', author_name.strip())
    return poster_name == target


def _filter_color_by_author(text: str, author_name: str, *, author_only: bool = False) -> str:
    """在無 dt/dd 結構的文字中，僅保留作者貼文的顏色標記。

    以「N 名前：」行作為貼文分界，判斷該貼文是否為指定作者。
    若 author_only=True，則完全跳過非作者的貼文。
    """
    lines = text.split('\n')
    result: list[str] = []
    is_author_block = False

    for line in lines:
        # 先移除 span 標籤再判斷標頭（標頭行可能被 color span 包住）
        clean_line = _COLOR_SPAN_RE.sub('', line)
        if _POST_HEADER_RE.search(clean_line):
            is_author_block = _is_author_post(clean_line, author_name)
            if author_only and not is_author_block:
                continue
            # 標頭行本身不保留顏色
            result.append(clean_line)
        elif is_author_block:
            result.append(line)
        elif not author_only:
            result.append(clean_line)

    return _cleanup_unmatched_spans('\n'.join(result))


def _extract_dt_dd_posts(
    container_html: str,
    author_name: str = "",
    author_only: bool = False,
) -> list[str]:
    """從含有 <dt>/<dd> 的 HTML 片段提取貼文列表。

    回傳格式：['dt文字\n內文行1\n內文行2', ...]

    若指定 author_name，僅保留該作者貼文的顏色標記；
    其他貼文的 HTML 標籤全部移除。
    若 author_only=True 且指定 author_name，則完全跳過非作者貼文。
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

        # 判斷是否為指定作者的貼文
        is_author = _is_author_post(dt_text, author_name)

        # 忽略非作者貼文
        if author_only and author_name and not is_author:
            continue

        dd_content = post.group(2)
        dd_text = re.sub(r'<br\s*/?>', '\n', dd_content)
        if is_author:
            dd_text = _strip_tags_keep_color(dd_text)
            dd_text = _cleanup_unmatched_spans(dd_text)
        else:
            dd_text = _strip_all_tags(dd_text)
        dd_text = html.unescape(dd_text)
        dd_lines = dd_text.split('\n')
        # 只 trim 尾端空行（避免貼文之間多出空白），保留開頭空行 —
        # 作者貼文中「作者行」與實際內容之間的空行屬於排版，必須保留。
        while dd_lines and not dd_lines[-1].strip():
            dd_lines.pop()
        if dd_lines:
            lines_out.append(dt_text + '\n' + '\n'.join(dd_lines))

    return lines_out


# ════════════════════════════════════════════════════════════════
#  解析器：預設格式
# ════════════════════════════════════════════════════════════════

def _parse_default(page_html: str, base_url: str, *, author_name: str = "", author_only: bool = False) -> tuple[str | None, list[dict], str]:
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

    lines_out = _extract_dt_dd_posts(article_html, author_name=author_name, author_only=author_only)
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

def _parse_himanatokiniyaruo(page_html: str, base_url: str, *, author_name: str = "", author_only: bool = False) -> tuple[str | None, list[dict], str]:
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

    lines_out = _extract_dt_dd_posts(content_html, author_name=author_name, author_only=author_only)
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

    # ── 巢狀 div fallback：若原始 regex 未取到連結，改用平衡 div 深度抓取 ──
    if not nav_links:
        start_m = re.search(
            r'<div\s+class="related-entries">', page_html, re.DOTALL,
        )
        if start_m:
            inner_start = start_m.end()
            depth = 1
            pos = inner_start
            while depth > 0 and pos < len(page_html):
                next_open = re.search(r'<div[\s>]', page_html[pos:])
                next_close = re.search(r'</div>', page_html[pos:])
                if next_close is None:
                    break
                if next_open and next_open.start() < next_close.start():
                    depth += 1
                    pos += next_open.start() + 1
                else:
                    depth -= 1
                    if depth == 0:
                        related_html_full = page_html[inner_start:pos + next_close.start()]
                        for a_m in re.finditer(
                            r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
                            related_html_full, re.DOTALL,
                        ):
                            href = urljoin(base_url, a_m.group(1))
                            title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
                            if not title:
                                continue
                            is_current = href.rstrip('/') == base_url.rstrip('/')
                            nav_links.append({'title': title, 'url': href, 'is_current': is_current})
                    pos += next_close.start() + len('</div>')

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：FC2 Blog
# ════════════════════════════════════════════════════════════════

def _parse_fc2blog(page_html: str, base_url: str, *, author_name: str = "", author_only: bool = False) -> tuple[str | None, list[dict], str]:
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
    content_text = _strip_tags_keep_color(content_text)
    content_text = html.unescape(content_text)

    # 依作者名稱過濾顏色：非作者貼文移除 color span（或完全跳過）
    if author_name:
        content_text = _filter_color_by_author(content_text, author_name, author_only=author_only)
    else:
        content_text = re.sub(r'<span\s+style="color:[^"]*">|</span>', '', content_text)

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
#  解析器：yaruobook.jp
# ════════════════════════════════════════════════════════════════

def _parse_yaruobook(page_html: str, base_url: str, *, author_name: str = "", author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析 yaruobook.jp 格式。

    內文：<dt class="author-res-dt"> ... </dt><dd class="author-res"> ... </dd>
    標頭格式：「N ： AUTHOR ： YYYY/MM/DD(曜) HH:MM:SS.SS ID:XXX」
    標題：<title>...</title>
    關聯：<ul class="relatedPostsWrap relatedPostsPrev/Next">，<li class="currentPost"> 標記當前話。
    """
    # ── 標題 ──
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""
    # 去除常見站名後綴
    page_title = re.sub(r'\s*[\|｜\-－–—]\s*やる夫ブック.*$', '', page_title)

    # ── 內文區塊 ──
    first_m = re.search(r'<dt\s+[^>]*class="[^"]*author-res-dt', page_html)
    if not first_m:
        # 備援機制：針對無特定 class 標籤或早期文章，尋找包含「<dt>...數字...：...年份」的標準安價發言
        # 容許數字與冒號之間存在 HTML 標籤 (例如：<span>2175</span> ：)
        first_m = re.search(r'<dt[^>]*>(?:\s|<[^>]+>)*\d+(?:\s|<[^>]+>)*[：:].*?\d{4}[/年]', page_html)
        if not first_m:
            return None, [], page_title

    end_m = re.search(
        r'<div\s+[^>]*class="[^"]*widget-single-content-bottom'
        r'|<ul\s+[^>]*class="[^"]*relatedPostsWrap',
        page_html[first_m.start():],
    )
    content_end = first_m.start() + end_m.start() if end_m else len(page_html)
    content_html = page_html[first_m.start():content_end]

    lines_out = _extract_dt_dd_posts(content_html, author_name=author_name, author_only=author_only)
    text_content = '\n\n'.join(lines_out) if lines_out else None

    # ── 關聯連結 ──
    def _parse_related_ul(ul_html: str) -> list[dict]:
        items: list[dict] = []
        for li in re.finditer(
            r'<li(?:\s+[^>]*class="([^"]*)")?[^>]*>(.*?)</li>',
            ul_html, re.DOTALL,
        ):
            li_class = li.group(1) or ""
            li_inner = li.group(2)
            is_current = 'currentPost' in li_class
            a_m = re.search(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', li_inner, re.DOTALL)
            if a_m:
                href = urljoin(base_url, a_m.group(1))
                title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
                if title:
                    items.append({'title': title, 'url': href, 'is_current': is_current})
            else:
                title = html.unescape(re.sub(r'<[^>]+>', '', li_inner)).strip()
                if title:
                    items.append({'title': title, 'url': None, 'is_current': is_current})
        return items

    nav_links: list[dict] = []
    prev_m = re.search(
        r'<ul\s+[^>]*class="[^"]*relatedPostsWrap\s+relatedPostsPrev[^"]*"[^>]*>(.*?)</ul>',
        page_html, re.DOTALL,
    )
    next_m = re.search(
        r'<ul\s+[^>]*class="[^"]*relatedPostsWrap\s+relatedPostsNext[^"]*"[^>]*>(.*?)</ul>',
        page_html, re.DOTALL,
    )
    # Prev 區塊原始順序為 [currentPost, 前一話, 前前話, ...]（當前最前，舊話倒序）；
    # 反轉後變 [..., 前前話, 前一話, currentPost]，再接 Next 即為時間順序，
    # 讓「下一話」按鈕可正確以 current_idx + 1 取得下一集。
    if prev_m:
        nav_links.extend(reversed(_parse_related_ul(prev_m.group(1))))
    if next_m:
        nav_links.extend(_parse_related_ul(next_m.group(1)))

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  公開入口
# ════════════════════════════════════════════════════════════════

# 網域 → 解析函式的對應表
_DOMAIN_PARSERS: dict[str, callable] = {
    'himanatokiniyaruo.com': _parse_himanatokiniyaruo,
    'blog.fc2.com': _parse_fc2blog,
    'yaruobook.jp': _parse_yaruobook,
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
    *,
    author_name: str = "",
    author_only: bool = False,
) -> tuple[str | None, list[dict], str]:
    """解析頁面 HTML，回傳 (文本內容, 關聯連結列表, 頁面標題)。

    依據 base_url 的網域自動選擇對應的解析器；
    若無匹配則使用預設解析器。
    支援 web.archive.org 封存 URL 的網域識別。

    若指定 author_name，僅保留該作者貼文的顏色標記。
    若 author_only=True 且指定 author_name，則完全排除非作者貼文。

    - 文本內容：提取的純文字，找不到時為 ``None``
    - 關聯連結：``[{'title': ..., 'url': ... or None, 'is_current': bool}, ...]``
    - 頁面標題：清理後的標題字串
    """
    domain = _resolve_domain(base_url)

    for key, parser in _DOMAIN_PARSERS.items():
        if key in domain:
            return parser(page_html, base_url, author_name=author_name, author_only=author_only)

    return _parse_default(page_html, base_url, author_name=author_name, author_only=author_only)
