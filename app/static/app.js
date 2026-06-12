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
        window.alert(`Only the first ${max} files will be uploaded.`);
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
  bindLoadingForms(root);
  bindFileLabels(root);
  bindProviderNotes();
  renderMarkdown(root);
  bindSuggestionFill(root);
  bindAskFormThinkingBubble(root);
  bindChatInput(root);
  bindCopyButtons(root);
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
        button.textContent = "✓ 已複製";
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
      bubble.innerHTML =
        "<span>思考中</span>" +
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
  bindOnce(root, "[data-confirm]", "confirm", (form) => {
    form.addEventListener("submit", (event) => {
      const message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) event.preventDefault();
    });
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
        button.textContent = button.getAttribute("data-loading-text") || "Working...";
        button.disabled = true;
      }
      form.classList.add("is-submitting");
    });
  });
}

// ---- File-input labels ----------------------------------------------------

function bindFileLabels(root) {
  bindOnce(root, "input[type='file'][data-file-label]", "file", (input) => {
    const label = document.querySelector(input.getAttribute("data-file-label"));
    const max = parseInt(input.getAttribute("data-max-files") || "0", 10);
    input.addEventListener("change", () => {
      if (max > 0 && input.files.length > max) {
        window.alert(`Please select at most ${max} files.`);
        const transfer = new DataTransfer();
        for (let i = 0; i < max; i += 1) transfer.items.add(input.files[i]);
        input.files = transfer.files;
      }
      if (!label) return;
      const count = input.files.length;
      if (!count) label.textContent = "No file selected";
      else if (count === 1) label.textContent = input.files[0].name;
      else label.textContent = `${count} files selected`;
    });
  });
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
    openai_compatible: "請填入相容 /v1 的 base URL；模型欄位填模型名稱。",
    azure_openai: "請填入 Azure 資源端點；模型欄位填部署（deployment）名稱。",
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

function onCitationClick(event) {
  event.preventDefault();
  const link = event.currentTarget;
  let sourceId = link.dataset.sourceId;
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

  if (sourceId) {
    const sourceEl = document.getElementById(`source-${sourceId}`);
    if (sourceEl) {
      sourceEl.scrollIntoView({ behavior: "smooth", block: "center" });
      sourceEl.classList.add("source-flash");
      setTimeout(() => sourceEl.classList.remove("source-flash"), 1600);
    }
  }
  if (messageId && index) {
    const details = document.getElementById(`${messageId}-cite-${index}`);
    if (details) {
      details.open = true;
      details.scrollIntoView({ behavior: "smooth", block: "nearest" });
      details.classList.add("source-flash");
      setTimeout(() => details.classList.remove("source-flash"), 1600);
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
