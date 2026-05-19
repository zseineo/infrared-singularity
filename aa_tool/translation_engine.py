import re
import unicodedata

from .constants import BORDER_CHARS, DEFAULT_SYMBOL_REGEX
from .text_extraction import _BIDI_CONTROL_RE, _local_aa_density, aa_noise_ratio


def _disp_width(text: str) -> int:
    """字串在 MS PGothic 等寬 AA 基準下的顯示寬度（半形=1、全形=2）。

    補空白對齊右側 AA 圖時必須以「顯示寬度」而非「字元數」計算 —— AA 圖
    周圍常混用半形空白(寬1)與全形空白(寬2)，半形片假名、ASCII 也是寬1；
    以字元數計會在這些字元上累積誤差，使右側 AA 圖填得歪掉。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1
               for c in text)


def _make_pad(width: int) -> str:
    """組出剛好 `width` 個半形寬度的填白：全形空白(2) ＋ 視需要一個半形空白(1)。"""
    if width <= 0:
        return ''
    return '　' * (width // 2) + (' ' if width % 2 else '')


# 用 backtick 作為「保留外圍空白」的分隔符。選 backtick 是因為：
#   1) 鍵盤可直接打（左上角／Shift+@）
#   2) CJK 內文與一般日中翻譯內容幾乎不會出現
#   3) 與雙引號 `"` 比，內文中誤觸機率更低（少數作品會用半形 `"`）
_GLOSSARY_QUOTE = '`'

# 拆分標記：術語行中的 \X（X 為任意字元）表示「以 X 為分隔符拆成多條子條目」。
# 例：ライザリン\・シュタウト=萊莎琳\・斯托特 → ライザリン=萊莎琳、シュタウト=斯托特
_SPLIT_MARKER_RE = re.compile(r'\\(.)')


def decode_glossary_term(s: str) -> str:
    """解析術語表的單一 key 或 value。

    - 預設：剝除外圍空白（兼容舊行為；例如 `Hello = World` 會被解為 `Hello` 與 `World`）。
    - 若用 backtick 包覆（`` `...` ``），其內的空白完全保留，backtick 本身會被剝除。
      用於需要把前後空白也納入比對/取代的場合，例：

        ``` ` は？`=` 蛤？` ```        → key=` は？`, value=` 蛤？`
        ``` ` Trooper `=Trooper ```   → key=` Trooper `, value=`Trooper`

    - 內部空白不論有沒有包 backtick 都會保留（`Hello World=哈囉 世界` 一直都正常）。
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == _GLOSSARY_QUOTE and s[-1] == _GLOSSARY_QUOTE:
        return s[1:-1]
    return s


def encode_glossary_term(s: str) -> str:
    """把術語 key 或 value 寫回術語表字串時使用。

    若字串外圍有空白（或為空字串），用 backtick 包覆以利下次正確解析回原值；
    否則原樣回傳，避免不必要的視覺雜訊。
    """
    if s == '' or s != s.strip():
        return f'{_GLOSSARY_QUOTE}{s}{_GLOSSARY_QUOTE}'
    return s


def expand_glossary_entry(key: str, value: str) -> list[tuple[str, str]]:
    """若 key 含 \\X 標記，以 X 為分隔符將 key/value 各自切割並配對成多條子條目。

    兩側切割數量必須相等，否則視為普通單條條目回傳。
    例：('ライザリン\\・シュタウト', '萊莎琳\\・斯托特')
        → [('ライザリン', '萊莎琳'), ('シュタウト', '斯托特')]
    """
    m = _SPLIT_MARKER_RE.search(key)
    if not m:
        return [(key, value)]
    marker = '\\' + m.group(1)
    key_parts = [p.strip() for p in key.split(marker)]
    val_parts = [p.strip() for p in value.split(marker)]
    if len(key_parts) != len(val_parts) or len(key_parts) < 2:
        return [(key, value)]
    return [(k, v) for k, v in zip(key_parts, val_parts) if k]


def parse_glossary(glossary_str: str) -> dict[str, str]:
    """將 '日文=中文' 格式的術語表字串解析為 dict。

    Key 與 value 透過 `decode_glossary_term` 處理：預設剝外圍空白，
    若用 `"..."` 包覆則完整保留外圍空白（內部空白一律保留）。
    """
    glossary: dict[str, str] = {}
    for line in glossary_str.split('\n'):
        parts = line.split('=', 1)
        if len(parts) == 2:
            key = decode_glossary_term(parts[0])
            if key:
                val = decode_glossary_term(parts[1])
                for k, v in expand_glossary_entry(key, val):
                    if k:
                        glossary[k] = v
    return glossary


# 「右側 AA 圖偵測」判定門檻：被替換原文右側剩餘字串（已剝掉緊接的收尾括號與
# 空白）去空白後，AA 噪聲字元佔比 >= 此值即視為「右側擋著 AA 圖」需要補空白
# （沿用實驗提取演算法的 AA 噪聲定義，見 text_extraction.aa_noise_ratio）。
_RIGHT_AA_NOISE_THRESHOLD = 0.34
# 緊接在被替換文字之後的「對話／括號收尾」字元 — 屬於文字本身的尾巴而非右側
# 的 AA 圖（否則「　　「こんにちは」」這種以 `」` 結尾的對話行會被誤判）。
_TRAILING_CLOSERS = set('」』）)】〕〉》｝}］]')

# 純橫線字元（分隔線／表格線）。一整段非空白內容若只由這些字元組成，是排版
# 用的分隔線而非 AA 圖，不應觸發右側補空白（例：`─── `）。
_HORIZONTAL_RULE_CHARS = set('─━═╌╍┄┅┈┉╴╶╾╼-－‐‑‒–—―')


def _right_side_has_aa(
    rest: str,
    pad_right_aa: bool,
    symbol_regex: 're.Pattern | None',
) -> bool:
    """判斷某行被替換原文右側的剩餘字串 `rest` 是否「擋著 AA 圖」需要補空白。

    - 一律：`rest` 含對話框邊框字元（`BORDER_CHARS`）→ True（向後相容既有行為，
      無論實驗開關是否開啟）。
    - 額外（`pad_right_aa=True` 且提供 `symbol_regex` 時）：先跳過緊接的收尾
      括號（`_TRAILING_CLOSERS`）與空白，剩下的內容以 `aa_noise_ratio` 評估，
      去空白後 AA 噪聲比例 >= `_RIGHT_AA_NOISE_THRESHOLD` → True。
    """
    if any(c in rest for c in BORDER_CHARS):
        return True
    if not pad_right_aa or symbol_regex is None:
        return False
    i = 0
    n = len(rest)
    while i < n and (rest[i] in _TRAILING_CLOSERS or rest[i] in (' ', '　')):
        i += 1
    tail = rest[i:]
    if not tail:
        return False
    # 純橫線分隔線（如 `─── `）不算 AA 圖：去空白後若只剩橫線字元，不補空白。
    tail_nospace = [c for c in tail if c not in (' ', '　')]
    if tail_nospace and all(c in _HORIZONTAL_RULE_CHARS for c in tail_nospace):
        return False
    return aa_noise_ratio(tail, symbol_regex) >= _RIGHT_AA_NOISE_THRESHOLD


def _replace_with_padding(
    line: str,
    original: str,
    translated: str,
    padded_translated: str,
    pad_right_aa: bool = False,
    symbol_regex: 're.Pattern | None' = None,
    width_diff: int = 0,
) -> str:
    """在一行中執行替換，依右側是否擋著 AA 圖判斷補空白或消空白。

    `width_diff` 為「原文顯示寬度 − 譯文顯示寬度」（半形=1、全形=2）：
    - `width_diff > 0`（譯文較窄）：右側擋著 AA 圖時，於譯文後補等寬空白
      （用 `padded_translated`），把右側 AA 圖推回原欄位。
    - `width_diff < 0`（譯文較寬，且 `pad_right_aa=True`）：右側擋著 AA 圖時，
      跳過並保留緊接的收尾括號後，依「顯示寬度」吃掉右側空白共 `-width_diff`
      寬；若最後吃到全形空白而吃過頭，補回多吃的半形寬度，使 AA 圖維持原欄位。
    """
    if original not in line:
        return line
    try:
        pattern = re.compile(re.escape(original))
        result: list[str] = []
        pos = 0
        for m in pattern.finditer(line):
            result.append(line[pos:m.start()])
            rest = line[m.end():]
            has_aa = _right_side_has_aa(rest, pad_right_aa, symbol_regex)
            if has_aa and width_diff > 0:
                # 譯文較窄：補等寬空白
                result.append(padded_translated)
                pos = m.end()
            elif has_aa and width_diff < 0 and pad_right_aa:
                # 譯文較寬：依顯示寬度吃掉右側空白（實驗性，僅 pad_right_aa 啟用時）
                result.append(translated)
                k = m.end()
                # 先保留緊接的收尾括號（屬於文字尾巴，不可吃）
                while k < len(line) and line[k] in _TRAILING_CLOSERS:
                    k += 1
                result.append(line[m.end():k])
                # 依顯示寬度吃掉右側空白共 -width_diff 寬（全形記 2、半形記 1）
                want = -width_diff
                eaten = 0
                while eaten < want and k < len(line) and line[k] in ('　', ' '):
                    eaten += 2 if line[k] == '　' else 1
                    k += 1
                # 吃過頭（最後吃到全形空白）→ 補回多吃的半形寬度
                if eaten > want:
                    result.append(_make_pad(eaten - want))
                pos = k
            else:
                result.append(translated)
                pos = m.end()
        result.append(line[pos:])
        return ''.join(result)
    except Exception:
        return line.replace(original, translated)


# 全形＋半形片假名（含長音 ー）；用於 glossary_avoid_aa 的「緊鄰片假名」判定。
_KATAKANA_RE = re.compile(r'[ァ-ヺ・ー゠ｦ-ﾟ]')
# 片假名形式的敬稱 — 名字後接這些時不算「誤切更長片假名詞」，不應排除套用。
_KATAKANA_HONORIFICS = ('サン', 'チャン', 'クン', 'サマ', 'タン', 'ニキ', 'ネキ')
# glossary_avoid_aa：命中位置左右視窗 AA 噪聲密度 >= 此值 → 視為落在 AA 圖上。
_GLOSSARY_AA_DENSITY_TH = 0.5


def _glossary_hit_on_aa(line: str, start: int, end: int, key: str,
                        symbol_regex: 're.Pattern') -> bool:
    """判斷術語表的這次命中位置是否疑似落在 AA 圖上（experimental）。

    規則 A — 周圍 AA 噪聲密度：命中位置左右視窗（`_local_aa_density`）的 AA
      噪聲密度 >= `_GLOSSARY_AA_DENSITY_TH` → 視為 AA 圖（例：`::::アム::::`
      中的 `アム`，周圍全是 `:` 等 AA 字元）。
    規則 B — 緊鄰片假名：`key` 本身含片假名，且命中後緊鄰（前一字或後一字）
      仍是片假名 → 疑似把一個更長的片假名詞硬切成術語碎片。但後方緊接片假名
      敬稱（`サン` 等）時排除（屬正常的「名字＋敬稱」）。
    """
    if _local_aa_density(line, start, end, symbol_regex) >= _GLOSSARY_AA_DENSITY_TH:
        return True
    if _KATAKANA_RE.search(key):
        before = line[start - 1] if start > 0 else ''
        after = line[end] if end < len(line) else ''
        if before and _KATAKANA_RE.match(before):
            return True
        if after and _KATAKANA_RE.match(after):
            tail = line[end:end + 4]
            if not any(tail.startswith(h) for h in _KATAKANA_HONORIFICS):
                return True
    return False


def apply_glossary_to_text(text: str, glossary: dict[str, str], *,
                           avoid_aa: bool = False,
                           symbol_regex: 're.Pattern | None' = None) -> str:
    """對任意文本套用術語表（含 Auto-Padding 與邊框判定）。

    使用單輪掃描：把所有術語組成一條交替正則（長度遞減），
    re.sub 一次跑完，避免多輪替換時「短 LHS 命中長 LHS 替換結果」
    造成後續規則覆蓋前面成果的問題。

    `avoid_aa=True`（實驗性）時，略過 `_glossary_hit_on_aa` 判定為「疑似落在
    AA 圖上」的命中，保留原文不替換；`symbol_regex` 供該判定使用，None 時退回
    `DEFAULT_SYMBOL_REGEX`。
    """
    if not glossary:
        return text

    sorted_items = sorted(glossary.items(), key=lambda x: len(x[0]), reverse=True)
    term_map = {k: v for k, v in sorted_items}
    pattern = re.compile('|'.join(re.escape(k) for k, _ in sorted_items))

    sym: 're.Pattern | None' = None
    if avoid_aa:
        sym = symbol_regex or re.compile(DEFAULT_SYMBOL_REGEX)

    lines = text.split('\n')
    for i, line in enumerate(lines):
        def repl(m, _line=line):
            jp = m.group(0)
            tw = term_map[jp]
            if avoid_aa and _glossary_hit_on_aa(
                    _line, m.start(), m.end(), jp, sym):
                return jp  # 疑似 AA 圖，保留原文不套用
            len_diff = len(jp) - len(tw)
            rest = _line[m.end():]
            if len_diff > 0 and any(c in rest for c in BORDER_CHARS):
                return tw + ('　' * len_diff)
            return tw
        lines[i] = pattern.sub(repl, line)

    return '\n'.join(lines)


def apply_reverse_glossary_to_text(text: str, glossary: dict[str, str]) -> str:
    """對任意文本套用反向術語表：把「替代文字」還原為「原文」。

    與 apply_glossary_to_text 互為反向操作。為抵銷 Auto-Padding 的全形空白，
    匹配後若緊接 '\u3000' 會吃掉最多 (len(原文) - len(替代文字)) 個。

    同一個替代文字若對應多個原文（反向 map 會衝突），以最長原文為優先。
    """
    if not glossary:
        return text

    # 建立反向 map：repl -> orig；衝突時以最長 orig 為優先
    reverse_map: dict[str, str] = {}
    for orig, repl in glossary.items():
        if not repl:
            continue
        if repl not in reverse_map or len(orig) > len(reverse_map[repl]):
            reverse_map[repl] = orig

    if not reverse_map:
        return text

    sorted_items = sorted(reverse_map.items(), key=lambda x: len(x[0]), reverse=True)
    pattern = re.compile('|'.join(re.escape(k) for k, _ in sorted_items))

    lines = text.split('\n')
    for i, line in enumerate(lines):
        result_parts: list[str] = []
        pos = 0
        for m in pattern.finditer(line):
            result_parts.append(line[pos:m.start()])
            repl = m.group(0)
            orig = reverse_map[repl]
            len_diff = len(orig) - len(repl)
            # 吃掉尾端最多 len_diff 個全形空白（抵銷 Auto-Padding）
            eat = 0
            end = m.end()
            if len_diff > 0:
                while eat < len_diff and end + eat < len(line) and line[end + eat] == '\u3000':
                    eat += 1
            result_parts.append(orig)
            pos = end + eat
        result_parts.append(line[pos:])
        lines[i] = ''.join(result_parts)

    return '\n'.join(lines)


def apply_translation(
    source: str,
    extracted: str,
    translated: str,
    glossary: dict[str, str],
    append_mode: bool = False,
    translation_only: bool = False,
    pad_right_aa: bool = False,
    symbol_regex_str: 'str | None' = None,
    glossary_avoid_aa: bool = False,
) -> str:
    """執行翻譯替換：將提取的原文替換為翻譯文，套用術語表並自動補全形空白。

    Args:
        source: 帶有 AA 圖的原始全文
        extracted: 提取結果（'ID|原文' 格式，每行一條）
        translated: AI 翻譯結果（'ID|翻譯文' 格式，每行一條）
        glossary: 術語表 dict
        append_mode: 為 True 時，將翻譯文「附加在原文之後」而非取代原文。
            此模式下不補全形空白（替換結果一定比原文長）。
        translation_only: 為 True 時，術語表「只套用於譯文部分」；
            跳過最後對整份 source 的全域 glossary 覆蓋（AA 圖等未提取區域不變）。
        pad_right_aa: 實驗性。為 True 時，除了「右側有對話框邊框字元」之外，
            「右側剩餘內容像 AA 圖」（依 `symbol_regex_str` 與 `aa_noise_ratio`
            判定，門檻見 `_RIGHT_AA_NOISE_THRESHOLD`）也會在譯文較短時補等量
            全形空白，盡量讓右側 AA 圖維持原位置。
        symbol_regex_str: AA 符號正則字串（通常為主程式的 `symbol_regex`）；
            在 `pad_right_aa=True` 或 `glossary_avoid_aa=True` 時使用，
            None / 編譯失敗時退回 `DEFAULT_SYMBOL_REGEX`。
        glossary_avoid_aa: 實驗性。為 True 時，全域術語覆蓋階段會略過「疑似落在
            AA 圖上」的命中（見 `_glossary_hit_on_aa`），避免術語表把 AA 圖中
            剛好等於某術語 key 的片假名碎片誤替換掉。

    Returns:
        替換後的完整文本
    """
    # AA 圖偵測用的 symbol regex（pad_right_aa 或 glossary_avoid_aa 啟用時編譯）
    symbol_regex: 're.Pattern | None' = None
    if pad_right_aa or glossary_avoid_aa:
        try:
            symbol_regex = re.compile(symbol_regex_str or DEFAULT_SYMBOL_REGEX)
        except re.error:
            symbol_regex = re.compile(DEFAULT_SYMBOL_REGEX)
    # 提取階段會整份移除 Unicode 雙向控制字元，故 `extracted` 的原文不含這些
    # 隱形字元；但 `source` 仍保有它們，會夾在句中（如 `…かっ‬た。`）使
    # `original not in line` 失配而無法替換。替換前先把 source 同步清除。
    source = _BIDI_CONTROL_RE.sub('', source)

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

    # 長度優先排序（最長原文先替換，防止短字串先命中造成巢狀問題）
    valid_ids = [k for k in trans_map.keys() if k in orig_map]
    valid_ids.sort(key=lambda k: len(orig_map[k]), reverse=True)

    # 每行的最後一個 segment 序號：同一行有多句被翻譯時，只有最後一段的右側
    # 才可能是 AA 圖，非最後段右側緊接的是同行其他翻譯句，補空白只會在句子
    # 之間塞入多餘全形空白。由 orig_map（所有提取段）算出每行最大 segment。
    last_seg_of_line: dict[int, int] = {}
    for k in orig_map:
        try:
            _ln_s, _sg_s = k.split('-', 1)
            _ln_i, _sg_i = int(_ln_s), int(_sg_s)
        except (ValueError, IndexError):
            continue
        if _sg_i > last_seg_of_line.get(_ln_i, -1):
            last_seg_of_line[_ln_i] = _sg_i

    sorted_glossary = sorted(glossary.items(), key=lambda x: len(x[0]), reverse=True)

    # 預先切割行（整個替換過程只切割一次）
    source_lines = source.split('\n')

    # 逐條替換，嚴格限定在 ID 指定的行號
    for _id in valid_ids:
        trans_text = trans_map[_id]
        original = orig_map[_id]
        final_translated = trans_text

        # 對翻譯文套用術語表
        for jp_term, tw_term in sorted_glossary:
            final_translated = final_translated.replace(jp_term, tw_term)

        if append_mode:
            # 附加模式：以「原文 + 半形空白 + 翻譯文」取代原文位置；不補/不消空白
            final_translated = original + ' ' + final_translated
            padded = final_translated
            width_diff = 0
        else:
            # 以「顯示寬度」(半形=1/全形=2) 而非字元數計算差距，否則 AA 圖
            # 周圍的半形空白／半形假名會造成累積誤差、右側 AA 填得歪掉。
            width_diff = _disp_width(original) - _disp_width(final_translated)
            padded = final_translated + (
                _make_pad(width_diff) if width_diff > 0 else '')
            # 非該行最後一段：右側是同行的其他翻譯句而非 AA 圖，一律不補/不消
            # 空白，避免在句子之間塞入多餘全形空白（只有最後一段需要對齊 AA）。
            try:
                _ln_s, _sg_s = _id.split('-', 1)
                if last_seg_of_line.get(int(_ln_s), -1) != int(_sg_s):
                    width_diff = 0
                    padded = final_translated
            except (ValueError, IndexError):
                pass

        # 從 ID 解析行號（格式 NNN-S，NNN 為 1-indexed 行號）
        try:
            line_idx = int(_id.split('-')[0]) - 1
        except (ValueError, IndexError):
            line_idx = -1

        if 0 <= line_idx < len(source_lines):
            source_lines[line_idx] = _replace_with_padding(
                source_lines[line_idx], original, final_translated, padded,
                pad_right_aa, symbol_regex, width_diff,
            )
        else:
            # ID 無法解析時退回全行掃描（保護性 fallback）
            for i in range(len(source_lines)):
                source_lines[i] = _replace_with_padding(
                    source_lines[i], original, final_translated, padded,
                    pad_right_aa, symbol_regex, width_diff,
                )

    source = '\n'.join(source_lines)

    # 全域術語覆蓋：未被提取的原文部分也套用術語表
    # translation_only=True 時跳過此步，僅譯文部分套用術語表
    if not translation_only:
        source = apply_glossary_to_text(
            source, glossary,
            avoid_aa=glossary_avoid_aa, symbol_regex=symbol_regex)

    return source
