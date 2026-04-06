# AA 漫畫翻譯輔助工具 - 模組套件
#
# 純邏輯模組（無 UI 依賴，PyQt6 遷移時零修改）：
#   constants         - 預設正規式與顏色常數
#   font_measure      - FontMeasurer Protocol（字寬量測抽象層）
#   html_io           - HTML 讀寫
#   settings_manager  - 設定 / 快取管理（AppSettings / AppCache dataclass）
#   text_extraction   - 文本提取演算法
#   translation_engine- 翻譯替換引擎（含 Auto-Padding）
#   bubble_alignment  - 對話框對齊演算法（三種框型）
#   url_fetcher       - HTTP 抓取 + 頁面 HTML 解析
#
# UI 模組（依賴 customtkinter，遷移時需重寫）：
#   ui_result_modal   - 最終結果預覽視窗
