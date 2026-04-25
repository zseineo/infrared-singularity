from typing import Protocol


class FontMeasurer(Protocol):
    """字體寬度量測介面。PyQt6 實作見 aa_edit_qt.py 的 QtFontMeasurer。"""
    def measure(self, text: str) -> float:
        """回傳文字在目標字體下的像素寬度（浮點，避免 1px 累積誤差）。"""
        ...
