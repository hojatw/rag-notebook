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
| 強調 | `--accent/-strong/-soft/-deep`、`--accent-2`、`--accent-soft-2`、`--accent-wash` | 主色(靛紫);後三者是漸層/淡底的搭配色 |
| 反白前景 | `--on-accent` / `--on-danger` | 疊在 accent / danger 填色上的文字色 |
| 語意色 | `--ok* / --warn* / --danger*`(各 base/soft/line) | 成功/警告/危險 |
| 元件表面 | `--glass`(頂欄)、`--scrim`(遮罩)、`--placeholder`、`--chip-neutral-bg/-text`、`--code-bg/-text` | U11 從寫死值收斂而來 |
| Motion | `--ease`、過場 140ms | 已全域套在 `a/button/input...` |

**規則**:任何新 CSS 的顏色、間距、圓角、陰影都必須引用上表變數,不得寫死數值。

### 1.1 深色模式(U11)

- **主題只存在於 token 層**。`[data-theme="dark"]` 區塊(`style.css`,緊接 `:root` 之後)只覆寫變數值;**元件規則永遠不得依主題分支**。想加新顏色時,先加 token 並在深色區塊給對應值,不要在元件裡寫 `[data-theme="dark"] .foo { color: ... }`。
- **唯一的例外**是 `select` 的下拉箭頭:它是 data URI SVG,stroke 無法讀 CSS 變數,所以深色另存一份。新增任何 data-URI 圖形時要記得同樣處理。
- **偏好來源**:`users.theme`(`system` | `light` | `dark`),在 `/account` 設定。`light`/`dark` 由伺服器直接渲染成 `<html data-theme>`,**無 JS 也有效**;`system` 不輸出 `data-theme`,由 `base.html` head 內的同步 inline script 依 `prefers-color-scheme` 解析(避免換頁閃白),並持續跟隨系統切換。
- **對比要求**:正文 ≥ 4.5:1、大字與圖形 ≥ 3:1,兩個主題都要滿足。深色的 `--accent` 會調亮,所以 `--on-accent` 反轉為近黑——**填色按鈕的文字一律用 `var(--on-accent)`,不要寫 `#fff`**。

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
>
> **已採用**:`search`、notebook domain settings。數量 slot 目前用既有的 `.pane-count`
> (不另立 `.count`,避免多一套 pill)。
>
> ⚠️ **CJK 注意**:全域 `h2` 是 12px/uppercase/letter-spacing 的 overline 樣式,設計給
> 英文小標。中文沒有 uppercase 效果,12px 會**比它底下的 13px `<label>` 還小**,層級直接
> 倒過來。`.section-head h2` 已覆寫為 18px,所以**區塊標題一律走 `.section-head`**,不要
> 裸用 `<h2>`。

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
> 已套用:`admin_users` 角色、`_eval_items_section` 題型 + 來源 origin 改 `.tag`(approved/draft 仍是 `.status`)、產出置物架的類型徽章與類型篩選 chip(`.note-kind` / `.note-filter`,U16 Phase 2 — 類型不隨時間改變,所以是 `.tag` 軌;選取態用 `.is-active`)。

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
> ~~真正待修的只是 `.danger-link` 的 CSS 用了 `!important`~~ ✅ 已移除(`.danger-link` 定義在 `.ghost` 之後,同特異度靠原始碼順序即勝出,不需 `!important`)。
> **`.secondary` 濫用要收斂**:只給「次要動作」;主要送出鈕一律 primary。

### 3.7 表單
- **每個欄位用 `<label>` 包**(label 文字 + 控制項),`label` 已是 grid 直排(`style.css:575`)。
- 排列:多欄位直排用 `.stacked-form`;少數並排用 `.inline-form` 或 `.two-col`。
- 選填說明用 `<span class="muted">（…）</span>`,範例值放 `placeholder`。
- 表單底部主要動作 + 返回放 `.page-foot`(或現行 `.settings-foot`)。
- 送出鎖見 §4。
- **行內 checkbox 用 `.setting-check`**(全域基礎樣式已建立)。基礎 `label` 是 grid 直排,
  checkbox 掉進去會被撐成整欄寬、文字被擠到第二列,所以**不要裸用 `<label>` 包 checkbox**。
- **「啟用某個區塊」的開關用 `.switch-field`**:左標題 + 一行說明,右側 switch,整列可點。
  它是原生 checkbox 用 `appearance: none` 畫成軌道 + `::after` 旋鈕,鍵盤與表單序列化都
  照舊;軌道色走 `--line-strong` / `--accent`,旋鈕走 `--switch-knob`(深色模式有覆寫)。
- **一頁多個 POST scope 時,每個 scope 各自畫成一張卡、儲存列收在卡片框線內**。單一頁尾
  儲存列只有在整頁就是一個 form 時才成立;否則那條列會停在頁面中段,讀起來像「存全部」。

E2 的 Notebook domain settings 使用獨立的 `.settings` 頁面，不把多筆
hints 編輯器塞進 Notebook dropdown。頁面提供兩個獨立 enable toggle、bounded
answer policy，以及垂直 hint cards；term/synonyms/definition/query
expansions/answer note 均由真正的 `<label>` 包住。Hint delete 必須是帶
`data-confirm` 的 POST。窄螢幕下長 term/policy 使用可換行 textarea、按鈕可
堆疊，不得產生水平捲軸；所有 copy 走 `domain.*` / `error.*` catalog keys。

版面定案(2026-08):兩個 enable toggle 用 `.switch-field`;筆記本層級設定收在一張
`.domain-panel` 卡裡、儲存列 `.domain-panel-foot` 在卡片框線內(返回靠左、儲存靠右,
手機 `column-reverse` 讓儲存在上);提示清單是卡外的獨立 section,標題走 `.section-head`
+ `.pane-count`。每張 hint card 的標題顯示**該筆的 term**(空白才退回通用標題),刪除鍵
在卡片右上、儲存鍵在卡片右下,兩者刻意分開。刪除表單與編輯表單是**同層 sibling**
(HTML 不允許 form 巢狀),且只掛 `data-confirm`——與 `data-loading-form` 併用會在使用者
取消確認後把按鈕永久留在 disabled。

### 3.8 分頁 tabs（單一視覺,兩種機制）
視覺統一 `.eval-tab`(目前命名綁 eval,**重構時更名 `.tab`**)。兩種語意分清楚:
- **跨頁導覽** → `<a href>`(如 `_eval_nav.html`:評測集 / Retrieval Profiles)。
- **頁內切換** → Alpine `<button>` + `x-show`(如 `admin_eval_set.html` 的 Authoring/Runs)。
兩者用同一組 class 與 `.is-current` 高亮,markup 對齊。

### 3.9 空狀態(兩級,依「空的是整頁還是一個區塊」選)
```html
<div class="empty-state">
  <h3>標題(沒有東西時的一句話)</h3>
  <p>下一步提示,可含一個行動連結。</p>
</div>
```

| class | 用在 | 外觀 | 狀態 |
|---|---|---|---|
| `.empty` | **整頁**沒東西(home/search 的搜尋無結果) | 大留白 `--sp-7`、accent 徑向漸層底 | [標準] |
| `.empty-state` | **區塊內**的清單是空的(領域提示清單) | `--sp-5`、虛線框、無漸層 | [標準] |

> 兩者的差別是音量,不是語意:整頁空著要有存在感,區塊空著不該蓋過同區的其他內容。
> **裸 `<p class="muted">尚未…</p>` 一律換成其中一個**,`.run-compare-empty` 待收斂。
> (§6 對照表原本記「統一用 `.empty`」,那是 eval 頁那一輪的結論;`.empty` 的音量放進
> 區塊裡太重,因此本節改記為兩級制。)

### 3.10 Alerts(補滿四語意)
頁面層級訊息列,固定左側色條 + 文字:

| class | 語意 | 狀態 |
|---|---|---|
| `.notice` | 成功 | [標準] 綠 |
| `.alert` | 錯誤 | [標準] 紅 |
| `.support-note` | 資訊/提示 | [標準] 紫 |
| `.warn` | 警告 | **[標準]** 黃(`--warn*`) |

行內微提示(非訊息列)用 `<p class="hint muted small">`。

### 3.11 Image sources and image-search results **[待建立]**

圖片來源沿用 notebook workspace 的既有三欄模型,不要新增獨立圖片庫頁面。第一版 image search
是「圖片被索引成 OCR/caption 文字,結果回指到原圖」,因此 UI 要同時呈現原圖與它被系統理解出的文字。

- **Sources pane**:圖片列使用既有 source row + status pill,加一個小型 thumbnail/format badge。thumbnail
  尺寸固定,避免圖片比例造成左欄 row 高度大幅跳動。若 capability gate 不通過,上傳前就阻擋,不要讓圖片以
  failed source 混進列表。
- **Source preview drawer**:圖片來源打開後先顯示 bounded image preview,接著顯示 extraction diagnostics
  (檔名、尺寸、OCR/caption 狀態、警告),再列出可 citation 的文字 sections:OCR text、Visual description、
  Image metadata。使用者要能看出搜尋命中的是哪一段 derived text。
- **Citation behavior**:chat citation 點擊時仍開 source preview drawer,並高亮對應 OCR/caption section;
  citation label 用「filename · image / OCR / caption」這種短標籤,不要把大圖直接塞進 chat message。
- **Search results**:搜尋頁的圖片結果要顯示小 thumbnail、filename、命中 section 類型與 snippet。
  點擊後進 notebook 並打開 preview drawer 到對應 section,與文字來源的 citation 行為一致。
- **Empty/blocked states**:若 `/settings` image-understanding diagnostic 未通過,upload hint/錯誤訊息要明確說明
  「目前模型未通過圖片理解測試,請管理員到 /settings 執行測試或改用 OCR-only 模式」。不要把這寫成一般
  「不支援檔案格式」。

---

## 4. 互動行為標準(已半成形,以下為定版)

- **送出鎖(兩種機制,依場景擇一,不要混用)**:
  - **整頁送出 / 一般 POST 表單** → `data-loading-form` + 主要鈕 `data-loading-text="處理中..."`。app.js 會加 `.is-submitting`(鎖住表單 + 轉圈)並換鈕文字(`app.js`)。
  - **HTMX partial 送出**(`hx-post` 就地換片段) → 用 HTMX 原生 `hx-disabled-elt="find button[type=submit]"` + `hx-indicator="#…"`。這類請求生命週期由 HTMX 管,用原生屬性比 `data-loading-form` 更貼合,毋須強套。
  > 判準:**這個送出會整頁/重導,還是只換一個片段?** 整頁 → `data-loading-form`;只換片段(partial) → `hx-disabled-elt`。**每個會送出的表單都要有其中一種鎖**,不可兩者皆無。
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
| 外層容器 | `.settings` / 無 | `.page`(更名) | 全部置中頁 |〔P2 待辦,純更名〕 |
| 頁首 | `.settings-head` / `.page-head` | **決定保留差異**:admin 用 `.settings-head`、user 端 `.page-head` 各自維持(刻意區分產品/管理頁) | — |
| 區塊標題 | 4 種 | P0 已把 eval 的 `.section-heading` 攤平為 admin 標準 `<h2>`;`.section-title-row` 保留為「標題+行內動作」 | `_eval_items_section` 等 |
| 卡片 | 5 套 | ✅ `.card`(+`--flat`/`--active`)已上線;`index_stat`/工具磚待收 | `notebook`/`profile`/`eval_authoring`/`eval_item`/`eval_result` |
| 表格 RWD | 部分有 `data-label` | ✅ eval 全表已補 `data-label` | 所有 eval 表格 |
| 狀態色 | `.status` 超載 | ✅ `.status`(狀態)/ `.tag`(分類)已上線 | `admin_users`、`_eval_items_section` |
| 破壞性鈕 | `!important` on `.danger-link` | ✅ 保留兩級;已去 `!important` | `style.css` |
| 空狀態 | 3 種 | ✅ 兩級制(§3.9):`.empty` 整頁 / `.empty-state` 區塊內 | eval 頁、domain settings |
| Alert | 缺 warning | ✅ `.warn` 已補(紅/黃/綠/紫齊全) | `style.css` |
| 分頁 | 2 套 class 對齊但命名綁 eval | `.tab`(更名) | `_eval_nav`、`admin_eval_set`〔P2 待辦,純更名〕 |
| 語言 | eval 頁英文漂移 | ✅ zh-Hant(P0) | `eval_*` |
| Admin 寬度 | 720/1120/1180 三種 | ✅ 兩級制:表單 720 / 資料頁 1120(eval+稽核共用) | `style.css` |

### 進度與收尾(2026-06,本階段視為完結)

這一輪 UI 重構的**有感工作已全部落地**:
- **P0** — eval 頁中文化、區塊標題攤平為 admin 標準、空狀態/inline 訊息標準化。
- **P1** — pill 雙軌(`.status`/`.tag`)、eval 全表 `data-label`、卡片收斂(`.card`/`--flat`/`--active`)、Eval/稽核寬度統一為單一寬版、`.settings-head` 間距放鬆。
- **P2** — 補 `.warn` alert、去 `.danger-link` 的 `!important`、清掉 6 個死選擇器。

**刻意不做(本階段結束,記錄決定,非待辦遺漏):**
- **頁首不統一**:admin 用 `.settings-head`、user 端用 `.page-head`,**刻意讓管理頁與產品頁有區別**(產品擁有者決定)。
- **兩個純更名 deferred**:`.eval-tab`→`.tab`、`.settings`→`.page`。理由:純改名、使用者無感,但 `.settings` 牽動 ~12 模板 + 大量後代選擇器,churn 大、回歸風險高,違反「除非降低風險否則避免大範圍重構」。**若哪天要做,連同 §2.1 的 `.page` 骨架一起當獨立 PR**,不要單獨改名。
- **`.index-stat`、Studio 工具磚**尚未併入 `.card`(§3.3 待收),屬未來小項,非本階段範圍。
