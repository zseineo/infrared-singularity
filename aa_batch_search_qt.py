"""AA 漫畫翻譯輔助工具 — 獨立 PyQt6 批次搜尋。

以 subprocess 方式從 customtkinter 主程式啟動，
透過 JSON 命令檔（IPC）與主程式溝通「開啟」操作。

用法：
    python aa_batch_search_qt.py --folder <path> --cmd-file <path>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from aa_tool.html_io import read_html_pre_content, write_html_file
from aa_tool.qt_helpers import make_button, show_toast

# ════════════════════════════════════════════════════════════════
#  QSS 載入
# ════════════════════════════════════════════════════════════════

def _load_qss() -> str:
    qss_path = os.path.join(os.path.dirname(__file__), "aa_tool", "dark_theme.qss")
    if os.path.exists(qss_path):
        with open(qss_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


# ════════════════════════════════════════════════════════════════
#  主視窗
# ════════════════════════════════════════════════════════════════

class BatchSearchWindow(QMainWindow):
    # 背景執行緒 → 主執行緒的信號
    _sig_progress = pyqtSignal(str)
    _sig_done = pyqtSignal(list, int)

    def __init__(self, *, folder: str = "", cmd_file: str = ""):
        super().__init__()
        self.setWindowTitle("AA 批次搜尋")
        self.resize(1000, 700)

        self._cmd_file = cmd_file

        # 連接信號
        self._sig_progress.connect(self._on_progress)
        self._sig_done.connect(self._search_done)

        # 字體
        self.ui_font = QFont("Microsoft JhengHei", 14, QFont.Weight.Bold)
        self.ui_small_font = QFont("Microsoft JhengHei", 12)

        self.batch_matches: list[dict] = []

        self._build_ui()

        if folder:
            self.folder_entry.setText(folder)

    # ────────────────────────────────────────────────────────
    #  UI 建構
    # ────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        # 資料夾選擇
        folder_row = QHBoxLayout()
        lbl_folder = QLabel("資料夾:")
        lbl_folder.setFont(self.ui_font)
        folder_row.addWidget(lbl_folder)

        self.folder_entry = QLineEdit()
        self.folder_entry.setFont(self.ui_small_font)
        folder_row.addWidget(self.folder_entry, 1)

        btn_browse = make_button("瀏覽…", color="#6c757d", hover="#5a6268",
                                 font=self.ui_small_font, width=70)
        btn_browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(btn_browse)
        layout.addLayout(folder_row)

        # 搜尋 / 替換列
        search_row = QHBoxLayout()
        lbl_search = QLabel("搜尋:")
        lbl_search.setFont(self.ui_font)
        search_row.addWidget(lbl_search)

        self.search_entry = QLineEdit()
        self.search_entry.setFont(self.ui_small_font)
        self.search_entry.setFixedWidth(250)
        self.search_entry.returnPressed.connect(self._do_search)
        search_row.addWidget(self.search_entry)

        self.regex_switch = QCheckBox("正則")
        self.regex_switch.setFont(self.ui_small_font)
        search_row.addWidget(self.regex_switch)

        lbl_replace = QLabel("替換:")
        lbl_replace.setFont(self.ui_font)
        search_row.addWidget(lbl_replace)

        self.replace_entry = QLineEdit()
        self.replace_entry.setFont(self.ui_small_font)
        self.replace_entry.setFixedWidth(250)
        search_row.addWidget(self.replace_entry)

        self.search_btn = make_button("搜尋", color="#007bff", hover="#0069d9",
                                      font=self.ui_font, width=80)
        self.search_btn.clicked.connect(self._do_search)
        search_row.addWidget(self.search_btn)

        btn_replace_all = make_button("全部替換", color="#dc3545", hover="#c82333",
                                      font=self.ui_font, width=80)
        btn_replace_all.clicked.connect(self._replace_all)
        search_row.addWidget(btn_replace_all)

        search_row.addStretch()
        layout.addLayout(search_row)

        # 狀態
        self.status_label = QLabel("")
        self.status_label.setFont(self.ui_small_font)
        self.status_label.setStyleSheet("color: #888888;")
        layout.addWidget(self.status_label)

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
        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(1)
        self._results_layout.addStretch()
        scroll_area.setWidget(self._results_container)
        layout.addWidget(scroll_area, 1)

    # ────────────────────────────────────────────────────────
    #  資料夾瀏覽
    # ────────────────────────────────────────────────────────

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "選取 HTML 檔案所在的資料夾")
        if folder:
            self.folder_entry.setText(folder)

    # ────────────────────────────────────────────────────────
    #  Toast
    # ────────────────────────────────────────────────────────

    def _toast(self, msg: str, color: str = "#28a745", duration: int = 3000):
        show_toast(self, msg, color=color, duration=duration)

    # ────────────────────────────────────────────────────────
    #  搜尋
    # ────────────────────────────────────────────────────────

    def _do_search(self):
        folder = self.folder_entry.text().strip()
        if not folder or not os.path.isdir(folder):
            self._toast("請先選擇有效的資料夾", color="#f39c12")
            return

        query = self.search_entry.text()
        if not query:
            self._toast("請輸入搜尋內容", color="#f39c12")
            return

        use_regex = self.regex_switch.isChecked()
        if use_regex:
            try:
                pattern = re.compile(query)
            except re.error as e:
                self._toast(f"正則語法錯誤: {e}", color="#dc3545")
                return
        else:
            pattern = re.compile(re.escape(query))

        self._clear_results()
        self.batch_matches.clear()

        html_files = [f for f in os.listdir(folder) if f.lower().endswith('.html')]
        html_files.sort()
        file_count = len(html_files)

        self.search_btn.setEnabled(False)
        self.status_label.setText(f"搜尋中... (0 / {file_count} 個檔案)")
        self.status_label.setStyleSheet("color: #888888;")

        def _search():
            matches: list[dict] = []
            for i, fname in enumerate(html_files):
                fpath = os.path.join(folder, fname)
                text = read_html_pre_content(fpath)
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
                    self._sig_progress.emit(
                        f"搜尋中... ({i + 1} / {file_count} 個檔案)")

            self._sig_done.emit(matches, file_count)

        threading.Thread(target=_search, daemon=True).start()

    def _on_progress(self, text: str):
        self.status_label.setText(text)

    def _clear_results(self):
        layout = self._results_layout
        while layout.count() > 1:  # keep the stretch
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _search_done(self, matches: list[dict], file_count: int):
        self.batch_matches = matches
        self.search_btn.setEnabled(True)
        total = len(matches)

        if total == 0:
            self.status_label.setText("找不到符合結果")
            self.status_label.setStyleSheet("color: #f39c12;")
            return

        capped = total >= 500
        status_text = (
            f"找到 {total} 筆結果"
            + ("（已達上限 500 筆）" if capped else "")
            + f"，共掃描 {file_count} 個檔案"
        )
        self.status_label.setText(f"{status_text}，渲染中...")
        self.status_label.setStyleSheet("color: #888888;")
        self._render_batch(0, total, status_text)

    def _render_batch(self, start: int, total: int, status_text: str,
                      batch_size: int = 30):
        end = min(start + batch_size, total)
        for mi in self.batch_matches[start:end]:
            self._build_result_row(mi)
        if end < total:
            self.status_label.setText(f"{status_text}，渲染中 {end}/{total}...")
            QTimer.singleShot(0, lambda: self._render_batch(end, total, status_text, batch_size))
        else:
            self.status_label.setText(status_text)
            self.status_label.setStyleSheet("color: #28a745;")

    # ────────────────────────────────────────────────────────
    #  結果行
    # ────────────────────────────────────────────────────────

    def _build_result_row(self, mi: dict):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(5, 1, 5, 1)

        mi['_row'] = row

        lbl_name = QLabel(mi['short_name'])
        lbl_name.setFont(self.ui_small_font)
        lbl_name.setFixedWidth(120)
        lbl_name.setStyleSheet("color: #6f42c1;")
        row_layout.addWidget(lbl_name)

        btn_replace = make_button("替換", color="#dc3545", hover="#c82333",
                                  font=self.ui_small_font, width=45)
        btn_replace.setFixedHeight(22)
        btn_replace.clicked.connect(lambda checked=False, m=mi: self._replace_single(m))
        row_layout.addWidget(btn_replace)

        btn_open = make_button("開啟", color="#007bff", hover="#0069d9",
                               font=self.ui_small_font, width=45)
        btn_open.setFixedHeight(22)
        btn_open.clicked.connect(lambda checked=False, m=mi: self._open_file(m))
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

        self._results_layout.insertWidget(self._results_layout.count() - 1, row)

    def _rebuild_row_as_replaced(self, row: QWidget, mi: dict, replacement: str):
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

        lbl_done = QLabel("已替換")
        lbl_done.setFont(self.ui_small_font)
        lbl_done.setStyleSheet("color: #28a745;")
        layout.addWidget(lbl_done)

        btn_open = make_button("開啟", color="#007bff", hover="#0069d9",
                               font=self.ui_small_font, width=45)
        btn_open.setFixedHeight(22)
        btn_open.clicked.connect(lambda checked=False, m=mi: self._open_file(m))
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

    # ────────────────────────────────────────────────────────
    #  開啟（IPC 命令）
    # ────────────────────────────────────────────────────────

    def _open_file(self, mi: dict):
        """寫入 IPC 命令檔，通知主程式開啟檔案並跳到該行。"""
        if not self._cmd_file:
            self._toast("未指定命令檔路徑，無法開啟", color="#dc3545")
            return

        cmd = {
            "action": "open",
            "file_path": mi['file_path'],
            "line": mi['line_idx'] + 1,
        }
        try:
            with open(self._cmd_file, 'w', encoding='utf-8') as f:
                json.dump(cmd, f, ensure_ascii=False)
            self._toast("已傳送開啟指令")
        except Exception as e:
            self._toast(f"寫入命令檔失敗: {e}", color="#dc3545")

    # ────────────────────────────────────────────────────────
    #  單筆替換
    # ────────────────────────────────────────────────────────

    def _replace_single(self, mi: dict):
        replacement = self.replace_entry.text()
        fpath = mi['file_path']

        text = read_html_pre_content(fpath)
        if text is None:
            self._toast("無法讀取檔案", color="#dc3545")
            return

        lines = text.split('\n')
        li = mi['line_idx']
        if li < len(lines):
            line = lines[li]
            lines[li] = line[:mi['match_start']] + replacement + line[mi['match_end']:]

        new_text = '\n'.join(lines)
        try:
            write_html_file(fpath, new_text)
        except Exception as e:
            self._toast(f"儲存失敗: {e}", color="#dc3545")
            return

        self.batch_matches = [m for m in self.batch_matches if m is not mi]

        old_row = mi.get('_row')
        if old_row:
            self._rebuild_row_as_replaced(old_row, mi, replacement)

        self._toast("已替換並儲存")

    # ────────────────────────────────────────────────────────
    #  全部替換
    # ────────────────────────────────────────────────────────

    def _replace_all(self):
        if not self.batch_matches:
            self._toast("沒有可替換的結果", color="#f39c12")
            return

        replacement = self.replace_entry.text()

        by_file: dict[str, list[dict]] = {}
        for mi in self.batch_matches:
            by_file.setdefault(mi['file_path'], []).append(mi)

        replaced_count = 0
        file_count = 0
        for fpath, matches in by_file.items():
            text = read_html_pre_content(fpath)
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
                write_html_file(fpath, new_text)
                file_count += 1
            except Exception:
                pass

        for mi in self.batch_matches:
            old_row = mi.get('_row')
            if old_row:
                self._rebuild_row_as_replaced(old_row, mi, replacement)

        self.batch_matches.clear()
        self.status_label.setText(f"已替換 {replaced_count} 筆，涉及 {file_count} 個檔案")
        self.status_label.setStyleSheet("color: #28a745;")
        self._toast(f"全部替換完成！共 {replaced_count} 筆")


# ════════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="AA 批次搜尋（PyQt6）")
    parser.add_argument("--folder", default="", help="預設資料夾路徑")
    parser.add_argument("--cmd-file", default="", help="IPC 命令檔路徑")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyleSheet(_load_qss())

    win = BatchSearchWindow(folder=args.folder, cmd_file=args.cmd_file)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
