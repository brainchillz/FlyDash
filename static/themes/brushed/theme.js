/* Brushed Metal. Registered as window.NexusThemes.brushed; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch.
   In the desktop shell the whole theme is CSS: the metal title bar and the
   three gems are the shell's .win-title / .win-btn, and the metal strip is
   the shell's #taskbar, all restyled in theme.css. There is nothing to
   inject and nothing to clean up. The hooks stay so the manifest's "js"
   entry keeps resolving. */
(function () {
  'use strict';
  window.NexusThemes = window.NexusThemes || {};
  window.NexusThemes.brushed = {
    mount() {},
    unmount() {},
  };
})();
