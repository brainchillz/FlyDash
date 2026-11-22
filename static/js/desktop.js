// ─── FlyDash: the desktop shell ──────────────────────────────────────
//
// Loaded LAST. Replaces the sidebar-and-single-page shell with a desktop:
// wallpaper, desktop icons, a widget, floating app windows, a launcher and
// a taskbar. An "app" is a group of the existing pages (the `apps` list in
// GET /api/modules/nav: Storage, Sharing, SAN, Settings, …); each app window
// has a rail of its sections on the left and the page on the right.
//
// The pages themselves are unchanged: every page_<id>() still writes to
// $('page-content'). core.js routes that lookup through _pageHost(), which
// this file points at the pane of the window being rendered into (or, after
// a render completes, the focused window — so a page's own button handlers
// keep writing into their own window).
//
// Functions redefined here (same global scope, later definition wins):
// showLogin, showApp, showPage, renderNav, applyModuleVisibility,
// renderMachineStrip, doLogout. Everything else in core.js is used as is.

var _pageHost = null;    // installed below; core.js `$` consults it

const DESK = {
  allApps: [],          // every app in the manifest (+ Overview), with every section
  wins: {},             // appId -> window record
  order: [],            // z-order, bottom → top (minimized windows stay in it)
  active: null,         // appId of the focused window
  renderWin: null,      // window a page is rendering into right now
  launcher: false,
  menu: false,
  sel: null,            // selected desktop icon
  wallpapers: null,     // {wallpapers:[…], default:{wallpaper}} from /api/wallpaper
  res: null,            // last /api/system/resources
  gpu: null,            // last /api/gpu (null when the module is off)
  gpuAbsent: false,     // node answered available:false — poll rarely
  summary: null,        // last /api/summary (alerts, host identity)
  restored: false,
};
const DESK_GEO_KEY = 'desk.geo.v1';
const DESK_OPEN_KEY = 'desk.open.v1';
const DESK_WALL_KEY = 'desk.wallpaper.v1' + (typeof THEME_BASE === 'string' && THEME_BASE ? ':' + THEME_BASE : '');
const DESK_MIN_W = 420, DESK_MIN_H = 280;
const RE_WALL_URL = /^\/(static\/wallpapers|wallpaper-assets)\/[A-Za-z0-9._-]+(\?v=[A-Za-z0-9]+)?$/;

function deskPageHost() {
  if (DESK.renderWin) return DESK.renderWin.innerEl;
  const w = DESK.active && DESK.wins[DESK.active];
  return w ? w.innerEl : null;
}
_pageHost = deskPageHost;

function deskApp(id) { return DESK.allApps.find(a => a.id === id) || null; }
function deskArea() {
  const el = $('desk-windows');
  return el ? { w: el.clientWidth, h: el.clientHeight } : { w: window.innerWidth, h: window.innerHeight - 56 };
}
function deskStore(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) {} }
function deskLoad(key) { try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; } }

// ─── Apps from the manifest ─────────────────────────────
// Sections a viewer may see: module enabled (or core page) and not admin-only
// for a read-only account. An app with no visible section is hidden from the
// desktop, launcher and taskbar; it shows dimmed in the launcher's
// "Not enabled" row when every section is merely toggled off.
function deskBuildApps(man) {
  const apps = (((man || {}).nav || {}).apps || [])
    .filter(a => NAV_ID_RE.test(String(a.id || '')))
    .map(a => ({ id: a.id, label: a.label || a.id, icon: a.icon, order: a.order || 1000,
                 sections: (a.sections || []).filter(s => PAGE_ID_RE.test(String(s.page || ''))) }));
  apps.unshift({ id: 'overview', label: 'Overview', icon: 'grid', order: 0,
                 sections: [{ page: 'dashboard', label: 'Dashboard', icon: 'grid', module: null, admin_only: false, group: null }] });
  // Keep ad-hoc windows (showPage on a page no app claims) across manifest refreshes.
  DESK.allApps.filter(a => a.transient).forEach(a => apps.push(a));
  DESK.allApps = apps;
}
function deskVisibleSections(app) {
  const ro = currentRole !== 'admin';
  return (app.sections || []).filter(s =>
    (!s.module || moduleEnabled[s.module] !== false) && !(ro && s.admin_only));
}
function deskVisibleApps() { return DESK.allApps.filter(a => !a.transient && deskVisibleSections(a).length); }
function deskDisabledApps() {
  return DESK.allApps.filter(a => !a.transient && !deskVisibleSections(a).length &&
    (a.sections || []).some(s => s.module && moduleEnabled[s.module] === false));
}
function deskAppForPage(page) {
  return DESK.allApps.find(a => (a.sections || []).some(s => s.page === page)) || null;
}

// core.js hooks: the manifest orchestrator calls these by name.
function renderNav(nav) {
  deskBuildApps(uiManifest || { nav });
  themeEvent('nav');
}
function applyModuleVisibility(mods) {
  moduleEnabled = {};
  (mods || []).forEach(m => {
    if (!NAV_ID_RE.test(String(m.id || ''))) return;
    moduleEnabled[m.id] = m.enabled !== false && m.installed !== false;
  });
  deskRenderIcons();
  deskRenderTaskbar();
  if (DESK.launcher) deskRenderLauncher();
  // A window whose app lost every section closes; one whose current section
  // was toggled off falls back to the app's first section.
  Object.keys(DESK.wins).forEach(id => {
    const w = DESK.wins[id];
    const app = deskApp(id);
    const secs = app ? deskVisibleSections(app) : [];
    if (!secs.length) { deskClose(id); return; }
    deskRenderRail(w);
    if (!secs.find(s => s.page === w.page)) deskNavigate(w, secs[0].page);
  });
}

// ─── Navigation entry point used by every page ──────────
function showPage(id) {
  if (!PAGE_ID_RE.test(String(id || ''))) return;
  if (id === 'dashboard') return deskOpen('overview', 'dashboard');
  const app = deskAppForPage(id);
  if (app) return deskOpen(app.id, id);
  // A page with a renderer but no app (legacy caller, plugin mid-load): its own window.
  if (typeof window['page_' + id] === 'function' || _pendingScripts > 0) {
    if (!deskApp('page-' + id))
      DESK.allApps.push({ id: 'page-' + id, label: id, icon: 'file', order: 9999, transient: true,
                          sections: [{ page: id, label: id, icon: 'file', module: null, admin_only: false, group: null }] });
    deskOpen('page-' + id, id);
  }
}

// ─── Windows ────────────────────────────────────────────
function deskOpen(appId, page) {
  const app = deskApp(appId);
  if (!app) return;
  const secs = deskVisibleSections(app);
  if (!secs.length) return;
  let w = DESK.wins[appId];
  if (!w) w = deskCreateWindow(app);
  if (w.el.classList.contains('min')) w.el.classList.remove('min');
  deskFocus(appId);
  const want = page && secs.find(s => s.page === page) ? page
             : (w.page && secs.find(s => s.page === w.page) ? w.page : secs[0].page);
  if (want !== w.page || !w.rendered) deskNavigate(w, want);
  deskCloseLauncher();
  deskRenderTaskbar();
  deskSaveOpen();
}

function deskCreateWindow(app) {
  const el = document.createElement('section');
  el.className = 'win';
  el.dataset.app = app.id;
  el.setAttribute('aria-label', app.label);
  el.innerHTML = `
    <header class="win-title">
      ${navIcon(app)}
      <span class="win-name">${escapeHtml(app.label)}</span>
      <span class="win-sub"></span>
      <button class="win-btn" data-act="min" aria-label="Minimize ${escapeHtml(app.label)}" title="Minimize">${icon('min')}</button>
      <button class="win-btn" data-act="max" aria-label="Maximize ${escapeHtml(app.label)}" title="Maximize">${icon('max')}</button>
      <button class="win-btn x" data-act="close" aria-label="Close ${escapeHtml(app.label)}" title="Close">${icon('close')}</button>
    </header>
    <div class="win-body">
      <nav class="win-rail" aria-label="${escapeHtml(app.label)} sections"></nav>
      <div class="win-pane content"><div class="page-inner"></div></div>
    </div>
    <div class="win-resize" aria-hidden="true"></div>`;
  const w = {
    appId: app.id, el,
    subEl: el.querySelector('.win-sub'), railEl: el.querySelector('.win-rail'),
    paneEl: el.querySelector('.win-pane'), innerEl: el.querySelector('.page-inner'),
    page: null, rendered: false, cleanup: null, seq: 0,
    geo: deskInitialGeo(app.id),
  };
  DESK.wins[app.id] = w;
  DESK.order.push(app.id);
  deskApplyGeo(w);
  deskRenderRail(w);
  $('desk-windows').appendChild(el);

  // Focus on any press inside the window (capture: before a button handler
  // that might re-render the page into the "active" pane).
  el.addEventListener('pointerdown', () => deskFocus(app.id), true);
  el.querySelector('.win-title').addEventListener('click', e => {
    const b = e.target.closest('.win-btn');
    if (!b) return;
    if (b.dataset.act === 'min') deskMinimize(app.id);
    else if (b.dataset.act === 'max') deskToggleMax(app.id);
    else if (b.dataset.act === 'close') deskClose(app.id);
  });
  el.querySelector('.win-title').addEventListener('dblclick', e => {
    if (!e.target.closest('.win-btn')) deskToggleMax(app.id);
  });
  deskDraggable(w);
  deskResizable(w);
  return w;
}

function deskInitialGeo(appId) {
  const saved = (deskLoad(DESK_GEO_KEY) || {})[appId];
  const area = deskArea();
  const n = DESK.order.length;
  const g = saved && Number.isFinite(saved.w) ? Object.assign({}, saved) : {
    // Leave the widget column (right, ~310px) clear on a normal screen.
    w: Math.min(1000, Math.max(DESK_MIN_W, area.w - 130 - 330)),
    h: Math.min(700, Math.max(DESK_MIN_H, area.h - 48)),
    x: 130 + n * 28, y: 24 + n * 28, max: false,
  };
  return deskClampGeo(g, area);
}
function deskClampGeo(g, area) {
  g.w = Math.max(DESK_MIN_W, Math.min(g.w, area.w));
  g.h = Math.max(DESK_MIN_H, Math.min(g.h, area.h));
  g.x = Math.max(0, Math.min(g.x, area.w - g.w));
  g.y = Math.max(0, Math.min(g.y, area.h - g.h));
  return g;
}
function deskApplyGeo(w) {
  const g = w.geo;
  w.el.style.left = g.x + 'px'; w.el.style.top = g.y + 'px';
  w.el.style.width = g.w + 'px'; w.el.style.height = g.h + 'px';
  w.el.classList.toggle('max', !!g.max);
  const mb = w.el.querySelector('[data-act="max"]');
  if (mb) { mb.innerHTML = icon(g.max ? 'restore' : 'max'); mb.title = g.max ? 'Restore' : 'Maximize'; }
}
function deskSaveGeo(w) {
  const all = deskLoad(DESK_GEO_KEY) || {};
  all[w.appId] = w.geo;
  deskStore(DESK_GEO_KEY, all);
}

function deskDraggable(w) {
  const bar = w.el.querySelector('.win-title');
  let start = null;
  bar.addEventListener('pointerdown', e => {
    if (e.button !== 0 || e.target.closest('.win-btn') || w.geo.max) return;
    start = { x: e.clientX, y: e.clientY, gx: w.geo.x, gy: w.geo.y };
    bar.setPointerCapture(e.pointerId);
    w.el.classList.add('dragging');
  });
  bar.addEventListener('pointermove', e => {
    if (!start) return;
    const area = deskArea();
    w.geo.x = Math.max(-(w.geo.w - 120), Math.min(area.w - 120, start.gx + e.clientX - start.x));
    w.geo.y = Math.max(0, Math.min(area.h - 36, start.gy + e.clientY - start.y));
    w.el.style.left = w.geo.x + 'px'; w.el.style.top = w.geo.y + 'px';
  });
  const end = () => { if (!start) return; start = null; w.el.classList.remove('dragging'); deskSaveGeo(w); };
  bar.addEventListener('pointerup', end);
  bar.addEventListener('pointercancel', end);
}
function deskResizable(w) {
  const h = w.el.querySelector('.win-resize');
  let start = null;
  h.addEventListener('pointerdown', e => {
    if (e.button !== 0 || w.geo.max) return;
    start = { x: e.clientX, y: e.clientY, gw: w.geo.w, gh: w.geo.h };
    h.setPointerCapture(e.pointerId);
    w.el.classList.add('resizing');
    e.preventDefault();
  });
  h.addEventListener('pointermove', e => {
    if (!start) return;
    const area = deskArea();
    w.geo.w = Math.max(DESK_MIN_W, Math.min(area.w - w.geo.x, start.gw + e.clientX - start.x));
    w.geo.h = Math.max(DESK_MIN_H, Math.min(area.h - w.geo.y, start.gh + e.clientY - start.y));
    w.el.style.width = w.geo.w + 'px'; w.el.style.height = w.geo.h + 'px';
  });
  const end = () => { if (!start) return; start = null; w.el.classList.remove('resizing'); deskSaveGeo(w); };
  h.addEventListener('pointerup', end);
  h.addEventListener('pointercancel', end);
}

function deskFocus(appId) {
  const w = DESK.wins[appId];
  if (!w) return;
  if (DESK.order[DESK.order.length - 1] !== appId) {
    DESK.order = DESK.order.filter(x => x !== appId).concat([appId]);
  }
  DESK.order.forEach((id, i) => {
    const win = DESK.wins[id];
    if (!win) return;
    win.el.style.zIndex = String(10 + i);
    win.el.classList.toggle('inactive', id !== appId);
  });
  if (DESK.active !== appId) {
    DESK.active = appId;
    if (w.page) currentPage = w.page;
    deskRenderTaskbar();
  }
}
function deskTopVisible() {
  for (let i = DESK.order.length - 1; i >= 0; i--) {
    const w = DESK.wins[DESK.order[i]];
    if (w && !w.el.classList.contains('min')) return DESK.order[i];
  }
  return null;
}
function deskMinimize(appId) {
  const w = DESK.wins[appId];
  if (!w) return;
  w.el.classList.add('min');
  if (DESK.active === appId) {
    DESK.active = null;
    const next = deskTopVisible();
    if (next) deskFocus(next);
  }
  deskRenderTaskbar();
  deskSaveOpen();
}
function deskToggleMax(appId) {
  const w = DESK.wins[appId];
  if (!w) return;
  w.geo.max = !w.geo.max;
  deskApplyGeo(w);
  deskSaveGeo(w);
}
function deskClose(appId) {
  const w = DESK.wins[appId];
  if (!w) return;
  if (w.cleanup) { try { w.cleanup(); } catch (e) {} }
  w.el.remove();
  delete DESK.wins[appId];
  DESK.order = DESK.order.filter(x => x !== appId);
  if (DESK.active === appId) {
    DESK.active = null;
    const next = deskTopVisible();
    if (next) deskFocus(next);
  }
  if (deskApp(appId) && deskApp(appId).transient) DESK.allApps = DESK.allApps.filter(a => a.id !== appId);
  deskRenderTaskbar();
  deskSaveOpen();
}
function deskCloseAll() { Object.keys(DESK.wins).forEach(deskClose); }
function deskTaskClick(appId) {
  const w = DESK.wins[appId];
  if (w && DESK.active === appId && !w.el.classList.contains('min')) deskMinimize(appId);
  else deskOpen(appId);
}

// Which windows are open (and on which section) survives a reload.
function deskSaveOpen() {
  if (!DESK.restored) return;
  deskStore(DESK_OPEN_KEY, DESK.order
    .filter(id => DESK.wins[id] && !deskApp(id).transient)
    .map(id => ({ app: id, page: DESK.wins[id].page, min: DESK.wins[id].el.classList.contains('min') })));
}
function deskRestoreOpen() {
  const saved = deskLoad(DESK_OPEN_KEY);
  DESK.restored = true;
  // A fresh login lands on an empty desktop — the widget already says how
  // the node is doing; Overview is one click away if wanted.
  if (!Array.isArray(saved) || !saved.length) return;
  saved.forEach(s => {
    if (!s || !deskApp(s.app) || !deskVisibleSections(deskApp(s.app)).length) return;
    deskOpen(s.app, s.page);
    if (s.min) deskMinimize(s.app);
  });
}

// ─── Rail + page rendering inside a window ──────────────
function deskRenderRail(w) {
  const app = deskApp(w.appId);
  const secs = app ? deskVisibleSections(app) : [];
  w.el.classList.toggle('norail', secs.length <= 1);
  let html = '', grp = null;
  secs.forEach(s => {
    if (s.group && s.group !== grp) { html += `<div class="grp">${escapeHtml(s.group)}</div>`; grp = s.group; }
    html += `<button class="rail-item${s.page === w.page ? ' sel' : ''}${s.admin_only ? ' admin-only' : ''}"
      data-page="${s.page}" onclick="deskRailClick('${jsArg(w.appId)}','${jsArg(s.page)}')">${navIcon(s)}<span>${escapeHtml(s.label || s.page)}</span></button>`;
  });
  w.railEl.innerHTML = html;
}
function deskRailClick(appId, page) {
  const w = DESK.wins[appId];
  if (!w) return;
  deskFocus(appId);
  if (page !== w.page) deskNavigate(w, page);
}
async function deskNavigate(w, page) {
  const app = deskApp(w.appId);
  const sec = app && (app.sections || []).find(s => s.page === page);
  if (w.cleanup) { try { w.cleanup(); } catch (e) {} w.cleanup = null; }
  w.page = page;
  w.rendered = true;
  w.subEl.textContent = (app && deskVisibleSections(app).length > 1 && sec) ? (sec.label || page) : '';
  w.railEl.querySelectorAll('.rail-item').forEach(b => b.classList.toggle('sel', b.dataset.page === page));
  if (DESK.active === w.appId) currentPage = page;
  themeEvent('navigate', { page });
  deskSaveOpen();
  await deskRenderPage(w, page);
}
// Pages write to $('page-content') AFTER their awaits, and that lookup
// resolves to the window being rendered into — so renders are serialized:
// one page at a time, in order. Otherwise two windows opened together (a
// restored session) could have one page land in the other's pane.
let _deskRenderChain = Promise.resolve();
// quiet: the page is re-rendering ITSELF (a poll timer, a post-action refresh)
// — no placeholder, no scroll reset, and it never pre-empts a navigation that
// is already queued (the window's seq is left alone, and the run is skipped
// if the window has moved on to another page by the time its turn comes).
function deskRenderPage(w, page, quiet) {
  const seq = quiet ? w.seq : ++w.seq;
  if (!quiet) w.innerEl.innerHTML = '<div class="loading">Loading...</div>';
  const run = () => (w.seq === seq && DESK.wins[w.appId] === w && (!quiet || w.page === page))
    ? deskRenderPageNow(w, page, seq, quiet) : null;
  _deskRenderChain = _deskRenderChain.then(run, run);
  return _deskRenderChain;
}
async function deskRenderPageNow(w, page, seq, quiet) {
  _pageCleanup = null;
  DESK.renderWin = w;
  DESK.renderPage = page;
  if (!quiet) w.paneEl.scrollTop = 0;
  const fn = deskWrapPage(page);
  try {
    if (fn) await fn();
    else if (_pendingScripts > 0) w.innerEl.innerHTML = '<div class="loading plugin-wait">Loading plugin…</div>';
    else w.innerEl.innerHTML = '<h2>Page not found</h2>';
  } catch (e) {
    w.innerEl.innerHTML = `<div class="error">Error: ${escapeHtml(e.message)}</div>`;
  }
  if (DESK.renderWin === w) { DESK.renderWin = null; DESK.renderPage = null; }
  // A navigation installs the page's teardown; a quiet re-render only
  // replaces it when the page registered a new one (else the old stays).
  if (w.seq === seq && (!quiet || _pageCleanup)) { w.cleanup = _pageCleanup; }
  _pageCleanup = null;
  themeEvent('page', { page });
}
// Pages were written for one page at a time: a poll timer calls page_x()
// directly, which writes to $('page-content'). Here that lookup resolves to
// the window being rendered — so a direct page_x() call is routed into the
// window showing x (through the serialized chain, quietly), and stops on its
// own once no window shows it. The chain itself runs the original.
function deskWrapPage(id) {
  const cur = window['page_' + id];
  if (typeof cur !== 'function') return null;
  if (cur._deskOrig) return cur._deskOrig;
  const orig = cur;
  const wrapped = function () {
    if (DESK.renderWin && DESK.renderPage === id) return orig.apply(this, arguments);
    const w = deskWinShowing(id);
    return w ? deskRenderPage(w, id, true) : Promise.resolve();
  };
  wrapped._deskOrig = orig;
  window['page_' + id] = wrapped;
  return orig;
}
function deskWinShowing(id) {
  for (const k in DESK.wins) if (DESK.wins[k].page === id) return DESK.wins[k];
  return null;
}
// "Is page x on screen?" for pages that poll (core.js pageActive()): any
// window showing it counts, minimized included — it keeps current while hidden.
function deskPageActive(id) { return deskWinShowing(id) !== null; }
var _pageActiveHook = deskPageActive;
// A plugin script that finished loading after its page was opened: core.js's
// loader re-renders currentPage via renderPage(); route that into the window.
async function renderPage(page) {
  const app = deskAppForPage(page);
  const w = app && DESK.wins[app.id];
  if (w) await deskRenderPage(w, page);
}

// ─── Desktop icons ──────────────────────────────────────
function deskRenderIcons() {
  const box = $('desk-icons');
  if (!box) return;
  box.innerHTML = deskVisibleApps().map(a => `
    <button class="dicon${DESK.sel === a.id ? ' sel' : ''}" data-app="${a.id}" title="${escapeHtml(a.label)}"
      onclick="deskIconClick(event,'${jsArg(a.id)}')" ondblclick="deskOpen('${jsArg(a.id)}')"
      onkeydown="deskIconKey(event,'${jsArg(a.id)}')">
      <span class="ib">${navIcon(a)}</span><span class="lbl">${escapeHtml(a.label)}</span></button>`).join('');
}
function deskIconClick(e, id) {
  DESK.sel = id;
  document.querySelectorAll('#desk-icons .dicon').forEach(b => b.classList.toggle('sel', b.dataset.app === id));
  // A touch has no double-click: a second tap on the selected icon opens it.
  if (e && e.pointerType === 'touch' && deskIconClick._last === id) deskOpen(id);
  deskIconClick._last = id;
}
function deskIconKey(e, id) {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); deskOpen(id); }
}

// ─── Taskbar ────────────────────────────────────────────
function deskRenderTaskbar() {
  const bar = $('taskbar');
  if (!bar) return;
  const apps = deskVisibleApps();
  const alerts = (DESK.summary && DESK.summary.alerts) || [];
  const upd = (DESK.summary && DESK.summary.updates) || {};
  const sec = (upd.security || 0) > 0;
  const n = alerts.length + ((upd.available || 0) > 0 ? 1 : 0);
  const host = (stripInfo.host || location.hostname || '').split('.')[0];
  bar.innerHTML = `
    <button class="tb launch${DESK.launcher ? ' on' : ''}" aria-label="App launcher" title="Apps" onclick="deskToggleLauncher(event)">${icon('grid')}</button>
    <span class="tb-sep"></span>
    ${apps.map(a => {
      const w = DESK.wins[a.id];
      const cls = w ? (DESK.active === a.id && !w.el.classList.contains('min') ? ' run act' : ' run') : '';
      return `<button class="tb${cls}" data-app="${a.id}" title="${escapeHtml(a.label)}" aria-label="${escapeHtml(a.label)}"
        onclick="deskTaskClick('${jsArg(a.id)}')">${navIcon(a)}</button>`;
    }).join('')}
    <div class="tray">
      <button class="tray-item" id="tray-alerts" title="${n ? escapeHtml(alerts.join('\n')) : 'No alerts'}" aria-label="${n} alert${n === 1 ? '' : 's'}"
        onclick="deskOpen('overview')">${icon('bell')}${n ? `<span class="cnt${sec ? ' crit' : ''}">${n}</span>` : ''}</button>
      <span class="tray-item host-item" id="tray-host"><span class="host">${escapeHtml(host)}</span>${stripInfo.os ? `<span>${escapeHtml(stripInfo.os)}</span>` : ''}</span>
      <span class="tray-item clock" id="tray-clock">${deskClockText()}</span>
      <button class="tray-item" id="tray-user" aria-label="Account menu" aria-haspopup="menu" onclick="deskToggleMenu(event)">${icon('user')}<span>${escapeHtml(currentUser || 'account')}</span></button>
      ${currentRole === 'admin' ? `<button class="tray-item" aria-label="Power" title="Reboot / shut down" onclick="powerModal()">${icon('power')}</button>` : ''}
    </div>`;
}
function deskClockText() {
  const d = new Date();
  const day = d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  const t = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return `${day} · ${t}`;
}
function deskTickClock() {
  const el = $('tray-clock');
  if (el) el.textContent = deskClockText();
}
// core.js calls this after /api/summary and /api/system/resources land.
function renderMachineStrip() {
  const el = $('tray-host');
  if (el) {
    const host = (stripInfo.host || location.hostname || '').split('.')[0];
    el.innerHTML = `<span class="host">${escapeHtml(host)}</span>${stripInfo.os ? `<span>${escapeHtml(stripInfo.os)}</span>` : ''}`;
  }
  deskRenderWidget();
}

// Account menu
function deskToggleMenu(e) {
  if (e) e.stopPropagation();
  DESK.menu ? deskCloseMenu() : deskOpenMenu();
}
function deskOpenMenu() {
  const m = $('tray-menu');
  if (!m) return;
  DESK.menu = true;
  m.innerHTML = `
    <div class="mh"><strong>${escapeHtml(currentUser || 'account')}</strong>${escapeHtml(currentRole)}${appVersion ? ` · v${escapeHtml(appVersion)}` : ''}</div>
    <button class="mi" role="menuitem" onclick="deskCloseMenu(); showPage('account')">${icon('user')}My Account</button>
    <button class="mi" role="menuitem" onclick="deskCloseMenu(); showPage('appearance')">${icon('image')}Appearance</button>
    ${currentRole === 'admin' ? `<button class="mi" role="menuitem" onclick="deskCloseMenu(); showPage('modules')">${icon('sli')}Apps</button>` : ''}
    <button class="mi" role="menuitem" onclick="deskCloseMenu(); doLogout()">${icon('power')}Sign out</button>`;
  m.style.display = '';
}
function deskCloseMenu() {
  DESK.menu = false;
  const m = $('tray-menu');
  if (m) m.style.display = 'none';
}

// ─── Launcher ───────────────────────────────────────────
function deskToggleLauncher(e) {
  if (e) e.stopPropagation();
  DESK.launcher ? deskCloseLauncher() : deskOpenLauncher();
}
function deskOpenLauncher() {
  DESK.launcher = true;
  deskRenderLauncher();
  $('launcher').style.display = '';
  deskRenderTaskbar();
  const q = $('launcher-q');
  if (q) q.focus();
}
function deskCloseLauncher() {
  if (!DESK.launcher) return;
  DESK.launcher = false;
  const l = $('launcher');
  if (l) l.style.display = 'none';
  deskRenderTaskbar();
}
function deskRenderLauncher(query) {
  const box = $('launcher');
  if (!box) return;
  const q = (query || '').trim().toLowerCase();
  const match = a => !q || a.label.toLowerCase().includes(q) ||
    deskVisibleSections(a).some(s => (s.label || '').toLowerCase().includes(q));
  const firstSection = a => {
    if (!q) return null;
    const s = deskVisibleSections(a).find(x => (x.label || '').toLowerCase().includes(q));
    return s && !a.label.toLowerCase().includes(q) ? s.page : null;
  };
  const apps = deskVisibleApps().filter(match);
  const off = deskDisabledApps().filter(a => !q || a.label.toLowerCase().includes(q));
  const tile = (a, dim) => {
    const page = firstSection(a);
    const sub = page ? (a.sections.find(s => s.page === page) || {}).label : '';
    return `<button class="lapp${dim ? ' dim' : ''}" title="${escapeHtml(dim ? a.label + ' — not enabled' : (sub ? a.label + ' › ' + sub : a.label))}"
      onclick="${dim ? "deskLaunchDisabled()" : `deskOpen('${jsArg(a.id)}'${page ? `,'${jsArg(page)}'` : ''})`}">
      <span class="ib">${navIcon(a)}</span><span class="lbl">${escapeHtml(sub || a.label)}</span></button>`;
  };
  const had = $('launcher-q');
  const keep = had ? had.value : (query || '');
  box.innerHTML = `
    <div class="lsearch-wrap">${icon('search')}<input class="lsearch" id="launcher-q" placeholder="Search apps and sections…"
      aria-label="Search apps" autocomplete="off" value="${escapeHtml(keep)}" oninput="deskRenderLauncher(this.value)"
      onkeydown="deskLauncherKey(event)"></div>
    <div class="lhead"><span>Apps</span><span>${apps.length} of ${deskVisibleApps().length}</span></div>
    ${apps.length ? `<div class="lgrid">${apps.map(a => tile(a, false)).join('')}</div>` : `<div class="lempty">Nothing matches "${escapeHtml(q)}".</div>`}
    ${off.length ? `<div class="lhead"><span>Not enabled</span><span>${currentRole === 'admin' ? 'Settings › Apps' : ''}</span></div>
      <div class="lgrid">${off.map(a => tile(a, true)).join('')}</div>` : ''}`;
  const inp = $('launcher-q');
  if (inp && document.activeElement !== inp && query !== undefined) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
}
function deskLauncherKey(e) {
  if (e.key === 'Escape') { deskCloseLauncher(); return; }
  if (e.key === 'Enter') {
    const first = document.querySelector('#launcher .lgrid .lapp:not(.dim)');
    if (first) first.click();
  }
}
function deskLaunchDisabled() {
  deskCloseLauncher();
  if (currentRole === 'admin') showPage('modules');
}

// ─── Widget: node at a glance ───────────────────────────
function deskRenderWidget() {
  const box = $('desk-widgets');
  if (!box || !isAuthed) return;
  const r = DESK.res || {};
  const s = DESK.summary || {};
  const alerts = s.alerts || [];
  const mem = r.memory || {};
  const host = (stripInfo.host || location.hostname || '').split('.')[0];
  const health = alerts.length
    ? `<div class="wline warn" onclick="deskOpen('overview')" title="${escapeHtml(alerts.join('\n'))}">${icon('bell')}${alerts.length} issue${alerts.length > 1 ? 's' : ''} · open Overview</div>`
    : `<div class="wline ok">${icon('check')}All systems healthy</div>`;
  const row = (k, pct, txt, page) => `<div class="wrow${page ? ' link' : ''}"${page ? ` onclick="showPage('${jsArg(page)}')" title="Open ${escapeHtml(k)}"` : ''}><span class="k" title="${escapeHtml(k)}">${escapeHtml(k)}</span>${usageBar(pct || 0)}${txt ? `<span class="card-sub" style="margin:0">${escapeHtml(txt)}</span>` : ''}</div>`;
  // Storage: one row per ZFS pool (usable figures); with no pool, the real
  // disk filesystems instead (root first). Three rows at most.
  const mounts = (r.mounts || []).filter(m => m.pct != null);
  let store = mounts.filter(m => m.kind === 'zfs');
  if (!store.length) store = mounts.filter(m => m.kind === 'disk' && !String(m.mount).startsWith('/boot'))
    .sort((a, b) => (a.mount === '/' ? -1 : b.mount === '/' ? 1 : 0));
  const storeRows = store.slice(0, 3).map(m => row(m.kind === 'zfs' ? (m.source || m.mount) : m.mount, m.pct,
    m.total ? `${fmtBytes(m.used)} / ${fmtBytes(m.total)}` : '', m.kind === 'zfs' ? 'zfs' : 'disks')).join('');
  // GPU only when the node has one (the gpu module answers available:true).
  const g = DESK.gpu && DESK.gpu.available && (DESK.gpu.gpus || [])[0];
  const gpuRows = g ? row('GPU', g.util, g.temp != null ? `${Math.round(g.temp)}°C` : '', 'gpu')
      + (g.mem_pct != null ? row('VRAM', g.mem_pct, g.mem_used != null && g.mem_total != null ? `${fmtBytes(g.mem_used)} / ${fmtBytes(g.mem_total)}` : '', 'gpu') : '') : '';
  box.innerHTML = `
    <section class="widget" aria-label="Node status">
      <div class="wt"><strong>${escapeHtml(host)}</strong><span>${escapeHtml(stripInfo.os || '')}</span></div>
      ${health}
      ${r.cpu_pct != null ? row('CPU', r.cpu_pct) : ''}
      ${mem.pct != null ? row('Memory', mem.pct, fmtBytes(mem.used)) : ''}
      ${storeRows}
      ${gpuRows}
      ${r.uptime_seconds != null ? `<div class="wrow"><span class="k">Uptime</span><span class="card-sub" style="margin:0">${fmtUptime(r.uptime_seconds)}${r.load ? ` · load ${r.load['1'] ?? '-'} / ${r.load['5'] ?? '-'} / ${r.load['15'] ?? '-'}` : ''}</span></div>` : ''}
      ${stripInfo.ip ? `<div class="wrow"><span class="k">Address</span><span class="card-sub mono" style="margin:0">${escapeHtml(stripInfo.ip)}</span></div>` : ''}
    </section>`;
}

// Resources every 10 s (cheap, /proc-backed); summary every 30 s. The
// Overview window re-renders on the summary tick like the old dashboard did.
let _deskTick = 0;
async function deskRefresh() {
  if (!isAuthed) return;
  _deskTick++;
  try {
    DESK.res = await API.get('/api/system/resources');
    stripInfo.up = DESK.res.uptime_seconds;
    stripInfo.load = DESK.res.load || null;
  } catch (e) {}
  // GPU: every tick while a card is present; once a node says it has none,
  // re-check only every ~5 minutes (a disabled module is never asked).
  if (moduleEnabled.gpu !== false && (!DESK.gpuAbsent || _deskTick % 30 === 0)) {
    try {
      DESK.gpu = await API.get('/api/gpu');
      DESK.gpuAbsent = !(DESK.gpu && DESK.gpu.available && (DESK.gpu.gpus || []).length);
    } catch (e) { DESK.gpu = null; DESK.gpuAbsent = true; }
  } else if (moduleEnabled.gpu === false) DESK.gpu = null;
  if (_deskTick % 3 === 1) {
    try {
      DESK.summary = await API.get('/api/summary');
      const sys = DESK.summary.system || {};
      if (sys.hostname) stripInfo.host = sys.hostname;
      if (sys.os) stripInfo.os = sys.os;
      if (sys.ip) stripInfo.ip = sys.ip;
      deskRenderTaskbar();
    } catch (e) {}
    const ov = DESK.wins.overview;
    if (ov && ov.page === 'dashboard' && !ov.el.classList.contains('min') && _deskTick > 1 &&
        $('modal-overlay').style.display === 'none') deskRenderPage(ov, 'dashboard', true);
  }
  renderMachineStrip();
}

// ─── Wallpaper ──────────────────────────────────────────
function deskWallOk(u) { return typeof u === 'string' && RE_WALL_URL.test(u); }
function deskApplyWallpaperUrl(url) {
  const el = $('wallpaper');
  if (!el) return;
  el.style.backgroundImage = deskWallOk(url) ? `url("${(typeof THEME_BASE === 'string' ? THEME_BASE : '') + url}")` : '';
}
function deskWallEntry(id) {
  return ((DESK.wallpapers && DESK.wallpapers.wallpapers) || []).find(w => w.id === id) || null;
}
function deskWallUserChoice() {
  const c = deskLoad(DESK_WALL_KEY);
  return c && c.user && c.id ? c.id : null;
}
function deskApplyWallpaperChoice() {
  const cat = DESK.wallpapers;
  if (!cat) return;
  const mine = deskWallUserChoice();
  let e = mine ? deskWallEntry(mine) : null;
  if (!e && cat.default && cat.default.wallpaper) e = deskWallEntry(cat.default.wallpaper);
  if (!e) e = (cat.wallpapers || [])[0] || null;
  deskApplyWallpaperUrl(e ? e.url : null);
  deskStore(DESK_WALL_KEY, { id: e ? e.id : null, url: e ? e.url : null, user: !!(mine && deskWallEntry(mine)) });
}
async function deskWallpaperInit() {
  const c = deskLoad(DESK_WALL_KEY);       // pre-paint from the cache
  if (c && c.url) deskApplyWallpaperUrl(c.url);
  await deskWallpaperRefresh();
}
async function deskWallpaperRefresh() {
  try {
    const r = await fetch('/api/wallpaper');
    if (r.ok) DESK.wallpapers = await r.json();
  } catch (e) {}
  deskApplyWallpaperChoice();
}
function deskPickWallpaper(id) {
  const e = deskWallEntry(id);
  if (!e) return;
  deskStore(DESK_WALL_KEY, { id: e.id, url: e.url, user: true });
  deskApplyWallpaperUrl(e.url);
  deskRenderWallpaperPicker();
}
function deskWallFollowNode() {
  deskStore(DESK_WALL_KEY, { id: null, url: null, user: false });
  deskApplyWallpaperChoice();
  deskRenderWallpaperPicker();
}
async function deskWallSetDefault() {
  const c = deskLoad(DESK_WALL_KEY) || {};
  try {
    await API.post('/api/wallpaper/default', { wallpaper: c.id || null });
    await deskWallpaperRefresh();
    deskRenderWallpaperPicker('Node default saved.');
  } catch (e) { alert(e.message); }
}
async function deskWallUpload() {
  const f = $('wall-file');
  if (!f || !f.files || !f.files[0]) { alert('Choose an image first.'); return; }
  const fd = new FormData();
  fd.append('file', f.files[0]);
  const id = ($('wall-id') && $('wall-id').value.trim()) || '';
  if (id) fd.append('id', id);
  try {
    const r = await fetch('/api/wallpaper/upload', { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.success) throw new Error(j.error || 'Upload failed');
    await deskWallpaperRefresh();
    if (j.wallpaper && j.wallpaper.id) deskPickWallpaper(j.wallpaper.id);
    deskRenderWallpaperPicker('Uploaded ' + (j.wallpaper ? j.wallpaper.label : '') + '.');
  } catch (e) { alert(e.message); }
}
async function deskWallDelete(id) {
  const e = deskWallEntry(id);
  if (!e || !confirm(`Delete wallpaper "${e.label}"? Browsers using it fall back to the node default.`)) return;
  try {
    await API.delete('/api/wallpaper/' + encodeURIComponent(id));
    await deskWallpaperRefresh();
    deskRenderWallpaperPicker('Deleted.');
  } catch (err) { alert(err.message); }
}
function deskRenderWallpaperPicker(msg) {
  const box = $('wallpaper-picker');
  if (!box) return;
  const cat = DESK.wallpapers || { wallpapers: [], default: {} };
  const cur = deskLoad(DESK_WALL_KEY) || {};
  const mine = deskWallUserChoice();
  const def = deskWallEntry((cat.default || {}).wallpaper);
  const admin = currentRole === 'admin';
  box.innerHTML = `
    <div class="wall-grid">${(cat.wallpapers || []).map(w => `
      <button class="wall-sw${cur.id === w.id ? ' sel' : ''}" style="background-image:url('${escapeHtml(deskWallOk(w.url) ? w.url : '')}')"
        onclick="deskPickWallpaper('${jsArg(w.id)}')" aria-label="Wallpaper ${escapeHtml(w.label)}" title="${escapeHtml(w.label)}">
        <span class="src">${w.source === 'builtin' ? 'built-in' : 'drop-in'}</span>
        <span class="lbl">${escapeHtml(w.label)}</span>
        ${admin && w.source !== 'builtin' ? `<span class="del" role="button" aria-label="Delete ${escapeHtml(w.label)}" onclick="event.stopPropagation(); deskWallDelete('${jsArg(w.id)}')">${icon('close')}</span>` : ''}
      </button>`).join('')}
    </div>
    <div class="wall-tools">
      <label class="checkitem" style="padding-left:0"><input type="checkbox" id="wall-follow" ${mine ? '' : 'checked'} onchange="this.checked ? deskWallFollowNode() : null">
        Follow this node's default${def ? ` (currently ${escapeHtml(def.label)})` : ''}</label>
      ${admin ? `<button class="btn btn-sm btn-outline" onclick="deskWallSetDefault()">Set as node default</button>` : ''}
    </div>
    ${msg ? `<div class="health-ok" style="margin-top:10px">✓ ${escapeHtml(msg)}</div>` : ''}
    <p class="help">Your wallpaper is stored in this browser. Built-in wallpapers ship with the app;
      drop-ins live in <code>wallpapers/</code> next to app.py.</p>
    ${admin ? `
    <h3>Upload a wallpaper</h3>
    <div class="wall-tools">
      <input type="file" id="wall-file" accept="image/png,image/jpeg,image/webp" class="form-control" style="max-width:320px">
      <input id="wall-id" class="form-control" placeholder="name (optional, a-z 0-9 -)" style="max-width:220px">
      <button class="btn btn-sm" onclick="deskWallUpload()">Upload</button>
    </div>
    <p class="help">PNG, JPEG or WebP up to 8 MiB. Uploads are available to every browser on this node.</p>` : ''}`;
}

// The Appearance section of Settings: theme (the existing picker) + wallpaper.
async function page_appearance() {
  $('page-content').innerHTML = `
    <h2>Appearance</h2>
    <h3>Theme</h3>
    <div id="appearance"></div>
    <h3>Wallpaper</h3>
    <div id="wallpaper-picker"></div>`;
  await renderAppearance();
  deskRenderWallpaperPicker();
}

// ─── Login / app shell ──────────────────────────────────
function showLogin() {
  isAuthed = false;
  const d = $('desktop');
  if (d) d.style.display = 'none';
  deskCloseLauncher();
  deskCloseMenu();
  closeModal();
  const sub = $('login-sub');
  if (sub && location.hostname) sub.textContent = location.hostname;
  $('login-screen').style.display = 'flex';
  $('login-pass').value = '';
  renderSsoButton();
  themeEvent('login');
  const failed = new URLSearchParams(location.search).has('sso_error');
  if (ssoConfig && ssoConfig.auto_redirect && !failed) { ssoSignIn(); return; }
  if (failed) {
    const err = $('login-error');
    if (err) {
      err.textContent = 'Single sign-on did not complete. Sign in below.';
      err.style.display = 'block';
    }
  }
  $('login-user').focus();
}

async function showApp(user, fqdn, role, mustChange, version) {
  isAuthed = true;
  currentRole = role || 'admin';
  $('login-screen').style.display = 'none';
  $('desktop').style.display = '';
  document.body.classList.toggle('readonly', currentRole !== 'admin');
  currentUser = user || '';
  if (version) appVersion = version;
  stripInfo.host = fqdn || stripInfo.host;
  themeEvent('app', { user, role: currentRole });
  await applyModules();          // builds the apps, renders icons + taskbar
  deskRenderWidget();
  deskRefresh();                 // fire-and-forget; widget and tray fill in
  deskRestoreOpen();             // last session's windows (none on a first login)
  if (mustChange) forcePasswordChange();
}

async function doLogout(e) {
  if (e) e.preventDefault();
  try { await fetch('/api/logout', { method: 'POST' }); } catch (err) {}
  deskCloseAll();
  DESK.restored = false;
  showLogin();
}

// ─── Global desktop behaviour ───────────────────────────
document.addEventListener('pointerdown', e => {
  if (DESK.launcher && !e.target.closest('#launcher, .tb.launch')) deskCloseLauncher();
  if (DESK.menu && !e.target.closest('#tray-menu, #tray-user')) deskCloseMenu();
  if (DESK.sel && !e.target.closest('#desk-icons .dicon')) {
    DESK.sel = null;
    document.querySelectorAll('#desk-icons .dicon.sel').forEach(b => b.classList.remove('sel'));
  }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (DESK.launcher) deskCloseLauncher();
    if (DESK.menu) deskCloseMenu();
  }
});
window.addEventListener('resize', () => {
  const area = deskArea();
  Object.values(DESK.wins).forEach(w => { deskClampGeo(w.geo, area); deskApplyGeo(w); });
});
setInterval(deskTickClock, 30000);
setInterval(deskRefresh, 10000);
document.addEventListener('DOMContentLoaded', () => { deskWallpaperInit(); });
