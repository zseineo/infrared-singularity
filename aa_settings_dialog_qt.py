"""全域設定 Dialog — 集中管理自動複製、歷史上限、原文暫存。"""
from __future__ import annotations

import json
import os
from typing import Callable

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
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
        original_cache_limit: int,
        glossary_auto_search: bool,
        diff_save_mode: bool,
        embed_font_in_html: bool,
        embed_font_name: str,
        editor_default_wysiwyg: bool,
        orig_cache_path: str,
        on_apply: Callable[[dict], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setModal(True)
        self.resize(460, 460)
        self._orig_cache_path = orig_cache_path
        self._on_apply = on_apply
        self._build_ui(auto_copy, work_history_limit, fetch_history_limit,
                       original_cache_limit, glossary_auto_search,
                       diff_save_mode, embed_font_in_html, embed_font_name,
                       editor_default_wysiwyg)
        self._refresh_cache_size()

    def _build_ui(self, auto_copy: bool, wh_limit: int, fh_limit: int,
                  oc_limit: int, glossary_auto_search: bool,
                  diff_save_mode: bool, embed_font_in_html: bool,
                  embed_font_name: str, editor_default_wysiwyg: bool) -> None:
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

        self.diff_save_mode_cb = QCheckBox("儲存設定時僅合併差異")
        self.diff_save_mode_cb.setFont(_ui_font(12))
        self.diff_save_mode_cb.setChecked(diff_save_mode)
        self.diff_save_mode_cb.setToolTip(
            "開啟後，按「儲存設定」時：\n"
            "  • 術語表（一般+臨時）以等號左側為 key 比對\n"
            "  • 自訂過濾規則以整行為 key\n"
            "檔案中既有、UI 沒有的條目會被保留；UI 中的條目會覆蓋同 key 或追加")
        root.addWidget(self.diff_save_mode_cb)

        self.editor_default_wysiwyg_cb = QCheckBox(
            "進入編輯器時預設開啟「所見即所得」模式")
        self.editor_default_wysiwyg_cb.setFont(_ui_font(12))
        self.editor_default_wysiwyg_cb.setChecked(editor_default_wysiwyg)
        self.editor_default_wysiwyg_cb.setToolTip(
            "開啟後，每次切換到編輯面板（替換翻譯、開啟 HTML、批次開檔等）\n"
            "都會自動進入 Alt+3 所見即所得模式。")
        root.addWidget(self.editor_default_wysiwyg_cb)

        font_row = QHBoxLayout()
        self.embed_font_in_html_cb = QCheckBox("儲存 HTML 時內嵌字型：")
        self.embed_font_in_html_cb.setFont(_ui_font(12))
        self.embed_font_in_html_cb.setChecked(embed_font_in_html)
        self.embed_font_in_html_cb.setToolTip(
            "開啟後，儲存 HTML 時會把選定字型以 Base64 內嵌到 <head> 的 @font-face；\n"
            "產出的單一檔案不需任何外部依賴，下載到手機本地直接打開亦能正確顯示 AA。\n"
            "代價：每個 HTML 檔會依字型大小增大（詳見右側下拉選單的說明）。\n"
            "啟用此選項時會覆寫檔案原有的自訂 <head>。")
        font_row.addWidget(self.embed_font_in_html_cb)
        _FONT_CHOICES = [
            ("Monapo",    "monapo",    "+3.5 MB"),
            ("Saitamaar", "Saitamaar", "+2.7 MB"),
            ("textar",    "textar",    "+4.3 MB"),
        ]
        self.embed_font_combo = QComboBox()
        self.embed_font_combo.setFont(_ui_font(11))
        for label, key, _ in _FONT_CHOICES:
            self.embed_font_combo.addItem(label, key)
        key_to_idx = {key: i for i, (_, key, _) in enumerate(_FONT_CHOICES)}
        self.embed_font_combo.setCurrentIndex(key_to_idx.get(embed_font_name, 0))
        self.embed_font_combo.setEnabled(embed_font_in_html)
        self.embed_font_combo.setToolTip(
            "選擇要內嵌的 AA 字型（內嵌後的檔案大小增量）：\n"
            "  Monapo    — +3.5 MB\n"
            "  Saitamaar — +2.7 MB\n"
            "  textar    — +4.3 MB")
        self.embed_font_in_html_cb.toggled.connect(self.embed_font_combo.setEnabled)
        font_row.addWidget(self.embed_font_combo)
        font_row.addStretch()
        root.addLayout(font_row)

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

        # ── 網址讀取紀錄上限 ──
        row2 = QHBoxLayout()
        lbl2 = QLabel("網址讀取紀錄儲存數量：")
        lbl2.setFont(_ui_font(12))
        row2.addWidget(lbl2)
        self.fetch_spin = QSpinBox()
        self.fetch_spin.setRange(1, 500)
        self.fetch_spin.setValue(max(1, int(fh_limit)))
        self.fetch_spin.setFont(_ui_font(11))
        row2.addWidget(self.fetch_spin)
        row2.addStretch()
        root.addLayout(row2)

        # ── 原文暫存上限 ──
        row3 = QHBoxLayout()
        lbl3 = QLabel("原文暫存儲存數量：")
        lbl3.setFont(_ui_font(12))
        row3.addWidget(lbl3)
        self.orig_spin = QSpinBox()
        self.orig_spin.setRange(1, 1000)
        self.orig_spin.setValue(max(1, int(oc_limit)))
        self.orig_spin.setFont(_ui_font(11))
        row3.addWidget(self.orig_spin)
        row3.addStretch()
        root.addLayout(row3)

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
            'original_cache_limit': int(self.orig_spin.value()),
            'glossary_auto_search': self.glossary_auto_search_cb.isChecked(),
            'diff_save_mode': self.diff_save_mode_cb.isChecked(),
            'embed_font_in_html': self.embed_font_in_html_cb.isChecked(),
            'embed_font_name': self.embed_font_combo.currentData() or "monapo",
            'editor_default_wysiwyg':
                self.editor_default_wysiwyg_cb.isChecked(),
        }
        try:
            self._on_apply(values)
        except Exception as e:
            QMessageBox.warning(self, "套用失敗", str(e))
            return
        self.accept()
