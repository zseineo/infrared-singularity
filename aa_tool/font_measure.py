from typing import Protocol


class FontMeasurer(Protocol):
    """字體寬度量測介面。PyQt6 遷移時只需實作此 Protocol。"""
    def measure(self, text: str) -> int:
        """回傳文字在目標字體下的像素寬度。"""
        ...


class QtFontMeasurer:
    """PyQt6 字體量測實作。"""
    def __init__(self, font):
        from PyQt6.QtGui import QFontMetrics
        self._metrics = QFontMetrics(font)

    def measure(self, text: str) -> int:
        return self._metrics.horizontalAdvance(text)
