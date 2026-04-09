"""文字處理工具 — 去重複行 / 提取裝飾字元。"""
import re

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPlainTextEdit, QPushButton, QFrame, QWidget, QApplication,
)

from aa_tool.qt_helpers import make_button


class AADedupTool(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_app = parent
        self.setWindowTitle("文字處理工具")
        self.resize(600, 650)

        self.ui_font = QFont("Microsoft JhengHei", 14)
        self.ui_font_bold = QFont("Microsoft JhengHei", 13, QFont.Weight.Bold)
        self.mono_font = QFont("Consolas", 12)

        self._build_ui()

    def _build_ui(self):
        layout = QGridLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # ── Row 0: 正規表達式顯示 ──
        top_frame = QFrame()
        top_layout = QVBoxLayout(top_frame)
        top_layout.setContentsMargins(5, 5, 5, 5)

        header = QLabel("目前載入的正規表達式 (僅供檢視):")
        header.setFont(self.ui_font_bold)
        top_layout.addWidget(header)

        self.info_text = QPlainTextEdit()
        self.info_text.setFont(self.mono_font)
        self.info_text.setReadOnly(True)
        self.info_text.setFixedHeight(90)
        self.info_text.setStyleSheet(
            "QPlainTextEdit { background-color: #1e2b3c; color: #a0aab5; }"
        )
        top_layout.addWidget(self.info_text)
        self._refresh_info_text()

        layout.addWidget(top_frame, 0, 0, 1, 2)

        # ── Row 1 Left: 輸入 ──
        left_frame = QFrame()
        left_layout = QVBoxLayout(left_frame)
        left_layout.setContentsMargins(5, 5, 5, 5)

        lbl_input = QLabel("原始文字 (每行獨立):")
        lbl_input.setFont(self.ui_font)
        left_layout.addWidget(lbl_input)

        self.input_text = QPlainTextEdit()
        self.input_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        left_layout.addWidget(self.input_text)

        layout.addWidget(left_frame, 1, 0)

        # ── Row 1 Right: 輸出 ──
        right_frame = QFrame()
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(5, 5, 5, 5)

        right_top = QHBoxLayout()
        lbl_output = QLabel("去重複後結果:")
        lbl_output.setFont(self.ui_font)
        right_top.addWidget(lbl_output)
        right_top.addStretch()

        self.count_label = QLabel("行數: 0")
        self.count_label.setFont(self.ui_font)
        self.count_label.setStyleSheet("color: #17a2b8;")
        right_top.addWidget(self.count_label)
        right_layout.addLayout(right_top)

        self.output_text = QPlainTextEdit()
        self.output_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output_text.setStyleSheet(
            "QPlainTextEdit { background-color: #2a3b4c; }"
        )
        right_layout.addWidget(self.output_text)

        layout.addWidget(right_frame, 1, 1)

        # ── Row 2: 狀態 ──
        self.status_label = QLabel("")
        self.status_label.setFont(self.ui_font)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #e67e22;")
        layout.addWidget(self.status_label, 2, 0, 1, 2)

        # ── Row 3: 按鈕 ──
        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_dedup = make_button(
            "🚀 移除完全重複行", color="#28a745", hover="#218838",
            font=self.ui_font, width=170,
        )
        btn_dedup.setFixedHeight(40)
        btn_dedup.clicked.connect(self.dedup_lines)
        btn_layout.addWidget(btn_dedup)

        btn_extract = make_button(
            "🔍 提取裝飾字元", color="#e67e22", hover="#d35400",
            font=self.ui_font, width=170,
        )
        btn_extract.setFixedHeight(40)
        btn_extract.clicked.connect(self.extract_symbols)
        btn_layout.addWidget(btn_extract)

        btn_copy = make_button(
            "📋 複製結果", color="#007bff", hover="#0056b3",
            font=self.ui_font, width=120,
        )
        btn_copy.setFixedHeight(40)
        btn_copy.clicked.connect(self.copy_result)
        btn_layout.addWidget(btn_copy)

        btn_widget = QWidget()
        btn_widget.setLayout(btn_layout)
        layout.addWidget(btn_widget, 3, 0, 1, 2)

        # Grid stretch
        layout.setRowStretch(1, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

    def _refresh_info_text(self):
        regex_content = (
            f"【基本分段 Base Regex】\n{self.parent_app.current_base_regex}\n\n"
            f"【無效符號 Invalid Regex】\n{self.parent_app.current_invalid_regex}\n\n"
            f"【獨立符號 Symbol Regex】\n{self.parent_app.current_symbol_regex}"
        )
        self.info_text.setReadOnly(False)
        self.info_text.setPlainText(regex_content)
        self.info_text.setReadOnly(True)

    def dedup_lines(self):
        input_content = self.input_text.toPlainText().strip("\n")
        if not input_content:
            return

        lines = input_content.split("\n")
        seen = set()
        deduped_lines = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                deduped_lines.append(line)

        self.output_text.setPlainText("\n".join(deduped_lines))
        self.count_label.setText(f"行數: {len(deduped_lines)}")
        self.status_label.setText(f"已成功移除重複行，共保留 {len(deduped_lines)} 行")
        self.status_label.setStyleSheet("color: #28a745;")

    def copy_result(self):
        result = self.output_text.toPlainText().strip("\n")
        if result:
            QApplication.clipboard().setText(result)
            self.status_label.setText("已複製到剪貼簿")
            self.status_label.setStyleSheet("color: #007bff;")

    def extract_symbols(self):
        input_content = self.input_text.toPlainText().strip("\n")
        if not input_content:
            return

        lines = input_content.split("\n")
        cleaned_symbols = []
        for line in lines:
            cleaned = re.sub(r'^\d+[\s|,\.]*', '', line).strip()
            if cleaned:
                cleaned_symbols.append(cleaned)

        if not cleaned_symbols:
            self.output_text.setPlainText("未找到任何裝飾字元")
            self.count_label.setText("行數: 0")
            self.status_label.setText("沒有提取出有效的符號！")
            self.status_label.setStyleSheet("color: #dc3545;")
            return

        self.output_text.setPlainText("\n".join(cleaned_symbols))
        self.count_label.setText(f"行數: {len(cleaned_symbols)}")

        # 加入主程式的 invalid_regex
        current_invalid = self.parent_app.current_invalid_regex

        unique_new_chars = set()
        for sym in cleaned_symbols:
            for char in sym:
                unique_new_chars.add(char)

        added_count = 0
        added_items = []

        first_bracket = current_invalid.find('[')
        last_bracket = current_invalid.rfind(']')

        if first_bracket != -1 and last_bracket != -1 and first_bracket < last_bracket:
            prefix = current_invalid[:first_bracket + 1]
            existing_chars = current_invalid[first_bracket + 1:last_bracket]
            suffix = current_invalid[last_bracket:]

            for char in unique_new_chars:
                if char not in existing_chars:
                    added_items.append(char)

            if added_items:
                added_str = "".join(added_items)
                if existing_chars.endswith('-'):
                    existing_chars = existing_chars[:-1] + added_str + "-"
                else:
                    existing_chars += added_str
                current_invalid = prefix + existing_chars + suffix
                added_count = len(added_items)
        else:
            for sym in cleaned_symbols:
                parts = current_invalid.split('|')
                if sym not in parts and re.escape(sym) not in parts:
                    parts.append(sym)
                    current_invalid = "|".join(parts)
                    added_count += 1
                    added_items.append(sym)

        if added_count > 0:
            self.parent_app.current_invalid_regex = current_invalid
            self.parent_app.save_regex_to_settings()
            self._refresh_info_text()
            self.status_label.setText(f"成功加入了 {added_count} 個新裝飾字元到過濾名單中！")
            self.status_label.setStyleSheet("color: #28a745;")
        else:
            self.status_label.setText("提取的裝飾字元都已經存在於過濾名單中了。")
            self.status_label.setStyleSheet("color: #17a2b8;")
