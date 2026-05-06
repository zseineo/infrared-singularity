"""AA 創作翻譯輔助小工具 — URL 讀取面板（in-process，嵌入主視窗 QStackedWidget）。

由主程式 (aa_main_qt.py) 直接實例化並嵌入為 QStackedWidget 第 3 頁，
不再作為獨立視窗或 subprocess 啟動。

主程式 → 面板（公開方法）：
    win.sync_state(...)           # show 前同步狀態
    win.on_fetch_done(...)        # 抓取完成回報
    win.on_history_cleared(...)   # 清除歷史後更新
    win.on_history_updated(...)   # 其他程序寫入後推播
    win.on_author_updated(...)    # 戳印 meta 套用後同步作者

面板 → 主程式（直接呼叫）：
    main._handle_url_fetch_request(url, author_only, skip_cache)
    main.url_history / main.settings_mgr.clear_url_history()
    main._author_name / main._author_only / main.schedule_save()
    main.show_translate_panel()   # 關閉面板（返回首頁）
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from aa_tool.qt_helpers import make_button


class UrlFetchWindow(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self._main = main_window

        self.ui_small_font = QFont("Microsoft JhengHei", 12)

        self._url_history: list[dict] = []
        self._url_related_links: list[dict] = []
        self._current_url: str = ""
        self._author_only: bool = False
        self._author_name: str = ""
        self._fetching = False
        self._history_filter: str = ""

        self._build_ui()
        self._refresh_nav()
        self._refresh_history()

        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self._main.show_translate_panel)

    # ──────────────────────────── 狀態同步（由主程式呼叫） ────────────────────────────

    def sync_state(self, *, url_history: list, url_related_links: list,
                   current_url: str, author_only: bool, author_name: str,
                   initial_url: str = "") -> None:
        """主程式在切換到此面板之前呼叫，同步最新狀態。"""
        self._url_history = list(url_history)
        self._url_related_links = list(url_related_links)
        self._current_url = current_url
        self._author_only = author_only
        self._author_name = author_name
        self.author_only_switch.setChecked(author_only)
        self.author_name_entry.setText(author_name)
        if initial_url:
            self.url_entry.setText(initial_url)
        self._refresh_nav()
        self._refresh_history()

    def sync_back_to_main(self) -> None:
        """離開面板（返回首頁）時，由主程式呼叫，將狀態同步回去。"""
        self._main._author_only = self.author_only_switch.isChecked()
        self._main._author_name = self.author_name_entry.text().strip()
        self._main.schedule_save()

    def on_fetch_done(self, *, success: bool, status_message: str,
                      status_color: str, url_history: list | None = None,
                      url_related_links: list | None = None,
                      current_url: str | None = None,
                      auto_close: bool = False) -> None:
        self._fetching = False
        self.fetch_btn.setEnabled(True)
        self._set_status(status_message, status_color)
        if success:
            if url_history is not None:
                self._url_history = url_history
            if url_related_links is not None:
                self._url_related_links = url_related_links
            if current_url is not None:
                self._current_url = current_url
            self._refresh_nav()
            self._refresh_history()
            if auto_close:
                QTimer.singleShot(400, self._main.show_translate_panel)

    def on_history_cleared(self, url_history: list) -> None:
        self._url_history = url_history
        self._refresh_history()

    def on_history_updated(self, url_history: list,
                           url_related_links: list | None = None,
                           current_url: str | None = None) -> None:
        self._url_history = url_history
        if url_related_links is not None:
            self._url_related_links = url_related_links
            self._refresh_nav()
        if current_url is not None:
            self._current_url = current_url
        self._refresh_history()

    def on_author_updated(self, author_name: str) -> None:
        self._author_name = author_name
        try:
            self.author_name_entry.setText(author_name)
        except Exception:
            pass

    # ──────────────────────────── UI ────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
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

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self.hist_search = QLineEdit()
        self.hist_search.setFont(self.ui_small_font)
        self.hist_search.setPlaceholderText("🔍 搜尋紀錄（標題）")
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

        self._main._author_name = self.author_name_entry.text().strip()
        self._main._handle_url_fetch_request(
            raw,
            self.author_only_switch.isChecked(),
            skip_cache=self.skip_cache_switch.isChecked(),
        )

    def _clear_history(self):
        self._main.url_history = []
        self._main.settings_mgr.clear_url_history()
        self.on_history_cleared([])

    def _set_status(self, text: str, color: str):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")
