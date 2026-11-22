/* Amber Terminal theme chrome. Registered as window.NexusThemes.amber; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch. */
window.NexusThemes = window.NexusThemes || {};
window.NexusThemes.amber = (function () {
  let fk = null, oia = null;
  const KEYS = [['F1', 'HELP', 'dashboard'], ['F2', 'REFRESH', null], ['F3', 'END', 'dashboard'], ['F4', 'POOLS', 'zfs'], ['F5', 'SHARES', 'smb'],
                ['F6', 'DOCKER', 'docker'], ['F9', 'LOGS', 'logs'], ['F12', 'CANCEL', 'dashboard']];
  function host() { return (typeof stripInfo === 'object' && stripInfo.host) || location.hostname; }
  function paint() { if (oia) oia.querySelector('.h').textContent = host(); }
  return {
    mount() {
      fk = document.createElement('div'); fk.className = 'amber-fkeys'; fk.setAttribute('aria-hidden', 'true');
      fk.innerHTML = KEYS.map(k => '<span data-go="' + (k[2] || '') + '"><b>' + k[0] + '</b>' + k[1] + '</span>').join('');
      fk.addEventListener('click', e => { const s = e.target.closest('[data-go]'); if (!s) return;
        if (s.dataset.go) { if (typeof showPage === 'function') showPage(s.dataset.go); } else if (typeof renderPage === 'function') renderPage(currentPage); });
      oia = document.createElement('div'); oia.className = 'amber-oia'; oia.setAttribute('aria-hidden', 'true');
      oia.innerHTML = '<span>4B</span><span>⌨</span><span>NEXUS</span><span class="h"></span><span class="sp"></span><span>SESSION A</span>';
      document.body.appendChild(fk); document.body.appendChild(oia);
      document.addEventListener('nexus:app', paint); document.addEventListener('nexus:page', paint); paint();
    },
    unmount() {
      document.removeEventListener('nexus:app', paint); document.removeEventListener('nexus:page', paint);
      if (fk) fk.remove(); if (oia) oia.remove(); fk = oia = null;
    },
  };
})();
