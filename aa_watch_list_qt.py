"""追更清單視窗（v3.46，主畫面上方「📚 追更」）。

列出追蹤中的作品、各自有幾話新的；可新增網址、立即檢查、用網址讀取開新的一話、
從新的一話開始自動翻譯、標記已讀、刪除。檢查本身與資料存放在 MainWindow
（``check_watch_list``、``_watch_list``），本視窗只負責顯示與轉送操作。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)


def _font(size: int = 11, bold: bool = False) -> QFont:
    f = QFont("Microsoft JhengHei", size)
    f.setBold(bold)
    return f


class WatchListDialog(QDialog):
    def __init__(self, main) -> None:
        super().__init__(main)
        self._main = main
        self.setWindowTitle("追更清單")
        self.resize(980, 520)
        v = QVBoxLayout(self)

        hint = QLabel(
            "填入連載中作品的某一話網址（通常是看到／翻到的最新一話）。每天第一次開程式時會自動檢查，"
            "看那一話之後有沒有新的話（依網頁的上下話清單判斷）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6c757d;")
        v.addWidget(hint)

        add_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://…（作品的某一話網址）")
        self.url_edit.returnPressed.connect(self._on_add)
        add_row.addWidget(self.url_edit, 1)
        btn_add = QPushButton("＋ 新增")
        btn_add.clicked.connect(self._on_add)
        add_row.addWidget(btn_add)
        self.btn_check = QPushButton("🔄 立即檢查全部")
        self.btn_check.clicked.connect(lambda: self._main.check_watch_list(manual=True))
        add_row.addWidget(self.btn_check)
        v.addLayout(add_row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["作品（基準話）", "新話", "最後檢查", "狀態"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 深色主題（dark_theme.qss）沒有表頭規則，原生表頭會變成白底淺灰字看不清
        # → 與自動翻譯關鍵字表同樣明確指定表頭配色
        self.tree.setStyleSheet(
            "QHeaderView::section { background:#3c3f41; color:#dce4ee;"
            " font-weight:bold; padding:4px; border:none;"
            " border-right:1px solid #555555; border-bottom:1px solid #555555; }")
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(lambda *_: self._on_open())
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        v.addWidget(self.tree, 1)

        act = QHBoxLayout()
        self.btn_open = QPushButton("🌐 網址讀取新的一話")
        self.btn_open.setToolTip("用網址讀取開啟第一個新的一話（沒有新話時開基準話）；也可以雙擊該列")
        self.btn_open.clicked.connect(self._on_open)
        self.btn_auto = QPushButton("⚡ 從新的一話自動翻譯")
        self.btn_auto.setToolTip("切到自動翻譯，起始網址填第一個新的一話、勾「翻譯到最後一話」；確認設定後自己按開始")
        self.btn_auto.clicked.connect(self._on_auto)
        self.btn_read = QPushButton("✔ 標記已讀")
        self.btn_read.setToolTip("基準話改成目前找到的最新一話，之後只提示比它更新的話")
        self.btn_read.clicked.connect(self._on_mark_read)
        self.btn_check_one = QPushButton("🔄 檢查這部")
        self.btn_check_one.clicked.connect(self._on_check_one)
        self.btn_del = QPushButton("🗑 刪除")
        self.btn_del.clicked.connect(self._on_delete)
        for b in (self.btn_open, self.btn_auto, self.btn_read, self.btn_check_one):
            act.addWidget(b)
        act.addStretch()
        act.addWidget(self.btn_del)
        v.addLayout(act)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#6c757d;")
        v.addWidget(self.status)
        self.refresh()

    # ── 顯示 ──

    def refresh(self) -> None:
        """依 MainWindow._watch_list 重畫（保留目前選取）。"""
        cur = self._selected_index()
        self.tree.clear()
        for e in self._main._watch_list:
            n = len(e.get("new") or [])
            title = e.get("title") or e.get("url", "")
            n_txt = (f"{n}+" if e.get("more") else str(n)) if n else ""
            it = QTreeWidgetItem([title, n_txt, e.get("checked_at", ""),
                                  e.get("error") or ("有更新" if n else "")])
            tip = f"基準話：{e.get('url', '')}"
            if n:
                tip += "\n新的話：\n" + "\n".join(
                    f"・{x.get('title') or x['url']}" for x in e["new"][:30])
                if n > 30:
                    tip += f"\n…（共 {n} 話）"
            for c in range(4):
                it.setToolTip(c, tip)
            if n:
                for c in range(4):
                    it.setForeground(c, QColor("#d63384"))
                it.setFont(1, _font(11, bold=True))
            elif e.get("error"):
                it.setForeground(3, QColor("#dc3545"))
            self.tree.addTopLevelItem(it)
        if 0 <= cur < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(cur))
        checking = self._main._watch_checking
        self.btn_check.setEnabled(not checking)
        self.btn_check.setText("檢查中…" if checking else "🔄 立即檢查全部")
        last = self._main._watch_checked_date
        if not checking:
            self.status.setText(f"上次自動檢查：{last}" if last else "還沒檢查過")
        self._update_buttons()

    def set_progress(self, text: str) -> None:
        self.status.setText(text)

    def _selected_index(self) -> int:
        it = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(it) if it is not None else -1

    def _selected(self) -> dict | None:
        i = self._selected_index()
        return self._main._watch_list[i] if 0 <= i < len(self._main._watch_list) else None

    def _update_buttons(self) -> None:
        e = self._selected()
        has = e is not None
        has_new = bool(has and e.get("new"))
        busy = self._main._watch_checking
        self.btn_open.setEnabled(has)
        self.btn_auto.setEnabled(has_new)
        self.btn_read.setEnabled(has_new and not busy)
        self.btn_check_one.setEnabled(has and not busy)
        self.btn_del.setEnabled(has and not busy)

    # ── 操作 ──

    def _on_add(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            return
        if not url.startswith("http"):
            url = "https://" + url
        if not self._main.add_watch_entry(url):
            QMessageBox.information(self, "追更清單", "這一話已經在清單裡了。")
            return
        self.url_edit.clear()
        self.refresh()
        self.tree.setCurrentItem(self.tree.topLevelItem(self.tree.topLevelItemCount() - 1))

    def _on_open(self) -> None:
        e = self._selected()
        if e is None:
            return
        url = e["new"][0]["url"] if e.get("new") else e["url"]
        self._main.open_url_in_fetch(url)

    def _on_auto(self) -> None:
        e = self._selected()
        if e and e.get("new"):
            self._main.start_watch_auto_translate(e["new"][0]["url"])

    def _on_mark_read(self) -> None:
        i = self._selected_index()
        if i >= 0:
            self._main.mark_watch_read(i)

    def _on_check_one(self) -> None:
        i = self._selected_index()
        if i >= 0:
            self._main.check_watch_list(indices=[i], manual=True)

    def _on_delete(self) -> None:
        i = self._selected_index()
        e = self._selected()
        if e is None:
            return
        if QMessageBox.question(
                self, "刪除", f"從追更清單刪除「{e.get('title') or e['url']}」？"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._main.remove_watch_entry(i)
