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

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)


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

        self.max_session_spin = QSpinBox()
        self.max_session_spin.setRange(1, 99)
        self.max_session_spin.setSuffix(" 次")
        self.max_session_spin.setToolTip(
            "同一對話內最多送幾次給 Gemini，達上限自動開新對話。\n"
            "目的：避免單一對話累積太多上下文使翻譯品質下降。")
        form.addRow("每 N 次送出後換新對話：", self.max_session_spin)

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

    # ── 與 MainWindow 同步狀態 ──

    def _load_from_main(self) -> None:
        m = self._main
        self.url_edit.setText(getattr(m, "current_url", "") or "")
        self.count_spin.setValue(int(getattr(m, "_auto_translate_count", 5) or 5))
        self.until_last.setChecked(bool(
            getattr(m, "_auto_translate_until_last", False)))
        self.count_spin.setEnabled(not self.until_last.isChecked())
        self.gem_edit.setText(getattr(m, "_gemini_gem_url", "") or "")
        self.max_session_spin.setValue(int(
            getattr(m, "_gemini_max_per_session", 3) or 3))
        self.out_edit.setText(getattr(m, "_auto_translate_out_dir", "")
                              or getattr(m, "_last_dir", "") or "")

    def refresh_from_main(self) -> None:
        """從主視窗目前狀態重整欄位（每次 show_auto_translate_panel 都呼叫）。"""
        self._load_from_main()

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
            "max_per_session": self.max_session_spin.value(),
            "out_dir": out_dir,
        }

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
                  self.gem_edit, self.max_session_spin, self.out_edit):
            w.setEnabled(not running)
        # until_last 勾選時保持 count 灰
        if not running:
            self.count_spin.setEnabled(not self.until_last.isChecked())
