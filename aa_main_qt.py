"""AA 漫畫翻譯輔助工具 — PyQt6 主視窗。

架構：
    QMainWindow (MainWindow)
    ├── nav_bar: QWidget（返回導覽列，sub-panel 時顯示）
    ├── QStackedWidget
    │   ├── 0: TranslatePanel（翻譯主面板）
    │   ├── 1: EditWindow（HTML 編輯，embedded from aa_edit_qt.py）
    │   └── 2: BatchSearchWindow（批次搜尋，embedded from aa_batch_search_qt.py）
    └── status_label（最底部狀態列）

Entry point: python aa_main_qt.py
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import math
import os
import re as _re_mod
import subprocess
import sys
import tempfile
import threading
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from aa_tool.constants import (
    DEFAULT_BASE_REGEX, DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX,
    DEFAULT_BG_COLOR, DEFAULT_FG_COLOR,
)
from aa_tool.html_io import read_html_pre_content, write_html_file, read_html_bg_color
from aa_tool.qt_helpers import show_toast
from aa_tool.settings_manager import SettingsManager, AppSettings, AppCache
from aa_tool.text_extraction import (
    extract_text as _extract_text,
    format_extraction_output,
    analyze_extraction as _analyze_extraction,
    validate_ai_text as _validate_ai_text,
    check_chapter_number as _check_chapter_number,
    extract_work_title as _extract_work_title,
)
from aa_tool.translation_engine import (
    parse_glossary,
    apply_translation as _apply_translation,
)
from aa_tool.url_fetcher import fetch_url as _fetch_url, parse_page_html as _parse_page_html
from aa_edit_qt import EditWindow, load_bundled_fonts
from aa_batch_search_qt import BatchSearchWindow


# ── 共用字體 ──
def _apply_dark_title_bar(win: QWidget) -> None:
    """在 Windows 10/11 上將視窗標題列切換為深色 (DWM)。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(win.winId())
        value = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (Win11) / 19 (Win10 舊版)
        for attr in (20, 19):
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            if res == 0:
                break
    except Exception:
        pass


def _ui_font(size=14, bold=False) -> QFont:
    f = QFont("Microsoft JhengHei", size)
    if bold:
        f.setBold(True)
    return f


def _aa_font(size=14) -> QFont:
    return QFont("Meiryo", size)


def _make_btn(text: str, color: str, hover: str, *,
              width: int = 0, fg: str = "white",
              font: QFont | None = None) -> QPushButton:
    btn = QPushButton(text)
    style = (f"QPushButton {{ background:{color}; color:{fg};"
             f" padding:4px 10px; border:none; border-radius:4px; }}"
             f"QPushButton:hover {{ background:{hover}; }}")
    btn.setStyleSheet(style)
    if width:
        btn.setMinimumWidth(width)
    if font:
        btn.setFont(font)
    return btn


# ════════════════════════════════════════════════════════════
#  TranslatePanel
# ════════════════════════════════════════════════════════════

class TranslatePanel(QWidget):
    """翻譯主面板。包含原文、過濾規則、術語表、提取結果、翻譯結果。"""

    def __init__(self, main_win: MainWindow) -> None:
        super().__init__()
        self._main = main_win
        self._glossary_dup_positions: list[tuple[str, int]] = []
        self._glossary_dup_cycle_idx = 0
        self._glossary_tab = "一般"
        self._build_ui()

    # ── UI 建置 ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)

        # ── 工具列 ──
        root.addWidget(self._build_toolbar())

        # ── 主分割區 ──
        # 移除 splitter 分界線，改以顏色分區（參考舊版 UI）
        _splitter_qss = "QSplitter::handle { background: transparent; }"
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.setHandleWidth(0)
        vsplit.setStyleSheet(_splitter_qss)

        # 上半：原文 | 過濾+術語
        top_split = QSplitter(Qt.Orientation.Horizontal)
        top_split.setHandleWidth(0)
        top_split.setStyleSheet(_splitter_qss)
        top_split.addWidget(self._build_source_area())
        top_split.addWidget(self._build_right_area())
        top_split.setStretchFactor(0, 7)
        top_split.setStretchFactor(1, 3)
        top_split.setSizes([840, 360])
        top_split.setChildrenCollapsible(False)
        top_split.handle(1).setEnabled(False)
        vsplit.addWidget(top_split)

        # 提取按鈕列
        extract_row = self._build_extract_row()
        vsplit.addWidget(extract_row)

        # 下半：提取結果 | 翻譯結果
        bot_split = QSplitter(Qt.Orientation.Horizontal)
        bot_split.setHandleWidth(0)
        bot_split.setStyleSheet(_splitter_qss)
        bot_split.addWidget(self._build_extracted_area())
        bot_split.addWidget(self._build_ai_area())
        bot_split.setSizes([600, 600])
        vsplit.addWidget(bot_split)

        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 0)
        vsplit.setStretchFactor(2, 3)

        root.addWidget(vsplit, 1)

    def _build_toolbar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:#343a40;")
        row = QHBoxLayout(w)
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(5)

        title_lbl = QLabel("AA 漫畫翻譯輔助工具")
        title_lbl.setFont(_ui_font(16, bold=True))
        title_lbl.setStyleSheet("color:white;")
        row.addWidget(title_lbl)

        row.addSpacing(12)

        btn_batch = _make_btn("批次搜尋", "#6f42c1", "#5a3299",
                              font=_ui_font(11), width=90)
        btn_batch.clicked.connect(self._main.show_batch_panel)
        row.addWidget(btn_batch)

        btn_resume_edit = _make_btn("編輯模式", "#17a2b8", "#138496",
                                    font=_ui_font(11), width=90)
        btn_resume_edit.setToolTip("回到目前開啟中的編輯畫面（若有）")
        btn_resume_edit.clicked.connect(self._main.resume_edit_panel)
        row.addWidget(btn_resume_edit)

        row.addStretch()

        btn_import = _make_btn("📥 讀取設定", "#17a2b8", "#138496", font=_ui_font(12))
        btn_import.clicked.connect(self._main.import_settings)
        row.addWidget(btn_import)

        btn_export = _make_btn("📤 儲存設定", "#28a745", "#218838", font=_ui_font(12))
        btn_export.clicked.connect(self._main.export_settings)
        row.addWidget(btn_export)

        btn_debug = _make_btn("🔧提取Debug", "#6c757d", "#5a6268", font=_ui_font(12))
        btn_debug.clicked.connect(self._main.analyze_extraction)
        row.addWidget(btn_debug)

        return w

    def _build_source_area(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        # 標頭列
        top = QHBoxLayout()

        lbl = QLabel("1. 原始文本")
        lbl.setFont(_ui_font(13, bold=True))
        top.addWidget(lbl)

        btn_url = _make_btn("🌐 網址讀取", "#6f42c1", "#5a32a3",
                            font=_ui_font(10), width=90)
        btn_url.setFixedHeight(26)
        btn_url.clicked.connect(self._main.open_url_fetch_qt)
        top.addWidget(btn_url)

        self.btn_prev_chapter = _make_btn("◀ 上一話", "#0d6efd", "#0b5ed7",
                                          font=_ui_font(10), width=75)
        self.btn_prev_chapter.setFixedHeight(26)
        self.btn_prev_chapter.clicked.connect(self._main.fetch_prev_chapter)
        top.addWidget(self.btn_prev_chapter)

        self.btn_next_chapter = _make_btn("下一話 ▶", "#0d6efd", "#0b5ed7",
                                          font=_ui_font(10), width=75)
        self.btn_next_chapter.setFixedHeight(26)
        self.btn_next_chapter.clicked.connect(self._main.fetch_next_chapter)
        top.addWidget(self.btn_next_chapter)

        btn_copy_url = _make_btn("📋 複製網址", "#6c757d", "#5a6268",
                                 font=_ui_font(10), width=85)
        btn_copy_url.setFixedHeight(26)
        btn_copy_url.clicked.connect(self._main.copy_current_url)
        top.addWidget(btn_copy_url)

        top.addStretch()

        self.doc_title = QLineEdit()
        self.doc_title.setPlaceholderText("輸入標題 (選填)")
        self.doc_title.setFont(_ui_font(11))
        self.doc_title.setFixedWidth(150)
        self.doc_title.textChanged.connect(self._main.schedule_save)
        top.addWidget(self.doc_title)

        self.btn_work_history = QPushButton("🕘")
        self.btn_work_history.setFixedSize(24, 24)
        self.btn_work_history.setToolTip("作品/作者歷史記錄（最多 10 筆）")
        self.btn_work_history.setStyleSheet(
            "QPushButton { background:#495057; color:white;"
            " border:none; border-radius:3px; padding:0; font-size:12px; }"
            "QPushButton:hover { background:#3d4449; }")
        self.btn_work_history.clicked.connect(self._main.show_work_history_menu)
        top.addWidget(self.btn_work_history)

        self.doc_num = QLineEdit("1")
        self.doc_num.setFont(_ui_font(11))
        self.doc_num.setFixedWidth(50)
        self.doc_num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.doc_num.textChanged.connect(self._main.schedule_save)
        top.addWidget(self.doc_num)

        vl.addLayout(top)

        self.source_text = QTextEdit()
        self.source_text.setFont(_aa_font(14))
        self.source_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.source_text.setStyleSheet("background:#1e1e1e; color:#ddd;")
        self.source_text.textChanged.connect(self._main.schedule_save)
        self.source_text.textChanged.connect(
            lambda: QTimer.singleShot(50, self._main.check_chapter_number))
        vl.addWidget(self.source_text, 1)
        return w

    def _build_right_area(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        # 過濾規則
        lbl_f = QLabel("自訂過濾規則 (每行一條正則):")
        lbl_f.setFont(_ui_font(13, bold=True))
        vl.addWidget(lbl_f)

        self.filter_text = QTextEdit()
        self.filter_text.setFont(_aa_font(13))
        self.filter_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.filter_text.setStyleSheet("background:#3c3836; color:#ddd;")
        self.filter_text.textChanged.connect(self._main.schedule_save)
        vl.addWidget(self.filter_text, 1)

        # 術語表標頭
        ghs = QHBoxLayout()
        lbl_g = QLabel("術語表 (日文=中文):")
        lbl_g.setFont(_ui_font(13, bold=True))
        ghs.addWidget(lbl_g)

        self._dup_label = QLabel("")
        self._dup_label.setFont(_ui_font(11))
        self._dup_label.setStyleSheet("color:#ff4444;")
        ghs.addWidget(self._dup_label)

        self._dup_btn = _make_btn("跳到重複", "#e67e22", "#d35400",
                                  font=_ui_font(10), width=70)
        self._dup_btn.setFixedHeight(22)
        self._dup_btn.clicked.connect(self._jump_to_glossary_dup)
        self._dup_btn.hide()
        ghs.addWidget(self._dup_btn)
        ghs.addStretch()
        vl.addLayout(ghs)

        # 術語表 tab 切換
        tab_row = QHBoxLayout()
        self._btn_tab_general = _make_btn("一般", "#2a3b4c", "#3a4b5c",
                                          font=_ui_font(11), width=50)
        self._btn_tab_general.setFixedHeight(22)
        self._btn_tab_general.clicked.connect(lambda: self._switch_glossary_tab("一般"))
        tab_row.addWidget(self._btn_tab_general)

        self._btn_tab_temp = _make_btn("臨時", "#555555", "#4b2a2a",
                                       font=_ui_font(11), width=50)
        self._btn_tab_temp.setFixedHeight(22)
        self._btn_tab_temp.clicked.connect(lambda: self._switch_glossary_tab("臨時"))
        tab_row.addWidget(self._btn_tab_temp)
        tab_row.addStretch()
        vl.addLayout(tab_row)

        # 術語 QStackedWidget
        self._gloss_stack = QStackedWidget()
        self.glossary_text = QTextEdit()
        self.glossary_text.setFont(_aa_font(13))
        self.glossary_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.glossary_text.setStyleSheet("background:#2a3b4c; color:#ddd;")
        self.glossary_text.textChanged.connect(self._main.schedule_save)
        self.glossary_text.textChanged.connect(
            lambda: QTimer.singleShot(100, self._check_glossary_duplicates))

        self.glossary_text_temp = QTextEdit()
        self.glossary_text_temp.setFont(_aa_font(13))
        self.glossary_text_temp.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.glossary_text_temp.setStyleSheet("background:#3b2a2a; color:#ddd;")
        self.glossary_text_temp.textChanged.connect(self._main.schedule_save)
        self.glossary_text_temp.textChanged.connect(
            lambda: QTimer.singleShot(100, self._check_glossary_duplicates))

        self._gloss_stack.addWidget(self.glossary_text)
        self._gloss_stack.addWidget(self.glossary_text_temp)
        vl.addWidget(self._gloss_stack, 1)

        return w

    def _build_extract_row(self) -> QWidget:
        w = QWidget()
        w.setMaximumHeight(48)
        hl = QHBoxLayout(w)
        hl.setContentsMargins(4, 2, 4, 2)

        btn_ext = _make_btn("⬇️  提取日文  ⬇️", "#007bff", "#0056b3",
                             font=_ui_font(13, bold=True), width=250)
        btn_ext.setFixedHeight(40)
        btn_ext.clicked.connect(self._main.extract_text)
        hl.addWidget(btn_ext)

        self.auto_copy_cb = QCheckBox("自動複製")
        self.auto_copy_cb.setFont(_ui_font(12))
        hl.addWidget(self.auto_copy_cb)

        hl.addStretch()
        return w

    def _build_extracted_area(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        top = QHBoxLayout()
        lbl = QLabel("2. 提取結果:")
        lbl.setFont(_ui_font(13, bold=True))
        top.addWidget(lbl)

        self.ext_count_label = QLabel("")
        self.ext_count_label.setFont(_ui_font(13))
        self.ext_count_label.setStyleSheet("color:#17a2b8;")
        top.addWidget(self.ext_count_label)
        top.addStretch()

        for label, half in [("複製全部", "all"), ("複製上半", "top"), ("複製下半", "bottom")]:
            btn = _make_btn(label, "#495057", "#3d4449", font=_ui_font(10), width=70)
            btn.setFixedHeight(24)
            btn.clicked.connect(lambda checked=False, h=half: self._main.copy_split(h))
            top.addWidget(btn)
        vl.addLayout(top)

        self.extracted_text = QTextEdit()
        self.extracted_text.setFont(_aa_font(13))
        self.extracted_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.extracted_text.setStyleSheet("background:#1e1e1e; color:#ddd;")
        vl.addWidget(self.extracted_text, 1)
        return w

    def _build_ai_area(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        top = QHBoxLayout()
        lbl = QLabel("3. 填入翻譯:")
        lbl.setFont(_ui_font(13, bold=True))
        top.addWidget(lbl)

        self._ai_warn_label = QLabel("")
        self._ai_warn_label.setFont(_ui_font(11))
        self._ai_warn_label.setStyleSheet("color:#ff4444;")
        top.addWidget(self._ai_warn_label)
        top.addStretch()
        vl.addLayout(top)

        self.ai_text = QTextEdit()
        self.ai_text.setFont(_aa_font(13))
        self.ai_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.ai_text.setAcceptRichText(False)
        self.ai_text.setStyleSheet(
            "QTextEdit { background:#1e1e1e; color:#ddd; }")
        _pal = self.ai_text.palette()
        _pal.setColor(QPalette.ColorRole.Text, QColor("#ddd"))
        _pal.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
        self.ai_text.setPalette(_pal)
        self.ai_text.setTextColor(QColor("#ddd"))
        self.ai_text.textChanged.connect(
            lambda: QTimer.singleShot(100, self._main.validate_ai_text))
        vl.addWidget(self.ai_text, 1)
        return w

    # ── 術語表 tab 切換 ──

    def _switch_glossary_tab(self, tab: str) -> None:
        self._glossary_tab = tab
        if tab == "一般":
            self._gloss_stack.setCurrentIndex(0)
            self._btn_tab_general.setStyleSheet(
                "QPushButton { background:#2a3b4c; color:white; padding:2px 8px;"
                " border:none; border-radius:3px; }"
                "QPushButton:hover { background:#3a4b5c; }")
            self._btn_tab_temp.setStyleSheet(
                "QPushButton { background:#555; color:white; padding:2px 8px;"
                " border:none; border-radius:3px; }"
                "QPushButton:hover { background:#666; }")
        else:
            self._gloss_stack.setCurrentIndex(1)
            self._btn_tab_general.setStyleSheet(
                "QPushButton { background:#555; color:white; padding:2px 8px;"
                " border:none; border-radius:3px; }"
                "QPushButton:hover { background:#666; }")
            self._btn_tab_temp.setStyleSheet(
                "QPushButton { background:#3b2a2a; color:white; padding:2px 8px;"
                " border:none; border-radius:3px; }"
                "QPushButton:hover { background:#4b2a2a; }")

    # ── 重複術語偵測 ──

    def _check_glossary_duplicates(self) -> None:
        g1_lines = self.glossary_text.toPlainText().strip().split('\n')
        g2_lines = self.glossary_text_temp.toPlainText().strip().split('\n')
        key_positions: dict[str, list[tuple[str, int]]] = {}
        for i, line in enumerate(g1_lines):
            if '=' in line:
                key = line.split('=', 1)[0].strip()
                if key:
                    key_positions.setdefault(key, []).append(("一般", i))
        for i, line in enumerate(g2_lines):
            if '=' in line:
                key = line.split('=', 1)[0].strip()
                if key:
                    key_positions.setdefault(key, []).append(("臨時", i))

        dup_pos: list[tuple[str, int]] = []
        dup_keys: list[str] = []
        for key, positions in key_positions.items():
            if len(positions) >= 2:
                dup_keys.append(key)
                dup_pos.extend(positions)

        self._glossary_dup_positions = dup_pos
        self._glossary_dup_cycle_idx = 0
        if dup_keys:
            self._dup_label.setText("⚠ 術語有重複")
            self._dup_btn.show()
        else:
            self._dup_label.setText("")
            self._dup_btn.hide()

    def _jump_to_glossary_dup(self) -> None:
        if not self._glossary_dup_positions:
            return
        tab_name, line_idx = self._glossary_dup_positions[self._glossary_dup_cycle_idx]
        self._glossary_dup_cycle_idx = (
            self._glossary_dup_cycle_idx + 1) % len(self._glossary_dup_positions)
        self._switch_glossary_tab(tab_name)
        widget = self.glossary_text if tab_name == "一般" else self.glossary_text_temp
        doc = widget.document()
        block = doc.findBlockByLineNumber(line_idx)
        if block.isValid():
            from PyQt6.QtGui import QTextCursor
            cursor = widget.textCursor()
            cursor.setPosition(block.position())
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor)
            widget.setTextCursor(cursor)
            widget.ensureCursorVisible()

    # ── Getters ──

    def get_source_text(self) -> str:
        return self.source_text.toPlainText()

    def get_filter_text(self) -> str:
        return self.filter_text.toPlainText()

    def get_glossary_text(self) -> str:
        return self.glossary_text.toPlainText()

    def get_glossary_temp_text(self) -> str:
        return self.glossary_text_temp.toPlainText()

    def get_extracted_text(self) -> str:
        return self.extracted_text.toPlainText()

    def get_ai_text(self) -> str:
        return self.ai_text.toPlainText()

    def get_doc_title(self) -> str:
        return self.doc_title.text()

    def get_doc_num(self) -> str:
        return self.doc_num.text()

    def get_combined_glossary(self) -> str:
        g1 = self.get_glossary_text().strip()
        g2 = self.get_glossary_temp_text().strip()
        return '\n'.join(p for p in [g1, g2] if p)


# ════════════════════════════════════════════════════════════
#  MainWindow
# ════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    """PyQt6 主視窗。QStackedWidget 切換三個面板。"""

    # 背景執行緒 → 主執行緒 callable 轉送（QTimer.singleShot 不是 thread-safe）
    _invoke_on_main = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._invoke_on_main.connect(lambda fn: fn())
        self.setWindowTitle("AA 漫畫翻譯輔助工具")
        self.resize(1400, 900)
        self._dark_title_applied = False

        # ── 設定管理 ──
        self.settings_mgr = SettingsManager(
            os.path.dirname(os.path.abspath(__file__)))
        self.current_base_regex = DEFAULT_BASE_REGEX
        self.current_invalid_regex = DEFAULT_INVALID_REGEX
        self.current_symbol_regex = DEFAULT_SYMBOL_REGEX

        # ── 應用狀態 ──
        self.url_history: list[dict] = []
        self.url_related_links: list[dict] = []
        self.current_url: str = ""
        self._author_only: bool = False
        self._author_name: str = ""
        self._batch_folder: str = ""
        self.work_history: list[dict] = []
        self._editor_font_family: str = "submona"
        self._editor_font_size: int = 12
        self._last_dir: str = ""
        self._editor_bg_color: str = "#ffffff"
        self._save_timer: QTimer | None = None
        self._saved_glossary_lines = 0
        self._saved_glossary_temp_lines = 0
        self._saved_filter_lines = 0

        # ── 中央 Widget ──
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 導覽列（sub-panel 時顯示） ──
        self._nav_bar = self._build_nav_bar()
        root.addWidget(self._nav_bar)
        self._nav_bar.hide()

        # ── QStackedWidget ──
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # 提示訊息改以右上角浮動 toast 顯示（見 show_status）

        # ── 建立面板 ──
        self._translate_panel = TranslatePanel(self)
        self.stack.addWidget(self._translate_panel)   # index 0

        # 編輯面板（lazy init）
        self._edit_window: EditWindow | None = None
        self._edit_placeholder = QWidget()
        self.stack.addWidget(self._edit_placeholder)  # index 1

        # 批次搜尋面板（lazy init）
        self._batch_window: BatchSearchWindow | None = None
        self._batch_placeholder = QWidget()
        self.stack.addWidget(self._batch_placeholder)  # index 2

        # ── 底部動作列 ──
        self._action_bar = self._build_action_bar()
        root.addWidget(self._action_bar)

        # ── 快捷鍵 ──
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.apply_translation)

        # ── 載入設定 / 暫存 ──
        self._load_initial_state()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._dark_title_applied:
            _apply_dark_title_bar(self)
            self._dark_title_applied = True

    def _build_nav_bar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:#495057;")
        hl = QHBoxLayout(w)
        hl.setContentsMargins(10, 4, 10, 4)
        hl.setSpacing(8)

        btn_back = _make_btn("← 返回翻譯", "#6c757d", "#5a6268",
                             font=_ui_font(12), width=110)
        btn_back.setFixedHeight(28)
        btn_back.clicked.connect(self.show_translate_panel)
        hl.addWidget(btn_back)

        self._nav_label = QLabel("")
        self._nav_label.setFont(_ui_font(12))
        self._nav_label.setStyleSheet("color:white;")
        hl.addWidget(self._nav_label)

        hl.addStretch()
        return w

    def _build_action_bar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:#2b2b2b;")
        hl = QHBoxLayout(w)
        hl.setContentsMargins(10, 5, 10, 5)
        hl.setSpacing(6)

        btn_apply = _make_btn("🚀  替換翻譯並編輯  🚀", "#ff9800", "#e68a00",
                              font=_ui_font(13, bold=True))
        btn_apply.setFixedHeight(44)
        btn_apply.clicked.connect(self.apply_translation)
        hl.addWidget(btn_apply, 1)

        btn_cache = _make_btn("📥 讀入暫存", "#17a2b8", "#138496",
                              font=_ui_font(12), width=120)
        btn_cache.setFixedHeight(44)
        btn_cache.clicked.connect(self._manual_load_cache)
        hl.addWidget(btn_cache)

        btn_open = _make_btn("📂 打開已儲存的 HTML", "#6f42c1", "#5a32a3",
                             font=_ui_font(12), width=220)
        btn_open.setFixedHeight(44)
        btn_open.clicked.connect(self.import_html)
        hl.addWidget(btn_open)

        return w

    # ════════════════════════════════════════════════════════════
    #  面板切換
    # ════════════════════════════════════════════════════════════

    def show_translate_panel(self) -> None:
        self.stack.setCurrentIndex(0)
        self._nav_bar.hide()
        self._action_bar.show()
        self._update_work_title("")
        self._translate_panel.source_text.setFocus()

    def show_edit_panel(self, file_path: str, scroll_to_line: int = 0,
                        original_text: str | None = None,
                        display_title: str = "",
                        is_temp_file: bool = False) -> None:
        """載入 HTML 至 EditWindow 並切換到編輯面板。"""
        # 第一次建立 EditWindow
        if self._edit_window is None:
            self._edit_window = EditWindow(
                file_path,
                scroll_to_line=scroll_to_line,
                original_text=original_text,
                display_title=display_title,
                is_temp_file=is_temp_file,
                glossary_provider=self._translate_panel.get_combined_glossary,
                glossary_saver=self._save_glossary_entry,
                on_back=self.show_translate_panel,
                on_open=self.import_html,
                on_save=self._on_edit_saved,
                on_font_change=self._on_editor_font_changed,
                init_font_family=self._editor_font_family,
                init_font_size=self._editor_font_size,
                get_last_dir=lambda: self._last_dir,
                on_dir_change=self._on_last_dir_changed,
                on_bg_change=self._on_editor_bg_changed,
                init_bg=self._editor_bg_color,
            )
            # 替換 placeholder
            self.stack.removeWidget(self._edit_placeholder)
            self.stack.insertWidget(1, self._edit_window)
        else:
            # 重新載入檔案
            try:
                text = read_html_pre_content(file_path) or ""
            except OSError:
                text = ""
            self._edit_window._html_file = file_path
            self._edit_window._display_title = display_title
            self._edit_window._is_temp_file = is_temp_file
            # 保留使用者記住的底色；只有首次沒有記錄時才從 HTML 讀取
            if not self._editor_bg_color or self._editor_bg_color == "#ffffff":
                bg = read_html_bg_color(file_path) or "#ffffff"
                self._edit_window._bg_color = bg
            else:
                self._edit_window._bg_color = self._editor_bg_color
            self._edit_window._dirty = False
            self._edit_window._replace_document(text)
            self._edit_window._apply_editor_colors()
            header = display_title or os.path.basename(file_path)
            self._edit_window.setWindowTitle(
                f"AA 編輯器 (PyQt6) — {header}")
            self._edit_window._on_back = self.show_translate_panel
            self._edit_window._on_open = self.import_html
            self._edit_window._on_save = self._on_edit_saved
            if scroll_to_line:
                self._edit_window._scroll_to_line(scroll_to_line)
            # 更新比對原文
            if original_text is not None:
                self._edit_window._original_text = original_text
                if original_text:
                    self._edit_window.orig_view.setPlainText(original_text)
                    self._edit_window._apply_line_height_to(
                        self._edit_window.orig_view)
            # 若還在比對模式，切回編輯
            if self._edit_window._compare_active:
                self._edit_window._toggle_compare()

        nav_name = display_title or os.path.basename(file_path)
        self._nav_label.setText(f"編輯：{nav_name}")
        self._update_work_title(f"編輯 — {nav_name}")
        self.stack.setCurrentIndex(1)
        self._nav_bar.hide()
        self._action_bar.hide()

    def resume_edit_panel(self) -> None:
        """回到目前開啟中的編輯畫面（若有）。"""
        if self._edit_window is None:
            self.show_status("⚠️ 目前沒有可恢復的編輯畫面", "#f39c12")
            return
        self.stack.setCurrentIndex(1)
        self._nav_bar.hide()
        self._action_bar.hide()

    def show_batch_panel(self) -> None:
        """切換到批次搜尋面板。"""
        if self._batch_window is None:
            self._batch_window = BatchSearchWindow(
                folder=self._batch_folder,
                on_open_file=self._on_batch_open_file,
                on_folder_change=self._on_batch_folder_change,
            )
            self.stack.removeWidget(self._batch_placeholder)
            self.stack.insertWidget(2, self._batch_window)

        self._nav_label.setText("批次搜尋")
        self._update_work_title("批次搜尋")
        self.stack.setCurrentIndex(2)
        self._nav_bar.show()
        self._action_bar.hide()

    def _on_batch_open_file(self, file_path: str, line: int, folder: str) -> None:
        self._batch_folder = folder
        self.show_edit_panel(file_path, scroll_to_line=line)

    def _on_batch_folder_change(self, folder: str) -> None:
        self._batch_folder = folder
        self.schedule_save()

    # ════════════════════════════════════════════════════════════
    #  術語存入
    # ════════════════════════════════════════════════════════════

    def _save_glossary_entry(self, original: str, translation: str) -> None:
        """由 EditWindow callback 呼叫，將術語存入一般術語表。"""
        if not original or not translation:
            return
        g_text = self._translate_panel.get_glossary_text().rstrip('\n')
        if g_text:
            g_text += '\n'
        g_text += f"{original}={translation}"
        self._translate_panel.glossary_text.setPlainText(g_text)
        self.schedule_save()
        self.show_status(f"📖 已存入術語：{original} → {translation}", "#17a2b8")

    # ════════════════════════════════════════════════════════════
    #  提取 / 翻譯
    # ════════════════════════════════════════════════════════════

    def extract_text(self) -> None:
        source = self._translate_panel.get_source_text()
        if not source.strip():
            self.show_status("⚠️ 請先貼上原始文本！", "#f39c12")
            return
        self.save_cache()
        extracted_set = _extract_text(
            source,
            self.current_base_regex,
            self.current_invalid_regex,
            self.current_symbol_regex,
            self._translate_panel.get_filter_text().strip(),
        )
        output = format_extraction_output(extracted_set)
        self._translate_panel.extracted_text.setPlainText(output)
        self._translate_panel.ext_count_label.setText(
            f"(共提取 {len(extracted_set)} 行)")
        if self._translate_panel.auto_copy_cb.isChecked():
            QApplication.clipboard().setText(output.strip())
            self.show_status(f"✅ 已提取 {len(extracted_set)} 行並複製到剪貼簿", "#0f0")

    def copy_split(self, half: str) -> None:
        ext_text = self._translate_panel.get_extracted_text().strip()
        if not ext_text:
            return
        lines = [l for l in ext_text.split('\n') if l.strip()]
        if not lines:
            return
        if half == 'all':
            text = '\n'.join(lines)
        else:
            split_idx = int(math.ceil(len(lines) / 2))
            text = '\n'.join(lines[:split_idx] if half == 'top'
                             else lines[split_idx:])
        QApplication.clipboard().setText(text)

    def validate_ai_text(self) -> None:
        ai_content = self._translate_panel.get_ai_text().strip()
        lbl = self._translate_panel._ai_warn_label
        if not ai_content:
            lbl.setText("")
            return
        warnings = _validate_ai_text(ai_content)
        if warnings:
            lbl.setText("  ".join(warnings))
            lbl.setStyleSheet("color:#ff4444;")
        else:
            lbl.setText("✅ 格式正確")
            lbl.setStyleSheet("color:#28a745;")
            QTimer.singleShot(3000, lambda: lbl.setText(""))

    def check_chapter_number(self) -> None:
        text = self._translate_panel.source_text.toPlainText()[:200]
        result = _check_chapter_number(text)
        if result is not None:
            self._translate_panel.doc_num.setText(str(result))

    def apply_translation(self) -> None:
        if self.stack.currentIndex() != 0:
            return
        source = self._translate_panel.get_source_text()
        extracted = self._translate_panel.get_extracted_text()
        translated = self._translate_panel.get_ai_text()
        if not source.strip() or not extracted.strip() or not translated.strip():
            self.show_status(
                "⚠️ 請確保原始文本、提取結果和翻譯結果都有內容！", "#f39c12")
            return
        self.save_cache()
        # 記錄作品+作者歷史（只有按下此按鈕才記）
        self._record_work_history()
        glossary = parse_glossary(self._translate_panel.get_combined_glossary())
        result_text = _apply_translation(source, extracted, translated, glossary)
        # 以標題+話數命名暫存檔，讓視窗標題與儲存預設名都能正確顯示
        title = self._translate_panel.get_doc_title().strip() or "未命名"
        num = self._translate_panel.get_doc_num().strip()
        safe_title = _re_mod.sub(r'[\\/:*?"<>|]', '_', title)
        safe_num = _re_mod.sub(r'[\\/:*?"<>|]', '_', num)
        name_base = f"{safe_title}_{safe_num}" if safe_num else safe_title
        display_title = f"{title}_{num}" if num else title
        tmp = os.path.join(tempfile.gettempdir(), f"{name_base}.html")
        try:
            write_html_file(tmp, result_text)
        except OSError as e:
            self.show_status(f"❌ 寫入暫存失敗: {e}", "#dc3545")
            return
        self.show_edit_panel(
            tmp,
            original_text=source.rstrip('\n'),
            display_title=display_title,
            is_temp_file=True,
        )

    def import_html(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "選取已儲存的 HTML 檔案",
            self._last_dir,
            "HTML files (*.html);;All files (*.*)")
        if not file_path:
            return
        try:
            extracted = read_html_pre_content(file_path)
            if extracted is None:
                self.show_status(
                    "⚠️ 無法找到標準的 <pre> 標籤，讀取可能不完整。", "#f39c12")
                with open(file_path, 'r', encoding='utf-8') as f:
                    extracted = _html.unescape(f.read())
        except OSError as e:
            self.show_status(f"❌ 讀取 HTML 失敗！{e}", "#dc3545")
            return
        # 若暫存原文中有此檔名，載入作為比對原文
        self._last_dir = os.path.dirname(file_path)
        self.schedule_save()
        cached_original = self.load_original_for_file(file_path)
        self.show_edit_panel(
            file_path,
            original_text=cached_original,
            display_title=os.path.splitext(os.path.basename(file_path))[0],
            is_temp_file=False,
        )

    def analyze_extraction(self) -> None:
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        cursor = self._translate_panel.source_text.textCursor()
        sel = cursor.selectedText().replace('\u2029', '\n')
        if not sel.strip():
            self.show_status(
                "⚠️ 請先在原始文本區塊反白選取要分析的文字！", "#f39c12")
            return
        report = _analyze_extraction(
            sel,
            self.current_base_regex,
            self.current_invalid_regex,
            self.current_symbol_regex,
            self._translate_panel.get_filter_text().strip(),
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("🔧 提取分析 (Debug)")
        dlg.resize(800, 600)
        vl = QVBoxLayout(dlg)
        box = QTextEdit()
        box.setFont(_aa_font(13))
        box.setPlainText(report)
        box.setReadOnly(True)
        vl.addWidget(box)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        vl.addWidget(bb)
        dlg.exec()

    # ════════════════════════════════════════════════════════════
    #  Utils
    # ════════════════════════════════════════════════════════════

    def inc_num(self) -> None:
        try:
            val = int(self._translate_panel.doc_num.text() or "0")
            self._translate_panel.doc_num.setText(str(val + 1))
            self.schedule_save()
        except ValueError:
            pass

    def dec_num(self) -> None:
        try:
            val = int(self._translate_panel.doc_num.text() or "0")
            if val > 0:
                self._translate_panel.doc_num.setText(str(val - 1))
                self.schedule_save()
        except ValueError:
            pass

    def copy_current_url(self) -> None:
        if self.current_url:
            QApplication.clipboard().setText(self.current_url)
            self.show_status("✅ 已複製網址到剪貼簿", "#0f0")
        else:
            self.show_status("⚠️ 尚未讀取過網址！", "#f39c12")

    def _update_work_title(self, work_title: str = "") -> None:
        base = "AA 漫畫翻譯輔助工具"
        self.setWindowTitle(f"{base} — {work_title}" if work_title else base)

    # ════════════════════════════════════════════════════════════
    #  Toast / Status
    # ════════════════════════════════════════════════════════════

    # 將常見「亮綠」色對應到 toast 用的深綠底
    _STATUS_COLOR_MAP = {
        "#0f0": "#28a745",
        "#00ff00": "#28a745",
    }

    def show_status(self, message: str, color: str = "#28a745",
                    duration: int = 3000) -> None:
        bg = self._STATUS_COLOR_MAP.get(color.lower(), color)
        # duration <= 0 視為使用預設，避免 QTimer 立刻刪除 toast
        dur = duration if duration > 0 else 3000
        show_toast(self, message, color=bg, duration=dur)

    # ════════════════════════════════════════════════════════════
    #  URL 抓取（subprocess + IPC，與 customtkinter 版邏輯相同）
    # ════════════════════════════════════════════════════════════

    def open_url_fetch_qt(self) -> None:
        cmd_file = os.path.join(tempfile.gettempdir(), "aa_url_fetch_cmd.json")
        reverse_cmd_file = os.path.join(
            tempfile.gettempdir(), "aa_url_fetch_reverse_cmd.json")
        init_file = os.path.join(
            tempfile.gettempdir(), "aa_url_fetch_init.json")
        for f in (cmd_file, reverse_cmd_file, init_file):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        init_data = {
            "url_history": self.url_history,
            "url_related_links": self.url_related_links,
            "current_url": self.current_url,
            "author_only": self._author_only,
            "author_name": self._author_name,
            "initial_url": self.current_url,
        }
        try:
            with open(init_file, "w", encoding="utf-8") as f:
                json.dump(init_data, f, ensure_ascii=False)
        except OSError:
            pass

        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "aa_url_fetch_qt.py")
        args = [sys.executable, script,
                "--cmd-file", cmd_file,
                "--reverse-cmd-file", reverse_cmd_file,
                "--init-file", init_file]
        self._url_fetch_qt_process = subprocess.Popen(args)
        self._url_fetch_cmd_file = cmd_file
        self._url_fetch_reverse_cmd_file = reverse_cmd_file

        self._url_poll_timer = QTimer(self)
        self._url_poll_timer.setInterval(500)
        self._url_poll_timer.timeout.connect(self._poll_url_fetch_commands)
        self._url_poll_timer.start()

    def _poll_url_fetch_commands(self) -> None:
        proc = getattr(self, '_url_fetch_qt_process', None)
        if proc and proc.poll() is not None:
            self._url_fetch_qt_process = None
            self._url_poll_timer.stop()
            return
        cmd_file = getattr(self, '_url_fetch_cmd_file', '')
        if not cmd_file or not os.path.exists(cmd_file):
            return
        try:
            with open(cmd_file, 'r', encoding='utf-8') as f:
                cmd = json.load(f)
            os.remove(cmd_file)
        except (json.JSONDecodeError, OSError):
            return
        action = cmd.get('action')
        if action == 'fetch_request':
            # author_name 由 Qt 視窗直接傳入，同步到 MainWindow 狀態
            new_author = cmd.get('author_name')
            if new_author is not None:
                self._author_name = str(new_author)
            self._handle_url_fetch_request(
                cmd.get('url', ''),
                bool(cmd.get('author_only', False)),
                skip_cache=bool(cmd.get('skip_cache', False)),
            )
        elif action == 'clear_history':
            self.url_history = []
            self.schedule_save()
            self._write_url_fetch_reverse({'action': 'history_cleared',
                                           'url_history': []})
        elif action == 'close_sync':
            self._author_only = bool(cmd.get('author_only', False))
            if 'author_name' in cmd:
                self._author_name = str(cmd['author_name'])
            self.schedule_save()

    def _write_url_fetch_reverse(self, cmd: dict, retries: int = 20) -> None:
        rev = getattr(self, '_url_fetch_reverse_cmd_file', '')
        if not rev:
            return
        if os.path.exists(rev):
            if retries > 0:
                QTimer.singleShot(
                    100, lambda: self._write_url_fetch_reverse(cmd, retries - 1))
            return
        try:
            with open(rev, 'w', encoding='utf-8') as f:
                json.dump(cmd, f, ensure_ascii=False)
        except OSError:
            pass

    def _url_cache_dir(self) -> str:
        d = os.path.join(tempfile.gettempdir(), "aa_url_cache")
        os.makedirs(d, exist_ok=True)
        return d

    def _url_cache_path(self, url: str) -> str:
        h = hashlib.md5(url.encode('utf-8')).hexdigest()
        return os.path.join(self._url_cache_dir(), f"{h}.html")

    def _read_url_cache(self, url: str) -> str | None:
        path = self._url_cache_path(url)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except OSError:
            return None

    def _write_url_cache(self, url: str, page_html: str) -> None:
        try:
            with open(self._url_cache_path(url), 'w', encoding='utf-8') as f:
                f.write(page_html)
        except OSError:
            return
        try:
            valid = {self._url_cache_path(h['url'])
                     for h in self.url_history if h.get('url')}
            valid.add(self._url_cache_path(url))
            for fname in os.listdir(self._url_cache_dir()):
                fpath = os.path.join(self._url_cache_dir(), fname)
                if fpath not in valid:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
        except OSError:
            pass

    def _handle_url_fetch_request(self, raw_url: str, author_only: bool,
                                   skip_cache: bool = False) -> None:
        self._author_only = author_only
        self.schedule_save()
        author = self._author_name

        def _bg() -> None:
            try:
                page_html = None
                if not skip_cache:
                    page_html = self._read_url_cache(raw_url)
                if page_html is None:
                    page_html = _fetch_url(raw_url)
                    self._write_url_cache(raw_url, page_html)
                text_content, nav_links, page_title = _parse_page_html(
                    page_html, raw_url, author_name=author,
                    author_only=author_only)
            except Exception as ex:
                err = str(ex)
                self._invoke_on_main.emit(
                    lambda: self._write_url_fetch_reverse({
                        'action': 'fetch_done', 'success': False,
                        'status_message': f"❌ 讀取失敗: {err}",
                        'status_color': '#dc3545',
                    }))
                return

            if text_content is None:
                if author_only and author:
                    try:
                        fb, _, _ = _parse_page_html(
                            page_html, raw_url, author_name="",
                            author_only=False)
                    except Exception:
                        fb = None
                    if fb:
                        msg = (f"⚠️ 未找到作者「{author}」的貼文，"
                               f"請檢查名稱或關閉「僅作者」選項")
                        c = '#f39c12'
                    else:
                        msg = "❌ 找不到 article 區塊！"
                        c = '#dc3545'
                else:
                    msg = "❌ 找不到 article 區塊！"
                    c = '#dc3545'
                self._invoke_on_main.emit(
                    lambda m=msg, cc=c: self._write_url_fetch_reverse({
                        'action': 'fetch_done', 'success': False,
                        'status_message': m, 'status_color': cc,
                    }))
                return

            def _apply() -> None:
                display_title = _extract_work_title(page_title) if page_title else ""
                full_text = (display_title + "\n\n" + text_content
                             if display_title else text_content)
                self._translate_panel.source_text.setPlainText(full_text)
                QTimer.singleShot(50, self.check_chapter_number)
                self._update_work_title(display_title)
                self.url_related_links = nav_links
                self.current_url = raw_url
                hist = {'url': raw_url, 'title': page_title or raw_url}
                self.url_history = [h for h in self.url_history
                                    if h['url'] != raw_url]
                self.url_history.append(hist)
                if len(self.url_history) > 50:
                    self.url_history = self.url_history[-50:]
                self.schedule_save()
                line_count = text_content.count('\n') + 1
                self.show_status(f"✅ 網址讀取成功！共 {line_count} 行", "#0f0")
                self._write_url_fetch_reverse({
                    'action': 'fetch_done', 'success': True,
                    'status_message': f"✅ 讀取成功！共 {line_count} 行",
                    'status_color': '#28a745',
                    'url_history': self.url_history,
                    'url_related_links': self.url_related_links,
                    'current_url': self.current_url,
                    'auto_close': True,
                })
            self._invoke_on_main.emit(_apply)

        threading.Thread(target=_bg, daemon=True).start()

    def fetch_prev_chapter(self) -> None:
        self._fetch_adjacent_chapter(direction=-1)

    def fetch_next_chapter(self) -> None:
        self._fetch_adjacent_chapter(direction=+1)

    def _fetch_adjacent_chapter(self, direction: int) -> None:
        links = self.url_related_links
        label = "下一話" if direction > 0 else "上一話"
        if not links:
            self.show_status("⚠️ 尚未讀取過網址，無關聯記事資料！", "#f39c12")
            return
        current_idx = next(
            (i for i, lk in enumerate(links) if lk.get('is_current')), -1)
        if current_idx < 0:
            self.show_status("⚠️ 找不到目前所在的話數！", "#f39c12")
            return
        target_idx = current_idx + direction
        if target_idx < 0:
            self.show_status("⚠️ 已經是最早一話了！", "#f39c12")
            return
        if target_idx >= len(links):
            self.show_status("⚠️ 已經是最新一話了！", "#f39c12")
            return
        next_lk = links[target_idx]
        if not next_lk.get('url'):
            self.show_status(f"⚠️ {label}沒有連結！", "#f39c12")
            return
        next_url = next_lk['url']
        self.show_status(f"⏳ 正在讀取{label}…", "#17a2b8", duration=0)
        author = self._author_name

        def _bg() -> None:
            try:
                page_html = self._read_url_cache(next_url)
                if page_html is None:
                    page_html = _fetch_url(next_url)
                    self._write_url_cache(next_url, page_html)
                text_content, nav_links, page_title = _parse_page_html(
                    page_html, next_url, author_name=author,
                    author_only=self._author_only)
                if text_content is None:
                    ao = self._author_only
                    if ao and author:
                        try:
                            fb, _, _ = _parse_page_html(
                                page_html, next_url, author_name="",
                                author_only=False)
                        except Exception:
                            fb = None
                        m = (f"⚠️ 未找到作者「{author}」的貼文"
                             if fb else "❌ 找不到 article 區塊！")
                        c = "#f39c12" if fb else "#dc3545"
                    else:
                        m, c = "❌ 找不到 article 區塊！", "#dc3545"
                    self._invoke_on_main.emit(
                        lambda mm=m, cc=c: self.show_status(mm, cc))
                    return

                def _apply() -> None:
                    display_title = (
                        _extract_work_title(page_title) if page_title else "")
                    full_text = (display_title + "\n\n" + text_content
                                 if display_title else text_content)
                    self._translate_panel.source_text.setPlainText(full_text)
                    QTimer.singleShot(50, self.check_chapter_number)
                    self._update_work_title(display_title)
                    self.url_related_links = nav_links
                    self.current_url = next_url
                    hist = {'url': next_url, 'title': page_title or next_url}
                    self.url_history = [h for h in self.url_history
                                        if h['url'] != next_url]
                    self.url_history.append(hist)
                    if len(self.url_history) > 50:
                        self.url_history = self.url_history[-50:]
                    self.schedule_save()
                    self.show_status(
                        f"✅ 讀取成功！共 {text_content.count(chr(10)) + 1} 行",
                        "#0f0")
                self._invoke_on_main.emit(_apply)
            except Exception as ex:
                self._invoke_on_main.emit(
                    lambda e=ex: self.show_status(
                        f"❌ 讀取失敗: {e}", "#dc3545"))

        threading.Thread(target=_bg, daemon=True).start()

    # ════════════════════════════════════════════════════════════
    #  設定 / 暫存
    # ════════════════════════════════════════════════════════════

    def schedule_save(self) -> None:
        if self._save_timer is not None:
            self._save_timer.stop()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.save_cache)
        self._save_timer.start()

    def _gather_cache(self) -> AppCache:
        p = self._translate_panel
        return AppCache(
            source_text=p.get_source_text().rstrip('\n'),
            filter_text=p.get_filter_text().rstrip('\n'),
            glossary_text=p.get_glossary_text().rstrip('\n'),
            glossary_text_temp=p.get_glossary_temp_text().rstrip('\n'),
            doc_title=p.get_doc_title(),
            doc_num=p.get_doc_num(),
            bg_color=DEFAULT_BG_COLOR,
            fg_color=DEFAULT_FG_COLOR,
            preview_text="",
            url_history=self.url_history,
            url_related_links=self.url_related_links,
            current_url=self.current_url,
            auto_copy=p.auto_copy_cb.isChecked(),
            batch_folder=self._batch_folder,
            author_name=self._author_name,
            author_only=self._author_only,
            work_history=list(self.work_history),
            editor_font_family=self._editor_font_family,
            editor_font_size=self._editor_font_size,
            last_open_dir=self._last_dir,
            editor_bg_color=self._editor_bg_color,
        )

    def _apply_cache(self, cache: AppCache) -> None:
        p = self._translate_panel
        if cache.source_text:
            p.source_text.setPlainText(cache.source_text)
        if cache.filter_text:
            p.filter_text.setPlainText(cache.filter_text)
        if cache.glossary_text:
            p.glossary_text.setPlainText(cache.glossary_text)
        if cache.glossary_text_temp:
            p.glossary_text_temp.setPlainText(cache.glossary_text_temp)
        if cache.doc_title:
            p.doc_title.setText(cache.doc_title)
        if cache.doc_num:
            p.doc_num.setText(cache.doc_num)
        if cache.url_history:
            self.url_history = cache.url_history
        if cache.url_related_links:
            self.url_related_links = cache.url_related_links
        if cache.current_url:
            self.current_url = cache.current_url
        if cache.auto_copy:
            p.auto_copy_cb.setChecked(True)
        if cache.batch_folder:
            self._batch_folder = cache.batch_folder
        if cache.author_name:
            self._author_name = cache.author_name
        self._author_only = cache.author_only
        if cache.work_history:
            self.work_history = list(cache.work_history)
        if cache.editor_font_family:
            self._editor_font_family = cache.editor_font_family
        if cache.editor_font_size:
            self._editor_font_size = int(cache.editor_font_size)
        if cache.last_open_dir and os.path.isdir(cache.last_open_dir):
            self._last_dir = cache.last_open_dir
        if cache.editor_bg_color:
            self._editor_bg_color = cache.editor_bg_color

    def save_cache(self) -> None:
        self.settings_mgr.save_cache(self._gather_cache())

    def load_cache(self) -> None:
        cache = self.settings_mgr.load_cache()
        self._apply_cache(cache)

    def _load_initial_state(self) -> None:
        self.load_cache()
        settings = self.settings_mgr.load_settings()
        p = self._translate_panel
        if settings.filter_text:
            p.filter_text.setPlainText(settings.filter_text)
        if settings.glossary:
            p.glossary_text.setPlainText(settings.glossary)
        if settings.glossary_temp:
            p.glossary_text_temp.setPlainText(settings.glossary_temp)
        self.current_base_regex = settings.base_regex
        self.current_invalid_regex = settings.invalid_regex
        self.current_symbol_regex = settings.symbol_regex
        self._saved_glossary_lines = self._count_nonempty(settings.glossary)
        self._saved_glossary_temp_lines = self._count_nonempty(settings.glossary_temp)
        self._saved_filter_lines = self._count_nonempty(settings.filter_text)

    @staticmethod
    def _count_nonempty(text: str) -> int:
        return sum(1 for l in text.strip().splitlines() if l.strip())

    def export_settings(self) -> None:
        self.save_cache()
        p = self._translate_panel
        settings = AppSettings(
            filter_text=p.get_filter_text().strip(),
            glossary=p.get_glossary_text().strip(),
            glossary_temp=p.get_glossary_temp_text().strip(),
            base_regex=self.current_base_regex,
            invalid_regex=self.current_invalid_regex,
            symbol_regex=self.current_symbol_regex,
        )
        try:
            self.settings_mgr.save_settings(settings)
            self._saved_glossary_lines = self._count_nonempty(settings.glossary)
            self._saved_glossary_temp_lines = self._count_nonempty(settings.glossary_temp)
            self._saved_filter_lines = self._count_nonempty(settings.filter_text)
            self.show_status("✅ 設定儲存成功！", "#0f0")
        except Exception as e:
            self.show_status(f"❌ 設定儲存失敗: {e}", "#dc3545")

    def import_settings(self) -> None:
        if not os.path.exists(self.settings_mgr.get_settings_file()):
            self.show_status("⚠️ 找不到設定檔 AA_Settings.json！", "#f39c12")
            return
        try:
            settings = self.settings_mgr.load_settings()
            p = self._translate_panel
            p.filter_text.setPlainText(settings.filter_text or "")
            p.glossary_text.setPlainText(settings.glossary or "")
            p.glossary_text_temp.setPlainText(settings.glossary_temp or "")
            self.current_base_regex = settings.base_regex
            self.current_invalid_regex = settings.invalid_regex
            self.current_symbol_regex = settings.symbol_regex
            self.save_cache()
            self.show_status("✅ 設定已成功讀取！", "#0f0")
        except Exception:
            self.show_status("❌ 讀取失敗，請確認檔案格式是否正確。", "#dc3545")

    def _manual_load_cache(self) -> None:
        self.load_cache()
        self.show_status("✅ 暫存讀取成功！", "#0f0")

    # ════════════════════════════════════════════════════════════
    #  原文暫存 (依檔名索引，上限 50)
    # ════════════════════════════════════════════════════════════

    _ORIG_CACHE_LIMIT = 50

    def _orig_cache_path(self) -> str:
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'aa_original_cache.json')

    def _load_orig_cache_data(self) -> dict:
        p = self._orig_cache_path()
        if not os.path.exists(p):
            return {}
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_orig_cache_data(self, data: dict) -> None:
        """原子寫入：先寫暫存檔再改名，減少多執行緒損壞機率。"""
        target = self._orig_cache_path()
        tmp = target + ".tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError:
            pass

    def save_original_for_file(self, file_path: str,
                               original_text: str) -> None:
        if not file_path or not original_text:
            return
        # 讀回現有資料再合併（保留其他執行緒/進程已寫入的條目）
        data = self._load_orig_cache_data()
        key = os.path.basename(file_path)
        data[key] = {'text': original_text, 'ts': time.time()}
        # 上限裁切（依時間戳保留最新的 N 筆）
        if len(data) > self._ORIG_CACHE_LIMIT:
            ordered = sorted(data.items(),
                             key=lambda kv: kv[1].get('ts', 0),
                             reverse=True)
            data = dict(ordered[:self._ORIG_CACHE_LIMIT])
        self._save_orig_cache_data(data)

    def load_original_for_file(self, file_path: str) -> str | None:
        if not file_path:
            return None
        data = self._load_orig_cache_data()
        entry = data.get(os.path.basename(file_path))
        if not isinstance(entry, dict):
            return None
        text = entry.get('text')
        return text if isinstance(text, str) and text else None

    def _on_editor_bg_changed(self, color: str) -> None:
        self._editor_bg_color = color
        self.schedule_save()

    def _on_last_dir_changed(self, directory: str) -> None:
        if directory and os.path.isdir(directory):
            self._last_dir = directory
            self.schedule_save()

    def _on_editor_font_changed(self, family: str, size: int) -> None:
        self._editor_font_family = family
        self._editor_font_size = int(size)
        self.schedule_save()

    def _on_edit_saved(self, file_path: str) -> None:
        """EditWindow 儲存成功後的 callback。"""
        # 更新導覽列與標題
        base = os.path.basename(file_path)
        title = self._edit_window._display_title if self._edit_window else ""
        nav_name = title or base
        self._nav_label.setText(f"編輯：{nav_name}")
        self._update_work_title(f"編輯 — {nav_name}")
        # 暫存原文
        if self._edit_window is not None and self._edit_window._original_text:
            self.save_original_for_file(
                file_path, self._edit_window._original_text)

    # ════════════════════════════════════════════════════════════
    #  作品 + 作者 歷史記錄 (最多 10 筆)
    # ════════════════════════════════════════════════════════════

    _WORK_HISTORY_LIMIT = 10

    def _record_work_history(self) -> None:
        """按下替換翻譯時呼叫，將當前 (title, author) 記入歷史。"""
        p = self._translate_panel
        title = p.get_doc_title().strip()
        author = self._author_name.strip()
        if not title and not author:
            return
        pair = {'title': title, 'author': author}
        history = [h for h in getattr(self, 'work_history', [])
                   if not (h.get('title') == title
                           and h.get('author') == author)]
        history.insert(0, pair)
        self.work_history = history[:self._WORK_HISTORY_LIMIT]
        self.schedule_save()

    def show_work_history_menu(self) -> None:
        history = getattr(self, 'work_history', [])
        menu = QMenu(self)
        if not history:
            act = menu.addAction("(尚無歷史記錄)")
            act.setEnabled(False)
        else:
            for h in history:
                t = h.get('title', '') or "(無標題)"
                a = h.get('author', '') or "(無作者)"
                act = menu.addAction(f"{t}　|　{a}")
                act.triggered.connect(
                    lambda checked=False, hh=h: self._apply_work_history(hh))
        btn = self._translate_panel.btn_work_history
        pos = btn.mapToGlobal(btn.rect().bottomLeft())
        menu.exec(pos)

    def _apply_work_history(self, entry: dict) -> None:
        p = self._translate_panel
        p.doc_title.setText(entry.get('title', ''))
        self._author_name = entry.get('author', '')
        self.schedule_save()

    # ════════════════════════════════════════════════════════════
    #  關閉事件
    # ════════════════════════════════════════════════════════════

    def closeEvent(self, event) -> None:
        self.save_cache()
        p = self._translate_panel
        cur_g = self._count_nonempty(p.get_glossary_text())
        cur_gt = self._count_nonempty(p.get_glossary_temp_text())
        cur_f = self._count_nonempty(p.get_filter_text())

        parts = []
        if cur_g > self._saved_glossary_lines:
            parts.append(
                f"術語表（目前 {cur_g} 行，已儲存 {self._saved_glossary_lines} 行）")
        if cur_gt > self._saved_glossary_temp_lines:
            parts.append(f"臨時術語表（目前 {cur_gt} 行）")
        if cur_f > self._saved_filter_lines:
            parts.append(f"自訂過濾規則（目前 {cur_f} 行）")

        if parts:
            reply = QMessageBox.question(
                self, "儲存設定？",
                f"以下項目有未儲存的新增內容：\n{'、'.join(parts)}\n\n"
                "是否在關閉前儲存至 AA_Settings.json？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.StandardButton.Yes:
                self.export_settings()
        event.accept()


# ════════════════════════════════════════════════════════════
#  Entry Point
# ════════════════════════════════════════════════════════════

def main() -> None:
    app = QApplication(sys.argv)
    load_bundled_fonts()
    qss_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "aa_tool", "dark_theme.qss")
    if os.path.exists(qss_path):
        with open(qss_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    win = MainWindow()
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
