# 疑難排解手冊（實際發生過的事故）

維運端的 runbook：**只收真實發生過的事故**，不收假設性的問題。每一則都附發生日期、
當時版本、可直接搜尋的 log 字串，以及當下實際採取的處理方式。

設計文件在別處：向量索引的設計理由見 [`PERFORMANCE.md`](PERFORMANCE.md) `P2-1`、
維度遷移見 [`DEVELOPMENT.md`](DEVELOPMENT.md#changing-the-embedding-dimension)、
檢索管線見 [`RETRIEVAL.md`](RETRIEVAL.md)。本文不重複那些內容，只負責
**「看到這個症狀時該怎麼辦」**。

## 怎麼新增一則

一則事故解決之後就補進來，格式固定：**症狀 → log 字串 → 根因 → 處理 → 預防**。
加上發生日期與當時版本。若之後由程式修掉了，在標題後面標 **[已於 vX.Y.Z 修正]**
並保留內容——舊版部署仍然會遇到。

---

## 1. 向量索引損毀：系統照常回答，但答案悄悄變差

**發生**：2026-09-09，客戶環境升級至 `0.7.0` 期間。

**症狀**

- 網站正常、能登入、**問答也照常有答案**，但答案品質明顯下降。
- 使用者不會看到任何錯誤訊息。**唯一的徵兆在 log 裡。**
- `/admin/index` 顯示索引維度讀不出來。

**log 字串**（`logs/app.log`，可直接 grep）

```text
WARNING [app.retrieval] retrieve_vector_failed ... falling back to capped SQLite scan
chromadb.errors.InternalError: Error executing plan: Internal error: Error finding id
WARNING [app.vector_store] vector_index_dimension_unreadable
chromadb.errors.InternalError: Error executing plan: Error sending backfill request to compactor: Failed to apply logs to the hnsw segment writer
```

**為什麼「沒壞掉」反而危險**：Chroma 查詢失敗時，`retrieve()` 會自動降級成上限
2000 筆的 SQLite 掃描（`P1-3` 刻意加的保護，避免整個行程被拖垮），所以服務會繼續
回答。實際案例中這個降級狀態**持續了約兩小時無人察覺**，因為除了 log 之外沒有任何
地方會說。

> **先確認是哪一種。** `Error finding id` 這一行，第 3 則也會出現。差別在於
> **本則一定伴隨** `vector_index_dimension_unreadable` 或
> `Failed to apply logs to the hnsw segment writer`；只有 `Error finding id`
> 而沒有這兩行的，是第 3 則（行程內索引過期），**重啟即可，不要刪 `data/chroma`**。

**根因**：`data/chroma` 裡持久化的 HNSW segment 損毀。**不是** SQLite schema
migration 的問題，也**不是** ChromaDB 版本升級造成的——本專案自始至終釘在
`chromadb==1.5.9`。最可能的觸發是升級切換時新舊行程同時寫入同一個 `data/chroma`，
或行程在壓縮（compaction）途中被硬砍。

**處理**（實際採用，且不需要重新產生 embedding）

SQLite 的 `chunks.embedding_json` 才是向量的**正本**，Chroma 只是由它衍生的索引
（設計理由見 `PERFORMANCE.md` `P2-1`）。因此重建索引不必重新呼叫 embedding 模型，
不花 GPU 時間，也不需要模型服務在線。

1. 停掉**所有**會碰到 `data/` 的行程：`docker compose stop app worker`
   （非 Docker 則停掉 uvicorn 與 `python -m app.worker`）。確認真的都停了。
2. 備份整個資料夾：`tar czf data-$(date +%F).tar.gz data/`。
3. **只刪除 `data/chroma`**，其餘保留：
   `rm -rf data/chroma`
   **不要**刪 `data/app.sqlite3` 與 `data/uploads/`——那是使用者資料，而且是重建的來源。
4. 重新啟動。startup sync 會從 SQLite 重新建立整個 Chroma 索引。
5. 確認：`/admin/index` 的索引維度與向量數量恢復正常，且 log 出現
   `vector_sync_completed`；隨便問一題，確認 log 不再出現 `retrieve_vector_failed`。

> `/admin/index` 的 **Rebuild** 是同一件事的正常入口，**索引沒壞掉時應優先使用**。
> 但集合本身已損毀到讀不出來時，Rebuild 也可能一起失敗，這時才用上面的手動流程。
> 完全無法啟動、連 `/admin/index` 都進不去時，見
> [`DEVELOPMENT.md`](DEVELOPMENT.md#changing-the-embedding-dimension) 的
> `scripts/reset_chroma_dimension.py`（破窗用）。

**預防**

- **升級時確保舊行程完全停止再啟動新的**，不要讓兩組容器短暫同時寫 `data/chroma`：
  `docker compose stop app worker` → `docker compose build` → `docker compose up -d`。
- 停止服務時給予正常關閉時間，避免在壓縮途中硬砍容器。
- 升級後**問一題**，並確認 log 沒有 `retrieve_vector_failed`；這是目前最快的健檢。

**尚未做到的**：管理員在畫面上看不到「向量檢索正在失敗」，只能靠看 log。
追蹤於 [`ROADMAP.md`](ROADMAP.md) `O3`。

---

## 2. 來源索引失敗：chunk 超過 embedding 模型的 token 上限 [已於 v0.7.0 修正]

**發生**：2026-09-08 之前，客戶上傳某份 FDA 藥品仿單 PDF。

**症狀**

- 某一份來源始終停在 `failed`，其他來源正常。
- 該來源在資料庫裡**一筆 chunk 都沒有**——因為整批 embedding 被拒絕，索引不會完成。

**log 字串**

```text
HTTP Request: POST .../v1/embeddings "HTTP/1.1 400 Bad Request"
```

**根因**：整條 ingest pipeline 只用「字元數」當上限，沒有一處用 token 數把關。
pdfplumber 抽出的稀疏合併表頭會變成整片 `| | | | | |`，對 e5 而言每根管線是兩個
token，一個 773 字的 chunk 因此吃掉 533 個 token，超過模型 512 的上限；vLLM 對超長
輸入是直接回 HTTP 400 而不是截斷，於是整份來源索引失敗。當時的 token 估算又把這面
管線牆當英文散文計價（估 193、實際 533），所以 `chunk_over_token_budget` 警告從頭到尾
沒亮。

**處理**

- 升級到 `0.7.0` 以上（表格空欄壓縮、逐字元類別的 token 估算、試算表 chunk 上限）。
- **只需要重新索引失敗的那幾份來源。** 先前已成功索引的來源不受影響——它們當初
  就沒有超限的 chunk。那些來源仍帶著舊的切塊方式（管線牆會多佔 token、稀釋語意），
  屬於品質面的殘留，不影響可用性，可留待日後自然重新索引。
- 上傳前想先確認某個檔案會不會超限，用離線檢查（不連網、不碰 DB，可直接指著客戶檔案跑）：

```bash
PYTHONPATH=. .venv/bin/python -m tests.inspect_file_tokens /path/to/file.pdf
```

**預防**：交付或試用前，先用上面的指令跑過客戶的代表性樣本，特別是含大量表格的 PDF、
掃描轉檔的文件與長問答試算表。

---

## 3. 向量檢索全數降級：另一個行程寫入後，查詢端的索引沒跟著更新 [已修正，尚未發版]

**發生**：2026-09-10 至 2026-09-11，`0.7.0`，split-worker 部署（`app` 與 `worker`
兩個容器共用 bind-mount 的 `./data`）。

**症狀**

- 與第 1 則**完全相同**：服務正常、照常有答案，品質悄悄變差，使用者看不到任何錯誤。
- 差別在於索引本身沒有損毀——`/admin/index` 讀得出維度與向量數，Rebuild 也能跑完。
- 影響範圍比想像大：該期間 13 次提問只有 3 次走到向量檢索，其中一名使用者連續
  7 次提問、**一次都沒吃到向量檢索**，跨兩天無人察覺。

**log 字串**

```text
WARNING [app.retrieval] retrieve_vector_failed ... falling back to capped SQLite scan
chromadb.errors.InternalError: Error executing plan: Internal error: Error finding id
```

**而且沒有**（有的話請看第 1 則）：

```text
vector_index_dimension_unreadable
Failed to apply logs to the hnsw segment writer
```

**根因**：不是損毀，是**行程內的索引過期**。Chroma 的 `PersistentClient`
把 HNSW 索引放在行程記憶體裡，並以 store 路徑為 key 快取一份 `System`；
另一個行程的寫入不會使它失效。`worker` 容器寫進新來源的 chunk 之後，`app` 容器
仍拿舊的記憶體索引去比對磁碟上已經更新的 metadata，帶 `where` 的查詢就全數失敗。

專案本來就有跨行程失效機制（`vector_index_state.generation`），但它只被
`reset_collection()`（O0 維度遷移）遞增，一般的 upsert / delete 完全不動它——
三條寫入路徑只有一條被保護。

實測（chromadb 1.5.9）確認過三件事，前兩件都**無效**：

| 做法 | 結果 |
|---|---|
| 對同一個 client 重新 `get_or_create_collection()` | ❌ 仍然失敗 |
| 建一個新的 `PersistentClient` | ❌ 仍然失敗（System 以路徑為 key 被快取） |
| `SharedSystemClient.clear_system_cache()` 後再開 client | ✅ 恢復 |

**處理**

- **升級到含本修正的版本**（目前在 `CHANGELOG.md` 的 `[未發布]`，發版後請把這裡
  改成實際版號）。修正後每次 upsert / delete / clear 都會遞增
  `vector_index_state.write_seq`，其他行程在下一次讀取前會清掉 System 快取並重開
  client。
- **舊版部署的立即處置：重啟 `app` 容器即可**（`docker compose restart app`）。
  **不需要**刪 `data/chroma`，也不需要 Rebuild——資料本身是好的。誤用第 1 則的
  流程雖然也會恢復，但多停機、多一次全量重建。
- 重啟只能撐到下一次 worker 寫入，所以這是止血、不是修好。

**預防**

- 升級後確認 log 出現 `retrieve_completed mode=chroma`。看到
  `mode=sqlite_fallback` 就代表正在降級。
- `retrieve_vector_failed` 現在帶 `consecutive_failures=`，連續 3 次會從 WARNING
  升級成 ERROR，可直接拿來做告警條件。
- 長期解：讓兩個容器改用 Chroma server 模式（`HttpClient`），由 server 端序列化
  寫入，從根本消除多行程共享記憶體索引的問題。追蹤於 [`ROADMAP.md`](ROADMAP.md)。

**尚未做到的**：與第 1 則相同——管理員在畫面上仍看不到「向量檢索正在失敗」，
目前只有 log 與 `retrieval.vector_health()`。追蹤於 [`ROADMAP.md`](ROADMAP.md) `O3`。

---

## 4. 提問得不到任何回答：問題太長，超過 embedding 模型的輸入視窗 [已修正，尚未發版]

**發生**：2026-09-11 與 2026-09-16，`0.7.0`。

**症狀**

- 使用者送出一個**很長**的問題（貼整段條文、整封信），畫面上完全沒有回答。
- 短問題在同一個 notebook 正常。與第 1、3 則不同：**這次使用者是真的看得到失敗的**。

**log 字串**

```text
ERROR [app.llm] llm_http_error status=400 url=.../v1/embeddings
  "maximum context length is 512 tokens ... value=513"
  "your prompt contains 11011 characters (more than 8192 characters ...)"
ERROR [app.llm] embedding_api_failed ... model=intfloat/multilingual-e5-large
ERROR [app.main] chat_stream_failed user_id=... notebook_id=...
```

**根因**：`embed_texts()` 只用 `batch_size` 依「則數」切批，對單則文字長度沒有任何
檢查。索引路徑安全，是因為切塊器已經把 chunk 控制在 512 token 內；**查詢路徑完全
沒有經過這套機制**，使用者貼多長就送多長。vLLM 對超長輸入是回 HTTP 400 而不是截斷。

三次裡有兩次是**兩個問題疊加**：query rewrite 先失敗（模型輸出的 JSON 不合法），
fallback 成原始問題，於是「長問題」原封不動變成「長 query」。

**處理**

- 升級到含本修正的版本（目前在 `CHANGELOG.md` 的 `[未發布]`）。送出前會先修剪到
  `[diagnostics] embedding_token_budget`。
- 舊版的暫時做法：請使用者把長問題拆短。沒有設定可以繞過。
- **使用較大視窗的 embedding 模型時記得調高 `embedding_token_budget`**
  （e5 是 512；OpenAI `text-embedding-3` 是 8191）。留在 512 不會失敗，但長問題會被
  截短，影響檢索品質。

**預防**

- grep `embedding_input_truncated`。出現 `role=query` 是正常的保護動作；出現
  `role=passage` 代表切塊器產出了超預算的 chunk，那是 bug（參見第 2 則）。
- `query_rewrite_failed` 會放大這個問題，兩者一起看。
