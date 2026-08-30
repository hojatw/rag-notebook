# 變更紀錄（Changelog）

本檔記錄本專案對使用者/維運者有感的變更。
格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版號遵循 [語意化版本](https://semver.org/lang/zh-TW/)。專案仍在 `0.x` 階段：
新功能進 MINOR，純修正與依賴更新進 PATCH。

版號的單一事實來源是 repo 根目錄的 `VERSION` 檔（見 `app/version.py`）。

## [未發布]

### 新增

- **U17 topic-focused source comparison**：來源比較重新提供選填的「比較主題」。
  留白時維持原本的摘要比較；輸入主題時，對每份已授權來源依序執行既有 hybrid
  retrieval，再以取得的 chunks 整理「共同點」、「差異」與「待釐清」。Topic mode 限 2–3 份來源，
  超過上限會在任何 retrieval 前拒絕；無結果或低於既有信心門檻的來源會明確標示
  為沒有足夠主題證據，不回退到可能無關的 summary，也不允許模型自行補推論。
  無主題的摘要比較限制為 2–10 份來源，超過上限會在生成前顯示錯誤；這是輸入量與
  報告複雜度的操作護欄，不保證任意模型的 context window 都足夠。
  兩種模式共用中性比較提示詞：按面向並列來源說法，不把不同專案／版本或缺少證據
  直接判成矛盾；待釐清事項須指出依據及需要確認的條件。這不是全文差異或文件效力
  的自動判定，模型輸出仍需人工確認；既有已儲存筆記不改寫，重新生成才會套用。
- **回答串流的部分串流能力（預設關閉）**：新增
  `[runtime].answer_stream_gate_chars`。串流回答原本會把整段生成緩衝完才一次
  顯示，因為要先分類 `[[RAG_ABSTAIN]]` 結構標記；設成正整數後改為「前 N 個
  字元緩衝並分類，通過才逐步輸出」，並永遠扣住任何可能長成標記的結尾片段
  （providers 會把標記拆在兩個 chunk 之間）。若分類在已顯示文字之後才判定
  拒答，伺服器會送出 `discard` SSE 事件讓前端立即清空。
  **預設為 `0`（維持原本的全緩衝）**，因為那是最強的防幻覺保證，且效益完全
  取決於上游是否真的逐步送：實測 Azure OpenAI 的 243 個 chunk 中，第一個在
  串流全長的 90% 才抵達（中位間隔 0ms），開啟閘門只快 0.2 秒卻要放棄保證。
  啟用前請先用新增的 `python -m tests.probe_provider_stream` 量測上游行為，
  方法與 120 這個建議值的推導記在 `docs/DEVELOPMENT.md`。持久化、引用與
  `metadata.outcome` 一律仍以完整文字分類，不受此設定影響。

### 修正

- **來源比較的來源辨識**：結果加入由程式產生的來源編號／檔名對照與證據狀態，
  可看出每份來源使用摘要、取得幾段主題證據，或未取得足夠證據；對照隨筆記與
  匯出保留。提示詞要求每項共同點、差異與待釐清事項標明來源，部分共同點需明示
  適用哪些文件，避免「兩份報告」與裸 `[1]` 引用造成混淆。證據狀態不是模型完整
  涵蓋各來源的保證；既有結果需重新生成。
- **來源比較的驗證錯誤未顯示**：有主題且超過 3 份來源時，後端雖已拒絕，HTMX
  原先會略過 HTTP 400 的訊息。比較視窗現在會顯示紅色錯誤提示（包含無主題超過
  10 份來源的驗證錯誤），保留選擇與主題
  供修正後重試；HTTP 400 與其他頁面的錯誤處理不變。
- **聊天模型診斷未進入 runtime request builder**：`diagnostics_json` 原本只供設定頁
  顯示，`load_llm_settings()` 沒有載入內容，因此已實測的 sampling／token-limit 能力
  在正式呼叫永遠不可見。Runtime 現在會解析診斷，但只接受與目前 non-secret 設定
  fingerprint 相符的結果；換 endpoint、model 或任一 connection 設定後，舊結果會
  安全失效並要求重新測試。
- **維度遷移永遠被擋住**：`/admin/index` 的遷移閘門比對 embedding 診斷的
  `status != "ok"`，但探測寫入的值是 `"succeeded"`——`"ok"` 這個字串在整個
  codebase 裡只存在於那一行比較。因此無論在「設定」頁測試成功幾次，紅色的
  「尚無法遷移」都不會消失，**整個 O0 維度遷移流程從 UI 無法觸發**。狀態值
  改由 `llm.DIAGNOSTIC_STATUS_SUCCEEDED` 共用，兩端不會再漂移。
  相關測試原本自己偽造 `{"status": "ok"}` 餵給讀取端，等於把錯誤假設抄進
  測試；已改用真值，並新增一個串起「探測 → 存檔 → 閘門」的契約測試。
- **重新索引後來源狀態不更新**（兩個獨立成因）：
  1. `enqueue_source()` 只寫 `ingest_jobs`，沒有改 `sources.status`。而來源列
     只在狀態為 `uploaded`/`processing` 時才帶 HTMX 輪詢屬性，所以重新索引後
     那一列以 `indexed` 重繪且完全不輪詢。重新索引改為在同一個請求內把狀態
     設回 `uploaded` 並清除舊的錯誤訊息，與上傳路徑一致。
  2. 修好上面那點之後狀態會顯示「已上傳」，卻仍然不會變成「處理中」——因為
     `extract_sections()` 是同步 CPU 密集工作卻直接跑在 event loop 上，開著
     inline worker（預設）時**整個 app 在抽取期間停止回應**，實測一份 881
     sections 的 PDF 凍住 32 秒，而那正好覆蓋 processing 狀態的整個存在期間。
     抽取改用 `asyncio.to_thread`。修正後同一份 PDF 抽取的 34 秒內，伺服器
     正常服務了 7 次狀態輪詢。副作用：該期間請求延遲從約 4ms 升到約 200ms
     （GIL 競爭）；正式環境仍建議用獨立的 `python -m app.worker`。
- **設定頁測試後跳回頁首、且還原未儲存的設定**：測試鈕原本是 `formaction`
  整頁送出，重繪時用資料庫的值填回表單——捲動位置與剛剛拿去測試的輸入值
  兩者都會消失。改為 HTMX 局部更新，只換回診斷卡片；表單完全不動，兩個問題
  一併消失，測試結果的提示也改為顯示在卡片內而非頁首。無 JS 時仍可整頁運作。
- **Image understanding 診斷誤報 failed**：舊 probe 的圖片只有 2×2，且只接受回覆中
  含字面 `red`；endpoint 已接受 image request、回傳有效 JSON 時仍可能被誤判失敗。
  改用 64×64 內建紅色 PNG 與 `red|green|blue` 限定輸出；request/endpoint 失敗維持
  `failed`，已接受但語意不足改標「無法判定」，只有 exact match 才是 `succeeded`。
- **領域設定頁的「啟用」核取方塊破版**：`.setting-check` 只在 `.rename-form`
  底下有樣式，領域提示頁的三處用法沒被涵蓋，掉回基礎 `label` 的 grid 直排——
  核取方塊被撐成整欄寬（實測 654px）、看起來置中，說明文字被擠到第二列，且整
  條寬度都是點擊熱區。改為建立**全域** `.setting-check` 基礎樣式，這個 class
  以後在任何頁面都不會再是無樣式狀態。

### 效能

- **Async LLM routes 不再於 event loop 直接執行 SQLite（PERF-2）**：starter
  questions、briefing、compare、chat／streaming chat、follow-ups、meeting minutes、
  artifacts 與 summary translation 的 bounded SQLite 階段改由
  `asyncio.to_thread` 執行。每個同步 helper 都在 worker thread 內自行開關連線，
  並把相關查詢合併成單一階段，避免 lock wait 或慢磁碟讓同一 worker 的所有 request
  一起停住。新增 route-level 測試，以延遲真實連線邊界確認 event loop 仍可持續排程。
- **CSV ingestion 改為串流解析（P1-5）**：不再以 `read_bytes → decode → splitlines`
  同時保留多份整檔內容；改由 64 KiB 樣本判斷 encoding／delimiter，再以 incremental
  decoder `TextIOWrapper(newline="")` 將 physical lines 送入 `csv.reader`。即使超過
  列數上限仍驗證到 EOF，後段才出現的 Big5 會觸發 bounded re-detection 與串流重讀；
  quoted field 的內嵌換行也不再被 `splitlines()` 刪除。20 MB parser cap 仍保留以限制
  總工作量與單一巨型 record。

### 安全性

- **管理員建立帳號失敗不再洩漏 SQLite 例外（SEC-7）**：頁面只顯示 i18n 通用錯誤，
  完整例外與 traceback 保留在 server log，避免把 constraint／schema 細節帶入 HTML。
- **登出 cookie 屬性與登入發行對齊（SEC-8）**：session cookie 的發行與刪除共用
  `Path=/`、`HttpOnly`、`SameSite=Lax`，並依 HTTP／HTTPS request scheme 一致設定
  `Secure`。舊版 Starlette 的 deletion 已預設 `Path=/`，因此這是避免未來設定漂移的
  契約硬化，未宣稱已重現既有瀏覽器的登出失敗。

### 變更

- **LLM-5 semantic intent 與 reasoning effort**：11 個功能呼叫點不再各自寫死
  temperature，改以 `deterministic`、`precise`、`balanced`、`exploratory`、
  `creative` 表達任務語意。支援 sampling 的 Gemma／OpenAI-compatible endpoint 仍收到
  完全相同的 0.0／0.2／0.3／0.4／0.6；拒絕 temperature 的模型則只會收到設定頁
  實測成功的 `reasoning_effort`，不以 model 名稱猜測能力。設定頁新增自動、Provider
  預設與固定值 policy；自動模式探測 `low|medium`，固定模式再探測所選值，且未實測
  通過前不能儲存。探測失敗、不確定或過期時不送 effort，維持既有 request 行為。
- **領域提示與回答政策頁改版**：兩個「啟用」開關改用新的 `.switch-field`
  整列式 toggle（左標題＋說明、右開關、整列可點）。筆記本層級設定收進一張卡片、
  儲存列改放在卡片框線內，不再停在頁面中段看起來像「儲存全部」；提示清單成為
  卡外的獨立區塊。每張提示卡以**該筆術語**為標題（原本每張都叫「編輯領域提示」），
  刪除鍵移到卡片右上、與右下的儲存鍵分開。
- **區塊標題層級修正**：全域 `h2` 是 12px uppercase 的 overline 樣式，中文沒有
  uppercase 效果，實際比底下 13px 的欄位標籤還小。領域設定頁改走 `.section-head`
  （18px），並在 `docs/UI.md` §3.2 記下這個 CJK 陷阱。
- **`.empty-state` 補上樣式**：先前模板已在用這個 class，但 CSS 從未定義它。
  現定為區塊層級空狀態，與整頁層級的 `.empty` 分成兩級（`docs/UI.md` §3.9）。

## [0.5.0] - 2026-08-24

### 新增

- **E2 Notebook domain hints / answer policy**：notebook owner 可維護有界的
  terms、synonyms、definitions、query expansions、answer notes 與 answer policy。
  Hints 只在 query time 參與 rewrite，不需 re-index 或額外 LLM call；answer policy
  約束回答方式，但不能取代來源證據。Eval Workbench 可用 baseline／hints／policy／combined
  模式執行同一題集，凍結 domain snapshot，並選擇 answer/citation judging。

### 安全性

- **E2 prompt／export 邊界**：sanitized export 只含 domain 摘要，full internal
  export 使用 CSRF-protected POST explicit confirmation，並延續 admin 與
  high-sensitivity audit 邊界。Domain mutations 使用 64 KiB request 上限、序列化
  revision 寫入及 case-insensitive unique term index；frozen snapshot 採 exact-version、
  bounded canonical validation。Answer policy／notes 保持在 user role，provider stream
  先完成 bounded classification，確保結構化 abstain marker 不會部分外洩。

- **登入速率限制（SEC-4）**：本機帳密登入新增 SQLite 共用的帳號失敗桶與短租約
  password-verification slots；預設帳號 15 分鐘 5 次、整個部署同時最多 4 個
  PBKDF2，且同帳號一次只驗證一個。達門檻或容量忙碌時回通用 HTTP 429；bucket id
  以 `NOTEBOOKLM_SECRET` 做 HMAC，不保存原始帳號或 `X-Forwarded-For`。未採用可被
  任意帳號流量填滿的全域 cooldown，避免登入保護本身成為全站 DoS。
- **中文 prompt-injection 訊號（SEC-5）**：`local.rules.v2` 增加繁中／簡中
  ignore、reveal、bypass 規則與正反例測試；維持 warn-only，不把 heuristic
  誤當成阻擋式安全邊界。
- **縮小登入使用者 context（SEC-6）**：`current_user()` 改成欄位白名單，
  `password_hash` 不再隨使用者資料進入 template/session request context。

### 修正

- **CJK-aware token 估算（LLM-4）**：provider 未提供完整 usage 時，改依 Han、kana、
  Hangul 與非 CJK 字元分別套用既有 diagnostics 比例；包含 chat、embedding、
  diagnostics 與 streaming 路徑，不再用固定 `chars/4` 系統性低估中文。只要
  prompt/completion 任一欄需估算，整列即保守標記 `is_estimated=1`；可由
  prompt/completion 與 total 相減得到的欄位則仍視為 provider 精確值。
- **檢索評測啟動條件**：`tests.eval_retrieval` 不再錯誤要求 API key 與 chat model；
  現在只要求 embedding model。Blank key 的本機 embedding service 可直接執行，
  未設定 chat model 時則量測 production 的 single-query／hybrid fallback。
- **Embedding-only ingestion readiness**：upload gate 不再錯誤要求 chat model；只要
  embedding model 已設定即可上傳／索引，per-source summary 在沒有 chat model 時維持
  best-effort skip。Chat、回答與其他生成式功能仍需 chat model。

### 維護

- **HTTP error i18n（MNT-1）**：`app/main.py`、`app/admin.py`、`app/evals.py` 與
  `app/settings.py` 的使用者可見 `HTTPException.detail` 移入 catalog，並加 AST
  測試防止重新硬編碼。Embedding endpoint 連線錯誤也不再把原始 exception 回顯給管理員。
- **發版前文件稽核**：重新對照 HTTP routes、SQLite schema、config、security、
  retrieval、Eval Workbench 與 E2 行為，修正 README、操作／架構文件、backlog、
  白皮書與封存設計紀錄中的過時或不完整敘述。

### 升級注意事項

- 啟動時會以既有 idempotent migration 自動建立 `login_rate_limits`、
  `login_verification_leases`、`notebook_domain_config`、`notebook_domain_hints`，並補上
  Eval run 的 domain snapshot／mode 欄位；不需手動 migration，也不需重新索引。
- Domain hints 與 answer policy 預設停用，升級後不會自行改變既有 notebook 的檢索或回答；
  owner 明確設定並啟用後才生效。限制值可由 `[domain_policy]` 調整。
- 本機登入 rate limiting 預設啟用：同帳號預設 15 分鐘最多 5 次失敗，PBKDF2 預設全部署
  最多 4 個並行 verification lease；可由 `[auth].login_*` 調整。
- 為防止結構化 abstain marker 部分外洩，SSE 仍即時傳送 retrieval／generation 狀態，
  但 provider answer 會完成 bounded buffering／classification 後才以單一 final event 顯示。

## [0.4.0] - 2026-08-23

### 新增

- **自動偵測聊天模型接受哪些請求參數（LLM-2）**：`build_chat_request` 先前**無條件**送出
  `temperature`，沒有任何 provider 或 model 判斷。GPT-5 世代的推理模型只在 reasoning
  effort 為 `none` 時才接受 sampling 參數，否則整個請求 400——而本專案**所有** LLM 呼叫
  都經過這個函式，所以那不是降級，是 chat／查詢改寫／rerank／簡報／評測**同時全掛**。

  業界沒有標準的能力查詢方式（`GET /v1/models` 不回傳能力，OpenAI 官方 SDK 的
  openai-python#3073 至今仍 open），唯二做法是硬編碼 model 名稱前綴（每次出新版就壞）
  或送出去看錯誤。「系統設定」→「測試聊天模型」現在會**實測一次並記住**：送一個帶
  `temperature` + `max_tokens` 的最小請求，若被拒且錯誤訊息指名了參數，再試
  `max_completion_tokens` 的形狀。之後每個請求依這個結果組裝。

  刻意只在「provider 明確指名參數」時才判定為不支援——逾時或 500 完全不代表能力如何，
  從那些推論會讓一次偶發失敗永久拿掉一個其實支援的參數。設定頁會顯示偵測到的請求形狀，
  且**不用紅色的「失敗」呈現**：不吃 temperature 的模型不是壞了，只是形狀不同。

- **LLM 回應長度上限（LLM-3）**：先前完全不送 `max_tokens`，在借來的共用推論端點上，
  一次失控的長輸出會長時間佔住 GPU。新增 `[max_tokens]` 設定群組，依 `call_type` 給不同
  上限（診斷探針 128、查詢改寫 512、答題 2048、長文產出 3072…）。GPT-5 系列把欄位改名為
  `max_completion_tokens`，用哪一個同樣由上述偵測決定。

  上限是**截斷限制而非長度目標**，且對回 JSON 的呼叫特別危險——呼叫端有 try/except
  優雅降級，截斷會表現為「檢索莫名變差但日誌乾淨」而不是錯誤，所以那幾類的上限抓得寬。
  預設值以繁中約 1 字 1 token 估算；有實際流量後應改用 `llm_usage_events` 的
  p95 × 1.5 取代，方法寫在 `docs/DEVELOPMENT.md`。

### 變更

- **設定鍵改名，舊名稱仍可用**：`max_source_bytes` → `extract_max_file_bytes`，
  並新增 `upload_max_file_bytes`。原本兩個名字讀起來像同義詞，實際上是管線中不同階段的
  兩種保護（上傳時／抽取時），新名稱以階段當前綴，看名字就知道誰先誰後。
  舊的 `max_source_bytes`（`config.toml` 與 `NOTEBOOKLM_RUNTIME_MAX_SOURCE_BYTES`）
  仍會被讀取，只是啟動時會記一筆 deprecation 警告，**既有部署不需要改設定**。

  中介層不再讀取 multipart body；上傳路由改為透過 `verify_multipart_csrf` 從自己解析出的
  表單驗證 token（header 與表單欄位都接受），`request.form()` 會溢寫到磁碟而非常駐記憶體。
  由於這把檢查責任移到了路由上，另加一道**啟動時檢查**：任何宣告 `UploadFile` 卻沒有
  掛上該相依的路由會讓程式直接啟動失敗——否則漏掉就是靜默不設防。

### 修正

- **「不支援的參數」錯誤現在看得懂（LLM-1）**：HTTP 400 先前一律歸類為
  「請檢查設定」的通用訊息。這是唯一一種管理員真的能處理的 4xx，而且症狀是全站 LLM
  功能同時失效，看起來像整體當機而不是一個欄位不被接受。現在會回一則明確訊息，
  指引到設定頁重新測試連線。

### 安全性

- **預設帳號不再是常設密碼（SEC-1）**：`init_db()` 在網頁程式與 worker 的**每次啟動**都會執行，
  而它過去無條件種入 `admin/admin123` 與 `user/user123`。這代表管理員照 `docs/SECURITY.md`
  的指示把示範帳號**刪掉**之後，下次重啟它們就會帶著原本的密碼回來——文件教的做法實際上無效。

  現在改為依環境決定：設有真實 `NOTEBOOKLM_SECRET` 的部署**只種入 `admin`**，而且該帳號
  **首次登入必須先變更密碼**才能使用任何其他頁面，等於把它從常設密碼降級為一次性的開通憑證；
  `user` 完全不種入，所以刪掉之後不會再回來。本機開發不受影響——新增
  `NOTEBOOKLM_SEED_DEMO_USERS` 可明確開關，未設定時沿用「是否使用不安全的開發用 secret」
  這個既有訊號，也就是登入頁決定要不要印出示範帳密的同一個判斷。

  **升級既有部署會有感**：下次啟動時，任何仍在使用初始密碼的帳號都會被標記為必須變更密碼
  （逐一驗證該帳號的密碼是否仍等於當初種入的值，改過的不受影響）。不會刪除任何帳號、不會遺失資料，
  但 `admin123` / `user123` 從此只能用來設定新密碼。建議升級後的第一次登入有人在場。

  檢查範圍刻意很窄：**只看這兩個帳號名**，而且只確認它們是否仍在使用系統發出去的初始密碼。
  你自己建立、密碼剛好也設成 `admin123` 的帳號不會被動到——這不是弱密碼稽核。
  **SSO 連結的帳號一律跳過**：`/account/password` 對外部身分是擋掉的，標記它等於鎖死沒有出口；
  這種情況改為記錄 `seeded_default_password_left_unflagged` 警告，由維運者關閉本機登入或以管理工具重設。

  強制變更的閘門放在 `require_login`，因此自動涵蓋所有需要登入的路由，包含管理後台；
  被鎖住的帳號只能到 `/account`、送出 `/account/password` 與登出，導覽列也一併隱藏，
  不會出現點了只會彈回來的連結。完成變更會寫入 `bootstrap_password_changed` 稽核事件。

- **上傳大小上限與記憶體上限（SEC-2）**：先前**完全沒有**任何上傳大小限制——只限制
  一次幾個檔（5 個），而 `max_source_bytes` 是**解析階段**才檢查，那時檔案早就寫進磁碟了，
  而且只涵蓋 `.xlsx` / `.pptx` / `.csv`，PDF 與 DOCX 根本不在範圍內。

  更嚴重的是 CSRF 中介層對 multipart 請求會呼叫 `await request.body()`，用正規表達式
  從原始 body 撈 token；而上傳表單是純 HTML form，不會帶 `X-CSRF-Token` header，
  所以**每一次上傳都必定把整包內容完整讀進記憶體**。一個正常登入的使用者傳幾個大檔
  就能把記憶體吃光，不需要任何漏洞。

  現在：新增 `[runtime].upload_max_file_bytes`（預設 50 MB）作為**每個檔案**在上傳當下的上限，
  涵蓋所有格式，且是邊寫入磁碟邊計數，超過就中止、刪掉半個檔案並回 413。整個請求另外以
  `upload_max_file_bytes × upload_batch_limit` 為界，直接從 `Content-Length` 判斷，
  **在讀取任何 body 之前**就拒絕。上傳區的格式提示也會直接寫出單檔上限，
  不用等挑完檔案才被拒絕。

  上傳時會取「一般上限」與「該格式的抽取上限」兩者中較嚴的一個。試算表與簡報是壓縮檔、
  CSV 會整份讀進記憶體，這三種格式本來就有較嚴的抽取上限；以前一個 30 MB 的試算表會
  **上傳成功、然後在 worker 裡失敗**，現在會在上傳當下就被擋下並說明原因。

- **Session 有時效，且改密碼會踢掉其他 session（SEC-3）**：先前 session token 用的是
  不帶時間的簽章器，payload 只有使用者 id。也就是說 **token 永遠不會過期**，複製出去
  就永久有效；而且改密碼、管理員重設密碼都**不會**讓既有的 session 失效。

  這同時是 `SEC-1` 的驗收缺口：`SEC-1` 讓初始密碼在「登入」這件事上失效，但**用初始密碼
  建立的 session 在強制變更密碼之後仍然可用**（已實測確認）。也就是說如果有人在管理員
  做強制變更之前就用 `admin123` 登入過，強制變更沒有把他踢出去。

  現在：token 改用帶時間戳的簽章器，並新增 `[auth].session_max_age_hours`
  （預設 12 小時）作為**絕對**壽命——從登入起算，**不因使用而延長**。滾動式延長會讓
  被偷走的 cookie 只要持續被使用就一直有效，那正是這項要防的情境。上限在
  token 層驗證而不只是 cookie 屬性，因為 cookie 的 `max_age` 只是對瀏覽器的建議，
  任何複製 token 值的東西都不受它約束。有效值會印在 `app_started` 日誌，
  設成天文數字不會無聲無息。

  撤銷則透過新的 `users.password_version`：token 帶著發放當下的版本號，每次請求比對。
  任何密碼變更都會 +1。自助變更會**重新發放**操作者自己的 cookie，所以「改密碼」是
  踢掉你**其他**裝置而不是把自己踢掉；管理員重設不重新發放，目標帳號在所有裝置都被登出
  （稽核事件記 `sessions_revoked`）。

### 效能

- **上傳不再卡住事件迴圈（P1-4）**：`upload_source` 原本是 `async def`，但內容全是同步
  阻塞 I/O（複製檔案、多次 SQLite 寫入、排入攝取佇列），沒有任何 await。也就是說上傳期間
  整個網頁程序服務不了其他請求。改為同步 `def`，交由 FastAPI 的 threadpool 執行；
  複製改為分塊串流，單一檔案也不會整份留在記憶體。

### 升級注意事項

本版有**兩個會改變既有部署行為的動作**，升級前請先讀完這一段。無破壞性的 schema 變更，
新欄位於啟動時自動套用（既有的 idempotent migration 機制）。

**升級當下會發生（每個環境都會）：**

- **所有使用者會被登出一次。** SEC-3 之前發出的 session token 不帶時間戳，簽章驗證會
  失敗，因此升級後每個人都要重新登入。這是刻意的——那些正是本次要淘汰的「永不過期」憑證。
- **仍在使用初始密碼的帳號會被要求變更密碼才能繼續使用。** 啟動時會逐一驗證
  `admin` / `user` 的密碼是否仍等於當初種入的值，是的話標記為必須變更。
  不刪除任何帳號、不遺失資料，但 `admin123` / `user123` 從此只能用來設定新密碼。
  **請安排升級後的第一次登入有人在鍵盤前**，否則沒有人能進管理後台。
  範圍很窄：只看這兩個帳號名，你自己建立的帳號不受影響；SSO 連結的帳號一律跳過。

**升級後建議做一次：**

- **到「系統設定」按一次「測試聊天模型」。** 本版新增的參數能力偵測只在這個動作時執行；
  沒跑過之前會沿用寬鬆預設（照送 `temperature`、用 `max_tokens`），對既有的
  OpenAI-compatible 端點行為完全不變，但跑過之後才會針對不接受 `temperature` 的模型
  自動調整。
- **確認上傳大小上限符合你的語料。** 本版開始在上傳當下就限制檔案大小：一般格式
  50 MB、試算表與簡報 20 MB（後者展開後可能大上百倍）。可用
  `[runtime].upload_max_file_bytes` / `extract_max_file_bytes` 調整。

**不需要立刻處理：**

- `max_source_bytes` 已更名為 `extract_max_file_bytes`。**舊鍵仍會被讀取**，
  只在啟動時記一筆 deprecation 警告，既有的 `config.toml` 與環境變數不必馬上改。

## [0.3.0] - 2026-08-22

### 新增

- **`scripts/reset_chroma_dimension.py`（break-glass 維度遷移工具）**：在 app 起不來、
  進不了 `/admin/index` 時，從應用程式外部完成維度遷移。預設 dry-run，只有同時指定
  `--apply` 與 `--services-stopped` 才會變更資料；執行前自動備份 SQLite + Chroma，重建
  collection，回填可安全沿用的目標維度 vectors，並把仍是舊維度的 indexed 來源標記為待
  Reindex。**一般情況請用 `/admin/index` 的遷移流程**（見下方 O0 修正）。Docker 與本機
  操作步驟見 `docs/DEVELOPMENT.md`。

- **O0 Phase B — `stale_embedding` 來源狀態與 startup sync 維度守衛**：新增
  `app/index_migration.py`，依照既有向量的維度把來源分類（可沿用／只因鎖定而失敗
  可復原／需重新 embed／不受影響）。維度不符的來源改為新狀態 `stale_embedding`，
  被所有 `status = 'indexed'` 查詢排除，因此 startup sync 不會把舊維度向量重新
  寫回去、把剛重設的 collection 又鎖回舊維度。狀態獨立於 `failed`，因為「檔案壞了」
  與「需要重新 embed」是兩回事，修法是「重新索引」而非重新上傳。
  `sync_from_sqlite()` 另加一層守衛：維度不符的分塊會被跳過而非寫入，並在回傳值
  多一個 `skipped_dimension` 計數與一筆 warning log。

- **O0 Phase C — `/admin/index` 維度遷移流程**：管理員可直接在索引頁完成 embedding
  維度遷移，不必停服務跑腳本。目標維度取自「設定」頁最近一次**成功**的 embedding
  測試而非手動輸入——這樣那個數字一定是 endpoint 真的回傳過的；頁面先顯示 dry-run
  預覽（可沿用／需重新索引／可復原各幾個、哪些檔名會被標記），要把目標維度打字回來
  才能送出。有攝取工作正在執行時直接拒絕（它們仍在用舊模型），遷移期間持有一把鎖，
  worker 的 `claim_next_job()` 會暫停認領，避免任何 upsert 落在 collection 刪除與
  重建之間、把新集合又鎖回舊維度。鎖逾時會被視為 stale 回收，死掉的程序不會卡住佇列。
  審計事件 `index_dimension_migrated` 記錄前後維度與各項數量。

### 修正

- **P0 修正完成 — `/admin/index` Clear 不再是維度遷移的死路（O0）**：原本 Clear 只執行
  `collection.delete(ids=...)`，即使 vector count 歸零，collection schema 仍鎖在舊維度；
  設定頁看不到任何 stored embedding 會誤判為未鎖定，直到新維度第一次 upsert 才以
  `Collection expecting embedding with dimension of X, got Y` 失敗，且反覆 Clear/Rebuild
  無法修復。現在有完整的遷移路徑：`reset_collection()` 真正換掉 collection 物件、
  `vector_index_state.generation` 讓其他 process 的快取 handle 失效、遷移期間持鎖暫停
  ingest 佇列、startup sync 拒絕寫入維度不符的分塊、維度不符的來源進入 `stale_embedding`
  等待重新索引。`scripts/reset_chroma_dimension.py` 保留為 break-glass（app 起不來時用）。
  涵蓋 inline worker 與 split worker 兩種部署形態的回歸測試。

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

- **安全性更新**：`cryptography` 50.0.0（GHSA-g6cj-pr64-35w5，high — 漏洞在 PKCS#7
  EnvelopedData 解密，本專案只用 Fernet + PBKDF2HMAC，實際踩不到，仍升級）；
  `pypdf` 6.15.0（GHSA-fp3f-mc75-235c、GHSA-fwg2-594c-jp42，medium — **這兩個踩得到**，
  惡意 PDF 可耗盡記憶體/CPU）。已實測確認 `cryptography` 49 加密的 API key 在 50 下
  仍能正確解密，**既有部署不需要重新輸入 API key**。

- 例行更新：`fastapi` 0.141.1、`uvicorn` 0.52.3、`joserfc` 1.7.4、
  `charset-normalizer` 3.5.1。charset-normalizer 的升級順帶修好一個無聲的錯誤——
  3.4.9 會把 GBK 編碼的 CSV 判成 cp949（韓文）並回傳看似正常的亂碼，不會拋錯，
  那堆亂碼會被當成內容切塊並 embed。已補回歸測試。

### 變更

- 決策紀錄：`P2-1`（SQLite 向量複本）裁定維持現狀並寫下重啟條件；
  `Q1-1`（RRF）記為排序決策並列出重新校準的前置條件。
  詳見 `docs/PERFORMANCE.md`、`docs/QUALITY.md`。

### 升級注意事項

- 無破壞性變更，直接部署即可；新的 `vector_index_state` 資料表與其欄位於啟動時
  自動套用（既有的 idempotent migration 機制）。

- **要更換 embedding 模型的維度時，請改用 `/admin/index` 的「更換 embedding 維度」，
  不要用「清除／重建」。** 這是本版最重要的維運變更：Chroma 在第一次寫入時鎖定集合
  維度，清除只刪 records、不會解鎖，過去這條路會讓索引看似清空卻仍拒絕新維度，且
  反覆清除／重建無法修復（O0）。新流程會先要求在「設定」頁測試 embedding 模型成功，
  據此取得目標維度，顯示 dry-run 預覽後再執行。

- **既有加密的 API key 不受影響。** 本版把 `cryptography` 升到 50.0.0，已實測確認
  49.0.0 產生的 Fernet ciphertext 在 50.0.0 下仍能正確解密，**不需要重新輸入 API key**。

- 新增來源狀態 `stale_embedding`（介面顯示「需重新索引」），代表檔案沒問題、只是
  既有向量的維度不符。若你先前用 `scripts/reset_chroma_dimension.py` 做過遷移，那些
  來源會是 `failed`（腳本的既有行為，刻意不變），照樣執行「重新索引」即可。

- 建議升級後檢查 `/admin/index`：若「Chroma 缺少」不為 0，執行一次「重建索引」。

## [0.2.0] - 2026-07-25

### 新增

- **企業 SSO**（#70）：支援信任反向代理標頭（I1a）與 OIDC（I1b）兩種登入路徑，
  含 IP allowlist、角色對應、`/admin/auth` 管理頁。**預設關閉**，未啟用時登入行為不變。
  文件：`docs/AUTHENTICATION.md`、`docs/SSO_DEPLOYMENT.zh-TW.md`（維運指引）。
- **Eval Workbench E1e-2：答案品質與引用判斷**（#72）：以 LLM judge 評估答案品質與
  引用正確性，並可跨 run 比較。實作於 `app/evals.py`，測試 `tests/test_evals_judge.py`。
  規劃文件：`docs/archive/E1E2_ANSWER_JUDGING_PLAN.md`（#71）。
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
