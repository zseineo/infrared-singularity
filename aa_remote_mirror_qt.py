"""手機遠端控制台的「面板鏡像」：把電腦上的 Qt 面板轉成手機可渲染的欄位清單，
並把手機的修改／點擊套回電腦上的元件（v3.33）。

自動走訪面板的 layout（依畫面順序、帶 QFormLayout 的列標籤與提示文字），支援
QLineEdit／QPlainTextEdit／QComboBox／QCheckBox／QRadioButton／QSpinBox／QPushButton／
QLabel／QTableWidget。電腦面板之後新增的欄位會自動出現在手機上，不必另外改手機頁。

- 套用修改一律模擬使用者操作，讓面板原本接的訊號照常觸發（核取框用 click()、
  輸入框 setText 後補發 editingFinished、下拉補發 activated、按鈕延後到下一輪事件迴圈
  再 click，避免按鈕開的對話框把這次呼叫卡住）。
- 電腦跳出的模態對話框（QMessageBox、清單編輯 QDialog、QInputDialog）由 ``describe_modal``
  鏡像到手機，手機可直接回答；系統原生的檔案對話框無法鏡像，呼叫端要另外處理。
- 元件 id ＝ Python 物件 id；每次描述時重建對照表，找不到（畫面已變）就請手機重整。
"""
from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFormLayout, QFrame, QGroupBox, QLabel, QLayout, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
    QToolButton, QWidget,
)

_MAX_TEXT = 20000      # 多行文字欄位超過這個長度就只顯示、不讓手機改（避免一次傳太大）


class Mirror:
    """一個 Mirror 管一組 id → 元件 對照（每個 view 各一個，動作時跨 view 查找）。"""

    def __init__(self) -> None:
        self._reg: dict[str, QWidget] = {}

    # ── 描述 ──

    def describe(self, root: QWidget, *, exclude: set | None = None,
                 overrides: dict | None = None) -> list[dict]:
        """root 底下（看得到的）所有控制項，依畫面順序。

        exclude：不顯示的元件（連同其子元件）；overrides：{元件: 手機端特殊處理名稱}，
        該按鈕在手機上不直接 click，而是交給手機頁的對應功能（例如資料夾瀏覽）。
        """
        self._reg = {}
        self._root = root
        self._exclude = {id(w) for w in (exclude or set()) if w is not None}
        self._overrides = {id(w): v for w, v in (overrides or {}).items() if w is not None}
        out: list[dict] = []
        lay = root.layout()
        if lay is not None:
            self._walk_layout(lay, out)
        return out

    def describe_modal(self) -> dict | None:
        """目前的模態對話框（沒有回 None）。"""
        w = QApplication.activeModalWidget()
        if w is None:
            return None
        self._reg = {}
        self._root = w
        self._exclude = set()
        self._overrides = {}
        if isinstance(w, QMessageBox):
            btns = []
            for b in w.buttons():
                bid = self._key(b)
                btns.append({"id": bid, "text": b.text().replace("&", "")})
            return {"kind": "message", "title": w.windowTitle(), "text": w.text(),
                    "info": w.informativeText(), "buttons": btns}
        items: list[dict] = []
        if w.layout() is not None:
            self._walk_layout(w.layout(), items)
        return {"kind": "dialog", "title": w.windowTitle(), "items": items}

    def _key(self, w: QWidget) -> str:
        k = str(id(w))
        self._reg[k] = w
        return k

    def _shown(self, w: QWidget) -> bool:
        """root 顯示時這個元件看不看得到（root 本身目前在不在畫面上都一樣判斷）。"""
        return w is self._root or w.isVisibleTo(self._root)

    def _walk_layout(self, lay: QLayout, out: list, label: str = "") -> None:
        if isinstance(lay, QFormLayout):
            for r in range(lay.rowCount()):
                span = lay.itemAt(r, QFormLayout.ItemRole.SpanningRole)
                lab = lay.itemAt(r, QFormLayout.ItemRole.LabelRole)
                fld = lay.itemAt(r, QFormLayout.ItemRole.FieldRole)
                text = ""
                if lab is not None and isinstance(lab.widget(), QLabel):
                    if not lab.widget().isHidden():
                        text = _plain(lab.widget().text()).rstrip("：:").strip()
                    elif fld is not None and fld.widget() is not None and fld.widget().isHidden():
                        continue
                # 整列跨欄（addRow(widget)）時 FieldRole 也會回同一個 item，只走一次
                for item in ((span,) if span is not None else (fld,)):
                    if item is not None:
                        self._walk_item(item, out, text)
            return
        pending = label
        for i in range(lay.count()):
            item = lay.itemAt(i)
            w = item.widget()
            if isinstance(w, QLabel) and self._shown(w) and id(w) not in self._exclude:
                t = _plain(w.text()).strip()
                if t and len(t) <= 16 and t.endswith(("：", ":")) and not w.wordWrap():
                    pending = t.rstrip("：:").strip()   # 行內標籤，接到下一個控制項
                    continue
            n = len(out)
            self._walk_item(item, out, pending)
            if len(out) > n:
                pending = ""

    def _walk_item(self, item, out: list, label: str) -> None:
        if item.widget() is not None:
            self._walk_widget(item.widget(), out, label)
        elif item.layout() is not None:
            self._walk_layout(item.layout(), out, label)

    def _walk_widget(self, w: QWidget, out: list, label: str) -> None:
        # remoteSkip：面板上標記「不給手機」的元件（API 金鑰、Log 區…）
        if id(w) in self._exclude or w.property("remoteSkip") or not self._shown(w):
            return
        d = self._control(w, label)
        if d is not None:
            out.append(d)
            return
        if isinstance(w, QGroupBox):
            if w.title():
                out.append({"kind": "heading", "text": _plain(w.title())})
            if w.layout() is not None:
                self._walk_layout(w.layout(), out)
        elif isinstance(w, QScrollArea):
            if w.widget() is not None:
                self._walk_widget(w.widget(), out, label)
        elif isinstance(w, QTabWidget):
            for i in range(w.count()):
                out.append({"kind": "heading", "text": w.tabText(i)})
                self._walk_widget(w.widget(i), out, "")
        elif isinstance(w, QSplitter):
            for i in range(w.count()):
                self._walk_widget(w.widget(i), out, "")
        elif isinstance(w, QFrame) and w.frameShape() in (QFrame.Shape.HLine,):
            out.append({"kind": "sep"})
        elif w.layout() is not None:
            self._walk_layout(w.layout(), out, label)

    def _control(self, w: QWidget, label: str) -> dict | None:
        tip = _plain(w.toolTip())
        base = {"label": label, "tip": tip, "enabled": w.isEnabled()}
        if isinstance(w, QLabel):
            t = _plain(w.text()).strip()
            if not t:
                return None
            return {"kind": "text", "text": t, "tip": tip,
                    "bold": w.font().bold() or w.font().pointSize() >= 14}
        if isinstance(w, QLineEdit):
            return {**base, "id": self._key(w), "kind": "line", "value": w.text(),
                    "placeholder": w.placeholderText(), "readonly": w.isReadOnly()}
        if isinstance(w, (QPlainTextEdit, QTextEdit)):
            text = w.toPlainText()
            ro = w.isReadOnly() or len(text) > _MAX_TEXT
            return {**base, "id": self._key(w), "kind": "multiline",
                    "value": text[-_MAX_TEXT:], "readonly": ro,
                    "placeholder": w.placeholderText()}
        if isinstance(w, QComboBox):
            return {**base, "id": self._key(w), "kind": "combo",
                    "options": [w.itemText(i) for i in range(w.count())],
                    "index": w.currentIndex(), "editable": w.isEditable(),
                    "value": w.currentText()}
        if isinstance(w, (QCheckBox, QRadioButton)):
            return {**base, "id": self._key(w), "kind": "check",
                    "radio": isinstance(w, QRadioButton), "text": _plain(w.text()),
                    "checked": w.isChecked()}
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            return {**base, "id": self._key(w), "kind": "spin", "value": w.value(),
                    "min": w.minimum(), "max": w.maximum(), "step": w.singleStep(),
                    "suffix": w.suffix(), "prefix": w.prefix(),
                    "float": isinstance(w, QDoubleSpinBox)}
        if isinstance(w, (QPushButton, QToolButton)):
            text = _plain(w.text()) or tip.split("\n")[0][:20]
            if not text:
                return None
            d = {**base, "id": self._key(w), "kind": "button", "text": text,
                 "checkable": w.isCheckable(), "checked": w.isChecked()}
            if id(w) in self._overrides:   # 「名稱」或「名稱:目標元件 id」
                special, _, target = self._overrides[id(w)].partition(":")
                d["special"] = special
                if target:
                    d["target"] = target
            return d
        if isinstance(w, QTableWidget):
            return self._table(w, base)
        if isinstance(w, QListWidget):
            return {**base, "kind": "list",
                    "items": [w.item(i).text() for i in range(w.count())]}
        if isinstance(w, QAbstractSpinBox):
            return None
        return None

    def _table(self, t: QTableWidget, base: dict) -> dict:
        heads = [(t.horizontalHeaderItem(c).text() if t.horizontalHeaderItem(c) else str(c + 1))
                 for c in range(t.columnCount())]
        rows = []
        for r in range(t.rowCount()):
            cells = []
            for c in range(t.columnCount()):
                cw = t.cellWidget(r, c)
                if cw is not None:
                    cells.append(self._control(cw, "") or {"kind": "text", "text": ""})
                else:
                    it = t.item(r, c)
                    cells.append({"kind": "cell", "value": it.text() if it else ""})
            rows.append(cells)
        return {**base, "id": self._key(t), "kind": "table", "heads": heads,
                "rows": rows, "current": t.currentRow()}

    # ── 套用 ──

    def lookup(self, key: str) -> QWidget | None:
        w = self._reg.get(key)
        if w is None or sip.isdeleted(w):
            return None
        return w


def apply_set(w: QWidget, value, extra: dict | None = None) -> None:
    """把手機的新值套到元件上（模擬使用者輸入，讓原本接的訊號照常觸發）。"""
    extra = extra or {}
    if not w.isEnabled():
        raise ValueError("這個欄位目前不能修改")
    if isinstance(w, QLineEdit):
        if w.isReadOnly():
            raise ValueError("唯讀欄位")
        w.setText(str(value))
        w.editingFinished.emit()
    elif isinstance(w, (QPlainTextEdit, QTextEdit)):
        if w.isReadOnly():
            raise ValueError("唯讀欄位")
        w.setPlainText(str(value))
    elif isinstance(w, QComboBox):
        if isinstance(value, int) and not isinstance(value, bool):
            w.setCurrentIndex(value)
            w.activated.emit(value)
        elif w.isEditable():
            w.setEditText(str(value))
        else:
            i = w.findText(str(value))
            if i < 0:
                raise ValueError("沒有這個選項")
            w.setCurrentIndex(i)
            w.activated.emit(i)
    elif isinstance(w, (QCheckBox, QRadioButton)):
        if bool(value) != w.isChecked():
            w.click()
    elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
        w.setValue(float(value) if isinstance(w, QDoubleSpinBox) else int(value))
        w.editingFinished.emit()
    elif isinstance(w, QTableWidget):
        r, c = int(extra.get("row", -1)), int(extra.get("col", -1))
        if extra.get("select"):
            w.selectRow(r)
            w.setCurrentCell(r, max(0, c))
            return
        if not (0 <= r < w.rowCount() and 0 <= c < w.columnCount()):
            raise ValueError("表格位置不存在")
        it = w.item(r, c)
        if it is None:
            w.setItem(r, c, QTableWidgetItem(str(value)))
        else:
            it.setText(str(value))
    else:
        raise ValueError("這種欄位不能修改")


def apply_click(w: QWidget) -> None:
    """按下按鈕。延到下一輪事件迴圈：按鈕若開模態對話框，這次呼叫不會被卡住，
    對話框之後由 describe_modal 鏡像到手機。"""
    if not isinstance(w, QAbstractButton):
        raise ValueError("不是按鈕")
    if not w.isEnabled():
        raise ValueError("這個按鈕目前不能按")
    QTimer.singleShot(0, w.click)


def _plain(text: str) -> str:
    """去掉 HTML 標籤與 & 快捷鍵符號（QLabel 可能是 rich text）。"""
    import html
    import re
    if "<" in text and ">" in text:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return text.replace("&&", "\x00").replace("&", "").replace("\x00", "&")
