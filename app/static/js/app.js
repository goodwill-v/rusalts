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

async function fetchJson(url, opts = {}) {
  const res = await fetch(url, { credentials: "same-origin", ...opts });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || text || res.statusText;
    throw new Error(msg);
  }
  return data;
}

function stripMd(md) {
  const t = String(md || "").replace(/\r/g, "");
  const lines = t.split("\n").map((l) => l.trim()).filter(Boolean);
  const first = lines.find((l) => !l.startsWith("#") && !l.startsWith("- "));
  return (first || lines[0] || "").replace(/[*_`>#-]/g, "").trim();
}

function fmtDate(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const yy = d.getFullYear();
  return `${dd}.${mm}.${yy}`;
}

async function initHomeNews() {
  const list = document.querySelector("[data-home-news-list]");
  if (!list) return;
  try {
    const idx = await fetchJson("/api/content/site/index");
    const items = Array.isArray(idx.items) ? idx.items.slice(0, 6) : [];
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = '<li class="news-item"><p class="muted-site">Пока нет новостей.</p></li>';
      return;
    }
    items.forEach((it) => {
      const li = document.createElement("li");
      li.className = "news-item";
      const dt = fmtDate(it.published_at_utc);
      const pubId = String(it.publication_id || "").padStart(5, "0");
      const announce = stripMd(it.title || "");
      const href = "/news/#" + String(it.publication_id || "");
      li.innerHTML = `
        <div class="news-item__meta">
          <time datetime="${String(it.published_at_utc || "")}">${dt || ""} (${escapeHtml(pubId)})</time>
          ${it.pinned ? '<span class="news-item__pin" title="Закреплено" aria-label="Закреплено">📎</span>' : ""}
        </div>
        <p class="news-item__announce">${escapeHtml(announce || "Новость")}</p>
        <a class="news-item__more" href="${href}">Открыть</a>
      `;
      list.appendChild(li);
    });
  } catch (e) {
    list.innerHTML = `<li class="news-item"><p class="muted-site">Не удалось загрузить новости: ${escapeHtml(e?.message || e)}</p></li>`;
  }
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function hostLabel(url) {
  try {
    const u = new URL(String(url));
    return String(u.hostname || "").replace(/^www\./i, "") || String(url);
  } catch {
    return String(url || "");
  }
}

async function initNewsPage() {
  const root = document.querySelector("[data-news-page]");
  const grid = document.querySelector("[data-news-grid]");
  const loading = document.querySelector("[data-news-loading]");
  if (!root || !grid) return;
  try {
    const idx = await fetchJson("/api/content/site/index");
    const items = Array.isArray(idx.items) ? idx.items : [];
    if (loading) loading.remove();
    grid.innerHTML = "";
    if (!items.length) {
      grid.innerHTML = '<p class="muted-site">Пока нет новостей.</p>';
      return;
    }
    items.forEach((it) => {
      const card = document.createElement("article");
      card.className = "news-card";
      const pubId = String(it.publication_id || "");
      card.id = pubId;
      const dt = fmtDate(it.published_at_utc);
      const pubFmt = String(pubId || "").padStart(5, "0");
      const sources = Array.isArray(it.sources) ? it.sources : [];
      const src = sources.find((u) => String(u || "").trim()) || "";
      const srcLabel = src ? hostLabel(src) : "";
      card.innerHTML = `
        <div class="news-meta">
          <span class="news-meta__left">${it.pinned ? "📎 Закреплено" : ""}</span>
          <time datetime="${String(it.published_at_utc || "")}">${dt} (${escapeHtml(pubFmt)})</time>
        </div>
        <h2 class="news-title">${escapeHtml(String(it.title || "Новость"))}</h2>
        <p class="news-announce" data-news-announce hidden></p>
        <div class="news-text" data-news-body>Загрузка…</div>
        ${
          src
            ? `<div class="news-sources">
                 <div class="news-sources__label">Источник:</div>
                 <a class="news-source-link" href="${escapeHtml(src)}" target="_blank" rel="noopener noreferrer">${escapeHtml(srcLabel)}</a>
               </div>`
            : ""
        }
      `;
      grid.appendChild(card);
      // load body markdown as plain text (MVP)
      const bodyEl = card.querySelector("[data-news-body]");
      const annEl = card.querySelector("[data-news-announce]");
      fetch(it.url || "")
        .then((r) => r.text())
        .then((t) => {
          const raw = String(t || "").trim();
          const clean = raw.replace(/\s+/g, " ").trim();
          if (annEl) {
            const ann = clean.slice(0, 200).trim();
            annEl.textContent = ann ? `Анонс: ${ann}` : "";
            annEl.hidden = !ann;
          }
          if (bodyEl) {
            const shown = raw.slice(0, 1000);
            bodyEl.textContent = raw.length > 1000 ? shown + "…" : shown;
          }
        })
        .catch(() => {
          if (bodyEl) bodyEl.textContent = "Не удалось загрузить текст.";
        });
    });
  } catch (e) {
    grid.innerHTML = `<p class="muted-site">Не удалось загрузить новости: ${escapeHtml(e?.message || e)}</p>`;
  }
}

function buildApprovalsItem(it) {
  const wrap = document.createElement("div");
  wrap.className = "approv-item";
  wrap.dataset.pubId = String(it.publication_id || "");
  wrap.dataset.pinned = it.pinned ? "1" : "0";
  wrap.innerHTML = `
    <div class="approv-item__head">
      <div class="approv-item__meta">
        <div class="approv-item__id">ID: ${escapeHtml(String(it.publication_id || ""))} ${it.pinned ? "📎" : ""}</div>
        <div class="approv-item__time">${escapeHtml(fmtDate(it.created_at_utc) || "")}</div>
      </div>
      <div class="approv-item__flags">
        <label class="approv-flag"><input type="checkbox" data-flag-approve> Одобрить</label>
        <label class="approv-flag"><input type="checkbox" data-flag-pin ${it.pinned ? "checked" : ""}> Закрепить</label>
        <label class="approv-flag"><input type="checkbox" data-flag-cancel> Отмена</label>
      </div>
    </div>

    <label class="approv-label">Заголовок</label>
    <input class="approv-input" type="text" data-edit-title value="${escapeHtml(String(it.title || ""))}">

    <div class="approv-grid">
      <div class="approv-col">
        <div class="approv-col__title">Сайт</div>
        <textarea class="approv-textarea" rows="10" data-edit-site>${escapeHtml(String(it.site_text || ""))}</textarea>
      </div>
      <div class="approv-col">
        <div class="approv-col__title">ВКонтакте</div>
        <textarea class="approv-textarea" rows="10" data-edit-vk>${escapeHtml(String(it.vk_text || ""))}</textarea>
      </div>
    </div>

    <div class="approv-actions">
      <button class="btn-site btn-site--primary" type="button" data-action-save>Сохранить</button>
      <button class="btn-site" type="button" data-action-publish>Опубликовать</button>
      <span class="muted-site" data-action-status></span>
    </div>
  `;
  const err = String(it.last_publish_error || "").trim();
  if (err) {
    const st = document.createElement("div");
    st.className = "muted-site";
    st.style.marginTop = "0.35rem";
    st.textContent = `Ошибка публикации: ${err}`;
    wrap.appendChild(st);
  }
  return wrap;
}

function parseCorpSources(raw) {
  return String(raw || "")
    .split(/\n+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function clearCorpForm(root) {
  const setv = (sel, v) => {
    const el = root.querySelector(sel);
    if (el) el.value = v;
  };
  setv("[data-corp-title]", "");
  setv("[data-corp-site]", "");
  setv("[data-corp-vk]", "");
  setv("[data-corp-sources]", "");
  setv("[data-corp-note]", "");
  const pin = root.querySelector("[data-corp-pin]");
  if (pin) pin.checked = false;
}

function buildPublishedRow(it) {
  const row = document.createElement("label");
  row.className = "approv-published-row";
  const pubId = String(it.publication_id || "").trim();
  const pubFmt = pubId.padStart(5, "0");
  const dt = fmtDate(it.published_at_utc);
  const pinMark = it.pinned ? " 📎" : "";
  row.innerHTML = `<input type="checkbox" name="pub" value="${escapeHtml(pubFmt)}" /><span>${escapeHtml(dt)} (${escapeHtml(pubFmt)})${pinMark}</span>`;
  return row;
}

async function initPublApprov() {
  const root = document.querySelector("[data-publapprov]");
  if (!root) return;
  const list = root.querySelector("[data-approv-list]");
  const status = root.querySelector("[data-approv-status]");
  const refreshBtn = root.querySelector("[data-approv-refresh]");
  const approveAllBtn = root.querySelector("[data-approv-approve-all]");
  const pubList = root.querySelector("[data-published-list]");
  const corpStatus = root.querySelector("[data-corp-status]");
  if (!list) return;

  const loadPublished = async () => {
    if (!pubList) return;
    pubList.innerHTML = "";
    try {
      const idx = await fetchJson("/api/content/site/index");
      const items = Array.isArray(idx.items) ? idx.items : [];
      if (!items.length) {
        pubList.innerHTML = '<p class="muted-site">Нет опубликованных новостей.</p>';
        return;
      }
      items.forEach((it) => pubList.appendChild(buildPublishedRow(it)));
    } catch (e) {
      pubList.innerHTML = `<p class="muted-site">Не удалось загрузить список: ${escapeHtml(e?.message || e)}</p>`;
    }
  };

  const selectedPubIds = () => {
    if (!pubList) return [];
    return Array.from(pubList.querySelectorAll('input[type="checkbox"][name="pub"]:checked')).map((el) => el.value);
  };

  const load = async () => {
    if (status) status.textContent = "Загрузка…";
    const data = await fetchJson("/api/content/queue");
    const items = Array.isArray(data.items) ? data.items : [];
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = '<p class="muted-site">Очередь пуста.</p>';
      if (status) status.textContent = "";
      await loadPublished();
      return;
    }
    items.forEach((it) => list.appendChild(buildApprovalsItem(it)));
    if (status) status.textContent = `Материалов: ${items.length}`;
    await loadPublished();
  };

  root.querySelector("[data-site-batch-delete]")?.addEventListener("click", async () => {
    const ids = selectedPubIds();
    if (!ids.length) {
      if (status) status.textContent = "Отметьте новости для удаления.";
      return;
    }
    try {
      if (status) status.textContent = "Удаление…";
      await fetchJson("/api/content/site/batch-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ publication_ids: ids }),
      });
      if (status) status.textContent = "Удалено.";
      await loadPublished();
    } catch (e) {
      if (status) status.textContent = `Ошибка: ${e?.message || e}`;
    }
  });

  root.querySelector("[data-site-batch-pin]")?.addEventListener("click", async () => {
    const ids = selectedPubIds();
    if (!ids.length) {
      if (status) status.textContent = "Отметьте новости для закрепления.";
      return;
    }
    try {
      if (status) status.textContent = "Закрепление…";
      await fetchJson("/api/content/site/batch-pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ publication_ids: ids, pinned: true }),
      });
      if (status) status.textContent = "Закреплено.";
      await loadPublished();
    } catch (e) {
      if (status) status.textContent = `Ошибка: ${e?.message || e}`;
    }
  });

  root.querySelector("[data-site-batch-unpin]")?.addEventListener("click", async () => {
    const ids = selectedPubIds();
    if (!ids.length) {
      if (status) status.textContent = "Отметьте новости для снятия закрепления.";
      return;
    }
    try {
      if (status) status.textContent = "Снятие закрепления…";
      await fetchJson("/api/content/site/batch-pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ publication_ids: ids, pinned: false }),
      });
      if (status) status.textContent = "Готово.";
      await loadPublished();
    } catch (e) {
      if (status) status.textContent = `Ошибка: ${e?.message || e}`;
    }
  });

  root.querySelector("[data-corp-save]")?.addEventListener("click", async () => {
    const title = root.querySelector("[data-corp-title]")?.value?.trim() || "";
    const site_text = root.querySelector("[data-corp-site]")?.value?.trim() || "";
    const vk_text = root.querySelector("[data-corp-vk]")?.value?.trim() || "";
    const internal_note = root.querySelector("[data-corp-note]")?.value?.trim() || "";
    const sources = parseCorpSources(root.querySelector("[data-corp-sources]")?.value);
    const pinned = Boolean(root.querySelector("[data-corp-pin]")?.checked);
    try {
      if (corpStatus) corpStatus.textContent = "Сохранение…";
      await fetchJson("/api/content/corporate/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, site_text, vk_text, internal_note, sources, pinned }),
      });
      clearCorpForm(root);
      if (corpStatus) corpStatus.textContent = "Отправлено в очередь согласования.";
      await load();
    } catch (e) {
      if (corpStatus) corpStatus.textContent = `Ошибка: ${e?.message || e}`;
    }
  });

  root.querySelector("[data-corp-publish]")?.addEventListener("click", async () => {
    const title = root.querySelector("[data-corp-title]")?.value?.trim() || "";
    const site_text = root.querySelector("[data-corp-site]")?.value?.trim() || "";
    const vk_text = root.querySelector("[data-corp-vk]")?.value?.trim() || "";
    const internal_note = root.querySelector("[data-corp-note]")?.value?.trim() || "";
    const sources = parseCorpSources(root.querySelector("[data-corp-sources]")?.value);
    const pinned = Boolean(root.querySelector("[data-corp-pin]")?.checked);
    try {
      if (corpStatus) corpStatus.textContent = "Публикация…";
      const res = await fetchJson("/api/content/corporate/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, site_text, vk_text, internal_note, sources, pinned }),
      });
      clearCorpForm(root);
      if (res?.last_publish_error) {
        if (corpStatus) corpStatus.textContent = `Опубликовано с ошибкой: ${res.last_publish_error}`;
      } else if (res?.vk_post_url) {
        if (corpStatus) corpStatus.textContent = `Опубликовано. VK: ${res.vk_post_url}`;
      } else if (corpStatus) corpStatus.textContent = "Опубликовано.";
      await load();
    } catch (e) {
      if (corpStatus) corpStatus.textContent = `Ошибка: ${e?.message || e}`;
    }
  });

  refreshBtn?.addEventListener("click", () => load().catch((e) => (status.textContent = `Ошибка: ${e?.message || e}`)));
  approveAllBtn?.addEventListener("click", async () => {
    try {
      if (status) status.textContent = "Одобряем все…";
      const res = await fetchJson("/api/content/queue/approve-all", { method: "POST" });
      const failed = Number(res.failed || 0);
      const approved = Number(res.approved || 0);
      if (status) status.textContent = failed ? `Готово: ок=${approved}, с ошибками=${failed}` : `Готово: ок=${approved}`;
      await load();
    } catch (e) {
      if (status) status.textContent = `Ошибка: ${e?.message || e}`;
    }
  });

  list.addEventListener("click", async (ev) => {
    const btn = ev.target?.closest?.("button");
    if (!btn) return;
    const item = btn.closest("[data-pub-id]");
    if (!item) return;
    const pubId = item.dataset.pubId;
    const st = item.querySelector("[data-action-status]");
    const title = item.querySelector("[data-edit-title]")?.value || "";
    const site = item.querySelector("[data-edit-site]")?.value || "";
    const vk = item.querySelector("[data-edit-vk]")?.value || "";

    try {
      if (st) st.textContent = "…";
      if (btn.hasAttribute("data-action-save")) {
        await fetchJson(`/api/content/queue/${encodeURIComponent(pubId)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, site_text: site, vk_text: vk }),
        });
        if (st) st.textContent = "Сохранено";
      }
      if (btn.hasAttribute("data-action-publish")) {
        // interpret checkboxes
        const approve = Boolean(item.querySelector("[data-flag-approve]")?.checked);
        const cancel = Boolean(item.querySelector("[data-flag-cancel]")?.checked);
        const pinBox = item.querySelector("[data-flag-pin]");
        const pinChecked = Boolean(pinBox?.checked);
        const wasPinned = item.dataset.pinned === "1";

        await fetchJson(`/api/content/queue/${encodeURIComponent(pubId)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, site_text: site, vk_text: vk }),
        });
        // pin toggle if changed
        if (pinBox && pinChecked !== wasPinned) {
          await fetchJson(`/api/content/queue/${encodeURIComponent(pubId)}/pin`, { method: "POST" });
        }
        if (cancel) {
          await fetchJson(`/api/content/queue/${encodeURIComponent(pubId)}/cancel`, { method: "POST" });
          if (st) st.textContent = "Отменено";
          await load();
          return;
        }
        if (approve) {
          const ares = await fetchJson(`/api/content/queue/${encodeURIComponent(pubId)}/approve`, { method: "POST" });
          if (ares.last_publish_error) {
            if (st) st.textContent = `Опубликовано с ошибкой: ${ares.last_publish_error}`;
          } else if (ares.vk_post_url) {
            if (st) st.textContent = `Опубликовано. VK: ${ares.vk_post_url}`;
          } else {
            if (st) st.textContent = "Опубликовано";
          }
          await load();
          return;
        }
        if (st) st.textContent = "Сохранено (без публикации)";
      }
    } catch (e) {
      if (st) st.textContent = `Ошибка: ${e?.message || e}`;
    }
  });

  await load();
}

function initAltExpertWidget() {
  const root = document.querySelector("[data-altbot]");
  if (!root) return;

  const panel = root.querySelector(".altbot__panel");
  const log = root.querySelector("[data-chat-log]");
  const form = root.querySelector("[data-chat-form]");
  const input = root.querySelector("input");
  const closer = root.querySelector("[data-close-chat]");
  const fab = root.querySelector("[data-open-altchat]");

  const show = () => {
    if (panel) panel.hidden = false;
    fab?.setAttribute("aria-expanded", "true");
    if (input) input.focus();
  };
  const hide = () => {
    if (panel) panel.hidden = true;
    fab?.setAttribute("aria-expanded", "false");
  };

  if (panel) panel.hidden = true;

  fab?.addEventListener("click", () => {
    if (panel?.hidden) show();
  });
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
initHomeNews();
initNewsPage();
initPublApprov();
initAltExpertWidget();
initVkBridge();
