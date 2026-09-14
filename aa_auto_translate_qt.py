"""自動翻譯面板（嵌入主視窗 QStackedWidget）。

對應使用者流程：在主畫面工具列按「⚡ 自動翻譯」→ 切換到本面板（index 4）。
面板分上下兩部分：
  上：設定欄位（起始網址、話數＋翻譯到最後一話、檔名（含作品名稱）、輸出資料夾）。
  下：執行 Log（即時顯示 :func:`aa_auto_translate.run_auto_translate` 的進度）。

「連線設定」（翻譯方式／Gem 網址／要求模型／換新對話次數／API 金鑰與 Prompt）改以
浮層面板呈現，由主視窗導覽列「⚙ 連線設定」鈕（返回首頁鈕右側）開合，作法比照
網址讀取頁的「展開標題按鈕面板」。

執行緒生命週期、stop_event、橫幅由 ``MainWindow`` 統籌；本面板只負責收集
參數、顯示 Log，並提供 Start / Stop 按鈕，避免狀態散落兩處。
"""
from __future__ import annotations

import os
import threading

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from aa_tool.gemini_api import API_MODELS
from aa_tool.openai_api import API_PROVIDERS
from aa_tool import app_paths, secure_store

# 翻譯後端選項：(顯示文字, 內部值)
_BACKEND_OPTIONS: list[tuple[str, str]] = [
    ("瀏覽器", "browser"),
    ("API", "api"),
]

# 連線設定「進階設定」的項目：(分組, [(項目 key, 顯示文字, 說明), ...])。
# key 與預設值見 aa_tool.gemini_web.ERROR_POLICY_DEFAULTS。
_ERROR_POLICY_UI: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("API 模式", [
        ("api_5xx", "伺服器忙碌（HTTP 5xx）",
         "API 回 500／502／503／504，多半是伺服器高負載。"),
        ("api_timeout", "回應逾時／連線逾時",
         "超過「API 逾時」秒數沒收到完整回覆，或連線握手逾時。\n"
         "每次都逾時代表這一話太長，請調高「API 逾時」。"),
        ("api_conn_after_ok", "連線中斷（這批已成功翻譯過）",
         "連不上、伺服器中途斷線、回應不是 JSON——且這批已經成功翻譯過，\n"
         "設定沒問題，多半是網路一時中斷（Wi-Fi、睡眠喚醒、VPN 重連）。"),
        ("api_conn_first", "連線失敗（這批還沒成功翻譯過）",
         "同上，但第一次送出就失敗——多半是網路、Proxy 或端點網址設定有問題。\n"
         "改成重試的話，設定錯誤時每一話都要空等整輪重試才看得出來。"),
        ("api_4xx", "HTTP 4xx 錯誤（429 除外）",
         "例如 400 請求格式錯、401／403 金鑰無效、404 模型不存在。\n"
         "額度上限（429）另有金鑰冷卻邏輯，不受此項影響。"),
        ("api_empty", "空回應",
         "API 回應成功但沒有內容，且不是被安全過濾擋下、也不是輸出被截斷。"),
    ]),
    ("瀏覽器模式", [
        ("web_stuck", "Gemini 卡住",
         "送出後超過 10 分鐘沒有回應，開新對話重送一次仍沒有回應。\n"
         "重試＝暫時跳過這一話、放進待補翻列表，下一話翻譯成功後再補翻。"),
    ]),
    ("兩種模式", [
        ("fetch_fail", "抓取網頁失敗",
         "讀取作品網頁時連線失敗或 HTTP 錯誤（解析失敗、找不到內文不在此列）。\n"
         "重試＝每 90 秒重抓一次，最多 10 次；仍失敗才中斷（不知道下一話，無法跳過）。"),
    ]),
]

# API 供應商選項：(顯示文字, 內部值)；順序沿用 API_PROVIDERS 宣告順序。
_PROVIDER_OPTIONS: list[tuple[str, str]] = [
    (meta["label"], pid) for pid, meta in API_PROVIDERS.items()
]


def _provider_models(provider: str) -> list[str]:
    """回傳供應商的模型建議清單；gemini 用 gemini_api.API_MODELS。"""
    if provider == "gemini":
        return list(API_MODELS)
    return list(API_PROVIDERS.get(provider, {}).get("models", []))

# 自動產生、不能手動修改的欄位（自動模式的檔名、作品資料夾）：刻意挑可辨識的
# 中灰底色與可編輯欄位（白底）作視覺區隔，避免誤以為可以編輯。
_READONLY_FIELD_QSS = (
    "QLineEdit { background:#d6d8db; color:#343a40; border:1px solid #adb5bd; }"
    "QLineEdit:disabled { background:#e9ecef; color:#adb5bd;"
    " border:1px solid #dee2e6; }")

# 起始網址等欄位變動後，等使用者停手這麼久才重算檔名／作品資料夾（毫秒）。
_REFRESH_DEBOUNCE_MS = 600

# 翻譯方式切換鈕樣式：選中（藍底）／未選中（灰底）
_BACKEND_BTN_SEL = (
    "QPushButton { background:#0d6efd; color:white; padding:6px 16px;"
    " border:none; border-radius:4px; }"
    "QPushButton:disabled { background:#6c757d; color:#ced4da; }")
_BACKEND_BTN_UNSEL = (
    "QPushButton { background:#e9ecef; color:#495057; padding:6px 16px;"
    " border:1px solid #ced4da; border-radius:4px; }"
    "QPushButton:hover { background:#dee2e6; }"
    "QPushButton:disabled { background:#f1f3f5; color:#adb5bd; }")


# (顯示文字, 內部值)；內部值需與 aa_tool.gemini_web.model_matches 一致。
_MODEL_OPTIONS: list[tuple[str, str]] = [
    ("Pro", "pro"),
    ("Flash", "flash"),
    ("Flash-Lite", "flash-lite"),
    ("不檢查（任何模型都接受）", "any"),
]


def _font(size: int = 12, bold: bool = False) -> QFont:
    f = QFont("Microsoft JhengHei", size)
    if bold:
        f.setBold(True)
    return f


def _btn(text: str, color: str, hover: str, *, width: int = 0) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(
        f"QPushButton {{ background:{color}; color:white;"
        f" padding:6px 14px; border:none; border-radius:4px; }}"
        f"QPushButton:hover {{ background:{hover}; }}"
        f"QPushButton:disabled {{ background:#6c757d; color:#ced4da; }}"
    )
    if width:
        b.setMinimumWidth(width)
    b.setFont(_font(12, bold=True))
    return b


class AutoTranslatePanel(QWidget):
    """連續多話自動翻譯設定＋Log 面板。"""

    def __init__(self, main_window) -> None:
        super().__init__()
        self._main = main_window
        self._running = False
        self._build_ui()
        self._load_from_main()
        # ESC：連線設定浮層開著→關浮層；否則→返回首頁。
        # 用 WidgetWithChildren context，子欄位（QLineEdit / QPlainTextEdit）
        # 有焦點時 ESC 也能觸發。
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self._on_escape)

    def _on_escape(self) -> None:
        if self._conn_panel.isVisible():
            self._conn_panel.hide()
        else:
            self._main.show_translate_panel()

    # ── UI ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # ── 上半：設定 ──
        top = QWidget()
        form = QFormLayout(top)
        form.setContentsMargins(4, 4, 4, 4)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        url_row = QWidget()
        url_hl = QHBoxLayout(url_row)
        url_hl.setContentsMargins(0, 0, 0, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("起始話的網址")
        url_hl.addWidget(self.url_edit, 1)
        # 手動網址清單：關聯記事尚未支援的站台，可自行貼上整批網址（一行一個）。
        # 清單非空時整批完全照清單跑，本欄位（起始網址）本次忽略。
        self._url_list_text = ""
        self.btn_url_list = QPushButton("📋 網址清單")
        self.btn_url_list.setToolTip(
            "手動指定每一話的網址（一行一個）。" + chr(10) +
            "填了之後就不依關聯記事找下一話，改照清單順序跑，" + chr(10) +
            "適合關聯記事尚未支援的站台。清空即恢復原本行為。")
        self.btn_url_list.clicked.connect(self._open_url_list_dialog)
        url_hl.addWidget(self.btn_url_list)
        form.addRow("起始網址：", url_row)

        count_row = QWidget()
        count_hl = QHBoxLayout(count_row)
        count_hl.setContentsMargins(0, 0, 0, 0)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 999)
        self.count_spin.setSuffix(" 話")
        self.until_last = QCheckBox("翻譯到最後一話")
        self.until_last.toggled.connect(
            lambda chk: self.count_spin.setEnabled(not chk))
        count_hl.addWidget(self.count_spin)
        count_hl.addSpacing(8)
        count_hl.addWidget(self.until_last)
        count_hl.addStretch()
        form.addRow("連續話數：", count_row)

        # 檔名：作品名稱與檔名預覽共用一列，依「自動填入作品名稱」設定切換
        # （_apply_title_mode）。手動模式＝可編輯的作品名稱＋右側灰字尾碼
        # （話數／同名序號／副檔名）；自動模式＝整個檔名由頁面標題產生，改顯示
        # 唯讀欄位。起始網址／清單／輸出資料夾／作品名稱變動時自動重算
        # （_schedule_refresh），不必另外按鈕。
        name_row = QWidget()
        name_hl = QHBoxLayout(name_row)
        name_hl.setContentsMargins(0, 0, 0, 0)
        name_hl.setSpacing(4)
        self.doc_title_edit = QLineEdit()
        self.doc_title_edit.setPlaceholderText("作品名稱（必填，作為檔名前綴）")
        self.doc_title_edit.setToolTip(
            "檔名＝{作品名稱}_{偵測到的話數}.html，右側灰字為起始網址這一話\n"
            "實際會接上的部分（同名衝突時自動加 -2、-3 等序號）。")
        self.doc_title_edit.textChanged.connect(self._on_doc_title_changed)
        self.filename_suffix = QLabel("")
        self.filename_suffix.setStyleSheet("color:#6c757d;")
        self.filename_preview = QLineEdit()
        self.filename_preview.setReadOnly(True)
        self.filename_preview.setStyleSheet(_READONLY_FIELD_QSS)
        self.filename_preview.setToolTip(
            "已開啟「自動填入作品名稱」設定：檔名取自每話的頁面標題，不能手動修改。\n"
            "這裡顯示起始網址這一話實際會寫入的檔名（同名衝突時自動加 -2、-3 等序號）。")
        name_hl.addWidget(self.doc_title_edit, 1)
        name_hl.addWidget(self.filename_suffix)
        name_hl.addWidget(self.filename_preview, 1)
        form.addRow("檔名：", name_row)

        out_row = QWidget()
        out_hl = QHBoxLayout(out_row)
        out_hl.setContentsMargins(0, 0, 0, 0)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("輸出 HTML 的資料夾")
        # 輸出資料夾即時持久化：手動輸入完成（失焦／Enter）也記住，不必等按「開始」。
        self.out_edit.editingFinished.connect(self._persist_out_dir)
        # 同名序號（-2／-3）看輸出資料夾裡有沒有同名檔，換資料夾要重算
        self.out_edit.textChanged.connect(lambda _t: self._schedule_refresh())
        # 作品資料夾「已存在」提示看的是輸出資料夾底下，換資料夾立即重判
        self.out_edit.textChanged.connect(lambda _t: self._update_series_exists())
        btn_browse = QPushButton("瀏覽…")
        btn_browse.clicked.connect(self._browse_out_dir)
        out_hl.addWidget(self.out_edit, 1)
        out_hl.addWidget(btn_browse)
        form.addRow("輸出資料夾：", out_row)

        # 依作品名分資料夾：勾選後在輸出資料夾底下開一層以作品命名的子資料夾。
        # 名稱整批只算一次（起始網址那一話），故不會一話一個資料夾。
        self.group_by_series_cb = QCheckBox(
            "依作品名稱建立資料夾（同名資料夾已存在則直接放進去）")
        self.group_by_series_cb.setToolTip(
            "勾選後在輸出資料夾底下開一層以作品命名的子資料夾，本批各話都存進去。\n"
            "・資料夾名整批只決定一次（依起始網址那一話），不會每話開一個資料夾\n"
            "・自動填入作品名稱模式 → 由頁面標題去掉話數後取得作品名主體\n"
            "・手動模式 → 直接用上面檔名欄的作品名稱\n"
            "・同名資料夾已存在就直接沿用，不另外建新的\n"
            "算出的名稱會顯示在下面「作品資料夾」欄。")
        self.group_by_series_cb.toggled.connect(self._set_series_row_enabled)
        # 勾選與否決定存進輸出資料夾還是作品子資料夾，同名序號要重算
        self.group_by_series_cb.toggled.connect(lambda _c: self._schedule_refresh())
        form.addRow("", self.group_by_series_cb)

        # 作品資料夾名（唯讀）：跟著檔名欄的模式——手動模式等於作品名稱，
        # 自動模式由頁面標題去掉話數後產生。要改名就改作品名稱（手動模式）。
        self.series_folder_edit = QLineEdit()
        self.series_folder_edit.setReadOnly(True)
        self.series_folder_edit.setStyleSheet(_READONLY_FIELD_QSS)
        self.series_folder_edit.setToolTip(
            "本批實際會使用的作品資料夾名稱（輸出資料夾底下的一層），不能手動修改。\n"
            "・手動模式 → 等於檔名欄的作品名稱\n"
            "・自動填入作品名稱模式 → 起始網址的頁面標題去掉話數後的作品名主體")
        self._series_folder_value = ""  # 目前起始網址算出的有效名稱（開始時帶給協調器）
        # 右側提示：輸出資料夾底下是否已有同名資料夾（已存在就直接放進去）
        self.series_exists_label = QLabel("")
        series_row = QWidget()
        series_hl = QHBoxLayout(series_row)
        series_hl.setContentsMargins(0, 0, 0, 0)
        series_hl.setSpacing(6)
        series_hl.addWidget(self.series_folder_edit, 1)
        series_hl.addWidget(self.series_exists_label)
        form.addRow("作品資料夾：", series_row)
        self._series_folder_label = form.labelForField(series_row)

        # 起始網址等欄位變動後延遲一下再重算檔名／作品資料夾（連續輸入只算一次）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(_REFRESH_DEBOUNCE_MS)
        self._refresh_timer.timeout.connect(self._refresh_previews)
        self._preview_gen = 0      # 重算世代；背景結果回來時不是最新一次就丟掉
        self._title_auto = False   # 目前是否為「自動填入作品名稱」模式（_apply_title_mode）
        self._previewed_url = ""  # 最近一次成功算出檔名的網址（換網址才顯示讀取中）
        self.url_edit.textChanged.connect(lambda _t: self._schedule_refresh())

        self.skip_existing_cb = QCheckBox("已存在同名檔則跳過（重跑時略過已完成的話）")
        self.skip_existing_cb.setToolTip(
            "翻譯前先算好這一話的檔名，若輸出資料夾已有同名檔（不計 -2／-3 序號）\n"
            "就跳過該話、直接翻下一話。適合批次中斷後重跑、略過已翻好的話並省 API 額度。\n"
            "關閉時維持原行為：同名一律加 -2／-3 序號另存新檔。")
        form.addRow("", self.skip_existing_cb)

        # 加入翻譯 ↔ 替換翻譯：對應主畫面兩顆按鈕（自動翻譯一律直接存檔、不進編輯器）
        self.append_mode_cb = QCheckBox("加入翻譯（保留原文，翻譯附在原文之後）")
        self.append_mode_cb.setToolTip(
            "對應主畫面兩顆按鈕：\n"
            "  勾選 → 「加入翻譯」：保留原文，翻譯附加在原文之後。\n"
            "  取消 → 「替換翻譯」：以翻譯取代原文（預設，維持原行為）。\n"
            "兩種都直接存檔、不進編輯器。")
        form.addRow("", self.append_mode_cb)

        # 翻譯方式（瀏覽器／API）——兩顆切換鈕，被選中的以顏色提示；常用切換放主頁
        self._backend = "browser"
        self._backend_btns: dict[str, QPushButton] = {}
        bk_row = QWidget()
        bk_hl = QHBoxLayout(bk_row)
        bk_hl.setContentsMargins(0, 0, 0, 0)
        bk_hl.setSpacing(6)
        for label, value in _BACKEND_OPTIONS:
            b = QPushButton(label)
            b.setMinimumWidth(84)
            b.setFont(_font(11, bold=True))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=value: self._set_backend(v))
            self._backend_btns[value] = b
            bk_hl.addWidget(b)
        bk_hl.addStretch()
        form.addRow("翻譯方式：", bk_row)

        # 替換過濾詞：送給 AI 前把清單裡的詞換成 ○（兩種翻譯方式都適用）
        mask_row = QWidget()
        mask_hl = QHBoxLayout(mask_row)
        mask_hl.setContentsMargins(0, 0, 0, 0)
        self.mask_words_cb = QCheckBox("替換過濾詞（送出前把關鍵字換成 ○）")
        self.mask_words_cb.setToolTip(
            "勾選後，提取結果送給 AI 前，會把「過濾詞清單」裡的詞換成 ○（每個字一個 ○），\n"
            "降低整段被 AI 審查擋下的機率。\n"
            "・只影響送給 AI 的文字；存檔的譯文中，這些詞會以 ○ 呈現\n"
            "・清單一行一個詞（直接比對文字，不是正則）")
        mask_hl.addWidget(self.mask_words_cb)
        self._mask_word_list_text = ""
        self.btn_mask_list = QPushButton("📝 過濾詞清單")
        self.btn_mask_list.setToolTip("編輯要換成 ○ 的關鍵字（一行一個）")
        self.btn_mask_list.clicked.connect(self._open_mask_list_dialog)
        mask_hl.addWidget(self.btn_mask_list)
        mask_hl.addStretch()
        form.addRow("", mask_row)

        # 譯文關鍵字檢查：譯文出現關鍵字時，依各詞設定暫停／停止／跳過（兩種翻譯方式都適用）
        kw_row = QWidget()
        kw_hl = QHBoxLayout(kw_row)
        kw_hl.setContentsMargins(0, 0, 0, 0)
        self.output_kw_cb = QCheckBox("譯文關鍵字檢查（出現時暫停／停止／跳過）")
        self.output_kw_cb.setToolTip(
            "勾選後，每一話翻譯回來的譯文若出現「關鍵字設定」裡的詞，依該詞設定的動作處理：\n"
            "・暫停：照常存檔後原地暫停，按上方橫幅的「▶ 繼續」才翻下一話\n"
            "・停止：這一話不存檔，結束整批（起始網址會回填這一話）\n"
            "・跳過：這一話不存檔，列入失敗清單，繼續翻下一話\n"
            "同一話同時命中多種動作時：停止 > 跳過 > 暫停。直接比對文字，不是正則。")
        kw_hl.addWidget(self.output_kw_cb)
        self._output_kw_rules: list[dict] = []
        self.btn_output_kw = QPushButton("📝 關鍵字設定")
        self.btn_output_kw.setToolTip("設定要檢查的關鍵字，以及各自要暫停／停止／跳過")
        self.btn_output_kw.clicked.connect(self._open_output_kw_dialog)
        kw_hl.addWidget(self.btn_output_kw)
        kw_hl.addStretch()
        form.addRow("", kw_row)

        # 動作按鈕列
        btn_row = QWidget()
        btn_hl = QHBoxLayout(btn_row)
        btn_hl.setContentsMargins(0, 4, 0, 0)
        self.btn_start = _btn("▶ 開始自動翻譯", "#d63384", "#b02a6f", width=140)
        self.btn_start.clicked.connect(self._on_start)
        btn_hl.addWidget(self.btn_start)
        self.btn_stop = _btn("■ 停止", "#dc3545", "#b02a37", width=80)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        btn_hl.addWidget(self.btn_stop)
        btn_clear = _btn("清空 Log", "#6c757d", "#5a6268", width=80)
        btn_clear.clicked.connect(self._clear_log)
        btn_hl.addWidget(btn_clear)
        btn_hl.addStretch()
        form.addRow(btn_row)

        splitter.addWidget(top)

        # ── 下半：Log ──
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 6, 0, 0)
        bv.setSpacing(4)
        lbl = QLabel("執行 Log")
        lbl.setFont(_font(12, bold=True))
        bv.addWidget(lbl)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 10))
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#1e1e1e; color:#dcdcdc;"
            " border:1px solid #3c3c3c; }")
        bv.addWidget(self.log_view, 1)
        splitter.addWidget(bottom)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 480])

        self._build_conn_panel()

    def _build_conn_panel(self) -> None:
        """連線設定浮層：翻譯方式／Gem 網址／要求模型／換新對話次數／API 金鑰、模型、Prompt。

        作法比照網址讀取頁的「展開標題按鈕面板」——`self` 的浮層子元件，預設隱藏，
        由主視窗導覽列「⚙ 連線設定」鈕呼叫 :meth:`toggle_conn_panel` 開合。
        """
        self._conn_panel = QWidget(self)
        self._conn_panel.setObjectName("autoConnPanel")
        self._conn_panel.setStyleSheet(
            "#autoConnPanel { background:#f1f3f5; border:1px solid #adb5bd;"
            " border-radius:6px; }")
        self._conn_panel.hide()
        outer = QVBoxLayout(self._conn_panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        outer.addWidget(scroll, 1)

        inner = QWidget()
        self._conn_inner = inner  # 供 _position_conn_panel 依內容高度收掉底部空白
        scroll.setWidget(inner)
        v = QVBoxLayout(inner)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        title = QLabel("連線設定")
        title.setFont(_font(15, bold=True))
        v.addWidget(title)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        v.addLayout(form)

        self.gem_edit = QLineEdit()
        self.gem_edit.setPlaceholderText("https://gemini.google.com/gem/...")
        form.addRow("Gemini Gem 網址：", self.gem_edit)

        self.use_gem_cb = QCheckBox("瀏覽器模式使用 Gem（不發送 Prompt 2）")
        self.use_gem_cb.setToolTip(
            "本核取框只影響「翻譯 Prompt 2」在瀏覽器模式是否送出：\n"
            "  勾選 → 瀏覽器模式靠 Gem 內建 Prompt，不送出 Prompt 2。\n"
            "  取消勾選 → 瀏覽器模式會附加 Prompt 2。\n"
            "「翻譯 Prompt 1」在瀏覽器模式『一律不送』，不受此選項影響。\n"
            "API 模式一律會送出 Prompt 1＋Prompt 2，不受此選項影響。")
        form.addRow("", self.use_gem_cb)

        self.model_combo = QComboBox()
        for label, value in _MODEL_OPTIONS:
            self.model_combo.addItem(label, value)
        self.model_combo.setToolTip(
            "若偵測到 Gemini 目前使用的模型與此不符，整批自動中止。\n"
            "讀不到模型字串時不會阻擋（會在 Log 顯示警告，請自行於瀏覽器確認）。")
        form.addRow("要求模型：", self.model_combo)

        self.max_session_spin = QSpinBox()
        self.max_session_spin.setRange(1, 99)
        self.max_session_spin.setSuffix(" 次")
        self.max_session_spin.setToolTip(
            "同一對話內最多送幾次給 Gemini，達上限自動開新對話。\n"
            "目的：避免單一對話累積太多上下文使翻譯品質下降。")
        form.addRow("每 N 次送出後換新對話：", self.max_session_spin)

        # 分隔線：上方為通用／瀏覽器設定，下方為 API 專屬設定
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setStyleSheet("color:#adb5bd;")
        form.addRow(sep)

        self._conn_form = form  # 供切換供應商時隱藏「自定義端點」列
        # 每供應商在本次面板開啟期間的暫存（切供應商即互換，儲存時一併寫入）
        self._provider_keys: dict[str, list[str]] = {}
        self._provider_models: dict[str, str] = {}
        self._cur_provider = "gemini"

        self.api_provider_combo = QComboBox()
        for label, value in _PROVIDER_OPTIONS:
            self.api_provider_combo.addItem(label, value)
        self.api_provider_combo.setToolTip(
            "選擇 API 供應商。各供應商的金鑰、模型分開記住，切換不互相覆蓋。\n"
            "OpenAI／DeepSeek／自定義走 OpenAI 相容端點；Claude 走 Anthropic 端點。")
        self.api_provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("API 供應商：", self.api_provider_combo)

        self.api_model_combo = QComboBox()
        self.api_model_combo.setEditable(True)  # 允許自行輸入其他 model id
        self.api_model_combo.setToolTip(
            "下拉為常見建議值，亦可直接輸入其他 model id（如新推出的型號）。")
        form.addRow("API 模型：", self.api_model_combo)

        self.api_base_url_edit = QLineEdit()
        self.api_base_url_edit.setPlaceholderText(
            "自定義 OpenAI 相容端點，例：https://openrouter.ai/api/v1")
        self.api_base_url_edit.setToolTip(
            "僅「自定義」供應商需填；需為 OpenAI 相容端點（會呼叫 {base_url}/chat/completions）。")
        form.addRow("自定義端點：", self.api_base_url_edit)

        self.api_keys_edit = QPlainTextEdit()
        self.api_keys_edit.setPlaceholderText(
            "每行一把 API 金鑰；多把會輪流送出請求（各供應商分開記住）")
        self.api_keys_edit.setFixedHeight(90)
        # 金鑰加密與儲存說明改放此欄位的浮動提示（滑過顯示）
        self.api_keys_edit.setToolTip(
            "🔒 金鑰以 Windows DPAPI 加密存於 aa_api_keys.dat" if secure_store.is_real_encryption()
            else "⚠️ 非 Windows：金鑰僅 base64 混淆儲存，安全性較低")
        form.addRow("API 金鑰：", self.api_keys_edit)

        self.api_timeout_spin = QSpinBox()
        self.api_timeout_spin.setRange(30, 3600)
        self.api_timeout_spin.setSingleStep(30)
        self.api_timeout_spin.setSuffix(" 秒")
        self.api_timeout_spin.setToolTip(
            "單次 API 請求最多等多久才判定失敗（預設 600 秒）。\n"
            "逾時會自動等待後重試（與伺服器忙碌相同）；但若 Log 每次都出現\n"
            "「API 回應逾時」→ 代表這一話太長、模型跑不完，請把此值調高。\n"
            "慢速／長輸出的模型翻長篇 AA 時可能需要 1200 秒以上。")
        form.addRow("API 逾時：", self.api_timeout_spin)

        # 翻譯 Prompt（兩段）——區塊 1：僅 API 送出；區塊 2：API 送，瀏覽器於未勾選
        # 「使用 Gem」時也送（即「不送出 Prompt」的核取框為未勾選時）。
        self.api_only_prompt_edit = QPlainTextEdit()
        self.api_only_prompt_edit.setPlaceholderText(
            "（區塊 1）僅 API 模式送出。瀏覽器模式『永遠不送』此區塊。\n"
            "適合放：替代 Gem 內建人設的翻譯指令／角色設定（API 沒有 Gem 人設）。")
        self.api_only_prompt_edit.setFixedHeight(120)
        form.addRow("翻譯 Prompt 1：", self.api_only_prompt_edit)

        self.api_prompt_edit = QPlainTextEdit()
        self.api_prompt_edit.setPlaceholderText(
            "（區塊 2）API 模式一律送出；瀏覽器模式僅在『使用 Gem（不發送 Prompt）』\n"
            "核取框未勾選時送出（會附加在每個新對話的第一則訊息開頭）。\n"
            "適合放：兩種模式共用的補充指令／格式要求。")
        self.api_prompt_edit.setFixedHeight(120)
        form.addRow("翻譯 Prompt 2：", self.api_prompt_edit)

        # 進階設定：各種錯誤要中斷或重試（預設收合）
        self._build_error_policy_section(v)

        # 動作列
        btn_row = QWidget()
        bh = QHBoxLayout(btn_row)
        bh.setContentsMargins(0, 4, 0, 0)
        btn_save = _btn("💾 儲存連線設定", "#28a745", "#218838", width=140)
        btn_save.clicked.connect(self._save_conn_settings)
        bh.addWidget(btn_save)
        btn_close = _btn("← 關閉", "#6c757d", "#5a6268", width=80)
        btn_close.clicked.connect(self._conn_panel.hide)
        bh.addWidget(btn_close)
        bh.addStretch()
        v.addWidget(btn_row)

    def _build_error_policy_section(self, v: QVBoxLayout) -> None:
        """連線設定浮層底部的「進階設定」：每種中斷／重試狀況一個下拉（重試／中斷）。

        預設收合；展開後重新計算浮層高度。選項值存 `self._policy_combos`
        （{項目: QComboBox}，data 為 "retry"／"stop"），儲存時只記與預設不同的項目。
        """
        from aa_tool.gemini_web import ERROR_POLICY_DEFAULTS
        self.btn_policy_toggle = QPushButton("▸ 進階設定：遇到錯誤要中斷或重試")
        self.btn_policy_toggle.setCheckable(True)
        self.btn_policy_toggle.setFlat(True)
        # 只改對齊與粗體；底色／字色沿用全域主題（dark_theme.qss：藍底白字）
        self.btn_policy_toggle.setStyleSheet(
            "QPushButton { text-align:left; font-weight:bold; }")
        v.addWidget(self.btn_policy_toggle)

        box = QFrame()
        box.setObjectName("policyBox")
        box.setStyleSheet("#policyBox { border:1px solid #ced4da; border-radius:4px; }")
        bv = QVBoxLayout(box)
        bv.setContentsMargins(10, 8, 10, 8)
        bv.setSpacing(6)
        hint = QLabel(
            "「重試」：API 錯誤每 90 秒重試一次（每 5 次多等 10 分鐘），同一次請求重試 10 次"
            "仍失敗就暫時跳過、放進待補翻列表；瀏覽器 Gemini 卡住直接放進待補翻列表；"
            "抓取網頁每 90 秒重抓，最多 10 次，仍失敗才中斷。" + chr(10) +
            "「中斷」：第一次遇到就停止整批，起始網址會回填這一話。" + chr(10) +
            "找不到頁面元素、登入逾時、存檔失敗等重試也沒用的錯誤一律中斷；"
            "額度上限（429）另有冷卻與暫停邏輯，不在此列。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6c757d;")
        bv.addWidget(hint)

        pf = QFormLayout()
        pf.setHorizontalSpacing(10)
        pf.setVerticalSpacing(4)
        bv.addLayout(pf)
        self._policy_combos: dict[str, QComboBox] = {}
        for group, items in _ERROR_POLICY_UI:
            head = QLabel(group)
            head.setStyleSheet("font-weight:bold;")  # 字色沿用主題
            pf.addRow(head)
            for key, label, tip in items:
                combo = QComboBox()
                default = ERROR_POLICY_DEFAULTS[key]
                for val, text in (("retry", "重試"), ("stop", "中斷")):
                    combo.addItem(text + ("（預設）" if val == default else ""), val)
                combo.setToolTip(tip)
                lbl = QLabel(label + "：")
                lbl.setToolTip(tip)
                pf.addRow(lbl, combo)
                self._policy_combos[key] = combo

        btn_reset = QPushButton("恢復預設")
        btn_reset.clicked.connect(lambda: self._apply_error_policy({}))
        rh = QHBoxLayout()
        rh.addWidget(btn_reset)
        rh.addStretch()
        bv.addLayout(rh)
        box.hide()
        v.addWidget(box)
        self._policy_box = box

        def _toggle(checked: bool) -> None:
            box.setVisible(checked)
            self.btn_policy_toggle.setText(
                ("▾" if checked else "▸") + " 進階設定：遇到錯誤要中斷或重試")
            # 內容高度變了 → 重新計算浮層高度（等版面更新後）
            QTimer.singleShot(0, self._position_conn_panel)
        self.btn_policy_toggle.toggled.connect(_toggle)

    def _apply_error_policy(self, policy: dict) -> None:
        """把設定值（缺的補預設）套到進階設定的各個下拉。"""
        from aa_tool.gemini_web import resolve_error_policy
        for key, val in resolve_error_policy(policy).items():
            combo = self._policy_combos.get(key)
            if combo is not None:
                combo.setCurrentIndex(max(0, combo.findData(val)))

    def _collect_error_policy(self) -> dict:
        """目前下拉的選擇中，與預設不同的項目 → {項目: 值}。"""
        from aa_tool.gemini_web import ERROR_POLICY_DEFAULTS
        return {k: c.currentData() for k, c in self._policy_combos.items()
                if c.currentData() != ERROR_POLICY_DEFAULTS[k]}

    def toggle_conn_panel(self) -> None:
        """開合連線設定浮層（由主視窗導覽列「⚙ 連線設定」鈕呼叫）。"""
        if self._conn_panel.isVisible():
            self._conn_panel.hide()
            return
        self._load_conn_from_main()
        self._position_conn_panel()
        self._conn_panel.show()
        self._conn_panel.raise_()
        # 把焦點移進浮層，讓 ESC（WidgetWithChildren context）一打開就能關閉
        # （「⚙ 連線設定」鈕位於主視窗導覽列、在本面板子樹之外）。
        self.gem_edit.setFocus()

    def _position_conn_panel(self) -> None:
        """把浮層放在面板左上角；寬度固定上限、高度依內容收緊（不留底部空白）。"""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        # 寬度：較先前上限再寬約 10%（600 → 660）
        pw = min(660, max(360, w - 16))
        # 高度依內容實際所需，避免「儲存設定」鈕下方出現過大空白；
        # 內容超過可用高度時才由 QScrollArea 捲動（上限 h-16）。
        content_h = self._conn_inner.sizeHint().height() + 16
        ph = min(max(300, content_h), h - 16)
        self._conn_panel.setGeometry(8, 8, pw, ph)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        if (getattr(self, "_conn_panel", None) is not None
                and self._conn_panel.isVisible()):
            self._position_conn_panel()

    def _set_backend(self, value: str) -> None:
        """切換翻譯方式並重繪兩顆鈕（選中上色）；連動 API 欄位啟用狀態。"""
        self._backend = value if value in ("browser", "api") else "browser"
        for v, b in self._backend_btns.items():
            b.setStyleSheet(_BACKEND_BTN_SEL if v == self._backend
                            else _BACKEND_BTN_UNSEL)
        self._on_backend_changed()

    def _on_backend_changed(self) -> None:
        """連線設定的 API 欄位不受翻譯方式影響——兩種方式下皆可先行編輯供應商／模型／
        金鑰／Prompt（之後再切換使用）。自定義端點列的顯示由 `_set_base_url_visible` 依
        供應商決定，此處不干涉。"""
        for w in (self.api_provider_combo, self.api_model_combo,
                  self.api_keys_edit, self.api_prompt_edit):
            w.setEnabled(True)

    # ── API 供應商切換 ──

    def _set_base_url_visible(self, visible: bool) -> None:
        """自定義端點列僅在「自定義」供應商顯示（不支援 setRowVisible 時退回啟用切換）。"""
        form = self._conn_form
        if hasattr(form, "setRowVisible"):
            try:
                form.setRowVisible(self.api_base_url_edit, visible)
                return
            except Exception:
                pass
        self.api_base_url_edit.setEnabled(visible)

    def _apply_provider_to_fields(self, provider: str) -> None:
        """把指定供應商的暫存模型／金鑰填入欄位，並依供應商切換模型建議與端點列。"""
        suggestions = _provider_models(provider)
        default_model = suggestions[0] if suggestions else ""
        model = self._provider_models.get(provider) or default_model
        self.api_model_combo.blockSignals(True)
        self.api_model_combo.clear()
        self.api_model_combo.addItems(suggestions)
        self.api_model_combo.setEditText(model)
        self.api_model_combo.blockSignals(False)
        keys = self._provider_keys.get(provider, [])
        self.api_keys_edit.blockSignals(True)
        self.api_keys_edit.setPlainText("\n".join(keys))
        self.api_keys_edit.blockSignals(False)
        self._set_base_url_visible(provider == "custom")

    def _flush_current_provider(self) -> None:
        """把目前欄位的模型／金鑰暫存回目前供應商（切換或儲存前呼叫）。"""
        p = self._cur_provider
        self._provider_models[p] = self.api_model_combo.currentText().strip()
        self._provider_keys[p] = [
            l.strip() for l in self.api_keys_edit.toPlainText().splitlines()
            if l.strip()]

    def _on_provider_changed(self) -> None:
        """供應商下拉改變：先暫存舊供應商欄位，再載入新供應商欄位。"""
        self._flush_current_provider()
        provider = self.api_provider_combo.currentData() or "gemini"
        self._cur_provider = provider
        self._apply_provider_to_fields(provider)

    # ── 與 MainWindow 同步狀態 ──

    def _load_from_main(self) -> None:
        m = self._main
        self.url_edit.setText(getattr(m, "current_url", "") or "")
        self.count_spin.setValue(int(getattr(m, "_auto_translate_count", 5) or 5))
        self.until_last.setChecked(bool(
            getattr(m, "_auto_translate_until_last", False)))
        self.count_spin.setEnabled(not self.until_last.isChecked())
        self.gem_edit.setText(getattr(m, "_gemini_gem_url", "") or "")
        required = (getattr(m, "_gemini_required_model", "pro") or "pro").lower()
        idx = next((i for i, (_, v) in enumerate(_MODEL_OPTIONS) if v == required),
                   0)
        self.model_combo.setCurrentIndex(idx)
        self.max_session_spin.setValue(int(
            getattr(m, "_gemini_max_per_session", 3) or 3))
        # 翻譯方式現於主頁，須在開啟面板時就反映已存後端（不必先開連線設定）
        backend = (getattr(m, "_translate_backend", "browser") or "browser")
        self._set_backend(backend)
        self.out_edit.setText(getattr(m, "_auto_translate_out_dir", "")
                              or getattr(m, "_last_dir", "") or "")
        self._url_list_text = str(
            getattr(m, "_auto_translate_url_list", "") or "")
        self._update_url_list_btn()
        self.skip_existing_cb.setChecked(bool(
            getattr(m, "_auto_translate_skip_existing", False)))
        self.append_mode_cb.setChecked(bool(
            getattr(m, "_auto_translate_append_mode", False)))
        self.mask_words_cb.setChecked(bool(
            getattr(m, "_auto_translate_mask_words", False)))
        self._mask_word_list_text = str(
            getattr(m, "_auto_translate_mask_word_list", "") or "")
        self._update_mask_list_btn()
        self.output_kw_cb.setChecked(bool(
            getattr(m, "_auto_translate_output_kw", False)))
        self._output_kw_rules = [dict(r) for r in (
            getattr(m, "_auto_translate_output_kw_rules", []) or [])]
        self._update_output_kw_btn()
        group_series = bool(getattr(m, "_auto_translate_group_by_series", False))
        self.group_by_series_cb.setChecked(group_series)
        self._set_series_row_enabled(group_series)
        # 「自動填入作品名稱」設定決定檔名欄是可編輯的作品名稱還是唯讀檔名
        self._apply_title_mode(bool(getattr(m, "_fetch_auto_fill_title", False)))
        # 作品名稱：與首頁同步——優先用首頁 doc_title，沒有就空
        try:
            home_title = m._translate_panel.get_doc_title().strip()
        except Exception:
            home_title = ""
        self.doc_title_edit.setText(home_title)
        # 資料夾名不持久化（換作品時記住舊值反而會存錯地方），每次開面板重算。
        # 上面 setText 觸發的延遲重算改為立即執行一次（值沒變時也要算）。
        self._refresh_previews()

    def refresh_from_main(self) -> None:
        """從主視窗目前狀態重整欄位（每次 show_auto_translate_panel 都呼叫）。"""
        self._load_from_main()

    def set_start_url(self, url: str) -> None:
        """把（停止／暫停／中止時）最後一次未完成的網址回填到起始網址欄，方便直接接續。"""
        if not url:
            return
        self.url_edit.setText(url)
        # 網址沒變時 textChanged 不會觸發，但剛跑完一批、輸出資料夾多了檔案，
        # 同名序號可能不同 → 一律重算
        self._schedule_refresh()

    def _load_conn_from_main(self) -> None:
        """把主視窗的連線設定載入浮層欄位（翻譯方式在主頁，見 _load_from_main）。"""
        m = self._main
        self.use_gem_cb.setChecked(bool(getattr(m, "_browser_use_gem", True)))
        self.api_only_prompt_edit.setPlainText(
            getattr(m, "_gemini_api_only_prompt", "") or "")
        self.api_prompt_edit.setPlainText(
            getattr(m, "_gemini_api_system_prompt", "") or "")
        self.api_base_url_edit.setText(getattr(m, "_api_custom_base_url", "") or "")
        self.api_timeout_spin.setValue(
            max(30, int(getattr(m, "_api_timeout", 600) or 600)))
        # 各供應商金鑰／模型載入暫存 dict（切換供應商時互換，儲存時一併寫入）。
        # base_dir 必須與主程式一致（統一為設定資料夾），否則讀不到已存金鑰。
        base_dir = getattr(m, "_settings_base_dir", None) \
            or app_paths.data_dir()
        self._provider_keys = {
            p: list(ks) for p, ks in secure_store.load_all_keys(base_dir).items()}
        models = dict(getattr(m, "_api_models", {}) or {})
        models.setdefault("gemini",
                          getattr(m, "_gemini_api_model", "") or API_MODELS[0])
        self._provider_models = models
        provider = (getattr(m, "_api_provider", "gemini") or "gemini")
        self._cur_provider = provider
        self.api_provider_combo.blockSignals(True)
        pidx = next((i for i, (_, v) in enumerate(_PROVIDER_OPTIONS)
                     if v == provider), 0)
        self.api_provider_combo.setCurrentIndex(pidx)
        self.api_provider_combo.blockSignals(False)
        self._apply_provider_to_fields(provider)
        self._on_backend_changed()
        self._apply_error_policy(getattr(m, "_auto_translate_error_policy", {}) or {})

    def _save_conn_settings(self) -> None:
        self._flush_current_provider()  # 存回目前顯示中的供應商欄位
        provider = self._cur_provider
        provider_keys = {p: ks for p, ks in self._provider_keys.items() if ks}
        # gemini 的模型存回舊欄位（維持配額邏輯不變）；其餘進 api_models
        gemini_model = self._provider_models.get("gemini") or API_MODELS[0]
        api_models = {p: mdl for p, mdl in self._provider_models.items()
                      if p != "gemini" and mdl}
        params = {
            "backend": self._backend,
            "browser_use_gem": self.use_gem_cb.isChecked(),
            "api_provider": provider,
            "api_model": gemini_model,
            "api_models": api_models,
            "api_custom_base_url": self.api_base_url_edit.text().strip(),
            "api_timeout": self.api_timeout_spin.value(),
            "api_only_prompt": self.api_only_prompt_edit.toPlainText(),
            "api_system_prompt": self.api_prompt_edit.toPlainText(),
            "provider_keys": provider_keys,
            # 隨連線設定一併持久化的瀏覽器後端參數（已從主頁移入本浮層）
            "gem_url": self.gem_edit.text().strip(),
            "required_model": self.model_combo.currentData(),
            "max_per_session": self.max_session_spin.value(),
            # 進階設定：只記與預設不同的項目（預設日後調整時，未改過的項目跟著走）
            "error_policy": self._collect_error_policy(),
        }
        self._main.save_connection_settings(params)
        kn = len(provider_keys.get(provider, []))
        label = {v: l for l, v in _PROVIDER_OPTIONS}.get(provider, provider)
        if params["backend"] == "api" and kn == 0:
            self._main.show_status(
                f"⚠️ 已儲存，但「{label}」尚未輸入任何金鑰", "#f39c12")
        else:
            self._main.show_status(
                f"✅ 連線設定已儲存（{label}：{kn} 把金鑰）", "#28a745")

    def collect_params(self) -> dict | None:
        """收集表單參數；任一必填欄位空缺則彈 toast 並回 None。"""
        url = self.url_edit.text().strip()
        gem = self.gem_edit.text().strip()
        out_dir = self.out_edit.text().strip()
        backend = self._backend
        url_list = self._url_list_lines()
        if url_list:
            url = url_list[0]  # 清單模式：第一行即第一話（協調器亦同此規則）
        if not url:
            self._main.show_status("⚠️ 請填入起始網址（或設定手動網址清單）", "#f39c12")
            return None
        # Gem 網址僅瀏覽器模式必填；API 模式不需要 Gem
        if backend == "browser" and not gem:
            self._main.show_status("⚠️ 請填入 Gemini Gem 網址", "#f39c12")
            return None
        if not out_dir:
            self._main.show_status("⚠️ 請選擇輸出資料夾", "#f39c12")
            return None
        group_by_series = self.group_by_series_cb.isChecked()
        # 只帶「目前起始網址」算出的有效名稱；還在讀取或讀取失敗時是空的
        series_folder = self._series_folder_value
        if group_by_series and not series_folder:
            # 名稱留空不擋開始：協調器會在抓到第一話後自行推算（推不出則存回輸出資料夾）
            self._main.show_status(
                "ℹ️ 作品資料夾尚未算出，將依第一話標題自動判斷", "#3498db")
        return {
            "start_url": url,
            "count": self.count_spin.value(),
            "until_last": self.until_last.isChecked(),
            "backend": backend,
            "gem_url": gem,
            "required_model": self.model_combo.currentData(),
            "max_per_session": self.max_session_spin.value(),
            "doc_title": self.doc_title_edit.text().strip(),
            "out_dir": out_dir,
            "skip_existing": self.skip_existing_cb.isChecked(),
            "group_by_series": group_by_series,
            "series_folder": series_folder,
            "append_mode": self.append_mode_cb.isChecked(),
            "mask_words": self.mask_words_cb.isChecked(),
            "output_kw": self.output_kw_cb.isChecked(),
            "url_list": url_list,
        }

    # ── 檔名／作品資料夾（隨欄位變動自動重算） ──

    def _apply_title_mode(self, auto_fill: bool) -> None:
        """檔名欄依「自動填入作品名稱」設定切換，作品資料夾的來源也跟著切換。

        手動模式：可編輯的作品名稱＋右側灰字尾碼；作品資料夾＝作品名稱。
        自動模式：唯讀的完整檔名；作品資料夾＝頁面標題去掉話數。
        """
        self._title_auto = auto_fill
        self.doc_title_edit.setVisible(not auto_fill)
        self.filename_suffix.setVisible(not auto_fill)
        self.filename_preview.setVisible(auto_fill)
        self.series_folder_edit.setPlaceholderText(
            "（依起始網址的頁面標題自動產生）" if auto_fill else "（等於作品名稱）")
        self._previewed_url = ""

    def _set_series_row_enabled(self, enabled: bool) -> None:
        """作品資料夾欄位（含標籤）僅在勾選「依作品名稱建立資料夾」時可用。"""
        self.series_folder_edit.setEnabled(enabled)
        self.series_exists_label.setVisible(enabled)  # 沒勾選時不建資料夾，不必提示
        if self._series_folder_label is not None:
            self._series_folder_label.setEnabled(enabled)

    def _set_series_folder(self, value: str, display: str | None = None) -> None:
        """設定作品資料夾：value 為開始時帶給協調器的名稱，display 為欄位顯示文字。"""
        self._series_folder_value = value
        self.series_folder_edit.setText(value if display is None else display)
        self._update_series_exists()

    def _update_series_exists(self) -> None:
        """依目前資料夾名與輸出資料夾，提示該作品資料夾是否已存在。"""
        name = self._series_folder_value
        out_dir = self.out_edit.text().strip()
        if not name or not out_dir:
            self.series_exists_label.setText("")
            self.series_exists_label.setToolTip("")
        elif os.path.isdir(os.path.join(out_dir, name)):
            self.series_exists_label.setText("📂 已存在，直接放進去")
            self.series_exists_label.setStyleSheet("color:#0d6efd; font-weight:bold;")
            self.series_exists_label.setToolTip(
                f"輸出資料夾底下已有「{name}」資料夾，本批會直接存進去（不另建新的）。")
        else:
            self.series_exists_label.setText("🆕 將新建")
            self.series_exists_label.setStyleSheet("color:#6c757d;")
            self.series_exists_label.setToolTip(
                f"輸出資料夾底下還沒有「{name}」資料夾，開始翻譯時會建立。")

    def _on_doc_title_changed(self, _text: str) -> None:
        """手動模式的作品名稱：資料夾名立即同步；檔名尾碼（同名序號）延遲重算。"""
        if self._title_auto:
            return  # 自動模式不看作品名稱（欄位隱藏，只由首頁同步帶入）
        self._sync_manual_series_folder()
        self._schedule_refresh()

    def _sync_manual_series_folder(self) -> None:
        """手動模式：作品資料夾＝作品名稱（與協調器同一套清理規則），不必讀網址。"""
        import aa_auto_translate as a
        self._set_series_folder(a.compute_series_folder_name(
            doc_title=self.doc_title_edit.text(), fetch_auto_fill_title=False,
            page_title=""))

    def _schedule_refresh(self) -> None:
        """欄位變動 → 延遲重算（連續輸入只算一次）。

        先作廢進行中的重算，並清掉自動模式下舊網址算出的資料夾名：延遲期間
        按開始，才不會把上一個網址的資料夾名帶給協調器。
        """
        self._preview_gen += 1
        if self._title_auto:
            self._series_folder_value = ""
            self._update_series_exists()
        self._refresh_timer.start()

    def _refresh_previews(self) -> None:
        """依目前起始網址算出實際檔名與作品資料夾（背景執行緒）。

        一律允許上網：抓過的網址會進本地快取，之後只改作品名稱／輸出資料夾時
        直接讀快取，不會重抓；正式翻譯第一話也會讀到同一份快取。
        """
        self._refresh_timer.stop()
        self._preview_gen += 1
        gen = self._preview_gen
        auto_fill = self._title_auto
        doc_title = self.doc_title_edit.text().strip()
        lines = self._url_list_lines()
        url = lines[0] if lines else self.url_edit.text().strip()
        if not auto_fill:
            self._sync_manual_series_folder()  # 從自動模式切回來時要補算
        if not url:
            self._previewed_url = ""
            self.filename_suffix.setText("_<話數>.html")
            self.filename_suffix.setToolTip("")
            self.filename_preview.clear()
            self.filename_preview.setPlaceholderText("（填入起始網址後自動顯示）")
            if auto_fill:
                self._set_series_folder("")
            return
        if url != self._previewed_url:
            # 換了網址才顯示讀取中；只改作品名稱／輸出資料夾時讀快取很快，不閃爍
            self.filename_suffix.setText("⏳ 讀取網址中…")
            self.filename_preview.setText("⏳ 讀取網址中…")
            if auto_fill:
                self._set_series_folder("", "⏳ 讀取網址中…")
        out_dir = self.out_edit.text().strip()
        group = self.group_by_series_cb.isChecked()
        manual_folder = self._series_folder_value if not auto_fill else ""
        # 與主程式一致（統一為設定資料夾），讓預覽讀到正確設定
        base_dir = getattr(self._main, "_settings_base_dir", None) \
            or app_paths.data_dir()

        def _bg() -> None:
            import aa_auto_translate as a
            name = folder = None
            prefix = err = ""
            try:
                name = a.preview_first_filename(
                    out_dir, url, base_dir=base_dir, doc_title=doc_title,
                    fetch_auto_fill_title=auto_fill, allow_network=True)
                if name and auto_fill:
                    # 上面已把網頁寫進快取，這裡不會再上網
                    folder = a.preview_series_folder(
                        url, base_dir=base_dir, doc_title=doc_title,
                        fetch_auto_fill_title=True, allow_network=False)
                sub = folder if auto_fill else manual_folder
                if name and group and sub and out_dir:
                    # 依作品名稱建立資料夾時實際存進子資料夾，同名序號要看那裡
                    # （算不出資料夾名時協調器存回輸出資料夾，上面的結果即正確）
                    name = a.preview_first_filename(
                        os.path.join(out_dir, sub), url, base_dir=base_dir,
                        doc_title=doc_title, fetch_auto_fill_title=auto_fill,
                        allow_network=False) or name
                # 手動模式檔名＝{作品名稱清理後}{尾碼}；不給原文就只剩名稱部分
                prefix = a.compute_chapter_name_base(
                    doc_title=doc_title, fetch_auto_fill_title=False,
                    source="", page_title="", fallback_index=1)
            except Exception as e:  # noqa: BLE001 — 預覽失敗只顯示，不影響流程
                err = str(e) or type(e).__name__

            def _apply() -> None:
                if gen != self._preview_gen:
                    return  # 之後又有欄位變動，這份結果已過期
                self._apply_preview(url, auto_fill, name, folder, prefix, err)
            self._main._invoke_on_main.emit(_apply)

        threading.Thread(target=_bg, daemon=True).start()

    def _apply_preview(self, url: str, auto_fill: bool, name: str | None,
                       folder: str | None, prefix: str, err: str) -> None:
        """把背景算出的檔名／資料夾名填回欄位（主執行緒）。"""
        if not name:
            self._previewed_url = ""
            msg = (f"⚠️ 讀取失敗：{err}" if err
                   else "⚠️ 無法讀取此網址（抓取或解析失敗）")
            self.filename_suffix.setText("_<話數>.html  ⚠️ 無法讀取網址")
            self.filename_suffix.setToolTip(msg)
            self.filename_preview.setText(msg)
            if auto_fill:
                self._set_series_folder("", msg)
            return
        self._previewed_url = url
        self.filename_preview.setText(name)
        suffix = name[len(prefix):] if prefix and name.startswith(prefix) else name
        self.filename_suffix.setText(suffix)
        self.filename_suffix.setToolTip(f"實際檔名：{name}")
        if auto_fill:
            if folder:
                self._set_series_folder(folder)
            else:
                self._set_series_folder(
                    "", "⚠️ 無法從標題判斷作品名稱（將直接存進輸出資料夾）")

    # ── Slots ──

    def _browse_out_dir(self) -> None:
        cur = self.out_edit.text().strip() or os.getcwd()
        d = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾", cur)
        if d:
            self.out_edit.setText(d)
            self._persist_out_dir()

    # ── 手動網址清單 ──

    def _url_list_lines(self) -> list[str]:
        """目前清單的有效網址（去空行、去頭尾空白）。"""
        return [ln.strip() for ln in self._url_list_text.splitlines() if ln.strip()]

    def _update_url_list_btn(self) -> None:
        """按鈕文字帶出清單筆數，讓使用者一眼看出清單正在生效。"""
        n = len(self._url_list_lines())
        self.btn_url_list.setText(f"📋 網址清單 ({n})" if n else "📋 網址清單")
        self.url_edit.setEnabled(n == 0)
        self.url_edit.setPlaceholderText(
            "（已改用網址清單，本欄位本次忽略）" if n else "起始話的網址")

    def _open_url_list_dialog(self) -> None:
        """開啟清單編輯對話框；確定後即時寫回主視窗並存檔。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("手動網址清單")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)
        hint = QLabel(
            "一行一個網址，自動翻譯會照這個順序逐話翻譯。" + chr(10) +
            "・第一行就是第一話；上方「起始網址」欄位本次會被忽略" + chr(10) +
            "・「連續話數」仍然有效（只跑前 N 個）；勾「翻譯到最後一話」則跑完整份清單" + chr(10) +
            "・清空內容即恢復原本依關聯記事找下一話的行為")
        hint.setWordWrap(True)
        v.addWidget(hint)
        edit = QPlainTextEdit()
        edit.setPlaceholderText("https://example.com/?p=1" + chr(10) +
                                "https://example.com/?p=2")
        edit.setPlainText(self._url_list_text)
        v.addWidget(edit, 1)
        count_lbl = QLabel("")
        v.addWidget(count_lbl)

        def _update_count() -> None:
            n = len([ln for ln in edit.toPlainText().splitlines() if ln.strip()])
            count_lbl.setText(f"目前 {n} 個網址" if n
                              else "目前沒有網址（＝停用清單，依關聯記事找下一話）")
        edit.textChanged.connect(_update_count)
        _update_count()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        clear_btn = buttons.addButton("清空", QDialogButtonBox.ButtonRole.ResetRole)
        clear_btn.clicked.connect(edit.clear)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._url_list_text = edit.toPlainText().strip()
        self._update_url_list_btn()
        self._persist_url_list()
        self._schedule_refresh()  # 清單第一行＝第一話，檔名／資料夾跟著重算
        n = len(self._url_list_lines())
        self._main.show_status(
            f"✅ 已設定 {n} 個網址的手動清單" if n else "✅ 已清空手動網址清單", "#0f0")

    # ── 過濾詞清單 ──

    def _update_mask_list_btn(self) -> None:
        """按鈕文字帶出詞數，一眼看出清單有沒有內容。"""
        import aa_auto_translate as a
        n = len(a.parse_mask_words(self._mask_word_list_text))
        self.btn_mask_list.setText(f"📝 過濾詞清單 ({n})" if n else "📝 過濾詞清單")

    def _open_mask_list_dialog(self) -> None:
        """開啟過濾詞清單編輯對話框；確定後即時寫回主視窗並存檔。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("過濾詞清單")
        dlg.resize(420, 420)
        v = QVBoxLayout(dlg)
        hint = QLabel(
            "一行一個詞。勾選「替換過濾詞」時，送給 AI 前會把這些詞換成 ○" + chr(10) +
            "（每個字一個 ○，例如「殺す」→「○○」），降低被審查擋下的機率。" + chr(10) +
            "・直接比對文字，不是正則；較長的詞優先替換" + chr(10) +
            "・只影響送給 AI 的文字，存檔的譯文中這些詞會以 ○ 呈現")
        hint.setWordWrap(True)
        v.addWidget(hint)
        edit = QPlainTextEdit()
        edit.setPlainText(self._mask_word_list_text)
        v.addWidget(edit, 1)
        count_lbl = QLabel("")
        v.addWidget(count_lbl)

        def _update_count() -> None:
            n = len({ln.strip() for ln in edit.toPlainText().splitlines() if ln.strip()})
            count_lbl.setText(f"目前 {n} 個詞" if n else "目前沒有詞")
        edit.textChanged.connect(_update_count)
        _update_count()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        clear_btn = buttons.addButton("清空", QDialogButtonBox.ButtonRole.ResetRole)
        clear_btn.clicked.connect(edit.clear)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._mask_word_list_text = edit.toPlainText().strip()
        self._update_mask_list_btn()
        m = self._main
        if getattr(m, "_auto_translate_mask_word_list", "") != self._mask_word_list_text:
            m._auto_translate_mask_word_list = self._mask_word_list_text
            m.save_cache()
        self._main.show_status("✅ 已更新過濾詞清單", "#0f0")

    # ── 譯文關鍵字設定 ──

    def _update_output_kw_btn(self) -> None:
        """按鈕文字帶出關鍵字數。"""
        import aa_auto_translate as a
        n = len(a.parse_output_keyword_rules(self._output_kw_rules))
        self.btn_output_kw.setText(f"📝 關鍵字設定 ({n})" if n else "📝 關鍵字設定")

    def _open_output_kw_dialog(self) -> None:
        """開啟譯文關鍵字設定（表格：關鍵字＋動作）；確定後即時寫回主視窗並存檔。"""
        import aa_auto_translate as a
        actions = list(a.OUTPUT_KEYWORD_ACTIONS.items())  # [(值, 顯示文字), ...]
        action_desc = {
            "pause": "暫停（存檔後等我按繼續）",
            "stop": "停止（不存檔，結束整批）",
            "skip": "跳過（不存檔，續下一話）",
        }
        dlg = QDialog(self)
        dlg.setWindowTitle("譯文關鍵字設定")
        dlg.resize(600, 440)
        v = QVBoxLayout(dlg)
        hint = QLabel(
            "勾選「譯文關鍵字檢查」時，每一話翻譯回來的譯文若出現下列關鍵字，"
            "依設定的動作處理（直接比對文字，不是正則）：" + chr(10) +
            "・暫停：照常存檔後原地暫停，按上方橫幅的「▶ 繼續」才翻下一話" + chr(10) +
            "・停止：這一話不存檔，結束整批（起始網址會回填這一話）" + chr(10) +
            "・跳過：這一話不存檔，列入失敗清單，繼續翻下一話" + chr(10) +
            "同一話命中多種動作時：停止 > 跳過 > 暫停。")
        hint.setWordWrap(True)
        v.addWidget(hint)

        def _make_combo(action: str = "pause") -> QComboBox:
            combo = QComboBox()
            for val, _label in actions:
                combo.addItem(action_desc[val], val)
            idx = combo.findData(action)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            return combo

        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["關鍵字", "動作"])
        # 深色主題（dark_theme.qss）沒有表頭規則，原生表頭會變成淺藍底＋淺色字
        # 看不清 → 這裡明確指定表格與表頭配色（沿用主題的深灰底、淺色字）。
        table.setStyleSheet(
            "QTableWidget { background:#343638; color:#dce4ee;"
            " gridline-color:#555555; }"
            "QHeaderView::section { background:#3c3f41; color:#dce4ee;"
            " font-weight:bold; padding:4px; border:none;"
            " border-right:1px solid #555555; border-bottom:1px solid #555555; }")
        # 動作欄：下拉放在儲存格裡，ResizeToContents 只看文字、不會算進下拉寬度
        # → 用一個樣本下拉的 sizeHint 固定欄寬與列高，選項文字才完整顯示。
        probe = _make_combo()
        probe.ensurePolished()  # 套上主題樣式後再量，寬度才含實際字型與內距
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, probe.sizeHint().width() + 12)
        table.verticalHeader().setDefaultSectionSize(
            max(table.verticalHeader().defaultSectionSize(),
                probe.sizeHint().height() + 6))
        probe.deleteLater()
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        v.addWidget(table, 1)

        def _add_row(word: str = "", action: str = "pause") -> None:
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(word))
            table.setCellWidget(r, 1, _make_combo(action))

        for word, action in a.parse_output_keyword_rules(self._output_kw_rules):
            _add_row(word, action)

        row_btns = QHBoxLayout()
        btn_add = QPushButton("＋ 新增")

        def _on_add() -> None:
            _add_row()
            r = table.rowCount() - 1
            table.setCurrentCell(r, 0)
            table.editItem(table.item(r, 0))
        btn_add.clicked.connect(_on_add)
        row_btns.addWidget(btn_add)
        btn_del = QPushButton("－ 刪除選取")

        def _on_del() -> None:
            rows = {i.row() for i in table.selectedIndexes()}
            if not rows and table.currentRow() >= 0:
                rows = {table.currentRow()}
            for r in sorted(rows, reverse=True):
                table.removeRow(r)
        btn_del.clicked.connect(_on_del)
        row_btns.addWidget(btn_del)
        row_btns.addStretch()
        v.addLayout(row_btns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rules: list[dict] = []
        for r in range(table.rowCount()):
            item = table.item(r, 0)
            word = item.text().strip() if item is not None else ""
            if not word:
                continue
            combo = table.cellWidget(r, 1)
            action = combo.currentData() if combo is not None else "pause"
            rules.append({"word": word, "action": action})
        # 同詞重複時以後面的為準（與協調器 parse_output_keyword_rules 一致）
        self._output_kw_rules = [{"word": w, "action": act}
                                 for w, act in a.parse_output_keyword_rules(rules)]
        self._update_output_kw_btn()
        m = self._main
        if getattr(m, "_auto_translate_output_kw_rules", []) != self._output_kw_rules:
            m._auto_translate_output_kw_rules = [dict(r) for r in self._output_kw_rules]
            m.save_cache()
        self._main.show_status("✅ 已更新譯文關鍵字設定", "#0f0")

    def _persist_url_list(self) -> None:
        """把清單即時寫回主視窗並存檔（比照輸出資料夾，不必等按「開始」）。"""
        m = self._main
        if getattr(m, "_auto_translate_url_list", "") == self._url_list_text:
            return
        m._auto_translate_url_list = self._url_list_text
        m.save_cache()

    def _persist_out_dir(self) -> None:
        """把目前輸出資料夾即時寫回主視窗並存檔，讓選擇不必按「開始」也能持久化。"""
        out_dir = self.out_edit.text().strip()
        m = self._main
        if getattr(m, "_auto_translate_out_dir", "") == out_dir:
            return
        m._auto_translate_out_dir = out_dir
        m.save_cache()

    def _on_start(self) -> None:
        params = self.collect_params()
        if params is None:
            return
        self._main.start_auto_translate_from_panel(params)

    def _on_stop(self) -> None:
        self._main._stop_auto_translate()

    def _clear_log(self) -> None:
        self.log_view.clear()

    # ── 由 MainWindow 主執行緒呼叫 ──

    def append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_running(self, running: bool) -> None:
        self._running = running
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        # 執行中鎖住設定欄位，避免使用者中途改值造成混亂
        for w in (self.url_edit, self.count_spin, self.until_last,
                  self.gem_edit, self.model_combo, self.max_session_spin,
                  self.doc_title_edit, self.out_edit, self.skip_existing_cb,
                  self.btn_url_list, self.group_by_series_cb,
                  self.mask_words_cb, self.btn_mask_list,
                  self.output_kw_cb, self.btn_output_kw,
                  *self._backend_btns.values()):
            w.setEnabled(not running)
        # 作品資料夾欄位：執行中一律鎖；結束後回到「依勾選狀態」
        self._set_series_row_enabled(
            (not running) and self.group_by_series_cb.isChecked())
        # until_last 勾選時保持 count 灰
        if not running:
            self.count_spin.setEnabled(not self.until_last.isChecked())
            # 手動清單生效時，起始網址欄位仍要維持停用（本次不會被使用）
            self._update_url_list_btn()
