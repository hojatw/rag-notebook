# UI.md — 前端設計公約 (single source of truth)

這份文件定義全站 UI/UX 的**標準**:頁面原型、元件、資料呈現、互動行為與用語。
目標是讓不同類型的頁面長得像同一個產品。新頁面/元件**先讀這份**;重構既有頁面**對齊這份**。

技術約束(來自 `AGENTS.md` / `CLAUDE.md`):server-rendered Jinja + HTMX + Alpine,
**無 build、無 npm、無 CDN**;HTMX partial 命名 `_*.html`;樣式集中在 `app/static/style.css`,
互動 helper 集中在 `app/static/app.js`。**不要硬編碼顏色/間距/圓角——一律用 token。**

> 標記說明:**[標準]** = 既有且為正規寫法,沿用;**[待建立]** = 本公約新訂、尚未實作的目標元件;
> **[淘汰]** = 既有但應逐步移除、改用標準寫法。重構時以本文件的「對照表」為檢查清單。

---

## 1. 基礎 token(已存在,`style.css:13–78`)

| 類別 | 變數 | 用途 |
|---|---|---|
| 間距 | `--sp-1..8`(4/8/12/16/24/32/48/64) | 所有 margin/gap/padding |
| 圓角 | `--r-sm/md/lg/xl/pill`(6/10/14/18/999) | 卡片 md、pill 999 |
| 陰影 | `--shadow-sm/md/lg`、`--shadow-focus` | 卡片 md、抽屜 lg、focus ring |
| 文字 | `--text/--text-soft/--muted/--muted-strong` | |
| 線條 | `--line/--line-strong/--line-accent` | |
| 強調 | `--accent/-strong/-soft/-deep` | 主色(靛紫) |
| 語意色 | `--ok* / --warn* / --danger*`(各 base/soft/line) | 成功/警告/危險 |
| Motion | `--ease`、過場 140ms | 已全域套在 `a/button/input...` |

**規則**:任何新 CSS 的顏色、間距、圓角、陰影都必須引用上表變數,不得寫死數值。

---

## 2. 頁面原型(只有三種骨架)

### 2.1 置中內容頁(預設,絕大多數頁面)
首頁、搜尋、設定、所有 admin、所有 eval 頁都屬此類。骨架固定:

```html
<section class="page">                     <!-- [待建立] 取代名實不符的 .settings 外層 -->
  <header class="page-head">…</header>       <!-- §3.1 -->
  <section class="section">…</section>       <!-- §3.2,可多個 -->
  <footer class="page-foot">…</footer>       <!-- 選用:返回/主要動作 -->
</section>
```

> 過渡期:現有頁面外層多為 `.settings`(`settings.html`、`admin_*`、`eval` 皆是),
> `home`/`search` 則無外層。目標統一為 `.page`;在 `.page` 上線前,新頁沿用 `.settings` 並比照其內距。

**內容寬度(兩級制,不要再加第三種)**:
- **表單/一般頁** = `.settings`,**720px**(設定、使用者、索引)。
- **資料密集頁** = `.settings` + `.eval-workbench` 或 `.audit-page`,**1120px**(寬表格、多欄網格、compare;Eval 全頁與稽核共用此寬)。

> 兩者共用同一條 `max-width` 規則(`.eval-workbench, .audit-page`)。新增寬版 admin 頁就掛這個寬度標記,**不要再發明新的數值**(先前 eval=1120 / 稽核=1180 的意外 60px 差已收斂)。

### 2.2 Workspace(滿版三欄)
只有 `notebook.html`(來源 / 對話 / 工作台三欄)。維持其專屬 layout,**不套** `.page`。

### 2.3 Auth
只有 `login.html`,維持獨立 `.auth-panel` 置中卡。

---

## 3. 元件標準

### 3.1 頁首 `.page-head` **[標準,以 `home`/`search` 為準]**
所有置中內容頁的標題塊統一用 `.page-head`,內部固定三件 + 選用右側動作:

```html
<header class="page-head">
  <div>
    <p class="eyebrow">區域名稱</p>        <!-- 小標,大寫字距 -->
    <h1>頁面標題</h1>
    <p class="muted">一句話說明這頁能做什麼。</p>
  </div>
  <!-- 選用:右側主要動作,如 home 的「+ 新增筆記本」 -->
</header>
```

> **[淘汰] `.settings-head`**:結構與 `.page-head` 完全相同,重構時改名為 `.page-head`。

### 3.2 區塊 `.section` + 區塊標題 `.section-head` **[待建立,收斂四種]**
一頁切成數個 `.section`;每個 section 的標題統一用 `.section-head`,提供三個選用 slot:

```html
<section class="section">
  <div class="section-head">
    <div>
      <p class="eyebrow">選用 eyebrow</p>
      <h2>區塊標題</h2>
    </div>
    <span class="count">12</span>            <!-- 選用:數量 -->
    <div class="section-actions">…</div>      <!-- 選用:右側動作 -->
  </div>
  …內容…
</section>
```

> 收斂對象(都改用 `.section-head`):`.settings-section>h2`、`.section-title-row`、
> `.section-head`+`.pane-count`、`.section-heading`+eyebrow。

### 3.3 卡片 `.card` **[標準]**
基礎卡(feature card,grid 磚):`background: --surface`、`border:1px solid --line`、
`border-radius: --r-lg`、`padding: --sp-4`、`box-shadow: --shadow-sm`。Modifier:
- `.card--flat`:密集**清單項**用(`--r-md`、無 shadow),例:逐題結果、題目列。
- `.card--active`:選中/作用態(`--line-accent` + `--accent-soft`),例:作用中的 profile。
用法:markup 同時掛 `.card`(+ 視情況 `--flat`/`--active`)與該卡的專屬 class;專屬 class **只留**
獨有 layout(grid/flex/min-height/hover),base 外觀全由 `.card` 提供。卡內動作列放底部
`margin-top:auto`,**位置不隨內容高度浮動**。

> 已套用:`.notebook-card`、`.profile-card`、`.eval-authoring-card`(feature);`.eval-item-card`、
> `.eval-result`(`.card--flat` 清單項)。**待收**:`.index-stat`(統計格,未來可加 `.card--stat`);
> Studio 工具磚 `.tool-tile` 可加 `.card--tile`。各卡專屬 class 的舊 base 屬性已從 CSS 移除。

### 3.4 表格 `.table-wrap > table` **[標準,以 `admin_users` 為準]**
- 一律包 `.table-wrap`(處理橫向捲動與圓角)。
- **每個 `<td>` 一律加 `data-label="…"`**,手機才能堆疊(目前只有 `admin_users` 有做;eval 表格要補)。
- 操作欄用 `<td class="actions">`,內含 ghost/small 按鈕或 `<form>`。
- 資料密度高時優先**卡片列表**(見 `_eval_run_results` 的逐題卡)而非寬表。

### 3.5 狀態與標籤 pill(分兩軌,**停止超載**)
目前 `.status indexed/failed/processing` 被同時拿來表示索引狀態、角色、同步、run 狀態、hit/miss——
綠色同時代表「已索引/管理員/成功/命中」,語意失效。改成兩軌:

| 軌 | class | 用途 | 視覺 |
|---|---|---|---|
| **處理狀態** | `.status`(+ `indexed/processing/failed/uploaded`) **[標準]** | 真正的生命週期狀態:indexed/processing/failed、queued/running/succeeded、hit/miss、approved/draft | 語意色 + **左側圓點**(`::before`) |
| **中性標籤** | `.tag`(+ `.tag--accent` / `.tag--warn`) **[標準]** | 非狀態的分類標籤:角色(管理員)、題型(answerable…)、來源 origin、profile「系統預設」 | 扁平、**無圓點**、中性 |

> **判斷規則(很好記)**:這個 pill 會隨時間改變嗎?會 → `.status`(有點);不會、只是分類 → `.tag`(無點)。
> 「有點 = 生命週期」「無點 = 分類」是兩軌的視覺差異,顏色因此不再被當成狀態誤讀。
> 已套用:`admin_users` 角色、`_eval_items_section` 題型 + 來源 origin 改 `.tag`(approved/draft 仍是 `.status`)。

### 3.6 按鈕階層(五級)
| 角色 | 寫法 | 何時用 |
|---|---|---|
| Primary | `<button>`(預設樣式) | 每個區塊**最多一個**主要動作(送出、建立、套用) |
| Secondary | `.secondary` | 次要但非低調的動作(自動生成、approve) |
| Ghost | `.ghost`(+`.small`) | 低調/連結式動作、表格內操作、返回 |
| **Danger(顯著)** | `.danger` | 醒目的破壞性動作:整個區塊的主刪除/清除(清除向量、刪除 profile) |
| **Danger(低調)** | `.ghost.small.danger-link` | 密集清單/選單裡的低調刪除(刪對話 `×`、刪筆記本、刪來源、刪筆記、刪使用者列) |

尺寸 modifier:`.small`(28px)、`.wide`(滿寬)。
> **破壞性有兩種強度,不是一種**:顯著用有框的 `.danger`;在 list/menu 裡為了不喧賓奪主,用 ghost+紅字的 `.danger-link`(全 app 已一致這樣用)。**不要**把密集清單裡的刪除全換成有框 `.danger`——那會更突兀。
> 真正待修的只是 `.danger-link` 的 CSS 用了 `!important`(P2 清理,不影響外觀)。
> **`.secondary` 濫用要收斂**:只給「次要動作」;主要送出鈕一律 primary。

### 3.7 表單
- **每個欄位用 `<label>` 包**(label 文字 + 控制項),`label` 已是 grid 直排(`style.css:575`)。
- 排列:多欄位直排用 `.stacked-form`;少數並排用 `.inline-form` 或 `.two-col`。
- 選填說明用 `<span class="muted">（…）</span>`,範例值放 `placeholder`。
- 表單底部主要動作 + 返回放 `.page-foot`(或現行 `.settings-foot`)。
- 送出鎖見 §4。

### 3.8 分頁 tabs（單一視覺,兩種機制）
視覺統一 `.eval-tab`(目前命名綁 eval,**重構時更名 `.tab`**)。兩種語意分清楚:
- **跨頁導覽** → `<a href>`(如 `_eval_nav.html`:評測集 / Retrieval Profiles)。
- **頁內切換** → Alpine `<button>` + `x-show`(如 `admin_eval_set.html` 的 Authoring/Runs)。
兩者用同一組 class 與 `.is-current` 高亮,markup 對齊。

### 3.9 空狀態 `.empty-state` **[待建立,收斂三種]**
```html
<div class="empty-state">
  <h2>標題(沒有東西時的一句話)</h2>
  <p>下一步提示,可含一個行動連結。</p>
</div>
```
> 收斂對象:`.empty`(home/search)、`.run-compare-empty`、以及裸 `<p class="muted">尚未…</p>`。
> 表格/列表「目前是空的」一律用 `.empty-state`,不要用裸 muted 段落。

### 3.10 Alerts(補滿四語意)
頁面層級訊息列,固定左側色條 + 文字:

| class | 語意 | 狀態 |
|---|---|---|
| `.notice` | 成功 | [標準] 綠 |
| `.alert` | 錯誤 | [標準] 紅 |
| `.support-note` | 資訊/提示 | [標準] 紫 |
| `.warn` | 警告 | **[待建立]** 黃(`--warn*` token 已存在) |

行內微提示(非訊息列)用 `<p class="hint muted small">`。

---

## 4. 互動行為標準(已半成形,以下為定版)

- **送出鎖**:表單加 `data-loading-form`,主要鈕加 `data-loading-text="處理中..."`。
  app.js 會加 `.is-submitting`(鎖住表單 + 轉圈)並換鈕文字(`app.js:337`)。**所有會送出的表單都要加。**
- **破壞性確認**:任何刪除/清除/會改變線上行為的動作,在 `<form>` 加
  `data-confirm="清楚說明後果的一句話"`(`app.js:321` 觸發原生確認框)。
- **HTMX 局部更新**:就地更新用 `hx-target`/`hx-swap="outerHTML"`,partial 命名 `_*.html`;
  跨片段連動用 `HX-Trigger` 事件(見 `CLAUDE.md` 的 `indexed-sources-changed` 等)。
- **輪詢**:背景工作(如 eval run)用 `hx-trigger="load delay:1s, every 2s"`,完成後停止輪詢。
- **hover/focus**:已由全域 token 統一(`:focus-visible` → `--shadow-focus`),元件不要各自覆寫。

---

## 5. 內容與語氣

- **全站 zh-Hant**。介面標題、按鈕、空狀態一律中文。
  > 目前 eval 頁有英文漂移(Authoring / Runs / Run History / Compare / Per-question Results / Top retrieved)——重構時中文化。
- **專有名詞保留原文**:Recall / MRR / Profile / chunk / embedding / RAG 等技術詞不強譯。
- 標題用名詞短語;說明句精簡、講「使用者能做什麼/後果是什麼」。
- 破壞性 `data-confirm` 文案要明確講**後果**(例:「刪除使用者與其所有筆記本…無法復原」)。

---

## 6. 現況 → 目標 對照表(重構檢查清單)

| 主題 | 現況(多套) | 目標 | 主要影響檔 |
|---|---|---|---|
| 外層容器 | `.settings` / 無 | `.page` | 全部置中頁 |
| 頁首 | `.settings-head` / `.page-head` | `.page-head` | `settings`、`admin_*`、`eval_*` |
| 區塊標題 | 4 種 | `.section-head`(+slots) | `_eval_items_section`、`search`、`admin_eval_set` |
| 卡片 | 5 套 | ✅ `.card`(+`--flat`/`--active`)已上線;`index_stat`/工具磚待收 | `notebook`/`profile`/`eval_authoring`/`eval_item`/`eval_result` |
| 表格 RWD | 部分有 `data-label` | 一律 `data-label` | 所有 eval 表格 |
| 狀態色 | `.status` 超載 | ✅ `.status`(狀態)/ `.tag`(分類)已上線 | `admin_users`、`_eval_items_section` |
| 破壞性鈕 | `!important` on `.danger-link` | 保留兩級(`.danger` 顯著 / `.danger-link` 低調),僅去 `!important` | `style.css`(P2) |
| 空狀態 | 3 種 | `.empty-state` | eval 頁 |
| Alert | 缺 warning | 補 `.warn` | `style.css` |
| 分頁 | 2 套 class 對齊但命名綁 eval | `.tab`(更名) | `_eval_nav`、`admin_eval_set` |
| 語言 | eval 頁英文漂移 | zh-Hant | `eval_*` |

> 落地順序建議:**P0** eval 頁對齊既有 admin 標準(頁首/區塊/空狀態/按鈕/中文化)→
> **P1** 抽 `.page-head`/`.section-head`/`.card`/`.empty-state` 共用元件 + pill 雙軌、表格補 `data-label` →
> **P2** `.settings`→`.page` 更名、補 `.warn`、清死 CSS。每階段獨立 PR。
