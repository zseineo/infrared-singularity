import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import json
import re
import math
import os
import html
import subprocess
import sys
import tempfile
import threading

from aa_tool.constants import (
    DEFAULT_BASE_REGEX, DEFAULT_INVALID_REGEX, DEFAULT_SYMBOL_REGEX,
    DEFAULT_BG_COLOR, DEFAULT_FG_COLOR, BORDER_CHARS,
)
from aa_tool.html_io import read_html_pre_content, write_html_file
from aa_tool.settings_manager import SettingsManager, AppSettings, AppCache
from aa_tool.text_extraction import (
    extract_text as _extract_text,
    format_extraction_output,
    analyze_extraction as _analyze_extraction,
    validate_ai_text as _validate_ai_text,
    check_chapter_number as _check_chapter_number,
    extract_work_title as _extract_work_title,
)
from aa_tool.translation_engine import (
    parse_glossary,
    apply_translation as _apply_translation,
)
from aa_tool.url_fetcher import fetch_url as _fetch_url, parse_page_html as _parse_page_html
from aa_tool.ui_result_modal import show_result_modal as _show_result_modal

from aa_dedup_tool import AADedupTool

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class AATranslationTool(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("AA 漫畫翻譯輔助工具")
        self.geometry("1400x900")

        self.aa_font = ctk.CTkFont(family="Meiryo", size=14)
        self.result_font = ctk.CTkFont(family="MS PGothic", size=16)
        self.ui_font = ctk.CTkFont(family="Microsoft JhengHei", size=14, weight="bold")
        self.ui_small_font = ctk.CTkFont(family="Microsoft JhengHei", size=12)
        
        self.bg_color = DEFAULT_BG_COLOR
        self.fg_color = DEFAULT_FG_COLOR
        self.preview_text_cache = ""

        self.settings_mgr = SettingsManager(os.path.dirname(os.path.abspath(__file__)))

        # 記錄上次儲存至 AA_Settings.json 時的行數（用於關閉時比對）
        self._saved_glossary_lines = 0
        self._saved_glossary_temp_lines = 0
        self._saved_filter_lines = 0

        self.setup_ui()
        self.load_cache()  # 啟動時先讀取暫存檔 (保底)
        self.load_settings_at_startup()  # 從 AA_Settings.json 讀取設定檔並覆蓋 (優先)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    @staticmethod
    def _count_nonempty_lines(text: str) -> int:
        return sum(1 for line in text.strip().splitlines() if line.strip())

    def on_closing(self):
        self.save_cache()
        # 比對目前 UI 內容的行數與上次儲存至設定檔的行數
        cur_glossary_lines = self._count_nonempty_lines(self.glossary_text.get("1.0", tk.END))
        cur_glossary_temp_lines = self._count_nonempty_lines(self.glossary_text_temp.get("1.0", tk.END))
        cur_filter_lines = self._count_nonempty_lines(self.filter_text.get("1.0", tk.END))

        parts = []
        if cur_glossary_lines > self._saved_glossary_lines:
            parts.append(f"術語表（目前 {cur_glossary_lines} 行，已儲存 {self._saved_glossary_lines} 行）")
        if cur_glossary_temp_lines > self._saved_glossary_temp_lines:
            parts.append(f"臨時術語表（目前 {cur_glossary_temp_lines} 行，已儲存 {self._saved_glossary_temp_lines} 行）")
        if cur_filter_lines > self._saved_filter_lines:
            parts.append(f"自訂過濾規則（目前 {cur_filter_lines} 行，已儲存 {self._saved_filter_lines} 行）")

        if parts:
            detail = "、".join(parts)
            answer = messagebox.askyesno(
                "儲存設定？",
                f"以下項目有未儲存的新增內容：\n{detail}\n\n是否在關閉前儲存至 AA_Settings.json？",
            )
            if answer:
                self.export_settings()

        self.destroy()

    def show_toast(self, message, color="#28a745", duration=3000):
        """在主視窗右上角顯示浮動提示訊息。"""
        toast = ctk.CTkFrame(self, fg_color=color, corner_radius=8)
        toast.place(relx=1.0, rely=0.0, anchor="ne", x=-20, y=55)
        lbl = ctk.CTkLabel(toast, text=message, text_color="white", font=ctk.CTkFont(family="Microsoft JhengHei", size=14, weight="bold"))
        lbl.pack(padx=20, pady=10)
        self.after(duration, toast.destroy)

    def show_confirm_toast(self, message, on_yes, color="#17a2b8", duration=8000):
        """在主視窗右上角顯示帶有「是/否」按鈕的確認浮動視窗。"""
        toast = ctk.CTkFrame(self, fg_color=color, corner_radius=8)
        toast.place(relx=1.0, rely=0.0, anchor="ne", x=-20, y=55)
        lbl = ctk.CTkLabel(toast, text=message, text_color="white", font=ctk.CTkFont(family="Microsoft JhengHei", size=14, weight="bold"), wraplength=350)
        lbl.pack(padx=20, pady=(10, 5))
        btn_frame = ctk.CTkFrame(toast, fg_color="transparent")
        btn_frame.pack(padx=20, pady=(0, 10))
        def yes_action():
            toast.destroy()
            on_yes()
        ctk.CTkButton(btn_frame, text="是", width=60, fg_color="#28a745", hover_color="#218838", command=yes_action).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="否", width=60, fg_color="#dc3545", hover_color="#c82333", command=toast.destroy).pack(side="left", padx=5)
        self.after(duration, lambda: toast.destroy() if toast.winfo_exists() else None)

    def setup_ui(self):
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)
        
        # 1. Top toolbar
        self.toolbar = ctk.CTkFrame(self)
        toolbar = self.toolbar
        toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        
        lbl = ctk.CTkLabel(toolbar, text="AA 漫畫翻譯輔助工具", font=ctk.CTkFont(size=20, weight="bold"))
        lbl.pack(side="left", padx=10, pady=10)

        # Mode switch buttons
        mode_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        mode_frame.pack(side="left", padx=10)
        self.btn_mode_translate = ctk.CTkButton(mode_frame, text="翻譯模式", width=80, height=28, font=self.ui_small_font, fg_color="#ff9800", hover_color="#e68a00", command=lambda: self.switch_mode("translate"))
        self.btn_mode_translate.pack(side="left", padx=2)
        self.btn_batch_qt = ctk.CTkButton(mode_frame, text="批次搜尋(Qt)", width=95, height=28, font=self.ui_small_font, fg_color="#6f42c1", hover_color="#5a3299", command=self.open_batch_search_qt)
        self.btn_batch_qt.pack(side="left", padx=2)
        
        btn_import = ctk.CTkButton(toolbar, text="📥 讀取設定", command=self.import_settings, fg_color="#17a2b8", hover_color="#138496", font=self.ui_font)
        btn_import.pack(side="right", padx=5)
        
        btn_export = ctk.CTkButton(toolbar, text="📤 儲存設定", command=self.export_settings, fg_color="#28a745", hover_color="#218838", font=self.ui_font)
        btn_export.pack(side="right", padx=5)

        def open_dedup_tool():
            AADedupTool(self)
            
        btn_dedup = ctk.CTkButton(toolbar, text="文字處理工具", command=open_dedup_tool, fg_color="#17a2b8", text_color="white", hover_color="#138496", font=self.ui_font)
        btn_dedup.pack(side="right", padx=5)

        btn_debug = ctk.CTkButton(toolbar, text="🔧提取Debug", command=self.analyze_extraction, fg_color="#6c757d", hover_color="#5a6268", font=self.ui_font)
        btn_debug.pack(side="right", padx=5)

        # 2. Main content area
        self.main_frame = ctk.CTkFrame(self)
        main_frame = self.main_frame
        main_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        
        main_frame.grid_rowconfigure(0, weight=4) # Top section height
        main_frame.grid_rowconfigure(2, weight=3) # Bottom section height
        main_frame.grid_columnconfigure(0, weight=6) # Left side width
        main_frame.grid_columnconfigure(1, weight=4) # Right side width
        
        # === Top Segment ===
        # Left: Source Text
        src_frame = ctk.CTkFrame(main_frame)
        src_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        src_top = ctk.CTkFrame(src_frame, fg_color="transparent")
        src_top.pack(fill="x", padx=5, pady=2)
        ctk.CTkLabel(src_top, text="1. 原始文本 (貼上來源):", font=self.ui_font).pack(side="left")
        ctk.CTkButton(src_top, text="🌐 網址讀取", command=self.open_url_fetch_dialog, fg_color="#6f42c1", hover_color="#5a32a3", text_color="white", font=self.ui_small_font, width=90, height=26).pack(side="left", padx=(8, 2))
        self.btn_next_chapter = ctk.CTkButton(src_top, text="下一話 ▶", command=self.fetch_next_chapter, fg_color="#0d6efd", hover_color="#0b5ed7", text_color="white", font=self.ui_small_font, width=75, height=26)
        self.btn_next_chapter.pack(side="left", padx=2)
        ctk.CTkButton(src_top, text="📋 複製網址", command=self.copy_current_url, fg_color="#6c757d", hover_color="#5a6268", text_color="white", font=self.ui_small_font, width=85, height=26).pack(side="left", padx=2)

        num_frame = ctk.CTkFrame(src_top, fg_color="transparent")
        num_frame.pack(side="right")
        
        ctk.CTkButton(num_frame, text="+", width=25, height=24, command=self.inc_num, font=self.ui_small_font).pack(side="right", padx=(2, 0))
        ctk.CTkButton(num_frame, text="-", width=25, height=24, command=self.dec_num, font=self.ui_small_font).pack(side="right", padx=(2, 2))
        
        self.doc_num = ctk.CTkEntry(num_frame, width=40, font=self.ui_small_font, justify="center")
        self.doc_num.pack(side="right")
        self.doc_num.insert(0, "1")
        self.doc_num.bind("<KeyRelease>", lambda e: self.schedule_save())

        self.doc_title = ctk.CTkEntry(src_top, placeholder_text="輸入標題 (選填)", font=self.ui_small_font, width=150)
        self.doc_title.pack(side="right", padx=10)
        self.doc_title.bind("<KeyRelease>", lambda e: self.schedule_save())

        self.source_text = ctk.CTkTextbox(src_frame, font=self.aa_font, wrap="none", undo=True)
        self.source_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.source_text.bind("<KeyRelease>", lambda e: self.schedule_save())
        self.source_text.bind("<<Paste>>", self.on_text_paste)
        self.source_text.bind("<Control-v>", self.on_text_paste)
        self.source_text.bind("<Control-V>", self.on_text_paste)
        
        # Right: Filters and Glossary
        right_frame = ctk.CTkFrame(main_frame)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        right_frame.grid_rowconfigure(1, weight=1)
        right_frame.grid_rowconfigure(3, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(right_frame, text="自訂過濾規則 (每行一條正則):", font=self.ui_font).grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.filter_text = ctk.CTkTextbox(right_frame, font=self.aa_font, wrap="none", fg_color="#3c3836", undo=True)
        self.filter_text.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.filter_text.bind("<KeyRelease>", lambda e: self.schedule_save())

        # Glossary with tab switching (一般 / 臨時)
        glossary_header = ctk.CTkFrame(right_frame, fg_color="transparent")
        glossary_header.grid(row=2, column=0, sticky="ew", padx=5, pady=2)
        ctk.CTkLabel(glossary_header, text="術語表 (日文=中文):", font=self.ui_font).pack(side="left")

        self.glossary_tab_var = tk.StringVar(value="一般")
        glossary_container = ctk.CTkFrame(right_frame)
        glossary_container.grid(row=3, column=0, sticky="nsew", padx=5, pady=5)
        glossary_container.grid_rowconfigure(1, weight=1)
        glossary_container.grid_columnconfigure(0, weight=1)

        tab_frame = ctk.CTkFrame(glossary_container, fg_color="transparent")
        tab_frame.grid(row=0, column=0, sticky="ew")

        self.glossary_text = ctk.CTkTextbox(glossary_container, font=self.aa_font, wrap="none", fg_color="#2a3b4c", undo=True)
        self.glossary_text_temp = ctk.CTkTextbox(glossary_container, font=self.aa_font, wrap="none", fg_color="#3b2a2a", undo=True)
        self.glossary_text.bind("<KeyRelease>", lambda e: self.schedule_save())
        self.glossary_text_temp.bind("<KeyRelease>", lambda e: self.schedule_save())

        def switch_glossary_tab(tab_name):
            self.glossary_tab_var.set(tab_name)
            if tab_name == "一般":
                self.glossary_text_temp.grid_forget()
                self.glossary_text.grid(row=1, column=0, sticky="nsew")
                btn_tab_general.configure(fg_color="#2a3b4c")
                btn_tab_temp.configure(fg_color="#555555")
            else:
                self.glossary_text.grid_forget()
                self.glossary_text_temp.grid(row=1, column=0, sticky="nsew")
                btn_tab_general.configure(fg_color="#555555")
                btn_tab_temp.configure(fg_color="#3b2a2a")

        btn_tab_general = ctk.CTkButton(tab_frame, text="一般", width=50, height=22, font=self.ui_small_font, fg_color="#2a3b4c", hover_color="#3a4b5c", command=lambda: switch_glossary_tab("一般"))
        btn_tab_general.pack(side="left", padx=(0, 2))
        btn_tab_temp = ctk.CTkButton(tab_frame, text="臨時", width=50, height=22, font=self.ui_small_font, fg_color="#555555", hover_color="#4b2a2a", command=lambda: switch_glossary_tab("臨時"))
        btn_tab_temp.pack(side="left")

        # Show default tab
        self.glossary_text.grid(row=1, column=0, sticky="nsew")

        # === Middle Button ===
        extract_bar = ctk.CTkFrame(main_frame, fg_color="transparent")
        extract_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=5)
        
        extract_inner = ctk.CTkFrame(extract_bar, fg_color="transparent")
        extract_inner.pack(pady=5)
        
        btn_ext = ctk.CTkButton(extract_inner, text="⬇️提取日文⬇️", command=self.extract_text, fg_color="#007bff", width=250, font=self.ui_font)
        btn_ext.pack(side="left", padx=5)

        self.auto_copy_switch = ctk.CTkSwitch(extract_inner, text="自動複製", font=self.ui_small_font, width=40)
        self.auto_copy_switch.pack(side="left", padx=8)

        # === Bottom Segment ===
        # Left: Extracted Text
        ext_frame = ctk.CTkFrame(main_frame)
        ext_frame.grid(row=2, column=0, sticky="nsew", padx=5, pady=5)
        
        ext_top = ctk.CTkFrame(ext_frame, fg_color="transparent")
        ext_top.pack(fill="x")
        ctk.CTkLabel(ext_top, text="2. 提取結果:", font=self.ui_font).pack(side="left", padx=5)
        self.ext_count_label = ctk.CTkLabel(ext_top, text="", font=self.ui_font, text_color="#17a2b8")
        self.ext_count_label.pack(side="left", padx=5)
        
        ctk.CTkButton(ext_top, text="複製下半", command=lambda: self.copy_split('bottom'), width=70, height=24, font=self.ui_small_font).pack(side="right", padx=5)
        ctk.CTkButton(ext_top, text="複製上半", command=lambda: self.copy_split('top'), width=70, height=24, font=self.ui_small_font).pack(side="right", padx=5)
        ctk.CTkButton(ext_top, text="複製全部", command=lambda: self.copy_split('all'), width=70, height=24, font=self.ui_small_font).pack(side="right", padx=5)
        
        self.extracted_text = ctk.CTkTextbox(ext_frame, font=self.aa_font, wrap="none", undo=True)
        self.extracted_text.pack(fill="both", expand=True, padx=5, pady=5)

        # Right: AI Translated Result
        ai_frame = ctk.CTkFrame(main_frame)
        ai_frame.grid(row=2, column=1, sticky="nsew", padx=5, pady=5)
        
        ai_top = ctk.CTkFrame(ai_frame, fg_color="transparent")
        ai_top.pack(fill="x")
        ctk.CTkLabel(ai_top, text="3. 翻譯結果:", font=self.ui_font).pack(side="left", padx=5, pady=2)
        self.ai_warn_label = ctk.CTkLabel(ai_top, text="", font=self.ui_small_font, text_color="#ff4444")
        self.ai_warn_label.pack(side="left", padx=5)
        
        self.ai_text = ctk.CTkTextbox(ai_frame, font=self.aa_font, wrap="none", undo=True)
        self.ai_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.ai_text.bind("<<Paste>>", lambda e: self.after(50, self.validate_ai_text))

        # 3. Bottom Execution bar
        self.gen_bar = ctk.CTkFrame(self)
        gen_bar = self.gen_bar
        gen_bar.grid(row=3, column=0, sticky="ew", padx=10, pady=5)
        
        btn_apply = ctk.CTkButton(gen_bar, text="🚀 替換翻譯並編輯 🚀", command=self.apply_translation, fg_color="#ff9800", text_color="white", hover_color="#e68a00", font=self.ui_font, height=45)
        btn_apply.pack(side="left", fill="x", expand=True, padx=5)
        
        btn_openhtml = ctk.CTkButton(gen_bar, text="📂 打開已儲存的 HTML", command=self.import_html, fg_color="#6f42c1", text_color="white", hover_color="#5a32a3", font=self.ui_font, height=45, width=250)
        btn_openhtml.pack(side="right", padx=5)

        # Deduplication tool button moved to top toolbar

        def manual_load():
            self.load_cache(load_preview_text=True)
            self.show_toast("✅ 暫存讀取成功！")
            if getattr(self, 'preview_text_cache', ""):
                self.show_confirm_toast("偵測到您有未完成的預覽視窗暫存，請問要現在開啟該視窗嗎？", lambda: self.show_result_modal(self.preview_text_cache))

        btn_loadcache = ctk.CTkButton(gen_bar, text="📥 讀入暫存", command=manual_load, fg_color="#17a2b8", text_color="white", hover_color="#138496", font=self.ui_font, height=45, width=150)
        btn_loadcache.pack(side="right", padx=5)

        self._save_timer = None
        
        # Default Regexes (from constants)
        self.default_base_regex = DEFAULT_BASE_REGEX
        self.default_invalid_regex = DEFAULT_INVALID_REGEX
        self.default_symbol_regex = DEFAULT_SYMBOL_REGEX

        self.current_base_regex = self.default_base_regex
        self.current_invalid_regex = self.default_invalid_regex
        self.current_symbol_regex = self.default_symbol_regex

        # Setup edit tab UI (for inline editing mode)
        self.setup_edit_ui()

    def setup_edit_ui(self):
        """建立內嵌編輯模式的框架（預設隱藏，實驗性功能）。"""
        self.edit_frame = ctk.CTkFrame(self)
        self._current_mode = "translate"
        self._previous_mode = "translate"
        self.edit_tab_textbox = None
        self.batch_folder_var = tk.StringVar()

    def switch_mode(self, mode_name):
        """切換翻譯模式 / 內嵌編輯模式。"""
        self._previous_mode = getattr(self, '_current_mode', 'translate')
        self._current_mode = mode_name

        # 離開 edit 模式時，解除綁定在主視窗上的快捷鍵
        if self._previous_mode == 'edit' and mode_name != 'edit':
            if hasattr(self, '_edit_tab_unbind'):
                self._edit_tab_unbind()
                del self._edit_tab_unbind

        self.main_frame.grid_forget()
        self.gen_bar.grid_forget()
        self.edit_frame.grid_forget()

        self.btn_mode_translate.configure(fg_color="#555555")

        if mode_name == "translate":
            self.grid_rowconfigure(0, weight=0)
            self.toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
            self.main_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
            self.gen_bar.grid(row=3, column=0, sticky="ew", padx=10, pady=5)
            self.btn_mode_translate.configure(fg_color="#ff9800")
        elif mode_name == "edit":
            self.grid_rowconfigure(0, weight=1)
            self.toolbar.grid_forget()
            self.edit_frame.grid(row=0, column=0, rowspan=4, sticky="nsew", padx=0, pady=0)

    # ── PyQt6 批次搜尋（獨立 process） ──

    def open_batch_search_qt(self):
        """啟動獨立的 PyQt6 批次搜尋視窗，並開始 IPC 輪詢。"""
        cmd_file = os.path.join(tempfile.gettempdir(), "aa_batch_cmd.json")
        # 確保舊命令檔不存在
        if os.path.exists(cmd_file):
            os.remove(cmd_file)

        folder = self.batch_folder_var.get().strip()
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aa_batch_search_qt.py")

        args = [sys.executable, script, "--cmd-file", cmd_file]
        if folder and os.path.isdir(folder):
            args.extend(["--folder", folder])

        self._batch_qt_process = subprocess.Popen(args)
        self._batch_cmd_file = cmd_file
        self._poll_batch_commands()

    def _poll_batch_commands(self):
        """每 500ms 檢查 IPC 命令檔，收到開啟命令則開啟 result modal。"""
        # 若 subprocess 已結束，停止輪詢
        if hasattr(self, '_batch_qt_process') and self._batch_qt_process.poll() is not None:
            self._batch_qt_process = None
            return

        cmd_file = getattr(self, '_batch_cmd_file', '')
        if cmd_file and os.path.exists(cmd_file):
            try:
                with open(cmd_file, 'r', encoding='utf-8') as f:
                    cmd = json.load(f)
                os.remove(cmd_file)

                if cmd.get('action') == 'open':
                    file_path = cmd.get('file_path', '')
                    line = cmd.get('line')
                    # 將主視窗提到最上層
                    if cmd.get('raise'):
                        self.deiconify()
                        self.lift()
                        self.focus_force()
                    text = self.read_html_pre_content(file_path)
                    if text:
                        self.show_result_modal(text, source_file=file_path, scroll_to_line=line)
                    else:
                        self.show_toast("❌ 無法讀取檔案！", color="#dc3545")
            except (json.JSONDecodeError, OSError):
                pass

        self.after(500, self._poll_batch_commands)

    def read_html_pre_content(self, file_path):
        return read_html_pre_content(file_path)

    def write_html_file(self, file_path, text_content):
        write_html_file(file_path, text_content)

    def get_settings_file(self):
        return self.settings_mgr.get_settings_file()

    def load_settings_at_startup(self):
        """從 AA_Settings.json 讀取設定並套用至 UI。"""
        settings = self.settings_mgr.load_settings()
        if settings.filter_text:
            self.filter_text.delete("1.0", tk.END)
            self.filter_text.insert("1.0", settings.filter_text)
        if settings.glossary:
            self.glossary_text.delete("1.0", tk.END)
            self.glossary_text.insert("1.0", settings.glossary)
        if settings.glossary_temp:
            self.glossary_text_temp.delete("1.0", tk.END)
            self.glossary_text_temp.insert("1.0", settings.glossary_temp)
        self.current_base_regex = settings.base_regex
        self.current_invalid_regex = settings.invalid_regex
        self.current_symbol_regex = settings.symbol_regex
        # 記錄此次從設定檔載入的行數作為基準
        self._saved_glossary_lines = self._count_nonempty_lines(settings.glossary)
        self._saved_glossary_temp_lines = self._count_nonempty_lines(settings.glossary_temp)
        self._saved_filter_lines = self._count_nonempty_lines(settings.filter_text)

    def save_regex_to_settings(self):
        """將目前的正則表達式寫回 AA_Settings.json。"""
        self.settings_mgr.save_regex_to_settings(
            self.current_base_regex, self.current_invalid_regex, self.current_symbol_regex
        )

    def inc_num(self):
        try:
            val = int(self.doc_num.get() or "0")
            self.doc_num.delete(0, tk.END)
            self.doc_num.insert(0, str(val + 1))
            self.schedule_save()
        except ValueError:
            pass

    def dec_num(self):
        try:
            val = int(self.doc_num.get() or "0")
            if val > 0:
                self.doc_num.delete(0, tk.END)
                self.doc_num.insert(0, str(val - 1))
                self.schedule_save()
        except ValueError:
            pass

    def get_combined_glossary(self):
        """合併一般與臨時術語表的內容，回傳合併後的字串。"""
        g1 = self.glossary_text.get("1.0", tk.END).strip()
        g2 = self.glossary_text_temp.get("1.0", tk.END).strip()
        parts = [p for p in [g1, g2] if p]
        return '\n'.join(parts)

    def schedule_save(self):
        if self._save_timer is not None:
            self.after_cancel(self._save_timer)
        self._save_timer = self.after(500, self.save_cache)

    def get_cache_file(self):
        return self.settings_mgr.get_cache_file()

    def _gather_cache(self) -> AppCache:
        """從 UI widget 蒐集狀態至 AppCache dataclass。"""
        return AppCache(
            source_text=self.source_text.get("1.0", tk.END).rstrip('\n'),
            filter_text=self.filter_text.get("1.0", tk.END).rstrip('\n'),
            glossary_text=self.glossary_text.get("1.0", tk.END).rstrip('\n'),
            glossary_text_temp=self.glossary_text_temp.get("1.0", tk.END).rstrip('\n'),
            doc_title=self.doc_title.get(),
            doc_num=self.doc_num.get(),
            bg_color=getattr(self, 'bg_color', DEFAULT_BG_COLOR),
            fg_color=getattr(self, 'fg_color', DEFAULT_FG_COLOR),
            preview_text=getattr(self, 'preview_text_cache', ''),
            url_history=getattr(self, 'url_history', []),
            url_related_links=getattr(self, 'url_related_links', []),
            current_url=getattr(self, 'current_url', ''),
            auto_copy=bool(self.auto_copy_switch.get()) if hasattr(self, 'auto_copy_switch') else False,
            batch_folder=self.batch_folder_var.get() if hasattr(self, 'batch_folder_var') else '',
        )

    def _apply_cache(self, cache: AppCache, load_preview_text: bool = False):
        """將 AppCache 資料推送至 UI widget。"""
        if cache.source_text:
            self.source_text.insert("1.0", cache.source_text)
        if cache.filter_text:
            self.filter_text.delete("1.0", tk.END)
            self.filter_text.insert("1.0", cache.filter_text)
        if cache.glossary_text:
            self.glossary_text.delete("1.0", tk.END)
            self.glossary_text.insert("1.0", cache.glossary_text)
        if cache.glossary_text_temp:
            self.glossary_text_temp.delete("1.0", tk.END)
            self.glossary_text_temp.insert("1.0", cache.glossary_text_temp)
        if cache.doc_title:
            self.doc_title.delete(0, tk.END)
            self.doc_title.insert(0, cache.doc_title)
        if cache.doc_num:
            self.doc_num.delete(0, tk.END)
            self.doc_num.insert(0, cache.doc_num)
        if cache.bg_color:
            self.bg_color = cache.bg_color
        if cache.fg_color:
            self.fg_color = cache.fg_color
        if load_preview_text and cache.preview_text:
            self.preview_text_cache = cache.preview_text
        elif not load_preview_text:
            self.preview_text_cache = ""
        if cache.url_history:
            self.url_history = cache.url_history
        if cache.url_related_links:
            self.url_related_links = cache.url_related_links
        if cache.current_url:
            self.current_url = cache.current_url
        if cache.auto_copy:
            self.auto_copy_switch.select()
        if cache.batch_folder and hasattr(self, 'batch_folder_var'):
            self.batch_folder_var.set(cache.batch_folder)

    def save_cache(self):
        self.settings_mgr.save_cache(self._gather_cache())

    def load_cache(self, load_preview_text=False):
        cache = self.settings_mgr.load_cache()
        self._apply_cache(cache, load_preview_text)

    def open_url_fetch_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("🌐 網址讀取")
        dialog.geometry("700x600")
        dialog.transient(self)
        dialog.grab_set()
        dialog.focus_force()

        # --- URL history & related links (load from attribute) ---
        if not hasattr(self, 'url_history'):
            self.url_history = []
        if not hasattr(self, 'url_related_links'):
            self.url_related_links = []

        # --- Top: URL input + fetch button ---
        top_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        top_frame.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(top_frame, text="網址:", font=self.ui_small_font).pack(side="left")
        url_entry = ctk.CTkEntry(top_frame, font=self.ui_small_font, width=420)
        url_entry.pack(side="left", padx=5, fill="x", expand=True)

        status_label = ctk.CTkLabel(dialog, text="", font=self.ui_small_font, text_color="#888888")
        status_label.pack(fill="x", padx=15)

        # --- Middle: Related links navigation ---
        nav_frame = ctk.CTkFrame(dialog)
        nav_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(nav_frame, text="關聯記事:", font=self.ui_small_font).pack(anchor="w", padx=5, pady=2)

        nav_scroll = ctk.CTkScrollableFrame(nav_frame, height=160)
        nav_scroll.pack(fill="x", padx=5, pady=(0, 5))

        # --- Bottom: URL history ---
        hist_frame = ctk.CTkFrame(dialog)
        hist_frame.pack(fill="both", expand=True, padx=10, pady=5)

        hist_top = ctk.CTkFrame(hist_frame, fg_color="transparent")
        hist_top.pack(fill="x", padx=5, pady=2)
        ctk.CTkLabel(hist_top, text="讀取紀錄:", font=self.ui_small_font).pack(side="left")

        def clear_history():
            self.url_history.clear()
            self.schedule_save()
            refresh_history()

        ctk.CTkButton(hist_top, text="清除紀錄", width=70, height=22,
                       fg_color="#dc3545", hover_color="#c82333",
                       font=self.ui_small_font, command=clear_history).pack(side="right")

        hist_scroll = ctk.CTkScrollableFrame(hist_frame, height=150)
        hist_scroll.pack(fill="both", expand=True, padx=5, pady=(0, 5))

        def refresh_history():
            for w in hist_scroll.winfo_children():
                w.destroy()
            for entry in reversed(self.url_history):
                row = ctk.CTkFrame(hist_scroll, fg_color="transparent")
                row.pack(fill="x", pady=1)
                title_text = entry.get('title', entry['url'])
                if len(title_text) > 60:
                    title_text = title_text[:60] + "…"
                lbl = ctk.CTkLabel(row, text=title_text, font=self.ui_small_font,
                                   text_color="#6f42c1", cursor="hand2", anchor="w")
                lbl.pack(side="left", fill="x", expand=True, padx=5)
                _url = entry['url']
                lbl.bind("<Button-1>", lambda e, u=_url: (url_entry.delete(0, tk.END),
                                                           url_entry.insert(0, u)))
                ctk.CTkButton(row, text="讀取", width=45, height=20,
                              fg_color="#17a2b8", hover_color="#138496",
                              font=self.ui_small_font,
                              command=lambda u=_url: (url_entry.delete(0, tk.END),
                                                       url_entry.insert(0, u),
                                                       do_fetch())).pack(side="right", padx=2)

        def refresh_nav(links):
            for w in nav_scroll.winfo_children():
                w.destroy()
            if not links:
                ctk.CTkLabel(nav_scroll, text="（尚未讀取或無關聯記事）",
                             font=self.ui_small_font, text_color="#888888").pack(anchor="w", padx=5)
                return

            # Find current page index
            current_idx = -1
            for i, lk in enumerate(links):
                if lk.get('is_current'):
                    current_idx = i
                    break

            for i, lk in enumerate(links):
                row = ctk.CTkFrame(nav_scroll, fg_color="transparent")
                row.pack(fill="x", pady=1)

                # Indicator
                if lk.get('is_current'):
                    indicator = "▶ "
                    text_color = "#dc3545"
                else:
                    indicator = "　"
                    text_color = "#0d6efd"

                title = indicator + lk['title']
                if len(title) > 65:
                    title = title[:65] + "…"

                if lk.get('url'):
                    lbl = ctk.CTkLabel(row, text=title, font=self.ui_small_font,
                                       text_color=text_color, cursor="hand2", anchor="w")
                    lbl.pack(side="left", fill="x", expand=True, padx=2)
                    _url = lk['url']
                    lbl.bind("<Button-1>", lambda e, u=_url: (url_entry.delete(0, tk.END),
                                                               url_entry.insert(0, u),
                                                               do_fetch()))
                else:
                    # Current page (no link)
                    lbl = ctk.CTkLabel(row, text=title, font=self.ui_small_font,
                                       text_color=text_color, anchor="w")
                    lbl.pack(side="left", fill="x", expand=True, padx=2)

            # Prev / Next buttons
            btn_row = ctk.CTkFrame(nav_scroll, fg_color="transparent")
            btn_row.pack(fill="x", pady=(5, 0))

            if current_idx > 0:
                prev_lk = links[current_idx - 1]
                if prev_lk.get('url'):
                    ctk.CTkButton(btn_row, text="▲ 上一話", width=90, height=26,
                                  fg_color="#0d6efd", hover_color="#0b5ed7",
                                  font=self.ui_small_font,
                                  command=lambda u=prev_lk['url']: (
                                      url_entry.delete(0, tk.END),
                                      url_entry.insert(0, u),
                                      do_fetch())).pack(side="left", padx=5)

            if current_idx >= 0 and current_idx < len(links) - 1:
                next_lk = links[current_idx + 1]
                if next_lk.get('url'):
                    ctk.CTkButton(btn_row, text="▼ 下一話", width=90, height=26,
                                  fg_color="#0d6efd", hover_color="#0b5ed7",
                                  font=self.ui_small_font,
                                  command=lambda u=next_lk['url']: (
                                      url_entry.delete(0, tk.END),
                                      url_entry.insert(0, u),
                                      do_fetch())).pack(side="left", padx=5)

        def do_fetch():
            raw_url = url_entry.get().strip()
            if not raw_url:
                status_label.configure(text="⚠️ 請輸入網址！", text_color="#f39c12")
                return
            if not raw_url.startswith('http'):
                raw_url = 'https://' + raw_url
                url_entry.delete(0, tk.END)
                url_entry.insert(0, raw_url)

            status_label.configure(text="⏳ 讀取中…", text_color="#17a2b8")
            fetch_btn.configure(state="disabled")
            dialog.update_idletasks()

            def _fetch():
                try:
                    page_html = _fetch_url(raw_url)
                    text_content, nav_links, page_title = _parse_page_html(page_html, raw_url)

                    if text_content is None:
                        dialog.after(0, lambda: status_label.configure(
                            text="❌ 找不到 article 區塊！", text_color="#dc3545"))
                        dialog.after(0, lambda: fetch_btn.configure(state="normal"))
                        return

                    def _apply():
                        # Put text into source_text, with title as first line
                        self.source_text.delete("1.0", tk.END)
                        display_title = _extract_work_title(page_title) if page_title else ""
                        if display_title:
                            self.source_text.insert("1.0", display_title + "\n\n" + text_content)
                        else:
                            self.source_text.insert("1.0", text_content)
                        self.after(50, self.check_chapter_number)
                        self._update_work_title(display_title)

                        # Update related links (and persist)
                        self.url_related_links = nav_links
                        self.schedule_save()
                        refresh_nav(nav_links)

                        # Track current URL and add to history
                        self.current_url = raw_url
                        hist_entry = {'url': raw_url, 'title': page_title or raw_url}
                        self.url_history = [h for h in self.url_history if h['url'] != raw_url]
                        self.url_history.append(hist_entry)
                        # Keep last 50
                        if len(self.url_history) > 50:
                            self.url_history = self.url_history[-50:]
                        self.schedule_save()
                        refresh_history()

                        line_count = text_content.count('\n') + 1
                        self.show_toast(f"✅ 網址讀取成功！共 {line_count} 行")
                        fetch_btn.configure(state="normal")

                        # Auto-close dialog after successful fetch
                        dialog.after(300, lambda: dialog.destroy() if dialog.winfo_exists() else None)

                    dialog.after(0, _apply)

                except Exception as ex:
                    dialog.after(0, lambda: status_label.configure(
                        text=f"❌ 讀取失敗: {ex}", text_color="#dc3545"))
                    dialog.after(0, lambda: fetch_btn.configure(state="normal"))

            threading.Thread(target=_fetch, daemon=True).start()

        fetch_btn = ctk.CTkButton(top_frame, text="讀取", width=60, height=28,
                                   fg_color="#28a745", hover_color="#218838",
                                   font=self.ui_small_font, command=do_fetch)
        fetch_btn.pack(side="left", padx=5)

        # Also allow Enter key to trigger fetch
        url_entry.bind("<Return>", lambda e: do_fetch())

        # Initialize displays
        refresh_nav(self.url_related_links)
        refresh_history()

    def copy_current_url(self):
        url = getattr(self, 'current_url', '')
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.show_toast("✅ 已複製網址到剪貼簿")
        else:
            self.show_toast("⚠️ 尚未讀取過網址！", color="#f39c12")

    def fetch_next_chapter(self):
        """從關聯記事中找到下一話並直接讀取。"""
        links = getattr(self, 'url_related_links', [])
        if not links:
            self.show_toast("⚠️ 尚未讀取過網址，無關聯記事資料！", color="#f39c12")
            return

        # Find current page index
        current_idx = -1
        for i, lk in enumerate(links):
            if lk.get('is_current'):
                current_idx = i
                break

        if current_idx < 0:
            self.show_toast("⚠️ 找不到目前所在的話數！", color="#f39c12")
            return

        if current_idx >= len(links) - 1:
            self.show_toast("⚠️ 已經是最新一話了！", color="#f39c12")
            return

        next_lk = links[current_idx + 1]
        if not next_lk.get('url'):
            self.show_toast("⚠️ 下一話沒有連結！", color="#f39c12")
            return

        next_url = next_lk['url']
        self.show_toast(f"⏳ 正在讀取下一話…", color="#17a2b8", duration=5000)

        def _fetch_next():
            try:
                page_html = _fetch_url(next_url)
                text_content, nav_links, page_title = _parse_page_html(page_html, next_url)

                if text_content is None:
                    self.after(0, lambda: self.show_toast("❌ 找不到 article 區塊！", color="#dc3545"))
                    return

                def _apply():
                    self.source_text.delete("1.0", tk.END)
                    display_title = _extract_work_title(page_title) if page_title else ""
                    if display_title:
                        self.source_text.insert("1.0", display_title + "\n\n" + text_content)
                    else:
                        self.source_text.insert("1.0", text_content)
                    self.after(50, self.check_chapter_number)
                    self._update_work_title(display_title)

                    self.url_related_links = nav_links
                    self.current_url = next_url
                    hist_entry = {'url': next_url, 'title': page_title or next_url}
                    self.url_history = [h for h in self.url_history if h['url'] != next_url]
                    self.url_history.append(hist_entry)
                    if len(self.url_history) > 50:
                        self.url_history = self.url_history[-50:]
                    self.schedule_save()

                    line_count = text_content.count('\n') + 1
                    self.show_toast(f"✅ 網址讀取成功！共 {line_count} 行")

                self.after(0, _apply)

            except Exception as ex:
                self.after(0, lambda: self.show_toast(f"❌ 讀取失敗: {ex}", color="#dc3545"))

        threading.Thread(target=_fetch_next, daemon=True).start()

    def export_settings(self):
        self.save_cache()
        settings = AppSettings(
            filter_text=self.filter_text.get("1.0", tk.END).strip(),
            glossary=self.glossary_text.get("1.0", tk.END).strip(),
            glossary_temp=self.glossary_text_temp.get("1.0", tk.END).strip(),
            base_regex=self.current_base_regex,
            invalid_regex=self.current_invalid_regex,
            symbol_regex=self.current_symbol_regex,
        )
        try:
            self.settings_mgr.save_settings(settings)
            # 更新已儲存行數基準
            self._saved_glossary_lines = self._count_nonempty_lines(settings.glossary)
            self._saved_glossary_temp_lines = self._count_nonempty_lines(settings.glossary_temp)
            self._saved_filter_lines = self._count_nonempty_lines(settings.filter_text)
            self.show_toast("✅ 設定儲存成功！")
        except Exception as e:
            self.show_toast(f"❌ 設定儲存失敗: {e}", color="#dc3545")

    def import_settings(self):
        if not os.path.exists(self.get_settings_file()):
            self.show_toast("⚠️ 找不到設定檔 AA_Settings.json！", color="#f39c12")
            return
        try:
            settings = self.settings_mgr.load_settings()
            self.filter_text.delete("1.0", tk.END)
            if settings.filter_text:
                self.filter_text.insert("1.0", settings.filter_text)
            self.glossary_text.delete("1.0", tk.END)
            if settings.glossary:
                self.glossary_text.insert("1.0", settings.glossary)
            self.glossary_text_temp.delete("1.0", tk.END)
            if settings.glossary_temp:
                self.glossary_text_temp.insert("1.0", settings.glossary_temp)
            self.current_base_regex = settings.base_regex
            self.current_invalid_regex = settings.invalid_regex
            self.current_symbol_regex = settings.symbol_regex
            self.save_regex_to_settings()
            self.save_cache()
            self.show_toast("✅ 設定已成功讀取！")
        except Exception as e:
            self.show_toast("❌ 讀取失敗，請確認檔案格式是否正確。", color="#dc3545")

    def analyze_extraction(self):
        try:
            selected_text = self.source_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            self.show_toast("⚠️ 請先在上方『原始文本』區塊中反白選取要分析的一段文字！", color="#f39c12")
            return
            
        if not selected_text.strip():
            self.show_toast("⚠️ 選取的文字為空！", color="#f39c12")
            return
            
        self.show_analyzer_modal(selected_text)
        
    def show_analyzer_modal(self, text):
        modal = ctk.CTkToplevel(self)
        modal.title("🔧 提取分析 (Debug)")
        modal.geometry("800x600")
        modal.transient(self)
        modal.grab_set()

        textbox = ctk.CTkTextbox(modal, font=self.aa_font, wrap="word")
        textbox.pack(fill="both", expand=True, padx=10, pady=10)

        report = _analyze_extraction(
            text,
            self.current_base_regex, self.current_invalid_regex, self.current_symbol_regex,
            self.filter_text.get("1.0", tk.END).strip(),
        )
        textbox.insert("1.0", report)
        textbox.configure(state="disabled")

        btn_close = ctk.CTkButton(modal, text="關閉視窗", command=modal.destroy, font=self.ui_font, fg_color="#dc3545", hover_color="#c82333")
        btn_close.pack(pady=10)

    def extract_text(self):
        source = self.source_text.get("1.0", tk.END)
        if not source.strip():
            self.show_toast("⚠️ 請先貼上原始文本！", color="#f39c12")
            return

        self.save_cache()
        extracted_set = _extract_text(
            source,
            self.current_base_regex, self.current_invalid_regex, self.current_symbol_regex,
            self.filter_text.get("1.0", tk.END).strip(),
        )
        output = format_extraction_output(extracted_set)

        self.extracted_text.delete("1.0", tk.END)
        self.extracted_text.insert("1.0", output)
        self.ext_count_label.configure(text=f"(共提取 {len(extracted_set)} 行)")

        if self.auto_copy_switch.get():
            self.clipboard_clear()
            self.clipboard_append(output.strip())
            self.show_toast(f"✅ 已提取 {len(extracted_set)} 行並複製到剪貼簿")

    def copy_split(self, half):
        ext_text = self.extracted_text.get("1.0", tk.END).strip()
        if not ext_text:
            return
        lines = [l for l in ext_text.split('\n') if l.strip()]
        if not lines:
            return
        
        if half == 'all':
            text_to_copy = "\n".join(lines)
        else:
            split_idx = int(math.ceil(len(lines) / 2))
            if half == 'top':
                copy_lines = lines[:split_idx]
            else:
                copy_lines = lines[split_idx:]
            text_to_copy = "\n".join(copy_lines)
        self.clipboard_clear()
        self.clipboard_append(text_to_copy)

    def on_text_paste(self, event=None):
        self.after(50, self.check_chapter_number)

    def check_chapter_number(self):
        text = self.source_text.get("1.0", "5.0")
        result = _check_chapter_number(text)
        if result is not None:
            self.doc_num.delete(0, tk.END)
            self.doc_num.insert(0, result)

    def _update_work_title(self, work_title: str = ""):
        """更新主視窗標題列，顯示目前作品名稱。"""
        base = "AA 漫畫翻譯輔助工具"
        if work_title:
            self.title(f"{base} — {work_title}")
        else:
            self.title(base)

    def validate_ai_text(self):
        """貼上 AI 翻譯後，自動檢查 ID 格式與行數是否正確。"""
        ai_content = self.ai_text.get("1.0", tk.END).strip()
        if not ai_content:
            self.ai_warn_label.configure(text="")
            return
        warnings = _validate_ai_text(ai_content)
        if warnings:
            self.ai_warn_label.configure(text="  ".join(warnings), text_color="#ff4444")
        else:
            self.ai_warn_label.configure(text="✅ 格式正確", text_color="#28a745")
            self.after(3000, lambda: self.ai_warn_label.configure(text="", text_color="#ff4444"))

    def apply_translation(self):
        source = self.source_text.get("1.0", tk.END)
        extracted = self.extracted_text.get("1.0", tk.END)
        translated = self.ai_text.get("1.0", tk.END)

        if not source.strip() or not extracted.strip() or not translated.strip():
            self.show_toast("⚠️ 請確保原始文本、提取結果和翻譯結果都有內容！", color="#f39c12")
            return

        self.save_cache()
        glossary = parse_glossary(self.get_combined_glossary())
        result = _apply_translation(source, extracted, translated, glossary)
        self.show_result_modal(result)

    def show_result_modal(self, text, source_file="", scroll_to_line=None):
        _show_result_modal(self, text, source_file=source_file, scroll_to_line=scroll_to_line)

    def import_html(self):
        file_path = filedialog.askopenfilename(
            filetypes=[("HTML files", "*.html"), ("All files", "*.*")],
            title="選取已儲存的 HTML 檔案"
        )
        if file_path:
            try:
                extracted = self.read_html_pre_content(file_path)
                if extracted is None:
                    self.show_toast("⚠️ 無法找到標準的 <pre> 標籤，讀取可能不完整。", color="#f39c12")
                    with open(file_path, 'r', encoding='utf-8') as f:
                        extracted = html.unescape(f.read())
                self.show_result_modal(extracted, source_file=file_path)
            except Exception as e:
                self.show_toast(f"❌ 讀取 HTML 檔案失敗！{e}", color="#dc3545")

if __name__ == "__main__":
    app = AATranslationTool()
    app.mainloop()
