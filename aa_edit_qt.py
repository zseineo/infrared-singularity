"""PyQt6 編輯器（Phase A 最小可執行版本）。

獨立 process，透過 CLI 參數接收 HTML 檔案路徑、scroll-to-line。
負責：
  - 讀取 HTML <pre> 內容
  - 提供 AA 編輯區（MS PGothic 預設）
  - Ctrl+S 儲存回原檔案
  - 關閉視窗時 exit，主程式偵測後重新載入

Phase A 範圍：
  ✓ 載入 / 顯示 / 編輯 / 儲存
  ✗ 搜尋、術語、對話框工具（Phase B/C/D）

Usage:
  python aa_edit_qt.py --html-file <path> [--scroll-to-line N]
"""
from __future__ import annotations

import argparse
import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QFont, QKeySequence, QShortcut, QTextBlockFormat, QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QTextEdit, QPushButton, QVBoxLayout, QWidget,
)

# 行距百分比 — 對應 html_io 的 CSS line-height: 1.2
LINE_HEIGHT_PERCENT = 120

from aa_tool.html_io import read_html_pre_content, write_html_file


class EditWindow(QMainWindow):
    def __init__(self, html_file: str, scroll_to_line: int = 0) -> None:
        super().__init__()
        self._html_file = html_file
        self._dirty = False

        # 讀檔
        try:
            text = read_html_pre_content(html_file) or ""
        except OSError as e:
            QMessageBox.critical(self, "讀取失敗", f"無法讀取檔案：\n{e}")
            text = ""

        file_name = os.path.basename(html_file) if html_file else "(未命名)"
        self.setWindowTitle(f"AA 編輯器 (PyQt6) — {file_name}")
        self.resize(1200, 820)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 工具列 ──
        toolbar = QWidget()
        toolbar.setStyleSheet("background:#343a40;")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(10, 6, 10, 6)

        self.file_label = QLabel(file_name)
        self.file_label.setStyleSheet("color:#eee; font-weight:bold;")
        tb_layout.addWidget(self.file_label)

        tb_layout.addStretch()

        btn_save = QPushButton("💾 儲存 (Ctrl+S)")
        btn_save.setStyleSheet(
            "background:#28a745; color:white; padding:4px 10px;"
            "border:none; border-radius:4px;")
        btn_save.clicked.connect(self._save)
        tb_layout.addWidget(btn_save)

        btn_close = QPushButton("關閉")
        btn_close.setStyleSheet(
            "background:#dc3545; color:white; padding:4px 10px;"
            "border:none; border-radius:4px;")
        btn_close.clicked.connect(self.close)
        tb_layout.addWidget(btn_close)

        root.addWidget(toolbar)

        # ── 編輯區 ──
        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)
        self.editor.setPlainText(text)
        self.editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        # 關閉 tab 轉 space，AA 不應有 tab
        self.editor.setTabChangesFocus(False)
        self.editor.setStyleSheet(
            "QTextEdit { background:#ffffff; color:#000000;"
            " border:1px solid #cccccc; }")

        # AA 顯示字型：MS PGothic 12pt（已驗證可接受）
        aa_font = QFont("MS PGothic", 12)
        aa_font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.editor.setFont(aa_font)

        # 套用行距（對整份文件的所有 block 設定 line-height 120%）
        self._apply_line_height()

        self.editor.textChanged.connect(self._on_changed)
        root.addWidget(self.editor, 1)

        # ── 狀態列 ──
        self.status_label = QLabel("就緒")
        self.status_label.setStyleSheet(
            "background:#212529; color:#0f0; padding:3px 10px;"
            "font-family:Consolas;")
        root.addWidget(self.status_label)

        # ── 捷徑 ──
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self._save)

        # 跳到指定行
        if scroll_to_line and scroll_to_line > 0:
            self._scroll_to_line(scroll_to_line)

    def _apply_line_height(self) -> None:
        """對整份文件套用行距（以 ProportionalHeight 模式，LINE_HEIGHT_PERCENT %）。"""
        cursor = QTextCursor(self.editor.document())
        cursor.select(QTextCursor.SelectionType.Document)
        block_fmt = QTextBlockFormat()
        block_fmt.setLineHeight(
            LINE_HEIGHT_PERCENT,
            QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
        )
        cursor.mergeBlockFormat(block_fmt)

    def _scroll_to_line(self, line: int) -> None:
        doc = self.editor.document()
        block = doc.findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(block.position())
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

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
            write_html_file(self._html_file, text)
        except OSError as e:
            QMessageBox.critical(self, "儲存失敗", str(e))
            return
        self._dirty = False
        self.setWindowTitle(self.windowTitle().lstrip("* "))
        self.status_label.setText(
            f"✅ 已儲存：{os.path.basename(self._html_file)}")

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
    # 保留給 Phase D 用（目前忽略）
    parser.add_argument("--cmd-file", default="")
    parser.add_argument("--reply-file", default="")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    win = EditWindow(args.html_file, args.scroll_to_line)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
