"""AA 創作翻譯輔助小工具 — 獨立 PyQt6 網址讀取視窗。

以 subprocess 從 PyQt6 主程式 (`aa_main_qt.py`) 啟動，透過 JSON 命令檔 (IPC) 雙向溝通。
作者名稱取自主程式最新狀態，因此抓取動作由主程式執行：
- Qt → 主程式 (cmd_file)：
    {action: "fetch_request", url, author_only}
    {action: "clear_history"}
    {action: "close_sync", author_only}
- 主程式 → Qt (reverse_cmd_file)：
    {action: "fetch_done", success, status_message, status_color,
      [url_history, url_related_links, current_url, auto_close]}
    {action: "history_cleared", url_history}

用法：
    python aa_url_fetch_qt.py --cmd-file <path> --reverse-cmd-file <path> --init-file <path>
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from aa_tool.qt_helpers import make_button


def _load_qss() -> str:
    qss_path = os.path.join(os.path.dirname(__file__), "aa_tool", "dark_theme.qss")
    if os.path.exists(qss_path):
        with open(qss_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


class UrlFetchWindow(QMainWindow):
    def __init__(self, *, cmd_file: str, reverse_cmd_file: str, init_file: str):
        super().__init__()
        self.setWindowTitle("🌐 網址讀取")
        self.resize(720, 620)

        self._cmd_file = cmd_file
        self._reverse_cmd_file = reverse_cmd_file

        self.ui_small_font = QFont("Microsoft JhengHei", 12)

        self._url_history: list[dict] = []
        self._url_related_links: list[dict] = []
        self._current_url: str = ""
        self._author_only: bool = False
        self._author_name: str = ""
        initial_url: str = ""
        if init_file and os.path.exists(init_file):
            try:
                with open(init_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self._url_history = d.get("url_history") or []
                self._url_related_links = d.get("url_related_links") or []
                self._current_url = d.get("current_url") or ""
                self._author_only = bool(d.get("author_only"))
                self._author_name = d.get("author_name") or ""
                initial_url = d.get("initial_url") or ""
            except (OSError, json.JSONDecodeError):
                pass

        self._fetching = False
        self._history_filter: str = ""

        self._build_ui()

        if initial_url:
            self.url_entry.setText(initial_url)
        self.author_only_switch.setChecked(self._author_only)
        self.author_name_entry.setText(self._author_name)

        self._refresh_nav()
        self._refresh_history()

        self._reverse_timer = QTimer(self)
        self._reverse_timer.timeout.connect(self._poll_reverse_commands)
        self._reverse_timer.start(300)

    # ──────────────────────────── UI ────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # 網址列
        top = QHBoxLayout()
        lbl_url = QLabel("網址:")
        lbl_url.setFont(self.ui_small_font)
        top.addWidget(lbl_url)

        self.url_entry = QLineEdit()
        self.url_entry.setFont(self.ui_small_font)
        self.url_entry.returnPressed.connect(self._do_fetch)
        top.addWidget(self.url_entry, 1)

        self.author_only_switch = QCheckBox("忽略留言")
        self.author_only_switch.setFont(self.ui_small_font)
        top.addWidget(self.author_only_switch)

        self.skip_cache_switch = QCheckBox("不讀暫存")
        self.skip_cache_switch.setFont(self.ui_small_font)
        self.skip_cache_switch.setToolTip("勾選後強制重新從網路抓取，不使用本機暫存")
        top.addWidget(self.skip_cache_switch)

        self.fetch_btn = make_button("讀取", color="#28a745", hover="#218838",
                                     font=self.ui_small_font, width=60)
        self.fetch_btn.setFixedHeight(28)
        self.fetch_btn.clicked.connect(self._do_fetch)
        top.addWidget(self.fetch_btn)
        layout.addLayout(top)

        # 作者名稱列
        author_row = QHBoxLayout()
        lbl_author = QLabel("作者名稱:")
        lbl_author.setFont(self.ui_small_font)
        author_row.addWidget(lbl_author)

        self.author_name_entry = QLineEdit()
        self.author_name_entry.setFont(self.ui_small_font)
        self.author_name_entry.setPlaceholderText("（配合「忽略留言」使用）")
        author_row.addWidget(self.author_name_entry, 1)
        layout.addLayout(author_row)

        self.status_label = QLabel("")
        self.status_label.setFont(self.ui_small_font)
        self.status_label.setStyleSheet("color: #888888;")
        layout.addWidget(self.status_label)

        # 關聯記事
        nav_frame = QFrame()
        nav_frame.setFrameShape(QFrame.Shape.StyledPanel)
        nav_outer = QVBoxLayout(nav_frame)
        nav_outer.setContentsMargins(5, 5, 5, 5)
        nav_outer.setSpacing(2)
        lbl_nav = QLabel("關聯記事:")
        lbl_nav.setFont(self.ui_small_font)
        nav_outer.addWidget(lbl_nav)

        self.nav_scroll = QScrollArea()
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setFixedHeight(200)
        self.nav_inner = QWidget()
        self.nav_inner_layout = QVBoxLayout(self.nav_inner)
        self.nav_inner_layout.setContentsMargins(5, 5, 5, 5)
        self.nav_inner_layout.setSpacing(2)
        self.nav_inner_layout.addStretch()
        self.nav_scroll.setWidget(self.nav_inner)
        nav_outer.addWidget(self.nav_scroll)
        layout.addWidget(nav_frame)

        # 讀取紀錄
        hist_frame = QFrame()
        hist_frame.setFrameShape(QFrame.Shape.StyledPanel)
        hist_outer = QVBoxLayout(hist_frame)
        hist_outer.setContentsMargins(5, 5, 5, 5)
        hist_outer.setSpacing(2)

        hist_top = QHBoxLayout()
        lbl_hist = QLabel("讀取紀錄:")
        lbl_hist.setFont(self.ui_small_font)
        hist_top.addWidget(lbl_hist)
        hist_top.addStretch()
        self.clear_btn = make_button("清除紀錄", color="#dc3545", hover="#c82333",
                                     font=self.ui_small_font, width=80)
        self.clear_btn.setFixedHeight(24)
        self.clear_btn.clicked.connect(self._clear_history)
        hist_top.addWidget(self.clear_btn)
        hist_outer.addLayout(hist_top)

        # 搜尋框：對標題 + URL 做 case-insensitive 子字串過濾
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self.hist_search = QLineEdit()
        self.hist_search.setFont(self.ui_small_font)
        self.hist_search.setPlaceholderText("🔍 搜尋紀錄（標題或網址）")
        self.hist_search.setClearButtonEnabled(True)
        self.hist_search.textChanged.connect(self._on_history_search_changed)
        search_row.addWidget(self.hist_search, 1)
        hist_outer.addLayout(search_row)

        self.hist_scroll = QScrollArea()
        self.hist_scroll.setWidgetResizable(True)
        self.hist_inner = QWidget()
        self.hist_inner_layout = QVBoxLayout(self.hist_inner)
        self.hist_inner_layout.setContentsMargins(5, 5, 5, 5)
        self.hist_inner_layout.setSpacing(2)
        self.hist_inner_layout.addStretch()
        self.hist_scroll.setWidget(self.hist_inner)
        hist_outer.addWidget(self.hist_scroll, 1)

        layout.addWidget(hist_frame, 1)

    # ──────────────────────────── 列表刷新 ────────────────────────────

    def _clear_layout_rows(self, layout: QVBoxLayout):
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def _refresh_nav(self):
        self._clear_layout_rows(self.nav_inner_layout)
        links = self._url_related_links
        insert_at = lambda w: self.nav_inner_layout.insertWidget(
            self.nav_inner_layout.count() - 1, w)

        if not links:
            lbl = QLabel("（尚未讀取或無關聯記事）")
            lbl.setFont(self.ui_small_font)
            lbl.setStyleSheet("color: #888888;")
            insert_at(lbl)
            return

        current_idx = -1
        for i, lk in enumerate(links):
            if lk.get("is_current"):
                current_idx = i
                break

        for lk in links:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(2)

            is_cur = bool(lk.get("is_current"))
            indicator = "▶ " if is_cur else "　"
            title = indicator + (lk.get("title") or "")
            if len(title) > 65:
                title = title[:65] + "…"
            color = "#dc3545" if is_cur else "#4dabf7"

            if lk.get("url"):
                btn = QPushButton(title)
                btn.setFont(self.ui_small_font)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {color};
                        border: none; text-align: left; padding: 2px 4px; }}
                    QPushButton:hover {{ color: #82c8ff; }}
                """)
                url = lk["url"]
                btn.clicked.connect(lambda checked=False, u=url: self._fetch_url(u))
                rl.addWidget(btn, 1)
            else:
                lbl = QLabel(title)
                lbl.setFont(self.ui_small_font)
                lbl.setStyleSheet(f"color: {color}; padding: 2px 4px;")
                rl.addWidget(lbl, 1)

            insert_at(row)

        if current_idx >= 0:
            btn_row = QWidget()
            br = QHBoxLayout(btn_row)
            br.setContentsMargins(0, 5, 0, 0)
            br.setSpacing(5)

            if current_idx > 0 and links[current_idx - 1].get("url"):
                prev_url = links[current_idx - 1]["url"]
                b = make_button("▲ 上一話", color="#0d6efd", hover="#0b5ed7",
                                font=self.ui_small_font, width=90)
                b.setFixedHeight(26)
                b.clicked.connect(lambda checked=False, u=prev_url: self._fetch_url(u))
                br.addWidget(b)

            if current_idx < len(links) - 1 and links[current_idx + 1].get("url"):
                next_url = links[current_idx + 1]["url"]
                b = make_button("▼ 下一話", color="#0d6efd", hover="#0b5ed7",
                                font=self.ui_small_font, width=90)
                b.setFixedHeight(26)
                b.clicked.connect(lambda checked=False, u=next_url: self._fetch_url(u))
                br.addWidget(b)

            br.addStretch()
            insert_at(btn_row)

    def _on_history_search_changed(self, text: str) -> None:
        self._history_filter = (text or "").strip().lower()
        self._refresh_history()

    def _refresh_history(self):
        self._clear_layout_rows(self.hist_inner_layout)
        insert_at = lambda w: self.hist_inner_layout.insertWidget(
            self.hist_inner_layout.count() - 1, w)

        kw = self._history_filter
        entries = list(reversed(self._url_history))
        if kw:
            entries = [
                e for e in entries
                if kw in (e.get("title") or "").lower()
                or kw in (e.get("url") or "").lower()
            ]
            if not entries:
                lbl = QLabel(f"（無符合「{self.hist_search.text()}」的紀錄）")
                lbl.setFont(self.ui_small_font)
                lbl.setStyleSheet("color: #888888; padding: 4px;")
                insert_at(lbl)
                return

        for entry in entries:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(2)

            title_text = entry.get("title") or entry.get("url", "")
            if len(title_text) > 60:
                title_text = title_text[:60] + "…"

            url = entry.get("url", "")

            # 複製按鈕：字型與寬度依 DPI 縮放調整，避免 125%／150% 縮放下「複製」文字溢出。
            # 以 96 DPI（100% 縮放）為基準；高縮放時 pointSize 再降 1pt、width 依比例放大。
            screen = self.screen() if hasattr(self, "screen") else None
            dpi_scale = (screen.logicalDotsPerInch() / 96.0) if screen else 1.0
            copy_font = QFont(self.ui_small_font)
            base_pt = self.ui_small_font.pointSize()
            copy_pt = base_pt - (2 if dpi_scale >= 1.2 else 1)
            copy_font.setPointSize(max(1, copy_pt))
            copy_width = int(45 * max(1.0, dpi_scale))
            copy_btn = make_button("複製", color="#6c757d", hover="#5a6268",
                                   font=copy_font, width=copy_width)
            copy_btn.setFixedHeight(22)
            copy_btn.setStyleSheet(copy_btn.styleSheet() + " QPushButton{padding:0 2px;}")
            copy_btn.setToolTip("複製此網址到剪貼簿")
            copy_btn.clicked.connect(
                lambda checked=False, u=url: self._copy_url_to_clipboard(u))
            rl.addWidget(copy_btn)

            btn = QPushButton(title_text)
            btn.setFont(self.ui_small_font)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("點擊讀取此網址")
            btn.setStyleSheet("""
                QPushButton { background: transparent; color: #b39ddb;
                    border: none; text-align: left; padding: 2px 4px; }
                QPushButton:hover { color: #d1c4e9; }
            """)
            btn.clicked.connect(lambda checked=False, u=url: self._fetch_url(u))
            rl.addWidget(btn, 1)

            insert_at(row)

    def _copy_url_to_clipboard(self, url: str) -> None:
        if not url:
            return
        QApplication.clipboard().setText(url)
        self._set_status("✅ 已複製網址到剪貼簿", "#28a745")

    # ──────────────────────────── 動作 ────────────────────────────

    def _fetch_url(self, url: str):
        self.url_entry.setText(url)
        self._do_fetch()

    def _do_fetch(self):
        if self._fetching:
            return
        raw = self.url_entry.text().strip()
        if not raw:
            self._set_status("⚠️ 請輸入網址！", "#f39c12")
            return
        if not raw.startswith("http"):
            raw = "https://" + raw
            self.url_entry.setText(raw)

        self._set_status("⏳ 讀取中…", "#17a2b8")
        self.fetch_btn.setEnabled(False)
        self._fetching = True

        self._write_cmd({
            "action": "fetch_request",
            "url": raw,
            "author_only": self.author_only_switch.isChecked(),
            "author_name": self.author_name_entry.text().strip(),
            "skip_cache": self.skip_cache_switch.isChecked(),
        })

    def _clear_history(self):
        self._write_cmd({"action": "clear_history"})

    def _set_status(self, text: str, color: str):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    # ──────────────────────────── IPC ────────────────────────────

    def _write_cmd(self, cmd: dict, retries: int = 20):
        """寫入 cmd_file。若檔案已存在（主程式尚未消費），稍後重試。"""
        if not self._cmd_file:
            return
        if os.path.exists(self._cmd_file):
            if retries > 0:
                QTimer.singleShot(100, lambda: self._write_cmd(cmd, retries - 1))
            return
        try:
            with open(self._cmd_file, "w", encoding="utf-8") as f:
                json.dump(cmd, f, ensure_ascii=False)
        except OSError:
            pass

    def _poll_reverse_commands(self):
        if not self._reverse_cmd_file or not os.path.exists(self._reverse_cmd_file):
            return
        try:
            with open(self._reverse_cmd_file, "r", encoding="utf-8") as f:
                cmd = json.load(f)
            os.remove(self._reverse_cmd_file)
        except (OSError, json.JSONDecodeError):
            return

        action = cmd.get("action")
        if action == "fetch_done":
            self._fetching = False
            self.fetch_btn.setEnabled(True)
            self._set_status(cmd.get("status_message", ""),
                             cmd.get("status_color", "#888888"))
            if cmd.get("success"):
                self._url_history = cmd.get("url_history") or []
                self._url_related_links = cmd.get("url_related_links") or []
                self._current_url = cmd.get("current_url") or ""
                self._refresh_nav()
                self._refresh_history()
                if cmd.get("auto_close"):
                    QTimer.singleShot(400, self.close)
        elif action == "history_cleared":
            self._url_history = cmd.get("url_history") or []
            self._refresh_history()
        elif action == "history_updated":
            # 主程式偵測到 cache 變動（例如另一個 aa_main_qt.py 寫入新紀錄）後推送
            self._url_history = cmd.get("url_history") or []
            related = cmd.get("url_related_links")
            if related is not None:
                self._url_related_links = related
                self._refresh_nav()
            cur = cmd.get("current_url")
            if cur is not None:
                self._current_url = cur
            self._refresh_history()

    def closeEvent(self, event):
        # 同步最終的 author_only 設定到主程式
        try:
            if self._cmd_file and not os.path.exists(self._cmd_file):
                with open(self._cmd_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "action": "close_sync",
                        "author_only": self.author_only_switch.isChecked(),
                        "author_name": self.author_name_entry.text().strip(),
                    }, f, ensure_ascii=False)
        except OSError:
            pass
        event.accept()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmd-file", default="")
    parser.add_argument("--reverse-cmd-file", default="")
    parser.add_argument("--init-file", default="")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    qss = _load_qss()
    if qss:
        app.setStyleSheet(qss)

    win = UrlFetchWindow(
        cmd_file=args.cmd_file,
        reverse_cmd_file=args.reverse_cmd_file,
        init_file=args.init_file,
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
