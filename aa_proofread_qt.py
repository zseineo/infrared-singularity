"""AI 校稿素材匯出（實驗性，v3.55）的 Qt 端：貼文截圖＋整批輸出。

對照表的產生在 `aa_tool/proofread_export.py`（純函式）；這裡負責
把每則貼文的譯文畫成 PNG，以及組出整包資料夾：

    <輸出位置>/AI校稿_YYYYMMDD_HHMMSS/
        校稿說明.md      給 AI 的任務說明與輸出格式
        術語表.txt       主畫面術語表（一般＋暫時）
        <檔名>/對照表.txt
        <檔名>/images/p012.png …
"""
from __future__ import annotations

import os
import time

from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter

from aa_tool import original_cache
from aa_tool import proofread_export as pe

# 截圖字型＝存檔 HTML 在瀏覽器的顯示（MS PGothic 16px），AA 才對得齊。
_IMG_FONT_FAMILY = "MS PGothic"
_IMG_FONT_PX = 16
_IMG_PAD = 8


def _image_font() -> QFont:
    f = QFont(_IMG_FONT_FAMILY)
    f.setPixelSize(_IMG_FONT_PX)
    return f


def render_lines(lines: list[str], path: str, font: QFont) -> None:
    fm = QFontMetrics(font)
    ls = fm.lineSpacing()
    w = max([fm.horizontalAdvance(t) for t in lines] + [100]) + _IMG_PAD * 2
    h = ls * len(lines) + _IMG_PAD * 2
    # 帶 alpha 的格式：Qt 不做 ClearType 次像素反鋸齒，字邊不會有彩色鑲邊
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setFont(font)
    p.setPen(QColor("black"))
    for i, t in enumerate(lines):
        p.drawText(_IMG_PAD, _IMG_PAD + fm.ascent() + i * ls, t)
    p.end()
    img.save(path, "PNG")


def _unique_dir(parent: str, name: str) -> str:
    path, n = os.path.join(parent, name), 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{name}-{n}")
        n += 1
    return path


def export_packets(files: list[str], out_dir: str, base_dir: str,
                   glossary_text: str, progress=None) -> dict:
    """匯出整包校稿素材。

    ``progress(i, total, name)`` 回傳 False 表示使用者取消。
    回傳 ``{'dir', 'done': [名稱], 'skipped': [(名稱, 原因)], 'cancelled'}``。
    """
    root = _unique_dir(out_dir, time.strftime("AI校稿_%Y%m%d_%H%M%S"))
    os.makedirs(root)
    with open(os.path.join(root, "校稿說明.md"), "w", encoding="utf-8") as f:
        f.write(pe.INSTRUCTIONS)
    with open(os.path.join(root, "術語表.txt"), "w", encoding="utf-8") as f:
        f.write(glossary_text.strip() + "\n")

    font = _image_font()
    result = {"dir": root, "done": [], "skipped": [], "cancelled": False}
    for i, path in enumerate(files):
        name = os.path.basename(path)
        if progress and progress(i, len(files), name) is False:
            result["cancelled"] = True
            break
        try:
            cur = pe.plain_lines_from_html(path)
        except OSError as e:
            result["skipped"].append((name, f"讀不到檔案：{e}"))
            continue
        if cur is None:
            result["skipped"].append((name, "不是譯文檔（沒有 <pre>）"))
            continue
        entry = original_cache.load_entry_for_html(base_dir, path)
        if not entry:
            result["skipped"].append((name, "找不到原文暫存，無法對照原文"))
            continue
        # 舊暫存可能只有原文沒有提取結果：仍可列出譯文（只是沒有「原：」行）
        table, posts, _st = pe.build_table(
            name, entry["text"], entry.get("extracted") or "", cur)
        sub = _unique_dir(root, os.path.splitext(name)[0])
        img_dir = os.path.join(sub, "images")
        os.makedirs(img_dir)
        with open(os.path.join(sub, "對照表.txt"), "w", encoding="utf-8") as f:
            f.write(table)
        for post in posts:
            if not post["has_text"]:
                continue
            chunks = pe.image_chunks(post["start"], post["end"], cur)
            for k, (a, b) in enumerate(chunks, 1):
                suffix = f"-{k}" if len(chunks) > 1 else ""
                render_lines(cur[a - 1:b],
                             os.path.join(img_dir, f"p{post['no']:03d}{suffix}.png"), font)
        result["done"].append(name)
    return result
