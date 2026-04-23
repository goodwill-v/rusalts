const $ = (sel, root = document) => root.querySelector(sel);

function esc(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function errText(e) {
  if (!e) return "Неизвестная ошибка";
  if (typeof e === "string") return e;
  if (e instanceof Error) return e.message || String(e);
  if (typeof e?.message === "string") return e.message;
  try {
    return JSON.stringify(e);
  } catch {
    return String(e);
  }
}

function addMsg(log, who, text) {
  const div = document.createElement("div");
  div.className = `msg ${who === "me" ? "msg--me" : "msg--app"}`;
  div.textContent = String(text || "");
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function addFileMsg(log, { who, text, file }) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${who === "me" ? "msg--me" : "msg--app"}`;
  const parts = [];
  if (text) parts.push(`<div>${esc(text)}</div>`);
  if (file?.url) {
    const name = esc(file.orig || file.name || "file");
    const type = String(file.type || "");
    if (type.startsWith("image/")) {
      parts.push(`<div style="margin-top:8px"><img src="${esc(file.url)}" alt="${name}" style="max-width:100%;border-radius:12px" /></div>`);
    } else if (type.startsWith("video/")) {
      parts.push(
        `<div style="margin-top:8px"><video src="${esc(file.url)}" controls playsinline style="max-width:100%;border-radius:12px"></video></div>`
      );
    }
    parts.push(`<div style="margin-top:8px"><a href="${esc(file.url)}" download style="color:inherit;text-decoration:underline">${name}</a></div>`);
  }
  wrap.innerHTML = parts.join("");
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
}

function getKey() {
  return localStorage.getItem("talk_key") || "";
}

function setKey(k) {
  localStorage.setItem("talk_key", String(k || ""));
}

async function fetchJson(url, opts = {}) {
  const key = getKey();
  const headers = new Headers(opts.headers || {});
  if (key) headers.set("Authorization", `Bearer ${key}`);
  const res = await fetch(url, { credentials: "same-origin", ...opts, headers });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    let msg = (data && (data.detail || data.error)) || text || res.statusText;
    if (typeof msg !== "string") {
      try {
        msg = JSON.stringify(msg);
      } catch {
        msg = String(msg);
      }
    }
    throw new Error(msg);
  }
  return data;
}

async function pingKey() {
  try {
    await fetchJson("/api/talk/ping");
    return true;
  } catch {
    return false;
  }
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(220, ta.scrollHeight) + "px";
}

async function sendText({ text, file }) {
  const targetEl = $("[data-talk-target]");
  const target = Number(targetEl?.value || 1) || 1;
  if (file) {
    const fd = new FormData();
    fd.append("target", String(target));
    fd.append("text", text || "");
    fd.append("file", file);
    return fetchJson("/api/talk/relay-file", { method: "POST", body: fd });
  }
  return fetchJson("/api/talk/relay", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, text }),
  });
}

async function main() {
  const log = $("[data-talk-log]");
  const form = $("[data-talk-form]");
  const input = $("[data-talk-input]");
  const fileInput = $("[data-talk-file]");
  const toast = $("[data-toast]");
  const targetSel = $("[data-talk-target]");
  const gate = $("[data-keygate]");
  const gateInput = $("[data-keygate-input]");
  const gateBtn = $("[data-keygate-btn]");
  const gateErr = $("[data-keygate-err]");
  let lastInboxId = localStorage.getItem("talk_last_inbox_id") || "";

  const showToast = (t) => {
    if (!toast) return;
    toast.textContent = String(t || "");
    toast.classList.add("toast--show");
    setTimeout(() => toast.classList.remove("toast--show"), 2500);
  };

  const showGate = (show) => {
    gate.classList.toggle("keygate--show", Boolean(show));
    if (show) setTimeout(() => gateInput?.focus?.(), 0);
  };

  const ensureKey = async () => {
    const ok = await pingKey();
    showGate(!ok);
    if (!ok) {
      if (gateErr) gateErr.textContent = "";
      if (gateInput) gateInput.value = "";
    }
  };

  gateBtn?.addEventListener("click", async () => {
    const k = (gateInput?.value || "").trim();
    if (!k) {
      if (gateErr) gateErr.textContent = "Введите ключ.";
      return;
    }
    setKey(k);
    const ok = await pingKey();
    if (!ok) {
      if (gateErr) gateErr.textContent = "Ключ не подходит.";
      setKey("");
      return;
    }
    showGate(false);
  });

  // target selector: если URLs меньше 3 — прячем лишние
  if (targetSel) {
    // держим селектор максимально простым; если не настроено 3 адреса — сервер всё равно вернёт ошибку,
    // но в UI не будем мешать: пользователь сам выберет 1/2/3 по настройке.
    targetSel.value = localStorage.getItem("talk_target") || "1";
    targetSel.addEventListener("change", () => localStorage.setItem("talk_target", targetSel.value));
  }

  input?.addEventListener("input", () => autosize(input));

  form?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = (input?.value || "").trim();
    const file = fileInput?.files?.[0] || null;
    if (!text && !file) return;
    if (input) input.value = "";
    if (fileInput) fileInput.value = "";
    autosize(input);

    if (file) addMsg(log, "me", `${text || ""}\n[Вложение: ${file.name}]`.trim());
    else addMsg(log, "me", text);
    const btn = form.querySelector("button[type='submit']");
    if (btn) btn.disabled = true;
    try {
      const res = await sendText({ text, file });
      const d = res?.data ?? {};
      const reply = d?.reply ?? d?.text ?? d?.message ?? "";
      const f = d?.file || null;
      if (reply || f) addFileMsg(log, { who: "app", text: reply, file: f });
      else addMsg(log, "app", `Ответ получен: ${esc(JSON.stringify(d ?? {}))}`);
    } catch (e) {
      const m = errText(e);
      addMsg(log, "app", `Ошибка: ${m}`);
      if (m.includes("Неверный ключ") || m.includes("401")) {
        setKey("");
        showGate(true);
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  const pollInbox = async () => {
    try {
      const res = await fetchJson(`/api/talk/inbox?after=${encodeURIComponent(lastInboxId || "")}`);
      const events = Array.isArray(res?.events) ? res.events : [];
      if (events.length) {
        for (const ev of events) {
          const t = String(ev?.text || "");
          const f = ev?.file || null;
          addFileMsg(log, { who: "app", text: t, file: f });
          if (f) showToast("Получен файл от приложения");
          else showToast("Получено сообщение от приложения");
          lastInboxId = String(ev?.id || lastInboxId);
        }
        localStorage.setItem("talk_last_inbox_id", lastInboxId);
      }
    } catch (e) {
      // если ключ слетел — снова попросим
      const m = errText(e);
      if (m.includes("Неверный ключ") || m.includes("401")) {
        setKey("");
        showGate(true);
      }
    }
  };

  await ensureKey();
  setInterval(pollInbox, 2000);
}

main();

