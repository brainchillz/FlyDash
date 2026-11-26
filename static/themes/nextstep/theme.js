/* NeXTSTEP. Registered as window.NexusThemes.nextstep; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch.
   In the desktop shell the whole theme is CSS: the black title bar and its
   square buttons are the shell's .win-title / .win-btn, and the Dock is the
   shell's #taskbar (every .tb a tile), all restyled in theme.css. There is
   nothing to inject and nothing to clean up. The hooks stay so the
   manifest's "js" entry keeps resolving. */
(function () {
  'use strict';
  window.NexusThemes = window.NexusThemes || {};
  window.NexusThemes.nextstep = {
    mount() {},
    unmount() {},
  };
})();
