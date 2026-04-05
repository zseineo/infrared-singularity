import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, colorchooser, messagebox
import json
import re
import math
import os
import html
import urllib.request
import threading
import gzip

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
        
        self.bg_color = "#ffffff"
        self.fg_color = "#000000"
        self.preview_text_cache = ""
        
        self.setup_ui()
        self.load_cache()  # 啟動時先讀取暫存檔 (保底)
        self.load_settings_at_startup()  # 從 AA_Settings.json 讀取設定檔並覆蓋 (優先)
        
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
    def on_closing(self):
        self.save_cache()
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
        self.btn_mode_batch = ctk.CTkButton(mode_frame, text="批次搜尋", width=80, height=28, font=self.ui_small_font, fg_color="#555555", hover_color="#444444", command=lambda: self.switch_mode("batch"))
        self.btn_mode_batch.pack(side="left", padx=2)

        self.experimental_edit_tab = ctk.CTkSwitch(mode_frame, text="實驗:內嵌編輯", font=self.ui_small_font, width=40)
        self.experimental_edit_tab.pack(side="left", padx=8)
        
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
        
        # Default Regexes
        self.default_base_regex = r"([：＋a-zA-ZＡ-Ｚ０-９0-9ぁ-んァ-ヶ一-龠々〆〤【】（）「」！？、。…，．？！,.―ーッ%]{3,})"
        self.default_invalid_regex = r"^[―ノツ人乂彡ミﾘﾊｿヽ丶、,.亠厂イ从「二ィ八ヘ二三ミノ王丁二爻」淡上丕旦丑．逑斧丞圭歪秘炸冽洲竺今一垈劣迦才守於主釼恢刈淤寸心イ气逍アA-Za-z個斗]+$"
        self.default_symbol_regex = r"[ノツ人乂彡ミﾘﾊｿヽ、,\.亠厂イ从「二ィ八ヘ三王丁爻」淡上丕旦丑￣＿／＼\|┌└┐┘│─━┏┓┣┫┝╿╂┴┬┤├:]"
        
        self.current_base_regex = self.default_base_regex
        self.current_invalid_regex = self.default_invalid_regex
        self.current_symbol_regex = self.default_symbol_regex

        # Setup batch search UI (hidden by default)
        self.setup_batch_ui()
        # Setup edit tab UI (experimental, hidden by default)
        self.setup_edit_ui()

    def setup_batch_ui(self):
        """建立批次搜尋模式的 UI（預設隱藏）。"""
        self.batch_frame = ctk.CTkFrame(self)
        # Don't grid yet — switch_mode will show it

        # --- Top: folder selector ---
        folder_row = ctk.CTkFrame(self.batch_frame, fg_color="transparent")
        folder_row.pack(fill="x", padx=10, pady=(10, 5))
        ctk.CTkLabel(folder_row, text="資料夾:", font=self.ui_font).pack(side="left")
        self.batch_folder_var = tk.StringVar()
        self.batch_folder_entry = ctk.CTkEntry(folder_row, textvariable=self.batch_folder_var, font=self.ui_small_font, width=400)
        self.batch_folder_entry.pack(side="left", padx=5, fill="x", expand=True)
        ctk.CTkButton(folder_row, text="瀏覽…", width=70, font=self.ui_small_font, fg_color="#6c757d", hover_color="#5a6268",
                       command=self._batch_browse_folder).pack(side="left", padx=5)

        # --- Search / Replace row ---
        search_row = ctk.CTkFrame(self.batch_frame, fg_color="transparent")
        search_row.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(search_row, text="搜尋:", font=self.ui_font).pack(side="left")
        self.batch_search_var = tk.StringVar()
        ctk.CTkEntry(search_row, textvariable=self.batch_search_var, font=self.ui_small_font, width=250).pack(side="left", padx=5)

        self.batch_regex_switch = ctk.CTkSwitch(search_row, text="正則", font=self.ui_small_font, width=40)
        self.batch_regex_switch.pack(side="left", padx=5)

        ctk.CTkLabel(search_row, text="替換:", font=self.ui_font).pack(side="left", padx=(15, 0))
        self.batch_replace_var = tk.StringVar()
        ctk.CTkEntry(search_row, textvariable=self.batch_replace_var, font=self.ui_small_font, width=250).pack(side="left", padx=5)

        self.batch_search_btn = ctk.CTkButton(search_row, text="🔍 搜尋", width=80, font=self.ui_font, fg_color="#007bff", hover_color="#0069d9",
                       command=self.batch_search)
        self.batch_search_btn.pack(side="left", padx=5)
        ctk.CTkButton(search_row, text="全部替換", width=80, font=self.ui_font, fg_color="#dc3545", hover_color="#c82333",
                       command=self.batch_replace_all).pack(side="left", padx=5)

        # --- Status label ---
        self.batch_status_label = ctk.CTkLabel(self.batch_frame, text="", font=self.ui_small_font, text_color="#888888")
        self.batch_status_label.pack(fill="x", padx=15)

        # --- Results area ---
        # Header row
        header = ctk.CTkFrame(self.batch_frame, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(5, 0))
        ctk.CTkLabel(header, text="檔名", font=self.ui_small_font, width=120, anchor="w").pack(side="left", padx=5)
        ctk.CTkLabel(header, text="操作", font=self.ui_small_font, width=100, anchor="w").pack(side="left", padx=2)
        ctk.CTkLabel(header, text="搜尋結果", font=self.ui_small_font, anchor="w").pack(side="left", padx=5, fill="x", expand=True)

        self.batch_results_frame = ctk.CTkScrollableFrame(self.batch_frame)
        self.batch_results_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Store matches for replace operations
        self.batch_matches: list[dict] = []

    def setup_edit_ui(self):
        """建立內嵌編輯模式的框架（預設隱藏，實驗性功能）。"""
        self.edit_frame = ctk.CTkFrame(self)
        self._current_mode = "translate"
        self._previous_mode = "translate"
        self.edit_tab_textbox = None

    def switch_mode(self, mode_name):
        """切換翻譯模式 / 批次搜尋模式 / 內嵌編輯模式。"""
        self._previous_mode = getattr(self, '_current_mode', 'translate')
        self._current_mode = mode_name

        # 離開 edit 模式時，解除綁定在主視窗上的快捷鍵
        if getattr(self, '_current_mode', '') == 'edit' and mode_name != 'edit':
            if hasattr(self, '_edit_tab_unbind'):
                self._edit_tab_unbind()
                del self._edit_tab_unbind

        self.main_frame.grid_forget()
        self.gen_bar.grid_forget()
        self.batch_frame.grid_forget()
        self.edit_frame.grid_forget()

        self.btn_mode_translate.configure(fg_color="#555555")
        self.btn_mode_batch.configure(fg_color="#555555")

        if mode_name == "translate":
            self.grid_rowconfigure(0, weight=0)
            self.toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
            self.main_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
            self.gen_bar.grid(row=3, column=0, sticky="ew", padx=10, pady=5)
            self.btn_mode_translate.configure(fg_color="#ff9800")
        elif mode_name == "batch":
            self.grid_rowconfigure(0, weight=0)
            self.toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
            self.batch_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
            self.btn_mode_batch.configure(fg_color="#007bff")
        elif mode_name == "edit":
            self.grid_rowconfigure(0, weight=1)
            self.toolbar.grid_forget()
            self.edit_frame.grid(row=0, column=0, rowspan=4, sticky="nsew", padx=0, pady=0)

    def _batch_browse_folder(self):
        folder = filedialog.askdirectory(title="選取 HTML 檔案所在的資料夾")
        if folder:
            self.batch_folder_var.set(folder)

    def read_html_pre_content(self, file_path):
        """讀取 HTML 檔案，提取 <pre> 區塊內容並 unescape，回傳純文字。"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search(r'<pre>([\s\S]*?)</pre>', content, re.IGNORECASE)
        if match:
            return html.unescape(match.group(1))
        return None

    def write_html_file(self, file_path, text_content):
        """將文字內容包裝為 HTML 並寫入檔案（保留 span 標籤）。"""
        # Escape HTML but preserve <span> color tags
        parts = re.split(r'(<span style="color:[^"]*">|</span>)', text_content)
        escaped_parts = []
        for part in parts:
            if re.match(r'<span style="color:[^"]*">', part) or part == '</span>':
                escaped_parts.append(part)
            else:
                escaped_parts.append(html.escape(part))
        escaped_content = ''.join(escaped_parts)

        html_struct = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <title>AA_Translated</title>
    <style>
        body {{ background-color: #fff; color: #000; padding: 20px; }}
        pre {{
            font-family: 'MS PGothic', 'Meiryo', monospace;
            font-size: 16px;
            line-height: 1.2;
            white-space: pre;
            word-wrap: normal;
        }}
    </style>
</head>
<body>
<pre>{escaped_content}</pre>
</body>
</html>'''
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html_struct)

    def batch_search(self):
        """在指定資料夾中搜尋所有 HTML 的 <pre> 內容（背景執行緒 + 分批渲染）。"""
        folder = self.batch_folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            self.show_toast("⚠️ 請先選擇有效的資料夾！", color="#f39c12")
            return

        query = self.batch_search_var.get()
        if not query:
            self.show_toast("⚠️ 請輸入搜尋內容！", color="#f39c12")
            return

        use_regex = self.batch_regex_switch.get()
        if use_regex:
            try:
                pattern = re.compile(query)
            except re.error as e:
                self.show_toast(f"⚠️ 正則語法錯誤: {e}", color="#dc3545")
                return
        else:
            pattern = re.compile(re.escape(query))

        # Clear previous results and reset scroll to top
        for w in self.batch_results_frame.winfo_children():
            w.destroy()
        self.batch_matches.clear()
        try:
            self.batch_results_frame._parent_canvas.yview_moveto(0)
        except Exception:
            pass

        html_files = [f for f in os.listdir(folder) if f.lower().endswith('.html')]
        html_files.sort()
        file_count = len(html_files)

        # 禁用搜尋按鈕，顯示搜尋中狀態
        self.batch_search_btn.configure(state="disabled")
        self.batch_status_label.configure(text=f"🔍 搜尋中... (0 / {file_count} 個檔案)", text_color="#888888")

        def _search():
            """在背景執行緒中掃描檔案，不直接操作任何 UI。"""
            matches = []
            for i, fname in enumerate(html_files):
                fpath = os.path.join(folder, fname)
                text = self.read_html_pre_content(fpath)
                if text is None:
                    continue

                lines = text.split('\n')
                for line_idx, line in enumerate(lines):
                    for m in pattern.finditer(line):
                        match_start = m.start()
                        match_end = m.end()
                        matched_text = m.group(0)

                        ctx_start = max(0, match_start - 10)
                        ctx_end = min(len(line), match_end + 10)
                        before = line[ctx_start:match_start]
                        after = line[match_end:ctx_end]

                        stem = os.path.splitext(fname)[0]
                        short_name = stem if len(stem) <= 12 else "…" + stem[-10:]

                        matches.append({
                            'file_path': fpath,
                            'file_name': fname,
                            'line_idx': line_idx,
                            'match_start': match_start,
                            'match_end': match_end,
                            'matched_text': matched_text,
                            'ctx_before': ("…" + before) if ctx_start > 0 else before,
                            'ctx_after': (after + "…") if ctx_end < len(line) else after,
                            'short_name': short_name,
                        })
                        if len(matches) >= 500:
                            break
                    if len(matches) >= 500:
                        break
                if len(matches) >= 500:
                    break

                # 每 10 個檔案回報一次進度
                if (i + 1) % 10 == 0 or i + 1 == file_count:
                    progress_i = i + 1
                    self.after(0, lambda p=progress_i: self.batch_status_label.configure(
                        text=f"🔍 搜尋中... ({p} / {file_count} 個檔案)", text_color="#888888"))

            self.after(0, lambda: _search_done(matches))

        def _search_done(matches):
            """搜尋完成後在主執行緒中啟動分批渲染。"""
            self.batch_matches = matches
            self.batch_search_btn.configure(state="normal")
            total = len(matches)

            if total == 0:
                self.batch_status_label.configure(text="找不到符合結果", text_color="#f39c12")
                return

            capped = total >= 500
            status_text = f"找到 {total} 筆結果" + ("（已達上限 500 筆）" if capped else "") + f"，共掃描 {file_count} 個檔案"
            self.batch_status_label.configure(text=f"{status_text}，渲染中...", text_color="#888888")
            _render_batch(0, total, status_text)

        def _render_batch(start, total, status_text, batch_size=30):
            """分批建立 widget，每次建立 batch_size 筆後讓出事件循環。"""
            end = min(start + batch_size, total)
            for mi in self.batch_matches[start:end]:
                self._build_batch_result_row(mi)
            if end < total:
                self.batch_status_label.configure(
                    text=f"{status_text}，渲染中 {end}/{total}...", text_color="#888888")
                self.after(0, lambda: _render_batch(end, total, status_text, batch_size))
            else:
                self.batch_status_label.configure(
                    text=status_text, text_color="#28a745")

        threading.Thread(target=_search, daemon=True).start()

    def _build_batch_result_row(self, mi, insert_after=None):
        """建立一行搜尋結果 UI。"""
        row = ctk.CTkFrame(self.batch_results_frame, fg_color="transparent")
        if insert_after:
            row.pack(fill="x", pady=1, after=insert_after)
        else:
            row.pack(fill="x", pady=1)

        # Store reference so replace can find and update it
        mi['_row'] = row

        ctk.CTkLabel(row, text=mi['short_name'], font=self.ui_small_font, width=120, anchor="w",
                     text_color="#6f42c1").pack(side="left", padx=5)

        ctk.CTkButton(row, text="替換", width=45, height=22, font=self.ui_small_font,
                      fg_color="#dc3545", hover_color="#c82333",
                      command=lambda m=mi: self.replace_single_match(m)).pack(side="left", padx=2)
        ctk.CTkButton(row, text="開啟", width=45, height=22, font=self.ui_small_font,
                      fg_color="#007bff", hover_color="#0069d9",
                      command=lambda m=mi: self.open_file_at_match(m)).pack(side="left", padx=2)

        # Context: before (grey) + match (highlighted) + after (grey)
        ctx_frame = ctk.CTkFrame(row, fg_color="transparent")
        ctx_frame.pack(side="left", padx=5, fill="x", expand=True)
        ctk.CTkLabel(ctx_frame, text=mi['ctx_before'], font=self.ui_small_font, anchor="w",
                     text_color="#888888").pack(side="left")
        ctk.CTkLabel(ctx_frame, text=mi['matched_text'],
                     font=ctk.CTkFont(family="Microsoft JhengHei", size=12, weight="bold"), anchor="w",
                     text_color="#ff6b6b").pack(side="left")
        ctk.CTkLabel(ctx_frame, text=mi['ctx_after'], font=self.ui_small_font, anchor="w",
                     text_color="#888888").pack(side="left")

    def open_file_at_match(self, match_info):
        """開啟 HTML 檔案到編輯模式，並跳到搜尋結果所在行。"""
        text = self.read_html_pre_content(match_info['file_path'])
        if text is None:
            self.show_toast("❌ 無法讀取檔案！", color="#dc3545")
            return
        self.show_result_modal(text, source_file=match_info['file_path'], scroll_to_line=match_info['line_idx'] + 1)

    def replace_single_match(self, match_info):
        """替換單一搜尋結果，儲存檔案，並重新讀取顯示該行結果。"""
        replacement = self.batch_replace_var.get()
        fpath = match_info['file_path']

        text = self.read_html_pre_content(fpath)
        if text is None:
            self.show_toast("❌ 無法讀取檔案！", color="#dc3545")
            return

        lines = text.split('\n')
        li = match_info['line_idx']
        if li < len(lines):
            line = lines[li]
            lines[li] = line[:match_info['match_start']] + replacement + line[match_info['match_end']:]

        new_text = '\n'.join(lines)
        try:
            self.write_html_file(fpath, new_text)
        except Exception as e:
            self.show_toast(f"❌ 儲存失敗: {e}", color="#dc3545")
            return

        # Update the row to show the replaced result
        old_row = match_info.get('_row')
        self.batch_matches = [m for m in self.batch_matches if m is not match_info]

        if old_row:
            # Clear old row content and rebuild as a "replaced" display
            for w in old_row.winfo_children():
                w.destroy()

            ctk.CTkLabel(old_row, text=match_info['short_name'], font=self.ui_small_font, width=120, anchor="w",
                         text_color="#6f42c1").pack(side="left", padx=5)

            ctk.CTkLabel(old_row, text="✔ 已替換", font=self.ui_small_font,
                         text_color="#28a745").pack(side="left", padx=2)
            ctk.CTkButton(old_row, text="開啟", width=45, height=22, font=self.ui_small_font,
                          fg_color="#007bff", hover_color="#0069d9",
                          command=lambda m=match_info: self.open_file_at_match(m)).pack(side="left", padx=2)

            # Show: before + replaced text (green) + after
            ctx_frame = ctk.CTkFrame(old_row, fg_color="transparent")
            ctx_frame.pack(side="left", padx=5, fill="x", expand=True)
            ctk.CTkLabel(ctx_frame, text=match_info['ctx_before'], font=self.ui_small_font, anchor="w",
                         text_color="#888888").pack(side="left")
            ctk.CTkLabel(ctx_frame, text=replacement if replacement else "（刪除）",
                         font=ctk.CTkFont(family="Microsoft JhengHei", size=12, weight="bold"), anchor="w",
                         text_color="#28a745").pack(side="left")
            ctk.CTkLabel(ctx_frame, text=match_info['ctx_after'], font=self.ui_small_font, anchor="w",
                         text_color="#888888").pack(side="left")

        self.show_toast("✅ 已替換並儲存")

    def batch_replace_all(self):
        """替換所有搜尋結果。"""
        if not self.batch_matches:
            self.show_toast("⚠️ 沒有可替換的結果！", color="#f39c12")
            return

        replacement = self.batch_replace_var.get()

        # Group by file
        by_file: dict[str, list[dict]] = {}
        for mi in self.batch_matches:
            by_file.setdefault(mi['file_path'], []).append(mi)

        replaced_count = 0
        file_count = 0
        for fpath, matches in by_file.items():
            text = self.read_html_pre_content(fpath)
            if text is None:
                continue

            lines = text.split('\n')
            # Sort matches by line then by position descending (to replace from end to start)
            matches.sort(key=lambda m: (m['line_idx'], -m['match_start']))

            for mi in matches:
                li = mi['line_idx']
                if li < len(lines):
                    line = lines[li]
                    lines[li] = line[:mi['match_start']] + replacement + line[mi['match_end']:]
                    replaced_count += 1

            new_text = '\n'.join(lines)
            try:
                self.write_html_file(fpath, new_text)
                file_count += 1
            except Exception:
                pass

        # Update each row to show replaced result (same as single replace)
        for mi in self.batch_matches:
            old_row = mi.get('_row')
            if old_row:
                for w in old_row.winfo_children():
                    w.destroy()

                ctk.CTkLabel(old_row, text=mi['short_name'], font=self.ui_small_font, width=120, anchor="w",
                             text_color="#6f42c1").pack(side="left", padx=5)

                ctk.CTkLabel(old_row, text="✔ 已替換", font=self.ui_small_font,
                             text_color="#28a745").pack(side="left", padx=2)
                ctk.CTkButton(old_row, text="開啟", width=45, height=22, font=self.ui_small_font,
                              fg_color="#007bff", hover_color="#0069d9",
                              command=lambda m=mi: self.open_file_at_match(m)).pack(side="left", padx=2)

                ctx_frame = ctk.CTkFrame(old_row, fg_color="transparent")
                ctx_frame.pack(side="left", padx=5, fill="x", expand=True)
                ctk.CTkLabel(ctx_frame, text=mi['ctx_before'], font=self.ui_small_font, anchor="w",
                             text_color="#888888").pack(side="left")
                ctk.CTkLabel(ctx_frame, text=replacement if replacement else "（刪除）",
                             font=ctk.CTkFont(family="Microsoft JhengHei", size=12, weight="bold"), anchor="w",
                             text_color="#28a745").pack(side="left")
                ctk.CTkLabel(ctx_frame, text=mi['ctx_after'], font=self.ui_small_font, anchor="w",
                             text_color="#888888").pack(side="left")

        self.batch_matches.clear()
        self.batch_status_label.configure(text=f"✅ 已替換 {replaced_count} 筆，涉及 {file_count} 個檔案", text_color="#28a745")
        self.show_toast(f"✅ 全部替換完成！共 {replaced_count} 筆")

    def get_settings_file(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'AA_Settings.json')

    def load_settings_at_startup(self):
        """從 AA_Settings.json 讀取設定 (包含正則表達式與濾網/術語)，找不到檔案或欄位時保留現狀或使用預設值。"""
        settings_file = self.get_settings_file()
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 如果 settings 裡面有 filter 或 glossary，優先使用設定檔的！
                if 'filter' in data:
                    self.filter_text.delete("1.0", tk.END)
                    self.filter_text.insert("1.0", data['filter'])
                if 'glossary' in data:
                    self.glossary_text.delete("1.0", tk.END)
                    self.glossary_text.insert("1.0", data['glossary'])
                if 'glossary_temp' in data:
                    self.glossary_text_temp.delete("1.0", tk.END)
                    self.glossary_text_temp.insert("1.0", data['glossary_temp'])

                self.current_base_regex = data.get('base_regex', self.default_base_regex)
                self.current_invalid_regex = data.get('invalid_regex', self.default_invalid_regex)
                self.current_symbol_regex = data.get('symbol_regex', self.default_symbol_regex)
            except Exception as e:
                print("AA_Settings.json load failed:", e)

    def save_regex_to_settings(self):
        """將目前的正則表達式寫回 AA_Settings.json（保留其他欄位不動）。"""
        settings_file = str(self.get_settings_file())
        data: dict[str, str] = {}
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    data = dict(json.load(f))  # type: ignore
            except Exception:
                pass
        data['base_regex'] = self.current_base_regex
        data['invalid_regex'] = self.current_invalid_regex
        data['symbol_regex'] = self.current_symbol_regex
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("AA_Settings.json regex save failed:", e)

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
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aa_settings_cache.json')

    def save_cache(self):
        data = {
            'source_text': self.source_text.get("1.0", tk.END),
            'filter_text': self.filter_text.get("1.0", tk.END),
            'glossary_text': self.glossary_text.get("1.0", tk.END),
            'glossary_text_temp': self.glossary_text_temp.get("1.0", tk.END),
            'doc_title': self.doc_title.get(),
            'doc_num': self.doc_num.get(),
            'bg_color': getattr(self, 'bg_color', "#ffffff"),
            'fg_color': getattr(self, 'fg_color', "#000000"),
            'preview_text': getattr(self, 'preview_text_cache', ""),
            'url_history': getattr(self, 'url_history', []),
            'url_related_links': getattr(self, 'url_related_links', []),
            'current_url': getattr(self, 'current_url', ''),
            'auto_copy': self.auto_copy_switch.get() if hasattr(self, 'auto_copy_switch') else 0,
            'batch_folder': self.batch_folder_var.get() if hasattr(self, 'batch_folder_var') else '',
            'experimental_edit_tab': self.experimental_edit_tab.get() if hasattr(self, 'experimental_edit_tab') else 0
        }
        try:
            with open(self.get_cache_file(), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print("Cache save failed:", e)

    def load_cache(self, load_preview_text=False):
        cache_file = self.get_cache_file()
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data.get('source_text'):
                        self.source_text.insert("1.0", data['source_text'].rstrip('\n'))
                    if data.get('filter_text'):
                        self.filter_text.delete("1.0", tk.END)
                        self.filter_text.insert("1.0", data['filter_text'].rstrip('\n'))
                    if data.get('glossary_text'):
                        self.glossary_text.delete("1.0", tk.END)
                        self.glossary_text.insert("1.0", data['glossary_text'].rstrip('\n'))
                    if data.get('glossary_text_temp'):
                        self.glossary_text_temp.delete("1.0", tk.END)
                        self.glossary_text_temp.insert("1.0", data['glossary_text_temp'].rstrip('\n'))
                    if data.get('doc_title'):
                        self.doc_title.delete(0, tk.END)
                        self.doc_title.insert(0, data['doc_title'])
                    if data.get('doc_num'):
                        self.doc_num.delete(0, tk.END)
                        self.doc_num.insert(0, data['doc_num'])
                    if data.get('bg_color'):
                        self.bg_color = data['bg_color']
                    if data.get('fg_color'):
                        self.fg_color = data['fg_color']
                    # 只在有要求載入時，才去恢復預覽的 cache 並詢問
                    if load_preview_text and data.get('preview_text'):
                        self.preview_text_cache = data['preview_text']
                    elif not load_preview_text:
                        # Ensure we don't accidentally hold the old cache text if we didn't explicitly load it
                        self.preview_text_cache = ""
                        
                    if data.get('url_history'):
                        self.url_history = data['url_history']
                    if data.get('url_related_links'):
                        self.url_related_links = data['url_related_links']
                    if data.get('current_url'):
                        self.current_url = data['current_url']
                    if data.get('auto_copy'):
                        self.auto_copy_switch.select()
                    if data.get('batch_folder') and hasattr(self, 'batch_folder_var'):
                        self.batch_folder_var.set(data['batch_folder'])
                    if data.get('experimental_edit_tab') and hasattr(self, 'experimental_edit_tab'):
                        self.experimental_edit_tab.select()

                    # 正則表達式不從暫存檔讀取，改由 AA_Settings.json 管理
            except Exception as e:
                print("Cache load failed:", e)

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

        def parse_page(page_html, base_url):
            return self._parse_page_html(page_html, base_url)

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
                    req = urllib.request.Request(raw_url, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept-Encoding': 'gzip, deflate'
                    })
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        raw_bytes = resp.read()
                        if resp.headers.get('Content-Encoding') == 'gzip':
                            page_bytes = gzip.decompress(raw_bytes)
                        else:
                            page_bytes = raw_bytes
                    # Try utf-8 first, fallback to shift_jis/euc-jp
                    for enc in ['utf-8', 'cp932', 'euc-jp', 'shift_jis']:
                        try:
                            page_html = page_bytes.decode(enc)
                            break
                        except (UnicodeDecodeError, LookupError):
                            continue
                    else:
                        page_html = page_bytes.decode('utf-8', errors='replace')

                    text_content, nav_links, page_title = parse_page(page_html, raw_url)

                    if text_content is None:
                        dialog.after(0, lambda: status_label.configure(
                            text="❌ 找不到 article 區塊！", text_color="#dc3545"))
                        dialog.after(0, lambda: fetch_btn.configure(state="normal"))
                        return

                    def _apply():
                        # Put text into source_text, with title as first line
                        self.source_text.delete("1.0", tk.END)
                        if page_title:
                            self.source_text.insert("1.0", page_title + "\n\n" + text_content)
                        else:
                            self.source_text.insert("1.0", text_content)
                        self.after(50, self.check_chapter_number)

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
                req = urllib.request.Request(next_url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept-Encoding': 'gzip, deflate'
                })
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw_bytes = resp.read()
                    if resp.headers.get('Content-Encoding') == 'gzip':
                        page_bytes = gzip.decompress(raw_bytes)
                    else:
                        page_bytes = raw_bytes
                for enc in ['utf-8', 'cp932', 'euc-jp', 'shift_jis']:
                    try:
                        page_html = page_bytes.decode(enc)
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                else:
                    page_html = page_bytes.decode('utf-8', errors='replace')

                # Reuse the same parse logic
                text_content, nav_links, page_title = self._parse_page_html(page_html, next_url)

                if text_content is None:
                    self.after(0, lambda: self.show_toast("❌ 找不到 article 區塊！", color="#dc3545"))
                    return

                def _apply():
                    self.source_text.delete("1.0", tk.END)
                    if page_title:
                        self.source_text.insert("1.0", page_title + "\n\n" + text_content)
                    else:
                        self.source_text.insert("1.0", text_content)
                    self.after(50, self.check_chapter_number)

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

    def _parse_page_html(self, page_html, base_url):
        """Parse HTML and return (text_content, related_links, page_title). Shared by dialog and direct fetch."""
        from urllib.parse import urljoin

        m = re.search(r'<div\s+class="article">', page_html)
        if not m:
            return None, [], ""

        start = m.start()
        # End boundary: relate_dl (関連記事) or second div.article, whichever comes first
        m_relate = re.search(r'<dl\s+class="relate_dl">', page_html[start + 1:])
        m2 = re.search(r'<div\s+class="article">', page_html[start + 1:])
        candidates = []
        if m_relate:
            candidates.append(start + 1 + m_relate.start())
        if m2:
            candidates.append(start + 1 + m2.start())
        end = min(candidates) if candidates else len(page_html)
        article_html = page_html[start:end]

        posts = re.finditer(
            r'<dt(?:\s[^>]*)?>(.+?)</dt>\s*<dd(?:\s[^>]*)?>(.*?)(?=<dt|</dl>)',
            article_html, re.DOTALL)

        lines_out: list[str] = []
        for post in posts:
            dt_content = post.group(1)
            dt_text = re.sub(r'<[^>]+>', '', dt_content)
            dt_text = html.unescape(dt_text).strip()

            dd_content = post.group(2)
            dd_text = re.sub(r'<br\s*/?>', '\n', dd_content)
            dd_text = re.sub(r'<[^>]+>', '', dd_text)
            dd_text = html.unescape(dd_text)
            dd_lines = dd_text.split('\n')
            while dd_lines and not dd_lines[-1].strip():
                dd_lines.pop()
            while dd_lines and not dd_lines[0].strip():
                dd_lines.pop(0)
            if dd_lines:
                lines_out.append(dt_text + '\n' + '\n'.join(dd_lines))

        text_content = '\n\n'.join(lines_out)

        title_m = re.search(r'<title>([^<]+)</title>', page_html)
        page_title = html.unescape(title_m.group(1)).strip() if title_m else ""
        page_title = re.sub(r'^.*?まとめ\S*\s+', '', page_title)

        nav_links: list[dict] = []
        relate_m = re.search(r'<dl\s+class="relate_dl">(.*?)</dl>', page_html, re.DOTALL)
        if relate_m:
            relate_html = relate_m.group(1)
            # Parse all <li> in order of appearance to preserve correct position
            for li in re.finditer(
                r'<li\s+class="(relate_li(?:_nolink)?)"[^>]*>(.*?)</li>',
                relate_html, re.DOTALL):
                li_class = li.group(1)
                li_inner = li.group(2)
                if li_class == 'relate_li':
                    # Has link
                    a_m = re.search(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', li_inner)
                    if a_m:
                        href = urljoin(base_url, a_m.group(1))
                        title = html.unescape(a_m.group(2)).strip()
                        nav_links.append({'title': title, 'url': href, 'is_current': False})
                else:
                    # Current page (no link)
                    title = html.unescape(re.sub(r'<[^>]+>', '', li_inner)).strip()
                    nav_links.append({'title': title, 'url': None, 'is_current': True})
            nav_links.reverse()

        return text_content, nav_links, page_title

    def export_settings(self):
        self.save_cache()
        file_path = self.get_settings_file()
        data = {
            'filter': self.filter_text.get("1.0", tk.END).strip(),
            'glossary': self.glossary_text.get("1.0", tk.END).strip(),
            'glossary_temp': self.glossary_text_temp.get("1.0", tk.END).strip(),
            'base_regex': self.current_base_regex,
            'invalid_regex': self.current_invalid_regex,
            'symbol_regex': self.current_symbol_regex
        }
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.show_toast("✅ 設定儲存成功！")
        except Exception as e:
            self.show_toast(f"❌ 設定儲存失敗: {e}", color="#dc3545")

    def import_settings(self):
        file_path = self.get_settings_file()
        if not os.path.exists(file_path):
            self.show_toast("⚠️ 找不到設定檔 AA_Settings.json！", color="#f39c12")
            return
            
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.filter_text.delete("1.0", tk.END)
            if 'filter' in data:
                self.filter_text.insert("1.0", data['filter'])
            self.glossary_text.delete("1.0", tk.END)
            if 'glossary' in data:
                self.glossary_text.insert("1.0", data['glossary'])
            self.glossary_text_temp.delete("1.0", tk.END)
            if 'glossary_temp' in data:
                self.glossary_text_temp.insert("1.0", data['glossary_temp'])

            self.current_base_regex = data.get('base_regex', self.default_base_regex)
            self.current_invalid_regex = data.get('invalid_regex', self.default_invalid_regex)
            self.current_symbol_regex = data.get('symbol_regex', self.default_symbol_regex)
            
            self.save_regex_to_settings()  # 同步寫回 AA_Settings.json
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
        
        # Load regexes from UI inputs
        custom_regexes = []
        filter_str = self.filter_text.get("1.0", tk.END).strip()
        for line in filter_str.split('\n'):
            line = line.strip()
            if line:
                try:
                    custom_regexes.append(re.compile(line))
                except re.error:
                    pass
                    
        try:
            base_regex = re.compile(self.current_base_regex)
        except re.error:
            base_regex = re.compile(self.default_base_regex)
            
        try:
            invalid_regex = re.compile(self.current_invalid_regex)
        except re.error:
            invalid_regex = re.compile(self.default_invalid_regex)
            
        try:
            symbol_regex = re.compile(self.current_symbol_regex)
        except re.error:
            symbol_regex = re.compile(self.default_symbol_regex)
            
        # Analysis logic
        report = []
        lines = text.split('\n')
        for line_idx, line in enumerate(lines, 1):
            if not line.strip():
                continue
            report.append(f"--- 開始分析字串 (第 {line_idx} 行) ---")
            report.append(f"原始字串: '{line}'")
            
            chunks = re.split(r'[ 　]{2,}', line)
            report.append(f"[步驟 1] 藉由連續兩個以上空白進行分割，分割出 {len(chunks)} 個區塊:")
            for i, chunk in enumerate(chunks):
                report.append(f"  區塊 {i+1}: '{chunk}'")
                
            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    report.append(f"\n[區塊 {i+1} 分析] '{chunk}'")
                    report.append("  -> ❌ 剔除：區塊為空字串或純空白。")
                    continue
                    
                report.append(f"\n[區塊 {i+1} 分析] '{chunk}'")
                chunk_str = str(chunk)
                
                symbol_count = len(symbol_regex.findall(chunk_str))
                symbol_ratio = symbol_count / len(chunk_str) if len(chunk_str) > 0 else 0
                report.append(f"  [步驟 2] 判斷 AA 符號比例 (符號數: {symbol_count}, 總字元數: {len(chunk_str)}, 比例: {symbol_ratio:.2f})")
                
                if symbol_ratio > 0.5:
                    report.append("  -> ❌ 剔除：符號比例超過 50%，判定為 AA 圖案。")
                    continue
                    
                matches = base_regex.findall(chunk)
                report.append(f"  [步驟 3] 執行 Base Regex 匹配: 找到 {len(matches)} 個可能詞彙")
                if not matches:
                    report.append("  -> ❌ 剔除：無法匹配出任何文字。")
                    continue
                    
                for j, match_text in enumerate(matches):
                    report.append(f"\n  >> 對 [結果 {j+1}] '{match_text}' 進行進階檢驗:")
                    t = match_text.strip()
                    
                    if len(t) < 3:
                        report.append("    -> ❌ 剔除：去除前後空白後，長度小於 3 字元。")
                        continue
                    else:
                        report.append(f"    - 長度檢驗通過 (長度: {len(t)})")
                        
                    if invalid_regex.match(t):
                        report.append(f"    -> ❌ 剔除：全句符合無意義符號組合正則 (Invalid Regex)。")
                        continue
                    else:
                        report.append("    - 無意義符號組合檢驗通過")
                        
                    filtered_by_custom = False
                    for reg in custom_regexes:
                        if reg.search(t):
                            report.append(f"    -> ❌ 剔除：命中自訂濾網正則表達式 ({reg.pattern})。")
                            filtered_by_custom = True
                            break
                    if filtered_by_custom:
                        continue
                    else:
                        report.append("    - 自訂過濾清單檢驗通過")
                        
                    original_t = t
                    
                    # 若被邊框符號 │ 或 | 包起來，則只取邊框內的文字並清除多餘的空白與句點
                    match = re.search(r'[│\|](.*)[│\|]', t)
                    if match:
                        t = match.group(1).strip(' \t　.')
                        
                    # 移除開頭非平假名、片假名、漢字、英文、數字的部分
                    t = re.sub(r'^([^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFa-zA-ZＡ-Ｚａ-ｚ0-9０-９]+)', '', t).strip()
                    
                    # 移除結尾的骰子格式 【數字D數字:數字】
                    t = re.sub(r'【\d+D\d+:\d+】$', '', t).strip()
                    # 移除開頭的「數字＋點」格式（全形半形皆可），如 １．、1.、３.
                    t = re.sub(r'^[0-9０-９]+[.．]', '', t).strip()
                    # 若句尾包含特定符號，則將其刪除
                    t = t.rstrip('|｜(（').strip()
                    
                    report.append(f"    [步驟 4] 後處理解析 (去除非內文字元): '{original_t}' => '{t}'")
                    
                    if len(t) <= 2:
                        report.append("    -> ❌ 剔除：經過後處理後，剩餘文字長度 <= 2 字元。")
                        continue
                    else:
                        report.append(f"    - 最終長度檢驗通過 (長度: {len(t)})")
                        
                    report.append(f"    -> ✅ 成功提取最終文字: '{t}'")
            report.append("\n" + "="*40 + "\n")
            
        textbox.insert("1.0", "\n".join(report))
        textbox.configure(state="disabled")
        
        btn_close = ctk.CTkButton(modal, text="關閉視窗", command=modal.destroy, font=self.ui_font, fg_color="#dc3545", hover_color="#c82333")
        btn_close.pack(pady=10)

    def extract_text(self):
        source = self.source_text.get("1.0", tk.END)
        if not source.strip():
            self.show_toast("⚠️ 請先貼上原始文本！", color="#f39c12")
            return
            
        self.save_cache()
        filter_str = self.filter_text.get("1.0", tk.END).strip()
        
        custom_regexes = []
        for line in filter_str.split('\n'):
            line = line.strip()
            if line:
                try:
                    custom_regexes.append(re.compile(line))
                except re.error:
                    pass
        
        lines = source.split('\n')
        extracted_set: dict[str, int] = {}  # text -> source line number
        
        try:
            base_regex = re.compile(self.current_base_regex)
        except re.error:
            base_regex = re.compile(self.default_base_regex)
            
        try:
            invalid_regex = re.compile(self.current_invalid_regex)
        except re.error:
            invalid_regex = re.compile(self.default_invalid_regex)
            
        # Removed `invalid_prefix_regex` compilation logic as per user request
            
        try:
            symbol_regex = re.compile(self.current_symbol_regex)
        except re.error:
            symbol_regex = re.compile(self.default_symbol_regex)
            
        for line_num, line in enumerate(lines, 1):
            # Split the line by 2 or more half-width and full-width spaces
            chunks = re.split(r'[ 　]{2,}', line)
                
            for chunk in chunks:
                if not chunk.strip():
                    continue
                    
                chunk_str = str(chunk)
                check_segment = chunk_str
                
                symbol_count = len(symbol_regex.findall(check_segment))
                if symbol_count > len(check_segment) * 0.5:
                    continue
                    
                matches = base_regex.findall(chunk)
                for text in matches:
                    text = text.strip()
                    if len(text) < 3:
                        continue
                    is_valid = True
                    if invalid_regex.match(text):
                        is_valid = False
                    
                    if is_valid:
                        for reg in custom_regexes:
                            if reg.search(text):
                                is_valid = False
                                break
                                
                    if is_valid:
                        # 若被邊框符號 │ 或 | 包起來，則只取邊框內的文字並清除多餘的空白與句點
                        match = re.search(r'[│\|](.*)[│\|]', text)
                        if match:
                            text = match.group(1).strip(' \t　.')
                            
                        # 移除開頭非平假名、片假名、漢字、英文、數字的部分
                        text = re.sub(r'^([^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFa-zA-ZＡ-Ｚａ-ｚ0-9０-９]+)', '', text).strip()
                        
                        # 移除結尾的骰子格式 【數字D數字:數字】
                        text = re.sub(r'【\d+D\d+:\d+】$', '', text).strip()
                        # 移除開頭的「數字＋點」格式（全形半形皆可），如 １．、1.、３.
                        text = re.sub(r'^[0-9０-９]+[.．]', '', text).strip()
                        # 若句尾包含特定符號，則將其刪除
                        text = text.rstrip('|｜(（').strip()
                        
                        if len(text) <= 2:
                            continue
                            
                        if text and text not in extracted_set:
                            extracted_set[text] = line_num
        
        # 統計每個行號出現幾次，用於決定是否需要流水號
        line_count: dict[int, int] = {}
        for ln in extracted_set.values():
            line_count[ln] = line_count.get(ln, 0) + 1
        
        output = ""
        line_sub_index: dict[int, int] = {}  # 追蹤每個行號目前的流水號
        for text, ln in extracted_set.items():
            sub = line_sub_index.get(ln, 1)
            line_sub_index[ln] = sub + 1
            output += f"{ln:03d}-{sub}|{text}\n"
            
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
        # Scan the first few lines of the text for the chapter number pattern
        text = self.original_textbox.get("1.0", "5.0")
        match = re.search(r'第\s*(\d+)\s*話', text)
        if not match:
            match = re.search(r'番外編\s*(\d+)', text)
        if match:
            num_str = match.group(1)
            try:
                normalized_num = str(int(num_str))
                self.doc_num.delete(0, tk.END)
                self.doc_num.insert(0, normalized_num)
            except ValueError:
                pass

    def on_text_paste(self, event=None):
        self.after(50, self.check_chapter_number)

    def check_chapter_number(self):
        # Scan the first few lines of the text for the chapter number pattern
        text = self.source_text.get("1.0", "5.0")
        match = re.search(r'第\s*(\d+)\s*話', text)
        if not match:
            match = re.search(r'番外編\s*(\d+)', text)
        if match:
            num_str = match.group(1)
            try:
                normalized_num = str(int(num_str))
                self.doc_num.delete(0, tk.END)
                self.doc_num.insert(0, normalized_num)
            except ValueError:
                pass

    def validate_ai_text(self):
        """貼上 AI 翻譯後，自動檢查 ID 格式與行數是否正確。"""
        ai_content = self.ai_text.get("1.0", tk.END).strip()
        ext_content = self.extracted_text.get("1.0", tk.END).strip()

        if not ai_content:
            self.ai_warn_label.configure(text="")
            return

        ai_lines = [l for l in ai_content.split('\n') if l.strip()]
        ext_lines = [l for l in ext_content.split('\n') if l.strip()]

        warnings = []

        # 檢查每一行是否有超過一個 ID（格式為 數字-數字|）
        id_pattern = re.compile(r'\d{2,4}-\d+\|')
        multi_id_lines = []
        for i, line in enumerate(ai_lines, 1):
            ids_found = id_pattern.findall(line)
            if len(ids_found) >= 2:
                multi_id_lines.append(str(i))
        if multi_id_lines:
            warnings.append(f"⚠ 第 {','.join(multi_id_lines)} 行含有多個ID")

        if warnings:
            self.ai_warn_label.configure(text="  ".join(warnings), text_color="#ff4444")
        else:
            self.ai_warn_label.configure(text="✅ 格式正確", text_color="#28a745")
            # 3 秒後自動清除「格式正確」提示
            self.after(3000, lambda: self.ai_warn_label.configure(text="", text_color="#ff4444"))

    def apply_translation(self):
        source = self.source_text.get("1.0", tk.END)
        extracted = self.extracted_text.get("1.0", tk.END)
        translated = self.ai_text.get("1.0", tk.END)

        if not source.strip() or not extracted.strip() or not translated.strip():
            self.show_toast("⚠️ 請確保原始文本、提取結果和翻譯結果都有內容！", color="#f39c12")
            return
            
        self.save_cache()
        
        glossary_str = self.get_combined_glossary()
        glossary = {}
        for line in glossary_str.split('\n'):
            parts = line.split('=', 1)
            if len(parts) == 2 and parts[0].strip():
                glossary[parts[0].strip()] = parts[1].strip()

        orig_map = {}
        for line in extracted.split('\n'):
            if '|' in line:
                parts = line.split('|', 1)
                orig_map[parts[0].strip()] = parts[1].strip()

        trans_map = {}
        for line in translated.split('\n'):
            if '|' in line:
                parts = line.split('|', 1)
                trans_map[parts[0].strip()] = parts[1].strip()

        valid_ids = [k for k in trans_map.keys() if k in orig_map]
        valid_ids.sort(key=lambda k: len(orig_map[k]), reverse=True)

        sorted_glossary = sorted(glossary.items(), key=lambda x: len(x[0]), reverse=True)

        for _id in valid_ids:
            translated = trans_map[_id]
            original = orig_map[_id]
            final_translated = translated

            for jp_term, tw_term in sorted_glossary:
                final_translated = final_translated.replace(jp_term, tw_term)

            len_diff = len(original) - len(final_translated)
            padded_translated = final_translated
            if len_diff > 0:
                padded_translated += '　' * len_diff

            source_lines = str(source).split('\n')
            for i in range(len(source_lines)):
                if original in source_lines[i]:
                    try:
                        pattern = re.compile(re.escape(original))
                        def repl(m):
                            rest = m.string[m.end():]
                            if any(c in rest for c in ['|', '│', '｜', '┃']):
                                return padded_translated
                            return final_translated
                        source_lines[i] = pattern.sub(repl, source_lines[i])
                    except:
                        source_lines[i] = source_lines[i].replace(original, final_translated)
            source = '\n'.join(source_lines)

        # 針對沒有被抽取的原文部分，也全域套用一次術語表
        source_lines = source.split('\n')
        for jp_term, tw_term in sorted_glossary:
            len_diff = len(jp_term) - len(tw_term)
            padded_tw_term = tw_term
            if len_diff > 0:
                padded_tw_term += '　' * len_diff
                
            for i in range(len(source_lines)):
                if jp_term in source_lines[i]:
                    try:
                        pattern = re.compile(re.escape(jp_term))
                        def repl(m):
                            rest = m.string[m.end():]
                            if any(c in rest for c in ['|', '│', '｜', '┃']):
                                return padded_tw_term
                            return tw_term
                        source_lines[i] = pattern.sub(repl, source_lines[i])
                    except:
                        source_lines[i] = source_lines[i].replace(jp_term, tw_term)
        source = '\n'.join(source_lines)

        self.show_result_modal(source)

    def show_result_modal(self, text, source_file="", scroll_to_line=None):
        use_tab = hasattr(self, 'experimental_edit_tab') and self.experimental_edit_tab.get()

        if use_tab:
            # 內嵌編輯模式：使用 edit_frame 作為容器
            for w in self.edit_frame.winfo_children():
                w.destroy()
            container = self.edit_frame
            modal = None
        else:
            modal = ctk.CTkToplevel(self)
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

        def close_action():
            self.preview_text_cache = final_textbox.get("1.0", tk.END).rstrip('\n')
            self.save_cache()
            if modal:
                modal.destroy()
            else:
                self.switch_mode(self._previous_mode)

        if modal:
            modal.bind("<Escape>", lambda e: close_action())
            modal.protocol("WM_DELETE_WINDOW", close_action)
            modal.transient(self)

        def show_toast(message, color="#28a745", duration=3000):
            target = modal if modal else self
            toast = ctk.CTkFrame(target, fg_color=color, corner_radius=8)
            toast.place(relx=1.0, rely=0.0, anchor="ne", x=-20, y=60 if modal else 55)
            lbl = ctk.CTkLabel(toast, text=message, text_color="white", font=self.ui_font)
            lbl.pack(padx=20, pady=10)
            target.after(duration, toast.destroy)

        toolbar = ctk.CTkFrame(container, fg_color="#343a40", corner_radius=0)
        toolbar.pack(fill="x")

        tb_inner = ctk.CTkFrame(toolbar, fg_color="transparent")
        tb_inner.pack(fill="x", padx=10, pady=5)

        grp1 = ctk.CTkFrame(tb_inner, fg_color="transparent")
        grp1.pack(side="left", padx=5)
        
        ctk.CTkLabel(grp1, text="全文替換", text_color="white", font=self.ui_font).pack(side="left", padx=2)
        quick_orig = ctk.CTkEntry(grp1, placeholder_text="原文", width=120, font=self.ui_font)
        quick_orig.pack(side="left", padx=2)
        quick_trans = ctk.CTkEntry(grp1, placeholder_text="翻譯", width=120, font=self.ui_font)
        quick_trans.pack(side="left", padx=2)
        
        save_to_glossary_var = ctk.BooleanVar(value=True)
        save_to_glossary_cb = ctk.CTkCheckBox(grp1, text="存入術語表", variable=save_to_glossary_var, text_color="white", font=self.ui_small_font, width=80)
        save_to_glossary_cb.pack(side="left", padx=5)
        
        final_textbox = ctk.CTkTextbox(container, font=self.result_font, wrap="none", fg_color=self.bg_color, text_color=self.fg_color, undo=True)
        # Emulate browser line-height: 1.2 by adding spacing above and below each line (approx 2-3 pixels each)
        final_textbox._textbox.configure(spacing1=2, spacing3=2)
        
        # Override mouse wheel scrolling speed (目前設定為 8 行 per tick)
        def _on_mousewheel(event):
            # event.delta on Windows is usually +/- 120 per tick
            units = int(-(event.delta / 120) * 8)
            final_textbox._textbox.yview_scroll(units, "units")
            return "break"
            
        final_textbox._textbox.bind("<MouseWheel>", _on_mousewheel)
        
        def quick_replace():
            orig = quick_orig.get().strip()
            trans = quick_trans.get().strip()
            if not orig or not trans:
                show_toast("⚠️ 不可為空！", color="#f39c12")
                return

            len_diff = len(orig) - len(trans)
            padded_trans = trans
            if len_diff > 0:
                padded_trans += '　' * len_diff

            # 記住當前的捲軸位置
            current_yview = final_textbox._textbox.yview()

            current_text = final_textbox.get("1.0", tk.END).rstrip('\n')
            lines = current_text.split('\n')
            for i in range(len(lines)):
                if orig in lines[i]:
                    try:
                        pattern = re.compile(re.escape(orig))
                        def repl(m):
                            rest = m.string[m.end():]
                            if any(c in rest for c in ['|', '│', '｜', '┃']):
                                return padded_trans
                            return trans
                        lines[i] = pattern.sub(repl, lines[i])
                    except:
                        lines[i] = lines[i].replace(orig, trans)
            
            current_text = '\n'.join(lines)
            final_textbox.delete("1.0", tk.END)
            final_textbox.insert("1.0", current_text)

            # 恢復剛剛的捲軸位置
            final_textbox._textbox.yview_moveto(current_yview[0])

            if save_to_glossary_var.get():
                g_text = self.glossary_text.get("1.0", tk.END).rstrip('\n')
                if g_text:
                    g_text += '\n'
                g_text += f"{orig}={trans}"
                self.glossary_text.delete("1.0", tk.END)
                self.glossary_text.insert("1.0", g_text)
                self.save_cache()

            quick_orig.delete(0, tk.END)
            quick_trans.delete(0, tk.END)

        btn_rep = ctk.CTkButton(grp1, text="執行", command=quick_replace, fg_color="#17a2b8", hover_color="#138496", font=self.ui_small_font, width=50)
        btn_rep.pack(side="left", padx=5)

        def reapply_glossary():
            glossary_str = self.get_combined_glossary()
            if not glossary_str:
                show_toast("⚠️ 術語表為空！", color="#f39c12")
                return

            glossary = {}
            for line in glossary_str.split('\n'):
                parts = line.split('=', 1)
                if len(parts) == 2 and parts[0].strip():
                    glossary[parts[0].strip()] = parts[1].strip()

            if not glossary:
                show_toast("⚠️ 術語表格式不正確或為空！", color="#f39c12")
                return

            # 記住當前的捲軸位置
            current_yview = final_textbox._textbox.yview()
            current_text = final_textbox.get("1.0", tk.END).rstrip('\n')
            lines = current_text.split('\n')

            sorted_glossary = sorted(glossary.items(), key=lambda x: len(x[0]), reverse=True)
            for orig, trans in sorted_glossary:
                len_diff = len(orig) - len(trans)
                padded_trans = trans
                if len_diff > 0:
                    padded_trans += '　' * len_diff

                for i in range(len(lines)):
                    if orig in lines[i]:
                        try:
                            pattern = re.compile(re.escape(orig))
                            def repl(m):
                                rest = m.string[m.end():]
                                if any(c in rest for c in ['|', '│', '｜', '┃']):
                                    return padded_trans
                                return trans
                            lines[i] = pattern.sub(repl, lines[i])
                        except:
                            lines[i] = lines[i].replace(orig, trans)

            current_text = '\n'.join(lines)
            final_textbox.delete("1.0", tk.END)
            final_textbox.insert("1.0", current_text)

            # 恢復剛剛的捲軸位置
            final_textbox._textbox.yview_moveto(current_yview[0])
            self.save_cache()
            
            show_toast("✅ 已套用術語表變更！", color="#28a745")

        btn_reapply = ctk.CTkButton(grp1, text="重套術語", command=reapply_glossary, fg_color="#28a745", hover_color="#218838", font=self.ui_small_font, width=60)
        btn_reapply.pack(side="left", padx=5)

        grp2 = ctk.CTkFrame(tb_inner, fg_color="transparent")
        grp2.pack(side="left", padx=20)

        # ctk.CTkLabel(grp2, text="選區操作：", text_color="white", font=self.ui_font).pack(side="left", padx=2)
        
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

                # 如果選取範圍內含有上色標籤，則移除所有上色標籤（去色模式）
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

        ctk.CTkButton(grp2, text="上色", command=apply_color, fg_color="#6f42c1", hover_color="#5a32a3", font=self.ui_small_font, width=60).pack(side="left", padx=5)

        def strip_spaces():
            try:
                first = final_textbox._textbox.index(tk.SEL_FIRST)
                last = final_textbox._textbox.index(tk.SEL_LAST)
                selected_text = final_textbox._textbox.get(first, last)
                if not selected_text:
                    return
                # Replace different types of spaces
                stripped_text = selected_text.replace(" ", "").replace("　", "")
                
                final_textbox._textbox.delete(first, last)
                final_textbox._textbox.insert(first, stripped_text)
            except tk.TclError:
                show_toast("⚠️ 請先選取想要消除空白的文字！", color="#f39c12")

        ctk.CTkButton(grp2, text="消空白", command=strip_spaces, fg_color="#e0a800", hover_color="#c82333", text_color="black", font=self.ui_small_font, width=60).pack(side="left", padx=5)

        def add_double_spaces():
            try:
                first = final_textbox._textbox.index(tk.SEL_FIRST)
                last = final_textbox._textbox.index(tk.SEL_LAST)
                selected_text = final_textbox._textbox.get(first, last)
                if not selected_text:
                    return
                # Insert two full-width spaces between each character, preserving newlines
                lines = selected_text.split('\n')
                spaced_lines = []
                for line in lines:
                    spaced_line = "　　".join(list(line))
                    spaced_lines.append(spaced_line)
                spaced_text = '\n'.join(spaced_lines)
                
                final_textbox._textbox.delete(first, last)
                final_textbox._textbox.insert(first, spaced_text)
            except tk.TclError:
                show_toast("⚠️ 請先選取想要補空白的文字！", color="#f39c12")

        ctk.CTkButton(grp2, text="補空白", command=add_double_spaces, fg_color="#17a2b8", hover_color="#138496", text_color="white", font=self.ui_small_font, width=60).pack(side="left", padx=5)

        def adjust_bubble():
            try:
                first_idx = final_textbox._textbox.index(tk.SEL_FIRST)
                last_idx = final_textbox._textbox.index(tk.SEL_LAST)
                
                # 自動擴展為完整行，方便使用者不用精準選取到每一行的行首行尾
                first = final_textbox._textbox.index(f"{first_idx} linestart")
                
                if last_idx.split('.')[1] == '0' and last_idx != first_idx:
                    last = final_textbox._textbox.index(f"{last_idx} - 1 chars lineend")
                else:
                    last = final_textbox._textbox.index(f"{last_idx} lineend")

                selected_text = final_textbox._textbox.get(first, last)
                if not selected_text:
                    return
                
                # ── 特規：吶喊對話框 ､__人_人_...人人 / ）...（ / ⌒Y⌒Y...⌒Ｙ ──
                # 核心思路：每行左側是 AA 圖案（前綴），右側才是吶喊框。
                #           只提取並調整右側的氣泡部分，左側 AA 完全不動。
                shout_lines = selected_text.split('\n')
                has_top = any('_人' in ln for ln in shout_lines)
                has_bot = any('⌒Y' in ln or '⌒Ｙ' in ln for ln in shout_lines)
                if has_top and has_bot:
                    delim_pairs = [('）', '（'), ('＞', '＜'), ('>', '<'), ('》', '《')]

                    parsed_shout: list[dict] = []
                    for sl in shout_lines:
                        if '_人' in sl:
                            # 上邊框：用正則找出 ､__人_人... 的重複邊框圖樣
                            m = re.search(r'[､、]?[_＿]+(?:人[_＿]*)+', sl)
                            if m:
                                parsed_shout.append({
                                    'type': 'top',
                                    'prefix': sl[:m.start()],
                                    'bubble': sl[m.start():].rstrip(),
                                    'orig': sl
                                })
                            else:
                                parsed_shout.append({'type': 'other', 'orig': sl})
                        elif '⌒Y' in sl or '⌒Ｙ' in sl:
                            # 下邊框：找出 ⌒Y 重複圖樣的起始位置
                            m = re.search(r'(?:⌒[YＹ]){2,}', sl)
                            if m:
                                parsed_shout.append({
                                    'type': 'bot',
                                    'prefix': sl[:m.start()],
                                    'bubble': sl[m.start():].rstrip(),
                                    'orig': sl
                                })
                            else:
                                parsed_shout.append({'type': 'other', 'orig': sl})
                        else:
                            # 內容行：在行內搜尋 ）...（ 等分隔符（行尾為右括號）
                            stripped_rl = sl.rstrip(' \u3000\r\n')
                            found_content = False

                            for lc, rc in delim_pairs:
                                if not stripped_rl.endswith(rc):
                                    continue
                                # 從行尾的右括號往前找對應的左括號
                                rc_pos = len(stripped_rl) - 1
                                lc_pos = stripped_rl.rfind(lc, 0, rc_pos)
                                if lc_pos == -1:
                                    continue

                                prefix = sl[:lc_pos]
                                inner = stripped_rl[lc_pos + 1:rc_pos]
                                # 清除內容尾部的多餘空白與句點/逗號
                                inner_clean = re.sub(r'[ \u3000.,]+$', '', inner)

                                parsed_shout.append({
                                    'type': 'content',
                                    'prefix': prefix,
                                    'left_char': lc,
                                    'right_char': rc,
                                    'inner': inner_clean,
                                    'orig': sl
                                })
                                found_content = True
                                break

                            if not found_content:
                                parsed_shout.append({'type': 'other', 'orig': sl})

                    # 取得原始邊框的「氣泡部分」像素寬度
                    orig_border_w = 0
                    for ps in parsed_shout:
                        if ps['type'] in ('top', 'bot') and 'bubble' in ps:
                            orig_border_w = int(self.result_font.measure(ps['bubble']))
                            break

                    if orig_border_w == 0:
                        show_toast("⚠️ 無法計算吶喊框寬度！", color="#f39c12")
                        return

                    # 計算所有內容行所需的最小寬度（內容 + 一個全形空白的留白 + 左右括號）
                    max_content_w = 0
                    for ps in parsed_shout:
                        if ps['type'] == 'content':
                            lc = ps['left_char']
                            rc = ps['right_char']
                            inner = ps['inner'].rstrip(' \u3000')
                            needed = int(self.result_font.measure(lc + inner + '　' + rc))
                            if needed > max_content_w:
                                max_content_w = needed

                    # 目標寬度 = 取邊框與內容所需寬度的最大值（自動伸縮）
                    target_width = max(orig_border_w, max_content_w)

                    # 重建各行：邊框與內容都調整至 target_width
                    new_shout: list[str] = []
                    for ps in parsed_shout:
                        if ps['type'] == 'top':
                            # 重建上邊框：､_ + 重複 _人 直到匹配目標寬度
                            bubble = ps['bubble']
                            corner = bubble[0] if bubble and bubble[0] in '､、' else ''
                            res = corner + '_'
                            unit = '_人'
                            while int(self.result_font.measure(res + unit)) <= target_width:
                                res += unit
                            new_shout.append(ps['prefix'] + res)

                        elif ps['type'] == 'bot':
                            # 重建下邊框：重複 ⌒Y 直到匹配目標寬度，最後一個換成全形 Ｙ
                            res = ''
                            unit = '⌒Y'
                            while int(self.result_font.measure(res + unit)) <= target_width:
                                res += unit
                            if res.endswith('Y'):
                                res = res[:-1] + 'Ｙ'
                            new_shout.append(ps['prefix'] + res)

                        elif ps['type'] == 'content':
                            lc = ps['left_char']
                            rc = ps['right_char']
                            inner = ps['inner'].rstrip(' \u3000')

                            paren_w = int(self.result_font.measure(lc)) + int(self.result_font.measure(rc))
                            target_inner_w = target_width - paren_w
                            if target_inner_w < 0:
                                target_inner_w = 0

                            padded = inner
                            fw_sp = '　'
                            hw_sp = ' '
                            while int(self.result_font.measure(padded + fw_sp)) <= target_inner_w:
                                padded += fw_sp
                            while int(self.result_font.measure(padded + hw_sp)) <= target_inner_w:
                                padded += hw_sp

                            new_shout.append(ps['prefix'] + lc + padded + rc)

                        else:
                            new_shout.append(ps['orig'])

                    new_text = '\n'.join(new_shout)
                    final_textbox._textbox.delete(first, last)
                    final_textbox._textbox.insert(first, new_text)
                    return
                # ── 特規結束 ──

                # ── 特規：斜線框對話框 ＼─|──|──|─／ / │...│ or ─...─ / ／─|──|──|─＼ ──
                # 核心思路：與吶喊框相同，只調整右側氣泡部分，左側 AA 完全不動。
                has_slash_top = any(re.search(r'＼─\|(?:──\|){2,}', ln) for ln in shout_lines)
                has_slash_bot = any(re.search(r'／─\|(?:──\|){2,}', ln) for ln in shout_lines)
                if has_slash_top and has_slash_bot:
                    slash_delim_chars = ['│', '─']

                    parsed_slash: list[dict] = []
                    for sl in shout_lines:
                        # 偵測上邊框：＼─|──|...──|─／
                        m_top = re.search(r'＼─\|(?:──\|)+─?／', sl)
                        # 偵測下邊框：／─|──|...──|─＼
                        m_bot = re.search(r'／─\|(?:──\|)+─?＼', sl)

                        if m_top:
                            parsed_slash.append({
                                'type': 'top',
                                'prefix': sl[:m_top.start()],
                                'bubble': sl[m_top.start():m_top.end()],
                                'orig': sl
                            })
                        elif m_bot:
                            parsed_slash.append({
                                'type': 'bot',
                                'prefix': sl[:m_bot.start()],
                                'bubble': sl[m_bot.start():m_bot.end()],
                                'orig': sl
                            })
                        else:
                            # 偵測內容行：行尾為 │ 或 ─，往回找配對的左分隔符
                            stripped_rl = sl.rstrip(' \u3000\r\n')
                            found_content = False

                            for dc in slash_delim_chars:
                                if not stripped_rl.endswith(dc):
                                    continue
                                rc_pos = len(stripped_rl) - 1
                                # 從右分隔符往前找同一字元的左分隔符
                                lc_pos = stripped_rl.rfind(dc, 0, rc_pos)
                                if lc_pos == -1:
                                    continue

                                prefix = sl[:lc_pos]
                                inner = stripped_rl[lc_pos + 1:rc_pos]
                                inner_clean = re.sub(r'[ \u3000.]+$', '', inner)

                                parsed_slash.append({
                                    'type': 'content',
                                    'prefix': prefix,
                                    'left_char': dc,
                                    'right_char': dc,
                                    'inner': inner_clean,
                                    'orig': sl
                                })
                                found_content = True
                                break

                            if not found_content:
                                parsed_slash.append({'type': 'other', 'orig': sl})

                    # 取得原始邊框寬度
                    orig_border_w = 0
                    for ps in parsed_slash:
                        if ps['type'] in ('top', 'bot') and 'bubble' in ps:
                            orig_border_w = int(self.result_font.measure(ps['bubble']))
                            break

                    if orig_border_w == 0:
                        show_toast("⚠️ 無法計算斜線框寬度！", color="#f39c12")
                        return

                    # 計算所有內容行所需的最小寬度
                    max_content_w = 0
                    for ps in parsed_slash:
                        if ps['type'] == 'content':
                            lc = ps['left_char']
                            rc = ps['right_char']
                            inner = ps['inner'].rstrip(' \u3000')
                            needed = int(self.result_font.measure(lc + inner + '　' + rc))
                            if needed > max_content_w:
                                max_content_w = needed

                    target_width = max(orig_border_w, max_content_w)

                    # 重建各行
                    new_slash: list[str] = []
                    for ps in parsed_slash:
                        if ps['type'] == 'top':
                            # 重建上邊框：＼─| + ──| × N + ─／
                            res = '＼─|'
                            unit = '──|'
                            end = '─／'
                            while int(self.result_font.measure(res + unit + end)) <= target_width:
                                res += unit
                            res += end
                            new_slash.append(ps['prefix'] + res)

                        elif ps['type'] == 'bot':
                            # 重建下邊框：／─| + ──| × N + ─＼
                            res = '／─|'
                            unit = '──|'
                            end = '─＼'
                            while int(self.result_font.measure(res + unit + end)) <= target_width:
                                res += unit
                            res += end
                            new_slash.append(ps['prefix'] + res)

                        elif ps['type'] == 'content':
                            lc = ps['left_char']
                            rc = ps['right_char']
                            inner = ps['inner'].rstrip(' \u3000')

                            paren_w = int(self.result_font.measure(lc)) + int(self.result_font.measure(rc))
                            target_inner_w = target_width - paren_w
                            if target_inner_w < 0:
                                target_inner_w = 0

                            padded = inner
                            fw_sp = '　'
                            hw_sp = ' '
                            while int(self.result_font.measure(padded + fw_sp)) <= target_inner_w:
                                padded += fw_sp
                            while int(self.result_font.measure(padded + hw_sp)) <= target_inner_w:
                                padded += hw_sp

                            new_slash.append(ps['prefix'] + lc + padded + rc)

                        else:
                            new_slash.append(ps['orig'])

                    new_text = '\n'.join(new_slash)
                    final_textbox._textbox.delete(first, last)
                    final_textbox._textbox.insert(first, new_text)
                    return
                # ── 斜線框特規結束 ──

                lines = selected_text.split('\n')
                parsed = []
                for line in lines:
                    if not line:
                        parsed.append({'type': 'orig', 'orig': line})
                        continue
                        
                    line_n = line.rstrip('\r\n \u3000')
                    matched_border = False
                    for char in ['￣', '＿', '─', '-', '=']:
                        # Find all matches of 3 or more consecutive border characters, to be slightly stricter
                        matches = list(re.finditer(f'({re.escape(char)}{{3,}})', line_n))
                        if matches:
                            # Check matches from right to left
                            for match in reversed(matches):
                                m_end = int(match.end()) # type: ignore
                                m_start = int(match.start()) # type: ignore
                                right_part = line_n[m_end:]
                                # A true border has its sequence near the very end of the line.
                                # The corner characters (like ｀ヽ or ノ) are usually 1-3 chars max.
                                clean_right = right_part.strip(' \u3000')
                                if len(clean_right) <= 4:
                                    left_part = line_n[:m_start]
                                    
                                    parsed.append({
                                        'type': 'border', 
                                        'left': left_part, 
                                        'char': char, 
                                        'right': right_part, 
                                        'orig': line_n
                                    })
                                    matched_border = True
                                    break
                            if matched_border:
                                break
                    if matched_border:
                        continue
                        
                    match = re.search(r'^(.*?[^ \u3000.])([ \u3000.]+)([^ \u3000.]{1,3})$', line_n)
                    if match:
                        right_chunk = match.group(3)
                        if any(c in right_chunk for c in '│｜|〉》>）)ノﾉ＼ヽ｝}］]'):
                            left_text = match.group(1)
                            left_text = re.sub(r'[ \u3000.,]+$', '', left_text)
                            parsed.append({'type': 'content', 'left': left_text, 'char': ' ', 'right_padding': match.group(2), 'right': right_chunk, 'orig': line_n})
                            continue

                    left_text = re.sub(r'[ \u3000.,]+$', '', line_n)
                    parsed.append({'type': 'content', 'left': left_text, 'char': ' ', 'right_padding': '', 'right': '', 'orig': line_n})

                # Only consider Traditional Chinese (and common CJK characters), full-width punctuation, Katakana/Hiragana (if applicable, though user wants strictly CJK/Alphanum), and alphanumeric characters as valid textual measurement limits.
                # \u4e00-\u9fff: CJK Unified Ideographs (Chinese Characters)
                # \u3000-\u303f: CJK Symbols and Punctuation (Full width punctuations)
                # \uff00-\uffef: Halfwidth and Fullwidth Forms
                # a-zA-Z0-9: Alphanumeric
                valid_text_regex = re.compile(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9…―—]')

                max_left_w = 0
                has_right_border = False
                has_border_line = False
                for p in parsed:
                    if p['type'] == 'border':
                        has_border_line = True
                    elif p.get('type') == 'content':
                        # Find the logical end of the sentence to bound the text measurement
                        matches = list(valid_text_regex.finditer(str(p.get('left', ''))))
                        if matches:
                            last_char_end = int(matches[-1].end()) # type: ignore
                            text_up_to_last = str(p.get('left', ''))[:last_char_end]
                            w = int(self.result_font.measure(text_up_to_last))
                        else:
                            # Fallback if the line has no typical valid text characters, just strip spaces
                            w = int(self.result_font.measure(str(p.get('left', '')).rstrip(' \u3000')))

                        if w > max_left_w:
                            max_left_w = w
                        if p.get('right'):
                            has_right_border = True
                
                if not has_border_line:
                    show_toast("⚠️ 選取的範圍沒有標準對話框邊界 (￣ 或 ＿)，請確認選取範圍！", color="#f39c12")
                    return

                target_width = int(max_left_w) - int(self.result_font.measure("　"))
                if target_width < 0:
                    target_width = 0
                
                if has_right_border:
                    target_width += int(self.result_font.measure("　"))

                border_target_width = int(int(max_left_w) + int(self.result_font.measure("￣")))
                if border_target_width < 0:
                    border_target_width = 0

                # Target width for right-aligned items (e.g. '|', '>', ')') should align with the end of the border
                # but shifted left by about 1 full-width space to sit visually inside the corner of ｀ヽ or ＿ノ
                align_target_width = int(border_target_width - int(self.result_font.measure("　")))

                new_lines: list[str] = []
                last_border = ""
                
                for p in parsed:
                    if p.get('type') == 'border':
                        pad_char = str(p.get('char', ''))
                        res = str(p.get('left', '')) + pad_char
                        while int(self.result_font.measure(res + pad_char)) <= border_target_width:
                            res += pad_char
                        
                        d1 = int(border_target_width) - int(self.result_font.measure(res))
                        d2 = int(self.result_font.measure(res + pad_char)) - int(border_target_width)
                        if d2 < d1:
                            res += pad_char
                        
                        last_border = res + str(p.get('right', ''))
                        new_lines.append(last_border)
                        
                    elif p.get('type') == 'content':
                        if p.get('right'):
                            if last_border:
                                target_width = int(self.result_font.measure(last_border[:-1])) # type: ignore
                            else:
                                target_width = align_target_width # fallback if no top border seen yet

                            res_prefix = str(p.get('left', ''))

                            physical_base_width = int(self.result_font.measure(res_prefix))

                            if physical_base_width < target_width:
                                fw_sp = "　"
                                hw_sp = " "

                                while int(self.result_font.measure(res_prefix + fw_sp)) <= target_width:
                                    res_prefix += fw_sp
                                while int(self.result_font.measure(res_prefix + hw_sp)) <= target_width:
                                    res_prefix += hw_sp

                                d1 = target_width - int(self.result_font.measure(res_prefix))
                                d2 = int(self.result_font.measure(res_prefix + hw_sp)) - target_width
                                if d2 < d1:
                                    res_prefix += hw_sp

                            new_lines.append(res_prefix + str(p.get('right', '')))
                        else:
                            # 沒有右邊框的內容行：使用已清除尾部句點的 left 而非原始行
                            new_lines.append(str(p.get('left', p.get('orig', ''))))
                    else:
                        new_lines.append(str(p.get('orig', '')))
                
                new_text = '\n'.join(new_lines)
                final_textbox._textbox.delete(first, last)
                final_textbox._textbox.insert(first, new_text)
                
            except tk.TclError:
                show_toast("⚠️ 請先選取想要調整的對話框！", color="#f39c12")

        ctk.CTkButton(grp2, text="對話框修正", command=adjust_bubble, fg_color="#28a745", hover_color="#218838", text_color="white", font=self.ui_small_font, width=80).pack(side="left", padx=5)

        def align_to_prev_line():
            try:
                cursor_pos = final_textbox._textbox.index(tk.INSERT)
                line_idx, col_idx = cursor_pos.split('.')
                line_idx = int(line_idx)
                col_idx = int(col_idx)

                # Check if there is a previous line
                prev_line_idx = line_idx - 1
                if prev_line_idx < 1:
                    show_toast("⚠️ 這是第一行，沒有上一行可以對齊！", color="#f39c12")
                    return
                
                prev_line_text = final_textbox._textbox.get(f"{prev_line_idx}.0", f"{prev_line_idx}.end")
                prev_line_text = prev_line_text.rstrip('\r\n \u3000') # Remove trailing spaces
                
                if not prev_line_text:
                    show_toast("⚠️ 上一行為空，無法對齊！", color="#f39c12")
                    return
                
                current_line_text = final_textbox._textbox.get(f"{line_idx}.0", f"{line_idx}.end")
                
                # Find first non-space character after cursor
                target_col = -1
                for i in range(col_idx, len(current_line_text)):
                    if current_line_text[i] not in [' ', '　']:
                        target_col = i
                        break
                        
                if target_col == -1:
                    show_toast("⚠️ 游標後方沒有可以對齊的符號！", color="#f39c12")
                    return
                
                selected_text = current_line_text[target_col:]
                
                # The target width we want the text BEFORE our symbol to take up
                # is the width of the prev_line_text excluding its last character
                target_width = self.result_font.measure(prev_line_text[:-1])
                
                # The text that comes before our target symbol
                text_before_selection = current_line_text[:target_col]
                stripped_before = text_before_selection.rstrip(' \u3000') # remove trailing spaces
                
                base_width = self.result_font.measure(stripped_before)
                if base_width >= target_width:
                    res_prefix = stripped_before
                else:
                    res_prefix = stripped_before
                    fw_sp = "　"
                    hw_sp = " "
                    while self.result_font.measure(res_prefix + fw_sp) <= target_width:
                        res_prefix += fw_sp
                    while self.result_font.measure(res_prefix + hw_sp) <= target_width:
                        res_prefix += hw_sp
                    
                    d1 = target_width - self.result_font.measure(res_prefix)
                    d2 = self.result_font.measure(res_prefix + hw_sp) - target_width
                    if d2 < d1:
                         res_prefix += hw_sp
                
                final_textbox._textbox.delete(f"{line_idx}.0", f"{line_idx}.end")
                final_textbox._textbox.insert(f"{line_idx}.0", res_prefix + " " + selected_text)
                
                # Move cursor to the position right before the aligned symbol
                new_col = len(res_prefix) + 1
                final_textbox._textbox.mark_set(tk.INSERT, f"{line_idx}.{new_col}")
                final_textbox._textbox.see(tk.INSERT)
                
            except Exception as e:
                show_toast(f"❌ 無法對齊：{e}", color="#dc3545", duration=5000)

        ctk.CTkButton(grp2, text="對齊上一行", command=align_to_prev_line, fg_color="#17a2b8", hover_color="#138496", text_color="white", font=self.ui_small_font, width=80).pack(side="left", padx=5)

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
                
        ctk.CTkButton(grp2, text="自動判斷", command=smart_action, fg_color="#e67e22", hover_color="#d35400", text_color="white", font=self.ui_small_font, width=70).pack(side="left", padx=5)

        def adjust_all_bubbles():
            """掃描全文，找出所有獨立對話框並自動對齊。
            支援三種類型：普通對話框（￣/＿）、吶喊框（_人/⌒Y）、斜線框（＼─|／）。
            安全規則：嚴格檢查邊框角落字元，避免誤判 AA 圖像。"""
            text = final_textbox.get("1.0", tk.END).rstrip('\n')
            all_lines = text.split('\n')
            valid_text_re = re.compile(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9…―—]')

            # all_boxes: (top_idx, bot_idx, box_type)
            all_boxes: list[tuple[int, int, str]] = []
            used_lines: set[int] = set()

            # ================================================================
            # 偵測 A: 吶喊框 ── ､_人_人... / ）...（ / ⌒Y⌒Y...⌒Ｙ
            # ================================================================
            re_shout_top = re.compile(r'[､、][_＿]+(?:人[_＿]*){3,}')
            re_shout_bot = re.compile(r'(?:⌒[YＹ]){3,}')
            s_tops: list[int] = []
            s_bots: list[int] = []
            for i, ln in enumerate(all_lines):
                if re_shout_top.search(ln):
                    s_tops.append(i)
                if re_shout_bot.search(ln):
                    s_bots.append(i)
            for ti in s_tops:
                if ti in used_lines:
                    continue
                for bi in s_bots:
                    if bi <= ti or bi in used_lines:
                        continue
                    if bi - ti > 30:
                        break
                    all_boxes.append((ti, bi, 'shout'))
                    for k in range(ti, bi + 1):
                        used_lines.add(k)
                    break

            # ================================================================
            # 偵測 B: 斜線框 ── ＼─|──|──|─／ / │...│ / ／─|──|──|─＼
            # ================================================================
            re_slash_top = re.compile(r'＼─\|(?:──\|){2,}─?／')
            re_slash_bot = re.compile(r'／─\|(?:──\|){2,}─?＼')
            for i, ln in enumerate(all_lines):
                if i in used_lines:
                    continue
                m_top = re_slash_top.search(ln)
                if not m_top:
                    continue
                # 找最近的下邊框
                for j in range(i + 1, min(i + 31, len(all_lines))):
                    if j in used_lines:
                        continue
                    if re_slash_bot.search(all_lines[j]):
                        all_boxes.append((i, j, 'slash'))
                        for k in range(i, j + 1):
                            used_lines.add(k)
                        break

            # ================================================================
            # 偵測 C: 普通對話框 ── f´￣￣￣｀ヽ / 乂＿＿＿ノ
            # 安全規則：左右角落須為已知字元
            # ================================================================
            def _is_safe_normal_border(left_part: str, right_part: str, char: str) -> bool:
                rp_clean = right_part.strip(' \u3000')
                if len(rp_clean) > 4:
                    return False
                lp_end = left_part.rstrip(' \u3000')
                last_c = lp_end[-1] if lp_end else ''
                if char == '￣':
                    if last_c not in "´'":
                        return False
                    if not any(c in rp_clean for c in '｀ヽﾍ'):
                        return False
                elif char == '＿':
                    if last_c not in '乂ヽ丶':
                        return False
                    if not any(c in rp_clean for c in 'ノﾉ'):
                        return False
                else:
                    return False
                return True

            n_borders: list[dict] = []
            for i, line in enumerate(all_lines):
                if i in used_lines:
                    continue
                line_n = line.rstrip('\r\n \u3000')
                for char, btype in [('￣', 'top'), ('＿', 'bot')]:
                    matches = list(re.finditer(f'({re.escape(char)}{{3,}})', line_n))
                    if not matches:
                        continue
                    for m in reversed(matches):
                        rp = line_n[m.end():]
                        if len(rp.strip(' \u3000')) <= 4:
                            lp = line_n[:m.start()]
                            if _is_safe_normal_border(lp, rp, char):
                                n_borders.append({
                                    'line': i, 'btype': btype,
                                    'left': lp, 'char': char, 'right': rp, 'orig': line_n
                                })
                            break
                    break

            n_tops = [b for b in n_borders if b['btype'] == 'top']
            n_bots = [b for b in n_borders if b['btype'] == 'bot']
            for top in n_tops:
                ti = top['line']
                if ti in used_lines:
                    continue
                for bot in n_bots:
                    bi = bot['line']
                    if bi <= ti or bi in used_lines:
                        continue
                    if bi - ti > 30:
                        break
                    has_inner = any(
                        t['line'] > ti and t['line'] < bi and t['line'] not in used_lines
                        for t in n_tops if t is not top
                    )
                    if has_inner:
                        break
                    all_boxes.append((ti, bi, 'normal'))
                    for k in range(ti, bi + 1):
                        used_lines.add(k)
                    break

            if not all_boxes:
                show_toast("⚠️ 未找到可處理的獨立對話框！", color="#f39c12")
                return

            # ================================================================
            # 各類型的處理函式
            # ================================================================
            def _process_shout(box_lines: list[str]) -> list[str] | None:
                """處理吶喊框"""
                delim_pairs = [('）', '（'), ('＞', '＜'), ('>', '<'), ('》', '《')]
                re_top = re.compile(r'[､、]?[_＿]+(?:人[_＿]*)+')
                re_bot = re.compile(r'(?:⌒[YＹ]){2,}')
                parsed: list[dict] = []
                for sl in box_lines:
                    m_t = re_top.search(sl)
                    m_b = re_bot.search(sl)
                    if m_t and not m_b:
                        parsed.append({'type': 'top', 'prefix': sl[:m_t.start()],
                                       'bubble': sl[m_t.start():].rstrip(), 'orig': sl})
                    elif m_b:
                        parsed.append({'type': 'bot', 'prefix': sl[:m_b.start()],
                                       'bubble': sl[m_b.start():].rstrip(), 'orig': sl})
                    else:
                        stripped = sl.rstrip(' \u3000\r\n')
                        found = False
                        for lc, rc in delim_pairs:
                            if not stripped.endswith(rc):
                                continue
                            rc_pos = len(stripped) - 1
                            lc_pos = stripped.rfind(lc, 0, rc_pos)
                            if lc_pos == -1:
                                continue
                            inner = re.sub(r'[ \u3000.,]+$', '', stripped[lc_pos + 1:rc_pos])
                            parsed.append({'type': 'content', 'prefix': sl[:lc_pos],
                                           'left_char': lc, 'right_char': rc, 'inner': inner, 'orig': sl})
                            found = True
                            break
                        if not found:
                            parsed.append({'type': 'other', 'orig': sl})

                obw = 0
                for ps in parsed:
                    if ps['type'] in ('top', 'bot') and 'bubble' in ps:
                        obw = int(self.result_font.measure(ps['bubble']))
                        break
                if obw == 0:
                    return None

                mcw = 0
                for ps in parsed:
                    if ps['type'] == 'content':
                        needed = int(self.result_font.measure(
                            ps['left_char'] + ps['inner'].rstrip(' \u3000') + '　' + ps['right_char']))
                        if needed > mcw:
                            mcw = needed
                tw = max(obw, mcw)

                result: list[str] = []
                for ps in parsed:
                    if ps['type'] == 'top':
                        bubble = ps['bubble']
                        corner = bubble[0] if bubble and bubble[0] in '､、' else ''
                        res = corner + '_'
                        while int(self.result_font.measure(res + '_人')) <= tw:
                            res += '_人'
                        result.append(ps['prefix'] + res)
                    elif ps['type'] == 'bot':
                        res = ''
                        while int(self.result_font.measure(res + '⌒Y')) <= tw:
                            res += '⌒Y'
                        if res.endswith('Y'):
                            res = res[:-1] + 'Ｙ'
                        result.append(ps['prefix'] + res)
                    elif ps['type'] == 'content':
                        lc, rc = ps['left_char'], ps['right_char']
                        inner = ps['inner'].rstrip(' \u3000')
                        pw = int(self.result_font.measure(lc)) + int(self.result_font.measure(rc))
                        tiw = tw - pw
                        if tiw < 0:
                            tiw = 0
                        padded = inner
                        while int(self.result_font.measure(padded + '　')) <= tiw:
                            padded += '　'
                        while int(self.result_font.measure(padded + ' ')) <= tiw:
                            padded += ' '
                        result.append(ps['prefix'] + lc + padded + rc)
                    else:
                        result.append(ps['orig'])
                return result

            def _process_slash(box_lines: list[str]) -> list[str] | None:
                """處理斜線框"""
                slash_delims = ['│', '─']
                parsed: list[dict] = []
                for sl in box_lines:
                    mt = re.search(r'＼─\|(?:──\|)+─?／', sl)
                    mb = re.search(r'／─\|(?:──\|)+─?＼', sl)
                    if mt:
                        parsed.append({'type': 'top', 'prefix': sl[:mt.start()],
                                       'bubble': sl[mt.start():mt.end()], 'orig': sl})
                    elif mb:
                        parsed.append({'type': 'bot', 'prefix': sl[:mb.start()],
                                       'bubble': sl[mb.start():mb.end()], 'orig': sl})
                    else:
                        stripped = sl.rstrip(' \u3000\r\n')
                        found = False
                        for dc in slash_delims:
                            if not stripped.endswith(dc):
                                continue
                            rc_pos = len(stripped) - 1
                            lc_pos = stripped.rfind(dc, 0, rc_pos)
                            if lc_pos == -1:
                                continue
                            inner = re.sub(r'[ \u3000.]+$', '', stripped[lc_pos + 1:rc_pos])
                            parsed.append({'type': 'content', 'prefix': sl[:lc_pos],
                                           'left_char': dc, 'right_char': dc, 'inner': inner, 'orig': sl})
                            found = True
                            break
                        if not found:
                            parsed.append({'type': 'other', 'orig': sl})

                obw = 0
                for ps in parsed:
                    if ps['type'] in ('top', 'bot') and 'bubble' in ps:
                        obw = int(self.result_font.measure(ps['bubble']))
                        break
                if obw == 0:
                    return None

                mcw = 0
                for ps in parsed:
                    if ps['type'] == 'content':
                        needed = int(self.result_font.measure(
                            ps['left_char'] + ps['inner'].rstrip(' \u3000') + '　' + ps['right_char']))
                        if needed > mcw:
                            mcw = needed
                tw = max(obw, mcw)

                result: list[str] = []
                for ps in parsed:
                    if ps['type'] == 'top':
                        res = '＼─|'
                        while int(self.result_font.measure(res + '──|' + '─／')) <= tw:
                            res += '──|'
                        res += '─／'
                        result.append(ps['prefix'] + res)
                    elif ps['type'] == 'bot':
                        res = '／─|'
                        while int(self.result_font.measure(res + '──|' + '─＼')) <= tw:
                            res += '──|'
                        res += '─＼'
                        result.append(ps['prefix'] + res)
                    elif ps['type'] == 'content':
                        lc, rc = ps['left_char'], ps['right_char']
                        inner = ps['inner'].rstrip(' \u3000')
                        pw = int(self.result_font.measure(lc)) + int(self.result_font.measure(rc))
                        tiw = tw - pw
                        if tiw < 0:
                            tiw = 0
                        padded = inner
                        while int(self.result_font.measure(padded + '　')) <= tiw:
                            padded += '　'
                        while int(self.result_font.measure(padded + ' ')) <= tiw:
                            padded += ' '
                        result.append(ps['prefix'] + lc + padded + rc)
                    else:
                        result.append(ps['orig'])
                return result

            def _process_normal(box_lines: list[str]) -> list[str] | None:
                """處理普通對話框"""
                parsed: list[dict] = []
                for bl in box_lines:
                    if not bl:
                        parsed.append({'type': 'orig', 'orig': bl})
                        continue
                    bl_n = bl.rstrip('\r\n \u3000')
                    matched_border = False
                    for bc in ['￣', '＿']:
                        bm_list = list(re.finditer(f'({re.escape(bc)}{{3,}})', bl_n))
                        if not bm_list:
                            continue
                        for bm in reversed(bm_list):
                            rp = bl_n[bm.end():]
                            if len(rp.strip(' \u3000')) <= 4:
                                lp = bl_n[:bm.start()]
                                parsed.append({'type': 'border', 'left': lp,
                                               'char': bc, 'right': rp, 'orig': bl_n})
                                matched_border = True
                                break
                        if matched_border:
                            break
                    if matched_border:
                        continue
                    cm = re.search(r'^(.*?[^ \u3000.])([ \u3000.]+)([^ \u3000.]{1,3})$', bl_n)
                    if cm:
                        rc = cm.group(3)
                        if any(c in rc for c in '│｜|〉》>）)ノﾉ＼ヽ｝}］]'):
                            lt = re.sub(r'[ \u3000.,]+$', '', cm.group(1))
                            parsed.append({'type': 'content', 'left': lt, 'char': ' ',
                                           'right_padding': cm.group(2), 'right': rc, 'orig': bl_n})
                            continue
                    lt = re.sub(r'[ \u3000.,]+$', '', bl_n)
                    parsed.append({'type': 'content', 'left': lt, 'char': ' ',
                                   'right_padding': '', 'right': '', 'orig': bl_n})

                max_left_w = 0
                has_right_border = False
                for p in parsed:
                    if p['type'] == 'content':
                        vm = list(valid_text_re.finditer(str(p.get('left', ''))))
                        if vm:
                            w = int(self.result_font.measure(str(p['left'])[:vm[-1].end()]))
                        else:
                            w = int(self.result_font.measure(str(p.get('left', '')).rstrip(' \u3000')))
                        if w > max_left_w:
                            max_left_w = w
                        if p.get('right'):
                            has_right_border = True
                if max_left_w == 0:
                    return None

                tw_n = max_left_w - int(self.result_font.measure('　'))
                if tw_n < 0:
                    tw_n = 0
                if has_right_border:
                    tw_n += int(self.result_font.measure('　'))
                btw = max_left_w + int(self.result_font.measure('￣'))
                if btw < 0:
                    btw = 0
                atw = btw - int(self.result_font.measure('　'))

                new_box: list[str] = []
                last_bdr = ''
                for p in parsed:
                    if p['type'] == 'border':
                        pc = str(p['char'])
                        res = str(p['left']) + pc
                        while int(self.result_font.measure(res + pc)) <= btw:
                            res += pc
                        d1 = btw - int(self.result_font.measure(res))
                        d2 = int(self.result_font.measure(res + pc)) - btw
                        if d2 < d1:
                            res += pc
                        last_bdr = res + str(p['right'])
                        new_box.append(last_bdr)
                    elif p['type'] == 'content':
                        if p.get('right'):
                            ctw = int(self.result_font.measure(last_bdr[:-1])) if last_bdr else atw
                            rp = str(p['left'])
                            if int(self.result_font.measure(rp)) < ctw:
                                while int(self.result_font.measure(rp + '　')) <= ctw:
                                    rp += '　'
                                while int(self.result_font.measure(rp + ' ')) <= ctw:
                                    rp += ' '
                                d1 = ctw - int(self.result_font.measure(rp))
                                d2 = int(self.result_font.measure(rp + ' ')) - ctw
                                if d2 < d1:
                                    rp += ' '
                            new_box.append(rp + str(p['right']))
                        else:
                            new_box.append(str(p.get('left', p.get('orig', ''))))
                    else:
                        new_box.append(str(p.get('orig', '')))
                return new_box

            # ================================================================
            # 由下而上逐框處理（避免行號偏移）
            # ================================================================
            all_boxes.sort(key=lambda x: x[0])
            count = 0
            for top_idx, bot_idx, box_type in reversed(all_boxes):
                box_lines = all_lines[top_idx:bot_idx + 1]
                if box_type == 'shout':
                    result = _process_shout(box_lines)
                elif box_type == 'slash':
                    result = _process_slash(box_lines)
                else:
                    result = _process_normal(box_lines)
                if result is not None:
                    all_lines[top_idx:bot_idx + 1] = result
                    count += 1

            if count == 0:
                show_toast("⚠️ 沒有需要調整的對話框。", color="#f39c12")
                return

            new_text = '\n'.join(all_lines)
            # 保存目前的捲動位置與游標位置
            scroll_pos = final_textbox._textbox.yview()
            cursor_pos = final_textbox._textbox.index(tk.INSERT)
            final_textbox._textbox.delete("1.0", tk.END)
            final_textbox._textbox.insert("1.0", new_text)
            # 恢復游標與捲動位置
            final_textbox._textbox.mark_set(tk.INSERT, cursor_pos)
            final_textbox._textbox.yview_moveto(scroll_pos[0])
            show_toast(f"✅ 已自動調整 {count} 個對話框！", color="#28a745")

        ctk.CTkButton(grp2, text="對話框(全)", command=adjust_all_bubbles, fg_color="#20c997", hover_color="#17a085", text_color="white", font=self.ui_small_font, width=80).pack(side="left", padx=5)

        def handle_smart_action_event(e=None):
            smart_action()
            return "break"

        container.bind("<Control-q>", handle_smart_action_event)
        container.bind("<Control-Q>", handle_smart_action_event)
        final_textbox._textbox.bind("<Control-q>", handle_smart_action_event)
        final_textbox._textbox.bind("<Control-Q>", handle_smart_action_event)

        grp3 = ctk.CTkFrame(tb_inner, fg_color="transparent")
        grp3.pack(side="right", padx=5)

        def choose_bg_color():
            color_code = colorchooser.askcolor(title="選擇背景顏色")[1]
            if color_code:
                self.bg_color = color_code
                final_textbox.configure(fg_color=color_code)
                self.save_cache()

        def choose_fg_color():
            color_code = colorchooser.askcolor(title="選擇文字顏色")[1]
            if color_code:
                self.fg_color = color_code
                final_textbox.configure(text_color=color_code)
                self.save_cache()

        def dl_html():
             raw_text = final_textbox.get("1.0", tk.END).rstrip('\n')
             if not raw_text:
                 show_toast("⚠️ 預覽視窗沒有內容！", color="#f39c12")
                 return

             if source_file:
                 init_name = os.path.basename(source_file)
             else:
                 title_val = self.doc_title.get().strip()
                 num_val = self.doc_num.get().strip()
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
                 title="儲存 HTML 檔案"
             )
             if file_path:
                 try:
                     self.write_html_file(file_path, raw_text)
                     show_toast("✅ 已儲存 HTML 檔案！", color="#28a745")
                 except Exception as e:
                     show_toast(f"❌ 無法儲存: {e}", color="#dc3545", duration=5000)

        ctk.CTkButton(grp3, text="底色", command=choose_bg_color, fg_color="#6c757d", hover_color="#5a6268", font=self.ui_small_font, width=45).pack(side="left", padx=2)
        ctk.CTkButton(grp3, text="文字色", command=choose_fg_color, fg_color="#17a2b8", hover_color="#138496", font=self.ui_small_font, width=45).pack(side="left", padx=2)
        ctk.CTkButton(grp3, text="💾 儲存", command=dl_html, fg_color="#28a745", hover_color="#218838", font=self.ui_small_font, width=60).pack(side="left", padx=5)
        _close_text = "↩ 返回" if use_tab else "✖ 關閉"
        ctk.CTkButton(grp3, text=_close_text, command=close_action, fg_color="#dc3545", hover_color="#c82333", font=self.ui_small_font, width=60).pack(side="left", padx=5)

        search_frame = ctk.CTkFrame(container, fg_color="transparent")
        search_var = tk.StringVar()
        search_entry = ctk.CTkEntry(search_frame, textvariable=search_var, placeholder_text="搜尋...", font=self.ui_small_font, width=200)
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
        ctk.CTkButton(search_frame, text="下一個", command=find_next, font=self.ui_small_font, width=60).pack(side="left", padx=5)

        def search_dice(event=None):
            search_var.set("1D10:10")
            if not search_frame.winfo_ismapped():
                search_frame.pack(fill="x", padx=10, pady=(0, 5), before=final_textbox)
            find_next()
        ctk.CTkButton(search_frame, text="🎲 1D10:10", command=search_dice, fg_color="#f39c12", hover_color="#d68910", font=self.ui_small_font, width=80).pack(side="left", padx=5)

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

        final_textbox.pack(fill="both", expand=True, padx=10, pady=10)
        final_textbox.insert("1.0", text)

        if use_tab:
            self.edit_tab_textbox = final_textbox
            # 在內嵌模式下，將快捷鍵綁定到主視窗（因為 Frame.bind 無法捕捉子元件鍵盤事件）
            self.bind("<Control-f>", toggle_search)
            self.bind("<Control-F>", toggle_search)
            self.bind("<Control-s>", save_shortcut)
            self.bind("<Control-S>", save_shortcut)
            self.bind("<Control-q>", handle_smart_action_event)
            self.bind("<Control-Q>", handle_smart_action_event)
            self.bind("<Escape>", lambda e: close_action())
            # 記錄解綁函式，供 switch_mode 離開 edit 時清除
            self._edit_tab_unbind = lambda: [
                self.unbind("<Control-f>"), self.unbind("<Control-F>"),
                self.unbind("<Control-s>"), self.unbind("<Control-S>"),
                self.unbind("<Control-q>"), self.unbind("<Control-Q>"),
                self.unbind("<Escape>")
            ]
            self.switch_mode("edit")

        # Auto-scroll to specified line
        if scroll_to_line is not None:
            def _scroll():
                target = f"{scroll_to_line}.0"
                final_textbox._textbox.see(target)
                final_textbox._textbox.mark_set(tk.INSERT, target)
                # Select the entire line for visibility
                line_end = f"{scroll_to_line}.end"
                final_textbox._textbox.tag_add(tk.SEL, target, line_end)
            (modal or self).after(100, _scroll)

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
