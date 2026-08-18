"""PyQt6 UI 輔助工具 — 按鈕工廠、Toast 浮動提示。"""
from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, QTimer, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QLabel, QLayout, QLayoutItem, QPushButton, QWidget,
)


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
    """在 parent 右上角顯示浮動 Toast 提示，duration 毫秒後自動消失。
    新 Toast 出現時會清除同一 parent 上尚未消失的舊 Toast，避免重疊。"""
    # 移除舊 Toast（避免重疊）
    active: list = getattr(parent, "_active_toasts", [])
    for old in active:
        try:
            old.deleteLater()
        except RuntimeError:
            pass
    active = []

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

    active.append(toast)
    parent._active_toasts = active

    def _cleanup() -> None:
        try:
            if toast in parent._active_toasts:
                parent._active_toasts.remove(toast)
        except (AttributeError, ValueError):
            pass
        try:
            toast.deleteLater()
        except RuntimeError:
            pass

    QTimer.singleShot(duration, _cleanup)
    return toast


# ════════════════════════════════════════════════════════════
#  WrapRow — 視窗變窄時會自動換行的「左區塊／右區塊」橫列
# ════════════════════════════════════════════════════════════
#
#  問題背景：工具列用 QHBoxLayout 一字排開時，整列的最小寬度＝所有按鈕文字寬
#  度總和（按鈕壓不下去）。視窗最小寬度因此被撐到 1292 邏輯 px，只要螢幕的
#  「邏輯寬度」小於它（例：1080p @150% 縮放 ＝ 1280、4K @300% ＝ 1280、
#  1366x768 @125% ＝ 1092），視窗就比畫面寬，右側按鈕會落到螢幕外看不到。
#
#  WrapRow 讓橫列在放不下時把右區塊換到第二行，最小寬度降為「較寬的那一個
#  區塊」，不再是兩者相加。

class TwoGroupWrapLayout(QLayout):
    """兩個區塊的橫列版面：放得下 → 同一列（左靠左、右靠右）；放不下 → 上下堆疊。

    只處理兩個 item（多的會被忽略，少的照樣運作）。高度隨換行變化，因此實作
    `heightForWidth()`；使用端（`WrapRow`）需一併把 sizePolicy 的
    heightForWidth 打開，父版面才會依實際換行結果配置高度。
    """

    #: 換行時兩列之間的垂直間距（px）
    ROW_GAP = 4

    def __init__(self, parent: QWidget | None = None, gap: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._gap = gap

    # ── QLayout 必要介面 ──

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 (Qt 命名)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), apply_geometry=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, apply_geometry=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        l, t, r, b = self.getContentsMargins()
        hints = [it.sizeHint() for it in self._items]
        if not hints:
            return QSize(l + r, t + b)
        w = sum(h.width() for h in hints) + self._gap * (len(hints) - 1)
        return QSize(w + l + r, max(h.height() for h in hints) + t + b)

    def minimumSize(self) -> QSize:  # noqa: N802
        """最小寬度＝「較寬的單一區塊」（放不下時會換行，不必兩者並排）。"""
        l, t, r, b = self.getContentsMargins()
        mins = [it.minimumSize() for it in self._items]
        if not mins:
            return QSize(l + r, t + b)
        return QSize(max(m.width() for m in mins) + l + r,
                     max(m.height() for m in mins) + t + b)

    # ── 實際配置 ──

    def _do_layout(self, rect: QRect, apply_geometry: bool) -> int:
        """配置（或僅試算）並回傳所需總高度。"""
        l, t, r, b = self.getContentsMargins()
        eff = rect.adjusted(l, t, -r, -b)
        if not self._items:
            return t + b
        if len(self._items) == 1:
            it = self._items[0]
            h = it.sizeHint().height()
            if apply_geometry:
                it.setGeometry(QRect(eff.x(), eff.y(), eff.width(), h))
            return h + t + b

        left, right = self._items[0], self._items[1]
        lw, lh = left.sizeHint().width(), left.sizeHint().height()
        rw, rh = right.sizeHint().width(), right.sizeHint().height()

        if lw + self._gap + rw <= eff.width():
            if apply_geometry:
                left.setGeometry(QRect(eff.x(), eff.y(), lw, lh))
                # 右區塊靠右對齊，維持原本 addStretch() 的視覺
                right.setGeometry(
                    QRect(eff.x() + eff.width() - rw, eff.y(), rw, rh))
            return max(lh, rh) + t + b

        # 放不下 → 右區塊換到第二行（仍靠右）
        if apply_geometry:
            left.setGeometry(
                QRect(eff.x(), eff.y(), min(lw, eff.width()), lh))
            y2 = eff.y() + lh + self.ROW_GAP
            x2 = max(eff.x(), eff.x() + eff.width() - rw)
            right.setGeometry(QRect(x2, y2, min(rw, eff.width()), rh))
        return lh + self.ROW_GAP + rh + t + b


class WrapRow(QWidget):
    """承載 `TwoGroupWrapLayout` 的容器；窄視窗時可自動隱藏一個「可省略」元件。

    `collapsible`（通常是標題 QLabel）在整列排不進同一行時自動隱藏，讓工具列
    在常見的窄邏輯寬度（如 1280）仍維持單行；門檻以「標題顯示時的完整寬度」
    為準且固定不變，因此不會在顯示／隱藏之間來回跳動。
    """

    def __init__(self, left: QWidget, right: QWidget, *,
                 margins: tuple[int, int, int, int] = (10, 5, 10, 5),
                 gap: int = 8, collapsible: QWidget | None = None) -> None:
        super().__init__()
        lay = TwoGroupWrapLayout(self, gap=gap)
        lay.setContentsMargins(*margins)
        lay.addWidget(left)
        lay.addWidget(right)
        self._collapsible = collapsible
        self._need_width: int | None = None
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        if self._collapsible is None:
            return
        if self._need_width is None:
            if not self._collapsible.isVisible():
                return  # 尚未量到「含標題」的完整寬度，等下次再量
            self._need_width = self.layout().sizeHint().width()
        want = self.width() >= self._need_width
        if want == self._collapsible.isVisible():
            return
        self._collapsible.setVisible(want)
        # 顯示／隱藏會改變本列所需高度（少一個元件可能就不必換行了）；父版面的
        # heightForWidth 已針對這次 resize 算過一次，不主動通知就會留下多餘空白。
        self.layout().invalidate()
        self.updateGeometry()
