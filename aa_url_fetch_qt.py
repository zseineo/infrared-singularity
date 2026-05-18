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

import re

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from aa_tool.qt_helpers import make_button


class FlowLayout(QLayout):
    """簡易 flow layout：子元件由左至右排列、寬度不足時自動換行。

    用於「展開標題按鈕」面板：每顆按鈕依文字長度取 sizeHint，逐列填滿。
    （Qt 無內建 flow layout，此為官方範例的精簡版。）
    """

    def __init__(self, parent=None, margin: int = 0, spacing: int = 4) -> None:
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items: list = []

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x = rect.x()
        y = rect.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


# 標題正規化：去掉網站尾綴、外圍 tag、話數資訊，保留作品名稱主體。
_TITLE_CHAPTER_RES = [
    re.compile(r'第\s*[0-9０-９〇零一二三四五六七八九十百千]+\s*話'),
    re.compile(r'その\s*[0-9０-９〇零一二三四五六七八九十百千]+'),
    re.compile(r'番外編\s*[0-9０-９〇零一二三四五六七八九十百千]*'),
    re.compile(r'後日談\s*[0-9０-９〇零一二三四五六七八九十百千]*'),
    re.compile(r'[0-9０-９]+\s*話'),
]
_TITLE_SITE_SUFFIX_RE = re.compile(r'\s+[-—–]\s+.+$')
_TITLE_LEADING_TAGS_RE = re.compile(r'^(?:[【\[][^】\]]*[】\]][\s　]*)+')
_TITLE_TRAILING_TAGS_RE = re.compile(r'(?:[\s　]*[【\[][^】\]]*[】\]])+\s*$')


def _normalize_title_for_filter(raw_title: str) -> str:
    """把網址記錄的標題正規化成「作品名稱」主體，去掉網站名稱與話數資訊。

    處理順序：
      1. 去掉 " - 網站名稱" 尾綴（半形/長破折號）
      2. 去掉開頭的 【...】 / [...] tag 群
      3. 在第一個「話數模式」處截斷（第N話／そのN／番外編N／後日談N／N話）
      4. 去掉殘留在尾端的 【...】 tag 群
      5. 去掉前後空白（含全形）
    """
    if not raw_title:
        return ""
    t = _TITLE_SITE_SUFFIX_RE.sub("", raw_title)
    t = _TITLE_LEADING_TAGS_RE.sub("", t)
    earliest = len(t)
    for pat in _TITLE_CHAPTER_RES:
        m = pat.search(t)
        if m and m.start() < earliest:
            earliest = m.start()
    if earliest < len(t):
        t = t[:earliest]
    t = _TITLE_TRAILING_TAGS_RE.sub("", t)
    t = t.strip(' 　\t')
    # 只保留第一個空白（半形或全形）之前的部分，使前後篇等歸為同一作品
    m = re.search(r'[ 　\t]', t)
    if m:
        t = t[:m.start()]
    return t


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
        hist_outer.addLayout(hist_top)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(4)
        self.hist_search = QLineEdit()
        self.hist_search.setFont(self.ui_small_font)
        self.hist_search.setPlaceholderText("🔍 搜尋紀錄（標題）")
        self.hist_search.setClearButtonEnabled(True)
        self.hist_search.textChanged.connect(self._on_history_search_changed)
        # 搜尋框寬度：螢幕寬度的 1/4（最少 160px）
        screen_obj = QApplication.primaryScreen()
        screen_w = (screen_obj.availableGeometry().width()
                    if screen_obj is not None else 1280)
        self.hist_search.setFixedWidth(max(160, screen_w // 4))
        search_row.addWidget(self.hist_search, 0)
        # 展開標題按鈕的切換鈕（位於搜尋框與標題按鈕之間）
        self.title_expand_btn = QPushButton("⊞")
        self.title_expand_btn.setFixedSize(24, 24)
        self.title_expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_expand_btn.setToolTip("展開／收合完整標題按鈕")
        self.title_expand_btn.setStyleSheet(
            "QPushButton { background:#3a3f44; color:#ddd;"
            " border:1px solid #555; border-radius:3px; }"
            "QPushButton:hover { background:#4a5057; color:#fff; }")
        self.title_expand_btn.clicked.connect(self._toggle_title_expand)
        search_row.addWidget(self.title_expand_btn, 0)
        # 標題快速篩選按鈕區
        self.title_btn_row = QHBoxLayout()
        self.title_btn_row.setContentsMargins(0, 0, 0, 0)
        self.title_btn_row.setSpacing(3)
        search_row.addLayout(self.title_btn_row, 1)
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

        # 展開標題按鈕面板：hist_frame 的浮層子元件，預設隱藏，
        # 開啟時佔 hist_frame 右下、從中央起算的空間。
        self._hist_frame = hist_frame
        self.title_expand_panel = QWidget(hist_frame)
        self.title_expand_panel.setObjectName("titleExpandPanel")
        self.title_expand_panel.setStyleSheet(
            "#titleExpandPanel { background:#2b2f33; border:1px solid #555;"
            " border-radius:4px; }")
        self.title_expand_panel.hide()
        exp_outer = QVBoxLayout(self.title_expand_panel)
        exp_outer.setContentsMargins(4, 4, 4, 4)
        exp_outer.setSpacing(2)
        exp_lbl = QLabel("完整標題篩選")
        exp_lbl.setFont(self.ui_small_font)
        exp_lbl.setStyleSheet("color:#aaa; border:none;")
        exp_outer.addWidget(exp_lbl)
        self.title_expand_scroll = QScrollArea()
        self.title_expand_scroll.setWidgetResizable(True)
        self.title_expand_scroll.setStyleSheet("border:none;")
        self.title_expand_inner = QWidget()
        self.title_expand_flow = FlowLayout(
            self.title_expand_inner, margin=2, spacing=4)
        self.title_expand_scroll.setWidget(self.title_expand_inner)
        exp_outer.addWidget(self.title_expand_scroll, 1)

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

    def _unique_normalized_titles(self) -> list[str]:
        """取出 url_history 中唯一的正規化標題（依當前讀取順序，最新優先）。"""
        seen: set[str] = set()
        normalized: list[str] = []
        for entry in reversed(self._url_history):
            if not isinstance(entry, dict):
                continue
            raw = entry.get('title') or ''
            norm = _normalize_title_for_filter(raw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            normalized.append(norm)
        return normalized

    def _rebuild_title_filter_buttons(self) -> None:
        """依當前 url_history 重新產生「標題快速篩選按鈕」。

        - 取每筆 entry 的 `title`，呼叫 `_normalize_title_for_filter` 過濾話數／網站名稱
        - 依當前讀取順序（newest-first）去重後排列
        - 每按鈕固定寬度（90px）顯示前 8 字（超過 8 字尾端加 …），完整標題放 tooltip
        - 依當前可用空間決定可塞入幾顆按鈕，多出來的不顯示
        """
        # 清掉舊按鈕
        while self.title_btn_row.count() > 0:
            item = self.title_btn_row.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.deleteLater()

        normalized = self._unique_normalized_titles()
        if not normalized:
            return

        btn_w = 90
        spacing = self.title_btn_row.spacing()
        # 推算可用寬度：以面板當前寬度減去搜尋框與邊界
        panel_w = self.width()
        if panel_w <= 0:
            screen_obj = QApplication.primaryScreen()
            panel_w = (screen_obj.availableGeometry().width()
                       if screen_obj is not None else 1280)
        # 左右 padding + 列邊界 + 展開鈕（24）與其 spacing
        used = self.hist_search.width() + 40 + 24 + spacing
        avail = max(0, panel_w - used)
        max_btns = max(0, (avail + spacing) // (btn_w + spacing))
        if max_btns <= 0:
            return

        btn_font = QFont(self.ui_small_font)
        btn_font.setPointSize(max(1, btn_font.pointSize() - 2))

        for title in normalized[:max_btns]:
            display = title[:8] + ('…' if len(title) > 8 else '')
            btn = QPushButton(display)
            btn.setFont(btn_font)
            btn.setFixedSize(btn_w, 24)
            btn.setToolTip(title)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background:#3a3f44; color:#ddd;"
                " border:1px solid #555; border-radius:3px; padding:0 4px; }"
                "QPushButton:hover { background:#4a5057; color:#fff; }"
            )
            btn.clicked.connect(
                lambda checked=False, t=title: self.hist_search.setText(t))
            self.title_btn_row.addWidget(btn)

    # ──────────────────── 展開標題按鈕面板 ────────────────────

    def _toggle_title_expand(self) -> None:
        """切換「完整標題篩選」浮層面板的顯示。"""
        if self.title_expand_panel.isVisible():
            self.title_expand_panel.hide()
            self.title_expand_btn.setText("⊞")
            return
        self._rebuild_expanded_title_buttons()
        self._position_title_expand_panel()
        self.title_expand_panel.show()
        self.title_expand_panel.raise_()
        self.title_expand_btn.setText("⊟")

    def _position_title_expand_panel(self) -> None:
        """把展開面板從切換按鈕右緣同高處展開，覆蓋到 hist_frame 右下角。"""
        frame = self._hist_frame
        fw, fh = frame.width(), frame.height()
        if fw <= 0 or fh <= 0:
            return
        btn = self.title_expand_btn
        origin = btn.mapTo(frame, QPoint(btn.width() + 2, 0))
        x = max(0, origin.x())
        y = max(0, origin.y())
        self.title_expand_panel.setGeometry(
            x, y, max(0, fw - x - 4), max(0, fh - y - 4))

    def _rebuild_expanded_title_buttons(self) -> None:
        """重建展開面板中的完整標題按鈕（flow layout、不截斷標題）。"""
        # 清掉舊按鈕
        while self.title_expand_flow.count() > 0:
            item = self.title_expand_flow.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.deleteLater()

        btn_font = QFont(self.ui_small_font)
        btn_font.setPointSize(max(1, btn_font.pointSize() - 1))

        for title in self._unique_normalized_titles():
            btn = QPushButton(title)
            btn.setFont(btn_font)
            btn.setFixedHeight(26)
            btn.setSizePolicy(QSizePolicy.Policy.Minimum,
                              QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background:#3a3f44; color:#ddd;"
                " border:1px solid #555; border-radius:3px; padding:2px 8px; }"
                "QPushButton:hover { background:#4a5057; color:#fff; }")
            btn.clicked.connect(
                lambda checked=False, t=title: self._on_expanded_title_clicked(t))
            self.title_expand_flow.addWidget(btn)

    def _on_expanded_title_clicked(self, title: str) -> None:
        """點擊展開面板中的標題：填入搜尋框並收合面板。"""
        self.hist_search.setText(title)
        self.title_expand_panel.hide()
        self.title_expand_btn.setText("⊞")

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        # 視窗大小改變時重新計算可塞入的按鈕數量
        if hasattr(self, 'title_btn_row'):
            self._rebuild_title_filter_buttons()
        # 展開面板若開著，跟著重新定位
        if (hasattr(self, 'title_expand_panel')
                and self.title_expand_panel.isVisible()):
            self._position_title_expand_panel()

    def _refresh_history(self):
        self._rebuild_title_filter_buttons()
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

    def _set_status(self, text: str, color: str):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")
