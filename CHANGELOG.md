# 變更紀錄（Changelog）

本檔記錄本專案對使用者/維運者有感的變更。
格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版號遵循 [語意化版本](https://semver.org/lang/zh-TW/)。專案仍在 `0.x` 階段：
新功能進 MINOR，純修正與依賴更新進 PATCH。

版號的單一事實來源是 repo 根目錄的 `VERSION` 檔（見 `app/version.py`）。

## [未發布]

### 新增

- **Chroma embedding 維度重設 workaround（P0 暫行工具）**：新增
  `scripts/reset_chroma_dimension.py`。工具預設 dry-run，只有同時指定 `--apply` 與
  `--services-stopped` 才會變更資料；執行前自動備份 SQLite + Chroma，重建 collection，
  回填可安全沿用的目標維度 vectors，並把仍是舊維度的 indexed 來源改為待 Reindex。
  Docker 與本機操作步驟見 `docs/DEVELOPMENT.md`。

- **O0 Phase B — `stale_embedding` 來源狀態與 startup sync 維度守衛**：新增
  `app/index_migration.py`，依照既有向量的維度把來源分類（可沿用／只因鎖定而失敗
  可復原／需重新 embed／不受影響）。維度不符的來源改為新狀態 `stale_embedding`，
  被所有 `status = 'indexed'` 查詢排除，因此 startup sync 不會把舊維度向量重新
  寫回去、把剛重設的 collection 又鎖回舊維度。狀態獨立於 `failed`，因為「檔案壞了」
  與「需要重新 embed」是兩回事，修法是「重新索引」而非重新上傳。
  `sync_from_sqlite()` 另加一層守衛：維度不符的分塊會被跳過而非寫入，並在回傳值
  多一個 `skipped_dimension` 計數與一筆 warning log。

### 修正

- **`/admin/index` 不再把「清除／重建」當成更換 embedding 維度的方法**：原本空集合會顯示
  「尚未鎖定維度」，而清除的確認對話框承諾「之後執行重建即可恢復搜尋」——在維度變更的情境
  下兩者都是錯的，正好把管理員推進 P0 陷阱。現在維度統計、清除確認框、清除後訊息都會說明
  清除不解除維度鎖定，並新增「更換 embedding 維度」區塊指向上述 workaround。

- **簡報來源 `.pptx`（A6b Phase 1，只讀文字）**：投影片標題與內文、表格、
  備忘稿各自成為分段（`slide N` / `slide N table K` / `slide N notes`），引用可以指回
  特定投影片。群組起來的文字方塊會遞迴讀取——扁平走訪會讓整張投影片索引成空白。
  備忘稿不會和投影片內文混進同一個分塊（兩者語境不同）。只有圖片、圖表或 SmartArt
  的投影片會在診斷中明確標示未被索引，並說明需要 OCR 或影像理解能力，而不是靜默留白。
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

### 依賴

- 新增 `openpyxl`（A6c）、`python-pptx`（A6b，會連帶帶入 Pillow 與 XlsxWriter），
  並把原本是傳遞相依的 `charset-normalizer` 明確 pin 住（A6c 直接 import 它做編碼偵測）。

### 變更

- 決策紀錄：`P2-1`（SQLite 向量複本）裁定維持現狀並寫下重啟條件；
  `Q1-1`（RRF）記為排序決策並列出重新校準的前置條件。
  詳見 `docs/PERFORMANCE.md`、`docs/QUALITY.md`。

### 已知問題

- **P0 — `/admin/index` Clear 不會重設 Chroma collection 維度：**現行實作只執行
  `collection.delete(ids=...)`；即使 vector count 已是 0，collection schema 仍可能鎖在
  舊維度。設定頁因看不到任何 stored embedding 會誤判為未鎖定，新維度第一次 upsert
  才以 `Collection expecting embedding with dimension of X, got Y` 失敗。永久修正與
  multi-process cache/worker 協調列為 `ROADMAP.md` O0；修正完成前使用上述 workaround。

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
