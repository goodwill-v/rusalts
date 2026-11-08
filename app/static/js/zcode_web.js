/* ZCode web-chat (ВЭБ): vanilla JS без сборки (ТЗ §2.5).
   Табы «ZCode | Алик» (ТЗ-дополнение): ZCode — WS/v4-строки и SSE;
   Алик — история из БД на бэкенде (общая для телефона и десктопа),
   стрим ответа через fetch-POST SSE. */

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = {
    app: document.querySelector('.zc-app'),
    side: $('zc-side'), overlay: $('zc-overlay'), burger: $('zc-burger'),
    tabs: { zcode: $('zc-tab-zcode'), alik: $('zc-tab-alik') },
    list: $('zc-list'), newBtn: $('zc-new'), refresh: $('zc-refresh'), logout: $('zc-logout'),
    title: $('zc-title'), status: $('zc-status'),
    empty: $('zc-empty'), messages: $('zc-messages'), more: $('zc-more'), chat: $('zc-chat'),
    input: $('zc-input'), send: $('zc-send'),
  };

  const state = {
    tab: 'zcode',           // 'zcode' | 'alik'
    sessions: [],           // ZCode: список сессий
    current: null,          // ZCode: открытая сессия
    rows: new Map(),        // ZCode: rowId -> row
    oldestRowId: null,
    stream: null,           // ZCode: EventSource
    refreshTimer: null,
    alik: {
      chats: [],
      current: null,        // chat_id
      history: [],          // [{role, content, ts}] из БД
      streaming: null,      // узел стримящего ответа
    },
  };

  // ── Утилиты ─────────────────────────────────────────────────────────────

  function timeStr(ms) {
    if (!ms) return '';
    const d = new Date(ms < 1e12 ? ms * 1000 : ms); // сек или мс
    const now = new Date();
    const hm = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    if (d.toDateString() === now.toDateString()) return hm;
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' + hm;
  }

  function esc(s) {
    return String(s == null ? '' : s)
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

  function setStatus(label) {
    el.status.hidden = !label;
    el.status.textContent = label || '';
  }

  // ── Табы ────────────────────────────────────────────────────────────────

  function setTab(tab) {
    state.tab = tab;
    for (const [name, node] of Object.entries(el.tabs)) {
      node.classList.toggle('is-active', name === tab);
      node.setAttribute('aria-selected', name === tab ? 'true' : 'false');
    }
    stopZcodeStreamIfBackgrounded();
    if (tab === 'zcode') {
      renderZcodeList();
      if (state.current) { /* чат уже отрисован */ }
      else showEmpty('Выберите чат слева — история агентов подгрузится автоматически.');
    } else {
      renderAlikList();
      if (!state.alik.current) showEmpty('Выберите чат слева или создайте новый — диалог с Аликом.');
    }
  }

  function showEmpty(text) {
    el.empty.hidden = false;
    el.messages.hidden = true;
    el.more.hidden = true;
    el.empty.innerHTML = '<p>' + esc(text) + '</p>';
    el.title.textContent = 'Выберите чат';
    el.input.disabled = true;
    el.send.disabled = true;
  }

  function stopZcodeStreamIfBackgrounded() {
    if (state.tab !== 'zcode' && state.stream) { state.stream.close(); state.stream = null; }
  }

  // ── Список чатов ZCode ──────────────────────────────────────────────────

  function badge(s) {
    if (s.status === 'waiting') return '<span class="zc-badge zc-badge--wait zc-badge--dot">ждут вас</span>';
    if (s.status === 'running') return '<span class="zc-badge zc-badge--run zc-badge--dot">агент пишет</span>';
    if (s.status === 'error') return '<span class="zc-badge">ошибка</span>';
    return '<span class="zc-badge zc-badge--dot">простаивает</span>';
  }

  function renderZcodeList() {
    el.list.innerHTML = state.sessions.map((s) => {
      const active = state.tab === 'zcode' && state.current && state.current.sessionId === s.sessionId ? ' is-active' : '';
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
    if (state.tab === 'zcode') renderZcodeList();
    if (!keepOpen && !state.current && state.sessions.length && state.tab === 'zcode') {
      openSession(state.sessions[0].sessionId);
    }
  }

  // ── Чат ZCode: история и стрим ──────────────────────────────────────────

  function findSession(sid) {
    return state.sessions.find((s) => s.sessionId === sid) || null;
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
    scrollBottomIfNear();
  }

  function scrollBottomIfNear() {
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
    state.tab = 'zcode';
    syncTabButtons();
    state.current = session;
    state.rows = new Map();
    state.oldestRowId = null;
    renderZcodeList();
    el.empty.hidden = true;
    el.messages.hidden = false;
    el.more.hidden = true;
    el.title.textContent = session.title || 'Без названия';
    el.input.disabled = false;
    el.send.disabled = false;
    el.input.placeholder = 'Сообщение агенту… (Enter — отправить, Shift+Enter — новая строка)';
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
      el.more.hidden = false;
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

  function syncTabButtons() {
    for (const [name, node] of Object.entries(el.tabs)) {
      node.classList.toggle('is-active', name === state.tab);
      node.setAttribute('aria-selected', name === state.tab ? 'true' : 'false');
    }
  }

  function startStream(sid, workspace) {
    closeStream();
    const ids = [...state.rows.keys()].filter((x) => x > 0);
    const url = '/zcode/api/stream?session_id=' + encodeURIComponent(sid) +
      '&workspace=' + encodeURIComponent(workspace || '') +
      '&after_row_id=' + (ids.length ? Math.max(...ids) : 0);
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
    if (rows.length < 60) el.more.hidden = true;
  });

  // ── Отправка в ZCode ────────────────────────────────────────────────────

  async function sendZcode() {
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
      upsertRow({ kind: 'userInput', rowId: -Date.now(), text: text });
      el.chat.scrollTop = el.chat.scrollHeight;
      setStatus('агент пишет…');
    } catch (e) {
      el.input.value = text;
      setStatus('');
      alert('Не отправлено: ' + (e.message || e));
    } finally {
      el.send.disabled = false;
      el.input.focus();
    }
  }

  // ── Таб «Алик»: список чатов (из БД на бэкенде) ─────────────────────────

  function renderAlikList() {
    el.list.innerHTML = state.alik.chats.map((c) => {
      const active = state.tab === 'alik' && state.alik.current === c.chat_id ? ' is-active' : '';
      return '<li class="zc-item' + active + '" data-alik="' + esc(c.chat_id) + '">' +
        '<div class="zc-item__row1"><span class="zc-item__title">' + esc(c.title || 'Новый чат') + '</span></div>' +
        '<div class="zc-item__meta">Алик · ' + c.messages + ' сообщ. · ' + timeStr(c.last_ts) + '</div>' +
        '</li>';
    }).join('') || '<li class="zc-item"><div class="zc-item__meta">Чатов с Аликом пока нет</div></li>';
    el.list.querySelectorAll('.zc-item[data-alik]').forEach((node) => {
      node.addEventListener('click', () => openAlikChat(node.getAttribute('data-alik')));
    });
  }

  async function loadAlikChats(keepOpen) {
    const data = await api('/zcode/api/alik/chats');
    state.alik.chats = data.chats || [];
    if (state.tab === 'alik') {
      renderAlikList();
      if (!keepOpen && !state.alik.current) {
        showEmpty('Выберите чат слева или создайте новый — диалог с Аликом.');
      }
    }
  }

  function alikMsgNode(m) {
    if (m.role === 'user') {
      const node = document.createElement('div');
      node.className = 'zc-msg zc-msg--user';
      node.textContent = m.content;
      return node;
    }
    const node = document.createElement('div');
    node.className = 'zc-msg zc-msg--agent zc-md';
    node.innerHTML = md(m.content);
    return node;
  }

  async function openAlikChat(chatId) {
    state.tab = 'alik';
    syncTabButtons();
    state.alik.current = chatId;
    renderAlikList();
    el.empty.hidden = true;
    el.messages.hidden = false;
    el.more.hidden = true;
    const meta = state.alik.chats.find((c) => c.chat_id === chatId);
    el.title.textContent = (meta && meta.title) || 'Чат с Аликом';
    el.input.disabled = false;
    el.send.disabled = false;
    el.input.placeholder = 'Сообщение Алику… (Enter — отправить, Shift+Enter — новая строка)';
    el.messages.innerHTML = '<div class="zc-item__meta">Загрузка истории…</div>';
    let data;
    try {
      data = await api('/zcode/api/alik/history?chat_id=' + encodeURIComponent(chatId));
    } catch (e) {
      el.messages.innerHTML = '<div class="zc-item__meta">Ошибка: ' + esc(String(e.message || e)) + '</div>';
      return;
    }
    el.messages.innerHTML = '';
    state.alik.history = data.messages || [];
    if (!state.alik.history.length) {
      el.messages.innerHTML = '<div class="zc-item__meta">Сообщений пока нет — напишите первым.</div>';
    } else {
      const frag = document.createDocumentFragment();
      state.alik.history.forEach((m) => frag.appendChild(alikMsgNode(m)));
      el.messages.appendChild(frag);
    }
    el.chat.scrollTop = el.chat.scrollHeight;
    setStatus('');
    if (window.innerWidth <= 900) el.app.classList.remove('zc-side-open');
  }

  // ── Отправка Алику: fetch-POST + SSE-парсер ─────────────────────────────

  async function sendAlik() {
    if (!state.alik.current) return;
    const text = el.input.value.trim();
    if (!text) return;
    el.send.disabled = true;
    el.input.value = '';
    autosize();
    // пользовательский пузырь сразу; серверная история придёт при перезагрузке
    const userNode = document.createElement('div');
    userNode.className = 'zc-msg zc-msg--user';
    userNode.textContent = text;
    if (el.messages.lastElementChild && el.messages.lastElementChild.classList &&
        el.messages.lastElementChild.classList.contains('zc-item__meta')) {
      el.messages.innerHTML = '';
    }
    el.messages.appendChild(userNode);
    el.chat.scrollTop = el.chat.scrollHeight;

    // стримящий пузырь ответа
    const aNode = document.createElement('div');
    aNode.className = 'zc-msg zc-msg--agent zc-msg--streaming';
    aNode.textContent = '';
    el.messages.appendChild(aNode);
    let full = '';
    const paint = () => { aNode.textContent = full; el.chat.scrollTop = el.chat.scrollHeight; };

    try {
      const resp = await fetch('/zcode/api/alik/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: state.alik.current, text: text }),
      });
      if (resp.status === 401) { location.reload(); return; }
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        throw new Error(d.detail || ('HTTP ' + resp.status));
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let paintTimer = null;
      const processEvent = (line) => {
        if (!line.startsWith('data:')) return;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch { return; }
        if (ev.type === 'delta') {
          full += ev.text || '';
          if (!paintTimer) {
            paintTimer = setTimeout(() => { paintTimer = null; paint(); }, 120);
          }
        } else if (ev.type === 'error') {
          throw new Error(ev.message || 'ошибка gateway');
        }
      };
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          raw.split('\n').forEach(processEvent);
        }
      }
      if (paintTimer) { clearTimeout(paintTimer); paintTimer = null; }
      aNode.classList.remove('zc-msg--streaming');
      aNode.innerHTML = md(full); // финальный маркдаун
      el.chat.scrollTop = el.chat.scrollHeight;
      setStatus('');
      loadAlikChats(true); // у чата появилось название (из первого сообщения)
    } catch (e) {
      aNode.classList.remove('zc-msg--streaming');
      if (!full) aNode.remove();
      el.input.value = text; // вернуть текст для повторной отправки
      setStatus('');
      alert('Не отправлено: ' + (e.message || e));
    } finally {
      el.send.disabled = false;
      el.input.focus();
    }
  }

  // ── «+ Новый чат» — поведение по табу ───────────────────────────────────

  el.newBtn.addEventListener('click', async () => {
    el.newBtn.disabled = true;
    try {
      if (state.tab === 'alik') {
        const data = await api('/zcode/api/alik/chats', { method: 'POST', body: '{}' });
        await loadAlikChats(true);
        openAlikChat(data.chat_id);
        setStatus('новый чат создан');
      } else {
        const data = await api('/zcode/api/sessions', {
          method: 'POST',
          body: JSON.stringify({ workspace: (state.sessions[0] || {}).workspace || '' }),
        });
        if (data.session) {
          await loadSessions(true);
          openSession(data.session.sessionId);
          setStatus('новый чат создан — название появится из первого сообщения');
        }
      }
    } catch (e) {
      alert('Не удалось создать чат: ' + (e.message || e));
    } finally {
      el.newBtn.disabled = false;
    }
  });

  // ── Общий ввод ──────────────────────────────────────────────────────────

  async function sendMessage() {
    if (state.tab === 'alik') return sendAlik();
    return sendZcode();
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

  // ── Прочее ──────────────────────────────────────────────────────────────

  el.refresh.addEventListener('click', () => {
    if (state.tab === 'alik') loadAlikChats(true);
    else loadSessions(true);
  });
  el.logout.addEventListener('click', async () => {
    await fetch('/zcode/logout', { method: 'POST' });
    location.reload();
  });
  el.burger.addEventListener('click', () => el.app.classList.toggle('zc-side-open'));
  el.overlay.addEventListener('click', () => el.app.classList.remove('zc-side-open'));
  Object.entries(el.tabs).forEach(([name, node]) => {
    node.addEventListener('click', () => setTab(name));
  });

  loadSessions(false);
  loadAlikChats(true);
  state.refreshTimer = setInterval(() => {
    if (state.tab === 'alik') loadAlikChats(true);
    else loadSessions(true);
  }, 5000);
})();
