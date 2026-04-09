"""PyQt6 UI 輔助工具 — 按鈕工廠、Toast 浮動提示。"""
from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QPushButton, QWidget


def make_button(
    text: str,
    *,
    color: str,
    hover: str,
    font: QFont,
    text_color: str = "white",
    width: int | None = None,
    parent: QWidget | None = None,
) -> QPushButton:
    """建立帶有 Bootstrap 風格顏色的按鈕。"""
    btn = QPushButton(text, parent)
    btn.setFont(font)
    if width:
        btn.setFixedWidth(width)
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {color};
            color: {text_color};
            border: none;
            border-radius: 4px;
            padding: 4px 10px;
        }}
        QPushButton:hover {{
            background-color: {hover};
        }}
        QPushButton:disabled {{
            background-color: #555555;
            color: #888888;
        }}
    """)
    return btn


def show_toast(
    parent: QWidget,
    message: str,
    *,
    color: str = "#28a745",
    duration: int = 3000,
) -> QLabel:
    """在 parent 右上角顯示浮動 Toast 提示，duration 毫秒後自動消失。"""
    toast = QLabel(message, parent)
    toast.setStyleSheet(f"""
        QLabel {{
            background-color: {color};
            color: white;
            font-family: "Microsoft JhengHei";
            font-size: 14px;
            font-weight: bold;
            padding: 10px 20px;
            border-radius: 8px;
        }}
    """)
    toast.adjustSize()
    # 定位到右上角
    x = parent.width() - toast.width() - 20
    y = 55
    toast.move(x, y)
    toast.raise_()
    toast.show()
    QTimer.singleShot(duration, toast.deleteLater)
    return toast
