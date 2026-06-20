# UX_REVIEW_GUIDE.md — how to run a UX/UI review (rubric + report format)

The durable spec for **every** UX/UI review of this app, so each pass judges by
the same directions and produces the same shaped report. The living findings log
is [`UX_REVIEW.md`](UX_REVIEW.md); this file is the method behind it.

## The six directions (judge every surface against these)

| 方向 | 意思 | 反例（要抓的） |
|---|---|---|
| **專業** | 看起來像成品、可信賴 | 露出原生英文控件、半成品占位、未對齊的色彩/間距 |
| **簡單** | 不超載、路徑短 | 一頁塞太多、要多步才到主要動作 |
| **直覺** | 不用想就會用 | 行動版要捲過整列才到主要區、隱藏的操作 |
| **明確** | 文案/狀態講清楚後果 | 截斷到無法辨識、破壞性動作無說明 |
| **一致** | 同產品同語彙 | 中英混雜、同義 pill 兩種畫法、間距/分隔線各頁不同 |
| **可預期** | 行為符合預期、可發現 | 無關閉鈕、送出無鎖、靜默截斷、機制不一致 |

技術約束與元件規範以 [`UI.md`](UI.md) 為準；UI 文案一律走 i18n（[`I18N.md`](I18N.md)），不得硬編碼。

## 走查方法

1. 起本機伺服器、以 admin 登入（驗證設定見 i18n/dev secret；worktree 需指對主 repo `.venv` + `NOTEBOOKLM_DATA_DIR`）。
2. **逐頁實際渲染截圖**，至少涵蓋：登入、筆記本列表、工作區（桌機＋375px 手機）、搜尋、設定、帳號、索引、使用者、Eval（主頁/Profiles/set/run/compare/help）、稽核、工具 modal、各空狀態。
3. 每頁對照「六大方向 + [`UI.md`](UI.md)」逐項記問題；用 text 工具（snapshot/inspect）確認文字與樣式，不只看截圖。
4. 沙箱無外網：純前端可在 preview 驗；要打 LLM/embedding 需真實 uvicorn。

## 報告格式（寫進 / 更新 `UX_REVIEW.md`）

固定結構：
1. **走查方法**（這次怎麼跑、涵蓋哪些頁）
2. **總評**（一段：整體水準 + 問題集中在哪）
3. **做得好的地方**（避免重構誤傷）
4. **嚴重度分表**，每列固定欄位：`問題 | 影響方向 | 修改建議 | 既有 backlog 對應 | 狀態`
   - 嚴重度三級：🔴 高（最傷六大方向、優先）／🟡 中／🟢 低（打磨）
   - 狀態用 checkbox：`[ ]` 未做 / `[~]` 進行中 / `[x] 日期：一句話` 已完成
   - 修掉一項就回來打勾，不要刪列（保留可追溯）
5. **優先順序建議**（影響 vs 工程量）

慣例：
- 確定要做的項目最終收斂進 [`ROADMAP.md`](ROADMAP.md)（產品面）或 [`UI.md`](UI.md)（屬規範者）；報告負責「發現與追蹤」。
- 局部貼布若沒解根因就**還原**，並把該項標為延後 + 記根因假設（見 V1/V3）。
- 每輪結束跑 `pytest`、`py_compile`；UI 變更以 preview 實測佐證。
