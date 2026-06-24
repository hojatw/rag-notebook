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
        "nav.eval": "評測",
        "nav.audit": "稽核",
        "nav.settings": "系統設定",
        "nav.logout": "登出",
        # --- Shared admin/common copy ----------------------------------------
        "common.admin_console": "管理主控台",
        "common.all": "全部",
        "common.apply": "套用",
        "common.clear": "清除",
        "common.close": "關閉",
        "common.regenerate": "重新生成",
        "common.retry": "再試一次",
        "common.search": "搜尋",
        "common.name": "名稱",
        "common.description": "描述",
        "common.created_at": "建立時間",
        "common.actions": "操作",
        "common.status": "狀態",
        "common.open": "開啟",
        "common.view": "查看",
        "common.delete": "刪除",
        "common.list_truncated": "僅顯示最近 {count} 筆，較舊的未顯示。",
        # --- Notebook workspace ---------------------------------------------
        "workspace.mobile_sources": "來源",
        "workspace.mobile_chat": "對話",
        "workspace.mobile_studio": "工作台",
        # --- Eval workbench — landing + nav (Phase 2b, hybrid naming) ---------
        # 評測 = the activity/section; "Eval Set" / "Eval run" kept as data terms.
        "eval.title": "評測工作台",
        "eval.intro": "在部署內使用既有 DB 資料建立評測題組、執行 retrieval-only eval，避免客戶資料外流。",
        "eval.tab_sets": "評測集",
        "eval.tab_profiles": "檢索 Profile",
        "eval.tab_help": "調參指南",
        "eval.nav_sections": "評測工作台分頁",
        "eval.active_profile": "目前作用中的檢索 Profile：",
        "eval.manage_params": "管理 / 調整參數 →",
        "eval.create_set": "建立 Eval Set",
        "eval.create_loading": "建立中...",
        "eval.search_notebooks": "搜尋全站已索引筆記本",
        "eval.search_ph": "筆記本名稱或擁有者",
        "eval.create_hint": "目前 Eval Set 綁定單一目標筆記本。清單列出全站最近 100 個有已索引來源的筆記本；資料量大時請先搜尋。",
        "eval.set_name_ph": "例如：客戶中文研究報告 baseline",
        "eval.target_notebook": "單一目標筆記本",
        "eval.indexed_sources_suffix": "個已索引來源",
        "eval.set_desc_ph": "資料範圍、語言、用途或注意事項",
        "eval.no_notebooks": "目前沒有可用的筆記本。",
        "eval.sets_heading": "評測集",
        "eval.col_notebook": "筆記本",
        "eval.col_count": "題數",
        "eval.approved_suffix": "已核准",
        "eval.delete_set_confirm": "刪除此 Eval Set？相關題目、執行紀錄與結果都會一併刪除。",
        "eval.empty_sets_title": "尚未建立評測集",
        "eval.empty_sets_body": "從上方選一個筆記本建立第一個評測集。",
        "eval.runs_heading": "歷史執行紀錄",
        "eval.col_latency": "延遲",
        "eval.empty_runs_title": "尚未執行任何評測",
        "eval.empty_runs_body": "建立評測集並核准題目後即可執行 retrieval eval。",
        # --- Retrieval Profiles page (Phase 2b-ii) ---------------------------
        "prof.intro": "調整 runtime-safe 檢索參數（不影響索引）。作用中的 profile 會即時套用到線上 chat 檢索並在重啟後保留。",
        "prof.list_heading": "Profile 清單",
        "prof.hint": "建立候選 profile → 在「評測集」用它跑 eval、比較結果 → 滿意後按「套用」上線，或對先前 profile 按「套用」即為回滾。",
        "prof.badge_active": "作用中",
        "prof.badge_default": "系統預設",
        "prof.export": "匯出設定",
        "prof.apply": "套用",
        "prof.apply_confirm": "套用此 profile 到線上檢索？這會立即改變所有使用者的 chat 檢索行為。",
        "prof.in_use": "線上使用中",
        "prof.default_note": "回復預設的保底，不可刪除",
        "prof.delete_confirm": "刪除此 profile？歷史 run 的 snapshot 會保留。",
        "prof.create": "建立候選 Profile",
        "prof.name_ph": "例如：keyword 權重提高 candidate",
        "prof.desc_ph": "這個候選想驗證什麼假設",
        # --- aria-labels (Phase 2b-ii, L3) -----------------------------------
        "a11y.breadcrumb": "麵包屑導覽",
        "a11y.primary_nav": "主要導覽",
        "a11y.sources": "來源面板",
        "a11y.chat": "對話面板",
        "a11y.studio": "工作台面板",
        "a11y.workspace_pane_switcher": "工作區面板切換",
        "a11y.eval_set_sections": "評測集分頁",
        # --- Eval run/result/item partials (Phase 2b-iii) --------------------
        "evalr.progress": "進度",
        "evalr.avg_latency": "平均延遲",
        "evalr.per_question": "逐題結果",
        "evalr.no_results": "尚未產生逐題結果；執行進行中時本區塊會自動刷新。",
        "evalr.diagnosis": "診斷：",
        "evalr.evidence_toggle": "依據與實際檢索",
        "evalr.expected_evidence": "預期依據",
        "evalr.no_expected": "— 無預期依據（未評分）",
        "evalr.actual_top": "實際檢索 top",
        "evalr.items_heading": "題目管理",
        "evalr.items_hint": "草稿題目需核准後才會進入執行；不可回答題目會在 retrieval-only 執行中標為未評分。",
        "evalr.active_suffix": "（作用中）",
        "evalr.run_btn": "執行 retrieval eval",
        "evalr.run_starting": "啟動中...",
        "evalr.item_approved": "已核准",
        "evalr.item_draft": "草稿",
        "evalr.approve_btn": "核准",
        "evalr.delete_item_confirm": "刪除此題目？",
        "evalr.view_expected": "查看預期依據",
        "evalr.detail_source": "預期來源",
        "evalr.detail_answer": "參考答案",
        "evalr.empty_items_title": "尚未新增題目",
        "evalr.empty_items_body": "請從上方三種方式建立題目；只有核准的題目會進入執行。",
        # --- Chat empty-state + answer flow (Phase 1a) -----------------------
        "chat.empty_ask_title": "問任何關於你來源文件的問題",
        "chat.empty_ask_body": "在下方輸入問題，回答會附上引用的來源段落。",
        "chat.empty_indexing_title": "索引建立中",
        "chat.empty_indexing_body": "請等待左側來源完成索引，就可以開始提問。",
        "chat.empty_new_title": "先新增一個來源開始使用",
        "chat.empty_new_body": "依照這三步驟建立第一個可提問的筆記本。",
        "chat.step1_title": "上傳來源",
        "chat.step1_body": "把 PDF、DOCX、Markdown、TXT、HTML 或字幕(SRT/VTT)拖到左側。",
        "chat.step2_title": "等待索引",
        "chat.step2_body": "來源完成索引後，左側狀態會變成已完成。",
        "chat.step3_title": "開始提問",
        "chat.step3_body": "在下方輸入問題，回答會附上引用來源。",
        "chat.abstain": "依據所選的來源，我無法判斷這個問題的答案。",
        # --- Studio tools + suggestions (Phase 1a) ---------------------------
        "studio.tools_heading": "工具",
        "studio.tools_empty": "先上傳並完成索引，工具才能使用。",
        "studio.tool_need_index": "先完成來源索引",
        "studio.tool_need_n": "需要 {count} 個以上已索引的來源",
        "studio.suggestions_heading": "建議問題",
        "studio.suggestions_generate": "生成建議問題",
        "studio.suggestions_need_index": "先完成來源索引，才能生成建議問題。",
        "studio.suggestions_blurb": "從你的來源生成 4 個有依據的起始問題。",
        "studio.suggestions_loading": "建議問題生成中…",
        # --- Studio tool tiles + config panels (Phase 1a) -------------------
        "tool.compare": "來源比較",
        "tool.minutes": "會議記錄",
        "tool.study_guide": "學習指南",
        "tool.faq": "常見問答",
        "tool.timeline": "時間軸",
        "tool.translate": "翻譯摘要",
        "tool.compare_blurb": "勾選 2 個以上來源，依摘要比較共同點、差異與矛盾。",
        "tool.compare_btn": "開始比較",
        "tool.minutes_title": "會議記錄整理",
        "tool.minutes_blurb": "選一份會議逐字稿，整理成結構化會議記錄並存入筆記。",
        "tool.minutes_source_label": "選擇來源（會議逐字稿）",
        "tool.minutes_btn": "整理成會議記錄",
        "tool.translate_blurb": "選一份來源，把它的摘要翻譯成指定語言。",
        "tool.source_label": "選擇來源",
        "tool.target_lang": "目標語言",
        "tool.translate_btn": "翻譯",
        "tool.study_guide_blurb": "從所有已索引來源的摘要生成學習指南（核心概念、重點、自我測驗、名詞解釋）。",
        "tool.faq_blurb": "從所有已索引來源生成常見問答（FAQ）。",
        "tool.timeline_blurb": "從來源中抽取事件與里程碑，整理成時間軸。",
        "tool.generate": "生成{label}",
        "tool.generating": "生成中…",
        # --- Markdown export headings (Phase 1a) -----------------------------
        "export.citations": "引用來源：",
        "export.notes_suffix": "筆記",
        # --- User-facing server errors (Phase 1b) ----------------------------
        # friendly_error_message body templates ({action} is filled in)
        "error.timeout": "{action}逾時，請稍後再試。",
        "error.auth": "模型服務驗證失敗，請檢查系統設定中的 API key。",
        "error.ratelimit": "{action}暫時被模型服務限流，請稍後再試。",
        "error.unavailable": "模型服務暫時無法回應，請稍後再試。",
        "error.generic_check": "{action}失敗，請檢查系統設定後再試。",
        "error.no_llm": "尚未完成 LLM 設定，請先到系統設定填入模型連線資訊。",
        "error.generic_retry": "{action}失敗，請稍後再試；如果持續發生，請查看系統記錄。",
        # action labels passed to friendly_error_message
        "error.action_default": "處理",
        "error.action_answer": "回答生成",
        "error.action_suggestions": "建議問題生成",
        "error.action_briefing": "簡報生成",
        "error.action_compare": "來源比較",
        "error.action_translate": "摘要翻譯",
        "error.action_generate": "{label}生成",
        # per-flow inline errors shown in the Studio/chat partials
        "flow.suggestions_no_llm": "請先完成 LLM 設定，才能生成建議問題。",
        "flow.suggestions_empty": "模型未回傳建議問題，請再試一次。",
        "flow.briefing_no_llm": "請先完成 LLM 設定，才能生成簡報。",
        "flow.briefing_empty": "模型未回傳簡報內容，請再試一次。",
        "flow.compare_need_2": "請至少勾選 2 個來源進行比較。",
        "flow.compare_need_2_content": "至少需要 2 個有內容的來源才能比較。",
        "flow.compare_no_llm": "請先完成 LLM 設定，才能比較來源。",
        "flow.compare_empty": "模型回傳的比較結果為空，請再試一次。",
        "flow.minutes_no_source": "找不到已索引的來源，請重新整理後再試。",
        "flow.minutes_not_meeting": "這份來源看起來不像會議逐字稿或會議紀錄。",
        "flow.minutes_no_llm": "請先在系統設定完成 LLM 設定。",
        "flow.minutes_empty": "模型未能產生會議記錄，請再試一次。",
        "flow.artifact_need_index": "先完成來源索引，才能生成。",
        "flow.artifact_no_llm": "請先完成 LLM 設定，才能生成。",
        "flow.artifact_empty": "模型未回傳內容，請再試一次。",
        "flow.translate_no_source": "找不到已索引、且有摘要可翻譯的來源。",
        "flow.translate_no_llm": "請先完成 LLM 設定，才能翻譯。",
        "flow.translate_empty": "模型未回傳翻譯，請再試一次。",
        # --- LLM settings diagnostics (O1 Phase 1) --------------------------
        "settings.diag_title": "模型診斷",
        "settings.diag_intro": "測試目前表單中的候選設定；測試結果只保存狀態、延遲、模型摘要與錯誤類別。",
        "settings.diag_notice_chat": "聊天模型測試已完成。",
        "settings.diag_notice_embedding": "Embedding 模型測試已完成。",
        "settings.test_chat": "測試聊天模型",
        "settings.test_chat_loading": "測試聊天中...",
        "settings.test_chat_hint": "測試聊天連線與 capability probes。",
        "settings.test_embedding": "測試 embedding 模型",
        "settings.test_embedding_loading": "測試 embedding 中...",
        "settings.test_embedding_hint": "測試 embedding 連線並讀取 dimension。",
        "settings.test_image": "同時測試 image understanding",
        "settings.test_image_hint": "預設關閉；勾選後會送出內建極小測試圖片，只記錄 capability 狀態。",
        "settings.chat_diag": "聊天模型",
        "settings.embedding_diag": "Embedding 模型",
        "settings.no_diag": "尚未測試。",
        "settings.status": "狀態",
        "settings.provider": "提供者",
        "settings.model": "模型 / deployment",
        "settings.latency": "延遲",
        "settings.tested_at": "測試時間",
        "settings.error_class": "錯誤類別",
        "settings.embedding_dimension": "Embedding dimension",
        "settings.current_index_dimension": "目前索引 dimension",
        "settings.capabilities": "Capability probes",
        "settings.cap_streaming": "Streaming",
        "settings.cap_usage": "Provider usage reporting",
        "settings.cap_json": "JSON-following sanity check",
        "settings.cap_image": "Image understanding",
        "settings.status_succeeded": "成功",
        "settings.status_failed": "失敗",
        "settings.status_skipped": "未測試",
        "settings.status_not_tested": "未測試",
        # Split chat/embedding settings cards
        "settings.chat_card_title": "聊天模型",
        "settings.chat_card_intro": "回答問題、摘要、簡報等所有生成式功能使用的聊天模型與其連線。",
        "settings.embedding_card_title": "Embedding 模型",
        "settings.embedding_card_intro": "把來源切塊向量化、供檢索使用的 embedding 模型與其連線；可與聊天模型指向不同服務。",
        "settings.connection": "連線",
        "settings.model_section": "模型",
        "settings.api_key_optional": "選填 — 本地服務（如 e5 / Ollama / vLLM）不需要 key 時可留空",
        "settings.api_key_saved": "已儲存 — 留空表示沿用",
        "settings.azure_api_version": "Azure API 版本",
        "settings.runtime_params": "執行參數",
        "settings.dim_mismatch_warn": "偵測到的維度與目前索引不同：更換 embedding 維度需先到 /admin/index 點 Clear 再 Rebuild，現有向量才會相容。",
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
        "js.retrieving": "正在檢索來源…",
        "js.processing": "處理中…",
        "js.generating": "正在生成回答…",
        "js.answer_failed": "回答生成失敗。",
        "js.answer_failed_retry": "回答生成失敗，請稍後再試。",
        "js.role_you": "你",
        "js.role_assistant": "助理",
        "js.copied": "✓ 已複製",
        # upload widget (Phase 1c) — {max}/{count}/{size} filled by tr()/t()
        "js.upload_too_many": "一次最多上傳 {max} 個檔案，已保留前 {max} 個。",
        "js.upload_hint": "拖曳最多 {max} 個檔案到此，或點擊選擇",
        "js.upload_selected": "已選擇 {count} 個檔案",
        "js.upload_summary": "{count} / {max} 個檔案 · {size} · 送出後會排入索引",
        "js.provider_hint_openai": "請填入相容 /v1 的 base URL；模型欄位填模型名稱。",
        "js.provider_hint_azure": "請填入 Azure 資源端點；模型欄位填部署（deployment）名稱。",
        # upload formats hint (template-side, Phase 1c)
        "upload.formats": "PDF · TXT · Markdown · DOCX · HTML · 字幕(SRT/VTT) · 一次最多 {count} 個",
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

# Eval run lifecycle status (raw value stored; display localised).
RUN_STATUS_LABELS: dict[str, str] = {
    "queued": "排隊中",
    "running": "執行中",
    "succeeded": "成功",
    "failed": "失敗",
}

# Per-question eval result status (raw value stored; display localised).
EVAL_RESULT_STATUS_LABELS: dict[str, str] = {
    "hit": "命中",
    "miss": "未命中",
    "error": "錯誤",
    "unscored": "未評分",
    "pending": "處理中",
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
