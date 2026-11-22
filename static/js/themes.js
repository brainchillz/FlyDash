// ─── Theme loader ───────────────────────────────────────
// A theme is a directory (static/themes/<id>/ built-in, <app>/themes/<id>/
// drop-in; THEMES.md) whose theme.css redefines the token block style.css is
// built on, plus an optional theme.js. The server lists them at GET
// /api/themes (public — the login screen is themed too) with the node's
// admin-set default.
//
// Choice precedence: a theme this browser picked on My Account wins; otherwise
// the node default is followed, live. The choice is cached in localStorage so
// the inline head script in index.html can attach the stylesheet BEFORE first
// paint; the only flash is a brand-new browser's first visit, which paints
// Nexus once and then switches to the node default.
//
// Dark/light ("mode") stays the pre-existing html.theme-light class and the
// 'theme' localStorage key, so nothing that reads them changes. A theme
// declares which modes it supports; an unsupported one is forced to the
// nearest supported mode for display without overwriting the stored choice.

// Where this page's assets really live. Direct: ''. Through the
// NexusController's drill-in the node's page is served under /nodes/<id>/ and
// its <script src="/static/js/themes.js"> was rewritten to that prefix — so a
// theme stylesheet attached at runtime must carry the same prefix, or it
// resolves against the controller and 404s (the mode class still flips, which
// is how this bit: "light/dark changes immediately but the theme doesn't").
// API calls are NOT prefixed: the drill-in's fetch shim routes /api/* itself.
const THEME_BASE = (() => {
  try {
    const s = document.querySelector('script[src*="/static/js/themes.js"]');
    return s ? new URL(s.getAttribute('src'), location.href).pathname.replace(/\/static\/js\/themes\.js.*$/, '') : '';
  } catch (e) { return ''; }
})();
// One cached choice per base: the controller's origin hosts every node's
// drill-in, and a theme picked on one node must not pre-paint on another.
const THEME_CACHE_KEY = 'nexusTheme.v1' + (THEME_BASE ? ':' + THEME_BASE : '');
let themeCatalog = null;       // {themes:[…], default:{theme, variant}} from /api/themes
let themeCurrent = null;       // the applied catalog entry
let themeCurrentVariant = null;
const _themeScripts = new Set();   // theme JS URLs already appended (scripts cannot be unloaded)

function _themeCacheRead() {
  try { return JSON.parse(localStorage.getItem(THEME_CACHE_KEY) || 'null'); } catch (e) { return null; }
}
function _themeCacheWrite(obj) {
  try {
    if (obj) localStorage.setItem(THEME_CACHE_KEY, JSON.stringify(obj));
    else localStorage.removeItem(THEME_CACHE_KEY);
  } catch (e) {}
}
function _themeAssetOk(u) {
  return typeof u === 'string'
    && /^\/(static\/themes|theme-assets)\/[a-z][a-z0-9-]{1,31}\/[A-Za-z0-9._-]+(\?v=[A-Za-z0-9]+)?$/.test(u);
}
function themeEntry(id) {
  return ((themeCatalog && themeCatalog.themes) || []).find(t => t.id === id) || null;
}
function themeIsUsable(t) { return !!(t && !t.error); }

// The user's own pick, if any ({id, variant}); null = follow the node default.
function themeUserChoice() {
  const c = _themeCacheRead();
  return c && c.user && c.id ? { id: c.id, variant: c.variant || null } : null;
}

// Keep the displayed mode inside what the theme supports. Returns the mode shown.
function _themeEnforceMode(t) {
  const html = document.documentElement;
  const modes = (t && t.modes) || ['dark', 'light'];
  const wantLight = (() => { try { return localStorage.getItem('theme') === 'light'; } catch (e) { return false; } })();
  const light = modes.includes('light') && (wantLight || !modes.includes('dark'));
  html.classList.toggle('theme-light', light);
  return light ? 'light' : 'dark';
}

// Apply a catalog entry. Swaps the stylesheet (new link first, old removed on
// load, so the page never sits unstyled), unmounts the previous theme's JS,
// loads/mounts the new one, sets data-theme / data-theme-variant, caches.
function themeApply(t, variant, opts) {
  opts = opts || {};
  const html = document.documentElement;
  if (!themeIsUsable(t)) t = themeEntry('nexus');
  const vids = (t.variants || []).map(v => v.id);
  if (!vids.includes(variant)) variant = t.default_variant || null;

  const prev = themeCurrent;
  if (prev && prev.id !== t.id) _themeUnmount(prev);

  html.setAttribute('data-theme', t.id);
  if (variant) html.setAttribute('data-theme-variant', variant);
  else html.removeAttribute('data-theme-variant');
  _themeEnforceMode(t);

  const old = $('theme-css');
  const same = old && old.getAttribute('href') === THEME_BASE + t.css;
  if (t.css && _themeAssetOk(t.css)) {
    if (!same) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = THEME_BASE + t.css;
      link.onload = link.onerror = () => {
        if (old && old !== link) old.remove();
        link.id = 'theme-css';
        applyThemeLabel();
        themeEvent('theme', { id: t.id, variant });
      };
      if (old) old.removeAttribute('id');
      document.head.appendChild(link);
    } else {
      applyThemeLabel();
      themeEvent('theme', { id: t.id, variant });
    }
  } else {
    if (old) old.remove();
    applyThemeLabel();
    themeEvent('theme', { id: t.id, variant });
  }

  themeCurrent = t;
  themeCurrentVariant = variant;
  if (t.js && _themeAssetOk(t.js)) _themeLoadScript(t);
  else if (!t.js) _themeMount(t);

  _themeCacheWrite({ id: t.id, variant, css: t.css || null, js: t.js || null,
                     modes: t.modes, user: !!opts.user });
}

function _themeLoadScript(t) {
  if (_themeScripts.has(t.js)) { _themeMount(t); return; }
  _themeScripts.add(t.js);
  const s = document.createElement('script');
  s.src = THEME_BASE + t.js;
  s.async = false;
  s.onload = () => _themeMount(t);
  s.onerror = () => console.warn('theme script failed:', t.js);
  document.head.appendChild(s);
}
// A theme's JS registers window.NexusThemes[<id>] = { mount(ctx), unmount() }.
function _themeHooks(t) {
  const reg = window.NexusThemes || {};
  return (t && reg[t.id]) || null;
}
function _themeMount(t) {
  if (themeCurrent !== t) return;      // superseded while its script loaded
  const h = _themeHooks(t);
  if (h && typeof h.mount === 'function' && !h._mounted) {
    try { h.mount({ isAuthed: () => isAuthed, currentPage: () => currentPage, variant: () => themeCurrentVariant }); h._mounted = true; }
    catch (e) { console.warn('theme mount failed:', t.id, e); }
  }
}
function _themeUnmount(t) {
  const h = _themeHooks(t);
  if (h && h._mounted) {
    try { if (typeof h.unmount === 'function') h.unmount(); } catch (e) { console.warn('theme unmount failed:', t.id, e); }
    h._mounted = false;
  }
}

// Mode switch used by the Appearance controls (toggleTheme stays for the
// older sidebar/account callers).
function setThemeMode(mode) {
  try { localStorage.setItem('theme', mode === 'light' ? 'light' : 'dark'); } catch (e) {}
  _themeEnforceMode(themeCurrent);
  applyThemeLabel();
  themeEvent('mode', { light: mode === 'light' });
}

// User actions
function themeSelectUser(id, variant) {
  const t = themeEntry(id);
  if (!themeIsUsable(t)) return false;
  themeApply(t, variant, { user: true });
  return true;
}
function themeFollowNode() {
  const d = (themeCatalog && themeCatalog.default) || { theme: 'nexus', variant: null };
  themeApply(themeEntry(d.theme) || themeEntry('nexus'), d.variant, { user: false });
}

// Boot: fetch the catalog, then apply the user's choice (re-validated against
// the catalog) or the node default. Runs before login — /api/themes is public.
async function themesInit() {
  try {
    const r = await fetch('/api/themes');
    if (!r.ok) throw new Error(r.status);
    themeCatalog = await r.json();
  } catch (e) {
    // No catalog (server down, very old node): keep whatever the head script
    // attached from the cache; nothing else to do.
    themeCatalog = { themes: [{ id: 'nexus', name: 'Nexus', modes: ['dark', 'light'], variants: [] }], default: { theme: 'nexus', variant: null } };
    return;
  }
  const pick = themeUserChoice();
  if (pick && themeIsUsable(themeEntry(pick.id))) themeApply(themeEntry(pick.id), pick.variant, { user: true });
  else themeFollowNode();
}

// Re-read the catalog (after an admin saved a new node default) and re-apply
// for browsers following the default.
async function themesRefresh() {
  try {
    const r = await fetch('/api/themes');
    if (r.ok) themeCatalog = await r.json();
  } catch (e) {}
  if (!themeUserChoice()) themeFollowNode();
}

document.addEventListener('DOMContentLoaded', () => { themesInit(); });
