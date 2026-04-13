"""PyQt6 AA 字型渲染最小驗證工具。

用途：
    測試 PyQt6 在不同字型引擎下，對 AA 圖形的渲染效果。

用法：
    以不同字型引擎啟動（在啟動前設定環境變數）：

    # 預設 DirectWrite（Qt6 預設）
    python aa_qt_font_test.py

    # GDI 相容
    set QT_QPA_PLATFORM=windows:fontengine=gdi
    python aa_qt_font_test.py

    # FreeType
    set QT_QPA_PLATFORM=windows:fontengine=freetype
    python aa_qt_font_test.py

    注意：fontengine 必須在 QApplication 建立前設定，無法執行中切換。
"""
from __future__ import annotations

import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontDatabase, QFontMetricsF
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow,
    QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)


# 測試用 AA 樣本 — 對話框 + 基本圖形
SAMPLE_AA = """\
      ____
    /      \\
   /  _ノ ヽ、_  \\
  /  o゚⌒   ⌒゚o  \\      ________________
  |     (__人__)    |   < やる夫だお！       >
  \\     ` ⌒´     /     ￣￣￣￣￣￣￣￣￣￣
   ヽ、        /
    /        \\

┏━━━━━━━━━━━━━━━━━━━━━┓
┃  AA 渲染測試 12345   ┃
┃  半角: abcdefghij    ┃
┃  全角: あいうえおかきくけこ ┃
┗━━━━━━━━━━━━━━━━━━━━━┛

           ,. -─-  、
         /       \\
        /  ノ  \\ 、 ヽ
       |  (●) (●)  |    可編輯！試試輸入中文、日文、AA
       |   (__人__)   |
        ヽ、  `⌒ ´  ノ
"""


class FontTestWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        engine = os.environ.get("QT_QPA_PLATFORM", "(預設 DirectWrite)")
        self.setWindowTitle(f"PyQt6 AA 字型測試 — {engine}")
        self.resize(1100, 800)

        # 載入 fonts/monapo.ttf
        self._loaded_families: list[str] = []
        font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        if os.path.isdir(font_dir):
            for fname in os.listdir(font_dir):
                if fname.lower().endswith((".ttf", ".otf")):
                    fid = QFontDatabase.addApplicationFont(
                        os.path.join(font_dir, fname))
                    if fid >= 0:
                        self._loaded_families.extend(
                            QFontDatabase.applicationFontFamilies(fid))

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ── 控制列 ──
        ctrl = QHBoxLayout()

        ctrl.addWidget(QLabel("字型："))
        self.family_combo = QComboBox()
        candidates = list(dict.fromkeys(
            self._loaded_families + [
                "MS PGothic", "MS Gothic", "Mona", "MonaPo",
                "IPAMonaPGothic", "Saitamaar", "Consolas",
            ]
        ))
        self.family_combo.addItems(candidates)
        self.family_combo.currentTextChanged.connect(self._apply_font)
        ctrl.addWidget(self.family_combo)

        ctrl.addWidget(QLabel("大小："))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 40)
        self.size_spin.setValue(16)
        self.size_spin.valueChanged.connect(self._apply_font)
        ctrl.addWidget(self.size_spin)

        ctrl.addWidget(QLabel("Hinting："))
        self.hint_combo = QComboBox()
        self.hint_combo.addItems([
            "Default", "NoHinting", "PreferVerticalHinting",
            "PreferFullHinting",
        ])
        self.hint_combo.currentTextChanged.connect(self._apply_font)
        ctrl.addWidget(self.hint_combo)

        ctrl.addWidget(QLabel("Strategy："))
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItems([
            "Default", "NoAntialias", "PreferBitmap",
            "NoSubpixelAntialias", "NoAntialias+PreferBitmap",
        ])
        self.strategy_combo.currentTextChanged.connect(self._apply_font)
        ctrl.addWidget(self.strategy_combo)

        btn_reset = QPushButton("重置範例")
        btn_reset.clicked.connect(lambda: self.editor.setPlainText(SAMPLE_AA))
        ctrl.addWidget(btn_reset)

        ctrl.addStretch()
        root.addLayout(ctrl)

        # ── 字型量測標籤 ──
        self.metric_label = QLabel()
        self.metric_label.setStyleSheet(
            "background:#222; color:#0f0; padding:4px; font-family:Consolas;")
        root.addWidget(self.metric_label)

        # ── 編輯器 ──
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(SAMPLE_AA)
        self.editor.setStyleSheet(
            "background:#1e1e1e; color:#eee; border:1px solid #444;")
        # 關閉自動換行，AA 必須保持原樣
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self.editor, 1)

        # 預設用第一個載入的字型
        if self._loaded_families:
            idx = self.family_combo.findText(self._loaded_families[0])
            if idx >= 0:
                self.family_combo.setCurrentIndex(idx)
        self._apply_font()

    def _apply_font(self) -> None:
        family = self.family_combo.currentText()
        size = self.size_spin.value()
        font = QFont(family, size)

        # Hinting
        hint_map = {
            "Default": QFont.HintingPreference.PreferDefaultHinting,
            "NoHinting": QFont.HintingPreference.PreferNoHinting,
            "PreferVerticalHinting": QFont.HintingPreference.PreferVerticalHinting,
            "PreferFullHinting": QFont.HintingPreference.PreferFullHinting,
        }
        font.setHintingPreference(hint_map[self.hint_combo.currentText()])

        # Style strategy
        ss = QFont.StyleStrategy
        strategy_map = {
            "Default": ss.PreferDefault,
            "NoAntialias": ss.NoAntialias,
            "PreferBitmap": ss.PreferBitmap,
            "NoSubpixelAntialias": ss.NoSubpixelAntialias,
            "NoAntialias+PreferBitmap": ss(ss.NoAntialias | ss.PreferBitmap),
        }
        font.setStyleStrategy(strategy_map[self.strategy_combo.currentText()])

        self.editor.setFont(font)

        # 量測半全角寬度比例
        fm = QFontMetricsF(font)
        half = fm.horizontalAdvance("a")
        full = fm.horizontalAdvance("あ")
        ratio = full / half if half else 0
        line_h = fm.lineSpacing()
        perfect = abs(ratio - 2.0) < 0.001
        mark = "✓ 完美 1:2" if perfect else f"✗ 偏差 {ratio - 2.0:+.4f}"
        self.metric_label.setText(
            f"字型：{family} @ {size}pt  |  "
            f"半角 a = {half:.3f}px  |  "
            f"全角 あ = {full:.3f}px  |  "
            f"比例 = {ratio:.4f}  {mark}  |  "
            f"行高 = {line_h:.2f}px"
        )


def main() -> None:
    # 可用環境變數指定字型引擎：
    #   set QT_QPA_PLATFORM=windows:fontengine=gdi
    #   set QT_QPA_PLATFORM=windows:fontengine=freetype
    app = QApplication(sys.argv)
    win = FontTestWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
