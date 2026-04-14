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
    QColor, QFont, QFontMetricsF, QKeySequence, QShortcut,
    QTextBlockFormat, QTextCursor, QTextDocument,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QStackedWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from aa_tool.bubble_alignment import (
    adjust_bubble as _adjust_bubble,
    adjust_all_bubbles as _adjust_all_bubbles,
    align_to_prev_line as _align_to_prev_line,
)
from aa_tool.html_io import (
    read_html_bg_color, read_html_pre_content, write_html_file,
)
from aa_tool.translation_engine import (
    apply_glossary_to_text, parse_glossary,
)

LINE_HEIGHT_PERCENT = 120  # 對應 CSS line-height: 1.2
DEFAULT_BG = "#ffffff"
DEFAULT_COLOR = "#ff0000"


class QtFontMeasurer:
    """FontMeasurer 的 PyQt6 實作（使用 QFontMetricsF 量測像素寬度）。"""
    def __init__(self, font: QFont) -> None:
        self._fm = QFontMetricsF(font)

    def measure(self, text: str) -> int:
        return int(round(self._fm.horizontalAdvance(text)))

_COLOR_SPAN_OPEN_RE = re.compile(r'<span\s+style="color:[^"]*">')
_COLOR_SPAN_CLOSE = '</span>'


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
    ) -> None:
        super().__init__()
        self._html_file = html_file
        self._dirty = False
        self._current_color = DEFAULT_COLOR
        self._bg_color = DEFAULT_BG

        # ── IPC 狀態 ──
        self._cmd_file = cmd_file
        self._reply_file = reply_file
        self._next_req_id = 1
        self._pending_callbacks: dict[int, callable] = {}
        self._ipc_timer: QTimer | None = None

        # ── 原文比對狀態 ──
        self._original_text: str | None = None
        if original_file and os.path.exists(original_file):
            try:
                with open(original_file, 'r', encoding='utf-8') as f:
                    self._original_text = f.read()
            except OSError:
                self._original_text = None
        self._compare_active = False
        self._edit_buttons: list[QPushButton | QLineEdit | QCheckBox] = []
        self._toolbar_widget: QWidget | None = None

        try:
            text = read_html_pre_content(html_file) or ""
        except OSError as e:
            QMessageBox.critical(self, "讀取失敗", f"無法讀取檔案：\n{e}")
            text = ""
        loaded_bg = read_html_bg_color(html_file) if html_file else None
        if loaded_bg:
            self._bg_color = loaded_bg

        file_name = os.path.basename(html_file) if html_file else "(未命名)"
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
        aa_font = QFont("MS PGothic", 12)
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

        self.stack = QStackedWidget()
        self.stack.addWidget(self.editor)      # index 0
        self.stack.addWidget(self.orig_view)   # index 1

        self._apply_editor_colors()
        self._apply_line_height()

        self.editor.textChanged.connect(self._on_changed)
        root.addWidget(self.stack, 1)

        # ── 狀態列 ──
        self.status_label = QLabel("就緒")
        self.status_label.setStyleSheet(
            "background:#212529; color:#0f0; padding:3px 10px;"
            " font-family:Consolas;")
        root.addWidget(self.status_label)

        # ── 全域快捷鍵 ──
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self._save)
        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=self._toggle_search)
        QShortcut(QKeySequence("Esc"), self, activated=self._hide_search)
        QShortcut(QKeySequence("Ctrl+Q"), self,
                  activated=self._smart_action)
        QShortcut(QKeySequence("Ctrl+W"), self,
                  activated=self._toggle_compare)

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
        btn_smart.setToolTip("Ctrl+Q：依選取狀態自動執行對話框修正/上色/對齊")
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
        btn_save.clicked.connect(self._save)
        tb.addWidget(btn_save)

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

        layout.addStretch()

        btn_hide = _make_button("✕", "#6c757d", "#5a6268", width=30)
        btn_hide.clicked.connect(self._hide_search)
        layout.addWidget(btn_hide)

        return bar

    # ════════════════════════════════════════════════════════════
    #  格式 / 樣式
    # ════════════════════════════════════════════════════════════

    def _apply_editor_colors(self) -> None:
        self.editor.setStyleSheet(
            f"QTextEdit {{ background:{self._bg_color};"
            f" color:#000000;"
            f" border:1px solid #cccccc; }}"
        )

    def _apply_line_height(self) -> None:
        self._apply_line_height_to(self.editor)

    def _apply_line_height_to(self, widget: QTextEdit) -> None:
        cursor = QTextCursor(widget.document())
        cursor.select(QTextCursor.SelectionType.Document)
        block_fmt = QTextBlockFormat()
        block_fmt.setLineHeight(
            LINE_HEIGHT_PERCENT,
            QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
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

    def _find_next(self) -> None:
        query = self.search_entry.text()
        if not query:
            return
        found = self.editor.find(query)
        if not found:
            # wrap around
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            found = self.editor.find(query)
        if not found:
            self._set_status("🔍 找不到符合的文字", "#ffc107")
        else:
            self._set_status(f"找到：{query}", "#0f0")

    def _search_dice(self) -> None:
        self.search_entry.setText("1D10:10")
        if not self.search_bar.isVisible():
            self.search_bar.show()
        self._find_next()

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

        # 若勾選「存入術語」，透過 IPC 通知主程式
        if self.save_to_glossary_cb.isChecked() and self._cmd_file:
            self._send_request(
                "save_to_glossary", original=orig, translation=trans)

        self.quick_orig.clear()
        self.quick_trans.clear()
        self._set_status(f"✅ 已替換 {count} 處：{orig} → {trans}", "#0f0")

    def _reapply_glossary(self) -> None:
        """向主程式請求目前術語表，收到後套用到編輯內容。"""
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
        prev_text = prev_block.text().rstrip('\r\n \u3000')
        if not prev_text:
            self._set_status("⚠️ 上一行為空，無法對齊", "#ffc107")
            return
        curr_text = curr_block.text()

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
        self._compare_active = not self._compare_active
        if self._compare_active:
            cursor = self.editor.textCursor()
            line = cursor.blockNumber()
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
            self._set_status("🔍 比對模式：顯示原文（Ctrl+W 切回）", "#0f0")
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
    #  其他
    # ════════════════════════════════════════════════════════════

    def _scroll_to_line(self, line: int) -> None:
        doc = self.editor.document()
        block = doc.findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(block.position())
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def _set_status(self, msg: str, color: str = "#0f0") -> None:
        self.status_label.setText(msg)
        self.status_label.setStyleSheet(
            f"background:#212529; color:{color}; padding:3px 10px;"
            " font-family:Consolas;")

    def _on_changed(self) -> None:
        if not self._dirty:
            self._dirty = True
            self.setWindowTitle("* " + self.windowTitle().lstrip("* "))

    def _save(self) -> None:
        if not self._html_file:
            QMessageBox.warning(self, "無檔案", "沒有指定 HTML 檔案路徑")
            return
        text = self.editor.toPlainText()
        try:
            write_html_file(self._html_file, text, bg_color=self._bg_color)
        except OSError as e:
            QMessageBox.critical(self, "儲存失敗", str(e))
            return
        self._dirty = False
        self.setWindowTitle(self.windowTitle().lstrip("* "))
        self._set_status(f"✅ 已儲存：{os.path.basename(self._html_file)}")

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
    win = EditWindow(
        args.html_file, args.scroll_to_line,
        cmd_file=args.cmd_file, reply_file=args.reply_file,
        original_file=args.original_file,
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
