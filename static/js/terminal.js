// ─── Terminal: ssh to this node, in a window ─────────────────────────
// The backend runs `ssh <user>@127.0.0.1` on a pty and relays bytes; the
// node's own sshd asks for the password. This page only picks the login name
// and hosts xterm.js. Its state is its own (not the modal consoles' _term /
// _termWs), so a container shell can be open at the same time.
let _trmTerm = null, _trmWs = null, _trmFit = null, _trmObs = null;
const TRM_USER_KEY = 'terminal.user.v1';
const TRM_USER_RE = /^[A-Za-z_][A-Za-z0-9_.-]{0,31}$/;

function trmClose() {
  try { if (_trmObs) _trmObs.disconnect(); } catch (e) {}
  try { if (_trmWs) { _trmWs.onclose = null; _trmWs.close(); } } catch (e) {}
  try { if (_trmTerm) _trmTerm.dispose(); } catch (e) {}
  _trmObs = null; _trmWs = null; _trmTerm = null; _trmFit = null;
}

async function page_terminal() {
  trmClose();
  const host = $('page-content');
  let info;
  try { info = await API.get('/api/terminal'); }
  catch (e) { host.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; return; }
  if (!info || info.success === false) {
    host.innerHTML = `<div class="error">${escapeHtml((info && info.error) || 'Terminal unavailable')}</div>`;
    return;
  }
  let last = '';
  try { last = localStorage.getItem(TRM_USER_KEY) || ''; } catch (e) {}
  if (!TRM_USER_RE.test(last)) last = '';
  const target = `${info.host}:${info.port}`;
  host.innerHTML = `
    <div class="term-page">
      <form class="term-bar" onsubmit="trmConnect(event)">
        <label for="term-user">Log in as</label>
        <input id="term-user" type="text" autocomplete="username" autocapitalize="off" spellcheck="false"
               maxlength="32" placeholder="system account" value="${escapeHtml(last)}">
        <button class="btn btn-sm" id="term-go" type="submit"${info.available ? '' : ' disabled'}>Connect</button>
        <button class="btn btn-sm btn-outline" id="term-stop" type="button" onclick="trmDisconnect()" style="display:none">Disconnect</button>
        <span class="term-status" id="term-status" role="status"></span>
      </form>
      <div class="term-intro" id="term-intro">
        ${info.available ? `
        <p>This opens an SSH session to this node (<code>${escapeHtml(target)}</code>).
          You sign in with that system account's own password. The dashboard
          login does not grant a shell and no password is stored.</p>
        <p class="help">A node whose SSH server accepts keys only will refuse the login.
          Opening and closing a session is written to the audit log; what you type is not.</p>`
        : `<div class="error">${escapeHtml(info.reason || 'Terminal unavailable on this node')}</div>`}
      </div>
      <div class="term-screen" id="term-screen" style="display:none"></div>
    </div>`;
  _pageCleanup = trmClose;
  const inp = $('term-user');
  if (inp && info.available) inp.focus();
}

function trmStatus(text) { const el = $('term-status'); if (el) el.textContent = text; }
function trmLive(live) {
  const go = $('term-go'), stop = $('term-stop'), inp = $('term-user');
  if (go) { go.style.display = live ? 'none' : ''; go.textContent = _trmTerm ? 'Reconnect' : 'Connect'; }
  if (stop) stop.style.display = live ? '' : 'none';
  if (inp) inp.disabled = live;
}
function trmDisconnect() {
  try { if (_trmWs) _trmWs.close(); } catch (e) {}
}

function trmConnect(ev) {
  if (ev) ev.preventDefault();
  const inp = $('term-user'), screen = $('term-screen');
  if (!inp || !screen) return;
  const user = inp.value.trim();
  if (!TRM_USER_RE.test(user)) { trmStatus('Enter a valid login name'); inp.focus(); return; }
  if (!window.Terminal) { trmStatus('terminal unavailable'); return; }
  try { localStorage.setItem(TRM_USER_KEY, user); } catch (e) {}
  trmClose();
  $('term-intro').style.display = 'none';
  screen.style.display = '';
  screen.innerHTML = '';
  const term = new Terminal({ cursorBlink: true, fontSize: 13, theme: consoleTheme() });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(screen);
  _trmTerm = term; _trmFit = fit;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/terminal?user=${encodeURIComponent(user)}`);
  ws.binaryType = 'arraybuffer';
  _trmWs = ws;
  const sendResize = () => {
    if (!screen.offsetWidth || !screen.offsetHeight) return;   // minimized window
    try { fit.fit(); } catch (e) {}
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'resize', width: term.cols, height: term.rows }));
  };
  trmStatus('connecting…');
  trmLive(true);
  ws.onopen = () => { trmStatus(`${user}@${location.hostname}`); sendResize(); term.focus(); };
  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {
      try {
        const o = JSON.parse(e.data);
        // The server waits for us to close after an error (so the reason
        // is not lost to an abrupt teardown) — close as soon as it is shown.
        if (o && o.type === 'error') { term.write('\r\n\x1b[31m' + o.error + '\x1b[0m\r\n'); ws.close(); return; }
      } catch (x) {}
      term.write(e.data);
    } else {
      term.write(new Uint8Array(e.data));
    }
  };
  ws.onclose = () => {
    if (_trmWs !== ws) return;
    _trmWs = null;
    trmStatus('disconnected');
    trmLive(false);
    try { term.write('\r\n\x1b[2m[session closed]\x1b[0m\r\n'); } catch (e) {}
  };
  ws.onerror = () => { trmStatus('connection error'); };
  term.onData(d => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'stdin', data: d })); });
  // The window is resizable and can be maximized: follow the element, not
  // the browser window.
  if (window.ResizeObserver) {
    _trmObs = new ResizeObserver(sendResize);
    _trmObs.observe(screen);
  }
  sendResize();
}

// Switching theme or light/dark while a session is open recolours it.
['nexus:mode', 'nexus:theme'].forEach(n => document.addEventListener(n, () => {
  if (_trmTerm) try { _trmTerm.options.theme = consoleTheme(); } catch (e) {}
}));
