---
name: extraction-testcase-maintenance
description: 維護提取演算法（aa_tool/text_extraction.py 的 extract_text／format_extraction_output 等）的 testcase。當使用者更新 testcase/failcase.txt 的 fail case、或修改提取演算法且修復後通過驗證時使用，負責把通過的 failcase 移入 base.txt、清空 failcase.txt、重新產生 base_result.txt。
---

# 提取演算法 Testcase 維護流程

當使用者更新 [testcase/failcase.txt](../../../testcase/failcase.txt) 中的 fail case，且修復後通過驗證時，主動依序執行以下步驟（不需等使用者再次指示，驗證通過後即自動執行）：

## 1. 將通過的 failcase 移入 base.txt

寫入 [testcase/base.txt](../../../testcase/base.txt)：
- 漏抓案例（應被提取的）→ 加入「# 過去的failcase(漏抓)」區段
- 誤抓案例（不應被提取的）→ 加入「# 過去的failcase(誤抓)」區段，每條 AA source 後一行標註不應抓出的 token

## 2. 清空 failcase.txt

清空 [testcase/failcase.txt](../../../testcase/failcase.txt)，保留「開頭的 `#` 提示行」以及 `漏抓\n\n\n誤抓\n` 標題框架（提示行用來在下次處理 fail case 時提醒呼叫本 skill，不可刪除）。

## 3. 重新產生 base_result.txt

跑 `extract_text(base.txt, experimental=True)` → `format_extraction_output()` → 寫回 [testcase/base_result.txt](../../../testcase/base_result.txt)。
