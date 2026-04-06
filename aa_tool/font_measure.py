from typing import Protocol


class FontMeasurer(Protocol):
    """字體寬度量測介面。PyQt6 遷移時只需實作此 Protocol。"""
    def measure(self, text: str) -> int:
        """回傳文字在目標字體下的像素寬度。"""
        ...


class TkFontMeasurer:
    """Tkinter / customtkinter 字體量測實作。"""
    def __init__(self, font):
        self._font = font

    def measure(self, text: str) -> int:
        return int(self._font.measure(text))
