/* NeXTSTEP theme chrome. Registered as window.NexusThemes.nextstep; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch. */
window.NexusThemes = window.NexusThemes || {};
window.NexusThemes.nextstep = (function () {
  /* The Dock: one tile per sidebar category (first visible page), built from
     the live nav so it follows this node's modules; the cube is Dashboard. */
  let bar = null, dock = null;
  function host() { return (typeof stripInfo === 'object' && stripInfo.host) || location.hostname; }
  function paint() { if (bar) bar.querySelector('.t').textContent = 'Nexus Dashboard — ' + host(); }
  function build() {
    if (!dock) return;
    const tiles = ['<button type="button" class="next-tile" data-go="dashboard" title="Dashboard"><span class="next-cube"></span></button>'];
    for (const g of document.querySelectorAll('.nav-list li.nav-group')) {
      if (g.classList.contains('group-hidden')) continue;
      const a = [...g.querySelectorAll('li > a[data-page]')].find(x => !x.parentElement.classList.contains('module-hidden')
        && !(x.parentElement.classList.contains('nav-admin-only') && document.body.classList.contains('readonly')));
      if (!a) continue;
      const cat = g.querySelector('.nav-cat');
      const label = (cat ? cat.firstChild.textContent.trim() : '').split(/[\s\/]+/)[0].slice(0, 7).toUpperCase();
      const svg = a.querySelector('svg');
      tiles.push('<button type="button" class="next-tile" data-go="' + a.dataset.page + '" title="' + (cat ? cat.firstChild.textContent.trim() : '') + '">'
        + (svg ? svg.outerHTML : '') + label + '</button>');
      if (tiles.length >= 11) break;
    }
    dock.innerHTML = tiles.join('');
  }
  return {
    mount() {
      bar = document.createElement('div'); bar.className = 'next-titlebar'; bar.setAttribute('aria-hidden', 'true');
      bar.innerHTML = '<span class="next-tb min"></span><span class="t"></span><span class="next-tb close"></span>';
      dock = document.createElement('nav'); dock.className = 'next-dock'; dock.setAttribute('aria-label', 'Dock');
      dock.addEventListener('click', e => { const b = e.target.closest('.next-tile'); if (b && typeof showPage === 'function') showPage(b.dataset.go); });
      document.body.appendChild(bar); document.body.appendChild(dock);
      document.addEventListener('nexus:nav', build); document.addEventListener('nexus:app', build);
      document.addEventListener('nexus:app', paint); document.addEventListener('nexus:page', paint);
      if (document.querySelector('.nav-list li')) build(); paint();
    },
    unmount() {
      document.removeEventListener('nexus:nav', build); document.removeEventListener('nexus:app', build);
      document.removeEventListener('nexus:app', paint); document.removeEventListener('nexus:page', paint);
      if (bar) bar.remove(); if (dock) dock.remove(); bar = dock = null;
    },
  };
})();
