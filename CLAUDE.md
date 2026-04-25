# AA 創作翻譯輔助小工具 — AI 協作規則

## 查閱 SPEC 文件

修改任何功能前，若不確定要改哪些地方，請先閱讀 [AATool_Technical_Spec.md](AATool_Technical_Spec.md)。
SPEC 文件記錄了所有核心模組、function 對照表與工作流程，可快速定位目標程式碼。

## 大更新前先 commit 備份

進行「大更新」之前，請先 commit 當前狀態作為備份點，方便失敗時回滾。
- 大更新定義：跨多個檔案的重構、新增主要功能、UI 架構變動、遷移框架等
- 小更新（單檔修錯字、調整參數、修 bug）不需要先 commit

## 修改後更新 SPEC

完成任何程式碼修改後，必須同步更新 [AATool_Technical_Spec.md](AATool_Technical_Spec.md)，將異動內容反映至對應章節。
- 新增 function → 在第 4 節「核心模組與對應 Function 解析」補充對應條目
- 修改行為邏輯 → 更新對應章節的說明
- 新增狀態欄位或設定 → 更新第 1 節「系統架構與技術棧」
