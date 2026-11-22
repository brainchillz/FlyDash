// ─── Radiance Engine (StillDeadcode's standalone engine, systemd unit) ───────
// Page + dashboard card for the `radiance_engine` module. Everything comes
// from /api/radiance_engine: unit state via systemctl, the engine's flags
// from ExecStart, /v1/models readiness, the engine's own /stats figures and
// its boot receipt + recent requests read back from the container log.
// Actions are systemctl verbs; there is no parameter editor (the settings
// are argv on the unit).

function reNum(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 1 : d); }
function reInt(x) { return x == null ? '—' : Number(x).toLocaleString(); }
function reStateBadge(st) {
  if (!st.installed) return '<span class="status-badge gray">not installed</span>';
  if (st.running) return st.ready ? '<span class="status-badge green">serving</span>' : '<span class="status-badge yellow">active · engine loading</span>';
  const cls = st.active === 'failed' ? 'red' : st.active === 'activating' ? 'yellow' : 'gray';
  return `<span class="status-badge ${cls}">${escapeHtml(st.active || 'inactive')}${st.sub && st.sub !== st.active ? ' · ' + escapeHtml(st.sub) : ''}</span>`;
}

async function page_radiance_engine() {
  const el = $('page-content');
  el.innerHTML = '<h2>Radiance</h2><p class="help">Loading…</p>';
  let st;
  try { st = await API.get('/api/radiance_engine'); }
  catch (e) { el.innerHTML = `<h2>Radiance</h2><div class="error">${escapeHtml(e.message)}</div>`; return; }
  const admin = currentRole === 'admin';
  let html = '<h2>Radiance <span class="help">standalone inference engine (stilldeadcode/radiance)</span></h2>';

  if (!st.installed) {
    html += `<div class="alert alert-warning">No <code>${escapeHtml(st.unit)}.service</code> on this node. The module expects the Radiance launcher's systemd unit (set <code>DASHBOARD_RADIANCE_ENGINE_UNIT</code> if yours is named differently). This is the standalone <code>stilldeadcode/radiance</code> engine; the vLLM fork has its own page, vLLM Radiance.</div>`;
    el.innerHTML = html; return;
  }
  const su = st.startup || {}, m = st.stats || {}, conflicts = st.conflicts || [];
  const activeConflicts = conflicts.filter(c => c.active).map(c => c.unit);
  const warm = Math.max(1, Math.round(st.warm_start_s / 60));
  const budget = su.budget || {};

  // ── Unit card
  html += `<div class="card"><h3>Unit <code>${escapeHtml(st.unit)}.service</code> ${reStateBadge(st)}</h3>
    <table class="table kv"><tbody>
      <tr><td>Since</td><td>${escapeHtml(st.since || '—')}${st.nrestarts ? ` · <span class="status-badge yellow">${st.nrestarts} systemd restart(s)</span>` : ''}</td></tr>
      <tr><td>Container</td><td><code>${escapeHtml(st.container)}</code> · port ${st.port}${st.gpus ? ` · GPUs ${escapeHtml(st.gpus)}` : ''}${st.tp ? ` · TP=${st.tp}` : ''}${st.image ? ` · <code>${escapeHtml(st.image)}</code>` : ''}</td></tr>
      ${conflicts.map(c => `<tr><td>${escapeHtml(c.unit)}</td><td>${c.present ? `${c.active ? '<span class="status-badge red">running</span>' : '<span class="status-badge gray">stopped</span>'} · ${c.enabled ? '<strong>enabled at boot</strong>' : 'disabled at boot'} — conflicts with this unit (same GPUs)` : 'not installed'}</td></tr>`).join('')}
    </tbody></table>
    ${admin ? `<div class="toolbar">
      ${st.running
        ? `<button class="btn btn-sm btn-outline" onclick="reAction('stop')">Stop</button>
           <button class="btn btn-sm btn-outline" onclick="reAction('restart')" title="Restart ≈ ${warm} min (weights back into VRAM)">Restart</button>`
        : `<button class="btn btn-sm" onclick="reAction('start')" title="Start ≈ ${warm} min">Start${activeConflicts.length ? ' (stops ' + escapeHtml(activeConflicts.join(', ')) + ')' : ''}</button>`}
      <span style="margin-left:auto"></span>
      <label class="help"><input type="checkbox" ${st.enabled ? 'checked' : ''} onchange="reAction(this.checked ? 'enable' : 'disable')"> Enabled at boot</label>
    </div>${activeConflicts.length ? `<p class="help">Start / restart — <strong>${escapeHtml(activeConflicts.join(', '))} is running and will be STOPPED</strong> (the unit declares Conflicts=).</p>` : ''}` : ''}
  </div>`;

  // ── Engine card: the boot receipt the engine printed, plus /stats identity
  html += '<div class="card"><h3>Engine</h3>';
  if (!st.running) html += '<p class="help">Not running.</p>';
  else {
    const mods = (st.models || []).map(x => `<code>${escapeHtml(x.id)}</code>${x.max_model_len ? ` <span class="help">${reInt(x.max_model_len)} ctx</span>` : ''}`).join(' ');
    const p2p = su.p2p === true ? '<span class="status-badge green">peer to peer</span>'
      : su.p2p === false ? '<span class="status-badge red">host memory (no P2P)</span>' : '<span class="status-badge gray">unknown</span>';
    html += `<table class="table kv"><tbody>
      <tr><td>Model id</td><td>${mods || `<span class="help">(engine not answering yet${st.models_error ? ': ' + escapeHtml(st.models_error) : ''})</span>`}${su.arch ? ` · ${escapeHtml(su.arch)} · ${su.layers} layers` : ''}${m.version ? ` · radiance ${escapeHtml(m.version)}` : ''}</td></tr>
      <tr><td>Cards</td><td>${su.devices ? `${su.devices} device(s)` : '—'} · link ${p2p}${su.wire_lossy ? ' · <span class="status-badge gray" title="--tp-wire wht6: all-reduce payloads from 128 KiB up are quantised; single-sequence decode stays exact">lossy wire</span>' : ''}</td></tr>
      <tr><td>VRAM per card</td><td>${budget.experts != null ? `experts <strong>${reNum(budget.experts, 2)} GiB</strong> · KV cache <strong>${reNum(budget['kv cache'], 2)} GiB</strong> · static ${reNum(budget['static weights'], 2)} GiB · of ${reNum(budget['card total'], 2)} GiB` : '—'}${su.kv_sessions != null ? `<br><span class="help">KV backs ${su.kv_backing_pct}% of the worst case: ${reNum(su.kv_sessions, 1)} full-length sessions</span>` : ''}</td></tr>
      <tr><td>Host side</td><td>${su.ngram_gib != null ? `n-gram table ${reNum(su.ngram_gib, 1)} GiB in <code>${escapeHtml(su.ngram_where)}</code>` : '—'}${su.drafter ? ` · drafter <code>${escapeHtml(su.drafter)}</code> × ${su.draft_tokens}` : ''}</td></tr>
      <tr><td>Status</td><td>${su.ready ? `<span class="status-badge green">listening</span> <code>${escapeHtml(su.listening || '')}</code>` : `<span class="status-badge yellow">loading</span> <span class="help">≈ ${warm} min</span>`}${m.startup_s != null ? ` · up in ${reInt(m.startup_s)} s` : ''}${su.headroom_warnings ? ` · <span class="status-badge yellow">${su.headroom_warnings} headroom warning(s)</span>` : ''}${su.errors ? ` · <span class="status-badge red">${su.errors} error line(s) in the log</span>` : ''}</td></tr>
    </tbody></table>`;
    if (m.cards && m.cards.length) html += `<p class="help">${m.cards.map(c => `card ${c.index}: ${reNum(c.vram_used_gib, 1)} / ${reNum(c.vram_total_gib, 1)} GiB`).join(' · ')}</p>`;
    if (su.lines && su.lines.length) html += `<details><summary class="help">The engine's own boot receipt (last start)</summary><pre class="log" style="white-space:pre-wrap;max-height:30vh;overflow:auto">${escapeHtml(su.lines.join('\n'))}</pre></details>`;
  }
  html += '</div>';

  // ── Throughput card: /stats is live; the log has one line per finished request.
  html += '<div class="card"><h3>Throughput</h3>';
  if (!st.running) html += '<p class="help">Not running.</p>';
  else if (!st.ready) html += '<p class="help">Engine loading — figures appear once /v1/models answers.</p>';
  else {
    html += `<div class="stat-row">
      <div class="stat"><div class="stat-v">${reNum(m.decode_tps, 0)}</div><div class="stat-l">decode tok/s (recent)</div></div>
      <div class="stat"><div class="stat-v">${reNum(m.step_ms_recent, 2)}</div><div class="stat-l">ms per decode step${m.step_ms_best != null ? ` · best ${reNum(m.step_ms_best, 2)}` : ''}</div></div>
      <div class="stat"><div class="stat-v">${reNum(m.draft_accept_pct, 0)}%</div><div class="stat-l">draft acceptance${(m.draft_positions_pct || []).length ? ` · ${m.draft_positions_pct.map(p => reNum(p, 0)).join(' / ')}` : ''}</div></div>
      <div class="stat"><div class="stat-v">${reNum(m.kv_pct, 1)}%</div><div class="stat-l">KV in use · ${reInt(m.kv_tokens_used)} of ${reInt(m.kv_tokens_total)} tokens</div></div>
      <div class="stat"><div class="stat-v">${reInt(m.running)} / ${reInt(m.waiting)}</div><div class="stat-l">running / waiting</div></div>
      <div class="stat"><div class="stat-v">${reInt(m.requests_total)}</div><div class="stat-l">requests since start${m.prefill_tps ? ` · prefill ${reNum(m.prefill_tps, 0)} tok/s` : ''}</div></div>
    </div>`;
    if (!su.requests_total) html += '<p class="help">No finished requests in the log tail yet — the engine prints one line per request; they appear here.</p>';
    else html += `<table class="table"><thead><tr><th>Request</th><th>Client</th><th>Finish</th><th>Prompt</th><th>Generated</th><th>Reasoning</th><th>TTFT</th><th>tok/s</th><th>Seconds</th></tr></thead><tbody>
      ${(su.requests || []).slice().reverse().map(r => `<tr><td class="mono">${escapeHtml(r.id.slice(-12))}</td><td class="mono">${escapeHtml(r.client)}</td><td>${escapeHtml(r.finish)}</td><td>${reInt(r.prompt)}${r.cached ? ` <span class="help">(${reInt(r.cached)} cached)</span>` : ''}</td><td>${reInt(r.gen)}</td><td>${r.reasoning != null ? reInt(r.reasoning) : '—'}</td><td>${reNum(r.ttft_s, 2)} s</td><td><strong>${reNum(r.tps, 1)}</strong></td><td>${reNum(r.seconds, 2)}</td></tr>`).join('')}
      </tbody></table>`;
    html += '<div id="re-spark"></div>';
  }
  html += '</div>';

  // ── Launch settings (read-only: the flags on the unit's ExecStart)
  const named = st.named_args || [], other = st.other_args || {};
  html += `<div class="card"><h3>Launch settings</h3>
    <p class="help">The engine's flags on <code>ExecStart</code> of <code>${escapeHtml(st.fragment || st.unit + '.service')}</code>, plus the launcher's environment. Read-only here — edit the unit and <code>daemon-reload</code> to change them.</p>
    <table class="table"><thead><tr><th>Setting</th><th>Value</th></tr></thead><tbody>
    ${named.map(a => `<tr><td>${escapeHtml(a.label)} <code class="help">${escapeHtml(a.flag)}</code></td><td class="mono">${escapeHtml(a.value == null ? '—' : String(a.value))}</td></tr>`).join('')}
    ${Object.keys(other).sort().map(k => `<tr><td><code>--${escapeHtml(k)}</code></td><td class="mono">${escapeHtml(String(other[k]))}</td></tr>`).join('')}
    ${Object.keys(st.env || {}).sort().map(k => `<tr><td>env <code>${escapeHtml(k)}</code></td><td class="mono">${escapeHtml(String(st.env[k]))}</td></tr>`).join('')}
    </tbody></table></div>`;

  html += `<div class="toolbar"><button class="btn btn-sm btn-outline" onclick="reLogs('container')">Container log</button>
    <button class="btn btn-sm btn-outline" onclick="reLogs('journal')">Unit journal</button></div>`;
  el.innerHTML = html;
  if (st.ready) reFillSpark(st.unit);
}

async function reFillSpark(unit) {
  try {
    const r = await API.get(`/api/history?metric=radiance_engine_step_ms&label=${encodeURIComponent(unit)}&since=86400`);
    const pts = (r.points || []).filter(p => p && p[1] != null);
    const box = $('re-spark');
    if (box && pts.length > 2) box.innerHTML = `<span class="help">ms per decode step per tick, last 24 h</span> ${sparkline(pts)}`;
  } catch (e) { /* history off or empty */ }
}

async function reAction(action) {
  const warn = { stop: 'Stop the Radiance server? Clients on this node lose the inference API until it is started again.',
                 restart: 'Restart the server now? Reloading the weights into VRAM takes a minute or two; in-flight requests are dropped.' }[action];
  if (warn && !confirm(warn)) return;
  try {
    const r = await API.post('/api/radiance_engine/action', { action });
    if (r && r.note) alert(r.note);
  } catch (e) { alert(e.message); }
  page_radiance_engine();
}

async function reLogs(source) {
  try {
    const r = await API.get(`/api/radiance_engine/logs?source=${encodeURIComponent(source)}&tail=400`);
    openModal(`Radiance ${source === 'journal' ? 'unit journal' : 'container log'} — ${r.name}`, `<pre class="log" style="max-height:60vh;overflow:auto">${escapeHtml(r.logs || '(empty)')}</pre>`, { wide: true });
  } catch (e) { alert(e.message); }
}

// Dashboard card — from the /api/summary block (unit state, /v1/models
// readiness, the engine's own /stats figures).
function dashcard_radiance_engine(ctx) {
  const a = ctx.s.radiance_engine;
  if (!a) return '';            // no unit here — no card
  const dot = !a.running ? (a.state === 'failed' ? 'red' : 'gray') : a.ready ? 'green' : 'yellow';
  const val = a.running
    ? (a.ready ? escapeHtml(a.model || '?') : '<span class="card-unit">loading</span>')
    : `<span class="card-unit">${escapeHtml(a.state || 'stopped')}</span>`;
  const sub = a.ready
    ? `${a.decode_tps != null ? `<strong>${reNum(a.decode_tps, 0)}</strong> tok/s` : 'idle'}${a.step_ms != null ? ` · ${reNum(a.step_ms, 1)} ms/step` : ''}${a.draft_accept_pct != null ? ` · accept ${reNum(a.draft_accept_pct, 0)}%` : ''}${a.requests_running ? ` · ${a.requests_running} running` : ''}`
    : `${a.gpus ? `GPUs ${escapeHtml(a.gpus)}` : ''}${a.tp ? ` · TP=${a.tp}` : ''}`;
  return `
    <div class="card card-link" onclick="showPage('radiance_engine')">
      <div class="card-head"><span class="status-dot ${dot}"></span>Radiance</div>
      <div class="card-value" style="font-size:1.2em">${val}</div>
      <div class="card-sub">${sub}</div>
      <div class="card-sub">port ${a.port}${a.p2p === false ? ' · <span class="status-badge red">no P2P</span>' : ''}${a.enabled === false ? ' · <span class="status-badge yellow">not at boot</span>' : ''}${a.nrestarts ? ` · ${a.nrestarts} restarts` : ''}${a.errors ? ` · <span class="status-badge red">${a.errors} errors</span>` : ''}</div>
    </div>`;
}
