/* Brushed Metal theme chrome. Registered as window.NexusThemes.brushed; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch. */
window.NexusThemes = window.NexusThemes || {};
window.NexusThemes.brushed = (function () {
  let top = null, bottom = null;
  function host() { return (typeof stripInfo === 'object' && stripInfo.host) || location.hostname; }
  function paint() { if (top) top.lastChild.textContent = 'Nexus Dashboard — ' + host(); }
  return {
    mount() {
      top = document.createElement('div'); top.className = 'brushed-titlebar'; top.setAttribute('aria-hidden', 'true');
      top.innerHTML = '<span class="brushed-gems"><span class="brushed-gem r"></span><span class="brushed-gem y"></span><span class="brushed-gem g"></span></span><span></span>';
      bottom = document.createElement('div'); bottom.className = 'brushed-bottombar'; bottom.setAttribute('aria-hidden', 'true');
      bottom.innerHTML = '<span>Nexus Dashboard</span><span class="brushed-grip"></span>';
      document.body.appendChild(top); document.body.appendChild(bottom);
      document.addEventListener('nexus:app', paint); document.addEventListener('nexus:page', paint); paint();
    },
    unmount() {
      document.removeEventListener('nexus:app', paint); document.removeEventListener('nexus:page', paint);
      if (top) top.remove(); if (bottom) bottom.remove(); top = bottom = null;
    },
  };
})();
