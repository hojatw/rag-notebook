# 變更紀錄（Changelog）

本檔記錄本專案對使用者/維運者有感的變更。
格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版號遵循 [語意化版本](https://semver.org/lang/zh-TW/)。專案仍在 `0.x` 階段：
新功能進 MINOR，純修正與依賴更新進 PATCH。

版號的單一事實來源是 repo 根目錄的 `VERSION` 檔（見 `app/version.py`）。

## [未發布]

### 新增

- **試算表來源 `.xlsx` / `.csv`（A6c MVP）**：問答（Q&A）表會被辨識——欄位名稱
  同義詞可在 `[spreadsheet]` 設定（支援 `客戶提問` / `回覆內容` 這類自家用語），
  無標題的兩欄表也會判為問答並標示「自動判斷」。其他形狀一律以有界的「一般
  資料列」切塊，**並在診斷中明說**語意搜尋可用、但精確篩選與加總不在支援範圍。
  每列問答一個分塊；資料列依估算 token 自動打包，過寬的單列會拆成數個部分、
  每部分重複識別欄位，避免被 embedding 靜默截斷。隱藏工作表預設略過，CSV 會
  偵測編碼（預期 Big5/CP950）與分隔符號，這些決定都會出現在攝取診斷裡。
- **攝取診斷（A6a）**：來源預覽抽屜新增「萃取診斷」，顯示 app 實際讀到多少字、
  分成幾段幾塊、用哪條萃取路徑，以及四種會影響後續判讀的警訊：幾乎沒讀到文字
  （掃描檔訊號）、PDF 退回逐頁純文字（引用只精確到頁）、分塊可能超過 embedding
  輸入上限（估算值）、有空白分段。失敗時會記錄是在哪個階段失敗（讀取／切塊／
  向量／寫入），且**失敗的來源現在也能打開抽屜查看診斷**——這是判斷「檔案沒有
  文字」還是「端點掛掉」的唯一線索。門檻可在 `[diagnostics]` 調整，只影響顯示，
  不需要重新索引。
- **產出置物架的類型徽章與篩選（U16 Phase 2）**：釘選的回答與各工具產出（來源比較、
  會議紀錄、學習指南、常見問答、時間軸、翻譯摘要）現在會記住自己的類型
  （`notes.kind`），在置物架上顯示徽章，並可依類型篩選（純前端，不發請求）。
  匯出的 Markdown 也會帶上類型。既有筆記會在啟動時分類一次：釘選回答精確判定，
  工具產出以當初的標題前綴 best-effort 回推，認不出來的維持一般筆記。
- **深色模式（U11）**：每位使用者可在 `/account` 選擇「跟隨系統 / 淺色 / 深色」，
  設定存在帳號上（`users.theme`），換裝置也會沿用。明確選淺色/深色時由伺服器渲染，
  沒有 JS 也能運作；「跟隨系統」則在首次繪製前依作業系統設定解析，不會閃白。
  實作為單一 token 覆寫層，淺色外觀完全不變。文件：`docs/UI.md` §1.1。

### 變更

- 決策紀錄：`P2-1`（SQLite 向量複本）裁定維持現狀並寫下重啟條件；
  `Q1-1`（RRF）記為排序決策並列出重新校準的前置條件。
  詳見 `docs/PERFORMANCE.md`、`docs/QUALITY.md`。

## [0.2.0] - 2026-07-25

### 新增

- **企業 SSO**（#70）：支援信任反向代理標頭（I1a）與 OIDC（I1b）兩種登入路徑，
  含 IP allowlist、角色對應、`/admin/auth` 管理頁。**預設關閉**，未啟用時登入行為不變。
  文件：`docs/AUTHENTICATION.md`、`docs/SSO_DEPLOYMENT.zh-TW.md`（維運指引）。
- **Eval Workbench E1e-2：答案品質與引用判斷**（#72）：以 LLM judge 評估答案品質與
  引用正確性，並可跨 run 比較。實作於 `app/evals.py`，測試 `tests/test_evals_judge.py`。
  規劃文件：`docs/E1E2_ANSWER_JUDGING_PLAN.md`（#71）。
- 共享 Claude Code 開發設定（#63）：committed permissions、hooks、slash commands。

### 變更

- 新增數項可調參數至 `app/config.py` 與 `config.example.toml`；預設值維持既有行為
  （由 `tests/test_config.py` 保護）。
- 資料庫 schema 以既有 idempotent migration 新增欄位/資料表，向後相容；
  `docs/SCHEMA.md` 同步更新。
- 文件更新：`docs/ROADMAP.md`、`docs/SECURITY.md`、`docs/QUALITY.md`、
  `docs/SPREADSHEET_INGESTION.md`、`docs/UI.md`、`docs/ROUTES.md`、`README.md`。

### 依賴

- fastapi 0.138.0 → 0.139.2（#64、#69）
- uvicorn 0.49.0 → 0.51.0（#68）
- pypdf 6.13.3 → 6.14.2（#65）

### 升級注意事項

- 無破壞性變更，直接部署即可；schema migration 於啟動時自動套用。
- 若要啟用 SSO，請先讀 `docs/SSO_DEPLOYMENT.zh-TW.md` 的安全契約檢查表——
  信任標頭模式在沒有正確設定反向代理與 IP allowlist 時可被偽造。

## [0.1.0] - 2026-06-25

- 首次標記版本。導入 `VERSION` 檔與執行期版本識別（`app/version.py`），
  並加上樣式化的 404／500 錯誤頁（#62）。
  此版本之前的內容為 POC 初始開發，未逐項記錄。
