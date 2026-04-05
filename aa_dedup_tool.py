import customtkinter as ctk
import tkinter as tk
import re

class AADedupTool(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title("文字處理工具")
        self.geometry("600x650")
        self.transient(parent)
        
        self.ui_font = ctk.CTkFont(family="Microsoft JhengHei", size=14)
        
        # UI Layout
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        
        # Regex display frame
        top_frame = ctk.CTkFrame(self)
        top_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 0))
        
        ctk.CTkLabel(top_frame, text="目前載入的正規表達式 (僅供檢視):", font=ctk.CTkFont(family="Microsoft JhengHei", size=13, weight="bold")).pack(anchor="w", padx=5, pady=(5, 0))
        
        self.info_text = ctk.CTkTextbox(top_frame, height=90, wrap="word", fg_color="#1e2b3c", text_color="#a0aab5", font=ctk.CTkFont(family="Consolas", size=12))
        self.info_text.pack(fill="x", padx=5, pady=5)
        self.update_info_text()
        
    def update_info_text(self):
        regex_content = f"【基本分段 Base Regex】\n{self.parent.current_base_regex}\n\n"
        regex_content += f"【無效符號 Invalid Regex】\n{self.parent.current_invalid_regex}\n\n"
        regex_content += f"【獨立符號 Symbol Regex】\n{self.parent.current_symbol_regex}"
        
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", tk.END)
        self.info_text.insert("1.0", regex_content)
        self.info_text.configure(state="disabled")
        
        # Output frame
        left_frame = ctk.CTkFrame(self)
        left_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        ctk.CTkLabel(left_frame, text="原始文字 (每行獨立):", font=self.ui_font).pack(anchor="w", padx=5, pady=2)
        self.input_text = ctk.CTkTextbox(left_frame, wrap="none", undo=True)
        self.input_text.pack(fill="both", expand=True, padx=5, pady=5)
        
        right_frame = ctk.CTkFrame(self)
        right_frame.grid(row=1, column=1, sticky="nsew", padx=10, pady=10)
        
        right_top = ctk.CTkFrame(right_frame, fg_color="transparent")
        right_top.pack(fill="x", padx=5, pady=2)
        ctk.CTkLabel(right_top, text="去重複後結果:", font=self.ui_font).pack(side="left")
        
        self.count_label = ctk.CTkLabel(right_top, text="行數: 0", font=self.ui_font, text_color="#17a2b8")
        self.count_label.pack(side="right")
        
        self.output_text = ctk.CTkTextbox(right_frame, wrap="none", fg_color="#2a3b4c")
        self.output_text.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Bottom controls
        self.status_label = ctk.CTkLabel(self, text="", font=self.ui_font, text_color="#e67e22")
        self.status_label.grid(row=2, column=0, columnspan=2, pady=(10, 0))
        
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)
        
        btn_dedup = ctk.CTkButton(btn_frame, text="🚀 移除完全重複行", command=self.dedup_lines, fg_color="#28a745", hover_color="#218838", font=self.ui_font, width=150, height=40)
        btn_dedup.pack(side="left", padx=5)
        
        btn_extract = ctk.CTkButton(btn_frame, text="🔍 提取裝飾字元", command=self.extract_symbols, fg_color="#e67e22", hover_color="#d35400", font=self.ui_font, width=150, height=40)
        btn_extract.pack(side="left", padx=5)
        
        btn_copy = ctk.CTkButton(btn_frame, text="📋 複製結果", command=self.copy_result, fg_color="#007bff", font=self.ui_font, height=40, width=100)
        btn_copy.pack(side="left", padx=5)

    def dedup_lines(self):
        input_content = self.input_text.get("1.0", tk.END).strip("\n")
        if not input_content:
            return
            
        lines = input_content.split("\n")
        seen = set()
        deduped_lines = []
        
        for line in lines:
            # keeping exact line string including spaces
            if line not in seen:
                seen.add(line)
                deduped_lines.append(line)
                
        result = "\n".join(deduped_lines)
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", result)
        self.count_label.configure(text=f"行數: {len(deduped_lines)}")
        self.status_label.configure(text=f"已成功移除重複行，共保留 {len(deduped_lines)} 行", text_color="#28a745")
        
    def copy_result(self):
        result = self.output_text.get("1.0", tk.END).strip("\n")
        if result:
            self.clipboard_clear()
            self.clipboard_append(result)
            self.status_label.configure(text="已複製到剪貼簿", text_color="#007bff")

    def extract_symbols(self):
        input_content = self.input_text.get("1.0", tk.END).strip("\n")
        if not input_content:
            return
            
        lines = input_content.split("\n")
        
        # This regex removes numbers (0-9) and common separators/punctuation at the start or anywhere if typical of the format 001|
        # Also strips leading/trailing spaces.
        cleaned_symbols = []
        for line in lines:
            # specifically for the format "001|,.癶", remove digits and a pipe/comma/period if it acts as a separator
            cleaned = re.sub(r'^\d+[\s|,\.]*', '', line)
            cleaned = cleaned.strip()
            if cleaned:
                cleaned_symbols.append(cleaned)
                
        if not cleaned_symbols:
            self.output_text.delete("1.0", tk.END)
            self.output_text.insert("1.0", "未找到任何裝飾字元")
            self.count_label.configure(text="行數: 0")
            self.status_label.configure(text="沒有提取出有效的符號！", text_color="#dc3545")
            return
            
        result_text = "\n".join(cleaned_symbols)
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", result_text)
        self.count_label.configure(text=f"行數: {len(cleaned_symbols)}")
        
        # Add to main application's invalid_regex
        current_invalid = self.parent.current_invalid_regex
        
        # Collect unique characters from all extracted symbols
        unique_new_chars = set()
        for sym in cleaned_symbols:
            for char in sym:
                unique_new_chars.add(char)
        
        added_count = 0
        added_items = []
        
        first_bracket = current_invalid.find('[')
        last_bracket = current_invalid.rfind(']')
        
        if first_bracket != -1 and last_bracket != -1 and first_bracket < last_bracket:
            prefix = current_invalid[:first_bracket+1]
            existing_chars = current_invalid[first_bracket+1:last_bracket]
            suffix = current_invalid[last_bracket:]
            
            for char in unique_new_chars:
                # If char requires escaping in bracket, you could re.escape it, but usually standard chars are fine.
                # However, checking existence literally works, unless it's a structural bracket character.
                # Specifically checking ] or \ could be tricky, but those shouldn't be extracted normally.
                if char not in existing_chars:
                    added_items.append(char)
                    
            if added_items:
                added_str = "".join(added_items)
                if existing_chars.endswith('-'):
                    existing_chars = existing_chars[:-1] + added_str + "-"
                else:
                    existing_chars += added_str
                
                current_invalid = prefix + existing_chars + suffix
                added_count = len(added_items)
        else:
            # Fallback for unexpected regex format
            for sym in cleaned_symbols:
                parts = current_invalid.split('|')
                if sym not in parts and re.escape(sym) not in parts:
                    parts.append(sym)
                    current_invalid = "|".join(parts)
                    added_count += 1
                    added_items.append(sym)
                
        if added_count > 0:
            self.parent.current_invalid_regex = current_invalid
            self.parent.save_regex_to_settings()  # 寫回 AA_Settings.json
            self.update_info_text()  # Refresh the UI regex display
            self.status_label.configure(text=f"成功加入了 {added_count} 個新裝飾字元到過濾名單中！", text_color="#28a745")
        else:
            self.status_label.configure(text="提取的裝飾字元都已經存在於過濾名單中了。", text_color="#17a2b8")
