// i18n (Phase 1): client-side strings come from window.I18N (emitted by
// base.html from the app/i18n.py catalog). `tr()` falls back to the literal so
// a missing key degrades safely. Pass `vars` to fill `{name}` placeholders
// (all occurrences), matching the server-side t(**kwargs) contract.
function tr(key, fallback, vars) {
  let s = (window.I18N && window.I18N[key]) || fallback;
  if (vars) {
    for (const name in vars) s = s.split("{" + name + "}").join(String(vars[name]));
  }
  return s;
}

// Register the drag-and-drop component with Alpine. v3 sandboxes x-data
// expressions, so a bare `dropzone()` lookup against window does NOT resolve.
// Using Alpine.data() guarantees the factory is in scope.
document.addEventListener("alpine:init", () => {
  window.Alpine.data("dropzone", () => ({
    over: false,
    enter() { this.over = true; },
    leave() { this.over = false; },
    drop(event) {
      this.over = false;
      const dropped = event.dataTransfer && event.dataTransfer.files;
      if (!dropped || !dropped.length) return;
      const input = this.$refs.input;
      const max = parseInt(input.getAttribute("data-max-files") || "0", 10);
      // DataTransfer is the only cross-browser way to assign a FileList
      // synthesised from drag-and-drop into <input type=file>.
      const transfer = new DataTransfer();
      const cap = max > 0 ? Math.min(dropped.length, max) : dropped.length;
      for (let i = 0; i < cap; i += 1) transfer.items.add(dropped[i]);
      input.files = transfer.files;
      if (max > 0 && dropped.length > max) {
        window.alert(tr("upload_too_many", "一次最多上傳 {max} 個檔案，已保留前 {max} 個。", { max }));
      }
      input.dispatchEvent(new Event("change", { bubbles: true }));
    },
  }));
});

document.addEventListener("DOMContentLoaded", () => {
  bindAll(document);
  scrollToLatestMessage();
  initSourceScope();
});

document.body.addEventListener("htmx:configRequest", (event) => {
  const token = csrfToken();
  if (token) event.detail.headers["X-CSRF-Token"] = token;
});

document.addEventListener("htmx:afterSwap", (event) => {
  bindAll(event.target);
  restoreSourceScopeState();
  // U1: after the ask partial swap, bring the new answer into view.
  if (event.target && event.target.id === "chat-messages") {
    scrollToLatestMessage();
  }
});

function bindAll(root) {
  bindConfirms(root);
  bindWorkspacePaneSwitcher(root);
  bindStreamingAskForms(root);
  bindLoadingForms(root);
  bindFileLabels(root);
  bindProviderNotes();
  renderMarkdown(root);
  bindSuggestionFill(root);
  bindAskFormThinkingBubble(root);
  bindChatInput(root);
  bindCopyButtons(root);
}

// ---- Mobile workspace pane switcher (U10) --------------------------------

function bindWorkspacePaneSwitcher(root) {
  bindOnce(root, "[data-workspace-switcher]", "workspacepane", (switcher) => {
    const workspace = document.querySelector("[data-workspace-mobile-tabs]");
    if (!workspace) return;

    const buttons = Array.from(switcher.querySelectorAll("[data-workspace-tab]"));
    if (!buttons.length) return;

    const paneNames = buttons.map((button) => button.dataset.workspaceTab).filter(Boolean);
    const setActive = (pane) => {
      const nextPane = paneNames.includes(pane) ? pane : "chat";
      workspace.dataset.activePane = nextPane;
      workspace.classList.add("is-mobile-tabs-ready");
      buttons.forEach((button) => {
        const active = button.dataset.workspaceTab === nextPane;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
        button.tabIndex = active ? 0 : -1;
      });
    };

    buttons.forEach((button) => {
      button.addEventListener("click", () => setActive(button.dataset.workspaceTab));
    });

    switcher.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const current = buttons.findIndex((button) => button.classList.contains("is-active"));
      const offset = event.key === "ArrowRight" ? 1 : -1;
      const next = buttons[(Math.max(current, 0) + offset + buttons.length) % buttons.length];
      next.focus();
      setActive(next.dataset.workspaceTab);
    });

    setActive(workspace.dataset.activePane || "chat");
  });
}

// ---- Streaming chat submit (U2) ------------------------------------------

function bindStreamingAskForms(root) {
  bindOnce(root, "form.ask-form[data-stream-url]", "stream", (form) => {
    form.addEventListener("submit", async (event) => {
      if (!window.fetch || !window.ReadableStream) return;
      event.preventDefault();
      const textarea = form.querySelector("textarea[name='question']");
      const question = (textarea && textarea.value || "").trim();
      if (!question) return;

      const messages = document.getElementById("chat-messages");
      if (!messages) return;
      const stream = createStreamingMessage(messages, question);
      const button = form.querySelector("button[type='submit']");
      const resetForm = () => {
        form.classList.remove("is-submitting");
        if (button && button.dataset.originalText !== undefined) {
          button.textContent = button.dataset.originalText;
          button.classList.remove("icon-only-loading");
          button.removeAttribute("aria-label");
          button.disabled = false;
          delete button.dataset.originalText;
        }
        if (textarea) {
          textarea.value = "";
          textarea.style.height = "auto";
          textarea.focus();
        }
      };

      try {
        const headers = { "Accept": "text/event-stream" };
        const token = csrfToken();
        if (token) headers["X-CSRF-Token"] = token;
        const response = await fetch(form.dataset.streamUrl, {
          method: "POST",
          body: new FormData(form),
          headers,
        });
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        await consumeEventStream(response.body, (eventName, data) => {
          if (eventName === "init") {
            const hidden = document.getElementById("conversation-id-field");
            if (hidden && data.conversation_id) hidden.value = data.conversation_id;
            if (data.url) window.history.pushState({}, "", data.url);
          } else if (eventName === "status") {
            stream.status.textContent = data.text || tr("processing", "處理中…");
          } else if (eventName === "chunk") {
            stream.status.textContent = tr("generating", "正在生成回答…");
            stream.answer += data.text || "";
            stream.body.textContent = stream.answer;
            stream.body.scrollIntoView({ behavior: "smooth", block: "end" });
          } else if (eventName === "error") {
            stream.status.textContent = data.text || tr("answer_failed", "回答生成失敗。");
          } else if (eventName === "done") {
            replaceMessagesHtml(data.html);
            if (data.url) window.history.pushState({}, "", data.url);
          }
        });
      } catch (error) {
        console.error("[notebook] stream failed", error);
        stream.status.textContent = tr("answer_failed_retry", "回答生成失敗，請稍後再試。");
      } finally {
        resetForm();
      }
    });
  });
}

function csrfToken() {
  const meta = document.querySelector("meta[name='csrf-token']");
  return meta ? meta.getAttribute("content") || "" : "";
}

function createStreamingMessage(messages, question) {
  const empty = messages.querySelector(".chat-empty");
  if (empty) empty.remove();

  const user = document.createElement("article");
  user.className = "message user";
  user.innerHTML = "<div class=\"message-head\"><div class=\"role\">" + tr("role_you", "你") + "</div></div>";
  const userBody = document.createElement("p");
  userBody.className = "message-body";
  userBody.textContent = question;
  user.appendChild(userBody);

  const assistant = document.createElement("article");
  assistant.className = "message assistant streaming-message";
  assistant.innerHTML =
    "<div class=\"message-head\"><div class=\"role\">" + tr("role_assistant", "助理") + "</div></div>" +
    "<p class=\"stream-status muted small\">" + tr("retrieving", "正在檢索來源…") + "</p>";
  const body = document.createElement("div");
  body.className = "message-body markdown-body streaming-body";
  assistant.appendChild(body);

  messages.appendChild(user);
  messages.appendChild(assistant);
  assistant.scrollIntoView({ behavior: "smooth", block: "start" });
  return {
    body,
    status: assistant.querySelector(".stream-status"),
    answer: "",
  };
}

async function consumeEventStream(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() || "";
    events.forEach((raw) => dispatchSseEvent(raw, onEvent));
  }
  if (buffer.trim()) dispatchSseEvent(buffer, onEvent);
}

function dispatchSseEvent(raw, onEvent) {
  let eventName = "message";
  const dataLines = [];
  raw.split(/\r?\n/).forEach((line) => {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return;
  try {
    onEvent(eventName, JSON.parse(dataLines.join("\n")));
  } catch (error) {
    console.error("[notebook] bad stream event", error);
  }
}

function replaceMessagesHtml(html) {
  const current = document.getElementById("chat-messages");
  if (!current) return;
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  const next = template.content.firstElementChild;
  if (!next) return;
  current.replaceWith(next);
  if (window.htmx) window.htmx.process(next);
  bindAll(next);
  restoreSourceScopeState();
  scrollToLatestMessage();
}

// ---- Copy assistant answer (U7) -------------------------------------------
// Copies the raw Markdown (stored by renderMarkdown before it rewrites the
// DOM) so the paste target gets clean source text, not rendered HTML.
function bindCopyButtons(root) {
  bindOnce(root, "[data-copy-message]", "copy", (button) => {
    button.addEventListener("click", async () => {
      const message = button.closest(".message");
      const body = message && message.querySelector(".message-body");
      if (!body) return;
      const text = body.dataset.rawMarkdown || body.innerText || "";
      try {
        await navigator.clipboard.writeText(text);
        const original = button.textContent;
        button.textContent = tr("copied", "✓ 已複製");
        setTimeout(() => { button.textContent = original; }, 1500);
      } catch (e) {
        console.error("[notebook] copy failed", e);
      }
    });
  });
}

// ---- Chat input: Enter-to-send, Shift+Enter newline, IME-safe, auto-grow --
// Standard chatbot input behaviour. CRITICAL for CJK: Enter that confirms an
// IME candidate must NOT submit — guarded via composition tracking +
// isComposing/keyCode 229. Submit goes through requestSubmit() so the existing
// submit handlers (source_ids injection, loading lock, thinking bubble) all run.
function bindChatInput(root) {
  const MAX_GROW_PX = 200; // ~8 lines, then the textarea scrolls internally
  bindOnce(root, "textarea#question-input", "chatinput", (textarea) => {
    const form = textarea.closest("form.ask-form");
    if (!form) return;

    let composing = false;
    textarea.addEventListener("compositionstart", () => { composing = true; });
    textarea.addEventListener("compositionend", () => { composing = false; });

    textarea.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      // Mid-IME-composition Enter only confirms the candidate — never submit.
      if (composing || event.isComposing || event.keyCode === 229) return;
      // Shift+Enter keeps the default newline.
      if (event.shiftKey) return;
      event.preventDefault();
      if (!textarea.value.trim()) return; // ignore blank / whitespace-only
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    });

    // Grow with content up to MAX_GROW_PX, then overflow scrolls (see CSS).
    const autogrow = () => {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, MAX_GROW_PX) + "px";
    };
    textarea.addEventListener("input", autogrow);
    autogrow();

    // U1: the ask form posts via HTMX (no page reload), so undo the
    // data-loading-form submit lock ourselves and reset the box for the
    // next question once the messages pane has been swapped in.
    form.addEventListener("htmx:afterRequest", (event) => {
      if (event.detail.elt !== form) return;
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = false;
        if (button.dataset.originalText) button.textContent = button.dataset.originalText;
      }
      form.classList.remove("is-submitting");
      if (event.detail.successful) {
        textarea.value = "";
        autogrow();
      }
      textarea.focus();
    });
  });
}

// Optimistic "thinking" placeholder inside the chat pane. The ask form does
// a full POST -> redirect; without something visible during the 5-30s LLM
// call the user can't tell the request was received. We insert an echo of
// their question + a typing-dots bubble that vanishes when the redirect
// completes and the page re-renders with the real assistant reply.
function bindAskFormThinkingBubble(root) {
  bindOnce(root, "form.ask-form", "askThinking", (form) => {
    form.addEventListener("submit", () => {
      if (form.dataset.streamUrl) return;
      const messages = document.querySelector(".messages");
      const textarea = form.querySelector("textarea[name='question']");
      const question = (textarea && textarea.value || "").trim();
      if (!messages || !question) return;
      const wrap = document.createElement("div");
      wrap.className = "chat-thinking";
      const q = document.createElement("div");
      q.className = "thinking-question";
      q.textContent = question;
      const bubble = document.createElement("div");
      bubble.className = "thinking-bubble";
      const thinkingLabel = tr("thinking", "思考中");
      bubble.innerHTML =
        "<span>" + thinkingLabel + "</span>" +
        "<span class=\"thinking-dots\"><span></span><span></span><span></span></span>";
      wrap.appendChild(q);
      wrap.appendChild(bubble);
      messages.appendChild(wrap);
      wrap.scrollIntoView({ behavior: "smooth", block: "end" });
    });
  });
}

// Idempotent binder: runs `setup(el)` once per element matching `selector`,
// using a data-* flag to remember which elements have been processed.
// Lets us safely re-bind after every HTMX swap without double-binding.
function bindOnce(root, selector, marker, setup) {
  const flag = `${marker}Bound`;
  root.querySelectorAll(`${selector}:not([data-${marker}-bound])`).forEach((el) => {
    el.dataset[flag] = "1";
    setup(el);
  });
}

// ---- Confirm dialogs ------------------------------------------------------

function bindConfirms(root) {
  bindOnce(root, "[data-confirm]", "confirm", (el) => {
    const confirm = (event) => {
      const message = el.getAttribute("data-confirm");
      if (message && !window.confirm(message)) event.preventDefault();
    };
    if (el.matches("form")) {
      el.addEventListener("submit", confirm);
    } else {
      el.addEventListener("click", confirm);
    }
  });
}

// ---- Submit-button loading state -----------------------------------------

function bindLoadingForms(root) {
  bindOnce(root, "form[data-loading-form]", "loading", (form) => {
    form.addEventListener("submit", () => {
      // CRITICAL: do NOT call .disabled = true on input/textarea/select inside
      // the submit handler — disabled controls are excluded from form
      // serialization, which means the browser would POST an empty body and
      // the server would silently clear every column. We learned this the
      // hard way with /settings wiping itself on save.
      // Visual lock comes from the .is-submitting class; the submit button is
      // disabled here to prevent double-submission.
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.dataset.originalText = button.textContent;
        if (button.getAttribute("data-loading-icon-only") === "true") {
          button.setAttribute("aria-label", button.getAttribute("data-loading-text") || button.dataset.originalText || "Working...");
          button.classList.add("icon-only-loading");
          button.textContent = "";
        } else {
          button.textContent = button.getAttribute("data-loading-text") || "Working...";
        }
        button.disabled = true;
      }
      form.classList.add("is-submitting");
    });
    form.addEventListener("htmx:afterRequest", () => {
      const button = form.querySelector("button[type='submit']");
      if (button && button.dataset.originalText !== undefined) {
        button.textContent = button.dataset.originalText;
        button.classList.remove("icon-only-loading");
        button.removeAttribute("aria-label");
        button.disabled = false;
        delete button.dataset.originalText;
      }
      form.classList.remove("is-submitting");
    });
  });
}

// ---- File-input labels ----------------------------------------------------

function bindFileLabels(root) {
  bindOnce(root, "input[type='file'][data-file-label]", "file", (input) => {
    const label = document.querySelector(input.getAttribute("data-file-label"));
    const summary = document.querySelector(input.getAttribute("data-file-summary"));
    const max = parseInt(input.getAttribute("data-max-files") || "0", 10);
    input.addEventListener("change", () => {
      if (max > 0 && input.files.length > max) {
        window.alert(tr("upload_too_many", "一次最多上傳 {max} 個檔案，已保留前 {max} 個。", { max }));
        const transfer = new DataTransfer();
        for (let i = 0; i < max; i += 1) transfer.items.add(input.files[i]);
        input.files = transfer.files;
      }
      if (!label) return;
      const count = input.files.length;
      if (!count) label.textContent = tr("upload_hint", "拖曳最多 {max} 個檔案到此，或點擊選擇", { max: max || 5 });
      else if (count === 1) label.textContent = input.files[0].name;
      else label.textContent = tr("upload_selected", "已選擇 {count} 個檔案", { count });
      if (summary) renderFileSummary(summary, input.files, max);
    });
  });
}

function renderFileSummary(summary, files, max) {
  if (!files.length) {
    summary.innerHTML = "";
    return;
  }
  const total = [...files].reduce((sum, file) => sum + file.size, 0);
  const list = [...files].map((file) =>
    `<li><span>${escapeHtml(file.name)}</span><span>${formatBytes(file.size)}</span></li>`
  ).join("");
  const summaryText = tr(
    "upload_summary",
    "{count} / {max} 個檔案 · {size} · 送出後會排入索引",
    { count: files.length, max: max || files.length, size: formatBytes(total) },
  );
  summary.innerHTML = `<p>${escapeHtml(summaryText)}</p><ul>${list}</ul>`;
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

// ---- Source scope: checkbox-per-item in the left panel + localStorage ----
// Selection is stored as the SET OF EXCLUDED source IDs so newly indexed
// sources default to checked without any explicit save.

function getNotebookId() {
  const form = document.querySelector("form.ask-form");
  if (!form) return null;
  const m = (form.getAttribute("action") || "").match(/\/notebooks\/(\d+)\//);
  return m ? m[1] : null;
}

function getScopeExcluded(notebookId) {
  try {
    const raw = localStorage.getItem(`scope-excluded-${notebookId}`);
    return new Set(JSON.parse(raw || "[]").map(String));
  } catch { return new Set(); }
}

function saveScopeExcluded(notebookId, excluded) {
  localStorage.setItem(`scope-excluded-${notebookId}`, JSON.stringify([...excluded]));
}

function restoreSourceScopeState() {
  const notebookId = getNotebookId();
  if (!notebookId) return;
  const excluded = getScopeExcluded(notebookId);
  document.querySelectorAll(".source-scope-toggle").forEach((cb) => {
    if (excluded.has(String(cb.dataset.sourceId))) cb.checked = false;
  });
}

function initSourceScope() {
  const notebookId = getNotebookId();
  if (!notebookId) return;

  restoreSourceScopeState();

  // Checkbox toggles — event-delegated so newly-swapped items are covered.
  document.addEventListener("change", (e) => {
    const cb = e.target.closest(".source-scope-toggle");
    if (!cb) return;
    const excluded = getScopeExcluded(notebookId);
    if (cb.checked) excluded.delete(String(cb.dataset.sourceId));
    else excluded.add(String(cb.dataset.sourceId));
    saveScopeExcluded(notebookId, excluded);
  });

  // All / None buttons in the Sources pane header.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-scope-action]");
    if (!btn) return;
    const selectAll = btn.dataset.scopeAction === "select-all";
    const checkboxes = [...document.querySelectorAll(".source-scope-toggle")];
    checkboxes.forEach((cb) => { cb.checked = selectAll; });
    saveScopeExcluded(
      notebookId,
      selectAll ? new Set() : new Set(checkboxes.map((cb) => String(cb.dataset.sourceId))),
    );
  });

  // Inject source_ids hidden inputs into the ask form just before submit so
  // the server receives only the checked sources. Capture phase runs before
  // data-loading-form's submit lock.
  const askForm = document.querySelector("form.ask-form");
  if (!askForm) return;
  askForm.addEventListener("submit", () => {
    askForm.querySelectorAll("input[name='source_ids'][type='hidden']").forEach((el) => el.remove());
    document.querySelectorAll(".source-scope-toggle:checked").forEach((cb) => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "source_ids";
      input.value = cb.dataset.sourceId;
      askForm.appendChild(input);
    });
  }, true);
}

// ---- Settings provider note ----------------------------------------------

function bindProviderNotes() {
  const provider = document.querySelector("[data-provider-select]");
  const providerNote = document.querySelector("[data-provider-note]");
  if (!provider || !providerNote) return;
  if (provider.dataset.providerBound) return;
  provider.dataset.providerBound = "1";
  const notes = {
    openai_compatible: tr("provider_hint_openai", "請填入相容 /v1 的 base URL；模型欄位填模型名稱。"),
    azure_openai: tr("provider_hint_azure", "請填入 Azure 資源端點；模型欄位填部署（deployment）名稱。"),
  };
  const updateNote = () => {
    providerNote.textContent = notes[provider.value] || notes.openai_compatible;
  };
  provider.addEventListener("change", updateNote);
  updateNote();
}

// ---- Markdown rendering + inline citation links --------------------------

function renderMarkdown(root) {
  if (typeof window.marked === "undefined" || typeof window.DOMPurify === "undefined") {
    console.warn("[notebook] marked or DOMPurify missing; skipping Markdown render");
    return;
  }
  marked.setOptions({ breaks: true, gfm: true });
  root.querySelectorAll("[data-markdown]:not([data-markdown-rendered])").forEach((node) => {
    node.dataset.markdownRendered = "1";
    const raw = node.textContent || "";
    node.dataset.rawMarkdown = raw; // kept for the copy button (U7)
    let html = "";
    try {
      html = DOMPurify.sanitize(marked.parse(raw));
    } catch (e) {
      console.error("[notebook] markdown render failed", e);
      return;
    }
    node.innerHTML = html;
    let citations = [];
    try { citations = JSON.parse(node.dataset.citations || "[]"); } catch (e) { citations = []; }
    if (citations.length) replaceCitationTokens(node, citations);
  });
}

function replaceCitationTokens(root, citations) {
  const map = {};
  citations.forEach((c) => { map[c.index] = c; });
  const messageId = (root.closest("[id^='msg-']") || {}).id || "";
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const targets = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.parentElement && node.parentElement.tagName === "A") continue;
    if (/\[\d+\]/.test(node.nodeValue)) targets.push(node);
  }
  targets.forEach((textNode) => {
    const fragment = document.createDocumentFragment();
    const parts = textNode.nodeValue.split(/(\[\d+\])/);
    parts.forEach((part) => {
      const m = part.match(/^\[(\d+)\]$/);
      if (m && map[Number(m[1])]) {
        const cite = map[Number(m[1])];
        const a = document.createElement("a");
        a.className = "cite-link";
        a.href = "#";
        a.textContent = `[${cite.index}]`;
        a.dataset.citationIndex = String(cite.index);
        if (cite.source_id != null) a.dataset.sourceId = String(cite.source_id);
        if (cite.chunk_id != null) a.dataset.chunkId = String(cite.chunk_id);
        if (cite.filename) a.dataset.filename = cite.filename;
        if (messageId) a.dataset.messageId = messageId;
        a.title = `${cite.filename || ""} · ${cite.location || ""}`;
        a.addEventListener("click", onCitationClick);
        fragment.appendChild(a);
      } else if (part) {
        fragment.appendChild(document.createTextNode(part));
      }
    });
    textNode.parentNode.replaceChild(fragment, textNode);
  });
}

function flashElement(el, block) {
  el.scrollIntoView({ behavior: "smooth", block: block || "center" });
  el.classList.add("source-flash");
  setTimeout(() => el.classList.remove("source-flash"), 1600);
}

function notebookIdFromPath() {
  const m = location.pathname.match(/\/notebooks\/(\d+)/);
  return m ? m[1] : null;
}

// U3: open the source preview drawer and highlight the cited chunk. Loads the
// preview fragment via HTMX, opens the modal, then scrolls to + flashes
// #preview-chunk-{id}. Returns true if it could start (source + chunk known).
function openSourcePreviewAtChunk(sourceId, chunkId) {
  const nbId = notebookIdFromPath();
  if (!nbId || !sourceId || !chunkId || !window.htmx) return false;
  const url = `/notebooks/${nbId}/sources/${sourceId}/preview`;
  window.htmx
    .ajax("GET", url, { target: "#preview-content", swap: "innerHTML" })
    .then(() => {
      window.dispatchEvent(new CustomEvent("open-preview"));
      // Let Alpine reveal the modal (x-show) before scrolling into it.
      setTimeout(() => {
        const chunkEl = document.getElementById(`preview-chunk-${chunkId}`);
        if (chunkEl) {
          // Persistent accent highlight (#preview-content is reloaded fresh on
          // each open, so no stale target lingers) + a one-shot arrival pulse.
          chunkEl.classList.add("cite-target");
          flashElement(chunkEl, "center");
        }
      }, 60);
    })
    .catch(() => {});
  return true;
}

function onCitationClick(event) {
  event.preventDefault();
  const link = event.currentTarget;
  let sourceId = link.dataset.sourceId;
  const chunkId = link.dataset.chunkId;
  const messageId = link.dataset.messageId;
  const index = link.dataset.citationIndex;
  const filename = link.dataset.filename;

  // Old citations stored before source_id was added: fall back to filename match.
  if (!sourceId && filename) {
    const candidates = document.querySelectorAll(".source-item");
    for (const item of candidates) {
      const nameEl = item.querySelector(".source-name");
      if (nameEl && nameEl.textContent.trim() === filename) {
        sourceId = item.id.replace(/^source-/, "");
        break;
      }
    }
  }

  // Always flash the matching left-pane source row as a locator.
  if (sourceId) {
    const sourceEl = document.getElementById(`source-${sourceId}`);
    if (sourceEl) flashElement(sourceEl, "center");
  }

  // U3: open the preview drawer at the exact cited chunk. For older citations
  // without a chunk_id, fall back to expanding the inline snippet under the answer.
  const openedDrawer = openSourcePreviewAtChunk(sourceId, chunkId);
  if (!openedDrawer && messageId && index) {
    const details = document.getElementById(`${messageId}-cite-${index}`);
    if (details) {
      details.open = true;
      flashElement(details, "nearest");
    }
  }
}

// ---- Auto-scroll to latest message on page load --------------------------
// After asking a question (or clicking a suggestion), the form POSTs and the
// server 303-redirects back to the notebook page. Without this, the new
// answer lands at the bottom and the viewport stays at the top — the user
// has to hunt for it. Scroll the latest message into view instead.

function scrollToLatestMessage() {
  const messages = document.querySelectorAll(".messages .message");
  if (!messages.length) return;
  const last = messages[messages.length - 1];
  // Use 'start' so the user sees the answer header (role + pin) and can
  // read top-down, with citations naturally below.
  last.scrollIntoView({ behavior: "instant", block: "start" });
}

// ---- Suggestion chip -> fill question textarea + auto-submit -------------
// NotebookLM-style: clicking a starter question immediately asks it.

function bindSuggestionFill(root) {
  bindOnce(root, "[data-fill-question]", "fill", (button) => {
    button.addEventListener("click", () => {
      const textarea = document.getElementById("question-input");
      if (!textarea) return;
      const text = button.dataset.text || button.textContent.trim();
      textarea.value = text;
      textarea.disabled = false;
      const form = textarea.closest("form.ask-form");
      if (!form) {
        textarea.focus();
        return;
      }
      // Trigger native submit so existing data-loading-form binding fires.
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    });
  });
}
