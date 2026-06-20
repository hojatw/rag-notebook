# NotebookLM 風格 RAG POC

這是一個單機 FastAPI 概念驗證專案，用來建立 NotebookLM 風格工作區：將來源整理到
**notebooks** 中，根據選取的來源進行 grounded chat，並把值得保留的回答或工具產出釘選成筆記。

App 使用 FastAPI、Jinja2、HTMX、Alpine.js、SQLite、Chroma 與本機檔案上傳。沒有 frontend build step、npm 或 CDN 依賴。

## 狀態

這是概念驗證，不是可直接用於正式環境的服務。設定真正的 `NOTEBOOKLM_SECRET`
之後，它適合本機實驗與小型可信任的單機部署，但尚未針對直接暴露在公開網際網路的情境完整強化。

## 快速開始

```bash
cd notebooklm-rag-poc
./setup.sh
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
```

開啟 `http://127.0.0.1:8000` 並登入：

- Admin: `admin` / `admin123`
- User: `user` / `user123`

Demo 帳號與 insecure development secret 只供本機開發使用。在任何暴露於網路的部署前，請先變更或移除 demo 帳號。

## Docker

```bash
cp .env.example .env       # 然後填入 NOTEBOOKLM_SECRET
docker compose up --build -d
docker compose logs -f
```

Docker Compose 需要 `.env` 中有 `NOTEBOOKLM_SECRET`；缺少時 app 會 fail
closed。Compose file 會 bind-mount `./data` 與 `./logs`，所以 rebuild 後仍會保留使用者狀態。

升級：

```bash
git pull
docker compose up --build -d
```

重設，會刪除使用者、notebooks、uploads、vectors 與 logs：

```bash
docker compose down
rm -rf data/ logs/
```

部署細節、worker 模式、logging、調參與測試指令請看
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。

## 設定 LLM

以 admin 身分登入後開啟 `/settings`。Chat 與 embeddings 都需要已設定的
OpenAI-compatible 或 Azure OpenAI endpoint。Embedding model 尚未設定前，上傳功能會停用。

儲存時，app 會 probe embedding endpoint 一次，並拒絕和既有 Chroma index 維度不符的設定。API key 會使用以
`NOTEBOOKLM_SECRET` 為基礎的 Fernet 靜態加密。

OpenAI-compatible 範例：

```text
Provider:           OpenAI-compatible
Base URL:           https://api.openai.com/v1
Embedding base URL: (blank - share the chat URL)
API key:            sk-...
Chat model:         gpt-4.1-mini
Embedding model:    text-embedding-3-small
Temperature:        0.2
Timeout seconds:    60
```

Ollama、vLLM、TEI 這類本機 OpenAI-compatible 服務會透過 `/v1` endpoint
支援。有些本機服務仍需要填入非空的 dummy API key。

選填的 embedding query/passage prefix 可支援 `multilingual-e5-large` 這類模型：搜尋 query
可用 `query: `，索引文字可用 `passage: `。Prefix 只會影響送到 embedding endpoint 的文字，不會改變儲存的 chunk。

## 你會得到什麼

- **Notebook workspace：** 每個 notebook 都有自己的來源、對話、釘選筆記與工具產出。
- **Sources pane：** 拖放上傳、索引狀態輪詢、重新索引/刪除、來源預覽抽屜、citation-to-chunk 高亮。
- **Grounded chat：** 串流回答、Markdown 轉譯、引用來源、複製/匯出、追問 chip、起始問題、中文 IME-safe 輸入，以及繁中 UI。
- **Studio tools：** briefing strip、來源比較、會議記錄、學習指南、FAQ、時間軸、翻譯，以及手動存成筆記流程。
- **Hybrid retrieval：** query rewrite、Chroma vector search、SQLite keyword search、LLM reranking、abstain threshold 與每則訊息的 retrieval debug details。
- **Admin surfaces：** 使用者管理、vector-index console、LLM settings、audit trail，以及支援 retrieval profiles、run comparison、exports、調參指南的 in-deployment Eval Workbench。
- **Governance backend：** 精簡 LLM usage 與 safety-event telemetry，不把 prompts、來源文字、retrieved snippets、模型輸出或 API keys 複製到 governance metadata。
- **支援來源格式：** PDF、TXT、Markdown、DOCX、HTML、字幕（`.srt` / `.vtt`）。
- **持久化：** `data/` 下的 SQLite metadata、本機 uploads、Chroma vectors，以及 `logs/` 下的輪替 logs。

## 文件導覽

- [`docs/ROADMAP.md`](docs/ROADMAP.md) - 產品/admin roadmap：UX、Eval Workbench、AI governance、LLM operations、來源格式支援與新 AI 功能。
- [`docs/PRODUCT_WHITEPAPER.zh-TW.md`](docs/PRODUCT_WHITEPAPER.zh-TW.md) - 客戶向繁中產品白皮書。
- [`docs/RETRIEVAL.md`](docs/RETRIEVAL.md) - 檢索 pipeline、ranking、reranking、eval workflow 與調參旋鈕。
- [`docs/QUALITY.md`](docs/QUALITY.md) - retrieval 與 answer-quality backlog。
- [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) - performance 與 scalability backlog。
- [`docs/SECURITY.md`](docs/SECURITY.md) - security policy 與 dependency-audit triage。
- [`docs/SCHEMA.md`](docs/SCHEMA.md) - SQLite schema reference。
- [`docs/UI.md`](docs/UI.md) - frontend design contract 與 component conventions。
- [`docs/ROUTES.md`](docs/ROUTES.md) - 完整 HTTP route reference。
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) - setup、testing、logging、tuning、deployment notes 與 repository layout。
- [`docs/SPREADSHEET_INGESTION.md`](docs/SPREADSHEET_INGESTION.md) - 試算表 ingestion design notes。

## 開發檢查

```bash
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
git diff --check
```

如果修改 retrieval，且目前有可用 LLM 設定，也請執行 eval harness：

```bash
.venv/bin/python -m tests.eval_retrieval
.venv/bin/python -m tests.eval_retrieval --no-rerank
```

## 已知後續事項

- 沒有 offline embedding fallback：接受上傳前必須先設定 embedding model。
- UI strings 仍 hardcoded zh-TW；i18n work 追蹤在 `ROADMAP.md` U15a/U15b。
- Admin LLM settings 目前仍是單一全域設定；診斷測試與多 profile 安全切換追蹤在 `ROADMAP.md` O1。
- 新來源格式支援應先做 ingestion diagnostics，再做 Q&A 試算表、SSRF-safe Web URL ingestion、PPTX text-first ingestion（`ROADMAP.md` A6a/A6c/A6/A6b）。
- Keyword search 仍使用 SQLite `LIKE`；FTS5 + BM25 追蹤在 `docs/QUALITY.md` / `docs/PERFORMANCE.md`。

## License

此專案採 MIT License 授權。請見 [LICENSE](LICENSE)。
