# UX/UI Review — 2026-06-19

全站 UX/UI 走查報告。評斷方向：**專業 · 簡單 · 直覺 · 明確 · 一致 · 可預期**，
並對照設計公約 [`docs/UI.md`](UI.md) 與產品 backlog [`docs/ROADMAP.md`](ROADMAP.md)。
每次 review 的準則與報告格式見 [`docs/UX_REVIEW_GUIDE.md`](UX_REVIEW_GUIDE.md)（本檔是依該準則產出的發現紀錄）。

> 這是**某個時間點的走查快照**，不是公約本身。修掉一項就把 checkbox 打勾；
> 確定要做的項目，最終應收斂進 `ROADMAP.md`（UX improvements）或 `UI.md`（若屬規範）。

## 走查方法

本機 uvicorn（dev secret + 共用主 repo `data/`），以 admin 身分登入，逐頁實際渲染截圖：
登入、筆記本列表、工作區（桌機＋375px 手機）、搜尋、設定、帳號、索引、使用者、
Eval 工作台、Retrieval Profiles、稽核、工具 modal、空狀態。

## 總評

底子**相當專業且一致**——共用 `page-head`/`eyebrow`/`section` 骨架、token 化樣式、
雙軌 pill、五級按鈕、破壞性 `data-confirm`、三步驟 onboarding、modal 的 ESC/backdrop/
`role=dialog`。問題集中在三處：**行動版工作區導覽**、**殘留英文**、**少數規範沒落實到底**。

---

## 🔴 高

| ID | 問題 | 方向 | 建議 | 既有 backlog 對應 | 狀態 |
|---|---|---|---|---|---|
| H1 | **行動版工作區無欄位切換**：375px 下三欄垂直堆疊，要捲過整個來源清單才到對話、再捲過對話才到工作台 | 直覺、簡單 | 手機寬度加分段切換（來源／對話／工作台），預設停「對話」 | ROADMAP **U10** | [x] 2026-06-20：新增 mobile-only 分段切換器，預設對話；JS 初始化後才收起非作用 pane，無 JS 仍保留堆疊 fallback |
| H2 | **來源狀態 pill 對終端使用者顯示英文** `indexed/processing/failed`（[`app/templates/_source_item.html:42`](../app/templates/_source_item.html) 直接輸出原始狀態）；admin 索引頁卻用中文「已同步」 | 一致、專業、明確 | 加狀態→中文對照（已索引／處理中／失敗／已上傳），class 名不動 | ROADMAP **U15b**（i18n 殘留）→ 但最顯眼，建議獨立快修先做 | [x] 2026-06-19：以 Jinja global `source_status_labels` 集中對照表，套用左側列表 + 預覽 modal 標題兩處 |
| H3 | **上傳控件是未美化的原生英文控件** `Choose Files / No file chosen`，夾在中文＋自訂面板裡，手機版更突兀 | 專業、一致 | 自訂 `<label>` 包 file input、隱藏原生外觀、中文化 | **未追蹤**（U6 做了上傳回饋但沒重塑控件） | [x] 2026-06-19：原生 input 無障礙隱藏（仍可鍵盤聚焦／label 觸發），改用 `.file-picker-trigger`「＋ 選擇檔案」 |

## 🟡 中

| ID | 問題 | 方向 | 建議 | 既有 backlog 對應 | 狀態 |
|---|---|---|---|---|---|
| M1 | **桌機來源名稱全部截斷成「PS115014_202...」**，彼此無法分辨 | 明確、直覺 | `title` tooltip；或中段省略保留日期／副檔名 | **未追蹤** | [x] 2026-06-19：來源名 `title` tooltip（完整檔名）。中段省略未做，視需要再加 |
| M2 | **Eval 區與稽核頁殘留大量英文**：導覽「Eval」、頁標「Retrieval Profiles」、「建立 Eval Set」、稽核篩選 Action/Actor/Target type/Sensitivity、欄位標題 ACTOR/ACTION/TARGET/IP/METADATA、下拉「2 indexed sources」 | 一致、專業 | 比照 UI.md §5 中文化（Recall/MRR/Profile 等技術詞保留原文） | ROADMAP **U15a/U15b** | [x] 2026-06-19：由 i18n 2a（稽核）+ 2b（Eval 全頁）完成 |
| M3 | **送出鎖機制不一致**：UI.md §4 規定送出表單用 `data-loading-form`，實際僅 10/20；工具面板等改用 `hx-disabled-elt`+`hx-indicator` | 一致、可預期 | 統一到 `data-loading-form`，或在 UI.md 明訂兩者並存判準 | **未追蹤**（牴觸 UI.md §4） | [x] 2026-06-19：採方案 B——[UI.md §4](UI.md) 改為兩機制並存（整頁送出用 `data-loading-form`、HTMX partial 送出用 `hx-disabled-elt`）+ 判準，現況正規化 |
| M4 | **列表無分頁、舊資料靜默截斷**：全站無 pagination/「載入更多」。各列表為「最新優先 + 砍尾」硬上限（來源 200／對話 50／訊息 200／筆記 50／**Eval run 20**／eval sets 50／搜尋每類 12）；超過者從 UI 消失。其中 **Eval run 無 truncated 提示**（舊 run 僅能用直接網址 `/admin/evals/runs/{id}` 開）；**筆記本首頁網格無任何 LIMIT**，全部一次撈出渲染 | 可預期、擴展性 | 至少給截斷列表加 truncated 提示（尤其 Eval run）；筆記本網格與 Eval run 視規模加分頁或「載入更多」；POC 階段可先只補提示 | **未追蹤**（POC 刻意「砍尾不分頁」，見 main.py 註解） | [x] 2026-06-20：用 LIMIT+1 偵測截斷 + `common.list_truncated` 提示，已涵蓋 Eval 主頁、評測集明細、搜尋結果每類 12 筆、筆記本首頁最近 100 本。完整 pagination / 載入更多仍視實際資料規模另開新需求 |

## 🟢 低

| ID | 問題 | 方向 | 建議 | 既有 backlog 對應 | 狀態 |
|---|---|---|---|---|---|
| L1 | 稽核「嚴重度」用 `.status`（含圓點），但它是固定分類、不隨時間變，依 UI.md §3.5 規則該用 `.tag`（無點） | 一致 | 改 `.tag`／`.tag--warn` | **未追蹤**（牴觸 UI.md §3.5） | [x] 2026-06-19：high→`tag--warn`、normal→中性 `tag`。值文字（high/normal）中文化留給 M2 |
| L2 | 工具／預覽 modal **無可見的 ✕ 關閉鈕**（只有 ESC＋點背景） | 可預期、可發現性 | 面板右上補明顯關閉鈕 | ROADMAP **U13**（ESC 已做，X 鈕未做） | [x] 2026-06-19：modal 右上補 `.modal-close` ✕（ESC／點背景仍可用） |
| L3 | 多處 `aria-label` 英文（Sources/Chat/Studio/Breadcrumb/Primary/Audit metadata） | 一致（無障礙） | 改 zh-Hant | ROADMAP **U13**（標記完成但殘留）/ U15 | [x] 2026-06-19（i18n 2b-ii，`a11y.*` + 稽核 2a） |
| L4 | 設定頁標籤雙語不一致（「Embedding base URL」全英 vs 其他「中文 / Azure 英文」） | 一致 | 統一雙語格式 | ROADMAP **U15b** | [x] 2026-06-19（i18n 2b-ii） |
| L5 | 登入頁直接印示範密碼 `admin / admin123`（POC 可接受） | 專業、安全觀感 | 上線前移除/改 | 已列管 [`docs/SECURITY.md:21`](SECURITY.md) | [x] 2026-06-19：示範帳號提示改為**只在 insecure dev secret 模式顯示**（`login_form` 傳 `demo_hint = SECRET == INSECURE_DEV_SECRET`）；真實部署（設 `NOTEBOOKLM_SECRET`）不再印帳密 |

---

## 視覺打磨（2026-06-19 走查後使用者回報）

CSS-only，皆在 [`app/static/style.css`](../app/static/style.css)：

| # | 問題 | 方向 | 修法 | 狀態 |
|---|---|---|---|---|
| V1 | **任何內容過長的頁面**（含多數 admin 置中頁）捲動時出現背景色接縫、上下不連續 | 一致、專業 | 待查根因（疑為 `html`/`body` 背景傳播或漸層未涵蓋整個捲動高度，非工作區單一問題）。先前只改 `.workspace` sticky 側欄高度＝局部貼布、未解根因，**已還原** | [x] 2026-06-20：改用全站根層修法，`html` 明確鋪 `--bg`，body 背景不重複，避免長頁面露出背景傳播接縫 |
| V2 | Eval 三個 tab 旁出現多餘的細捲軸 | 一致 | `.eval-tabs` `scrollbar-width: thin`→`none` + `::-webkit-scrollbar{display:none}`；保留 `overflow-x:auto` 供窄螢幕滑動 | [x] 2026-06-19 |
| V3 | `.settings-section` 的 `border-top` 分隔線緊貼上一段內容、無留白——**admin 多頁common**，非僅稽核 | 明確、一致 | 應系統性處理（如統一給 `.settings-section` 內容底部留白 / 調整 divider 間距），先前只在 `.audit-filter-form` 加 margin＝局部貼布，**已還原** | [x] 2026-06-20：以共用 `.settings-section + .settings-section` 間距處理跨 admin 頁分隔線，不做單頁 margin 貼補 |

## i18n 治本（M2 + L3/L4 的根，對應 ROADMAP U15a/U15b）

決策：**治本**（建訊息目錄地基），分段進行；locale 範圍先 zh-TW、結構預留 en。
這裡是**分段落地紀錄**；i18n 的架構與 how-to（加字串/加語系/切語系/已知例外）以 [`docs/I18N.md`](I18N.md) 為準。

| 階段 | 內容 | 狀態 |
|---|---|---|
| **Phase 0 — 地基**（外觀零變化） | `app/i18n.py` 訊息目錄（dict，無新依賴）+ `t()` Jinja global + `window.I18N`（base.html，給 app.js）+ `[ui].language` config（env `NOTEBOOKLM_UI_LANGUAGE`，預設 zh-TW）+ 測試。導覽列 7 標籤＋登出（模板 rail）與「思考中」（JS rail）當搬遷範例 | [x] 2026-06-19（`pytest` 143 passed；nav/`window.I18N` 實測外觀不變） |
| **Phase 1 — 高頻使用者面** | 導覽、chat 空/串流狀態、Studio 工具標籤、回對話的伺服器錯誤訊息、匯出 Markdown 標題 | [~] 進行中 |
| &nbsp;&nbsp;↳ Phase 1a — Studio/chat 介面 | `_chat_empty`/`_studio_tools`/`_suggestions`/`_tool_panel` + app.js 答題流程字串（`tr()`/window.I18N）+ chat 放棄回答 + 匯出標題（`引用來源`/`筆記`）。工具標籤改 `tool.<kind>` 單一來源 | [x] 2026-06-19（143 passed；實測 chat 空狀態/工具列/工具面板正確） |
| &nbsp;&nbsp;↳ Phase 1b — 使用者面伺服器錯誤 | `friendly_error_message` 對照表＋action 標籤、各 flow（建議/簡報/比較/會議記錄/artifact/翻譯）inline 錯誤、SSE 狀態文字全走 `error.*`/`flow.*` 目錄 | [x] 2026-06-19（143 passed；compare need-2 路徑＋error mapper 實測） |
| &nbsp;&nbsp;↳ Phase 1c — 其餘 JS（帶 placeholder） | `tr()` 升級支援 `{name}` 格式化；上傳檔案 JS 字串（alert/標籤/摘要）、設定頁 provider 提示全走 `js.*`；上傳區初始文字＋格式提示模板側走 `t()`（`js.upload_hint`/`upload.formats`）。**app.js 已無硬編碼中文（全為 tr() fallback）** | [x] 2026-06-19（143 passed；上傳區/格式提示變數替換實測、I18N 鍵齊） |

> Phase 1 三段（1a/1b/1c）皆完成；唯導覽「Eval→評測」內容決定留待 Phase 2b 與 Eval 頁標題一起改。
| **Phase 2 — 英文漂移大本營（= M2 主體 + L3/L4）** | Eval 工作台/Retrieval Profiles/建立 Eval Set/indexed sources、稽核頁（篩選＋欄位標題＋severity 值）、設定頁雙語統一（L4）、aria-label 中文化（L3） | [~] 進行中 |
| &nbsp;&nbsp;↳ Phase 2a — 稽核頁 | admin_audit.html 全頁走 `t()`：篩選標籤/表頭/severity(`SENSITIVITY_LABELS`)/說明/空狀態/modal aria 全中文化（含 L1 severity 文字、該頁 aria）。剩英文僅資料識別碼與 API key（刻意保留） | [x] 2026-06-19（143 passed；實測整頁中文） |
| &nbsp;&nbsp;↳ Phase 2b — Eval 工作台叢集（多模板）、設定頁 L4、全站 aria L3 | 混合命名：`評測`=活動/區塊；`Eval Set`/`Eval run`/`Run` 等資料詞保留；`Retrieval`→`檢索`、`Profile`/Recall/MRR 保留 | [~] 進行中 |
| &nbsp;&nbsp;&nbsp;&nbsp;· 2b-i 導覽 + 主頁 | nav「Eval→評測」、`_eval_nav` 分頁（評測集/檢索 Profile/調參指南）、`admin_evals` 整頁、eval 子頁麵包屑、run 狀態 map（`RUN_STATUS_LABELS`）。清掉 indexed sources/Retrieval Profiles/Eval 工作台 等英文 | [x] 2026-06-19（143 passed；主頁+導覽實測） |
| &nbsp;&nbsp;&nbsp;&nbsp;· 2b-ii Profiles + aria + 設定 L4 | `admin_profiles` 整頁（`prof.*`）；全站 aria-label 中文化（`a11y.*`：麵包屑/主要導覽/來源·對話·工作台面板/評測集分頁）＝**L3 完成**；設定頁「Embedding Base URL」對齊雙語格式＝**L4 完成** | [x] 2026-06-19（143 passed；Profiles/aria/設定實測） |
| &nbsp;&nbsp;&nbsp;&nbsp;· 2b-iii eval 動態 partials | `_eval_run_status`/`_eval_run_results`/`_eval_items_section` 整理（`evalr.*`）＋狀態 map（`RUN_STATUS_LABELS`/`EVAL_RESULT_STATUS_LABELS`：成功·命中·未命中·已核准·草稿）。技術詞 Recall/MRR/chunk/snippet/Profile 保留 | [x] 2026-06-19（143 passed；run 頁實測狀態全中文） |
| &nbsp;&nbsp;&nbsp;&nbsp;· 2b-iv eval 子頁外框 | `admin_eval_set`/`admin_eval_run`/`admin_eval_compare`/`admin_eval_help` 清掉殘留英文：eyebrow/標題→評測*、owner→擁有者、created→建立於、Progress→進度、狀態 pill→既有 map、Domain hints→中文 | [x] 2026-06-19（143 passed；4 頁實測無殘留英文） |

> **i18n 視為完成。** 記錄性例外（刻意保留，非遺漏）：(1) 最深層 eval 頁的**已是中文**的長段說明文字保留為 inline 字面值，未逐一進 catalog——不影響顯示語言，避免大量零變化 churn；(2) `main.py` `PROFILE_PARAM_LABELS`（Vector/Keyword/Rerank 權重等）為技術參數名，依 UI.md §5 保留；(3) 全站技術詞 Recall/MRR/Profile/chunk/embedding/RAG/Eval Set/Eval run/Run/Baseline/Candidate 一律保留原文。
| **Phase 3 — 選配** | 補 en locale 字串 + 每使用者語言切換（ROADMAP U15b） | [ ] |

> 每個 Phase 為獨立可上線的小段；新字串一律走 `t()`/`window.I18N`，不再回到模板/JS 硬編碼。

## 既有一致性債（UI.md §6 deferred，本次走查確認仍在）

非本次新發現，但與「一致」直接相關，列此備忘（handover.md 亦有記）：

- `.index-stat` 統計格、Studio 工具磚 `.tool-tile` 尚未併入共用 `.card`（UI.md §3.3 待收）。 [ ]
- 純更名 deferred：`.eval-tab` → `.tab`、`.settings` → `.page`（churn 大，須當獨立 PR）。 [ ]

## 做得好的地方（避免重構時誤傷）

- 統一 `page-head`/`eyebrow`/`section` 骨架 + token 化樣式 → 專業、一致。
- 空筆記本三步驟 onboarding、對話空狀態隨索引完成自動切換文案 → 直覺。
- pill 雙軌（`.status` 有點＝生命週期、`.tag` 無點＝分類）、五級按鈕、破壞性 `data-confirm` → 明確、可預期。
- modal 有 ESC／backdrop／`role=dialog`／`aria-modal` → 無障礙基礎到位。
- 使用者表格、Profiles 卡片作用態（`.card--active`）→ 一致。

## 優先順序建議（待討論）

1. **先快修最傷第一印象的**：H2（狀態中文化）、H3（上傳控件）、M2（Eval/稽核中文化）—— 行為保留、不碰檢索。
2. **行動版體驗**：H1（欄位切換器），影響最大但工程量也最大。
3. **打磨**：M1、M3、L1–L4 可併批處理。
4. L5 併入 pre-launch checklist（客戶 H200 部署）。
