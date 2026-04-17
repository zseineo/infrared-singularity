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
    *   `aa_settings_cache.json` — 暫存 UI 狀態 (原文、過濾規則、術語表、話數、預覽暫存、URL 記錄、背景/文字色、各開關狀態、作品+作者歷史 `work_history` 等)，確保關閉重開後不丟失資料。**正則表達式不從暫存讀取**，由 `AA_Settings.json` 管理。
    *   `AA_Settings.json` — 正式設定檔，儲存 `filter`、`glossary`、`glossary_temp`、`base_regex`、`invalid_regex`、`symbol_regex`。
    *   `aa_original_cache.json` — 依 HTML 檔名索引的原文暫存（鍵為 basename，值為 `{text, ts}`），上限 50 筆，超過時以時間戳保留最新。編輯器儲存時寫入，`import_html` 開啟同名檔案時自動載入為比對原文。
*   **字體設計**:
    *   `aa_font` — `Meiryo` 14px，用於輸入區 TextBox
    *   PyQt6 編輯器 (`aa_edit_qt.py`) 預設 **`MS PGothic` 12pt** — **⚠️ 不可改動預設值**：整個專案所有 AA 對齊演算法（`bubble_alignment.py`、`_pad_to_width`、吶喊/斜線/普通/方框寬度計算、全螢幕預覽的 `result_font.measure()`）都以 MS PGothic 的字寬 metrics 為基準；一旦改成 submona / Meiryo / 其他字體，對齊結果會全部錯位。`DEFAULT_EDITOR_FONT` 常數與 `aa_settings_cache.json` 的 `editor_font_family` 都必須維持 `"MS PGothic"`。
    *   `Ctrl+F` 延伸出的搜尋列提供字體下拉（可編輯，預設清單含 **Monapo**/submona/MS PGothic/Meiryo/Consolas/Microsoft JhengHei/MingLiU）與 6–48pt `QSpinBox`，**僅供使用者臨時切換預覽效果**，不應修改預設值。變更後同步更新 `editor`、`orig_view`、`_measurer`、CSS 與行高；使用者個人選擇持久化於 `aa_settings_cache.json` 的 `editor_font_family` / `editor_font_size`
    *   **內建字體載入** (`load_bundled_fonts()`)：`aa_main_qt.main()` 與 `aa_edit_qt.main()` 在 QApplication 建立後各呼叫一次，掃描 `fonts/` 資料夾中的 `.ttf` / `.otf` 並以 `QFontDatabase.addApplicationFont()` 載入；目前含 `fonts/monapo.ttf`（家族名 `Monapo`）
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

主視窗工具列（`TranslatePanel._build_toolbar`）在「批次搜尋」按鈕右側另提供「編輯模式」按鈕，對應 `MainWindow.resume_edit_panel()`：若目前有開啟中的 `EditWindow`，會直接切回編輯面板，避免從編輯模式誤按返回後需要重新走流程。

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
*   **括號自動補完**: 提取後處理後，檢查文字中的括號（`【】「」""''『』（）()`）是否成對。若有未配對的括號，在 `source_line` 中定位 `text` 的位置，再檢查緊鄰的相鄰字元：有未配對的右括號時檢查 `text` 前一個字元，有未配對的左括號時檢查 `text` 後一個字元。字元吻合則補上，不吻合則維持原樣。對應 Function: `_complete_brackets(text, source_line)`。
*   **資料去重與排版**: 將過濾後的字詞存入 Dict 作去重，以 `{行號:03d}-{流水號}|日文原文` 格式輸出，並顯示提取計數於 `ext_count_label`。
*   **URL 來源跳過規則**: `extract_text(..., skip_title, author_name)` 提供兩個可選參數：
    *   `skip_title`：若 source 的第一個非空行內容等於此字串，則整行跳過（URL 讀取會以作品標題作為首行，此機制避免標題被提取）。主程式傳入 `self._last_fetched_title`，該值於 URL 讀取／下一話成功時寫入；若使用者手動貼上新文字覆蓋標題行則不會命中，規則自動失效。
    *   `author_name`：提取結果經過後處理後，若去除前後空白等於此名稱則剔除（避免作者自述／簽名單獨被提取）。主程式傳入 `self._author_name`。另因作者名稱常含開頭符號（如「◆Hr94QM5gdI」），而提取後處理會去掉符號僅保留英數字 trip code，故會從 `author_name` 以 `re.findall(r'[A-Za-z0-9]{6,}')` 抽出最長英數字串作為額外比對鍵（符合即剔除）。
*   **自動複製**: 若 `auto_copy_switch` 開啟，提取後自動複製至剪貼簿。
*   **Debug 工具**: Toolbar 上的「提取 Debug」按鈕(`analyze_extraction()`)可對選取文字逐步分析提取流程，於 Modal 顯示報告。

### 4.2 分割複製 (`copy_split()`)
*   **對應 Function**: `def copy_split(self, half):`
*   支援三種模式：`'top'` (上半部)、`'bottom'` (下半部)、`'all'` (全部)，複製至剪貼簿。
*   介面上對應「複製上半」、「複製下半」、「複製全部」三顆按鈕。
*   **Toast 回饋**：複製後呼叫 `show_status()` 顯示「✅ 已複製{全部/上半/下半}（N 行）到剪貼簿」；若提取結果為空則顯示警告 Toast。

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
        * **內文 trim 規則**：解析 inner 時只剝除尾端的**半形逗點** (`,+$`)，其餘字元（半形空白、全形空白、句點、標點）全部納入寬度計算；處理階段亦不再對 `ps['inner']` 做 `rstrip(' \u3000')`。這是為了讓使用者輸入的翻譯句尾標點能夠正確影響邊框寬度。
    *   **對話框(全) (`adjust_all_bubbles()`)**: 掃描全文，自動偵測並對齊所有獨立對話框（普通/吶喊/斜線三種），由下而上逐框處理以避免行號偏移。
    *   **對齊上一行 (`align_to_prev_line()`)**: 於游標處往前補足空白，直到與上一行結尾長度切齊。
    *   **智慧判斷 (`smart_action()`)**: (Hotkey: `Ctrl+Q`) 自動判斷：選取多行 → `adjust_bubble()`；選取單行區塊 → `apply_color()`；僅有游標 → `align_to_prev_line()`。

    **群組 3 — 視窗控制:**
    *   **底色**: `_choose_bg()` 於 PyQt6 編輯器（`aa_edit_qt.py`）可調整編輯區底色，**僅作為編輯中的視覺效果**，儲存 HTML 時不寫入 `bg_color`（交由後續網站樣式控制）。
    *   **行高設定**: `_apply_line_height_to()` 取 `max(主字型 lineSpacing × 120%, Microsoft JhengHei lineSpacing × 1.02)` 作為固定像素值，套用 `LineHeightTypes.FixedHeight`。此作法可避免繁中字元（如「嗯」）觸發系統 fallback 字型時，`ProportionalHeight` 以該 fallback 字型較大的 ascent+descent 為基準導致單行行距被撐高的問題；同時取 CJK fallback 字型高度的 max 可確保 fallback 字不會被切頂/擠壓，保留純日文行的 120% 視覺比例。字型家族或大小變更時於 `_apply_editor_font()` 末尾重新計算並套用。另於 `_on_changed()` 呼叫 `editor.viewport().update()`，避免刪除 fallback 字元後殘留渲染痕跡。
    *   **搜尋找不到時不跳位**: `_find_next()` 會先保存原本 `textCursor` 與捲動位置，找不到（含 wrap-around 仍無結果）時恢復，避免捲到最上面。
    *   **💾 儲存（PyQt6 編輯器）**: 工具列的「💾 儲存」按鈕執行 `_save_as()`，**永遠彈出另存新檔對話框**；`Ctrl+S` 對應 `_save_overwrite()`，若有真實檔案路徑則直接覆寫、若為 `apply_translation` 產生的暫存檔（`_is_temp_file=True`）則仍走另存。儲存成功後透過 `on_save` callback 通知 `MainWindow._on_edit_saved()`，由主程式更新導覽列/視窗標題並將 `_original_text` 寫入 `aa_original_cache.json`。
    *   **📂 開啟（PyQt6 編輯器）**: 位於「儲存」與「返回」之間，呼叫 `MainWindow.import_html()`，與主畫面「打開已儲存的 HTML」按鈕相同；開啟時會自動查找 `aa_original_cache.json` 中的同名紀錄作為比對原文。
    *   **進入編輯器時自動回到最上層 (`_scroll_to_top()`)**: `MainWindow.show_edit_panel()` 重新載入既有 `EditWindow` 時，若沒有指定 `scroll_to_line`，會呼叫 `_scroll_to_top()` 將游標與 `verticalScrollBar`/`horizontalScrollBar` 都歸零；避免「按替換進入」或「開啟舊檔」時還停留在上一份檔案的捲動位置。
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
    *   讀取紀錄列表（最多 50 筆，倒序顯示）+ 清除紀錄按鈕（紅）。
    *   點擊關聯/紀錄項目自動填入 URL 並觸發抓取。
    *   成功後 400ms 自動關閉。
    *   `closeEvent` 同步最終 `author_only` 狀態。
*   **網頁解析 (`aa_tool/url_fetcher.py` → `parse_page_html()`)**: 依 URL 網域自動選擇解析器，由 `_DOMAIN_PARSERS` 對應表管理。分派策略：
    1. **已知網域**（在 `_DOMAIN_PARSERS` 鍵中出現）→ 直接使用該網域專屬的解析器。
    2. **未知網域** → 依序嘗試 `_parse_default` 與全部網域解析器，回傳第一個取到非空內文的結果；全部失敗則回傳最後一次的結果（通常是空）。每個解析器以 `try/except` 包覆，避免單一例外中斷整個流程。

    目前支援：
    *   **預設 (`_parse_default`)**: 5ch 類型 (`div.article` → `dt/dd` + `dl.relate_dl`)。關聯 `<dl>` 允許 class 帶額外字樣（正則 `relate_dl[^"]*"`），兼容 `relate_dl fc2relate_entry_thumbnail_off` 之類的變體。
    *   **himanatokiniyaruo.com (`_parse_himanatokiniyaruo`)**: `dt[id=數字]` / `dd` + `div.related-entries`。關聯連結解析含巢狀 div fallback：若原始 `(.*?)</div>` regex 未取到連結（因內部有巢狀 `<div>`），改用平衡 div 深度計數找出真正的關閉標籤後再提取 `<a>` 連結
    *   **blog.fc2.com (`_parse_fc2blog`)**: `div.ently_text` **或 `div.entry_body`**（兩種 FC2 模板擇一） + `dl.relate_dl`（含 `web.archive.org` 封存版、`yaruok.blog.fc2.com` 等變體）
    *   **yaruobook.jp (`_parse_yaruobook`)**: `dt.author-res-dt` / `dd.author-res` + `ul.relatedPostsWrap.relatedPostsPrev/Next`；`li.currentPost` 標記當前話。**關聯清單排序**：原始 Prev 區塊為 `[currentPost, 前一話, 前前話, ...]`（當前在前、舊話倒序），解析時須將 Prev **反轉**後再接 Next，才能得到按時間順序排列的清單，供「下一話」按鈕以 `current_idx + 1` 正確取得下一集
*   **保留貼文內空行**: `_extract_dt_dd_posts()` 在切 dd 內容時，只 trim 尾端空行、不 trim 開頭空行，以避免「作者行」與實際 AA 內文之間的排版空行被吃掉。
*   **作者名稱格式**: `_is_author_post()` 支援兩種標頭格式並自動正規化空白（透過 `_extract_poster_name()` 共用解析）：
    1.「N 名前：AUTHOR[...]」(5ch / FC2 / himana)
    2.「N ： AUTHOR ： YYYY/MM/DD(曜) HH:MM:SS ID:XXX」(yaruobook / yaruok.blog.fc2.com 等 FC2 變體)

    行內切分用的 `_POST_HEADER_RE`（供 `_filter_color_by_author` 判斷貼文邊界）同樣支援兩種格式，避免 FC2 變體在未分段的內文中被整段過濾掉。
*   **編碼自動偵測**: 依序嘗試 `utf-8`、`cp932`、`euc-jp`、`shift_jis`。
*   **Gzip 解壓縮**: 自動解壓縮伺服器回傳的 gzip 內容。
*   **關聯記事導航**: 顯示同系列各話的連結清單，可直接點擊切換；提供上一話/下一話按鈕。
*   **忽略留言開關 (`author_only`)**: Dialog 中的「忽略留言」開關，開啟時完全排除非作者的貼文。若同時填寫作者名稱則以該名稱為準；**若未填作者名稱則自動偵測**（`_detect_main_author_from_dt()` 對應 dt/dd 結構、`_detect_main_author_from_lines()` 對應 FC2 類型的「N 名前：…」行結構），取貼文中**第一個出現**的投稿者作為作者。共用 helper `_extract_poster_name()` 負責從標頭抽取名稱。狀態持久化至暫存，「下一話」功能同樣遵循此設定。
*   **讀取歷史記錄**: 最多保存 50 筆，可點擊重新讀取，支援清除記錄。
*   **直接讀取上/下一話 (`fetch_prev_chapter()` / `fetch_next_chapter()`)**: 首頁原文區標頭提供「◀ 上一話」「下一話 ▶」按鈕，皆透過共用 `_fetch_adjacent_chapter(direction)` 從已存的關聯記事中背景讀取相鄰話。
*   **複製網址（讀取紀錄內）**: `aa_url_fetch_qt.py` 的讀取紀錄每一列除「讀取」按鈕外，另提供「複製」按鈕，以 `_copy_url_to_clipboard()` 將該筆網址複製到剪貼簿。
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
    *   `write_html_file(file_path, text_content)`: 封裝 HTML（CSS 字體設定為 `MS PGothic`/`Meiryo` monospace, 16px），用分割保全 `<span>` tag 後 `html.escape` 其他內容。包含 `viewport` meta 與手機 RWD 樣式（768px 以下字體縮小至 10px），支援觸控橫向捲動。

### 4.8 UI 通知系統
*   **`show_toast(message, color, duration)`**: 在主視窗右上角顯示浮動提示，`duration` ms 後自動關閉。PyQt6 主視窗的 `MainWindow.show_status()` 已改以此函式呈現（原本位於左下角的「就緒」狀態列已移除），會將常見的 `#0f0` 亮綠自動映射為 toast 風格的 `#28a745`。
*   **編輯器 Toast**：PyQt6 編輯器 (`aa_edit_qt.py`) 的 `EditWindow._set_status()` 亦改為直接呼叫 `show_toast`，顯示於右上角（`MainWindow` 為 parent），與首頁風格一致。先前置於編輯區底部的 `status_label` 已整組移除（含 `_position_status_label`、`_status_hide_timer`、`resizeEvent` 重定位）。顏色映射 `_STATUS_COLOR_MAP` 比照主程式。
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
*   `self.btn_work_history` / `self.work_history`: 🕘 小圖示按鈕 + 歷史清單（最多 10 筆 `{title, author}`），點擊按鈕以 QMenu 顯示。只有在按下「替換翻譯並編輯」時才透過 `_record_work_history()` 新增記錄（去重後置於最前），選單項 callback 走 `_apply_work_history()` 填回兩個欄位。`apply_translation` 產生的暫存 HTML 以 `{title}_第{num}話.html` 命名，讓 EditWindow 的 `display_title` 能正確顯示於主視窗標題列與另存新檔預設檔名
*   `self.auto_copy_switch`: 提取後自動複製開關
*   `self.batch_folder_var`: 批次搜尋資料夾路徑（透過 IPC 與 Qt 批次搜尋視窗同步）
*   `self.bg_color` / `self.fg_color`: 預覽視窗背景/文字色（持久化至暫存）
*   `self.preview_text_cache`: 預覽視窗關閉前的文字暫存（關閉時寫入 `save_cache()`，下次開啟可選擇恢復）
*   `self.url_history`: URL 讀取歷史（最多 50 筆）
*   `self.url_related_links`: 當前頁面的關聯記事連結清單
*   `self.current_url`: 當前讀取的網址
*   `final_textbox` (在 `show_result_modal` 內): 全螢幕預覽模式的唯一文字輸出框，支援滑鼠滾輪（每格 8 行）快捷鍵客製。

## 6. 給後續開發維護 AI 的系統提示 (For AI Agents)
1.  **確保對齊機制的字體計算**: 所有對話框對齊與補空白演算法（`bubble_alignment.py`、`result_font.measure(...)`、`QtFontMeasurer`）都以 **MS PGothic** 的字寬 metrics 為基準，這是整個專案 AA 對齊的核心假設。**切勿改變預設字體設定**（含 `DEFAULT_EDITOR_FONT` 常數、`aa_settings_cache.json` 的 `editor_font_family`、主預覽 `result_font`），否則所有自動對齊功能會失準。若使用者透過編輯器搜尋列切換字體，那是使用者暫時預覽行為，不應把新字體寫回預設值。
2.  **效能考量**: 處理長文本時 `re.sub` 或大量 TextBox 修改 (`delete`, `insert`) 會造成一定延遲。必要時請以 string method 替代，或一次性取出大量 list 再 `'\n'.join()` 放回，減少 GUI 卡頓。
3.  **保持替換長度優先排序規矩**: 修改 `apply_translation()` 邏輯時，必須延續「由原文長度遞減排序 (`valid_ids.sort`) 後再 replace」，否則疊加翻譯文將互相破壞（如先替「あ」再替「あはは」）。
4.  **術語表有兩張**: 一般術語表 (`glossary_text`) 與臨時術語表 (`glossary_text_temp`)。所有需要套用術語的地方都應呼叫 `get_combined_glossary()` 合併後使用，而非只讀取其中一個。
5.  **術語表重複偵測**: 貼上術語時，系統會自動掃描兩張術語表中等號左邊的原文是否重複。若偵測到重複，會在術語表標題旁顯示警告，並出現「跳到重複」按鈕，可循環跳躍至每個重複項目（含跨 tab 跳躍）。對應 Function: `_check_glossary_duplicates()`、`_jump_to_glossary_dup()`。
5.  **批次操作的行號偏移**: `batch_replace_all()` 和 `adjust_all_bubbles()` 都採用「由下而上」或「由行尾至行首」逐筆替換策略，以避免替換後行號/字元位置偏移。新增類似功能時請維持此慣例。
6.  **URL 讀取功能**: `parse_page_html()`（於 `aa_tool/url_fetcher.py`）以 `_DOMAIN_PARSERS` 對應表分派到各網域專屬解析器 (`_parse_default` / `_parse_himanatokiniyaruo` / `_parse_fc2blog` / `_parse_yaruobook`)。新增網站支援時，需撰寫新的 `_parse_XXX` 函式並註冊至該對應表；若作者標頭格式不同，亦需擴充 `_is_author_post()` 的匹配規則。**未知網域**會自動依序嘗試所有解析器（預設優先），回傳第一個非空內文的結果；因此新網站若與既有格式類似，即使尚未註冊也可能直接可用。
