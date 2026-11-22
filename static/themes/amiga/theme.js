/* Amiga Workbench theme chrome. Registered as window.NexusThemes.amiga; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch. */
window.NexusThemes = window.NexusThemes || {};
window.NexusThemes.amiga = (function () {
  let sb = null, tb = null, sz = null;
  function host() { return (typeof stripInfo === 'object' && stripInfo.host) || location.hostname; }
  function paint() {
    const v = (typeof appVersion === 'string' && appVersion) || '';
    if (sb) sb.querySelector('.v').textContent = 'Nexus Workbench' + (v ? ' release ' + v : '');
    if (tb) tb.querySelector('.title').textContent = 'Nexus Dashboard - ' + host();
  }
  return {
    mount() {
      sb = document.createElement('div'); sb.className = 'amiga-screenbar'; sb.setAttribute('aria-hidden', 'true');
      sb.innerHTML = '<span class="v"></span><span class="sp"></span><span class="amiga-gadget"></span>';
      tb = document.createElement('div'); tb.className = 'amiga-titlebar'; tb.setAttribute('aria-hidden', 'true');
      tb.innerHTML = '<span class="close"></span><span class="title"></span><span class="drag"></span><span class="amiga-gadget"></span>';
      sz = document.createElement('div'); sz.className = 'amiga-sizebar'; sz.setAttribute('aria-hidden', 'true'); sz.innerHTML = '<span class="amiga-sizer"></span>';
      document.body.appendChild(sb); document.body.appendChild(tb); document.body.appendChild(sz);
      document.addEventListener('nexus:app', paint); document.addEventListener('nexus:page', paint); paint();
    },
    unmount() {
      document.removeEventListener('nexus:app', paint); document.removeEventListener('nexus:page', paint);
      [sb, tb, sz].forEach(e => e && e.remove()); sb = tb = sz = null;
    },
  };
})();
