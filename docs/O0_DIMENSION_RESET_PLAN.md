# O0 · 安全重設 Chroma collection 維度 — 實作計劃

> **狀態**：**Phase A 已實作**；Phase B–D 待排。暫行 workaround 已於 PR #81 merge 進 main（64a79e0）。
> **權威來源**：範圍與驗收標準見 [`ROADMAP.md`](ROADMAP.md) O0；向量層行為見 [`RETRIEVAL.md`](RETRIEVAL.md)；schema 見 [`SCHEMA.md`](SCHEMA.md)；操作程序見 [`DEVELOPMENT.md`](DEVELOPMENT.md)。本檔是實作藍圖，設計決策以那些權威文件為準。
> **前置已滿足**：`scripts/reset_chroma_dimension.py`（含分類邏輯與 5 個測試）已在 main；`/admin/index` 誤導文案已修正並有回歸測試。

---

## 1. 缺陷本質

`clear_all_vectors()`（[`app/vector_store.py:272`](../app/vector_store.py)）只做 `col.delete(ids=...)`。Chroma 在第一次 upsert 時把維度寫進 collection schema，**刪光 records 不會刪掉 schema**，於是：

1. collection 空了，但仍鎖在舊維度（例如 1024）。
2. `probe_index_dimension()` 讀不到任何 stored embedding → 回報 `None`。
3. `/settings` 把 `None` 解讀為「任何維度都合法」→ 接受 1536 維模型。
4. 第一次 upsert 才炸：`Collection expecting embedding with dimension of 1024, got 1536`。
5. 反覆 Clear/Rebuild 無法修復，因為 collection 物件從頭到尾沒被換掉。

**多進程放大**：`_collection` 是每個 process 的 module-global 快取。web app 與獨立 worker 各持一份 handle，只在 web process 換掉 collection，worker 仍指向已刪除的舊物件。

---

## 2. 待拍板的設計決策

實作前需要裁決，因為會影響 schema 與 UX：

| # | 決策 | 選項 | 建議 | 理由 |
|---|---|---|---|---|
| D1 | 遷移期間的並行 ingest | (a) 排空並等待 in-flight job (b) **佇列非空或有 running job 就拒絕遷移** | **(b)** | POC 單機、遷移是罕見的計劃性操作。排空邏輯要處理 timeout、部分失敗、worker 沒回應，複雜度不成比例。直接拒絕並告訴管理員「等佇列清空或停掉 worker」誠實且好懂。 |
| D2 | 舊維度來源的狀態 | (a) 沿用 `status='failed'`（workaround 現行做法） (b) **新增 `stale_embedding` 狀態** | **(b)** | `failed` 混淆「攝取壞掉」與「需要重新 embed」。管理員看到一排 failed 會以為檔案有問題。這是唯一值得動 schema 的地方——沿用 failed 會讓 admin 介面說謊。 |
| D3 | 遷移流程的落點 | (a) **`/admin/index` 新增遷移動作** (b) 綁進 `/settings` 儲存流程 | **(a)** | `/settings` 存檔時使用者心智在「設定模型」，不在「我要毀掉索引」。`/admin/index` 已是索引維運頁，且 dry-run 預覽需要空間。`/settings` 仍負責擋下不相容的維度變更並指向 `/admin/index`。 |
| D4 | 永久修法後 script 去留 | (a) 刪除 (b) **保留為 break-glass** | **(b)** | app 起不來時（例如 collection 已壞到 startup sync 就炸）UI 流程用不了。保留但在文件標為「僅限 app 無法啟動時」。 |

---

## 3. 核心設計

### 3.1 `reset_collection()` — 換掉 collection，而非清空它

取代 `clear_all_vectors()` 的語義（保留舊函式給「同維度清空」情境）：

```python
def reset_collection() -> int:
    """刪除並重建 collection，解除維度鎖定。回傳刪掉的向量數。"""
    client = _client_handle()
    removed = collection().count()
    client.delete_collection(name=COLLECTION_NAME)
    reset_client()          # 丟掉本 process 的快取 handle
    _bump_index_generation()  # 通知其他 process
    collection()            # 重建，未鎖定
    return removed
```

滿足驗收標準 1。

### 3.2 generation counter — 跨 process 失效快取

沿用專案既有的 SQLite 跨進程協調前例（`briefing_locks` + `BEGIN IMMEDIATE`）。

**新增單列表 `vector_index_state`**：

| Column | Type | Phase | Notes |
|---|---|---|---|
| `id` | INTEGER **PK** CHECK(id = 1) | **A（已建）** | 單列 |
| `generation` | INTEGER NOT NULL DEFAULT 0 | **A（已建）** | 每次 reset +1 |
| `locked_at` | REAL NULL | C | 遷移進行中；NULL 表示未鎖 |
| `locked_by` | TEXT | C | process 識別，僅供診斷 |

> **實作偏離**：Phase A 只建了 `id` + `generation` 兩欄。`locked_at` / `locked_by` 要到 §3.3 的寫入屏障才有用途，先加是投機；`app/db.py` 的 `_ensure_column()` 讓之後補欄很便宜。`SCHEMA.md` 描述的是**已建**的兩欄，不是這張完整規劃表——兩者的差異就是尚未實作的部分。

`collection()` 改為：讀 DB generation，與本 process 快取的值比對，不同就丟掉 handle 重新取得。

> **成本誠實說明**：這會在每次 `collection()` 呼叫加一次單列 SQLite 讀取。檢索路徑每次問答呼叫數次，ingest 每批一次。單機 SQLite 單列讀取約數十微秒，相對於一次 embedding HTTP 呼叫可忽略。**先用最簡單的「每次都讀」實作**，不要預先加 TTL 快取——若之後 profiling 顯示有影響再加，並把數據寫進 `PERFORMANCE.md`。

滿足驗收標準 2。

### 3.3 阻擋並行寫入（依 D1 建議）

遷移開始前，在 `BEGIN IMMEDIATE` 交易內：

1. 檢查 `ingest_jobs` 有無 `status IN ('queued', 'running')` 的列 → 有就中止，回報「請等佇列清空或停止 worker」。
2. 寫入 `locked_at`。
3. `claim_next_job()` 在 `locked_at` 非空時直接回傳 `None`（worker 自然空轉等待）。
4. 遷移結束（成功或失敗）都清掉 `locked_at`；超過 `INDEX_LOCK_TIMEOUT_S` 的鎖視為 stale，比照 `briefing_locks` 處理。

### 3.4 阻止 startup sync 重新鎖定

遷移後 SQLite 仍留著舊維度的 `embedding_json`。`sync_from_sqlite` 走 `WHERE sources.status = 'indexed'`，會把它們重新 upsert 回去，**再次把新 collection 鎖回舊維度**——這是最陰險的一條路徑。

作法：把 `scripts/reset_chroma_dimension.py` 已寫好且有測試的 `inspect_sources()` 分類邏輯**提升到 `app/vector_store.py`（或新的 `app/index_migration.py`）作為共用實作**，不要複製一份。分類結果：

- 目標維度且 `indexed` → 保留，遷移後回填。
- 目標維度但因這個 mismatch 而 `failed` → 復原為 `indexed`。
- 其他維度／混合維度的 `indexed` → 改為 `stale_embedding`（D2），`_indexed_chunk_ids()` 自然排除，UI 顯示「需重新索引」並提供 Reindex。
- 其餘（uploaded／processing／無關的 failed）→ 不動。

滿足驗收標準 3。

---

## 4. Phase 拆解

| Phase | 內容 | 產出 | 相依 |
|---|---|---|---|
| **A ✓** | `reset_collection()` + `vector_index_state` 表 + generation 檢查 | **已實作。**單進程維度遷移可行；schema + `SCHEMA.md` 已同步。另釘了一條缺陷見證測試（clear 後 probe 回報 `None` 卻仍拒絕新維度），Chroma 若改掉這行為會失敗提醒。**未含寫入屏障**——`reset_collection()` 的 docstring 標明「呼叫端須確保無並行 ingest」，屏障隨 Phase C 的觸發流程落地。 | — |
| **B** | 分類邏輯上提至 `app/`、`stale_embedding` 狀態、startup sync 防護 | 驗收標準 3；`SCHEMA.md`、`ROUTES.md` 視情況 | A |
| **C** | `/admin/index` 遷移 UI（dry-run 預覽 → 打字確認 → 執行 → 自動 enqueue Reindex）+ audit event | 驗收標準 5 的產品面 | A, B |
| **D** | split-worker 覆蓋、移除全部暫行警語、script 降級為 break-glass | 驗收標準 4；收尾 | A–C |

Phase A 單獨就能解掉 P0 的主要痛點（單機 inline worker 是目前預設部署形態），建議先落地 A 再排 B–D。

---

## 5. 測試策略

對應驗收標準 4，兩種運作模式都要覆蓋：

| # | 測試 | 驗證 |
|---|---|---|
| 1 | upsert 384 → `reset_collection()` → upsert 1536 成功 | 標準 1：schema 真的被換掉 |
| 2 | upsert 384 → 舊式清空（delete ids）→ `reset_collection()` → upsert 1536 成功 | 從既有壞狀態能救回 |
| 3 | 兩個 `vector_store` 實例指向同一路徑，A reset 後 B 的下一次 `collection()` 取得新 handle | 標準 2：generation 失效快取 |
| 4 | 佇列有 queued/running job 時遷移被拒 | D1 行為 |
| 5 | 遷移後 `sync_from_sqlite()` 不回填 `stale_embedding` 來源的舊維度 chunks | 標準 3：**最關鍵的一條** |
| 6 | 目標維度來源存活、其他維度標記 stale、無關來源不受影響 | 分類正確性 |
| 7 | inline worker（`NOTEBOOKLM_INLINE_WORKER=1`）與 split worker 兩種模式各跑一次 384→1536 | 標準 4 |

現有 `tests/test_reset_chroma_dimension.py` 的 5 個測試在分類邏輯上提後應改為測 `app/` 的共用實作，script 保留一層薄的 CLI 測試。

---

## 6. 收尾清單（驗收標準 5）

永久修法完成後，**依序**移除暫行警語，並確認彼此一致：

- [ ] `/admin/index` 的「更換 embedding 維度」區塊：從「跑 script」改寫為產品流程說明。
- [ ] 更新 `test_admin_index_page_warns_clear_does_not_reset_dimension` 的斷言（目前釘住 script 路徑字串）。
- [ ] audit event：擴充既有的 `index_cleared`（已是 high sensitivity），新增 `index_dimension_migrated`，metadata 記 `{from_dimension, to_dimension, restored, marked_stale}`，不記任何向量內容。
- [ ] 移除 `README.md` / `README.zh-TW.md` / `AGENTS.md` / `docs/RETRIEVAL.md` / `docs/DEVELOPMENT.md` 的 P0 警語。
- [ ] `CHANGELOG.md`「已知問題」段落移除，改列入「修正」。
- [ ] `docs/ROADMAP.md` O0 標記 `[x]`；`O1d` 移除「Do not direct admins to Clear/Rebuild until O0 is complete」的但書。
- [ ] `scripts/reset_chroma_dimension.py` 文件標為 break-glass（D4）。
- [ ] `docs/SCHEMA.md` 反映 `vector_index_state` 與 `stale_embedding` 狀態。

---

## 7. 刻意不做

- **不做自動重新 embed**：遷移只處理向量搬遷，重新產生 embedding 走既有 Reindex 流程（會呼叫 embedding endpoint、有成本），維持成本可見。
- **不做同維度換模型的偵測**：兩個模型都是 1536 維時，維度檢查無法分辨，仍需管理員自行判斷並 Reindex。此限制寫進 UI 文案。
- **不做 Redis/外部鎖**：單機 POC，SQLite `BEGIN IMMEDIATE` 已足夠，且與 `briefing_locks` 前例一致。若 ingest 之後搬到 Redis/RQ（`app/jobs.py` 是既定的 swap-point），鎖再一起搬。
