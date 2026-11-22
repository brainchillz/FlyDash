# Nexus Dashboard themes

Since 3.11.0 the dashboard's look is a **theme**: a directory holding a
stylesheet that redefines the design tokens `static/css/style.css` is built
on, plus an optional script for effects a stylesheet cannot express. Nine
ship with the app — **Nexus** (the default burnt orange), **Phosphor**,
**Construct**, **Pip-Boy**, **Brushed Metal**, **NeXTSTEP**, **Amber
Terminal**, **Solarized Paper** and **Amiga Workbench** — and operators drop
their own into a directory, exactly as plugins work.

```
static/themes/<id>/        # built-in, ships with the app
<app dir>/themes/<id>/     # operator drop-ins, e.g. /opt/nexus-dashboard/themes/
  theme.json               # manifest (below)
  theme.css                # the token block + any overrides
  theme.js                 # optional: effects, extra chrome
  *.woff2 …                # anything the stylesheet references, relative
```

Every signed-in user picks a theme on **My Account → Appearance** (stored in
that browser, like the dark/light switch always was). An admin sets the
**node default** on the same page for browsers that have not chosen; it is
written to `themes.json` next to `app.py`. `GET /api/themes` lists the
catalog and the default; `POST /api/themes/default {theme, variant}` sets it.

Install = copy the directory in. No restart: the catalog rescans when the
directory or any theme file changes, and the stylesheet URL carries a
version tag derived from the file mtimes, so an edited `theme.css` is
refetched on the next page load. Uninstall = remove the directory; a browser
that had picked it falls back to the node default. Directory name = theme id:
`^[a-z][a-z0-9-]{1,31}$`, and it may not reuse a built-in id. A theme that
fails validation is listed with its error on the Appearance page and cannot
be selected; it never affects the rest of the UI.

**Trust.** A theme's `theme.js` runs in every browser that selects it, with
the session's full powers — the same as a plugin's script. Installing a
theme is therefore a root action over SSH, never an upload; `themes/` is
root-owned by the installers, and a drop-in whose directory or files are
world-writable is refused. A theme with no `theme.js` is pure CSS and
carries no such trust.

## theme.json

```json
{
  "id": "mytheme",                 // optional; must equal the directory name if present
  "name": "My Theme",
  "description": "One sentence shown on the Appearance page.",
  "author": "you",
  "version": "1.0",
  "modes": ["dark", "light"],      // which of the two the theme supports (at least one)
  "variants": [                    // optional: user-selectable flavours, e.g. HUD colours
    {"id": "green", "label": "Green"},
    {"id": "amber", "label": "Amber"}
  ],
  "variant_label": "HUD colour",   // label for the variant picker (default "Variant")
  "default_variant": "green",      // default: the first variant
  "css": "theme.css",              // default theme.css
  "js": "theme.js"                 // optional
}
```

## theme.css — the token contract

The app sets `data-theme="<id>"` and, when the theme has variants,
`data-theme-variant="<variant>"` on `<html>`; dark/light is the pre-existing
`html.theme-light` class. Key every rule on `html[data-theme="<id>"]`: that
outranks the stylesheet's own light block (same specificity, later file) and
keeps your rules from touching the page while a switch to another theme is in
flight.

```css
html[data-theme="mytheme"] {
  color-scheme: dark;
  --bg: …; --sidebar-bg: …; --card-bg: …;
  --border: …; --border-soft: …;
  --text: …; --text-muted: …;
  --primary: …; --primary-hover: …; --on-primary: …;
  --green: …; --yellow: …; --red: …;
}
html[data-theme="mytheme"][data-theme-variant="amber"] { /* variant overrides */ }
html[data-theme="mytheme"].theme-light { color-scheme: light; /* light values, declared last */ }
```

The thirteen tokens above are **required** — a theme missing any is refused,
because the component that used it would keep the previous theme's colour.
Everything else falls back to the Nexus value and may be overridden:
`--on-yellow`, `--red-hover`, `--yellow-hover`, `--hover`, `--hover-soft`,
`--well`, `--overlay`, `--raw-bg`, `--raw-fg`, `--console-bg` (the xterm
consoles read it at open time), `--knob`, `--radius`, `--sans`, `--mono`.
Status colours (`--green` / `--yellow` / `--red`) are semantic: a monochrome
theme still needs distinct warn and critical values, or the ZFS error badges
and the alerts stop reading at a glance.

Beyond tokens, override component rules freely (the built-in Construct and
Pip-Boy themes restyle buttons, tables, the machine strip and the login box).
Fonts must be **self-hosted** next to the stylesheet (`url(font.woff2)`
resolves relative to it): a dashboard page must not call out to a font CDN,
and the internet-facing nodes could not reach one anyway. Respect
`prefers-reduced-motion` for anything that moves.

## theme.js — hooks

Optional. It is appended once, after the app's own scripts, and stays loaded
for the life of the page (a script cannot be unloaded), so it registers hooks
and does its work in `mount`:

```js
window.NexusThemes = window.NexusThemes || {};
window.NexusThemes.mytheme = {
  mount(ctx) { /* add overlays, canvases, extra chrome; attach listeners */ },
  unmount()  { /* remove everything mount added, cancel animation frames */ },
};
```

`mount` is called when the theme is applied (which may be on the login
screen, before anyone is signed in) and `unmount` when the user switches to
another theme — leave the DOM as you found it. `ctx` offers
`isAuthed()`, `currentPage()` and `variant()`. The app fires document-level
events a theme can rebuild on:

| event            | when                                                   |
|------------------|--------------------------------------------------------|
| `nexus:nav`      | the sidebar was (re)rendered — module toggles, login   |
| `nexus:navigate` | a page was chosen, before it renders (`detail.page`)   |
| `nexus:page`     | a page finished rendering (`detail.page`)              |
| `nexus:login`    | the login screen is shown                              |
| `nexus:app`      | the app shell is shown after sign-in                   |
| `nexus:mode`     | dark/light flipped (`detail.light`)                    |
| `nexus:theme`    | a theme finished applying (`detail.id`, `detail.variant`) |

Globals from the app are available (`showPage(id)`, `currentPage`,
`uiManifest`), but prefer reading the live DOM: the Pip-Boy theme builds its
tab bar from the rendered sidebar so it reflects this node's enabled modules
and the viewer's role without knowing the manifest's shape.

## The built-ins as worked examples

- **Phosphor** — `theme.css` only: the token blocks, dark and light. Start here.
- **Construct** — tokens + component overrides + self-hosted fonts +
  `theme.js` for the glyph rain and the CRT overlays, with the rain colour
  read from tokens so the light printout gets dark rain on paper.
- **Pip-Boy** — variants (`data-theme-variant` for the four HUD colours), a
  light mode that drops the effects entirely, and `theme.js` that adds a tab
  bar and relocates nothing: the machine strip moves to the bottom with CSS
  `order`, so a switch back leaves no trace.
- **Brushed Metal / NeXTSTEP / Amiga Workbench** — window chrome around the
  app. `body` is a flex row of sidebar + main column; these themes set
  `flex-wrap: wrap` on it and append full-width bars with `order: -1` (title
  bars) and `order: 99` (status / sizing bars), then give the two columns an
  explicit `height: calc(100vh - <bars>)` so the window always fills. NeXTSTEP's
  Dock is a third column (`order: 1`) built from the live sidebar, one tile per
  category.
- **Amber Terminal / Solarized Paper** — the same trick for a bottom bar only
  (an F-key row and operator information area; a colophon line), and the
  self-hosted `@font-face` pattern for IBM Plex Mono and the Source family.
