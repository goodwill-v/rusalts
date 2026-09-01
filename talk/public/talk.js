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

const PROVIDER_MODEL_ERR = "Модель недоступна на стороне провайдера";

function isProviderModelError(text) {
  const s = String(text || "").toLowerCase();
  if (!s) return false;
  const hints = [
    "incomplete terminal response",
    "incomplete_result",
    "failovererror",
    "model not found",
    "does not exist",
    "invalid model",
    "no such model",
    "model_unavailable",
    "model is not available",
    "unknown model",
    "unsupported model",
    "all models failed",
    "all configured models failed",
  ];
  return hints.some((h) => s.includes(h));
}

function formatTalkError(raw) {
  const text = errText(raw);
  if (/504\s+gateway\s+time-?out/i.test(text) || text.includes("Gateway Time-out")) {
    return "Сервер оборвал ожидание ответа (таймаут nginx). Нажмите «Новая», затем отправьте сообщение снова (таймаут увеличен до 180 с).";
  }
  if (!isProviderModelError(text)) return text;
  if (/incomplete/i.test(text)) {
    return `${PROVIDER_MODEL_ERR}. Ответ оборвался — нажмите «Новая» и отправьте запрос снова (короче, если был очень длинным).`;
  }
  const short = text.length > 220 ? `${text.slice(0, 220)}…` : text;
  return `${PROVIDER_MODEL_ERR}. ${short}`;
}

function getTalkModel() {
  return localStorage.getItem("talk_model") || "";
}

function setTalkModel(id) {
  localStorage.setItem("talk_model", String(id || ""));
}

/** @type {{ ts: string, role: string, text: string, file?: object | null }[]} */
let chatHistory = [];

function addMsg(log, who, text, opts = {}) {
  const role = who === "me" ? "user" : who === "system" ? "system" : "assistant";
  const body = String(text || "");
  if (!opts.skipHistory && body) {
    chatHistory.push({ ts: new Date().toISOString(), role, text: body, file: null });
  }
  const div = document.createElement("div");
  div.className =
    who === "system" ? "msg msg--system" : `msg ${who === "me" ? "msg--me" : "msg--app"}`;
  div.textContent = body;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function addFileMsg(log, { who, text, file }, opts = {}) {
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
  if (!opts.skipHistory) {
    const role = who === "me" ? "user" : "assistant";
    chatHistory.push({
      ts: new Date().toISOString(),
      role,
      text: String(text || ""),
      file: file || null,
    });
  }
}

function clearChatLog(log) {
  if (log) log.innerHTML = "";
  chatHistory = [];
}

function exportChatTranscript() {
  if (!chatHistory.length) return null;
  const lines = [`# TALK — экспорт`, `Дата: ${new Date().toLocaleString("ru-RU")}`, ""];
  for (const row of chatHistory) {
    const label = row.role === "user" ? "Вы" : row.role === "system" ? "Система" : "Алик";
    lines.push(`## ${label} (${row.ts})`);
    if (row.text) lines.push(row.text);
    if (row.file?.orig || row.file?.name) {
      lines.push(`[файл: ${row.file.orig || row.file.name}]`);
    }
    lines.push("");
  }
  return lines.join("\n").trim();
}

function downloadTextFile(filename, content, mime = "text/plain;charset=utf-8") {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function getKey() {
  return localStorage.getItem("talk_key") || "";
}

function setKey(k) {
  localStorage.setItem("talk_key", String(k || ""));
}

function getOkoAdminKey() {
  return localStorage.getItem("talk_oko_admin") || "";
}

function setOkoAdminKey(k) {
  localStorage.setItem("talk_oko_admin", String(k || ""));
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

/** Запросы к /api/talk с опциональным X-Oko-Admin (кнопки управления Gateway). */
async function fetchTalkApi(url, opts = {}) {
  const key = getKey();
  const headers = new Headers(opts.headers || {});
  if (key) headers.set("Authorization", `Bearer ${key}`);
  if (opts.withAdmin) {
    const adm = getOkoAdminKey();
    if (adm) headers.set("X-Oko-Admin", adm);
  }
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

async function ensureOkoAdminForWrite() {
  let a = getOkoAdminKey();
  if (a) return a;
  a = (typeof window !== "undefined" && window.prompt("Админ-ключ ОКО (TALK_OKO_ADMIN_KEY):")) || "";
  a = String(a).trim();
  if (!a) throw new Error("Админ-ключ не задан — операция отменена.");
  setOkoAdminKey(a);
  return a;
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

async function sendText({ text, file, model }) {
  const modelId = String(model || getTalkModel() || "").trim();
  if (file) {
    const fd = new FormData();
    fd.append("text", text || "");
    fd.append("file", file);
    if (modelId) fd.append("model", modelId);
    return fetchJson("/api/talk/relay-file", { method: "POST", body: fd });
  }
  const body = { text };
  if (modelId) body.model = modelId;
  return fetchJson("/api/talk/relay", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function main() {
  const log = $("[data-talk-log]");
  const form = $("[data-talk-form]");
  const input = $("[data-talk-input]");
  const fileInput = $("[data-talk-file]");
  const toast = $("[data-toast]");
  const gate = $("[data-keygate]");
  const gateInput = $("[data-keygate-input]");
  const gateBtn = $("[data-keygate-btn]");
  const gateErr = $("[data-keygate-err]");
  const toolbar = $("[data-talk-toolbar]");
  const btnOkoStatus = $("[data-oko-status]");
  const btnOkoStop = $("[data-oko-stop]");
  const btnOkoStart = $("[data-oko-start]");
  const modelSelect = $("[data-talk-model]");
  const btnNewSession = $("[data-talk-new-session]");
  const btnExport = $("[data-talk-export]");
  const btnSend = $("[data-talk-send]");
  const btnVoice = $("[data-talk-voice]");
  const attachHint = $("[data-talk-attach-hint]");
  let lastInboxId = localStorage.getItem("talk_last_inbox_id") || "";
  let voiceRecognition = null;
  let voiceActive = false;
  let voiceStopping = false;
  let voiceMicStream = null;
  let voiceFinalText = "";

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

  const loadModels = async () => {
    if (!modelSelect) return;
    try {
      const j = await fetchTalkApi("/api/talk/models");
      const items = Array.isArray(j?.models) ? j.models : [];
      const def = String(j?.default || "").trim();
      modelSelect.innerHTML = "";
      if (!items.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "Нет моделей";
        modelSelect.appendChild(opt);
        return;
      }
      for (const it of items) {
        const id = String(it?.id || it?.label || "").trim();
        if (!id) continue;
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = String(it?.label || id);
        modelSelect.appendChild(opt);
      }
      const saved = getTalkModel();
      const pick = saved && items.some((it) => String(it?.id || "") === saved) ? saved : def || items[0]?.id || "";
      if (pick) {
        modelSelect.value = pick;
        setTalkModel(pick);
      }
    } catch (e) {
      modelSelect.innerHTML = "";
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "Модели: ошибка";
      modelSelect.appendChild(opt);
      console.warn("talk models:", errText(e));
    }
  };

  modelSelect?.addEventListener("change", () => {
    setTalkModel(modelSelect.value || "");
    showToast(`Модель: ${modelSelect.value || "—"}`);
  });

  const refreshOkoStatus = async () => {
    if (!btnOkoStatus) return;
    try {
      const j = await fetchTalkApi("/api/talk/oko/status");
      const st = String(j?.active || j?.data?.active || "unknown");
      btnOkoStatus.title = `Gateway: ${st}`;
      btnOkoStatus.dataset.gatewayState = st;
    } catch (e) {
      btnOkoStatus.title = `Gateway: ? (${errText(e).slice(0, 80)})`;
      delete btnOkoStatus.dataset.gatewayState;
    }
  };

  const ensureKey = async () => {
    const ok = await pingKey();
    showGate(!ok);
    if (!ok) {
      if (gateErr) gateErr.textContent = "";
      if (gateInput) gateInput.value = "";
      if (toolbar) toolbar.hidden = true;
    } else {
      if (toolbar) toolbar.hidden = false;
      await Promise.all([refreshOkoStatus(), loadModels()]);
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
    if (toolbar) toolbar.hidden = false;
    await Promise.all([refreshOkoStatus(), loadModels()]);
  });

  btnOkoStatus?.addEventListener("click", async () => {
    try {
      await refreshOkoStatus();
      showToast(btnOkoStatus?.title || "OK");
    } catch (e) {
      showToast(formatTalkError(e));
    }
  });

  btnOkoStop?.addEventListener("click", async () => {
    try {
      await ensureOkoAdminForWrite();
      const j = await fetchTalkApi("/api/talk/oko/stop", { method: "POST", withAdmin: true });
      showToast(j?.message || "ОКО остановлен");
      await refreshOkoStatus();
    } catch (e) {
      showToast(formatTalkError(e));
    }
  });

  btnOkoStart?.addEventListener("click", async () => {
    try {
      await ensureOkoAdminForWrite();
      const j = await fetchTalkApi("/api/talk/oko/start", { method: "POST", withAdmin: true });
      showToast(j?.message || "ОКО запущен");
      await refreshOkoStatus();
    } catch (e) {
      showToast(formatTalkError(e));
    }
  });

  input?.addEventListener("input", () => autosize(input));

  input?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      form?.requestSubmit?.();
    }
  });

  fileInput?.addEventListener("change", () => {
    const f = fileInput.files?.[0];
    if (!attachHint) return;
    if (!f) {
      attachHint.hidden = true;
      attachHint.textContent = "";
      return;
    }
    attachHint.hidden = false;
    attachHint.textContent = `Файл: ${f.name} (${Math.round(f.size / 1024)} КБ)`;
  });

  const setBusy = (busy) => {
    if (btnSend) btnSend.disabled = busy;
    if (btnNewSession) btnNewSession.disabled = busy;
    if (btnExport) btnExport.disabled = busy;
    if (input) input.disabled = busy;
    if (fileInput) fileInput.disabled = busy;
  };

  const submitMessage = async () => {
    const text = (input?.value || "").trim();
    const file = fileInput?.files?.[0] || null;
    if (!text && !file) return;
    if (input) input.value = "";
    if (fileInput) fileInput.value = "";
    if (attachHint) {
      attachHint.hidden = true;
      attachHint.textContent = "";
    }
    autosize(input);

    if (file) addMsg(log, "me", `${text || ""}\n[Вложение: ${file.name}]`.trim());
    else addMsg(log, "me", text);
    setBusy(true);
    try {
      const res = await sendText({ text, file });
      const d = res?.data ?? res ?? {};
      const reply = d?.reply ?? d?.text ?? d?.message ?? "";
      const f = d?.file || null;
      if (reply || f) addFileMsg(log, { who: "app", text: reply, file: f });
      else addMsg(log, "app", `Ответ получен: ${JSON.stringify(d ?? {})}`);
    } catch (e) {
      const m = formatTalkError(e);
      addMsg(log, "app", `Ошибка: ${m}`);
      if (m.includes("Неверный ключ") || m.includes("401")) {
        setKey("");
        showGate(true);
      }
    } finally {
      setBusy(false);
    }
  };

  form?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    await submitMessage();
  });

  btnNewSession?.addEventListener("click", async () => {
    const ok =
      typeof globalThis.confirm === "function"
        ? globalThis.confirm(
            "Начать новую сессию? История чата на экране и контекст OpenClaw (talk-relay) будут сброшены."
          )
        : true;
    if (!ok) return;
    setBusy(true);
    try {
      const j = await fetchTalkApi("/api/talk/session/reset", { method: "POST" });
      clearChatLog(log);
      addMsg(
        log,
        "system",
        `Новая сессия OpenClaw (${j?.sessionId || "talk-relay"}). Контекст сброшен.`
      );
      showToast("Новая сессия");
    } catch (e) {
      showToast(formatTalkError(e));
    } finally {
      setBusy(false);
    }
  });

  btnExport?.addEventListener("click", () => {
    const body = exportChatTranscript();
    if (!body) {
      showToast("Нечего экспортировать");
      return;
    }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    downloadTextFile(`talk-export-${stamp}.md`, body, "text/markdown;charset=utf-8");
    showToast("Экспорт сохранён");
  });

  const setVoiceHint = (text, show = true) => {
    if (!attachHint) return;
    if (!show || !text) {
      attachHint.hidden = true;
      if (!fileInput?.files?.[0]) attachHint.textContent = "";
      return;
    }
    attachHint.hidden = false;
    attachHint.textContent = text;
  };

  const voiceErrorHint = (code) => {
    const map = {
      "not-allowed": "Доступ к микрофону запрещён. Chrome → замок у адреса → Микрофон → Разрешить.",
      "service-not-allowed": "Распознавание речи отключено в настройках браузера.",
      "network": "Chrome отправляет звук на серверы Google. Проверьте интернет/VPN (в РФ часто блокируется).",
      "no-speech": "Речь не услышана. Говорите сразу после нажатия, ближе к микрофону.",
      "audio-capture": "Микрофон не найден или занят другим приложением.",
      aborted: "Запись остановлена.",
    };
    return map[code] || `Ошибка распознавания: ${code || "неизвестно"}`;
  };

  const releaseMicStream = () => {
    if (!voiceMicStream) return;
    voiceMicStream.getTracks().forEach((t) => t.stop());
    voiceMicStream = null;
  };

  const resetVoiceUi = () => {
    if (btnVoice) {
      btnVoice.classList.remove("talk__action--active");
      btnVoice.setAttribute("aria-label", "Голосовой ввод");
    }
  };

  const stopVoiceEngine = () => {
    if (voiceRecognition) {
      try {
        voiceRecognition.stop();
      } catch {
        /* ignore */
      }
      voiceRecognition = null;
    }
  };

  const finishVoice = async (opts = {}) => {
    const { send = false, keepHint = false } = opts;
    voiceActive = false;
    voiceStopping = false;
    stopVoiceEngine();
    releaseMicStream();
    resetVoiceUi();
    const spoken = (input?.value || voiceFinalText || "").trim();
    if (input && spoken) input.value = spoken;
    autosize(input);
    if (!keepHint) setVoiceHint("");
    if (send && spoken) {
      showToast("Отправка…");
      await submitMessage();
      return;
    }
    if (send && !spoken) {
      const msg =
        "Текст не распознан. Проверьте микрофон в системе, говорите громче или попробуйте VPN (Chrome → Google).";
      setVoiceHint(msg);
      showToast(msg);
    }
  };

  async function ensureMicPermission() {
    if (!navigator.mediaDevices?.getUserMedia) return true;
    try {
      releaseMicStream();
      voiceMicStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      return true;
    } catch (e) {
      const name = e?.name || "";
      if (name === "NotAllowedError" || name === "PermissionDeniedError") {
        showToast("Микрофон: доступ запрещён. Разрешите в настройках сайта (замок слева от URL).");
      } else if (name === "NotFoundError") {
        showToast("Микрофон не найден на устройстве.");
      } else {
        showToast(`Микрофон: ${errText(e)}`);
      }
      return false;
    }
  }

  const bindVoiceHandlers = (rec) => {
    rec.onstart = () => {
      setVoiceHint("🎤 Слушаю… говорите, затем нажмите «Остановить»");
    };
    rec.onsoundstart = () => {
      setVoiceHint("🎤 Звук слышен — продолжайте говорить…");
    };
    rec.onspeechstart = () => {
      setVoiceHint("🎤 Речь распознаётся…");
    };
    rec.onresult = (ev) => {
      let interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const t = ev.results[i][0]?.transcript || "";
        if (ev.results[i].isFinal) voiceFinalText += t;
        else interim += t;
      }
      const line = (voiceFinalText + interim).trim();
      if (input) input.value = line;
      autosize(input);
      if (line) setVoiceHint(`🎤 ${line.slice(0, 100)}${line.length > 100 ? "…" : ""}`);
    };
    rec.onerror = (ev) => {
      const code = ev.error || "";
      if (code === "no-speech" && voiceActive && !voiceStopping) {
        setVoiceHint("🎤 Тишина… говорите или нажмите «Остановить»");
        return;
      }
      if (code === "aborted" && voiceStopping) return;
      const msg = voiceErrorHint(code);
      setVoiceHint(msg);
      showToast(msg);
      finishVoice({ send: false, keepHint: true });
    };
    rec.onend = async () => {
      if (voiceActive && !voiceStopping) {
        try {
          rec.start();
        } catch {
          const SR = globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition;
          if (!SR) return;
          voiceRecognition = new SR();
          voiceRecognition.lang = rec.lang;
          voiceRecognition.interimResults = true;
          voiceRecognition.continuous = true;
          voiceRecognition.maxAlternatives = 1;
          bindVoiceHandlers(voiceRecognition);
          try {
            voiceRecognition.start();
          } catch (e) {
            showToast(errText(e));
            await finishVoice({ send: false });
          }
        }
        return;
      }
      await finishVoice({ send: voiceStopping });
    };
  };

  const startVoice = async () => {
    const SR = globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition;
    if (!SR) {
      showToast("Голосовой ввод не поддерживается в этом браузере");
      return;
    }
    if (voiceActive) {
      voiceStopping = true;
      stopVoiceEngine();
      return;
    }

    const micOk = await ensureMicPermission();
    if (!micOk) return;

    voiceFinalText = "";
    if (input) input.value = "";
    voiceStopping = false;
    voiceRecognition = new SR();
    voiceRecognition.lang = "ru-RU";
    voiceRecognition.interimResults = true;
    voiceRecognition.continuous = true;
    voiceRecognition.maxAlternatives = 1;
    bindVoiceHandlers(voiceRecognition);

    voiceActive = true;
    if (btnVoice) {
      btnVoice.classList.add("talk__action--active");
      btnVoice.setAttribute("aria-label", "Остановить запись");
    }
    showToast("Слушаю… нажмите «Остановить» когда закончите фразу");
    try {
      voiceRecognition.start();
    } catch (e) {
      await finishVoice({ send: false });
      showToast(errText(e));
    }
  };

  btnVoice?.addEventListener("click", () => {
    startVoice();
  });

  const pollInbox = async () => {
    try {
      const res = await fetchJson(`/api/talk/inbox?after=${encodeURIComponent(lastInboxId || "")}`);
      const events = Array.isArray(res?.events) ? res.events : [];
      if (events.length) {
        for (const ev of events) {
          const t = String(ev?.text || "");
          const f = ev?.file || null;
          addFileMsg(log, { who: "app", text: t ? `[Приложение] ${t}` : "", file: f });
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
  setInterval(() => {
    if (toolbar && !toolbar.hidden) refreshOkoStatus();
  }, 20000);
}

main();

