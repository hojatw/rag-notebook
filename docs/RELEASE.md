# 發版與 CI

版號怎麼走、CHANGELOG 誰來寫、CI 會擋什麼。這份文件補上先前只存在於維護者習慣裡、
repo 中找不到任何紀錄的流程。

## 版號的單一事實來源

repo 根目錄的 **`VERSION`** 檔。執行期由 [`app/version.py`](../app/version.py) 讀取，
顯示在頁尾、`app_started` 日誌行與 `GET /healthz`。

`NOTEBOOKLM_VERSION` / `NOTEBOOKLM_GIT_SHA` 環境變數可覆寫檔案與 git 查詢的結果，
給發版流水線使用。Docker 因為不會複製 `.git`，commit 要在 build 時傳進去：

```bash
docker build --build-arg NOTEBOOKLM_GIT_SHA=$(git rev-parse --short HEAD) -t notebooklm .
```

專案仍在 `0.x`：**新功能進 MINOR，純修正與依賴更新進 PATCH**。

## 核心慣例：功能 PR 不動版號

這是最容易做錯的一條，所以講明白：

> **功能 PR 只在 `CHANGELOG.md` 的 `[未發布]` 段落累積條目，不要碰 `VERSION`。**
> **版號提升是獨立的 `chore(release)` PR。**

為什麼要分開：

- 版號在功能 PR 裡改，多支 PR 平行進行時必然互相衝突，而且合併順序會決定版號，
  等於讓 git 的排程決定產品的版本語意。
- 「這一版包含哪些東西」是到了要發版那一刻才知道的判斷，不是每支 PR 各自猜的。
- 分開之後，`chore(release)` 的 diff 就是一份乾淨的發版說明，review 時看得到
  「這一版對維運者有什麼影響」，不會混在功能程式碼裡。

## 發功能 PR 時

1. 在 `CHANGELOG.md` 的 `[未發布]` 下加條目，依 Keep a Changelog 的分類
   （新增／變更／修正／依賴／升級注意事項）。
2. **寫給維運者看，不是寫給 reviewer 看。** 條目要回答「這對我的部署有什麼影響」，
   而不是「改了哪個函式」。破壞性變更或需要人為動作的（重新索引、重設密碼、
   改設定鍵）一律寫進「升級注意事項」。
3. **不要動 `VERSION`。**
4. 掛上 label —— `.github/release.yml` 依 **label**（不是標題文字）分類自動產生的
   Release notes，沒掛 label 的會落進 catch-all「功能與變更」。
   目前的分類：`dependencies` → 依賴更新、`documentation` → 文件、
   其餘 → 功能與變更；`ignore-for-release` 則完全排除。

## 要發版時

獨立開一支 `chore(release)` PR：

1. 更新 `VERSION`。
2. 把 `CHANGELOG.md` 的 `[未發布]` 改成 `## [X.Y.Z] - YYYY-MM-DD`，並在上方留一個
   新的空 `## [未發布]`。
3. 讀過一遍整段——多支 PR 累積下來常會有重複或前後矛盾的敘述，這是唯一會一次看到
   全部條目的時機。
4. Merge 後打 tag 並建立 GitHub Release：

```bash
git tag v$(cat VERSION) && git push origin v$(cat VERSION)
```

```bash
gh release create v$(cat VERSION) --generate-notes
```

`--generate-notes` 產出的是「發生了什麼」（依 label 分類的 PR 清單）。
**「升級時要注意什麼」不會自動產生**，那部分手寫在 `CHANGELOG.md`，
才是維運者真正需要的內容。

## Rebase 之後要檢查什麼

**git 只保證文字接得上，不保證內容還成立。** 這一輪連續三次遇到「自動合併成功、
但結果需要人看一眼」，三次的症狀都不一樣，所以列成清單而不是靠記憶：

1. **讀過每一個「自動合併成功」的檔案。** 沒有衝突標記不代表沒事——那只表示兩邊改的
   是不同的行。
2. **有沒有敘述被另一支 PR 推翻了？** 實際發生過：一支 PR 在 `SECURITY.md` 把某項列為
   「仍未關閉的最高風險項」，而**正在合併的這支就是修它的**。合併後文件會宣稱一個
   已經修好的問題還開著。**凡是寫「仍未」「尚未」「目前不支援」的句子，都要重新確認。**
3. **`CHANGELOG.md` 有沒有重複的分類標題？** 兩支 PR 各自在 `[未發布]` 下新增
   `### 安全性`，git 會兩個都留。Keep a Changelog 的格式是一個分類一段，要合併。
4. **標題有沒有掉到行首以外的地方？** 也實際發生過：用「以標題字串當錨點、把新內容
   接在前面」的方式插段落時，如果那個錨點原本不在行首，插進去的標題就會黏在前一句
   句尾，markdown 完全不會把它算成標題。插段落時要**連同前面的換行一起比對**。
   快速掃法：

   ```bash
   grep -nE '[a-z.,)`] #{2,4} ' docs/*.md *.md
   ```

5. **backlog 的打勾狀態。** 完成的項目要打勾並註明 durable 紀錄搬到哪了，
   否則下一個人會重做。
6. **重跑 `pytest`，不要只信 rebase 沒報錯。** 兩支 PR 各自綠不代表合起來綠——
   語意上的交互作用（新的相依、共用 fixture、被改變的預設值）只有全套件跑得出來。

用 `--force-with-lease` 而不是 `--force` 推送 rebase 過的分支：若遠端在你 rebase
期間被別人動過，前者會擋下來。

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) 在**每一支 PR** 與推上
`main` 時執行，Python 3.12：

```text
pip install -r requirements.txt -r requirements-dev.txt
python -m py_compile app/*.py tests/*.py
pytest -q            # 帶 NOTEBOOKLM_SECRET=ci-test-secret
```

也就是說本機的 `.venv/bin/pytest` 綠了，CI 就會綠——**兩邊跑的是同一組檢查**，
CI 沒有額外的門檻，也沒有涵蓋本機沒跑到的東西。反過來說，本機沒跑測試就送 PR，
CI 只是幫你晚幾分鐘發現同一件事。

CI **不會**做的事（需要人）：瀏覽器走查、檢索 eval（`tests.eval_retrieval` 需要
可用的 LLM 設定）、Docker build 煙霧測試。這些的判準見
[`AGENTS.md`](../AGENTS.md) 的 Verification 段落。

## 依賴更新

[`.github/dependabot.yml`](../.github/dependabot.yml) 每週檢查 pip 相依。

處理原則見 [`SECURITY.md`](SECURITY.md) 的 *Triaged dependency-audit findings*：
**每個安全警示都要留下判定紀錄**，說明它為什麼適用或不適用於這個部署，
這樣同一個警示不會被反覆重查。判定為不適用的，如果升級成本低仍然照升——
紀錄要解釋的是「這個警示為什麼存在」，不是「為什麼跳過升級」。

檔案解析相依（`pypdf` / `openpyxl` / `python-pptx` / `Pillow` / `charset-normalizer`）
是本專案價值最高的更新對象，因為它們處理的是使用者上傳的檔案。
