"""最終結果預覽視窗 — UI 模組，依賴 customtkinter。

PyQt6 遷移時只需重寫此檔案，邏輯模組無需變動。
"""
from __future__ import annotations

import os
import re
import tkinter as tk
from tkinter import filedialog, colorchooser
from typing import TYPE_CHECKING

import customtkinter as ctk

from .font_measure import TkFontMeasurer
from .bubble_alignment import (
    adjust_bubble as _adjust_bubble,
    adjust_all_bubbles as _adjust_all_bubbles,
    align_to_prev_line as _align_to_prev_line,
)
from .translation_engine import (
    parse_glossary,
    apply_glossary_to_text,
    _replace_with_padding,
)

if TYPE_CHECKING:
    from ..aa_translation_tool import AATranslationTool


def show_result_modal(
    app: AATranslationTool,
    text: str,
    source_file: str = "",
    scroll_to_line: int | None = None,
) -> None:
    """建立並顯示最終結果預覽視窗（或內嵌分頁）。"""
    use_tab = hasattr(app, 'experimental_edit_tab') and app.experimental_edit_tab.get()

    if use_tab:
        for w in app.edit_frame.winfo_children():
            w.destroy()
        container = app.edit_frame
        modal = None
    else:
        modal = ctk.CTkToplevel(app)
        container = modal

        match = re.search(r'第\s*(\d+)\s*話', text[:500])
        if match:
            chapter_str = f" - 第{match.group(1)}話"
        else:
            match = re.search(r'番外編\s*(\d+)', text[:500])
            if match:
                chapter_str = f" - 番外編{match.group(1)}"
            else:
                chapter_str = ""

        modal.title(f"✨ 最終結果預覽 (全螢幕){chapter_str}")
        modal.state("zoomed")

    # ── 關閉動作 ──
    def close_action():
        app.preview_text_cache = final_textbox.get("1.0", tk.END).rstrip('\n')
        app.save_cache()
        if modal:
            modal.destroy()
        else:
            app.switch_mode(app._previous_mode)

    if modal:
        modal.bind("<Escape>", lambda e: close_action())
        modal.protocol("WM_DELETE_WINDOW", close_action)
        modal.transient(app)

    # ── Toast 輔助 ──
    def show_toast(message, color="#28a745", duration=3000):
        target = modal if modal else app
        toast = ctk.CTkFrame(target, fg_color=color, corner_radius=8)
        toast.place(relx=1.0, rely=0.0, anchor="ne", x=-20, y=60 if modal else 55)
        lbl = ctk.CTkLabel(toast, text=message, text_color="white", font=app.ui_font)
        lbl.pack(padx=20, pady=10)
        target.after(duration, toast.destroy)

    # ════════════════════════════════════════════════════════════
    #  工具列
    # ════════════════════════════════════════════════════════════
    toolbar = ctk.CTkFrame(container, fg_color="#343a40", corner_radius=0)
    toolbar.pack(fill="x")

    tb_inner = ctk.CTkFrame(toolbar, fg_color="transparent")
    tb_inner.pack(fill="x", padx=10, pady=5)

    # ── 群組 1：全文替換 + 術語 ──
    grp1 = ctk.CTkFrame(tb_inner, fg_color="transparent")
    grp1.pack(side="left", padx=5)

    ctk.CTkLabel(grp1, text="全文替換", text_color="white", font=app.ui_font).pack(side="left", padx=2)
    quick_orig = ctk.CTkEntry(grp1, placeholder_text="原文", width=120, font=app.ui_font)
    quick_orig.pack(side="left", padx=2)
    quick_trans = ctk.CTkEntry(grp1, placeholder_text="翻譯", width=120, font=app.ui_font)
    quick_trans.pack(side="left", padx=2)

    save_to_glossary_var = ctk.BooleanVar(value=True)
    ctk.CTkCheckBox(
        grp1, text="存入術語表", variable=save_to_glossary_var,
        text_color="white", font=app.ui_small_font, width=80,
    ).pack(side="left", padx=5)

    # ── 文字框 ──
    final_textbox = ctk.CTkTextbox(
        container, font=app.result_font, wrap="none",
        fg_color=app.bg_color, text_color=app.fg_color, undo=True,
    )
    final_textbox._textbox.configure(spacing1=2, spacing3=2)

    def _on_mousewheel(event):
        units = int(-(event.delta / 120) * 8)
        final_textbox._textbox.yview_scroll(units, "units")
        return "break"

    final_textbox._textbox.bind("<MouseWheel>", _on_mousewheel)

    # ── 全文替換 ──
    def quick_replace():
        orig = quick_orig.get().strip()
        trans = quick_trans.get().strip()
        if not orig or not trans:
            show_toast("⚠️ 不可為空！", color="#f39c12")
            return

        len_diff = len(orig) - len(trans)
        padded_trans = trans + ('　' * len_diff if len_diff > 0 else '')

        current_yview = final_textbox._textbox.yview()
        current_text = final_textbox.get("1.0", tk.END).rstrip('\n')
        lines = current_text.split('\n')
        for i in range(len(lines)):
            lines[i] = _replace_with_padding(lines[i], orig, trans, padded_trans)

        final_textbox.delete("1.0", tk.END)
        final_textbox.insert("1.0", '\n'.join(lines))
        final_textbox._textbox.yview_moveto(current_yview[0])

        if save_to_glossary_var.get():
            g_text = app.glossary_text.get("1.0", tk.END).rstrip('\n')
            if g_text:
                g_text += '\n'
            g_text += f"{orig}={trans}"
            app.glossary_text.delete("1.0", tk.END)
            app.glossary_text.insert("1.0", g_text)
            app.save_cache()

        quick_orig.delete(0, tk.END)
        quick_trans.delete(0, tk.END)

    ctk.CTkButton(
        grp1, text="執行", command=quick_replace,
        fg_color="#17a2b8", hover_color="#138496", font=app.ui_small_font, width=50,
    ).pack(side="left", padx=5)

    # ── 重套術語 ──
    def reapply_glossary():
        glossary_str = app.get_combined_glossary()
        if not glossary_str:
            show_toast("⚠️ 術語表為空！", color="#f39c12")
            return

        glossary = parse_glossary(glossary_str)
        if not glossary:
            show_toast("⚠️ 術語表格式不正確或為空！", color="#f39c12")
            return

        current_yview = final_textbox._textbox.yview()
        current_text = final_textbox.get("1.0", tk.END).rstrip('\n')
        new_text = apply_glossary_to_text(current_text, glossary)
        final_textbox.delete("1.0", tk.END)
        final_textbox.insert("1.0", new_text)
        final_textbox._textbox.yview_moveto(current_yview[0])
        app.save_cache()
        show_toast("✅ 已套用術語表變更！", color="#28a745")

    ctk.CTkButton(
        grp1, text="重套術語", command=reapply_glossary,
        fg_color="#28a745", hover_color="#218838", font=app.ui_small_font, width=60,
    ).pack(side="left", padx=5)

    # ── 群組 2：選區操作 ──
    grp2 = ctk.CTkFrame(tb_inner, fg_color="transparent")
    grp2.pack(side="left", padx=20)

    color_btn = ctk.CTkButton(grp2, text="", fg_color="#ff0000", width=40, hover_color="#cc0000")
    color_btn.pack(side="left", padx=2)
    current_color = ["#ff0000"]

    def choose_color():
        color_code = colorchooser.askcolor(title="選擇顏色", initialcolor=current_color[0])[1]
        if color_code:
            current_color[0] = color_code
            color_btn.configure(fg_color=color_code, hover_color=color_code)

    color_btn.configure(command=choose_color)

    def apply_color():
        try:
            first = final_textbox._textbox.index(tk.SEL_FIRST)
            last = final_textbox._textbox.index(tk.SEL_LAST)
            selected_text = final_textbox._textbox.get(first, last)
            if not selected_text:
                return

            if re.search(r'<span style="color:[^"]*">', selected_text):
                stripped_text = re.sub(r'<span style="color:[^"]*">', '', selected_text)
                stripped_text = stripped_text.replace('</span>', '')
                final_textbox._textbox.delete(first, last)
                final_textbox._textbox.insert(first, stripped_text)
            else:
                colored_text = f'<span style="color:{current_color[0]}">{selected_text}</span>'
                final_textbox._textbox.delete(first, last)
                final_textbox._textbox.insert(first, colored_text)
        except tk.TclError:
            show_toast("⚠️ 請先選取想要上色的文字！", color="#f39c12")

    ctk.CTkButton(
        grp2, text="上色", command=apply_color,
        fg_color="#6f42c1", hover_color="#5a32a3", font=app.ui_small_font, width=60,
    ).pack(side="left", padx=5)

    def strip_spaces():
        try:
            first = final_textbox._textbox.index(tk.SEL_FIRST)
            last = final_textbox._textbox.index(tk.SEL_LAST)
            selected_text = final_textbox._textbox.get(first, last)
            if not selected_text:
                return
            stripped_text = selected_text.replace(" ", "").replace("　", "")
            final_textbox._textbox.delete(first, last)
            final_textbox._textbox.insert(first, stripped_text)
        except tk.TclError:
            show_toast("⚠️ 請先選取想要消除空白的文字！", color="#f39c12")

    ctk.CTkButton(
        grp2, text="消空白", command=strip_spaces,
        fg_color="#e0a800", hover_color="#c82333", text_color="black",
        font=app.ui_small_font, width=60,
    ).pack(side="left", padx=5)

    def add_double_spaces():
        try:
            first = final_textbox._textbox.index(tk.SEL_FIRST)
            last = final_textbox._textbox.index(tk.SEL_LAST)
            selected_text = final_textbox._textbox.get(first, last)
            if not selected_text:
                return
            lines = selected_text.split('\n')
            spaced_lines = ["　　".join(list(line)) for line in lines]
            spaced_text = '\n'.join(spaced_lines)
            final_textbox._textbox.delete(first, last)
            final_textbox._textbox.insert(first, spaced_text)
        except tk.TclError:
            show_toast("⚠️ 請先選取想要補空白的文字！", color="#f39c12")

    ctk.CTkButton(
        grp2, text="補空白", command=add_double_spaces,
        fg_color="#17a2b8", hover_color="#138496", text_color="white",
        font=app.ui_small_font, width=60,
    ).pack(side="left", padx=5)

    # ── 對話框修正 ──
    font_measurer = TkFontMeasurer(app.result_font)

    def adjust_bubble():
        try:
            first_idx = final_textbox._textbox.index(tk.SEL_FIRST)
            last_idx = final_textbox._textbox.index(tk.SEL_LAST)

            first = final_textbox._textbox.index(f"{first_idx} linestart")
            if last_idx.split('.')[1] == '0' and last_idx != first_idx:
                last = final_textbox._textbox.index(f"{last_idx} - 1 chars lineend")
            else:
                last = final_textbox._textbox.index(f"{last_idx} lineend")

            selected_text = final_textbox._textbox.get(first, last)
            if not selected_text:
                return

            result = _adjust_bubble(selected_text, font_measurer)
            if result is None:
                show_toast("⚠️ 無法辨識對話框類型！", color="#f39c12")
            elif result.startswith('⚠️'):
                show_toast(result, color="#f39c12")
            else:
                final_textbox._textbox.delete(first, last)
                final_textbox._textbox.insert(first, result)
        except tk.TclError:
            show_toast("⚠️ 請先選取想要調整的對話框！", color="#f39c12")

    ctk.CTkButton(
        grp2, text="對話框修正", command=adjust_bubble,
        fg_color="#28a745", hover_color="#218838", text_color="white",
        font=app.ui_small_font, width=80,
    ).pack(side="left", padx=5)

    def align_to_prev_line():
        try:
            cursor_pos = final_textbox._textbox.index(tk.INSERT)
            line_idx, col_idx = cursor_pos.split('.')
            line_idx = int(line_idx)
            col_idx = int(col_idx)

            prev_line_idx = line_idx - 1
            if prev_line_idx < 1:
                show_toast("⚠️ 這是第一行，沒有上一行可以對齊！", color="#f39c12")
                return

            prev_line_text = final_textbox._textbox.get(f"{prev_line_idx}.0", f"{prev_line_idx}.end")
            prev_line_text = prev_line_text.rstrip('\r\n \u3000')

            if not prev_line_text:
                show_toast("⚠️ 上一行為空，無法對齊！", color="#f39c12")
                return

            current_line_text = final_textbox._textbox.get(f"{line_idx}.0", f"{line_idx}.end")
            align_result = _align_to_prev_line(prev_line_text, current_line_text, col_idx, font_measurer)

            if align_result is None:
                show_toast("⚠️ 游標後方沒有可以對齊的符號！", color="#f39c12")
                return

            new_line, new_col = align_result
            final_textbox._textbox.delete(f"{line_idx}.0", f"{line_idx}.end")
            final_textbox._textbox.insert(f"{line_idx}.0", new_line)
            final_textbox._textbox.mark_set(tk.INSERT, f"{line_idx}.{new_col}")
            final_textbox._textbox.see(tk.INSERT)
        except Exception as e:
            show_toast(f"❌ 無法對齊：{e}", color="#dc3545", duration=5000)

    ctk.CTkButton(
        grp2, text="對齊上一行", command=align_to_prev_line,
        fg_color="#17a2b8", hover_color="#138496", text_color="white",
        font=app.ui_small_font, width=80,
    ).pack(side="left", padx=5)

    def smart_action(event=None):
        try:
            first = final_textbox._textbox.index(tk.SEL_FIRST)
            last = final_textbox._textbox.index(tk.SEL_LAST)
            selected_text = final_textbox._textbox.get(first, last)
            if "\n" in selected_text:
                adjust_bubble()
            else:
                apply_color()
        except tk.TclError:
            align_to_prev_line()

    ctk.CTkButton(
        grp2, text="自動判斷", command=smart_action,
        fg_color="#e67e22", hover_color="#d35400", text_color="white",
        font=app.ui_small_font, width=70,
    ).pack(side="left", padx=5)

    def adjust_all_bubbles():
        text_content = final_textbox.get("1.0", tk.END).rstrip('\n')
        new_text, count = _adjust_all_bubbles(text_content, font_measurer)

        if count == 0:
            show_toast("⚠️ 未找到可處理的獨立對話框！", color="#f39c12")
            return

        scroll_pos = final_textbox._textbox.yview()
        cursor_pos = final_textbox._textbox.index(tk.INSERT)
        final_textbox._textbox.delete("1.0", tk.END)
        final_textbox._textbox.insert("1.0", new_text)
        final_textbox._textbox.mark_set(tk.INSERT, cursor_pos)
        final_textbox._textbox.yview_moveto(scroll_pos[0])
        show_toast(f"✅ 已自動調整 {count} 個對話框！", color="#28a745")

    ctk.CTkButton(
        grp2, text="對話框(全)", command=adjust_all_bubbles,
        fg_color="#20c997", hover_color="#17a085", text_color="white",
        font=app.ui_small_font, width=80,
    ).pack(side="left", padx=5)

    def handle_smart_action_event(e=None):
        smart_action()
        return "break"

    container.bind("<Control-q>", handle_smart_action_event)
    container.bind("<Control-Q>", handle_smart_action_event)
    final_textbox._textbox.bind("<Control-q>", handle_smart_action_event)
    final_textbox._textbox.bind("<Control-Q>", handle_smart_action_event)

    # ── 群組 3：右側工具 ──
    grp3 = ctk.CTkFrame(tb_inner, fg_color="transparent")
    grp3.pack(side="right", padx=5)

    def choose_bg_color():
        color_code = colorchooser.askcolor(title="選擇背景顏色")[1]
        if color_code:
            app.bg_color = color_code
            final_textbox.configure(fg_color=color_code)
            app.save_cache()

    def choose_fg_color():
        color_code = colorchooser.askcolor(title="選擇文字顏色")[1]
        if color_code:
            app.fg_color = color_code
            final_textbox.configure(text_color=color_code)
            app.save_cache()

    def dl_html():
        raw_text = final_textbox.get("1.0", tk.END).rstrip('\n')
        if not raw_text:
            show_toast("⚠️ 預覽視窗沒有內容！", color="#f39c12")
            return

        if source_file:
            init_name = os.path.basename(source_file)
        else:
            title_val = app.doc_title.get().strip()
            num_val = app.doc_num.get().strip()
            if title_val and num_val:
                init_name = f"{title_val}_{num_val}.html"
            elif title_val:
                init_name = f"{title_val}.html"
            elif num_val:
                init_name = f"AA_Result_{num_val}.html"
            else:
                init_name = "AA_Result.html"

        file_path = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML files", "*.html")],
            initialfile=init_name,
            title="儲存 HTML 檔案",
        )
        if file_path:
            try:
                app.write_html_file(file_path, raw_text)
                show_toast("✅ 已儲存 HTML 檔案！", color="#28a745")
            except Exception as e:
                show_toast(f"❌ 無法儲存: {e}", color="#dc3545", duration=5000)

    ctk.CTkButton(grp3, text="底色", command=choose_bg_color, fg_color="#6c757d", hover_color="#5a6268", font=app.ui_small_font, width=45).pack(side="left", padx=2)
    ctk.CTkButton(grp3, text="文字色", command=choose_fg_color, fg_color="#17a2b8", hover_color="#138496", font=app.ui_small_font, width=45).pack(side="left", padx=2)
    ctk.CTkButton(grp3, text="💾 儲存", command=dl_html, fg_color="#28a745", hover_color="#218838", font=app.ui_small_font, width=60).pack(side="left", padx=5)
    _close_text = "↩ 返回" if use_tab else "✖ 關閉"
    ctk.CTkButton(grp3, text=_close_text, command=close_action, fg_color="#dc3545", hover_color="#c82333", font=app.ui_small_font, width=60).pack(side="left", padx=5)

    # ════════════════════════════════════════════════════════════
    #  搜尋列
    # ════════════════════════════════════════════════════════════
    search_frame = ctk.CTkFrame(container, fg_color="transparent")
    search_var = tk.StringVar()
    search_entry = ctk.CTkEntry(search_frame, textvariable=search_var, placeholder_text="搜尋...", font=app.ui_small_font, width=200)
    search_entry.pack(side="left", padx=5)

    def find_next(event=None):
        query = search_var.get()
        if not query:
            return

        start_pos = final_textbox._textbox.index(tk.INSERT)
        try:
            sel_first = final_textbox._textbox.index(tk.SEL_FIRST)
            sel_last = final_textbox._textbox.index(tk.SEL_LAST)
            if final_textbox._textbox.get(sel_first, sel_last).lower() == query.lower():
                start_pos = sel_last
        except tk.TclError:
            pass

        pos = final_textbox._textbox.search(query, start_pos, nocase=True, stopindex=tk.END)
        if not pos:
            pos = final_textbox._textbox.search(query, "1.0", nocase=True, stopindex=start_pos)

        if pos:
            end_pos = f"{pos}+{len(query)}c"
            final_textbox._textbox.tag_remove(tk.SEL, "1.0", tk.END)
            final_textbox._textbox.tag_add(tk.SEL, pos, end_pos)
            final_textbox._textbox.mark_set(tk.INSERT, end_pos)
            final_textbox._textbox.see(pos)
        else:
            show_toast("🔍 找不到符合的文字。", color="#17a2b8")

    search_entry.bind("<Return>", find_next)
    ctk.CTkButton(search_frame, text="下一個", command=find_next, font=app.ui_small_font, width=60).pack(side="left", padx=5)

    def search_dice(event=None):
        search_var.set("1D10:10")
        if not search_frame.winfo_ismapped():
            search_frame.pack(fill="x", padx=10, pady=(0, 5), before=final_textbox)
        find_next()

    ctk.CTkButton(search_frame, text="🎲 1D10:10", command=search_dice, fg_color="#f39c12", hover_color="#d68910", font=app.ui_small_font, width=80).pack(side="left", padx=5)

    def toggle_search(event=None):
        if search_frame.winfo_ismapped():
            search_frame.pack_forget()
        else:
            search_frame.pack(fill="x", padx=10, pady=(0, 5), before=final_textbox)
            search_entry.focus_set()
        return "break"

    def save_shortcut(event=None):
        dl_html()
        return "break"

    container.bind("<Control-f>", toggle_search)
    container.bind("<Control-F>", toggle_search)
    container.bind("<Control-s>", save_shortcut)
    container.bind("<Control-S>", save_shortcut)
    final_textbox._textbox.bind("<Control-f>", toggle_search)
    final_textbox._textbox.bind("<Control-F>", toggle_search)
    final_textbox._textbox.bind("<Control-s>", save_shortcut)
    final_textbox._textbox.bind("<Control-S>", save_shortcut)

    # ════════════════════════════════════════════════════════════
    #  填入文本
    # ════════════════════════════════════════════════════════════
    final_textbox.pack(fill="both", expand=True, padx=10, pady=10)
    final_textbox.insert("1.0", text)

    if use_tab:
        app.edit_tab_textbox = final_textbox
        app.bind("<Control-f>", toggle_search)
        app.bind("<Control-F>", toggle_search)
        app.bind("<Control-s>", save_shortcut)
        app.bind("<Control-S>", save_shortcut)
        app.bind("<Control-q>", handle_smart_action_event)
        app.bind("<Control-Q>", handle_smart_action_event)
        app.bind("<Escape>", lambda e: close_action())
        app._edit_tab_unbind = lambda: [
            app.unbind("<Control-f>"), app.unbind("<Control-F>"),
            app.unbind("<Control-s>"), app.unbind("<Control-S>"),
            app.unbind("<Control-q>"), app.unbind("<Control-Q>"),
            app.unbind("<Escape>"),
        ]
        app.switch_mode("edit")

    if scroll_to_line is not None:
        def _scroll():
            target = f"{scroll_to_line}.0"
            final_textbox._textbox.see(target)
            final_textbox._textbox.mark_set(tk.INSERT, target)
            line_end = f"{scroll_to_line}.end"
            final_textbox._textbox.tag_add(tk.SEL, target, line_end)
        (modal or app).after(100, _scroll)
