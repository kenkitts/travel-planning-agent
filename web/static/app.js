// Travel Planning Agent — web UI client.
//
// Talks to the local backend (server.py) over /api/chat and /api/config.
// Session persistence: the AgentCore runtimeSessionId is generated once
// per browser (per actor_id) and stored in localStorage, so reloading the
// page continues the same conversation. "New conversation" clears it and
// starts a fresh session, matching how cli/chat.py starts a fresh session
// each time it's run.

const SESSION_STORAGE_KEY = "travel-agent-session-id";
const HISTORY_STORAGE_KEY = "travel-agent-history";
// AgentCore Runtime requires runtimeSessionId to be 33-256 characters.
const MIN_SESSION_ID_LENGTH = 33;

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const newConversationBtn = document.getElementById("new-conversation-btn");

let actorId = "web-user";
let sessionId = null;

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

function saveHistory(history) {
  localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(history));
}

function loadHistory() {
  const raw = localStorage.getItem(HISTORY_STORAGE_KEY);
  if (!raw) return [];
  try {
    return JSON.parse(raw);
  } catch {
    return [];
  }
}

let history = [];

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

function appendMessage(role, text, { persist = true } = {}) {
  const el = document.createElement("div");
  el.className = `message ${role}`;
  el.innerHTML = role === "agent" ? renderMarkdown(text) : escapeHtml(text);
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  if (persist) {
    history.push({ role, text });
    saveHistory(history);
  }
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

function renderHistory() {
  messagesEl.innerHTML = "";
  for (const { role, text } of history) {
    appendMessage(role, text, { persist: false });
  }
}

// --- Chat submission ---------------------------------------------------

async function sendMessage(prompt) {
  appendMessage("user", prompt);
  const pendingEl = appendPending();
  sendBtn.disabled = true;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, session_id: sessionId }),
    });
    const data = await res.json();
    pendingEl.remove();

    if (!res.ok) {
      appendMessage("error", data.detail || "The agent returned an error.");
      return;
    }
    appendMessage("agent", data.response);
  } catch (err) {
    pendingEl.remove();
    appendMessage("error", `Could not reach the agent: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

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
  history = [];
  saveHistory(history);
  messagesEl.innerHTML = "";
  inputEl.focus();
});

// --- Init ----------------------------------------------------------------

async function init() {
  try {
    const res = await fetch("/api/config");
    const config = await res.json();
    actorId = config.actor_id || actorId;
  } catch {
    // Fall back to the default actorId if /api/config is unreachable;
    // sendMessage will surface the real connectivity error on first send.
  }
  sessionId = loadOrCreateSession();
  history = loadHistory();
  renderHistory();
}

init();
