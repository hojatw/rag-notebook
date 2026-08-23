# RAG Notebook 產品白皮書

版本：草案  
適用對象：業務決策者、專案負責人、知識管理/研發/法遵/IT 管理者  
文件目的：以非工程語言介紹 RAG Notebook 的產品定位、使用情境、價值、治理方式與導入邊界。

---

## 摘要

RAG Notebook 是一個面向企業內部知識工作的 AI 研究助理。使用者可以把文件整理成不同 notebook，針對選定來源提問，取得有引用依據的回答，並把有價值的回答、摘要、比較報告或會議整理保存成筆記。

它的核心價值不是「讓 AI 隨意回答」，而是讓 AI 在指定資料範圍內協助閱讀、比對、整理與產出初稿。對企業而言，這代表知識工作可以更快從大量文件進入可討論、可驗證、可追蹤的狀態。

目前產品定位是單機部署的概念驗證，適合本機實驗、小型可信任環境、客戶內部試點與高價值流程驗證。若要直接面向大量使用者或公開網路，仍需要依照企業資安、治理與維運要求進一步產品化。

## 要解決的問題

企業內部常見的知識工作痛點包括：

- 文件很多，但人很難快速掌握重點、差異與矛盾。
- 問題通常跨多份文件，手動查找、摘錄與比對成本高。
- AI 回答若沒有來源依據，難以被研發、法遵、醫藥、客服或管理團隊信任。
- 內部資料不一定能送出企業環境，導致一般雲端 AI 工具難以導入。
- 模型、檢索參數與文件格式會影響回答品質，但缺少可管理、可驗證的調校流程。

RAG Notebook 的設計目標，是把「文件、問題、答案、引用、筆記與評估」放在同一個可操作工作區裡，讓使用者既能提高效率，也能保留判斷依據。

## 產品概念

RAG Notebook 以 notebook 為主要工作單位。每個 notebook 可以包含一組來源文件、對話紀錄、釘選筆記與 AI 工具產出。

典型流程如下：

1. 使用者建立 notebook。
2. 上傳或匯入來源文件。
3. 系統抽取文字、建立分塊與索引。
4. 使用者針對文件提問，AI 回答時附上引用來源。
5. 使用者點擊引用，回到原始來源片段檢查依據。
6. 使用者把重要回答、比較結果、會議整理或摘要保存成筆記。
7. 管理員透過 Eval Workbench 檢查檢索與回答品質，並調整設定。

這種設計把 AI 從「一次性聊天工具」轉成「可持續累積的研究工作區」。

## 主要能力

### 1. 有來源依據的問答

使用者可以選擇 notebook 內的來源並提問。系統會先從文件中找出相關片段，再讓模型生成回答。回答會附上引用編號，使用者可以點回來源片段確認依據。

適合情境：

- 查找研究報告中的關鍵結論。
- 詢問合約、規格、標準文件中的條款。
- 根據多份文件整理客戶、產品或專案背景。
- 快速確認某個答案是否有文件支持。

### 2. 多文件比較與整理

Studio 工具提供面向知識工作的常用產出：

- 來源比較：比較多份來源的共同點、差異與矛盾。
- 會議記錄整理：從逐字稿整理主題、決議、行動項目與未決事項。
- 學習指南、FAQ、時間軸：把一組文件轉成更容易閱讀的結構化材料。
- 摘要翻譯：將單一來源摘要翻譯成指定語言。
- 筆記保存：將重要回答或工具結果保存為 notebook 內的可編輯筆記。

這些功能降低了從「讀完文件」到「形成可討論材料」之間的轉換成本。

### 3. 可管理的知識工作區

每個 notebook 都有自己的來源、對話與筆記。這讓使用者可以依照專案、客戶、研究主題、產品線或文件集合來管理資料。

管理面也包含：

- 多使用者與權限隔離。
- 管理員使用者管理。
- LLM 連線設定。
- 向量索引健康檢查與重建。
- 稽核紀錄與治理事件資料。

Notebook owner 也可以維護有界的 domain hints 與 answer policy。Domain hints
用來對應領域術語、別名與 query expansion，只在檢索 query time 生效；answer
policy 則約束回答方式，但不能取代來源證據或放寬系統的引用／grounding 規則。

### 4. Eval Workbench：把回答品質變成可討論的指標

RAG 系統的品質通常不只取決於模型，也取決於文件抽取、分塊、檢索、reranking、語言、領域詞彙與回答規則。RAG Notebook 內建管理員 Eval Workbench，讓團隊可以在部署環境內建立 eval set、執行檢索評估、比較 retrieval profile，並保留歷史結果。

這對企業導入很重要，因為它讓「回答品質不好」不再只是主觀感受，而可以拆成：

- 是否找到了正確文件片段？
- 正確片段排名是否足夠前面？
- 是否因為信心門檻、語言或關鍵字而漏找？
- 調整參數後是否真的改善？

除 Recall/MRR 等 retrieval metrics 外，管理員也可以選擇執行 answer-quality、
groundedness、citation correctness 與 abstain correctness judging。這些 judge
metrics 與 retrieval metrics 分開呈現，作為參考訊號而非 ground truth；若沒有
reference answer，answer-quality correctness 會標示為不適用。真正的客戶品質基準
仍需在客戶環境內建立經核准的代表性 Eval Set。

## 企業價值

### 提升知識工作效率

RAG Notebook 可以縮短閱讀、搜尋、整理、比較與初稿產出的時間。使用者不需要在多份文件、搜尋結果、筆記工具與聊天工具之間反覆切換。

### 強化回答可驗證性

引用來源與來源片段高亮讓使用者可以快速回到原始依據。這對法遵、研發、醫藥、客服知識庫、合約審閱與管理決策尤其重要。

### 支援資料留在客戶環境

產品設計可搭配客戶既有的 OpenAI-compatible 或 Azure OpenAI endpoint。資料、索引、稽核與評估結果可保留在部署環境內，降低資料外流與資料主權疑慮。

### 讓 AI 導入可治理

管理員可以檢查 LLM 設定、查看稽核紀錄、追蹤 usage/safety telemetry，並透過 Eval Workbench 驗證調整是否有效。這讓 AI 從個人試用工具逐步走向可管理的企業能力。

## 治理與資料邊界

RAG Notebook 的治理原則是：必要資料留在正確位置，治理紀錄避免複製敏感內容。

目前設計重點包括：

- API key 加密保存。
- 使用者與 notebook 資料隔離。
- 重要管理操作寫入 audit trail。
- LLM usage 與 safety event 以精簡 metadata 方式保存。
- Governance metadata 預設不複製 raw prompts、來源全文、retrieved snippets、模型輸出或 API keys。
- Full internal eval report 需要明確確認，並以高敏感度事件記錄。

這些設計能支援內部試點與治理討論，但不等同於已完成所有企業級資安認證。若要正式上線，仍應依客戶資安政策進行額外審查。

## 部署與整合模式

目前產品適合以下部署模式：

- 本機或單機測試。
- 小型可信任內部環境。
- 客戶環境內的 PoC 或 pilot。
- 搭配客戶既有 LLM/embedding endpoint 的封閉式部署。

目前不建議直接作為公開網際網路服務使用。Trusted reverse-proxy header SSO、
OIDC 與本機 break-glass account 已有實作，但若要正式產品化，仍建議補強：

- 依客戶需求補齊 SAML／IdP logout 等身分整合與更細緻的權限模型。
- 更完整的治理 dashboard。
- 更成熟的 LLM 設定管理與多 profile 切換。
- 部署監控、備份、復原與容量規劃。
- 依客戶政策完成資安測試與稽核。

## 目前支援與規劃中的資料格式

目前支援：

- PDF
- TXT
- Markdown
- DOCX
- HTML
- SRT/VTT 字幕逐字稿
- PPTX（text-first：標題、內文、表格、講者備註）
- XLSX/CSV（Q&A 偵測與有界的一般資料列分塊）

所有支援格式都會留下 ingestion diagnostics，例如抽取字數、section/chunk
數量、extractor path、警告、失敗階段與有界 preview，讓使用者能區分「抽取失敗」
與「檢索／回答失敗」。

規劃中的格式與能力：

- Web URL 匯入，需具備 SSRF 防護與客戶 egress policy 控制。
- OCR 與圖片理解，依客戶需求與模型能力逐步加入。
- 試算表的精確 filtering/counting/aggregation，另以受限制的 table-query
  workflow 處理，不把 top-k RAG 當成完整資料分析工具。

產品策略上，下一個新來源格式是具 SSRF 防護的 Web URL；OCR、圖片理解與
結構化表格查詢則依客戶需求和 serving capability 投入。

## 適合的使用情境

- 研究報告閱讀與跨文件整理。
- 產品、法規、醫藥、技術文件問答。
- 客戶專案背景整理。
- 會議逐字稿轉會議記錄。
- 內部 FAQ 與知識庫初稿建立。
- 多份文件的差異、矛盾與共同點分析。
- 模型/檢索品質的內部評估與調校。

## 不適合直接承諾的情境

目前版本不應被定位為：

- 取代正式文件管理系統。
- 取代資料倉儲、BI 或 SQL 分析系統。
- 取代人工審閱、法遵簽核或醫療判斷。
- 可直接公開上線的 SaaS 產品。
- 不需評估即可保證回答完全正確的 AI 系統。

它更適合作為企業內部 AI knowledge workflow 的 PoC/pilot 基礎，透過真實文件與評估流程逐步驗證價值。

## 導入建議

### Phase 1：PoC 驗證

- 選定 1 到 2 個高價值使用情境。
- 準備代表性文件集。
- 設定客戶 LLM/embedding endpoint。
- 建立 notebook，測試問答、引用、比較、摘要與筆記流程。
- 收集使用者對回答品質、引用可信度與工作效率的回饋。

### Phase 2：品質與治理

- 用 Eval Workbench 建立代表性 eval set。
- 比較不同 retrieval profile 的效果。
- 以 baseline／hints／policy／combined 模式比較 domain 設定，必要時開啟
  answer/citation judging；reference answer 不足時不得把 judge 分數當 ground truth。
- 檢查 audit trail、usage telemetry 與安全事件紀錄是否符合治理需求。
- 明確定義哪些問題適合回答、哪些情境應該 abstain。

### Phase 3：擴充資料來源與工作流

- 依需求加入 Web URL、OCR、圖片理解與結構化表格查詢等能力。
- 在客戶環境以核准的 Eval Set 驗證既有 domain hints 與 answer policy，避免把
  自動產生的 draft 當成客戶 evidence。
- 針對高頻工作流設計更完整的 artifact/report 產出流程。

## 衡量成功的指標

建議以以下指標評估 PoC 是否值得進一步產品化：

- 使用者完成閱讀/整理任務的時間是否下降。
- 回答是否能穩定附上正確引用。
- 使用者是否願意把回答或工具產出保存成筆記。
- Eval set 的 retrieval 指標是否改善。
- 管理員是否能理解並操作 LLM 設定、索引狀態與 eval 結果。
- 資料留存、稽核與部署方式是否符合客戶內部政策。

## 路線圖重點

近期建議優先方向：

1. Customer Eval Set：在資料不離開客戶環境的前提下，建立經核准且含必要 reference answer 的代表性題集，形成可重複的 judged baseline。
2. E2 validation：用相同 Eval Set 比較 baseline／hints／policy／combined，確認 domain 設定提升品質且不增加 false positive 或資料外洩。
3. Admin LLM operations：在已完成 chat/embedding diagnostics 的基礎上，加入多 profile 管理與安全切換。
4. Source-format next step：加入具 SSRF 防護與 egress policy 控制的 Web URL ingestion；文件結構、OCR 與圖片理解依實際語料／模型能力評估。
5. Governance：依需求補上 aggregate dashboard、report export 與 retention policy；圖片理解、音訊轉錄與 TTS 維持 customer-driven。

## 結語

RAG Notebook 的定位，是讓企業把內部文件轉成可提問、可引用、可整理、可評估的 AI 工作區。它不是單純的聊天介面，而是一個結合文件管理、檢索、生成、筆記、評估與治理的知識工作流程雛形。

對客戶而言，最適合的導入方式是從明確場景開始，用真實文件驗證效率與可信度，再逐步擴充格式、治理與正式部署能力。
