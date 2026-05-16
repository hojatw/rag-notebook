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
      const files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) return;
      this.$refs.input.files = files;
      this.$refs.input.dispatchEvent(new Event("change", { bubbles: true }));
    },
  }));
});

document.addEventListener("DOMContentLoaded", () => {
  bindAll(document);
  scrollToLatestMessage();
});

document.addEventListener("htmx:afterSwap", (event) => {
  bindAll(event.target);
});

function bindAll(root) {
  bindConfirms(root);
  bindLoadingForms(root);
  bindFileLabels(root);
  bindSourcePicker();
  bindProviderNotes();
  renderMarkdown(root);
  bindSuggestionFill(root);
}

// ---- Confirm dialogs ------------------------------------------------------

function bindConfirms(root) {
  root.querySelectorAll("[data-confirm]:not([data-confirm-bound])").forEach((form) => {
    form.dataset.confirmBound = "1";
    form.addEventListener("submit", (event) => {
      const message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) event.preventDefault();
    });
  });
}

// ---- Submit-button loading state -----------------------------------------

function bindLoadingForms(root) {
  root.querySelectorAll("form[data-loading-form]:not([data-loading-bound])").forEach((form) => {
    form.dataset.loadingBound = "1";
    form.addEventListener("submit", () => {
      // Disable the submit button AND every input/textarea/select so the user
      // cannot keep typing or change selections while the request is in flight.
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.dataset.originalText = button.textContent;
        button.textContent = button.getAttribute("data-loading-text") || "Working...";
        button.disabled = true;
      }
      form.querySelectorAll("input, textarea, select").forEach((el) => {
        if (el.type === "hidden") return;
        el.dataset.wasDisabled = el.disabled ? "1" : "0";
        el.disabled = true;
      });
    });
  });
}

// ---- File-input labels ----------------------------------------------------

function bindFileLabels(root) {
  root.querySelectorAll("input[type='file'][data-file-label]:not([data-file-bound])").forEach((input) => {
    input.dataset.fileBound = "1";
    const label = document.querySelector(input.getAttribute("data-file-label"));
    input.addEventListener("change", () => {
      if (!label) return;
      label.textContent = input.files.length ? input.files[0].name : "No file selected";
    });
  });
}

// ---- Source-picker All/None buttons --------------------------------------

function bindSourcePicker() {
  const picker = document.querySelector("[data-source-picker]");
  if (!picker) return;
  const boxes = () => Array.from(picker.querySelectorAll("input[type='checkbox']"));
  document.querySelectorAll("[data-source-action]:not([data-picker-bound])").forEach((button) => {
    button.dataset.pickerBound = "1";
    button.addEventListener("click", () => {
      const checked = button.getAttribute("data-source-action") === "select";
      boxes().forEach((box) => { box.checked = checked; });
    });
  });
}

// ---- Settings provider note ----------------------------------------------

function bindProviderNotes() {
  const provider = document.querySelector("[data-provider-select]");
  const providerNote = document.querySelector("[data-provider-note]");
  if (!provider || !providerNote) return;
  if (provider.dataset.providerBound) return;
  provider.dataset.providerBound = "1";
  const notes = {
    openai_compatible: "Use a /v1 compatible base URL. Model fields should be model names.",
    azure_openai: "Use the Azure resource endpoint. Model fields should be deployment names.",
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
  root.querySelectorAll("[data-fill-question]:not([data-fill-bound])").forEach((button) => {
    button.dataset.fillBound = "1";
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
