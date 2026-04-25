"""Wiki 角色日中對照抓取 Dialog — 非 modal 獨立對話框。

使用者輸入 Wikipedia 角色列表頁 URL，點擊「讀取」後背景抓取並解析，
將結果以「日文=中文」每行一筆的格式顯示於多行文字框，供使用者複製
或手動編輯後貼入主視窗術語表。
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from aa_tool.url_fetcher import fetch_url
from aa_tool.wiki_name_fetcher import parse_wiki_name_list


def _ui_font(size: int = 12, bold: bool = False) -> QFont:
    f = QFont("Microsoft JhengHei", size)
    if bold:
        f.setBold(True)
    return f


def _make_btn(text: str, color: str, hover: str, *, width: int = 0,
              font: QFont | None = None) -> QPushButton:
    btn = QPushButton(text)
    btn.setStyleSheet(
        f"QPushButton {{ background:{color}; color:white;"
        f" padding:4px 10px; border:none; border-radius:4px; }}"
        f"QPushButton:hover {{ background:{hover}; }}"
    )
    if width:
        btn.setMinimumWidth(width)
    if font:
        btn.setFont(font)
    return btn


class WikiNameDialog(QDialog):
    """非 modal 對話框：抓取 Wiki 角色列表頁日中對照。"""

    _fetch_done = pyqtSignal(object, str)  # (result_list | None, error_msg)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wiki 角色日中對照")
        self.setModal(False)
        self.resize(560, 520)
        self._build_ui()
        self._fetch_done.connect(self._on_fetch_done)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        # URL 輸入列
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("URL："))
        self.url_edit = QLineEdit()
        self.url_edit.setFont(_ui_font(11))
        self.url_edit.setPlaceholderText(
            "https://zh.wikipedia.org/zh-tw/XXX角色列表"
        )
        self.url_edit.returnPressed.connect(self._on_fetch_clicked)
        url_row.addWidget(self.url_edit, 1)

        self.btn_fetch = _make_btn("讀取", "#28a745", "#218838",
                                   font=_ui_font(11), width=70)
        self.btn_fetch.clicked.connect(self._on_fetch_clicked)
        url_row.addWidget(self.btn_fetch)
        root.addLayout(url_row)

        # 狀態列
        self.status_label = QLabel("")
        self.status_label.setFont(_ui_font(10))
        self.status_label.setStyleSheet("color:#aaa;")
        root.addWidget(self.status_label)

        # 說明
        hint = QLabel(
            "格式：日文=中文（每行一筆，可直接貼入術語表）"
        )
        hint.setFont(_ui_font(10))
        hint.setStyleSheet("color:#888;")
        root.addWidget(hint)

        # 結果文字框
        self.result_text = QPlainTextEdit()
        self.result_text.setFont(QFont("Meiryo", 12))
        self.result_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.result_text.setStyleSheet("background:#1e1e1e; color:#ddd;")
        root.addWidget(self.result_text, 1)

        # 底部按鈕列
        btm = QHBoxLayout()
        btm.addStretch()
        btn_copy = _make_btn("📋 複製全部", "#17a2b8", "#138496",
                             font=_ui_font(11), width=100)
        btn_copy.clicked.connect(self._on_copy_all)
        btm.addWidget(btn_copy)

        btn_close = _make_btn("關閉", "#6c757d", "#5a6268",
                              font=_ui_font(11), width=70)
        btn_close.clicked.connect(self.close)
        btm.addWidget(btn_close)
        root.addLayout(btm)

    # ── 事件 ──
    def _on_fetch_clicked(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self._set_status("⚠️ 請輸入 URL", "#ffc107")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            self._set_status("⚠️ URL 需以 http(s):// 開頭", "#ffc107")
            return

        self.btn_fetch.setEnabled(False)
        self._set_status("讀取中…", "#17a2b8")
        threading.Thread(target=self._worker, args=(url,), daemon=True).start()

    def _worker(self, url: str) -> None:
        try:
            html_text = fetch_url(url)
        except Exception as e:
            self._fetch_done.emit(None, f"連線失敗：{e}")
            return
        try:
            pairs = parse_wiki_name_list(html_text)
        except Exception as e:
            self._fetch_done.emit(None, f"解析失敗：{e}")
            return
        self._fetch_done.emit(pairs, "")

    def _on_fetch_done(self, pairs, err: str) -> None:
        self.btn_fetch.setEnabled(True)
        if err:
            self._set_status(f"❌ {err}", "#dc3545")
            return
        if not pairs:
            self._set_status("⚠️ 未解析到任何對照（可能格式不支援）", "#ffc107")
            return
        lines = [f"{jp}={cn}" for jp, cn in pairs]
        self.result_text.setPlainText("\n".join(lines))
        self._set_status(f"✅ 成功：{len(pairs)} 筆", "#28a745")

    def _on_copy_all(self) -> None:
        text = self.result_text.toPlainText()
        if not text.strip():
            self._set_status("⚠️ 無內容可複製", "#ffc107")
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self._set_status(
            f"✅ 已複製 {len(text.splitlines())} 行到剪貼簿", "#28a745"
        )

    def _set_status(self, msg: str, color: str) -> None:
        self.status_label.setText(msg)
        self.status_label.setStyleSheet(f"color:{color};")
