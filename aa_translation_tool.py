"""AA 漫畫翻譯輔助工具 — PyQt6 主視窗。"""
from __future__ import annotations

import html
import math
import os
import re
import sys
import threading

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget,
    QVBoxLayout, QWidget,
)

from aa_tool.constants import (
    DEFAULT_BASE_REGEX, DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX,
    DEFAULT_BG_COLOR, DEFAULT_FG_COLOR,
)
from aa_tool.html_io import read_html_pre_content, write_html_file
from aa_tool.settings_manager import SettingsManager, AppSettings, AppCache
from aa_tool.text_extraction import (
    extract_text as _extract_text,
    format_extraction_output,
    analyze_extraction as _analyze_extraction,
    validate_ai_text as _validate_ai_text,
    check_chapter_number as _check_chapter_number,
)
from aa_tool.translation_engine import (
    parse_glossary,
    apply_translation as _apply_translation,
)
from aa_tool.url_fetcher import fetch_url as _fetch_url, parse_page_html as _parse_page_html
from aa_tool.ui_result_modal import show_result_modal as _show_result_modal
from aa_tool.qt_helpers import make_button, show_toast as _show_toast

from aa_dedup_tool import AADedupTool


def _load_qss() -> str:
    """載入暗色主題 QSS。"""
    qss_path = os.path.join(os.path.dirname(__file__), "aa_tool", "dark_theme.qss")
    if os.path.exists(qss_path):
        with open(qss_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


class AATranslationTool(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("AA 漫畫翻譯輔助工具")
        self.resize(1400, 900)

        # 字體
        self.aa_font = QFont("Meiryo", 14)
        self.result_font = QFont("MS PGothic", 16)
        self.ui_font = QFont("Microsoft JhengHei", 14, QFont.Weight.Bold)
        self.ui_small_font = QFont("Microsoft JhengHei", 12)

        self.bg_color = DEFAULT_BG_COLOR
        self.fg_color = DEFAULT_FG_COLOR
        self.preview_text_cache = ""

        self.settings_mgr = SettingsManager(os.path.dirname(os.path.abspath(__file__)))

        # 正則
        self.default_base_regex = DEFAULT_BASE_REGEX
        self.default_invalid_regex = DEFAULT_INVALID_REGEX
        self.default_symbol_regex = DEFAULT_SYMBOL_REGEX
        self.current_base_regex = self.default_base_regex
        self.current_invalid_regex = self.default_invalid_regex
        self.current_symbol_regex = self.default_symbol_regex

        self._save_timer: QTimer | None = None

        self._setup_ui()
        self.load_cache()
        self.load_settings_at_startup()

    def closeEvent(self, event):
        self.save_cache()
        event.accept()

    # ════════════════════════════════════════════════════════════
    #  Toast
    # ════════════════════════════════════════════════════════════

    def show_toast(self, message: str, color: str = "#28a745", duration: int = 3000):
        _show_toast(self, message, color=color, duration=duration)

    def show_confirm_toast(self, message: str, on_yes, color: str = "#17a2b8", duration: int = 8000):
        toast = QWidget(self)
        toast.setStyleSheet(f"background-color: {color}; border-radius: 8px;")
        layout = QVBoxLayout(toast)
        layout.setContentsMargins(20, 10, 20, 10)

        lbl = QLabel(message)
        lbl.setFont(QFont("Microsoft JhengHei", 14, QFont.Weight.Bold))
        lbl.setStyleSheet("color: white; background: transparent;")
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(350)
        layout.addWidget(lbl)

        btn_layout = QHBoxLayout()
        btn_yes = make_button("是", color="#28a745", hover="#218838", font=self.ui_small_font, width=60)
        btn_no = make_button("否", color="#dc3545", hover="#c82333", font=self.ui_small_font, width=60)

        def yes_action():
            toast.deleteLater()
            on_yes()

        btn_yes.clicked.connect(yes_action)
        btn_no.clicked.connect(toast.deleteLater)
        btn_layout.addWidget(btn_yes)
        btn_layout.addWidget(btn_no)
        layout.addLayout(btn_layout)

        toast.adjustSize()
        toast.move(self.width() - toast.width() - 20, 55)
        toast.raise_()
        toast.show()
        QTimer.singleShot(duration, toast.deleteLater)

    # ════════════════════════════════════════════════════════════
    #  主 UI
    # ════════════════════════════════════════════════════════════

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        self._root_layout = QVBoxLayout(central)
        self._root_layout.setContentsMargins(10, 5, 10, 5)
        self._root_layout.setSpacing(5)

        # ── 工具列 ──
        self._build_toolbar()

        # ── 堆疊頁面 ──
        self._stack = QStackedWidget()
        self._root_layout.addWidget(self._stack, 1)

        # Page 0: 翻譯模式
        self._translate_page = QWidget()
        self._build_translate_page()
        self._stack.addWidget(self._translate_page)

        # Page 1: 批次搜尋
        self._batch_page = QWidget()
        self._build_batch_page()
        self._stack.addWidget(self._batch_page)

        # Page 2: 內嵌編輯
        self.edit_frame = QWidget()
        self._stack.addWidget(self.edit_frame)

        # ── 底部執行列（僅翻譯模式可見）──
        self._build_gen_bar()

        self._current_mode = "translate"
        self._previous_mode = "translate"
        self.edit_tab_textbox = None

    # ── 工具列 ──

    def _build_toolbar(self):
        self.toolbar = QWidget()
        self.toolbar.setObjectName("toolbar")
        self.toolbar.setStyleSheet("background-color: #343a40; border-radius: 4px;")
        tb_layout = QHBoxLayout(self.toolbar)
        tb_layout.setContentsMargins(10, 5, 10, 5)

        title_lbl = QLabel("AA 漫畫翻譯輔助工具")
        title_lbl.setFont(QFont("Microsoft JhengHei", 20, QFont.Weight.Bold))
        title_lbl.setStyleSheet("color: white; background: transparent;")
        tb_layout.addWidget(title_lbl)

        # 模式切換
        self.btn_mode_translate = make_button("翻譯模式", color="#ff9800", hover="#e68a00", font=self.ui_small_font, width=80)
        self.btn_mode_translate.clicked.connect(lambda: self.switch_mode("translate"))
        tb_layout.addWidget(self.btn_mode_translate)

        self.btn_mode_batch = make_button("批次搜尋", color="#555555", hover="#444444", font=self.ui_small_font, width=80)
        self.btn_mode_batch.clicked.connect(lambda: self.switch_mode("batch"))
        tb_layout.addWidget(self.btn_mode_batch)

        self.experimental_edit_tab = QCheckBox("實驗:內嵌編輯")
        self.experimental_edit_tab.setFont(self.ui_small_font)
        self.experimental_edit_tab.setStyleSheet("color: white; background: transparent;")
        tb_layout.addWidget(self.experimental_edit_tab)

        tb_layout.addStretch()

        btn_debug = make_button("🔧提取Debug", color="#6c757d", hover="#5a6268", font=self.ui_font)
        btn_debug.clicked.connect(self.analyze_extraction)
        tb_layout.addWidget(btn_debug)

        btn_dedup = make_button("文字處理工具", color="#17a2b8", hover="#138496", font=self.ui_font)
        btn_dedup.clicked.connect(self._open_dedup_tool)
        tb_layout.addWidget(btn_dedup)

        btn_export = make_button("📤 儲存設定", color="#28a745", hover="#218838", font=self.ui_font)
        btn_export.clicked.connect(self.export_settings)
        tb_layout.addWidget(btn_export)

        btn_import = make_button("📥 讀取設定", color="#17a2b8", hover="#138496", font=self.ui_font)
        btn_import.clicked.connect(self.import_settings)
        tb_layout.addWidget(btn_import)

        self._root_layout.addWidget(self.toolbar)

    # ── 翻譯模式頁面 ──

    def _build_translate_page(self):
        layout = QGridLayout(self._translate_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.setRowStretch(0, 4)
        layout.setRowStretch(2, 3)
        layout.setColumnStretch(0, 6)
        layout.setColumnStretch(1, 4)

        # ═ Top Left: 原始文本 ═
        src_frame = QFrame()
        src_layout = QVBoxLayout(src_frame)
        src_layout.setContentsMargins(5, 5, 5, 5)

        src_top = QHBoxLayout()
        lbl_src = QLabel("1. 原始文本 (貼上來源):")
        lbl_src.setFont(self.ui_font)
        src_top.addWidget(lbl_src)

        btn_url = make_button("🌐 網址讀取", color="#6f42c1", hover="#5a32a3", font=self.ui_small_font, width=90)
        btn_url.clicked.connect(self.open_url_fetch_dialog)
        src_top.addWidget(btn_url)

        self.btn_next_chapter = make_button("下一話 ▶", color="#0d6efd", hover="#0b5ed7", font=self.ui_small_font, width=75)
        self.btn_next_chapter.clicked.connect(self.fetch_next_chapter)
        src_top.addWidget(self.btn_next_chapter)

        btn_copy_url = make_button("📋 複製網址", color="#6c757d", hover="#5a6268", font=self.ui_small_font, width=85)
        btn_copy_url.clicked.connect(self.copy_current_url)
        src_top.addWidget(btn_copy_url)

        src_top.addStretch()

        self.doc_title = QLineEdit()
        self.doc_title.setPlaceholderText("輸入標題 (選填)")
        self.doc_title.setFont(self.ui_small_font)
        self.doc_title.setFixedWidth(150)
        self.doc_title.textChanged.connect(self.schedule_save)
        src_top.addWidget(self.doc_title)

        self.doc_num = QLineEdit("1")
        self.doc_num.setFont(self.ui_small_font)
        self.doc_num.setFixedWidth(40)
        self.doc_num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.doc_num.textChanged.connect(self.schedule_save)
        src_top.addWidget(self.doc_num)

        btn_dec = QPushButton("-")
        btn_dec.setFixedSize(25, 24)
        btn_dec.setFont(self.ui_small_font)
        btn_dec.clicked.connect(self.dec_num)
        src_top.addWidget(btn_dec)

        btn_inc = QPushButton("+")
        btn_inc.setFixedSize(25, 24)
        btn_inc.setFont(self.ui_small_font)
        btn_inc.clicked.connect(self.inc_num)
        src_top.addWidget(btn_inc)

        src_layout.addLayout(src_top)

        self.source_text = QPlainTextEdit()
        self.source_text.setFont(self.aa_font)
        self.source_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.source_text.setUndoRedoEnabled(True)
        self.source_text.textChanged.connect(self.schedule_save)
        src_layout.addWidget(self.source_text)

        layout.addWidget(src_frame, 0, 0)

        # ═ Top Right: 過濾規則 + 術語表 ═
        right_frame = QFrame()
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(5, 5, 5, 5)

        lbl_filter = QLabel("自訂過濾規則 (每行一條正則):")
        lbl_filter.setFont(self.ui_font)
        right_layout.addWidget(lbl_filter)

        self.filter_text = QPlainTextEdit()
        self.filter_text.setFont(self.aa_font)
        self.filter_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.filter_text.setStyleSheet("QPlainTextEdit { background-color: #3c3836; }")
        self.filter_text.setUndoRedoEnabled(True)
        self.filter_text.textChanged.connect(self.schedule_save)
        right_layout.addWidget(self.filter_text, 1)

        # 術語表
        glossary_header = QHBoxLayout()
        lbl_glossary = QLabel("術語表 (日文=中文):")
        lbl_glossary.setFont(self.ui_font)
        glossary_header.addWidget(lbl_glossary)
        glossary_header.addStretch()
        right_layout.addLayout(glossary_header)

        # 術語表 tab 切換
        tab_bar = QHBoxLayout()
        self._btn_tab_general = make_button("一般", color="#2a3b4c", hover="#3a4b5c", font=self.ui_small_font, width=50)
        self._btn_tab_temp = make_button("臨時", color="#555555", hover="#4b2a2a", font=self.ui_small_font, width=50)
        self._btn_tab_general.clicked.connect(lambda: self._switch_glossary_tab("一般"))
        self._btn_tab_temp.clicked.connect(lambda: self._switch_glossary_tab("臨時"))
        tab_bar.addWidget(self._btn_tab_general)
        tab_bar.addWidget(self._btn_tab_temp)
        tab_bar.addStretch()
        right_layout.addLayout(tab_bar)

        self._glossary_stack = QStackedWidget()
        self.glossary_text = QPlainTextEdit()
        self.glossary_text.setFont(self.aa_font)
        self.glossary_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.glossary_text.setStyleSheet("QPlainTextEdit { background-color: #2a3b4c; }")
        self.glossary_text.setUndoRedoEnabled(True)
        self.glossary_text.textChanged.connect(self.schedule_save)
        self._glossary_stack.addWidget(self.glossary_text)

        self.glossary_text_temp = QPlainTextEdit()
        self.glossary_text_temp.setFont(self.aa_font)
        self.glossary_text_temp.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.glossary_text_temp.setStyleSheet("QPlainTextEdit { background-color: #3b2a2a; }")
        self.glossary_text_temp.setUndoRedoEnabled(True)
        self.glossary_text_temp.textChanged.connect(self.schedule_save)
        self._glossary_stack.addWidget(self.glossary_text_temp)

        right_layout.addWidget(self._glossary_stack, 1)

        layout.addWidget(right_frame, 0, 1)

        # ═ Middle: 提取按鈕列 ═
        extract_bar = QWidget()
        eb_layout = QHBoxLayout(extract_bar)
        eb_layout.setContentsMargins(0, 0, 0, 0)
        eb_layout.addStretch()

        btn_ext = make_button("⬇️提取日文⬇️", color="#007bff", hover="#0069d9", font=self.ui_font, width=250)
        btn_ext.clicked.connect(self.extract_text)
        eb_layout.addWidget(btn_ext)

        self.auto_copy_switch = QCheckBox("自動複製")
        self.auto_copy_switch.setFont(self.ui_small_font)
        eb_layout.addWidget(self.auto_copy_switch)

        eb_layout.addStretch()
        layout.addWidget(extract_bar, 1, 0, 1, 2)

        # ═ Bottom Left: 提取結果 ═
        ext_frame = QFrame()
        ext_layout = QVBoxLayout(ext_frame)
        ext_layout.setContentsMargins(5, 5, 5, 5)

        ext_top = QHBoxLayout()
        lbl_ext = QLabel("2. 提取結果:")
        lbl_ext.setFont(self.ui_font)
        ext_top.addWidget(lbl_ext)

        self.ext_count_label = QLabel("")
        self.ext_count_label.setFont(self.ui_font)
        self.ext_count_label.setStyleSheet("color: #17a2b8;")
        ext_top.addWidget(self.ext_count_label)
        ext_top.addStretch()

        btn_copy_all = make_button("複製全部", color="#3a7ebf", hover="#325882", font=self.ui_small_font, width=70)
        btn_copy_all.clicked.connect(lambda: self.copy_split('all'))
        ext_top.addWidget(btn_copy_all)

        btn_copy_top = make_button("複製上半", color="#3a7ebf", hover="#325882", font=self.ui_small_font, width=70)
        btn_copy_top.clicked.connect(lambda: self.copy_split('top'))
        ext_top.addWidget(btn_copy_top)

        btn_copy_bottom = make_button("複製下半", color="#3a7ebf", hover="#325882", font=self.ui_small_font, width=70)
        btn_copy_bottom.clicked.connect(lambda: self.copy_split('bottom'))
        ext_top.addWidget(btn_copy_bottom)

        ext_layout.addLayout(ext_top)

        self.extracted_text = QPlainTextEdit()
        self.extracted_text.setFont(self.aa_font)
        self.extracted_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.extracted_text.setUndoRedoEnabled(True)
        ext_layout.addWidget(self.extracted_text)

        layout.addWidget(ext_frame, 2, 0)

        # ═ Bottom Right: AI 翻譯結果 ═
        ai_frame = QFrame()
        ai_layout = QVBoxLayout(ai_frame)
        ai_layout.setContentsMargins(5, 5, 5, 5)

        ai_top = QHBoxLayout()
        lbl_ai = QLabel("3. 翻譯結果:")
        lbl_ai.setFont(self.ui_font)
        ai_top.addWidget(lbl_ai)

        self.ai_warn_label = QLabel("")
        self.ai_warn_label.setFont(self.ui_small_font)
        self.ai_warn_label.setStyleSheet("color: #ff4444;")
        ai_top.addWidget(self.ai_warn_label)
        ai_top.addStretch()
        ai_layout.addLayout(ai_top)

        self.ai_text = QPlainTextEdit()
        self.ai_text.setFont(self.aa_font)
        self.ai_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.ai_text.setUndoRedoEnabled(True)
        self.ai_text.textChanged.connect(self._on_ai_text_changed)
        ai_layout.addWidget(self.ai_text)

        layout.addWidget(ai_frame, 2, 1)

    # ── 底部執行列 ──

    def _build_gen_bar(self):
        self.gen_bar = QWidget()
        gb_layout = QHBoxLayout(self.gen_bar)
        gb_layout.setContentsMargins(0, 0, 0, 0)

        btn_apply = make_button("🚀 替換翻譯並編輯 🚀", color="#ff9800", hover="#e68a00", font=self.ui_font)
        btn_apply.setFixedHeight(45)
        btn_apply.clicked.connect(self.apply_translation)
        gb_layout.addWidget(btn_apply, 1)

        btn_loadcache = make_button("📥 讀入暫存", color="#17a2b8", hover="#138496", font=self.ui_font, width=150)
        btn_loadcache.setFixedHeight(45)
        btn_loadcache.clicked.connect(self._manual_load)
        gb_layout.addWidget(btn_loadcache)

        btn_openhtml = make_button("📂 打開已儲存的 HTML", color="#6f42c1", hover="#5a32a3", font=self.ui_font, width=250)
        btn_openhtml.setFixedHeight(45)
        btn_openhtml.clicked.connect(self.import_html)
        gb_layout.addWidget(btn_openhtml)

        self._root_layout.addWidget(self.gen_bar)

    # ── 批次搜尋頁面 ──

    def _build_batch_page(self):
        layout = QVBoxLayout(self._batch_page)
        layout.setContentsMargins(0, 0, 0, 0)

        # 資料夾選擇
        folder_row = QHBoxLayout()
        lbl_folder = QLabel("資料夾:")
        lbl_folder.setFont(self.ui_font)
        folder_row.addWidget(lbl_folder)

        self.batch_folder_entry = QLineEdit()
        self.batch_folder_entry.setFont(self.ui_small_font)
        folder_row.addWidget(self.batch_folder_entry, 1)

        btn_browse = make_button("瀏覽…", color="#6c757d", hover="#5a6268", font=self.ui_small_font, width=70)
        btn_browse.clicked.connect(self._batch_browse_folder)
        folder_row.addWidget(btn_browse)
        layout.addLayout(folder_row)

        # 搜尋 / 替換列
        search_row = QHBoxLayout()
        lbl_search = QLabel("搜尋:")
        lbl_search.setFont(self.ui_font)
        search_row.addWidget(lbl_search)

        self.batch_search_entry = QLineEdit()
        self.batch_search_entry.setFont(self.ui_small_font)
        self.batch_search_entry.setFixedWidth(250)
        search_row.addWidget(self.batch_search_entry)

        self.batch_regex_switch = QCheckBox("正則")
        self.batch_regex_switch.setFont(self.ui_small_font)
        search_row.addWidget(self.batch_regex_switch)

        lbl_replace = QLabel("替換:")
        lbl_replace.setFont(self.ui_font)
        search_row.addWidget(lbl_replace)

        self.batch_replace_entry = QLineEdit()
        self.batch_replace_entry.setFont(self.ui_small_font)
        self.batch_replace_entry.setFixedWidth(250)
        search_row.addWidget(self.batch_replace_entry)

        self.batch_search_btn = make_button("🔍 搜尋", color="#007bff", hover="#0069d9", font=self.ui_font, width=80)
        self.batch_search_btn.clicked.connect(self.batch_search)
        search_row.addWidget(self.batch_search_btn)

        btn_replace_all = make_button("全部替換", color="#dc3545", hover="#c82333", font=self.ui_font, width=80)
        btn_replace_all.clicked.connect(self.batch_replace_all)
        search_row.addWidget(btn_replace_all)

        search_row.addStretch()
        layout.addLayout(search_row)

        # 狀態
        self.batch_status_label = QLabel("")
        self.batch_status_label.setFont(self.ui_small_font)
        self.batch_status_label.setStyleSheet("color: #888888;")
        layout.addWidget(self.batch_status_label)

        # 表頭
        header = QHBoxLayout()
        lbl_h_name = QLabel("檔名")
        lbl_h_name.setFont(self.ui_small_font)
        lbl_h_name.setFixedWidth(120)
        header.addWidget(lbl_h_name)

        lbl_h_op = QLabel("操作")
        lbl_h_op.setFont(self.ui_small_font)
        lbl_h_op.setFixedWidth(100)
        header.addWidget(lbl_h_op)

        lbl_h_ctx = QLabel("搜尋結果")
        lbl_h_ctx.setFont(self.ui_small_font)
        header.addWidget(lbl_h_ctx, 1)
        layout.addLayout(header)

        # 結果捲動區域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        self._batch_results_container = QWidget()
        self._batch_results_layout = QVBoxLayout(self._batch_results_container)
        self._batch_results_layout.setContentsMargins(0, 0, 0, 0)
        self._batch_results_layout.setSpacing(1)
        self._batch_results_layout.addStretch()
        scroll_area.setWidget(self._batch_results_container)
        layout.addWidget(scroll_area, 1)

        self.batch_matches: list[dict] = []

    # ════════════════════════════════════════════════════════════
    #  模式切換
    # ════════════════════════════════════════════════════════════

    def switch_mode(self, mode_name: str):
        self._previous_mode = self._current_mode
        self._current_mode = mode_name

        # 按鈕顏色重置
        self.btn_mode_translate.setStyleSheet(
            self.btn_mode_translate.styleSheet().replace("#ff9800", "#555555").replace("#007bff", "#555555")
        )
        self.btn_mode_batch.setStyleSheet(
            self.btn_mode_batch.styleSheet().replace("#ff9800", "#555555").replace("#007bff", "#555555")
        )

        if mode_name == "translate":
            self._stack.setCurrentWidget(self._translate_page)
            self.toolbar.setVisible(True)
            self.gen_bar.setVisible(True)
            self.btn_mode_translate.setStyleSheet(
                self.btn_mode_translate.styleSheet().replace("#555555", "#ff9800")
            )
        elif mode_name == "batch":
            self._stack.setCurrentWidget(self._batch_page)
            self.toolbar.setVisible(True)
            self.gen_bar.setVisible(False)
            self.btn_mode_batch.setStyleSheet(
                self.btn_mode_batch.styleSheet().replace("#555555", "#007bff")
            )
        elif mode_name == "edit":
            self._stack.setCurrentWidget(self.edit_frame)
            self.toolbar.setVisible(False)
            self.gen_bar.setVisible(False)

    # ════════════════════════════════════════════════════════════
    #  術語表 tab
    # ════════════════════════════════════════════════════════════

    def _switch_glossary_tab(self, tab_name: str):
        if tab_name == "一般":
            self._glossary_stack.setCurrentWidget(self.glossary_text)
            self._btn_tab_general.setStyleSheet(
                self._btn_tab_general.styleSheet().replace("#555555", "#2a3b4c")
            )
            self._btn_tab_temp.setStyleSheet(
                self._btn_tab_temp.styleSheet().replace("#3b2a2a", "#555555").replace("#2a3b4c", "#555555")
            )
        else:
            self._glossary_stack.setCurrentWidget(self.glossary_text_temp)
            self._btn_tab_temp.setStyleSheet(
                self._btn_tab_temp.styleSheet().replace("#555555", "#3b2a2a")
            )
            self._btn_tab_general.setStyleSheet(
                self._btn_tab_general.styleSheet().replace("#2a3b4c", "#555555")
            )

    # ════════════════════════════════════════════════════════════
    #  工具
    # ════════════════════════════════════════════════════════════

    def _open_dedup_tool(self):
        dlg = AADedupTool(self)
        dlg.exec()

    def _manual_load(self):
        self.load_cache(load_preview_text=True)
        self.show_toast("✅ 暫存讀取成功！")
        if getattr(self, 'preview_text_cache', ""):
            self.show_confirm_toast(
                "偵測到您有未完成的預覽視窗暫存，請問要現在開啟該視窗嗎？",
                lambda: self.show_result_modal(self.preview_text_cache),
            )

    # ════════════════════════════════════════════════════════════
    #  批次搜尋
    # ════════════════════════════════════════════════════════════

    def _batch_browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "選取 HTML 檔案所在的資料夾")
        if folder:
            self.batch_folder_entry.setText(folder)

    def read_html_pre_content(self, file_path):
        return read_html_pre_content(file_path)

    def write_html_file(self, file_path, text_content):
        write_html_file(file_path, text_content)

    def batch_search(self):
        folder = self.batch_folder_entry.text().strip()
        if not folder or not os.path.isdir(folder):
            self.show_toast("⚠️ 請先選擇有效的資料夾！", color="#f39c12")
            return

        query = self.batch_search_entry.text()
        if not query:
            self.show_toast("⚠️ 請輸入搜尋內容！", color="#f39c12")
            return

        use_regex = self.batch_regex_switch.isChecked()
        if use_regex:
            try:
                pattern = re.compile(query)
            except re.error as e:
                self.show_toast(f"⚠️ 正則語法錯誤: {e}", color="#dc3545")
                return
        else:
            pattern = re.compile(re.escape(query))

        # 清除舊結果
        self._clear_batch_results()
        self.batch_matches.clear()

        html_files = [f for f in os.listdir(folder) if f.lower().endswith('.html')]
        html_files.sort()
        file_count = len(html_files)

        self.batch_search_btn.setEnabled(False)
        self.batch_status_label.setText(f"🔍 搜尋中... (0 / {file_count} 個檔案)")
        self.batch_status_label.setStyleSheet("color: #888888;")

        def _search():
            matches = []
            for i, fname in enumerate(html_files):
                fpath = os.path.join(folder, fname)
                text = self.read_html_pre_content(fpath)
                if text is None:
                    continue

                lines = text.split('\n')
                for line_idx, line in enumerate(lines):
                    for m in pattern.finditer(line):
                        match_start = m.start()
                        match_end = m.end()
                        matched_text = m.group(0)

                        ctx_start = max(0, match_start - 10)
                        ctx_end = min(len(line), match_end + 10)
                        before = line[ctx_start:match_start]
                        after = line[match_end:ctx_end]

                        stem = os.path.splitext(fname)[0]
                        short_name = stem if len(stem) <= 12 else "…" + stem[-10:]

                        matches.append({
                            'file_path': fpath,
                            'file_name': fname,
                            'line_idx': line_idx,
                            'match_start': match_start,
                            'match_end': match_end,
                            'matched_text': matched_text,
                            'ctx_before': ("…" + before) if ctx_start > 0 else before,
                            'ctx_after': (after + "…") if ctx_end < len(line) else after,
                            'short_name': short_name,
                        })
                        if len(matches) >= 500:
                            break
                    if len(matches) >= 500:
                        break
                if len(matches) >= 500:
                    break

                if (i + 1) % 10 == 0 or i + 1 == file_count:
                    progress_i = i + 1
                    QTimer.singleShot(0, lambda p=progress_i: self.batch_status_label.setText(
                        f"🔍 搜尋中... ({p} / {file_count} 個檔案)"))

            QTimer.singleShot(0, lambda: self._search_done(matches, file_count))

        threading.Thread(target=_search, daemon=True).start()

    def _clear_batch_results(self):
        layout = self._batch_results_layout
        while layout.count() > 1:  # keep the stretch
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _search_done(self, matches, file_count):
        self.batch_matches = matches
        self.batch_search_btn.setEnabled(True)
        total = len(matches)

        if total == 0:
            self.batch_status_label.setText("找不到符合結果")
            self.batch_status_label.setStyleSheet("color: #f39c12;")
            return

        capped = total >= 500
        status_text = f"找到 {total} 筆結果" + ("（已達上限 500 筆）" if capped else "") + f"，共掃描 {file_count} 個檔案"
        self.batch_status_label.setText(f"{status_text}，渲染中...")
        self.batch_status_label.setStyleSheet("color: #888888;")
        self._render_batch(0, total, status_text)

    def _render_batch(self, start, total, status_text, batch_size=30):
        end = min(start + batch_size, total)
        for mi in self.batch_matches[start:end]:
            self._build_batch_result_row(mi)
        if end < total:
            self.batch_status_label.setText(f"{status_text}，渲染中 {end}/{total}...")
            QTimer.singleShot(0, lambda: self._render_batch(end, total, status_text, batch_size))
        else:
            self.batch_status_label.setText(status_text)
            self.batch_status_label.setStyleSheet("color: #28a745;")

    def _build_batch_result_row(self, mi):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(5, 1, 5, 1)

        mi['_row'] = row

        lbl_name = QLabel(mi['short_name'])
        lbl_name.setFont(self.ui_small_font)
        lbl_name.setFixedWidth(120)
        lbl_name.setStyleSheet("color: #6f42c1;")
        row_layout.addWidget(lbl_name)

        btn_replace = make_button("替換", color="#dc3545", hover="#c82333", font=self.ui_small_font, width=45)
        btn_replace.setFixedHeight(22)
        btn_replace.clicked.connect(lambda checked=False, m=mi: self.replace_single_match(m))
        row_layout.addWidget(btn_replace)

        btn_open = make_button("開啟", color="#007bff", hover="#0069d9", font=self.ui_small_font, width=45)
        btn_open.setFixedHeight(22)
        btn_open.clicked.connect(lambda checked=False, m=mi: self.open_file_at_match(m))
        row_layout.addWidget(btn_open)

        lbl_before = QLabel(mi['ctx_before'])
        lbl_before.setFont(self.ui_small_font)
        lbl_before.setStyleSheet("color: #888888;")
        row_layout.addWidget(lbl_before)

        lbl_match = QLabel(mi['matched_text'])
        lbl_match.setFont(QFont("Microsoft JhengHei", 12, QFont.Weight.Bold))
        lbl_match.setStyleSheet("color: #ff6b6b;")
        row_layout.addWidget(lbl_match)

        lbl_after = QLabel(mi['ctx_after'])
        lbl_after.setFont(self.ui_small_font)
        lbl_after.setStyleSheet("color: #888888;")
        row_layout.addWidget(lbl_after)

        row_layout.addStretch()

        # 插在 stretch 之前
        self._batch_results_layout.insertWidget(self._batch_results_layout.count() - 1, row)

    def open_file_at_match(self, match_info):
        text = self.read_html_pre_content(match_info['file_path'])
        if text is None:
            self.show_toast("❌ 無法讀取檔案！", color="#dc3545")
            return
        self.show_result_modal(text, source_file=match_info['file_path'], scroll_to_line=match_info['line_idx'] + 1)

    def replace_single_match(self, match_info):
        replacement = self.batch_replace_entry.text()
        fpath = match_info['file_path']

        text = self.read_html_pre_content(fpath)
        if text is None:
            self.show_toast("❌ 無法讀取檔案！", color="#dc3545")
            return

        lines = text.split('\n')
        li = match_info['line_idx']
        if li < len(lines):
            line = lines[li]
            lines[li] = line[:match_info['match_start']] + replacement + line[match_info['match_end']:]

        new_text = '\n'.join(lines)
        try:
            self.write_html_file(fpath, new_text)
        except Exception as e:
            self.show_toast(f"❌ 儲存失敗: {e}", color="#dc3545")
            return

        old_row = match_info.get('_row')
        self.batch_matches = [m for m in self.batch_matches if m is not match_info]

        if old_row:
            self._rebuild_row_as_replaced(old_row, match_info, replacement)

        self.show_toast("✅ 已替換並儲存")

    def batch_replace_all(self):
        if not self.batch_matches:
            self.show_toast("⚠️ 沒有可替換的結果！", color="#f39c12")
            return

        replacement = self.batch_replace_entry.text()

        by_file: dict[str, list[dict]] = {}
        for mi in self.batch_matches:
            by_file.setdefault(mi['file_path'], []).append(mi)

        replaced_count = 0
        file_count = 0
        for fpath, matches in by_file.items():
            text = self.read_html_pre_content(fpath)
            if text is None:
                continue

            lines = text.split('\n')
            matches.sort(key=lambda m: (m['line_idx'], -m['match_start']))

            for mi in matches:
                li = mi['line_idx']
                if li < len(lines):
                    line = lines[li]
                    lines[li] = line[:mi['match_start']] + replacement + line[mi['match_end']:]
                    replaced_count += 1

            new_text = '\n'.join(lines)
            try:
                self.write_html_file(fpath, new_text)
                file_count += 1
            except Exception:
                pass

        for mi in self.batch_matches:
            old_row = mi.get('_row')
            if old_row:
                self._rebuild_row_as_replaced(old_row, mi, replacement)

        self.batch_matches.clear()
        self.batch_status_label.setText(f"✅ 已替換 {replaced_count} 筆，涉及 {file_count} 個檔案")
        self.batch_status_label.setStyleSheet("color: #28a745;")
        self.show_toast(f"✅ 全部替換完成！共 {replaced_count} 筆")

    def _rebuild_row_as_replaced(self, row: QWidget, mi: dict, replacement: str):
        """將搜尋結果行改為「已替換」顯示。"""
        layout = row.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        lbl_name = QLabel(mi['short_name'])
        lbl_name.setFont(self.ui_small_font)
        lbl_name.setFixedWidth(120)
        lbl_name.setStyleSheet("color: #6f42c1;")
        layout.addWidget(lbl_name)

        lbl_done = QLabel("✔ 已替換")
        lbl_done.setFont(self.ui_small_font)
        lbl_done.setStyleSheet("color: #28a745;")
        layout.addWidget(lbl_done)

        btn_open = make_button("開啟", color="#007bff", hover="#0069d9", font=self.ui_small_font, width=45)
        btn_open.setFixedHeight(22)
        btn_open.clicked.connect(lambda checked=False, m=mi: self.open_file_at_match(m))
        layout.addWidget(btn_open)

        lbl_before = QLabel(mi['ctx_before'])
        lbl_before.setFont(self.ui_small_font)
        lbl_before.setStyleSheet("color: #888888;")
        layout.addWidget(lbl_before)

        lbl_replaced = QLabel(replacement if replacement else "（刪除）")
        lbl_replaced.setFont(QFont("Microsoft JhengHei", 12, QFont.Weight.Bold))
        lbl_replaced.setStyleSheet("color: #28a745;")
        layout.addWidget(lbl_replaced)

        lbl_after = QLabel(mi['ctx_after'])
        lbl_after.setFont(self.ui_small_font)
        lbl_after.setStyleSheet("color: #888888;")
        layout.addWidget(lbl_after)

        layout.addStretch()

    # ════════════════════════════════════════════════════════════
    #  設定 / 暫存
    # ════════════════════════════════════════════════════════════

    def get_settings_file(self):
        return self.settings_mgr.get_settings_file()

    def load_settings_at_startup(self):
        settings = self.settings_mgr.load_settings()
        if settings.filter_text:
            self.filter_text.setPlainText(settings.filter_text)
        if settings.glossary:
            self.glossary_text.setPlainText(settings.glossary)
        if settings.glossary_temp:
            self.glossary_text_temp.setPlainText(settings.glossary_temp)
        self.current_base_regex = settings.base_regex
        self.current_invalid_regex = settings.invalid_regex
        self.current_symbol_regex = settings.symbol_regex

    def save_regex_to_settings(self):
        self.settings_mgr.save_regex_to_settings(
            self.current_base_regex, self.current_invalid_regex, self.current_symbol_regex
        )

    def inc_num(self):
        try:
            val = int(self.doc_num.text() or "0")
            self.doc_num.setText(str(val + 1))
            self.schedule_save()
        except ValueError:
            pass

    def dec_num(self):
        try:
            val = int(self.doc_num.text() or "0")
            if val > 0:
                self.doc_num.setText(str(val - 1))
                self.schedule_save()
        except ValueError:
            pass

    def get_combined_glossary(self):
        g1 = self.glossary_text.toPlainText().strip()
        g2 = self.glossary_text_temp.toPlainText().strip()
        parts = [p for p in [g1, g2] if p]
        return '\n'.join(parts)

    def schedule_save(self):
        if self._save_timer is not None:
            self._save_timer.stop()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.save_cache)
        self._save_timer.start(500)

    def get_cache_file(self):
        return self.settings_mgr.get_cache_file()

    def _gather_cache(self) -> AppCache:
        return AppCache(
            source_text=self.source_text.toPlainText(),
            filter_text=self.filter_text.toPlainText(),
            glossary_text=self.glossary_text.toPlainText(),
            glossary_text_temp=self.glossary_text_temp.toPlainText(),
            doc_title=self.doc_title.text(),
            doc_num=self.doc_num.text(),
            bg_color=self.bg_color,
            fg_color=self.fg_color,
            preview_text=self.preview_text_cache,
            url_history=getattr(self, 'url_history', []),
            url_related_links=getattr(self, 'url_related_links', []),
            current_url=getattr(self, 'current_url', ''),
            auto_copy=self.auto_copy_switch.isChecked(),
            batch_folder=self.batch_folder_entry.text(),
            experimental_edit_tab=self.experimental_edit_tab.isChecked(),
        )

    def _apply_cache(self, cache: AppCache, load_preview_text: bool = False):
        if cache.source_text:
            self.source_text.setPlainText(cache.source_text)
        if cache.filter_text:
            self.filter_text.setPlainText(cache.filter_text)
        if cache.glossary_text:
            self.glossary_text.setPlainText(cache.glossary_text)
        if cache.glossary_text_temp:
            self.glossary_text_temp.setPlainText(cache.glossary_text_temp)
        if cache.doc_title:
            self.doc_title.setText(cache.doc_title)
        if cache.doc_num:
            self.doc_num.setText(cache.doc_num)
        if cache.bg_color:
            self.bg_color = cache.bg_color
        if cache.fg_color:
            self.fg_color = cache.fg_color
        if load_preview_text and cache.preview_text:
            self.preview_text_cache = cache.preview_text
        elif not load_preview_text:
            self.preview_text_cache = ""
        if cache.url_history:
            self.url_history = cache.url_history
        if cache.url_related_links:
            self.url_related_links = cache.url_related_links
        if cache.current_url:
            self.current_url = cache.current_url
        if cache.auto_copy:
            self.auto_copy_switch.setChecked(True)
        if cache.batch_folder:
            self.batch_folder_entry.setText(cache.batch_folder)
        if cache.experimental_edit_tab:
            self.experimental_edit_tab.setChecked(True)

    def save_cache(self):
        self.settings_mgr.save_cache(self._gather_cache())

    def load_cache(self, load_preview_text=False):
        cache = self.settings_mgr.load_cache()
        self._apply_cache(cache, load_preview_text)

    # ════════════════════════════════════════════════════════════
    #  網址讀取
    # ════════════════════════════════════════════════════════════

    def open_url_fetch_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("🌐 網址讀取")
        dialog.resize(700, 600)

        if not hasattr(self, 'url_history'):
            self.url_history = []
        if not hasattr(self, 'url_related_links'):
            self.url_related_links = []

        main_layout = QVBoxLayout(dialog)

        # URL 輸入列
        top_layout = QHBoxLayout()
        lbl_url = QLabel("網址:")
        lbl_url.setFont(self.ui_small_font)
        top_layout.addWidget(lbl_url)

        url_entry = QLineEdit()
        url_entry.setFont(self.ui_small_font)
        top_layout.addWidget(url_entry, 1)

        fetch_btn = make_button("讀取", color="#28a745", hover="#218838", font=self.ui_small_font, width=60)
        top_layout.addWidget(fetch_btn)
        main_layout.addLayout(top_layout)

        status_label = QLabel("")
        status_label.setFont(self.ui_small_font)
        status_label.setStyleSheet("color: #888888;")
        main_layout.addWidget(status_label)

        # 關聯記事
        nav_frame = QFrame()
        nav_layout = QVBoxLayout(nav_frame)
        nav_layout.setContentsMargins(5, 5, 5, 5)
        lbl_nav = QLabel("關聯記事:")
        lbl_nav.setFont(self.ui_small_font)
        nav_layout.addWidget(lbl_nav)

        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFixedHeight(160)
        nav_container = QWidget()
        nav_container_layout = QVBoxLayout(nav_container)
        nav_container_layout.setContentsMargins(5, 0, 5, 5)
        nav_container_layout.setSpacing(1)
        nav_scroll.setWidget(nav_container)
        nav_layout.addWidget(nav_scroll)
        main_layout.addWidget(nav_frame)

        # 讀取紀錄
        hist_frame = QFrame()
        hist_layout = QVBoxLayout(hist_frame)
        hist_layout.setContentsMargins(5, 5, 5, 5)

        hist_top = QHBoxLayout()
        lbl_hist = QLabel("讀取紀錄:")
        lbl_hist.setFont(self.ui_small_font)
        hist_top.addWidget(lbl_hist)
        hist_top.addStretch()

        btn_clear_hist = make_button("清除紀錄", color="#dc3545", hover="#c82333", font=self.ui_small_font, width=70)
        hist_top.addWidget(btn_clear_hist)
        hist_layout.addLayout(hist_top)

        hist_scroll = QScrollArea()
        hist_scroll.setWidgetResizable(True)
        hist_container = QWidget()
        hist_container_layout = QVBoxLayout(hist_container)
        hist_container_layout.setContentsMargins(5, 0, 5, 5)
        hist_container_layout.setSpacing(1)
        hist_scroll.setWidget(hist_container)
        hist_layout.addWidget(hist_scroll, 1)
        main_layout.addWidget(hist_frame, 1)

        def _clear_layout(layout):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    _clear_layout(item.layout())

        def refresh_history():
            _clear_layout(hist_container_layout)
            for entry in reversed(self.url_history):
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)

                title_text = entry.get('title', entry['url'])
                if len(title_text) > 60:
                    title_text = title_text[:60] + "…"
                lbl = QLabel(title_text)
                lbl.setFont(self.ui_small_font)
                lbl.setStyleSheet("color: #6f42c1; background: transparent;")
                lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                _url = entry['url']
                lbl.mousePressEvent = lambda e, u=_url: url_entry.setText(u)
                row_layout.addWidget(lbl, 1)

                btn_fetch = make_button("讀取", color="#17a2b8", hover="#138496", font=self.ui_small_font, width=45)
                btn_fetch.setFixedHeight(20)
                btn_fetch.clicked.connect(lambda checked=False, u=_url: (url_entry.setText(u), do_fetch()))
                row_layout.addWidget(btn_fetch)

                hist_container_layout.addWidget(row)
            hist_container_layout.addStretch()

        def refresh_nav(links):
            _clear_layout(nav_container_layout)
            if not links:
                lbl = QLabel("（尚未讀取或無關聯記事）")
                lbl.setFont(self.ui_small_font)
                lbl.setStyleSheet("color: #888888;")
                nav_container_layout.addWidget(lbl)
                return

            current_idx = -1
            for i, lk in enumerate(links):
                if lk.get('is_current'):
                    current_idx = i
                    break

            for i, lk in enumerate(links):
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)

                if lk.get('is_current'):
                    indicator = "▶ "
                    text_color = "#dc3545"
                else:
                    indicator = "　"
                    text_color = "#0d6efd"

                title = indicator + lk['title']
                if len(title) > 65:
                    title = title[:65] + "…"

                lbl = QLabel(title)
                lbl.setFont(self.ui_small_font)
                lbl.setStyleSheet(f"color: {text_color}; background: transparent;")

                if lk.get('url'):
                    lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                    _url = lk['url']
                    lbl.mousePressEvent = lambda e, u=_url: (url_entry.setText(u), do_fetch())

                row_layout.addWidget(lbl, 1)
                nav_container_layout.addWidget(row)

            # Prev / Next
            btn_row = QWidget()
            btn_row_layout = QHBoxLayout(btn_row)
            btn_row_layout.setContentsMargins(0, 5, 0, 0)

            if current_idx > 0:
                prev_lk = links[current_idx - 1]
                if prev_lk.get('url'):
                    btn_prev = make_button("▲ 上一話", color="#0d6efd", hover="#0b5ed7", font=self.ui_small_font, width=90)
                    btn_prev.clicked.connect(lambda checked=False, u=prev_lk['url']: (url_entry.setText(u), do_fetch()))
                    btn_row_layout.addWidget(btn_prev)

            if 0 <= current_idx < len(links) - 1:
                next_lk = links[current_idx + 1]
                if next_lk.get('url'):
                    btn_next = make_button("▼ 下一話", color="#0d6efd", hover="#0b5ed7", font=self.ui_small_font, width=90)
                    btn_next.clicked.connect(lambda checked=False, u=next_lk['url']: (url_entry.setText(u), do_fetch()))
                    btn_row_layout.addWidget(btn_next)

            btn_row_layout.addStretch()
            nav_container_layout.addWidget(btn_row)
            nav_container_layout.addStretch()

        def do_fetch():
            raw_url = url_entry.text().strip()
            if not raw_url:
                status_label.setText("⚠️ 請輸入網址！")
                status_label.setStyleSheet("color: #f39c12;")
                return
            if not raw_url.startswith('http'):
                raw_url = 'https://' + raw_url
                url_entry.setText(raw_url)

            status_label.setText("⏳ 讀取中…")
            status_label.setStyleSheet("color: #17a2b8;")
            fetch_btn.setEnabled(False)

            def _fetch():
                try:
                    page_html = _fetch_url(raw_url)
                    text_content, nav_links, page_title = _parse_page_html(page_html, raw_url)

                    if text_content is None:
                        QTimer.singleShot(0, lambda: status_label.setText("❌ 找不到 article 區塊！"))
                        QTimer.singleShot(0, lambda: status_label.setStyleSheet("color: #dc3545;"))
                        QTimer.singleShot(0, lambda: fetch_btn.setEnabled(True))
                        return

                    def _apply():
                        self.source_text.setPlainText(
                            (page_title + "\n\n" + text_content) if page_title else text_content
                        )
                        QTimer.singleShot(50, self.check_chapter_number)

                        self.url_related_links = nav_links
                        self.schedule_save()
                        refresh_nav(nav_links)

                        self.current_url = raw_url
                        hist_entry = {'url': raw_url, 'title': page_title or raw_url}
                        self.url_history = [h for h in self.url_history if h['url'] != raw_url]
                        self.url_history.append(hist_entry)
                        if len(self.url_history) > 50:
                            self.url_history = self.url_history[-50:]
                        self.schedule_save()
                        refresh_history()

                        line_count = text_content.count('\n') + 1
                        self.show_toast(f"✅ 網址讀取成功！共 {line_count} 行")
                        fetch_btn.setEnabled(True)
                        QTimer.singleShot(300, dialog.close)

                    QTimer.singleShot(0, _apply)

                except Exception as ex:
                    QTimer.singleShot(0, lambda: status_label.setText(f"❌ 讀取失敗: {ex}"))
                    QTimer.singleShot(0, lambda: status_label.setStyleSheet("color: #dc3545;"))
                    QTimer.singleShot(0, lambda: fetch_btn.setEnabled(True))

            threading.Thread(target=_fetch, daemon=True).start()

        fetch_btn.clicked.connect(do_fetch)
        url_entry.returnPressed.connect(do_fetch)

        def clear_history():
            self.url_history.clear()
            self.schedule_save()
            refresh_history()

        btn_clear_hist.clicked.connect(clear_history)

        refresh_nav(self.url_related_links)
        refresh_history()

        dialog.exec()

    def copy_current_url(self):
        url = getattr(self, 'current_url', '')
        if url:
            QApplication.clipboard().setText(url)
            self.show_toast("✅ 已複製網址到剪貼簿")
        else:
            self.show_toast("⚠️ 尚未讀取過網址！", color="#f39c12")

    def fetch_next_chapter(self):
        links = getattr(self, 'url_related_links', [])
        if not links:
            self.show_toast("⚠️ 尚未讀取過網址，無關聯記事資料！", color="#f39c12")
            return

        current_idx = -1
        for i, lk in enumerate(links):
            if lk.get('is_current'):
                current_idx = i
                break

        if current_idx < 0:
            self.show_toast("⚠️ 找不到目前所在的話數！", color="#f39c12")
            return

        if current_idx >= len(links) - 1:
            self.show_toast("⚠️ 已經是最新一話了！", color="#f39c12")
            return

        next_lk = links[current_idx + 1]
        if not next_lk.get('url'):
            self.show_toast("⚠️ 下一話沒有連結！", color="#f39c12")
            return

        next_url = next_lk['url']
        self.show_toast("⏳ 正在讀取下一話…", color="#17a2b8", duration=5000)

        def _fetch_next():
            try:
                page_html = _fetch_url(next_url)
                text_content, nav_links, page_title = _parse_page_html(page_html, next_url)

                if text_content is None:
                    QTimer.singleShot(0, lambda: self.show_toast("❌ 找不到 article 區塊！", color="#dc3545"))
                    return

                def _apply():
                    self.source_text.setPlainText(
                        (page_title + "\n\n" + text_content) if page_title else text_content
                    )
                    QTimer.singleShot(50, self.check_chapter_number)

                    self.url_related_links = nav_links
                    self.current_url = next_url
                    hist_entry = {'url': next_url, 'title': page_title or next_url}
                    self.url_history = [h for h in self.url_history if h['url'] != next_url]
                    self.url_history.append(hist_entry)
                    if len(self.url_history) > 50:
                        self.url_history = self.url_history[-50:]
                    self.schedule_save()

                    line_count = text_content.count('\n') + 1
                    self.show_toast(f"✅ 網址讀取成功！共 {line_count} 行")

                QTimer.singleShot(0, _apply)

            except Exception as ex:
                QTimer.singleShot(0, lambda: self.show_toast(f"❌ 讀取失敗: {ex}", color="#dc3545"))

        threading.Thread(target=_fetch_next, daemon=True).start()

    # ════════════════════════════════════════════════════════════
    #  匯入 / 匯出設定
    # ════════════════════════════════════════════════════════════

    def export_settings(self):
        self.save_cache()
        settings = AppSettings(
            filter_text=self.filter_text.toPlainText().strip(),
            glossary=self.glossary_text.toPlainText().strip(),
            glossary_temp=self.glossary_text_temp.toPlainText().strip(),
            base_regex=self.current_base_regex,
            invalid_regex=self.current_invalid_regex,
            symbol_regex=self.current_symbol_regex,
        )
        try:
            self.settings_mgr.save_settings(settings)
            self.show_toast("✅ 設定儲存成功！")
        except Exception as e:
            self.show_toast(f"❌ 設定儲存失敗: {e}", color="#dc3545")

    def import_settings(self):
        if not os.path.exists(self.get_settings_file()):
            self.show_toast("⚠️ 找不到設定檔 AA_Settings.json！", color="#f39c12")
            return
        try:
            settings = self.settings_mgr.load_settings()
            if settings.filter_text:
                self.filter_text.setPlainText(settings.filter_text)
            else:
                self.filter_text.clear()
            if settings.glossary:
                self.glossary_text.setPlainText(settings.glossary)
            else:
                self.glossary_text.clear()
            if settings.glossary_temp:
                self.glossary_text_temp.setPlainText(settings.glossary_temp)
            else:
                self.glossary_text_temp.clear()
            self.current_base_regex = settings.base_regex
            self.current_invalid_regex = settings.invalid_regex
            self.current_symbol_regex = settings.symbol_regex
            self.save_regex_to_settings()
            self.save_cache()
            self.show_toast("✅ 設定已成功讀取！")
        except Exception:
            self.show_toast("❌ 讀取失敗，請確認檔案格式是否正確。", color="#dc3545")

    # ════════════════════════════════════════════════════════════
    #  提取 / 翻譯 / 預覽
    # ════════════════════════════════════════════════════════════

    def analyze_extraction(self):
        cursor = self.source_text.textCursor()
        selected_text = cursor.selectedText()
        if not selected_text:
            self.show_toast("⚠️ 請先在上方『原始文本』區塊中反白選取要分析的一段文字！", color="#f39c12")
            return

        # QPlainTextEdit 用 \u2029 分段
        selected_text = selected_text.replace('\u2029', '\n')
        if not selected_text.strip():
            self.show_toast("⚠️ 選取的文字為空！", color="#f39c12")
            return

        self._show_analyzer_modal(selected_text)

    def _show_analyzer_modal(self, text: str):
        dialog = QDialog(self)
        dialog.setWindowTitle("🔧 提取分析 (Debug)")
        dialog.resize(800, 600)

        layout = QVBoxLayout(dialog)

        textbox = QPlainTextEdit()
        textbox.setFont(self.aa_font)
        textbox.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        textbox.setReadOnly(True)

        report = _analyze_extraction(
            text,
            self.current_base_regex, self.current_invalid_regex, self.current_symbol_regex,
            self.filter_text.toPlainText().strip(),
        )
        textbox.setPlainText(report)
        layout.addWidget(textbox)

        btn_close = make_button("關閉視窗", color="#dc3545", hover="#c82333", font=self.ui_font)
        btn_close.clicked.connect(dialog.close)
        layout.addWidget(btn_close, alignment=Qt.AlignmentFlag.AlignCenter)

        dialog.exec()

    def extract_text(self):
        source = self.source_text.toPlainText()
        if not source.strip():
            self.show_toast("⚠️ 請先貼上原始文本！", color="#f39c12")
            return

        self.save_cache()
        extracted_set = _extract_text(
            source,
            self.current_base_regex, self.current_invalid_regex, self.current_symbol_regex,
            self.filter_text.toPlainText().strip(),
        )
        output = format_extraction_output(extracted_set)

        self.extracted_text.setPlainText(output)
        self.ext_count_label.setText(f"(共提取 {len(extracted_set)} 行)")

        if self.auto_copy_switch.isChecked():
            QApplication.clipboard().setText(output.strip())
            self.show_toast(f"✅ 已提取 {len(extracted_set)} 行並複製到剪貼簿")

    def copy_split(self, half):
        ext_text = self.extracted_text.toPlainText().strip()
        if not ext_text:
            return
        lines = [l for l in ext_text.split('\n') if l.strip()]
        if not lines:
            return

        if half == 'all':
            text_to_copy = "\n".join(lines)
        else:
            split_idx = int(math.ceil(len(lines) / 2))
            if half == 'top':
                copy_lines = lines[:split_idx]
            else:
                copy_lines = lines[split_idx:]
            text_to_copy = "\n".join(copy_lines)
        QApplication.clipboard().setText(text_to_copy)

    def check_chapter_number(self):
        # 取前 5 行
        text = ""
        doc = self.source_text.document()
        for i in range(min(5, doc.blockCount())):
            text += doc.findBlockByNumber(i).text() + "\n"
        result = _check_chapter_number(text)
        if result is not None:
            self.doc_num.setText(result)

    def _on_ai_text_changed(self):
        """AI 翻譯文字改變時驗證格式。"""
        QTimer.singleShot(50, self._validate_ai_text)

    def _validate_ai_text(self):
        ai_content = self.ai_text.toPlainText().strip()
        if not ai_content:
            self.ai_warn_label.setText("")
            return
        warnings = _validate_ai_text(ai_content)
        if warnings:
            self.ai_warn_label.setText("  ".join(warnings))
            self.ai_warn_label.setStyleSheet("color: #ff4444;")
        else:
            self.ai_warn_label.setText("✅ 格式正確")
            self.ai_warn_label.setStyleSheet("color: #28a745;")
            QTimer.singleShot(3000, lambda: self.ai_warn_label.setText(""))

    def apply_translation(self):
        source = self.source_text.toPlainText()
        extracted = self.extracted_text.toPlainText()
        translated = self.ai_text.toPlainText()

        if not source.strip() or not extracted.strip() or not translated.strip():
            self.show_toast("⚠️ 請確保原始文本、提取結果和翻譯結果都有內容！", color="#f39c12")
            return

        self.save_cache()
        glossary = parse_glossary(self.get_combined_glossary())
        result = _apply_translation(source, extracted, translated, glossary)
        self.show_result_modal(result)

    def show_result_modal(self, text, source_file="", scroll_to_line=None):
        _show_result_modal(self, text, source_file=source_file, scroll_to_line=scroll_to_line)

    def import_html(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "選取已儲存的 HTML 檔案",
            "", "HTML files (*.html);;All files (*.*)"
        )
        if file_path:
            try:
                extracted = self.read_html_pre_content(file_path)
                if extracted is None:
                    self.show_toast("⚠️ 無法找到標準的 <pre> 標籤，讀取可能不完整。", color="#f39c12")
                    with open(file_path, 'r', encoding='utf-8') as f:
                        extracted = html.unescape(f.read())
                self.show_result_modal(extracted, source_file=file_path)
            except Exception as e:
                self.show_toast(f"❌ 讀取 HTML 檔案失敗！{e}", color="#dc3545")


def main():
    # AA 是為 GDI 字型引擎設計的，強制 Qt 使用 GDI 以取得正確的像素渲染
    os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=gdi")

    app = QApplication(sys.argv)
    app.setStyleSheet(_load_qss())

    window = AATranslationTool()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
