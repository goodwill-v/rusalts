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
  // лёгкая проверка: пробуем сделать пустой запрос с текстом,
  // сервер ответит 400 (пустой текст) но ключ будет проверен раньше.
  try {
    await fetchJson("/api/talk/relay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: 1, text: "" }),
    });
    return true;
  } catch (e) {
    const m = errText(e);
    // если это именно "Пустой текст" — значит ключ валиден
    return m.includes("Пустой текст");
  }
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(220, ta.scrollHeight) + "px";
}

async function sendText({ text, file }) {
  if (file) {
    const fd = new FormData();
    fd.append("target", "1");
    fd.append("text", text || "");
    fd.append("file", file);
    return fetchJson("/api/talk/relay-file", { method: "POST", body: fd });
  }
  return fetchJson("/api/talk/relay", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target: 1, text }),
  });
}

async function main() {
  const log = $("[data-talk-log]");
  const form = $("[data-talk-form]");
  const input = $("[data-talk-input]");
  const fileInput = $("[data-talk-file]");
  const gate = $("[data-keygate]");
  const gateInput = $("[data-keygate-input]");
  const gateBtn = $("[data-keygate-btn]");
  const gateErr = $("[data-keygate-err]");

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

  input?.addEventListener("input", () => autosize(input));

  form?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = (input?.value || "").trim();
    const file = fileInput?.files?.[0] || null;
    if (!text && !file) return;
    if (input) input.value = "";
    if (fileInput) fileInput.value = "";
    autosize(input);

    addMsg(log, "me", file ? `${text || ""}\n[Вложение: ${file.name}]`.trim() : text);
    const btn = form.querySelector("button[type='submit']");
    if (btn) btn.disabled = true;
    try {
      const res = await sendText({ text, file });
      const reply = res?.data?.reply ?? res?.data?.text ?? res?.data?.message ?? "";
      if (reply) addMsg(log, "app", reply);
      else addMsg(log, "app", `Ответ получен: ${esc(JSON.stringify(res?.data ?? {}))}`);
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

  await ensureKey();
}

main();

