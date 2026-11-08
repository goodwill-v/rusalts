/* ZCode web-chat (ВЭБ): vanilla JS без сборки (ТЗ §2.5).
   Список чатов, переключение, история v4-строк, SSE-стрим, отправка,
   бейджи «ждёт вас / агент пишет», создание новой сессии. */

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = {
    app: $('zc-app') || document.querySelector('.zc-app'),
    side: $('zc-side'), overlay: $('zc-overlay'), burger: $('zc-burger'),
    list: $('zc-list'), newBtn: $('zc-new'), refresh: $('zc-refresh'), logout: $('zc-logout'),
    title: $('zc-title'), status: $('zc-status'),
    empty: $('zc-empty'), messages: $('zc-messages'), more: $('zc-more'), chat: $('zc-chat'),
    input: $('zc-input'), send: $('zc-send'),
  };

  const state = {
    sessions: [],           // список сессий (с сервера)
    current: null,          // открытая сессия
    rows: new Map(),        // rowId -> row (public-формат)
    oldestRowId: null,      // для кнопки «ещё»
    stream: null,           // EventSource
    refreshTimer: null,
  };

  // ── Утилиты ─────────────────────────────────────────────────────────────

  function timeStr(ms) {
    if (!ms) return '';
    const d = new Date(ms);
    const now = new Date();
    const hm = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    if (d.toDateString() === now.toDateString()) return hm;
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' + hm;
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Мини-markdown: код-блоки, inline-код, **bold**, *italic*, ссылки, заголовки, списки.
  function md(src) {
    const blocks = [];
    let text = String(src == null ? '' : src);
    text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
      blocks.push('<pre><code>' + esc(code.replace(/\n$/, '')) + '</code></pre>');
      return '\u0000B' + (blocks.length - 1) + '\u0000';
    });
    text = esc(text);
    text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    const lines = text.split('\n');
    const out = [];
    let list = null;
    const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
    for (const line of lines) {
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (h) { closeList(); out.push('<h' + h[1].length + '>' + h[2] + '</h' + h[1].length + '>'); }
      else if (li) {
        const want = /^\s*\d+\./.test(line) ? 'ol' : 'ul';
        if (list !== want) { closeList(); out.push('<' + want + '>'); list = want; }
        out.push('<li>' + li[1] + '</li>');
      } else if (line.trim() === '') { closeList(); }
      else { closeList(); out.push('<p>' + line + '</p>'); }
    }
    closeList();
    let html = out.join('');
    html = html.replace(/\u0000B(\d+)\u0000/g, (_, i) => blocks[+i]);
    return html;
  }

  // ── API ─────────────────────────────────────────────────────────────────

  async function api(url, opts) {
    const resp = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
    if (resp.status === 401) { location.reload(); throw new Error('unauthorized'); }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
    return data;
  }

  // ── Список чатов ────────────────────────────────────────────────────────

  function badge(s) {
    if (s.status === 'waiting') return '<span class="zc-badge zc-badge--wait zc-badge--dot">ждут вас</span>';
    if (s.status === 'running') return '<span class="zc-badge zc-badge--run zc-badge--dot">агент пишет</span>';
    if (s.status === 'error') return '<span class="zc-badge">ошибка</span>';
    return '<span class="zc-badge zc-badge--dot">простаивает</span>';
  }

  function renderList() {
    el.list.innerHTML = state.sessions.map((s) => {
      const active = state.current && state.current.sessionId === s.sessionId ? ' is-active' : '';
      return '<li class="zc-item' + active + '" data-sid="' + esc(s.sessionId) + '">' +
        '<div class="zc-item__row1"><span class="zc-item__title">' + esc(s.title || 'Без названия') + '</span>' +
        badge(s) + '</div>' +
        '<div class="zc-item__meta">' + esc(s.workspace || '') + ' · ' + timeStr(s.updatedAt) + '</div>' +
        '</li>';
    }).join('') || '<li class="zc-item"><div class="zc-item__meta">Сессий не найдено</div></li>';
    el.list.querySelectorAll('.zc-item[data-sid]').forEach((node) => {
      node.addEventListener('click', () => openSession(node.getAttribute('data-sid')));
    });
  }

  async function loadSessions(keepOpen) {
    const data = await api('/zcode/api/sessions');
    state.sessions = data.sessions || [];
    renderList();
    if (!keepOpen && !state.current && state.sessions.length) {
      openSession(state.sessions[0].sessionId);
    }
  }

  // ── Открытие чата и история ─────────────────────────────────────────────

  function findSession(sid) {
    return state.sessions.find((s) => s.sessionId === sid) || null;
  }

  function renderMessages(appendMode) {
    if (!appendMode) {
      el.messages.innerHTML = '';
      const rows = [...state.rows.entries()].sort((a, b) => a[0] - b[0]);
      for (const [, row] of rows) el.messages.appendChild(rowNode(row));
    }
  }

  function rowNode(row) {
    let node;
    if (row.kind === 'userInput') {
      node = document.createElement('div');
      node.className = 'zc-msg zc-msg--user';
      node.textContent = row.text || '';
    } else if (row.kind === 'assistantText') {
      node = document.createElement('div');
      node.className = 'zc-msg zc-msg--agent zc-md' + (row.state === 'streaming' ? ' zc-msg--streaming' : '');
      node.innerHTML = md(row.text || '');
    } else if (row.kind === 'reasoning') {
      node = document.createElement('details');
      node.className = 'zc-msg zc-msg--reasoning';
      node.innerHTML = '<summary>размышления' + (row.state === 'streaming' ? '…' : '') + '</summary>' +
        '<div>' + esc(row.text || '') + '</div>';
    } else if (row.kind === 'toolCall') {
      node = document.createElement('div');
      const st = row.status === 'running' || row.status === 'inputStreaming' ? 'running' :
        (row.status === 'error' ? 'error' : '');
      node.className = 'zc-msg zc-msg--tool zc-tool' + (st ? ' zc-tool--' + st : '');
      const label = { success: 'ок', error: 'ошибка', running: 'выполняется', inputStreaming: '…',
        pendingApproval: 'ждёт подтверждения', cancelled: 'отменено' }[row.status] || row.status || '';
      node.innerHTML = '<span class="zc-tool__name">⚙ ' + esc(row.toolName || 'инструмент') + '</span>' +
        ' <span>· ' + esc(label) + '</span>' +
        (row.inputText ? '<div>' + esc(row.inputText.slice(0, 200)) + '</div>' : '');
    } else {
      node = document.createElement('div');
      node.hidden = true; // turnHeader и прочие служебные строки не показываем
    }
    node.setAttribute('data-row-id', row.rowId);
    return node;
  }

  function upsertRow(row) {
    if (!row || typeof row.rowId !== 'number') return;
    const existed = state.rows.get(row.rowId);
    if (existed && JSON.stringify(existed) === JSON.stringify(row)) return;
    state.rows.set(row.rowId, row);
    const prev = el.messages.querySelector('[data-row-id="' + row.rowId + '"]');
    const node = rowNode(row);
    if (prev) { prev.replaceWith(node); }
    else {
      const ids = [...state.rows.keys()].sort((a, b) => a - b);
      const idx = ids.indexOf(row.rowId);
      let inserted = false;
      if (idx >= 0) {
        for (let i = idx + 1; i < ids.length; i++) {
          const next = el.messages.querySelector('[data-row-id="' + ids[i] + '"]');
          if (next) { el.messages.insertBefore(node, next); inserted = true; break; }
        }
      }
      if (!inserted) el.messages.appendChild(node);
    }
    const nearBottom = el.chat.scrollHeight - el.chat.scrollTop - el.chat.clientHeight < 140;
    if (nearBottom) el.chat.scrollTop = el.chat.scrollHeight;
  }

  function closeStream() {
    if (state.stream) { state.stream.close(); state.stream = null; }
  }

  async function openSession(sid) {
    const session = findSession(sid);
    if (!session) return;
    closeStream();
    state.current = session;
    state.rows = new Map();
    state.oldestRowId = null;
    renderList();
    el.empty.hidden = true;
    el.messages.hidden = false;
    el.more.hidden = false;
    el.title.textContent = session.title || 'Без названия';
    el.input.disabled = false;
    el.send.disabled = false;
    el.messages.innerHTML = '<div class="zc-item__meta">Загрузка истории…</div>';
    let data;
    try {
      data = await api('/zcode/api/sessions/' + encodeURIComponent(sid) +
        '/rows?limit=60&workspace=' + encodeURIComponent(session.workspace || ''));
    } catch (e) {
      el.messages.innerHTML = '<div class="zc-item__meta">Ошибка: ' + esc(String(e.message || e)) + '</div>';
      return;
    }
    el.messages.innerHTML = '';
    const rows = (data.rows || []).filter((r) => typeof r.rowId === 'number');
    rows.forEach(upsertRow);
    if (rows.length >= 60) {
      state.oldestRowId = Math.min(...rows.map((r) => r.rowId));
    } else {
      el.more.hidden = true;
    }
    if (!rows.length) {
      el.messages.innerHTML = '<div class="zc-item__meta">Сообщений пока нет — напишите первым.</div>';
    }
    el.chat.scrollTop = el.chat.scrollHeight;
    setStatus(session.status === 'waiting' ? 'ждут вашего ответа' :
      session.status === 'running' ? 'агент пишет…' : '');
    startStream(sid, session.workspace);
    if (window.innerWidth <= 900) el.app.classList.remove('zc-side-open');
  }

  function startStream(sid, workspace) {
    closeStream();
    const url = '/zcode/api/stream?session_id=' + encodeURIComponent(sid) +
      '&workspace=' + encodeURIComponent(workspace || '') +
      '&after_row_id=' + (state.oldestRowId ? '' : Math.max(0, ...[...state.rows.keys()]));
    state.stream = new EventSource(url);
    state.stream.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'rows') {
        (msg.rows || []).forEach(upsertRow);
      } else if (msg.type === 'status') {
        const label = msg.status === 'running' ? 'агент пишет…' :
          msg.status === 'waiting' ? 'ждут вашего ответа' : '';
        setStatus(label);
        if (msg.status !== 'running') loadSessions(true);
      } else if (msg.type === 'error') {
        setStatus('сбой связи с zcode — повторяем…');
      }
    };
    state.stream.onerror = () => { /* EventSource сам переподключится */ };
  }

  function setStatus(label) {
    el.status.hidden = !label;
    el.status.textContent = label || '';
  }

  el.more.addEventListener('click', async () => {
    if (!state.current || state.oldestRowId == null) return;
    const data = await api('/zcode/api/sessions/' + encodeURIComponent(state.current.sessionId) +
      '/rows?limit=60&before_row_id=' + state.oldestRowId +
      '&workspace=' + encodeURIComponent(state.current.workspace || ''));
    const rows = (data.rows || []).filter((r) => typeof r.rowId === 'number');
    if (!rows.length) { el.more.hidden = true; return; }
    const frag = document.createDocumentFragment();
    rows.forEach((r) => { state.rows.set(r.rowId, r); frag.appendChild(rowNode(r)); });
    el.messages.prepend(frag);
    state.oldestRowId = Math.min(...rows.map((r) => r.rowId));
  });

  // ── Отправка ────────────────────────────────────────────────────────────

  async function sendMessage() {
    if (!state.current) return;
    const text = el.input.value.trim();
    if (!text) return;
    el.send.disabled = true;
    el.input.value = '';
    autosize();
    try {
      await api('/zcode/api/sessions/' + encodeURIComponent(state.current.sessionId) + '/send', {
        method: 'POST',
        body: JSON.stringify({ text: text, workspace: state.current.workspace || '' }),
      });
      // Пользовательскую строку пришлёт SSE (userInput row); для мгновенной отдачи
      // добавляем временную — она заменится серверной по rowId при приходе.
      upsertRow({ kind: 'userInput', rowId: -Date.now(), text: text });
      el.chat.scrollTop = el.chat.scrollHeight;
      setStatus('агент пишет…');
    } catch (e) {
      el.input.value = text; // вернуть текст при ошибке
      setStatus('');
      alert('Не отправлено: ' + (e.message || e));
    } finally {
      el.send.disabled = false;
      el.input.focus();
    }
  }

  el.send.addEventListener('click', sendMessage);
  el.input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); sendMessage(); }
  });
  function autosize() {
    el.input.style.height = 'auto';
    el.input.style.height = Math.min(el.input.scrollHeight, 160) + 'px';
  }
  el.input.addEventListener('input', autosize);

  // ── Новая сессия (Этап 2) ───────────────────────────────────────────────

  el.newBtn.addEventListener('click', async () => {
    const title = prompt('Название нового чата:', '');
    if (title === null) return;
    el.newBtn.disabled = true;
    try {
      const data = await api('/zcode/api/sessions', {
        method: 'POST',
        body: JSON.stringify({ title: title.trim() || '', workspace: (state.sessions[0] || {}).workspace || '' }),
      });
      if (data.session) {
        await loadSessions(true);
        openSession(data.session.sessionId);
      }
    } catch (e) {
      alert('Не удалось создать сессию: ' + (e.message || e) +
        '\n(если API zcode не поддерживает создание — см. отчёт в PR)');
    } finally {
      el.newBtn.disabled = false;
    }
  });

  // ── Прочее ──────────────────────────────────────────────────────────────

  el.refresh.addEventListener('click', () => loadSessions(true));
  el.logout.addEventListener('click', async () => {
    await fetch('/zcode/logout', { method: 'POST' });
    location.reload();
  });
  el.burger.addEventListener('click', () => el.app.classList.toggle('zc-side-open'));
  el.overlay.addEventListener('click', () => el.app.classList.remove('zc-side-open'));

  loadSessions(false);
  state.refreshTimer = setInterval(() => loadSessions(true), 5000);
})();
