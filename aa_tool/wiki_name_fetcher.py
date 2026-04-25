"""Wikipedia 角色列表頁日中對照抓取 — 純邏輯模組，無 UI 依賴。

輸入：Wikipedia 角色列表頁的 HTML 原始碼。
輸出：`[(日文, 中文), ...]` 以日文為 key 去重後的清單。

解析策略依序嘗試，彙整所有結果後以日文為 key 去重：
  A. `<dt>` + `<span lang="ja">` 結構（主要）；若 lang=ja 內容為假字串，
     fallback 取 `（…，XXX）` 或 `(…, XXX)` 中括號內逗號後之字串。
  B. `<table class="wikitable">` 表格：前兩欄判斷哪欄含假名作為日文名。
  C. 泛用 `<span lang="ja">` 配對：以 span 之前最近的中文字串為中文名。
"""
from __future__ import annotations

import html as _html
import re

# 平假名 / 片假名偵測
_JP_KANA_RE = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u30fc]')
# 漢字（可能是中/日共用）
_HAN_RE = re.compile(r'[\u4e00-\u9fff]')
# 中文字元（用於策略 C 取 span 之前的中文名候選）
_CN_NAME_RE = re.compile(r'[\u4e00-\u9fff·・‧\s]+$')


def _strip_tags(s: str) -> str:
    """移除 HTML tag 與 refs、註解等。"""
    s = re.sub(r'<!--.*?-->', '', s, flags=re.S)
    s = re.sub(r'<sup\b[^>]*>.*?</sup>', '', s, flags=re.S | re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = _html.unescape(s)
    return s.strip()


def _has_kana(s: str) -> bool:
    return bool(_JP_KANA_RE.search(s))


def _looks_like_jp(s: str) -> bool:
    """判斷是否像日文：含假名，或僅含漢字但非純空白。
    排除明顯假字串如 'ja'。"""
    if not s or s.strip().lower() == 'ja':
        return False
    return bool(_JP_KANA_RE.search(s) or _HAN_RE.search(s))


def _clean_name(s: str) -> str:
    """清理名稱：去除首尾標點、空白、括號殘留。"""
    s = s.strip()
    # 去除首尾的常見分隔符號
    s = s.strip('　 \t\r\n，,、。.；;：:()（）[]［］【】「」『』""''')
    return s.strip()


# ════════════════════════════════════════════════════════════════
#  策略 A — <dt> + <span lang="ja">
# ════════════════════════════════════════════════════════════════

_DT_RE = re.compile(r'<dt\b[^>]*>(.*?)</dt>', re.S | re.I)
_SPAN_JA_RE = re.compile(r'<span\b[^>]*\blang="ja"[^>]*>(.*?)</span>', re.S | re.I)
# （日語假名，XXX） 或 (日語假名, XXX) — 取逗號後至右括號前
_PAREN_JP_RE = re.compile(
    r'[（(][^（()）]*?[，,、]\s*([^（()）]+?)\s*[)）]',
    re.S,
)


def _extract_cn_from_dt(dt_inner: str) -> str:
    """從 <dt> 內容取中文名：第一個 `<span`/`<` 之前的純文字。"""
    # 取到第一個 tag 之前
    m = re.match(r'\s*([^<]+)', dt_inner)
    if not m:
        return ''
    return _clean_name(_html.unescape(m.group(1)))


def _extract_jp_from_dt(dt_inner: str) -> str:
    """從 <dt> 內容取日文名：優先 <span lang="ja">，其次括號內逗號後字串。"""
    # 先嘗試 <span lang="ja">
    for m in _SPAN_JA_RE.finditer(dt_inner):
        text = _clean_name(_strip_tags(m.group(1)))
        if _looks_like_jp(text):
            return text

    # fallback：括號內逗號後字串
    stripped = _strip_tags(dt_inner)
    for m in _PAREN_JP_RE.finditer(stripped):
        text = _clean_name(m.group(1))
        if _looks_like_jp(text):
            return text
    return ''


def _parse_strategy_dt(html_text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _DT_RE.finditer(html_text):
        inner = m.group(1)
        cn = _extract_cn_from_dt(inner)
        jp = _extract_jp_from_dt(inner)
        if cn and jp:
            out.append((jp, cn))
    return out


# ════════════════════════════════════════════════════════════════
#  策略 B — <table class="wikitable">
# ════════════════════════════════════════════════════════════════

_TABLE_RE = re.compile(
    r'<table\b[^>]*class="[^"]*wikitable[^"]*"[^>]*>(.*?)</table>',
    re.S | re.I,
)
_TR_RE = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.S | re.I)
_TD_RE = re.compile(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', re.S | re.I)


def _parse_strategy_table(html_text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for tm in _TABLE_RE.finditer(html_text):
        for rm in _TR_RE.finditer(tm.group(1)):
            cells_raw = _TD_RE.findall(rm.group(1))
            if len(cells_raw) < 2:
                continue
            cells = [_clean_name(_strip_tags(c)) for c in cells_raw[:3]]
            # 在前 3 欄中找「含假名」與「含中文但不含假名」的組合
            jp_cell = ''
            cn_cell = ''
            for c in cells:
                if not c:
                    continue
                if _has_kana(c) and not jp_cell:
                    jp_cell = c
                elif _HAN_RE.search(c) and not _has_kana(c) and not cn_cell:
                    cn_cell = c
            if jp_cell and cn_cell:
                out.append((jp_cell, cn_cell))
    return out


# ════════════════════════════════════════════════════════════════
#  策略 C — 泛用 <span lang="ja"> 配對
# ════════════════════════════════════════════════════════════════

_SPAN_JA_WITH_POS_RE = re.compile(
    r'<span\b[^>]*\blang="ja"[^>]*>(.*?)</span>',
    re.S | re.I,
)


def _parse_strategy_span(html_text: str) -> list[tuple[str, str]]:
    """對每個 <span lang="ja">，往前找最近的中文名。"""
    out: list[tuple[str, str]] = []
    for m in _SPAN_JA_WITH_POS_RE.finditer(html_text):
        jp = _clean_name(_strip_tags(m.group(1)))
        if not _looks_like_jp(jp):
            continue
        # 取 span 之前 200 字元範圍，去 tag 後取尾端連續中文串
        lookback_start = max(0, m.start() - 400)
        prefix_raw = html_text[lookback_start:m.start()]
        prefix_text = _strip_tags(prefix_raw)
        # 去掉尾端括號、空白
        prefix_text = re.sub(r'[（(]\s*$', '', prefix_text).rstrip()
        mm = _CN_NAME_RE.search(prefix_text)
        if not mm:
            continue
        cn = _clean_name(mm.group(0))
        if cn and _HAN_RE.search(cn) and not _has_kana(cn):
            out.append((jp, cn))
    return out


# ════════════════════════════════════════════════════════════════
#  對外主入口
# ════════════════════════════════════════════════════════════════

def parse_wiki_name_list(html_text: str) -> list[tuple[str, str]]:
    """從 Wikipedia HTML 取出 [(日文, 中文), ...]，以日文為 key 去重。"""
    collected: list[tuple[str, str]] = []
    for fn in (_parse_strategy_dt, _parse_strategy_table, _parse_strategy_span):
        try:
            collected.extend(fn(html_text))
        except Exception:
            # 單一策略失敗不影響整體
            continue

    seen: dict[str, str] = {}
    result: list[tuple[str, str]] = []
    for jp, cn in collected:
        jp = _clean_name(jp)
        cn = _clean_name(cn)
        if not jp or not cn or jp == cn:
            continue
        if not _looks_like_jp(jp):
            continue
        if jp in seen:
            continue
        seen[jp] = cn
        result.append((jp, cn))
    return result
