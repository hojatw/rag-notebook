# Authentication and enterprise SSO

Authoritative product and implementation notes for login, local accounts, and
enterprise identity integration.

## Current state

The app supports local SQLite-backed accounts, an optional trusted
reverse-proxy header login bridge, and optional OIDC login.

Local accounts:

- users sign in with username/password at `/login`;
- passwords are PBKDF2-SHA256 hashes stored in `users.password_hash`;
- a signed `session` cookie carries the local user id;
- `users.is_admin` controls access to `/settings` and `/admin/*`;
- admin user management can create users, reset passwords, toggle admin, and
  delete accounts.

Trusted-header mode (`I1a`, disabled by default):

- `GET /auth/trusted-header` accepts identity headers only when
  `[auth].trusted_header_enabled = true` and the request carries the configured
  shared-secret header;
- external identities are persisted in `external_identities` as
  `provider + subject -> users.id`, so all existing per-user authorization
  continues to use the local user id;
- first login can auto-provision a local user when enabled;
- configured group names map to `users.is_admin` at login time;
- SSO-linked accounts cannot set or reset a local password through `/account`
  or `/admin/users`;
- SSO-linked accounts cannot have their local admin role manually toggled in
  `/admin/users`; the IdP/proxy group mapping is authoritative at login time.

OIDC mode (`I1b`, disabled by default):

- `GET /auth/oidc/login` starts an Authorization Code flow and stores
  `state`/`nonce` in a short-lived signed, HTTP-only cookie;
- `GET /auth/oidc/callback` exchanges the code, validates the ID token
  signature and `iss`/`aud`/`exp`/`nbf`/`iat`/`nonce`/`sub` claims, then issues
  the same local `session` cookie used by local login;
- discovery, authorization, token, and JWKS endpoints must use HTTPS, with an
  HTTP exception only for localhost development, and discovery `issuer` must
  match configured `oidc_issuer` when one is set;
- provider subjects are persisted in `external_identities` as
  `provider + sub -> users.id`;
- configured group claims map to `users.is_admin` at login time;
- OIDC-linked accounts share the same local-password guardrail as
  trusted-header accounts.

Admin/operator diagnostics (`I1d`):

- `GET /admin/auth` displays enabled auth modes, static SSO configuration health
  checks, trusted-header/OIDC claim mapping summaries, and operator pointers to
  audit rejection reason codes;
- the page is intentionally static and does not call the external IdP, so an
  admin console view cannot hang on IdP network latency.

This is enough for the single-machine POC, but it is not the target model for
customer deployments that already have Active Directory, Microsoft Entra ID,
ADFS, Keycloak, or a corporate SSO gateway.

## Customer requirement

Some customer intranet sites automatically sign users in with the Windows
account they used to log in to their computer. In Microsoft-heavy environments
this is commonly Integrated Windows Authentication (IWA), usually Kerberos via
SPNEGO/Negotiate, with NTLM as a legacy fallback.

Do not treat that requirement as "add LDAP login". LDAP/LDAPS bind verifies a
username and password, but it does not by itself provide browser-based silent
SSO. The requirement has two separate parts:

1. **Identity source:** users, groups, and roles come from AD/Entra/IdP.
2. **Login experience:** domain-joined corporate browsers may sign in without
   re-entering credentials.

## Product direction

Prefer standard enterprise SSO integration at the application boundary:

1. **OIDC first.** This should be the primary enterprise SSO path for Microsoft
   Entra ID, modern ADFS, Keycloak, Auth0, and similar identity providers.
2. **SAML second.** Add when a customer IdP requires SAML or their enterprise
   app catalog standardizes on SAML.
3. **Trusted reverse-proxy header mode as an integration bridge.** Support
   deployments where a customer-owned gateway, IIS, Apache, Nginx, or identity
   proxy performs Kerberos/IWA/SAML/OIDC authentication and forwards a verified
   identity to the app.
4. **Keep local break-glass admin.** Local login should remain available for a
   small number of emergency admin accounts unless a deployment explicitly
   disables it.

This ordering describes the product's standards surface, not the delivery
order. For delivery, trusted header mode ships before OIDC — see "Recommended
MVP" — because it is much smaller and works in every Microsoft intranet
topology, including on-prem-AD-only deployments where no OIDC endpoint exists.

Avoid making Nginx + Kerberos/SPNEGO the product's primary integration path.
Kerberos setup depends on customer-owned infrastructure details such as SPNs,
keytabs, domain join state, DNS, browser intranet-zone policy, and fallback
behavior. It is better handled by the customer's existing SSO layer or a
deployment-specific reverse proxy than by the FastAPI app itself.

## Recommended MVP

**Step 0 — customer discovery.** Answer the "Open questions for each customer"
below before building anything. The answers decide the build order: a
deployment with on-prem AD only and no ADFS/Keycloak/Entra has no OIDC endpoint
to integrate with, and trusted header mode behind a customer-owned gateway is
then the only viable route to "automatic Windows account" login.

Ship enterprise authentication in bounded, independently deliverable phases.
Trusted header mode comes before OIDC: it is much smaller (no new dependency,
no discovery/JWKS, straightforward to cover with pytest) and it works in every
Microsoft intranet topology.

### Phase 1a: trusted reverse-proxy header mode — implemented

- deployment-disabled by default;
- configurable trusted header names for user id, display name/email, and groups;
- a concrete app-side trust check, not a topology assumption: bind the app to
  localhost / an internal container network so the proxy is the only network
  path, **and** require a configurable shared secret (or mTLS) that the proxy
  attaches to every request;
- optionally pin an app-side source-IP allowlist (`trusted_header_allowed_ips`,
  IP/CIDR) as defense-in-depth on top of the shared secret; it matches the TCP
  peer (`request.client.host`), never a forgeable `X-Forwarded-For` (with
  uvicorn `--proxy-headers`, pin `--forwarded-allow-ips` to the proxy);
- reject direct client-supplied identity headers by requiring the proxy to strip
  inbound versions and set its own;
- map groups to local admin/user roles;
- record audit events for header-auth login and provisioning.

Configuration lives in `config.toml` / `NOTEBOOKLM_AUTH_*` env vars:

```toml
[auth]
local_login_enabled = true
trusted_header_enabled = false
trusted_header_secret = ""
trusted_header_secret_header = "X-NotebookLM-Auth-Secret"
trusted_header_user_header = "X-Forwarded-User"
trusted_header_email_header = "X-Forwarded-Email"
trusted_header_name_header = "X-Forwarded-Name"
trusted_header_groups_header = "X-Forwarded-Groups"
trusted_header_admin_groups = ""
trusted_header_auto_provision = true
trusted_header_provider = "trusted_header"
trusted_header_allowed_ips = ""   # optional proxy source-IP allowlist (IP/CIDR); empty = secret-only
```

The proxy must strip inbound identity headers from clients, authenticate the
user itself, then set both the identity headers and the shared-secret header
when forwarding to the app. The app deliberately does not implement Kerberos,
SPNEGO, or NTLM itself.

For Linux/container customer environments, keep the Python app as a plain
FastAPI service and put the enterprise web/auth layer in front of it instead of
trying to embed IIS or Windows authentication into the app container. A typical
deployment can use Docker Compose (or an equivalent orchestrator) with the app
container on an internal network and one or more fronting services such as
Nginx, Apache httpd, Traefik, `oauth2-proxy`, or a customer-owned SSO gateway.
The fronting layer owns public ingress, TLS private keys and certificates,
HTTP-to-HTTPS redirects, and upstream enterprise authentication. The app should
only receive verified identity headers over the internal hop, together with the
configured shared-secret header.

Nginx alone is usually only the reverse proxy/TLS termination layer; it does
not provide Integrated Windows Authentication by itself. If the customer needs
domain-joined browser silent sign-in on Linux, the deployment normally needs an
additional auth-capable component, for example Apache httpd with a Kerberos/
GSSAPI module, an OIDC/OAuth2 sidecar such as `oauth2-proxy` backed by Entra ID,
ADFS, or Keycloak, a third-party Nginx SPNEGO module, or an existing enterprise
SSO gateway. Using both Nginx and Apache in front of the app is acceptable when
the customer environment benefits from separating TLS/proxy duties from
Kerberos/IWA duties, but the security contract remains the same: clients must
not be able to reach the app directly, inbound identity headers must be stripped
before auth, and only the trusted proxy may inject the app's identity headers.

### Phase 1b: OIDC — implemented

- configurable issuer / discovery URL, client id, client secret, scopes,
  redirect path, token endpoint auth method, claim names, admin group mapping,
  and allowed signing algorithms;
- Authorization Code flow with short-lived signed `state`/`nonce` cookie; no
  Starlette SessionMiddleware or second app session store;
- ID token validation using Authlib / joserfc against discovery JWKS:
  signature, `iss`, `aud`, `exp`, `nbf`, `iat`, `nonce`, and `sub`;
- local account linking by stable external subject (`sub`) plus provider id in
  `external_identities`;
- optional username/email display fields from claims;
- group/role claim mapping to local `is_admin`;
- HTTPS-only IdP endpoints, except localhost HTTP for development, and discovery
  issuer binding to configured `oidc_issuer`;
- client secret is config/env-only in this MVP. Keep it in `.env` or
  gitignored `config.toml`, never in committed files or audit metadata;
- audit events for OIDC login, first-time account provisioning, and role mapping
  changes. Token/code values are never written to audit metadata.

OIDC configuration lives in `[auth]`:

```toml
oidc_enabled = false
oidc_provider = "oidc"
oidc_issuer = ""
oidc_discovery_url = ""
oidc_client_id = ""
oidc_client_secret = ""
oidc_scopes = "openid profile email"
oidc_redirect_path = "/auth/oidc/callback"
oidc_token_auth_method = "client_secret_basic" # or client_secret_post
oidc_email_claim = "email"
oidc_name_claim = "name"
oidc_groups_claim = "groups"
oidc_admin_groups = ""
oidc_auto_provision = true
oidc_allowed_algorithms = "RS256"
```

Register the app callback URL with the IdP as
`https://<app-host>/auth/oidc/callback` unless `oidc_redirect_path` is changed.
For Microsoft Entra ID, prefer the tenant-specific v2.0 issuer URL and ensure
the app registration returns any required group claim.

### Phase 2: SAML when required

SAML features:

- service-provider metadata endpoint;
- assertion consumer service (ACS) endpoint;
- signed assertion validation;
- issuer/entity ID and certificate configuration;
- NameID / attribute mapping;
- group/role mapping equivalent to OIDC.

## Security requirements

Enterprise auth must preserve the existing route-level authorization model:

- every route that reads or mutates notebook data must still scope by local
  `user_id`;
- external identities must map to local users before application data is
  accessed;
- group claims may grant admin, but admin should be explicit and auditable;
- SSO group mapping is the authority for SSO-linked users; do not allow local
  admin toggles to silently override IdP/proxy role decisions;
- never accept identity headers from arbitrary clients;
- reject non-HTTPS OIDC issuer/discovery/authorization/token/JWKS endpoints,
  except explicit localhost development endpoints, and bind discovery `issuer`
  to configured `oidc_issuer` when present;
- never store IdP client secrets, SAML private keys, tokens, or assertions in
  audit/governance metadata;
- keep raw tokens out of logs;
- make local-login fallback explicit per deployment;
- disable or clearly flag `/admin/users` password reset for SSO-provisioned
  accounts — resetting a password for an SSO user silently re-opens a local
  login path around IdP deprovisioning;
- keep CSRF/session protections working for all browser flows.

Trusted header mode must only be enabled behind a controlled reverse proxy.
The proxy must be the only network path to the app, must terminate the upstream
authentication flow, and must strip any inbound user/group headers before
setting trusted values.

## Known limitations (MVP)

State these plainly to customers; they are deliberate POC trade-offs, not
oversights:

- group/role claims are evaluated at login time only — group changes in AD or
  the IdP take effect at the user's next login;
- session cookies are signed but have no server-side revocation; disabling a
  user at the IdP does not terminate an existing app session until it expires;
- no RP-initiated (IdP) logout — app logout clears the local session only.

## Implementation notes (repo conventions)

- Persisting external identities (provider + `sub`) is a schema change: update
  [`SCHEMA.md`](SCHEMA.md) in the same change, per `AGENTS.md`.
- Document new `/auth/*` routes in [`ROUTES.md`](ROUTES.md).
- Document admin/operator auth diagnostics routes in [`ROUTES.md`](ROUTES.md).
- Operator deployment steps, reverse-proxy config examples, and the auth test
  plan live in [`SSO_DEPLOYMENT.zh-TW.md`](SSO_DEPLOYMENT.zh-TW.md).
- SSO buttons, login hints, diagnostics text, and auth error copy go through the i18n catalog
  (`app/i18n.py`, see [`I18N.md`](I18N.md)); never hardcode UI strings.

## Open questions for each customer

Ask these questions before committing to a deployment design:

- Which identity platform is authoritative: Microsoft Entra ID, ADFS, on-prem
  AD only, Keycloak, or another IdP?
- Do they support OIDC? If not, do they require SAML?
- Is the "automatic Windows account login" handled by an existing SSO gateway,
  IIS/Kerberos, Entra Seamless SSO, or something else?
- Can their IT team provide app registration details and group claims?
- Which AD/IdP groups should map to app admin?
- Should local password login remain enabled for break-glass admin?
