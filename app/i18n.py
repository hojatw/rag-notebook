"""UI message catalog (Phase 0 of the i18n foundation).

A dependency-free dict catalog — `{locale: {key: text}}` — so language lives in
one place instead of being hardcoded across templates and `app.js`. No new
runtime deps, no build step (keeps the POC's constraints).

- The **source locale** is `zh-TW` and is the only fully-populated one for now.
  Other locales are intentionally deferred; lookups fall back locale → `zh-TW`
  → the key itself, so a missing translation degrades visibly-but-safely rather
  than raising.
- Templates use the `t()` Jinja global: ``{{ t("nav.notebooks") }}``.
- Client-side strings (keys under `js.*`) are emitted into ``window.I18N`` by
  base.html via :func:`js_messages`, so `app.js` reads them instead of holding
  its own copies.

Keys are dotted and grouped by surface (`nav.*`, `js.*`, …). Add new
user-facing strings here as later phases migrate each surface; do not reintroduce
hardcoded copy in templates/JS.
"""
from .config import config

# The source/fallback locale. Every key must exist here.
DEFAULT_LOCALE = "zh-TW"

# locale -> { dotted key -> text }. zh-TW is the source of truth; additional
# locales (e.g. "en") are a later phase — the structure already supports them.
MESSAGES: dict[str, dict[str, str]] = {
    "zh-TW": {
        # --- Primary nav (base.html top bar) — Phase 0 migration proof --------
        "nav.notebooks": "筆記本",
        "nav.search": "搜尋",
        "nav.users": "使用者",
        "nav.index": "索引",
        "nav.eval": "Eval",
        "nav.audit": "稽核",
        "nav.settings": "系統設定",
        "nav.logout": "登出",
        # --- Shared admin/common copy ----------------------------------------
        "common.admin_console": "管理主控台",
        "common.all": "全部",
        "common.apply": "套用",
        "common.clear": "清除",
        "common.close": "關閉",
        # --- Audit log page (Phase 2) ----------------------------------------
        "audit.title": "稽核紀錄",
        "audit.intro": "查核高風險操作、匯出、權限與系統設定變更。中繼資料只保存識別資訊與摘要，不保存 API key 或完整文件內容。",
        "audit.filter": "篩選",
        "audit.recent": "最近紀錄",
        "audit.f_action": "操作",
        "audit.f_action_ph": "例如 export / settings / profile",
        "audit.f_actor": "操作者",
        "audit.f_actor_ph": "username 或 user id",
        "audit.f_target_type": "對象類型",
        "audit.f_sensitivity": "敏感度",
        "audit.f_limit": "筆數",
        "audit.th_time": "時間",
        "audit.th_actor": "操作者",
        "audit.th_action": "操作",
        "audit.th_target": "對象",
        "audit.th_sensitivity": "敏感度",
        "audit.th_ip": "IP",
        "audit.th_metadata": "中繼資料",
        "audit.unknown_actor": "未知",
        "audit.metadata": "中繼資料",
        "audit.metadata_view": "查看中繼資料",
        "audit.metadata_view_for": "查看 {action} 中繼資料",
        "audit.metadata_close": "關閉中繼資料",
        "audit.empty": "尚無符合條件的稽核紀錄。",
        # --- Client-side strings consumed by app.js via window.I18N -----------
        "js.thinking": "思考中",
    },
}

# Lifecycle-classification label maps (zh-Hant). Kept as dicts (not t() keys)
# because the raw value comes from the DB row; `.get(value, value)` localises
# the display while preserving the stored identifier.
SENSITIVITY_LABELS: dict[str, str] = {
    "high": "高",
    "normal": "一般",
    "low": "低",
}


def active_locale() -> str:
    """The locale the UI should render in, resolved from config at call time."""
    return config.ui.language or DEFAULT_LOCALE


def t(key: str, locale: str | None = None, **kwargs: object) -> str:
    """Look up a message by key, falling back locale → DEFAULT_LOCALE → key.

    `**kwargs` are applied with ``str.format`` when present, so a catalog value
    may contain ``{name}`` placeholders. A missing key returns the key itself
    (visible-but-safe) rather than raising.
    """
    loc = locale or active_locale()
    text = (MESSAGES.get(loc) or {}).get(key)
    if text is None and loc != DEFAULT_LOCALE:
        text = (MESSAGES.get(DEFAULT_LOCALE) or {}).get(key)
    if text is None:
        text = key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def js_messages(locale: str | None = None) -> dict[str, str]:
    """Return the `js.*` strings (prefix stripped) for ``window.I18N``.

    Keys are sourced from the DEFAULT_LOCALE catalog (the complete set) and
    resolved through the active locale so a translated value wins when present.
    """
    loc = locale or active_locale()
    base = MESSAGES.get(DEFAULT_LOCALE) or {}
    table = MESSAGES.get(loc) or {}
    prefix = "js."
    return {
        key[len(prefix):]: (table.get(key) or base[key])
        for key in base
        if key.startswith(prefix)
    }
