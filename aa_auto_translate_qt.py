"""自動翻譯面板（嵌入主視窗 QStackedWidget）。

對應使用者流程：在主畫面工具列按「⚡ 自動翻譯」→ 切換到本面板（index 4）。
面板分上下兩部分：
  上：設定欄位（起始網址、話數＋翻譯到最後一話、Gem 網址、每 N 次換新對話、輸出資料夾）。
  下：執行 Log（即時顯示 :func:`aa_auto_translate.run_auto_translate` 的進度）。

執行緒生命週期、stop_event、橫幅由 ``MainWindow`` 統籌；本面板只負責收集
參數、顯示 Log，並提供 Start / Stop 按鈕，避免狀態散落兩處。
"""
from __future__ import annotations

import os
import threading

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from aa_tool.gemini_api import API_MODELS
from aa_tool import secure_store

# 翻譯後端選項：(顯示文字, 內部值)
_BACKEND_OPTIONS: list[tuple[str, str]] = [
    ("現行（瀏覽器操控網頁 Gemini）", "browser"),
    ("使用 Google API", "api"),
]


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

    # ── UI ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._pages = QStackedWidget()
        root.addWidget(self._pages)

        # ── 主頁（翻譯設定 + Log） ──
        main_page = QWidget()
        main_v = QVBoxLayout(main_page)
        main_v.setContentsMargins(10, 10, 10, 10)
        main_v.setSpacing(8)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        main_v.addWidget(splitter, 1)

        # ── 上半：設定 ──
        top = QWidget()
        form = QFormLayout(top)
        form.setContentsMargins(4, 4, 4, 4)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("起始話的網址")
        form.addRow("起始網址：", self.url_edit)

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

        self.gem_edit = QLineEdit()
        self.gem_edit.setPlaceholderText("https://gemini.google.com/gem/...")
        form.addRow("Gemini Gem 網址：", self.gem_edit)

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

        self.doc_title_edit = QLineEdit()
        self.doc_title_edit.setPlaceholderText("（手動模式必填，作為檔名前綴）")
        self.doc_title_edit.textChanged.connect(self._update_filename_preview)
        form.addRow("作品名稱：", self.doc_title_edit)

        preview_row = QWidget()
        preview_hl = QHBoxLayout(preview_row)
        preview_hl.setContentsMargins(0, 0, 0, 0)
        self.filename_preview = QLineEdit()
        self.filename_preview.setReadOnly(True)
        # 唯讀預覽欄位，刻意挑可辨識的中灰底色與其他欄位（白底）作視覺區隔，
        # 避免讓使用者誤以為可以編輯。
        self.filename_preview.setStyleSheet(
            "QLineEdit { background:#d6d8db; color:#343a40;"
            " border:1px solid #adb5bd; }")
        self.filename_preview.setToolTip(
            "起始網址這一話實際會寫入的檔名。\n"
            "・「自動填入作品名稱」設定開啟時 → 檔名取自頁面標題\n"
            "・關閉時 → {作品名稱}_{偵測到的話數}.html\n"
            "・同名衝突時自動加 -2、-3 等序號\n"
            "按「🔄 試算」會實際讀取網址解析後顯示真正的檔名。")
        btn_preview = QPushButton("🔄 試算")
        btn_preview.setToolTip(
            "讀取起始網址、解析後顯示這一話實際會寫入的檔名（含同名序號）")
        btn_preview.clicked.connect(
            lambda: self._refresh_actual_filename(allow_network=True))
        preview_hl.addWidget(self.filename_preview, 1)
        preview_hl.addWidget(btn_preview)
        form.addRow("檔名預覽：", preview_row)

        out_row = QWidget()
        out_hl = QHBoxLayout(out_row)
        out_hl.setContentsMargins(0, 0, 0, 0)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("輸出 HTML 的資料夾")
        btn_browse = QPushButton("瀏覽…")
        btn_browse.clicked.connect(self._browse_out_dir)
        out_hl.addWidget(self.out_edit, 1)
        out_hl.addWidget(btn_browse)
        form.addRow("輸出資料夾：", out_row)

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
        btn_conn = _btn("⚙ 連線設定", "#6f42c1", "#5a32a3", width=110)
        btn_conn.setToolTip("切換翻譯方式（瀏覽器／API）、API 金鑰與模型")
        btn_conn.clicked.connect(self._show_conn_page)
        btn_hl.addWidget(btn_conn)
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

        self._pages.addWidget(main_page)                # index 0
        self._pages.addWidget(self._build_conn_page())  # index 1

    def _build_conn_page(self) -> QWidget:
        """連線設定分頁：翻譯方式（瀏覽器／API）、API 金鑰、模型、系統指令。"""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        title = QLabel("連線設定")
        title.setFont(_font(15, bold=True))
        v.addWidget(title)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        v.addLayout(form)

        self.backend_combo = QComboBox()
        for label, value in _BACKEND_OPTIONS:
            self.backend_combo.addItem(label, value)
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        form.addRow("翻譯方式：", self.backend_combo)

        self.api_model_combo = QComboBox()
        self.api_model_combo.addItems(API_MODELS)
        form.addRow("API 模型：", self.api_model_combo)

        self.api_keys_edit = QPlainTextEdit()
        self.api_keys_edit.setPlaceholderText(
            "每行一把 Google API 金鑰；多把會輪流送出請求")
        self.api_keys_edit.setFixedHeight(90)
        form.addRow("API 金鑰：", self.api_keys_edit)

        enc_note = QLabel(
            "🔒 金鑰以 Windows DPAPI 加密存於 aa_api_keys.dat（綁定本機帳號、"
            "已列入 .gitignore，不會上傳）" if secure_store.is_real_encryption()
            else "⚠️ 非 Windows：金鑰僅 base64 混淆儲存，安全性較低")
        enc_note.setWordWrap(True)
        enc_note.setStyleSheet("color:#6c757d; font-size:11px;")
        form.addRow("", enc_note)

        self.api_prompt_edit = QPlainTextEdit()
        self.api_prompt_edit.setPlaceholderText(
            "API 模式的系統指令／翻譯人設（建議貼上你 Gem 的翻譯規則）。\n"
            "瀏覽器模式不使用此欄（人設在 Gem 內）。")
        self.api_prompt_edit.setFixedHeight(120)
        form.addRow("系統指令：", self.api_prompt_edit)

        # 動作列
        btn_row = QWidget()
        bh = QHBoxLayout(btn_row)
        bh.setContentsMargins(0, 4, 0, 0)
        btn_save = _btn("💾 儲存連線設定", "#28a745", "#218838", width=140)
        btn_save.clicked.connect(self._save_conn_settings)
        bh.addWidget(btn_save)
        btn_back = _btn("← 返回", "#6c757d", "#5a6268", width=80)
        btn_back.clicked.connect(lambda: self._pages.setCurrentIndex(0))
        bh.addWidget(btn_back)
        bh.addStretch()
        v.addWidget(btn_row)
        v.addStretch()
        return page

    def _show_conn_page(self) -> None:
        self._load_conn_from_main()
        self._pages.setCurrentIndex(1)

    def _on_backend_changed(self, _idx: int = 0) -> None:
        """API 模式才啟用 API 相關欄位。"""
        is_api = self.backend_combo.currentData() == "api"
        for w in (self.api_model_combo, self.api_keys_edit, self.api_prompt_edit):
            w.setEnabled(is_api)

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
        self.out_edit.setText(getattr(m, "_auto_translate_out_dir", "")
                              or getattr(m, "_last_dir", "") or "")
        # 作品名稱：與首頁同步——優先用首頁 doc_title，沒有就空
        try:
            home_title = m._translate_panel.get_doc_title().strip()
        except Exception:
            home_title = ""
        self.doc_title_edit.setText(home_title)
        # 「自動填入作品名稱」設定為 ON 時，鎖住手動填寫並改顯示自動模式提示
        auto_fill = bool(getattr(m, "_fetch_auto_fill_title", False))
        self.doc_title_edit.setEnabled(not auto_fill)
        if auto_fill:
            self.doc_title_edit.setPlaceholderText(
                "（已開啟「自動填入作品名稱」設定 — 由每話 page_title 自動萃取，本欄忽略）")
        else:
            self.doc_title_edit.setPlaceholderText("（手動模式必填，作為檔名前綴）")
        self._update_filename_preview()
        # 面板開啟時，若起始網址已在本地快取就直接試算實際檔名（不上網、不卡 UI）
        self._refresh_actual_filename(allow_network=False)

    def refresh_from_main(self) -> None:
        """從主視窗目前狀態重整欄位（每次 show_auto_translate_panel 都呼叫）。"""
        self._load_from_main()

    def _load_conn_from_main(self) -> None:
        """把主視窗的連線設定載入連線分頁欄位。"""
        m = self._main
        backend = (getattr(m, "_translate_backend", "browser") or "browser")
        bidx = next((i for i, (_, v) in enumerate(_BACKEND_OPTIONS)
                     if v == backend), 0)
        self.backend_combo.setCurrentIndex(bidx)
        model = getattr(m, "_gemini_api_model", "") or API_MODELS[0]
        midx = self.api_model_combo.findText(model)
        self.api_model_combo.setCurrentIndex(midx if midx >= 0 else 0)
        self.api_prompt_edit.setPlainText(
            getattr(m, "_gemini_api_system_prompt", "") or "")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        keys = secure_store.load_keys(base_dir)
        self.api_keys_edit.setPlainText("\n".join(keys))
        self._on_backend_changed()

    def _save_conn_settings(self) -> None:
        keys = [l.strip() for l in
                self.api_keys_edit.toPlainText().splitlines() if l.strip()]
        params = {
            "backend": self.backend_combo.currentData(),
            "api_model": self.api_model_combo.currentText(),
            "api_system_prompt": self.api_prompt_edit.toPlainText(),
            "api_keys": keys,
        }
        self._main.save_connection_settings(params)
        kn = len(keys)
        if params["backend"] == "api" and kn == 0:
            self._main.show_status(
                "⚠️ 已儲存，但 API 模式尚未輸入任何金鑰", "#f39c12")
        else:
            self._main.show_status(
                f"✅ 連線設定已儲存（{kn} 把金鑰）", "#28a745")

    def collect_params(self) -> dict | None:
        """收集表單參數；任一必填欄位空缺則彈 toast 並回 None。"""
        url = self.url_edit.text().strip()
        gem = self.gem_edit.text().strip()
        out_dir = self.out_edit.text().strip()
        if not url:
            self._main.show_status("⚠️ 請填入起始網址", "#f39c12")
            return None
        if not gem:
            self._main.show_status("⚠️ 請填入 Gemini Gem 網址", "#f39c12")
            return None
        if not out_dir:
            self._main.show_status("⚠️ 請選擇輸出資料夾", "#f39c12")
            return None
        return {
            "start_url": url,
            "count": self.count_spin.value(),
            "until_last": self.until_last.isChecked(),
            "gem_url": gem,
            "required_model": self.model_combo.currentData(),
            "max_per_session": self.max_session_spin.value(),
            "doc_title": self.doc_title_edit.text().strip(),
            "out_dir": out_dir,
        }

    def _update_filename_preview(self) -> None:
        """顯示靜態檔名模板（不讀網址）。實際檔名請按「🔄 試算」或開啟面板時自動帶。"""
        auto_fill = bool(getattr(self._main, "_fetch_auto_fill_title", False))
        if auto_fill:
            self.filename_preview.setText(
                "<每話自動取自 page_title>.html  （按 🔄 試算看實際檔名）")
            return
        title = self.doc_title_edit.text().strip() or "未命名"
        import re as _re
        safe = _re.sub(r'[\\/:*?"<>|]', "_", title)[:80].strip() or "未命名"
        self.filename_preview.setText(
            f"{safe}_<話數>.html  （同名時自動加 -2／-3…；按 🔄 試算看實際檔名）")

    def _refresh_actual_filename(self, allow_network: bool) -> None:
        """讀取起始網址、解析後在預覽欄顯示真正會寫入的檔名（背景執行緒）。

        `allow_network=False`：只吃本地快取，沒命中就維持靜態模板（不上網、不卡 UI）；
        面板開啟時用這個。按「🔄 試算」鈕則用 `allow_network=True` 真的上網抓。
        """
        url = self.url_edit.text().strip()
        if not url:
            if allow_network:
                self._main.show_status("⚠️ 請先填入起始網址", "#f39c12")
            return
        out_dir = self.out_edit.text().strip()
        doc_title = self.doc_title_edit.text().strip()
        auto_fill = bool(getattr(self._main, "_fetch_auto_fill_title", False))
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if allow_network:
            self.filename_preview.setText("⏳ 試算中（讀取網址）…")

        def _bg() -> None:
            import aa_auto_translate as a
            try:
                name = a.preview_first_filename(
                    out_dir, url, base_dir=base_dir, doc_title=doc_title,
                    fetch_auto_fill_title=auto_fill,
                    allow_network=allow_network)
            except Exception as e:  # noqa: BLE001 — 預覽失敗只回報，不影響流程
                self._main._invoke_on_main.emit(
                    lambda e=e: self.filename_preview.setText(f"⚠️ 試算失敗：{e}"))
                return

            def _apply(n=name) -> None:
                if n:
                    self.filename_preview.setText(n)
                elif allow_network:
                    self.filename_preview.setText(
                        "⚠️ 無法解析此網址（抓取或解析失敗）")
                # allow_network=False 且沒命中快取 → 保留靜態模板，不覆蓋
            self._main._invoke_on_main.emit(_apply)

        threading.Thread(target=_bg, daemon=True).start()

    # ── Slots ──

    def _browse_out_dir(self) -> None:
        cur = self.out_edit.text().strip() or os.getcwd()
        d = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾", cur)
        if d:
            self.out_edit.setText(d)

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
                  self.doc_title_edit, self.out_edit):
            w.setEnabled(not running)
        # until_last 勾選時保持 count 灰
        if not running:
            self.count_spin.setEnabled(not self.until_last.isChecked())
