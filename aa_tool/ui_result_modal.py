"""最終結果預覽視窗 — PyQt6 UI 模組。

提供兩種使用方式：
  1. ResultModalDialog — 獨立全螢幕預覽對話框
  2. ResultEditWidget  — 可內嵌在主視窗分頁中的 QWidget

show_result_modal() 為向後相容的入口函式。
"""
from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut, QTextBlockFormat, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QDialog, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .font_measure import QtFontMeasurer
from .qt_helpers import make_button, show_toast
from .qt_text_utils import expand_selection_to_lines, find_text, get_line_col, get_line_text, move_to_line
from .bubble_alignment import (
    adjust_bubble as _adjust_bubble,
    adjust_all_bubbles as _adjust_all_bubbles,
    align_to_prev_line as _align_to_prev_line,
)
from .translation_engine import (
    parse_glossary,
    apply_glossary_to_text,
    _replace_with_padding,
)

if TYPE_CHECKING:
    from ..aa_translation_tool import AATranslationTool


# ════════════════════════════════════════════════════════════════
#  核心編輯器元件
# ════════════════════════════════════════════════════════════════

class ResultEditorCore(QWidget):
    """結果預覽 / 編輯器核心 — 包含工具列、搜尋列、文字框。

    可被 ResultModalDialog 或 ResultEditWidget 嵌入使用。
    """

    def __init__(
        self,
        app: AATranslationTool,
        text: str,
        source_file: str = "",
        close_callback=None,
        toast_parent: QWidget | None = None,
        is_tab: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.app = app
        self.source_file = source_file
        self._close_callback = close_callback
        self._toast_parent = toast_parent or self
        self._is_tab = is_tab

        # 字體 — AA 依賴 GDI 渲染（由 main() 設定 fontengine=gdi）
        self._result_font = QFont("MS PGothic", 16)
        self._ui_font = QFont("Microsoft JhengHei", 14, QFont.Weight.Bold)
        self._ui_small_font = QFont("Microsoft JhengHei", 12)
        self._font_measurer = QtFontMeasurer(self._result_font)

        self._current_color = "#ff0000"

        self._build_ui(text)
        self._setup_shortcuts()

    # ────────────────────────────────────────────────────
    #  UI 建構
    # ────────────────────────────────────────────────────

    def _build_ui(self, text: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 工具列 ──
        self._build_toolbar(layout)

        # ── 搜尋列（預設隱藏）──
        self._build_search_bar(layout)

        # ── 文字編輯框 ──
        self.textbox = QPlainTextEdit()
        self.textbox.setFont(self._result_font)
        self.textbox.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.textbox.setUndoRedoEnabled(True)
        self.textbox.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {self.app.bg_color};
                color: {self.app.fg_color};
                border: none;
                padding: 10px;
            }}
        """)

        self.textbox.setPlainText(text)

        # 行間距 — 對應 tkinter spacing1=2, spacing3=2
        cursor = self.textbox.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        block_fmt = QTextBlockFormat()
        block_fmt.setTopMargin(2)
        block_fmt.setBottomMargin(2)
        cursor.mergeBlockFormat(block_fmt)
        cursor.clearSelection()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.textbox.setTextCursor(cursor)
        layout.addWidget(self.textbox, 1)

    def _build_toolbar(self, parent_layout: QVBoxLayout):
        toolbar = QWidget()
        toolbar.setStyleSheet("background-color: #343a40;")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(10, 5, 10, 5)

        # ── 群組 1：全文替換 + 術語 ──
        self._build_grp1(tb_layout)

        # ── 群組 2：選區操作 ──
        self._build_grp2(tb_layout)

        tb_layout.addStretch()

        # ── 群組 3：右側工具 ──
        self._build_grp3(tb_layout)

        parent_layout.addWidget(toolbar)

    def _build_grp1(self, tb_layout: QHBoxLayout):
        lbl = QLabel("全文替換")
        lbl.setFont(self._ui_font)
        lbl.setStyleSheet("color: white;")
        tb_layout.addWidget(lbl)

        self.quick_orig = QLineEdit()
        self.quick_orig.setPlaceholderText("原文")
        self.quick_orig.setFixedWidth(120)
        self.quick_orig.setFont(self._ui_font)
        tb_layout.addWidget(self.quick_orig)

        self.quick_trans = QLineEdit()
        self.quick_trans.setPlaceholderText("翻譯")
        self.quick_trans.setFixedWidth(120)
        self.quick_trans.setFont(self._ui_font)
        tb_layout.addWidget(self.quick_trans)

        self.save_to_glossary_cb = QCheckBox("存入術語表")
        self.save_to_glossary_cb.setChecked(True)
        self.save_to_glossary_cb.setFont(self._ui_small_font)
        self.save_to_glossary_cb.setStyleSheet("color: white;")
        tb_layout.addWidget(self.save_to_glossary_cb)

        btn_exec = make_button("執行", color="#17a2b8", hover="#138496", font=self._ui_small_font, width=50)
        btn_exec.clicked.connect(self._quick_replace)
        tb_layout.addWidget(btn_exec)

        btn_glossary = make_button("重套術語", color="#28a745", hover="#218838", font=self._ui_small_font, width=60)
        btn_glossary.clicked.connect(self._reapply_glossary)
        tb_layout.addWidget(btn_glossary)

    def _build_grp2(self, tb_layout: QHBoxLayout):
        # 顏色選擇按鈕
        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(40)
        self.color_btn.setStyleSheet(f"background-color: {self._current_color}; border: none; border-radius: 4px;")
        self.color_btn.clicked.connect(self._choose_color)
        tb_layout.addWidget(self.color_btn)

        btn_color = make_button("上色", color="#6f42c1", hover="#5a32a3", font=self._ui_small_font, width=60)
        btn_color.clicked.connect(self._apply_color)
        tb_layout.addWidget(btn_color)

        btn_strip = make_button("消空白", color="#e0a800", hover="#c82333", font=self._ui_small_font, text_color="black", width=60)
        btn_strip.clicked.connect(self._strip_spaces)
        tb_layout.addWidget(btn_strip)

        btn_add_sp = make_button("補空白", color="#17a2b8", hover="#138496", font=self._ui_small_font, width=60)
        btn_add_sp.clicked.connect(self._add_double_spaces)
        tb_layout.addWidget(btn_add_sp)

        btn_bubble = make_button("對話框修正", color="#28a745", hover="#218838", font=self._ui_small_font, width=80)
        btn_bubble.clicked.connect(self._adjust_bubble)
        tb_layout.addWidget(btn_bubble)

        btn_align = make_button("對齊上一行", color="#17a2b8", hover="#138496", font=self._ui_small_font, width=80)
        btn_align.clicked.connect(self._align_to_prev_line)
        tb_layout.addWidget(btn_align)

        btn_smart = make_button("自動判斷", color="#e67e22", hover="#d35400", font=self._ui_small_font, width=70)
        btn_smart.clicked.connect(self._smart_action)
        tb_layout.addWidget(btn_smart)

        btn_all = make_button("對話框(全)", color="#20c997", hover="#17a085", font=self._ui_small_font, width=80)
        btn_all.clicked.connect(self._adjust_all_bubbles)
        tb_layout.addWidget(btn_all)

    def _build_grp3(self, tb_layout: QHBoxLayout):
        btn_bg = make_button("底色", color="#6c757d", hover="#5a6268", font=self._ui_small_font, width=45)
        btn_bg.clicked.connect(self._choose_bg_color)
        tb_layout.addWidget(btn_bg)

        btn_fg = make_button("文字色", color="#17a2b8", hover="#138496", font=self._ui_small_font, width=45)
        btn_fg.clicked.connect(self._choose_fg_color)
        tb_layout.addWidget(btn_fg)

        btn_save = make_button("💾 儲存", color="#28a745", hover="#218838", font=self._ui_small_font, width=60)
        btn_save.clicked.connect(self._dl_html)
        tb_layout.addWidget(btn_save)

        close_text = "↩ 返回" if self._is_tab else "✖ 關閉"
        btn_close = make_button(close_text, color="#dc3545", hover="#c82333", font=self._ui_small_font, width=60)
        btn_close.clicked.connect(self._close)
        tb_layout.addWidget(btn_close)

    def _build_search_bar(self, parent_layout: QVBoxLayout):
        self.search_frame = QWidget()
        self.search_frame.setVisible(False)
        sf_layout = QHBoxLayout(self.search_frame)
        sf_layout.setContentsMargins(10, 0, 10, 5)

        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("搜尋...")
        self.search_entry.setFont(self._ui_small_font)
        self.search_entry.setFixedWidth(200)
        self.search_entry.returnPressed.connect(self._find_next)
        sf_layout.addWidget(self.search_entry)

        btn_next = make_button("下一個", color="#007bff", hover="#0056b3", font=self._ui_small_font, width=60)
        btn_next.clicked.connect(self._find_next)
        sf_layout.addWidget(btn_next)

        btn_dice = make_button("🎲 1D10:10", color="#f39c12", hover="#d68910", font=self._ui_small_font, width=80)
        btn_dice.clicked.connect(self._search_dice)
        sf_layout.addWidget(btn_dice)

        sf_layout.addStretch()
        parent_layout.addWidget(self.search_frame)

    # ────────────────────────────────────────────────────
    #  快捷鍵
    # ────────────────────────────────────────────────────

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Q"), self, self._smart_action)
        QShortcut(QKeySequence("Ctrl+F"), self, self._toggle_search)
        QShortcut(QKeySequence("Ctrl+S"), self, self._dl_html)

    # ────────────────────────────────────────────────────
    #  Toast 輔助
    # ────────────────────────────────────────────────────

    def _toast(self, message: str, color: str = "#28a745", duration: int = 3000):
        show_toast(self._toast_parent, message, color=color, duration=duration)

    # ────────────────────────────────────────────────────
    #  關閉
    # ────────────────────────────────────────────────────

    def _close(self):
        self.app.preview_text_cache = self.textbox.toPlainText()
        self.app.save_cache()
        if self._close_callback:
            self._close_callback()

    # ────────────────────────────────────────────────────
    #  群組 1：全文替換 + 術語
    # ────────────────────────────────────────────────────

    def _quick_replace(self):
        orig = self.quick_orig.text().strip()
        trans = self.quick_trans.text().strip()
        if not orig or not trans:
            self._toast("⚠️ 不可為空！", color="#f39c12")
            return

        len_diff = len(orig) - len(trans)
        padded_trans = trans + ('　' * len_diff if len_diff > 0 else '')

        scroll_val = self.textbox.verticalScrollBar().value()
        current_text = self.textbox.toPlainText()
        lines = current_text.split('\n')
        for i in range(len(lines)):
            lines[i] = _replace_with_padding(lines[i], orig, trans, padded_trans)

        self.textbox.setPlainText('\n'.join(lines))
        self.textbox.verticalScrollBar().setValue(scroll_val)

        if self.save_to_glossary_cb.isChecked():
            g_text = self.app.glossary_text.toPlainText().strip()
            if g_text:
                g_text += '\n'
            g_text += f"{orig}={trans}"
            self.app.glossary_text.setPlainText(g_text)
            self.app.save_cache()

        self.quick_orig.clear()
        self.quick_trans.clear()

    def _reapply_glossary(self):
        glossary_str = self.app.get_combined_glossary()
        if not glossary_str:
            self._toast("⚠️ 術語表為空！", color="#f39c12")
            return

        glossary = parse_glossary(glossary_str)
        if not glossary:
            self._toast("⚠️ 術語表格式不正確或為空！", color="#f39c12")
            return

        scroll_val = self.textbox.verticalScrollBar().value()
        current_text = self.textbox.toPlainText()
        new_text = apply_glossary_to_text(current_text, glossary)
        self.textbox.setPlainText(new_text)
        self.textbox.verticalScrollBar().setValue(scroll_val)
        self.app.save_cache()
        self._toast("✅ 已套用術語表變更！")

    # ────────────────────────────────────────────────────
    #  群組 2：選區操作
    # ────────────────────────────────────────────────────

    def _choose_color(self):
        from PyQt6.QtGui import QColor
        color = QColorDialog.getColor(QColor(self._current_color), self, "選擇顏色")
        if color.isValid():
            self._current_color = color.name()
            self.color_btn.setStyleSheet(
                f"background-color: {self._current_color}; border: none; border-radius: 4px;"
            )

    def _apply_color(self):
        cursor = self.textbox.textCursor()
        selected = cursor.selectedText()
        if not selected:
            self._toast("⚠️ 請先選取想要上色的文字！", color="#f39c12")
            return

        if re.search(r'<span style="color:[^"]*">', selected):
            stripped = re.sub(r'<span style="color:[^"]*">', '', selected)
            stripped = stripped.replace('</span>', '')
            cursor.insertText(stripped)
        else:
            colored = f'<span style="color:{self._current_color}">{selected}</span>'
            cursor.insertText(colored)

    def _strip_spaces(self):
        cursor = self.textbox.textCursor()
        selected = cursor.selectedText()
        if not selected:
            self._toast("⚠️ 請先選取想要消除空白的文字！", color="#f39c12")
            return
        stripped = selected.replace(" ", "").replace("　", "")
        cursor.insertText(stripped)

    def _add_double_spaces(self):
        cursor = self.textbox.textCursor()
        selected = cursor.selectedText()
        if not selected:
            self._toast("⚠️ 請先選取想要補空白的文字！", color="#f39c12")
            return
        # QPlainTextEdit 用 \u2029 表示段落分隔，轉回 \n
        lines = selected.replace('\u2029', '\n').split('\n')
        spaced_lines = ["　　".join(list(line)) for line in lines]
        spaced_text = '\n'.join(spaced_lines)
        cursor.insertText(spaced_text)

    def _adjust_bubble(self):
        cursor = expand_selection_to_lines(self.textbox)
        selected = cursor.selectedText()
        if not selected:
            self._toast("⚠️ 請先選取想要調整的對話框！", color="#f39c12")
            return
        # QPlainTextEdit 的 selectedText 用 \u2029 作為換行
        selected = selected.replace('\u2029', '\n')
        result = _adjust_bubble(selected, self._font_measurer)
        if result is None:
            self._toast("⚠️ 無法辨識對話框類型！", color="#f39c12")
        elif result.startswith('⚠️'):
            self._toast(result, color="#f39c12")
        else:
            cursor.insertText(result)

    def _align_to_prev_line(self):
        cursor = self.textbox.textCursor()
        line_idx, col_idx = get_line_col(cursor)

        if line_idx < 2:
            self._toast("⚠️ 這是第一行，沒有上一行可以對齊！", color="#f39c12")
            return

        prev_line_text = get_line_text(self.textbox, line_idx - 1).rstrip('\r\n \u3000')
        if not prev_line_text:
            self._toast("⚠️ 上一行為空，無法對齊！", color="#f39c12")
            return

        current_line_text = get_line_text(self.textbox, line_idx)
        align_result = _align_to_prev_line(prev_line_text, current_line_text, col_idx, self._font_measurer)
        if align_result is None:
            self._toast("⚠️ 游標後方沒有可以對齊的符號！", color="#f39c12")
            return

        new_line, new_col = align_result
        # 選取整行並替換
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_line)
        # 移到新欄位
        move_to_line(self.textbox, line_idx, new_col)
        self.textbox.ensureCursorVisible()

    def _smart_action(self):
        cursor = self.textbox.textCursor()
        selected = cursor.selectedText()
        if selected:
            if '\u2029' in selected or '\n' in selected:
                self._adjust_bubble()
            else:
                self._apply_color()
        else:
            self._align_to_prev_line()

    def _adjust_all_bubbles(self):
        text_content = self.textbox.toPlainText()
        new_text, count = _adjust_all_bubbles(text_content, self._font_measurer)

        if count == 0:
            self._toast("⚠️ 未找到可處理的獨立對話框！", color="#f39c12")
            return

        scroll_val = self.textbox.verticalScrollBar().value()
        cursor_line, cursor_col = get_line_col(self.textbox.textCursor())
        self.textbox.setPlainText(new_text)
        move_to_line(self.textbox, cursor_line, cursor_col)
        self.textbox.verticalScrollBar().setValue(scroll_val)
        self._toast(f"✅ 已自動調整 {count} 個對話框！")

    # ────────────────────────────────────────────────────
    #  群組 3：右側工具
    # ────────────────────────────────────────────────────

    def _choose_bg_color(self):
        from PyQt6.QtGui import QColor
        color = QColorDialog.getColor(QColor(self.app.bg_color), self, "選擇背景顏色")
        if color.isValid():
            self.app.bg_color = color.name()
            self.textbox.setStyleSheet(f"""
                QPlainTextEdit {{
                    background-color: {self.app.bg_color};
                    color: {self.app.fg_color};
                    border: none; padding: 10px;
                }}
            """)
            self.app.save_cache()

    def _choose_fg_color(self):
        from PyQt6.QtGui import QColor
        color = QColorDialog.getColor(QColor(self.app.fg_color), self, "選擇文字顏色")
        if color.isValid():
            self.app.fg_color = color.name()
            self.textbox.setStyleSheet(f"""
                QPlainTextEdit {{
                    background-color: {self.app.bg_color};
                    color: {self.app.fg_color};
                    border: none; padding: 10px;
                }}
            """)
            self.app.save_cache()

    def _dl_html(self):
        raw_text = self.textbox.toPlainText()
        if not raw_text:
            self._toast("⚠️ 預覽視窗沒有內容！", color="#f39c12")
            return

        if self.source_file:
            init_name = os.path.basename(self.source_file)
        else:
            title_val = self.app.doc_title.text().strip()
            num_val = self.app.doc_num.text().strip()
            if title_val and num_val:
                init_name = f"{title_val}_{num_val}.html"
            elif title_val:
                init_name = f"{title_val}.html"
            elif num_val:
                init_name = f"AA_Result_{num_val}.html"
            else:
                init_name = "AA_Result.html"

        file_path, _ = QFileDialog.getSaveFileName(
            self, "儲存 HTML 檔案", init_name,
            "HTML files (*.html)",
        )
        if file_path:
            try:
                self.app.write_html_file(file_path, raw_text)
                self._toast("✅ 已儲存 HTML 檔案！")
            except Exception as e:
                self._toast(f"❌ 無法儲存: {e}", color="#dc3545", duration=5000)

    # ────────────────────────────────────────────────────
    #  搜尋
    # ────────────────────────────────────────────────────

    def _toggle_search(self):
        visible = self.search_frame.isVisible()
        self.search_frame.setVisible(not visible)
        if not visible:
            self.search_entry.setFocus()

    def _find_next(self):
        query = self.search_entry.text()
        if not query:
            return
        if not find_text(self.textbox, query, wrap=True):
            self._toast("🔍 找不到符合的文字。", color="#17a2b8")

    def _search_dice(self):
        self.search_entry.setText("1D10:10")
        self.search_frame.setVisible(True)
        self._find_next()

    # ────────────────────────────────────────────────────
    #  捲動到指定行
    # ────────────────────────────────────────────────────

    def scroll_to_line(self, line: int):
        """捲動到指定行並選取該行。"""
        def _do_scroll():
            cursor = move_to_line(self.textbox, line)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            self.textbox.setTextCursor(cursor)
            self.textbox.ensureCursorVisible()
        QTimer.singleShot(100, _do_scroll)


# ════════════════════════════════════════════════════════════════
#  對話框模式
# ════════════════════════════════════════════════════════════════

class ResultModalDialog(QDialog):
    """全螢幕預覽對話框。"""

    def __init__(
        self,
        app: AATranslationTool,
        text: str,
        source_file: str = "",
        scroll_to_line: int | None = None,
    ):
        super().__init__(app)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)

        # 標題
        chapter_str = ""
        match = re.search(r'第\s*(\d+)\s*話', text[:500])
        if match:
            chapter_str = f" - 第{match.group(1)}話"
        else:
            match = re.search(r'番外編\s*(\d+)', text[:500])
            if match:
                chapter_str = f" - 番外編{match.group(1)}"
        self.setWindowTitle(f"✨ 最終結果預覽 (全螢幕){chapter_str}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.editor = ResultEditorCore(
            app, text, source_file,
            close_callback=self.close,
            toast_parent=self,
            is_tab=False,
            parent=self,
        )
        layout.addWidget(self.editor)

        # Esc 關閉
        QShortcut(QKeySequence("Escape"), self, self.close)

        # 最大化
        self.showMaximized()

        if scroll_to_line is not None:
            self.editor.scroll_to_line(scroll_to_line)

    def closeEvent(self, event):
        self.editor._close()
        event.accept()


# ════════════════════════════════════════════════════════════════
#  內嵌分頁模式
# ════════════════════════════════════════════════════════════════

class ResultEditWidget(QWidget):
    """可內嵌在主視窗的結果編輯元件。"""

    def __init__(
        self,
        app: AATranslationTool,
        text: str,
        source_file: str = "",
        scroll_to_line: int | None = None,
    ):
        super().__init__()
        self.app = app

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.editor = ResultEditorCore(
            app, text, source_file,
            close_callback=lambda: app.switch_mode(app._previous_mode),
            toast_parent=app,
            is_tab=True,
            parent=self,
        )
        layout.addWidget(self.editor)

        # 給主視窗存取
        app.edit_tab_textbox = self.editor.textbox

        if scroll_to_line is not None:
            self.editor.scroll_to_line(scroll_to_line)


# ════════════════════════════════════════════════════════════════
#  向後相容入口
# ════════════════════════════════════════════════════════════════

def show_result_modal(
    app: AATranslationTool,
    text: str,
    source_file: str = "",
    scroll_to_line: int | None = None,
) -> None:
    """建立並顯示最終結果預覽視窗（或內嵌分頁）。"""
    use_tab = hasattr(app, 'experimental_edit_tab') and app.experimental_edit_tab.isChecked()

    if use_tab:
        # 清除舊的內嵌編輯元件
        if hasattr(app, '_edit_widget'):
            app._edit_widget.deleteLater()

        widget = ResultEditWidget(app, text, source_file, scroll_to_line)
        app._edit_widget = widget

        # 將 widget 放入 edit_frame
        if app.edit_frame.layout() is None:
            from PyQt6.QtWidgets import QVBoxLayout as _QVBox
            app.edit_frame.setLayout(_QVBox())
            app.edit_frame.layout().setContentsMargins(0, 0, 0, 0)
        else:
            # 清除舊 children
            while app.edit_frame.layout().count():
                item = app.edit_frame.layout().takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        app.edit_frame.layout().addWidget(widget)
        app.switch_mode("edit")
    else:
        dialog = ResultModalDialog(app, text, source_file, scroll_to_line)
        dialog.exec()
