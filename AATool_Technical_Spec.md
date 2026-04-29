# AA 創作翻譯輔助小工具 - AI 技術規格書與 Function 對照表

## 1. 系統架構與技術棧 (Architecture & Tech Stack)
*   **檔案組成**:
    *   `aa_main_qt.py` — PyQt6 主視窗 (`MainWindow(QMainWindow)` + `TranslatePanel(QWidget)`)
    *   `aa_edit_qt.py` — PyQt6 HTML 編輯器 (`EditWindow`)，嵌入主視窗的 `QStackedWidget`
    *   `aa_url_fetch_qt.py` — PyQt6 網址讀取視窗 (`UrlFetchWindow`)，以 subprocess 啟動
    *   `aa_batch_search_qt.py` — PyQt6 批次搜尋視窗 (`BatchSearchWindow`)，嵌入主視窗的 `QStackedWidget`
    *   `aa_wiki_name_dialog_qt.py` — PyQt6 Wiki 角色日中對照抓取 Dialog (`WikiNameDialog`)，非 modal 獨立對話框
    *   `aa_qt_font_test.py` — PyQt6 字型引擎驗證小工具（獨立測試用）
    *   `aa_tool/` — 純邏輯模組（無 UI 依賴）：`constants`、`font_measure`（Protocol）、`html_io`、`settings_manager`、`text_extraction`、`translation_engine`、`bubble_alignment`、`url_fetcher`、`wiki_name_fetcher`、`qt_helpers`、`dark_theme.qss`
*   **技術棧**: Python 3, **PyQt6** (UI 框架), `re`, `math`, `os`, `html`, `urllib.request`, `threading`, `gzip`, `json`, `subprocess`
*   **狀態存儲機制**:
    *   `aa_settings_cache.json` — 暫存 UI 狀態 (原文、過濾規則、術語表、話數、預覽暫存、URL 記錄、背景/文字色、各開關狀態、作品+作者歷史 `work_history` 等)，確保關閉重開後不丟失資料。**正則表達式不從暫存讀取**，由 `AA_Settings.json` 管理。
    *   `AA_Settings.json` — 正式設定檔。寫入順序固定為 `base_regex`、`invalid_regex`、`symbol_regex`、`filter`、`glossary`、`glossary_temp`（regex 在前、文字內容在後，方便人工編輯時優先看到 regex）。讀取使用 `data.get(key)`，不依賴順序。`save_regex_to_settings()` 也會以同一順序重建 dict 寫回。
    *   `aa_original_cache.json` — 原文暫存。**主索引為 HTML 檔名 basename**，值為 `{text, ts, author_key?}`；**上限由 `aa_settings_cache.json` 的 `fetch_history_limit` 控制（預設 50）**，超過時以時間戳保留最新。編輯器儲存／翻譯按鈕按下時寫入（參見 §4.3）。`import_html` 或從批次搜尋開啟同名檔案時自動載入為比對原文。**備援索引**：`author_key` 為從原文第一則投稿標頭抽出的「日期 + 時間.毫秒 + ID」指紋（例：`2023/04/02(日) 20:54:38.52 ID:5UkYdPSV`），由 `MainWindow._compute_author_fingerprint()` 生成。`load_original_for_file()` 檔名查無時會讀取該 HTML 的 `<pre>` 內容、算出同樣指紋，掃描 cache 中 `author_key` 相符的 entry 作為 fallback；舊 entry 若無 `author_key` 欄位，在掃描當下以同規則從 `entry['text']` 即時計算，不需強制 migration。用途：使用者重新命名 HTML 檔後仍能找到對應原文。
    *   `aa_settings_cache.json` 新增 key：`work_history_limit`（作者歷史上限，預設 10）、`fetch_history_limit`（URL 讀取紀錄上限，預設 50）、`original_cache_limit`（`aa_original_cache.json` 上限，預設 50；舊版只有 `fetch_history_limit` 共用，讀取時若新欄位缺失則沿用 `fetch_history_limit` 作為遷移值）、`glossary_auto_search`（批次搜尋術語按鈕是否自動搜尋，預設 True）、`editor_default_wysiwyg`（進入編輯器時是否自動切換成所見即所得模式，預設 False）、`embed_font_name`（儲存 HTML 時要內嵌的字型名稱，可選 `"monapo"` / `"Saitamaar"` / `"textar"`，預設 `"monapo"`）；皆由 ⚙ 設定視窗（詳見 §4.12）調整並持久化。
    *   `aa_crash.log` — 啟動期錯誤日誌（append 模式）。`aa_tool/crash_logger.py` 的 `install_crash_logger()` 於 `main()` 最前面呼叫一次，安裝三層攔截：(1) `faulthandler.enable(file=...)` 捕 C 層 segfault（PyQt6 widget 生命期問題等）並 dump 所有執行緒的 C 堆疊；(2) `sys.excepthook` 記錄未捕捉的 Python 例外；(3) `qInstallMessageHandler` 記錄 Qt 的 WARNING / CRITICAL / FATAL 訊息（DEBUG/INFO 過濾）。閃退時打開檔案即可看到最後一次 session 的 trace。
*   **字體設計**:
    *   PyQt6 編輯器 (`aa_edit_qt.py`) 預設 **`MS PGothic` 12pt** — **⚠️ 不可改動預設值**：整個專案所有 AA 對齊演算法（`bubble_alignment.py`、`_pad_to_width`、吶喊/斜線/普通/方框寬度計算、`QtFontMeasurer`）都以 MS PGothic 的字寬 metrics 為基準；一旦改成 submona / Meiryo / 其他字體，對齊結果會全部錯位。`DEFAULT_EDITOR_FONT` 常數與 `aa_settings_cache.json` 的 `editor_font_family` 都必須維持 `"MS PGothic"`。
    *   `Ctrl+F` 延伸出的搜尋列提供字體下拉（可編輯，預設清單為 **MS PGothic / Monapo / TEXTAR / Saitamaar**，定義於 `aa_edit_qt.py:EDITOR_FONT_CHOICES`）與 6–48pt `QSpinBox`，**僅供使用者臨時切換預覽效果**，不應修改預設值。變更後同步更新 `editor`、`orig_view`、`_measurer`、CSS 與行高；使用者個人選擇持久化於 `aa_settings_cache.json` 的 `editor_font_family` / `editor_font_size`
    *   **內建字體載入** (`load_bundled_fonts()`)：`aa_main_qt.main()` 與 `aa_edit_qt.main()` 在 QApplication 建立後各呼叫一次，掃描 `fonts/` 資料夾中的 `.ttf` / `.otf` 並以 `QFontDatabase.addApplicationFont()` 載入；目前含 `fonts/monapo.ttf`（家族名 `Monapo`）、`fonts/textar.ttf`（家族名 `TEXTAR`）、`fonts/Saitamaar.ttf`（家族名 `Saitamaar`）。要新增字型只需放入 `fonts/`，並把家族名加進 `EDITOR_FONT_CHOICES`。
    *   主視窗 UI 字體以 Qt 預設 Style Sheet（`aa_tool/dark_theme.qss`）控制；主面板各輸入區共用 `MS PGothic` 作為顯示字體以利 AA 對齊預覽。

## 2. 核心目標與工作流程 (Core Workflow)
主要目標為協助使用者翻譯帶有 ASCII Art (AA) 圖像的漫畫文本，並**在替換成中文翻譯時，自動計算字元差異並補上全形空白**，以避免 AA 圖案因語系字元長度差異而引發的排版崩壞。
*   **Step 1**: 貼上或從網址讀取包含 AA 圖與日文原文的原始文本至「原始文本」區塊。
*   **Step 2**: 點擊「提取日文」，系統將基於規則與正則表達式，提取出需要翻譯的純文字片段，附加三位數行號流水號 ID (`001-1|text`)。
*   **Step 3**: 將提取出的列表交由外部（如 ChatGPT 或 Claude）翻譯後，將對應的翻譯結果貼回「翻譯結果」區塊。
*   **Step 4**: 點擊「替換翻譯並編輯」，系統執行精準的文本替換、術語替換（包含全文術語覆蓋）與自動補全形空白的機制。
*   **Step 5**: 進入全螢幕或內嵌預覽視窗，在最終文本上進行微調（自動對話框、選區上色、對齊等），完成後點擊儲存匯出為 `.html`。

## 3. 主要模式 (Application Modes)
主視窗以 `QStackedWidget` 管理三個面板，由 `MainWindow` 以 `show_translate_panel()` / `show_edit_panel()` / `show_batch_search_panel()` 切換：
*   **index 0 — `TranslatePanel`**（翻譯主面板，預設）
*   **index 1 — `EditWindow`**（HTML 編輯，來自 `aa_edit_qt.py`，內嵌）
*   **index 2 — `BatchSearchWindow`**（批次搜尋，來自 `aa_batch_search_qt.py`，內嵌）

進入 sub-panel（edit/batch）時會顯示頂部 `nav_bar` 返回按鈕；網址讀取視窗 (`aa_url_fetch_qt.py`) 仍以 subprocess 獨立視窗呈現並透過 IPC 與主程式溝通（詳見 4.5）。

主視窗工具列（`TranslatePanel._build_toolbar`）在「批次搜尋」按鈕右側另提供「編輯模式」按鈕，對應 `MainWindow.resume_edit_panel()`：若目前有開啟中的 `EditWindow`，會直接切回編輯面板，避免從編輯模式誤按返回後需要重新走流程。

主視窗標題「AA 創作翻譯輔助小工具」右側提供 **⚙ 設定按鈕**，對應 `MainWindow.open_settings_dialog()`（詳見 §4.12）。

## 4. 核心模組與對應 Function 解析 (Core Functions)
未來若要修改特定功能，請參考此清單尋找對應的 Python function。主程式類別為 `MainWindow(QMainWindow)`（`aa_main_qt.py`），翻譯面板為 `TranslatePanel(QWidget)`。

### 4.1 文本提取邏輯 (`extract_text()`)
*   **對應 Function**: `def extract_text(self):`
*   **發文者行整行剔除**: 若整行命中 `_POSTER_LINE_RE = re.compile(r'ID:[A-Za-z0-9+/.]{6,}')`（2ch/5ch 發文者行的 trip code 標記，例如「4402 ： ◆GESU1/dEaE ： 2021/05/06(木) 23:19:36 ID:nGcM5Umt」），整行跳過不提取。`analyze_extraction` 亦同步顯示「發文者行」剔除訊息。
*   **行分塊前處理**: 每行先以連續兩個以上半形/全形空白 (`[ 　]{2,}`) 切分為多個區塊，再各別處理，避免 AA 圖干擾。
*   **防 AA 圖干擾與正則匹配**:
    *   `base_regex`: 預設 `([：＋a-zA-ZＡ-Ｚ０-９0-9ぁ-んァ-ヶ一-龠々〆〤【】（）「」！？、。…，．？！,.―ーッ%]{3,})`，取出大於等於3個字元的詞塊。
    *   `invalid_regex`: 過濾無意義符號碎片（全行都是 AA 符號的情況）。
    *   `symbol_regex`: 統計邊界或 AA 符號比例，超過 50% 則認定該塊為 AA 並跳過。
*   **字串後處理**:
    *   若被 `│` 或 `|` 邊框包圍，只取邊框內的文字
    *   去除非平假名/片假名/漢字/英文/數字等開頭字元
    *   移除結尾的骰子格式 `【數字D數字:數字】`
    *   移除開頭的「數字+點」格式（如 `１．`、`1.`）
    *   移除句尾 `|`、`（`、`(` 等符號
*   **括號自動補完**: 提取後處理後，檢查文字中的括號（`【】「」""''『』（）()`）是否成對。處理兩種情況：(1) `text` 內部有未配對括號 → 在 `source_line` 中定位 `text`，若未配對右括號前一格或未配對左括號後一格恰為對應括號則補上；(2) `text` 內沒有括號但整個括號在 `text` 外（被提取時剝掉）→ 檢查 `source_line` 中 `text` 前一格為開括號 / 後一格為閉括號則補回（避免重複補已有的同字元）。對應 Function: `_complete_brackets(text, source_line)`。
*   **自訂正則過濾（作用於最終結果）**: 讀取 UI 上的過濾規則（每行一條正則），於整個提取流程（基底 regex、無效符號、後處理、括號補完）**結束之後**才對最終提取結果執行 `reg.search(text)` 比對；命中任一條即剔除。`analyze_extraction` 的步驟順序亦同步調整為「後處理 → 長度檢驗 → 自訂濾網」，報告訊息明確標示「對最終提取結果過濾」。
*   **資料去重與排版**: 將過濾後的字詞存入 Dict 作去重，以 `{行號:03d}-{流水號}|日文原文` 格式輸出，並顯示提取計數於 `ext_count_label`。
*   **URL 來源跳過規則**: `extract_text(..., skip_title, author_name)` 提供兩個可選參數：
    *   `skip_title`：若 source 的第一個非空行內容等於此字串，則整行跳過（URL 讀取會以作品標題作為首行，此機制避免標題被提取）。主程式傳入 `self._last_fetched_title`，該值於 URL 讀取／下一話成功時寫入；若使用者手動貼上新文字覆蓋標題行則不會命中，規則自動失效。
    *   `author_name`：提取結果經過後處理後，若去除前後空白等於此名稱則剔除（避免作者自述／簽名單獨被提取）。主程式傳入 `self._author_name`。另因作者名稱常含開頭符號（如「◆Hr94QM5gdI」），而提取後處理會去掉符號僅保留英數字 trip code，故會從 `author_name` 以 `re.findall(r'[A-Za-z0-9]{6,}')` 抽出最長英數字串作為額外比對鍵（符合即剔除）。
*   **自動複製**: 若 `auto_copy_switch` 開啟，提取後自動複製至剪貼簿。
*   **Debug 工具**: Toolbar 上的「提取 Debug」按鈕(`analyze_extraction()`)可對選取文字逐步分析提取流程，於 Modal 顯示報告。

### 4.2a 加入自訂過濾器 (`add_selection_to_filter()`)
*   **對應 Function**: `MainWindow.add_selection_to_filter()`；按鈕位於「提取結果」標題列左側（「複製全部」左邊）。
*   取 `extracted_text` 目前選取（`textCursor().selectedText()`，`\u2029` 轉回 `\n`），逐行以 `^\s*\d{2,4}-\d+\|` 去除流水號前綴，空行略過。
*   去重後以 `\n` 串接到 `filter_text` 末尾；完全無新增時顯示 ℹ️ Toast、未選取時顯示 ⚠️ Toast。

### 4.2 分割複製 (`copy_split()`)
*   **對應 Function**: `def copy_split(self, half):`
*   支援三種模式：`'top'` (上半部)、`'bottom'` (下半部)、`'all'` (全部)，複製至剪貼簿。
*   介面上對應「複製上半」、「複製下半」、「複製全部」三顆按鈕。
*   **Toast 回饋**：複製後呼叫 `show_status()` 顯示「✅ 已複製{全部/上半/下半}（N 行）到剪貼簿」；若提取結果為空則顯示警告 Toast。

### 4.3 文本替換與自動對齊邏輯 (`apply_translation()`)
*   **對應 Function**: `def apply_translation(self):`
*   **映射表還原**: 讀取「提取結果」與「翻譯結果」，透過 `|` 分割還原為 Dict。
*   **對應率檢查（Toast 警告）**: 按下「替換翻譯並編輯」或「翻譯並直接儲存」後、進入替換流程前，先計算提取 ID 與翻譯 ID 的交集 / 提取 ID 總數；若低於 `50%` 以 `show_status("⚠️ 原文跟翻譯可能不對應（對應率 XX%）", "#f39c12")` 顯示 Toast。僅提示，不中斷流程（仍會執行替換）。提取 ID 為空時不觸發檢查。
*   **共用前處理 `_prepare_translation()`**: 驗證輸入、記錄歷史、對應率檢查、跑替換、組標題/檔名後回傳 `(result_text, source, name_base, display_title)`；`apply_translation()`（替換後進編輯器）與 `apply_translation_and_save()`（替換後 QFileDialog 直接存檔）共用此前處理。
*   **「翻譯並直接儲存」按鈕 (`apply_translation_and_save()`)**: 首頁底部 action bar 中、位於「替換翻譯並編輯」右側、「讀入暫存」左側（寬 120、高 44，綠色 `#28a745`）。`_prepare_translation()` 完成後以 `QFileDialog.getSaveFileName` 詢問存檔路徑（預設檔名為 `{title}_{num}.html`、預設目錄 `self._last_dir`），使用者確認後 `write_html_file()` 直接寫檔（非暫存、不進編輯器），同步更新 `_last_dir` 與暫存。
*   **翻譯時立即寫入原文 cache（⚠️ 重要）**: `apply_translation()` 與 `apply_translation_and_save()` 都會在 `write_html_file()` 成功後 **立即** 呼叫 `self.save_original_for_file(file_path, source.rstrip('\n'))`，把原始文本寫入 `aa_original_cache.json`。此修正解決原本「cache 只在 `_on_edit_saved()` 時寫入」造成的漏洞：先前 `apply_translation_and_save` 完全不寫 cache、`apply_translation` 走了編輯器但使用者未按 Ctrl+S 也不寫 cache，導致之後從批次搜尋開啟該檔時 Alt+2 比對原文讀不到。加入這兩行後，只要按下任一翻譯按鈕，原文即保證進入 cache（不依賴使用者之後是否在編輯器內按儲存）。`_on_edit_saved()` 的寫入維持不動，兩者屬於 idempotent 重複寫入同一 key（時間戳更新）。
*   **合併術語表**: `get_combined_glossary()` 將一般術語表與臨時術語表合併後使用。
*   **長度優先排序策略 (Crucial)**: `valid_ids.sort(key=lambda k: len(orig_map[k]), reverse=True)`，由「最長的原文」優先替換，防止巢狀取代問題。
*   **術語與自動補全形空白演算法 (Auto-Padding)**:
    *   翻譯句套用術語表後，計算字元長度差：`len_diff = len(original) - len(final_translated)`。
    *   若原文大於翻譯文，在翻譯文後綴補足等量全形空白。
*   **替換執行**: 利用 `re.escape(original)` 轉換為正則，依行逐條替換。遇到保留字（對話框線 `|`、`│`、`｜`、`┃`）時套用補空白後的字串，否則直接替換以維護排版。
*   **全域術語覆蓋**: 替換完成後，對全文中未被提取的原文部分，再跑一次術語表全域取代（同樣套用 Auto-Padding 與邊框判定）。

### 4.4 HTML 編輯器狀態與進階後處理 (`EditWindow` — `aa_edit_qt.py`)
*   **對應**: `MainWindow.show_edit_panel(text, source_file="", scroll_to_line=None, back_callback=None)` 載入 `EditWindow`（PyQt6）。`back_callback` 決定編輯器「返回」按鈕目標：預設 `None` → `show_translate_panel`；從批次搜尋開啟（`_on_batch_open_file`）時傳入 `show_batch_panel`，按返回會回到批次搜尋介面。重新載入既有 `EditWindow` 時也會同步更新 `_edit_window._on_back`。
*   `source_file` 參數：由批次模式或 `import_html` 傳入，儲存時直接覆寫原檔。
*   `scroll_to_line` 參數：開啟後自動捲動並選取指定行（用於批次搜尋跳轉）。
*   **顯示方式**: 嵌入主視窗 `QStackedWidget`（index 1），不開獨立視窗。從編輯模式返回時，若有批次搜尋面板在背景，會自動恢復顯示。
*   編輯器上方有 Toolbar，操作分三群組：

    **群組 1 — 全文替換:**
    *   **即時術語替換 (`quick_replace()`)**: 填寫原文/翻譯並執行，套用 Auto-Padding 與邊框判定，可選擇存入主視窗一般術語表。
    *   **重套術語 (`reapply_glossary()`)**: 根據合併後術語表再次對全文執行替換（含 Auto-Padding 與邊框判定）。

    **群組 2 — 選區操作:**
    *   **富文本上色 (`apply_color()`)**: 以 `<span style="color:...">` 包覆選取文字。若選取範圍已含上色標籤，則進入「去色模式」移除所有 span。支援點擊色塊切換顏色 (`choose_color()`)。
    *   **消空白 (`strip_spaces()`)**: 刪除選取文字內所有半形/全形空白。
    *   **補空白 (`add_double_spaces()`)**: 在選取文字的每個字元之間插入兩個全形空白。
    *   **自動對話框 (`adjust_bubble()`)**: 選取行後進行智慧邊框對齊，支援四種對話框格式：
        1. **普通對話框** (`´￣￣￣｀ヽ` / `乂＿＿＿ノ`)：動態計算上下框長度，對齊右側角落符號。
        2. **吶喊框** (`､__人_人_...` / `⌒Y⌒Y...⌒Ｙ`)：識別 `）...（` 等內容分隔符，重建上下框與內容至統一目標寬度。
        3. **斜線框** (`＼─|──|─／` / `／─|──|─＼`)：與吶喊框邏輯類似，辨識 `│` 或 `─` 作為內容分隔符重建邊框。
        4. **方框** (`┌─┐` / `│…│` / `└─┘`)：邏輯同上。
        * **Padding 字元定義**：半形空白 ` `、全形空白 `　`、半形點 `.` 三者統一視為對齊填白（常數 `_PAD_CHARS`）。`.` 等價於空白是為了相容 AA 作者以點代替空白避免瀏覽器壓縮的慣用寫法。
        * **統一寬度公式**：四種框型一律以「內容寬」為目標寬。方框/吶喊/斜線 `tw = 內容最大寬 + 一個全形空白寬`（其量測時已含一個全形邊距，加總後等同視覺上多 1 格）。普通框 `tw = border 左側錨點寬 + 對話框內側最大寬 + border 右側錨點寬`，**不額外加全形空白餘裕**——因為「對話框內側最大寬」量測自 content 行最右邊 `|`/`│`/`｜` 之後到末尾非 padding，這段已自然包含框內 leading pad（如 `　 ` ≈ 1.5 fw），就是視覺餘裕本身；若再加會多出 1-2 個 ￣ 不必要延伸。**會依內容縮減**：原邊框比內容寬時也會被壓縮回內容寬度。**普通框 ref_n 對稱重建**：上下邊框 (`f´…￣` / `乂…＿`) 用 top border 算出的 ￣ 數量套到所有 border，維持視覺對稱；各 border 自身的 right (`｀ヽ` 2 fw vs `ノ` 1 fw) 不同會讓總寬差 1 fw，符合手寫慣例。**選 content 行最右邊 `|`/`│`/`｜` 而不是用 content.left 整段**，是為避開 content 行 AA 前綴與 border 行 AA 前綴在字寬量測上的差異（例 `,,..-＜` 累積寬度與 `（＿ノ--'　　ﾉ` 累積寬度可能差 ~3 fw），否則本應縮減的對話框會被誤判延伸。「內容寬度」透過 `_content_width()`（`m.measure(text.rstrip(_PAD_CHARS))`）量測。不再使用 `VALID_TEXT_RE` 的特殊字元白名單。
        * **浮點量測**：`QtFontMeasurer.measure()` 回傳 `QFontMetricsF.horizontalAdvance()` 的浮點值，避免多次 snap 累積 ±0.5px 誤差。`_pad_to_width()` 內建最終 snap，無需另外的 `_pad_to_width_snap`。
    *   **對話框(全) (`adjust_all_bubbles()`)**: 掃描全文，自動偵測並對齊所有獨立對話框（普通/吶喊/斜線/方框），由下而上逐框處理以避免行號偏移。與單選修正共用同一組 `process_*` 函式與 padding/寬度規則。
    *   **對齊上一行 (`align_to_prev_line()`)**: 於游標處往前補足空白，將游標後第一個非空白字元對齊到上一行末端。演算法：① 找游標後第一個非空白字元 (`target_col`)；② 先以 `_PAD_CHARS` 剝除上一行尾端 padding 得 `prev_content`，再取 `m.measure(prev_content[:-1])` 作為目標寬（若 `prev_content` 僅 1 字元則取全寬，避免 `[:-1]` 產生 0 的 bug）；③ 以 `_pad_to_width()` 補空白至目標；④ 回傳 `res_prefix + ' ' + selected_text`。UI 端（`_align_to_prev`）對 `QTextBlock.text()` 加入 `\u2029` 防禦，與 `_adjust_bubble` 一致。
    *   **智慧判斷 (`smart_action()`)**: (Hotkey: `Alt+Q`) 自動判斷：選取多行 → `adjust_bubble()`；選取單行區塊 → `apply_color()`；僅有游標 → `align_to_prev_line()`。

    **群組 3 — 視窗控制:**
    *   **底色**: `_choose_bg()` 於 PyQt6 編輯器（`aa_edit_qt.py`）可調整編輯區底色，**僅作為編輯中的視覺效果**，儲存 HTML 時不寫入 `bg_color`（交由後續網站樣式控制）。
    *   **行高設定**: `_apply_line_height_to()` 取 `max(主字型 lineSpacing × 120%, Microsoft JhengHei lineSpacing × 1.02)` 作為固定像素值，套用 `LineHeightTypes.FixedHeight`。此作法可避免繁中字元（如「嗯」）觸發系統 fallback 字型時，`ProportionalHeight` 以該 fallback 字型較大的 ascent+descent 為基準導致單行行距被撐高的問題；同時取 CJK fallback 字型高度的 max 可確保 fallback 字不會被切頂/擠壓，保留純日文行的 120% 視覺比例。字型家族或大小變更時於 `_apply_editor_font()` 末尾重新計算並套用。另於 `_on_changed()` 呼叫 `editor.viewport().update()`，避免刪除 fallback 字元後殘留渲染痕跡。
    *   **搜尋找不到時不跳位**: `_find_next()` 會先保存原本 `textCursor` 與捲動位置，找不到（含 wrap-around 仍無結果）時恢復，避免捲到最上面。
    *   **💾 儲存（PyQt6 編輯器）**: 工具列的「💾 儲存」按鈕執行 `_save_as()`，**永遠彈出另存新檔對話框**；`Ctrl+S` 對應 `_save_overwrite()`，若有真實檔案路徑則直接覆寫、若為 `apply_translation` 產生的暫存檔（`_is_temp_file=True`）則仍走另存。儲存成功後透過 `on_save` callback 通知 `MainWindow._on_edit_saved()`，由主程式更新導覽列/視窗標題並將 `_original_text` 寫入 `aa_original_cache.json`。
    *   **📂 開啟（PyQt6 編輯器）**: 位於「儲存」與「返回」之間，呼叫 `MainWindow.import_html()`，與主畫面「打開已儲存的 HTML」按鈕相同；開啟時會自動查找 `aa_original_cache.json` 中的同名紀錄作為比對原文。
    *   **進入編輯器時自動回到最上層 (`_scroll_to_top()`)**: `MainWindow.show_edit_panel()` 重新載入既有 `EditWindow` 時，若沒有指定 `scroll_to_line`，會呼叫 `_scroll_to_top()` 將游標與 `verticalScrollBar`/`horizontalScrollBar` 都歸零；避免「按替換進入」或「開啟舊檔」時還停留在上一份檔案的捲動位置。
    *   **檢視模式快捷鍵（Alt+1/2/3）**: `Alt+1 = _return_to_editor()` 回到編輯模式（若目前在比對/預覽則自動切回；已在編輯則僅 focus）；`Alt+2 = _toggle_compare()` 進/出原文比對；`Alt+3 = _toggle_preview()` 進/出上色預覽。三模式可直接互切：在比對模式按 Alt+3 會直接跳到預覽（沿用比對視圖的捲動位置），在預覽模式按 Alt+2 會直接跳到比對；同一模式的快捷鍵重複按下則退出回編輯（toggle）。
    *   **局部重套用面板 (`_toggle_translate_side()` — Hotkey: `Alt+4`)**: 在編輯器右側展開可同時編輯「提取結果」與「填入翻譯」的面板（兩個 `QTextEdit`），按下「重新套用」會用新內容重跑 `apply_translation`，但**只覆蓋目前可視行以下**的部分（可視行以上維持使用者已編輯的成果）。實作要點：
        1. **UI 結構**：把 `self.stack`（編輯/比對/預覽 `QStackedWidget`）與 `self._translate_side`（右側面板 `QWidget`）放進 `QSplitter(Horizontal)`，預設右側 hide；Alt+4 切換可見。
        2. **資料來源**：開啟時呼叫 `extracted_provider()` / `translation_provider()`（由主程式接到 `_translate_panel.get_extracted_text` / `get_ai_text`）拉最新內容；不會 cache 上次的編輯。
        3. **可視行對齊**：以 `editor.cursorForPosition(QPoint(0,0)).blockNumber()` 取得目前可視最頂行（0-based）。提取結果格式為 `NNN-N|text`（NNN 是 1-based source 行號），找出第一條 `id_line ≥ 可視行+1` 的 ID，把 `side_extracted` 與 `side_ai` 都捲到對應 ID 的那一行。
        4. **重新套用**：以 `self._original_text` 為 source 重跑 `apply_translation(source, side_extracted, side_ai, glossary)`；切割合併 `editor.lines[:top]` + `new_full.lines[top:]`；保留 `verticalScrollBar` 位置。
        5. **回寫主面板**：套用後呼叫 `extracted_setter` / `translation_setter` 把右側內容寫回主畫面的「提取結果」與「填入翻譯」，下次回主畫面看到的是修改過的版本。
        6. **限制**：必須有 `_original_text`（編輯器是從 `apply_translation` 進入時才會有）；比對/預覽模式中按下會提示先回編輯。
    *   **原文比對模式 (`_toggle_compare()` — Hotkey: `Alt+2`)**: 切換至 `QStackedWidget` index 1（`self.orig_view`，唯讀 `QTextEdit`），內容為傳入的 `_original_text`（或從 `aa_original_cache.json` 載入）。進入時同步游標行號與 `verticalScrollBar`；比對中停用 `_edit_buttons`、工具列染棕 (`#8b6f47`)。`orig_view` 底色沿用編輯器 `_bg_color`。
    *   **WYSIWYG 開檔自動重渲染**：`MainWindow.show_edit_panel()` 對既有 `EditWindow` `_replace_document()` 寫入新檔內容後，若 `_preview_active` 為 True 會立即呼叫 `_wysiwyg_rerender_after_editor_change()` 並**在捲動之前**執行，確保後續 `_scroll_to_line()` / `_scroll_to_top()` 能作用於最新的 preview 文件。`_scroll_to_line()` 與 `_scroll_to_top()` 在 `_preview_active=True` 時會同步把 `preview_view` 也捲到相同行（透過 `findBlockByLineNumber` 對 preview 文件取得對應 block）。批次搜尋從 WYSIWYG 預設開啟（`editor_default_wysiwyg=True`）切換到目標檔/目標行也走這條路徑：`_toggle_preview()` 之後額外補一次 `_scroll_to_line()` 才能正確捲到批次搜尋指定的行。先前需要手動 Alt+1 切回編輯再 Alt+3 切回預覽才會更新。
    *   **所見即所得編輯模式 (`_toggle_preview()` — Hotkey: `Alt+3`)**: 將編輯器文字中的 `<span style="color:...">...</span>` 標籤實際渲染為彩色（模擬瀏覽器輸出）並**直接在彩色檢視中編輯**，切換至 `QStackedWidget` 的 index 2（`self.preview_view`，WYSIWYG 模式下 `setReadOnly(False)`）。`_render_preview_doc()` 直接以 `QTextCursor` 逐段 `insertText`，依 `_COLOR_SPAN_RUN_RE` 解析出 (文字, 顏色) runs 並套用 `QTextCharFormat.setForeground`；**不使用 `setHtml`**，以避免 Qt 對 `<pre>` 區塊邊界產生的橫線與忽略 `line-height` 的問題。最後呼叫與編輯器相同的 `_apply_line_height_to(self.preview_view)` 取得一致的 FixedHeight 行高，使進入/離開預覽時的捲動位置可逐行對齊；同時套用與編輯器一致的底色/字型 stylesheet。預覽中停用 `_edit_buttons`（對話框/補空白/全文替換等需操作 plain text 的工具），但**保留 `_color_buttons`**（上色 / 🎨 取色器）可用，工具列染紫 (`#4a3470`)。

        實作要點：
        1. **資料模型維持單一來源**：`editor` 仍是「plain text + 字面 `<span>` markup」的 source of truth；preview_view 只在 WYSIWYG 期間以 `QTextCharFormat.foreground` 表現顏色。
        2. **離開時序列化** (`_sync_preview_to_editor()` → `_serialize_preview_to_markup()`)：走訪 `preview_view.document()` 的每個 `QTextBlock` / `QTextFragment`，foreground 顏色非 `#000000` 時包成 `<span style="color:#xxxxxx">…</span>`，否則 emit 原文，block 之間以 `\n` 分隔。完成後 `_replace_document` 寫回 editor，保留 verticalScrollBar。
        3. **進入時抑制 dirty**：`_render_preview_doc` 重建文件會觸發 `preview_view.textChanged`，用 `_preview_suppress_dirty` 旗標暫時擋掉 `_on_preview_changed`，避免「進入即標 dirty」。
        4. **使用者編輯標 dirty**：`preview_view.textChanged` → `_on_preview_changed` 在 WYSIWYG 期間觸發 `_dirty=True`，使 closeEvent 的「未儲存」提醒對 WYSIWYG 編輯也有效。
        5. **儲存路徑接通**：`_write_current()` 開頭判斷 `_preview_active`，先 `_sync_preview_to_editor()` 再從 editor 取 plain text 寫檔。Ctrl+S / 另存 / 工具列「← 返回」(`_handle_back_click`) / Esc (`_on_escape`) 都會觸發同步。Alt+1 (`_return_to_editor`) 走 `_toggle_preview` 的離開分支，自動序列化。
            - **Flag 翻轉順序陷阱**：`_sync_preview_to_editor()` 開頭有 `if not self._preview_active: return` 的安全檢查，因此在 `_toggle_preview` 與 `_toggle_compare` 中，**必須在翻轉 `_preview_active = False` 之前就呼叫 sync**，否則同步會被早期 return 擋掉，造成 WYSIWYG 中的編輯在切回 Alt+1 編輯模式 / Alt+2 比對模式時被「還原」。所有未來新增的「離開 WYSIWYG」路徑都必須遵守此順序（先 sync、再翻 flag）。
        6. **上色雙路徑** (`_apply_color`)：偵測 `_preview_active`，True → `_apply_color_wysiwyg()` 用 `cursor.mergeCharFormat(QTextCharFormat).setForeground` 直接改 fragment 顏色；若選取範圍中已有非黑顏色，再次按下會 merge 成黑色（移除顏色）。False → 既有的字面 `<span>` markup 包覆/拆除路徑。
        7. **Round-trip 注意**：既有 markup 中的 CSS 命名色（`color:red`）會被序列化正規化為 `#rrggbb`（`QColor.name()` 格式）。`<` `>` `&` 等字元保持原樣不做 HTML escape。
        8. **工具列功能對等**：WYSIWYG 模式下所有編輯工具與既有編輯模式對等，由各 handler 透過 `_active_edit_widget()` helper 路由到 preview_view（WYSIWYG）或 editor（一般）。具體分流：
            - **Selection-based 工具直接作用於 preview_view**（`_strip_spaces` / `_pad_spaces` / `_adjust_bubble` / `_align_to_prev` / `_smart_action` / `_reverse_glossary_replace` / `_restore_from_original` / `_extract_jp_from_selection`）。`_extend_selection_to_full_lines(target)` 改為帶 widget 參數。代價：被 `cursor.insertText()` 替換掉的選取範圍會以 cursor 預設 charFormat 寫入（顏色可能歸零），使用者可重新上色。
            - **Whole-doc 工具走 sync→run-on-editor→re-render**（`_replace_all` / `_on_glossary_received` / `_adjust_all_bubbles` / `_reapply_below_visible`）：`_sync_preview_to_editor()` 把 markup 還原回 editor → 在 editor 上跑既有邏輯（`_replace_document` 觸發 editor.textChanged → dirty）→ `_wysiwyg_rerender_after_editor_change()` 重建 preview。期間 `_preview_suppress_dirty=True` 避免 preview.textChanged 重複標 dirty。**這條路徑能完整保留顏色**，因為運算發生在帶字面 `<span>` markup 的 editor plain text 上。
            - **搜尋 Ctrl+F**（`_find_next` / `_hide_search`）改為作用於 `_active_edit_widget()`，WYSIWYG 中可直接搜尋彩色檢視。
            - **Alt+4 局部重套用**：解除 WYSIWYG 阻擋；`_get_visible_top_line()` 改用 `_active_edit_widget()` 取使用者實際看到的可視行；reapply 完走 `_wysiwyg_rerender_after_editor_change()` 即時更新彩色檢視。
            - **`_apply_line_height()`** 在 `_preview_active` 時連帶呼叫 `_apply_line_height_to(self.preview_view)`，避免插入新文字後行高不一致。
    *   **術語反向取代 (`_reverse_glossary_replace()` — Hotkey: `Alt+E`)**: 在使用者選取範圍內，把術語表等號「右邊」的替代文字還原回等號「左邊」的原文。透過既有 `self._glossary_provider` callback 取得合併術語表（一般 + 臨時），呼叫 `aa_tool.translation_engine.apply_reverse_glossary_to_text()`：以「替代文字 → 原文」反向 map 做長度遞減單輪 regex 取代，並會吃掉匹配尾端最多 `len(原文) - len(替代文字)` 個 `\u3000`（反向抵銷 Auto-Padding）。若反向 map 有衝突（多個原文對應同一替代文字），保留最長原文。替換後呼叫 `_apply_line_height()`。選取為空、術語表為空、無命中各有對應 toast。
    *   **選取範圍提取日文並複製 (`_extract_jp_from_selection()` — Hotkey: `Alt+C`)**: 在編輯模式或原文比對模式下有選取時，沿用主程式「提取日文」的正則邏輯（`aa_tool.text_extraction.extract_text`）剔除選取範圍內的 AA 圖形，只保留純文字行，以 `\n` 串接後寫入剪貼簿。資料來源：`_compare_active=True` 時讀 `orig_view`、否則讀 `editor`；預覽模式按下會提示切回。正則與過濾規則由 `EditWindow(extract_regex_provider=…)` callback 提供（主程式傳入 `(current_base_regex, current_invalid_regex, current_symbol_regex, get_filter_text())`），與主畫面「提取日文」使用同一組設定。選取為空、未取到文字各有對應 toast。與 Alt+W 差異：Alt+W 是把選取以原文覆寫（改變文件），Alt+C 只讀取選取並把剔 AA 後的結果放到剪貼簿（不改文件）。
    *   **ESC 返回 (`_on_escape()` — Hotkey: `Esc`)**: 搜尋列（Ctrl+F）開啟中時優先關閉搜尋列（對應舊行為 `_hide_search`）；否則呼叫 `self._on_back()` 回主畫面（無 back callback 時則 `self.close()` 關閉視窗）。
    *   **從原文覆蓋選取範圍 (`_restore_from_original()` — Hotkey: `Alt+W`)**: 編輯模式下有選取時，將選取範圍以原文同一 (行, 欄) 區間的內容覆蓋。實作以 `QTextCursor` 解析選取起訖的 `blockNumber()` / `positionInBlock()`，在 `self._original_text.split('\n')` 上取出對應子字串（單行 → `line[start_col:end_col]`；跨行 → 首行尾段 + 中間整行 + 末行頭段）後 `cursor.insertText()`。若超出原文行數、欄位超過該行長度則 clamp。用於翻譯時想局部還原回日文原文。比對/預覽模式中按下會提示先回編輯模式。
    *   **💾 另存 (`save_as()`)**: 另存新檔對話框，將預覽內容封裝進帶有 CSS 的 HTML 並儲存。
    *   **關閉/返回**: 關閉前自動將當前預覽內容寫入 `preview_text_cache` 並呼叫 `save_cache()`。

    **搜尋列 (預設隱藏，`Ctrl+F` 切換):**
    *   `find_next()`: 在預覽文字中循環搜尋（不分大小寫）。
    *   **骰子搜尋**: 快速搜尋殘留的 `1D10:10` 骰子格式。
    *   `Ctrl+S` 快捷鍵觸發直接存檔（覆蓋原檔，若有 `source_file`），否則走另存新檔。

### 4.5 網址讀取功能 (`open_url_fetch_qt()` — 獨立 PyQt6 視窗)
*   **對應 Function**: `def open_url_fetch_qt(self):`（主程式）、`UrlFetchWindow` 於 [aa_url_fetch_qt.py](aa_url_fetch_qt.py)
*   主程式以 subprocess 啟動獨立的 PyQt6 視窗，透過 JSON 命令檔 (IPC) 雙向溝通；UI 與抓取職責分離，**實際抓取/解析由主程式執行**，以確保作者名稱 (`author_name_entry`) 總是取最新值。
*   **IPC 通訊協定**:
    *   啟動時傳入 `--cmd-file`（Qt→主程式）、`--reverse-cmd-file`（主程式→Qt）、`--init-file`（JSON 初始狀態）。
    *   初始狀態 (`init_file`)：`url_history`、`url_related_links`、`current_url`、`author_only`、`author_name`、`initial_url`。
    *   Qt→主程式：`fetch_request {url, author_only, author_name}`、`clear_history`、`close_sync {author_only, author_name}`。
    *   **作者名稱欄位** 已從主畫面移至網址讀取視窗（`aa_url_fetch_qt.py`），主程式以 `MainWindow._author_name` 狀態保存，由 `fetch_request` / `close_sync` IPC 更新，不再綁定 widget。
    *   主程式→Qt：`fetch_done {success, status_message, status_color, [url_history, url_related_links, current_url, auto_close]}`、`history_cleared {url_history}`。
    *   碰撞避免：寫入方若發現檔案仍存在（接收方未消費），延後 100ms 重試（Qt 最多 20 次，主程式相同）。
*   **主程式端處理**:
    *   `_poll_url_fetch_commands()`：每 500ms 讀取 `cmd_file`，subprocess 結束即停止輪詢。
    *   `_handle_url_fetch_request(url, author_only)`：更新 `_author_only`→`schedule_save`→背景執行緒執行 `fetch_url` + `parse_page_html`，成功後透過 `_invoke_on_main` signal 將 `_apply` 派回主執行緒（`QTimer.singleShot` 只能在有 event loop 的執行緒呼叫，背景執行緒須用 signal 轉送），套用到 `source_text`、更新 `url_related_links`/`current_url`/`url_history`、觸發 `check_chapter_number`、`_update_work_title`、`show_toast`，最後以 `fetch_done` 回報 Qt 並要求 `auto_close`。
    *   `_invoke_on_main`：`pyqtSignal(object)`，在 `__init__` 連到 `lambda fn: fn()`，供背景執行緒把 callable 排到主執行緒執行（取代不能跨執行緒的 `QTimer.singleShot`）。
    *   `_write_url_fetch_reverse()`：含重試邏輯的反向命令寫入。
*   **Qt 端功能**:
    *   URL 輸入 + 忽略留言 Checkbox + 讀取按鈕（綠）+ Enter 觸發。
    *   關聯記事列表（含當前話標記 `▶`）+ 上一話/下一話按鈕。
    *   讀取紀錄列表（最多 `fetch_history_limit` 筆，倒序顯示）+ 清除紀錄按鈕（紅）+ 搜尋框（`hist_search` `QLineEdit`，placeholder「🔍 搜尋紀錄（標題或網址）」、`setClearButtonEnabled(True)`）。`textChanged` 觸發 `_on_history_search_changed()` 即時把 `_history_filter` 存成 lower-case 子字串，再由 `_refresh_history()` 對 `entry["title"]` / `entry["url"]` 做 case-insensitive 子字串比對；無命中時顯示「（無符合「…」的紀錄）」提示。
    *   點擊關聯/紀錄項目自動填入 URL 並觸發抓取。
    *   成功後 400ms 自動關閉。
    *   `closeEvent` 同步最終 `author_only` 狀態。
*   **網頁解析 (`aa_tool/url_fetcher.py` → `parse_page_html()`)**: 依 URL 網域自動選擇解析器，由 `_DOMAIN_PARSERS` 對應表管理。分派策略：
    1. **已知網域**（在 `_DOMAIN_PARSERS` 鍵中出現）→ 直接使用該網域專屬的解析器。
    2. **已知網域但模板異常** → 若網域匹配到的解析器取不到內文（回傳空字串或 `None`），**自動 fallback** 到其他解析器依序嘗試。實例：`yarucha.blog.fc2.com` 匹配 `blog.fc2.com` 進入 `_parse_fc2blog`，但該子站實際使用 `<div class="article">` + `<dt>/<dd>` 結構（`_parse_default` 的目標），必須退回 default 才能取得內文。
    3. **未知網域** → 依序嘗試 `_parse_default` 與全部網域解析器，回傳第一個取到非空內文的結果；全部失敗則回傳最後一次的結果（通常是空）。每個解析器以 `try/except` 包覆，避免單一例外中斷整個流程。

    目前支援：
    *   **預設 (`_parse_default`)**: 5ch 類型 (`div.article` → `dt/dd` + `dl.relate_dl`)。關聯 `<dl>` 允許 class 帶額外字樣（正則 `relate_dl[^"]*"`），兼容 `relate_dl fc2relate_entry_thumbnail_off` 之類的變體。
    *   **himanatokiniyaruo.com (`_parse_himanatokiniyaruo`)**: `dt[id=數字]` / `dd` + `div.related-entries`。關聯連結以 `<br />` 分段處理（`_parse_related_segments`）：有 `<a>` 者抽出 href+title；純文字段落視為**當前話**（`url=None, is_current=True`），用於處理站方把當前文章標題以無連結純文字呈現於清單中的情況。`<a>` 比對採 `<a\s[^>]*?href="..."` 容許 `class="kjax"` 等屬性排在 `href` 之前。另含巢狀 div fallback：若原始 `(.*?)</div>` regex 未取到連結，改用平衡 div 深度計數找出真正的關閉標籤後再套用 `_parse_related_segments`。**關聯清單排序**：原始 HTML 為「新 → 舊」（最新話在最上方），解析完成後 **reverse** 為時間順序，使「下一話」按鈕能以 `current_idx + 1` 正確取得下一集（與 yaruobook 相同慣例）
    *   **blog.fc2.com (`_parse_fc2blog`)**: `div.ently_text` **或 `div.entry_body`**（兩種 FC2 模板擇一） + `dl.relate_dl`（含 `web.archive.org` 封存版、`yaruok.blog.fc2.com` 等變體）
    *   **yaruobook.jp (`_parse_yaruobook`)**: `dt.author-res-dt` / `dd.author-res` + `ul.relatedPostsWrap.relatedPostsPrev/Next`；`li.currentPost` 標記當前話。**關聯清單排序**：原始 Prev 區塊為 `[currentPost, 前一話, 前前話, ...]`（當前在前、舊話倒序），解析時須將 Prev **反轉**後再接 Next，才能得到按時間順序排列的清單，供「下一話」按鈕以 `current_idx + 1` 正確取得下一集
    *   **yaruobook.net (`_parse_yaruobook_net`)**: 早期文章用的站點，內文幾乎全以 HTML 數字字元引用表示（`&#65306;` = `：`、`&#26085;` = `日` 等）。入口容器改用 `<div id="entry-content" class="entry-content cf">`（不受 entity 影響），結束邊界為 `<div id="custom_html-` / `widget-single-content-bottom` / `relatedPostsWrap` 任一最先出現者；內文仍為 `<dl>/<dt>/<dd>`，交由 `_extract_dt_dd_posts` 處理（其內部 `html.unescape` 會把 entity 還原成正常字元，讓 `_POST_HEADER_RE` / `_POSTER_NAME_RE_ALT` 正常匹配「N ： AUTHOR ： YYYY/MM/DD...」標頭）。關聯連結沿用 `.jp` 的 `relatedPostsWrap.relatedPostsPrev/Next` 結構與 Prev 反轉邏輯。
    *   **yaruo-matome.com (`_parse_yaruo_matome`)**: 入口容器為 `<div id="entry-content">`，結束邊界為 `<ul class="nexe-prev-post">`；內文為 `<dl>/<dt>/<dd>`，`<dt>` 使用 `<font color>` 包住 trip code，由 `_normalize_color_tags` 轉換為 color span。呼叫 `_extract_dt_dd_posts` 前做一項預處理：WordPress 會在每個 `<br />` 後插入 source HTML `\n`，若不處理會讓 `<br />` → `\n` 後變成 `\n\n`（每行多一個空行）；以 `re.sub(r'<br\s*/?>[ \t]*\n', '<br />', ...)` 移除 `<br />` 後的多餘換行。`<p>&nbsp;</p>` 等空白 spacer 不做處理，保留其自然轉換成的空行（對應作者行與內容之間的視覺空行）。`_extract_dt_dd_posts` 的 regex lookahead 已加入 `</dd>` 以防止最後一則 dd 因無後續 `<dt>`／`</dl>` 而把 `</dd>` 後的頁面垃圾內容一併擷取（此修改對所有解析器均有效）。關聯連結從 `<ul class="nexe-prev-post">` 解析：有 `<a>` 的 `<li>` 為其他話，只有 `<span>` 的 `<li>` 為當前話（`is_current=True`）。原始清單為「最新話 → … → 當前話（最底）」，解析後 **reverse** 為時間順序，讓「下一話」按鈕以 `current_idx + 1` 正確取得下一集。**平面 `<p>...<br/>...` 變體 fallback**：部分文章（例：`yaruo-matome.com/archives/21514`）整批貼文塞在單一 `<p>` 內、用 `<br/>` 分行，每行首字 `.` 為防瀏覽器壓縮空行的占位符；標頭格式為「.N ： AUTHOR ： YYYY/MM/DD(曜) HH:MM:SS.ms ID:XXX」。**僅在 `_extract_dt_dd_posts` 抽取為空時啟用**，避免影響既有正常頁面。流程：先以 regex 移除 `<button>` 與 `class="wpfp-*"` 的 span（書籤按鈕、收藏連結 cruft），`<br/>`→`\n`，`_strip_tags_keep_color`，每行 `^\.` 剝除占位字元；之後沿用 `_filter_color_by_author` 處理作者過濾。**安全閥**：只有當 `out_lines` 中存在符合 `_POST_HEADER_RE` 的標頭行才採用 fallback 結果，避免把純 cruft 文字當成內文。
*   **保留貼文內空行**: `_extract_dt_dd_posts()` 在切 dd 內容時，只 trim 尾端空行、不 trim 開頭空行，以避免「作者行」與實際 AA 內文之間的排版空行被吃掉。
*   **作者名稱格式**: `_is_author_post()` 支援三種標頭格式並自動正規化空白（透過 `_extract_poster_name()` 共用解析）：
    1.「N 名前：AUTHOR[...]」(5ch / FC2 / himana)
    2.「N ： AUTHOR ： YYYY/MM/DD(曜) HH:MM:SS ID:XXX」(yaruobook / yaruok.blog.fc2.com 等 FC2 變體)
    3.「N : AUTHOR [sage]/[] YYYY/MM/DD(曜) HH:MM:SS ID:XXX」(yarucha.blog.fc2.com 等站點，以 `[...]` 方括號而非 `：` 分隔名稱與日期)

    行內切分用的 `_POST_HEADER_RE`（供 `_filter_color_by_author` 判斷貼文邊界）同樣支援三種格式；`_POSTER_NAME_RE_ALT` 的第二分隔符擴為 `(?:[：:]|\[[^\]]*\])`，同時涵蓋 2 與 3。
    **尾端標點容錯**：名稱比對先做 `==` 精確比較，失敗時再以 `_NAME_TRAIL_RE`（`[\s.．。・,，、]+$`）剝除兩邊尾端標點後再比一次。用於容許作者在連續貼文偶爾在名稱後多打 dot 等情況（例：`yaruobookshelf.jp` 上 `三流 ◆WiAEg3iQI` ↔ `三流 ◆WiAEg3iQI.`，trip 碼與 ID 相同確認為同一人；若不容錯，第 2 篇之後在 `author_only=True` 時會被全部過濾掉）。
*   **編碼自動偵測**: 依序嘗試 `utf-8`、`cp932`、`euc-jp`、`shift_jis`。
*   **Gzip 解壓縮**: 自動解壓縮伺服器回傳的 gzip 內容。
*   **關聯記事導航**: 顯示同系列各話的連結清單，可直接點擊切換；提供上一話/下一話按鈕。
*   **忽略留言開關 (`author_only`)**: Dialog 中的「忽略留言」開關，開啟時完全排除非作者的貼文。若同時填寫作者名稱則以該名稱為準；**若未填作者名稱則自動偵測**（`_detect_main_author_from_dt()` 對應 dt/dd 結構、`_detect_main_author_from_lines()` 對應 FC2 類型的「N 名前：…」行結構），取貼文中**第一個出現**的投稿者作為作者。共用 helper `_extract_poster_name()` 負責從標頭抽取名稱。狀態持久化至暫存，「下一話」功能同樣遵循此設定。
*   **讀取歷史記錄**: 最多保存 50 筆，可點擊重新讀取，支援清除記錄。
*   **直接讀取上/下一話 (`fetch_prev_chapter()` / `fetch_next_chapter()`)**: 首頁原文區標頭提供「◀ 上一話」「下一話 ▶」按鈕，皆透過共用 `_fetch_adjacent_chapter(direction)` 從已存的關聯記事中背景讀取相鄰話。
*   **讀取紀錄列**: 每列依序為 **複製按鈕（最左）→ 標題按鈕（可點擊直接讀取）**；原本獨立的「讀取」按鈕已移除，點擊標題即呼叫 `_fetch_url(url)`。複製按鈕對應 `_copy_url_to_clipboard()`。
*   **複製按鈕的 DPI 縮放處理**: Windows 顯示縮放設定（100% / 125% / 150%）會同時放大按鈕寬度與字型，但中文字形在 Qt 的實際渲染會略大於英數字，導致「複製」二字在高縮放下溢出按鈕邊界。解法：以 `self.screen().logicalDotsPerInch() / 96.0` 取 DPI 比例，字型 pointSize 於 `dpi_scale >= 1.2` 時再 −2pt（否則 −1pt）、按鈕 `width` 乘以 `max(1.0, dpi_scale)`，並以 stylesheet 追加 `padding:0 2px`。若未來新增其他中文字按鈕在類似狹窄寬度場景出現相同問題，可比照此策略處理。
*   **章節號碼自動偵測 (`check_chapter_number()`)**: 貼上或讀取文字後，掃描前 200 字尋找話數格式，自動填入話數欄位。支援格式（依序嘗試）：`第N話`（阿拉伯數字 / 漢數字）、`番外編N`、`その N`（全形/半形）、**裸 `N話`（無「第」前綴）—— 僅限文本第一行**，例：「やる夫の淫らな日々　2話　きっと掌の上だった日…」。裸 `N話` 因易與內文誤命中，刻意限制在第一行；若需擴展請維持此限制。
*   **複製網址 (`copy_current_url()`)**: 複製目前讀取的網址至剪貼簿。

### 4.6 批次搜尋（獨立 PyQt6 視窗）
*   **檔案**: `aa_batch_search_qt.py` — `BatchSearchWindow(QMainWindow)`
*   **啟動方式**: 主程式透過 `open_batch_search_qt()` 以 subprocess 啟動，傳入 `--cmd-file`（Qt→主程式）、`--reverse-cmd-file`（主程式→Qt）、`--folder` 參數。
*   **IPC 雙向通訊**:
    *   Qt→主程式（`cmd_file`）：`open`（開啟檔案並跳行）、`sync_folder`（同步資料夾路徑）
    *   主程式→Qt（`reverse_cmd_file`）：`restore`（恢復視窗顯示）
*   **搜尋**: 在指定資料夾所有 HTML 的 `<pre>` 內容中搜尋，支援字串或正則，最多 500 筆結果。背景執行緒搜尋 + 分批渲染。
*   **單筆替換 / 全部替換 / 復原**: 與舊版邏輯相同，另支援檔案層級的復原（`_undo_backups`）。
*   **全部復原（`_undo_all_batch`）**: 按下「全部替換」後，其右側會顯示「全部復原」按鈕，點擊可將本次批次替換涉及的所有檔案一次還原至替換前狀態。採用 `_batch_undo` 快照（含 `backups`、`items`、`new_backup_files`），新搜尋或單筆復原動到同檔案時會使快照失效並隱藏按鈕。單筆「復原」按鈕則僅還原單一檔案。
*   **排除功能（✕ 按鈕）**: 可將特定搜尋結果從替換範圍中移除（不會被「全部替換」影響），並可隨時恢復。
*   **結果列欄位對齊**: 檔名欄 (`_NAME_COL_WIDTH=180`) 透過 `_make_name_label()` 以 `QFontMetrics.elidedText(..., ElideLeft)` 從左側截斷，並右對齊 + `setToolTip(stem)`，確保結尾「第X話」不會被裁掉。前置指示欄 (`_LEADING_COL_WIDTH=55`) 固定寬度：原始狀態（含從「已替換」復原、「已排除」恢復回來的狀態）放 ✕ 按鈕、替換後放「已替換」、排除後放「已排除」。操作欄兩顆按鈕均採 `_BTN_COL_WIDTH=55`；「已排除」狀態因只有「恢復」一顆按鈕，會補透明 placeholder，讓右側「搜尋結果」內文欄起點一致。表頭 (`檔名`/`操作`/`搜尋結果`) 以 `_init_row_layout()`（統一 `_ROW_MARGIN_H=5`、`_ROW_SPACING=6`）與 `_op_col_width()` 計算，與資料列精確對齊。
*   **術語快捷按鈕上次點擊標示**: `_apply_glossary_entry(a, b, btn)` 會把上次點擊的 `[→]` 按鈕以紫色 (`_GLOSSARY_BTN_PRESSED=#6f42c1/#5a32a3`) 著色，舊按鈕復原為預設青色 (`_GLOSSARY_BTN_DEFAULT=#17a2b8/#138496`)，方便使用者追蹤目前套用的是哪一條術語。`_refresh_glossary_list()` 重建列表時清掉 `self._last_glossary_btn` 指標。
*   **從批次搜尋開啟檔案的原文對照**: `MainWindow._on_batch_open_file()` 會呼叫 `load_original_for_file()` 由 `aa_original_cache.json` 取出對應原文，並以 `original_text` 參數傳給 `show_edit_panel()`，確保編輯器的「切換原文」/ Alt+W 覆蓋等比對功能可正常使用（先前此路徑未載入原文導致功能無法作用）。
*   **開啟並跳轉**: 點擊「開啟」後，Qt 視窗自動縮小，主程式開啟檔案進入編輯模式；從編輯模式返回時，Qt 視窗自動恢復顯示。
*   **資料夾同步**: Qt 視窗中選擇的資料夾會在開啟檔案與關閉視窗時同步回主程式，並持久化至暫存。
*   **操作互斥鎖（`_busy`）**: `_replace_single` / `_replace_all` / `_undo_single` / `_undo_all_batch` / `_dismiss_match` / `_restore_dismissed` 進入時以 `self._busy` 旗標互斥；若已有操作進行中則直接忽略本次點擊，避免在 `deleteLater()` 重建 row 過程中被連點造成 `wrapped C/C++ object has been deleted` 崩潰。
*   **row 重建一律延後（避免「點擊 sender 自我刪除」閃退）**: 批次搜尋的 row 內按鈕（`替換` / `復原` / `✕` / `恢復`）被點擊時，其 clicked signal 會呼叫把該 row 重建為別的狀態的函式（`_rebuild_row_as_replaced` / `_rebuild_row_as_active` / `_render_row_as_dismissed`），這會在 clicked 信號分派過程中 `deleteLater()` 掉按鈕本身，Qt 因 sender 被釋放而崩潰。統一做法：所有「由 row 按鈕觸發 + 會重建該 row」的流程都以 `QTimer.singleShot(0, lambda ...: ...)` 把重建延後到下一個事件迴圈。目前套用於 `_replace_single` / `_undo_single` / `_dismiss_match` / `_restore_dismissed` 四處。`_replace_all_impl` / `_undo_all_batch_impl` 因 sender 為工具列按鈕（不在被刪除的 row 內）理論上可直接重建，但仍建議新增類似流程時延用同一模式以保險。`_dismiss_match` 的實際 widget 變更抽出為 `_render_row_as_dismissed(row, mi)` 以便延後呼叫。
*   **快速替換面板（右側）**: 畫面右側約 1/4 寬度為 `_build_glossary_panel()` 建立的 `QPlainTextEdit`（標題「快速替換（每行 A=B）」），供使用者輸入 `A=B`（支援半形與全形 `＝`）每行一組。`textChanged` 即時解析並在下方清單顯示 `[→]` 按鈕；點擊按鈕會將 A 填入搜尋欄、B 填入替換欄、關閉正則開關，若 `glossary_auto_search = True` 則立即執行 `_do_search()`。該旗標由 `MainWindow` 在建構時傳入（cache key `glossary_auto_search`），並在設定視窗變更時即時同步 `self._batch_window.glossary_auto_search`。清單中每列 label 可**按兩下**透過 `on_add_to_glossary` callback 把該條 `A=B` 加入首頁主術語表（一般 tab）；callback 由 `MainWindow._save_glossary_entry` 實作，若 A 或 B 為空則跳 toast 拒絕，不新增。

### 4.7 狀態讀寫裝載管理
*   **讀取/儲存暫存**:
    *   `load_cache(load_preview_text=False)`: 讀取 `aa_settings_cache.json`，包含原文、過濾規則、術語表(一般+臨時)、話數、標題、背景/文字色、URL 歷史、當前 URL、開關狀態等。`load_preview_text=True` 時才載入預覽暫存並詢問是否恢復。
    *   `save_cache()`: 儲存上述所有狀態。
    *   `schedule_save()`: 防抖動延遲 500ms 後呼叫 `save_cache()`，綁定至所有 TextBox 的 `KeyRelease` 事件。
*   **多程序共享保護**（`aa_tool/settings_manager.py` + `aa_tool/file_lock.py`）：
    *   **Sidecar 檔案鎖**：對 `aa_settings_cache.json.lock` 加鎖（Windows `msvcrt.locking` / POSIX `fcntl.flock`），保護整個讀-合-寫序列，避免同時開兩個 `aa_main_qt.py` 或與子程序交替寫入時發生 TOCTOU race。
    *   **原子寫入**：`_atomic_write_json` 先寫 `<file>.tmp` 再 `os.replace()`，避免半截檔。
    *   **細粒度 helpers**（不經過 `AppCache`，只動目標欄位）：
        *   `append_url_history(entry, max_items=50)` — 以 `url` 去重、newest-last append；寫入當下觸發（`_handle_url_fetch_request` 內），不透過 `schedule_save`。
        *   `append_work_history(entry, max_items=10)` — 以 `(title, author)` 去重、newest-first prepend；由 `_record_work_history` 呼叫。
        *   `update_url_related_links(url, links)` — 以 `url` 為 key 更新 `url_related_links`（格式已改為 `dict[url → links]`，不同 URL 的連結互不覆蓋，讀取時依 `current_url` 取對應 entry；相容舊 flat-list 格式自動遷移）。
        *   `clear_url_history()` — 僅清空 URL 歷史，其他欄位保留。
    *   **`save_cache` 保留策略**：對 `url_history` / `work_history` 改為「保留檔上值」（由上列 helpers 維護），避免可能過時的 in-memory 版本蓋掉其他程序的新增。
    *   **即時跨程序同步**（`MainWindow._refresh_shared_history` + `QTimer` 1.5s 輪詢）：每 1.5 秒呼叫 `peek_shared_state(current_url)` 取出 `url_history` / `work_history` / 對應 URL 的 `url_related_links`，**直接比對內容**而非 mtime（避免低解析度檔案系統同秒寫入漏偵測）；有變動才更新 in-memory。若 URL 抓取子程序在跑，透過 `_write_url_fetch_reverse` 推送 `history_updated` 動作；子程序在 `_poll_reverse_commands` 中接收後刷新 `_url_history` / `_url_related_links` / `_current_url` 與 UI。效果：兩個 `aa_main_qt.py` 並開時，雙方寫入的歷史紀錄會在 ~1.5 秒內互相看見（類似討論板留言即時更新）。
    *   **所有 URL 寫入路徑都必須走 `append_url_history` + `update_url_related_links`**：`_handle_url_fetch_request._apply`（手動抓取）與 `_fetch_adjacent_chapter._apply`（上一話/下一話）皆已改用 helpers。若新增其他抓 URL 的路徑，務必同樣呼叫 helpers，否則該筆紀錄會被 `save_cache` 的「保留檔上值」策略當成不存在而不寫入。
*   **讀取/匯出設定檔**:
    *   `import_settings()`, `export_settings()`, `load_settings_at_startup()`: 管理 `AA_Settings.json`，包含自訂 Regex 規則、一般術語表與臨時術語表。
    *   `save_regex_to_settings()`: 僅更新 `AA_Settings.json` 中的三條正則，保留其他欄位不動。
    *   **差異合併儲存模式 (`diff_save_mode`)**：設定對話框可勾選「儲存設定時合併差異」。開啟後 `export_settings()` 會先 `load_settings()` 讀取既有 `AA_Settings.json`，再以 `merge_glossary_diff()`（術語表，等號左側為 key）與 `merge_filter_diff()`（自訂過濾規則，整行為 key）合併 UI 內容後寫回；檔上既有條目若 UI 沒有則保留，UI 中同 key 的條目覆蓋檔上版本，UI 新增條目 append 到末端。三條正則仍直接覆蓋。狀態持久化於 `aa_settings_cache.json` 的 `diff_save_mode`。
    *   **優先順序**: 啟動時先讀暫存（保底），再讀 `AA_Settings.json`（優先覆蓋過濾規則與術語表）。
*   **外部裝載 (`import_html()`)**: 「打開已儲存的 HTML」，利用 `read_html_pre_content()` 從已生成的 HTML 中提取 `<pre>` 區塊內容並解碼，直接載入 `EditWindow` 繼續作業（同時以 `aa_original_cache.json` 中的同名紀錄作為比對原文）。
*   **HTML 讀寫 Helpers**:
    *   `read_html_pre_content(file_path)`: 讀取 HTML 並以 `re.search(r'<pre>...<\/pre>')` 擷取 `<pre>` 內容並 `html.unescape`。
    *   `read_html_head(file_path)`: 以 `re.search(r'<head\b[^>]*>[\s\S]*?</head>')` 擷取整段 `<head>...</head>`（含標籤），供儲存時沿用；找不到或讀取失敗回傳 None。
    *   `write_html_file(file_path, text_content, bg_color="#fff", head_html=None, embed_font_path=None, embed_font_family=None)`: 封裝 HTML（CSS 字體設定為 `MS PGothic`/`Meiryo` monospace, 16px），用 `re.split` 把白名單 `<span>` 標籤切出後原樣保留，其餘內容一律 `html.escape`，以防 AA 圖形中的 `<` `>` 被瀏覽器誤判為標籤。白名單（`_PRESERVED_SPAN_OPEN_RE`）目前收錄兩個特例起始標籤：上色 `<span style="color:...">` 與隱藏 `<span style="display:none;">`（容許少量空白變體），加上共用關閉標籤 `</span>`。`head_html` 參數：若傳入非空字串則直接作為 `<head>` 區塊寫入（供保留原檔自訂 head，例如外掛字型 CSS）；未傳入時使用預設 head 模板（含 `bg_color` 背景色）。包含 `viewport` meta 與手機 RWD 樣式（768px 以下字體縮小至 10px），支援觸控橫向捲動。
        - **內嵌字型模式 `embed_font_path`**：指定 TTF/OTF 路徑時，呼叫 `_build_embed_font_face()` 把字型 Base64 編碼後組成 `@font-face` CSS 注入 `<head>`，並把 `pre.font-family` 設為 `'<embed_font_family>', 'MS PGothic', 'Meiryo', monospace`。產出的單一 HTML 不需任何外部資源，下載到手機本地直接打開即可正確顯示 AA。代價：每個檔案會增大約「字型檔大小 × 1.33」（Base64 overhead，Monapo 約 +3.6MB）。**啟用此模式時會強制重產 head（覆寫 `head_html`）**，以確保 `@font-face` 與 `pre.font-family` 一致。MIME 用 RFC 8081 的 `font/ttf` / `font/otf`，format hint 為 `truetype` / `opentype`。
        - **設定開關**：`AppCache.embed_font_in_html: bool` 與 `AppCache.embed_font_name: str`（預設 `"monapo"`）持久化於 `aa_settings_cache.json`；`SettingsDialog` 提供 checkbox + 字型下拉（Monapo / Saitamaar / textar，各有預估大小增量的 tooltip）。`MainWindow` 透過 `embed_font_provider` callback（回傳 `str | None`）把字型名稱傳到 `EditWindow._write_current()`；`None` 表示不嵌，非空字串則從 `_FONT_MAP` 查出對應 `(filename, css_family)` 帶入 `write_html_file`。
    *   ⚠️ **重點原則 — 所有存檔路徑的行為必須一致**：凡是呼叫 `write_html_file()` 覆寫 AA HTML 檔案的地方（編輯器儲存、批次取代、復原、其他任何未來新增的路徑），其存檔邏輯都必須一致，不能因入口不同而產生差異。目前已約定的一致行為包含：覆寫既有檔案時保留原 `<head>`（以 `read_html_head(fpath)` 讀出後傳入 `head_html`）、僅「產出全新檔案」的情境（例如翻譯結果初次輸出）才套用預設 head 模板。未來若要調整存檔行為（例如換 head 模板、加入新欄位），必須同步更新所有呼叫點，避免分歧。
    *   **EditWindow 載入/儲存連動**: `EditWindow.__init__` 載入檔案時同步呼叫 `read_html_head()` 存入 `self._custom_head`；`_write_current()` 儲存時把 `self._custom_head` 傳入 `write_html_file(..., head_html=self._custom_head)`。
    *   **批次搜尋/取代覆寫連動 (`aa_batch_search_qt.py`)**: `_replace_single` / `_replace_all_impl` / `_undo_single` / `_undo_all_batch_impl` 四處覆寫位置均於寫入前即時呼叫 `read_html_head(fpath)` 作為 `head_html` 傳入 `write_html_file`，避免批次取代與復原時把原檔的自訂 head 清掉。（復原時 head 仍能正確取回，因為先前的覆寫已保留了原 head。）

### 4.9 Wiki 角色日中對照抓取（非 modal QDialog）
*   **檔案**: `aa_wiki_name_dialog_qt.py` — `WikiNameDialog(QDialog)`；解析邏輯於 `aa_tool/wiki_name_fetcher.py`。
*   **啟動**: 主視窗工具列右側「📖 Wiki 對照」按鈕 → `MainWindow.open_wiki_name_dialog()`；實例以 `self._wiki_dialog` 保留，避免重開時丟失上次抓取結果。
*   **UI**: URL 輸入列（Enter 觸發）+ 狀態列 + 說明列 + 多行結果文字框（可複製/編輯）+ 底部「📋 複製全部」/「關閉」按鈕。
*   **抓取流程**: 按「讀取」→ `threading.Thread` 呼叫 `aa_tool.url_fetcher.fetch_url` + `aa_tool.wiki_name_fetcher.parse_wiki_name_list`，透過 `pyqtSignal(object, str)` 回傳 `(pairs, err)` 至主執行緒；成功則以 `日文=中文` 每行一筆填入結果文字框。
*   **解析策略 (`parse_wiki_name_list()`)**: 依序嘗試三種策略並合併，最後以日文為 key 去重：
    1. **策略 A — `<dt>` + `<span lang="ja">`**（Wikipedia 主要格式）：`<dt>` 開頭的純文字為中文名；日文名優先取 `<span lang="ja">` 內容，若為假字串（如 `ja`、空）fallback 取 `（日語文本，XXX）` 中逗號後至右括號前的字串。
    2. **策略 B — `<table class="wikitable">`**：對每列 `<tr>` 前三欄，以是否含假名判斷日文欄與中文欄。
    3. **策略 C — 泛用 `<span lang="ja">` 配對**：span 之前 400 字元範圍去 tag 後取尾端連續中文串為中文名。
*   **過濾條件**: 日文必須含假名或漢字；中文不得含假名；兩者相等或任一為空則剔除。
*   **輸出用途**: 使用者複製結果後手動貼入主視窗術語表（不自動寫入，因使用者通常需要手動編輯處理）。

### 4.8 UI 通知系統
*   **`show_toast(message, color, duration)`**: 在主視窗右上角顯示浮動提示，`duration` ms 後自動關閉。PyQt6 主視窗的 `MainWindow.show_status()` 已改以此函式呈現（原本位於左下角的「就緒」狀態列已移除），會將常見的 `#0f0` 亮綠自動映射為 toast 風格的 `#28a745`。**防重疊**：以 `parent._active_toasts` 追蹤仍在顯示中的 Toast，新 Toast 出現時會先 `deleteLater()` 清掉舊的再顯示新的；自動消失的 Toast 也會從清單移除。
*   **編輯器 Toast**：PyQt6 編輯器 (`aa_edit_qt.py`) 的 `EditWindow._set_status()` 亦改為直接呼叫 `show_toast`，顯示於右上角（`MainWindow` 為 parent），與首頁風格一致。先前置於編輯區底部的 `status_label` 已整組移除（含 `_position_status_label`、`_status_hide_timer`、`resizeEvent` 重定位）。顏色映射 `_STATUS_COLOR_MAP` 比照主程式。
*   **`show_confirm_toast(message, on_yes, color, duration)`**: 帶有「是/否」按鈕的確認浮動視窗，逾時（預設 8 秒）自動關閉。
*   **AI 翻譯格式驗證 (`validate_ai_text()`)**: 貼上翻譯文字後自動觸發，掃描每行是否含有多個 ID (`\d{2,4}-\d+\|`)，於 `ai_warn_label` 顯示警告或成功提示。

### 4.12 全域設定視窗 (`SettingsDialog` — `aa_settings_dialog_qt.py`)
*   **入口**：主視窗工具列標題「AA 創作翻譯輔助小工具」右側的 **⚙ 按鈕**，對應 `MainWindow.open_settings_dialog()`，以 modal `QDialog` 呈現。
*   **設定項目**：
    *   **提取後自動複製**（對應 `aa_settings_cache.json.auto_copy`）：先前置於主畫面提取結果區的 `auto_copy_cb` checkbox 已移除，改由此處集中控制；提取自動複製邏輯改讀 `MainWindow._auto_copy` bool 屬性。
    *   **作者名稱歷史記錄數量**（對應 `work_history_limit`，預設 10）：原 `_WORK_HISTORY_LIMIT` 常數已移除，改為 `MainWindow._work_history_limit`。
    *   **網址讀取紀錄儲存數量**（對應 `fetch_history_limit`，預設 50）：控制 `url_history` 上限（透過 `append_url_history(..., max_items=self._fetch_history_limit)` 套用）。
    *   **原文暫存儲存數量**（對應 `original_cache_limit`，預設 50）：獨立控制 `aa_original_cache.json` 的條目上限。`save_original_for_file()` 以 `self._original_cache_limit` 為裁切依據，與 URL 讀取紀錄的上限分離（先前共用 `fetch_history_limit`，舊 cache 載入時若缺欄會以 `fetch_history_limit` 值遷移）。
    *   **原文暫存檔**：以同一個 `QHBoxLayout` 同列放置「原文暫存檔：」標籤、檔案大小（`os.path.getsize(aa_original_cache.json)` 格式化為 B / KB / MB）與「清除暫存」按鈕。
    *   **清除暫存按鈕**：二次確認（`QMessageBox.question`）後把 `aa_original_cache.json` 覆寫為 `{}`，即時更新大小顯示。
    *   **批次搜尋：點擊術語按鈕時自動搜尋**（對應 `glossary_auto_search`，預設 True）：控制批次搜尋視窗右側「術語快捷面板」的 `[→]` 按鈕是否在填入搜尋/替換欄後自動觸發搜尋。對應 `MainWindow._glossary_auto_search`；套用時同步至 `self._batch_window.glossary_auto_search`。
    *   **儲存 HTML 時內嵌字型**（對應 `embed_font_in_html` + `embed_font_name`）：checkbox 開啟後可從下拉選單選擇要嵌入的 AA 字型（Monapo +3.5 MB、Saitamaar +2.7 MB、textar +4.3 MB）；checkbox 勾選/取消連動下拉可用狀態（`toggled → setEnabled`）。checkbox 與 combobox 同列以 `QHBoxLayout` 並排；各提示文字均以 tooltip 顯示，不寫在 label 內。
    *   **進入編輯器時預設開啟「所見即所得」模式**（對應 `editor_default_wysiwyg`，預設 False）：開啟後，每次 `MainWindow.show_edit_panel()` 切換到編輯面板時，若 `EditWindow._preview_active` 為 False 會自動呼叫 `_toggle_preview()` 進入 Alt+3 WYSIWYG 模式（替換翻譯、開啟 HTML、批次開檔等所有路徑都生效）。對應 `MainWindow._editor_default_wysiwyg`。
*   **套用流程**：按「確定」呼叫 `MainWindow._on_settings_applied(values)` → 寫回 instance 屬性（含 `_glossary_auto_search`）→ 立即修剪 in-memory `work_history` / `url_history` 以符合新上限 → 同步給已開啟的 `BatchSearchWindow` → `save_cache()` 持久化。「取消」不套用任何變更。

## 5. UI 介面關聯變數
UI widget 屬於 `TranslatePanel`（除特別註明外）：
*   `self.source_text`: 原文輸入區 `QPlainTextEdit`（含 AA 圖）
*   `self.filter_text`: RegExp 過濾規則 `QPlainTextEdit`
*   `self.glossary_text`: 一般術語表 `QTextEdit`（`日文=中文` 格式；已 `setAcceptRichText(False)`，貼上時一律視為純文字，避免外部 rich text 格式污染顯示字體/顏色）
*   `self.glossary_text_temp`: 臨時術語表 `QTextEdit`（**UI 已隱藏**：不再加入 layout，無 tab 切換按鈕；物件本身保留以維持 cache / `AA_Settings.json` I/O 與 `get_combined_glossary()` 不需大改。資料仍會被讀寫，但使用者目前無法在主視窗直接編輯。）
*   術語表搜尋列：`self._gloss_search` (`QLineEdit`) + `_gloss_search_status` 顯示「找不到 / 已從頭開始」+ 「下一個」按鈕。`textChanged` 從文件開頭重新搜尋；`Enter` / 「下一個」呼叫 `_search_glossary_next()`，搜不到時自動回頭從頂部再找一次。底層用 `QTextEdit.find()`。取代了原本的「一般 / 臨時」tab。
*   `self.extracted_text`: 提取結果 `QPlainTextEdit`
*   `self.ai_text`: AI 翻譯結果 `QPlainTextEdit`
*   `self.doc_title`: 標題輸入欄（用於 HTML 檔名）
*   `self.doc_num`: 話數輸入欄（支援 +/- 按鈕；貼上/讀取時自動偵測）
*   `self.btn_work_history` / `self.work_history`: 🕘 小圖示按鈕 + 歷史清單（最多 10 筆 `{title, author}`），點擊按鈕以 QMenu 顯示。只有在按下「替換翻譯並編輯」時才透過 `_record_work_history()` 新增記錄（去重後置於最前），選單項 callback 走 `_apply_work_history()` 填回兩個欄位。`apply_translation` 產生的暫存 HTML 以 `{title}_第{num}話.html` 命名，讓 EditWindow 的 `display_title` 能正確顯示於主視窗標題列與另存新檔預設檔名
*   `self.auto_copy_switch`: 提取後自動複製開關
*   `self.batch_folder_var`: 批次搜尋資料夾路徑（透過 IPC 與 Qt 批次搜尋視窗同步）
*   `self.bg_color` / `self.fg_color`: 編輯器背景/文字色（持久化至暫存）
*   `self.preview_text_cache`: 編輯器關閉前的文字暫存（關閉時寫入 `save_cache()`，下次開啟可選擇恢復）
*   `self.url_history`: URL 讀取歷史（最多 50 筆）
*   `self.url_related_links`: 當前頁面的關聯記事連結清單
*   `self.current_url`: 當前讀取的網址
*   `EditWindow.editor` (`aa_edit_qt.py`): 編輯器主要 `QTextEdit`，所有後處理動作（對齊、上色、搜尋等）皆作用於此。

## 6. 給後續開發維護 AI 的系統提示 (For AI Agents)
1.  **確保對齊機制的字體計算**: 所有對話框對齊與補空白演算法（`bubble_alignment.py`、`QtFontMeasurer`）都以 **MS PGothic** 的字寬 metrics 為基準，這是整個專案 AA 對齊的核心假設。**切勿改變預設字體設定**（含 `DEFAULT_EDITOR_FONT` 常數、`aa_settings_cache.json` 的 `editor_font_family`），否則所有自動對齊功能會失準。若使用者透過編輯器搜尋列切換字體，那是使用者暫時預覽行為，不應把新字體寫回預設值。
2.  **效能考量**: 處理長文本時 `re.sub` 或大量 `QTextEdit` 修改 (`setPlainText`, 逐行 insert) 會造成一定延遲。必要時請以 string method 替代，或一次性取出大量 list 再 `'\n'.join()` 放回，減少 GUI 卡頓。
3.  **保持替換長度優先排序規矩**: 修改 `apply_translation()` 邏輯時，必須延續「由原文長度遞減排序 (`valid_ids.sort`) 後再 replace」，否則疊加翻譯文將互相破壞（如先替「あ」再替「あはは」）。
4.  **術語表有兩張（臨時 UI 已隱藏）**: 一般術語表 (`glossary_text`) 與臨時術語表 (`glossary_text_temp`)。臨時術語表的 UI 在主視窗已被隱藏（不加入 layout、無 tab 切換），但物件保留供 cache / `AA_Settings.json` I/O 使用；資料載入後仍會合併到 `get_combined_glossary()`。所有需要套用術語的地方仍應呼叫 `get_combined_glossary()` 合併後使用。
5.  **術語表重複偵測**: 貼上術語時，系統會自動掃描**一般術語表**中等號左邊的原文是否重複（臨時 UI 已隱藏，不再參與檢查）。若偵測到重複，會在術語表標題旁顯示警告，並出現「跳到重複」按鈕，可循環跳躍至每個重複項目。對應 Function: `_check_glossary_duplicates()`、`_jump_to_glossary_dup()`。

    **Key/Value 含空白的處理**：所有術語表處理路徑（`parse_glossary` / `_check_glossary_duplicates` / `merge_glossary_diff` / 編輯器全文替換 `_replace_all` / 存入術語 `_save_glossary_entry`）共用 `decode_glossary_term()` 與 `encode_glossary_term()`（位於 `aa_tool/translation_engine.py`）。規則：
    - **預設**：剝除外圍空白、保留內部空白。`Hello World = 哈囉 世界` → `key="Hello World"`、`value="哈囉 世界"`；`term=val` 與 `term = val` 視為同一條規則。
    - **保留外圍空白**：以半形 backtick `` `...` `` 包覆 key 或 value，backtick 內的空白完整保留，backtick 本身被剝除。選 backtick 的原因：1) 鍵盤可直接打、2) CJK 內文與一般日中翻譯內容幾乎不會出現、3) 比 ASCII 雙引號 `"` 更不易誤觸（少數作品會用半形雙引號）。例：
        - `` ` は？`=` 蛤？` `` → key=` は？`、value=` 蛤？`（用於日文標點前的視覺空白也要納入比對）
        - `` ` Trooper `=Trooper `` → key=` Trooper `、value=`Trooper`（消除英文詞兩側的空白）
    - 兩側可獨立決定要不要包 backtick（`` ` 帶空白 `=不帶空白 `` 合法）。`re.escape` 處理 key 中的空白/符號，regex 單輪掃描照常匹配。
    - **重複偵測與 merge**：`` ` Trooper `=… `` 與 `Trooper=…` 因 decode 後 key 不同（前者含空白），會被當作**不同條目**保留而非互相覆蓋。
    - **編輯器全文替換輸入框**也支援同樣語法：在「原文」欄輸入 `` ` Trooper ` ``、「翻譯」欄輸入 `Trooper`，會精確比對含空白的字串。
    - **存入術語**會用 `encode_glossary_term()` 自動處理：若值有外圍空白會用 backtick 包好寫入術語表，下次解析可正確還原；無空白則原樣寫入避免雜訊。
5.  **批次操作的行號偏移**: `batch_replace_all()` 和 `adjust_all_bubbles()` 都採用「由下而上」或「由行尾至行首」逐筆替換策略，以避免替換後行號/字元位置偏移。新增類似功能時請維持此慣例。
6.  **URL 讀取功能**: `parse_page_html()`（於 `aa_tool/url_fetcher.py`）以 `_DOMAIN_PARSERS` 對應表分派到各網域專屬解析器 (`_parse_default` / `_parse_himanatokiniyaruo` / `_parse_fc2blog` / `_parse_yaruobook`)。新增網站支援時，需撰寫新的 `_parse_XXX` 函式並註冊至該對應表；若作者標頭格式不同，亦需擴充 `_is_author_post()` 的匹配規則。**未知網域**會自動依序嘗試所有解析器（預設優先），回傳第一個非空內文的結果；因此新網站若與既有格式類似，即使尚未註冊也可能直接可用。
