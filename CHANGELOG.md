# 變更紀錄（Changelog）

本檔記錄本專案對使用者/維運者有感的變更。
格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版號遵循 [語意化版本](https://semver.org/lang/zh-TW/)。專案仍在 `0.x` 階段：
新功能進 MINOR，純修正與依賴更新進 PATCH。

版號的單一事實來源是 repo 根目錄的 `VERSION` 檔（見 `app/version.py`）。

## [未發布]

### 新增

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
