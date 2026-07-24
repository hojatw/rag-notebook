# 企業 SSO 部署與測試手冊（operator 指南）

> **對象**：負責部署本應用的 IT／operator。
> **本文定位**：照著做的實操手冊（設定範例、逐步 checklist、測試計劃）。
> 設計理念與**安全契約的權威文件**是 [`AUTHENTICATION.md`](AUTHENTICATION.md)；兩者衝突時以該文為準。
> ⚠️ 本文的反向代理設定是**範例骨架**，必須依你的實際環境調整。標記 **`【待確認】`** 之處，請先向客戶 IT 問清楚再填。

相關規劃：[`ROADMAP.md`](ROADMAP.md) I1a（信任頭部）／I1b（OIDC）／I1d（operator 文件）。

---

## 0. 先回答這些（customer discovery — 決定你走哪條路）

部署前務必先取得答案，這些答案直接決定架構：

1. 權威身份平台是哪個？ Microsoft Entra ID／ADFS／純 on-prem AD／Keycloak／其他？
2. 支援 **OIDC** 嗎？若否，是否要求 SAML？（本版尚未實作 SAML，見 §6）
3. 「Windows 帳號自動登入」目前由誰處理？ IIS/Kerberos、Entra 無縫 SSO、還是現成的企業 SSO gateway？
4. 客戶 IT 能否提供 app 註冊資訊與 **group claim**？
5. 哪些 AD／IdP 群組要映射為本應用 **admin**？
6. 是否保留本地密碼登入作為 **break-glass**（緊急管理員）？

### 決策樹

| 你的環境 | 走哪一節 |
|---|---|
| 有 OIDC IdP（Entra／ADFS／Keycloak） | **§2 OIDC**（最簡單，app 直接對接 IdP） |
| 只有 on-prem AD、要 Windows 直登、無 OIDC 端點 | **§3 信任頭部** + 前置 Kerberos/IWA gateway |
| 已有企業 SSO gateway 能吐出「已驗證身份 header」 | **§3 信任頭部** |
| 想要 SSO 為主、又保留緊急本地登入 | 任一節 + 保持 `local_login_enabled = true` |

> **核心觀念**：本應用**不自己做** Kerberos／SPNEGO／NTLM。「Windows 直登」是由**前置的反代／gateway** 完成 Windows 驗證後，把已驗證身份交給本應用（§3），或由瀏覽器既有的 IdP session 走 OIDC（§2）。詳見 [`AUTHENTICATION.md`](AUTHENTICATION.md)。

---

## 1. 共通前置

- **`NOTEBOOKLM_SECRET`**：穩定、強隨機。輪替它會使已加密的 LLM API key 失效（需到 `/settings` 重新輸入），也會使任何 DB 內加密欄位失效。
- **網路隔離（安全契約核心）**：app container 綁在內網，**只有反向代理可達**。嚴禁客戶端直連 app。
- **設定途徑**：`config.toml` 的 `[auth]` 段，或 `NOTEBOOKLM_AUTH_*` 環境變數（同名大寫，例：`NOTEBOOKLM_AUTH_OIDC_ENABLED=true`）。完整參數見 [`config.example.toml`](../config.example.toml) 的 `[auth]` 段。
- **保留 break-glass**：先維持 `local_login_enabled = true`，待 SSO 端到端驗證無誤，再視政策決定是否關閉。**關閉本地登入前，務必確認至少一個由 SSO 映射成功的 admin 帳號可登入**，否則會把自己鎖在外面。

---

## 2. OIDC 模式（Entra ID／ADFS／Keycloak）

### 2.1 在 IdP 註冊應用

- **Redirect URI**：`https://<app-host>/auth/oidc/callback`（若改了 `oidc_redirect_path` 則對應調整）。
- 取得 **issuer**、**client_id**、**client_secret**。
- **【待確認】group claim**：確保 IdP 會在 token 回傳群組。
  - Microsoft Entra ID：於 App registration → Token configuration 加入 **groups claim**；並優先使用 **租戶專屬 v2.0 issuer**，格式範例：
    `https://login.microsoftonline.com/<tenant-id>/v2.0`
  - Keycloak：於 client 加一個 **groups** mapper（token claim name = `groups`）。
  - 若 IdP 回傳的是 group **物件 id** 而非名稱，`oidc_admin_groups` 需填對應 id（見 §2.2）。

### 2.2 應用設定（`config.toml` `[auth]` 範例）

```toml
[auth]
local_login_enabled = true            # 上線初期先保留 break-glass
oidc_enabled = true
oidc_issuer = "https://login.microsoftonline.com/<tenant-id>/v2.0"   # 【待確認】
oidc_client_id = "<client-id>"                                        # 【待確認】
oidc_client_secret = "<client-secret>"   # 放 .env 或 gitignored config.toml，勿 commit
oidc_scopes = "openid profile email"
oidc_redirect_path = "/auth/oidc/callback"
oidc_token_auth_method = "client_secret_basic"   # 或 client_secret_post，依 IdP
oidc_email_claim = "email"
oidc_name_claim = "name"
oidc_groups_claim = "groups"             # 【待確認】對應 IdP 實際 claim 名
oidc_admin_groups = "rag-admins"         # 【待確認】填 admin 群組名（或 group id）
oidc_auto_provision = true               # 首次登入自動建本地帳號
oidc_allowed_algorithms = "RS256"        # 勿放寬到對稱演算法
```

- `oidc_discovery_url` 可留空，預設用 `<issuer>/.well-known/openid-configuration`。
- IdP 的 discovery／authorization／token／JWKS 端點**必須是 HTTPS**（僅 localhost 開發例外）。

### 2.3 client secret 存放

- 目前為 **config/env-only**：放在 `.env` 或 gitignored 的 `config.toml`，**切勿 commit**，也不會寫入稽核 metadata。
- 尚未做 DB 加密儲存（Fernet），因此**輪替 secret 只需改設定並重啟**，不受 `NOTEBOOKLM_SECRET` 輪替影響。

### 2.4 驗證

1. 開登入頁 → 應出現「OIDC 登入」按鈕。
2. 點按 → 導向 IdP → 驗證 → 回到 `/auth/oidc/callback` → 自動建立／連結本地帳號 → 進 `/notebooks`。
3. 以 admin 群組帳號登入，確認取得 admin 權限（可進 `/settings`、`/admin/*`）。
4. 開 `/admin/auth` 檢視設定健檢與 claim 映射摘要（此頁**不呼叫 IdP**，不會卡在網路延遲）。

---

## 3. 信任反向代理頭部模式（on-prem AD Windows 直登／現成 gateway）

### 3.1 架構與安全契約

```
①  網域內瀏覽器
      │  (Kerberos/IWA 或 Entra 無縫 SSO — 由前置層完成 Windows 驗證)
      ▼
②  反向代理 / SSO Gateway     ← 客戶環境負責 Windows 驗證
      │  驗證成功後：strip 掉客戶端偽造的身份 header，
      │  改注入「已驗證身份 header」+ shared-secret header
      ▼
③  本應用 GET /auth/trusted-header   ← 接收端（本應用做的部分）
         驗 shared secret → 映射/建立本地帳號 → 發 session
```

**安全契約（三條，缺一不可）**：
1. 客戶端**不能直連**本應用（只有反代到得了）。
2. 反代**必須先 strip 掉**客戶端自帶的身份 header（如 `X-Forwarded-User`），再設自己的值。
3. shared secret 只存在於「反代 → app」這一跳。

> ⚠️ **本應用層預設只有 shared secret 一道**，可另設 `trusted_header_allowed_ips`（見 §3.2）作為第二道防線。即便如此，上述①②的部署正確性仍至關重要：secret 外洩、反代未 strip inbound header，或 allowlist 因啟用 uvicorn `--proxy-headers` 而被 `X-Forwarded-For` 繞過（此時須把 `--forwarded-allow-ips` 釘死為反代），都可能讓能連到 app 的請求冒充任意使用者（含 admin）。

### 3.2 應用設定（`config.toml` `[auth]` 範例）

```toml
[auth]
local_login_enabled = true
trusted_header_enabled = true
trusted_header_secret = "<long-random-shared-secret>"   # 【待確認】與反代共用
trusted_header_secret_header = "X-NotebookLM-Auth-Secret"
trusted_header_user_header = "X-Forwarded-User"     # 穩定的外部 subject（如 UPN/sAMAccountName）
trusted_header_email_header = "X-Forwarded-Email"
trusted_header_name_header = "X-Forwarded-Name"
trusted_header_groups_header = "X-Forwarded-Groups"  # 逗號/分號分隔
trusted_header_admin_groups = "rag-admins"           # 【待確認】admin 群組名
trusted_header_auto_provision = true
trusted_header_allowed_ips = ""   # 【待確認】選填第二道防線：生產填反代來源 IP/CIDR（如 "10.0.0.0/8"）只信任反代；本機 curl 測試留空。比對 request.client.host（TCP 對端），非 X-Forwarded-For
```

### 3.3 反代設定範例（依環境擇一或組合）

#### 3.3a Nginx（純反代／TLS 終結 — 本身不做 Windows 驗證）

```nginx
server {
    listen 443 ssl;
    server_name app.example.com;          # 【待確認】
    # ssl_certificate / ssl_certificate_key ...   # 【待確認】TLS 憑證

    location / {
        # 1) strip 掉客戶端偽造的身份 header（清空，不可信任 inbound）
        proxy_set_header X-Forwarded-User  "";
        proxy_set_header X-Forwarded-Email "";
        proxy_set_header X-Forwarded-Name  "";
        proxy_set_header X-Forwarded-Groups "";

        # 2) 由本層（或上游 auth 模組）設定「已驗證」身份 —— 見下方說明
        #    注意：Nginx 自身不提供 IWA，$remote_user 需由 auth 模組/上游填入
        proxy_set_header X-Forwarded-User   $remote_user;   # 【待確認】身份來源
        proxy_set_header X-NotebookLM-Auth-Secret "<shared-secret>";  # 與 app 共用

        proxy_pass http://app-internal:8000;   # 內網 app
    }
}
```

> ⚠️ **Nginx 單獨無法提供 Integrated Windows Authentication**。若要網域瀏覽器靜默登入，需搭配 §3.3b／§3.3c，或前置一個現成 SSO gateway。

#### 3.3b Apache httpd + mod_auth_gssapi（Linux 上做 Kerberos/IWA）

適用：純 on-prem AD、要 Windows 直登、無 OIDC。骨架：

```apache
<Location "/">
    AuthType GSSAPI
    AuthName "Windows SSO"
    GssapiCredStore keytab:/etc/krb5.keytab      # 【待確認】客戶 AD 提供的 keytab
    Require valid-user

    # 驗證成功後 REMOTE_USER = 使用者 principal
    RequestHeader unset X-Forwarded-User          # 先清 inbound
    RequestHeader set   X-Forwarded-User  "%{REMOTE_USER}s"
    RequestHeader set   X-NotebookLM-Auth-Secret "<shared-secret>"
    # 群組通常需再向 AD/LDAP 查詢後填入 X-Forwarded-Groups（依部署方式）  # 【待確認】

    ProxyPass        http://app-internal:8000/
    ProxyPassReverse http://app-internal:8000/
</Location>
```

**【待確認】依賴客戶 AD 環境**：SPN（如 `HTTP/app.example.com`）、keytab、realm、DNS、瀏覽器 intranet-zone 政策。這些由客戶 IT 提供，非本應用可決定。

#### 3.3c oauth2-proxy（把 OIDC/OAuth2 轉成身份 header）

適用：有 OIDC IdP，但想在代理層集中處理登入、對 app 只吐 header。oauth2-proxy 驗證後可帶出 `X-Forwarded-User`／`X-Forwarded-Email`／`X-Forwarded-Groups`；在其後再由反代注入 shared-secret header 給 app。**注意仍須遵守 §3.1 的三條契約。**

> 也可用「Nginx 負責 TLS/反代 + Apache 負責 Kerberos」的雙層組合；只要契約不變即可。

### 3.4 驗證

先用 `curl` 模擬「反代已驗證並注入 header」之後的第③段（本機起真 uvicorn）：

```bash
# 成功：帶正確 secret + 身份 header
curl -i -H "X-NotebookLM-Auth-Secret: <secret>" \
        -H "X-Forwarded-User: alice@corp.example" \
        -H "X-Forwarded-Groups: rag-admins" \
        http://localhost:8000/auth/trusted-header
# 期望：303 導向 /notebooks，Set-Cookie: session=...；alice 自動建帳號且為 admin

# 失敗：缺 secret（模擬偽造）→ 403
curl -i -H "X-Forwarded-User: mallory" http://localhost:8000/auth/trusted-header
```

再開 `/admin/auth` 檢視啟用模式與設定健檢。

---

## 4. 測試計劃

分四層，由「你自己就能做」推進到「客戶環境」。

### Level 0 — 單元測試（✅ 已完成）
`pytest` 全套件 **197 passed**，含 16 個 auth 測試（正/負路徑）。程式改動後回歸執行：
```bash
.venv/bin/pytest tests/test_ui.py -k "auth or oidc or trusted or sso" tests/test_config.py
```

### Level 1 — 本機整合（不需客戶環境）
- **信任頭部**：用 §3.4 的 `curl` 驗證 provision／admin 映射／缺 secret 被拒。
- **OIDC**：Docker 起一個 **Keycloak**（或用 Entra 測試租戶），建 realm/client → 填 `[auth]` → 走 `/auth/oidc/login` 端到端。
  - ⚠️ OIDC 需連 IdP 的 discovery/token/JWKS，**要有網路 egress**（預覽沙箱不通，須用真 uvicorn）。

### Level 2 — Staging／類生產（貼近客戶拓撲，驗安全契約）
把 app 綁內網、前面擺真反代，**逐項確認 §3.1 的三條契約**：
1. 客戶端無法直連 app（嘗試直打 app port 應失敗）。
2. 從外部帶假 `X-Forwarded-User` 經反代 → 應被反代 strip 掉，無法冒充。
3. 無 shared secret 的請求 → app 回 403。

### Level 3 — 客戶 UAT（真 IdP）
- domain-joined Windows 瀏覽器驗「Windows 直登」端到端。
- 用客戶真實 group 名驗 `*_admin_groups` 映射。
- **break-glass**：模擬 IdP 不可用，確認本地 admin 仍能登入。

### 測試檢核清單

| 類別 | 案例 | 期望 |
|---|---|---|
| 正向 | 本地帳密登入 | 不受影響，照舊成功 |
| 正向 | 信任頭部登入 + auto-provision | 建本地帳號、發 session |
| 正向 | OIDC 登入 + auto-provision | 同上 |
| 正向 | admin group 使用者登入 | 取得 admin；`/admin/*` 可進 |
| 正向 | 既有外部身份再次登入 | 更新 email/name/groups、不重複建帳號 |
| 負向 | 信任頭部：缺／錯 shared secret | 503／403，記稽核 |
| 負向 | 信任頭部：偽造身份 header 無 secret | 403 |
| 負向 | 未知身份 + auto-provision 關閉 | 403 |
| 負向 | OIDC：state／nonce 不符 | 400，記稽核 |
| 負向 | OIDC：`iat` 過期／未來、非 HTTPS issuer | 拒絕 |
| 負向 | 對 SSO 帳號重設本地密碼 | 被擋（400） |
| 回歸 | CSRF、per-user notebook 隔離 | 維持不變 |
| 授權 | SSO 新建 user 只看得到自己的 notebook | per-user scoping 不變 |

---

## 5. 上線後維運與已知限制

**已知限制（MVP，請向客戶明講，屬刻意取捨）**：
- **群組映射於登入時計算**：AD／IdP 的群組變更，要到使用者下次登入才生效。
- **session 無伺服器端撤銷**：在 IdP 停用某使用者，不會即時終止其現有 app session（到期才失效）。
- **無 IdP-initiated logout**：app 登出只清本地 session。

**維運動作**：
- 停用使用者：於 IdP 停用；如需立即斷開，另刪除／停用其本地 `users` 帳號。
- 稽核：`/admin/audit` 可見 `*_login_succeeded`／`*_user_provisioned`／`*_role_mapped`／`*_login_rejected`（含 reason code）。
- 疑難排解：先看 `/admin/auth` 設定健檢，再對照稽核的 rejection reason。

**已實現的強化**：
- 信任頭部**來源 IP allowlist**：設定 `trusted_header_allowed_ips`（IP/CIDR，見 §3.2）作為 shared secret 之外的第二道防線。

**建議強化（follow-up，非上線阻擋）**：
- OIDC 導入 **PKCE**（Entra／Keycloak 皆支援）。本應用是 confidential client（已有 state + nonce + client secret），PKCE 屬縱深防禦與 OAuth 2.1 對齊，非補漏洞。

---

## 6. 尚未涵蓋

- **SAML（I1c）**：依規劃屬 customer-driven，本版未實作。若客戶 IdP 只支援 SAML，需另立項目（見 [`AUTHENTICATION.md`](AUTHENTICATION.md) Phase 2）。
