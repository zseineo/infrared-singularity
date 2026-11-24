"""AI 校稿素材匯出（實驗性，v3.55）。

把已翻好的 HTML 整理成「給 AI 校稿用」的對照表，搭配原文暫存
（`original_cache` 的 ``text``／``extracted``）：

- 原文行號與譯文檔行號一一對應（翻譯只替換行內文字，不增刪行），
  ID ``NNN-k`` 的 NNN 是 1 起算的行號。
- 「目前的譯文」不能用暫存的 ``translation``：存檔後在編輯器手改、對話框
  換行把譯文接到下一行，都不會回寫暫存（實測約 15% 的 ID 對不上）。所以
  逐行拿「原文行 vs. 譯文檔現在這一行」去掉共同的頭尾，中間那段就是這一行
  現在的譯文（行內左右兩個對話框時，中間會夾一點 AA，不影響閱讀）。
- 依投稿標頭切成貼文，對照表標出每則貼文的行號範圍，供對應截圖。

純函式，無 GUI 依賴；截圖由 `aa_proofread_qt` 負責。
"""
from __future__ import annotations

import html
import os
import re

from .url_fetcher import _POST_HEADER_RE

_COLOR_MARKUP_RE = re.compile(r'<span\s+style="color:[^"]*">|</span>')
_PRE_RE = re.compile(r'<pre>([\s\S]*?)</pre>', re.IGNORECASE)
_BLANK = ' 　\t'
# 判斷一段差異是不是文字（不是只動到 AA 邊框／空白）：含假名、漢字或英數。
_TEXTISH_RE = re.compile(r'[ぁ-ゖァ-ヺ一-鿿々A-Za-z0-9Ａ-Ｚａ-ｚ０-９]')
# 譯文檔裡殘留的日文：含平假名、長度 ≥3 的日文字串。只看平假名是因為 AA 圖
# 常用片假名／漢字當筆畫（ノ、ヽ、八、乂），平假名則幾乎只出現在句子裡。
_JP_RUN_RE = re.compile(r'[ぁ-ゖァ-ヺー一-鿿々、。！？…「」『』]{3,}')
_HIRA_RE = re.compile(r'[ぁ-ゖ]')


def plain_lines_from_html(file_path: str) -> list[str] | None:
    """讀 HTML 的 <pre>，去掉顏色標記後 unescape，回傳逐行文字。"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    m = _PRE_RE.search(content)
    if not m:
        return None
    return html.unescape(_COLOR_MARKUP_RE.sub('', m.group(1))).split('\n')


def plain_lines(text: str) -> list[str]:
    return _COLOR_MARKUP_RE.sub('', text).split('\n')


def changed_region(orig: str, cur: str, fragments: list[str] | None = None) -> str:
    """原文行與譯文行去掉共同頭尾後，譯文那側剩下的部分（去頭尾空白）。

    ``fragments``＝這一行被提取的原文片段。中文譯文常和原文共用漢字／標點
    （「熱烈歓迎」→「熱烈歡迎」、句尾「！」），單純比共同頭尾會把它們當 AA
    切掉，所以頭尾最多只裁到第一個片段之前、最後一個片段之後。
    """
    if orig == cur:
        return ''
    p = len(os.path.commonprefix([orig, cur]))
    s = len(os.path.commonprefix([orig[p:][::-1], cur[p:][::-1]]))
    if fragments:
        pos = [(i, i + len(f)) for f in fragments if f and (i := orig.find(f)) >= 0]
        if pos:
            p = min(p, min(a for a, _ in pos))
            s = min(s, len(orig) - max(b for _, b in pos))
    return cur[p:len(cur) - s].strip(_BLANK)


# AA 圖常見、句子裡幾乎不會出現的字；含這些字的日文串不算殘留日文（「く二彡」）。
_AA_KANJI = set('二彡乂丶ヽヾ')


def leftover_japanese(line: str) -> list[str]:
    """譯文行裡疑似沒翻到的日文片段。"""
    return [t for m in _JP_RUN_RE.finditer(line)
            if _HIRA_RE.search(t := m.group(0)) and not _AA_KANJI.intersection(t)]


# 投稿標頭：`_POST_HEADER_RE`（行首有貼文編號），或沒有編號、只有
# 「名前 日期 時間 ID」的格式（`厄満◆xxx 2017/06/10(土) 15:32:01.02 ID:xxx`）。
_DATE_TIME_RE = re.compile(
    r'\d{4}/\d{1,2}/\d{1,2}\s*(?:\([^)]*\))?\s*\d{1,2}:\d{2}:\d{2}')
# 太長的貼文截圖切段：最多幾行一張，盡量切在空行。
MAX_IMAGE_LINES = 70


def _is_header(line: str) -> bool:
    if _POST_HEADER_RE.match(line):
        return True
    return len(line.strip()) < 150 and bool(_DATE_TIME_RE.search(line))


def image_chunks(start: int, end: int, lines: list[str]) -> list[tuple[int, int]]:
    """把一則貼文（行號 1 起算、含頭尾）切成每張最多 MAX_IMAGE_LINES 行，
    優先切在空行。"""
    out = []
    while end - start + 1 > MAX_IMAGE_LINES:
        cut = start + MAX_IMAGE_LINES - 1
        for ln in range(cut, start + MAX_IMAGE_LINES // 2, -1):
            if not lines[ln - 1].strip(_BLANK):
                cut = ln
                break
        out.append((start, cut))
        start = cut + 1
    out.append((start, end))
    return out


def split_posts(lines: list[str]) -> list[tuple[int, int]]:
    """依投稿標頭切貼文，回傳 [(起始行, 結束行)]（1 起算、含頭尾）。
    第一個標頭之前若有內容（標題、置頂）算第 0 則。"""
    heads = [i for i, ln in enumerate(lines) if _is_header(ln)]
    if not heads or heads[0] != 0:
        heads = [0] + heads
    bounds = heads + [len(lines)]
    return [(bounds[k] + 1, bounds[k + 1]) for k in range(len(heads))
            if bounds[k + 1] > bounds[k]]


def parse_extracted(extracted: str) -> dict[int, list[str]]:
    """``NNN-k|原文`` → {行號: [原文片段…]}。"""
    out: dict[int, list[str]] = {}
    for raw in (extracted or '').splitlines():
        key, sep, jp = raw.partition('|')
        if not sep:
            continue
        try:
            ln = int(key.split('-')[0])
        except ValueError:
            continue
        out.setdefault(ln, []).append(jp.strip())
    return out


def build_table(file_name: str, orig_text: str, extracted: str,
                cur_lines: list[str]) -> tuple[str, list[dict], dict]:
    """產生一個檔案的對照表。

    回傳 (對照表文字, 貼文清單, 統計)。貼文清單每項
    ``{'no', 'start', 'end', 'has_text'}``，供截圖決定要畫哪些貼文。
    """
    orig = plain_lines(orig_text)
    ex = parse_extracted(extracted)
    stats = {'lines': len(cur_lines), 'ids': sum(len(v) for v in ex.values()),
             'untranslated': 0, 'leftover': 0, 'line_mismatch': len(orig) != len(cur_lines)}
    out = [f'# 檔案：{file_name}']
    if stats['line_mismatch']:
        out.append(f'# ⚠ 原文 {len(orig)} 行、譯文檔 {len(cur_lines)} 行，行數不同，'
                   '對照可能錯位，請以截圖為準。')
    posts = []
    for no, (start, end) in enumerate(split_posts(cur_lines)):
        body = []
        for ln in range(start, end + 1):
            cur = cur_lines[ln - 1]
            o = orig[ln - 1] if ln - 1 < len(orig) else ''
            jps = ex.get(ln)
            region = changed_region(o, cur, jps)
            if region and not _TEXTISH_RE.search(region):
                region = ''          # 只動到 AA 邊框／空白
            if jps:
                body.append(f'{ln:03d}|原：' + ' ／ '.join(jps))
                if region:
                    body.append(f'{ln:03d}|譯：{region}')
                else:
                    body.append(f'{ln:03d}|譯：（這一行沒有變動，可能沒翻到）')
                    stats['untranslated'] += 1
            elif region:
                body.append(f'{ln:03d}|譯：{region}')
            left = leftover_japanese(cur)
            if left and (region or not jps):
                # 有 ID 卻沒變動的行上面已標過，不重複
                body.append(f'{ln:03d}|⚠未翻：' + ' ／ '.join(left))
                stats['leftover'] += 1
        posts.append({'no': no, 'start': start, 'end': end, 'has_text': bool(body)})
        if body:
            out.append('')
            out.append(f'=== 貼文 {no:03d}（第 {start}～{end} 行）===')
            out.extend(body)
    return '\n'.join(out) + '\n', posts, stats


INSTRUCTIONS = """\
# AI 校稿說明

你是 AA 故事（日文 → 繁體中文）的校稿員。請檢查這個資料夾裡每一話的譯文，
找出需要修正的地方，把結果寫成 `校稿結果.txt`（格式見最後）。

## 素材

- `術語表.txt`：譯者的術語表，每行 `日文=中文`。人名、地名、招式名以它為準。
- 每一話一個子資料夾：
  - `對照表.txt`：依貼文分段。每一行開頭是行號：
    - `行號|原：…` 日文原文（程式提取出來的片段；一行有多段時用 ／ 分開）
    - `行號|譯：…` 譯文檔裡這一行**現在**的譯文。長句會被拆到下一行，所以常見
      一個「原」後面跟好幾行「譯」。行內左右各有對話時，中間可能夾著一點 AA 圖形，請忽略。
    - `行號|⚠未翻：…` 譯文檔這一行還留著日文，可能漏抓或漏翻。
  - `images/`：每則貼文的譯文截圖，檔名＝貼文編號（`p012.png`；貼文太長時切成
    `p012-1.png`、`p012-2.png`）。對照表的「=== 貼文 012 ===」就對應 `p012*.png`。

## 怎麼用截圖

日文常省略主詞，光看文字判斷不出「你／我／他／她」時，打開那則貼文的截圖，
看是誰在說話（對話框旁邊的角色）、在對誰說。不需要每則都看，有疑問再看。
截圖也能確認句子有沒有被截斷、有沒有日文沒翻到。

## 要抓的錯誤（依重要性）

1. 誤譯：意思和原文不符、看錯主詞或對象（你我他、他她）。
2. 漏譯：原文有、譯文沒有；`⚠未翻` 的行；原文句子被切斷導致譯文不完整。
3. 譯名不一致：和術語表或同一話其他地方的譯名不同。
4. 錯字、漏字、明顯不通順到會讓讀者看不懂的句子。

## 不要改

- 可改可不改的用詞、語氣、風格偏好。只報你有把握是錯的地方。
- 角色口癖的譯法（例如句尾的「夫」「だお」對應寫法），照譯者原本的處理。
- AA 圖形、空白、對話框邊框。

## 輸出：`校稿結果.txt`

寫在這份說明同一層。每一筆兩行：

```
# 檔名 第N行：理由（這一行不可以含有等號）
校稿前=校稿後
```

規則：
- `校稿前` 必須**一字不差**地從對照表的「譯：」內容複製，且只能在同一行內（不可跨行）。
- `校稿前` 要長到在那一話裡只出現一次（通常 6 字以上），不要只寫「你」「他」這種短字。
- `校稿前`、`校稿後` 都不可以含有 `=` 或 `＝`。
- `校稿後` 的長度盡量和 `校稿前` 接近，太長會撐破對話框。
- 依檔案、行號順序排列。沒有要改的話就不要寫。
"""
