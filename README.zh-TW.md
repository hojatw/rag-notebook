# NotebookLM 風格 RAG POC

這是一個單機 FastAPI 概念驗證專案，用來建立 NotebookLM 風格的工作區：將來源整理到 **notebooks** 中，根據你選取的來源進行 grounded chat，並釘選值得保留的回答。

## 狀態

這是概念驗證，不是可直接用於正式環境的服務。設定真正的 `NOTEBOOKLM_SECRET` 之後，它適合本機實驗與小型單機部署，但仍有幾項正式環境強化工作尚未完成（CSRF 保護、串流回應）。請參考[已知後續事項](#已知後續事項)。

## 你會得到什麼

- **Notebook 首頁格狀列表。** 每個 notebook 都有自己的來源、對話與釘選筆記。
- **每個 notebook 都有三欄式工作區**：
  - **Sources**（左側）：拖放上傳、自動輪詢索引狀態、針對單一來源重新索引/刪除、**點擊任一已索引來源即可開啟 chunk 預覽抽屜**。
  - **Chat**（中間）：有來源根據的對話，包含對話切換器（每列可刪除、可**匯出 Markdown**）、Markdown 轉譯回答（每則回答可一鍵**複製**），以及行內 `[1]` `[2]` 引用 chip，點擊會打開來源預覽抽屜並捲動、高亮被引用的那個分塊。提問經由 HTMX 只更新訊息區（不再整頁重載）；每次成功回答後會建議 2–3 個**追問問題 chip**（延遲生成、逐訊息快取）。空狀態會顯示**建議問題**——由 LLM 根據來源一鍵產生的開場問題，來源完成索引後自動重新整理（24 小時快取）。Enter 送出、Shift+Enter 換行、中文選字 Enter 不會誤送。介面為繁體中文。
  - **Studio**（右側）：NotebookLM 風格工作區，分為環境脈絡、工具啟動器、產出架三部分（Studio IA 重構，見 `ROADMAP.md` U16）。
      - *簡報細條*：跨來源的一段式綜合摘要,以可展開的單行細條呈現,第一次檢視 notebook 時自動產生並快取 24 小時。可視需要按 *Regenerate*。跨分頁/相鄰來源完成時的並行產生，會由共享的 SQLite 鎖去重，所以一次上傳 5 個檔案只會呼叫 LLM 一次，而不是五次（可跨多個 worker 運作）。
      - *工具*：磚格啟動器,每個磚會在 preview-modal 中開啟設定、執行,並把結果連同**手動「存成筆記」**按鈕一起顯示(由使用者決定哪些落到產出架,不自動存)：
          - *來源比較*：選擇 2 個以上已索引來源（可選擇性提供聚焦提示）→ Shared / Distinct / Contradictions 的 Markdown 報告。
          - *會議記錄整理*：選擇一個已索引來源（會議逐字稿）→ 結構化會議記錄（主題 / 決議 / 行動項目 / 待辦 / 未決事項）；非會議來源會顯示模型判斷的理由,且不提供存檔。
          - *學習指南 / 常見問答 / 時間軸*：跨整個 notebook 的來源摘要生成（A4）。
          - *翻譯摘要*：把單一來源的摘要翻譯成目標語言（A5）。
      - *產出與筆記架*：將助理回答 *Pin* 到可收合筆記中（移除筆記會自動取消釘選原始訊息）；你存下的每個工具結果也會落在這裡；筆記可**行內編輯**；可**一鍵匯出全部筆記為 Markdown**。
  - **單一來源摘要**：每個上傳來源在索引完成後，都會自動產生 2 到 4 句 TL;DR，顯示在預覽抽屜頂端，並作為 Briefing / Compare / 產出工具的精簡脈絡重用。
- **混合式檢索**：query rewriting、Chroma vector search、SQLite keyword matching 與 LLM reranking。低於可設定信心閾值時，模型會被要求避免回答，而不是產生幻覺。完整 pipeline、調校旋鈕與 eval 工作流程請見 [`RETRIEVAL.md`](docs/RETRIEVAL.md)。
- **每則訊息的除錯窗格**：聊天回答附有可收合的「📊 N chunks · retrieved Xms · generated Yms · top score Z」徽章，點開後可看到每個引用的 vector / keyword / rerank / final 分數表格。
- **檢索 eval harness**（`tests/eval_retrieval.py`），包含 demo notebook 的起始問題，方便衡量 query rewrite / hybrid scoring / rerank 變更（recall@k、MRR）。
- **多使用者**，包含雜湊密碼與嚴格的每使用者/每 notebook 隔離。管理員可在 `/admin/users` 管理使用者帳號；任何已登入使用者都可在 `/account` 修改自己的密碼。
- **OpenAI-compatible**（包含本機 Ollama / vLLM / TEI）與 **Azure OpenAI** chat + embedding providers，由管理員在 `/settings` 設定。Chat 與 embedding endpoint 可透過選填的 **Embedding base URL** 欄位放在不同服務。**API keys 會使用 Fernet 靜態加密**（以 `NOTEBOOKLM_SECRET` 執行 PBKDF2-SHA256）。儲存時會 probe embedding endpoint 一次；若與現有 Chroma index 維度不符，會用清楚的「Clear at /admin/index first」訊息拒絕。
- **管理員向量索引主控台**位於 `/admin/index`：SQLite ↔ Chroma 漂移報告、手動 *Rebuild* 與 *Clear*。
- **啟動時只同步差異到 Chroma**：只 upsert 缺少的 chunks 並刪除孤兒 vectors；狀態相同的重啟幾乎即時完成。
- **來源格式**：PDF、TXT、Markdown、DOCX、HTML、字幕(SRT/VTT，會解析成乾淨逐字稿，A7)。
- **持久化**：SQLite 儲存 metadata、本機檔案系統儲存 uploads、Chroma 儲存 vectors。
- **防禦性列表上限**（sources 200、conversations 50、messages 200、notes 50），UI 會提供截斷提示。
- **Logging** 到 stdout 與 `logs/app.log`，並支援輪替。

前端維持 server-rendered（Jinja templates），並少量使用 Alpine.js、HTMX、marked 與 DOMPurify（全部自託管於 `app/static/vendor/`）- 沒有 build step、沒有 npm、沒有 CDN 依賴。

## 執行

本機 quickstart 可以選擇使用內建的不安全開發 secret。請勿在暴露於網路的部署中使用此模式。

```bash
cd notebooklm-rag-poc
./setup.sh                                              # 建立與 Docker 一致的 Python 3.12 .venv
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1 .venv/bin/uvicorn app.main:app --reload --port 8000
```

`setup.sh --force` 會先清除任何既有 `.venv`。此 script 預設使用 `python3.12`；若你的 Python 3.12 binary 名稱不同，可用 `PYTHON_BIN=/path/to/python3.12 ./setup.sh` 指定。

預設情況下 web process 會同時 inline 處理匯入佇列，所以上面單一 `uvicorn` 指令就會像以前一樣處理上傳。若要採用正式環境的拆分方式、把 PDF 抽取/embedding 移出 web process，請在 web app 設定 `NOTEBOOKLM_INLINE_WORKER=0` 並另外執行一個專用 worker（Docker Compose 會自動這麼做 — 見 `worker` service）：

```bash
.venv/bin/python -m app.worker                         # 專用匯入 worker（共用 data/app.sqlite3）
```

開啟 `http://127.0.0.1:8000` 並登入：

- Admin: `admin` / `admin123`
- User: `user` / `user123`

這些 demo 帳號只供本機開發使用。在將 app 暴露於網路前，請先變更或移除它們。

第一次啟動時，任何 legacy data 都會遷移到名為 *My Notebook* 的預設 notebook。新使用者一開始會看到空的 notebook grid，並可用 **+ New notebook** 建立自己的 notebook。

### Docker（建議用於部署）

```bash
cp .env.example .env       # 然後填入 NOTEBOOKLM_SECRET
docker compose up --build -d
docker compose logs -f
```

Docker Compose 需要 `.env` 中有 `NOTEBOOKLM_SECRET`；缺少時 app 會 fail closed。可用 `python -c "import secrets; print(secrets.token_urlsafe(48))"` 產生。

Compose file 會從 repo root bind-mount `./data`（SQLite + uploads + Chroma index）與 `./logs`（輪替 app log），因此 `docker compose down && docker compose up --build` 會保留所有使用者狀態。備份只需要執行 `tar czf data-$(date +%F).tar.gz data/`。

**升級流程**（零資料遺失）：

```bash
git pull
docker compose up --build -d
```

**重設**（注意：會刪除使用者、notebooks、vectors、uploads）：

```bash
docker compose down
rm -rf data/ logs/
```

映像檔使用與本機開發一致的 Python 3.12 runtime。預設 port 是 8000（可在 `.env` 中用 `HOST_PORT` 覆寫）。

### Python Runtime

本機開發與 Docker 都使用 Python 3.12。讓兩者一致可以避免 `onnxruntime` 這類 native dependency 在特定 Python / 平台組合上缺 wheel 的問題；ChromaDB 會將 `onnxruntime` 宣告為必要 dependency。repo 內的 `.python-version` 可供版本管理工具參考，但 `setup.sh` 只要求 `PATH` 上有可用的 `python3.12`。

## LLM 設定

以 admin 身分登入後，在 `/settings` 設定 LLM 連線。Chat 與 embeddings 都需要已設定的 OpenAI-compatible（或 Azure OpenAI）endpoint - embeddings 不再有 offline-hash fallback。上傳表單會在 embedding model 設定完成前停用。儲存 handler 會 probe embedding endpoint 一次，以驗證連線能力並偵測與現有 Chroma index 的維度不符。

OpenAI-compatible:

```text
Provider:           OpenAI-compatible
Base URL:           https://api.openai.com/v1
Embedding base URL: (blank — share the chat URL)
API key:            sk-...
Chat model:         gpt-4.1-mini
Embedding model:    text-embedding-3-small
Temperature:        0.2
Timeout seconds:    60
```

本機模型設定（vLLM 用於 chat + Ollama 在不同 port 提供 embeddings）：

```text
Provider:           OpenAI-compatible
Base URL:           http://localhost:8000/v1     (vLLM chat)
Embedding base URL: http://localhost:11434/v1    (Ollama embeddings)
API key:            EMPTY                        (any non-empty string)
Chat model:         meta-llama/Meta-Llama-3.1-8B-Instruct
Embedding model:    nomic-embed-text
Timeout seconds:    120                          (cold-load can be slow)
```

單一 Ollama 設定（chat + embeddings 都透過 Ollama）：

```text
Provider:           OpenAI-compatible
Base URL:           http://localhost:11434/v1
Embedding base URL: (blank)
API key:            ollama
Chat model:         llama3.1:8b
Embedding model:    nomic-embed-text
Timeout seconds:    120
```

Azure OpenAI:

```text
Provider: Azure OpenAI
Base URL / Azure endpoint: https://my-resource.openai.azure.com
API key: your Azure OpenAI key
Chat model / Azure chat deployment: my-gpt-4o-mini-deployment
Embedding model / Azure embedding deployment: my-text-embedding-3-small-deployment
Azure API version: 2024-02-15-preview
Temperature: 0.2
Timeout seconds: 60
```

## 調參 / 設定

檢索與運維的可調參數 —— 混合權重、放棄門檻、候選池/最終 chunk 數、chunking 目標、embedding 批次、retry 政策、匯入佇列逾時、TTL —— 都集中在 [`app/config.py`](app/config.py)。值依三層解析,後者覆寫前者:

1. **dataclass 預設值**(版本控管,與先前寫死的行為完全相同),
2. **TOML 檔** —— 把 [`config.example.toml`](config.example.toml) 複製成 `config.toml`(已 gitignore),或用 `NOTEBOOKLM_CONFIG_FILE` 指定任意路徑,
3. **環境變數** `NOTEBOOKLM_<GROUP>_<FIELD>`(優先序最高 —— 適合 eval sweep 與各部署覆寫)。

```bash
# 不改 code 掃一個檢索權重:
NOTEBOOKLM_RETRIEVAL_VECTOR_WEIGHT=0.6 .venv/bin/python -m tests.eval_retrieval
```

可依語料/語言保留一份調好的設定檔當交付物(例如 `config.zh.toml`)。改 `[chunking]` 需要對既有來源重新索引(它會改變 chunk 的儲存方式)。

## Logging

```bash
tail -f logs/app.log
NOTEBOOKLM_LOG_LEVEL=DEBUG .venv/bin/uvicorn app.main:app --reload --port 8000
```

可調整的環境變數：

```text
NOTEBOOKLM_LOG_LEVEL=INFO
NOTEBOOKLM_LOG_FILE=logs/app.log
NOTEBOOKLM_LOG_MAX_BYTES=5242880
NOTEBOOKLM_LOG_BACKUP_COUNT=5
NOTEBOOKLM_DATA_DIR=data
NOTEBOOKLM_SECRET=replace-me-with-a-long-random-string
NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1  # NOTEBOOKLM_SECRET 未設定時，僅供本機使用的明確 opt-in
```

`NOTEBOOKLM_SECRET` 預設為必填。若只是在本機 quick start，可以設定 `NOTEBOOKLM_ALLOW_INSECURE_DEV_SECRET=1` 來明確使用內建開發 secret；請勿在 production 設定此 flag。

App 會記錄：startup/shutdown、每個 HTTP request 的 status 與 elapsed time、login attempts、source upload/index/reindex/delete、embedding API calls、Chroma upsert/query、query rewriting、retrieval and rerank、chat success/failure、notebook and note CRUD，以及帶 stack traces 的 exceptions。

## Routes

```text
GET  /                                                    重新導向到 /notebooks（或 /login）
GET  /login                                               登入頁
POST /login                                               驗證登入
POST /logout                                              清除 session

GET  /notebooks                                           notebook grid
POST /notebooks/new                                       建立 notebook
GET  /notebooks/{id}                                      三欄式工作區
POST /notebooks/{id}/rename                               重新命名 / 變更 emoji
POST /notebooks/{id}/delete                               刪除（cascade sources + chats + notes）

POST /notebooks/{id}/sources/upload                       上傳 + 排入 ingest
POST /notebooks/{id}/sources/{sid}/reindex                重新排入 ingest
POST /notebooks/{id}/sources/{sid}/delete                 刪除來源 + vectors + 檔案
GET  /notebooks/{id}/sources/{sid}/_partial               HTMX polling：source row
GET  /notebooks/{id}/sources/{sid}/preview                來源預覽抽屜（chunk list）
GET  /notebooks/{id}/_source-picker                       HTMX swap：chat-form picker

POST /notebooks/{id}/chat/new                             新對話
POST /notebooks/{id}/chat/ask                             提問（HTMX 回傳訊息 partial；無 JS 則 303）
POST /notebooks/{id}/chat/{cid}/delete                    刪除對話
GET  /notebooks/{id}/chat/{cid}/_followups?message_id=N   延遲載入追問問題 chip（快取於 metadata）
GET  /notebooks/{id}/chat/{cid}/export                    下載對話 Markdown

POST /notebooks/{id}/suggestions                          產生 4 個起始問題（聊天空狀態）
GET  /notebooks/{id}/_briefing                            HTMX swap：briefing 細條（去重並行產生）
POST /notebooks/{id}/briefing[?force=1]                   產生 / 重新產生 notebook briefing
GET  /notebooks/{id}/_tools                               HTMX swap：Studio 工具啟動器（磚格）
GET  /notebooks/{id}/tools/{kind}                         preview-modal 的工具設定面板（compare|minutes|study_guide|faq|timeline|translate）
POST /notebooks/{id}/compare                              比較 2 個以上來源（回傳 result fragment + 存檔按鈕）
POST /notebooks/{id}/minutes                              從單一來源產生結構化會議記錄（結果 + 存檔按鈕）
POST /notebooks/{id}/artifacts/{kind}                     A4 產出：study_guide | faq | timeline（結果 + 存檔按鈕）
POST /notebooks/{id}/translate                            A5 將單一來源的摘要翻譯成目標語言（結果 + 存檔按鈕）

POST /notebooks/{id}/notes/pin                            將助理訊息釘選到 notes
POST /notebooks/{id}/notes/add                            儲存 raw note（title + content）
POST /notebooks/{id}/notes/{note_id}/edit                 就地編輯筆記 title/content（U8）
POST /notebooks/{id}/notes/{note_id}/delete               移除釘選筆記（也會 broadcast pin-cleared）
GET  /notebooks/{id}/_notes                               HTMX swap：notes section（notes-changed 事件）
GET  /notebooks/{id}/notes/export                         下載全部筆記 Markdown

GET  /account                                             變更自己的密碼
POST /account/password                                    儲存新密碼

GET  /admin/users                                         使用者列表（僅 admin）
POST /admin/users/new                                     建立使用者
POST /admin/users/{uid}/reset-password                    設定新密碼
POST /admin/users/{uid}/toggle-admin                      提升 / 降級 admin
POST /admin/users/{uid}/delete                            cascade-delete 使用者

GET  /admin/index                                         Chroma index 健康頁（僅 admin）
POST /admin/index/rebuild                                 完整重新 upsert 每個 SQLite chunk
POST /admin/index/clear                                   刪除每個 Chroma vector

GET  /settings                                            admin LLM settings（僅 admin）
POST /settings                                            儲存 LLM settings（API key 寫入時會加密）
```

## 測試

```bash
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py tests/*.py
```

### Retrieval eval

`tests/eval_questions.json` 包含約 25 個針對 demo notebook 的 ground-truth questions。執行 harness 來評分目前 live retrieval pipeline：

```bash
.venv/bin/python -m tests.eval_retrieval                # default: top-k=5, rerank on
.venv/bin/python -m tests.eval_retrieval --no-rerank    # skip LLM rerank for a hybrid-only baseline
.venv/bin/python -m tests.eval_retrieval --top-k 10
```

Harness 會回報每題 hit rank、**Recall@k** 與 **MRR**。編輯 `tests/eval_questions.json` 可加入關於你自己已索引來源的問題。未設定 LLM key 時，harness 會略過。

## Layout

```text
app/main.py            Routes、auth、retrieval orchestration、lifespan、logging。
app/config.py          集中式可調參數（預設值 <- config.toml <- env vars）。
app/db.py              SQLite schema、default-notebook migration、load_llm_settings（解密 API key）。
app/ingest.py          文字抽取、chunking、vector upsert。
app/jobs.py            DB-backed 匯入佇列（ingest_jobs）：enqueue + 原子 claim + retry。
app/worker.py          匯入 worker loop（standalone `python -m app.worker` 或 inline）。
app/llm.py             LLM/embedding HTTP、query rewrite、rerank、starter questions。
app/vector_store.py    Chroma persistent client + diff sync + index_status + clear_all_vectors。
app/security.py        密碼雜湊、signed session cookies、API keys 的 Fernet encryption。
app/templates/
  base.html            Topbar、breadcrumbs、自託管 vendor scripts（marked、DOMPurify、HTMX、Alpine）。
  home.html            Notebook grid。
  notebook.html        三欄式工作區 shell + 來源預覽 modal。
  _source_item.html    單一來源列表項目（HTMX polling target + preview trigger）。
  _source_preview.html 在 preview modal 中轉譯的 chunks list。
  _source_picker.html  Chat-form source-checkbox fieldset（HTMX swap target）。
  _suggestions.html    起始問題區塊（顯示在聊天空狀態）。
  _briefing.html       Studio briefing 細條（HTMX swap target；第一次檢視時自動觸發 POST）。
  _studio_tools.html   Studio 工具啟動器——在 preview-modal 開啟各工具的磚格（HTMX swap target）。
  _tool_panel.html     載入 preview-modal 的單一工具設定面板（compare/minutes/study_guide/faq/timeline）。
  _compare_result.html 來源比較結果 fragment（markdown body + 共用存檔按鈕）。
  _minutes_result.html 會議記錄結果 fragment（markdown body + 存檔按鈕；非會議來源不提供存檔）。
  _artifact_result.html A4 產出結果 fragment（markdown body + 共用存檔按鈕）。
  _translate_result.html A5 翻譯摘要結果 fragment（markdown body + 共用存檔按鈕）。
  _save_note_button.html 所有工具結果共用的「存成筆記」控制（一次性已存狀態）。
  _notes_section.html  Studio 產出與筆記架（HTMX swap target；行內編輯 = U8）。
  account.html         每位使用者的密碼變更頁。
  admin_users.html     Admin 使用者管理頁。
  admin_index.html     Admin vector-index 健康頁。
  login.html, settings.html, error.html
app/static/
  style.css            Design tokens + components + modal + admin index stats。
  app.js               Bindings、Alpine dropzone、Markdown render、citation click、suggestion fill、pin reset。
tests/
  test_core.py         Hash、ingest、isolation、retrieval、notebook migration、pin idempotency、settings decryption。
  test_chunking.py     Sentence-aware chunker：CJK detection、splitting、overlap、long-sentence fallback。
  test_llm.py          Provider request shapes、parsing、Studio helper short-circuits（summary / briefing / compare）。
  test_security.py     Fernet round-trip + legacy plaintext + wrong-secret behaviour。
  test_vector_store.py Index status + diff/full sync + clear，全都針對真實 Chroma temp dir。
  test_extract.py      Source extraction（PDF、DOCX、HTML edge cases）。
  test_briefing_lock.py SQLite briefing lock：acquire / release / stale timeout。
  test_jobs.py         匯入佇列：enqueue 冪等、原子 claim、stale 重認領、retry/fail。
  test_config.py       設定分層（預設值 <- TOML <- env）+ 範例檔同步檢查。
  eval_questions.json  Demo notebook 的 ground-truth retrieval Qs。
  eval_retrieval.py    Recall@k + MRR harness（見 RETRIEVAL.md）。
config.example.toml    可調參數設定範本（複製成 config.toml 即可覆寫）。

Runtime-generated，已 gitignore：
data/
  app.sqlite3          SQLite metadata（users、notebooks、sources、chunks、conversations、messages、notes、llm_settings）。
  uploads/             每位使用者的原始檔案。
  chroma/              Vector index。
logs/app.log           輪替 app log。
setup.sh               一次性 Python 3.12 env bootstrap。
requirements.txt       Docker 使用的 runtime dependencies。
requirements-dev.txt   疊在 runtime 上的本機開發 / 測試 dependencies。
```

## 已知後續事項

仍待處理的重點項目：

- 尚無串流回應 - 回答會在完整 LLM call 回傳後才抵達。
- POST routes 尚無 CSRF 保護。
- 沒有 offline embedding fallback - 接受上傳前必須先設定 embedding model。
- Keyword search 使用 SQLite 上的 `LIKE '%token%'`；FTS5 + BM25 已列入後續規劃（見 [`RETRIEVAL.md`](docs/RETRIEVAL.md)）。
- Hybrid merge 使用固定的 `0.7·vector + 0.3·keyword` 混合；Reciprocal Rank Fusion 已列入後續規劃。
- Qdrant 是未來可評估的 vector store 候選；替換 Chroma 前先做有界限的 spike。

## License

此專案採 MIT License 授權。請見 [LICENSE](LICENSE)。
