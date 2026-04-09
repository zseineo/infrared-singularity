"""QTextCursor 輔助函式 — 簡化 PyQt6 文字操作。"""
from __future__ import annotations

from PyQt6.QtGui import QTextCursor, QTextDocument
from PyQt6.QtWidgets import QPlainTextEdit


def get_line_col(cursor: QTextCursor) -> tuple[int, int]:
    """回傳游標的 (行號, 欄位)，行號從 1 開始。"""
    return cursor.blockNumber() + 1, cursor.positionInBlock()


def move_to_line(text_edit: QPlainTextEdit, line: int, col: int = 0) -> QTextCursor:
    """將游標移至指定行（1-based）的指定欄位，回傳新游標。"""
    cursor = text_edit.textCursor()
    block = text_edit.document().findBlockByLineNumber(line - 1)
    pos = block.position() + min(col, block.length() - 1)
    cursor.setPosition(pos)
    text_edit.setTextCursor(cursor)
    return cursor


def get_line_text(text_edit: QPlainTextEdit, line: int) -> str:
    """取得指定行（1-based）的文字內容。"""
    block = text_edit.document().findBlockByLineNumber(line - 1)
    return block.text()


def expand_selection_to_lines(text_edit: QPlainTextEdit) -> QTextCursor:
    """將目前選取範圍自動擴展至完整行（從行首到行尾）。

    回傳已擴展的 QTextCursor（已設為 text_edit 的游標）。
    """
    cursor = text_edit.textCursor()
    if not cursor.hasSelection():
        return cursor

    start = cursor.selectionStart()
    end = cursor.selectionEnd()

    cursor.setPosition(start)
    cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
    start = cursor.position()

    cursor.setPosition(end)
    # 如果選取結尾剛好在某行開頭（常見於拖曳選取），退回上一行行尾
    if cursor.positionInBlock() == 0 and end > start:
        cursor.movePosition(QTextCursor.MoveOperation.Left)
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
    end = cursor.position()

    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    text_edit.setTextCursor(cursor)
    return cursor


def find_text(
    text_edit: QPlainTextEdit,
    query: str,
    *,
    case_sensitive: bool = False,
    wrap: bool = True,
) -> bool:
    """從目前游標位置搜尋文字並選取。支援 wrap-around。

    回傳是否找到。
    """
    flags = QTextDocument.FindFlag(0)
    if case_sensitive:
        flags |= QTextDocument.FindFlag.FindCaseSensitively

    found = text_edit.find(query, flags)
    if not found and wrap:
        # 從文件開頭再搜一次
        cursor = text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        text_edit.setTextCursor(cursor)
        found = text_edit.find(query, flags)
    return found
