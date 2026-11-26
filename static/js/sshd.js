// ─── SSH Server (the node's own OpenSSH daemon) ────────────────────────
// Page for the `sshd` module. Everything comes from /api/sshd: unit and
// socket state, the settings sshd reports as in force (`sshd -T`), the
// overrides in the dashboard's one drop-in, logged-in remote sessions and
// the host-key fingerprints. Actions are systemctl verbs; settings go to the
// drop-in and are proven with `sshd -t` before anything is reloaded.

function sshStateBadge(st) {
  if (!st.installed) return '<span class="status-badge gray">not installed</span>';
  if (st.accepting) return st.socket_activated && st.service_state !== 'active'
    ? '<span class="status-badge green" title="The socket is listening; sshd itself starts on the first connection">listening</span>'
    : '<span class="status-badge green">running</span>';
  if (st.service_state === 'failed') return '<span class="status-badge red">failed</span>';
  return '<span class="status-badge gray">stopped</span>';
}

function sshInput(k, canEdit) {
  const cur = k.override != null ? String(k.override) : '';
  const eff = k.value == null ? '' : String(k.value);
  const dis = canEdit ? '' : 'disabled';
  const hint = eff === '' ? (k.type === 'addrs' ? 'every address' : k.type === 'names' ? 'no restriction' : 'not set') : eff;
  if (k.type === 'yn' || k.type === 'enum') {
    return `<select data-key="${k.key}" class="ssh-knob form-control" ${dis} style="width:auto;min-width:16em">
      <option value="" ${cur === '' ? 'selected' : ''}>System value (${escapeHtml(hint)})</option>
      ${k.choices.map(c => `<option value="${escapeHtml(c)}" ${cur === c ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('')}</select>`;
  }
  if (k.type === 'int') {
    return `<input data-key="${k.key}" class="ssh-knob form-control mono" type="number" min="${k.range[0]}" max="${k.range[1]}" step="1" value="${escapeHtml(cur)}" placeholder="${escapeHtml(hint)}" ${dis} style="width:9em">`;
  }
  return `<input data-key="${k.key}" class="ssh-knob form-control mono" type="text" value="${escapeHtml(cur)}" placeholder="${escapeHtml(hint)}" ${dis} style="width:${k.type === 'ports' ? '12em' : '100%'}">`;
}

async function page_sshd() {
  const el = $('page-content');
  el.innerHTML = '<h2>SSH Server</h2><p class="help">Loading…</p>';
  let st;
  try { st = await API.get('/api/sshd'); }
  catch (e) { el.innerHTML = `<h2>SSH Server</h2><div class="error">${escapeHtml(e.message)}</div>`; return; }
  let html = '<h2>SSH Server <span class="help">OpenSSH daemon on this node</span></h2>';
  if (!st.installed) {
    html += `<div class="alert alert-warning">${escapeHtml(st.error || `No ${st.service}.service on this node — the OpenSSH server is not installed.`)}</div>`;
    el.innerHTML = html; return;
  }

  // ── Service card
  const boot = st.socket_activated
    ? `socket-activated: <code>${escapeHtml(st.service)}.socket</code> listens and starts <code>${escapeHtml(st.service)}.service</code> on the first connection`
    : `<code>${escapeHtml(st.service)}.service</code>`;
  html += `<div class="card"><h3>Service ${sshStateBadge(st)}</h3>
    <table class="table kv"><tbody>
      <tr><td>Unit</td><td>${boot}</td></tr>
      <tr><td>Listening on</td><td>port <strong>${(st.ports || []).map(escapeHtml).join(', ') || '?'}</strong>${st.version ? ` · ${escapeHtml(st.version)}` : ''}</td></tr>
      <tr><td>Daemon</td><td>${escapeHtml(st.service_state)}${st.service_sub && st.service_sub !== st.service_state ? ' · ' + escapeHtml(st.service_sub) : ''}${st.since && st.service_state === 'active' ? ` since ${escapeHtml(st.since)}` : ''}</td></tr>
    </tbody></table>
    <div class="toolbar">
      ${st.accepting
        ? `<button class="btn btn-sm btn-outline" onclick="sshAction('stop')">Stop</button>
           <button class="btn btn-sm btn-outline" onclick="sshAction('restart')" title="Restart the daemon. Sessions that are already open stay open.">Restart</button>
           <button class="btn btn-sm btn-outline" onclick="sshAction('reload')" title="Re-read the configuration without dropping anything">Reload</button>`
        : `<button class="btn btn-sm" onclick="sshAction('start')">Start</button>`}
      <span style="margin-left:auto"></span>
      <label class="help"><input type="checkbox" ${st.boot_enabled ? 'checked' : ''} onchange="sshAction(this.checked ? 'enable' : 'disable', this)"> Enabled at boot</label>
    </div></div>`;

  // ── Settings card
  const canEdit = !!st.editable;
  html += `<div class="card"><h3>Settings</h3>
    <p class="help">The value in grey is what sshd uses now${st.effective_source === 'sshd -T' ? ' (its own answer, <code>sshd -T</code>)' : ' (read from the config files; defaults sshd compiles in are not visible here)'}.
      Choose a value to override it; leave a field on "System value" or empty to hand it back. Overrides are written to
      <code>${escapeHtml(st.dropin)}</code> — <code>sshd_config</code> and other drop-ins are never edited — and the whole
      configuration is tested with <code>sshd -t</code> before it is reloaded. Sessions that are already open are not affected.
      ${canEdit ? '' : `<br><strong>Read-only on this node:</strong> ${escapeHtml(st.readonly_reason || '')}.`}
      ${st.other_dropins && st.other_dropins.length ? `<br>Other drop-ins present: <code>${st.other_dropins.map(escapeHtml).join('</code>, <code>')}</code>.` : ''}</p>`;
  for (const [g, label] of (st.groups || [])) {
    const rows = (st.settings || []).filter(k => k.group === g);
    if (!rows.length) continue;
    html += `<h4>${escapeHtml(label)}</h4><table class="table"><thead><tr><th style="width:42%">Setting</th><th style="width:36%">Override</th><th>In force</th></tr></thead><tbody>`;
    for (const k of rows) {
      const eff = k.value == null ? '—' : (k.value === '' ? (k.type === 'addrs' ? 'every address' : 'none') : k.value);
      html += `<tr><td>${escapeHtml(k.label)} <code class="help">${escapeHtml(k.key)}</code>${k.help ? `<div class="help" style="max-width:30em">${escapeHtml(k.help)}</div>` : ''}</td>
        <td>${sshInput(k, canEdit)}</td>
        <td class="mono">${escapeHtml(eff)}${k.override != null ? ' <span class="status-badge yellow" title="Set by the dashboard drop-in">override</span>' : ''}${k.shadowed ? ' <span class="status-badge red" title="A drop-in that sorts earlier sets this first, so the override has no effect">shadowed</span>' : ''}</td></tr>`;
    }
    html += '</tbody></table>';
  }
  if (canEdit) html += `<div class="toolbar">
      <button class="btn btn-sm" onclick="sshApply()" title="Write the drop-in, test the configuration, reload sshd">Apply</button>
      <span style="margin-left:auto"></span>
      <button class="btn btn-sm btn-outline" onclick="sshReset()" ${st.dropin_present ? '' : 'disabled'} title="Remove the dashboard drop-in — the system configuration applies again">Reset overrides</button>
    </div>`;
  if (st.dropin_text) html += `<details><summary class="help">Current drop-in</summary><pre class="log" style="white-space:pre-wrap">${escapeHtml(st.dropin_text)}</pre></details>`;
  html += '</div>';

  // ── Sessions + host keys
  const ses = st.sessions || [];
  html += `<div class="card"><h3>Remote sessions <span class="help">${ses.length}</span></h3>`;
  html += ses.length ? `<table class="table"><thead><tr><th>User</th><th>From</th><th>Terminal</th><th>Since</th></tr></thead><tbody>${
    ses.map(s => `<tr><td>${escapeHtml(s.user)}</td><td class="mono">${escapeHtml(s.from)}</td><td class="mono">${escapeHtml(s.tty)}</td><td>${escapeHtml(s.since)}</td></tr>`).join('')}</tbody></table>`
    : '<p class="help">Nobody is logged in remotely (sessions without a terminal, such as scp or rsync, are not listed).</p>';
  html += '</div>';
  const keys = st.host_keys || [];
  if (keys.length) html += `<div class="card"><h3>Host keys</h3>
    <p class="help">What a client is shown the first time it connects. Compare before accepting a key.</p>
    <table class="table"><thead><tr><th>Type</th><th>Bits</th><th>Fingerprint</th></tr></thead><tbody>${
      keys.map(k => `<tr><td>${escapeHtml(k.type)}</td><td>${k.bits}</td><td class="mono">${escapeHtml(k.fingerprint)}</td></tr>`).join('')}</tbody></table></div>`;
  el.innerHTML = html;
}

async function sshAction(action, box, confirmed) {
  try {
    const r = await API.post('/api/sshd/action', { action, confirm: !!confirmed });
    if (r && r.needs_confirm) {
      if (confirm(r.warning + '\n\nContinue?')) return sshAction(action, box, true);
      if (box) box.checked = !box.checked;
      return;
    }
    if (r && r.note) alert(r.note);
  } catch (e) { alert(e.message); }
  page_sshd();
}

function sshCollect() {
  const settings = {};
  document.querySelectorAll('.ssh-knob').forEach(i => { settings[i.dataset.key] = i.value.trim(); });
  return settings;
}

async function sshApply(confirmed) {
  try {
    const r = await API.post('/api/sshd/settings', { settings: sshCollect(), confirm: !!confirmed });
    if (r && r.needs_confirm) {
      if (confirm(r.warning + '\n\nApply anyway?')) return sshApply(true);
      return;
    }
    if (r && r.warning) alert(r.warning);
    else if (r && r.reload_error) alert('Settings written and valid, but sshd could not be reloaded: ' + r.reload_error);
    else if (r && r.noop) alert('Nothing to change.');
  } catch (e) { alert(e.message); }
  page_sshd();
}

async function sshReset(confirmed) {
  try {
    const r = await API.post('/api/sshd/settings/reset', { confirm: !!confirmed });
    if (r && r.needs_confirm) {
      if (confirm(r.warning + '\n\nRemove them?')) return sshReset(true);
      return;
    }
    if (r && r.reload_error) alert('Overrides removed, but sshd could not be reloaded: ' + r.reload_error);
  } catch (e) { alert(e.message); }
  page_sshd();
}
