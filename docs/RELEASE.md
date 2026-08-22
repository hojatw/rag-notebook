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
