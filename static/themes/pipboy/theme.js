/* Pip-Boy theme: the tab bar across the top of the screen, the CRT overlays
   and the rolling refresh band. Registered as window.NexusThemes.pipboy and
   driven by the loader in static/js/themes.js.

   The tabs are built from the LIVE sidebar (whatever the nav manifest
   rendered on this node, after module toggles and read-only hiding), so a
   node with the AI modules off simply has no AI tab. Each tab opens the first
   visible page of its first group; the active tab follows the active page. */
(function () {
  'use strict';
  // Tab → the nav categories it gathers (matched on the category label).
  // Anything unmatched (a plugin's own category, say) lands in SYSTEM.
  const TABS = [
    ['STAT', null],
    ['STORAGE', /storage/i],
    ['SHARING', /shar/i],
    ['AI', /^ai\b/i],
    ['CONTAINERS', /lxd|incus|docker|container/i],
    ['NET', /\bweb\b|dns|network/i],
    ['SYSTEM', null],
  ];
  let bar = null, roll = null, overlays = [];

  function visibleLinks(group) {
    return [...group.querySelectorAll('li > a[data-page]')].filter(a => {
      const li = a.parentElement;
      if (li.classList.contains('module-hidden')) return false;
      if (li.classList.contains('nav-admin-only') && document.body.classList.contains('readonly')) return false;
      return true;
    });
  }
  function tabFor(label) {
    for (const [name, re] of TABS) if (re && re.test(label)) return name;
    return 'SYSTEM';
  }
  // {tab: {first: pageId, cats: Set(label)}}
  function collect() {
    const out = {};
    for (const [name] of TABS) out[name] = { first: null, cats: new Set() };
    out.STAT.first = 'dashboard';
    const nav = document.querySelector('.nav-list');
    if (!nav) return out;
    for (const g of nav.querySelectorAll('li.nav-group')) {
      if (g.classList.contains('group-hidden')) continue;
      const cat = g.querySelector('.nav-cat');
      const label = cat ? cat.firstChild.textContent.trim() : '';
      const links = visibleLinks(g);
      if (!links.length) continue;
      const t = out[tabFor(label)];
      t.cats.add(label);
      if (!t.first) t.first = links[0].dataset.page;
    }
    return out;
  }
  function build() {
    const col = document.querySelector('.main-col');
    if (!col) return;
    if (!bar) {
      bar = document.createElement('nav');
      bar.className = 'pipboy-tabs';
      bar.setAttribute('aria-label', 'Sections');
      bar.addEventListener('click', e => {
        const b = e.target.closest('.pipboy-tab');
        if (!b || typeof showPage !== 'function') return;
        showPage(b.dataset.go);
      });
      col.insertBefore(bar, col.firstChild);
    }
    const tabs = collect();
    bar.innerHTML = TABS.filter(([n]) => tabs[n].first).map(([n]) =>
      `<button type="button" class="pipboy-tab" data-tab="${n}" data-go="${tabs[n].first}" data-cats="${[...tabs[n].cats].join('|').replace(/"/g, '')}">${n}</button>`).join('');
    sync();
  }
  function sync() {
    if (!bar) return;
    const a = document.querySelector('.nav-list a.active');
    let tab = 'STAT';
    if (a && a.dataset.page !== 'dashboard') {
      const g = a.closest('li.nav-group');
      const cat = g && g.querySelector('.nav-cat');
      tab = tabFor(cat ? cat.firstChild.textContent.trim() : '');
    } else if (!a && typeof currentPage === 'string' && currentPage !== 'dashboard') {
      tab = 'SYSTEM';   // a page reached outside the nav (My Account, Logs)
    }
    for (const b of bar.querySelectorAll('.pipboy-tab')) b.classList.toggle('active', b.dataset.tab === tab);
  }
  function onNav() { build(); }
  function onPage() { sync(); }

  window.NexusThemes = window.NexusThemes || {};
  window.NexusThemes.pipboy = {
    mount() {
      for (const cls of ['pipboy-crt', 'pipboy-vig']) {
        const d = document.createElement('div'); d.className = cls; d.setAttribute('aria-hidden', 'true');
        document.body.appendChild(d); overlays.push(d);
      }
      const col = document.querySelector('.main-col');
      if (col) { roll = document.createElement('div'); roll.className = 'pipboy-roll'; roll.setAttribute('aria-hidden', 'true'); col.appendChild(roll); }
      document.addEventListener('nexus:nav', onNav);
      document.addEventListener('nexus:navigate', onPage);   // the click, before the page loads
      document.addEventListener('nexus:page', onPage);
      document.addEventListener('nexus:app', onNav);
      if (document.querySelector('.nav-list li')) build();
    },
    unmount() {
      document.removeEventListener('nexus:nav', onNav);
      document.removeEventListener('nexus:navigate', onPage);
      document.removeEventListener('nexus:page', onPage);
      document.removeEventListener('nexus:app', onNav);
      if (bar) bar.remove(); bar = null;
      if (roll) roll.remove(); roll = null;
      overlays.forEach(o => o.remove()); overlays = [];
    },
  };
})();
