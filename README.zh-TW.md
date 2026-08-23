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

Demo 帳號與 insecure development secret 只供本機開發使用，絕對不可把此模式暴露於
網路。使用真正 `NOTEBOOKLM_SECRET` 的部署只會建立一次性的 bootstrap `admin`，
第一次登入必須變更密碼；詳見 [`docs/SECURITY.md`](docs/SECURITY.md)。

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

以 admin 身分登入後開啟 `/settings`。Chat 與 embedding 是彼此獨立的
OpenAI-compatible 或 Azure OpenAI connection，依部署實際使用的能力分別設定。
Embedding model 尚未設定前，上傳功能會停用；回答與其他生成式功能則需要 chat model。

儲存時，app 會 probe embedding endpoint 一次，並拒絕和既有 Chroma index 維度不符的設定。API key 會使用以
`NOTEBOOKLM_SECRET` 為基礎的 Fernet 靜態加密。

**換到維度不同的 embedding 模型**有自己的流程，不是 Clear/Rebuild：Chroma 在第一次
寫入時鎖定集合維度，刪掉 records 並不會解除。先在 `/settings` 執行「測試 embedding
模型」，再到 `/admin/index` 使用**更換 embedding 維度** — 它會換掉整個集合、保留已是
目標維度的向量，其餘標記為待重新索引。詳見
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#changing-the-embedding-dimension)。

同一頁也提供 admin-only diagnostics：chat 與 embedding model 可分開測試，
顯示 latency/status/model 摘要與 embedding dimension，並可檢查 streaming、
provider usage、JSON-following，以及選填的圖片理解能力。Diagnostics 只保存精簡
status metadata，不保存 raw prompt、model output、API key 或 raw provider payload。

設定頁有兩張彼此獨立的卡片；chat 與 embedding 可使用不同 provider、Base URL、
API key、model 與 Azure API version。OpenAI-compatible chat card 範例：

```text
Provider:           OpenAI-compatible
Base URL:           https://api.openai.com/v1
API key:            sk-...
Chat model:         gpt-4.1-mini
Temperature:        0.2
```

Embedding card 範例（本機 e5、不需要 key）：

```text
Provider:           OpenAI-compatible
Base URL:           http://10.0.0.1:8001/v1
API key:            （留白）
Embedding model:    intfloat/multilingual-e5-large
Query prefix:       query:
Passage prefix:     passage:
```

Ollama、vLLM、TEI 這類本機 OpenAI-compatible 服務會透過 `/v1` endpoint
支援。API key 在 chat 與 embedding 兩邊都是選填；留白時不會送出 auth header。

選填的 embedding query/passage prefix 可支援 `multilingual-e5-large` 這類模型：搜尋 query
可用 `query: `，索引文字可用 `passage: `。Prefix 只會影響送到 embedding endpoint 的文字，不會改變儲存的 chunk。

## 你會得到什麼

- **Notebook workspace：** 每個 notebook 都有自己的來源、對話、釘選筆記與工具產出。
- **Sources pane：** 拖放上傳、索引狀態輪詢、重新索引/刪除、來源預覽抽屜、citation-to-chunk 高亮。
- **Grounded chat：** 串流 retrieval/generation 狀態、完成後的 bounded answer classification、Markdown 轉譯、引用來源、複製/匯出、追問 chip、起始問題、中文 IME-safe 輸入，以及繁中 UI。
- **Studio tools：** briefing strip、來源比較、會議記錄、學習指南、FAQ、時間軸、翻譯，以及手動存成筆記流程。
- **Hybrid retrieval：** query rewrite、Chroma vector search、SQLite keyword search、LLM reranking、abstain threshold 與每則訊息的 retrieval debug details。
- **Notebook domain controls：** notebook owner 可維護有界的 terms、synonyms、query expansions、answer notes 與 answer policy；hints 只在 query time 生效，不需 re-index 或額外 LLM call。
- **Admin surfaces：** 使用者管理、vector-index console、LLM settings、audit trail，以及支援 retrieval profiles、answer/citation judging、E2 mode comparison、exports 與調參指南的 in-deployment Eval Workbench。
- **Governance backend：** 精簡 LLM usage 與 safety-event telemetry，不把 prompts、來源文字、retrieved snippets、模型輸出或 API keys 複製到 governance metadata。
- **支援來源格式：** PDF、TXT、Markdown、DOCX、HTML、簡報（`.pptx`，只讀文字：標題、內文、表格、備忘稿）、字幕（`.srt` / `.vtt`）、試算表（`.xlsx` / `.csv`——問答表會被辨識，其他形狀以有界的資料列分塊攝取，詳見 [docs/SPREADSHEET_INGESTION.md](docs/SPREADSHEET_INGESTION.md)）。
- **持久化：** `data/` 下的 SQLite metadata、本機 uploads、Chroma vectors，以及 `logs/` 下的輪替 logs。

## 文件導覽

- [`docs/ROADMAP.md`](docs/ROADMAP.md) - 產品/admin roadmap：UX、Eval Workbench、AI governance、LLM operations、來源格式支援與新 AI 功能。
- [`docs/PRODUCT_WHITEPAPER.zh-TW.md`](docs/PRODUCT_WHITEPAPER.zh-TW.md) - 客戶向繁中產品白皮書。
- [`docs/RETRIEVAL.md`](docs/RETRIEVAL.md) - 檢索 pipeline、ranking、reranking、eval workflow 與調參旋鈕。
- [`docs/AUTHENTICATION.md`](docs/AUTHENTICATION.md) - 本機帳號、企業 SSO（信任標頭 / OIDC）、AD 整合與角色對應。
- [`docs/SSO_DEPLOYMENT.zh-TW.md`](docs/SSO_DEPLOYMENT.zh-TW.md) - 維運指引：反向代理／OIDC 設定範例、安全契約檢查表與認證測試計劃。
- [`docs/I18N.md`](docs/I18N.md) - UI 文案的 i18n 目錄（`t()` / `window.I18N`）、新增字串／語系的方式與既有例外。
- [`docs/DEPLOYMENT_CONTEXT.md`](docs/DEPLOYMENT_CONTEXT.md) - 形塑既有取捨的部署事實（推論端不可改、使用規模、語料與語言）。
- [`docs/RELEASE.md`](docs/RELEASE.md) - 版號、CHANGELOG 與 CI 的流程慣例。
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

如果修改 retrieval，且目前有設定 embedding model，也請執行 eval harness；chat
model 與 API key 都是選填：

```bash
.venv/bin/python -m tests.eval_retrieval
.venv/bin/python -m tests.eval_retrieval --no-rerank
```

## 已知後續事項

- 沒有 offline embedding fallback：接受上傳前必須先設定 embedding model。
- 共用／高頻 UI copy 已使用 `zh-TW` catalog（U15a）；既有 template 仍有 inline
  zh-TW 文案，需在 U15b 加入 `en` 與 admin/per-user locale controls 前完成盤點與抽取。
- Admin LLM settings 目前仍是單一全域設定；chat/embedding diagnostics 已完成，多 profile 管理與安全切換仍追蹤在 `ROADMAP.md` O1 Phase 2。
- Ingestion diagnostics、Q&A/一般資料列試算表與 PPTX text-first ingestion 已完成；下一個來源格式是具 SSRF 防護的 Web URL ingestion（`ROADMAP.md` A6），OCR/vision 仍依模型能力與客戶需求投入。
- Keyword search 仍使用 SQLite `LIKE`；FTS5 + BM25 追蹤在 `docs/QUALITY.md` / `docs/PERFORMANCE.md`。

## License

此專案採 MIT License 授權。請見 [LICENSE](LICENSE)。
