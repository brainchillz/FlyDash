// ─── Affinity (StillDeadcode's DeepSeek-V4-Flash engine, systemd unit) ──────
// Page + dashboard card for the `affinity` module. Everything comes from
// /api/affinity: unit state via systemctl, the launcher's argv and
// environment, /v1/models readiness, and the engine's own startup and
// per-request lines read back from the container log. Actions are
// systemctl verbs; there is no parameter editor (the settings are argv).

function afNum(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 1 : d); }
function afInt(x) { return x == null ? '—' : Number(x).toLocaleString(); }
function afStateBadge(st) {
  if (!st.installed) return '<span class="status-badge gray">not installed</span>';
  if (st.running) return st.ready ? '<span class="status-badge green">serving</span>' : '<span class="status-badge yellow">active · engine loading</span>';
  const cls = st.active === 'failed' ? 'red' : st.active === 'activating' ? 'yellow' : 'gray';
  return `<span class="status-badge ${cls}">${escapeHtml(st.active || 'inactive')}${st.sub && st.sub !== st.active ? ' · ' + escapeHtml(st.sub) : ''}</span>`;
}

async function page_affinity() {
  const el = $('page-content');
  el.innerHTML = '<h2>Affinity</h2><p class="help">Loading…</p>';
  let st;
  try { st = await API.get('/api/affinity'); }
  catch (e) { el.innerHTML = `<h2>Affinity</h2><div class="error">${escapeHtml(e.message)}</div>`; return; }
  const admin = currentRole === 'admin';
  let html = '<h2>Affinity <span class="help">DeepSeek-V4-Flash inference engine</span></h2>';

  if (!st.installed) {
    html += `<div class="alert alert-warning">No <code>${escapeHtml(st.unit)}.service</code> on this node. The module expects the affinity launcher's systemd unit (set <code>DASHBOARD_AFFINITY_UNIT</code> if yours is named differently).</div>`;
    el.innerHTML = html; return;
  }
  const su = st.startup || {}, conflicts = st.conflicts || [];
  const activeConflicts = conflicts.filter(c => c.active).map(c => c.unit);
  const warm = Math.max(1, Math.round(st.warm_start_s / 60));

  // ── Unit card
  html += `<div class="card"><h3>Unit <code>${escapeHtml(st.unit)}.service</code> ${afStateBadge(st)}</h3>
    <table class="table kv"><tbody>
      <tr><td>Since</td><td>${escapeHtml(st.since || '—')}${st.nrestarts ? ` · <span class="status-badge yellow">${st.nrestarts} systemd restart(s)</span>` : ''}</td></tr>
      <tr><td>Container</td><td><code>${escapeHtml(st.container)}</code> · port ${st.port}${st.gpus ? ` · GPUs ${escapeHtml(st.gpus)}` : ''}${st.env.AFF_ALLOW_NRANK ? ' · <span class="status-badge gray" title="AFF_ALLOW_NRANK=1: the engine refuses more than two cards without it">&gt;2-card override</span>' : ''}</td></tr>
      ${conflicts.map(c => `<tr><td>${escapeHtml(c.unit)}</td><td>${c.present ? `${c.active ? '<span class="status-badge red">running</span>' : '<span class="status-badge gray">stopped</span>'} · ${c.enabled ? '<strong>enabled at boot</strong>' : 'disabled at boot'} — conflicts with this unit (same GPUs)` : 'not installed'}</td></tr>`).join('')}
    </tbody></table>
    ${admin ? `<div class="toolbar">
      ${st.running
        ? `<button class="btn btn-sm btn-outline" onclick="afAction('stop')">Stop</button>
           <button class="btn btn-sm btn-outline" onclick="afAction('restart')" title="Restart ≈ ${warm} min (expert placement into VRAM)">Restart</button>`
        : `<button class="btn btn-sm" onclick="afAction('start')" title="Start ≈ ${warm} min">Start${activeConflicts.length ? ' (stops ' + escapeHtml(activeConflicts.join(', ')) + ')' : ''}</button>`}
      <span style="margin-left:auto"></span>
      <label class="help"><input type="checkbox" ${st.enabled ? 'checked' : ''} onchange="afAction(this.checked ? 'enable' : 'disable')"> Enabled at boot</label>
    </div>${activeConflicts.length ? `<p class="help">Start / restart — <strong>${escapeHtml(activeConflicts.join(', '))} is running and will be STOPPED</strong> (the unit declares Conflicts=).</p>` : ''}` : ''}
  </div>`;

  // ── Engine card
  html += '<div class="card"><h3>Engine</h3>';
  if (!st.running) html += '<p class="help">Not running.</p>';
  else {
    const mods = (st.models || []).map(x => `<code>${escapeHtml(x.id)}</code>`).join(' ');
    html += `<table class="table kv"><tbody>
      <tr><td>Model id</td><td>${mods || `<span class="help">(engine not answering yet${st.models_error ? ': ' + escapeHtml(st.models_error) : ''})</span>`}${su.layers ? ` · ${su.layers} layers · ${afInt(su.experts)} experts (top-${su.top_k})` : ''}</td></tr>
      <tr><td>Build</td><td>${escapeHtml(su.build || '?')}${su.tp ? ` · tensor parallel across ${su.tp} GPUs` : ''}</td></tr>
      <tr><td>Experts in VRAM</td><td>${su.experts_total ? `<strong>${afInt(su.experts_resident)}</strong> of ${afInt(su.experts_total)} (${afNum(su.resident_pct, 1)}%) · ${afNum(su.placement_gib, 1)} GiB placed in ${afNum(su.placement_s, 0)} s` : '—'}${su.host_pool ? `<br><span class="help">host pool: ${escapeHtml(su.host_pool)}</span>` : ''}</td></tr>
      <tr><td>KV cache</td><td>${su.kv_reserved ? `${afInt(su.kv_reserved)} positions reserved · ${afInt(su.kv_backed)} backed (${afNum(su.kv_gib, 2)} GiB)` : '—'}${su.dspark ? ` · DSpark draft ${escapeHtml(su.dspark)}` : ''}</td></tr>
      <tr><td>Status</td><td>${su.ready ? `<span class="status-badge green">listening</span> <code>${escapeHtml(su.listening || '')}</code>` : `<span class="status-badge yellow">loading</span> <span class="help">≈ ${warm} min</span>`}${su.errors ? ` · <span class="status-badge red">${su.errors} error line(s) in the log</span>` : ''}</td></tr>
    </tbody></table>`;
    if (su.lines && su.lines.length) html += `<details><summary class="help">The engine's own startup lines (last start)</summary><pre class="log" style="white-space:pre-wrap;max-height:30vh;overflow:auto">${escapeHtml(su.lines.join('\n'))}</pre></details>`;
  }
  html += '</div>';

  // ── Throughput card: the engine prints one line per finished request.
  html += '<div class="card"><h3>Throughput</h3>';
  if (!st.running) html += '<p class="help">Not running.</p>';
  else if (!su.requests_total) html += '<p class="help">No requests since the last start — the engine prints one line per finished request; they appear here.</p>';
  else {
    html += `<div class="stat-row">
      <div class="stat"><div class="stat-v">${afNum(su.last_tps, 1)}</div><div class="stat-l">tok/s, last request</div></div>
      <div class="stat"><div class="stat-v">${afNum(su.mean_accept_pct, 1)}%</div><div class="stat-l">draft acceptance</div></div>
      <div class="stat"><div class="stat-v">${afInt(su.requests_total)}</div><div class="stat-l">requests in the log tail</div></div>
      <div class="stat"><div class="stat-v">${afInt(su.tokens_total)}</div><div class="stat-l">tokens generated</div></div>
    </div>
    <table class="table"><thead><tr><th>Slot</th><th>Tokens</th><th>Seconds</th><th>tok/s</th><th>Blocks</th><th>Drafted</th><th>Accepted</th><th>tok/block</th></tr></thead><tbody>
    ${(su.requests || []).slice().reverse().map(r => `<tr><td>${r.slot}</td><td>${afInt(r.tokens)}</td><td>${afNum(r.seconds, 2)}</td><td><strong>${afNum(r.tps, 1)}</strong></td><td>${afInt(r.blocks)}</td><td>${afInt(r.drafted)}</td><td>${afInt(r.accepted)} (${afNum(r.accept_pct, 1)}%)</td><td>${afNum(r.tok_per_block, 2)}</td></tr>`).join('')}
    </tbody></table>
    <div id="af-spark"></div>`;
  }
  html += '</div>';

  // ── Launch settings (read-only: argv on the unit's ExecStart)
  const named = st.named_args || [], other = st.other_args || {};
  html += `<div class="card"><h3>Launch settings</h3>
    <p class="help">The engine's arguments on <code>ExecStart</code> of <code>${escapeHtml(st.fragment || st.unit + '.service')}</code>, plus the launcher's environment. Read-only here — edit the unit and <code>daemon-reload</code> to change them.</p>
    <table class="table"><thead><tr><th>Setting</th><th>Value</th></tr></thead><tbody>
    ${named.map(a => `<tr><td>${escapeHtml(a.label)} <code class="help">${escapeHtml(a.flag)}</code></td><td class="mono">${escapeHtml(a.value == null ? '—' : String(a.value))}</td></tr>`).join('')}
    ${Object.keys(other).sort().map(k => `<tr><td><code>--${escapeHtml(k)}</code></td><td class="mono">${escapeHtml(String(other[k]))}</td></tr>`).join('')}
    ${Object.keys(st.env || {}).sort().map(k => `<tr><td>env <code>${escapeHtml(k)}</code></td><td class="mono">${escapeHtml(String(st.env[k]))}</td></tr>`).join('')}
    </tbody></table></div>`;

  html += `<div class="toolbar"><button class="btn btn-sm btn-outline" onclick="afLogs('container')">Container log</button>
    <button class="btn btn-sm btn-outline" onclick="afLogs('journal')">Unit journal</button></div>`;
  el.innerHTML = html;
  if (st.running && su.requests_total) afFillSpark(st.unit);
}

async function afFillSpark(unit) {
  try {
    const r = await API.get(`/api/history?metric=affinity_tps&label=${encodeURIComponent(unit)}&since=86400`);
    const pts = (r.points || []).filter(p => p && p[1] != null);
    const box = $('af-spark');
    if (box && pts.length > 2) box.innerHTML = `<span class="help">tok/s of the last request per tick, last 24 h</span> ${sparkline(pts)}`;
  } catch (e) { /* history off or empty */ }
}

async function afAction(action) {
  const warn = { stop: 'Stop the Affinity server? Clients on this node lose the inference API until it is started again.',
                 restart: 'Restart the server now? Reloading the experts into VRAM takes a minute or two; in-flight requests are dropped.' }[action];
  if (warn && !confirm(warn)) return;
  try {
    const r = await API.post('/api/affinity/action', { action });
    if (r && r.note) alert(r.note);
  } catch (e) { alert(e.message); }
  page_affinity();
}

async function afLogs(source) {
  try {
    const r = await API.get(`/api/affinity/logs?source=${encodeURIComponent(source)}&tail=400`);
    openModal(`Affinity ${source === 'journal' ? 'unit journal' : 'container log'} — ${r.name}`, `<pre class="log" style="max-height:60vh;overflow:auto">${escapeHtml(r.logs || '(empty)')}</pre>`, { wide: true });
  } catch (e) { alert(e.message); }
}

// Dashboard card — from the /api/summary block (unit state, /v1/models
// readiness, the engine's last printed figures).
function dashcard_affinity(ctx) {
  const a = ctx.s.affinity;
  if (!a) return '';            // no unit here — no card
  const dot = !a.running ? (a.state === 'failed' ? 'red' : 'gray') : a.ready ? 'green' : 'yellow';
  const val = a.running
    ? (a.ready ? escapeHtml(a.model || '?') : '<span class="card-unit">loading</span>')
    : `<span class="card-unit">${escapeHtml(a.state || 'stopped')}</span>`;
  const sub = a.ready
    ? `${a.tps != null ? `<strong>${afNum(a.tps, 0)}</strong> tok/s` : 'no requests yet'}${a.draft_accept_pct != null ? ` · accept ${afNum(a.draft_accept_pct, 0)}%` : ''}${a.resident_pct != null ? ` · ${afNum(a.resident_pct, 0)}% experts in VRAM` : ''}`
    : `${a.gpus ? `GPUs ${escapeHtml(a.gpus)}` : ''}${a.tp ? ` · TP=${a.tp}` : ''}`;
  return `
    <div class="card card-link" onclick="showPage('affinity')">
      <div class="card-head"><span class="status-dot ${dot}"></span>Affinity</div>
      <div class="card-value" style="font-size:1.2em">${val}</div>
      <div class="card-sub">${sub}</div>
      <div class="card-sub">port ${a.port}${a.enabled === false ? ' · <span class="status-badge yellow">not at boot</span>' : ''}${a.nrestarts ? ` · ${a.nrestarts} restarts` : ''}${a.errors ? ` · <span class="status-badge red">${a.errors} errors</span>` : ''}</div>
    </div>`;
}
