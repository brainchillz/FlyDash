/* Solarized Paper theme chrome. Registered as window.NexusThemes.solarized; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch. */
window.NexusThemes = window.NexusThemes || {};
window.NexusThemes.solarized = (function () {
  let c = null;
  function host() { return (typeof stripInfo === 'object' && stripInfo.host) || location.hostname; }
  function paint() { if (c) c.textContent = 'Nexus Dashboard · ' + host() + ' · set in Source Serif, Source Sans and Source Code Pro · Solarized by Ethan Schoonover'; }
  return {
    mount() { c = document.createElement('div'); c.className = 'solar-colophon'; document.body.appendChild(c);
      document.addEventListener('nexus:app', paint); document.addEventListener('nexus:page', paint); paint(); },
    unmount() { document.removeEventListener('nexus:app', paint); document.removeEventListener('nexus:page', paint); if (c) c.remove(); c = null; },
  };
})();
