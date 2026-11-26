/* Amiga Workbench. Registered as window.NexusThemes.amiga; the loader in
   static/js/themes.js calls mount() on apply and unmount() on switch.
   In the desktop shell the whole theme is CSS: the Workbench title bar, the
   gadgets and the screen bar are the shell's own .win-title, .win-btn and
   #taskbar restyled in theme.css, so there is nothing to inject and nothing
   to clean up. The hooks stay so the manifest's "js" entry keeps resolving. */
(function () {
  'use strict';
  window.NexusThemes = window.NexusThemes || {};
  window.NexusThemes.amiga = {
    mount() {},
    unmount() {},
  };
})();
