"""網頁抓取與 HTML 解析 — 純 I/O 與純邏輯，不依賴任何 UI 框架。

支援的網域：
  - 預設格式（article div + relate_dl）
  - himanatokiniyaruo.com（dt / dd 結構 + related-entries；舊 <dt id="N"> 與新 <dt><span>N</span> 兩種模板）
  - blog.fc2.com（ently_text / entry_text div + relate_dl，含 web.archive.org 封存版、res_h/res_b 變體、
    yarucha 類 <a name/num> + <dd> 無容器模板）
  - blog105.fc2.com（textar-aa div + relate_dl，FC2 舊版模板，含不帶引號 font color）
  - blog136.fc2.com（entryblock / AA div + relate_dl，FC2 舊版模板）
  - yaruobook.jp（author-res-dt / author-res 結構 + relatedPostsWrap）
  - yaruohiroba.com（dt/dd 結構 + related-list 關聯清單）
  - yaruobook.net / yaruobook.com（entry-content div + dt/dd + relatedPostsWrap，早期文章含 HTML 數字字元引用）
  - yaruo-matome.com（entry-content div + nexe-prev-post ul）
  - blog.livedoor.jp（textar-aa / span.aa，無關聯記事、尾端嵌入下一話連結）
  - asitayaruo.com（entry-content + dt/dd；本身無 prev/next，關聯記事改由分類頁 ?cat=N&paged=K 取得）

顏色標籤一律輸出為 `<span style="color:#rrggbb">`；來源的 `rgb(R, G, B)` 形式
（yaruobook.com / .jp、himanatokiniyaruo.com 等）會正規化為 `#rrggbb`，
標籤內不留空白與逗號（見 `_canon_color_value`）。
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

# euc_jis_2004 為 euc-jp 的超集，涵蓋 JIS X 0213 機種依存文字（如 Ⅵ、①），
# 置於 euc-jp 之前；標準 euc-jp 頁面以它解碼結果一致。
_ENCODINGS = ['utf-8', 'cp932', 'euc_jis_2004', 'euc-jp', 'shift_jis']

_META_CHARSET_RE = re.compile(rb'charset=["\']?([\w-]+)', re.IGNORECASE)


def _decode_bytes(page_bytes: bytes, declared_charset: str | None = None) -> str:
    """將位元組解碼為字串，優先採用宣告的編碼。

    候選順序：HTTP header 宣告 → HTML <meta charset> → 常見日文編碼清單。
    先逐一嘗試嚴格解碼；全部失敗時（頁面含少量非法位元組）改以**第一順位
    候選**（通常為 HTTP header 宣告的編碼）寬鬆解碼——可正確處理「宣告
    euc-jp 但夾雜壞位元組」的 livedoor 等頁面（嚴格解 euc-jp 會因 1~2 個壞
    位元組失敗，若不指定編碼則所有編碼皆失敗而退回 utf-8 亂碼）。
    寬鬆解碼不採「替代字元最少」啟發式：cp932 等寬鬆 codec 會把錯誤位元組
    解成半形片假名而非 U+FFFD，反而騙過該啟發式，故一律信任宣告編碼。
    """
    head = page_bytes[:2048]
    meta_m = _META_CHARSET_RE.search(head)
    meta_charset = meta_m.group(1).decode('ascii', 'ignore').lower() if meta_m else None

    candidates: list[str] = []
    for c in (declared_charset, meta_charset):
        if c and c.lower() not in candidates:
            candidates.append(c.lower())
    for c in _ENCODINGS:
        if c not in candidates:
            candidates.append(c)

    for enc in candidates:
        try:
            return page_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # 全部嚴格失敗 → 以第一順位候選寬鬆解碼
    for enc in candidates:
        try:
            return page_bytes.decode(enc, errors='replace')
        except LookupError:
            continue
    return page_bytes.decode('utf-8', errors='replace')


def _fetch_raw(url: str, *, timeout: int,
               extra_headers: dict | None = None) -> tuple[bytes, str | None]:
    """發送請求，回傳 (解壓後的位元組, HTTP header 宣告的 charset 或 None)。"""
    headers = dict(_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        data = gzip.decompress(raw) if resp.headers.get('Content-Encoding') == 'gzip' else raw
        cm = re.search(r'charset=([\w-]+)', resp.headers.get('Content-Type', ''), re.IGNORECASE)
        return data, (cm.group(1) if cm else None)


def fetch_url(url: str, *, timeout: int = 20) -> str:
    """發送 HTTP GET 請求，回傳解碼後的 HTML 字串。

    自動處理 gzip 壓縮與日文常見編碼（優先採用 HTTP header／<meta> 宣告的
    charset，再退回 utf-8 → cp932 → euc_jis_2004 → euc-jp → shift_jis）。
    FC2 R-18 年齡閘門（`class="age_verify_box_form"`）偵測到時，
    自動帶 Cookie: age_check=1 重試以取得真實內容。
    """
    raw, charset = _fetch_raw(url, timeout=timeout)
    page_html = _decode_bytes(raw, charset)

    if 'age_verify_box_form' in page_html:
        raw, charset = _fetch_raw(url, timeout=timeout, extra_headers={'Cookie': 'age_check=1'})
        page_html = _decode_bytes(raw, charset)

    return page_html


# ════════════════════════════════════════════════════════════════
#  共用輔助
# ════════════════════════════════════════════════════════════════

# `rgb(R, G, B)` / `rgba(R, G, B, A)` 形式的顏色值（大小寫不拘、容許空白）
_RGB_FUNC_RE = re.compile(
    r'rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*'
    r'(?:,\s*[\d.]+\s*)?\)',
    re.IGNORECASE,
)


def _canon_color_value(value: str) -> str:
    """把 `rgb()/rgba()` 形式的顏色值正規化為 `#rrggbb`。

    部分站台（例：yaruobook.com）的 inline style 用 `color: rgb(255, 0, 0)`。
    這種寫法夾帶空白與逗號，與工具其他地方一律使用的 `#rrggbb` 不一致，會讓
    下游把標籤內的 `color`、`(255,` 等片段誤認成可翻譯文字。統一成 `#rrggbb`
    後標籤內不再有空白與逗號。非 rgb 形式（`#ff0000`、`red` 等）原樣回傳。
    """
    value = value.strip()
    m = _RGB_FUNC_RE.fullmatch(value)
    if not m:
        return value
    r, g, b = (min(255, int(x)) for x in m.groups())
    return f'#{r:02x}{g:02x}{b:02x}'


def _normalize_color_tags(text: str) -> str:
    """將各種顏色標籤統一為 <span style="color:VALUE"> 格式。

    支援：
    - <span style="...color:VALUE..."> （含混合屬性）
    - <font color="VALUE"> / </font>

    顏色值一律經 `_canon_color_value` 正規化（`rgb()` → `#rrggbb`）。
    """
    # <font color="VALUE"> / <font color=VALUE> → <span style="color:VALUE">
    # 支援帶引號與不帶引號兩種格式（舊式 FC2 模板如 blog105.fc2.com 使用不帶引號）
    text = re.sub(
        r'<font\s+color=(?:"([^"]+)"|([^"\s>]+))[^>]*>',
        lambda m: (f'<span style="color:'
                   f'{_canon_color_value(m.group(1) or m.group(2))}">'),
        text,
    )
    text = text.replace('</font>', '</span>')

    # <span style="...color:VALUE..."> → <span style="color:VALUE">
    def _repl(m: re.Match) -> str:
        style = m.group(1)
        cm = re.search(r'color\s*:\s*([^;"]+)', style)
        if cm:
            return f'<span style="color:{_canon_color_value(cm.group(1))}">'
        return ''
    text = re.sub(r'<span\s+style="([^"]*color[^"]*)">', _repl, text)

    return text


_BLACK_SPAN_RE = re.compile(
    r'<span\s+style="color:\s*(?:#0{3,6}|black)\s*">(.*?)</span>',
    re.DOTALL,
)

# 雙向文字控制字元（LRM/RLM、LRE~RLO、LRI~PDI）。部分站點（例：
# yaruobook.com）會在每行 AA 外包 <U+202D>…<U+202C> 以強制左到右排版，
# 這些字元不屬 AA 內容、為不可見控制碼，抽取時一律移除。
_BIDI_RE = re.compile('[\u200e\u200f\u202a-\u202e\u2066-\u2069]')


def _strip_tags_keep_color(text: str) -> str:
    """移除 HTML 標籤，但保留 <span style="color:..."> 和 </span>。

    黑色（#000, #000000, black）的 span 會被移除（只保留內文）。
    同時移除雙向文字控制字元（非 AA 內容的不可見控制碼）。
    """
    text = _normalize_color_tags(text)
    text = _BLACK_SPAN_RE.sub(r'\1', text)
    text = re.sub(r'<(?!span\s+style="color:|/span>)[^>]+>', '', text)
    return _BIDI_RE.sub('', text)


def _strip_all_tags(text: str) -> str:
    """移除所有 HTML 標籤（不保留任何 span）與雙向文字控制字元。"""
    return _BIDI_RE.sub('', re.sub(r'<[^>]+>', '', text))


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
# 支援四種格式：
#   1.「N 名前：NAME[...]」(5ch / FC2 / himana)
#   2.「N ： NAME ： YYYY/MM/DD(...)」(yaruobook / yaruok 等 FC2 變體)
#   3.「N ： NAME  YYYY/MM/DD(...)」(yaruyarach.blog.fc2.com 等 — 名稱與日期間僅空白)
#   4.「N NAME DATE HH:MM:SS ID:XXX」(yaranaioblog.blog.fc2.com 等 — 數字後無 `：`/`名前`)
# 第 2、3 種共用 regex：名稱與日期之間的分隔符 `(?:[：:]|\[[^\]]*\])` 為可選。
# 第 4 種無 `：` 錨點，故以「日期＋時間＋ID:」三段連續結構錨定以降低誤判
# （日期容許 2~4 位年份、`(曜)` 可選；時間 HH:MM:SS、`.ms` 可選）。
_POST_HEADER_RE = re.compile(
    r'^\s*\d+\s*(?:'
    r'(?:名前|Name)\s*[：:]'
    r'|[：:]\s*.+?\s*(?:[：:]|\[[^\]]*\])?\s*\d{4}[/年]'
    r'|\s+\S[^\n]*?\s+\d{1,4}/\d{1,2}/\d{1,2}(?:\([^)]*\))?\s+'
    r'\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s+ID[：:]'
    r')'
)
_COLOR_SPAN_RE = re.compile(r'<span\s+style="color:[^"]*">|</span>')

# 從標頭提取投稿者名稱：「N 名前：NAME[...]」→ NAME
_POSTER_NAME_RE = re.compile(r'(?:名前|Name)\s*[：:]\s*(.+?)(?:\[|投稿日|$)')

# 替代格式 → NAME。名稱與日期之間的分隔符為 `：` / `[...]`（mail 欄，如
# `[sage]`）/ 空白的**任意連續組合**（zero 個以上）：
#   「N ： NAME ： YYYY/MM/DD...」 (yaruobook.jp，冒號分隔)
#   「N : NAME [] YYYY/MM/DD...」 (yarucha，方括號分隔)
#   「N ： NAME  YYYY/MM/DD...」 (yaruyarach，僅空白分隔)
#   「N ： NAME [saga]：YYYY/MM/DD...」 (oreyaruoavalon.blog.2nt.com，mail 欄＋冒號)
# 若僅允許單一可選分隔符，mail 欄會被併入名稱，使同作者因 mail 欄不同
# （[saga] ↔ [sage]）而比對失配、在「忽略留言」模式下被誤跳過。
_POSTER_NAME_RE_ALT = re.compile(
    r'^\s*\d+\s*[：:]\s*(.+?)\s*(?:(?:[：:]|\[[^\]]*\])\s*)*\d{4}[/年]'
)

# 第 4 種格式（數字後無 `：`，名稱與日期僅以空白分隔）→ NAME
_POSTER_NAME_RE_ALT2 = re.compile(
    r'^\s*\d+\s+(\S[^\n]*?)\s+\d{1,4}/\d{1,2}/\d{1,2}(?:\([^)]*\))?\s+'
    r'\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s+ID[：:]'
)


def _extract_poster_name(header_text: str) -> str:
    """從標頭抽出投稿者名稱（三種格式依序嘗試，失敗回傳空字串）。"""
    m = _POSTER_NAME_RE.search(header_text)
    if not m:
        m = _POSTER_NAME_RE_ALT.search(header_text)
    if not m:
        m = _POSTER_NAME_RE_ALT2.search(header_text)
    if not m:
        return ""
    return re.sub(r'\s+', ' ', m.group(1).strip())


def _detect_main_author_from_dt(container_html: str) -> str:
    """掃描 dt 區塊的投稿標頭，回傳第一個非「名無」的作者名稱。

    多數網站的留言者 ID 都含「名無」（名無しさん），自動偵測作者時應略過
    這類匿名名稱、往下一樓尋找真正的作者；若整串貼文皆為匿名，退回第一個
    出現的名稱。此略過僅在自動偵測（未指定 author_name）時生效。
    """
    first = ""
    for post in re.finditer(r'<dt(?:\s[^>]*)?>(.+?)</dt>', container_html, re.DOTALL):
        dt_text = re.sub(r'<[^>]+>', '', post.group(1))
        dt_text = html.unescape(dt_text).strip()
        name = _extract_poster_name(dt_text)
        if name:
            if '名無' not in name:
                return name
            if not first:
                first = name
    return first


def _detect_main_author_from_lines(text_content: str) -> str:
    """掃描以「N 名前：…」行分段的內文，回傳第一個非「名無」的作者名稱。

    略過含「名無」的匿名留言者，往下一樓尋找；皆為匿名時退回第一個名稱。
    """
    first = ""
    for line in text_content.split('\n'):
        clean_line = _COLOR_SPAN_RE.sub('', line)
        if _POST_HEADER_RE.search(clean_line):
            name = _extract_poster_name(clean_line)
            if name:
                if '名無' not in name:
                    return name
                if not first:
                    first = name
    return first


_NAME_TRAIL_RE = re.compile(r'[\s.．。・,，、]+$')


def _is_author_post(header_text: str, author_name: str) -> bool:
    """判斷貼文標頭是否屬於指定作者。

    依序嘗試兩種標頭格式提取投稿者名稱：
    1.「N 名前：POSTER_NAME[...]」（5ch 類型）
    2.「N ： POSTER_NAME ： YYYY/MM/DD...」（yaruobook.jp 類型）
    名稱比對時會將連續空白正規化為單一空白，避免 HTML 多空白差異造成失配；
    並會去除名稱尾端常見標點（`.`/`。`/`、` 等），以容許作者在不同貼文偶爾在
    名稱後加 dot 的情況（如 yaruobookshelf 上 `三流 ◆WiAEg3iQI` ↔
    `三流 ◆WiAEg3iQI.`，trip 碼與 ID 相同確認為同一人）。
    """
    if not author_name:
        return False
    poster_name = _extract_poster_name(header_text)
    if not poster_name:
        return author_name in header_text  # fallback
    target = re.sub(r'\s+', ' ', author_name.strip())
    if poster_name == target:
        return True
    return _NAME_TRAIL_RE.sub('', poster_name) == _NAME_TRAIL_RE.sub('', target)


def _filter_color_by_author(text: str, author_name: str, *, author_only: bool = False) -> str:
    """在無 dt/dd 結構的文字中，僅保留作者貼文的顏色標記。

    以「N 名前：」行作為貼文分界，判斷該貼文是否為指定作者。
    若 author_only=True，則完全跳過非作者的貼文。
    若 author_only=True 但 author_name 為空，會自動以出現次數最多的作者為準。
    """
    if author_only and not author_name:
        author_name = _detect_main_author_from_lines(text)

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
    若 author_only=True 但未指定 author_name，會自動以出現次數最多的作者為準。
    """
    if author_only and not author_name:
        author_name = _detect_main_author_from_dt(container_html)

    posts = re.finditer(
        r'<dt(?:\s[^>]*)?>(.+?)</dt>\s*<dd(?:\s[^>]*)?>(.*?)(?=<dt|</dl>|</dd>|$)',
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
        # 來源 HTML 的字面換行屬排版（瀏覽器在一般流中折疊為空白），AA 真正
        # 的換行一律由 <br> 表示。先把 \r\n 折成單一空白，避免長 AA 行在
        # 來源換行處被誤斷成兩行（例：yaruobook.com 來源把過長行折行）。
        dd_content = re.sub(r'[\r\n]+', ' ', dd_content)
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


_NAV_DATE_RE = re.compile(r'\((\d{4})/(\d{1,2})/(\d{1,2})\)')


def _is_nav_ascending_by_date(items: list[dict]) -> bool:
    """偵測關聯清單是否已是「舊 → 新」時間順序。

    從每個 item 標題尾端的 `(YYYY/MM/DD)` 抽日期，比較相鄰配對：
    遞增多於遞減即視為已是時間順序。無日期或全相同則回傳 False
    （退回呼叫端既有的 reverse 預設行為）。
    """
    dates: list[tuple[int, int, int]] = []
    for it in items:
        m = _NAV_DATE_RE.search(it.get('title', ''))
        if m:
            dates.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if len(dates) < 2:
        return False
    asc = sum(1 for i in range(len(dates) - 1) if dates[i] < dates[i + 1])
    desc = sum(1 for i in range(len(dates) - 1) if dates[i] > dates[i + 1])
    return asc > desc


# ════════════════════════════════════════════════════════════════
#  解析器：預設格式
# ════════════════════════════════════════════════════════════════

def _parse_default(page_html: str, base_url: str, *, author_name: str = "", author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析預設格式：<div class="article"> + <dl class="relate_dl">。"""
    articles = list(re.finditer(r'<div\s+class="article">', page_html))
    if not articles:
        return None, [], ""

    # relate_dl 之後的 <div class="article"> 屬留言／引用區塊，非內文 → 排除
    m_relate = re.search(r'<dl\s+class="relate_dl[^"]*">', page_html)
    relate_pos = m_relate.start() if m_relate else len(page_html)
    content_articles = [a for a in articles if a.start() < relate_pos] or articles

    def _extract_article(article_html: str) -> str:
        # dt/dd 結構
        lines_out = _extract_dt_dd_posts(
            article_html, author_name=author_name, author_only=author_only)
        if lines_out:
            return '\n\n'.join(lines_out)
        # ── 平面 <span class="aa"> 變體 fallback（無 dt/dd 結構）──
        # 例：burakio002.blog97.fc2.com — 內文塞在多個 <span class="aa"> 內，
        # 以 <br> 分行、貼文之間以 <hr> + 新的 span 分隔；標頭「N 名前：…」
        # 直接寫在內文中。
        flat = article_html
        flat = re.sub(r'<script\b.*?</script>', '', flat, flags=re.DOTALL | re.IGNORECASE)
        flat = re.sub(r'<table\b.*?</table>', '', flat, flags=re.DOTALL | re.IGNORECASE)
        flat = re.sub(r'<a\s[^>]*>.*?</a>', '', flat, flags=re.DOTALL | re.IGNORECASE)
        flat = re.sub(r'<hr\b[^>]*/?>', '\n', flat, flags=re.IGNORECASE)
        flat = re.sub(r'<br\s*/?>', '\n', flat)
        flat = _strip_tags_keep_color(flat)
        flat = html.unescape(flat)
        if author_name or author_only:
            flat = _filter_color_by_author(flat, author_name, author_only=author_only)
        else:
            flat = re.sub(r'<span\s+style="color:[^"]*">|</span>', '', flat)
        # 多個 span/hr 邊界會堆出 3+ 連續換行，壓回最多一行空白
        flat = re.sub(r'\n{3,}', '\n\n', flat)
        out_lines = flat.split('\n')
        while out_lines and not out_lines[0].strip():
            out_lines.pop(0)
        while out_lines and not out_lines[-1].strip():
            out_lines.pop()
        # 安全閥：確認存在貼文標頭行才採用，避免把 cruft 當內文
        if out_lines and any(_POST_HEADER_RE.search(_COLOR_SPAN_RE.sub('', ln))
                             for ln in out_lines):
            return '\n'.join(out_lines)
        return ""

    # 逐一掃描 article 容器：首個常為 iframe 廣告等 cruft（例：
    # oreyaruoavalon.blog.2nt.com 在內文前另有一個 <div class="article">
    # 廣告區塊），取第一個能抽出內文者。
    text_content = ""
    for i, a in enumerate(content_articles):
        end = relate_pos
        if i + 1 < len(content_articles):
            end = min(end, content_articles[i + 1].start())
        text_content = _extract_article(page_html[a.start():end])
        if text_content:
            break

    # 頁面標題
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""
    page_title = re.sub(r'^.*?まとめ\S*\s+', '', page_title)

    # 關聯連結（允許 class 帶額外字樣，如 "relate_dl fc2relate_entry_thumbnail_off"）
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

    內文：<dt ...> ... </dt><dd> ... </dd> 結構
          （舊模板 <dt id="N">；新模板 <dt><span>N</span> ： 作者 ： …）
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

    # ── 內文：找第一個 <dt ...> 的所在區段 ──
    # 舊模板用 <dt id="數字">，新模板改為 <dt><span>N</span> ： … 結構，
    # 兩者都以泛用 <dt> 錨定（頁面僅單一 <dl>，無留言區 dt/dd 干擾）。
    first_dt = re.search(r'<dt(?:\s[^>]*)?>', page_html)
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
    # 以 <br /> 分段：有 <a> 的段落 → 連結條目；純文字段落 → 當前話（無連結）
    def _parse_related_segments(fragment: str) -> list[dict]:
        items: list[dict] = []
        for seg in re.split(r'<br\s*/?>', fragment):
            a_m = re.search(r'<a\s[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', seg, re.DOTALL)
            if a_m:
                href = urljoin(base_url, a_m.group(1))
                title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
                if not title:
                    continue
                is_current = href.rstrip('/') == base_url.rstrip('/')
                items.append({'title': title, 'url': href, 'is_current': is_current})
            else:
                title = html.unescape(re.sub(r'<[^>]+>', '', seg)).strip()
                if title:
                    items.append({'title': title, 'url': None, 'is_current': True})
        return items

    nav_links: list[dict] = []
    related_m = re.search(
        r'<div\s+class="related-entries">(.*?)</div>',
        page_html, re.DOTALL,
    )
    if related_m:
        nav_links.extend(_parse_related_segments(related_m.group(1)))

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
                        nav_links.extend(_parse_related_segments(related_html_full))
                    pos += next_close.start() + len('</div>')

    # 原始清單為「新 → 舊」（第 N 話 → 第 1 話）；反轉為時間順序，
    # 讓「下一話」按鈕可以 current_idx + 1 正確取得下一集。
    nav_links.reverse()

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：FC2 Blog
# ════════════════════════════════════════════════════════════════

def _extract_fc2_relate_nav(page_html: str, base_url: str) -> list[dict]:
    """抽取 FC2 `<dl class="relate_dl">` 關聯記事清單。

    僅匹配 class 恰為 `relate_dl`（可帶空白分隔的額外字樣），排除
    `relate_dl2`（部分站點如 yaranaioblog 的「前後エントリー」區塊，
    內文之前、僅含上下一話），改取真正的「関連記事」清單。
    """
    nav_links: list[dict] = []
    relate_m = re.search(r'<dl\s+class="relate_dl(?:\s[^"]*)?">(.*?)</dl>', page_html, re.DOTALL)
    if relate_m:
        relate_html = relate_m.group(1)
        for li in re.finditer(
            r'<li\s+class="(relate_li(?:_nolink)?)"[^>]*>(.*?)</li>',
            relate_html, re.DOTALL,
        ):
            li_class = li.group(1)
            li_inner = li.group(2)
            # 移除縮圖／視覺連結 span（如 yaranaioblog 的 `<span class="vislin">`
            # 內含一個只寫著「v」的 <a>），避免後面誤取它而非真正的標題連結。
            li_inner = re.sub(
                r'<span\s+class="vislin">.*?</span>', '', li_inner, flags=re.DOTALL)
            if li_class == 'relate_li':
                a_m = re.search(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', li_inner, re.DOTALL)
                if a_m:
                    href = urljoin(base_url, a_m.group(1))
                    title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
                    nav_links.append({'title': title, 'url': href, 'is_current': False})
            else:
                title = html.unescape(re.sub(r'<[^>]+>', '', li_inner)).strip()
                nav_links.append({'title': title, 'url': None, 'is_current': True})
        # FC2 各站的 relate_dl 排序不一致：多數為「新 → 舊」需 reverse 取得時間順序，
        # 但部分站點（例：yaruosippu.blog.fc2.com）原始 HTML 已是「舊 → 新」時間順序，
        # 此時 reverse 反而會讓「下一話」按鈕變成上一集。
        # 偵測方式：從標題尾端 `(YYYY/MM/DD)` 抽日期，多數相鄰配對為遞增 → 不 reverse；
        # 無日期或日期皆同 → 退回既有「reverse」慣例。
        if not _is_nav_ascending_by_date(nav_links):
            nav_links.reverse()
    return nav_links


def _extract_fc2_num_dd_posts(
    page_html: str, *, author_name: str = "", author_only: bool = False,
) -> str | None:
    """抽取 yarucha.blog.fc2.com 類 FC2 模板的貼文內文。

    此模板無 ently_text/entry_text 等容器；內文置於 `<div class="article">`
    內的 `<span class="aa">`，每則貼文結構為：
        <div><a name="N" class="num">N</a> ： <span class="name"><b>作者</b></span>
             [mail] YYYY/MM/DD(曜) HH:MM:SS.SS ID:XXX<dd>…內文（<br> 分行）…</dd></div>
    合成標準 `<dt>N 標頭</dt><dd>內文</dd>` 後複用 ``_extract_dt_dd_posts``，
    以共用作者過濾／顏色保留邏輯。標頭格式（`N : NAME [] 日期 ID`）由
    ``_POSTER_NAME_RE_ALT`` 涵蓋。
    """
    # 內文邊界：relate_dl（関連記事）之前 —— 其後的 <div class="article"> 為留言區。
    rel = re.search(r'<dl\s+class="relate_dl', page_html)
    region = page_html[:rel.start()] if rel else page_html
    frag = ''.join(
        '<dt>{}{}</dt><dd>{}</dd>'.format(m.group(1), m.group(2), m.group(3))
        for m in re.finditer(
            r'<div><a name="\d+"\s+class="num">(\d+)</a>(.*?)<dd>(.*?)</dd>\s*</div>',
            region, re.DOTALL,
        )
    )
    if not frag:
        return None
    lines_out = _extract_dt_dd_posts(
        '<dl>' + frag + '</dl>', author_name=author_name, author_only=author_only)
    return '\n\n'.join(lines_out) if lines_out else None


def _parse_fc2blog(page_html: str, base_url: str, *, author_name: str = "", author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析 FC2 Blog 格式（含 web.archive.org 封存版）。

    內文：<div class="ently_text">、<div class="entry_text">、<div class="entry_body">、
          <div class="eBody"> 或 <div class="entryblock">（tonarinoaa.blog136.fc2.com
          等 FC2 舊版模板），截止於 fc2button-clap div 或 relate_dl。
    標題：<title>...</title>
    關聯：<dl class="relate_dl ..."> 中的 <li class="relate_li">
    """
    # ── 標題 ──
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""

    # ── 內文 ──（支援多種 FC2 模板：ently_text / entry_text / entry_body / eBody）
    # eBody 變體（例：chronoyaruo.blog.fc2.com）以多個 <div class="aa"> 容納內文、
    # <br> 分行，且續篇貼文另置於 <div class="eBody" id="more"> —— 兩段之間沒有
    # 邊界標記，會一併被 fc2button-clap / relate_dl 終止點涵蓋進來。
    # 另：長期未更新的 FC2 blog 會在文章前插入「スポンサーサイト」廣告區塊，
    # 它同樣是 <div class="ently_text">（內含 sita_rss / counter script 等 cruft），
    # 因此逐一掃描所有容器，取第一個內文含貼文標頭的；都沒有則退回第一個。
    _end_re = re.compile(
        r'<div\s+class="fc2button-clap[^"]*"'
        r'|<dl\s+class="relate_dl'
        r'|<!--/ently_text-->'
        r'|<!--/entry_body-->'
        r'|<!--/entry_text-->'
        r'|<!--/eBody-->'
    )

    # res_h/res_b 結構（例：yaranaioblog.blog.fc2.com）：每則貼文為
    # <div class="res_h">標頭</div><div class="res_b">內文</div>，來源 HTML 帶
    # <br>\r\n + 縮排空白。此結構下換行一律由 <br> 與 <div> 邊界決定，
    # 來源的 \r\n 純屬排版，須移除以免產生空行；行首 ASCII 縮排另於主流程剝除。
    _res_struct = 'class="res_h"' in page_html

    def _extract_body(start_idx: int, max_end: int | None = None) -> str:
        rest = page_html[start_idx:]
        em = _end_re.search(rest)
        body_end = em.start() if em else len(rest)
        # 若有下一個容器的起點，以兩者較小值為實際邊界，避免當前容器的終止點
        # 越過下一個容器（兩容器共享同一 fc2button-clap 終點時會發生）。
        if max_end is not None:
            body_end = min(body_end, max_end - start_idx)
        raw = rest[:body_end]
        # 移除非內文元素：<script>（FC2 計數器/blogroll）、<a>...</a>（導覽連結）
        raw = re.sub(r'<script\b.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r'<a\s[^>]*>.*?</a>', '', raw, flags=re.DOTALL)
        if _res_struct:
            # <br> 與 <div> 開標籤皆視為換行；移除所有來源 \r\n 後再還原，
            # 確保只有 <br>/<div> 邊界產生換行（res_h、res_b 不會黏成同一行）。
            raw = re.sub(r'<br\s*/?>', '\x00', raw)
            raw = re.sub(r'<div\b[^>]*>', '\x00', raw)
            raw = raw.replace('\r', '').replace('\n', '')
            txt = _strip_tags_keep_color(raw).replace('\x00', '\n')
        else:
            txt = re.sub(r'<br\s*/?>', '\n', raw)
            txt = _strip_tags_keep_color(txt)
        return html.unescape(txt)

    def _has_post_header(txt: str) -> bool:
        return any(_POST_HEADER_RE.search(_COLOR_SPAN_RE.sub('', ln))
                   for ln in txt.split('\n'))

    containers = list(re.finditer(
        r'<div\s+class="(?:ently_text|entry_text|entry_body|eBody|entryblock|textar-aa)"[^>]*>', page_html))
    if not containers:
        # yarucha.blog.fc2.com 類模板無上述容器，內文改用
        # <div class="article"><span class="aa"> 內的 <a name/num> + <dd> 結構。
        text_content = _extract_fc2_num_dd_posts(
            page_html, author_name=author_name, author_only=author_only)
        return text_content, _extract_fc2_relate_nav(page_html, base_url), page_title

    # 兩步策略：
    # Step 1. 以「下一個容器起點」為上限 bounded 掃描，找出第一個含貼文標頭的容器
    #         （避免被廣告/スポンサーサイト 區塊騙到：廣告區塊與正文共享同一
    #          fc2button-clap 終止點，若不設上限，廣告容器的範圍會一路延伸到
    #          正文，讓 _has_post_header 在廣告容器中誤命中正文的標頭）。
    # Step 2. 找到正確起點後，改以 **無上限** 抽取，讓終止點自然延伸到
    #         fc2button-clap / relate_dl，自動涵蓋 eBody id="more" 等連續區段。
    fallback_text: str | None = None
    first_real_cm = None
    for i, cm in enumerate(containers):
        next_start = containers[i + 1].start() if i + 1 < len(containers) else None
        bounded = _extract_body(cm.end(), max_end=next_start)
        if fallback_text is None:
            fallback_text = bounded
        if _has_post_header(bounded):
            first_real_cm = cm
            break

    if first_real_cm is not None:
        content_text = _extract_body(first_real_cm.end())  # unbounded，涵蓋連續區段
    else:
        content_text = fallback_text or _extract_body(containers[0].end())

    # 依作者名稱過濾顏色：非作者貼文移除 color span（或完全跳過）
    # 若勾了忽略留言但未指定作者，交由 _filter_color_by_author 自動偵測
    # 未指定作者時：保留所有 color span（讓站方的作者標色等原色顯示），
    # 但仍需呼叫 _cleanup_unmatched_spans 清除 <span class="aa"> 等外層
    # span 被 _strip_tags_keep_color 剝掉開標籤後殘留的孤兒 </span>。
    if author_name or author_only:
        content_text = _filter_color_by_author(content_text, author_name, author_only=author_only)
    else:
        content_text = _cleanup_unmatched_spans(content_text)

    # HTML 縮排的 tab 字元在 strip tags 後殘留；AA 藝術用全形空格（U+3000）而非 tab，
    # 可以安全移除所有 ASCII tab。需在 _cleanup_unmatched_spans 之後執行，否則
    # 孤兒 </span> 可能擋在 tab 前面導致 "^\t+" 行首匹配失效。
    content_text = content_text.replace('\t', '')

    # res_h/res_b 結構：行首 ASCII 空白為 HTML 來源縮排（AA 對齊用全形空格
    # U+3000，不受影響），逐行剝除；<div>/<br> 邊界堆出的多餘空行壓回一行。
    if _res_struct:
        # 先逐行剝除行首 ASCII 空白（空白-only 行因此變為真正空行），
        # 再把 <div>/<br> 邊界堆出的多餘空行壓回最多一行。
        content_text = '\n'.join(ln.lstrip(' ') for ln in content_text.split('\n'))
        content_text = re.sub(r'\n{3,}', '\n\n', content_text)

    lines = content_text.split('\n')
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text_content = '\n'.join(lines) if lines else None

    # ── 關聯連結 ──
    nav_links = _extract_fc2_relate_nav(page_html, base_url)

    return text_content or None, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：yaruobook.jp
# ════════════════════════════════════════════════════════════════

def _parse_yaruobook(page_html: str, base_url: str, *, author_name: str = "", author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析 yaruobook.jp 格式（亦涵蓋 yaruohiroba.com 等共用 dt/dd 結構的站點）。

    內文：<dt class="author-res-dt"> ... </dt><dd class="author-res"> ... </dd>
    標頭格式：「N ： AUTHOR ： YYYY/MM/DD(曜) HH:MM:SS.SS ID:XXX」
    標題：<title>...</title>
    關聯：<ul class="relatedPostsWrap relatedPostsPrev/Next">，<li class="currentPost"> 標記當前話；
          若無此結構，改解析 <ul class="related-list">（yaruohiroba.com，
          <li class="related-current"> 標記當前話、清單已為時間順序）。
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
        r'|<ul\s+[^>]*class="[^"]*relatedPostsWrap'
        r'|<div\s+[^>]*class="[^"]*related-section',
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

    # ── related-list 變體（例：yaruohiroba.com）──
    # 無 relatedPostsWrap 時，改解析 <ul class="related-list">：
    #   <li class=""><a href="URL">TITLE</a></li>          → 其他話
    #   <li class="related-current"><strong>TITLE</strong> → 當前話（無連結）
    # 清單原始順序已是「舊 → 新」時間順序，不需反轉。
    if not nav_links:
        rl_m = re.search(
            r'<ul\s+class="related-list">(.*?)</ul>', page_html, re.DOTALL)
        if rl_m:
            for li in re.finditer(
                r'<li\s+class="([^"]*)"[^>]*>(.*?)</li>', rl_m.group(1), re.DOTALL):
                li_class = li.group(1)
                li_inner = li.group(2)
                is_current = 'related-current' in li_class
                a_m = re.search(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', li_inner, re.DOTALL)
                if a_m:
                    href = urljoin(base_url, a_m.group(1))
                    title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
                    if title:
                        nav_links.append({'title': title, 'url': href, 'is_current': is_current})
                else:
                    title = html.unescape(re.sub(r'<[^>]+>', '', li_inner)).strip()
                    if title:
                        nav_links.append({'title': title, 'url': None, 'is_current': is_current})

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：yaruobook.net（早期 HTML 數字字元引用版本）
# ════════════════════════════════════════════════════════════════

def _parse_yaruobook_net(page_html: str, base_url: str, *,
                         author_name: str = "",
                         author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析 yaruobook.net 格式（含早期以 HTML 數字字元引用編碼的文章）。

    內文：<div id="entry-content" class="entry-content ..."> 內的 <dl>/<dt>/<dd>。
    早期文章中「：」等符號以 `&#65306;` 等 numeric character reference 呈現，
    由 `_extract_dt_dd_posts` 內部的 `html.unescape` 正規化後即可用現有
    `_POST_HEADER_RE` / `_POSTER_NAME_RE_ALT` 處理。
    關聯：沿用 `yaruobook.jp` 的 `<ul class="relatedPostsWrap relatedPostsPrev/Next">` 結構。
    """
    # ── 標題 ──
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""
    page_title = re.sub(r'\s*[\|｜\-－–—]\s*やる夫ブック.*$', '', page_title)

    # ── 內文區塊 ──
    start_m = re.search(r'<div\s+id="entry-content"[^>]*>', page_html)
    if not start_m:
        return None, [], page_title

    end_m = re.search(
        r'<div\s+id="custom_html-'
        r'|<div\s+[^>]*class="[^"]*widget-single-content-bottom'
        r'|<ul\s+[^>]*class="[^"]*relatedPostsWrap',
        page_html[start_m.end():],
    )
    content_end = start_m.end() + end_m.start() if end_m else len(page_html)
    content_html = page_html[start_m.end():content_end]

    lines_out = _extract_dt_dd_posts(
        content_html, author_name=author_name, author_only=author_only)
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
    # Prev 結構與 yaruobook.jp 相同：[currentPost, 前一話, 前前話, ...]；
    # 反轉後變 [..., 前前話, 前一話, currentPost]，再接 Next 即為時間順序。
    if prev_m:
        nav_links.extend(reversed(_parse_related_ul(prev_m.group(1))))
    if next_m:
        nav_links.extend(_parse_related_ul(next_m.group(1)))

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：yaruo-matome.com
# ════════════════════════════════════════════════════════════════

def _parse_yaruo_matome(page_html: str, base_url: str, *,
                        author_name: str = "",
                        author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析 yaruo-matome.com 格式。

    內文：<div id="entry-content" class="entry-content"> 內的 <dt>/<dd>。
    標頭格式：「N ：<font color="green">NAME</font>：YYYY/MM/DD(曜) HH:MM:SS ID:XXX」
    （<font color> 標籤由 _strip_tags_keep_color 轉換為 color span）
    關聯：<ul class="nexe-prev-post"> — <li><a> 為其他話，<li><span> 為當前話（無連結）。
    清單原始順序為「最新話 → 舊話 → 當前話」，反轉後即為時間順序。
    """
    # ── 標題 ──
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""
    # 去除常見站名後綴（「タイトル - やる夫まとめ」等）
    page_title = re.sub(r'\s*[\|｜\-－–—]\s*.{0,20}まとめ.*$', '', page_title)

    # ── 內文區塊：找 <div ... id="entry-content" ...> ──
    start_m = re.search(r'<div\b[^>]*\bid="entry-content"[^>]*>', page_html)
    if not start_m:
        return None, [], page_title

    # 截止點：關聯記事列表或其他常見頁尾區塊
    end_m = re.search(
        r'<ul\b[^>]*\bclass="[^"]*nexe-prev-post'
        r'|<div\b[^>]*\bclass="[^"]*wp-block-related'
        r'|<div\b[^>]*\bid="comments"',
        page_html[start_m.end():],
    )
    content_end = start_m.end() + end_m.start() if end_m else len(page_html)
    content_html = page_html[start_m.end():content_end]

    # WordPress が <br /> の直後に source HTML の改行を挿入するため、
    # <br />\n が後で \n\n（空行）になるのを防ぐ。
    # <p>&nbsp;</p> のような空行 spacer はそのまま残して blank line として保持する。
    content_html = re.sub(r'<br\s*/?>[ \t]*\n', '<br />', content_html)

    lines_out = _extract_dt_dd_posts(
        content_html, author_name=author_name, author_only=author_only)
    text_content = '\n\n'.join(lines_out) if lines_out else None

    # ── 平面 <p>...<br/>... 變體 fallback（無 dt/dd 結構）──
    # 例：yaruo-matome.com/archives/21514 — 整批貼文塞在一個 <p> 內，
    # 每行前綴 `.` 是防瀏覽器壓縮空行的占位符。標頭格式為
    # 「.N ： AUTHOR ： YYYY/MM/DD(曜) HH:MM:SS.ms ID:XXX」（dot 前綴）。
    # 僅在 dt/dd 抽取失敗時啟用，避免影響既有正常頁面。
    if not text_content:
        flat = content_html
        # 移除非內文元素（書籤按鈕、收藏連結 span）
        flat = re.sub(r'<button\b[^>]*>.*?</button>', '', flat, flags=re.DOTALL)
        flat = re.sub(
            r"<span\b[^>]*\bclass=['\"][^'\"]*wpfp[^'\"]*['\"][^>]*>.*?</span>",
            '', flat, flags=re.DOTALL,
        )
        flat = re.sub(r'<br\s*/?>', '\n', flat)
        flat = _strip_tags_keep_color(flat)
        flat = html.unescape(flat)
        # 每行去除首字 `.`（防壓縮占位）。第一行常被 <p> 的 HTML 縮排塞入
        # 行首 tab／空白（如 `\t\t\t.3825 ：…`），需連同其前的 ASCII 空白一併
        # 吸收，否則占位點殘留會使該樓標頭 `_POST_HEADER_RE` 失配、在「忽略
        # 留言」模式下被整段跳過（AA 對齊用全形 U+3000，行首 ASCII 空白屬縮排可去）。
        flat_lines = [re.sub(r'^[ \t]*\.', '', ln) for ln in flat.split('\n')]
        flat = '\n'.join(flat_lines)
        if author_name or author_only:
            flat = _filter_color_by_author(
                flat, author_name, author_only=author_only)
        else:
            flat = re.sub(r'<span\s+style="color:[^"]*">|</span>', '', flat)
        out_lines = flat.split('\n')
        while out_lines and not out_lines[0].strip():
            out_lines.pop(0)
        while out_lines and not out_lines[-1].strip():
            out_lines.pop()
        # 確認真的取到貼文（標頭行存在）才採用，避免把純 cruft 當內文
        if out_lines and any(_POST_HEADER_RE.search(_COLOR_SPAN_RE.sub('', ln))
                             for ln in out_lines):
            text_content = '\n'.join(out_lines)

    # ── 關聯連結 ──
    nav_links: list[dict] = []
    ul_m = re.search(
        r'<ul\b[^>]*\bclass="[^"]*nexe-prev-post[^"]*"[^>]*>(.*?)</ul>',
        page_html, re.DOTALL,
    )
    if ul_m:
        for li in re.finditer(r'<li[^>]*>(.*?)</li>', ul_m.group(1), re.DOTALL):
            li_inner = li.group(1)
            a_m = re.search(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', li_inner, re.DOTALL)
            if a_m:
                href = urljoin(base_url, a_m.group(1))
                title = html.unescape(re.sub(r'<[^>]+>', '', a_m.group(2))).strip()
                if title:
                    nav_links.append({'title': title, 'url': href, 'is_current': False})
            else:
                # <span> のみ（リンクなし）→ 当前話
                title = html.unescape(re.sub(r'<[^>]+>', '', li_inner)).strip()
                if title:
                    nav_links.append({'title': title, 'url': None, 'is_current': True})
        # 原始清單為「最新話 → … → 當前話」；反轉為時間順序，
        # 讓「下一話」按鈕可以 current_idx + 1 正確取得下一集。
        nav_links.reverse()

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：livedoor Blog
# ════════════════════════════════════════════════════════════════

def _parse_livedoor(page_html: str, base_url: str, *,
                    author_name: str = "",
                    author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析 livedoor Blog（blog.livedoor.jp）格式。

    內文：<div class="textar-aa"><span class="aa"> … </span>，以 <br> 分行，
          截止於 <br clear="all">。
    標題：<h3 class="title"> 內文字（去除 <span class="s"> 分類連結）。
    關聯：此站無「關聯記事」區塊，僅在最後一則貼文尾端嵌入下一話的
          <a href=".../archives/N.html">；僅取內文**尾段**的 archives 連結
          （內文開頭常另有「同作者さんの作品」推薦區塊，須排除），置於
          當前話之後。
    """
    _TAIL_WINDOW = 5000  # 內文尾段範圍：尾端話次連結必落於此，開頭推薦區塊則否
    # ── 標題 ──
    title_m = re.search(r'<h3\s+class="title">(.*?)</h3>', page_html, re.DOTALL)
    if title_m:
        t = re.sub(r'<span\s+class="s">.*?</span>', '', title_m.group(1), flags=re.DOTALL)
        page_title = html.unescape(re.sub(r'<[^>]+>', '', t)).strip()
    else:
        tm = re.search(r'<title>([^<]+)</title>', page_html)
        page_title = html.unescape(tm.group(1)).strip() if tm else ""

    # ── 內文容器 ──
    cm = re.search(r'<div\s+class="textar-aa">', page_html)
    if not cm:
        return None, [], page_title
    rest = page_html[cm.end():]
    em = re.search(r'<br\s+clear="all"', rest)
    body_html = rest[:em.start()] if em else rest

    # ── 關聯連結（內文尾段的 archives 連結 = 後續話次）──
    nav_links: list[dict] = []
    tail_start = len(body_html) - _TAIL_WINDOW
    for a in re.finditer(
        r'<a\s+href="([^"]*/archives/\d+\.html)"[^>]*>(.*?)</a>',
        body_html, re.DOTALL,
    ):
        if a.start() < tail_start:
            continue  # 開頭「同作者さんの作品」推薦區塊，非後續話次
        href = urljoin(base_url, a.group(1))
        if href.rstrip('/') == base_url.rstrip('/'):
            continue
        a_title = html.unescape(re.sub(r'<[^>]+>', '', a.group(2))).strip()
        if a_title:
            nav_links.append({'title': a_title, 'url': href, 'is_current': False})
    if nav_links:
        nav_links.insert(0, {'title': page_title, 'url': None, 'is_current': True})

    # ── 內文抽取（flat：<br>→\n、保留 color span）──
    raw = re.sub(r'<script\b.*?</script>', '', body_html, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'<a\s[^>]*>.*?</a>', '', raw, flags=re.DOTALL)
    raw = raw.replace('\r', '')
    # 來源 HTML 每行為 `LINE<br>\n`：<br> 才是真正換行，其後的排版換行須一併
    # 吸收，否則每行會多出一個空行。<br><br> 等連續換行仍各自保留為空行。
    txt = re.sub(r'<br\s*/?>[ \t]*\n?', '\n', raw)
    txt = _strip_tags_keep_color(txt)
    txt = html.unescape(txt)
    if author_name or author_only:
        txt = _filter_color_by_author(txt, author_name, author_only=author_only)
    else:
        txt = _cleanup_unmatched_spans(txt)
    txt = txt.replace('\t', '')

    lines = txt.split('\n')
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text_content = '\n'.join(lines) if lines else None

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  解析器：asitayaruo.com
# ════════════════════════════════════════════════════════════════

def _fetch_asitayaruo_category(cat_url: str, current_url: str) -> list[dict]:
    """逐頁抓取 asitayaruo.com 分類頁的所有貼文清單，回傳「舊 → 新」時間順序的 nav。

    分類頁分頁形式為 `?cat=N&paged=K`，每頁列出 50 篇（最新→最舊），
    `<h3><a href="?p=N">TITLE</a></h3>` 為條目樣式。
    當前 URL 對應條目 `is_current=True`、`url=None`。
    """
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    page_num = 1
    while page_num <= 50:  # 安全閥
        page_url = cat_url if page_num == 1 else f"{cat_url}&paged={page_num}"
        try:
            cat_html = fetch_url(page_url)
        except Exception:
            break
        page_added = 0
        for m in re.finditer(
            r'<h3><a\s+href="([^"]+)"[^>]*>\s*(.*?)\s*</a></h3>',
            cat_html, re.DOTALL,
        ):
            href = html.unescape(m.group(1))
            title = html.unescape(re.sub(r'\s+', ' ', m.group(2))).strip()
            if not title or href in seen:
                continue
            seen.add(href)
            items.append((href, title))
            page_added += 1
        if page_added == 0:
            break
        # 是否還有下一頁：尋找 `paged=K+1` 連結
        if not re.search(rf'paged=(?:0*){page_num + 1}\b', cat_html):
            break
        page_num += 1

    # 來源為「新 → 舊」；反轉為時間順序，讓「下一話」按鈕可以
    # current_idx + 1 正確取得下一集。
    items.reverse()

    cur_norm = current_url.split('#')[0].rstrip('/')
    nav: list[dict] = []
    for href, title in items:
        is_current = href.split('#')[0].rstrip('/') == cur_norm
        nav.append({
            'title': title,
            'url': None if is_current else href,
            'is_current': is_current,
        })
    return nav


def _parse_asitayaruo(page_html: str, base_url: str, *,
                      author_name: str = "",
                      author_only: bool = False) -> tuple[str | None, list[dict], str]:
    """解析 asitayaruo.com 格式（明日やる夫は馬鹿やる夫）。

    內文：<div class="entry-content"> 內的 <dt>/<dd>，標頭格式為
          「<span>N</span> ： <span ...>AUTHOR</span> ： <span>YYYY/MM/DD(曜) HH:MM:SS</span>
           <span style="color:#ff0000">ID:XXX</span>」。
    標題：<title>...</title>（去除站名後綴「- 明日やる夫は馬鹿やる夫」）。
    關聯：本站文章頁面**沒有** prev/next 關聯區塊；分類資訊以 `<p class="tagst">`
          中的 `?cat=N` 連結指向分類頁，需另發 HTTP 抓取該分類頁（分頁形式
          `?cat=N&paged=K`）取得同系列文章清單，做為關聯記事。
    """
    # ── 標題 ──
    title_m = re.search(r'<title>([^<]+)</title>', page_html)
    page_title = html.unescape(title_m.group(1)).strip() if title_m else ""
    page_title = re.sub(r'\s*[\|｜\-－–—]\s*明日やる夫.*$', '', page_title)

    # ── 內文 ──
    start_m = re.search(r'<div\s+class="entry-content"[^>]*>', page_html)
    if not start_m:
        return None, [], page_title

    # entry-content 之後到 `<p class="tagst">`（分類列）為止為純內文範圍
    end_m = re.search(r'<p\s+class="tagst"', page_html[start_m.end():])
    content_end = start_m.end() + end_m.start() if end_m else len(page_html)
    content_html = page_html[start_m.end():content_end]

    lines_out = _extract_dt_dd_posts(
        content_html, author_name=author_name, author_only=author_only)
    text_content = '\n\n'.join(lines_out) if lines_out else None

    # ── 關聯記事：從 tagst 取分類 URL，再 HTTP 抓分類頁 ──
    nav_links: list[dict] = []
    tag_m = re.search(r'<p\s+class="tagst"[^>]*>(.*?)</p>', page_html, re.DOTALL)
    if tag_m:
        cat_a = re.search(
            r'<a\s+href="([^"]*\?cat=\d+)"', tag_m.group(1))
        if cat_a:
            cat_url = urljoin(base_url, html.unescape(cat_a.group(1)))
            try:
                nav_links = _fetch_asitayaruo_category(cat_url, base_url)
            except Exception:
                nav_links = []

    return text_content, nav_links, page_title


# ════════════════════════════════════════════════════════════════
#  公開入口
# ════════════════════════════════════════════════════════════════

# 網域 → 解析函式的對應表
_DOMAIN_PARSERS: dict[str, callable] = {
    'himanatokiniyaruo.com': _parse_himanatokiniyaruo,
    'blog.livedoor.jp': _parse_livedoor,
    'blog.fc2.com': _parse_fc2blog,
    'blog105.fc2.com': _parse_fc2blog,
    'blog136.fc2.com': _parse_fc2blog,
    'yaruobook.jp': _parse_yaruobook,
    'yaruohiroba.com': _parse_yaruobook,
    'yaruobook.net': _parse_yaruobook_net,
    'yaruobook.com': _parse_yaruobook_net,
    'yaruo-matome.com': _parse_yaruo_matome,
    'asitayaruo.com': _parse_asitayaruo,
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

    分派策略：
    1. **已知網域**（見 ``_DOMAIN_PARSERS``）→ 直接使用指定解析器。
    2. **未知網域** → 依序嘗試 ``_parse_default`` 與所有網域專屬解析器，
       回傳第一個成功取到內文的結果；全部失敗時回傳最後一次嘗試的結果。

    支援 web.archive.org 封存 URL 的網域識別。
    若指定 author_name，僅保留該作者貼文的顏色標記。
    若 author_only=True 且指定 author_name，則完全排除非作者貼文。

    - 文本內容：提取的純文字，找不到時為 ``None``
    - 關聯連結：``[{'title': ..., 'url': ... or None, 'is_current': bool}, ...]``
    - 頁面標題：清理後的標題字串
    """
    domain = _resolve_domain(base_url)

    # 網域匹配到的解析器優先嘗試；若取不到內文（子站模板不同等），
    # 再 fallback 到其他解析器。多數 FC2 子站的模板差異已在 `_parse_fc2blog`
    # 內部涵蓋（例：`yarucha.blog.fc2.com` 用 `<a name/num>` + `<dd>` 結構，
    # 由該解析器的無容器分支處理），仍取不到內文時才走 fallback。
    primary_parser = None
    for key, parser in _DOMAIN_PARSERS.items():
        if key in domain:
            primary_parser = parser
            break

    last_result: tuple[str | None, list[dict], str] = (None, [], "")
    if primary_parser is not None:
        try:
            result = primary_parser(
                page_html, base_url,
                author_name=author_name, author_only=author_only,
            )
            if result[0] and result[0].strip():
                return result
            last_result = result
        except Exception:
            pass

    # fallback：依序嘗試所有解析器（預設優先，因為最通用）
    parsers: list = [_parse_default, *_DOMAIN_PARSERS.values()]
    for parser in parsers:
        if parser is primary_parser:
            continue  # 已試過
        try:
            result = parser(
                page_html, base_url,
                author_name=author_name, author_only=author_only,
            )
        except Exception:
            continue
        text_content = result[0]
        if text_content and text_content.strip():
            return result
        if not last_result[0]:
            last_result = result
    return last_result
