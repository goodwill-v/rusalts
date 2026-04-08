/**
 * Прототип UI: чат и шаблоны. API остаётся стабильным для подключения LLM и авторизации.
 */

const $ = (sel, root = document) => root.querySelector(sel);

function appendMessage(log, role, text) {
  if (!log) return;
  const div = document.createElement("div");
  div.className = `msg ${role === "user" ? "user" : "bot"}`;
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = role === "user" ? "Вы" : "Ответ";
  div.appendChild(label);
  div.appendChild(document.createTextNode(text));
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function loadTemplates() {
  const listEl = $("#template-list");
  const emptyEl = $("#templates-empty");
  if (!listEl) return;
  try {
    const res = await fetch("/api/document-templates", { credentials: "same-origin" });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    const items = data.items || [];
    listEl.innerHTML = "";
    if (items.length === 0) {
      if (emptyEl) emptyEl.hidden = false;
      return;
    }
    if (emptyEl) emptyEl.hidden = true;
    for (const it of items) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = it.url;
      a.textContent = it.name || it.filename;
      a.setAttribute("download", "");
      li.appendChild(a);
      const meta = document.createElement("span");
      meta.className = "muted";
      meta.style.marginLeft = "0.35rem";
      meta.style.fontSize = "0.8rem";
      meta.textContent = ` (${formatSize(it.size)})`;
      li.appendChild(meta);
      listEl.appendChild(li);
    }
  } catch (e) {
    console.error(e);
    if (emptyEl) {
      emptyEl.hidden = false;
      emptyEl.textContent = "Не удалось загрузить список шаблонов.";
    }
  }
}

function formatSize(n) {
  if (n < 1024) return `${n} Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} КБ`;
  return `${(n / (1024 * 1024)).toFixed(1)} МБ`;
}

async function sendChat(message) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || res.statusText);
  }
  return res.json();
}

async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/uploads", {
    method: "POST",
    body: fd,
    credentials: "same-origin",
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || res.statusText);
  }
  return res.json();
}

function initChatForm({ form, input, log, fileInput, hint }) {
  if (!form || !input || !log) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = (input.value || "").trim();
    if (!text) return;
    const btn = form.querySelector('button[type="submit"]');
    appendMessage(log, "user", text);
    input.value = "";
    if (btn) btn.disabled = true;
    try {
      if (fileInput?.files?.length) {
        const f = fileInput.files[0];
        const up = await uploadFile(f);
        if (hint) {
          hint.hidden = false;
          hint.textContent = `Файл «${up.filename}» принят (${formatSize(up.size)}).`;
        }
        fileInput.value = "";
      } else if (hint) {
        hint.hidden = true;
      }
      const data = await sendChat(text);
      appendMessage(log, "bot", data.reply || "");
    } catch (err) {
      appendMessage(log, "bot", `Ошибка: ${err?.message || err}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  });
}

function initConsultantUi() {
  const form = $("#chat-form");
  const input = $("#question");
  const log = $("#chat-log");
  const fileInput = $("#attachment");
  const hint = $("#upload-hint");
  if (!form || !input || !log) return;
  loadTemplates();
  initChatForm({ form, input, log, fileInput, hint });
}

function initA11yToggle() {
  const btn = $("#a11y-toggle");
  if (!btn) return;
  const KEY = "alt_a11y";
  const apply = (on) => {
    document.documentElement.classList.toggle("a11y", Boolean(on));
    try {
      localStorage.setItem(KEY, on ? "1" : "0");
    } catch {}
  };
  let initial = false;
  try {
    initial = localStorage.getItem(KEY) === "1";
  } catch {}
  apply(initial);
  btn.addEventListener("click", () => apply(!document.documentElement.classList.contains("a11y")));
}

function initNavToggle() {
  const toggle = $(".nav-toggle");
  const nav = $("#site-nav");
  if (!toggle || !nav) return;
  toggle.addEventListener("click", () => {
    const open = !nav.classList.contains("is-open");
    nav.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

function initNewsFilter() {
  const tabs = document.querySelectorAll("[data-news-filter]");
  const grid = document.querySelector("[data-news-grid]");
  if (!tabs.length || !grid) return;
  const items = Array.from(grid.querySelectorAll("[data-news-cat]"));
  const setActive = (btn) => {
    tabs.forEach((t) => t.classList.toggle("is-active", t === btn));
    tabs.forEach((t) => t.setAttribute("aria-selected", t === btn ? "true" : "false"));
  };
  const apply = (cat) => {
    items.forEach((it) => {
      const ok = cat === "all" || it.getAttribute("data-news-cat") === cat;
      it.style.display = ok ? "" : "none";
    });
  };
  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const cat = btn.getAttribute("data-news-filter") || "all";
      setActive(btn);
      apply(cat);
    });
  });
}

function initAltExpertWidget() {
  const root = document.querySelector("[data-altbot]");
  if (!root) return;

  const panel = root.querySelector(".altbot__panel");
  const log = root.querySelector("[data-chat-log]");
  const form = root.querySelector("[data-chat-form]");
  const input = root.querySelector("input");
  const openers = document.querySelectorAll("[data-open-chat]");
  const closer = root.querySelector("[data-close-chat]");
  const fab = root.querySelector(".altbot__fab");

  const show = () => {
    root.hidden = false;
    if (panel) panel.style.display = "";
    if (input) input.focus();
  };
  const hide = () => {
    if (panel) panel.style.display = "none";
  };

  // init hidden: keep FAB visible even if panel hidden
  root.hidden = false;
  hide();

  openers.forEach((b) => b.addEventListener("click", show));
  fab?.addEventListener("click", () => (panel?.style.display === "none" ? show() : hide()));
  closer?.addEventListener("click", hide);

  if (log) appendMessage(log, "bot", "Здравствуйте! Чем могу помочь: миграция, правовые вопросы, ИИ‑инструменты или ваш вопрос.");
  initChatForm({ form, input, log });
}

function initVkBridge() {
  // window.__APP__.vkAppId — для будущей интеграции VK Bridge / VKWebApp (https://dev.vk.com/ru)
}

initConsultantUi();
initA11yToggle();
initNavToggle();
initNewsFilter();
initAltExpertWidget();
initVkBridge();
