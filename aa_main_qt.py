"""AA 創作翻譯輔助工具 — PyQt6 主視窗。

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
import sys
import tempfile
import threading
import time

from PyQt6.QtCore import Qt, QEvent, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QGuiApplication, QKeySequence, QPalette, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit,
    QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSplitter, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from aa_tool.constants import (
    DEFAULT_BASE_REGEX, DEFAULT_BASE_REGEX_KO,
    DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX,
    DEFAULT_BG_COLOR, DEFAULT_FG_COLOR,
)
from aa_tool.html_io import read_html_pre_content, write_html_file, read_html_bg_color
from aa_tool import original_cache
from aa_tool import url_fetcher as _url_fetcher
from aa_tool.qt_helpers import WrapRow, show_toast
from aa_tool.settings_manager import (
    SettingsManager, AppSettings, AppCache,
    merge_glossary_diff, merge_filter_diff,
)
from aa_tool.text_extraction import (
    extract_text as _extract_text,
    extract_single_kana as _extract_single_kana,
    format_extraction_output,
    analyze_extraction as _analyze_extraction,
    validate_ai_text as _validate_ai_text,
    check_chapter_number as _check_chapter_number,
    extract_work_title as _extract_work_title,
)
from aa_tool.translation_engine import (
    parse_glossary,
    apply_translation as _apply_translation,
    decode_glossary_term,
    encode_glossary_term,
)
from aa_tool.url_fetcher import fetch_url as _fetch_url, parse_page_html as _parse_page_html
from aa_edit_qt import EditWindow, load_bundled_fonts
from aa_batch_search_qt import BatchSearchWindow
from aa_auto_translate_qt import AutoTranslatePanel

APP_VERSION = "2.14"
APP_TITLE = f"AA 創作翻譯輔助小工具 v{APP_VERSION}"

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
#  術語表 QTextEdit（右鍵選單加上「刪除整筆條目」）
# ════════════════════════════════════════════════════════════

class GlossaryTextEdit(QTextEdit):
    """術語表專用 QTextEdit；在框選文字後右鍵會出現「刪除整筆條目」項目。

    動作：刪除選取範圍涉及的整行（含換行符），標準 copy/paste 等行為保留。
    刪除後 textChanged 會觸發既有的 schedule_save，連帶從 AA_Settings.json 移除。
    """

    def contextMenuEvent(self, event):  # noqa: N802 (Qt 命名)
        from PyQt6.QtGui import QTextCursor as _TC
        menu = self.createStandardContextMenu()
        cursor = self.textCursor()
        if cursor.hasSelection():
            menu.addSeparator()
            act = menu.addAction("刪除整筆條目")
            act.triggered.connect(self._delete_selected_lines)
        menu.exec(event.globalPos())

    def _delete_selected_lines(self) -> None:
        from PyQt6.QtGui import QTextCursor as _TC
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        text_len = len(self.toPlainText())

        cursor.setPosition(start)
        cursor.movePosition(_TC.MoveOperation.StartOfBlock)
        line_start = cursor.position()

        cursor.setPosition(end)
        if cursor.positionInBlock() == 0 and end > start:
            cursor.movePosition(_TC.MoveOperation.Left)
        cursor.movePosition(_TC.MoveOperation.EndOfBlock)
        line_end = cursor.position()
        if line_end < text_len:
            line_end += 1  # 一併移除行尾換行

        cursor.beginEditBlock()
        cursor.setPosition(line_start)
        cursor.setPosition(line_end, _TC.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.endEditBlock()


# ════════════════════════════════════════════════════════════
#  TranslatePanel
# ════════════════════════════════════════════════════════════

class TranslatePanel(QWidget):
    """翻譯主面板。包含原文、過濾規則、術語表、提取結果、翻譯結果。"""

    def __init__(self, main_win: MainWindow) -> None:
        super().__init__()
        self._main = main_win
        self._glossary_dup_positions: list[int] = []
        self._glossary_dup_cycle_idx = 0
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
        # 左右兩個區塊各自一個 QHBoxLayout，外層用 WrapRow：視窗夠寬時維持
        # 「左側導覽靠左、右側工具靠右」的原樣；不夠寬時右區塊自動換到第二行，
        # 讓工具列的最小寬度不再是所有按鈕寬度總和（否則高 DPI 縮放的小邏輯
        # 寬度螢幕會因視窗撐不下而把右側按鈕擠出畫面）。
        left = QWidget()
        row = QHBoxLayout(left)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)

        title_lbl = QLabel("AA 創作翻譯輔助工具")
        title_lbl.setFont(_ui_font(16, bold=True))
        title_lbl.setStyleSheet("color:white;")
        row.addWidget(title_lbl)

        row.addSpacing(8)
        btn_settings = _make_btn("⚙", "#6c757d", "#5a6268",
                                 font=_ui_font(14), width=34)
        btn_settings.setToolTip("設定")
        btn_settings.clicked.connect(self._main.toggle_settings_panel)
        row.addWidget(btn_settings)

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

        btn_auto = _make_btn("自動翻譯", "#d63384", "#b02a6f",
                             font=_ui_font(11), width=90)
        btn_auto.setToolTip("連續多話全自動翻譯（操控網頁版 Gemini）")
        btn_auto.clicked.connect(self._main.show_auto_translate_panel)
        row.addWidget(btn_auto)

        right = QWidget()
        row = QHBoxLayout(right)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)

        btn_file_list = _make_btn(
            "📂 檔案列表", "#0d6efd", "#0b5ed7", font=_ui_font(12))
        btn_file_list.setToolTip(
            "列出最後開啟檔案所在資料夾的所有 HTML 檔，目前檔案置中、可捲動")
        btn_file_list.clicked.connect(self._main.toggle_file_list_panel)
        row.addWidget(btn_file_list)

        btn_wiki = _make_btn("📖 Wiki 對照", "#6f42c1", "#5a32a3", font=_ui_font(12))
        btn_wiki.setToolTip("從 Wiki 角色列表頁抓取中日文對照")
        btn_wiki.clicked.connect(self._main.open_wiki_name_dialog)
        row.addWidget(btn_wiki)

        btn_import = _make_btn("📥 讀取設定", "#17a2b8", "#138496", font=_ui_font(12))
        btn_import.clicked.connect(self._main.import_settings)
        row.addWidget(btn_import)

        btn_export = _make_btn("📤 儲存設定", "#28a745", "#218838", font=_ui_font(12))
        btn_export.setToolTip(
            "左鍵：依設定行為儲存（可能合併差異）\n右鍵：強制以覆蓋方式儲存")
        btn_export.clicked.connect(self._main.export_settings)
        btn_export.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn_export.customContextMenuRequested.connect(
            lambda _pos: self._main.export_settings(force_overwrite=True))
        row.addWidget(btn_export)

        btn_debug = _make_btn("🔧提取Debug", "#6c757d", "#5a6268", font=_ui_font(12))
        btn_debug.clicked.connect(self._main.analyze_extraction)
        row.addWidget(btn_debug)

        # collapsible：排不進同一行時自動隱藏標題（視窗標題列已有同樣資訊），
        # 省下的寬度讓 1280 邏輯寬度的螢幕仍能維持單行工具列。
        w = WrapRow(left, right, margins=(10, 5, 10, 5), gap=8,
                    collapsible=title_lbl)
        w.setStyleSheet("background:#343a40;")

        # 供設定浮層定位用：讓浮層自工具列（⚙ 鈕所在）底下展開，不蓋住 ⚙ 鈕。
        self._toolbar = w
        return w

    def _build_source_area(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        # 標頭列
        top = QHBoxLayout()

        lbl = QLabel("原始文本")
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
        # 強制只接受純文字：避免從網頁／Word 等來源貼上時帶入 font-family、
        # color 等 inline 格式，導致後續傳到編輯器時無法被字型切換功能覆蓋。
        self.source_text.setAcceptRichText(False)
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
        lbl_f = QLabel("自訂過濾規則 (每行一條支援正則):")
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
        lbl_g = QLabel("術語表 (格式:原文=替代):")
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

        # 術語表搜尋列（取代原本的 一般/臨時 tab）
        search_row = QHBoxLayout()
        self._gloss_search = QLineEdit()
        self._gloss_search.setPlaceholderText("搜尋術語表… (Enter 跳到下一個)")
        self._gloss_search.setFont(_ui_font(11))
        self._gloss_search.setStyleSheet(
            "background:#2a3b4c; color:#ddd; padding:2px 6px; border:1px solid #555;"
            " border-radius:3px;")
        self._gloss_search.textChanged.connect(self._search_glossary_from_top)
        self._gloss_search.returnPressed.connect(self._search_glossary_next)
        search_row.addWidget(self._gloss_search, 1)

        self._gloss_search_status = QLabel("")
        self._gloss_search_status.setFont(_ui_font(10))
        self._gloss_search_status.setStyleSheet("color:#888;")
        search_row.addWidget(self._gloss_search_status)

        btn_next = _make_btn("下一個", "#495057", "#3d4449",
                             font=_ui_font(10), width=60)
        btn_next.setFixedHeight(22)
        btn_next.clicked.connect(self._search_glossary_next)
        search_row.addWidget(btn_next)
        vl.addLayout(search_row)

        # 一般術語表
        self.glossary_text = GlossaryTextEdit()
        self.glossary_text.setFont(_aa_font(13))
        self.glossary_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.glossary_text.setAcceptRichText(False)
        self.glossary_text.setStyleSheet("background:#2a3b4c; color:#ddd;")
        self.glossary_text.textChanged.connect(self._main.schedule_save)
        self.glossary_text.textChanged.connect(
            lambda: QTimer.singleShot(100, self._check_glossary_duplicates))
        vl.addWidget(self.glossary_text, 1)

        # 臨時術語表：UI 已隱藏，但物件仍保留供 cache / AA_Settings.json I/O 使用。
        # 不加入 layout 即不顯示。
        self.glossary_text_temp = QTextEdit()
        self.glossary_text_temp.setAcceptRichText(False)
        self.glossary_text_temp.textChanged.connect(self._main.schedule_save)

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

        hl.addStretch()
        return w

    def _build_extracted_area(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        top = QHBoxLayout()
        lbl = QLabel("提取結果:")
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

        btn_range = _make_btn("複製指定範圍", "#495057", "#3d4449",
                              font=_ui_font(10), width=90)
        btn_range.setFixedHeight(24)
        btn_range.setToolTip("依原文行號範圍複製提取結果（例：1~3000）")
        btn_range.clicked.connect(self._main.copy_range)
        top.addWidget(btn_range)

        add_filter_btn = _make_btn("加入自訂過濾", "#6f42c1", "#5a34a0",
                                   font=_ui_font(10), width=110)
        add_filter_btn.setFixedHeight(24)
        add_filter_btn.setToolTip("將選取文字加入自訂過濾（自動去除流水號）")
        add_filter_btn.clicked.connect(self._main.add_selection_to_filter)
        top.addWidget(add_filter_btn)
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
        lbl = QLabel("填入翻譯:")
        lbl.setFont(_ui_font(13, bold=True))
        top.addWidget(lbl)

        # 讀取新的一話（上一話／下一話／網址讀取）成功後自動清空本欄，
        # 避免上一話的譯文殘留下來被誤套用。狀態存進 cache。
        self.clear_ai_cb = QCheckBox("讀取後清空")
        self.clear_ai_cb.setFont(_ui_font(11))
        self.clear_ai_cb.setStyleSheet("color:#ddd;")
        self.clear_ai_cb.setToolTip(
            "勾選後，按「上一話／下一話」或用「🌐 網址讀取」讀取成功時，"
            "自動清空「填入翻譯」欄位。")
        self.clear_ai_cb.toggled.connect(self._main._on_clear_ai_toggled)
        top.addWidget(self.clear_ai_cb)

        self._ai_warn_label = QLabel("")
        self._ai_warn_label.setFont(_ui_font(11))
        self._ai_warn_label.setStyleSheet("color:#ff4444;")
        top.addWidget(self._ai_warn_label)
        top.addStretch()
        # 翻譯 ↔ 提取 對應率：低於 50% 時以橘色提示，避免漏譯／錯行
        # 沒被察覺。改在這裡即時顯示，不再用 Toast 中斷流程。
        self._ai_match_label = QLabel("")
        self._ai_match_label.setFont(_ui_font(11))
        top.addWidget(self._ai_match_label)
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

    # ── 術語表搜尋 ──

    def _search_glossary_from_top(self, text: str) -> None:
        """輸入框內容變動：從文件開頭重新搜尋第一個匹配。"""
        if not text:
            self._gloss_search_status.setText("")
            cursor = self.glossary_text.textCursor()
            cursor.clearSelection()
            self.glossary_text.setTextCursor(cursor)
            return
        from PyQt6.QtGui import QTextCursor
        cursor = self.glossary_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.glossary_text.setTextCursor(cursor)
        if self.glossary_text.find(text):
            self._gloss_search_status.setText("")
        else:
            self._gloss_search_status.setText("找不到")

    def _search_glossary_next(self) -> None:
        """跳到下一個匹配；若到底則自動回到開頭再找一次。"""
        text = self._gloss_search.text()
        if not text:
            return
        if self.glossary_text.find(text):
            self._gloss_search_status.setText("")
            return
        from PyQt6.QtGui import QTextCursor
        cursor = self.glossary_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.glossary_text.setTextCursor(cursor)
        if self.glossary_text.find(text):
            self._gloss_search_status.setText("已從頭開始")
        else:
            self._gloss_search_status.setText("找不到")

    # ── 重複術語偵測 ──

    def _check_glossary_duplicates(self) -> None:
        # 臨時術語表已隱藏；只檢查一般術語表。
        g_lines = self.glossary_text.toPlainText().strip().split('\n')
        key_positions: dict[str, list[int]] = {}
        for i, line in enumerate(g_lines):
            if '=' in line:
                key = decode_glossary_term(line.split('=', 1)[0])
                if key:
                    key_positions.setdefault(key, []).append(i)

        dup_pos: list[int] = []
        for positions in key_positions.values():
            if len(positions) >= 2:
                dup_pos.extend(positions)

        self._glossary_dup_positions = dup_pos
        self._glossary_dup_cycle_idx = 0
        if dup_pos:
            self._dup_label.setText("⚠ 術語有重複")
            self._dup_btn.show()
        else:
            self._dup_label.setText("")
            self._dup_btn.hide()

    def _jump_to_glossary_dup(self) -> None:
        if not self._glossary_dup_positions:
            return
        line_idx = self._glossary_dup_positions[self._glossary_dup_cycle_idx]
        self._glossary_dup_cycle_idx = (
            self._glossary_dup_cycle_idx + 1) % len(self._glossary_dup_positions)
        widget = self.glossary_text
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
        self.setWindowTitle(APP_TITLE)
        # 預設尺寸夾在螢幕可用區內：高 DPI 縮放下的「邏輯解析度」可能遠小於
        # 1400x900（例：1080p @150% ＝ 1280x720），直接 resize(1400, 900) 會讓
        # 還原視窗時右側／底部超出畫面，按鈕點不到也看不到。
        avail = None
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
        if avail is not None and avail.width() > 0 and avail.height() > 0:
            self.resize(min(1400, avail.width()), min(900, avail.height()))
        else:
            self.resize(1400, 900)
        self._dark_title_applied = False

        # ── 設定管理 ──
        # frozen（PyInstaller）時 __file__ 指向 _internal/；改用 exe 旁的目錄
        if getattr(sys, 'frozen', False):
            _base_dir = os.path.dirname(sys.executable)
        else:
            _base_dir = os.path.dirname(os.path.abspath(__file__))
        # 設定／金鑰／cache 的統一基準目錄（凍結時為 exe 旁）。自動翻譯的金鑰存取與
        # run_auto_translate 都必須用它，否則打包版會從 _internal/ 讀不到而回退預設。
        self._settings_base_dir = _base_dir
        self.settings_mgr = SettingsManager(_base_dir)
        self.current_base_regex = DEFAULT_BASE_REGEX
        self.current_invalid_regex = DEFAULT_INVALID_REGEX
        self.current_symbol_regex = DEFAULT_SYMBOL_REGEX
        self._korean_mode: bool = False
        self._experimental_extraction: bool = False
        self._pad_right_aa: bool = False
        self._glossary_avoid_aa: bool = False
        self._glossary_kana_fold: bool = False
        self._glossary_skip_extract: bool = False
        self._glossary_auto_persist: bool = False
        self._glossary_translation_only: bool = False
        self._fetch_auto_fill_title: bool = False
        self._fetch_clear_ai_text: bool = False
        # 代理伺服器：抓網頁與 API 翻譯分開設定（見 aa_tool/net_proxy.py）
        self._fetch_proxy_url: str = ""
        self._api_proxy_url: str = ""
        # 「複製指定範圍」上次輸入的範圍字串（僅本次執行期間記住）
        self._copy_range_last: str = ""

        # ── 應用狀態 ──
        self.url_history: list[dict] = []
        self.url_related_links: list[dict] = []
        self.current_url: str = ""
        # 自動翻譯（aa_auto_translate）設定，由 cache 載入／回存
        self._gemini_gem_url: str = ""
        self._gemini_profile_dir: str = ""
        self._gemini_max_per_session: int = 3
        self._gemini_required_model: str = "pro"
        self._gemini_selectors: dict = {}
        self._auto_translate_out_dir: str = ""
        # 手動網址清單（原始文字，一行一個）；非空時自動翻譯照清單跑
        self._auto_translate_url_list: str = ""
        self._auto_translate_count: int = 5
        self._auto_translate_until_last: bool = False
        self._auto_translate_skip_existing: bool = False
        # 自動翻譯：加入翻譯（True，保留原文）／替換翻譯（False）。預設替換。
        self._auto_translate_append_mode: bool = False
        # 翻譯後端與 API 設定（金鑰另存於加密檔，不在 cache）
        self._translate_backend: str = "browser"
        self._api_provider: str = "gemini"  # API 供應商（gemini/openai/claude/deepseek/custom）
        self._gemini_api_model: str = "gemini-2.5-pro"
        self._api_models: dict = {}  # 非 gemini 供應商各自的模型 id：{供應商: model}
        self._api_custom_base_url: str = ""  # 自定義供應商的 OpenAI 相容端點
        self._api_timeout: int = 600  # 單次 API 請求逾時（秒），連線設定可調
        self._gemini_api_only_prompt: str = ""  # 僅 API 送出（瀏覽器模式不送）
        self._gemini_api_system_prompt: str = ""  # API 送；瀏覽器模式於 !use_gem 才送
        self._browser_use_gem: bool = True
        self._auto_translate_running: bool = False
        self._auto_stop_event = None  # threading.Event，執行中時設定
        self._author_only: bool = False
        self._author_name: str = ""
        # URL 讀取後暫存的標題（line 1），供「提取日文」時跳過標題行之用
        self._last_fetched_title: str = ""
        self._batch_folder: str = ""
        self.work_history: list[dict] = []
        self._editor_font_family: str = "submona"
        self._editor_font_size: int = 12
        self._editor_line_height: int = 120
        self._last_dir: str = ""
        self._last_opened_file: str = ""
        self._editor_bg_color: str = "#ffffff"
        self._auto_copy: bool = False
        self._work_history_limit: int = 10
        self._fetch_history_limit: int = 50
        self._original_cache_limit: int = 50
        self._glossary_auto_search: bool = True
        self._diff_save_mode: bool = False
        self._embed_font_in_html: bool = False
        self._embed_font_name: str = "monapo"
        self._editor_default_wysiwyg: bool = False
        self._editor_copy_to_replace: bool = False
        self._glossary_sync_to_batch_quick: bool = False
        # 編輯器右側「局部重套用」面板（Alt+4）的持久化狀態
        self._side_panel_width: int = 0
        self._glossary_panel_width: int = 0
        self._side_auto_scroll: bool = False
        # 編輯器「補空白」每字之間插入的全形空白數量（1~3）
        self._pad_space_count: int = 2
        self._save_timer: QTimer | None = None
        self._url_fetch_win = None  # UrlFetchWindow lazy-init（in-process）
        self._url_fetch_from_auto = False  # 網址讀取是否由自動翻譯「網址記錄」鈕進入
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

        # ── 自動翻譯進度橫幅（執行中顯示在所有面板頂部） ──
        self._auto_banner = self._build_auto_banner()
        root.addWidget(self._auto_banner)
        self._auto_banner.hide()

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

        # 網址讀取面板（lazy init）
        self._url_fetch_placeholder = QWidget()
        self.stack.addWidget(self._url_fetch_placeholder)  # index 3

        # 自動翻譯面板（lazy init）
        self._auto_window: AutoTranslatePanel | None = None
        self._auto_placeholder = QWidget()
        self.stack.addWidget(self._auto_placeholder)  # index 4

        # 設定浮層（lazy init；以子 widget 浮層方式疊在內容上，比照自動翻譯
        # 的連線設定浮層，不再開獨立 modal 視窗）
        self._settings_panel: QWidget | None = None
        self._settings_scroll: QScrollArea | None = None
        self._settings_content: 'SettingsDialog | None' = None
        # 「📂 檔案列表」浮層（lazy init；列出最後開啟檔案所在資料夾的相鄰檔案）
        self._file_list_panel: QWidget | None = None
        self._file_list_widget: QListWidget | None = None
        self._file_list_status: QLabel | None = None
        # Popup 點面板外關閉時的時間戳；防止「點工具列鈕時 Popup 先關掉、然後
        # 同一次點擊又把面板重新打開」的閃爍 reopen。
        self._file_list_hide_ts: float = 0.0

        # ── 底部動作列 ──
        self._action_bar = self._build_action_bar()
        root.addWidget(self._action_bar)

        # ── 快捷鍵 ──
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.apply_translation)

        # ── 載入設定 / 暫存 ──
        self._load_initial_state()

        # ── 多程序共享歷史紀錄即時同步 ──
        # 每 1.5 秒讀檔比對 url_history / work_history / url_related_links，
        # 有變動就刷新 in-memory 並推送給 URL 抓取子程序（若在跑）。
        # 不動編輯器中的文字。
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(1500)
        self._sync_timer.timeout.connect(self._refresh_shared_history)
        self._sync_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._dark_title_applied:
            _apply_dark_title_bar(self)
            self._dark_title_applied = True

    def _build_auto_banner(self) -> QWidget:
        """執行自動翻譯時顯示於頁面頂部的常駐橫幅（含進度與停止鈕）。"""
        w = QWidget()
        w.setStyleSheet("background:#d63384;")
        hl = QHBoxLayout(w)
        hl.setContentsMargins(12, 6, 10, 6)
        hl.setSpacing(10)

        icon = QLabel("⚡")
        icon.setFont(_ui_font(14, bold=True))
        icon.setStyleSheet("color:white;")
        hl.addWidget(icon)

        self._auto_banner_label = QLabel("自動翻譯進行中…")
        self._auto_banner_label.setFont(_ui_font(12, bold=True))
        self._auto_banner_label.setStyleSheet("color:white;")
        hl.addWidget(self._auto_banner_label, 1)

        btn_stop = _make_btn("■ 停止", "#dc3545", "#b02a37",
                             font=_ui_font(11, bold=True), width=80)
        btn_stop.setFixedHeight(28)
        btn_stop.setToolTip("停止自動翻譯（當前話可能未完成）")
        btn_stop.clicked.connect(self._stop_auto_translate)
        hl.addWidget(btn_stop)
        self._auto_banner_stop_btn = btn_stop

        return w

    def _build_nav_bar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:#495057;")
        hl = QHBoxLayout(w)
        hl.setContentsMargins(10, 4, 10, 4)
        hl.setSpacing(8)

        btn_back = _make_btn("← 返回首頁", "#6c757d", "#5a6268",
                             font=_ui_font(12), width=110)
        btn_back.setFixedHeight(28)
        btn_back.clicked.connect(self._nav_back)
        hl.addWidget(btn_back)

        # 標題（與「連線設定」鈕交換位置：標題在左、連線設定在右）
        self._nav_label = QLabel("")
        self._nav_label.setFont(_ui_font(12))
        self._nav_label.setStyleSheet("color:white;")
        hl.addWidget(self._nav_label)

        # 連線設定鈕：僅自動翻譯面板顯示，開合該面板的連線設定浮層。
        self._nav_conn_btn = _make_btn("⚙ 連線設定", "#6f42c1", "#5a32a3",
                                       font=_ui_font(11), width=100)
        self._nav_conn_btn.setFixedHeight(28)
        self._nav_conn_btn.setToolTip("切換翻譯方式（瀏覽器／API）、Gem 網址、模型與 API 金鑰")
        self._nav_conn_btn.clicked.connect(self._toggle_auto_conn_panel)
        self._nav_conn_btn.hide()
        hl.addWidget(self._nav_conn_btn)

        # 網址記錄鈕：僅自動翻譯面板顯示，切到網址讀取面板挑/換網址，返回回到自動翻譯。
        self._nav_history_btn = _make_btn("🌐 網址記錄", "#0d6efd", "#0b5ed7",
                                          font=_ui_font(11), width=100)
        self._nav_history_btn.setFixedHeight(28)
        self._nav_history_btn.setToolTip(
            "切到網址讀取面板挑選／切換網址；按返回會回到自動翻譯，"
            "並把選定的網址帶回「起始網址」")
        self._nav_history_btn.clicked.connect(self._open_url_fetch_from_auto)
        self._nav_history_btn.hide()
        hl.addWidget(self._nav_history_btn)

        hl.addStretch()
        return w

    def _nav_back(self) -> None:
        """導覽列「← 返回首頁」：在網址讀取面板時依進入來源返回，否則回首頁。"""
        if self.stack.currentIndex() == 3:
            self.return_from_url_fetch()
            return
        self.show_translate_panel()

    def return_from_url_fetch(self) -> None:
        """離開網址讀取面板的統一出口：依進入來源回到自動翻譯或首頁。

        導覽列返回鈕、ESC、抓取後自動關閉等所有返回路徑都走這裡，確保一致。
        `_url_fetch_from_auto` 由 `_open_url_fetch_from_auto`（自動翻譯入口）設為 True、
        `open_url_fetch_qt`（首頁入口）設為 False。
        """
        if self.stack.currentIndex() != 3:
            return  # 已不在網址讀取面板（例如 auto_close 延遲觸發時已手動離開）→ 忽略
        if getattr(self, "_url_fetch_from_auto", False):
            self._url_fetch_from_auto = False
            if self._url_fetch_win is not None:
                self._url_fetch_win.sync_back_to_main()
            self.show_auto_translate_panel()  # refresh_from_main 會把 current_url 帶回起始網址
        else:
            self.show_translate_panel()

    def _open_url_fetch_from_auto(self) -> None:
        """自動翻譯「網址記錄」鈕：進入網址讀取面板，並記住返回時要回自動翻譯。"""
        self.open_url_fetch_qt()
        self._url_fetch_from_auto = True

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

        btn_append = _make_btn("➕ 加入翻譯並編輯", "#fd7e14", "#e76b00",
                               font=_ui_font(12), width=160)
        btn_append.setFixedHeight(44)
        btn_append.clicked.connect(self.apply_translation_append)
        hl.addWidget(btn_append)

        btn_save = _make_btn("💾 替換翻譯並儲存", "#28a745", "#1e7e34",
                             font=_ui_font(12), width=120)
        btn_save.setFixedHeight(44)
        btn_save.clicked.connect(self.apply_translation_and_save)
        hl.addWidget(btn_save)

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
        if self._url_fetch_win is not None and self.stack.currentIndex() == 3:
            self._url_fetch_win.sync_back_to_main()
        self.stack.setCurrentIndex(0)
        self._nav_bar.hide()
        self._action_bar.show()
        self._update_work_title("")
        self._translate_panel.source_text.setFocus()

    def show_edit_panel(self, file_path: str, scroll_to_line: int = 0,
                        original_text: str | None = None,
                        display_title: str = "",
                        is_temp_file: bool = False,
                        back_callback=None) -> None:
        """載入 HTML 至 EditWindow 並切換到編輯面板。

        back_callback: 編輯器「返回」按鈕的目標面板。預設回翻譯面板；
        從批次搜尋開啟時可傳入 `show_batch_panel` 以回到批次搜尋。
        """
        on_back = back_callback or self.show_translate_panel
        # 記錄最後開啟過的檔案路徑（供「📂 檔案列表」浮層列出相鄰檔案）
        if file_path and not is_temp_file:
            self._last_opened_file = file_path
            self.schedule_save()
            if (self._file_list_panel is not None
                    and self._file_list_panel.isVisible()):
                self._refresh_file_list_panel()
        # 第一次建立 EditWindow
        if self._edit_window is None:
            self._edit_window = EditWindow(
                file_path,
                scroll_to_line=scroll_to_line,
                original_text=original_text,
                display_title=display_title,
                is_temp_file=is_temp_file,
                glossary_provider=self._translate_panel.get_combined_glossary,
                glossary_saver=lambda o, t: self._save_glossary_entry(
                    o, t, also_batch_quick=self._glossary_sync_to_batch_quick),
                extract_regex_provider=lambda: (
                    self._active_base_regex(),
                    self.current_invalid_regex,
                    self.current_symbol_regex,
                    self._translate_panel.get_filter_text(),
                    self._korean_mode,
                    self._experimental_extraction,
                ),
                extracted_provider=self._translate_panel.get_extracted_text,
                translation_provider=self._translate_panel.get_ai_text,
                extracted_setter=lambda t: (
                    self._translate_panel.extracted_text.setPlainText(t)),
                translation_setter=lambda t: (
                    self._translate_panel.ai_text.setPlainText(t)),
                embed_font_provider=lambda: (
                    self._embed_font_name if self._embed_font_in_html else None),
                on_back=on_back,
                on_open=self.import_html,
                on_save=self._on_edit_saved,
                on_font_change=self._on_editor_font_changed,
                init_font_family=self._editor_font_family,
                init_font_size=self._editor_font_size,
                on_line_height_change=self._on_editor_line_height_changed,
                init_line_height=self._editor_line_height,
                get_last_dir=lambda: self._last_dir,
                on_dir_change=self._on_last_dir_changed,
                on_bg_change=self._on_editor_bg_changed,
                init_bg=self._editor_bg_color,
                init_side_panel_width=self._side_panel_width,
                init_side_auto_scroll=self._side_auto_scroll,
                on_side_state_change=self._on_side_state_changed,
                glossary_text_provider=self._translate_panel.get_glossary_text,
                glossary_text_setter=lambda t: (
                    self._translate_panel.glossary_text.setPlainText(t)),
                glossary_save=self.save_glossary_only,
                init_glossary_panel_width=self._glossary_panel_width,
                on_glossary_panel_width_change=(
                    self._on_glossary_panel_width_changed),
                init_pad_count=self._pad_space_count,
                on_pad_count_change=self._on_pad_count_changed,
                default_wysiwyg_provider=lambda: self._editor_default_wysiwyg,
                translation_only_provider=lambda: self._glossary_translation_only,
                pad_right_aa_provider=lambda: self._pad_right_aa,
                glossary_avoid_aa_provider=lambda: self._glossary_avoid_aa,
                glossary_kana_fold_provider=lambda: self._glossary_kana_fold,
                url_for_text_provider=self._find_url_for_text,
                reload_original_for_file=self.load_original_with_url_fallback,
                copy_to_replace_provider=lambda: self._editor_copy_to_replace,
                on_open_file_list=self.toggle_file_list_panel,
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
            self._edit_window._on_back = on_back
            self._edit_window._on_open = self.import_html
            self._edit_window._on_save = self._on_edit_saved
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
            # 若目前在 WYSIWYG 模式，先重新渲染預覽以反映新檔內容
            # （否則 preview_view 仍顯示前一份檔的內容，需手動切編輯模式再回來）。
            # 必須在 _scroll_to_line 之前執行，這樣後續捲動才能套用到全新的 preview。
            if self._edit_window._preview_active:
                self._edit_window._wysiwyg_rerender_after_editor_change()
            if scroll_to_line:
                self._edit_window._scroll_to_line(scroll_to_line)
            else:
                self._edit_window._scroll_to_top()

        # 若設定要求預設 WYSIWYG 而目前不在預覽模式，於此進入；
        # 進入後再次補上 scroll_to_line，讓批次搜尋的目標行也能正確捲到。
        if (self._editor_default_wysiwyg
                and not self._edit_window._preview_active):
            self._edit_window._toggle_preview()
            if scroll_to_line:
                self._edit_window._scroll_to_line(scroll_to_line)
            else:
                self._edit_window._scroll_to_top()

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
                on_add_to_glossary=self._save_glossary_entry,
                on_back=self.show_translate_panel,
                glossary_auto_search=self._glossary_auto_search,
            )
            self.stack.removeWidget(self._batch_placeholder)
            self.stack.insertWidget(2, self._batch_window)

        self._nav_label.setText("批次搜尋")
        self._update_work_title("批次搜尋")
        self.stack.setCurrentIndex(2)
        self._nav_bar.show()
        self._nav_conn_btn.hide()
        self._nav_history_btn.hide()
        self._action_bar.hide()

    def _on_batch_open_file(self, file_path: str, line: int, folder: str) -> None:
        self._batch_folder = folder
        entry = self._load_cache_entry_for_file(file_path)
        cached_original = entry['text'] if entry else None
        if entry:
            cached_ext = entry.get('extracted', '')
            if cached_ext:
                self._translate_panel.extracted_text.setPlainText(cached_ext)
            cached_tl = entry.get('translation', '')
            if cached_tl:
                self._translate_panel.ai_text.setPlainText(cached_tl)
        display_title = os.path.splitext(os.path.basename(file_path))[0]
        self.show_edit_panel(file_path, scroll_to_line=line,
                             original_text=cached_original,
                             display_title=display_title,
                             back_callback=self.show_batch_panel)

    def _on_batch_folder_change(self, folder: str) -> None:
        self._batch_folder = folder
        self.schedule_save()

    # ════════════════════════════════════════════════════════════
    #  術語存入
    # ════════════════════════════════════════════════════════════

    def _save_glossary_entry(self, original: str, translation: str,
                             *, also_batch_quick: bool = False) -> None:
        """由 EditWindow callback 呼叫，將術語存入一般術語表。

        若 original/translation 含外圍空白（例如 ` Trooper ` → `Trooper`），
        以 backtick 編碼寫入，下次解析時可被 `decode_glossary_term` 正確還原。

        `also_batch_quick=True`（編輯器全文替換存入術語、且開啟對應設定時傳入）：
        同步把該術語加入批次搜尋「快速替換」面板（批次面板未開啟則略過）。
        批次搜尋雙擊「加入術語」走的是預設 False，不會反向同步回自己。
        """
        if not original or not translation:
            return
        existing = self._translate_panel.get_glossary_text().strip('\n')
        entry = f"{encode_glossary_term(original)}={encode_glossary_term(translation)}"
        # 新條目放到最上面（最下面為較舊條目）
        g_text = f"{entry}\n{existing}" if existing else entry
        self._translate_panel.glossary_text.setPlainText(g_text)
        self.schedule_save()
        suffix = ""
        if self._glossary_auto_persist:
            try:
                self.save_glossary_only()
                suffix = "（已寫入設定檔）"
            except Exception as e:
                suffix = f"（⚠️ 設定檔寫入失敗：{e}）"
        if also_batch_quick and self._batch_window is not None:
            self._batch_window.add_glossary_quick_entry(original, translation)
            suffix += "（並加入批次快速替換）"
        self.show_status(
            f"📖 已存入術語：{original} → {translation}{suffix}", "#17a2b8")

    # ════════════════════════════════════════════════════════════
    #  提取 / 翻譯
    # ════════════════════════════════════════════════════════════

    def _active_base_regex(self) -> str:
        """韓文模式啟用時改用韓文字元集，否則使用 AA_Settings.json 的 base_regex。"""
        return DEFAULT_BASE_REGEX_KO if self._korean_mode else self.current_base_regex

    def extract_text(self) -> None:
        source = self._translate_panel.get_source_text()
        if not source.strip():
            self.show_status("⚠️ 請先貼上原始文本！", "#f39c12")
            return
        self.save_cache()
        filter_text = self._translate_panel.get_filter_text().strip()
        extracted_list = _extract_text(
            source,
            self._active_base_regex(),
            self.current_invalid_regex,
            self.current_symbol_regex,
            filter_text,
            skip_title=self._last_fetched_title,
            author_name=self._author_name,
            korean_mode=self._korean_mode,
            experimental=self._experimental_extraction,
            work_title=self._translate_panel.get_doc_title().strip(),
        )
        single_kana_list = _extract_single_kana(source, filter_text)
        seen = set(extracted_list)
        for item in single_kana_list:
            if item not in seen:
                extracted_list.append(item)
                seen.add(item)
        if self._glossary_skip_extract:
            glossary_keys = set(parse_glossary(
                self._translate_panel.get_combined_glossary(),
                kana_fold=self._glossary_kana_fold).keys())
            if glossary_keys:
                extracted_list = [
                    item for item in extracted_list
                    if item[0] not in glossary_keys]
        output = format_extraction_output(extracted_list)
        self._translate_panel.extracted_text.setPlainText(output)
        self._translate_panel.ext_count_label.setText(
            f"(共提取 {len(extracted_list)} 行)")
        if self._auto_copy:
            QApplication.clipboard().setText(output.strip())
            self.show_status(f"✅ 已提取 {len(extracted_list)} 行並複製到剪貼簿", "#0f0")

    def copy_split(self, half: str) -> None:
        ext_text = self._translate_panel.get_extracted_text().strip()
        if not ext_text:
            self.show_status("⚠️ 目前沒有可複製的提取結果", "#f39c12")
            return
        lines = [l for l in ext_text.split('\n') if l.strip()]
        if not lines:
            self.show_status("⚠️ 目前沒有可複製的提取結果", "#f39c12")
            return
        if half == 'all':
            text = '\n'.join(lines)
            label = "全部"
        else:
            split_idx = int(math.ceil(len(lines) / 2))
            text = '\n'.join(lines[:split_idx] if half == 'top'
                             else lines[split_idx:])
            label = "上半" if half == 'top' else "下半"
        QApplication.clipboard().setText(text)
        copied = len(text.split('\n')) if text else 0
        self.show_status(f"✅ 已複製{label}（{copied} 行）到剪貼簿", "#0f0")

    def copy_range(self) -> None:
        """依原文行號範圍複製提取結果。

        提取結果每行為 `行號-序號|文字`（見 text_extraction.format_extraction_output），
        取 `-` 前的行號判斷是否落在使用者輸入的 `起~迄` 範圍內（含頭尾）。
        """
        ext_text = self._translate_panel.get_extracted_text().strip()
        if not ext_text:
            self.show_status("⚠️ 目前沒有可複製的提取結果", "#f39c12")
            return
        text_in, ok = QInputDialog.getText(
            self, "複製指定範圍", "輸入原文行號範圍（例：1~3000）：",
            text=self._copy_range_last)
        if not ok:
            return
        raw = text_in.strip().replace('～', '~').replace('－', '-')
        m = _re_mod.match(r'^(\d+)\s*[~\-]\s*(\d+)$', raw)
        if not m:
            self.show_status("⚠️ 範圍格式錯誤，請輸入如 1~3000", "#f39c12")
            return
        start, end = int(m.group(1)), int(m.group(2))
        if start > end:
            start, end = end, start
        self._copy_range_last = raw
        id_re = _re_mod.compile(r'^\s*(\d+)-\d+\|')
        picked: list[str] = []
        for line in ext_text.split('\n'):
            mm = id_re.match(line)
            if mm and start <= int(mm.group(1)) <= end:
                picked.append(line)
        if not picked:
            self.show_status(f"⚠️ {start}~{end} 範圍內沒有提取結果", "#f39c12")
            return
        QApplication.clipboard().setText('\n'.join(picked))
        self.show_status(
            f"✅ 已複製 {start}~{end} 行範圍（{len(picked)} 行）到剪貼簿", "#0f0")

    def add_selection_to_filter(self) -> None:
        p = self._translate_panel
        cursor = p.extracted_text.textCursor()
        selected = cursor.selectedText()
        if not selected.strip():
            self.show_status("⚠️ 請先選取要加入過濾器的文字", "#f39c12")
            return
        selected = selected.replace('\u2029', '\n')
        id_prefix_re = _re_mod.compile(r'^\s*\d{2,5}-\d+\|')
        new_lines: list[str] = []
        for raw in selected.split('\n'):
            stripped = id_prefix_re.sub('', raw).strip()
            if stripped:
                new_lines.append(stripped)
        if not new_lines:
            self.show_status("⚠️ 選取內容無可加入的文字", "#f39c12")
            return
        existing = p.get_filter_text().rstrip('\n')
        existing_set = {l.strip() for l in existing.split('\n') if l.strip()}
        added = [l for l in new_lines if l not in existing_set]
        if not added:
            self.show_status("ℹ️ 選取內容已存在於過濾器中", "#17a2b8")
            return
        combined = (existing + '\n' if existing else '') + '\n'.join(added)
        p.filter_text.setPlainText(combined)
        self.show_status(f"✅ 已加入 {len(added)} 行至自訂過濾規則", "#0f0")

    def validate_ai_text(self) -> None:
        ai_content = self._translate_panel.get_ai_text().strip()
        lbl = self._translate_panel._ai_warn_label
        if not ai_content:
            lbl.setText("")
        else:
            warnings = _validate_ai_text(ai_content)
            if warnings:
                lbl.setText("  ".join(warnings))
                lbl.setStyleSheet("color:#ff4444;")
            else:
                lbl.setText("✅ 格式正確")
                lbl.setStyleSheet("color:#28a745;")
                QTimer.singleShot(3000, lambda: lbl.setText(""))
        self._update_ai_match_label()

    def _update_ai_match_label(self) -> None:
        """以提取結果與貼入翻譯的 ID 交集計算對應率，低於 50% 時在
        填入翻譯區塊右側以橘色提示。≥50% 或任一邊為空時清空標籤。
        """
        match_lbl = self._translate_panel._ai_match_label
        extracted = self._translate_panel.get_extracted_text()
        translated = self._translate_panel.get_ai_text()
        extracted_ids = {
            line.split('|', 1)[0].strip()
            for line in extracted.split('\n') if '|' in line
        }
        translated_ids = {
            line.split('|', 1)[0].strip()
            for line in translated.split('\n') if '|' in line
        }
        if not extracted_ids or not translated_ids:
            match_lbl.setText("")
            return
        ratio = len(extracted_ids & translated_ids) / len(extracted_ids)
        if ratio < 0.50:
            match_lbl.setText(
                f"⚠️ 原文跟翻譯可能不對應（對應率 {ratio:.0%}）")
            match_lbl.setStyleSheet("color:#f39c12;")
        else:
            match_lbl.setText("")

    def check_chapter_number(self) -> None:
        if self._fetch_auto_fill_title:
            return
        text = self._translate_panel.source_text.toPlainText()[:200]
        result = _check_chapter_number(text)
        if result is not None:
            self._translate_panel.doc_num.setText(str(result))

    def _prepare_translation(self, append_mode: bool = False) -> tuple[str, str, str, str] | None:
        """共用的翻譯前處理：驗證輸入、執行替換、回傳 (result_text, source,
        name_base, display_title)；失敗時回傳 None 並已顯示 toast。

        append_mode=True 時，將翻譯文附加在原文之後（不取代原文）。
        """
        if self.stack.currentIndex() != 0:
            return None
        source = self._translate_panel.get_source_text()
        extracted = self._translate_panel.get_extracted_text()
        translated = self._translate_panel.get_ai_text()
        if not source.strip() or not extracted.strip() or not translated.strip():
            self.show_status(
                "⚠️ 請確保原始文本、提取結果和翻譯結果都有內容！", "#f39c12")
            return None
        self.save_cache()
        self._record_work_history()
        # 覆蓋率檢查改為「貼入翻譯」即時顯示於填入翻譯區塊右側
        # （見 _update_ai_match_label），這裡不再跳 Toast。
        glossary = parse_glossary(
            self._translate_panel.get_combined_glossary(),
            kana_fold=self._glossary_kana_fold)
        result_text = _apply_translation(
            source, extracted, translated, glossary,
            append_mode=append_mode,
            translation_only=self._glossary_translation_only,
            pad_right_aa=self._pad_right_aa,
            symbol_regex_str=self.current_symbol_regex,
            glossary_avoid_aa=self._glossary_avoid_aa)
        title = self._translate_panel.get_doc_title().strip() or "未命名"
        num = self._translate_panel.get_doc_num().strip()
        safe_title = _re_mod.sub(r'[\\/:*?"<>|]', '_', title)
        safe_num = _re_mod.sub(r'[\\/:*?"<>|]', '_', num)
        name_base = f"{safe_title}_{safe_num}" if safe_num else safe_title
        display_title = f"{title}_{num}" if num else title
        return result_text, source, name_base, display_title

    def apply_translation(self, append_mode: bool = False) -> None:
        prepared = self._prepare_translation(append_mode=append_mode)
        if prepared is None:
            return
        result_text, source, name_base, display_title = prepared
        tmp = os.path.join(tempfile.gettempdir(), f"{name_base}.html")
        try:
            write_html_file(tmp, result_text)
        except OSError as e:
            self.show_status(f"❌ 寫入暫存失敗: {e}", "#dc3545")
            return
        # 立即暫存原文，避免使用者之後不經編輯器儲存流程時 cache 缺漏
        self.save_original_for_file(
            tmp, source.rstrip('\n'),
            extracted=self._translate_panel.get_extracted_text(),
            translation=self._translate_panel.get_ai_text(),
        )
        self.show_edit_panel(
            tmp,
            original_text=source.rstrip('\n'),
            display_title=display_title,
            is_temp_file=True,
        )

    def apply_translation_append(self, _checked: bool = False) -> None:
        """『加入翻譯並編輯』：將翻譯文附加在原文之後（不取代）後進入編輯器。"""
        self.apply_translation(append_mode=True)

    # ── 內嵌字型 (settings) → write_html_file 參數的共用解析 ──
    _EMBED_FONT_MAP: dict[str, tuple[str, str]] = {
        "monapo":    ("monapo.ttf",    "Monapo"),
        "Saitamaar": ("Saitamaar.ttf", "Saitamaar"),
        "textar":    ("textar.ttf",    "textar"),
    }

    def _resolved_embed_font(self) -> tuple[str | None, str | None]:
        """依設定回傳 (font_path, font_family)；未啟用或字型檔不存在時回 (None, None)。

        ⚠️ 僅供「另存新檔」流程使用（翻譯並直接儲存）。
        覆蓋舊檔的路徑（Ctrl+S、批次搜尋）**不該**呼叫此方法。
        """
        if not self._embed_font_in_html:
            return None, None
        fn, fam = self._EMBED_FONT_MAP.get(
            self._embed_font_name, ("monapo.ttf", "Monapo"))
        fonts_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fonts")
        cand = os.path.join(fonts_dir, fn)
        if not os.path.exists(cand):
            return None, None
        return cand, fam

    def apply_translation_and_save(self) -> None:
        """執行翻譯替換後，直接透過 QFileDialog 詢問路徑並存檔，
        不進入編輯器。"""
        prepared = self._prepare_translation()
        if prepared is None:
            return
        result_text, source, name_base, _display_title = prepared
        default_dir = self._last_dir or os.getcwd()
        default_path = os.path.join(default_dir, f"{name_base}.html")
        file_path, _ = QFileDialog.getSaveFileName(
            self, "翻譯並直接儲存 — 選擇存檔位置",
            default_path,
            "HTML files (*.html);;All files (*.*)")
        if not file_path:
            return
        if not file_path.lower().endswith('.html'):
            file_path += '.html'
        # 直接存檔路徑也要遵循「設定 → 內嵌字型」（與編輯器存檔對齊）
        embed_font_path, embed_font_family = self._resolved_embed_font()
        try:
            write_html_file(
                file_path, result_text,
                embed_font_path=embed_font_path,
                embed_font_family=embed_font_family,
            )
        except OSError as e:
            self.show_status(f"❌ 寫入失敗: {e}", "#dc3545")
            return
        # 直接儲存路徑不進編輯器，必須立刻把原文寫入 cache，
        # 否則之後從批次搜尋開啟時 Alt+2 比對原文會是空的。
        self.save_original_for_file(
            file_path, source.rstrip('\n'),
            extracted=self._translate_panel.get_extracted_text(),
            translation=self._translate_panel.get_ai_text(),
        )
        self._last_dir = os.path.dirname(file_path)
        self.schedule_save()
        self._stamp_current_url_history()
        self.show_status(
            f"✅ 已儲存至 {os.path.basename(file_path)}", "#28a745")

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
        entry = self._load_cache_entry_for_file(file_path)
        cached_original = entry['text'] if entry else None
        if entry:
            cached_ext = entry.get('extracted', '')
            if cached_ext:
                self._translate_panel.extracted_text.setPlainText(cached_ext)
            cached_tl = entry.get('translation', '')
            if cached_tl:
                self._translate_panel.ai_text.setPlainText(cached_tl)
        self.show_edit_panel(
            file_path,
            original_text=cached_original,
            display_title=os.path.splitext(os.path.basename(file_path))[0],
            is_temp_file=False,
        )

    def analyze_extraction(self) -> None:
        """提取分析 (Debug)：開啟對話框，使用者在輸入框貼上並反白選取要分析的
        文字，按「分析選取文字」後在下方顯示分析報告。"""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowTitle("🔧 提取分析 (Debug)")
        dlg.resize(820, 680)
        vl = QVBoxLayout(dlg)

        hint = QLabel(
            "① 在下方輸入框貼上文字　② 反白選取要分析的文字　③ 按「分析選取文字」")
        vl.addWidget(hint)

        input_box = QTextEdit()
        input_box.setFont(_aa_font(13))
        input_box.setAcceptRichText(False)
        input_box.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        input_box.setPlaceholderText("在此貼上要分析的原始文本…")
        vl.addWidget(input_box, 1)

        btn_analyze = _make_btn(
            "分析選取文字", "#007bff", "#0056b3", width=140)
        vl.addWidget(btn_analyze)

        report_box = QTextEdit()
        report_box.setFont(_aa_font(13))
        report_box.setReadOnly(True)
        report_box.setPlaceholderText("分析結果會顯示在這裡…")
        vl.addWidget(report_box, 1)

        def _do_analyze() -> None:
            cursor = input_box.textCursor()
            sel = cursor.selectedText().replace('\u2029', '\n')
            if not sel.strip():
                report_box.setPlainText(
                    "⚠️ 請先在上方輸入框反白選取要分析的文字！")
                return
            report = _analyze_extraction(
                sel,
                self._active_base_regex(),
                self.current_invalid_regex,
                self.current_symbol_regex,
                self._translate_panel.get_filter_text().strip(),
                korean_mode=self._korean_mode,
                experimental=self._experimental_extraction,
            )
            report_box.setPlainText(report)

        btn_analyze.clicked.connect(_do_analyze)

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

    def _find_url_for_text(self, text: str) -> str | None:
        """以投稿標頭指紋查 url_history，回傳對應網址；找不到回 None。

        編輯器「複製網址」按鈕用此 callback：以當前編輯器全文計算指紋，
        在 url_history（含舊有但無 fingerprint 欄位的條目）中尋找匹配。
        """
        if not text:
            return None
        fp = self._compute_author_fingerprint(text)
        if not fp:
            return None
        for h in self.url_history:
            if not isinstance(h, dict):
                continue
            entry_fp = h.get('fingerprint')
            if not entry_fp:
                continue
            if entry_fp == fp:
                return h.get('url') or None
        return None

    def _update_work_title(self, work_title: str = "") -> None:
        self.setWindowTitle(f"{APP_TITLE} — {work_title}" if work_title else APP_TITLE)

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
    #  自動翻譯（連續多話，操控網頁版 Gemini）
    # ════════════════════════════════════════════════════════════

    def show_auto_translate_panel(self) -> None:
        """切換到自動翻譯面板（index 4）。"""
        if self._auto_window is None:
            self._auto_window = AutoTranslatePanel(self)
            self.stack.removeWidget(self._auto_placeholder)
            self.stack.insertWidget(4, self._auto_window)
        else:
            # 切回面板時若不在執行中，重新從目前狀態同步欄位（current_url 等可能變了）
            if not self._auto_translate_running:
                self._auto_window.refresh_from_main()
        self._auto_window.set_running(self._auto_translate_running)
        self._nav_label.setText("自動翻譯")
        self._update_work_title("自動翻譯")
        self.stack.setCurrentIndex(4)
        self._nav_bar.show()
        self._nav_conn_btn.show()
        self._nav_history_btn.show()
        self._action_bar.hide()

    def _toggle_auto_conn_panel(self) -> None:
        """導覽列「⚙ 連線設定」鈕：開合自動翻譯面板的連線設定浮層。"""
        if self._auto_window is not None:
            self._auto_window.toggle_conn_panel()

    def save_connection_settings(self, params: dict) -> None:
        """由連線設定分頁的「儲存」鈕進來：持久化後端／模型／系統指令與 API 金鑰。

        金鑰存進 DPAPI 加密檔（aa_api_keys.dat），其餘存進一般 cache。
        """
        self._translate_backend = params.get("backend", "browser") or "browser"
        self._api_provider = params.get("api_provider", "gemini") or "gemini"
        self._gemini_api_model = (
            params.get("api_model") or "gemini-2.5-pro")
        if "api_models" in params:
            am = params.get("api_models") or {}
            self._api_models = {str(k): str(v) for k, v in am.items()} \
                if isinstance(am, dict) else {}
        if "api_custom_base_url" in params:
            self._api_custom_base_url = params.get("api_custom_base_url", "") or ""
        if "api_timeout" in params:
            try:
                self._api_timeout = max(30, int(params.get("api_timeout") or 600))
            except (TypeError, ValueError):
                self._api_timeout = 600
        self._gemini_api_only_prompt = params.get("api_only_prompt", "")
        self._gemini_api_system_prompt = params.get("api_system_prompt", "")
        self._browser_use_gem = bool(params.get("browser_use_gem", True))
        # 已從主頁移入連線設定浮層的瀏覽器後端參數，一併持久化。
        if "gem_url" in params:
            self._gemini_gem_url = params.get("gem_url", "") or ""
        if "required_model" in params:
            self._gemini_required_model = params.get("required_model") or "pro"
        if "max_per_session" in params:
            self._gemini_max_per_session = max(
                1, int(params.get("max_per_session") or 3))
        self.save_cache()
        try:
            from aa_tool import secure_store
            base_dir = self._settings_base_dir
            # 各供應商金鑰一併覆寫（空供應商自動移除）
            if "provider_keys" in params:
                secure_store.save_all_keys(
                    base_dir, params.get("provider_keys") or {})
            elif "api_keys" in params:  # 相容舊呼叫形式
                secure_store.save_keys(base_dir, params.get("api_keys", []))
        except Exception as e:  # noqa: BLE001 — 金鑰寫入失敗回報但不崩潰
            self.show_status(f"⚠️ 金鑰儲存失敗：{e}", "#dc3545")

    def start_auto_translate_from_panel(self, params: dict) -> None:
        """從 AutoTranslatePanel 的「開始」按鈕進來。"""
        if self._auto_translate_running:
            self.show_status("⚠️ 自動翻譯正在執行中…", "#f39c12")
            return
        # 持久化到 cache（下次打開面板自動帶入）
        # 翻譯方式現於主頁，開始時即以面板選擇為準（不必先按「儲存連線設定」）
        if params.get("backend"):
            self._translate_backend = params["backend"]
        self._gemini_gem_url = params["gem_url"]
        self._auto_translate_out_dir = params["out_dir"]
        self._auto_translate_count = params["count"]
        self._auto_translate_until_last = params["until_last"]
        self._auto_translate_skip_existing = params.get("skip_existing", False)
        if "url_list" in params:
            self._auto_translate_url_list = '\n'.join(
                params.get("url_list") or [])
        self._auto_translate_append_mode = params.get("append_mode", False)
        self._gemini_max_per_session = params["max_per_session"]
        self._gemini_required_model = params["required_model"] or "pro"
        # 手動模式下也把使用者填的作品名稱同步回首頁（保持兩邊一致）
        try:
            if not self._fetch_auto_fill_title and params.get("doc_title"):
                self._translate_panel.doc_title.setText(params["doc_title"])
        except Exception:
            pass
        self.save_cache()
        self._start_auto_translate(
            params["start_url"], params["count"], params["out_dir"],
            params["gem_url"], params["until_last"],
            params["max_per_session"], params["required_model"],
            params.get("doc_title", ""),
            params.get("skip_existing", False),
            params.get("url_list") or [])

    def _start_auto_translate(self, start_url: str, count: int,
                               out_dir: str, gem_url: str,
                               until_last: bool,
                               max_per_session: int,
                               required_model: str = "",
                               doc_title: str = "",
                               skip_existing: bool = False,
                               url_list: list[str] | None = None) -> None:
        """在背景執行緒跑自動翻譯，進度同步至橫幅、狀態列與面板 Log。"""
        self._auto_translate_running = True
        self._auto_stop_event = threading.Event()
        self._auto_banner_stop_btn.setEnabled(True)
        self._auto_banner_stop_btn.setText("■ 停止")
        if self._translate_backend == "api":
            self._auto_banner_label.setText("⚡ 自動翻譯啟動中（API 模式）…")
        else:
            self._auto_banner_label.setText(
                "⚡ 自動翻譯啟動中（請在彈出的瀏覽器完成登入）…")
        self._auto_banner.show()
        if self._auto_window is not None:
            self._auto_window.set_running(True)
            self._auto_window.append_log(
                f"=== 啟動自動翻譯：count={count} until_last={until_last} "
                f"skip_existing={skip_existing} "
                f"url_list={len(url_list or [])} "
                f"max_per_session={max_per_session} ===")
        self.show_status("⏳ 自動翻譯啟動中…", "#17a2b8")

        def _progress(msg: str) -> None:
            def _apply(m=msg) -> None:
                # 橫幅：單行短訊
                short = m.strip().replace("\n", " ")
                if len(short) > 120:
                    short = short[:117] + "…"
                if self._auto_banner_label is not None:
                    self._auto_banner_label.setText(f"⚡ {short}")
                # 面板 Log：原文整段
                if self._auto_window is not None:
                    self._auto_window.append_log(m)
            self._invoke_on_main.emit(_apply)

        stop_event = self._auto_stop_event

        def _bg() -> None:
            from aa_auto_translate import run_auto_translate
            try:
                result = run_auto_translate(
                    start_url, count, out_dir,
                    base_dir=self._settings_base_dir,
                    backend=self._translate_backend,
                    append_mode=self._auto_translate_append_mode,
                    gem_url=gem_url,
                    profile_dir=self._gemini_profile_dir or None,
                    max_per_session=max_per_session,
                    required_model=required_model,
                    doc_title=doc_title,
                    fetch_auto_fill_title=self._fetch_auto_fill_title,
                    until_last=until_last,
                    skip_existing=skip_existing,
                    url_list=url_list,
                    stop_event=stop_event,
                    progress=_progress,
                    print_summary=False)  # GUI 端自己印更完整的總結
            except Exception as e:  # noqa: BLE001 — 背景執行緒須吞例外回報 UI
                self._invoke_on_main.emit(
                    lambda err=e: self._auto_translate_done(None, str(err)))
                return
            self._invoke_on_main.emit(
                lambda r=result: self._auto_translate_done(r, None))

        threading.Thread(target=_bg, daemon=True).start()

    def _stop_auto_translate(self) -> None:
        """由橫幅停止鈕觸發；通知背景執行緒結束。"""
        if not self._auto_translate_running:
            return
        if self._auto_stop_event is not None:
            self._auto_stop_event.set()
        self._auto_banner_stop_btn.setEnabled(False)
        self._auto_banner_stop_btn.setText("停止中…")
        self._auto_banner_label.setText("⏹️ 停止指令已送出，等待當前動作結束…")

    def _auto_translate_done(self, result, error: 'str | None') -> None:
        self._auto_translate_running = False
        self._auto_stop_event = None
        self._auto_banner.hide()
        if self._auto_window is not None:
            self._auto_window.set_running(False)
        if error is not None:
            if self._auto_window is not None:
                self._auto_window.append_log(f"❌ 自動翻譯失敗：{error}")
            self.show_status(f"❌ 自動翻譯失敗：{error}", "#dc3545")
            QMessageBox.critical(self, "自動翻譯失敗", error)
            return
        # 把接續網址回填到「起始網址」，方便直接接續：
        #   pending_url＝停止／暫停／中止時未完成的話；next_url＝跑滿話數後的下一話。
        fill_url = (getattr(result, "pending_url", "")
                    or getattr(result, "next_url", ""))
        if self._auto_window is not None and fill_url:
            self._auto_window.set_start_url(fill_url)
        lines = [f"成功翻譯 {len(result.done)} 話。"]
        for p in result.done:
            lines.append(f"  ✅ {p}")
        if result.failed:
            lines.append(f"失敗／跳過 {len(result.failed)} 話：")
            lines += [f"  ❌ {u} — {why}" for u, why in result.failed]
        skipped = getattr(result, "skipped", [])
        if skipped:
            lines.append(f"跳過（已存在同名檔）{len(skipped)} 話：")
            lines += [f"  ⏭️ {u} — {fn}" for u, fn in skipped]
        if result.reached_end:
            lines.append("")
            lines.append("🏁 已翻到最後一話。")
        if getattr(result, "next_url", ""):
            lines.append("")
            lines.append("▶ 已達設定話數；下一話網址已帶入「起始網址」，可直接按開始接續：")
            lines.append(result.next_url)
        if result.quota_paused:
            lines.append("")
            lines.append("⏸️ 因撞到 Gemini 額度上限而暫停。")
            lines.append("待額度恢復後，用下列網址當起始網址接續：")
            lines.append(result.pending_url)
        if result.stopped:
            lines.append("")
            lines.append("⏹️ 已手動停止。")
            if result.pending_url:
                lines.append("要接續，用下列網址當起始網址：")
                lines.append(result.pending_url)
        if result.model_mismatch:
            lines.append("")
            lines.append("🛑 偵測到 Gemini 模型與要求不符，已中止整批。")
            lines.append("請在瀏覽器切換到正確模型後重跑。")
        ok = (not result.failed and not result.quota_paused
              and not result.stopped and not result.model_mismatch)
        if result.model_mismatch:
            color = "#dc3545"
            head = "🛑 模型不符已中止"
        elif result.stopped:
            color = "#6c757d"
            head = "⏹️ 已停止"
        elif ok:
            color = "#28a745"
            head = "✅ 自動翻譯完成"
        else:
            color = "#f39c12"
            head = "⚠️ 自動翻譯結束（含失敗或暫停）"
        self.show_status(f"{head}：成功 {len(result.done)} 話", color)
        summary = "\n".join(lines)
        if self._auto_window is not None:
            self._auto_window.append_log("──────── 總結 ────────")
            self._auto_window.append_log(summary)
        QMessageBox.information(self, "自動翻譯完成", summary)

    # ════════════════════════════════════════════════════════════
    #  Wiki 角色日中對照抓取（非 modal QDialog）
    # ════════════════════════════════════════════════════════════

    def open_wiki_name_dialog(self) -> None:
        from aa_wiki_name_dialog_qt import WikiNameDialog
        if getattr(self, "_wiki_dialog", None) is None:
            self._wiki_dialog = WikiNameDialog(self)
        self._wiki_dialog.show()
        self._wiki_dialog.raise_()
        self._wiki_dialog.activateWindow()

    # ════════════════════════════════════════════════════════════
    #  URL 抓取（in-process UrlFetchWindow，嵌入 stack index 3）
    # ════════════════════════════════════════════════════════════

    def open_url_fetch_qt(self) -> None:
        from aa_url_fetch_qt import UrlFetchWindow
        self._url_fetch_from_auto = False  # 預設來自首頁；自動翻譯入口會於呼叫後改 True
        if self._url_fetch_win is None:
            self._url_fetch_win = UrlFetchWindow(self)
            self.stack.removeWidget(self._url_fetch_placeholder)
            self.stack.insertWidget(3, self._url_fetch_win)
        self._url_fetch_win.sync_state(
            url_history=self.url_history,
            url_related_links=self.url_related_links,
            current_url=self.current_url,
            author_only=self._author_only,
            author_name=self._author_name,
            initial_url=self.current_url,
        )
        self._nav_label.setText("網址讀取")
        self._update_work_title("網址讀取")
        self.stack.setCurrentIndex(3)
        self._nav_bar.show()
        self._nav_conn_btn.hide()
        self._nav_history_btn.hide()
        self._action_bar.hide()

    def _url_fetch_win_visible(self) -> bool:
        return self._url_fetch_win is not None

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
                # 連線層失敗時 url_fetcher 會附診斷（見 SPEC §4.5）；取第一行
                # 「結論」附在訊息後，讓使用者不必看 Log 也知道原因。
                _diag = getattr(ex, "diagnosis", None)
                if _diag:
                    err += chr(10) + _diag[0]
                self._invoke_on_main.emit(
                    lambda: self._url_fetch_win.on_fetch_done(
                        success=False,
                        status_message=f"❌ 讀取失敗: {err}",
                        status_color='#dc3545',
                    ) if self._url_fetch_win_visible() else None)
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
                    lambda m=msg, cc=c: self._url_fetch_win.on_fetch_done(
                        success=False, status_message=m, status_color=cc,
                    ) if self._url_fetch_win_visible() else None)
                return

            def _apply() -> None:
                display_title = _extract_work_title(page_title) if page_title else ""
                full_text = (display_title + "\n\n" + text_content
                             if display_title else text_content)
                self._translate_panel.source_text.setPlainText(full_text)
                if self._fetch_clear_ai_text:
                    self._translate_panel.ai_text.clear()
                if self._fetch_auto_fill_title:
                    self._translate_panel.doc_num.clear()
                else:
                    QTimer.singleShot(50, self.check_chapter_number)
                self._update_work_title(display_title)
                if display_title and self._fetch_auto_fill_title:
                    self._translate_panel.doc_title.setText(display_title)
                self._last_fetched_title = display_title
                self.url_related_links = nav_links
                self.current_url = raw_url
                _old = next((h for h in self.url_history if h.get('url') == raw_url), {})
                hist = {'url': raw_url, 'title': page_title or raw_url}
                if _old.get('work_title'):
                    hist['work_title'] = _old['work_title']
                if _old.get('author'):
                    hist['author'] = _old['author']
                fp = self._compute_author_fingerprint(text_content)
                if fp:
                    hist['fingerprint'] = fp
                elif _old.get('fingerprint'):
                    hist['fingerprint'] = _old['fingerprint']
                # 持久化：鎖定 append（多程序安全）+ per-URL 相關連結
                self.settings_mgr.append_url_history(hist)
                self.settings_mgr.update_url_related_links(raw_url, nav_links)
                # 同步 in-memory（newest-last 慣例）
                self.url_history = [h for h in self.url_history
                                    if h.get('url') != raw_url]
                self.url_history.append(hist)
                # 套用戳印 meta（若有）：work_title → doc_title、author → 作者欄
                self._apply_url_history_meta(raw_url)
                self.schedule_save()
                line_count = text_content.count('\n') + 1
                self.show_status(f"✅ 網址讀取成功！共 {line_count} 行", "#0f0")
                if self._url_fetch_win_visible():
                    self._url_fetch_win.on_fetch_done(
                        success=True,
                        status_message=f"✅ 讀取成功！共 {line_count} 行",
                        status_color='#28a745',
                        url_history=self.url_history,
                        url_related_links=self.url_related_links,
                        current_url=self.current_url,
                        auto_close=True,
                    )
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
                    if self._fetch_clear_ai_text:
                        self._translate_panel.ai_text.clear()
                    if self._fetch_auto_fill_title:
                        self._translate_panel.doc_num.clear()
                    else:
                        QTimer.singleShot(50, self.check_chapter_number)
                    self._update_work_title(display_title)
                    if display_title and self._fetch_auto_fill_title:
                        self._translate_panel.doc_title.setText(display_title)
                    self._last_fetched_title = display_title
                    self.url_related_links = nav_links
                    self.current_url = next_url
                    _old = next((h for h in self.url_history if h.get('url') == next_url), {})
                    hist = {'url': next_url, 'title': page_title or next_url}
                    if _old.get('work_title'):
                        hist['work_title'] = _old['work_title']
                    if _old.get('author'):
                        hist['author'] = _old['author']
                    fp = self._compute_author_fingerprint(text_content)
                    if fp:
                        hist['fingerprint'] = fp
                    elif _old.get('fingerprint'):
                        hist['fingerprint'] = _old['fingerprint']
                    # 持久化：鎖定 append（多程序安全）+ per-URL 相關連結
                    self.settings_mgr.append_url_history(hist)
                    self.settings_mgr.update_url_related_links(next_url, nav_links)
                    # 同步 in-memory（newest-last）
                    self.url_history = [h for h in self.url_history
                                        if h.get('url') != next_url]
                    self.url_history.append(hist)
                    # 套用戳印 meta（若有）：work_title → doc_title、author → 作者欄
                    self._apply_url_history_meta(next_url)
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
            auto_copy=self._auto_copy,
            batch_folder=self._batch_folder,
            author_name=self._author_name,
            author_only=self._author_only,
            work_history=list(self.work_history),
            editor_font_family=self._editor_font_family,
            editor_font_size=self._editor_font_size,
            editor_line_height=self._editor_line_height,
            last_open_dir=self._last_dir,
            last_opened_file=self._last_opened_file,
            editor_bg_color=self._editor_bg_color,
            work_history_limit=self._work_history_limit,
            fetch_history_limit=self._fetch_history_limit,
            original_cache_limit=self._original_cache_limit,
            glossary_auto_search=self._glossary_auto_search,
            diff_save_mode=self._diff_save_mode,
            embed_font_in_html=self._embed_font_in_html,
            embed_font_name=self._embed_font_name,
            editor_default_wysiwyg=self._editor_default_wysiwyg,
            editor_copy_to_replace=self._editor_copy_to_replace,
            glossary_sync_to_batch_quick=self._glossary_sync_to_batch_quick,
            side_panel_width=self._side_panel_width,
            side_auto_scroll=self._side_auto_scroll,
            glossary_panel_width=self._glossary_panel_width,
            korean_mode=self._korean_mode,
            experimental_extraction=self._experimental_extraction,
            pad_right_aa=self._pad_right_aa,
            glossary_avoid_aa=self._glossary_avoid_aa,
            glossary_kana_fold=self._glossary_kana_fold,
            glossary_skip_extract=self._glossary_skip_extract,
            glossary_auto_persist=self._glossary_auto_persist,
            glossary_translation_only=self._glossary_translation_only,
            pad_space_count=self._pad_space_count,
            fetch_auto_fill_title=self._fetch_auto_fill_title,
            fetch_clear_ai_text=self._fetch_clear_ai_text,
            fetch_proxy_url=self._fetch_proxy_url,
            api_proxy_url=self._api_proxy_url,
            gemini_gem_url=self._gemini_gem_url,
            gemini_profile_dir=self._gemini_profile_dir,
            gemini_max_per_session=self._gemini_max_per_session,
            gemini_required_model=self._gemini_required_model,
            gemini_selectors=self._gemini_selectors,
            auto_translate_out_dir=self._auto_translate_out_dir,
            auto_translate_url_list=self._auto_translate_url_list,
            auto_translate_count=self._auto_translate_count,
            auto_translate_until_last=self._auto_translate_until_last,
            auto_translate_skip_existing=self._auto_translate_skip_existing,
            auto_translate_append_mode=self._auto_translate_append_mode,
            translate_backend=self._translate_backend,
            api_provider=self._api_provider,
            gemini_api_model=self._gemini_api_model,
            api_models=self._api_models,
            api_custom_base_url=self._api_custom_base_url,
            api_timeout=self._api_timeout,
            gemini_api_only_prompt=self._gemini_api_only_prompt,
            gemini_api_system_prompt=self._gemini_api_system_prompt,
            browser_use_gem=self._browser_use_gem,
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
        self._auto_copy = bool(cache.auto_copy)
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
        if cache.editor_line_height:
            self._editor_line_height = int(cache.editor_line_height)
        if cache.last_open_dir and os.path.isdir(cache.last_open_dir):
            self._last_dir = cache.last_open_dir
        self._last_opened_file = str(cache.last_opened_file or "")
        if cache.editor_bg_color:
            self._editor_bg_color = cache.editor_bg_color
        self._work_history_limit = max(1, int(cache.work_history_limit or 10))
        self._fetch_history_limit = max(1, int(cache.fetch_history_limit or 50))
        self._original_cache_limit = max(1, int(
            cache.original_cache_limit or self._fetch_history_limit))
        self._glossary_auto_search = bool(cache.glossary_auto_search)
        self._diff_save_mode = bool(cache.diff_save_mode)
        self._embed_font_in_html = bool(cache.embed_font_in_html)
        self._embed_font_name = str(cache.embed_font_name or "monapo")
        self._editor_default_wysiwyg = bool(cache.editor_default_wysiwyg)
        self._editor_copy_to_replace = bool(cache.editor_copy_to_replace)
        self._glossary_sync_to_batch_quick = bool(
            cache.glossary_sync_to_batch_quick)
        try:
            self._side_panel_width = int(cache.side_panel_width or 0)
        except (TypeError, ValueError):
            self._side_panel_width = 0
        try:
            self._glossary_panel_width = int(cache.glossary_panel_width or 0)
        except (TypeError, ValueError):
            self._glossary_panel_width = 0
        self._side_auto_scroll = bool(cache.side_auto_scroll)
        self._korean_mode = bool(cache.korean_mode)
        self._experimental_extraction = bool(cache.experimental_extraction)
        self._pad_right_aa = bool(cache.pad_right_aa)
        self._glossary_avoid_aa = bool(cache.glossary_avoid_aa)
        self._glossary_kana_fold = bool(cache.glossary_kana_fold)
        self._glossary_skip_extract = bool(cache.glossary_skip_extract)
        self._glossary_auto_persist = bool(cache.glossary_auto_persist)
        self._glossary_translation_only = bool(cache.glossary_translation_only)
        self._fetch_auto_fill_title = bool(cache.fetch_auto_fill_title)
        self._fetch_clear_ai_text = bool(
            getattr(cache, "fetch_clear_ai_text", False))
        self._fetch_proxy_url = str(getattr(cache, "fetch_proxy_url", "") or "")
        self._api_proxy_url = str(getattr(cache, "api_proxy_url", "") or "")
        # 抓網頁代理是 url_fetcher 的模組狀態，載入設定後立即套用
        _url_fetcher.set_fetch_proxy(self._fetch_proxy_url)
        cb = getattr(p, "clear_ai_cb", None)
        if cb is not None:
            cb.blockSignals(True)
            cb.setChecked(self._fetch_clear_ai_text)
            cb.blockSignals(False)
        self._gemini_gem_url = str(cache.gemini_gem_url or "")
        self._gemini_profile_dir = str(cache.gemini_profile_dir or "")
        self._gemini_max_per_session = max(1, int(
            cache.gemini_max_per_session or 3))
        self._gemini_required_model = (
            str(cache.gemini_required_model or "pro").lower() or "pro")
        self._gemini_selectors = dict(cache.gemini_selectors or {})
        self._auto_translate_out_dir = str(cache.auto_translate_out_dir or "")
        self._auto_translate_url_list = str(
            getattr(cache, "auto_translate_url_list", "") or "")
        try:
            self._auto_translate_count = max(1, int(
                cache.auto_translate_count or 5))
        except (TypeError, ValueError):
            self._auto_translate_count = 5
        self._auto_translate_until_last = bool(cache.auto_translate_until_last)
        self._auto_translate_skip_existing = bool(
            cache.auto_translate_skip_existing)
        self._auto_translate_append_mode = bool(
            getattr(cache, "auto_translate_append_mode", False))
        self._translate_backend = str(cache.translate_backend or "browser")
        self._api_provider = str(getattr(cache, "api_provider", "gemini") or "gemini")
        self._gemini_api_model = str(
            cache.gemini_api_model or "gemini-2.5-pro")
        am = getattr(cache, "api_models", {})
        self._api_models = {str(k): str(v) for k, v in am.items()} \
            if isinstance(am, dict) else {}
        self._api_custom_base_url = str(
            getattr(cache, "api_custom_base_url", "") or "")
        try:
            self._api_timeout = max(30, int(getattr(cache, "api_timeout", 600) or 600))
        except (TypeError, ValueError):
            self._api_timeout = 600
        self._gemini_api_only_prompt = str(
            getattr(cache, "gemini_api_only_prompt", "") or "")
        self._gemini_api_system_prompt = str(
            cache.gemini_api_system_prompt or "")
        self._browser_use_gem = bool(cache.browser_use_gem)
        try:
            v = int(cache.pad_space_count)
            self._pad_space_count = v if v in (1, 2, 3) else 2
        except (TypeError, ValueError):
            self._pad_space_count = 2

    def save_cache(self) -> None:
        self.settings_mgr.save_cache(self._gather_cache())

    def load_cache(self) -> None:
        cache = self.settings_mgr.load_cache()
        self._apply_cache(cache)
        self._apply_doc_num_state()

    def toggle_settings_panel(self) -> None:
        """⚙ 設定鈕：開合設定浮層（比照自動翻譯的連線設定浮層）。

        每次開啟都以目前狀態重建內容，避免顯示過時數值；關閉只是 hide。
        """
        if (self._settings_panel is not None
                and self._settings_panel.isVisible()):
            self._settings_panel.hide()
            return
        self._build_settings_content()
        self._position_settings_panel()
        self._settings_panel.show()
        self._settings_panel.raise_()
        # 把焦點移進浮層，讓 ESC 一打開就能關閉（⚙ 鈕在浮層子樹之外）
        self._settings_panel.setFocus()

    def _build_settings_content(self) -> None:
        """（重）建設定浮層內容；以目前狀態填入各欄位。"""
        from aa_settings_dialog_qt import SettingsDialog
        central = self.centralWidget()
        if self._settings_panel is None:
            panel = QWidget(central)
            panel.setObjectName("settingsPanel")
            panel.setStyleSheet(
                "#settingsPanel { background:#f1f3f5; border:1px solid #adb5bd;"
                " border-radius:6px; }")
            panel.hide()
            # 可接收焦點，配合下方 ESC 快捷鍵：開啟時 setFocus 進浮層即可按 ESC 關閉
            panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            outer = QVBoxLayout(panel)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet(
                "QScrollArea { border:none; background:transparent; }")
            outer.addWidget(scroll, 1)
            # ESC 關閉浮層（WidgetWithChildren：浮層或其子欄位有焦點時皆可觸發）
            esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), panel)
            esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            esc.activated.connect(panel.hide)
            self._settings_panel = panel
            self._settings_scroll = scroll
        content = SettingsDialog(
            self,
            auto_copy=self._auto_copy,
            work_history_limit=self._work_history_limit,
            fetch_history_limit=self._fetch_history_limit,
            fetch_history_count=len(self.url_history),
            original_cache_limit=self._original_cache_limit,
            glossary_auto_search=self._glossary_auto_search,
            diff_save_mode=self._diff_save_mode,
            embed_font_in_html=self._embed_font_in_html,
            embed_font_name=self._embed_font_name,
            editor_default_wysiwyg=self._editor_default_wysiwyg,
            editor_copy_to_replace=self._editor_copy_to_replace,
            glossary_sync_to_batch_quick=self._glossary_sync_to_batch_quick,
            korean_mode=self._korean_mode,
            experimental_extraction=self._experimental_extraction,
            pad_right_aa=self._pad_right_aa,
            glossary_avoid_aa=self._glossary_avoid_aa,
            glossary_kana_fold=self._glossary_kana_fold,
            glossary_skip_extract=self._glossary_skip_extract,
            glossary_auto_persist=self._glossary_auto_persist,
            glossary_translation_only=self._glossary_translation_only,
            fetch_auto_fill_title=self._fetch_auto_fill_title,
            fetch_proxy_url=self._fetch_proxy_url,
            api_proxy_url=self._api_proxy_url,
            orig_cache_path=os.path.join(
                self._base_dir(), original_cache.CACHE_FILENAME),
            on_apply=self._on_settings_applied,
            on_clear_url_history=self._on_clear_url_history_from_settings,
            on_close=self._settings_panel.hide,
        )
        # setWidget 會接管所有權並刪除舊內容 widget
        self._settings_scroll.setWidget(content)
        self._settings_content = content

    def _position_settings_panel(self) -> None:
        """把設定浮層放在 ⚙ 鈕所在工具列的正下方（比照自動翻譯「連線設定」浮層在導覽列下方）。

        以工具列底緣為基準，避免蓋住 ⚙ 鈕，讓使用者可再按一次 ⚙ 關閉浮層。
        """
        central = self.centralWidget()
        w, h = central.width(), central.height()
        if w <= 0 or h <= 0:
            return
        pw = min(560, max(360, w - 16))
        content_h = self._settings_content.sizeHint().height() + 16
        # 工具列在 TranslatePanel（stack index 0）內；映射其底緣至 central 座標作為 y 基準。
        toolbar = getattr(self._translate_panel, "_toolbar", None)
        if toolbar is not None and toolbar.isVisible():
            top_y = toolbar.mapTo(central, QPoint(0, toolbar.height())).y()
        else:
            top_y = 8
        ph = min(max(300, content_h), h - top_y - 8)
        self._settings_panel.setGeometry(8, top_y, pw, ph)

    # ════════════════════════════════════════════════════════════
    #  📂 檔案列表浮層
    # ════════════════════════════════════════════════════════════

    def toggle_file_list_panel(self) -> None:
        """開合「📂 檔案列表」浮層。每次開啟時依目前 _last_opened_file 重建內容。"""
        import time
        if (self._file_list_panel is not None
                and self._file_list_panel.isVisible()):
            self._file_list_panel.hide()
            return
        # Popup 旗標下，點工具列鈕時 Popup 先把面板關掉、然後同一次點擊事件
        # 又走到 button.clicked → 這裡，會立即重新打開。用「剛關閉 < 300 ms
        # 就視為使用者要關閉」攔下這個 reopen。
        if time.monotonic() - self._file_list_hide_ts < 0.3:
            return
        self._build_file_list_panel()
        self._refresh_file_list_panel()
        self._position_file_list_panel()
        self._file_list_panel.show()
        self._file_list_panel.raise_()
        self._file_list_panel.setFocus()

    def eventFilter(self, obj, ev):  # noqa: N802 (Qt naming)
        if (self._file_list_panel is not None
                and obj is self._file_list_panel
                and ev.type() == QEvent.Type.Hide):
            import time
            self._file_list_hide_ts = time.monotonic()
        return super().eventFilter(obj, ev)

    def _build_file_list_panel(self) -> None:
        if self._file_list_panel is not None:
            return
        # 用 Qt.Popup 旗標：點擊面板外任何位置時 Qt 會自動關掉浮層，且攔下該次
        # 點擊事件（不會穿透到工具列「📂 檔案列表」按鈕又把面板重新打開）。
        panel = QWidget(self, Qt.WindowType.Popup)
        panel.setObjectName("fileListPanel")
        # 監聽 Hide 事件，記錄 Popup 點外側關閉的時間戳（供 toggle 過濾 reopen）
        panel.installEventFilter(self)
        panel.setStyleSheet(
            "#fileListPanel { background:#343a40; border:1px solid #495057;"
            " border-radius:6px; }")
        panel.hide()
        panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("📂 檔案列表")
        title.setFont(_ui_font(12, bold=True))
        title.setStyleSheet("color:white;")
        header.addWidget(title)
        status = QLabel("")
        status.setFont(_ui_font(10))
        status.setStyleSheet("color:#adb5bd;")
        header.addWidget(status)
        header.addStretch()
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(24, 24)
        btn_close.setToolTip("關閉檔案列表")
        btn_close.setStyleSheet(
            "QPushButton { background:transparent; color:#dee2e6;"
            " border:none; font-size:14px; font-weight:bold; }"
            "QPushButton:hover { background:#495057; color:white;"
            " border-radius:3px; }")
        btn_close.clicked.connect(panel.hide)
        header.addWidget(btn_close)
        outer.addLayout(header)

        listw = QListWidget()
        listw.setFont(_ui_font(11))
        # 檔名在資料時已手動中間省略（最多 30 字、不含副檔名）；若面板寬度仍
        # 不足以塞下 30 字，交給 Qt 再做一次中間省略，避免前綴被右對齊裁掉。
        listw.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        listw.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        listw.setStyleSheet(
            "QListWidget { background:#212529; color:#dee2e6;"
            " border:1px solid #495057; border-radius:4px; }"
            "QListWidget::item { padding:4px 8px; }"
            "QListWidget::item:selected { background:#0d6efd; color:white; }")
        listw.itemActivated.connect(self._on_file_list_item_activated)
        listw.itemDoubleClicked.connect(self._on_file_list_item_activated)
        outer.addWidget(listw, 1)

        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), panel)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(panel.hide)

        self._file_list_panel = panel
        self._file_list_widget = listw
        self._file_list_status = status

    def _refresh_file_list_panel(self) -> None:
        """依 _last_opened_file 重新填充清單；無檔時顯示提示。"""
        listw = self._file_list_widget
        status = self._file_list_status
        if listw is None:
            return
        listw.clear()
        path = self._last_opened_file
        if not path or not os.path.isfile(path):
            status.setText("尚無開啟過的檔案")
            return
        folder = os.path.dirname(path)
        try:
            names = [n for n in os.listdir(folder)
                     if n.lower().endswith(('.html', '.htm'))
                     and os.path.isfile(os.path.join(folder, n))]
        except OSError as e:
            status.setText(f"⚠ 無法讀取資料夾：{e}")
            return
        # 自然排序：把連續數字當整數比較，讓「第6話」排在「第69話」之前
        # （原本的字串排序會因為「話」(U+8A71) > '9' 而誤把第6話夾在第69話與
        # 第70話之間）。
        def _natural_key(s: str):
            return [int(tok) if tok.isdigit() else tok.lower()
                    for tok in _re_mod.split(r'(\d+)', s)]
        names.sort(key=_natural_key)
        current = os.path.basename(path)
        try:
            current_pos = names.index(current)
        except ValueError:
            lname = current.lower()
            try:
                current_pos = [n.lower() for n in names].index(lname)
            except ValueError:
                current_pos = -1
        status.setText(f"共 {len(names)} 個檔案")
        for i, name in enumerate(names):
            stem = os.path.splitext(name)[0]
            if len(stem) > 30:
                # 中間省略：保留前 14 + "..." + 後 13 = 30 字
                display = stem[:14] + "..." + stem[-13:]
            else:
                display = stem
            item = QListWidgetItem(display)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip(name)
            item.setData(Qt.ItemDataRole.UserRole,
                         os.path.join(folder, name))
            if i == current_pos:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            listw.addItem(item)
        if current_pos >= 0:
            listw.setCurrentRow(current_pos)
            listw.scrollToItem(
                listw.item(current_pos),
                QListWidget.ScrollHint.PositionAtCenter)

    def _on_file_list_item_activated(self, item: QListWidgetItem) -> None:
        target = item.data(Qt.ItemDataRole.UserRole)
        if not target or not os.path.isfile(target):
            self.show_status("⚠️ 檔案不存在或已被移動", "#f39c12")
            return
        if self._file_list_panel is not None:
            self._file_list_panel.hide()
        # 走與「📥 讀取設定」相同的開檔流程，確保暫存原文／提取／翻譯一併還原
        self._open_html_path(target)

    def _open_html_path(self, file_path: str) -> None:
        """以指定路徑載入 HTML 並進入編輯面板（內部共用：檔案列表點擊等）。"""
        try:
            extracted = read_html_pre_content(file_path)
            if extracted is None:
                self.show_status(
                    "⚠️ 無法找到標準的 <pre> 標籤，讀取可能不完整。", "#f39c12")
        except OSError as e:
            self.show_status(f"❌ 讀取 HTML 失敗！{e}", "#dc3545")
            return
        self._last_dir = os.path.dirname(file_path)
        self.schedule_save()
        entry = self._load_cache_entry_for_file(file_path)
        cached_original = entry['text'] if entry else None
        if entry:
            cached_ext = entry.get('extracted', '')
            if cached_ext:
                self._translate_panel.extracted_text.setPlainText(cached_ext)
            cached_tl = entry.get('translation', '')
            if cached_tl:
                self._translate_panel.ai_text.setPlainText(cached_tl)
        self.show_edit_panel(
            file_path,
            original_text=cached_original,
            display_title=os.path.splitext(os.path.basename(file_path))[0],
            is_temp_file=False,
        )

    def _position_file_list_panel(self) -> None:
        """把檔案列表浮層放在內容區右上角（top-level Popup 用全域座標）。"""
        central = self.centralWidget()
        w, h = central.width(), central.height()
        if w <= 0 or h <= 0 or self._file_list_panel is None:
            return
        pw = min(380, max(260, w // 3))
        ph = min(max(360, h - 32), h - 16)
        x_local = max(8, w - pw - 8)
        top_left = central.mapToGlobal(QPoint(x_local, 8))
        self._file_list_panel.setGeometry(
            top_left.x(), top_left.y(), pw, ph)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        if (self._settings_panel is not None
                and self._settings_panel.isVisible()):
            self._position_settings_panel()
        if (self._file_list_panel is not None
                and self._file_list_panel.isVisible()):
            self._position_file_list_panel()

    def _on_settings_applied(self, values: dict) -> None:
        self._auto_copy = bool(values.get('auto_copy', self._auto_copy))
        self._work_history_limit = max(1, int(values.get(
            'work_history_limit', self._work_history_limit)))
        self._fetch_history_limit = max(1, int(values.get(
            'fetch_history_limit', self._fetch_history_limit)))
        self._original_cache_limit = max(1, int(values.get(
            'original_cache_limit', self._original_cache_limit)))
        self._glossary_auto_search = bool(values.get(
            'glossary_auto_search', self._glossary_auto_search))
        self._diff_save_mode = bool(values.get(
            'diff_save_mode', self._diff_save_mode))
        self._embed_font_in_html = bool(values.get(
            'embed_font_in_html', self._embed_font_in_html))
        self._embed_font_name = str(values.get(
            'embed_font_name', self._embed_font_name) or "monapo")
        self._editor_default_wysiwyg = bool(values.get(
            'editor_default_wysiwyg', self._editor_default_wysiwyg))
        self._editor_copy_to_replace = bool(values.get(
            'editor_copy_to_replace', self._editor_copy_to_replace))
        self._glossary_sync_to_batch_quick = bool(values.get(
            'glossary_sync_to_batch_quick',
            self._glossary_sync_to_batch_quick))
        self._korean_mode = bool(values.get(
            'korean_mode', self._korean_mode))
        self._experimental_extraction = bool(values.get(
            'experimental_extraction', self._experimental_extraction))
        self._pad_right_aa = bool(values.get(
            'pad_right_aa', self._pad_right_aa))
        self._glossary_avoid_aa = bool(values.get(
            'glossary_avoid_aa', self._glossary_avoid_aa))
        self._glossary_kana_fold = bool(values.get(
            'glossary_kana_fold', self._glossary_kana_fold))
        self._glossary_skip_extract = bool(values.get(
            'glossary_skip_extract', self._glossary_skip_extract))
        self._glossary_auto_persist = bool(values.get(
            'glossary_auto_persist', self._glossary_auto_persist))
        self._glossary_translation_only = bool(values.get(
            'glossary_translation_only', self._glossary_translation_only))
        self._fetch_auto_fill_title = bool(values.get(
            'fetch_auto_fill_title', self._fetch_auto_fill_title))
        if 'fetch_proxy_url' in values:
            # 立即套用，不必重開程式（url_fetcher 以模組狀態保存）
            self._fetch_proxy_url = _url_fetcher.set_fetch_proxy(
                values.get('fetch_proxy_url', ''))
        if 'api_proxy_url' in values:
            self._api_proxy_url = str(values.get('api_proxy_url', '') or '')
        self._apply_doc_num_state()
        if self._batch_window is not None:
            self._batch_window.glossary_auto_search = self._glossary_auto_search
        # 立即修剪作者歷史以符合新上限
        if len(self.work_history) > self._work_history_limit:
            self.work_history = self.work_history[:self._work_history_limit]
        self.save_cache()
        self.show_status("✅ 設定已套用", "#0f0")

    def _on_clear_ai_toggled(self, checked: bool) -> None:
        """「填入翻譯」旁核取框：讀取新一話後是否自動清空填入翻譯。"""
        self._fetch_clear_ai_text = bool(checked)
        self.schedule_save()

    def _apply_doc_num_state(self) -> None:
        """依 _fetch_auto_fill_title 設定切換話數欄位的啟用狀態。"""
        p = self._translate_panel
        enabled = not self._fetch_auto_fill_title
        p.doc_num.setEnabled(enabled)
        if not enabled:
            p.doc_num.clear()

    def _on_clear_url_history_from_settings(self, keep_n: int) -> int:
        """從設定視窗清除 URL 歷史，保留最近 keep_n 筆。回傳清除後筆數。"""
        new_count = self.settings_mgr.clear_url_history_keep_n(keep_n)
        self.url_history = self.url_history[-keep_n:] if keep_n > 0 else []
        if self._url_fetch_win_visible():
            self._url_fetch_win.on_history_cleared(self.url_history)
        self.save_cache()
        return new_count

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

    def export_settings(self, force_overwrite: bool = False) -> None:
        """儲存設定到 AA_Settings.json。

        force_overwrite=True 時強制以覆蓋方式儲存，忽略「儲存差異」設定
        （由首頁「儲存設定」按鈕右鍵觸發）；左鍵走預設行為。
        """
        self.save_cache()
        p = self._translate_panel
        cur_filter = p.get_filter_text().strip()
        cur_glossary = p.get_glossary_text().strip()
        cur_glossary_temp = p.get_glossary_temp_text().strip()
        diff_mode = self._diff_save_mode and not force_overwrite
        if diff_mode:
            existing = self.settings_mgr.load_settings()
            cur_filter = merge_filter_diff(existing.filter_text, cur_filter)
            cur_glossary = merge_glossary_diff(existing.glossary, cur_glossary)
            cur_glossary_temp = merge_glossary_diff(
                existing.glossary_temp, cur_glossary_temp)
        settings = AppSettings(
            filter_text=cur_filter,
            glossary=cur_glossary,
            glossary_temp=cur_glossary_temp,
            base_regex=self.current_base_regex,
            invalid_regex=self.current_invalid_regex,
            symbol_regex=self.current_symbol_regex,
        )
        try:
            self.settings_mgr.save_settings(settings)
            self._saved_glossary_lines = self._count_nonempty(settings.glossary)
            self._saved_glossary_temp_lines = self._count_nonempty(settings.glossary_temp)
            self._saved_filter_lines = self._count_nonempty(settings.filter_text)
            if diff_mode:
                tag = "（合併差異）"
            elif force_overwrite and self._diff_save_mode:
                tag = "（強制覆蓋）"
            else:
                tag = ""
            self.show_status(f"✅ 設定儲存成功！{tag}", "#0f0")
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
            self._saved_glossary_lines = self._count_nonempty(settings.glossary or "")
            self._saved_glossary_temp_lines = self._count_nonempty(settings.glossary_temp or "")
            self._saved_filter_lines = self._count_nonempty(settings.filter_text or "")
            self.save_cache()
            self.show_status("✅ 設定已成功讀取！", "#0f0")
        except Exception:
            self.show_status("❌ 讀取失敗，請確認檔案格式是否正確。", "#dc3545")

    def _manual_load_cache(self) -> None:
        self.load_cache()
        self.show_status("✅ 暫存讀取成功！", "#0f0")

    # ════════════════════════════════════════════════════════════
    #  原文暫存 (依「投稿標頭指紋」索引，上限由 self._original_cache_limit 控制)
    # ════════════════════════════════════════════════════════════

    def _base_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))

    @classmethod
    def _compute_author_fingerprint(cls, text: str) -> str | None:
        """便利轉接：實作改由 aa_tool.original_cache 提供（兩處共用同一份規則）。"""
        return original_cache.compute_fingerprint(text)

    def save_original_for_file(self, file_path: str, original_text: str,
                               extracted: str = "",
                               translation: str = "") -> None:
        """以「投稿標頭指紋」作為索引存入原文暫存。

        指紋由伺服器產生、翻譯流程不會動到，跨檔名仍能命中。實作委派給
        `aa_tool.original_cache.save_entry`；本方法只負責把 GUI 端的上限設定
        傳下去。`file_path` 目前未直接使用（保留簽名以維持其他呼叫端）。
        """
        if not file_path or not original_text:
            return
        original_cache.save_entry(
            self._base_dir(), original_text,
            extracted=extracted, translation=translation,
            limit=self._original_cache_limit)

    def _load_cache_entry_for_file(self, file_path: str) -> dict | None:
        return original_cache.load_entry_for_html(self._base_dir(), file_path)

    def load_original_for_file(self, file_path: str) -> str | None:
        return original_cache.load_text_for_html(self._base_dir(), file_path)

    def load_original_with_url_fallback(self, file_path: str) -> str | None:
        """重找原文：先查 ``aa_original_cache.json``；查不到時改查 ``url_history``
        是否有指紋相符的網址，直接抓網頁解析重建原文，並同步寫回暫存。

        編輯器「🔎 重找原文」鈕走這條：cache 失同步或自動翻譯前的舊檔，
        只要還能從網址重抓就能對得回來。
        """
        # 1) 先試本地暫存
        text = self.load_original_for_file(file_path)
        if text:
            return text
        # 2) 從 HTML 的 <pre> 算指紋
        try:
            pre = read_html_pre_content(file_path)
        except OSError:
            return None
        if not pre:
            return None
        fp = self._compute_author_fingerprint(pre)
        if not fp:
            return None
        # 3) 在 url_history 中找指紋相符的網址
        matching_url = next(
            (h.get('url') for h in self.url_history
             if isinstance(h, dict) and h.get('fingerprint') == fp
             and h.get('url')),
            None)
        if not matching_url:
            return None
        # 4) 抓網頁＋解析（先吃 %TEMP%/aa_url_cache 的內容，沒命中才上網）
        try:
            page_html = self._read_url_cache(matching_url)
            if page_html is None:
                page_html = _fetch_url(matching_url)
                self._write_url_cache(matching_url, page_html)
            text_content, _nav, page_title = _parse_page_html(
                page_html, matching_url,
                author_name=self._author_name,
                author_only=self._author_only)
        except Exception:
            return None
        if not text_content or not text_content.strip():
            return None
        # 5) 重建 source（與自動翻譯一致：display_title + 空行 + 內文）並寫回暫存
        display_title = _extract_work_title(page_title) if page_title else ""
        source = (display_title + "\n\n" + text_content
                  if display_title else text_content)
        try:
            original_cache.save_entry(
                self._base_dir(), source,
                limit=self._original_cache_limit)
        except Exception:
            pass
        return source

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

    def _on_editor_line_height_changed(self, percent: int) -> None:
        self._editor_line_height = int(percent)
        self.schedule_save()

    def _on_side_state_changed(
        self, width: int | None, auto_scroll: bool | None
    ) -> None:
        """編輯器右側「局部重套用」面板狀態變更：寬度（splitterMoved）或
        勾選框（toggled）。任一參數為 None 表示這次只更新另一個欄位。"""
        if width is not None:
            try:
                self._side_panel_width = int(width)
            except (TypeError, ValueError):
                pass
        if auto_scroll is not None:
            self._side_auto_scroll = bool(auto_scroll)
        self.schedule_save()

    def _on_glossary_panel_width_changed(self, width: int) -> None:
        """編輯器「用語集」面板（Alt+5）寬度變更（splitterMoved）→ 持久化。"""
        try:
            self._glossary_panel_width = int(width)
        except (TypeError, ValueError):
            return
        self.schedule_save()

    def save_glossary_only(self, force_overwrite: bool = False) -> str:
        """只把「一般術語表」寫入 AA_Settings.json，其他設定保留原檔不動。

        依「僅儲存差異」設定決定合併或覆蓋（force_overwrite=True 強制覆蓋）。
        供編輯器「用語集」面板的「儲存用語」按鈕呼叫；回傳狀態尾註字串。
        """
        existing = self.settings_mgr.load_settings()
        cur_glossary = self._translate_panel.get_glossary_text().strip()
        diff_mode = self._diff_save_mode and not force_overwrite
        if diff_mode:
            cur_glossary = merge_glossary_diff(existing.glossary, cur_glossary)
        existing.glossary = cur_glossary
        self.settings_mgr.save_settings(existing)
        self._saved_glossary_lines = self._count_nonempty(cur_glossary)
        if diff_mode:
            return "（合併差異）"
        if force_overwrite and self._diff_save_mode:
            return "（強制覆蓋）"
        return ""

    def _on_pad_count_changed(self, count: int) -> None:
        try:
            n = int(count)
        except (TypeError, ValueError):
            return
        if n not in (1, 2, 3):
            return
        self._pad_space_count = n
        self.schedule_save()

    def _on_edit_saved(self, file_path: str,
                       is_save_as: bool = False) -> None:
        """EditWindow 儲存成功後的 callback。

        `is_save_as=True` 表示走另存新檔路徑（含暫存檔首次落地），
        此時會把當前 (doc_title, author) 戳印到對應 URL 歷史條目；
        Ctrl+S 覆寫既有檔案不觸發戳印。
        """
        # 更新導覽列與標題
        base = os.path.basename(file_path)
        title = self._edit_window._display_title if self._edit_window else ""
        nav_name = title or base
        self._nav_label.setText(f"編輯：{nav_name}")
        self._update_work_title(f"編輯 — {nav_name}")
        # 暫存原文
        if self._edit_window is not None and self._edit_window._original_text:
            self.save_original_for_file(
                file_path, self._edit_window._original_text,
                extracted=self._translate_panel.get_extracted_text(),
                translation=self._translate_panel.get_ai_text(),
            )
        if is_save_as:
            self._stamp_current_url_history()

    # ════════════════════════════════════════════════════════════
    #  作品 + 作者 歷史記錄 (上限由 self._work_history_limit 控制)
    # ════════════════════════════════════════════════════════════

    def _record_work_history(self) -> None:
        """按下替換翻譯時呼叫，將當前 (title, author) 記入歷史。"""
        p = self._translate_panel
        title = p.get_doc_title().strip()
        author = self._author_name.strip()
        if not title and not author:
            return
        pair = {'title': title, 'author': author}
        # 持久化：直接對檔案做鎖定 append（多程序安全，不會被其他程序蓋掉）
        self.settings_mgr.append_work_history(pair,
                                              max_items=self._work_history_limit)
        # 同步 in-memory 副本供 UI 立即使用
        history = [h for h in getattr(self, 'work_history', [])
                   if not (h.get('title') == title
                           and h.get('author') == author)]
        history.insert(0, pair)
        self.work_history = history[:self._work_history_limit]

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

    def _refresh_shared_history(self) -> None:
        """每 1.5 秒讀檔比對共享歷史欄位；有變動即刷新 in-memory + 推子程序。

        改用「直接比對內容」而非 mtime，避免某些檔案系統 mtime 解析度只有 1～2 秒
        造成兩個程序同秒寫入時漏偵測。peek 對小 JSON 開銷微小。
        """
        try:
            state = self.settings_mgr.peek_shared_state(self.current_url)
        except Exception:
            return
        new_url_hist = list(state['url_history'])
        new_work_hist = list(state['work_history'])
        url_changed = new_url_hist != self.url_history
        work_changed = new_work_hist != self.work_history
        rel = state['url_related_links']
        rel_changed = bool(rel) and list(rel) != self.url_related_links
        if not (url_changed or work_changed or rel_changed):
            return
        self.url_history = new_url_hist
        self.work_history = new_work_hist
        if rel_changed:
            self.url_related_links = list(rel)
        # 若 URL 讀取視窗開著，將最新狀態推送過去即時刷新
        if (url_changed or rel_changed) and self._url_fetch_win_visible():
            self._url_fetch_win.on_history_updated(
                self.url_history,
                self.url_related_links,
                self.current_url,
            )

    def _apply_url_history_meta(self, url: str) -> None:
        """抓取完成後若 url_history 該條目帶有戳印 meta，套用至 doc_title/author。

        - work_title → doc_title 欄位（覆蓋頁面解析得到的標題）
        - author → self._author_name，並推送至 URL 抓取子視窗的作者欄位
        - 任一欄為空則跳過該欄套用
        """
        if not url:
            return
        entry = next((h for h in self.url_history
                      if isinstance(h, dict) and h.get('url') == url), None)
        if not entry:
            return
        work_title = (entry.get('work_title') or '').strip()
        author = (entry.get('author') or '').strip()
        if work_title:
            self._translate_panel.doc_title.setText(work_title)
        if author:
            self._author_name = author
            if self._url_fetch_win_visible():
                self._url_fetch_win.on_author_updated(author)

    def _stamp_current_url_history(self) -> None:
        """把當前 doc_title + author 戳印到 url_history 中對應 self.current_url 的條目。

        觸發點：EditWindow 另存新檔成功、apply_translation_and_save 成功儲存後。
        覆寫策略：直接覆蓋既有戳印值（最新一次儲存即最新的對應作品/作者）。
        """
        url = (getattr(self, 'current_url', '') or '').strip()
        if not url:
            return
        title = self._translate_panel.get_doc_title().strip()
        # 自動填入模式下，doc_title 是程式自動填的，不算「使用者手填」，
        # 不寫入歷史；保留該 URL 既有的 work_title 戳印值（若有）。
        if self._fetch_auto_fill_title:
            _entry = next((h for h in self.url_history
                           if isinstance(h, dict) and h.get('url') == url), None)
            title = (_entry.get('work_title') or '').strip() if _entry else ''
        author = (self._author_name or '').strip()
        try:
            self.settings_mgr.stamp_url_history_meta(url, title, author)
        except Exception:
            return
        # 同步 in-memory（避免 _refresh_shared_history 比對時漏看新值）
        for h in self.url_history:
            if isinstance(h, dict) and h.get('url') == url:
                h['work_title'] = title
                h['author'] = author
                break

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
    from aa_tool.crash_logger import install_crash_logger
    install_crash_logger()
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
