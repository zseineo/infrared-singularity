import re

from .constants import DEFAULT_BASE_REGEX, DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX


def _compile_regex(pattern: str, default: str) -> re.Pattern:
    """嘗試編譯正則，失敗時使用預設值。"""
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(default)


def _compile_custom_filters(filter_str: str) -> list[re.Pattern]:
    """將使用者輸入的過濾規則（每行一條正則）編譯為 list。"""
    regexes = []
    for line in filter_str.split('\n'):
        line = line.strip()
        if line:
            try:
                regexes.append(re.compile(line))
            except re.error:
                pass
    return regexes


def _postprocess_text(text: str, korean_mode: bool = False,
                      experimental: bool = False) -> str:
    """提取後的字串後處理（去除非內文字元）。

    - 一般（日文）模式：移除開頭非平假名/片假名/漢字/英文/數字的部分。
    - 韓文模式：跳過該開頭剝除步驟（Hangul 不在白名單內，會被誤判為非內文）。
    - **實驗性模式（experimental=True）**：也跳過該步驟 — anchor + boundary
      expansion 已精準定位候選邊界，不再用「剝除非內文開頭」做兜底；保留
      邊界擴展選到的字元（含 `（` `,` `.` 等），讓 `_complete_brackets` 等
      後處理直接作用於原始候選。
    """
    # 若被邊框符號 │ 或 | 包起來，則只取邊框內的文字
    match = re.search(r'[│\|](.*)[│\|]', text)
    if match:
        text = match.group(1).strip(' \t　.')

    if not korean_mode and not experimental:
        # 移除開頭非平假名、片假名、漢字、英文、數字的部分。
        # 韓文模式跳過此步驟，否則 Hangul 不在白名單內會把整段砍掉。
        text = re.sub(
        r'^([^\u2190-\u2193\u25CB\u3007\u2026\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFa-zA-ZＡ-Ｚａ-ｚ0-9０-９]+)',
        '', text
    ).strip()

    # 移除開頭的「(可選 AA noise 標點)+數字＋點」格式（全形半形皆可）。
    # 容許 `[、,，.．]*` 前綴是為了實驗性模式（無 strip-head 兜底）下，
    # 邊界擴展把 AA 圖中的 `.２．` `..４．` `、..８．` 等 AA-flavored
    # 前綴吃進候選；這裡一併剝除才能還原成乾淨的 `２．→ ""` 結果。
    text = re.sub(r'^[、,，.．]*[0-9０-９]+[.．]', '', text).strip()
    # 若句尾包含特定符號，則將其刪除
    text = text.rstrip('|｜(（').strip()

    return text


# Unicode 雙向控制 / 隱形格式字元（LRM/RLM/LRE/RLE/PDF/LRO/RLO/LRI/RLI/FSI/PDI）。
# 部分來源（如某些網站複製貼上）會夾帶這些不可見字元，夾在句中時會把候選
# 邊界擴展硬生生切斷（例：`…特化してい‬る。` 中的 U+202C 把 `る。` 隔開）。
# 提取前一律整份移除，不影響可見文字。
_BIDI_CONTROL_RE = re.compile('[‎‏‪-‮⁦-⁩]')

# 2ch/5ch 發文者行：形如「4402 ： ◆GESU1/dEaE ： 2021/05/06(木) 23:19:36 ID:nGcM5Umt」
# 特徵：含 ID:xxxxxx（trip code），這組標記對發文者行幾乎是唯一判別
_POSTER_LINE_RE = re.compile(r'ID:[A-Za-z0-9+/.]{6,}')

# 骰子格式 【NDN:N】（如 【1D10:10】）：句中出現時，內部的半形 `:` 不在
# DEFAULT_BASE_REGEX 字元集中，會把整句切成兩段提取。
# 解法：提取前先把骰子內的 `:` 改成全形 `：`（base regex 字元集已含 `：`），
# 提取後再 `_DICE_NOTATION_FW_RE` 把它換回半形，使最終輸出維持原始的半形冒號。
_DICE_NOTATION_RE = re.compile(r'(【\d+D\d+):(\d+】)')
_DICE_NOTATION_FW_RE = re.compile(r'(【\d+D\d+)：(\d+】)')


# ────────────────────────────────────────────────────────────────────────────
# 實驗性提取演算法（experimental=True）
#
# 核心想法：
#   現有 chunk-by-2-spaces + base_regex 對「AA 圖中夾雜少量合法字元」的情境
#   過於寬鬆（例：`,ｘf〔 ア亥ｱｱ,､rf7父く三}h` 會被切成一個 chunk，
#   AA 符號比例 < 50%，base_regex 抓出 `rf7父く三`、`ムV心` 等碎片）。
#
# 新演算法：
#   1. 錨點掃描：找「≥2 連續平/片假名」或「漢字 + 助詞」（真實日文文本
#      幾乎必然出現，AA 圖幾乎不會湊出的特徵）
#   2. 邊界擴展：從錨點向兩側貪心擴張到 valid char set，碰 AA 噪聲字元
#      / 連續空白即停
#   3. 分數過濾：對 candidate 算「日文文本可能性分數」
#        +3 含 ≥2 連續假名
#        +2 含日文助詞
#        +1 長度 ≥ 5
#        -4 局部 AA 噪聲密度（左右各 8 字元視窗）> 0.6
#        -2 字元類別跳轉率 > 0.5
#      門檻：score >= 2
#
# 韓文模式不走此路徑（韓文無假名錨點概念）。
# ────────────────────────────────────────────────────────────────────────────

# 假名疊字標記：ゝ(U+309D) ゞ(U+309E) ヽ(U+30FD) ヾ(U+30FE)。雖屬合法假名範圍，
# 但 AA 圖中常被借作裝飾元素（如「ゝゝゝ」「ヾミミ」），作為 anchor 觸發信號太弱。
# 解法：anchor regex 排除這四個字元；但 `_VALID_CHAR_RE` 仍保留它們（合法日文
# 邊界擴展時不應切斷，例如「く＼，」式的疊字寫法）。
# 連續 ≥2 平假名 anchor（平假名出現於 AA 圖機率低，2 char 仍是強信號）。
# 允許 `ー`（U+30FC 長音符號）、`～`(U+FF5E)、`〜`(U+301C) 夾於平假名之間，
# 捕捉「だーかーらー」「で～も～ね～」這類拉長口吻。排除 ゝゞ 疊字標記。
# **要求 ≥2 個真平假名**（首尾皆為真平假名，`ー～〜` 只能夾在中間不計數）—
# 避免「へーー三」這類「1 個真平假名 + 多個長音符號」被誤判為平假名 run。
_HIRAGANA_RUN_RE = re.compile(
    r'[ぁ-゜ゟ][ぁ-゜ゟー～〜]*[ぁ-゜ゟ]')
# 連續 ≥3 片假名 anchor（片假名常出現於 AA 圖作為視覺元素，2 char run 如
# `トミ`、`ニヽ`、`トー` 在 AA 中假陽性多；真實日文片假名詞幾乎都 ≥3 char：
# `コスト`、`ヒーロー`、`コンピューター`、`チョロ`、`カット`）。需要 2-char
# 片假名片段時可改由 `_SHORT_UTT_RE`（含日文句末標點）涵蓋。
# 排除 ヽヾ 疊字標記（保留 ー 長音符號 U+30FC）。
_KATAKANA_RUN_RE = re.compile(r'[゠-ーヿ]{3,}')
# 漢字 + 平假名 anchor（廣義 kanji+okurigana 模式）：捕捉「物が」「奪う」「私だ」
# 「行く」「歳ね」「だと」這類動詞活用／助詞／形容詞活用。漢字部分允許 `々`。
# **排除 ゝゞ 疊字標記**（與 `_HIRAGANA_RUN_RE` 一致）— 「佳三ゞ」這類片段
# 包含 ゞ 但不該被視為合法 kanji+okurigana 模式。
_KANJI_HIRA_RE = re.compile(
    r'[一-鿿々][ぁ-゜ゟ]')
# 送り仮名 anchor：漢字 + 1 個平假名 + 漢字（如「張れ御」「歩いて行」），
# 涵蓋「頑張れ御貴族様」這類「動詞活用形 + 後接漢字」的常見構造。
# 中間平假名同樣排除 ゝゞ 疊字標記。
_OKURIGANA_RE = re.compile(
    r'[一-鿿々][ぁ-゜ゟ][一-鿿々]')
# 片假名 + 日文助詞 anchor：捕捉「ダメで」「メだ」「ノで」「タの」這類
# 「片假名單字+助詞」的對話模式（如「ダメで元々」「ダメだ」「ノルで」）。
# AA 圖極少湊出「kata+ 真正助詞」組合（AA 多用 kata+小字假名/標點 ぃぅゝヾ
# 等裝飾），假陽性風險低。第二字僅限 `_PARTICLES` 集合，更具區辨力。
_KATA_PARTICLE_RE = re.compile(
    r'[゠-ーヿ][のはをにがでとへやかもだねよわ]')
# 漢字字元集（用於「短候選含漢字 = 強短信號」判斷）
_KANJI_RE = re.compile(r'[一-鿿々]')

# 對話框邊框字元（左/右）：用於 `_is_dialogue_box_bounded` 偵測候選是否
# 位於 ｜…| / ＜…│ / │…＞ 等對話框內。命中即列入 strong_jp（免疫密度懲罰）
# 並 +2 分，讓對話框內的合法日文不會因為鄰近邊框導致密度過高被誤殺。
_DIALOGUE_LEFT_MARKERS = set('＜<│|｜┃')
_DIALOGUE_RIGHT_MARKERS = set('＞>│|｜┃')

# 名牌（speaker tag）前綴：候選前方為「【說話者】」名牌（後可接破折號連接號），
# 是強烈的對話信號（如「【響】───だってさ」）。命中時 +2 並列入 strong_jp，
# 讓名牌後的對話不因破折號 `───` 被計入局部 AA 密度而誤殺。
_NAME_TAG_PREFIX_RE = re.compile(
    r'【[^【】]{1,16}】[\s　─━―ー\-‐—–−〜~]*$')

# 半形片假名 — AA 圖最大宗的噪聲來源
_HALFWIDTH_KANA_CHARS = set(chr(c) for c in range(0xFF66, 0xFFA0))
# 額外的 AA 慣用標點（base_regex 字元集外但常出現於 AA 圖）。
# 注意：不可放入 `々`（漢字疊字標記，常見於「後々／人々／日々」等合法日文）、
# 不可放入 `〆`（締め，合法日文用法）；放入會把它們視為邊界，
# 導致 `主導権を取り返せないから、後々響くんですよ` 這類句子在 `々` 處被切斷。
_AA_PUNCT_CHARS = set('〔〕㌻㍉〟〝丶ヽヾ')

# 日文句末標點（用於 anchor 與 scoring）
_JP_END_PUNCT = set('！？。、…')

# 短句 anchor：假名 run + 日文句末標點。分兩種強度：
# - **強標點**（**全形** `！？`）：≥1 假名（含 ≥1 平假名）即可，如「え！？」「ま！」。
# - **弱標點**（`、。…`）：需 ≥2 假名（含 ≥1 平假名），如「いや、」「はあ…」。
#   排除「へ、」這類「單 1 平假名 + 弱標點」的 AA 易混淆模式。
# **半形 `!?` 不算強標點**：AA 圖中半形驚嘆/問號常被借作裝飾（如「く!」「ンへ!」
# 「く!.」），不可單憑半形 `!` 把短碎片當成短句 anchor。半形 `!?` 在 scoring
# 仍給較低 bonus（見 `_score_candidate`）。
# lookahead 確保整段 kana run 含 ≥1 平假名（排除純片假名短語如「メヘ、」與
# ゝゞ 疊字標記）。
_SHORT_UTT_RE = re.compile(
    r'(?=[ぁ-゜ゟ゠-ーヿ]*[ぁ-゜ゟ])'
    r'(?:[ぁ-゜ゟ゠-ーヿ]+[…]*[！？]'
    r'|[ぁ-゜ゟ゠-ーヿ]{2,}[、。…])')

# 純片假名短句 anchor：≥2 片假名 + 全形 `！？`（如「クソ！」「ヤダ！」）。
# `_SHORT_UTT_RE` 因 lookahead 要求含真平假名而排除純片假名短句，但括號內的
# 連續喊叫（如「（クソ！　クソ！　クソッタレッ！！）」）應被逐段提取。
# 為避免放掉 AA 圖中的純片假名碎片：恰好 2 片假名 + 標點的最短型（`_SHORT_KATA_UTT_RE`）
# 須位於括號區間內才保留（見 `_extract_experimental_line` 的 bracket 閘）。
_KATA_UTT_RE = re.compile(r'[゠-ーヿ]{2,}[！？]')
_SHORT_KATA_UTT_RE = re.compile(r'^[゠-ーヿ]{2}[！？]+$')

# 平假名 + 真片假名（非 ー）+ 平假名 — 「こンっ」這類片假名單字夾在平假名間。
# 真實日文罕見此模式（片假名詞會整串寫，不會單字夾入平假名）；AA 圖則常見。
# `゠-ヺ`(U+30A0-30FA) 排除 `ー`(U+30FC 長音符號) — 避免「だーか」這類拉長口吻誤判。
_HIRA_KATA_HIRA_RE = re.compile(r'[ぁ-゜ゟ][゠-ヺ][ぁ-゜ゟ]')

# 漢字 + 句末標點 anchor：純漢字片語 + 日文標點（如「自分？」「諸君！」「認…」）。
# 純漢字短語在 AA 圖中極少出現（AA 用半形片假名／符號），通常是省略平假名的
# 對話寫法。此 anchor 補足 _KANJI_HIRA_RE 漏掉的「漢字直接接句末標點」情境。
_KANJI_END_PUNCT_RE = re.compile(r'[一-鿿々][！？。、…]')

# 數字 + 平假名計數詞 + 漢字 anchor：捕捉「1か月」「2つ目」「3か所」這類
# 「數字 + ひらがな助数詞 + 漢字」結構（か月 / つ目 / か所 等）。AA 圖中
# 「數字緊接平假名再接漢字」極為罕見（AA 多用 `1.:.:` 點線結構），假陽性風險低。
_COUNTER_RE = re.compile(r'[0-9０-９]+[ぁ-゜ゟ][一-鿿]')

# 片假名 stutter anchor：片假名（可帶 `、`）至少重複 3 次，捕捉「パ、パパ」
# 「マ、ママ」這類結巴／呼喊式對話。`_KATAKANA_RUN_RE` 已涵蓋無 `、` 的
# 3+ 連續片假名；此 anchor 補足 `、` 夾雜的情況。注意：全同字（如「ニ、ニニ」）
# 仍可能命中，但後續 score 過濾與密度檢查會擋下純 AA 裝飾。
_KATA_STUTTER_RE = re.compile(r'(?:[゠-ーヿ]、?){3,}')

# 「拉長念法」spaced-out anchor：CJK 字元 + 全形空白（**1~2 個**）至少重複 3 次，
# 最後再接 CJK 字元 + 可選的（空白 + 句末標點）。
# 限制 inter-char 空白為 1~2 個（`　{1,2}`）— 使用者觀察「一般對話空格不會超過
# 2 格」，3+ 連續空白應視為 chunk 邊界（AA 圖與對話的分隔），不該被拉長念法
# 偵測橋接。例如「ミ　　　　　何　　故　　脱　　ぐ」應只抓「何　　故　　脱　　ぐ」，
# 不該把孤立的「ミ」也拉進來。
# 例：「シ　ロ　ウ　殿　は　勇　者　で　す　な！」、
#     「な　ん　と　痛　ま　し　　い　事　件　な　の　で　し　ょ　う」、
#     「何　　故　　脱　　ぐ　　。」
_SPACED_OUT_RE = re.compile(
    r'(?:[ぁ-ゟ゠-ヿ一-鿿]　{1,2}){3,}[ぁ-ゟ゠-ヿ一-鿿]'
    r'(?:[　 ]*[！？。!?]+)?')

# 全形片假名整句（角色全程以片假名講話的特殊情況，如「ホウシシュゾク　ハ　イシガ
# 　ナイカギリ」）：≥2 個由「全形片假名／長音／中黑點／英字」構成的詞、以單一全形
# 空白分隔，可結尾 `。`。AA 圖裝飾多為**半形**片假名（ｦ-ﾟ）；整句**全形**片假名
# 加全形空白分詞，在 AA 圖中極罕見，是可信的對話信號。為避免「ニニ　ニニ」這類
# 重複裝飾，於 `_find_kata_sentence` 另要求全形片假名數 ≥5 且相異片假名 ≥3。
_KATA_SENTENCE_RE = re.compile(
    r'[A-Za-z゠-ヿ々]+(?:　[A-Za-z゠-ヿ々]+)+。?')

# 數字選項 anchor：數字（可帶範圍 `～`）+ **全形**「：」/「．」分隔。
# 例：「１：」、「１０：」、「１～３：」、「1．」。
# 用途：捕捉「１０：ご主人様」「１～３：ママぁ」這類短內容的選項列。
# 使用者觀察：「數字選項後通常不會接 AA 圖」，故對命中此 anchor 的候選給予較強信號。
# 限制分隔符為全形 `：．`（不含半形 `:.`）— AA 圖中半形 `:` 與 `.` 大量出現於
# `::::` 邊框與 `..:.:` 點線，會把 `0:i`、`7:.`、`ii7:` 這類碎片誤判為選項格式。
# 真實日文安価/投票格式幾乎都用全形分隔符。
_NUMBER_OPTION_RE = re.compile(
    r'[0-9０-９①-⑳]+(?:[～~][0-9０-９①-⑳]+)?[：．]')

# 數字範圍前綴：候選以「數字～數字」開頭、無分隔符（如「⑦～⑨出来る」）。
# 安価/投票格式偶爾以圈號數字（①-⑳）標多選項合併（⑦⑧⑨皆導向同一結果）。
# 命中時與 `_NUMBER_OPTION_RE` 同等視為數字選項信號（+3 並列入 strong_jp）。
_NUMBER_RANGE_RE = re.compile(
    r'^[0-9０-９①-⑳]+[～~][0-9０-９①-⑳]+')

# 連續 ≥3 個相同小字假名（ぁぃぅぇぉっゃゅょゎゕゖ）— AA 圖裝飾特徵（如「ぃぃぃ」）。
# 真實日文罕用 3+ 連續小字假名；普通字假名（如「ねええ」的 え）排除在此規則外。
_REPEAT_SMALL_HIRA_RE = re.compile(
    r'([ぁぃぅぇぉっゃゅょゎゕゖ])\1{2,}')

# AA 風格雜訊標點前綴：以 `、,，.．\-` 等「日文非起始字元」開頭（如「-へ!.」
# 「,..へ！-」「.込り!」）。真實日文對話幾乎不會以這些字元開頭（會用 ←、…、
# 全形括號或直接內容）；boundary expansion 把 AA 圖中的雜訊標點吃進候選時
# 是強 AA 信號。對命中此模式的候選 -4 分。
# 注意：實驗模式停用 strip-head（讓 AA flavor 留在輸出）後，這個前綴會出現
# 在候選裡 — 此規則就是用「前綴存在」當證據判斷候選是 AA 碎片並剔除。
_AA_PUNCT_PREFIX_RE = re.compile(r'^[、,，.．\-]+')

# AA 風格 latin 前綴：以半形 latin 字母（含大小寫）開頭、後接（可選）更多
# 字母/數字、最後接 CJK 字元（如「jニ」「j爻」「rf7父」「i7从V」「jI斗ぅ示」）。
# 真實日文句子幾乎不會以 latin 字母開頭直接接 CJK；AA 圖則常用此模式作裝飾。
# 即使是合法縮寫（如「LV5の魔法」「iPhone6を買った」），這類句子通常更長
# 且有更多日文信號（連續假名、助詞、長度 ≥ 8），即使被 -3 也能通過總分門檻；
# 短的 AA 碎片則會直接被擋下。
_AA_LATIN_PREFIX_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9]*[぀-ヿ一-鿿]')

# AA 風格小字片假名前綴：以 ァィゥェォャュョッヮヶ 開頭。
# 真實日文罕見以小字片假名開頭（通常作為前一個片假名的修飾，如「ティ」「ファ」）；
# AA 圖則常借小字片假名作裝飾起頭（如「ィｆ芥ぅ」「ィ7从V」）。
_AA_SMALL_KATA_PREFIX_RE = re.compile(r'^[ァィゥェォャュョッヮヶ]')

# AA 風格 latin 尾綴：CJK 字元後接孤立的 1-2 個半形字母（大小寫皆可）、
# 可再接一個尾標點（如「行灯な心マi」結尾的 `マi`、「怎うぅx,」結尾的 `ぅx,`）。
# 真實日文句子罕見以 CJK 後直接接半形 latin 結尾；AA 圖則常用此模式（單字母
# 當視覺裝飾）。對命中此模式的候選 -3 分。
_AA_LATIN_SUFFIX_RE = re.compile(r'[぀-ヿ一-鿿][a-zA-Z]{1,2}[、,，.．]?$')

# 連續 ≥3 個相同平假名 — AA 圖裝飾特徵（如「んんんんんん」「あああ」）。
# 真實日文擬聲詞如「あはは」「ふふふ」3 同字至多 3 個；若 ≥4 個連續，幾乎肯定是
# AA 裝飾。為兼顧「ふふふ」「あはは」這類笑聲，仍只罰 3+，但在 right_edge
# 時豁免（與 `_REPEAT_KATAKANA_RE` 一致）。
_REPEAT_HIRA_RE = re.compile(r'([ぁ-゜ゟ])\1{2,}')

# 邊界擴展用的 valid char set（與 base_regex 字元集對齊，但排除半形片假名與 AA 標點）
_VALID_CHAR_RE = re.compile(
    r'[：＋+a-zA-ZＡ-Ｚａ-ｚ0-9０-９①-⑳'
    r'぀-ゟ゠-ヿ一-鿿'
    r'々〆〤○〇★☆←→↑↓＆【】（）「」『』！？、。…，．？！,.\-―ーッ%％"“”‘’～〜]')

_PARTICLES = set('のはをにがでとへやかもだねよわ')

# 連續 ≥3 個相同片假名 — AA 圖邊框/裝飾的強烈特徵（如「ニニニ」「ンンン」）。
# 真實日文片假名詞幾乎不會有 3 個連續相同字元；少數擬聲詞如「ガガガ！」會
# 帶句末標點觸發 +2 分補償。
# 排除 `・`(U+30FB 中黑點)：「そんな・・・」這類三連中黑點是日文省略號變體（等價於 `…`），
# 屬合法日文標點而非 AA 裝飾，不應被罰分。
_REPEAT_KATAKANA_RE = re.compile(r'([゠-ヺー-ヿ])\1{2,}')


def _find_anchors(line: str) -> list[tuple[int, int]]:
    """掃描行內所有錨點 [start, end) 區間，依起點排序。"""
    anchors: list[tuple[int, int]] = []
    for m in _HIRAGANA_RUN_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _KATAKANA_RUN_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _KANJI_HIRA_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _OKURIGANA_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _SHORT_UTT_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _NUMBER_OPTION_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _KANJI_END_PUNCT_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _COUNTER_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _KATA_STUTTER_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _KATA_PARTICLE_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    for m in _KATA_UTT_RE.finditer(line):
        anchors.append((m.start(), m.end()))
    anchors.sort()
    return anchors


def _is_valid_char(ch: str) -> bool:
    """是否屬於可擴展的 valid char（非 AA 噪聲）。"""
    if ch in _HALFWIDTH_KANA_CHARS:
        return False
    if ch in _AA_PUNCT_CHARS:
        return False
    return bool(_VALID_CHAR_RE.match(ch))


def _expand_boundary(line: str, start: int, end: int) -> tuple[int, int]:
    """從錨點 [start, end) 向兩側擴張到非 valid char / 連續空白邊界。"""
    # 向左
    while start > 0:
        ch = line[start - 1]
        if ch in (' ', '　'):
            # 只要遇到空白就停（連續空白 / AA padding 邊界）
            break
        if not _is_valid_char(ch):
            break
        start -= 1
    # 向右
    while end < len(line):
        ch = line[end]
        if ch in (' ', '　'):
            break
        if not _is_valid_char(ch):
            break
        end += 1
    return start, end


def _merge_overlapping(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合併重疊或相鄰的區間。"""
    if not spans:
        return []
    spans = sorted(spans)
    merged: list[tuple[int, int]] = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = merged[-1]
        if s <= pe:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _local_aa_density(line: str, start: int, end: int,
                      symbol_regex: re.Pattern, window: int = 8) -> float:
    """計算 match 左右各 `window` 字元視窗內的 AA 噪聲密度。

    AA 噪聲 = 半形片假名 ∪ AA 慣用標點 ∪ symbol_regex 命中字元。
    **分母為「非空白字元數」** — AA 圖的 padding 大多為空白，若以總字元數為
    分母，會把噪聲密度稀釋（如 `:.:.:rﾏー-=彡` 周圍若混入大量空白）。改以
    非空白字元為分母，可正確反映「實際有實體字元的區域中 AA 噪聲佔比」。
    """
    left_start = max(0, start - window)
    right_end = min(len(line), end + window)
    context = line[left_start:start] + line[end:right_end]
    if not context:
        return 0.0
    noise = 0
    non_space = 0
    for ch in context:
        if ch in (' ', '　'):
            continue
        non_space += 1
        if (ch in _HALFWIDTH_KANA_CHARS
                or ch in _AA_PUNCT_CHARS
                or symbol_regex.match(ch)):
            noise += 1
    if non_space == 0:
        return 0.0
    return noise / non_space


def aa_noise_ratio(text: str, symbol_regex: re.Pattern) -> float:
    """`text` 中「AA 噪聲字元」佔「非空白字元」的比例（0.0 ~ 1.0）。

    AA 噪聲的定義與 `_local_aa_density` 一致：半形片假名 ∪ AA 慣用標點
    (`_AA_PUNCT_CHARS`) ∪ `symbol_regex` 命中字元。無非空白字元時回 0.0。

    用途：替換翻譯時判斷「某行被替換原文的右側殘留內容」是否像 AA 圖
    （見 `translation_engine._right_side_has_aa`）。
    """
    noise = 0
    non_space = 0
    for ch in text:
        if ch in (' ', '　'):
            continue
        non_space += 1
        if (ch in _HALFWIDTH_KANA_CHARS
                or ch in _AA_PUNCT_CHARS
                or symbol_regex.match(ch)):
            noise += 1
    return noise / non_space if non_space else 0.0


def _class_transitions(text: str) -> int:
    """字元類別跳轉次數（hira/kata/kanji/latin/digit/punct/other 之間）。"""
    def cls(ch: str) -> str:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F:
            return 'h'
        if 0x30A0 <= cp <= 0x30FF:
            return 'k'
        if 0x4E00 <= cp <= 0x9FFF:
            return 'c'
        if ('a' <= ch.lower() <= 'z') or 0xFF21 <= cp <= 0xFF5A:
            return 'l'
        if ch.isdigit():
            return 'd'
        return 'p'
    if len(text) < 2:
        return 0
    last = cls(text[0])
    n = 0
    for ch in text[1:]:
        c = cls(ch)
        if c != last:
            n += 1
        last = c
    return n


def _score_candidate(text: str, line: str, start: int, end: int,
                     symbol_regex: re.Pattern) -> int:
    s = 0
    has_run = bool(_HIRAGANA_RUN_RE.search(text)
                   or _KATAKANA_RUN_RE.search(text))
    if has_run:
        s += 3
    particle_count = sum(1 for ch in text if ch in _PARTICLES)
    if particle_count >= 1:
        s += 2
    if particle_count >= 2:
        s += 2  # 多重助詞 = 強烈日文對話信號（容忍局部 AA 噪聲）
    if len(text) >= 5:
        s += 1
    # 送り仮名 pattern（漢字+1平假名+漢字）— 動詞活用形 + 後接漢字的強日文信號，
    # 涵蓋「頑張れ御貴族様」「歩いて行く」這類 kanji-heavy、無連續假名、無格助詞的情況。
    # 給 +3 較高分數補償「不再列入 strong_jp」的影響（見 `strong_jp` 註解）。
    if _OKURIGANA_RE.search(text):
        s += 3
    # 漢字+平假名 pattern — 動詞活用 / 形容詞活用 / 名詞+助詞 的強日文信號。
    # 涵蓋「奪う」「私だ」「行く」這類短而標準的日文。AA 圖極少湊出此 pattern。
    elif _KANJI_HIRA_RE.search(text):
        s += 2
    # 數字選項 pattern — 安価/投票格式（「１０：ご主人様」「１～３：ママぁ」）。
    # 使用者觀察：選項後通常不接 AA 圖，是強對話信號，給較高 bonus。
    has_num_option = bool(_NUMBER_OPTION_RE.search(text)
                          or _NUMBER_RANGE_RE.match(text))
    if has_num_option:
        s += 3
    # 句末標點 bonus。全形 `！？` 是強口語/對話信號（+2）；半形 `!?` 在 AA 圖
    # 中常被借作裝飾，信號較弱（+1）；`。、…` 一般日文標點（+1）。
    if any(ch in text for ch in '！？'):
        s += 2
    elif any(ch in text for ch in '!?'):
        s += 1
    elif any(ch in text for ch in '。、…'):
        s += 1
    # 行尾對話 bonus + 多重懲罰豁免：典型 AA 排版（左圖右文），右側候選
    # 且左側有寬空白間隔時，幾乎肯定是對話。免疫局部密度／類別跳轉／
    # 連續同片假名（涵蓋「ハハハ」笑聲）／連續小字假名等懲罰。
    is_right_edge = _is_right_edge_dialogue(line, start, end)
    if is_right_edge:
        s += 2
    # 對話框內 bonus + 密度豁免：候選兩側被 ｜| / ＜＞ / │ 包夾時。
    # 對話框內的合法日文（如「│一旦止め―――│」）因 │ 邊框會把局部 AA 密度
    # 拉高被誤殺；偵測到對話框邊界時，把它視為強日文信號豁免密度懲罰。
    is_box_bounded = _is_dialogue_box_bounded(line, start, end)
    if is_box_bounded:
        s += 2
    # 名牌前綴 bonus + 密度豁免：候選前方為「【說話者】───」名牌時，
    # 名牌後的對話常被破折號 `───`（屬 symbol_regex）拉高局部密度而誤殺。
    has_name_tag = bool(_NAME_TAG_PREFIX_RE.search(line[:start]))
    if has_name_tag:
        s += 2
    # AA 噪聲懲罰：在「強日文信號」缺席時才嚴罰。
    # 注意：`_OKURIGANA_RE` 與 `_KANJI_HIRA_RE` 雖然有分數 bonus，但不列入
    # strong_jp — AA 圖中「kanji+hira」「kanji-hira-kanji」三字偶然碰撞時
    # （如「灯な心」「彡く弍」）若也豁免局部 AA 密度懲罰，會放掉大量假陽性。
    # has_run + len 門檻從 8 降到 7（為了 `じゃあオープン`）。
    any_hira = any(0x3041 <= ord(c) <= 0x309F for c in text)
    # `has_run + len>=7` 條件需要含平假名 — 純片假名長候選（如「ニニー―――ニ」）
    # 不可僅靠長度被視為強日文信號；真實長對話幾乎必含平假名。
    # strong_jp 的句末標點只認**全形** `！？。` — 半形 `!?` 信號不足以豁免
    # 局部 AA 密度懲罰（避免「ンへ!」這類含半形 `!` 的 AA 碎片被放行）。
    strong_jp = (particle_count >= 2
                 or (has_run and len(text) >= 7 and any_hira)
                 or any(ch in text for ch in '！？。')
                 or has_num_option
                 or is_right_edge
                 or is_box_bounded
                 or has_name_tag)
    # 注意：分母改為「非空白字元數」後（見 `_local_aa_density`），閾值
    # 從原本 0.6 降到 0.4，否則 AA padding 大量空白會把比例壓低逃避懲罰。
    density = _local_aa_density(line, start, end, symbol_regex, 8)
    if density > 0.4 and not strong_jp:
        s -= 4
    elif density > 0.4:
        s -= 1
    # 候選「自身」AA 噪聲密度懲罰：候選文字本身含過多 AA 噪聲字元（symbol_regex
    # 命中 / 半形片假名 / AA 標點）時，即使有 has_run 等強信號也幾乎肯定是 AA
    # 碎片（如「丈ｒうっ.o.,.」這類錨點假名混入點線雜訊）。此懲罰不受 strong_jp
    # 豁免 — strong_jp 豁免的是「鄰近」AA 噪聲，候選自身內部噪聲是更直接的證據。
    if len(text) >= 4 and aa_noise_ratio(text, symbol_regex) > 0.4:
        s -= 5
    # 註：曾評估「整行 AA 比例懲罰」，但實測無效 — AA 圖常用 content-class 字元
    # （latin `i`/`l`、漢字 `彡`/`从`）當裝飾「柱子」，會把整行比例稀釋（如
    # `|i:i:i|` 中的 `i` 算 content），導致純 AA 行的比例反而比合法短句行還低。
    # 改善「AA 圖中碰巧像日文的碎片」（如 `怎うぅx,`）的正解是補強
    # DEFAULT_SYMBOL_REGEX（用 scan_aa_symbols.py 找高頻裝飾字元），讓
    # `_local_aa_density` 的判定更準。
    # 類別跳轉率懲罰：只在較長文字、且無強日文信號時才套用。
    # 門檻從 5 升到 6 — 避免「お腹痛い）」這類 5-char 日文短句（漢字-平假名
    # -漢字-平假名-閉括號，類別頻繁跳轉但合法）被誤殺。
    if (len(text) >= 6 and _class_transitions(text) / len(text) > 0.5
            and not strong_jp):
        s -= 2
    # 連續相同片假名懲罰：AA 圖邊框/裝飾特徵；行尾對話豁免（涵蓋「ハハハ」笑聲）
    if _REPEAT_KATAKANA_RE.search(text) and not is_right_edge:
        s -= 3
    # 連續相同小字假名懲罰：「ぃぃぃ」這類 AA 裝飾
    if _REPEAT_SMALL_HIRA_RE.search(text) and not is_right_edge:
        s -= 3
    # 連續相同平假名懲罰：「んんんんん」這類 AA 裝飾；行尾豁免（涵蓋「ふふふ」笑聲）
    if _REPEAT_HIRA_RE.search(text) and not is_right_edge:
        s -= 3
    # AA latin 前綴懲罰：「jニ二スミー」「j爻そ」「jI斗ぅ示」這類片段
    if _AA_LATIN_PREFIX_RE.match(text):
        s -= 3
    # AA latin 尾綴懲罰：「行灯な心マi」這類片段
    if _AA_LATIN_SUFFIX_RE.search(text):
        s -= 3
    # AA 小字片假名前綴懲罰：「ィｆ芥ぅ」這類片段
    if _AA_SMALL_KATA_PREFIX_RE.match(text):
        s -= 3
    # AA 雜訊標點前綴懲罰：「-へ!.」「,..へ！-」「.込り!」這類片段
    if _AA_PUNCT_PREFIX_RE.match(text):
        s -= 4
    # 平假名夾真片假名懲罰：「とつこンっ」的「こンっ」這類片假名單字夾在平假名間。
    if _HIRA_KATA_HIRA_RE.search(text):
        s -= 3
    # 純片假名長候選（無平假名）懲罰：「ニニー―――ニ」這類純 kata + 裝飾片段。
    # 真實日文 ≥5 char 句子幾乎都含平假名（即使片假名為主的詞也常接助詞 の/だ
    # 等）；right_edge / box_bounded / spaced-out 等強信號可豁免（涵蓋
    # 「ハハハハッ」笑聲在行尾的情況）。
    if len(text) >= 5 and not any_hira and not strong_jp:
        s -= 3
    return s


def _find_spaced_out(line: str) -> list[tuple[str, int, int]]:
    """偵測「拉長念法」（CJK＋全形空白重複）並回傳「**保留空白**」的原始字串。

    例：「シ　ロ　ウ　殿　は　勇　者　で　す　な！」
        → 提取為「シ　ロ　ウ　殿　は　勇　者　で　す　な！」（含空白）

    為何保留空白：`apply_translation` 用 `original not in line` 做 substring 比對
    寫回譯文；若提取時把空白移除（如「シロウ殿は勇者ですな！」），會與原行
    （含空白）不匹配導致替換失敗，AI 翻譯也無從保留排版空格。
    判斷長度時仍以「去除空白後 ≥ 3 char」為準，避免太短的 candidate。

    Returns: [(raw_text_with_spaces, original_start, original_end), ...]
    """
    out: list[tuple[str, int, int]] = []
    for m in _SPACED_OUT_RE.finditer(line):
        raw = m.group(0)
        merged_len = len(raw.replace('　', '').replace(' ', ''))
        if merged_len >= 3:
            out.append((raw, m.start(), m.end()))
    return out


def _find_kata_sentence(line: str) -> list[tuple[str, int, int]]:
    """偵測「全形片假名整句」（角色全程以片假名講話）並回傳「**保留空白**」字串。

    例：「ホウシシュゾク　ハ　イシガ　ナイカギリ」、「ニンゲンノ　シタイ　ニナル。」
    判斷條件（見 `_KATA_SENTENCE_RE` 註解）：≥2 詞、全形片假名數 ≥5、相異片假名
    ≥3，藉「整句全形片假名 + 全形空白分詞」這個 AA 圖罕見特徵與裝飾區隔。
    保留內部空白的理由同 `_find_spaced_out`。
    """
    out: list[tuple[str, int, int]] = []
    for m in _KATA_SENTENCE_RE.finditer(line):
        raw = m.group(0)
        kata = [c for c in raw if 0x30A0 <= ord(c) <= 0x30FF]
        if len(kata) >= 5 and len(set(kata)) >= 3:
            out.append((raw, m.start(), m.end()))
    return out


def _is_strong_short_candidate(text: str) -> bool:
    """短候選（≤ 3 char）是否具備「強日文信號」可獨立成立。

    具備以下任一即可獨立保留：
    - 含**全形** `！？` **且長度 ≥ 3**（如「え！？」「だっ！？」）。長度 2 含
      `！？` 的情況（如「ま！」）信號不足，需要靠同行伴隨候選或行尾位置才保留。
      **半形 `!?` 不算** — AA 圖中半形驚嘆/問號常被借作裝飾。
    - 命中 `_OKURIGANA_RE`（漢字+平假名+漢字）
    - 含 ≥2 種不同助詞（不計重複出現）
    - 命中 `_NUMBER_OPTION_RE`（數字選項格式）
    """
    if any(c in text for c in '！？') and len(text) >= 3:
        return True
    if _OKURIGANA_RE.search(text):
        return True
    # 計算「不同種」助詞數量（不重複）：避免「へへ」這類同字假名重複
    # 因 `へ` 是助詞被 count=2 觸發，把 AA 裝飾誤判為強短信號。
    if len({c for c in text if c in _PARTICLES}) >= 2:
        return True
    if _NUMBER_OPTION_RE.search(text):
        return True
    # 純平假名 run（整段為 ≥3 個真平假名，如「まだだ」「そうだ」）— AA 圖極少
    # 湊出 3 個連續真平假名，視為可獨立成立的強短信號（同字重複如「んんん」
    # 雖也命中，但會被分數階段的 `_REPEAT_HIRA_RE` -3 罰分擋下）。
    if len(text) >= 3 and _HIRAGANA_RUN_RE.fullmatch(text):
        return True
    return False


_RIGHT_EDGE_CLOSERS = set('＞」』）〕]>―‐—–')


def _is_dialogue_box_bounded(line: str, start: int, end: int) -> bool:
    """候選位於對話框 ｜…| / ＜…│ / │…＞ 等內部 → 強烈日文對話信號。

    判定：跳過候選兩側空白後，緊鄰的非空白字元是對話框邊框字元
    （左側 `＜<│|｜`，右側 `＞>│|｜`）。命中時：
    - 列入 `strong_jp`（免疫局部 AA 密度懲罰）
    - 額外 +2 分
    用途：捕捉「│一旦止め―――│」「＜起きた.│」「｜（…………あ、ダメだ）|」
    這類對話框內的合法日文 — 候選因鄰近 `│|｜` 邊框會被 `_local_aa_density`
    判定為高 AA 密度而誤殺，此機制把這種「自帶 AA 邊界」情境視為強信號。
    """
    i = start - 1
    while i >= 0 and line[i] in (' ', '　'):
        i -= 1
    left_ok = i >= 0 and line[i] in _DIALOGUE_LEFT_MARKERS
    if not left_ok:
        return False
    j = end
    while j < len(line) and line[j] in (' ', '　'):
        j += 1
    return j < len(line) and line[j] in _DIALOGUE_RIGHT_MARKERS


def _is_right_edge_dialogue(line: str, start: int, end: int) -> bool:
    """候選位於行尾 + 左側有寬空白間隔 → 強烈日文對話信號。

    使用者領域知識：典型 AA 排版是「左側 AA 圖、右側對話」，當候選位於
    一行的右端且左側有 ≥ 3 個連續空白與 AA 圖區隔，幾乎肯定是對話文字。
    這是比 `_local_aa_density` 更強的位置信號 — 8 字元視窗的局部密度
    無法捕捉「AA 圖在 10+ 字元外但整行 AA 比例高」這類情境。

    判定條件：
    - 右側：候選 end 之後到行尾僅含空白與「行尾標誌」（如 `＞」』）` 等
      對話框收尾符號，不視為內容）
    - 左側：候選 start 之前 ≥ 3 個連續半形/全形空白（與 AA 圖隔開）
    """
    rest = line[end:].rstrip()
    while rest and rest[-1] in _RIGHT_EDGE_CLOSERS:
        rest = rest[:-1].rstrip()
    if rest != '':
        return False
    space_count = 0
    i = start - 1
    while i >= 0 and line[i] in (' ', '　'):
        space_count += 1
        i -= 1
    return space_count >= 3


def _in_bracket_region(line: str, start: int, end: int) -> bool:
    """候選 [start, end) 是否落在某對括號（【「『（( 等）之內。

    判定：`line[:start]` 中最後一個「尚未閉合」的開括號存在，且其對應閉括號
    出現在 `line[end:]`。用於 `_SHORT_KATA_UTT_RE` 短純片假名喊叫的括號閘。
    """
    pending = None
    for ch in line[:start]:
        if ch in _OPEN_BRACKETS:
            pending = ch
        elif ch in _CLOSE_BRACKETS:
            pending = None
    if pending is None:
        return False
    return _BRACKET_PAIRS.get(pending, '') in line[end:]


def _extract_experimental_line(
    line: str,
    invalid_regex: re.Pattern,
    symbol_regex: re.Pattern,
) -> list[tuple[str, int, int]]:
    """對單行執行錨點掃描 → 邊界擴展 → 分數過濾。

    Returns: [(text, start, end), ...] 已通過分數過濾，但尚未經過後處理。
    """
    out: list[tuple[str, int, int]] = []

    # 先處理「拉長念法」與「全形片假名整句」：直接回傳保留空白字串，不走邊界擴展與分數過濾
    covered_ranges: list[tuple[int, int]] = []
    spaced = _find_spaced_out(line)
    for text, s, e in spaced:
        covered_ranges.append((s, e))
        if invalid_regex.match(text):
            continue
        out.append((text, s, e))
    for text, s, e in _find_kata_sentence(line):
        if any(ss <= s and e <= ee for ss, ee in covered_ranges):
            continue
        covered_ranges.append((s, e))
        if invalid_regex.match(text):
            continue
        out.append((text, s, e))

    anchors = _find_anchors(line)
    if not anchors:
        return out
    expanded = [_expand_boundary(line, s, e) for s, e in anchors]
    expanded = _merge_overlapping(expanded)

    for s, e in expanded:
        # 跳過已被「拉長念法／全形片假名整句」覆蓋的區間
        if any(ss <= s and e <= ee for ss, ee in covered_ranges):
            continue
        text = line[s:e].strip()
        # 一般候選需 ≥ 3 char。長度 2 的特殊放行：
        # - `_SHORT_UTT_RE` 匹配（假名+句末標點，如「ま！」「あ？」）
        # - `_HIRAGANA_RUN_RE` 匹配（純平假名 run，如「さぁ」「はい」）
        # - `_KANJI_HIRA_RE` 匹配 **且** 位於行尾（如行尾的「奪う」「私だ」）
        #   ↑ 必須行尾：避免「二つ」「メヘ」等漢字+假名片段在 AA 中段被誤抓
        # 注意：通過 min_len 不等於最終保留，後續的 score 過濾與孤立過濾還會
        # 進一步把可疑短候選擋下。
        # - `_KANJI_END_PUNCT_RE` 匹配 **且** 位於行尾（如行尾的「何？」「諸君！」）
        #   ↑ 必須行尾：純漢字+標點短碎片只在「左圖右文」右端才信賴
        is_short_utt = bool(_SHORT_UTT_RE.fullmatch(text))
        is_pure_hira = bool(_HIRAGANA_RUN_RE.fullmatch(text))
        is_kanji_hira = bool(_KANJI_HIRA_RE.fullmatch(text))
        is_kanji_end = bool(_KANJI_END_PUNCT_RE.fullmatch(text))
        is_re = _is_right_edge_dialogue(line, s, e)
        allow_short = (is_short_utt or is_pure_hira
                       or (is_kanji_hira and is_re)
                       or (is_kanji_end and is_re))
        min_len = 2 if allow_short else 3
        if len(text) < min_len:
            continue
        if invalid_regex.match(text):
            continue
        if _score_candidate(text, line, s, e, symbol_regex) < 2:
            continue
        # 短純片假名喊叫閘：恰好「2 片假名 + 標點」的最短型（如「クソ！」）
        # 僅在括號區間內保留 — 括號外的同型碎片多為 AA 圖裝飾。
        if _SHORT_KATA_UTT_RE.match(text) and not _in_bracket_region(line, s, e):
            continue
        out.append((text, s, e))
    out.sort(key=lambda x: x[1])

    # 短候選孤立過濾：≤ 3 char 的候選若無「強短信號」（含 ！？／送り仮名／2+助詞／
    # 數字選項），必須同行有 ≥ 5 char 的伴隨候選才保留。AA 圖中常見孤立的「へ、」
    # 「して」「んん」「ーへ、」等短碎片，靠局部 AA 密度檢查無法區分（碎片周圍是
    # 空白而非 AA 噪聲）；改以「同行是否有實質日文對話」作為信賴指標。
    # 例：「さぁ 燃料をくべましょう」中的「さぁ」(len 2) 因有 9-char 伴隨而保留；
    #     「／／／ ｨへ、ヾ　 ｀＞、」中的「へ、」因孤立而剔除。
    has_long_companion = any(len(t) >= 5 for t, _, _ in out)
    if not has_long_companion:
        out = [(t, s, e) for t, s, e in out
               if len(t) >= 4
               or _is_strong_short_candidate(t)
               or _is_right_edge_dialogue(line, s, e)
               or _is_dialogue_box_bounded(line, s, e)]
    return out


_BRACKET_PAIRS = {
    '【': '】', '】': '【',
    '「': '」', '」': '「',
    '\u201c': '\u201d', '\u201d': '\u201c',  # ""
    '\u2018': '\u2019', '\u2019': '\u2018',  # ''
    '『': '』', '』': '『',
    '（': '）', '）': '（',
    '(': ')', ')': '(',
}
_OPEN_BRACKETS = set('【「\u201c\u2018『（(')
_CLOSE_BRACKETS = set('】」\u201d\u2019』）)')


def _complete_brackets(text: str, source_line: str,
                       hint_pos: int = -1) -> str:
    """檢查 text 中的括號是否成對，視需要補上缺少的括號。

    對每個未配對的括號，在原始行中定位 text 的位置，
    再檢查緊鄰的相鄰字元是否恰好是該缺少的括號：
    - 缺少左括號（有未配對的右括號）→ 檢查 text 前一個字元
    - 缺少右括號（有未配對的左括號）→ 檢查 text 後一個字元
    有就補上，沒有則維持原樣。

    `hint_pos`：呼叫端提供的「候選在 source_line 中的起始位置」hint。
    用於處理同一 text 在 source_line 中出現多次的情境（如：
    `「俺だって！　俺だって！…` 中兩個 `俺だって！`，第二個如果用
    `find(text)` 會抓到第一個位置 → 誤補 `「` 前綴）。
    """
    # 用 stack 找未配對的括號
    stack: list[tuple[str, int]] = []  # (bracket_char, index)
    for i, ch in enumerate(text):
        if ch in _OPEN_BRACKETS:
            stack.append((ch, i))
        elif ch in _CLOSE_BRACKETS:
            expected_open = _BRACKET_PAIRS[ch]
            if stack and stack[-1][0] == expected_open:
                stack.pop()
            else:
                stack.append((ch, i))

    # 在 source_line 中定位 text。hint_pos ≥ 0 時，從 hint_pos 起搜尋，
    # 確保拿到「該候選實際所在位置」而非 source_line 中的第一個 occurrence。
    if hint_pos >= 0:
        pos = source_line.find(text, hint_pos)
        if pos < 0:
            # 找不到（postprocess 改變字串）→ 退回原 hint_pos
            pos = hint_pos
    else:
        pos = source_line.find(text)
    if pos < 0:
        return text  # 無法定位，維持原樣

    result = text
    for bracket, _idx in stack:
        if bracket in _CLOSE_BRACKETS:
            # 有未配對的右括號 → 缺少左括號 → 檢查 text 前一個字元
            if pos > 0 and source_line[pos - 1] == _BRACKET_PAIRS[bracket]:
                result = _BRACKET_PAIRS[bracket] + result
                pos -= 1  # 補上後 result 在 source_line 中的起始位置前移
        else:
            # 有未配對的左括號 → 缺少右括號 → 檢查 text 後一個字元
            end = pos + len(result)
            if end < len(source_line) and source_line[end] == _BRACKET_PAIRS[bracket]:
                result = result + _BRACKET_PAIRS[bracket]

    # 額外處理：整個括號在 text 外（提取時被丟掉）的情況
    # 檢查 source_line 中緊鄰 text 的前/後字元是否為括號，有就補回
    if pos > 0:
        prev = source_line[pos - 1]
        if prev in _OPEN_BRACKETS and not result.startswith(prev):
            result = prev + result
            pos -= 1
    end = pos + len(result)
    if end < len(source_line):
        nxt = source_line[end]
        if nxt in _CLOSE_BRACKETS and not result.endswith(nxt):
            result = result + nxt

    return result


def extract_text(
    source: str,
    base_regex_str: str,
    invalid_regex_str: str,
    symbol_regex_str: str,
    filter_str: str,
    skip_title: str = "",
    author_name: str = "",
    korean_mode: bool = False,
    experimental: bool = False,
    work_title: str = "",
) -> list[tuple[str, int]]:
    """從原始文本中提取日文文字片段。

    Args:
        skip_title: URL 讀取來源的標題文字；若 source 的第一個非空行內容**完全等於**
            此字串，整行跳過（不提取）。空字串表示不套用此規則。
        author_name: 作者名稱；提取結果中若有文字等於此名稱則剔除。
            空字串表示不套用此規則。
        experimental: 啟用實驗性日文提取演算法（錨點掃描＋分數制過濾）。
            僅在非韓文模式下生效。
        work_title: 作品標題（來自 UI「作品」欄位）；若 source 的第一個非空行
            **包含**此字串（substring 比對），整行跳過。與 `skip_title` 的差異
            是這裡用 substring，因為作品欄位的文字常被包在頁面標題的更長字串中。
            空字串表示不套用此規則。

    Returns:
        list[tuple[str, int]]: [(提取文字, 來源行號), ...]，不去重（同一行內出現
        多次的相同文字也會全部保留，依匹配順序排列）。
    """
    base_regex = _compile_regex(base_regex_str, DEFAULT_BASE_REGEX)
    invalid_regex = _compile_regex(invalid_regex_str, DEFAULT_INVALID_REGEX)
    symbol_regex = _compile_regex(symbol_regex_str, DEFAULT_SYMBOL_REGEX)
    custom_regexes = _compile_custom_filters(filter_str)

    # 移除 Unicode 雙向控制/隱形格式字元（不影響可見文字、不改變行數）
    source = _BIDI_CONTROL_RE.sub('', source)
    lines = source.split('\n')
    extracted: list[tuple[str, int]] = []

    # 定位「第一個非空行」的行號（供 skip_title 完全比對 / work_title substring 比對用）
    title_line_num = 0
    skip_target = skip_title.strip()
    work_target = work_title.strip()
    if skip_target or work_target:
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped:
                if (skip_target and stripped == skip_target) or (
                        work_target and work_target in stripped):
                    title_line_num = idx
                break

    # 作者名稱通常含開頭符號（如「◆Hr94QM5gdI」），而提取後處理會去掉符號
    # 只剩英數字 trip code。因此從作者名稱抽出最長的英數字串（長度 ≥ 6）作為
    # 過濾鍵；找不到則退回原字串完全比對。
    author_target = author_name.strip()
    author_tripcode = ""
    if author_target:
        runs = re.findall(r'[A-Za-z0-9]{6,}', author_target)
        if runs:
            author_tripcode = max(runs, key=len)

    use_experimental = experimental and not korean_mode

    for line_num, line in enumerate(lines, 1):
        if line_num == title_line_num:
            continue
        if _POSTER_LINE_RE.search(line):
            continue
        # 骰子格式內的半形 `:` 暫時改全形 `：`，避免句子在骰子處被 base regex 切開
        proc_line = _DICE_NOTATION_RE.sub(r'\1：\2', line)

        if use_experimental:
            for raw_text, cand_s, _e in _extract_experimental_line(
                    proc_line, invalid_regex, symbol_regex):
                # 把骰子內的全形 `：` 還原為半形 `:`
                text = _DICE_NOTATION_FW_RE.sub(r'\1:\2', raw_text)
                text = _postprocess_text(text, korean_mode=False,
                                          experimental=True)
                # 短句允許長度 2 的條件：見 `_extract_experimental_line` 內註解。
                # 此處走 postprocess 之後再過一次 min_len 防剝離後過短。
                is_re_post = _is_right_edge_dialogue(line, cand_s, _e)
                allow_short = (_SHORT_UTT_RE.fullmatch(text)
                               or _HIRAGANA_RUN_RE.fullmatch(text)
                               or (_KANJI_HIRA_RE.fullmatch(text)
                                   and is_re_post)
                               or (_KANJI_END_PUNCT_RE.fullmatch(text)
                                   and is_re_post))
                min_len = 2 if allow_short else 3
                if len(text) < min_len:
                    continue
                if author_target:
                    stripped = text.strip()
                    if stripped == author_target or (
                            author_tripcode
                            and stripped == author_tripcode):
                        continue
                text = _complete_brackets(text, line, hint_pos=cand_s)
                if any(reg.search(text) for reg in custom_regexes):
                    continue
                if text:
                    extracted.append((text, line_num))
            continue

        chunks = re.split(r'[ 　]{2,}', proc_line)

        for chunk in chunks:
            if not chunk.strip():
                continue

            chunk_str = str(chunk)
            symbol_count = len(symbol_regex.findall(chunk_str))
            if symbol_count > len(chunk_str) * 0.5:
                continue

            matches = base_regex.findall(chunk)
            for text in matches:
                text = text.strip()
                if len(text) < 3:
                    continue

                # 把骰子內的全形 `：` 還原為半形 `:`，使後續規則（結尾骰子移除等）
                # 維持原行為，最終輸出也保留原始的半形冒號。
                text = _DICE_NOTATION_FW_RE.sub(r'\1:\2', text)

                if invalid_regex.match(text):
                    continue

                text = _postprocess_text(text, korean_mode=korean_mode)

                if len(text) <= 2:
                    continue

                if author_target:
                    stripped = text.strip()
                    if stripped == author_target or (
                        author_tripcode and stripped == author_tripcode
                    ):
                        continue

                text = _complete_brackets(text, line)

                # 自訂過濾規則：在提取流程結束後對最終結果做過濾
                if any(reg.search(text) for reg in custom_regexes):
                    continue

                if text:
                    extracted.append((text, line_num))

    return extracted


def format_extraction_output(extracted: list[tuple[str, int]]) -> str:
    """將提取結果格式化為 '001-1|text' 格式。"""
    line_sub_index: dict[int, int] = {}
    output = ""
    for text, ln in extracted:
        sub = line_sub_index.get(ln, 1)
        line_sub_index[ln] = sub + 1
        output += f"{ln:03d}-{sub}|{text}\n"
    return output


def analyze_extraction(
    text: str,
    base_regex_str: str,
    invalid_regex_str: str,
    symbol_regex_str: str,
    filter_str: str,
    korean_mode: bool = False,
    experimental: bool = False,
) -> str:
    """對輸入文字逐步分析提取流程，回傳分析報告字串。"""
    base_regex = _compile_regex(base_regex_str, DEFAULT_BASE_REGEX)
    invalid_regex = _compile_regex(invalid_regex_str, DEFAULT_INVALID_REGEX)
    symbol_regex = _compile_regex(symbol_regex_str, DEFAULT_SYMBOL_REGEX)
    custom_regexes = _compile_custom_filters(filter_str)
    use_experimental = experimental and not korean_mode

    report: list[str] = []
    text = _BIDI_CONTROL_RE.sub('', text)
    lines = text.split('\n')
    if use_experimental:
        report.append("【實驗性日文提取演算法啟用】錨點掃描＋分數制過濾")
        report.append("")

    for line_idx, line in enumerate(lines, 1):
        if not line.strip():
            continue
        report.append(f"--- 開始分析字串 (第 {line_idx} 行) ---")
        report.append(f"原始字串: '{line}'")

        if _POSTER_LINE_RE.search(line):
            report.append("  -> ❌ 整行剔除：判定為發文者行（含 ID:xxxxxx trip code）。")
            report.append("\n" + "=" * 40 + "\n")
            continue

        # 骰子格式內的半形 `:` 暫時改全形 `：`（與 extract_text 一致），避免被切開
        proc_line = _DICE_NOTATION_RE.sub(r'\1：\2', line)
        if proc_line != line:
            report.append(
                "[骰子格式預處理] 將句中 【NDN:N】 內的半形 `:` 暫換為 `：` 以維持整句連續匹配")

        if use_experimental:
            anchors = _find_anchors(proc_line)
            report.append(f"[步驟 1] 錨點掃描：找到 {len(anchors)} 個錨點")
            for ai, (s, e) in enumerate(anchors):
                report.append(
                    f"  錨點 {ai+1}: ({s}, {e}) -> '{proc_line[s:e]}'")
            if not anchors:
                report.append("  -> ❌ 整行無錨點，剔除。")
                report.append("\n" + "=" * 40 + "\n")
                continue
            expanded = _merge_overlapping(
                [_expand_boundary(proc_line, s, e) for s, e in anchors])
            report.append(f"[步驟 2] 邊界擴展與合併：得到 {len(expanded)} 個 candidate")
            for ci, (s, e) in enumerate(expanded):
                raw = proc_line[s:e]
                report.append(f"\n  >> Candidate {ci+1} '{raw}' (pos {s}-{e})")
                t = raw.strip()
                if len(t) < 3:
                    report.append("    -> ❌ 剔除：長度 < 3。")
                    continue
                if invalid_regex.match(t):
                    report.append("    -> ❌ 剔除：全句符合 invalid_regex。")
                    continue
                density = _local_aa_density(
                    proc_line, s, e, symbol_regex, 8)
                trans = _class_transitions(t)
                trans_ratio = trans / len(t) if len(t) > 0 else 0
                score = _score_candidate(t, proc_line, s, e, symbol_regex)
                report.append(
                    f"    [分數] 局部AA密度={density:.2f}, "
                    f"類別跳轉率={trans_ratio:.2f}, score={score}")
                if score < 2:
                    report.append("    -> ❌ 剔除：score < 2。")
                    continue
                t = _DICE_NOTATION_FW_RE.sub(r'\1:\2', t)
                original_t = t
                t = _postprocess_text(t, korean_mode=False, experimental=True)
                report.append(f"    [後處理] '{original_t}' => '{t}'")
                if len(t) <= 2:
                    report.append("    -> ❌ 剔除：後處理後長度 <= 2。")
                    continue
                filtered_by_custom = False
                for reg in custom_regexes:
                    if reg.search(t):
                        report.append(
                            f"    -> ❌ 剔除：命中自訂濾網 ({reg.pattern})。")
                        filtered_by_custom = True
                        break
                if filtered_by_custom:
                    continue
                report.append(f"    -> ✅ 成功提取最終文字: '{t}'")
            report.append("\n" + "=" * 40 + "\n")
            continue

        chunks = re.split(r'[ 　]{2,}', proc_line)
        report.append(f"[步驟 1] 藉由連續兩個以上空白進行分割，分割出 {len(chunks)} 個區塊:")
        for i, chunk in enumerate(chunks):
            report.append(f"  區塊 {i+1}: '{chunk}'")

        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                report.append(f"\n[區塊 {i+1} 分析] '{chunk}'")
                report.append("  -> ❌ 剔除：區塊為空字串或純空白。")
                continue

            report.append(f"\n[區塊 {i+1} 分析] '{chunk}'")
            chunk_str = str(chunk)

            symbol_count = len(symbol_regex.findall(chunk_str))
            symbol_ratio = symbol_count / len(chunk_str) if len(chunk_str) > 0 else 0
            report.append(
                f"  [步驟 2] 判斷 AA 符號比例 "
                f"(符號數: {symbol_count}, 總字元數: {len(chunk_str)}, 比例: {symbol_ratio:.2f})"
            )

            if symbol_ratio > 0.5:
                report.append("  -> ❌ 剔除：符號比例超過 50%，判定為 AA 圖案。")
                continue

            matches = base_regex.findall(chunk)
            report.append(f"  [步驟 3] 執行 Base Regex 匹配: 找到 {len(matches)} 個可能詞彙")
            if not matches:
                report.append("  -> ❌ 剔除：無法匹配出任何文字。")
                continue

            for j, match_text in enumerate(matches):
                report.append(f"\n  >> 對 [結果 {j+1}] '{match_text}' 進行進階檢驗:")
                t = match_text.strip()
                # 還原骰子格式的全形 `：` → 半形 `:`（與 extract_text 一致）
                t_restored = _DICE_NOTATION_FW_RE.sub(r'\1:\2', t)
                if t_restored != t:
                    report.append(f"    [骰子格式還原] '{t}' => '{t_restored}'")
                t = t_restored

                if len(t) < 3:
                    report.append("    -> ❌ 剔除：去除前後空白後，長度小於 3 字元。")
                    continue
                else:
                    report.append(f"    - 長度檢驗通過 (長度: {len(t)})")

                if invalid_regex.match(t):
                    report.append("    -> ❌ 剔除：全句符合無意義符號組合正則 (Invalid Regex)。")
                    continue
                else:
                    report.append("    - 無意義符號組合檢驗通過")

                original_t = t
                t = _postprocess_text(t, korean_mode=korean_mode)
                report.append(f"    [步驟 4] 後處理解析 (去除非內文字元): '{original_t}' => '{t}'")

                if len(t) <= 2:
                    report.append("    -> ❌ 剔除：經過後處理後，剩餘文字長度 <= 2 字元。")
                    continue
                else:
                    report.append(f"    - 最終長度檢驗通過 (長度: {len(t)})")

                filtered_by_custom = False
                for reg in custom_regexes:
                    if reg.search(t):
                        report.append(f"    -> ❌ 剔除：命中自訂濾網正則表達式 ({reg.pattern})（對最終提取結果過濾）。")
                        filtered_by_custom = True
                        break
                if filtered_by_custom:
                    continue
                else:
                    report.append("    - 自訂過濾清單檢驗通過（對最終提取結果）")

                report.append(f"    -> ✅ 成功提取最終文字: '{t}'")
        report.append("\n" + "=" * 40 + "\n")

    return "\n".join(report)


# ────────────────────────────────────────────────────────────────────────────
# 特別提取：單個假名字符夾在特定邊界符號之間
# 第一字元：空白、點、破折號、長音符號、省略號（半形/全形皆可）
# 第二字元：任意平假名或片假名
# 第三字元：句號（。）或問號（? / ？）或驚嘆號（！ / !）
# ────────────────────────────────────────────────────────────────────────────
_SINGLE_KANA_RE = re.compile(
    r'[ 　.．\-－‐—―ーｰ…]'   # pos 1
    r'([アあハはンんオおで])'    # pos 2（捕捉）：限定 ア/あ ハ/は ン/ん オ/お で
    r'[。？！]'                   # pos 3：**全形**句號／問號／驚嘆號（不含空白）
)


def extract_single_kana(
    source: str,
    filter_str: str,
) -> list[tuple[str, int]]:
    """特別提取：單個假名字符夾在特定邊界符號之間的單字。

    連續三字元條件：
    - 第一字元：空白（ / 　）、點（. / ．）、破折號（- / － / ‐ / — / ―）
                、長音符號（ー / ｰ）或省略號（…），半形/全形皆可
    - 第二字元：ア/あ、ハ/は、ン/ん、オ/お（平假名或片假名，各四種）
    - 第三字元：**全形**句號（。）／問號（？）／驚嘆號（！）；不含空白與半形
      `?` `!`（AA 圖常以半形驚嘆/問號作裝飾，如「 ン!」會誤判）

    完全獨立於 base_regex / invalid_regex / symbol_regex；
    只受自訂過濾規則（filter_str）影響。

    Returns:
        list[tuple[str, int]]: [(提取文字, 來源行號), ...]，不去重（同一行內出現
        多次的相同文字也會全部保留）。
    """
    custom_regexes = _compile_custom_filters(filter_str)
    lines = source.split('\n')
    extracted: list[tuple[str, int]] = []

    for line_num, line in enumerate(lines, 1):
        for match in _SINGLE_KANA_RE.finditer(line):
            text = match.group(0)
            if any(reg.search(text) for reg in custom_regexes):
                continue
            if text:
                extracted.append((text, line_num))

    return extracted


def validate_ai_text(ai_content: str) -> list[str]:
    """驗證 AI 翻譯結果格式，回傳警告訊息列表（空 = 格式正確）。"""
    if not ai_content.strip():
        return []

    ai_lines = [l for l in ai_content.split('\n') if l.strip()]
    warnings: list[str] = []

    id_pattern = re.compile(r'\d{2,5}-\d+\|')
    multi_id_lines = []
    for i, line in enumerate(ai_lines, 1):
        ids_found = id_pattern.findall(line)
        if len(ids_found) >= 2:
            multi_id_lines.append(str(i))
    if multi_id_lines:
        warnings.append(f"⚠ 第 {','.join(multi_id_lines)} 行含有多個ID")

    return warnings


_KANJI_DIGITS = {
    '〇': 0, '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '百': 100, '千': 1000,
}


def _kanji_to_int(text: str) -> int | None:
    """將漢數字字串轉為整數。支援「八」「十二」「百二十三」等格式。"""
    if not text:
        return None
    # 先嘗試直接轉阿拉伯數字
    if text.isdigit():
        return int(text)

    result = 0
    current = 0
    for ch in text:
        val = _KANJI_DIGITS.get(ch)
        if val is None:
            return None
        if val >= 10:
            # 十百千 — 乘算單位
            if current == 0:
                current = 1
            result += current * val
            current = 0
        else:
            current = val
    result += current
    return result if result > 0 or text == '〇' or text == '零' else None


def check_chapter_number(text_first_lines: str) -> str | None:
    """掃描文字的前幾行，尋找話數格式，回傳章節號碼字串。

    支援格式：第N話、番外編N、その N（阿拉伯數字＋漢數字＋全角數字）。

    Returns:
        章節號碼字串或 None。
    """
    result = get_chapter_display(text_first_lines)
    if result:
        return result[0]
    return None


def get_chapter_display(text_first_lines: str) -> tuple[str, str] | None:
    """掃描文字的前幾行，尋找話數格式。

    Returns:
        (number_str, display_label) 元組，例如 ("8", "第8話")、("3", "番外編3")、
        ("216", "その216")，或 None。
    """
    # 第N-N話 — 複合話數（如「第5-2話」），完整保留分節編號
    match = re.search(
        r'第\s*([０-９\d]+)\s*[-－ー−ｰ]\s*([０-９\d]+)\s*話', text_first_lines)
    if match:
        trans = str.maketrans('０１２３４５６７８９', '0123456789')
        a = str(int(match.group(1).translate(trans)))
        b = str(int(match.group(2).translate(trans)))
        n = f"{a}-{b}"
        return (n, f"第{n}話")

    # 第N話 … そのZ — 話＋分篇複合（如「第28話 … その２」→ 28-2）
    _trans_fw = str.maketrans('０１２３４５６７８９', '0123456789')
    match = re.search(
        r'第\s*([０-９\d]+)\s*話.*?その\s*([０-９\d]+)', text_first_lines)
    if match:
        a = str(int(match.group(1).translate(_trans_fw)))
        b = str(int(match.group(2).translate(_trans_fw)))
        n = f"{a}-{b}"
        return (n, f"第{n}話その{b}")

    # 第N話 前編／後編 — 兩部分篇（如「第32話 後編」→ 32-2、「第7話 前編」→ 7-1）。
    # 同行內「第N話」之後出現「前編」或「後編」即視為分篇，前=1、後=2。
    match = re.search(
        r'第\s*([０-９\d]+)\s*話[^\n]*?(前|後)編', text_first_lines)
    if match:
        a = str(int(match.group(1).translate(_trans_fw)))
        b = '1' if match.group(2) == '前' else '2'
        n = f"{a}-{b}"
        return (n, f"第{n}話{match.group(2)}編")

    # 第N話 — 阿拉伯數字
    match = re.search(r'第\s*(\d+)\s*話', text_first_lines)
    if match:
        n = str(int(match.group(1)))
        return (n, f"第{n}話")

    # 第N話 — 漢數字
    match = re.search(r'第\s*([〇零一二三四五六七八九十百千]+)\s*話', text_first_lines)
    if match:
        num = _kanji_to_int(match.group(1))
        if num is not None:
            return (str(num), f"第{num}話")

    # 番外編 N
    match = re.search(r'番外編\s*(\d+)', text_first_lines)
    if not match:
        match = re.search(r'番外編\s*([〇零一二三四五六七八九十百千]+)', text_first_lines)
    if match:
        num = _kanji_to_int(match.group(1))
        if num is not None:
            return (str(num), f"番外編{num}")

    # 後日談 N（阿拉伯／全形／漢數字）— 例：「エージェントやる夫の日常茶飯事　後日談3」
    match = re.search(r'後日談\s*([０-９\d]+)', text_first_lines)
    if match:
        num_str = match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        n = str(int(num_str))
        return (n, f"後日談{n}")
    match = re.search(r'後日談\s*([〇零一二三四五六七八九十百千]+)', text_first_lines)
    if match:
        num = _kanji_to_int(match.group(1))
        if num is not None:
            return (str(num), f"後日談{num}")

    # その N（全角・半角数字）— FC2 Blog タイトル形式
    match = re.search(r'その\s*([０-９\d]+)', text_first_lines)
    if match:
        num_str = match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        n = str(int(num_str))
        return (n, f"その{n}")

    # 裸 N話（無「第」前綴）— 限定第一行，避免內文中的「N話」誤命中。
    # 例：「やる夫の淫らな日々　2話　きっと掌の上だった日…」
    first_line = text_first_lines.split('\n', 1)[0]
    match = re.search(r'(?<![第そ])(?<!\d)([０-９\d]+)\s*話', first_line)
    if match:
        num_str = match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        n = str(int(num_str))
        return (n, f"第{n}話")

    # 裸漢數字話（無「第」前綴）— 例：「三話」「十二話」，同樣限定第一行
    match = re.search(r'([〇零一二三四五六七八九十百千]+)\s*話', first_line)
    if match:
        num = _kanji_to_int(match.group(1))
        if num is not None:
            return (str(num), f"第{num}話")

    return None


# 已知站名 — 可出現在標題開頭（前綴）或末尾（後綴），兩者都會被去除。
# 新增站名直接加入此列表即可。
_SITE_NAMES = [
    '安価でやるお！',
    'やる夫達のいる日常',
    'やる夫まとめくす',
    'やる夫短編集',
    'やる夫スレ本棚  | ',
    'RPG系AA物語まとめるお',
    '| 隣のAA',
    '－ やらない夫オンリーブログ',
    'やる夫スレ本棚2号館  |  ',
    'やる夫短編集モララーのビデオ棚　(,,`д`)＜とっても！地獄編 ',
    'やる夫スレ本棚別館 | ',
    '俺得やる夫やらまとめ',
    ' | やる夫広場',
]

_SITE_PREFIX_RE = re.compile(
    r'^(?:' + '|'.join(re.escape(s) for s in _SITE_NAMES) + r')[\s　]*'
)
_SITE_SUFFIX_RE = re.compile(
    r'[\s　]+(?:' + '|'.join(re.escape(s) for s in _SITE_NAMES) + r')$'
)
# dash 系尾綴：「 - 站名」「 — 站名」等形式
_SITE_DASH_SUFFIX_RE = re.compile(r'[\s　]+[-—–][\s　]+.+$')


def extract_work_title(title: str) -> str:
    """從頁面標題中去除已知站名（前綴／後綴）與 dash 尾綴，回傳作品名稱部分。"""
    t = _SITE_DASH_SUFFIX_RE.sub('', title)
    t = _SITE_PREFIX_RE.sub('', t)
    t = _SITE_SUFFIX_RE.sub('', t)
    return t.strip(' 　\t')
