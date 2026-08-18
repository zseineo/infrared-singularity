"""UI 版面解析度適配檢查（離螢幕跑，不需要真的有 2K／4K 螢幕）。

用途
----
主視窗能縮到多小，取決於各橫列（工具列、按鈕列…）的最小寬度總和。只要螢幕的
**邏輯寬度**小於視窗最小寬度，即使最大化，視窗仍比畫面寬，右側按鈕就會落到
螢幕外——這正是「非 1080P 使用者看不到某些按鈕」的成因。

邏輯寬度 ＝ 實際解析度 ÷ Windows 縮放比例，常見值：

    1920x1080 @100% → 1920      1920x1080 @150% → 1280
    1920x1080 @125% → 1536      1920x1080 @175% → 1097
    2560x1440 @150% → 1706      2560x1440 @200% → 1280
    3840x2160 @150% → 2560      3840x2160 @300% → 1280
    1366x768  @100% → 1366      1366x768  @125% → 1092

用法
----
    py -3.12 check_ui_layout.py

輸出各面板最小寬度，並在一連串邏輯尺寸下檢查有沒有元件被裁切／超出視窗。
最小寬度超過 MAX_MIN_WIDTH 或發現裁切時以 exit code 1 結束（可接 CI／手動跑）。

想用眼睛看實際畫面，改用縮放倍率直接跑主程式（1080P 螢幕即可模擬）：

    QT_SCALE_FACTOR=1.5  py -3.12 aa_main_qt.py   # 等同邏輯 1280
    QT_SCALE_FACTOR=1.75 py -3.12 aa_main_qt.py   # 等同邏輯 1097
    QT_FONT_DPI=144      py -3.12 aa_main_qt.py   # 只放大字級
"""
from __future__ import annotations

import os
import sys

# 必須在 import PyQt6 之前設定
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QAbstractScrollArea, QApplication, QCheckBox, QComboBox, QLineEdit,
    QPushButton, QStackedWidget,
)

# 要通過檢查的最大「視窗最小寬度」。1024 是保守值：涵蓋 1366x768 @125%
# （邏輯 1092）與 1080p @150%（邏輯 1280）等實際回報過的組合。
MAX_MIN_WIDTH = 1024
MAX_MIN_HEIGHT = 700

# 檢查用的邏輯尺寸矩陣（寬, 高）
SIZES = [
    (1920, 1080),
    (1706, 960),
    (1536, 864),
    (1366, 768),
    (1280, 720),
    (1092, 614),
]

WIDGET_TYPES = (QPushButton, QLineEdit, QCheckBox, QComboBox)

# (顯示名稱, MainWindow 上切換到該面板的方法名)
PANELS = [
    ("翻譯主畫面", "show_translate_panel"),
    ("批次搜尋", "show_batch_panel"),
    ("自動翻譯", "show_auto_translate_panel"),
    ("網址讀取", "open_url_fetch_qt"),
]


def _label(widget) -> str:
    text = widget.text() if hasattr(widget, "text") else ""
    return (text or widget.__class__.__name__)[:24]


def _in_scroll_area(widget) -> bool:
    """元件是否位於可捲動區內——捲動區裡的內容超出視野是正常的，不算裁切。"""
    node = widget.parentWidget()
    while node is not None:
        if isinstance(node, QAbstractScrollArea):
            return True
        node = node.parentWidget()
    return False


def _scan_clipping(win) -> list[str]:
    """回傳被裁切或超出視窗範圍的元件描述。"""
    bad: list[str] = []
    for cls in WIDGET_TYPES:
        for child in win.findChildren(cls):
            if not child.isVisible() or _in_scroll_area(child):
                continue
            top_left = child.mapTo(win, QPoint(0, 0))
            right = top_left.x() + child.width()
            bottom = top_left.y() + child.height()
            visible = child.visibleRegion().boundingRect()
            if right > win.width() + 1 or bottom > win.height() + 1:
                bad.append(f"    [超出視窗] {_label(child):<24} "
                           f"x={top_left.x()}..{right} y={top_left.y()}..{bottom}")
            elif (visible.width() < child.width() - 1
                    or visible.height() < child.height() - 1):
                bad.append(f"    [被裁切]   {_label(child):<24} "
                           f"實際 {child.width()}x{child.height()} "
                           f"可見 {visible.width()}x{visible.height()}")
    return bad


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import aa_main_qt as M

    app = QApplication([])
    win = M.MainWindow()
    win.show()
    app.processEvents()

    failures: list[str] = []

    min_w = win.minimumSizeHint().width()
    min_h = win.minimumSizeHint().height()
    print("── 視窗最小尺寸 ──")
    print(f"  MainWindow  最小寬 {min_w}  最小高 {min_h}"
          f"   (門檻 {MAX_MIN_WIDTH}x{MAX_MIN_HEIGHT})")
    if min_w > MAX_MIN_WIDTH:
        failures.append(f"視窗最小寬度 {min_w} > {MAX_MIN_WIDTH}")
    if min_h > MAX_MIN_HEIGHT:
        failures.append(f"視窗最小高度 {min_h} > {MAX_MIN_HEIGHT}")

    print()
    print("── 各面板／橫列最小寬度（找出撐大最小寬度的元凶）──")
    for stack in win.findChildren(QStackedWidget):
        for i in range(stack.count()):
            page = stack.widget(i)
            page_min = page.minimumSizeHint().width()
            if page_min < 0:
                continue  # 尚未建立內容的頁面
            print(f"  {page.__class__.__name__:<22} 最小寬 {page_min}")
    panel = getattr(win, "_translate_panel", None)
    row = getattr(panel, "_toolbar", None) if panel is not None else None
    if row is not None:
        print(f"  {'工具列 (_toolbar)':<20} 最小寬 {row.minimumSizeHint().width()}")

    print()
    print("── 各面板 × 各邏輯尺寸的裁切檢查 ──")
    for panel_name, switch in PANELS:
        try:
            getattr(win, switch)()
            app.processEvents()
        except Exception as exc:  # noqa: BLE001 — 面板開不起來也要報告，不中斷
            print(f"  {panel_name}：<切換失敗 {exc}>")
            failures.append(f"{panel_name} 切換失敗：{exc}")
            continue
        print(f"  【{panel_name}】")
        for width, height in SIZES:
            # 部分面板（如網址讀取的歷史清單）需要幾輪 layout 才會收斂到真正的
            # 最小寬度，只跑一次會量到偏大的過渡值，故重複 resize 直到穩定。
            for _ in range(4):
                win.resize(width, height)
                app.processEvents()
            actual = f"{win.width()}x{win.height()}"
            bad = _scan_clipping(win)
            note = "" if actual == f"{width}x{height}" else f"  ← 縮不下去，實際 {actual}"
            print(f"    {width}x{height}{note}"
                  + ("" if bad else "  (無裁切)"))
            if actual != f"{width}x{height}":
                failures.append(f"{panel_name} {width}x{height} 縮不下去，實際 {actual}")
            for line in bad:
                print(line)
            failures.extend(f"{panel_name} {b.strip()}" for b in bad)

    print()
    if failures:
        print(f"❌ 檢查未通過（{len(failures)} 項）：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✅ 檢查通過：所有測試尺寸都放得下，沒有元件被裁切。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
