"""PyQt6 編輯器（Phase D：完整功能）。

獨立 process，透過 CLI 參數接收 HTML 檔案路徑、scroll-to-line、IPC 檔案路徑。

Phase A：載入 / 顯示 / 編輯 / 儲存
Phase B：搜尋、全文替換、上色、底色、補消空白
Phase C：對話框修正、對齊上一行、自動判斷、對話框(全)
Phase D：術語 IPC（重套術語、存入術語表）

Usage:
  python aa_edit_qt.py --html-file <path> [--scroll-to-line N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QFontMetricsF, QKeySequence, QShortcut,
    QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from aa_tool.bubble_alignment import (
    adjust_bubble as _adjust_bubble,
    adjust_all_bubbles as _adjust_all_bubbles,
    align_to_prev_line as _align_to_prev_line,
)
from aa_tool.html_io import (
    read_html_bg_color, read_html_head, read_html_pre_content, write_html_file,
)
from aa_tool.qt_helpers import show_toast
from aa_tool.text_extraction import extract_text as _extract_text
from aa_tool.translation_engine import (
    apply_glossary_to_text, apply_reverse_glossary_to_text, parse_glossary,
)

LINE_HEIGHT_PERCENT = 120  # 對應 CSS line-height: 1.2，與瀏覽器顯示一致
DEFAULT_BG = "#ffffff"
DEFAULT_COLOR = "#ff0000"
DEFAULT_EDITOR_FONT = "MS PGothic"  # ⚠️ 請勿改動：AA 對齊計算依賴此字體的 metrics
DEFAULT_EDITOR_FONT_SIZE = 12
EDITOR_FONT_CHOICES = ["MS PGothic", "Monapo"]

# 專案內建字體（fonts/ 資料夾），Qt 啟動後呼叫一次
_BUNDLED_FONTS_LOADED = False

def load_bundled_fonts() -> None:
    """從 fonts/ 資料夾載入 TTF 字體至 QFontDatabase（只執行一次）。"""
    global _BUNDLED_FONTS_LOADED
    if _BUNDLED_FONTS_LOADED:
        return
    _BUNDLED_FONTS_LOADED = True
    fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    if not os.path.isdir(fonts_dir):
        return
    for fname in os.listdir(fonts_dir):
        if fname.lower().endswith((".ttf", ".otf")):
            QFontDatabase.addApplicationFont(os.path.join(fonts_dir, fname))


class QtFontMeasurer:
    """FontMeasurer 的 PyQt6 實作（使用 QFontMetricsF 量測像素寬度）。"""
    def __init__(self, font: QFont) -> None:
        self._fm = QFontMetricsF(font)

    def measure(self, text: str) -> float:
        return self._fm.horizontalAdvance(text)

_COLOR_SPAN_OPEN_RE = re.compile(r'<span\s+style="color:[^"]*">')
_COLOR_SPAN_CLOSE = '</span>'


def _css_color_to_qcolor(css: str) -> 'QColor':
    """將 CSS 顏色字串（#rrggbb、rgb(r,g,b)、rgba(r,g,b,a)、名稱）轉為 QColor。"""
    s = css.strip()
    if s.startswith('rgb(') and s.endswith(')'):
        parts = s[4:-1].split(',')
        if len(parts) == 3:
            try:
                r, g, b = (int(p.strip()) for p in parts)
                return QColor(r, g, b)
            except ValueError:
                pass
    elif s.startswith('rgba(') and s.endswith(')'):
        parts = s[5:-1].split(',')
        if len(parts) == 4:
            try:
                r, g, b = (int(p.strip()) for p in parts[:3])
                a = round(float(parts[3].strip()) * 255)
                return QColor(r, g, b, a)
            except ValueError:
                pass
    return QColor(s)


def _make_button(text: str, color: str, hover: str, *,
                 width: int = 0, fg: str = "white") -> QPushButton:
    btn = QPushButton(text)
    style = (
        f"QPushButton {{ background:{color}; color:{fg};"
        f" padding:4px 10px; border:none; border-radius:4px; }}"
        f"QPushButton:hover {{ background:{hover}; }}"
    )
    btn.setStyleSheet(style)
    if width:
        btn.setMinimumWidth(width)
    return btn


class EditWindow(QMainWindow):
    def __init__(
        self,
        html_file: str,
        scroll_to_line: int = 0,
        cmd_file: str = "",
        reply_file: str = "",
        original_file: str = "",
        original_text: str | None = None,
        display_title: str = "",
        is_temp_file: bool = False,
        # Embedded mode: pass callables instead of IPC
        glossary_provider=None,   # () -> str
        glossary_saver=None,      # (orig: str, trans: str) -> None
        extract_regex_provider=None,  # () -> (base, invalid, symbol, filter_str)
        on_back=None,             # () -> None; embedded 模式回主畫面
        on_open=None,             # () -> None; 開啟已儲存的 HTML
        on_save=None,             # (file_path: str) -> None; 儲存成功後通知
        on_font_change=None,      # (family, size) -> None; 字體變更持久化
        init_font_family: str = DEFAULT_EDITOR_FONT,
        init_font_size: int = DEFAULT_EDITOR_FONT_SIZE,
        get_last_dir=None,        # () -> str; 取得上次開啟目錄
        on_dir_change=None,       # (dir: str) -> None; 目錄變更通知
        on_bg_change=None,        # (color: str) -> None; 底色變更即時持久化
        init_bg: str = "",        # 上次記住的編輯器底色
    ) -> None:
        super().__init__()
        self._html_file = html_file
        self._display_title = display_title
        self._is_temp_file = is_temp_file
        self._dirty = False
        self._current_color = DEFAULT_COLOR
        self._bg_color = DEFAULT_BG

        # ── Callback（embedded 模式）──
        self._glossary_provider = glossary_provider
        self._glossary_saver = glossary_saver
        self._extract_regex_provider = extract_regex_provider
        self._on_back = on_back
        self._on_open = on_open
        self._on_save = on_save
        self._on_font_change = on_font_change
        self._font_family = init_font_family or DEFAULT_EDITOR_FONT
        self._font_size = int(init_font_size) if init_font_size else DEFAULT_EDITOR_FONT_SIZE
        self._get_last_dir = get_last_dir
        self._on_dir_change = on_dir_change
        self._on_bg_change = on_bg_change

        # ── IPC 狀態 ──
        self._cmd_file = cmd_file
        self._reply_file = reply_file
        self._next_req_id = 1
        self._pending_callbacks: dict[int, callable] = {}
        self._ipc_timer: QTimer | None = None

        # ── 原文比對狀態 ──
        self._original_text: str | None = original_text
        if self._original_text is None and original_file and os.path.exists(original_file):
            try:
                with open(original_file, 'r', encoding='utf-8') as f:
                    self._original_text = f.read()
            except OSError:
                self._original_text = None
        self._compare_active = False
        self._preview_active = False
        self._edit_buttons: list[QPushButton | QLineEdit | QCheckBox] = []
        self._toolbar_widget: QWidget | None = None

        try:
            text = read_html_pre_content(html_file) or ""
        except OSError as e:
            QMessageBox.critical(self, "讀取失敗", f"無法讀取檔案：\n{e}")
            text = ""
        self._custom_head = read_html_head(html_file) if html_file else None
        # init_bg（使用者上次調整的顯示底色）優先，
        # 找不到時再嘗試從 HTML 讀取（舊版相容）
        if init_bg:
            self._bg_color = init_bg
        else:
            loaded_bg = read_html_bg_color(html_file) if html_file else None
            if loaded_bg:
                self._bg_color = loaded_bg

        file_name = display_title or (
            os.path.basename(html_file) if html_file else "(未命名)")
        self.setWindowTitle(f"AA 編輯器 (PyQt6) — {file_name}")
        self.resize(1280, 860)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar(file_name))

        # 搜尋列（預設隱藏，Ctrl+F 切換）
        self.search_bar = self._build_search_bar()
        self.search_bar.hide()
        root.addWidget(self.search_bar)

        # ── 編輯區（主要 + 原文，以 QStackedWidget 切換） ──
        aa_font = QFont(self._font_family, self._font_size)
        aa_font.setStyleHint(QFont.StyleHint.TypeWriter)
        self._measurer = QtFontMeasurer(aa_font)

        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)
        self.editor.setPlainText(text)
        self.editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.editor.setTabChangesFocus(False)
        self.editor.setFont(aa_font)

        self.orig_view = QTextEdit()
        self.orig_view.setAcceptRichText(False)
        self.orig_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.orig_view.setReadOnly(True)
        self.orig_view.setFont(aa_font)
        if self._original_text is not None:
            self.orig_view.setPlainText(self._original_text)
            self._apply_line_height_to(self.orig_view)

        self.preview_view = QTextEdit()
        self.preview_view.setReadOnly(True)
        self.preview_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.preview_view.setFont(aa_font)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.editor)        # index 0
        self.stack.addWidget(self.orig_view)     # index 1
        self.stack.addWidget(self.preview_view)  # index 2

        self._apply_editor_colors()
        self._apply_line_height()

        self.editor.textChanged.connect(self._on_changed)
        root.addWidget(self.stack, 1)

        # 浮動狀態提示改為右上角 Toast（show_toast），由 _set_status 統一呼叫

        # ── 全域快捷鍵 ──
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self._save_overwrite)
        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=self._toggle_search)
        QShortcut(QKeySequence("Esc"), self, activated=self._on_escape)
        QShortcut(QKeySequence("Alt+Q"), self,
                  activated=self._smart_action)
        QShortcut(QKeySequence("Alt+W"), self,
                  activated=self._restore_from_original)
        QShortcut(QKeySequence("Alt+1"), self,
                  activated=self._return_to_editor)
        QShortcut(QKeySequence("Alt+2"), self,
                  activated=self._toggle_compare)
        QShortcut(QKeySequence("Alt+3"), self,
                  activated=self._toggle_preview)
        QShortcut(QKeySequence("Alt+E"), self,
                  activated=self._reverse_glossary_replace)
        QShortcut(QKeySequence("Alt+C"), self,
                  activated=self._extract_jp_from_selection)

        if scroll_to_line and scroll_to_line > 0:
            self._scroll_to_line(scroll_to_line)

        # 啟動 IPC 輪詢
        if self._cmd_file and self._reply_file:
            self._ipc_timer = QTimer(self)
            self._ipc_timer.setInterval(200)
            self._ipc_timer.timeout.connect(self._poll_reply)
            self._ipc_timer.start()

    # ════════════════════════════════════════════════════════════
    #  UI 建置
    # ════════════════════════════════════════════════════════════

    def _build_toolbar(self, file_name: str) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("mainToolbar")
        toolbar.setStyleSheet("#mainToolbar { background:#343a40; }")
        self._toolbar_widget = toolbar
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(4)

        # Group 1: 全文替換
        lbl = QLabel("全文替換")
        lbl.setStyleSheet("color:white; font-weight:bold;")
        tb.addWidget(lbl)

        self.quick_orig = QLineEdit()
        self.quick_orig.setPlaceholderText("原文")
        self.quick_orig.setFixedWidth(140)
        tb.addWidget(self.quick_orig)

        self.quick_trans = QLineEdit()
        self.quick_trans.setPlaceholderText("翻譯")
        self.quick_trans.setFixedWidth(140)
        tb.addWidget(self.quick_trans)

        btn_exec = _make_button("執行", "#17a2b8", "#138496", width=50)
        btn_exec.clicked.connect(self._replace_all)
        tb.addWidget(btn_exec)

        self.save_to_glossary_cb = QCheckBox("存入術語")
        self.save_to_glossary_cb.setChecked(True)
        self.save_to_glossary_cb.setStyleSheet("color:white;")
        tb.addWidget(self.save_to_glossary_cb)

        btn_reapply = _make_button("重套術語", "#28a745", "#218838", width=75)
        btn_reapply.clicked.connect(self._reapply_glossary)
        tb.addWidget(btn_reapply)

        # 分隔
        tb.addSpacing(10)

        # Group 2: 文字操作
        btn_color = _make_button("上色", "#6f42c1", "#5a32a3", width=60)
        btn_color.clicked.connect(self._apply_color)
        tb.addWidget(btn_color)

        btn_pick_color = _make_button("🎨", "#6f42c1", "#5a32a3", width=32)
        btn_pick_color.clicked.connect(self._pick_color)
        btn_pick_color.setToolTip("選擇上色顏色")
        tb.addWidget(btn_pick_color)

        btn_strip = _make_button("消空白", "#e0a800", "#c69500",
                                 width=65, fg="black")
        btn_strip.clicked.connect(self._strip_spaces)
        tb.addWidget(btn_strip)

        btn_pad = _make_button("補空白", "#17a2b8", "#138496", width=65)
        btn_pad.clicked.connect(self._pad_spaces)
        tb.addWidget(btn_pad)

        tb.addSpacing(10)

        # Group 3: 對話框工具
        btn_bubble = _make_button("對話框修正", "#28a745", "#218838", width=85)
        btn_bubble.clicked.connect(self._adjust_bubble)
        tb.addWidget(btn_bubble)

        btn_align = _make_button("對齊上一行", "#17a2b8", "#138496", width=85)
        btn_align.clicked.connect(self._align_to_prev)
        tb.addWidget(btn_align)

        btn_smart = _make_button("自動判斷", "#e67e22", "#d35400", width=75)
        btn_smart.clicked.connect(self._smart_action)
        btn_smart.setToolTip("Alt+Q：依選取狀態自動執行對話框修正/上色/對齊")
        tb.addWidget(btn_smart)

        btn_bubble_all = _make_button("對話框(全)", "#20c997", "#17a085", width=85)
        btn_bubble_all.clicked.connect(self._adjust_all_bubbles)
        tb.addWidget(btn_bubble_all)

        # 比對模式時需要 disable 的控制項
        self._edit_buttons.extend([
            self.quick_orig, self.quick_trans, btn_exec, btn_reapply,
            btn_color, btn_pick_color, btn_strip, btn_pad,
            btn_bubble, btn_align, btn_smart, btn_bubble_all,
        ])

        tb.addStretch()

        # Group 4: 右側
        btn_bg = _make_button("底色", "#6c757d", "#5a6268", width=50)
        btn_bg.clicked.connect(self._choose_bg)
        tb.addWidget(btn_bg)

        btn_save = _make_button("💾 儲存", "#28a745", "#218838", width=70)
        btn_save.setToolTip("另存新檔（Ctrl+S 直接覆寫原檔）")
        btn_save.clicked.connect(self._save_as)
        tb.addWidget(btn_save)

        if self._on_open is not None:
            btn_open = _make_button("📂 開啟", "#6f42c1", "#5a32a3", width=70)
            btn_open.setToolTip("打開已儲存的 HTML 檔案")
            btn_open.clicked.connect(self._on_open)
            tb.addWidget(btn_open)

        if self._on_back is not None:
            btn_back = _make_button("← 返回", "#6c757d", "#5a6268", width=70)
            btn_back.clicked.connect(
                lambda: self._on_back() if self._on_back else None)
            tb.addWidget(btn_back)
        else:
            btn_close = _make_button("關閉", "#dc3545", "#c82333", width=50)
            btn_close.clicked.connect(self.close)
            tb.addWidget(btn_close)

        return toolbar

    def _build_search_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:#495057;")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(4)

        lbl = QLabel("搜尋")
        lbl.setStyleSheet("color:white; font-weight:bold;")
        layout.addWidget(lbl)

        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("輸入要搜尋的文字")
        self.search_entry.setFixedWidth(260)
        self.search_entry.returnPressed.connect(self._find_next)
        layout.addWidget(self.search_entry)

        btn_next = _make_button("下一個", "#007bff", "#0056b3", width=60)
        btn_next.clicked.connect(self._find_next)
        layout.addWidget(btn_next)

        btn_dice = _make_button("🎲 1D10:10", "#f39c12", "#d68910", width=90)
        btn_dice.clicked.connect(self._search_dice)
        layout.addWidget(btn_dice)

        layout.addSpacing(12)

        self._lbl_font = QLabel("字體")
        self._lbl_font.setStyleSheet("color:white; font-weight:bold;")
        layout.addWidget(self._lbl_font)

        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.addItems(EDITOR_FONT_CHOICES)
        if self._font_family not in EDITOR_FONT_CHOICES:
            self.font_combo.addItem(self._font_family)
        self.font_combo.setCurrentText(self._font_family)
        self.font_combo.setFixedWidth(140)
        self.font_combo.setStyleSheet(
            "QComboBox { background:#343638; color:#dce4ee;"
            " border:1px solid #555; padding:2px 4px; }")
        self.font_combo.currentTextChanged.connect(self._on_font_family_changed)
        layout.addWidget(self.font_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(6, 48)
        self.font_size_spin.setValue(self._font_size)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.setFixedWidth(72)
        self.font_size_spin.setStyleSheet(
            "QSpinBox { background:#343638; color:#dce4ee;"
            " border:1px solid #555; padding:2px 4px; }")
        self.font_size_spin.valueChanged.connect(self._on_font_size_changed)
        layout.addWidget(self.font_size_spin)

        layout.addStretch()

        btn_hide = _make_button("✕", "#6c757d", "#5a6268", width=30)
        btn_hide.clicked.connect(self._hide_search)
        layout.addWidget(btn_hide)

        return bar

    # ════════════════════════════════════════════════════════════
    #  格式 / 樣式
    # ════════════════════════════════════════════════════════════

    def _apply_editor_colors(self) -> None:
        fam = self._font_family
        sz = self._font_size
        self.editor.setStyleSheet(
            f"QTextEdit {{ background:{self._bg_color};"
            f" color:#000000;"
            f" border:1px solid #cccccc;"
            f" font-family:'{fam}';"
            f" font-size:{sz}pt; }}"
        )
        if hasattr(self, "orig_view"):
            self.orig_view.setStyleSheet(
                f"QTextEdit {{ background:{self._bg_color};"
                f" color:#000000;"
                f" border:1px solid #cccccc;"
                f" font-family:'{fam}';"
                f" font-size:{sz}pt; }}"
            )

    def _apply_line_height(self) -> None:
        self._apply_line_height_to(self.editor)

    def _apply_line_height_to(self, widget: QTextEdit) -> None:
        # 以主字型 × 120% 與 CJK fallback 字型自然高度取 max，套用 FixedHeight。
        # 單獨使用主字型（MS PGothic）× 120% 時，繁中字會 fallback 到
        # MingLiU/JhengHei 等較高的字型，仍會擠壓/切頂；取 max 後固定行高能
        # 完整容納 fallback 字，同時維持純日文行的 120% 視覺比例。
        main_font = widget.font()
        fm_main = QFontMetricsF(main_font)
        cjk_font = QFont("Microsoft JhengHei", main_font.pointSize())
        fm_cjk = QFontMetricsF(cjk_font)
        fixed_px = max(
            fm_main.lineSpacing() * LINE_HEIGHT_PERCENT / 100.0,
            fm_cjk.lineSpacing() * 1.02,  # 1.02 為 CJK fallback 上下緣留白
        )
        cursor = QTextCursor(widget.document())
        cursor.select(QTextCursor.SelectionType.Document)
        block_fmt = QTextBlockFormat()
        block_fmt.setLineHeight(
            fixed_px,
            QTextBlockFormat.LineHeightTypes.FixedHeight.value,
        )
        cursor.mergeBlockFormat(block_fmt)

    # ════════════════════════════════════════════════════════════
    #  搜尋
    # ════════════════════════════════════════════════════════════

    def _toggle_search(self) -> None:
        if self.search_bar.isVisible():
            self._hide_search()
        else:
            self.search_bar.show()
            self.search_entry.setFocus()
            self.search_entry.selectAll()

    def _hide_search(self) -> None:
        self.search_bar.hide()
        self.editor.setFocus()

    def _on_escape(self) -> None:
        """ESC 行為：搜尋列開啟時優先關閉；否則執行返回（或關閉視窗）。"""
        if self.search_bar.isVisible():
            self._hide_search()
            return
        if self._on_back is not None:
            self._on_back()
        else:
            self.close()

    def _find_next(self) -> None:
        query = self.search_entry.text()
        if not query:
            return
        saved_cursor = self.editor.textCursor()
        saved_scroll = self.editor.verticalScrollBar().value()
        found = self.editor.find(query)
        if not found:
            # wrap around
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            found = self.editor.find(query)
        if not found:
            # 找不到時恢復原本游標位置與捲動，避免跳到最上面
            self.editor.setTextCursor(saved_cursor)
            self.editor.verticalScrollBar().setValue(saved_scroll)
            self._set_status("🔍 找不到符合的文字", "#ffc107")
        else:
            self._set_status(f"找到：{query}", "#0f0")

    def _search_dice(self) -> None:
        self.search_entry.setText("1D10:10")
        if not self.search_bar.isVisible():
            self.search_bar.show()
        self._find_next()

    # ════════════════════════════════════════════════════════════
    #  字體切換
    # ════════════════════════════════════════════════════════════

    def _on_font_family_changed(self, family: str) -> None:
        family = (family or "").strip()
        if not family or family == self._font_family:
            return
        self._font_family = family
        self._apply_editor_font()

    def _on_font_size_changed(self, size: int) -> None:
        if size == self._font_size:
            return
        self._font_size = int(size)
        self._apply_editor_font()

    def _apply_editor_font(self) -> None:
        new_font = QFont(self._font_family, self._font_size)
        new_font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.editor.setFont(new_font)
        self.orig_view.setFont(new_font)
        self._measurer = QtFontMeasurer(new_font)
        self._apply_editor_colors()
        self._apply_line_height()
        self._apply_line_height_to(self.orig_view)
        if self._on_font_change is not None:
            try:
                self._on_font_change(self._font_family, self._font_size)
            except Exception:
                pass

    # ════════════════════════════════════════════════════════════
    #  全文替換
    # ════════════════════════════════════════════════════════════

    def _replace_all(self) -> None:
        orig = self.quick_orig.text().strip()
        trans = self.quick_trans.text().strip()
        if not orig or not trans:
            self._set_status("⚠️ 原文與翻譯皆不可為空", "#ffc107")
            return
        text = self.editor.toPlainText()
        if orig not in text:
            self._set_status(f"🔍 找不到「{orig}」", "#ffc107")
            return
        count = text.count(orig)
        new_text = text.replace(orig, trans)
        self._replace_document(new_text)

        # 若勾選「存入術語」，通知主程式
        if self.save_to_glossary_cb.isChecked():
            if self._glossary_saver is not None:
                self._glossary_saver(orig, trans)
            elif self._cmd_file:
                self._send_request(
                    "save_to_glossary", original=orig, translation=trans)

        self.quick_orig.clear()
        self.quick_trans.clear()
        self._set_status(f"✅ 已替換 {count} 處：{orig} → {trans}", "#0f0")

    def _reapply_glossary(self) -> None:
        """向主程式請求目前術語表，收到後套用到編輯內容。"""
        if self._glossary_provider is not None:
            glossary_str = self._glossary_provider()
            self._on_glossary_received({"ok": True, "glossary_text": glossary_str})
            return
        if not self._cmd_file:
            self._set_status("⚠️ 未啟用 IPC，無法取得術語表", "#ffc107")
            return
        self._send_request("get_glossary", callback=self._on_glossary_received)
        self._set_status("⏳ 正在取得術語表…", "#ffc107")

    def _on_glossary_received(self, reply: dict) -> None:
        if not reply.get("ok"):
            self._set_status(
                f"❌ 取得術語表失敗：{reply.get('error', '未知錯誤')}", "#dc3545")
            return
        glossary_str = reply.get("glossary_text", "")
        if not glossary_str:
            self._set_status("⚠️ 術語表為空", "#ffc107")
            return
        glossary = parse_glossary(glossary_str)
        if not glossary:
            self._set_status("⚠️ 術語表格式不正確", "#ffc107")
            return
        current_text = self.editor.toPlainText()
        new_text = apply_glossary_to_text(current_text, glossary)
        if new_text == current_text:
            self._set_status("術語表已套用（無變更）", "#0f0")
            return
        # 保留捲動位置
        scroll_bar = self.editor.verticalScrollBar()
        scroll_val = scroll_bar.value()
        self._replace_document(new_text)
        scroll_bar.setValue(scroll_val)
        self._set_status(
            f"✅ 已套用術語表（{len(glossary)} 條）", "#0f0")

    def _replace_document(self, new_text: str) -> None:
        """以新內容取代整份文件，保留 undo history 與 line-height。"""
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(new_text)
        cursor.endEditBlock()
        self._apply_line_height()

    # ════════════════════════════════════════════════════════════
    #  上色（選取範圍）
    # ════════════════════════════════════════════════════════════

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._current_color), self, "選擇上色顏色")
        if color.isValid():
            self._current_color = color.name()
            self._set_status(f"當前顏色：{self._current_color}", "#0f0")

    def _apply_color(self) -> None:
        """對選取範圍套用顏色；若已有 color span 則移除。"""
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._set_status("⚠️ 請先選取要上色的文字", "#ffc107")
            return
        selected = cursor.selectedText().replace('\u2029', '\n')
        if _COLOR_SPAN_OPEN_RE.search(selected) or _COLOR_SPAN_CLOSE in selected:
            stripped = _COLOR_SPAN_OPEN_RE.sub('', selected)
            stripped = stripped.replace(_COLOR_SPAN_CLOSE, '')
            cursor.insertText(stripped)
            self._set_status("已移除顏色標籤", "#0f0")
        else:
            colored = (
                f'<span style="color:{self._current_color}">'
                f'{selected}</span>'
            )
            cursor.insertText(colored)
            self._set_status(f"已套用顏色：{self._current_color}", "#0f0")

    # ════════════════════════════════════════════════════════════
    #  底色 / 文字色（整個編輯區）
    # ════════════════════════════════════════════════════════════

    def _choose_bg(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._bg_color), self, "選擇背景顏色")
        if color.isValid():
            self._bg_color = color.name()
            self._apply_editor_colors()
            if self._on_bg_change is not None:
                try:
                    self._on_bg_change(self._bg_color)
                except Exception:
                    pass

    # ════════════════════════════════════════════════════════════
    #  消空白 / 補空白
    # ════════════════════════════════════════════════════════════

    def _strip_spaces(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._set_status("⚠️ 請先選取要消空白的文字", "#ffc107")
            return
        selected = cursor.selectedText().replace('\u2029', '\n')
        stripped = selected.replace(" ", "").replace("　", "")
        cursor.insertText(stripped)
        self._set_status("已消除選取範圍的空白", "#0f0")

    def _reverse_glossary_replace(self) -> None:
        """Alt+E：選取範圍內的「替代文字」還原回「原文」。"""
        if self._glossary_provider is None:
            self._set_status("⚠️ 無法取得術語表", "#ffc107")
            return
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._set_status("⚠️ 請先選取要反向替代的文字", "#ffc107")
            return
        glossary_str = self._glossary_provider() or ""
        glossary = parse_glossary(glossary_str)
        if not glossary:
            self._set_status("⚠️ 術語表為空", "#ffc107")
            return
        selected = cursor.selectedText().replace('\u2029', '\n')
        new_text = apply_reverse_glossary_to_text(selected, glossary)
        if new_text == selected:
            self._set_status("ℹ️ 選取範圍中沒有可還原的術語", "#17a2b8")
            return
        cursor.insertText(new_text)
        self._apply_line_height()
        self._set_status("✅ 已反向替代", "#0f0")

    def _pad_spaces(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._set_status("⚠️ 請先選取要補空白的文字", "#ffc107")
            return
        selected = cursor.selectedText().replace('\u2029', '\n')
        lines = selected.split('\n')
        padded_lines = ["　　".join(list(line)) for line in lines]
        padded = '\n'.join(padded_lines)
        cursor.insertText(padded)
        self._set_status("已補入全形空白", "#0f0")

    # ════════════════════════════════════════════════════════════
    #  對話框工具（Phase C）
    # ════════════════════════════════════════════════════════════

    def _extend_selection_to_full_lines(self) -> QTextCursor:
        """將目前選取擴展到完整行；回傳調整後的 cursor。"""
        cursor = self.editor.textCursor()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()

        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        new_start = cursor.position()

        cursor.setPosition(end)
        # 若選到下一行的行首，退回上一行行尾
        if cursor.positionInBlock() == 0 and end > start:
            cursor.movePosition(QTextCursor.MoveOperation.Left)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        new_end = cursor.position()

        cursor.setPosition(new_start)
        cursor.setPosition(new_end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        return cursor

    def _adjust_bubble(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._set_status("⚠️ 請先選取想要調整的對話框", "#ffc107")
            return
        cursor = self._extend_selection_to_full_lines()
        selected = cursor.selectedText().replace('\u2029', '\n')
        if not selected:
            return
        result = _adjust_bubble(selected, self._measurer)
        if result is None:
            self._set_status("⚠️ 無法辨識對話框類型", "#ffc107")
            return
        if result.startswith('⚠️'):
            self._set_status(result, "#ffc107")
            return
        cursor.insertText(result)
        self._apply_line_height()
        self._set_status("✅ 對話框已修正", "#0f0")

    def _align_to_prev(self) -> None:
        cursor = self.editor.textCursor()
        line_idx = cursor.blockNumber()
        col_idx = cursor.positionInBlock()

        if line_idx < 1:
            self._set_status("⚠️ 這是第一行，沒有上一行可以對齊", "#ffc107")
            return

        doc = self.editor.document()
        prev_block = doc.findBlockByLineNumber(line_idx - 1)
        curr_block = doc.findBlockByLineNumber(line_idx)
        prev_text = prev_block.text().rstrip('\r\n \u3000\u2029')
        if not prev_text:
            self._set_status("⚠️ 上一行為空，無法對齊", "#ffc107")
            return
        curr_text = curr_block.text().rstrip('\u2029')

        result = _align_to_prev_line(
            prev_text, curr_text, col_idx, self._measurer)
        if result is None:
            self._set_status("⚠️ 游標後方沒有可以對齊的符號", "#ffc107")
            return

        new_line, new_col = result
        # 選取整行再 replace
        line_cursor = QTextCursor(curr_block)
        line_cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        line_cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock,
            QTextCursor.MoveMode.KeepAnchor,
        )
        line_cursor.insertText(new_line)
        self._apply_line_height()

        # 移到新游標位置
        new_block = self.editor.document().findBlockByLineNumber(line_idx)
        if new_block.isValid():
            cursor = self.editor.textCursor()
            cursor.setPosition(new_block.position()
                               + min(new_col, new_block.length() - 1))
            self.editor.setTextCursor(cursor)
        self._set_status("✅ 已對齊上一行", "#0f0")

    def _smart_action(self) -> None:
        """自動判斷：有多行選取→對話框修正；單行選取→上色；無選取→對齊上一行。"""
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText().replace('\u2029', '\n')
            if '\n' in selected:
                self._adjust_bubble()
            else:
                self._apply_color()
        else:
            self._align_to_prev()

    def _adjust_all_bubbles(self) -> None:
        text = self.editor.toPlainText()
        new_text, count = _adjust_all_bubbles(text, self._measurer)
        if count == 0:
            self._set_status("⚠️ 未找到可處理的獨立對話框", "#ffc107")
            return
        # 保留捲動位置與游標
        scroll_bar = self.editor.verticalScrollBar()
        scroll_val = scroll_bar.value()
        cursor_pos = self.editor.textCursor().position()
        self._replace_document(new_text)
        cursor = self.editor.textCursor()
        cursor.setPosition(min(cursor_pos,
                               self.editor.document().characterCount() - 1))
        self.editor.setTextCursor(cursor)
        scroll_bar.setValue(scroll_val)
        self._set_status(f"✅ 已自動調整 {count} 個對話框", "#0f0")

    # ════════════════════════════════════════════════════════════
    #  IPC（Phase D）
    # ════════════════════════════════════════════════════════════

    def _send_request(self, action: str, *, callback=None, **payload) -> int:
        """寫入請求到 cmd_file，註冊 callback。回傳 request id。"""
        if not self._cmd_file:
            return -1
        req_id = self._next_req_id
        self._next_req_id += 1
        req = {"id": req_id, "action": action, **payload}
        if callback is not None:
            self._pending_callbacks[req_id] = callback
        try:
            with open(self._cmd_file, 'w', encoding='utf-8') as f:
                json.dump(req, f, ensure_ascii=False)
        except OSError as e:
            self._set_status(f"❌ IPC 寫入失敗：{e}", "#dc3545")
            self._pending_callbacks.pop(req_id, None)
            return -1
        return req_id

    def _poll_reply(self) -> None:
        """每 200ms 檢查 reply_file，若存在則讀取並派送至 callback。"""
        if not self._reply_file or not os.path.exists(self._reply_file):
            return
        try:
            with open(self._reply_file, 'r', encoding='utf-8') as f:
                reply = json.load(f)
            os.remove(self._reply_file)
        except (OSError, json.JSONDecodeError) as e:
            self._set_status(f"❌ IPC 讀取失敗：{e}", "#dc3545")
            return
        req_id = reply.get("id", -1)
        callback = self._pending_callbacks.pop(req_id, None)
        if callback is not None:
            try:
                callback(reply)
            except Exception as e:
                self._set_status(f"❌ IPC callback 錯誤：{e}", "#dc3545")

    # ════════════════════════════════════════════════════════════
    #  原文比對模式
    # ════════════════════════════════════════════════════════════

    def _toggle_compare(self) -> None:
        if self._original_text is None:
            self._set_status("⚠️ 無原文可供比對", "#ffc107")
            return
        # 若預覽模式開啟，先沿用其捲動位置切換過去（不必先回編輯模式）
        if self._preview_active:
            scroll_val = self.preview_view.verticalScrollBar().value()
            self._preview_active = False
            self._set_preview_ui(False)
            self._compare_active = True
        else:
            self._compare_active = not self._compare_active
            scroll_val = None
        if self._compare_active:
            cursor = self.editor.textCursor()
            line = cursor.blockNumber()
            if scroll_val is None:
                scroll_val = self.editor.verticalScrollBar().value()
            self.stack.setCurrentIndex(1)
            # 同步游標到相同行
            block = self.orig_view.document().findBlockByLineNumber(line)
            if block.isValid():
                oc = self.orig_view.textCursor()
                oc.setPosition(block.position())
                self.orig_view.setTextCursor(oc)
            self.orig_view.verticalScrollBar().setValue(scroll_val)
            self._set_compare_ui(True)
            self._set_status("🔍 比對模式：顯示原文（Alt+1 編輯／Alt+3 預覽）", "#0f0")
        else:
            scroll_val = self.orig_view.verticalScrollBar().value()
            self.stack.setCurrentIndex(0)
            self.editor.verticalScrollBar().setValue(scroll_val)
            self._set_compare_ui(False)
            self.editor.setFocus()
            self._set_status("✏️ 編輯模式", "#0f0")

    def _set_compare_ui(self, compare: bool) -> None:
        for w in self._edit_buttons:
            w.setEnabled(not compare)
        if self._toolbar_widget is not None:
            bg = "#8b6f47" if compare else "#343a40"
            self._toolbar_widget.setStyleSheet(
                f"#mainToolbar {{ background:{bg}; }}")

    # ════════════════════════════════════════════════════════════
    #  上色預覽模式（Ctrl+E）
    # ════════════════════════════════════════════════════════════

    _COLOR_SPAN_RUN_RE = re.compile(
        r'<span style="color:([^"]+)">(.*?)</span>', re.DOTALL)

    def _render_preview_doc(self) -> None:
        """直接操作 QTextDocument 以 (文字, 顏色) runs 建構預覽，
        避免 setHtml 對 <pre> 的塊邊界渲染（每行出現橫線）。"""
        text = self.editor.toPlainText()
        self.preview_view.setFont(self.editor.font())
        doc = self.preview_view.document()
        doc.clear()
        cursor = QTextCursor(doc)
        default_fmt = QTextCharFormat()
        default_fmt.setForeground(QColor("#000000"))
        pos = 0
        for m in self._COLOR_SPAN_RUN_RE.finditer(text):
            if m.start() > pos:
                cursor.insertText(text[pos:m.start()], default_fmt)
            colored_fmt = QTextCharFormat()
            colored_fmt.setForeground(_css_color_to_qcolor(m.group(1)))
            cursor.insertText(m.group(2), colored_fmt)
            pos = m.end()
        if pos < len(text):
            cursor.insertText(text[pos:], default_fmt)
        # 套用與編輯器相同的 FixedHeight 行高，使捲動位置能對齊
        self._apply_line_height_to(self.preview_view)
        # 沿用編輯器底色與字型樣式
        fam = self._font_family
        sz = self._font_size
        self.preview_view.setStyleSheet(
            f"QTextEdit {{ background:{self._bg_color};"
            f" color:#000000;"
            f" border:1px solid #cccccc;"
            f" font-family:'{fam}';"
            f" font-size:{sz}pt; }}"
        )

    def _toggle_preview(self) -> None:
        # 若比對模式開啟，先沿用其捲動位置切換過去（不必先回編輯模式）
        if self._compare_active:
            scroll_val = self.orig_view.verticalScrollBar().value()
            self._compare_active = False
            self._set_compare_ui(False)
            self._preview_active = True
        else:
            self._preview_active = not self._preview_active
            scroll_val = None
        if self._preview_active:
            if scroll_val is None:
                scroll_val = self.editor.verticalScrollBar().value()
            self._render_preview_doc()
            self.stack.setCurrentIndex(2)
            self.preview_view.verticalScrollBar().setValue(scroll_val)
            self._set_preview_ui(True)
            self._set_status("🎨 預覽上色結果（Alt+1 編輯／Alt+2 比對）", "#0f0")
        else:
            scroll_val = self.preview_view.verticalScrollBar().value()
            self.stack.setCurrentIndex(0)
            self.editor.verticalScrollBar().setValue(scroll_val)
            self._set_preview_ui(False)
            self.editor.setFocus()
            self._set_status("✏️ 編輯模式", "#0f0")

    def _set_preview_ui(self, preview: bool) -> None:
        for w in self._edit_buttons:
            w.setEnabled(not preview)
        if self._toolbar_widget is not None:
            bg = "#4a3470" if preview else "#343a40"
            self._toolbar_widget.setStyleSheet(
                f"#mainToolbar {{ background:{bg}; }}")

    # ════════════════════════════════════════════════════════════
    #  返回編輯模式（Alt+1）
    # ════════════════════════════════════════════════════════════

    def _return_to_editor(self) -> None:
        """Alt+1：從比對/預覽模式回到編輯模式；已在編輯模式則僅 focus。"""
        if self._compare_active:
            self._toggle_compare()
        elif self._preview_active:
            self._toggle_preview()
        else:
            self.editor.setFocus()

    # ════════════════════════════════════════════════════════════
    #  從原文覆蓋選取範圍（Alt+W）
    # ════════════════════════════════════════════════════════════

    def _restore_from_original(self) -> None:
        """Alt+W：將編輯器選取範圍以原文同一 (行, 欄) 區間的內容覆蓋。
        用於翻譯時想局部還原回日文原文的情境。"""
        if self._compare_active or self._preview_active:
            self._set_status("⚠️ 請先回到編輯模式（Alt+1）", "#ffc107")
            return
        if self._original_text is None:
            self._set_status("⚠️ 無原文可供還原", "#ffc107")
            return
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._set_status("⚠️ 請先選取要以原文覆蓋的範圍", "#ffc107")
            return
        start_pos = cursor.selectionStart()
        end_pos = cursor.selectionEnd()
        doc = self.editor.document()
        sc = QTextCursor(doc)
        sc.setPosition(start_pos)
        start_line = sc.blockNumber()
        start_col = sc.positionInBlock()
        ec = QTextCursor(doc)
        ec.setPosition(end_pos)
        end_line = ec.blockNumber()
        end_col = ec.positionInBlock()

        orig_lines = self._original_text.split('\n')
        if start_line >= len(orig_lines):
            self._set_status("⚠️ 選取範圍超出原文行數", "#ffc107")
            return
        last_line = min(end_line, len(orig_lines) - 1)

        if start_line == last_line:
            line = orig_lines[start_line]
            extracted = line[min(start_col, len(line)):min(end_col, len(line))]
        else:
            first = orig_lines[start_line][min(start_col, len(orig_lines[start_line])):]
            middle = orig_lines[start_line + 1:last_line]
            last = orig_lines[last_line][:min(end_col, len(orig_lines[last_line]))]
            extracted = '\n'.join([first, *middle, last])

        cursor.insertText(extracted)
        self._set_status(f"↩️ 已以原文覆蓋（{len(extracted)} 字）", "#0f0")

    # ════════════════════════════════════════════════════════════
    #  從選取範圍提取日文並複製（Alt+C）
    # ════════════════════════════════════════════════════════════

    def _extract_jp_from_selection(self) -> None:
        """Alt+C：從選取範圍（編輯器或原文比對模式）提取日文並複製到剪貼簿。

        先複製選取內容，再以主程式「提取日文」相同的正則邏輯剔除 AA 圖形，
        只保留純文字行後以 \\n 串接寫入剪貼簿。
        """
        if self._preview_active:
            self._set_status("⚠️ 預覽模式不支援提取（請 Alt+1 回編輯或 Alt+2 切比對）", "#ffc107")
            return
        view = self.orig_view if self._compare_active else self.editor
        cursor = view.textCursor()
        if not cursor.hasSelection():
            self._set_status("⚠️ 請先選取要提取日文的範圍", "#ffc107")
            return
        selected = cursor.selectedText().replace('', '\n')
        if not selected.strip():
            self._set_status("⚠️ 選取範圍內沒有內容", "#ffc107")
            return

        if self._extract_regex_provider is None:
            self._set_status("⚠️ 無法取得提取正則設定", "#ffc107")
            return
        base_re, invalid_re, symbol_re, filter_str = self._extract_regex_provider()

        extracted_set = _extract_text(
            selected,
            base_re,
            invalid_re,
            symbol_re,
            (filter_str or "").strip(),
        )
        if not extracted_set:
            self._set_status("ℹ️ 選取範圍內未提取到日文", "#ffc107")
            return
        output = '\n'.join(extracted_set.keys())
        QApplication.clipboard().setText(output)
        self._set_status(
            f"✅ 已提取 {len(extracted_set)} 行日文並複製到剪貼簿", "#0f0")

    # ════════════════════════════════════════════════════════════
    #  其他
    # ════════════════════════════════════════════════════════════

    def _scroll_to_top(self) -> None:
        """將游標與捲軸移到文件最上方（切入編輯器時使用）。"""
        cursor = self.editor.textCursor()
        cursor.setPosition(0)
        self.editor.setTextCursor(cursor)
        self.editor.verticalScrollBar().setValue(0)
        self.editor.horizontalScrollBar().setValue(0)

    def _scroll_to_line(self, line: int) -> None:
        doc = self.editor.document()
        block = doc.findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(block.position())
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    # 將常見「亮綠」對應到 toast 風格的深綠底（與 MainWindow.show_status 對齊）
    _STATUS_COLOR_MAP = {
        "#0f0": "#28a745",
        "#00ff00": "#28a745",
    }

    def _set_status(self, msg: str, color: str = "#0f0") -> None:
        bg = self._STATUS_COLOR_MAP.get(color.lower(), color)
        show_toast(self, msg, color=bg, duration=3000)

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_dark_title_applied", False):
            if sys.platform == "win32":
                try:
                    import ctypes
                    hwnd = int(self.winId())
                    value = ctypes.c_int(1)
                    for attr in (20, 19):
                        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
                        if res == 0:
                            break
                except Exception:
                    pass
            self._dark_title_applied = True

    def _on_changed(self) -> None:
        # 強制重繪 viewport：刪除繁中 fallback 字元後 Qt 有時會殘留上一次
        # 渲染痕跡（因字身超出該行的 FixedHeight 邊界），手動觸發清除。
        self.editor.viewport().update()
        if not self._dirty:
            self._dirty = True
            self.setWindowTitle("* " + self.windowTitle().lstrip("* "))

    def _write_current(self, file_path: str) -> bool:
        text = self.editor.toPlainText()
        try:
            # 底色僅為編輯器預覽效果，不寫入檔案（交由後續網站控制）
            # 若原檔已有自訂 <head>（例如外掛字型 CSS），儲存時沿用。
            write_html_file(file_path, text, head_html=self._custom_head)
        except OSError as e:
            QMessageBox.critical(self, "儲存失敗", str(e))
            return False
        return True

    def _save_overwrite(self) -> None:
        """Ctrl+S：若已有真實檔案則直接覆寫，否則走另存新檔。"""
        if not self._html_file or self._is_temp_file:
            self._save_as()
            return
        if not self._write_current(self._html_file):
            return
        self._after_save_success(self._html_file)

    def _save_as(self) -> None:
        """儲存按鈕：跳出檔案選擇對話框。"""
        default_name = self._display_title or (
            os.path.splitext(os.path.basename(self._html_file))[0]
            if self._html_file else "未命名")
        default_name = re.sub(r'[\\/:*?"<>|]', '_', default_name) + ".html"
        # 決定預設目錄：上次使用目錄 > 原檔目錄 > 空
        if self._get_last_dir is not None:
            last_dir = self._get_last_dir() or ""
        else:
            last_dir = ""
        if not last_dir and self._html_file and not self._is_temp_file:
            last_dir = os.path.dirname(self._html_file)
        default_path = os.path.join(last_dir, default_name) if last_dir else default_name
        file_path, _ = QFileDialog.getSaveFileName(
            self, "另存新檔", default_path,
            "HTML files (*.html);;All files (*.*)")
        if not file_path:
            return
        if not self._write_current(file_path):
            return
        self._html_file = file_path
        self._is_temp_file = False
        new_dir = os.path.dirname(file_path)
        if self._on_dir_change is not None:
            try:
                self._on_dir_change(new_dir)
            except Exception:
                pass
        self._after_save_success(file_path)

    def _after_save_success(self, file_path: str) -> None:
        self._dirty = False
        base = os.path.basename(file_path)
        title = self._display_title or base
        self.setWindowTitle(f"AA 編輯器 (PyQt6) — {title}")
        self._set_status(f"✅ 已儲存：{base}")
        if self._on_save is not None:
            try:
                self._on_save(file_path)
            except Exception:
                pass

    # Backwards-compat alias used by closeEvent
    def _save(self) -> None:
        self._save_overwrite()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._dirty:
            reply = QMessageBox.question(
                self, "未儲存的變更",
                "有未儲存的變更，要儲存後再關閉嗎？",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.StandardButton.Save:
                self._save()
        event.accept()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html-file", required=True)
    parser.add_argument("--scroll-to-line", type=int, default=0)
    parser.add_argument("--cmd-file", default="")
    parser.add_argument("--reply-file", default="")
    parser.add_argument("--original-file", default="")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    load_bundled_fonts()
    win = EditWindow(
        args.html_file, args.scroll_to_line,
        cmd_file=args.cmd_file, reply_file=args.reply_file,
        original_file=args.original_file,
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
