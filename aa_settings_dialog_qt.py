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
        fetch_history_count: int,
        original_cache_limit: int,
        glossary_auto_search: bool,
        diff_save_mode: bool,
        embed_font_in_html: bool,
        embed_font_name: str,
        editor_default_wysiwyg: bool,
        korean_mode: bool,
        experimental_extraction: bool,
        pad_right_aa: bool,
        glossary_translation_only: bool,
        fetch_auto_fill_title: bool,
        orig_cache_path: str,
        on_apply: Callable[[dict], None],
        on_clear_url_history: Callable[[int], int],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setModal(True)
        self.resize(460, 460)
        self._orig_cache_path = orig_cache_path
        self._on_apply = on_apply
        self._on_clear_url_history = on_clear_url_history
        self._build_ui(auto_copy, work_history_limit, fetch_history_limit,
                       fetch_history_count, original_cache_limit,
                       glossary_auto_search, diff_save_mode, embed_font_in_html,
                       embed_font_name, editor_default_wysiwyg, korean_mode,
                       experimental_extraction, pad_right_aa,
                       glossary_translation_only, fetch_auto_fill_title)
        self._refresh_cache_size()

    def _build_ui(self, auto_copy: bool, wh_limit: int, fh_limit: int,
                  fh_count: int, oc_limit: int, glossary_auto_search: bool,
                  diff_save_mode: bool, embed_font_in_html: bool,
                  embed_font_name: str, editor_default_wysiwyg: bool,
                  korean_mode: bool, experimental_extraction: bool,
                  pad_right_aa: bool, glossary_translation_only: bool,
                  fetch_auto_fill_title: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ── 自動複製 ──
        self.auto_copy_cb = QCheckBox("提取日文後自動複製到剪貼簿")
        self.auto_copy_cb.setFont(_ui_font(12))
        self.auto_copy_cb.setChecked(auto_copy)
        root.addWidget(self.auto_copy_cb)

        self.korean_mode_cb = QCheckBox("韓文提取模式（改用韓文字元集）")
        self.korean_mode_cb.setFont(_ui_font(12))
        self.korean_mode_cb.setChecked(korean_mode)
        self.korean_mode_cb.setToolTip(
            "開啟後，「提取」與「提取分析」會改用韓文字元集（Hangul 가-힣＋相容字母 ㄱ-ㅎㅏ-ㅣ）；\n"
            "AA_Settings.json 中的 base_regex（日文版）將被略過，其他流程不變。")
        root.addWidget(self.korean_mode_cb)

        self.experimental_extraction_cb = QCheckBox(
            "🧪 實驗性日文提取演算法（錨點掃描＋分數制過濾）")
        self.experimental_extraction_cb.setFont(_ui_font(12))
        self.experimental_extraction_cb.setChecked(experimental_extraction)
        self.experimental_extraction_cb.setToolTip(
            "實驗性功能：以「連續假名／助詞」為錨點向兩側擴展取出候選，\n"
            "再依「日文文本可能性分數」（連續假名 / 助詞 / 局部 AA 噪聲密度）過濾。\n"
            "目的：減少 AA 圖中夾雜少量合法字元時被誤提取的情況（如 rf7父く三）。\n"
            "關閉時走原有 chunk-by-2-spaces + base_regex 路徑（韓文模式不受此選項影響）。")
        root.addWidget(self.experimental_extraction_cb)

        self.pad_right_aa_cb = QCheckBox(
            "🧪 替換翻譯時：偵測文字右側 AA 圖並補全形空白")
        self.pad_right_aa_cb.setFont(_ui_font(12))
        self.pad_right_aa_cb.setChecked(pad_right_aa)
        self.pad_right_aa_cb.setToolTip(
            "實驗性功能：替換翻譯時，若某行被替換原文的「右側」殘留內容像 AA 圖\n"
            "（半形片假名／框線／AA 常用符號比例偏高 — 沿用實驗提取演算法的 AA 噪聲判定），\n"
            "則盡量讓右側的 AA 圖維持原位置、減少替換後需手動重新排版的情況：\n"
            "  • 譯文比原文短 → 於譯文後補等量全形空白。\n"
            "  • 譯文比原文長 → 吃掉右側多餘空白（全/半形），空白不夠就能吃幾個算幾個。\n"
            "（對話框替換也走此路徑。）\n"
            "關閉時（預設）僅當右側出現對話框邊框字元（| │ ｜ ┃）才補空白、且不吃空白。")
        root.addWidget(self.pad_right_aa_cb)

        self.fetch_auto_fill_title_cb = QCheckBox(
            "網址讀取成功時，自動填入作品名稱框")
        self.fetch_auto_fill_title_cb.setFont(_ui_font(12))
        self.fetch_auto_fill_title_cb.setChecked(fetch_auto_fill_title)
        self.fetch_auto_fill_title_cb.setToolTip(
            "開啟後，網址讀取成功且頁面有可辨識的標題時，\n"
            "會自動把解析出的作品名稱填入首頁「標題」欄位。\n"
            "若該 URL 已有手動戳印的作品名稱，戳印值仍會優先覆蓋。")
        root.addWidget(self.fetch_auto_fill_title_cb)

        self.glossary_auto_search_cb = QCheckBox("批次搜尋：點擊術語按鈕時自動搜尋")
        self.glossary_auto_search_cb.setFont(_ui_font(12))
        self.glossary_auto_search_cb.setChecked(glossary_auto_search)
        root.addWidget(self.glossary_auto_search_cb)

        self.glossary_translation_only_cb = QCheckBox(
            "套用翻譯時：術語表只套用於譯文部分")
        self.glossary_translation_only_cb.setFont(_ui_font(12))
        self.glossary_translation_only_cb.setChecked(glossary_translation_only)
        self.glossary_translation_only_cb.setToolTip(
            "開啟後，「替換翻譯／加入翻譯」執行時，術語表僅套用於 AI 翻譯文部分；\n"
            "原文中未被提取出的區域（AA 圖、空行等）不會再被全域掃描替換。\n"
            "關閉時（預設）會於整份文本最後再做一輪全域 glossary 套用。")
        root.addWidget(self.glossary_translation_only_cb)

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
        self.embed_font_in_html_cb = QCheckBox("另存新檔時內嵌字型：")
        self.embed_font_in_html_cb.setFont(_ui_font(12))
        self.embed_font_in_html_cb.setChecked(embed_font_in_html)
        self.embed_font_in_html_cb.setToolTip(
            "開啟後，另存新檔時會把選定字型以 Base64 內嵌到 <head> 的 @font-face；\n"
            "產出的單一檔案不需任何外部依賴，下載到手機本地直接打開亦能正確顯示 AA。\n"
            "代價：每個 HTML 檔會依字型大小增大（詳見右側下拉選單的說明）。\n"
            "啟用此選項時另存的檔案會覆寫原有的自訂 <head>。\n"
            "注意：Ctrl+S 覆蓋現有檔案時不寫入內嵌字型，Head 設定維持原狀。")
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

        # ── 網址讀取紀錄 ──
        row2 = QHBoxLayout()
        lbl2 = QLabel("清除網址紀錄時保留筆數：")
        lbl2.setFont(_ui_font(12))
        row2.addWidget(lbl2)
        self.fetch_spin = QSpinBox()
        self.fetch_spin.setRange(0, 9999)
        self.fetch_spin.setValue(max(0, int(fh_limit)))
        self.fetch_spin.setFont(_ui_font(11))
        row2.addWidget(self.fetch_spin)
        clear_url_btn = _make_btn("清除紀錄", "#dc3545", "#c82333",
                                  font=_ui_font(11), width=80)
        clear_url_btn.clicked.connect(self._on_clear_url_btn)
        row2.addWidget(clear_url_btn)
        self.fetch_count_lbl = QLabel(f"（目前共 {fh_count} 筆）")
        self.fetch_count_lbl.setFont(_ui_font(11))
        self.fetch_count_lbl.setStyleSheet("color:#888;")
        row2.addWidget(self.fetch_count_lbl)
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
        btn_clear = _make_btn("清除暫存", "#dc3545", "#c82333",
                              font=_ui_font(11), width=80)
        btn_clear.clicked.connect(self._on_clear_cache)
        row3.addWidget(btn_clear)
        self.size_label = QLabel("（—）")
        self.size_label.setFont(_ui_font(11))
        self.size_label.setStyleSheet("color:#888;")
        row3.addWidget(self.size_label)
        row3.addStretch()
        root.addLayout(row3)

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
            self.size_label.setText(f"（{_format_size(size)}）")
        except OSError:
            self.size_label.setText("（檔案不存在）")

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

    def _on_clear_url_btn(self) -> None:
        keep_n = int(self.fetch_spin.value())
        msg = (f"清除後將保留最新的 {keep_n} 筆紀錄。"
               if keep_n > 0 else "所有網址紀錄都會被刪除。")
        ret = QMessageBox.question(
            self, "清除網址紀錄",
            f"確定要清除網址紀錄嗎？\n{msg}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        new_count = self._on_clear_url_history(keep_n)
        self.fetch_count_lbl.setText(f"（目前共 {new_count} 筆）")

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
            'korean_mode': self.korean_mode_cb.isChecked(),
            'experimental_extraction':
                self.experimental_extraction_cb.isChecked(),
            'pad_right_aa': self.pad_right_aa_cb.isChecked(),
            'glossary_translation_only':
                self.glossary_translation_only_cb.isChecked(),
            'fetch_auto_fill_title': self.fetch_auto_fill_title_cb.isChecked(),
        }
        try:
            self._on_apply(values)
        except Exception as e:
            QMessageBox.warning(self, "套用失敗", str(e))
            return
        self.accept()
