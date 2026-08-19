/* capwrap operator UI.
 *
 * One WebSocket carries every system event; a second one is opened per
 * container for its terminal. State lives in `state` and the DOM is re-rendered
 * from it, which is plenty for a handful of agents and keeps the whole thing
 * readable without a framework.
 */

'use strict';

const state = {
  containers: [],
  tree: [],
  approvals: [],
  messages: [],
  selected: null,
  caps: {},
};

let term = null;
let fitAddon = null;
let termSocket = null;
let termContainer = null;

const $ = (id) => document.getElementById(id);
const api = async (path, options) => {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || body.detail || `${res.status} ${res.statusText}`);
  }
  return res.status === 204 ? null : res.json();
};

const escapeHtml = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

// ------------------------------------------------------------------ theme

function initTheme() {
  const saved = localStorage.getItem('capwrap-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  $('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'light'
      ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('capwrap-theme', next);
    if (term) applyTerminalTheme();
  });
}

function applyTerminalTheme() {
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  term.options.theme = light
    ? { background: '#eceff3', foreground: '#1d2330', cursor: '#0a66d0' }
    : { background: '#0a0d12', foreground: '#d7dee8', cursor: '#4c9aff' };
}

// ------------------------------------------------------------------ resizing

// Which CSS variable each splitter drives, which way it grows, and the range it
// is allowed to move in.
const PANELS = {
  'resize-sidebar': { varName: '--sidebar-w', from: 'left', min: 150, max: 560, def: 260 },
  'resize-inbox': { varName: '--inbox-w', from: 'right', min: 200, max: 900, def: 320 },
};

const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));

function setPanelWidth(id, px, { persist = true } = {}) {
  const spec = PANELS[id];
  const width = Math.round(clamp(px, spec.min, spec.max));
  document.documentElement.style.setProperty(spec.varName, `${width}px`);
  if (persist) localStorage.setItem(`capwrap-${spec.varName}`, String(width));
  return width;
}

function initResizers() {
  // Restore whatever the operator chose last time.
  for (const [id, spec] of Object.entries(PANELS)) {
    const saved = Number(localStorage.getItem(`capwrap-${spec.varName}`));
    if (saved) setPanelWidth(id, saved, { persist: false });
  }

  for (const id of Object.keys(PANELS)) {
    const handle = $(id);
    if (!handle) continue;
    const spec = PANELS[id];

    handle.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      // Capture on the handle so the drag survives the pointer crossing the
      // terminal, which would otherwise swallow the move events.
      handle.setPointerCapture(event.pointerId);
      handle.classList.add('dragging');
      document.body.classList.add('resizing');

      const onMove = (move) => {
        const width = spec.from === 'left'
          ? move.clientX
          : window.innerWidth - move.clientX;
        setPanelWidth(id, width);
        // Reflow the terminal as the column moves, so it tracks the drag
        // instead of snapping when you let go.
        requestAnimationFrame(fitTerminal);
      };

      const onUp = () => {
        handle.releasePointerCapture(event.pointerId);
        handle.classList.remove('dragging');
        document.body.classList.remove('resizing');
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onUp);
        handle.removeEventListener('pointercancel', onUp);
        // The PTY only needs telling once, at the end.
        syncTerminalSize();
      };

      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);
      handle.addEventListener('pointercancel', onUp);
    });

    handle.addEventListener('dblclick', () => {
      setPanelWidth(id, spec.def);
      syncTerminalSize();
    });

    // Keyboard access, so the layout is not mouse-only.
    handle.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 40 : 10;
      const current = parseInt(
        getComputedStyle(document.documentElement).getPropertyValue(spec.varName), 10,
      ) || spec.def;
      const grow = spec.from === 'left' ? 1 : -1;

      if (event.key === 'ArrowLeft') setPanelWidth(id, current - step * grow);
      else if (event.key === 'ArrowRight') setPanelWidth(id, current + step * grow);
      else if (event.key === 'Home') setPanelWidth(id, spec.def);
      else return;

      event.preventDefault();
      syncTerminalSize();
    });
  }
}

// ------------------------------------------------------------------ tree

function statusOf(container) {
  if (container.running) return 'running';
  return container.state || 'created';
}

function renderTree() {
  const host = $('tree');
  if (!state.tree.length) {
    host.innerHTML = '<div class="empty">No containers registered.</div>';
    return;
  }

  const byName = Object.fromEntries(state.containers.map((c) => [c.name, c]));

  const node = (entry) => {
    const live = byName[entry.name] || entry;
    const status = statusOf(live);
    const selected = state.selected === entry.name ? ' selected' : '';
    const kids = (entry.children || []).map(node).join('');
    // Only a finished container offers dismiss, so a mis-click cannot end an
    // agent that is mid-task. Stop it first if you mean to.
    const dismiss = status === 'running' ? '' : `
      <button class="dismiss" data-dismiss="${escapeHtml(entry.name)}"
              title="Dismiss ${escapeHtml(entry.name)}">×</button>`;
    const exit = live.exit_code !== null && live.exit_code !== undefined
      ? `exit ${live.exit_code}` : `${entry.caps ?? 0} caps`;
    return `
      <div>
        <div class="node${selected}" data-name="${escapeHtml(entry.name)}">
          <span class="dot ${status}"></span>
          <span class="name">${escapeHtml(entry.name)}</span>
          <span class="meta">${escapeHtml(exit)}</span>
          ${dismiss}
        </div>
        ${kids ? `<div class="children">${kids}</div>` : ''}
      </div>`;
  };

  host.innerHTML = state.tree.map(node).join('');
  host.querySelectorAll('.node').forEach((el) => {
    el.addEventListener('click', () => select(el.dataset.name));
  });
  host.querySelectorAll('[data-dismiss]').forEach((b) => {
    b.addEventListener('click', (event) => {
      event.stopPropagation();   // do not also select the node we are removing
      dismissContainer(b.dataset.dismiss);
    });
  });

  const finished = state.containers.filter((c) => !c.running).length;
  $('dismiss-finished').hidden = finished === 0;
  $('dismiss-finished').textContent = `Dismiss ${finished} finished`;
}

async function dismissContainer(name) {
  const ok = confirm(
    `Dismiss ${name}?\n\n`
    + 'It is removed from the tree and its capabilities are revoked, including '
    + 'any that other agents hold on it.\n\n'
    + 'Its work on disk is kept — the git branch, overlay writes and private '
    + `copies all survive. Run \`capwrap clean ${name} --yes\` to delete those too.`,
  );
  if (!ok) return;
  try {
    const result = await api(`/api/containers/${name}`, { method: 'DELETE' });
    if (result.reparented && result.reparented.length) {
      alert(`${name} dismissed. Its children now sit under its parent: `
            + result.reparented.join(', '));
    }
    if (state.selected === name) {
      state.selected = null;
      if (termSocket) { termSocket.close(); termSocket = null; }
      term.reset();
      $('term-title').textContent = 'no container selected';
      renderCaps();
    }
    await refreshOverview();
  } catch (err) {
    alert(`Could not dismiss ${name}: ${err.message}`);
  }
}

function renderComposeTargets() {
  const select = $('compose-target');
  const previous = select.value;
  select.innerHTML = state.containers
    .map((c) => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}</option>`)
    .join('');
  if (previous) select.value = previous;
}

// ------------------------------------------------------------------ terminal

function initTerminal() {
  term = new Terminal({
    fontFamily: 'ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace',
    fontSize: 13,
    cursorBlink: true,
    scrollback: 5000,
    convertEol: false,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($('terminal'));
  applyTerminalTheme();

  // Keystrokes go straight down the socket; the agent sees a real terminal.
  term.onData((data) => {
    if (termSocket && termSocket.readyState === WebSocket.OPEN) {
      termSocket.send(JSON.stringify({ type: 'input', data }));
    }
  });

  window.addEventListener('resize', syncTerminalSize);
  setTimeout(syncTerminalSize, 50);
}

/** Reflow xterm to its container. Cheap enough to call during a drag. */
function fitTerminal() {
  if (!fitAddon) return;
  try { fitAddon.fit(); } catch (_) { /* panel not visible yet */ }
}

/** Reflow, then tell the PTY its new size so the agent's TUI redraws to match. */
function syncTerminalSize() {
  fitTerminal();
  if (term && termSocket && termSocket.readyState === WebSocket.OPEN) {
    termSocket.send(JSON.stringify({
      type: 'resize', cols: term.cols, rows: term.rows,
    }));
  }
}

function openTerminal(name) {
  if (termSocket) { termSocket.close(); termSocket = null; }
  term.reset();
  termContainer = name;
  $('term-title').textContent = name;

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${proto}//${location.host}/ws/terminal/${name}`);
  socket.binaryType = 'arraybuffer';

  socket.onmessage = (event) => {
    if (typeof event.data === 'string') {
      const payload = JSON.parse(event.data);
      if (payload.type === 'error') term.writeln(`\r\n\x1b[33m${payload.message}\x1b[0m`);
      return;
    }
    term.write(new Uint8Array(event.data));
  };
  socket.onopen = () => setTimeout(syncTerminalSize, 30);
  socket.onclose = () => {
    if (termContainer === name) term.writeln('\r\n\x1b[90m[disconnected]\x1b[0m');
  };
  termSocket = socket;
}

async function select(name, { focusTerminal = false } = {}) {
  // Switch tabs *before* opening the socket: xterm measures its container when
  // it fits, and a hidden panel has no dimensions, so opening a terminal into a
  // display:none panel leaves it stuck at the default 80x24.
  if (focusTerminal) showTab('terminal');
  state.selected = name;
  $('grant-form').hidden = true;
  renderTree();
  openTerminal(name);
  await loadCaps(name);
}

// ------------------------------------------------------------------ caps

async function loadCaps(name) {
  try {
    state.caps[name] = await api(`/api/caps/${name}`);
  } catch (err) {
    state.caps[name] = [];
  }
  renderCaps();
}

// Rights that let the holder disrupt another container get highlighted, since
// those are the ones worth noticing in a glance.
const STRONG_RIGHTS = new Set(['kill', 'signal', 'write_input', 'write', 'create', 'map']);

function renderCaps() {
  const name = state.selected;
  $('caps-title').textContent = name ? `capabilities held by ${name}` : 'capabilities';
  const caps = state.caps[name] || [];
  const host = $('caps');

  if (!caps.length) {
    host.innerHTML = '<div class="empty">Select a container to see what it may do.</div>';
    return;
  }

  const rows = caps.map((cap) => {
    const rights = cap.rights
      .map((r) => `<span class="right${STRONG_RIGHTS.has(r) ? ' strong' : ''}">${r}</span>`)
      .join('');
    const detail = cap.detail || {};
    const extra = detail.path || detail.state
      || (detail.remaining !== undefined ? `${detail.remaining} spawns left` : '');
    return `
      <tr>
        <td class="mono">${cap.slot}</td>
        <td>${escapeHtml(cap.kind)}</td>
        <td class="mono">${escapeHtml(cap.label)}</td>
        <td>${rights}</td>
        <td class="muted small">${escapeHtml(extra)}</td>
        <td><button class="ghost small danger" data-revoke="${cap.slot}">Revoke</button></td>
      </tr>`;
  }).join('');

  host.innerHTML = `
    <div class="cap-group">
      <table>
        <thead><tr>
          <th>Slot</th><th>Kind</th><th>Label</th><th>Rights</th><th></th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="muted small">
        Revoking is recursive: anything this container passed on derived from the
        same capability dies with it.
      </p>
    </div>`;

  host.querySelectorAll('[data-revoke]').forEach((button) => {
    button.addEventListener('click', async () => {
      const slot = Number(button.dataset.revoke);
      if (!confirm(`Revoke slot ${slot} from ${name}, and everything derived from it?`)) return;
      try {
        const result = await api('/api/caps/revoke', {
          method: 'POST',
          body: JSON.stringify({ container: name, slot, include_self: true }),
        });
        alert(`Revoked ${result.revoked} mapping(s). Affected: ${result.holders.join(', ') || 'nobody'}`);
        await loadCaps(name);
      } catch (err) {
        alert(`Revoke failed: ${err.message}`);
      }
    });
  });
}

// ------------------------------------------------------------------ granting

// What the operator can hand out on a container capability. Ordered so the
// least alarming come first; `kill` and `write_input` are the ones that let the
// holder actually disrupt its neighbour, so they are not pre-ticked.
const GRANTABLE = [
  { name: 'send', on: true, hint: 'post messages to it' },
  { name: 'inspect', on: true, hint: 'see that it exists and its status' },
  { name: 'read_output', on: false, hint: 'read its terminal output' },
  { name: 'delegate', on: false, hint: 'pass this capability on to others' },
  { name: 'signal', on: false, hint: 'interrupt it' },
  { name: 'write_input', on: false, hint: 'type at its terminal' },
  { name: 'kill', on: false, hint: 'terminate it' },
];

function renderGrantForm() {
  const holder = state.selected;
  if (!holder) return;
  $('grant-holder').textContent = holder;

  // You cannot usefully grant a container a capability on itself.
  $('grant-target').innerHTML = state.containers
    .filter((c) => c.name !== holder)
    .map((c) => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}</option>`)
    .join('') || '<option value="">(no other containers)</option>';

  $('grant-rights').innerHTML = GRANTABLE.map((r) => `
    <label class="check" title="${escapeHtml(r.hint)}">
      <input type="checkbox" value="${r.name}" ${r.on ? 'checked' : ''}>
      <span class="right${STRONG_RIGHTS.has(r.name) ? ' strong' : ''}">${r.name}</span>
    </label>`).join('');
}

function wireGrant() {
  const form = $('grant-form');

  $('btn-grant').addEventListener('click', () => {
    if (!state.selected) return alert('Select a container first.');
    renderGrantForm();
    form.hidden = !form.hidden;
  });
  $('grant-cancel').addEventListener('click', () => { form.hidden = true; });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const holder = state.selected;
    const target = $('grant-target').value;
    const rights = [...$('grant-rights').querySelectorAll('input:checked')]
      .map((i) => i.value);
    if (!target) return;
    if (!rights.length) return alert('Pick at least one right.');

    try {
      const result = await api('/api/caps/grant', {
        method: 'POST',
        body: JSON.stringify({
          holder, target_container: target, rights,
        }),
      });
      form.hidden = true;
      await loadCaps(holder);
      alert(`${holder} now holds slot ${result.slot} on ${target} (${rights.join(', ')}).`);
    } catch (err) {
      alert(`Grant failed: ${err.message}`);
    }
  });
}

// ------------------------------------------------------------------ approvals

function renderApprovals() {
  const host = $('approvals');
  $('approval-count').textContent = state.approvals.length;
  $('approval-count').className = state.approvals.length
    ? 'pill pill-warn' : 'pill pill-quiet';

  if (!state.approvals.length) {
    host.innerHTML = '<div class="empty">Nothing waiting on you.</div>';
    return;
  }

  host.innerHTML = state.approvals.map((approval) => {
    const ctx = approval.context || {};

    // A capability request is answered by *granting*, not by saying yes: the
    // approval performs the delegation, and the operator can trim the rights
    // on the way through.
    if (ctx.kind === 'capability_request') {
      const req = ctx.request || {};
      const asked = new Set(req.rights || []);
      // Offer the rights that mean something for this object kind, not a
      // container-shaped list regardless of what was asked for.
      const choices = req.valid_rights && req.valid_rights.length
        ? req.valid_rights
        : GRANTABLE.map((r) => r.name);
      const boxes = [...new Set(choices)].map((name) => `
        <label class="check" title="${escapeHtml(name)}">
          <input type="checkbox" value="${escapeHtml(name)}"
                 ${asked.has(name) ? 'checked' : ''}>
          <span class="right${STRONG_RIGHTS.has(name) ? ' strong' : ''}">${escapeHtml(name)}</span>
        </label>`).join('');

      return `
        <div class="approval" data-request="${approval.id}">
          <div class="who">${escapeHtml(approval.container)} · capability request</div>
          <div class="q">
            wants a <strong>${escapeHtml(req.kind || '?')}</strong> capability
            ${req.target ? `on <span class="mono">${escapeHtml(req.target)}</span>` : ''}
            ${req.kind === 'factory' ? `(quota ${Number(req.quota) || 1})` : ''}
          </div>
          ${req.reason ? `<div class="ctx">${escapeHtml(req.reason)}</div>` : ''}
          <div class="rights-picker">${boxes}</div>
          <div class="actions">
            <button class="primary small" data-grant="${approval.id}">Grant</button>
            <button class="small danger" data-deny="${approval.id}">Deny</button>
            <button class="ghost small" data-goto="${escapeHtml(approval.container)}">Open</button>
          </div>
        </div>`;
    }

    const context = Object.keys(ctx).length
      ? `<div class="ctx">${escapeHtml(JSON.stringify(ctx, null, 2))}</div>`
      : '';
    return `
      <div class="approval">
        <div class="who">${escapeHtml(approval.container)}</div>
        <div class="q">${escapeHtml(approval.question)}</div>
        ${context}
        <div class="actions">
          <button class="primary small" data-allow="${approval.id}">Allow</button>
          <button class="small danger" data-deny="${approval.id}">Deny</button>
          <button class="ghost small" data-goto="${escapeHtml(approval.container)}">Open</button>
        </div>
      </div>`;
  }).join('');

  const resolve = async (id, decision, rights = null) => {
    try {
      await api(`/api/approvals/${id}`, {
        method: 'POST',
        body: JSON.stringify({ decision, reason: '', rights }),
      });
      state.approvals = state.approvals.filter((a) => a.id !== id);
      renderApprovals();
      if (state.selected) loadCaps(state.selected);
    } catch (err) {
      alert(`Could not answer: ${err.message}`);
    }
  };

  host.querySelectorAll('[data-grant]').forEach((b) =>
    b.addEventListener('click', () => {
      const card = b.closest('.approval');
      const rights = [...card.querySelectorAll('.rights-picker input:checked')]
        .map((i) => i.value);
      if (!rights.length) return alert('Pick at least one right, or Deny.');
      resolve(Number(b.dataset.grant), 'allow', rights);
    }));

  host.querySelectorAll('[data-allow]').forEach((b) =>
    b.addEventListener('click', () => resolve(Number(b.dataset.allow), 'allow')));
  host.querySelectorAll('[data-deny]').forEach((b) =>
    b.addEventListener('click', () => resolve(Number(b.dataset.deny), 'deny')));
  host.querySelectorAll('[data-goto]').forEach((b) =>
    b.addEventListener('click', () => select(b.dataset.goto, { focusTerminal: true })));
}

function renderMessages() {
  const host = $('messages');
  if (!state.messages.length) {
    host.innerHTML = '<div class="empty">No messages yet.</div>';
    return;
  }
  host.innerHTML = state.messages.slice(-40).reverse().map((m) => {
    // A question carries its outcome once answered. The inbox is replayed on
    // reload, so without showing that, a decided request looks open again.
    if (m.kind === 'question' && m.payload && typeof m.payload === 'object') {
      const decided = m.payload.decision;
      const mark = decided
        ? `<span class="pill ${decided === 'allow' ? 'pill-ok' : 'pill-bad'}">${escapeHtml(decided)}</span>`
        : '<span class="pill pill-warn">waiting</span>';
      return `
        <div class="message${decided ? ' answered' : ''}">
          <div class="from">${escapeHtml(m.from)} asked ${mark}</div>
          <div>${escapeHtml(m.payload.question || '')}</div>
          ${m.payload.reason ? `<div class="muted small">${escapeHtml(m.payload.reason)}</div>` : ''}
        </div>`;
    }
    const body = typeof m.payload === 'string'
      ? m.payload : JSON.stringify(m.payload);
    return `
      <div class="message">
        <div class="from">${escapeHtml(m.from)} → operator</div>
        <div>${escapeHtml(body)}</div>
      </div>`;
  }).join('');
}

// ------------------------------------------------------------------ grid

// Rows shown per tile. Enough to see a prompt plus its last output.
const TILE_ROWS = 14;

// How often the overview refreshes. Deliberately unhurried: this is a glance
// view, not a terminal, and the box running it is also running the agents.
const GRID_POLL_MS = 4000;

// pyte reports colours by name; map them onto the palette the terminal uses so
// a tile and its full-size terminal look like the same program.
const ANSI_COLORS = {
  black: '#22262e', red: '#f85149', green: '#3fb950', brown: '#d29922',
  yellow: '#d29922', blue: '#4c9aff', magenta: '#bc8cff', cyan: '#39c5cf',
  white: '#d7dee8',
  brightblack: '#6e7681', brightred: '#ff7b72', brightgreen: '#56d364',
  brightbrown: '#e3b341', brightyellow: '#e3b341', brightblue: '#79b8ff',
  brightmagenta: '#d2a8ff', brightcyan: '#56d4dd', brightwhite: '#f0f6fc',
};

const cssColor = (name) =>
  ANSI_COLORS[name] || (/^[0-9a-f]{6}$/i.test(name) ? `#${name}` : null);

/** One screen row of styled runs → HTML. */
function renderRow(runs) {
  if (!runs || !runs.length) return '';
  return runs.map((run) => {
    const text = escapeHtml(run.t);
    const styles = [];
    // `reverse` swaps fg and bg — that is how selected items and status bars
    // are drawn, so ignoring it makes them vanish rather than merely lose colour.
    const fg = cssColor(run.r ? (run.b || 'white') : run.f);
    const bg = cssColor(run.r ? (run.f || 'black') : run.b);
    if (fg) styles.push(`color:${fg}`);
    if (bg) styles.push(`background:${bg}`);
    if (run.o) styles.push('font-weight:600');
    return styles.length
      ? `<span style="${styles.join(';')}">${text}</span>`
      : text;
  }).join('');
}

// The set of tiles currently on screen, so a refresh can update their contents
// without rebuilding the DOM. Re-rendering the whole grid every tick made it
// flicker, dropped text selection, and re-bound every listener.
let gridTiles = new Set();
let gridBusy = false;

function syncGridTiles(names) {
  const host = $('grid');
  const wanted = names.join(' ');
  if (wanted === [...gridTiles].join(' ')) return;

  gridTiles = new Set(names);
  if (!names.length) {
    host.innerHTML = '<div class="empty">No agents are running.</div>';
    return;
  }
  host.innerHTML = names.map((name) => `
    <div class="tile" data-name="${escapeHtml(name)}">
      <div class="tile-head">
        <span class="dot running"></span>${escapeHtml(name)}
      </div>
      <pre data-screen="${escapeHtml(name)}">loading…</pre>
    </div>`).join('');

  host.querySelectorAll('.tile').forEach((tile) => {
    tile.addEventListener('click', () =>
      select(tile.dataset.name, { focusTerminal: true }));
  });
}

async function renderGrid() {
  // One tick at a time. If the Pi is busy and a refresh outlasts the interval,
  // stacking more of them only makes it worse.
  if (gridBusy) return;
  gridBusy = true;
  try {
    // Snapshots come from the daemon's pyte model, so a full-screen TUI renders
    // correctly instead of showing a half-replayed redraw. We ask for the
    // *tail*: a shell sits at the bottom of its screen, so the top rows are the
    // least interesting part to put in a small tile.
    const { screens } = await api(`/api/screens?rows=${TILE_ROWS}`);
    syncGridTiles(screens.map((s) => s.container));

    for (const screen of screens) {
      const element = document.querySelector(
        `[data-screen="${CSS.escape(screen.container)}"]`);
      if (!element) continue;
      const html = screen.styled && screen.styled.length
        ? screen.styled.map(renderRow).join('\n')
        : '<span class="muted">(no output yet)</span>';
      // Only touch the DOM when the frame actually changed; an idle agent
      // otherwise costs a full reflow every tick.
      if (element.dataset.frame !== html) {
        element.dataset.frame = html;
        element.innerHTML = html;
      }
    }
  } catch (_) {
    /* a container may exit mid-refresh; the next tick picks it up */
  } finally {
    gridBusy = false;
  }
}


// ------------------------------------------------------------------ audit

async function renderAudit() {
  const denied = $('audit-denied').checked;
  const rows = await api(`/api/audit?limit=150&denied=${denied}`);
  const host = $('audit');
  if (!rows.length) {
    host.innerHTML = '<div class="empty">Nothing logged yet.</div>';
    return;
  }
  host.innerHTML = `
    <table>
      <thead><tr>
        <th>Time</th><th>Actor</th><th>Operation</th><th>Target</th>
        <th>Slot</th><th>Rights</th><th>Result</th>
      </tr></thead>
      <tbody>
        ${rows.map((r) => `
          <tr>
            <td class="muted small">${new Date(r.ts * 1000).toLocaleTimeString()}</td>
            <td class="mono">${escapeHtml(r.actor)}</td>
            <td class="mono">${escapeHtml(r.op)}</td>
            <td class="mono">${escapeHtml(r.target || '')}</td>
            <td class="mono">${r.slot ?? ''}</td>
            <td class="muted small">${escapeHtml(r.rights || '')}</td>
            <td>${r.allowed
              ? '<span class="pill pill-ok">allowed</span>'
              : '<span class="pill pill-bad">denied</span>'}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

// ------------------------------------------------------------------ tabs

let gridTimer = null;

function showTab(name) {
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((p) =>
    p.classList.toggle('active', p.id === `tab-${name}`));

  // Only poll while the overview is actually visible.
  clearInterval(gridTimer);
  gridTimer = null;
  if (name === 'grid') {
    renderGrid();
    gridTimer = setInterval(renderGrid, GRID_POLL_MS);
  }
  if (name === 'audit') renderAudit();
  if (name === 'terminal') setTimeout(syncTerminalSize, 30);
}

// ------------------------------------------------------------------ events

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${proto}//${location.host}/ws/events`);

  socket.onopen = () => {
    $('conn').textContent = 'live';
    $('conn').className = 'pill pill-ok';
  };
  socket.onclose = () => {
    $('conn').textContent = 'reconnecting…';
    $('conn').className = 'pill pill-bad';
    setTimeout(connect, 2000);
  };
  socket.onmessage = (raw) => handleEvent(JSON.parse(raw.data));
}

function handleEvent(event) {
  switch (event.event) {
    case 'overview':
      state.containers = event.containers || [];
      state.tree = event.tree || [];
      state.approvals = event.approvals || [];
      state.messages = event.operator_inbox || [];
      renderTree();
      renderComposeTargets();
      renderApprovals();
      renderMessages();
      if (!state.selected && state.containers.length) select(state.containers[0].name);
      break;

    case 'approval.requested':
      state.approvals.push(event);
      renderApprovals();
      break;

    case 'approval.resolved':
      state.approvals = state.approvals.filter((a) => a.id !== event.id);
      renderApprovals();
      break;

    case 'message':
      if (event.to === 'operator') {
        state.messages.push(event.message);
        renderMessages();
      }
      break;

    case 'container.registered':
    case 'container.started':
    case 'container.exited':
    case 'container.destroyed':
      refreshOverview();
      break;
  }
}

async function refreshOverview() {
  const data = await api('/api/overview');
  state.containers = data.containers;
  state.tree = data.tree;
  state.approvals = data.approvals;
  renderTree();
  renderComposeTargets();
  renderApprovals();
  if (state.selected) loadCaps(state.selected);
}

// ------------------------------------------------------------------ wiring

function wire() {
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.addEventListener('click', () => showTab(b.dataset.tab)));

  $('audit-refresh').addEventListener('click', renderAudit);
  $('audit-denied').addEventListener('change', renderAudit);

  $('compose').addEventListener('submit', async (event) => {
    event.preventDefault();
    const target = $('compose-target').value;
    const message = $('compose-body').value.trim();
    if (!target || !message) return;
    try {
      await api('/api/send', {
        method: 'POST',
        body: JSON.stringify({ target, message }),
      });
      $('compose-body').value = '';
    } catch (err) {
      alert(`Send failed: ${err.message}`);
    }
  });

  const action = async (path, method = 'POST') => {
    if (!state.selected) return;
    try {
      await api(`/api/containers/${state.selected}${path}`, { method });
      await refreshOverview();
      if (path === '/start') setTimeout(() => openTerminal(state.selected), 300);
    } catch (err) {
      alert(err.message);
    }
  };

  $('btn-start').addEventListener('click', () => action('/start'));
  $('btn-stop').addEventListener('click', () => action('/stop'));
  $('btn-interrupt').addEventListener('click', () => action('/signal?sig=2'));

  $('dismiss-finished').addEventListener('click', async () => {
    const names = state.containers.filter((c) => !c.running).map((c) => c.name);
    if (!names.length) return;
    if (!confirm(`Dismiss ${names.length} finished container(s)?\n\n`
                 + names.join(', ') + '\n\nWork on disk is kept.')) return;
    try {
      await api('/api/containers/dismiss-finished', { method: 'POST' });
      if (names.includes(state.selected)) state.selected = null;
      await refreshOverview();
    } catch (err) {
      alert(err.message);
    }
  });

  wireGrant();
}

// A hidden tab should cost nothing; browsers throttle timers but still run them.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(gridTimer);
    gridTimer = null;
  } else if (document.querySelector('#tab-grid.active')) {
    renderGrid();
    gridTimer = setInterval(renderGrid, GRID_POLL_MS);
  }
});

initTheme();
initResizers();
initTerminal();
wire();
connect();
