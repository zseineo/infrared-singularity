# AA 漫畫翻譯輔助工具 - AI 技術規格書與 Function 對照表

## 1. 系統架構與技術棧 (Architecture & Tech Stack)
*   **檔案組成**:
    *   `aa_translation_tool.py` — 主程式
    *   `aa_dedup_tool.py` — 附屬文字去重/處理工具
    *   `aa_batch_extractor.py` — 附屬批次提取工具
    *   `aa_extraction_analyzer.py` — 附屬提取分析工具
    *   `batch_escape_tags.py` — 附屬 HTML Tag 批次逸出工具
*   **技術棧**: Python 3, `customtkinter` (UI 框架), `tkinter`, `re`, `math`, `os`, `html`, `urllib.request`, `threading`, `gzip`, `json`
*   **狀態存儲機制**:
    *   `aa_settings_cache.json` — 暫存 UI 狀態 (原文、過濾規則、術語表、話數、預覽暫存、URL 記錄、背景/文字色、各開關狀態等)，確保關閉重開後不丟失資料。**正則表達式不從暫存讀取**，由 `AA_Settings.json` 管理。
    *   `AA_Settings.json` — 正式設定檔，儲存 `filter`、`glossary`、`glossary_temp`、`base_regex`、`invalid_regex`、`symbol_regex`。
*   **字體設計**:
    *   `aa_font` — `Meiryo` 14px，用於輸入區 TextBox
    *   `result_font` — `MS PGothic` 16px，用於預覽輸出區；所有對齊計算皆依賴此字體的 `measure()` 函式
    *   `ui_font` — `Microsoft JhengHei` 14px bold，用於按鈕與標籤
    *   `ui_small_font` — `Microsoft JhengHei` 12px，用於次要按鈕與提示

## 2. 核心目標與工作流程 (Core Workflow)
主要目標為協助使用者翻譯帶有 ASCII Art (AA) 圖像的漫畫文本，並**在替換成中文翻譯時，自動計算字元差異並補上全形空白**，以避免 AA 圖案因語系字元長度差異而引發的排版崩壞。
*   **Step 1**: 貼上或從網址讀取包含 AA 圖與日文原文的原始文本至「原始文本」區塊。
*   **Step 2**: 點擊「提取日文」，系統將基於規則與正則表達式，提取出需要翻譯的純文字片段，附加三位數行號流水號 ID (`001-1|text`)。
*   **Step 3**: 將提取出的列表交由外部（如 ChatGPT 或 Claude）翻譯後，將對應的翻譯結果貼回「翻譯結果」區塊。
*   **Step 4**: 點擊「替換翻譯並編輯」，系統執行精準的文本替換、術語替換（包含全文術語覆蓋）與自動補全形空白的機制。
*   **Step 5**: 進入全螢幕或內嵌預覽視窗，在最終文本上進行微調（自動對話框、選區上色、對齊等），完成後點擊儲存匯出為 `.html`。

## 3. 主要模式 (Application Modes)
`switch_mode(mode_name)` 切換兩種模式，對應 `_current_mode` 屬性：
*   **`"translate"`** (翻譯模式，預設)：主面板 `main_frame` + 底部執行列 `gen_bar`
*   **`"edit"`** (內嵌編輯)：`show_result_modal()` 的預覽區嵌入主視窗 `edit_frame`（始終使用此模式，無跳出式視窗）

批次搜尋功能由獨立的 PyQt6 視窗 (`aa_batch_search_qt.py`) 提供，以 subprocess 方式啟動，透過 JSON 命令檔（IPC）與主程式雙向溝通。

## 4. 核心模組與對應 Function 解析 (Core Functions)
未來若要修改特定功能，請參考此清單尋找對應的 Python function。主程式類別為 `AATranslationTool(ctk.CTk)`。

### 4.1 文本提取邏輯 (`extract_text()`)
*   **對應 Function**: `def extract_text(self):`
*   **行分塊前處理**: 每行先以連續兩個以上半形/全形空白 (`[ 　]{2,}`) 切分為多個區塊，再各別處理，避免 AA 圖干擾。
*   **防 AA 圖干擾與正則匹配**:
    *   `base_regex`: 預設 `([：＋a-zA-ZＡ-Ｚ０-９0-9ぁ-んァ-ヶ一-龠々〆〤【】（）「」！？、。…，．？！,.―ーッ%]{3,})`，取出大於等於3個字元的詞塊。
    *   `invalid_regex`: 過濾無意義符號碎片（全行都是 AA 符號的情況）。
    *   `symbol_regex`: 統計邊界或 AA 符號比例，超過 50% 則認定該塊為 AA 並跳過。
*   **自訂正則過濾**: 讀取 UI 上的過濾規則 (每行一條正則) 將其排除。
*   **字串後處理**:
    *   若被 `│` 或 `|` 邊框包圍，只取邊框內的文字
    *   去除非平假名/片假名/漢字/英文/數字等開頭字元
    *   移除結尾的骰子格式 `【數字D數字:數字】`
    *   移除開頭的「數字+點」格式（如 `１．`、`1.`）
    *   移除句尾 `|`、`（`、`(` 等符號
*   **資料去重與排版**: 將過濾後的字詞存入 Dict 作去重，以 `{行號:03d}-{流水號}|日文原文` 格式輸出，並顯示提取計數於 `ext_count_label`。
*   **自動複製**: 若 `auto_copy_switch` 開啟，提取後自動複製至剪貼簿。
*   **Debug 工具**: Toolbar 上的「提取 Debug」按鈕(`analyze_extraction()`)可對選取文字逐步分析提取流程，於 Modal 顯示報告。

### 4.2 分割複製 (`copy_split()`)
*   **對應 Function**: `def copy_split(self, half):`
*   支援三種模式：`'top'` (上半部)、`'bottom'` (下半部)、`'all'` (全部)，複製至剪貼簿。
*   介面上對應「複製上半」、「複製下半」、「複製全部」三顆按鈕。

### 4.3 文本替換與自動對齊邏輯 (`apply_translation()`)
*   **對應 Function**: `def apply_translation(self):`
*   **映射表還原**: 讀取「提取結果」與「翻譯結果」，透過 `|` 分割還原為 Dict。
*   **合併術語表**: `get_combined_glossary()` 將一般術語表與臨時術語表合併後使用。
*   **長度優先排序策略 (Crucial)**: `valid_ids.sort(key=lambda k: len(orig_map[k]), reverse=True)`，由「最長的原文」優先替換，防止巢狀取代問題。
*   **術語與自動補全形空白演算法 (Auto-Padding)**:
    *   翻譯句套用術語表後，計算字元長度差：`len_diff = len(original) - len(final_translated)`。
    *   若原文大於翻譯文，在翻譯文後綴補足等量全形空白。
*   **替換執行**: 利用 `re.escape(original)` 轉換為正則，依行逐條替換。遇到保留字（對話框線 `|`、`│`、`｜`、`┃`）時套用補空白後的字串，否則直接替換以維護排版。
*   **全域術語覆蓋**: 替換完成後，對全文中未被提取的原文部分，再跑一次術語表全域取代（同樣套用 Auto-Padding 與邊框判定）。

### 4.4 全螢幕預覽狀態與進階後處理 (`show_result_modal()`)
*   **對應 Function**: `def show_result_modal(self, text, source_file="", scroll_to_line=None):`
*   `source_file` 參數：由批次模式開啟時傳入，儲存時直接覆寫原檔。
*   `scroll_to_line` 參數：開啟後自動捲動並選取指定行（用於批次搜尋跳轉）。
*   **顯示方式**: 始終嵌入 `edit_frame`（內嵌編輯模式），不開獨立視窗。從編輯模式返回時，若有批次搜尋視窗在背景，會自動恢復顯示。
*   預覽視窗上方有 Toolbar，操作以 Nested function 定義，分三群組：

    **群組 1 — 全文替換:**
    *   **即時術語替換 (`quick_replace()`)**: 填寫原文/翻譯並執行，套用 Auto-Padding 與邊框判定，可選擇存入主視窗一般術語表。
    *   **重套術語 (`reapply_glossary()`)**: 根據合併後術語表再次對全文執行替換（含 Auto-Padding 與邊框判定）。

    **群組 2 — 選區操作:**
    *   **富文本上色 (`apply_color()`)**: 以 `<span style="color:...">` 包覆選取文字。若選取範圍已含上色標籤，則進入「去色模式」移除所有 span。支援點擊色塊切換顏色 (`choose_color()`)。
    *   **消空白 (`strip_spaces()`)**: 刪除選取文字內所有半形/全形空白。
    *   **補空白 (`add_double_spaces()`)**: 在選取文字的每個字元之間插入兩個全形空白。
    *   **自動對話框 (`adjust_bubble()`)**: 選取行後進行智慧邊框對齊，支援三種對話框格式：
        1. **普通對話框** (`´￣￣￣｀ヽ` / `乂＿＿＿ノ`)：動態計算上下框長度，對齊右側角落符號。
        2. **吶喊框** (`､__人_人_...` / `⌒Y⌒Y...⌒Ｙ`)：識別 `）...（` 等內容分隔符，重建上下框與內容至統一目標寬度。
        3. **斜線框** (`＼─|──|─／` / `／─|──|─＼`)：與吶喊框邏輯類似，辨識 `│` 或 `─` 作為內容分隔符重建邊框。
    *   **對話框(全) (`adjust_all_bubbles()`)**: 掃描全文，自動偵測並對齊所有獨立對話框（普通/吶喊/斜線三種），由下而上逐框處理以避免行號偏移。
    *   **對齊上一行 (`align_to_prev_line()`)**: 於游標處往前補足空白，直到與上一行結尾長度切齊。
    *   **智慧判斷 (`smart_action()`)**: (Hotkey: `Ctrl+Q`) 自動判斷：選取多行 → `adjust_bubble()`；選取單行區塊 → `apply_color()`；僅有游標 → `align_to_prev_line()`。

    **群組 3 — 視窗控制:**
    *   **底色/文字色**: `choose_bg_color()` / `choose_fg_color()` 調整預覽視窗的背景色與文字色，設定持久化至暫存。
    *   **儲存 HTML (`dl_html()`)**: 將預覽內容封裝進帶有 CSS 的 HTML 並儲存；利用 `write_html_file()` 逸出 `< >`，同時保全 `<span>` tag。
    *   **關閉/返回**: 關閉前自動將當前預覽內容寫入 `preview_text_cache` 並呼叫 `save_cache()`。

    **搜尋列 (預設隱藏，`Ctrl+F` 切換):**
    *   `find_next()`: 在預覽文字中循環搜尋（不分大小寫）。
    *   **骰子搜尋**: 快速搜尋殘留的 `1D10:10` 骰子格式。
    *   `Ctrl+S` 快捷鍵觸發儲存 HTML。

### 4.5 網址讀取功能 (`open_url_fetch_dialog()`)
*   **對應 Function**: `def open_url_fetch_dialog(self):`
*   開啟一個 Dialog，可輸入網址直接抓取日文 AA 漫畫的頁面文字。
*   **網頁解析 (`_parse_page_html()`)**: 支援解析 5ch 類型網站 (`div.article` → `dt/dd` 結構)，提取文章內文、頁面標題、以及 `dl.relate_dl` 中的關聯記事導航連結。
*   **編碼自動偵測**: 依序嘗試 `utf-8`、`cp932`、`euc-jp`、`shift_jis`。
*   **Gzip 解壓縮**: 自動解壓縮伺服器回傳的 gzip 內容。
*   **關聯記事導航**: 顯示同系列各話的連結清單，可直接點擊切換；提供上一話/下一話按鈕。
*   **讀取歷史記錄**: 最多保存 50 筆，可點擊重新讀取，支援清除記錄。
*   **直接讀取下一話 (`fetch_next_chapter()`)**: Toolbar 上的「下一話 ▶」按鈕，從已存的關聯記事中直接背景讀取下一話。
*   **章節號碼自動偵測 (`check_chapter_number()`)**: 貼上或讀取文字後，掃描前五行尋找 `第N話` 或 `番外編N` 格式，自動填入話數欄位。
*   **複製網址 (`copy_current_url()`)**: 複製目前讀取的網址至剪貼簿。

### 4.6 批次搜尋（獨立 PyQt6 視窗）
*   **檔案**: `aa_batch_search_qt.py` — `BatchSearchWindow(QMainWindow)`
*   **啟動方式**: 主程式透過 `open_batch_search_qt()` 以 subprocess 啟動，傳入 `--cmd-file`（Qt→主程式）、`--reverse-cmd-file`（主程式→Qt）、`--folder` 參數。
*   **IPC 雙向通訊**:
    *   Qt→主程式（`cmd_file`）：`open`（開啟檔案並跳行）、`sync_folder`（同步資料夾路徑）
    *   主程式→Qt（`reverse_cmd_file`）：`restore`（恢復視窗顯示）
*   **搜尋**: 在指定資料夾所有 HTML 的 `<pre>` 內容中搜尋，支援字串或正則，最多 500 筆結果。背景執行緒搜尋 + 分批渲染。
*   **單筆替換 / 全部替換 / 復原**: 與舊版邏輯相同，另支援檔案層級的復原（`_undo_backups`）。
*   **排除功能（✕ 按鈕）**: 可將特定搜尋結果從替換範圍中移除（不會被「全部替換」影響），並可隨時恢復。
*   **開啟並跳轉**: 點擊「開啟」後，Qt 視窗自動縮小，主程式開啟檔案進入編輯模式；從編輯模式返回時，Qt 視窗自動恢復顯示。
*   **資料夾同步**: Qt 視窗中選擇的資料夾會在開啟檔案與關閉視窗時同步回主程式，並持久化至暫存。

### 4.7 狀態讀寫裝載管理
*   **讀取/儲存暫存**:
    *   `load_cache(load_preview_text=False)`: 讀取 `aa_settings_cache.json`，包含原文、過濾規則、術語表(一般+臨時)、話數、標題、背景/文字色、URL 歷史、當前 URL、開關狀態等。`load_preview_text=True` 時才載入預覽暫存並詢問是否恢復。
    *   `save_cache()`: 儲存上述所有狀態。
    *   `schedule_save()`: 防抖動延遲 500ms 後呼叫 `save_cache()`，綁定至所有 TextBox 的 `KeyRelease` 事件。
*   **讀取/匯出設定檔**:
    *   `import_settings()`, `export_settings()`, `load_settings_at_startup()`: 管理 `AA_Settings.json`，包含自訂 Regex 規則、一般術語表與臨時術語表。
    *   `save_regex_to_settings()`: 僅更新 `AA_Settings.json` 中的三條正則，保留其他欄位不動。
    *   **優先順序**: 啟動時先讀暫存（保底），再讀 `AA_Settings.json`（優先覆蓋過濾規則與術語表）。
*   **外部裝載 (`import_html()`)**: 「打開已儲存的 HTML」，利用 `read_html_pre_content()` 從已生成的 HTML 中提取 `<pre>` 區塊內容並解碼，載入回預覽器繼續作業。
*   **HTML 讀寫 Helpers**:
    *   `read_html_pre_content(file_path)`: 讀取 HTML 並以 `re.search(r'<pre>...<\/pre>')` 擷取 `<pre>` 內容並 `html.unescape`。
    *   `write_html_file(file_path, text_content)`: 封裝 HTML（CSS 字體設定為 `MS PGothic`/`Meiryo` monospace, 16px），用分割保全 `<span>` tag 後 `html.escape` 其他內容。

### 4.8 UI 通知系統
*   **`show_toast(message, color, duration)`**: 在主視窗右上角顯示浮動提示，`duration` ms 後自動關閉。
*   **`show_confirm_toast(message, on_yes, color, duration)`**: 帶有「是/否」按鈕的確認浮動視窗，逾時（預設 8 秒）自動關閉。
*   **AI 翻譯格式驗證 (`validate_ai_text()`)**: 貼上翻譯文字後自動觸發，掃描每行是否含有多個 ID (`\d{2,4}-\d+\|`)，於 `ai_warn_label` 顯示警告或成功提示。

## 5. UI 介面關聯變數
*   `self.source_text`: 原文 TextBox（含 AA 圖）
*   `self.filter_text`: RegExp 過濾規則 TextBox
*   `self.glossary_text`: 一般術語表 TextBox（`日文=中文` 格式）
*   `self.glossary_text_temp`: 臨時術語表 TextBox（同格式，存入 `AA_Settings.json`）
*   `self.extracted_text`: 提取結果 TextBox
*   `self.ai_text`: AI 翻譯結果 TextBox
*   `self.doc_title`: 標題輸入欄（用於 HTML 檔名）
*   `self.doc_num`: 話數輸入欄（支援 +/- 按鈕；貼上/讀取時自動偵測）
*   `self.auto_copy_switch`: 提取後自動複製開關
*   `self.batch_folder_var`: 批次搜尋資料夾路徑（透過 IPC 與 Qt 批次搜尋視窗同步）
*   `self.bg_color` / `self.fg_color`: 預覽視窗背景/文字色（持久化至暫存）
*   `self.preview_text_cache`: 預覽視窗關閉前的文字暫存（關閉時寫入 `save_cache()`，下次開啟可選擇恢復）
*   `self.url_history`: URL 讀取歷史（最多 50 筆）
*   `self.url_related_links`: 當前頁面的關聯記事連結清單
*   `self.current_url`: 當前讀取的網址
*   `final_textbox` (在 `show_result_modal` 內): 全螢幕預覽模式的唯一文字輸出框，支援滑鼠滾輪（每格 8 行）快捷鍵客製。

## 6. 給後續開發維護 AI 的系統提示 (For AI Agents)
1.  **確保對齊機制的字體計算**: 介面使用 `self.result_font.measure(...)` 依賴 Tkinter Font 測量文字在 `MS PGothic` 下的實際物理寬度 (pixel)。這是所有對話框對齊操作的核心基礎，**切勿改變字體設定**，否則所有自動對齊功能會失準。
2.  **效能考量**: 處理長文本時 `re.sub` 或大量 TextBox 修改 (`delete`, `insert`) 會造成一定延遲。必要時請以 string method 替代，或一次性取出大量 list 再 `'\n'.join()` 放回，減少 GUI 卡頓。
3.  **保持替換長度優先排序規矩**: 修改 `apply_translation()` 邏輯時，必須延續「由原文長度遞減排序 (`valid_ids.sort`) 後再 replace」，否則疊加翻譯文將互相破壞（如先替「あ」再替「あはは」）。
4.  **術語表有兩張**: 一般術語表 (`glossary_text`) 與臨時術語表 (`glossary_text_temp`)。所有需要套用術語的地方都應呼叫 `get_combined_glossary()` 合併後使用，而非只讀取其中一個。
5.  **批次操作的行號偏移**: `batch_replace_all()` 和 `adjust_all_bubbles()` 都採用「由下而上」或「由行尾至行首」逐筆替換策略，以避免替換後行號/字元位置偏移。新增類似功能時請維持此慣例。
6.  **URL 讀取功能**: `_parse_page_html()` 的解析邏輯針對特定網站結構 (`div.article` / `dt`+`dd`)，若需支援其他網站，需另外擴充此函式的解析分支。
