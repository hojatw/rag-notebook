"""i18n Phase 0: message catalog lookup, fallback, and the window.I18N feed."""
import ast
from pathlib import Path

import app.i18n as i18n


def test_t_returns_source_locale_value():
    assert i18n.t("nav.notebooks") == "筆記本"
    assert i18n.t("nav.settings") == "系統設定"


def test_t_unknown_key_falls_back_to_the_key():
    # visible-but-safe: a missing string never raises, it shows the key
    assert i18n.t("nav.does_not_exist") == "nav.does_not_exist"


def test_t_unknown_locale_falls_back_to_default_locale():
    # an unpopulated locale resolves through DEFAULT_LOCALE rather than the key
    assert i18n.t("nav.search", locale="en") == "搜尋"


def test_t_applies_format_kwargs():
    table = i18n.MESSAGES[i18n.DEFAULT_LOCALE]
    table["test.greeting"] = "嗨 {name}"
    try:
        assert i18n.t("test.greeting", name="Phil") == "嗨 Phil"
    finally:
        del table["test.greeting"]


def test_js_messages_strips_prefix_and_covers_all_js_keys():
    msgs = i18n.js_messages()
    assert msgs["thinking"] == "思考中"
    # every js.* key in the source catalog must surface (prefix stripped)
    js_keys = {
        k[len("js."):] for k in i18n.MESSAGES[i18n.DEFAULT_LOCALE] if k.startswith("js.")
    }
    assert set(msgs) == js_keys


def test_js_messages_unknown_locale_uses_default_values():
    assert i18n.js_messages(locale="en")["thinking"] == "思考中"


def test_route_http_exception_details_are_not_hardcoded():
    """MNT-1 regression: rendered HTTP error copy must go through i18n."""
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((root / "app").glob("*.py")):
        relative_path = str(path.relative_to(root))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if function_name != "HTTPException":
                continue
            detail = next((keyword.value for keyword in node.keywords if keyword.arg == "detail"), None)
            if isinstance(detail, (ast.Constant, ast.JoinedStr)):
                offenders.append(f"{relative_path}:{node.lineno}")
    assert offenders == []
