"""全域設定 Dialog — 集中管理自動複製、歷史上限、原文暫存。"""
from __future__ import annotations

import json
import os
from typing import Callable

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)


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


def _format_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


class SettingsDialog(QDialog):
    """全域設定視窗。點擊「確定」時呼叫 on_apply(values)。"""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        auto_copy: bool,
        work_history_limit: int,
        fetch_history_limit: int,
        glossary_auto_search: bool,
        diff_save_mode: bool,
        orig_cache_path: str,
        on_apply: Callable[[dict], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setModal(True)
        self.resize(440, 380)
        self._orig_cache_path = orig_cache_path
        self._on_apply = on_apply
        self._build_ui(auto_copy, work_history_limit, fetch_history_limit,
                       glossary_auto_search, diff_save_mode)
        self._refresh_cache_size()

    def _build_ui(self, auto_copy: bool, wh_limit: int, fh_limit: int,
                  glossary_auto_search: bool, diff_save_mode: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ── 自動複製 ──
        self.auto_copy_cb = QCheckBox("提取日文後自動複製到剪貼簿")
        self.auto_copy_cb.setFont(_ui_font(12))
        self.auto_copy_cb.setChecked(auto_copy)
        root.addWidget(self.auto_copy_cb)

        self.glossary_auto_search_cb = QCheckBox("批次搜尋：點擊術語按鈕時自動搜尋")
        self.glossary_auto_search_cb.setFont(_ui_font(12))
        self.glossary_auto_search_cb.setChecked(glossary_auto_search)
        root.addWidget(self.glossary_auto_search_cb)

        self.diff_save_mode_cb = QCheckBox(
            "儲存設定時僅合併差異（不覆蓋既有條目，僅新增/取代相同key）")
        self.diff_save_mode_cb.setFont(_ui_font(12))
        self.diff_save_mode_cb.setChecked(diff_save_mode)
        self.diff_save_mode_cb.setToolTip(
            "開啟後，按「儲存設定」時：\n"
            "  • 術語表（一般+臨時）以等號左側為 key 比對\n"
            "  • 自訂過濾規則以整行為 key\n"
            "檔案中既有、UI 沒有的條目會被保留；UI 中的條目會覆蓋同 key 或追加")
        root.addWidget(self.diff_save_mode_cb)

        # ── 作者歷史 ──
        row1 = QHBoxLayout()
        lbl1 = QLabel("作者名稱歷史記錄數量：")
        lbl1.setFont(_ui_font(12))
        row1.addWidget(lbl1)
        self.work_spin = QSpinBox()
        self.work_spin.setRange(1, 200)
        self.work_spin.setValue(max(1, int(wh_limit)))
        self.work_spin.setFont(_ui_font(11))
        row1.addWidget(self.work_spin)
        row1.addStretch()
        root.addLayout(row1)

        # ── 網址/原文紀錄 ──
        row2 = QHBoxLayout()
        lbl2 = QLabel("網址/原文紀錄儲存數量：")
        lbl2.setFont(_ui_font(12))
        row2.addWidget(lbl2)
        self.fetch_spin = QSpinBox()
        self.fetch_spin.setRange(1, 500)
        self.fetch_spin.setValue(max(1, int(fh_limit)))
        self.fetch_spin.setFont(_ui_font(11))
        row2.addWidget(self.fetch_spin)
        row2.addStretch()
        root.addLayout(row2)

        # ── 原文暫存資訊 ──
        clear_row = QHBoxLayout()
        cache_lbl = QLabel("原文暫存檔：")
        cache_lbl.setFont(_ui_font(11, bold=True))
        clear_row.addWidget(cache_lbl)
        self.size_label = QLabel("大小：—")
        self.size_label.setFont(_ui_font(11))
        self.size_label.setStyleSheet("color:#888;")
        clear_row.addWidget(self.size_label)
        clear_row.addSpacing(12)
        btn_clear = _make_btn("清除暫存", "#dc3545", "#c82333",
                              font=_ui_font(11), width=100)
        btn_clear.clicked.connect(self._on_clear_cache)
        clear_row.addWidget(btn_clear)
        clear_row.addStretch()
        root.addLayout(clear_row)

        root.addStretch()

        # ── 底部按鈕 ──
        btm = QHBoxLayout()
        btm.addStretch()
        btn_cancel = _make_btn("取消", "#6c757d", "#5a6268",
                               font=_ui_font(11), width=80)
        btn_cancel.clicked.connect(self.reject)
        btm.addWidget(btn_cancel)
        btn_ok = _make_btn("確定", "#28a745", "#218838",
                           font=_ui_font(11, bold=True), width=80)
        btn_ok.clicked.connect(self._on_ok)
        btm.addWidget(btn_ok)
        root.addLayout(btm)

    def _refresh_cache_size(self) -> None:
        try:
            size = os.path.getsize(self._orig_cache_path)
            self.size_label.setText(f"大小：{_format_size(size)}")
        except OSError:
            self.size_label.setText("大小：（檔案不存在）")

    def _on_clear_cache(self) -> None:
        ret = QMessageBox.question(
            self, "清除原文暫存",
            "確定要清除所有原文暫存嗎？此動作無法復原。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            with open(self._orig_cache_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
            self._refresh_cache_size()
            QMessageBox.information(self, "清除完成", "原文暫存已清空。")
        except OSError as e:
            QMessageBox.warning(self, "清除失敗", f"無法寫入暫存檔：{e}")

    def _on_ok(self) -> None:
        values = {
            'auto_copy': self.auto_copy_cb.isChecked(),
            'work_history_limit': int(self.work_spin.value()),
            'fetch_history_limit': int(self.fetch_spin.value()),
            'glossary_auto_search': self.glossary_auto_search_cb.isChecked(),
            'diff_save_mode': self.diff_save_mode_cb.isChecked(),
        }
        try:
            self._on_apply(values)
        except Exception as e:
            QMessageBox.warning(self, "套用失敗", str(e))
            return
        self.accept()
