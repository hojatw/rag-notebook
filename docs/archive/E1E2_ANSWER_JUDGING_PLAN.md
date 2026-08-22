# E1e-2 · 答案品質與引用判斷 — 實作計劃

> **已封存 2026-08-22。這份計劃已經實作完成，不是待辦清單。**
> E1e-2 於 PR #72 實作並 merge（`ROADMAP.md` 標 `[x]`，測試在 `tests/test_evals_judge.py`）。
> 本檔保留為**設計紀錄**——讀它是為了理解「為什麼判分機制長這樣」，例如為何 judge 的
> 標籤在缺少 reference answer 時要作廢、為何 groundedness 與 citation 不受影響。
> 現況以 [`QUALITY.md`](../QUALITY.md) 與 [`ROADMAP.md`](../ROADMAP.md) 為準。
>
> *（封存前這裡寫的是「已規劃、待實作」——那在實作完成後就沒有更新過，會讓讀者
> 以為這件事還沒做。）*
> **權威來源**：範圍見 [`ROADMAP.md`](../ROADMAP.md) E1e-2；答案品質背景見 [`QUALITY.md`](../QUALITY.md) Q1-5；eval 流程見 [`RETRIEVAL.md`](../RETRIEVAL.md)「Evaluation」。本檔是實作藍圖，設計決策以那些權威文件為準。
> **前置已滿足**：PR #70（企業 auth）已 merge 進 main；Eval Workbench E1a–f 已完成；`eval_items.expected_answer`（reference answer）欄位已存在。

---

## 1. 目標與範圍

在 Eval Workbench 的 eval run 中，**選擇性**對每題生成答案並用 LLM judge 評分，量化四個答案品質維度，與既有的檢索指標（Recall/MRR/top-score）**分開儲存與呈現**。這是「量答案品質的尺」，後續 E2（notebook domain hints）要靠它驗證。

**只做 E1e-2；E2 不在本計劃**（見 §11）。

## 2. 已確認的設計決策（使用者拍板）

1. **judge 路線 = A（對照式結構化評分）**：judge 不做開放式 1–5 打分，而是對照 `expected_answer`（reference）+ 逐句 groundedness + `expected_substrings` 半確定性比對。目的：把 judge 自身的幻覺與 self-preference bias 壓到最低（客戶只有一顆 Gemma，選手兼裁判）。
2. **四維一次到位**：answer quality、groundedness、citation correctness、abstain correctness。
3. **成本可接受**：每題最多多「1 次生成 + 1 次 judge」= 約 2× LLM 呼叫。run 摘要顯示 token 估算（沿用 G1a telemetry）。
4. judge 結果一律標為「**參考信號、非真相**」（沿用 G1c ai_safety_events 的態度），搭配人工 spot-check。

## 3. 現狀與落點

- 目前 `run_eval_job`（[`app/evals.py`](../../app/evals.py) line ~270）**只做檢索**：每題 `retrieve()` → `eval_item_hit_rank()` → 存 `eval_results`（status/hit_rank/top_score/latency/retrieved_json）。
- `run_metrics_from_results`（evals.py ~235）聚合 Recall/MRR 等到 `eval_runs.metrics_json`。
- 可複用：`generate_answer()`（[`app/llm.py`](../../app/llm.py) line ~924）。**無現成 judge 函式**（需新增）。
- abstain 邏輯在 `ask()`（不在 `retrieve()`）；eval 走 `retrieve()`，所以 abstain 需在 eval 內**自行複製決策**（見 §4）。
- 既有欄位：`eval_items.expected_answer`、`expected_substrings_json`、`item_type`（answerable / cross_lingual / **unanswerable**）。

## 4. judge 路線 A：對照式結構化評分（核心設計）

### 四維定義

| 維度 | 判定方式 | 主要靠 |
|---|---|---|
| **abstain_correctness** | `expected_abstain`（unanswerable → true）與 `did_abstain` 是否一致 | **確定性**（不靠 LLM） |
| **groundedness** | 答案每條事實宣稱是否被 retrieved chunks 支撐；列出 `unsupported_claims` | LLM judge + chunk 文本 |
| **citation_correctness** | 每個 `[N]` 是否指向真正支撐該句的 chunk | LLM judge + chunk 對應 |
| **answer_quality** | 對照 `expected_answer` 判 correct/partial/incorrect；`expected_substrings` 命中率作客觀錨點 | LLM judge + 確定性錨點 |

> 注意：**abstain_correctness 幾乎全確定性**——最防幻覺的維度反而最不依賴 judge。這是路線 A 的優點，先把它做穩。

### abstain 的確定性判定（eval 內複製 ask() 行為）

```
did_abstain = (not retrieved) or (top_score < active_low_confidence_threshold())
expected_abstain = (item_type == "unanswerable")
abstain_correct = (did_abstain == expected_abstain)
```

- `did_abstain == True` → **跳過 `generate_answer`**（省一次呼叫，且符合真實行為），答案記為 canned refusal，其餘三維標記 `not_applicable`。
- `did_abstain == False` 且 answerable → `generate_answer` → judge 三維。

### judge 輸入 / 輸出契約

輸入給 `judge_answer()`：`question`、`generated_answer`（含 `[N]`）、`expected_answer`、`expected_substrings`、`item_type`、`retrieved_chunks`（帶編號 + 文本 + location）、`did_abstain`。

輸出（judge 回傳、存 `eval_results.judge_json`）：
```json
{
  "answer_quality":       {"label": "correct|partial|incorrect", "score": 0.0-1.0, "rationale": "..."},
  "groundedness":         {"score": 0.0-1.0, "unsupported_claims": ["..."], "rationale": "..."},
  "citation_correctness": {"score": 0.0-1.0, "wrong_citations": [N, ...], "rationale": "..."},
  "substring_hit_rate":   0.0-1.0,          // 確定性錨點，程式算，非 judge 產
  "judge_model": "…", "judge_ok": true      // parse 失敗時 judge_ok=false，四維留空
}
```

### 降低 self-bias 的手法（路線 A 的重點）

1. answer_quality 以「對照 reference」的分級（correct/partial/incorrect）取代開放打分。
2. `substring_hit_rate` 由程式確定性計算，作為 answer_quality 的客觀錨點喂給 judge（也獨立存）。
3. groundedness 要求 judge **列出 unsupported_claims**（逼它指證，不能只給分）。
4. judge prompt 要求「只依據提供的 chunks 判斷，不得引入外部知識」。

## 5. Schema 變更（同步更新 `docs/SCHEMA.md`）

- `eval_results`：新增
  - `judge_json TEXT NOT NULL DEFAULT '{}'`（四維 + rationale + judge_ok）
  - `answer_text TEXT NOT NULL DEFAULT ''`（生成的答案；full internal export 才輸出）
  - `answer_outcome TEXT NOT NULL DEFAULT ''`（answered / abstained / error）
- `eval_runs`：新增 `judge_enabled INTEGER NOT NULL DEFAULT 0`（本次 run 是否跑 judge）；judge 聚合指標放進既有 `metrics_json`（如 `metrics_json.judge = {...}`），**不與 Recall/MRR 混在同層**。
- 用 `_ensure_column` idempotent migration（比照 `eval_items.item_type` / `expected_answer` 的既有寫法，db.py ~461）。

## 6. 分步實作（Phase 1–4）

**Phase 1 — judge 函式 + schema（無 UI）**
- `db.py`：加上述欄位 + migration；`SCHEMA.md` 同步。
- `llm.py`：新增 `judge_answer(...)` + `ANSWER_JUDGE_PROMPT`（強語言/JSON-only 規則，比照 `generate_eval_candidates` 的 provider 呼叫與 JSON 解析模式）；新增 `parse_answer_judge()`（比照 `parse_eval_candidates`，parse 失敗回 `judge_ok=false`）。
- 單元測 judge 解析（正常 / 壞 JSON / 缺維度）。

**Phase 2 — 接進 run 流程**
- `evals.py` `run_eval_job`：`run["judge_enabled"]` 為真時，`retrieve()` 後計算 `did_abstain`；未 abstain 則 `generate_answer` → `judge_answer`；寫 `judge_json` / `answer_text` / `answer_outcome`。**judge/generate 失敗只影響該題（記 error），不 fail 整個 run。** progress step 文案加「生成/評分第 N 題」。
- `run_metrics_from_results`：聚合 answer_quality 分佈、groundedness_avg、citation_correct_rate、abstain_correct_rate（拆 answerable 誤拒率 + unanswerable 正確拒答率），寫入 `metrics_json.judge`。

**Phase 3 — UI + i18n**
- run 建立表單（eval set 詳情頁）加「☐ 同時評測答案品質（會多花 ~2× LLM）」勾選 → 帶入 `judge_enabled`。
- run 詳情頁：檢索指標區塊下方新增「答案品質」區塊（四維聚合 + per-item 展開顯示 judge rationale / unsupported_claims），並標「參考信號，非絕對真相」。
- compare 頁：兩 run 對比加 judge 四維 diff（僅當兩者皆 `judge_enabled`）。
- 所有新文案走 `app/i18n.py` 目錄（`t()` / `window.I18N`），勿硬編碼（見 `docs/I18N.md`）。

**Phase 4 — telemetry / export / 收尾**
- G1a：judge 與 eval 生成各記一筆 `llm_usage_events`，`call_type` 新增 `eval_answer` / `eval_judge`（同步 `SCHEMA.md` call_type 列舉）。
- export：judge 細節（rationale、answer_text、unsupported_claims）**只進 full internal export**；sanitized export 僅含四維聚合數字，不含題目/答案/證據文本。full internal export 記入 audit（沿用 E1d）。
- `docs/ROADMAP.md` 把 E1e-2 由 `[ ]` 改 `[x]`；`QUALITY.md` Q1-5 交叉引用。

## 7. 關鍵前提與風險

- **Gemma JSON-following**：judge 依賴 chat model 穩定輸出 JSON。前置——確認 active chat model 通過 `/settings` 的 JSON 探測（O1b）/ `QUALITY.md` Q0-3。judge parse 失敗 → 該題 `judge_ok=false`，run 續跑。
- **judge 本身要被校準**：先拿一小批人工已知對/錯的題跑 judge，spot-check judge 的判斷是否合理，再信任聚合數字。judge 不是真理。
- **abstain threshold 連動**：`abstain_correctness` 依賴 `active_low_confidence_threshold()`（目前 0.25，`QUALITY.md` Q0-2 標記待重調）。threshold 變動會改變 abstain 判定——這是特性不是 bug，但報告要標明用的是哪個 threshold（run 已凍結 profile snapshot，一致即可）。
- **成本**：judge 預設關閉（`judge_enabled=0`），需使用者在建 run 時明確勾選，避免每次 run 都付 2× 成本。

## 8. 測試策略

- 單元：`judge_answer` prompt 組裝、`parse_answer_judge`（正常/壞 JSON/缺維度）、`did_abstain` 決策、metrics 聚合（含 unanswerable 誤判）。
- 整合：`run_eval_job` with `judge_enabled=1`——mock LLM 回傳固定 judge JSON，驗證 abstain 題跳過 generate、answerable 題 generate+judge、失敗題不 fail run、judge 結果與檢索指標分層存。
- 回歸：`judge_enabled=0` 時行為與現在完全一致（現有 eval 測試不變）。
- verify：`.venv/bin/pytest` 全綠 + `py_compile`。（retrieval 未動，eval_retrieval harness 非必要，但可跑。）

## 9. 驗收標準

1. unanswerable 題能正確判 `abstain_correctness`（確定性，不靠 judge）。
2. groundedness 能抓出「答案有、但 chunks/reference 沒有」的宣稱（列進 `unsupported_claims`）。
3. judge JSON parse 失敗時該題 `judge_ok=false`、run 續跑不中斷。
4. judge 指標與 Recall/MRR **分層儲存與呈現**；UI 標「參考信號」。
5. sanitized export 不含題目/答案/證據文本，只有聚合數字；full internal export 才含細節且入 audit。
6. `judge_enabled=0` 時完全回歸現狀；全套件綠 + 新 judge 測試綠。

## 10. 如何開工

```bash
# 從最新 main 開分支（PR #70 已 merged）
git -C /Users/philip_1/Repos/My/side_projects/notebooklm-rag-poc fetch origin main
# 開新 worktree 或分支，例如：
#   git worktree add .claude/worktrees/e1e2 -b feat/e1e2-answer-judging origin/main
.venv/bin/pytest            # 確認起點全綠（199）再動手
```

依 Phase 1 → 4 順序；每個 Phase 收尾跑 `pytest` + `py_compile`。Phase 3 動 UI 後做桌面/行動雙寬瀏覽器 smoke test（`docs/UI.md`）。實作與文件分開 commit。

## 11. 不在本計劃範圍（後續）

- **E2**（notebook domain hints + answer policy）：E1e-2 的尺就緒後才做，用「with/without hints 的 run 對比」驗證（`ROADMAP.md` E2 / `QUALITY.md` Q1-5）。
- E1e-2 **可選延伸**（非本輪）：judge 用不同於答題模型的第二模型（客戶目前無更強模型，暫不可行）。
