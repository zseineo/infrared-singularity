"""AA 創作翻譯輔助小工具 — PyQt6 批次搜尋。

`BatchSearchWindow` 由 `aa_main_qt.py` 直接嵌入 `QStackedWidget`；
亦支援以 subprocess 方式啟動，透過 JSON 命令檔（IPC）與主程式溝通「開啟」操作。

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
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from aa_tool.html_io import read_html_head, read_html_pre_content, write_html_file
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

    def __init__(self, *, folder: str = "", cmd_file: str = "",
                 reverse_cmd_file: str = "",
                 on_open_file=None,       # (file_path, line, folder) -> None
                 on_folder_change=None,   # (folder) -> None
                 on_add_to_glossary=None, # (a, b) -> None
                 glossary_auto_search: bool = True):
        super().__init__()
        self.setWindowTitle("AA 批次搜尋")
        self.resize(1280, 700)

        self._on_open_file = on_open_file
        self._on_folder_change = on_folder_change
        self._on_add_to_glossary = on_add_to_glossary
        self._cmd_file = cmd_file
        self._reverse_cmd_file = reverse_cmd_file
        self.glossary_auto_search: bool = glossary_auto_search

        # 連接信號
        self._sig_progress.connect(self._on_progress)
        self._sig_done.connect(self._search_done)

        # 字體
        self.ui_font = QFont("Microsoft JhengHei", 14, QFont.Weight.Bold)
        self.ui_small_font = QFont("Microsoft JhengHei", 12)

        self.batch_matches: list[dict] = []
        # 復原用：{file_path: original_text_content}
        self._undo_backups: dict[str, str] = {}
        self._undo_items: list[dict] = []
        # 本次「全部替換」快照（僅保留最近一次）
        self._batch_undo: dict | None = None
        # 操作互斥鎖：避免替換/復原過程中快速連點造成 widget 存取崩潰
        self._busy: bool = False

        self._build_ui()

        if folder:
            self.folder_entry.setText(folder)

        # 輪詢反向命令檔（主程式 → 批次搜尋）
        if self._reverse_cmd_file:
            self._reverse_timer = QTimer(self)
            self._reverse_timer.timeout.connect(self._poll_reverse_commands)
            self._reverse_timer.start(500)

    # ────────────────────────────────────────────────────────
    #  UI 建構
    # ────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        left_panel = QWidget()
        layout = QVBoxLayout(left_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(left_panel, 3)
        outer.addWidget(self._build_glossary_panel(), 1)

        # 資料夾選擇
        folder_row = QHBoxLayout()
        lbl_folder = QLabel("資料夾:")
        lbl_folder.setFont(self.ui_font)
        folder_row.addWidget(lbl_folder)

        self.folder_entry = QLineEdit()
        self.folder_entry.setFont(self.ui_small_font)
        folder_row.addWidget(self.folder_entry, 1)

        btn_browse = make_button("瀏覽…", color="#6c757d", hover="#5a6268",
                                 font=self.ui_small_font, width=80)
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
                                      font=self.ui_font, width=90)
        self.search_btn.clicked.connect(self._do_search)
        search_row.addWidget(self.search_btn)

        btn_replace_all = make_button("全部替換", color="#dc3545", hover="#c82333",
                                      font=self.ui_font, width=100)
        btn_replace_all.clicked.connect(self._replace_all)
        search_row.addWidget(btn_replace_all)

        self.btn_undo_all = make_button("全部復原", color="#f39c12", hover="#d68910",
                                        font=self.ui_font, width=100)
        self.btn_undo_all.clicked.connect(self._undo_all_batch)
        self.btn_undo_all.hide()
        search_row.addWidget(self.btn_undo_all)

        search_row.addStretch()
        layout.addLayout(search_row)

        # 狀態
        self.status_label = QLabel("")
        self.status_label.setFont(self.ui_small_font)
        self.status_label.setStyleSheet("color: #888888;")
        layout.addWidget(self.status_label)

        # 表頭 — 使用與資料列相同的 margin/spacing/寬度，確保欄位對齊
        header = QHBoxLayout()
        self._init_row_layout(header, margin_v=0)
        lbl_h_name = QLabel("檔名")
        lbl_h_name.setFont(self.ui_small_font)
        lbl_h_name.setFixedWidth(self._NAME_COL_WIDTH)
        header.addWidget(lbl_h_name)

        lbl_h_op = QLabel("操作")
        lbl_h_op.setFont(self.ui_small_font)
        lbl_h_op.setFixedWidth(self._op_col_width())
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
    #  術語快捷面板
    # ────────────────────────────────────────────────────────

    def _build_glossary_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(panel)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        lbl = QLabel("快速替換（每行 A=B）")
        lbl.setFont(self.ui_small_font)
        v.addWidget(lbl)

        self._glossary_edit = QPlainTextEdit()
        self._glossary_edit.setFont(self.ui_small_font)
        self._glossary_edit.setPlaceholderText("例：\nやる夫=亞魯夫\nやらない夫=亞拉奈伊夫")
        self._glossary_edit.setToolTip("下方清單每一列可按兩下加入主術語表（一般）")
        self._glossary_edit.setMaximumHeight(160)
        self._glossary_edit.textChanged.connect(self._refresh_glossary_list)
        v.addWidget(self._glossary_edit)

        list_scroll = QScrollArea()
        list_scroll.setWidgetResizable(True)
        self._glossary_list_container = QWidget()
        self._glossary_list_layout = QVBoxLayout(self._glossary_list_container)
        self._glossary_list_layout.setContentsMargins(0, 0, 0, 0)
        self._glossary_list_layout.setSpacing(2)
        self._glossary_list_layout.addStretch()
        list_scroll.setWidget(self._glossary_list_container)
        v.addWidget(list_scroll, 1)

        return panel

    def _parse_glossary_entries(self, text: str) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for raw in text.split('\n'):
            line = raw.strip()
            if not line:
                continue
            sep_idx = -1
            for ch in ('=', '＝'):
                i = line.find(ch)
                if i >= 0 and (sep_idx < 0 or i < sep_idx):
                    sep_idx = i
            if sep_idx <= 0:
                continue
            a = line[:sep_idx].strip()
            b = line[sep_idx + 1:].strip()
            if not a:
                continue
            entries.append((a, b))
        return entries

    def _refresh_glossary_list(self):
        layout = self._glossary_list_layout
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 術語按鈕重建後，先前記錄的 pressed button 已失效，需重置
        self._last_glossary_btn = None

        entries = self._parse_glossary_entries(self._glossary_edit.toPlainText())
        for a, b in entries:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)

            btn = make_button("→", color="#17a2b8", hover="#138496",
                              font=self.ui_small_font, width=30)
            btn.setFixedHeight(24)
            btn.clicked.connect(
                lambda checked=False, sa=a, sb=b, sbtn=btn:
                self._apply_glossary_entry(sa, sb, sbtn))
            row_layout.addWidget(btn)

            display = f"{a} → {b}" if b else f"{a} →（刪除）"
            lbl = QLabel(display)
            lbl.setFont(self.ui_small_font)
            lbl.setStyleSheet("color: #cfcfcf;")
            lbl.setToolTip("按兩下加入主術語表（一般）")
            lbl.mouseDoubleClickEvent = (
                lambda ev, sa=a, sb=b: self._add_entry_to_main_glossary(sa, sb))
            row_layout.addWidget(lbl, 1)

            layout.insertWidget(layout.count() - 1, row)

    _GLOSSARY_BTN_DEFAULT = ("#17a2b8", "#138496")
    _GLOSSARY_BTN_PRESSED = ("#6f42c1", "#5a32a3")  # 紫色：標示上次點擊

    def _style_glossary_btn(self, btn: QPushButton, color: str, hover: str) -> None:
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{
                background-color: {hover};
            }}
        """)

    def _add_entry_to_main_glossary(self, a: str, b: str) -> None:
        """將快速替換的一列加入主術語表（一般）。透過 callback 由主程式處理。"""
        if self._on_add_to_glossary is None:
            self._toast("無法加入主術語表（未連接主程式）", color="#dc3545")
            return
        if not a or not b:
            self._toast("空白或刪除規則無法加入術語表", color="#f39c12")
            return
        try:
            self._on_add_to_glossary(a, b)
        except Exception as e:
            self._toast(f"加入失敗: {e}", color="#dc3545")
            return
        # 成功提示由 callback (MainWindow._save_glossary_entry) 自行 toast，避免重複。

    def _apply_glossary_entry(self, a: str, b: str,
                              btn: QPushButton | None = None):
        self.search_entry.setText(a)
        self.replace_entry.setText(b)
        self.regex_switch.setChecked(False)
        # 標示上次點擊的按鈕，復原前一顆
        prev = getattr(self, '_last_glossary_btn', None)
        if prev is not None and prev is not btn:
            try:
                self._style_glossary_btn(prev, *self._GLOSSARY_BTN_DEFAULT)
            except RuntimeError:
                pass  # 前次按鈕已被 deleteLater
        if btn is not None:
            self._style_glossary_btn(btn, *self._GLOSSARY_BTN_PRESSED)
            self._last_glossary_btn = btn
        if self.glossary_auto_search:
            self._do_search()

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
        # 新搜尋：隱藏上一次的「全部復原」按鈕
        self._batch_undo = None
        self.btn_undo_all.hide()

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

                        matches.append({
                            'file_path': fpath,
                            'file_name': fname,
                            'line_idx': line_idx,
                            'match_start': match_start,
                            'match_end': match_end,
                            'matched_text': matched_text,
                            'ctx_before': ("…" + before) if ctx_start > 0 else before,
                            'ctx_after': (after + "…") if ctx_end < len(line) else after,
                            'stem': stem,
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

    # 結果列欄位的固定寬度與 layout 參數，集中定義以便表頭與各狀態 row
    # 共用，確保替換前後、表頭與資料列皆對齊。
    _NAME_COL_WIDTH = 180
    _LEADING_COL_WIDTH = 55
    _BTN_COL_WIDTH = 55
    _ROW_MARGIN_H = 5
    _ROW_SPACING = 6

    @classmethod
    def _op_col_width(cls) -> int:
        """操作欄總寬 = leading + 2 個按鈕 + 之間的 spacing。"""
        return (cls._LEADING_COL_WIDTH + cls._BTN_COL_WIDTH * 2
                + cls._ROW_SPACING * 2)

    def _init_row_layout(self, layout: QHBoxLayout,
                         margin_v: int = 1) -> None:
        layout.setContentsMargins(
            self._ROW_MARGIN_H, margin_v, self._ROW_MARGIN_H, margin_v)
        layout.setSpacing(self._ROW_SPACING)

    def _make_name_label(self, stem: str) -> QLabel:
        metrics = QFontMetrics(self.ui_small_font)
        # 預留 4px 內距，避免最後一個字被邊界裁掉
        elided = metrics.elidedText(
            stem, Qt.TextElideMode.ElideLeft, self._NAME_COL_WIDTH - 4)
        lbl = QLabel(elided)
        lbl.setFont(self.ui_small_font)
        lbl.setFixedWidth(self._NAME_COL_WIDTH)
        lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl.setStyleSheet("color: #6f42c1;")
        lbl.setToolTip(stem)
        return lbl

    def _build_result_row(self, mi: dict):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        self._init_row_layout(row_layout)

        mi['_row'] = row

        row_layout.addWidget(self._make_name_label(mi['stem']))

        btn_dismiss = make_button("✕", color="#6c757d", hover="#5a6268",
                                   font=self.ui_small_font,
                                   width=self._LEADING_COL_WIDTH)
        btn_dismiss.setFixedHeight(26)
        btn_dismiss.clicked.connect(lambda checked=False, m=mi: self._dismiss_match(m))
        row_layout.addWidget(btn_dismiss)

        btn_replace = make_button("替換", color="#dc3545", hover="#c82333",
                                  font=self.ui_small_font,
                                  width=self._BTN_COL_WIDTH)
        btn_replace.setFixedHeight(26)
        btn_replace.clicked.connect(lambda checked=False, m=mi: self._replace_single(m))
        row_layout.addWidget(btn_replace)

        btn_open = make_button("開啟", color="#007bff", hover="#0069d9",
                               font=self.ui_small_font,
                               width=self._BTN_COL_WIDTH)
        btn_open.setFixedHeight(26)
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

        layout.addWidget(self._make_name_label(mi['stem']))

        lbl_done = QLabel("已替換")
        lbl_done.setFont(self.ui_small_font)
        # 寬度固定等於原本 ✕ 按鈕的位置，避免右側欄位位移
        lbl_done.setFixedWidth(self._LEADING_COL_WIDTH)
        lbl_done.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        lbl_done.setStyleSheet("color: #28a745;")
        layout.addWidget(lbl_done)

        btn_undo = make_button("復原", color="#f39c12", hover="#d68910",
                               font=self.ui_small_font,
                               width=self._BTN_COL_WIDTH)
        btn_undo.setFixedHeight(26)
        btn_undo.clicked.connect(lambda checked=False, m=mi: self._undo_single(m))
        layout.addWidget(btn_undo)

        btn_open = make_button("開啟", color="#007bff", hover="#0069d9",
                               font=self.ui_small_font,
                               width=self._BTN_COL_WIDTH)
        btn_open.setFixedHeight(26)
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

    # ────────────────────────────────────────────────────────
    #  關閉時同步資料夾
    # ────────────────────────────────────────────────────────

    def closeEvent(self, event):
        """關閉視窗時，同步資料夾。"""
        folder = self.folder_entry.text().strip()
        if folder:
            if self._on_folder_change is not None:
                self._on_folder_change(folder)
            elif self._cmd_file:
                cmd = {"action": "sync_folder", "folder": folder}
                try:
                    with open(self._cmd_file, 'w', encoding='utf-8') as f:
                        json.dump(cmd, f, ensure_ascii=False)
                except OSError:
                    pass
        event.accept()

    # ────────────────────────────────────────────────────────
    #  排除（不替換此筆）
    # ────────────────────────────────────────────────────────

    def _dismiss_match(self, mi: dict):
        """將此筆結果從替換範圍中移除，並在 UI 標示為已排除。"""
        if self._busy:
            return
        self._busy = True
        try:
            self.batch_matches = [m for m in self.batch_matches if m is not mi]
            row = mi.get('_row')
            if row:
                # 延後重建：當前是由該 row 內的 ✕ 按鈕 clicked 信號
                # 觸發，若立即 deleteLater 該按鈕會在信號分派中崩潰。
                QTimer.singleShot(
                    0, lambda r=row, m=mi: self._render_row_as_dismissed(r, m))
        finally:
            self._busy = False

    def _render_row_as_dismissed(self, row: QWidget, mi: dict) -> None:
        """將 row 重建為「已排除」狀態（實際 widget 操作）。"""
        row_layout = row.layout()
        while row_layout.count():
            item = row_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        lbl_name = self._make_name_label(mi['stem'])
        lbl_name.setStyleSheet("color: #6c757d;")
        row_layout.addWidget(lbl_name)

        lbl_status = QLabel("已排除")
        lbl_status.setFont(self.ui_small_font)
        lbl_status.setFixedWidth(self._LEADING_COL_WIDTH)
        lbl_status.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        lbl_status.setStyleSheet("color: #6c757d;")
        row_layout.addWidget(lbl_status)

        btn_restore = make_button("恢復", color="#17a2b8", hover="#138496",
                                  font=self.ui_small_font,
                                  width=self._BTN_COL_WIDTH)
        btn_restore.setFixedHeight(26)
        btn_restore.clicked.connect(
            lambda checked=False, m=mi: self._restore_dismissed(m))
        row_layout.addWidget(btn_restore)
        # 已排除狀態沒有第二顆按鈕；用空 placeholder 填滿第三欄，
        # 讓「搜尋結果」內文欄起點與其他狀態一致。
        spacer = QWidget()
        spacer.setFixedWidth(self._BTN_COL_WIDTH)
        row_layout.addWidget(spacer)

        lbl_ctx = QLabel(
            f"{mi['ctx_before']}{mi['matched_text']}{mi['ctx_after']}")
        lbl_ctx.setFont(self.ui_small_font)
        lbl_ctx.setStyleSheet("color: #6c757d;")
        row_layout.addWidget(lbl_ctx)

        row_layout.addStretch()

    def _restore_dismissed(self, mi: dict):
        """恢復已排除的結果。"""
        if self._busy:
            return
        self._busy = True
        try:
            self.batch_matches.append(mi)
            row = mi.get('_row')
            if row:
                # 延後重建：sender 為該 row 內的「恢復」按鈕，自我刪除會崩。
                QTimer.singleShot(
                    0, lambda r=row, m=mi: self._rebuild_row_as_active(r, m))
        finally:
            self._busy = False

    # ────────────────────────────────────────────────────────
    #  反向 IPC（主程式 → 批次搜尋）
    # ────────────────────────────────────────────────────────

    def _poll_reverse_commands(self):
        """輪詢反向命令檔，處理主程式傳來的指令。"""
        if not self._reverse_cmd_file or not os.path.exists(self._reverse_cmd_file):
            return
        try:
            with open(self._reverse_cmd_file, 'r', encoding='utf-8') as f:
                cmd = json.load(f)
            os.remove(self._reverse_cmd_file)

            if cmd.get('action') == 'restore':
                self.showNormal()
                self.activateWindow()
                self.raise_()
        except (json.JSONDecodeError, OSError):
            pass

    def _open_file(self, mi: dict):
        """通知主程式開啟檔案並跳到該行。支援 callback（embedded）與 IPC（subprocess）兩種模式。"""
        folder = self.folder_entry.text().strip()
        if self._on_open_file is not None:
            self._on_open_file(mi['file_path'], mi['line_idx'] + 1, folder)
            return
        if not self._cmd_file:
            self._toast("未指定命令檔路徑，無法開啟", color="#dc3545")
            return
        cmd = {
            "action": "open",
            "file_path": mi['file_path'],
            "line": mi['line_idx'] + 1,
            "raise": True,
            "folder": folder,
        }
        try:
            with open(self._cmd_file, 'w', encoding='utf-8') as f:
                json.dump(cmd, f, ensure_ascii=False)
            self.showMinimized()
        except Exception as e:
            self._toast(f"寫入命令檔失敗: {e}", color="#dc3545")

    # ────────────────────────────────────────────────────────
    #  單筆替換
    # ────────────────────────────────────────────────────────

    def _replace_single(self, mi: dict):
        if self._busy:
            return
        self._busy = True
        try:
            replacement = self.replace_entry.text()
            fpath = mi['file_path']

            text = read_html_pre_content(fpath)
            if text is None:
                self._toast("無法讀取檔案", color="#dc3545")
                return

            # 備份原始內容（每個檔案只備份一次）
            if fpath not in self._undo_backups:
                self._undo_backups[fpath] = text
            self._undo_items.append(mi)

            lines = text.split('\n')
            li = mi['line_idx']
            if li < len(lines):
                line = lines[li]
                lines[li] = line[:mi['match_start']] + replacement + line[mi['match_end']:]

            new_text = '\n'.join(lines)
            try:
                write_html_file(fpath, new_text,
                                head_html=read_html_head(fpath))
            except Exception as e:
                self._toast(f"儲存失敗: {e}", color="#dc3545")
                return

            self.batch_matches = [m for m in self.batch_matches if m is not mi]

            old_row = mi.get('_row')
            if old_row:
                # 延後重建：當前是由該 row 內的「替換」按鈕 clicked 信號
                # 觸發進來，若同步刪除該按鈕 Qt 會在信號分派中崩潰。
                QTimer.singleShot(
                    0, lambda r=old_row, m=mi, repl=replacement:
                    self._rebuild_row_as_replaced(r, m, repl))

            self._toast("已替換並儲存")
        finally:
            self._busy = False

    # ────────────────────────────────────────────────────────
    #  全部替換
    # ────────────────────────────────────────────────────────

    def _replace_all(self):
        if self._busy:
            return
        if not self.batch_matches:
            self._toast("沒有可替換的結果", color="#f39c12")
            return
        self._busy = True
        try:
            self._replace_all_impl()
        finally:
            self._busy = False

    def _replace_all_impl(self):
        replacement = self.replace_entry.text()

        by_file: dict[str, list[dict]] = {}
        for mi in self.batch_matches:
            by_file.setdefault(mi['file_path'], []).append(mi)

        # 本批次快照：用於「全部復原」（以替換前的當下內容為基準）
        batch_pre: dict[str, str] = {}
        new_backup_files: set[str] = set()

        replaced_count = 0
        file_count = 0
        for fpath, matches in by_file.items():
            text = read_html_pre_content(fpath)
            if text is None:
                continue

            batch_pre[fpath] = text
            # 備份原始內容（單筆復原用）
            if fpath not in self._undo_backups:
                self._undo_backups[fpath] = text
                new_backup_files.add(fpath)

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
                write_html_file(fpath, new_text,
                                head_html=read_html_head(fpath))
                file_count += 1
            except Exception:
                pass

        batch_items = list(self.batch_matches)
        for mi in batch_items:
            self._undo_items.append(mi)
            old_row = mi.get('_row')
            if old_row:
                self._rebuild_row_as_replaced(old_row, mi, replacement)

        self._batch_undo = {
            'backups': batch_pre,
            'items': batch_items,
            'new_backup_files': new_backup_files,
        }
        self.btn_undo_all.show()

        self.batch_matches.clear()
        self.status_label.setText(f"已替換 {replaced_count} 筆，涉及 {file_count} 個檔案")
        self.status_label.setStyleSheet("color: #28a745;")
        self._toast(f"全部替換完成！共 {replaced_count} 筆")

    # ────────────────────────────────────────────────────────
    #  復原
    # ────────────────────────────────────────────────────────

    def _undo_single(self, mi: dict):
        """復原單筆替換：從備份還原該檔案的原始內容。"""
        if self._busy:
            return
        self._busy = True
        try:
            fpath = mi['file_path']
            backup = self._undo_backups.get(fpath)
            if backup is None:
                self._toast("無備份可復原", color="#dc3545")
                return

            try:
                write_html_file(fpath, backup,
                                head_html=read_html_head(fpath))
            except Exception as e:
                self._toast(f"復原失敗: {e}", color="#dc3545")
                return

            # 移除此檔案所有相關的 undo 項目並恢復成可操作狀態
            restored = [item for item in self._undo_items if item['file_path'] == fpath]
            self._undo_items = [item for item in self._undo_items if item['file_path'] != fpath]
            del self._undo_backups[fpath]

            for item in restored:
                row = item.get('_row')
                if row:
                    # 延後到下一個事件迴圈再重建 row：當前是由該 row 內
                    # 的「復原」按鈕的 clicked 信號觸發進來的，若立即
                    # 刪除該按鈕，Qt 會在信號分派過程中訪問已被釋放的
                    # widget 而崩潰。
                    QTimer.singleShot(
                        0, lambda r=row, it=item: self._rebuild_row_as_active(r, it))
                self.batch_matches.append(item)

            # 單筆復原已動過此檔案，使批次快照失效
            if self._batch_undo and fpath in self._batch_undo['backups']:
                self._batch_undo = None
                self.btn_undo_all.hide()

            self._toast(f"已復原 {len(restored)} 筆（{os.path.basename(fpath)}）")
        finally:
            self._busy = False

    def _undo_all_batch(self):
        """復原最近一次「全部替換」：將所有相關檔案還原至替換前狀態。"""
        if self._busy:
            return
        if not self._batch_undo:
            self._toast("沒有可復原的批次替換", color="#f39c12")
            return
        self._busy = True
        try:
            self._undo_all_batch_impl()
        finally:
            self._busy = False

    def _undo_all_batch_impl(self):
        backups: dict[str, str] = self._batch_undo['backups']
        items: list[dict] = self._batch_undo['items']
        new_backup_files: set[str] = self._batch_undo['new_backup_files']

        restored_files = 0
        for fpath, content in backups.items():
            try:
                write_html_file(fpath, content,
                                head_html=read_html_head(fpath))
                restored_files += 1
            except Exception:
                pass

        # 從單筆 undo 追蹤中移除本批次的項目
        item_ids = {id(mi) for mi in items}
        self._undo_items = [m for m in self._undo_items if id(m) not in item_ids]
        for fpath in new_backup_files:
            self._undo_backups.pop(fpath, None)

        for mi in items:
            row = mi.get('_row')
            if row:
                self._rebuild_row_as_active(row, mi)
            self.batch_matches.append(mi)

        self._batch_undo = None
        self.btn_undo_all.hide()

        self.status_label.setText(
            f"已復原本次全部替換（{len(items)} 筆，{restored_files} 個檔案）"
        )
        self.status_label.setStyleSheet("color: #17a2b8;")
        self._toast(f"已復原本次全部替換！共 {len(items)} 筆")

    def _rebuild_row_as_active(self, row: QWidget, mi: dict):
        """將已替換的行恢復為可操作狀態（替換＋開啟按鈕）。"""
        layout = row.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        layout.addWidget(self._make_name_label(mi['stem']))

        # 與初次建立的 row 一致：復原 / 恢復後都要能再次排除。
        btn_dismiss = make_button("✕", color="#6c757d", hover="#5a6268",
                                   font=self.ui_small_font,
                                   width=self._LEADING_COL_WIDTH)
        btn_dismiss.setFixedHeight(26)
        btn_dismiss.clicked.connect(lambda checked=False, m=mi: self._dismiss_match(m))
        layout.addWidget(btn_dismiss)

        btn_replace = make_button("替換", color="#dc3545", hover="#c82333",
                                  font=self.ui_small_font,
                                  width=self._BTN_COL_WIDTH)
        btn_replace.setFixedHeight(26)
        btn_replace.clicked.connect(lambda checked=False, m=mi: self._replace_single(m))
        layout.addWidget(btn_replace)

        btn_open = make_button("開啟", color="#007bff", hover="#0069d9",
                               font=self.ui_small_font,
                               width=self._BTN_COL_WIDTH)
        btn_open.setFixedHeight(26)
        btn_open.clicked.connect(lambda checked=False, m=mi: self._open_file(m))
        layout.addWidget(btn_open)

        lbl_before = QLabel(mi['ctx_before'])
        lbl_before.setFont(self.ui_small_font)
        lbl_before.setStyleSheet("color: #888888;")
        layout.addWidget(lbl_before)

        lbl_match = QLabel(mi['matched_text'])
        lbl_match.setFont(QFont("Microsoft JhengHei", 12, QFont.Weight.Bold))
        lbl_match.setStyleSheet("color: #ff6b6b;")
        layout.addWidget(lbl_match)

        lbl_after = QLabel(mi['ctx_after'])
        lbl_after.setFont(self.ui_small_font)
        lbl_after.setStyleSheet("color: #888888;")
        layout.addWidget(lbl_after)

        layout.addStretch()


# ════════════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="AA 批次搜尋（PyQt6）")
    parser.add_argument("--folder", default="", help="預設資料夾路徑")
    parser.add_argument("--cmd-file", default="", help="IPC 命令檔路徑")
    parser.add_argument("--reverse-cmd-file", default="", help="反向 IPC 命令檔路徑")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyleSheet(_load_qss())

    win = BatchSearchWindow(folder=args.folder, cmd_file=args.cmd_file,
                            reverse_cmd_file=args.reverse_cmd_file)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
