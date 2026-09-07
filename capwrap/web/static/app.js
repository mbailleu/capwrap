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

const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));

// ------------------------------------------------------------------ theme

/** The one way the theme changes.
 *
 * xterm does not read CSS variables -- it is handed an explicit palette -- so
 * the document attribute and the terminal's colours have to be set together.
 * Anything that sets one without the other leaves a light terminal in a dark
 * interface, or the reverse.
 */
function setTheme(name) {
  document.documentElement.setAttribute('data-theme', name);
  try {
    localStorage.setItem('capwrap-theme', name);
  } catch (_) {
    /* private browsing: the choice just is not remembered */
  }
  if (term) applyTerminalTheme();
}

function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light'
    ? 'light' : 'dark';
}

function initTheme() {
  const saved = localStorage.getItem('capwrap-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  $('theme-toggle').addEventListener('click', () =>
    setTheme(currentTheme() === 'light' ? 'dark' : 'light'));

  // Keep every open tab of the same console in step: the theme is stored
  // per-browser, so a change in one window is a change everywhere.
  window.addEventListener('storage', (event) => {
    if (event.key === 'capwrap-theme' && event.newValue) {
      document.documentElement.setAttribute('data-theme', event.newValue);
      if (term) applyTerminalTheme();
    }
  });
}

function applyTerminalTheme() {
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  term.options.theme = light
    ? { background: '#eceff3', foreground: '#1d2330', cursor: '#0a66d0' }
    : { background: '#0a0d12', foreground: '#d7dee8', cursor: '#4c9aff' };
}

// ------------------------------------------------------------------ identity

/** Put this instance's name in the header and the browser tab.
 *
 * Several capwraps run at once on different ports, one per piece of work. Left
 * unnamed they are all called "capwrap", and picking the right browser tab out
 * of five becomes guesswork.
 */
function applyInstanceName(name) {
  const named = Boolean(name);
  $('instance-name').textContent = named ? name : 'capwrap';
  $('instance-tag').textContent = named
    ? 'capwrap' : 'capability-governed agent containers';
  document.title = named ? `${name} · capwrap` : 'capwrap';
}

// ------------------------------------------------------------------ layout

// Each window edge is a dock, and either side panel can be moved into any of
// them. The middle column is always the content pane, so a panel is described
// entirely by which edge it sits on and, when it shares one, in what order.
const EDGES = ['top', 'left', 'right', 'bottom'];
const PANEL_IDS = ['containers', 'inbox'];

const LAYOUT_DEFAULT = {
  docks: { containers: 'left', inbox: 'right' },
  order: ['containers', 'inbox'],
  sizes: { left: 260, right: 320, top: 220, bottom: 220 },
};

// A dock must not be able to eat the window: the content pane is the point of
// the layout, and a dock dragged to the far edge cannot be dragged back.
const DOCK_MIN = 150;
const dockMax = (edge) => Math.max(
  DOCK_MIN,
  Math.round((edge === 'top' || edge === 'bottom'
    ? window.innerHeight : window.innerWidth) * 0.75),
);

// A badge each panel keeps beside its name. Only ever something small: the bar
// also holds the dock control, and in a 260px panel there is no room for a
// button with words on it.
const PANEL_EXTRA = { inbox: 'approval-count' };

const DOCK_BUTTONS = {
  top: { glyph: '▲', title: 'Move to the top' },
  left: { glyph: '◀', title: 'Move to the left' },
  right: { glyph: '▶', title: 'Move to the right' },
  bottom: { glyph: '▼', title: 'Move to the bottom' },
};

let layout = loadLayout();

function loadLayout() {
  const fallback = {
    docks: { ...LAYOUT_DEFAULT.docks },
    order: [...LAYOUT_DEFAULT.order],
    sizes: { ...LAYOUT_DEFAULT.sizes },
  };
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem('capwrap-layout') || 'null');
  } catch (_) {
    saved = null;
  }
  if (!saved || typeof saved !== 'object') return fallback;

  // Field by field, because a layout read back from storage was written by an
  // older version of this file as often as not, and a half-understood one
  // should degrade to the default rather than throw during startup.
  for (const id of PANEL_IDS) {
    const edge = saved.docks && saved.docks[id];
    if (EDGES.includes(edge)) fallback.docks[id] = edge;
  }
  if (Array.isArray(saved.order)) {
    const kept = saved.order.filter((id) => PANEL_IDS.includes(id));
    fallback.order = [...new Set([...kept, ...PANEL_IDS])];
  }
  for (const edge of EDGES) {
    const size = Number(saved.sizes && saved.sizes[edge]);
    if (Number.isFinite(size) && size > 0) fallback.sizes[edge] = size;
  }
  return fallback;
}

function saveLayout() {
  try {
    localStorage.setItem('capwrap-layout', JSON.stringify(layout));
  } catch (_) {
    /* private browsing: the layout just is not remembered */
  }
}

function buildDocks() {
  const host = $('layout');
  const content = $('content');

  const dock = (edge) => {
    const el = document.createElement('div');
    el.className = 'dock';
    el.dataset.edge = edge;
    return el;
  };

  const splitter = (edge) => {
    const el = document.createElement('div');
    el.className = 'resizer';
    el.dataset.edge = edge;
    el.dataset.axis = (edge === 'top' || edge === 'bottom') ? 'y' : 'x';
    el.setAttribute('role', 'separator');
    el.setAttribute('tabindex', '0');
    el.setAttribute('aria-label', `Resize the ${edge} panel area`);
    el.title = 'Drag to resize · double-click to reset';
    wireResizer(el, edge);
    return el;
  };

  // A row nested inside a column: that is what lets the top and bottom docks
  // span the whole width while the left and right ones flank the content.
  const mid = document.createElement('div');
  mid.className = 'layout-mid';
  host.append(dock('top'), splitter('top'), mid, splitter('bottom'), dock('bottom'));
  mid.append(dock('left'), splitter('left'), content, splitter('right'), dock('right'));
}

function dockElement(edge) {
  return $('layout').querySelector(`.dock[data-edge="${edge}"]`);
}

function applyLayout() {
  for (const edge of EDGES) {
    const dock = dockElement(edge);
    const resizer = $('layout').querySelector(`.resizer[data-edge="${edge}"]`);
    const here = layout.order.filter((id) => layout.docks[id] === edge);
    // append() moves an element that already has a parent, so this both places
    // the panels and puts them in the order the operator asked for.
    for (const id of here) dock.append($(`panel-${id}`));
    dock.hidden = here.length === 0;
    resizer.hidden = here.length === 0;
    setDockSize(edge, layout.sizes[edge]);
  }
  for (const id of PANEL_IDS) renderPanelBar(id);
  saveLayout();
  requestAnimationFrame(syncTerminalSize);
}

function setDockSize(edge, px) {
  const size = Math.round(clamp(px, DOCK_MIN, dockMax(edge)));
  layout.sizes[edge] = size;
  document.documentElement.style.setProperty(`--dock-${edge}`, `${size}px`);
  return size;
}

function renderPanelBar(id) {
  const panel = $(`panel-${id}`);
  let bar = panel.querySelector(':scope > .panel-bar');
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'panel-bar';
    panel.prepend(bar);
  }

  const here = layout.docks[id];
  const shared = PANEL_IDS.some(
    (other) => other !== id && layout.docks[other] === here,
  );

  // Detach the badge before the rewrite. It is a live element that other
  // renderers write to by id, and `innerHTML =` would destroy it -- after which
  // every later lookup finds nothing and the badge is gone for good.
  const extra = PANEL_EXTRA[id] && $(PANEL_EXTRA[id]);
  if (extra) extra.remove();

  bar.innerHTML = `
    <span class="panel-name">${escapeHtml(panel.dataset.title)}</span>
    <span data-extra></span>
    <span class="spacer"></span>
    <div class="dock-picker">
      ${EDGES.map((edge) => `
        <button type="button" data-dock="${edge}"
                class="${edge === here ? 'here' : ''}"
                ${edge === here ? 'disabled' : ''}
                title="${escapeHtml(DOCK_BUTTONS[edge].title)}"
        >${DOCK_BUTTONS[edge].glyph}</button>`).join('')}
      ${shared ? `
        <button type="button" data-swap
                title="Swap the order of the two panels on this edge">⇅</button>` : ''}
    </div>`;

  // Moved rather than duplicated, so the elements the renderers already write
  // to keep working wherever the bar happens to be.
  if (extra) bar.querySelector('[data-extra]').append(extra);

  bar.querySelectorAll('[data-dock]').forEach((button) =>
    button.addEventListener('click', () => {
      if (layout.docks[id] === button.dataset.dock) return;
      layout.docks[id] = button.dataset.dock;
      applyLayout();
    }));

  const swap = bar.querySelector('[data-swap]');
  if (swap) {
    swap.addEventListener('click', () => {
      layout.order.reverse();
      applyLayout();
    });
  }
}

function wireResizer(handle, edge) {
  const axis = (edge === 'top' || edge === 'bottom') ? 'y' : 'x';

  // Measured against the dock's own rectangle, so dragging works the same
  // whichever edge the panel has been moved to.
  const sizeAt = (rect, event) => {
    if (edge === 'left') return event.clientX - rect.left;
    if (edge === 'right') return rect.right - event.clientX;
    if (edge === 'top') return event.clientY - rect.top;
    return rect.bottom - event.clientY;
  };

  handle.addEventListener('pointerdown', (event) => {
    const dock = dockElement(edge);
    if (!dock || dock.hidden) return;
    event.preventDefault();
    // Capture on the handle so the drag survives the pointer crossing the
    // terminal, which would otherwise swallow the move events.
    handle.setPointerCapture(event.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add('resizing', `resizing-${axis}`);
    const rect = dock.getBoundingClientRect();

    const onMove = (move) => {
      setDockSize(edge, sizeAt(rect, move));
      // Reflow the terminal as the edge moves, so it tracks the drag instead
      // of snapping when you let go.
      requestAnimationFrame(fitTerminal);
    };

    const onUp = () => {
      handle.releasePointerCapture(event.pointerId);
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing', `resizing-${axis}`);
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', onUp);
      handle.removeEventListener('pointercancel', onUp);
      saveLayout();
      // The PTY only needs telling once, at the end.
      syncTerminalSize();
    };

    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', onUp);
    handle.addEventListener('pointercancel', onUp);
  });

  handle.addEventListener('dblclick', () => {
    setDockSize(edge, LAYOUT_DEFAULT.sizes[edge]);
    saveLayout();
    syncTerminalSize();
  });

  // Keyboard access, so the layout is not mouse-only.
  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 40 : 10;
    // Growing a left or top dock means moving the handle towards the far edge;
    // for right and bottom it is the other way round.
    const grow = (edge === 'left' || edge === 'top') ? 1 : -1;
    const smaller = axis === 'x' ? 'ArrowLeft' : 'ArrowUp';
    const bigger = axis === 'x' ? 'ArrowRight' : 'ArrowDown';

    if (event.key === smaller) setDockSize(edge, layout.sizes[edge] - step * grow);
    else if (event.key === bigger) setDockSize(edge, layout.sizes[edge] + step * grow);
    else if (event.key === 'Home') setDockSize(edge, LAYOUT_DEFAULT.sizes[edge]);
    else return;

    event.preventDefault();
    saveLayout();
    syncTerminalSize();
  });
}

function initLayout() {
  buildDocks();
  applyLayout();

  $('layout-reset').addEventListener('click', () => {
    layout = {
      docks: { ...LAYOUT_DEFAULT.docks },
      order: [...LAYOUT_DEFAULT.order],
      sizes: { ...LAYOUT_DEFAULT.sizes },
    };
    applyLayout();
  });

  // A window that shrinks can leave a dock over its share of it; re-clamping
  // keeps the content pane from disappearing entirely.
  window.addEventListener('resize', () => {
    for (const edge of EDGES) setDockSize(edge, layout.sizes[edge]);
  });
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

// ------------------------------------------------------------------ compose

// Who the composer will send to. Held outside `state` because it is a UI
// selection rather than daemon truth, and it has to survive every re-render
// the event stream provokes.
const composeTargets = new Set();
let composeSeeded = false;

function renderComposeTargets() {
  const host = $('compose-targets');
  const names = state.containers.map((c) => c.name);

  // A container that has been dismissed must not stay quietly selected and
  // then fail the send with a 404 the operator cannot explain.
  for (const name of [...composeTargets]) {
    if (!names.includes(name)) composeTargets.delete(name);
  }
  if (!composeSeeded && names.length) {
    composeSeeded = true;
    composeTargets.add(names.includes(state.selected) ? state.selected : names[0]);
  }

  if (!names.length) {
    host.innerHTML = '<div class="empty">No containers to send to.</div>';
    updateComposeButton();
    return;
  }

  const all = names.length > 1 && names.every((n) => composeTargets.has(n));
  host.innerHTML = `
    ${names.length > 1 ? `
      <label class="check all${all ? ' on' : ''}">
        <input type="checkbox" data-all ${all ? 'checked' : ''}> all
      </label>` : ''}
    ${names.map((name) => {
      const on = composeTargets.has(name);
      return `
        <label class="check${on ? ' on' : ''}">
          <input type="checkbox" value="${escapeHtml(name)}" ${on ? 'checked' : ''}>
          ${escapeHtml(name)}
        </label>`;
    }).join('')}`;

  host.querySelectorAll('input[value]').forEach((box) =>
    box.addEventListener('change', () => {
      if (box.checked) composeTargets.add(box.value);
      else composeTargets.delete(box.value);
      renderComposeTargets();
    }));

  const every = host.querySelector('[data-all]');
  if (every) {
    every.addEventListener('change', () => {
      if (every.checked) names.forEach((n) => composeTargets.add(n));
      else composeTargets.clear();
      renderComposeTargets();
    });
  }
  updateComposeButton();
}

function updateComposeButton() {
  const button = $('compose-send');
  const count = composeTargets.size;
  button.disabled = count === 0;
  button.textContent = count > 1
    ? `Broadcast to ${count} agents` : 'Send as operator';
}

// ------------------------------------------------------------------ terminal

// Width to assume for an overlay scrollbar, which occupies no layout space and
// therefore measures zero however you ask.
const OVERLAY_GUTTER = 12;

function initTerminal() {
  term = new Terminal({
    fontFamily: 'ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace',
    fontSize: 13,
    cursorBlink: true,
    // Deep, because the daemon retains megabytes per container and replays all
    // of it on connect -- a shallow client buffer would throw that away again
    // on arrival.
    scrollback: 100000,
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

/** Keep the scrollbar out of the last column, whichever kind the browser draws.
 *
 * xterm is supposed to do this itself -- FitAddon subtracts the viewport's
 * scrollbar width when it works out how many columns fit -- but the width it
 * measures internally comes back as zero here even with a classic 10px
 * scrollbar drawn, so the last column ends up underneath it and a full-screen
 * TUI has its right-hand border painted over. Claude Code draws its boxes right
 * at that edge, so it is the first thing to go.
 *
 * Reserving the lane as padding on .xterm is the fix that does not depend on
 * that measurement being right: FitAddon subtracts this padding too, and the
 * viewport is absolutely positioned against the padding box, so the scrollbar
 * stays where it was and only the text stops short of it.
 */
function reserveScrollbarGutter() {
  if (!term || !term.element) return;
  const viewport = term.element.querySelector('.xterm-viewport');
  if (!viewport) return;
  term.element.style.paddingRight = `${scrollbarLane(viewport)}px`;
}

/** How wide the scrollbar's lane is. */
function scrollbarLane(viewport) {
  // Zero means an overlay scrollbar, drawn on top of the content rather than
  // beside it -- so it still needs a lane, it just cannot be measured.
  return (viewport.offsetWidth - viewport.clientWidth) || OVERLAY_GUTTER;
}

/** Widen the gutter by however much of the last column is still under it.
 *
 * xterm rounds its screen up to whole cells, so reserving exactly the
 * scrollbar's width can still leave a sliver of the final column beneath the
 * thumb. Returns whether anything changed, so the caller knows to lay out again.
 */
function trimGutterOverlap() {
  const viewport = term.element.querySelector('.xterm-viewport');
  const screen = term.element.querySelector('.xterm-screen');
  if (!viewport || !screen) return false;

  const lane = scrollbarLane(viewport);
  const overlap = screen.getBoundingClientRect().right
    - (viewport.getBoundingClientRect().right - lane);
  if (overlap <= 0) return false;

  const current = parseFloat(getComputedStyle(term.element).paddingRight) || 0;
  term.element.style.paddingRight = `${Math.ceil(current + overlap)}px`;
  return true;
}

/** Reflow xterm to its container. Cheap enough to call during a drag. */
function fitTerminal() {
  if (!fitAddon) return;
  // Reserve, lay out, then correct for the rounding and lay out once more. The
  // reservation is recomputed from scratch every time rather than accumulated,
  // so the second pass cannot drift wider on each call.
  reserveScrollbarGutter();
  try {
    fitAddon.fit();
    if (trimGutterOverlap()) fitAddon.fit();
  } catch (_) {
    /* panel not visible yet */
  }
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
    const extra = detail.pattern || detail.path || detail.state
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

/** A capability request: answered by granting, not merely by saying yes. */
function capabilityRequestCard(approval, ctx) {
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
        ${explainBlock(approval)}
      </div>
    </div>`;
}

/** The agent is asking its human something, via Claude's AskUserQuestion.
 *
 * Allowing this does not answer anything: it lets the tool run, and the picker
 * is then drawn in that agent's own terminal, where someone has to work through
 * it with the arrow keys. So the card shows what was asked and makes going
 * there the primary action, rather than offering an allow/deny pair that looks
 * like it settles the question and does not.
 */
function userQuestionCard(approval, ctx) {
  const blocks = (ctx.questions || []).map((q) => `
    <div class="ask">
      ${q.header ? `<div class="ask-header">${escapeHtml(q.header)}</div>` : ''}
      <div class="ask-q">${escapeHtml(q.question)}</div>
      ${(q.options || []).map((o) => `
        <div class="ask-option">
          <span class="label">${escapeHtml(o.label)}</span>
          <span class="desc">${escapeHtml(o.description)}</span>
        </div>`).join('')}
      ${q.multi_select
        ? '<div class="muted small">more than one answer allowed</div>' : ''}
    </div>`).join('');

  return `
    <div class="approval question">
      <div class="who">${escapeHtml(approval.container)} · asking you</div>
      ${blocks || `<div class="q">${escapeHtml(approval.question)}</div>`}
      <div class="note">
        A question, not a permission. Answering it means going to
        ${escapeHtml(approval.container)}'s terminal and picking there.
      </div>
      <div class="actions">
        <button class="primary small" data-answer="${approval.id}"
                data-container="${escapeHtml(approval.container)}">
          Answer in the terminal
        </button>
        <button class="small danger" data-deny="${approval.id}">Deny</button>
        ${explainBlock(approval)}
      </div>
    </div>`;
}

// Explanations, kept out of `state` because they are the operator's working
// notes on a card rather than daemon truth, and they have to survive the
// re-render every event provokes.
const explanations = new Map();

/** The Explain block for one card: a button, or what came back from it. */
function explainBlock(approval) {
  const held = explanations.get(approval.id);
  if (!held) {
    return `<button class="ghost small" data-explain="${approval.id}">Explain</button>`;
  }
  if (held.pending) {
    // The explainer uses the asking agent's own harness, so the label stays
    // agent-neutral; the result block below names the model that answered.
    return '<div class="explain pending">asking the agent\'s model…</div>';
  }
  if (held.error) {
    return `<div class="explain error">${escapeHtml(held.error)}</div>`;
  }
  return `
    <div class="explain">
      <div class="explain-note">
        ${escapeHtml(held.model)}'s reading of this request. It is advice about
        untrusted input, not a verdict — the decision is still yours.
      </div>
      <div class="explain-body">${escapeHtml(held.text)}</div>
    </div>`;
}

async function explainApproval(id) {
  explanations.set(id, { pending: true });
  renderApprovals();
  try {
    const result = await api(`/api/approvals/${id}/explain`, { method: 'POST' });
    explanations.set(id, { text: result.text, model: result.model });
  } catch (err) {
    explanations.set(id, { error: err.message });
  }
  renderApprovals();
}

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
    if (ctx.kind === 'capability_request') return capabilityRequestCard(approval, ctx);
    if (ctx.kind === 'user_question') return userQuestionCard(approval, ctx);

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
          ${explainBlock(approval)}
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

  host.querySelectorAll('[data-answer]').forEach((b) =>
    b.addEventListener('click', async () => {
      const container = b.dataset.container;
      // Allow first, then jump: the picker is then already being drawn when the
      // terminal comes up, instead of appearing a beat later on a blank screen.
      await resolve(Number(b.dataset.answer), 'allow');
      select(container, { focusTerminal: true });
    }));

  host.querySelectorAll('[data-allow]').forEach((b) =>
    b.addEventListener('click', () => resolve(Number(b.dataset.allow), 'allow')));
  host.querySelectorAll('[data-deny]').forEach((b) =>
    b.addEventListener('click', () => resolve(Number(b.dataset.deny), 'deny')));
  host.querySelectorAll('[data-goto]').forEach((b) =>
    b.addEventListener('click', () => select(b.dataset.goto, { focusTerminal: true })));
  host.querySelectorAll('[data-explain]').forEach((b) =>
    b.addEventListener('click', () => explainApproval(Number(b.dataset.explain))));
}

// A question that has been decided -- allowed, denied, timed out, or abandoned
// because its asker went away.
function isDecidedQuestion(message) {
  return message.kind === 'question'
    && message.payload
    && typeof message.payload === 'object'
    && Boolean(message.payload.decision);
}

// Whether to show those. Off by default: the inbox is replayed in full on every
// connect, so without this the panel fills up with old settled requests each
// time the socket comes back, and it takes reading each one to work out that
// none of them want anything.
let showAnsweredQuestions = false;

function renderMessages() {
  const host = $('messages');
  const decided = state.messages.filter(isDecidedQuestion);
  const visible = showAnsweredQuestions
    ? state.messages
    : state.messages.filter((m) => !isDecidedQuestion(m));

  const toggle = $('inbox-history');
  toggle.hidden = decided.length === 0;
  toggle.textContent = showAnsweredQuestions
    ? `hide ${decided.length} answered` : `show ${decided.length} answered`;

  if (!visible.length) {
    host.innerHTML = decided.length
      ? '<div class="empty">Nothing outstanding.</div>'
      : '<div class="empty">No messages yet.</div>';
    return;
  }
  const live = new Set(state.containers.map((c) => c.name));

  host.innerHTML = visible.slice(-40).reverse().map((m) => {
    // A question carries its outcome once answered. The inbox is replayed on
    // reload, so without showing that, a decided request looks open again.
    if (m.kind === 'question' && m.payload && typeof m.payload === 'object') {
      const decided = m.payload.decision;
      const mark = decided
        ? `<span class="pill ${decided === 'allow' ? 'pill-ok' : 'pill-bad'}">${escapeHtml(decided)}</span>`
        : '<span class="pill pill-warn">waiting</span>';
      // Going to the asker's terminal is the answer to half of these, so the
      // way there belongs on the entry rather than only on the live card.
      const open = live.has(m.from) ? `
        <div class="actions">
          <button class="ghost small" data-goto="${escapeHtml(m.from)}"
          >Open ${escapeHtml(m.from)}</button>
        </div>` : '';
      return `
        <div class="message${decided ? ' answered' : ''}">
          <div class="from">${escapeHtml(m.from)} asked ${mark}</div>
          <div>${escapeHtml(m.payload.question || '')}</div>
          ${m.payload.reason ? `<div class="muted small">${escapeHtml(m.payload.reason)}</div>` : ''}
          ${open}
        </div>`;
    }
    const body = typeof m.payload === 'string'
      ? m.payload : JSON.stringify(m.payload);
    return `
      <div class="message">
        <div class="from">${escapeHtml(m.from)} → operator ${signedBadge(m)}</div>
        <div>${escapeHtml(body)}</div>
      </div>`;
  }).join('');

  host.querySelectorAll('[data-goto]').forEach((b) =>
    b.addEventListener('click', () => select(b.dataset.goto, { focusTerminal: true })));
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
  const wanted = names.join(' ');
  if (wanted === [...gridTiles].join(' ')) return;

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

// ------------------------------------------------------------------ boards

/** Shared boards, and who may read or write each one.
 *
 * The holder list is the point of this view. A board is somewhere several
 * agents meet, so "who can post here and who can only read" is the question an
 * operator actually has, and it cannot be answered from any one container's
 * capability table.
 */
async function renderBoards() {
  const host = $('boards');
  let boards = [];
  try {
    ({ boards } = await api('/api/boards?limit=50'));
  } catch (err) {
    host.innerHTML = `<div class="empty">Could not load boards: ${escapeHtml(err.message)}</div>`;
    return;
  }

  $('boards-count').textContent = boards.length
    ? `${boards.length} board${boards.length === 1 ? '' : 's'}` : '';

  if (!boards.length) {
    host.innerHTML = `
      <div class="trace-off">
        No boards yet. An agent holding a factory capability creates one with
        <span class="mono">capctl board create &lt;factory-slot&gt; &lt;topic&gt;</span>,
        then hands each worker as much of it as they need — posting and reading
        are separate rights, so a worker can report progress without reading its
        peers' notes, or follow along without being able to speak.
      </div>`;
    return;
  }

  host.innerHTML = boards.map((board) => {
    const holders = (board.holders || []).map((h) => {
      const may = [h.may_post ? 'post' : null, h.may_read ? 'read' : null]
        .filter(Boolean).join(' + ') || 'neither';
      return `<span class="right" title="${escapeHtml(h.rights.join(', '))}"
        >${escapeHtml(h.container)}: ${may}</span>`;
    }).join('') || '<span class="muted small">nobody but the operator</span>';

    const posts = (board.recent || []).slice().reverse().map((p) => `
      <tr>
        <td class="mono">#${p.id}</td>
        <td class="mono">${escapeHtml(p.from)} ${signedBadge(p)}</td>
        <td class="muted small">${new Date(p.ts * 1000).toLocaleTimeString()}</td>
        <td><pre class="trace-payload">${escapeHtml(payloadText(p.payload))}</pre></td>
      </tr>`).join('');

    return `
      <div class="cap-group">
        <h3>${escapeHtml(board.topic)}</h3>
        <p class="muted small">
          created by ${escapeHtml(board.created_by || '?')} ·
          ${board.posts} post${board.posts === 1 ? '' : 's'}
        </p>
        <div class="rights-picker">${holders}</div>
        ${posts ? `<table><tbody>${posts}</tbody></table>`
                : '<div class="empty">Nothing posted yet.</div>'}
      </div>`;
  }).join('');
}

// ------------------------------------------------------------------ trace

// Rows held in the browser. The daemon keeps its own bounded buffer; this is
// only what is on screen.
const TRACE_VIEW_LIMIT = 300;

const trace = { enabled: false, rows: [], from: '', to: '' };

/** The rows currently on show, after the sender/recipient filters. */
function tracedRows() {
  return trace.rows.filter((r) =>
    (!trace.from || r.from === trace.from) && (!trace.to || r.to === trace.to));
}

/** Keep the two filter dropdowns offering whoever has actually appeared.
 *
 * Built from the trace itself as well as the container list, because `root` is
 * a sender the operator will want to filter on and is not a container, and a
 * container that has since been dismissed may still be in the rows.
 */
function renderTraceFilters() {
  const names = new Set();
  for (const row of trace.rows) { names.add(row.from); names.add(row.to); }
  for (const c of state.containers) names.add(c.name);

  for (const [id, key, anyone] of [
    ['trace-from', 'from', 'anyone'], ['trace-to', 'to', 'anyone'],
  ]) {
    const select = $(id);
    const options = ['', ...[...names].sort()];
    const markup = options.map((name) => `
      <option value="${escapeHtml(name)}" ${name === trace[key] ? 'selected' : ''}
      >${name ? escapeHtml(name) : anyone}</option>`).join('');
    if (select.innerHTML !== markup) select.innerHTML = markup;
  }
}

/** How a signature is shown: a badge, and never a claim capwrap cannot back.
 *
 * The console reports what the kernel verified when the message was accepted --
 * a signature that did not check out was refused, so anything stored as signed
 * has been checked once. It says "signed by" rather than "genuine": the key is
 * the container's, and what it proves is that the post came from that container,
 * not that its contents are true.
 */
function signedBadge(entry) {
  if (!entry || !entry.signed) return '';
  const who = (entry.public_key || '').slice(0, 16);
  return `<span class="right signed" title="Signed by ${escapeHtml(entry.from)}, key ${escapeHtml(who)}">signed</span>`;
}

const payloadText = (payload) =>
  typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);

async function loadTrace() {
  try {
    const data = await api(`/api/messages?limit=${TRACE_VIEW_LIMIT}`);
    trace.enabled = Boolean(data.enabled);
    trace.rows = data.messages || [];
  } catch (_) {
    trace.rows = [];
  }
  renderTrace();
}

function renderTrace() {
  $('trace-toggle').checked = trace.enabled;
  renderTraceFilters();

  const rows = tracedRows();
  const filtered = trace.from || trace.to;
  $('trace-state').textContent = !trace.enabled ? ''
    : filtered ? `${rows.length} of ${trace.rows.length} recorded`
    : `${trace.rows.length} recorded`;

  const host = $('trace');
  if (!trace.enabled) {
    host.innerHTML = `
      <div class="trace-off">
        Off. Switch it on to record every message the kernel delivers between
        containers, payloads included — which is what you need when two agents
        are talking past each other, and is exactly why it is not on by default.
        The audit tab already records that a message was sent, and by whom.
      </div>`;
    return;
  }
  if (!rows.length) {
    host.innerHTML = trace.rows.length
      ? '<div class="empty">Nothing matches that filter.</div>'
      : '<div class="empty">Nothing has been sent yet.</div>';
    return;
  }

  host.innerHTML = `
    <table>
      <thead><tr>
        <th>Time</th><th>From</th><th>To</th><th>Kind</th><th>Slot</th><th>Payload</th>
      </tr></thead>
      <tbody>
        ${rows.slice(-TRACE_VIEW_LIMIT).reverse().map((r) => `
          <tr>
            <td class="muted small">${new Date(r.ts * 1000).toLocaleTimeString()}</td>
            <td class="mono">${escapeHtml(r.from)} ${signedBadge(r)}</td>
            <td class="mono">${escapeHtml(r.to)}</td>
            <td>${escapeHtml(r.kind)}</td>
            <td class="mono">${r.via_slot ?? ''}</td>
            <td><pre class="trace-payload">${escapeHtml(payloadText(r.payload))}</pre></td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

const traceVisible = () => Boolean(document.querySelector('#tab-messages.active'));

function wireTrace() {
  $('trace-toggle').addEventListener('change', async () => {
    const wanted = $('trace-toggle').checked;
    try {
      const result = await api('/api/trace', {
        method: 'POST',
        body: JSON.stringify({ enabled: wanted }),
      });
      trace.enabled = result.enabled;
      if (!trace.enabled) trace.rows = [];
    } catch (err) {
      alert(`Could not change tracing: ${err.message}`);
    }
    renderTrace();
  });

  for (const [id, key] of [['trace-from', 'from'], ['trace-to', 'to']]) {
    $(id).addEventListener('change', () => {
      trace[key] = $(id).value;
      renderTrace();
    });
  }

  $('trace-clear').addEventListener('click', async () => {
    try {
      await api('/api/messages', { method: 'DELETE' });
    } catch (err) {
      alert(`Could not clear the trace: ${err.message}`);
      return;
    }
    trace.rows = [];
    renderTrace();
  });
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
  if (name === 'boards') renderBoards();
  if (name === 'messages') loadTrace();
  if (name === 'terminal') setTimeout(syncTerminalSize, 30);
}

// ------------------------------------------------------------------ events

let eventSocket = null;
let reconnectTimer = null;

function connect() {
  // One socket, ever. A page that suspends and resumes can fire `onclose` more
  // than once, and every extra socket would deliver its own copy of every
  // event — which is one of the ways an approval ends up on screen twice.
  if (eventSocket
      && (eventSocket.readyState === WebSocket.OPEN
          || eventSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  clearTimeout(reconnectTimer);
  reconnectTimer = null;

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${proto}//${location.host}/ws/events`);
  eventSocket = socket;

  socket.onopen = () => {
    $('conn').textContent = 'live';
    $('conn').className = 'pill pill-ok';
  };
  socket.onclose = () => {
    if (eventSocket !== socket) return;    // already superseded
    eventSocket = null;
    $('conn').textContent = 'reconnecting…';
    $('conn').className = 'pill pill-bad';
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  };
  socket.onmessage = (raw) => handleEvent(JSON.parse(raw.data));
}

function handleEvent(event) {
  switch (event.event) {
    case 'overview':
      // Sent on every connect, so this is also what puts the queue straight
      // after a reconnect: anything the daemon has since retired is simply not
      // in the list any more.
      applyInstanceName(event.instance || '');
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
      // The overview that arrives on connect already carries every pending
      // approval, so an event racing it must not add a second card.
      if (!state.approvals.some((a) => a.id === event.id)) {
        state.approvals.push(event);
        renderApprovals();
      }
      break;

    case 'approval.resolved':
      state.approvals = state.approvals.filter((a) => a.id !== event.id);
      explanations.delete(event.id);
      // The inbox keeps its own copy of the question as history. Stamp the
      // outcome on it as well, or a question that has just been answered goes
      // on reading as one still waiting until the next reload.
      for (const message of state.messages) {
        if (message.kind === 'question'
            && message.payload && message.payload.id === event.id) {
          message.payload.decision = event.decision;
        }
      }
      renderApprovals();
      renderMessages();
      break;

    case 'message':
      if (event.to === 'operator') {
        state.messages.push(event.message);
        renderMessages();
      }
      break;

    case 'board.posted':
      if (document.querySelector('#tab-boards.active')) renderBoards();
      break;

    case 'message.trace':
      trace.rows.push(event.record);
      if (trace.rows.length > TRACE_VIEW_LIMIT * 2) {
        trace.rows.splice(0, trace.rows.length - TRACE_VIEW_LIMIT);
      }
      if (traceVisible()) renderTrace();
      break;

    case 'trace.changed':
      trace.enabled = Boolean(event.enabled);
      if (!trace.enabled) trace.rows = [];
      if (traceVisible()) renderTrace();
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
  applyInstanceName(data.instance || '');
  state.containers = data.containers;
  state.tree = data.tree;
  state.approvals = data.approvals;
  state.messages = data.operator_inbox || state.messages;
  renderTree();
  renderComposeTargets();
  renderApprovals();
  renderMessages();
  if (state.selected) loadCaps(state.selected);
}

// ------------------------------------------------------------------ wiring

function wire() {
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.addEventListener('click', () => showTab(b.dataset.tab)));

  $('audit-refresh').addEventListener('click', renderAudit);
  $('boards-refresh').addEventListener('click', renderBoards);

  $('inbox-history').addEventListener('click', () => {
    showAnsweredQuestions = !showAnsweredQuestions;
    renderMessages();
  });
  $('audit-denied').addEventListener('change', renderAudit);

  $('compose').addEventListener('submit', async (event) => {
    event.preventDefault();
    const targets = [...composeTargets];
    const message = $('compose-body').value.trim();
    if (!targets.length || !message) return;
    try {
      await api('/api/send', {
        method: 'POST',
        body: JSON.stringify({ targets, message }),
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
  wireTrace();
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
initLayout();
initTerminal();
wire();
connect();
