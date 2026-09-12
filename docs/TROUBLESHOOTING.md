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
