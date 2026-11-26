/* Construct theme: glyph rain behind the login screen, plus the CRT overlays
   (the desktop shell has no sidebar to rain behind). Registered as
   window.NexusThemes.construct and driven by the loader in
   static/js/themes.js (mount on apply, unmount on switch).
   The rain is the Hive console's Rain class, simplified: one canvas per
   surface, density fixed, colour read from the theme's --rain-* tokens so the
   light printout gets dark-green rain on paper. Stops while the tab is hidden
   and never starts under prefers-reduced-motion. */
(function () {
  'use strict';
  const GLYPHS = 'ﾊﾐﾋｰｳｼﾅﾓﾆｻﾜﾂｵﾘｱﾎﾃﾏｹﾒｴｶｷﾑﾕﾗｾﾈｽﾀﾇﾍ0123456789Z:=*+-<>|';
  const CELL = 15, COLW = 12;
  const reduced = () => window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  class Rain {
    constructor(canvas, density) {
      this.c = canvas; this.ctx = canvas.getContext('2d'); this.density = density; this.streams = [];
      this.dpr = Math.min(2, window.devicePixelRatio || 1);
      this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(canvas.parentElement);
      this.resize();
    }
    resize() {
      const r = this.c.parentElement.getBoundingClientRect();
      this.w = Math.max(1, r.width); this.h = Math.max(1, r.height);
      this.c.width = Math.round(this.w * this.dpr); this.c.height = Math.round(this.h * this.dpr);
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      this.ctx.font = '12px "Share Tech Mono", ui-monospace, monospace'; this.ctx.textBaseline = 'top';
      this.slots = Math.max(1, Math.floor(this.w / COLW));
      this.target = Math.max(1, Math.round(this.slots * this.density));
      for (const s of this.streams) if (s.slot >= this.slots) s.slot = Math.floor(Math.random() * this.slots);
    }
    spawn(anywhere) {
      const len = 6 + Math.floor(Math.random() * 14);
      return { slot: Math.floor(Math.random() * this.slots), y: anywhere ? Math.random() * this.h : -len * CELL,
               len, v: 0.6 + Math.random() * 0.7,
               chars: Array.from({ length: len }, () => GLYPHS[Math.floor(Math.random() * GLYPHS.length)]) };
    }
    step(dt) {
      if (this.streams.length < this.target) this.streams.push(this.spawn(this.streams.length < this.target / 2));
      const cs = getComputedStyle(document.documentElement);
      const rgb = cs.getPropertyValue('--rain-rgb').trim() || '0,255,65';
      const head = cs.getPropertyValue('--rain-head').trim() || '#e8ffe8';
      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.w, this.h);
      for (const s of this.streams) {
        s.y += 48 * s.v * dt;
        if (Math.random() < 0.06) s.chars[Math.floor(Math.random() * s.len)] = GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
        const x = s.slot * COLW + 1;
        for (let i = 0; i < s.len; i++) {
          const y = s.y - i * CELL;
          if (y < -CELL || y > this.h) continue;
          ctx.fillStyle = i === 0 ? head : 'rgba(' + rgb + ',' + (1 - i / s.len).toFixed(2) + ')';
          ctx.fillText(s.chars[i], x, y);
        }
        if (s.y - s.len * CELL > this.h) Object.assign(s, this.spawn(false));
      }
    }
    destroy() { this.ro.disconnect(); this.c.remove(); }
  }

  let rains = [], raf = 0, last = 0, overlays = [];
  function frame(now) {
    const dt = Math.min(0.1, (now - last) / 1000); last = now;
    for (const r of rains) r.step(dt);
    raf = requestAnimationFrame(frame);
  }
  function start() {
    if (raf || reduced()) return;
    last = performance.now(); raf = requestAnimationFrame(frame);
  }
  function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; }
  function onVisibility() { if (document.hidden) stop(); else if (rains.length) start(); }

  function canvasIn(parent, density) {
    if (!parent) return null;
    const c = document.createElement('canvas');
    c.className = 'construct-rain';
    c.setAttribute('aria-hidden', 'true');
    parent.insertBefore(c, parent.firstChild);
    return new Rain(c, density);
  }

  window.NexusThemes = window.NexusThemes || {};
  window.NexusThemes.construct = {
    mount() {
      for (const cls of ['construct-crt', 'construct-vig']) {
        const d = document.createElement('div'); d.className = cls; d.setAttribute('aria-hidden', 'true');
        document.body.appendChild(d); overlays.push(d);
      }
      rains = [canvasIn(document.getElementById('login-screen'), 0.7)].filter(Boolean);
      document.addEventListener('visibilitychange', onVisibility);
      start();
    },
    unmount() {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
      rains.forEach(r => r.destroy()); rains = [];
      overlays.forEach(o => o.remove()); overlays = [];
    },
  };
})();
