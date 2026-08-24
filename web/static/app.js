// Travel Planning Agent — web UI client.
//
// Talks to the local backend (server.py) over /api/chat, /api/config, and
// /api/conversations[/…]. Session persistence: the AgentCore
// runtimeSessionId is generated once per browser (per actor_id) and stored
// in localStorage, so reloading the page continues the same conversation.
// "New conversation" clears it and starts a fresh session, matching how
// cli/chat.py starts a fresh session each time it's run.
//
// Conversation history (the sidebar) is not stored locally — AgentCore
// Memory is the source of truth. Switching conversations fetches that
// session's transcript from the server (GET /api/conversations/{id}),
// which reads it straight out of Memory via ListEvents.

const SESSION_STORAGE_KEY = "travel-agent-session-id";
const DIAGNOSTIC_STORAGE_KEY = "travel-agent-diagnostics-enabled";
// AgentCore Runtime requires runtimeSessionId to be 33-256 characters.
const MIN_SESSION_ID_LENGTH = 33;

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const newConversationBtn = document.getElementById("new-conversation-btn");
const sidebarEl = document.getElementById("sidebar");
const conversationListEl = document.getElementById("conversation-list");
const sidebarToggleBtn = document.getElementById("sidebar-toggle-btn");
const sidebarCloseBtn = document.getElementById("sidebar-close-btn");
const diagnosticToggleInput = document.getElementById("diagnostic-toggle-input");

let actorId = "web-user";
let sessionId = null;
let historyEnabled = false;
let diagnosticsEnabled = false;

function randomHex(length) {
  const bytes = new Uint8Array(Math.ceil(length / 2));
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, length);
}

function buildSessionId(actor) {
  let id = `${actor}___${randomHex(32)}`;
  if (id.length < MIN_SESSION_ID_LENGTH) {
    id = id.padEnd(MIN_SESSION_ID_LENGTH, "0");
  }
  return id;
}

function loadOrCreateSession() {
  const stored = localStorage.getItem(SESSION_STORAGE_KEY);
  if (stored && stored.startsWith(`${actorId}___`)) {
    return stored;
  }
  const fresh = buildSessionId(actorId);
  localStorage.setItem(SESSION_STORAGE_KEY, fresh);
  return fresh;
}

// --- Minimal markdown renderer -------------------------------------------
// Covers the subset the agent's system prompt actually produces: headings
// (#/##/###), bold/italic, unordered/ordered lists, paragraphs. Escapes
// HTML first since the agent's output is untrusted text being inserted
// into the DOM.

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function renderInline(text) {
  let out = escapeHtml(text);
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, "<em>$1</em>");
  return out;
}

function renderMarkdown(markdown) {
  const lines = markdown.split("\n");
  const htmlParts = [];
  let listBuffer = [];
  let listTag = null;

  function flushList() {
    if (listBuffer.length > 0 && listTag) {
      htmlParts.push(
        `<${listTag}>${listBuffer.map((item) => `<li>${renderInline(item)}</li>`).join("")}</${listTag}>`
      );
    }
    listBuffer = [];
    listTag = null;
  }

  let paragraphBuffer = [];
  function flushParagraph() {
    if (paragraphBuffer.length > 0) {
      htmlParts.push(`<p>${renderInline(paragraphBuffer.join(" "))}</p>`);
      paragraphBuffer = [];
    }
  }

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const headingMatch = line.match(/^(#{1,3})\s+(.*)$/);
    const unorderedMatch = line.match(/^[-*]\s+(.*)$/);
    const orderedMatch = line.match(/^\d+\.\s+(.*)$/);

    if (headingMatch) {
      flushParagraph();
      flushList();
      const level = headingMatch[1].length;
      htmlParts.push(`<h${level}>${renderInline(headingMatch[2])}</h${level}>`);
    } else if (unorderedMatch) {
      flushParagraph();
      if (listTag !== "ul") {
        flushList();
        listTag = "ul";
      }
      listBuffer.push(unorderedMatch[1]);
    } else if (orderedMatch) {
      flushParagraph();
      if (listTag !== "ol") {
        flushList();
        listTag = "ol";
      }
      listBuffer.push(orderedMatch[1]);
    } else if (line.trim() === "") {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraphBuffer.push(line);
    }
  }
  flushParagraph();
  flushList();

  return htmlParts.join("\n");
}

// --- Rendering -------------------------------------------------------------

function appendMessage(role, text) {
  const el = document.createElement("div");
  el.className = `message ${role}`;
  el.innerHTML = role === "agent" ? renderMarkdown(text) : escapeHtml(text);
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return el;
}

function appendPending() {
  const el = document.createElement("div");
  el.className = "message pending";
  el.textContent = "Thinking…";
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return el;
}

function renderTranscript(turns) {
  messagesEl.innerHTML = "";
  for (const { role, text } of turns) {
    // The backend's transcript roles are "user"/"assistant" (from
    // AgentCore Memory); the chat bubble CSS classes are "user"/"agent".
    appendMessage(role === "assistant" ? "agent" : role, text);
  }
}

// --- Diagnostic panel ------------------------------------------------------
// Only rendered when diagnosticsEnabled is true. One <details> entry per
// non-text event, appended live as the stream progresses. Full raw
// payloads are shown verbatim (this is a diagnostics feature — see
// stream_agent_turn() in agent/agent.py for why nothing is summarized away).

function badgeLabel(type) {
  return type.replace(/_/g, " ");
}

function prettyPrintIfJson(text) {
  // Tool results (weather/places/web-search) often return JSON serialized
  // as a single-line text string — pretty-print it for readability in the
  // diagnostic panel. Falls back to the raw text unchanged if it isn't
  // valid JSON (e.g. plain prose from a tool), since this is a display-only
  // convenience — the underlying data streamed from the backend is never
  // altered.
  const trimmed = text.trim();
  if (!trimmed) return text;
  const looksLikeJson = trimmed.startsWith("{") || trimmed.startsWith("[");
  if (!looksLikeJson) return text;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return text;
  }
}

function formatDiagnosticBody(type, data) {
  if (type === "reasoning") {
    return typeof data === "string" ? data : JSON.stringify(data, null, 2);
  }
  if (type === "tool_use") {
    return `${data.name || "(unknown tool)"}\n\n${JSON.stringify(data.input, null, 2)}`;
  }
  if (type === "tool_result") {
    const status = data.status ? `[${data.status}]\n\n` : "";
    return `${status}${prettyPrintIfJson(data.text || "")}`;
  }
  if (type === "error") {
    if (data && typeof data === "object") {
      return data.note || JSON.stringify(data, null, 2);
    }
    return String(data);
  }
  return typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

function summaryLabel(type, data) {
  if (type === "tool_use") return `tool_use: ${data.name || "unknown"}`;
  if (type === "tool_result") {
    const len = (data.text || "").length;
    return `tool_result: ${len} chars`;
  }
  if (type === "reasoning") {
    const text = typeof data === "string" ? data : "";
    return `reasoning: ${text.length} chars`;
  }
  if (type === "error") return "error";
  return type;
}

function appendDiagnosticEntry(panelEl, type, data) {
  const details = document.createElement("details");
  details.className = "diagnostic-entry";

  const summary = document.createElement("summary");
  const badge = document.createElement("span");
  badge.className = `diagnostic-badge ${type}`;
  badge.textContent = badgeLabel(type);
  summary.appendChild(badge);
  const summaryText = document.createTextNode(summaryLabel(type, data));
  summary.appendChild(summaryText);
  details.appendChild(summary);

  const pre = document.createElement("pre");
  pre.textContent = formatDiagnosticBody(type, data);
  details.appendChild(pre);

  panelEl.appendChild(details);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return { details, summaryText, pre };
}

function updateDiagnosticEntry(entry, type, data) {
  entry.summaryText.textContent = summaryLabel(type, data);
  entry.pre.textContent = formatDiagnosticBody(type, data);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// --- Chat submission (streaming) -------------------------------------------

async function sendMessage(prompt) {
  appendMessage("user", prompt);
  const pendingEl = appendPending();
  sendBtn.disabled = true;

  let agentEl = null;
  let diagnosticPanelEl = null;
  let accumulatedText = "";
  let reasoningEntry = null;
  let accumulatedReasoning = "";

  function ensureAgentBubble() {
    if (!agentEl) {
      pendingEl.remove();
      agentEl = appendMessage("agent", "");
      if (diagnosticsEnabled) {
        diagnosticPanelEl = document.createElement("div");
        diagnosticPanelEl.className = "diagnostic-panel";
        messagesEl.appendChild(diagnosticPanelEl);
      }
    }
  }

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, session_id: sessionId }),
    });

    if (!res.ok || !res.body) {
      pendingEl.remove();
      const data = await res.json().catch(() => ({}));
      appendMessage("error", data.detail || "The agent returned an error.");
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are "data: <json>\n\n" — split on blank-line boundaries.
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (!frame.startsWith("data: ")) continue;

        let event;
        try {
          event = JSON.parse(frame.slice(6));
        } catch {
          continue; // Malformed frame — skip rather than break the whole stream.
        }

        if (event.type === "text") {
          ensureAgentBubble();
          accumulatedText += event.data;
          agentEl.innerHTML = renderMarkdown(accumulatedText);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (event.type === "done") {
          ensureAgentBubble();
          // "done" carries the guaranteed-complete final text — prefer it
          // over the accumulated deltas in case any were missed.
          const finalText = event.data || accumulatedText;
          agentEl.innerHTML = renderMarkdown(finalText);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (event.type === "error") {
          ensureAgentBubble();
          if (diagnosticsEnabled && diagnosticPanelEl) {
            appendDiagnosticEntry(diagnosticPanelEl, "error", event.data);
          }
          const note =
            event.data && typeof event.data === "object" ? event.data.note : event.data;
          if (note && !accumulatedText) {
            appendMessage("error", note);
          } else if (note) {
            agentEl.innerHTML += `<p><em>${escapeHtml(note)}</em></p>`;
          }
        } else if (event.type === "reasoning" && diagnosticsEnabled && diagnosticPanelEl) {
          // Strands streams reasoning as many small text deltas (like
          // "text" events). Accumulate into one running entry instead of
          // creating a new diagnostic block per token.
          ensureAgentBubble();
          accumulatedReasoning += event.data;
          if (!reasoningEntry) {
            reasoningEntry = appendDiagnosticEntry(
              diagnosticPanelEl,
              "reasoning",
              accumulatedReasoning
            );
          } else {
            updateDiagnosticEntry(reasoningEntry, "reasoning", accumulatedReasoning);
          }
        } else if (diagnosticsEnabled && diagnosticPanelEl) {
          // tool_use / tool_result — each already arrives once (deduped by
          // toolUseId upstream), so one entry per event is correct here.
          ensureAgentBubble();
          appendDiagnosticEntry(diagnosticPanelEl, event.type, event.data);
        } else {
          // Diagnostics off: still need the bubble to exist so reasoning/
          // tool events preceding the first text delta don't leave the
          // "Thinking…" placeholder up with nothing to replace it.
          ensureAgentBubble();
        }
      }
    }

    pendingEl.remove();
    refreshConversationList();
  } catch (err) {
    pendingEl.remove();
    appendMessage("error", `Could not reach the agent: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

// --- Conversation sidebar ------------------------------------------------

function setSidebarOpen(open) {
  sidebarEl.classList.toggle("open", open);
}

function renderConversationList(conversations) {
  conversationListEl.innerHTML = "";
  for (const conv of conversations) {
    const li = document.createElement("li");
    li.className = "conversation-item";
    if (conv.session_id === sessionId) {
      li.classList.add("active");
    }
    li.dataset.sessionId = conv.session_id;

    const textWrap = document.createElement("div");
    textWrap.className = "conversation-text";

    const label = document.createElement("div");
    label.className = "conversation-preview";
    // A user-set title takes priority; otherwise fall back to the
    // auto-generated preview from the first message.
    label.textContent = conv.title || conv.preview;
    textWrap.appendChild(label);

    if (conv.created_at) {
      const date = document.createElement("div");
      date.className = "conversation-date";
      date.textContent = new Date(conv.created_at).toLocaleString();
      textWrap.appendChild(date);
    }

    textWrap.addEventListener("click", () => switchConversation(conv.session_id));
    li.appendChild(textWrap);

    const renameBtn = document.createElement("button");
    renameBtn.className = "icon-btn conversation-rename-btn";
    renameBtn.title = "Rename conversation";
    renameBtn.setAttribute("aria-label", "Rename conversation");
    renameBtn.textContent = "✎";
    renameBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      startRenaming(li, conv);
    });
    li.appendChild(renameBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "icon-btn conversation-delete-btn";
    deleteBtn.title = "Delete conversation";
    deleteBtn.setAttribute("aria-label", "Delete conversation");
    deleteBtn.textContent = "🗑";
    deleteBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      confirmAndDelete(conv);
    });
    li.appendChild(deleteBtn);

    conversationListEl.appendChild(li);
  }
}

async function confirmAndDelete(conv) {
  const label = conv.title || conv.preview;
  const confirmed = window.confirm(
    `Delete "${label}"? This permanently removes it from AgentCore Memory and cannot be undone.`
  );
  if (!confirmed) return;

  try {
    const res = await fetch(`/api/conversations/${encodeURIComponent(conv.session_id)}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      appendMessage("error", data.detail || "Could not delete that conversation.");
      return;
    }

    // If the deleted conversation was the active one, there's nothing left
    // to show — start a fresh conversation rather than leaving a stale
    // transcript on screen for a session that no longer exists.
    if (conv.session_id === sessionId) {
      sessionId = buildSessionId(actorId);
      localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
      messagesEl.innerHTML = "";
    }
  } catch (err) {
    appendMessage("error", `Could not delete that conversation: ${err.message}`);
  } finally {
    refreshConversationList();
  }
}

function startRenaming(li, conv) {
  const textWrap = li.querySelector(".conversation-text");
  const currentValue = conv.title || "";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "conversation-rename-input";
  input.value = currentValue;
  input.maxLength = 80;

  textWrap.replaceChildren(input);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (commit) => {
    if (settled) return;
    settled = true;
    const newTitle = input.value.trim();
    if (commit && newTitle && newTitle !== currentValue) {
      await renameConversation(conv.session_id, newTitle);
    } else {
      refreshConversationList();
    }
  };

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));
}

async function renameConversation(targetSessionId, title) {
  try {
    const res = await fetch(
      `/api/conversations/${encodeURIComponent(targetSessionId)}/title`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      }
    );
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      appendMessage("error", data.detail || "Could not rename that conversation.");
    }
  } catch (err) {
    appendMessage("error", `Could not rename that conversation: ${err.message}`);
  } finally {
    refreshConversationList();
  }
}

async function refreshConversationList() {
  if (!historyEnabled) return;
  try {
    const res = await fetch("/api/conversations");
    if (!res.ok) return;
    const conversations = await res.json();
    renderConversationList(conversations);
  } catch {
    // Sidebar is a convenience; a failed refresh just leaves the last
    // known list in place rather than surfacing an error to the user.
  }
}

async function switchConversation(targetSessionId) {
  if (targetSessionId === sessionId) {
    setSidebarOpen(false);
    return;
  }

  sendBtn.disabled = true;
  try {
    const res = await fetch(`/api/conversations/${encodeURIComponent(targetSessionId)}`);
    const data = await res.json();
    if (!res.ok) {
      appendMessage("error", data.detail || "Could not load that conversation.");
      return;
    }
    sessionId = targetSessionId;
    localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    renderTranscript(data.turns);
    await refreshConversationList();
    setSidebarOpen(false);
  } catch (err) {
    appendMessage("error", `Could not load that conversation: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

// --- Event listeners -------------------------------------------------------

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const prompt = inputEl.value.trim();
  if (!prompt) return;
  inputEl.value = "";
  sendMessage(prompt);
});

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    formEl.requestSubmit();
  }
});

newConversationBtn.addEventListener("click", () => {
  sessionId = buildSessionId(actorId);
  localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  messagesEl.innerHTML = "";
  inputEl.focus();
  refreshConversationList();
});

sidebarToggleBtn.addEventListener("click", () => setSidebarOpen(true));
sidebarCloseBtn.addEventListener("click", () => setSidebarOpen(false));

diagnosticToggleInput.addEventListener("change", () => {
  diagnosticsEnabled = diagnosticToggleInput.checked;
  localStorage.setItem(DIAGNOSTIC_STORAGE_KEY, diagnosticsEnabled ? "1" : "0");
});

// --- Init ----------------------------------------------------------------

async function init() {
  // Off by default — this is a diagnostics feature (raw reasoning/tool
  // payloads), not something a normal chat session should show unasked.
  diagnosticsEnabled = localStorage.getItem(DIAGNOSTIC_STORAGE_KEY) === "1";
  diagnosticToggleInput.checked = diagnosticsEnabled;

  try {
    const res = await fetch("/api/config");
    const config = await res.json();
    actorId = config.actor_id || actorId;
    historyEnabled = Boolean(config.history_enabled);
  } catch {
    // Fall back to the default actorId if /api/config is unreachable;
    // sendMessage will surface the real connectivity error on first send.
  }
  sessionId = loadOrCreateSession();

  if (historyEnabled) {
    // Try to load this session's existing transcript from AgentCore
    // Memory (e.g. after a page reload); an empty/new session just
    // renders no messages, which is correct for a fresh conversation.
    try {
      const res = await fetch(`/api/conversations/${encodeURIComponent(sessionId)}`);
      if (res.ok) {
        const data = await res.json();
        renderTranscript(data.turns);
      }
    } catch {
      // Ignore — an empty chat window is a safe fallback.
    }
    refreshConversationList();
  } else {
    sidebarToggleBtn.hidden = true;
  }
}

init();
