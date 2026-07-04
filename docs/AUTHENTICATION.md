# Authentication and enterprise SSO

Authoritative product and implementation notes for login, local accounts, and
enterprise identity integration.

## Current state

The app supports local SQLite-backed accounts and an optional trusted
reverse-proxy header login bridge.

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
  or `/admin/users`.

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
```

The proxy must strip inbound identity headers from clients, authenticate the
user itself, then set both the identity headers and the shared-secret header
when forwarding to the app. The app deliberately does not implement Kerberos,
SPNEGO, or NTLM itself.

### Phase 1b: OIDC

- configurable issuer / discovery URL, client id, client secret, scopes, and
  redirect URI;
- Authorization Code flow;
- ID token validation (`iss`, `aud`, `exp`, `nonce`) and JWKS key rotation;
- local account linking by stable external subject (`sub`) plus provider id;
- optional username/email display fields from claims;
- group/role claim mapping to local `is_admin`;
- client secret stored with the existing encrypted-at-rest pattern
  (`app/security.py` Fernet keyed from `NOTEBOOKLM_SECRET`, as for LLM API
  keys), or kept env/`config.toml`-only for the first iteration. Either way,
  rotating `NOTEBOOKLM_SECRET` invalidates DB-stored encrypted secrets — note
  this in the deployment checklist;
- audit events for SSO login, first-time account provisioning, account linking,
  role mapping changes, and SSO configuration changes.

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
- never accept identity headers from arbitrary clients;
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
- SSO buttons, login hints, and auth error copy go through the i18n catalog
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
