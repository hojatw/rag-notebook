# Review backlog — 2026-08-22

本輪全面 review（安全／效能／維護性／文件／LLM 相容性）的發現清單，供後續逐項討論與追蹤。

**這是暫存文件，不是永久 backlog。** 每一項實作完成後，應該**溶解進**既有的權威 backlog
（`SECURITY.md` / `PERFORMANCE.md` / `QUALITY.md` / `ROADMAP.md`）而不是留在這裡。
全部清空後刪除本檔。

**基準線**：review 當下 `main @ 6b1c0d0`（VERSION 0.3.0），`pytest` **288 passed**。

**進度**：**文件批（DOC-1~15）全數完成**；安全性 `SEC-1`~`SEC-3` 與 LLM 相容性
`LLM-1`~`LLM-3` 亦已完成，`PERF-1` 同時解決。全部溶解進權威 backlog。
2026-08-23 再完成 `SEC-4`~`SEC-6`、`LLM-4` 與 `MNT-1`；2026-08-29 完成
`SEC-7`、`SEC-8`、`LLM-5` 與 `PERF-2`。

**仍待排（3 項，全為 P2/P3）**：`PERF-3`、`QLT-1`、`MNT-2`。

剩餘項目的順序建議：
- **`QLT-1`（關鍵字先截斷後評分）需要 `Q1-3` 代表性 eval set** —— 修法直覺正確，
  但它改變檢索行為，沒有尺可以量之前不建議動。

每項完成後請照上面的規則打勾並註明 durable 紀錄的位置；全部清空後刪除本檔。

優先權定義：
- **P0** — 網路曝險部署前必修
- **P1** — 近期排程，有明確受害情境
- **P2** — 排進後續輪次
- **P3** — 記錄備查，附重啟條件

狀態：`[ ]` 待辦 · `[~]` 進行中 · `[x]` 完成 · `[-]` 決定不做

---

## 1 · 安全（SEC）

### [x] SEC-1 · P0 · 預設帳號「重啟即復活」
> **已完成**（PR #91）。durable 紀錄見 [`SECURITY.md`](SECURITY.md) → *Secrets and data* 的
> Bootstrap accounts 段落，以及 [`SCHEMA.md`](SCHEMA.md) 的 `users.must_change_password`。
- **位置**：[`app/db.py:529-530`](../app/db.py) `_ensure_user()`；`init_db()` 由
  [`app/main.py:128`](../app/main.py)（web lifespan）與 [`app/worker.py:78`](../app/worker.py) 每次啟動呼叫。
- **問題**：`admin/admin123`、`user/user123` 無條件種入。`INSERT OR IGNORE` 只保證不覆寫既有帳號，
  但管理員照 `SECURITY.md` 指示刪掉 demo 帳號後，**下次重啟就會回來，密碼不變**。
- **影響**：等同「重啟即後門」。登入頁已做「只在 dev secret 模式顯示 demo 提示」，
  代表風險已被意識到，但只修了顯示、沒修種入。
- **已裁決的修法（保守方案）**：非 dev 環境**不種入 `user`**；`admin` 保留但**首次登入強制改密碼**。
  不採用「首次啟動要求建立管理員」——會讓既有部署重啟後進不去。
- **連帶**：需要 `users` 新欄位（密碼待變更旗標）→ 依 AGENTS.md 規則必須同步 [`SCHEMA.md`](SCHEMA.md)；
  完成後改寫 `SECURITY.md`（見 DOC-1）。

### [x] SEC-2 · P0 · 上傳無大小上限，且整包 body 進記憶體
> **已完成**（PR #93）。durable 紀錄見 [`SECURITY.md`](SECURITY.md) → *Attack surface note:
> uploaded-file parsers*，容量參數說明見 [`DEVELOPMENT.md`](DEVELOPMENT.md) → *File size caps*。
> 順帶把 `max_source_bytes` 改名為 `extract_max_file_bytes`（舊鍵仍相容）。
- **位置**：[`app/main.py:1681`](../app/main.py) `upload_source()`、
  [`app/main.py:256-273`](../app/main.py) `_submitted_csrf_token()`、
  [`app/templates/notebook.html:73`](../app/templates/notebook.html)。
- **問題**：兩個缺陷疊加——
  1. `upload_source` 只限**檔案數**（`upload_batch_limit=5`），不限大小。
     `max_source_bytes`（20 MB）要到 ingest 階段才檢查（[`app/ingest.py:552`](../app/ingest.py)），檔案早已落地。
  2. CSRF middleware 對 multipart 會 `await request.body()` 把整包讀進 RAM 才交給 route，
     而上傳表單是**純 HTML form**（非 HTMX），**不帶 `X-CSRF-Token` header**，
     所以每次上傳都必定走 buffering 路徑。
- **影響**：一般登入使用者傳 5 × 2 GB 即可打爆記憶體，不需任何漏洞。
- **修法**：(a) 上傳表單改帶 CSRF header（middleware 會在 `if token: return token` 提早返回，完全不讀 body）；
  (b) route 內邊寫邊計數，超過 `max_source_bytes` 即中止並刪檔；(c) 反向代理設 body size 上限。
- **一起做**：PERF-1（同一個函式）。

### [x] SEC-3 · P1 · session token 無時效、無法撤銷
> **已完成** — PR #95 — token 改用帶時間戳的簽章器 + `[auth].session_max_age_hours`（絕對壽命，不因使用延長）；撤銷靠新的 `users.password_version`。durable 紀錄見 [`SECURITY.md`](SECURITY.md) → *Sessions*。
- **位置**：[`app/security.py:108`](../app/security.py) `sign_user_id()`、
  [`app/main.py:684`](../app/main.py) `_set_session_cookie()`。
- **問題**：用 `URLSafeSerializer`（非 `URLSafeTimedSerializer`），payload 只有 `{"uid": id}`，
  cookie 也沒設 `max_age`。cookie 本身是 session cookie（關瀏覽器即消失），
  但 **token 字串本身永不過期**，複製出來就永久有效。
- **影響**：改密碼 / admin reset password **不會踢掉既有 session**。
  目前唯一撤銷手段是刪帳號（`current_user` 每次查 DB）。
- **修法**：改 `URLSafeTimedSerializer` + `max_age`；payload 加密碼版本號，改密碼時 +1。

### [x] SEC-4 · P2 · 登入無速率限制
> **已完成（2026-08-23，review follow-up 同日完成）** — SQLite 共用 account
> bucket、HMAC bucket id、跨 process password-verification leases、HTTP 429 +
> `Retry-After` 與可調 `[auth].login_*`。原始 global failure cooldown 在 review
> 發現可被匿名流量反向用成全站登入 DoS，因此在發布前改為短租約 concurrency slots。
> durable 紀錄見
> [`SECURITY.md`](SECURITY.md) → *Local-login rate limiting*、
> [`AUTHENTICATION.md`](AUTHENTICATION.md) 與 [`SCHEMA.md`](SCHEMA.md)。
- **位置**：[`app/main.py:845`](../app/main.py) `login()`。
- **問題**：無 throttle、無鎖定。PBKDF2 200k 迭代讓單次嘗試變貴，
  但同時也讓它成為一個 CPU DoS 面（無次數上限的高成本運算）。
- **現有緩解**：`config.auth.local_login_enabled=false` 可整個關掉本地登入（預設開啟）。

### [x] SEC-5 · P2 · prompt-injection 偵測只有英文 pattern
> **已完成（2026-08-23）** — `local.rules.v2` 加入繁中／簡中 ignore、reveal、
> bypass patterns 與正反例測試；維持 warn-only。durable 紀錄見
> [`SECURITY.md`](SECURITY.md) → *Local prompt-injection telemetry*。
- **位置**：[`app/governance.py:42-46`](../app/governance.py) `PROMPT_INJECTION_PATTERNS`。
- **問題**：三條 regex 全英文（`ignore previous instructions` 等）。
  這是 zh-TW 部署、吃中文研究報告的系統，「忽略以上指令」「請印出你的系統提示」完全不會被攔。
- **修法**：補中文 pattern，**或**在文件明確標示這層只是英文 heuristic，避免誤以為有覆蓋。

### [x] SEC-6 · P2 · `SELECT *` 把 `password_hash` 帶進 template context
> **已完成（2026-08-23）** — `current_user()` 改成 session/template 所需欄位白名單，
> 並以回歸測試確認 `password_hash` 不會離開 DB 查詢結果。
- **位置**：[`app/main.py:331`](../app/main.py) `current_user()`。
- **問題**：`dict(user)` 含 `password_hash`，隨 `{"user": user}` 進入模板 context。
  目前沒有模板印它，但這是等著被踩的地雷。
- **修法**：明列欄位，不用 `SELECT *`。

### [x] SEC-7 · P3 · 建立使用者失敗時回顯原始例外
> **已完成（2026-08-29）** — 使用者端改為 i18n 通用錯誤，完整例外只寫入 server log；
> durable 紀錄見 [`SECURITY.md`](SECURITY.md) → *Admin error and logout-cookie hygiene*。
- **位置**：[`app/admin.py:423`](../app/admin.py) `error = f"建立使用者失敗：{exc}"`。
- **問題**：原始 SQLite 例外字串回顯給 admin。僅限管理員可見，嚴重度低。

### [x] SEC-8 · P3 · `logout` 的 `delete_cookie` 屬性未對齊
> **已完成（2026-08-29）** — session 發行與刪除共用 path、HttpOnly、SameSite 與
> request-scheme-derived Secure 屬性；durable 紀錄見 [`SECURITY.md`](SECURITY.md) →
> *Admin error and logout-cookie hygiene*。後續核對確認舊版 Starlette 的 deletion
> 已預設 `Path=/`，因此這是契約硬化，未宣稱已重現實際登出失敗。
- **位置**：[`app/main.py:1271`](../app/main.py)。
- **問題**：`delete_cookie("session")` 未帶 set 時的 `samesite`/`secure`/`path`，
  某些瀏覽器組合下可能刪不掉。

> **本輪確認無問題的安全面**（避免重複稽核）：CSRF 是正牌 double-submit（簽名 + `compare_digest`）；
> 無任何吃使用者 URL 的對外 fetch（無 SSRF 面）；上傳檔名經 `Path().name` + uuid，無路徑穿越；
> OIDC 強制 HTTPS 且 issuer 綁定；例外處理不外洩 traceback。

---

## 2 · 效能（PERF）

> 已列在 [`PERFORMANCE.md`](PERFORMANCE.md) 的項目（P1-2 FTS5、P3-2 三次 LLM 呼叫）不重複列。

### [x] PERF-1 · P1 · `upload_source` 是 `async def` 但內部全是 blocking I/O
> **已完成**（PR #93，與 SEC-2 同一支）。durable 紀錄見 [`PERFORMANCE.md`](PERFORMANCE.md) `P1-4`。
- **位置**：[`app/main.py:1681`](../app/main.py)。
- **問題**：`shutil.copyfileobj`（磁碟寫入）、多次 `connect()`（SQLite）、`enqueue_source()`
  全部同步執行在 event loop 上。上傳大檔期間整個 web process 的所有請求被卡住。
- **修法**：改 `def`（FastAPI 自動丟 threadpool）或包 `run_in_threadpool`。
- **一起做**：SEC-2（同一個函式）。

### [x] PERF-2 · P2 · 其他 `async def` route 在 event loop 上直接跑 SQLite
> **已完成（2026-08-29）** — async LLM routes 的 SQLite 階段改由
> `asyncio.to_thread` 執行，連線在 worker thread 內建立與關閉；durable 紀錄見
> [`PERFORMANCE.md`](PERFORMANCE.md) `P2-4` 與 [`DEVELOPMENT.md`](DEVELOPMENT.md)
> 的 async route SQLite 邊界說明。
- **位置**：`ask` / `notebook_briefing` / `notebook_compare` / `notebook_suggestions` 等。
- **原現況判定**：查詢短、POC 規模可接受，原先延後；本輪因已排入目標而完成。

### [ ] PERF-3 · P3 · OIDC discovery / JWKS 每次登入重抓
- **位置**：[`app/main.py:1030`](../app/main.py) `_oidc_discover()`。
- **問題**：無快取，每次登入多 2 次對外 HTTPS 往返。加 TTL cache 即可。

---

## 3 · 檢索品質（QLT）

### [ ] QLT-1 · P1 · 關鍵字候選「先截斷、後評分」
- **位置**：[`app/retrieval.py:186`](../app/retrieval.py) `keyword_candidates_from_sqlite()`。
- **問題**：`WHERE ... LIKE ... ORDER BY chunks.id DESC LIMIT limit*4`——
  **先照 id 倒序截斷，才做 `keyword_score` 排序**。大 corpus 下，較舊但關鍵字命中更好的 chunk
  會在評分之前就被丟棄。
- **影響**：這是**品質**問題，不只是速度問題。與 `PERFORMANCE.md` P1-2（FTS5）是獨立的兩件事，
  在 FTS5 之前就能改善（例如把截斷改成基於命中數的粗排）。
- **歸屬**：應記進 [`QUALITY.md`](QUALITY.md)，不是 `PERFORMANCE.md`。

---

## 4 · LLM 相容性（LLM）

**背景查證結論**：「新版本模型不支援 temperature、改支援 effort」**半對**。
OpenAI GPT-5.x 是**條件式支援**（`temperature`/`top_p` 僅在 `reasoning.effort = none` 時可用，
effort 值域隨版本變動）；Anthropic Claude 4.7+ `temperature` 已 deprecated；
開源／vLLM（本專案目前部署的 Gemma）**不受影響**。
業界無標準的參數能力查詢 API（`openai-python` issue #3073 至今 open）。

### [x] LLM-1 · P1 · HTTP 400「不支援的參數」無法診斷
> **已完成** — PR #96 — 400 + 指名參數的錯誤改為專屬訊息，指引到設定頁重新測試。
- **位置**：[`app/main.py:367`](../app/main.py) `friendly_error_message()`。
- **問題**：400 在 [`app/llm.py:303`](../app/llm.py) 被歸為不可重試的 4xx，
  然後 `friendly_error_message` 對非 401/429/5xx 一律回 `error.generic_check`。
  admin 只會看到「請檢查設定」，看不到「這個模型不吃 temperature」。
- **修法**：加一條 400 + `unsupported`/`invalid parameter` 的專屬訊息。約 10 行。

### [x] LLM-2 · P1 · `build_chat_request` 無條件送 `temperature`
> **已完成** — PR #96 — `/settings` 測試連線時**實測**模型接受哪些參數並記住，`build_chat_request` 依結果決定是否送 `temperature`、用 `max_tokens` 還是 `max_completion_tokens`。
- **位置**：[`app/llm.py:2220`](../app/llm.py)。
- **問題**：payload 永遠帶 `temperature`，無 provider/model 判斷。
  若部署選 `azure_openai` + GPT-5.x（effort ≠ none），**每一次 chat 呼叫都會 400**——
  query rewrite、rerank、answer、briefing、compare、eval judge 全部走同一個函式，等於全站失效。
- **實測案例**：同事測試時用 **GPT-5.4-mini** 已踩到。
- **已裁決的修法**：**自動探測**（非讓使用者手動勾選）。理由：使用者不一定熟知模型有哪些參數可用，
  開放自選會選到不合適的。
  1. `probe_chat_diagnostics`（[`app/llm.py:537`](../app/llm.py)）新增 `sampling_params` 能力探針
     ——與既有的 `streaming`/`usage_reporting`/`json_following`/`image_understanding` 同構。
  2. 結果存進既有的 `llm_settings.diagnostics_json`。
  3. `build_chat_request` 依探測結果決定是否送 `temperature`。
- **落點**：`ROADMAP.md` `O1`「admin capability probes」已預留此位置。

### [x] LLM-3 · P1 · chat 請求完全不送 `max_tokens`
> **已完成** — PR #96 — 新增 `[max_tokens]` 依 `call_type` 給輸出上限；調參說明見 [`DEVELOPMENT.md`](DEVELOPMENT.md) → *LLM output caps*。
- **位置**：[`app/llm.py:2220`](../app/llm.py)。
- **問題**：無輸出上限。在借來的共用 endpoint 上，一次失控的長輸出會長時間佔住 GPU。
- **已裁決**：與 LLM-2 一起做（動到同一段 payload 組裝）。值放 `app/config.py` 新增群組，
  不要散落寫死（AGENTS.md 既有的 tunable 規範）。
- **注意**：GPT-5 系列把 `max_tokens` 改名 `max_completion_tokens`，需與 LLM-2 的能力探測一併處理。

#### 建議起始值（繁中輸出，抓法＝預估上限 × 2）

| call_type | 產出形狀 | 中文預估 tokens | 建議上限 |
|---|---|---|---|
| `settings_chat_probe` | 診斷探針，回 `ok` | ~10 | **128** |
| `query_rewrite` | JSON array，1–4 條短查詢 | ~60–150 | **512** |
| `followups` | JSON，3 個短問題 | ~250 | **512** |
| `summary` | 2–4 句來源摘要 | ~150–250 | **512** |
| `rerank` | JSON，20 × `{"id","score"}` | ~280 | **768** |
| `starter_questions` | JSON，4 題各 <80 字 | ~350 | **768** |
| `briefing` | 一段 80–110 詞 → 中文 200–300 字 | ~300 | **768** |
| `translate` | 摘要翻譯 | ~300 | **768** |
| `eval_judge` | JSON 三維度 + rationale + 未支撐主張 | ~300–600 | **1536** |
| `chat` / `eval_answer` | 有引用的答案，來自 6 個 chunk | ~300–800 | **2048** |
| `minutes` / `compare` / `artifact` / `eval_authoring` | 多段標題 + bullets 的長文 | ~800–2000 | **3072** |

三個必須知道的前提：
1. **`max_tokens` 是截斷上限，不是長度目標。** 設太低不會讓模型寫短，是寫到一半被切斷。
2. **對回 JSON 的呼叫，截斷 = 靜默品質下降。** 呼叫端都有 `try/except` 優雅降級
   （rerank 退回 hybrid 排序、rewrite 退回原問題），所以**不會報錯**——
   會得到「檢索品質莫名變差但日誌很乾淨」的系統。JSON 類上限要抓寬。
3. **中文 ≈ 1 字 1 token，英文 ≈ 4 字元 1 token。** 專案自己的
   `config.py` 已有正確常數（`cjk_chars_per_token=1.0` / `latin_chars_per_token=4.0`）。
   用英文語感抓的數字套到中文會少四倍然後被截斷。

**換成實測值的方法**（三個月後應該作廢上表）：

```bash
sqlite3 data/app.sqlite3 "WITH ranked AS (SELECT call_type, completion_tokens, NTILE(20) OVER (PARTITION BY call_type ORDER BY completion_tokens) AS b FROM llm_usage_events WHERE status='succeeded' AND completion_tokens IS NOT NULL AND is_estimated=0) SELECT call_type, COUNT(*) n, MAX(completion_tokens) p95 FROM ranked WHERE b<=19 GROUP BY call_type ORDER BY p95 DESC;"
```

取 p95 × 1.5。**必須加 `is_estimated=0`**，原因見 LLM-5。

### [x] LLM-4 · P2 · `estimate_tokens` 對中文低估約 4 倍
> **已完成（2026-08-23）** — Han／kana／Hangul 與非 CJK 字元分開計數，套用既有
> `[diagnostics].cjk_chars_per_token` / `latin_chars_per_token`；chat、embedding、
> diagnostics 與 streaming 路徑皆傳入 CJK 計數。durable 紀錄見
> [`DEVELOPMENT.md`](DEVELOPMENT.md) → *LLM output caps* 與 [`SCHEMA.md`](SCHEMA.md)。
> Review follow-up 同時修正 partial provider usage：只要 prompt/completion 任一欄
> 需要估算，整列保守標記 `is_estimated=1`。
- **位置**：[`app/governance.py:52`](../app/governance.py)。
- **問題**：`(chars + 3) // 4` 寫死英文比例。當 endpoint 未回報 usage 時（`is_estimated=1`），
  中文輸出的 token 數被系統性低估約 4 倍。
- **影響**：(a) 上面 LLM-3 的抓值失真；(b) **成本透明度報表系統性低報中文用量**。
- **修法**：改用 `config.py` 既有的 `cjk_chars_per_token` / `latin_chars_per_token`，
  依 CJK 佔比加權——這兩個常數已經存在，只是沒被用在這裡。

### [x] LLM-5 · P3 · effort 抽象層（13 個呼叫點的語意化）
> **已完成（2026-08-29）** — 原盤點的 13 處中，11 個功能呼叫點改以
> `deterministic`／`precise`／
> `balanced`／`exploratory`／`creative` 表達生成語意；支援 sampling 的 endpoint 仍收到
> 原本完全相同的 0.0／0.2／0.3／0.4／0.6。若聊天模型拒絕 temperature，設定頁會以
> 實際請求分別測試 `reasoning_effort=low|medium`；設定頁另提供自動、Provider 預設與
> 固定值 policy，固定模式只額外測所選值，未實測通過前不能儲存。正式請求只使用探測
> 確認且與目前設定 fingerprint 相符的值；未探測、結果不明或設定已變更時不推測能力。
> Image understanding probe 同步改為 64×64 限定色彩測試，將 request failure 與
> accepted-but-inconclusive 分開。durable 契約見
> [`DEVELOPMENT.md`](DEVELOPMENT.md) → *Semantic sampling and reasoning effort*、
> [`SCHEMA.md`](SCHEMA.md) 與 [`ROADMAP.md`](ROADMAP.md) O1b。
- **位置**：`app/llm.py` 中 13 個寫死 temperature 的呼叫點（0.0 / 0.2 / 0.3 / 0.4 / 0.6）。
- **問題**：這些數字承載的是**語意**（「這個任務要多少隨機性」），但語意只存在於數字本身、沒有名字。
  真要支援 effort，需要一張語意對照表（決定性任務 → effort low、發想型任務 → effort medium），
  不是把 temperature 刪掉。
- **歷史決定**：原先因部署的 serving 端固定是 Gemma 而暫緩；同事以 GPT-5.4-mini
  測試後已觸發實作條件。此次保留 Gemma 行為，同時加入不依賴 model 名稱的安全轉譯。
- **保留的低階呼叫**：另外 2 處是 sampling／streaming diagnostics，本來就必須直接
  指定探測參數；它們不代表產品任務語意，刻意不套用 `ChatIntent`。

---

## 5 · 維護性（MNT）

### [x] MNT-1 · P2 · 26+ 處使用者可見字串繞過 i18n catalog
> **已完成（2026-08-23）** — web route modules 的使用者可見
> `HTTPException.detail` 已移入 catalog；完成時的跨檔掃描另補上原 inventory 漏列的
> `app/evals.py` / `app/settings.py`，AST 回歸測試禁止 literal/f-string 倒退。
> durable 紀錄見 [`I18N.md`](I18N.md) → *Add a new UI string*。
- **位置**：`app/main.py` 的 `raise HTTPException(detail="找不到來源")` 類共 26 處以上；
  `app/admin.py` 另 4 處。
- **問題**：這些會被 `render_error` 印在 error page 上，是實打實的 user-facing copy。
  [`I18N.md`](I18N.md) 的「Known exceptions」列了 4 類，**不含這一類**——
  文件宣告的規則與程式現況不一致。
- **兩條路**：補進 catalog，**或**列成第 5 條 documented exception。
  傾向前者（量不大，且 U15b 多語系一旦要做，這批就是漏網的那批）。

### [ ] MNT-2 · P3 · 抽 `app/web.py` 打破循環相依
- **現況**：`app/main.py` 3700 行 / 48 routes，是唯一還在長大的檔案。
  retrieval / evals / admin / settings 已抽出，方向正確。
- **可再抽的三塊**：auth（login/logout/trusted-header/OIDC，約 640–1270 行）、
  Studio tools + A4 generators（3380 行之後，程式碼已自畫分隔線）、notes/notebooks CRUD。
- **關鍵**：**第一步不是拆 route**。`main.py` 是 import root，其他模組要 import 回來拿
  `render`/`require_admin`/`record_audit_event`，這個循環相依才是拆不動的真正原因。
  正確順序是先把共用 helper 抽成 `app/web.py`，打破循環，之後拆哪一塊都安全。
- **建議**：獨立一輪處理，不要跟其他修改混在同一個 diff。

---

## 6 · 文件（DOC）

### 過時

### [x] DOC-1 · P0 · `SECURITY.md` 的 demo 帳號指示實際無效
> **已完成** — PR #91 — `SECURITY.md` 的 Bootstrap accounts 段落已改寫，並留下歷史註記。
- **位置**：[`SECURITY.md:22`](SECURITY.md)「Change or **remove** them before exposing the app on a network」。
- **問題**：SEC-1 已證明「remove」無效（重啟就種回來）。
  **這是最危險的一種過時：文件給了一個會讓人以為自己安全的操作步驟。**
- **相依**：SEC-1 修完後一起改寫。

### [x] DOC-2 · P1 · `SECURITY.md`「無已知延後的加固項目」需改寫
> **已完成** — PR #93 — Hardening status 已改為分「已修正 / 仍未關閉」，並更新最高風險項。
- **位置**：[`SECURITY.md:44`](SECURITY.md)「No currently known application-level hardening item is intentionally deferred here.」
- **問題**：SEC-1～SEC-8 一旦記錄，這句話即不成立。

### [x] DOC-3 · P1 · `E1E2_ANSWER_JUDGING_PLAN.md` 狀態寫錯
> **已完成** — PR #94 — 狀態改正並搬至 [`archive/`](archive/E1E2_ANSWER_JUDGING_PLAN.md)。
- **問題**：開頭寫「狀態：已規劃、**待實作**（將由另一個 session 執行）」，
  但 E1e-2 早已完成（`ROADMAP.md:169` 標 `[x] Done`、`tests/test_evals_judge.py` 439 行在跑）。
- **修法**：改狀態 + 搬 `docs/archive/`。

### [x] DOC-4 · P1 · `O0_DIMENSION_RESET_PLAN.md` §6 收尾清單全未打勾
> **已完成** — PR #94 — §6 收尾清單 8 項全數補打勾，搬至 [`archive/`](archive/O0_DIMENSION_RESET_PLAN.md)。
- **問題**：8 項全是 `[ ]`，但檔案開頭同時寫「**已完成**（Phase A–D 全數實作）…暫行警語已全數移除」。
  已驗證：清單指名的 5 個檔案（README / README.zh-TW / AGENTS / RETRIEVAL / DEVELOPMENT）
  **P0 警語確實都已移除**。是清單沒打勾，同一份文件自己打自己。
- **修法**：打勾 + 搬 `docs/archive/`。

### [x] DOC-5 · P2 · `UX_REVIEW.md` 是 2026-06-19/20 的全結案快照
> **已完成** — PR #94 — 搬至 [`archive/2026-06-19-UX_REVIEW.md`](archive/2026-06-19-UX_REVIEW.md)；方法論 `UX_REVIEW_GUIDE.md` 留在原地。
- **問題**：所有 H/M/L/V 項目**全部 `[x]`**，0 個未結。檔案自己開頭就說「這是某個時間點的走查快照」。
- **修法**：搬 `docs/archive/2026-06-19-UX_REVIEW.md`。
  （方法論 `UX_REVIEW_GUIDE.md` 是durable 規範，**留在原地**。）

### 遺漏

### [x] DOC-6 · P1 · 發版流程完全沒有文件
> **已完成** — PR #94 — 新增 [`RELEASE.md`](RELEASE.md)。
- **問題**：「功能 PR 只累積 CHANGELOG `[未發布]`、VERSION bump 是獨立的 `chore(release)` PR」
  這套慣例在 repo 裡 grep 不到任何一處，只存在維護者腦中。
- **修法**：新增 `docs/RELEASE.md`。以可交接性標準，這是最該補的一份。

### [x] DOC-7 · P1 · CI 完全沒有文件
> **已完成** — PR #94 — 併入 [`RELEASE.md`](RELEASE.md) 的 CI 段落。
- **問題**：`.github/workflows/ci.yml`（每個 PR 跑 py_compile + pytest）、
  `dependabot.yml`、`release.yml` 都在運作，但 `DEVELOPMENT.md` 的 Verification 章節
  只講本機怎麼跑，**沒有一個字提到 CI 會擋 PR**。
- **修法**：併入 DOC-6 的 `docs/RELEASE.md`。

### [x] DOC-8 · P1 · 部署環境事實無 committed 來源
> **已完成** — PR #94 — 新增 [`DEPLOYMENT_CONTEXT.md`](DEPLOYMENT_CONTEXT.md)；`QUALITY.md` 與 `PERFORMANCE.md` 的斷掉指標已改指向它。
- **問題**：`QUALITY.md` 和 `PERFORMANCE.md` 的「Deployment context」都指向 `../handover.md`，
  但 handover.md 是 gitignored。**而且已經斷了**——handover.md 目前**完全沒有** deployment context
  （Gemma / 使用者規模 / 硬體全部不在裡面）。
- **修法**：新增 `docs/DEPLOYMENT_CONTEXT.md`，寫**去識別化**版本。
  **措辭要求（已裁決）**：本專案已在**不只一個**客戶環境使用過，
  因此用「初期是為了在…環境中做 POC」的歷史框架描述由來，
  **不要**寫成「本產品的部署對象就是 X」。客戶名與機器細節留在 handover.md，不進 git。

### [x] DOC-9 · P2 · `app/index_migration.py` 不在任何架構圖
> **已完成** — PR #94 — `CLAUDE.md` 架構圖與 `DEVELOPMENT.md` layout 都補上了。
- **問題**：397 行、上週隨 0.3.0 出貨，但 `CLAUDE.md` 架構圖、`AGENTS.md`、
  `DEVELOPMENT.md` repository layout **三處都沒有它**。
  （O0 的**功能**文件寫得很完整，是**模組**沒被登記進地圖。）

### [x] DOC-10 · P2 · 架構圖缺其他模組
> **已完成** — PR #94 — 併同 DOC-9 補齊（`governance.py` / `i18n.py` / `version.py`）。
- `DEVELOPMENT.md` repository layout 缺 `app/i18n.py`（605 行）。
- `CLAUDE.md` 架構圖缺 `governance.py`、`i18n.py`、`version.py`。

### [x] DOC-11 · P3 · `ROUTES.md` 缺兩個 legacy redirect
> **已完成** — PR #94 — `ROUTES.md` 現為 82/82 完全同步。
- **問題**：`/chat`、`/sources`（`legacy_redirect`）未記載。
  **其餘 80/82 條 route 全部有記載**——已用程式逐條比對。

### 不一致

### [x] DOC-12 · P2 · 兩份 README 已漂移
> **已完成** — PR #94 — 兩份 README 的 doc map 已一致。
- **問題**：`README.zh-TW.md` 的 doc map 比 `README.md` **少 3 份**——
  `AUTHENTICATION.md`、`I18N.md`、`SSO_DEPLOYMENT.zh-TW.md`。
  中文讀者剛好看不到企業 auth 與 i18n。

### [x] DOC-13 · P2 · 4 份文件不在任何頂層索引
> **已完成** — PR #94 — `AGENTS.md` 新增依用途的分類，`docs/` 覆蓋率 100%。
- `E1E2_ANSWER_JUDGING_PLAN.md`、`PRODUCT_DESIGN_NOTES.md`、`UX_REVIEW.md`、`UX_REVIEW_GUIDE.md`
  （只能從 ROADMAP/QUALITY 內文偶然連到）。
- **已裁決**：**不新增 `docs/README.md`**，分類直接長進 `AGENTS.md` 的
  「Context To Read First」。

### [x] DOC-14 · P2 · `CLAUDE.md` 的 quick index 漏了 auth/SSO
> **已完成** — PR #94 — quick index 補上 auth/SSO、發版、部署脈絡三個 gate。
- **問題**：要改登入的 session 從 `CLAUDE.md` 讀不到 `AUTHENTICATION.md` 的 gate，
  得繞到 `AGENTS.md` 才有。auth 是高風險區，不該留這個漏洞。

### [x] DOC-15 · P2 · `handover.md` 瘦身
> **已完成** — PR #94 — 可進 git 的那一半（`AGENTS.md` Verification）已併入；`handover.md` 本身是 gitignored 的本機檔案，另行瘦身。
- **建議（待確認）**：**瘦身，不刪除**。逐段拆解後 85% 重複，但非零價值：
  - **保留**：Current Workspace（repo 路徑、branch 狀態、殘留 `origin/codex/*` 遠端分支、
    不可 commit 的本機檔案）——只有這段不可取代。
  - **併入 `AGENTS.md` 後刪除**：Verification 的 `git diff --check`
    與「前端改動要在桌機與手機寬度各做一次 browser smoke test」（AGENTS.md 目前沒有這兩項）。
  - **直接刪除（重複）**：Authoritative Documents（＝README doc map 副本）、
    Recent Merged Work（＝CHANGELOG）、Immediate Follow-Up（＝ROADMAP）、
    Safety Notes（＝AGENTS.md Persistence And Safety）。
- **結果**：收成約 10 行的純短期工作區狀態。

> **本輪確認無問題的文件面**（避免重複稽核）：
> - `ROUTES.md` — 80/82 route 有記載（程式逐條比對）
> - `SCHEMA.md` — 20 個 table、**所有欄位名全部對得上**（建臨時 DB 跑 `PRAGMA table_info` 逐欄比對），
>   AGENTS.md 那條「改 schema 必須同步 SCHEMA.md」的硬規則**真的被遵守了**
> - 全部 `.md` 的相對連結 — **0 個死連結**
> - `CHANGELOG.md` / 版號慣例 — 一致

---

## 7 · 建議的 PR 切法與相依

| # | PR | 內容 | 理由 |
|---|---|---|---|
| 1 | `fix(auth)` | SEC-1 | 唯一「文件教的做法實際無效」的安全洞，改動小。含 schema 變更 → 同步 `SCHEMA.md` |
| 2 | `fix(upload)` | SEC-2 + PERF-1 | 同一個函式；一般使用者就能打爆記憶體，順手修掉 event loop 阻塞 |
| 3 | `feat(llm)` | LLM-1 + LLM-2 + LLM-3 | 三者動到同一段 payload 組裝與診斷框架，分開做要改三次 |
| 4 | `docs` | DOC-1 + DOC-2 + DOC-6 + DOC-7 + DOC-8 | 安全修法落地後才改 SECURITY.md；同時補上 RELEASE / DEPLOYMENT_CONTEXT |
| 5 | `docs` | DOC-3 + DOC-4 + DOC-5 + DOC-15 | archive 三份 + handover 瘦身，純搬移與狀態修正 |
| 6 | `docs` | DOC-9～DOC-14 | 索引與架構圖補洞，純機械性 |
| 7 | `fix(auth)` | SEC-3 | 補上「改密碼能踢人」這個基本能力 |
| 8 | `refactor(i18n)` | MNT-1（已完成 2026-08-23） | 純機械性，讓文件與程式一致 |
| 9 | 後續輪次 | PERF-3、QLT-1 | PERF-2 已於 2026-08-29 完成並記進對應 backlog |
| 10 | 獨立一輪 | MNT-2 | `app/web.py` 抽離，不要跟其他改動混在同一 diff |

**硬相依**：
- DOC-1 → SEC-1（安全修法決定文件怎麼寫）
- DOC-2 → SEC-1～SEC-8 全部（要先知道哪些會修、哪些延後）
- LLM-3 的實測值 → LLM-4（**已滿足 2026-08-23**；估算值仍非 provider 實測）
- QLT-1 的效果驗證 → `QUALITY.md` Q1-3（代表性 eval set，目前未做）
