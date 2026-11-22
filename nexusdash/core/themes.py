"""Themes: the UI's visual identity as a drop-in directory.

A theme is a directory holding ``theme.json`` and a ``theme.css`` that
redefines the token block ``static/css/style.css`` is built on (``--bg``,
``--primary``, ``--text`` …), plus an optional ``theme.js`` for effects the
stylesheet cannot express (glyph rain, a tab bar). Two sources:

    static/themes/<id>/        built-in, shipped with the app, served by /static
    <app_dir>/themes/<id>/     operator drop-ins (DASHBOARD_THEMES_DIR), served
                               by /theme-assets/<id>/<file>

The browser picks a theme per user (localStorage, like the light/dark switch
it already had); an admin sets the node's default in ``themes.json`` next to
app.py for browsers that never chose. ``GET /api/themes`` and the asset route
are PUBLIC: the login screen is themed too, and nothing in them is secret —
they are static files and a list of names.

Trust model (same as plugins): a theme's JS runs in every signed-in browser
with the session's full powers, so installing one is a root action over SSH,
never an upload. A drop-in whose directory or files are world-writable is
refused, and a directory that fails validation is listed with its error
rather than loaded — a broken theme never takes the UI down.
"""
import os
import re
import json
import hashlib
import logging

from flask import Blueprint, jsonify, request, send_from_directory

from .config import APP_DIR, STATIC_DIR, write_json_atomic
from .runcmd import err

bp = Blueprint('themes', __name__)
_log = logging.getLogger(__name__)

RE_THEME_ID = re.compile(r'^[a-z][a-z0-9-]{1,31}\Z')
RE_VARIANT_ID = re.compile(r'^[a-z][a-z0-9-]{0,31}\Z')   # one char is fine for a variant
RE_ASSET_FILE = re.compile(r'^[A-Za-z0-9._-]+\Z')
MODES = ('dark', 'light')
DEFAULT_THEME = 'nexus'
BUILTIN_DIR = os.path.join(STATIC_DIR, 'themes')
THEMES_FILE = os.environ.get('DASHBOARD_THEMES_FILE', os.path.join(APP_DIR, 'themes.json'))
# Files a drop-in theme may serve. Everything a stylesheet can reference plus
# the manifest; never Python, never dotfiles.
ASSET_EXT = {'.css', '.js', '.woff2', '.woff', '.ttf', '.otf', '.png', '.svg',
             '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.json', '.md', '.txt'}
# The token contract: a theme's CSS must define every one of these, or a
# component ends up with the previous theme's colour (or none at all).
REQUIRED_TOKENS = ('--bg', '--sidebar-bg', '--card-bg', '--border', '--border-soft',
                   '--text', '--text-muted', '--primary', '--primary-hover',
                   '--on-primary', '--green', '--yellow', '--red')


def themes_dir():
    return os.environ.get('DASHBOARD_THEMES_DIR', os.path.join(APP_DIR, 'themes'))


# ─── Discovery ─────────────────────────────────────────────────────────

def _world_writable(*paths):
    for p in paths:
        try:
            if os.stat(p).st_mode & 0o002:
                return p
        except OSError:
            continue
    return None


def _file_version(*paths):
    """Short cache-busting tag from the files' mtimes + sizes, so an edited
    theme.css is refetched without anyone bumping a version by hand."""
    h = hashlib.sha1()
    for p in paths:
        try:
            st = os.stat(p)
            h.update(('%s:%d:%d;' % (os.path.basename(p), int(st.st_mtime), st.st_size)).encode())
        except OSError:
            h.update(b'missing;')
    return h.hexdigest()[:10]


def _nexus_entry():
    """The default identity: its tokens live in style.css itself, so it has no
    stylesheet to load and can never be missing."""
    return {'id': DEFAULT_THEME, 'name': 'Nexus', 'source': 'builtin',
            'description': 'Burnt orange on warm grey. The dashboard\'s own look.',
            'author': 'Nexus Dashboard', 'version': '1',
            'modes': list(MODES), 'variants': [], 'variant_label': '',
            'default_variant': None, 'css': None, 'js': None}


def _read_theme(tid, tdir, source):
    """One directory → a catalog entry, or an entry carrying ``error``."""
    entry = {'id': tid, 'name': tid, 'source': source, 'description': '', 'author': '',
             'version': '0', 'modes': [], 'variants': [], 'variant_label': '',
             'default_variant': None, 'css': None, 'js': None, '_path': tdir}
    if not RE_THEME_ID.match(tid):
        entry['error'] = 'directory name is not a valid theme id (^[a-z][a-z0-9-]{1,31}$)'
        return entry
    mpath = os.path.join(tdir, 'theme.json')
    try:
        with open(mpath) as f:
            man = json.load(f)
    except FileNotFoundError:
        entry['error'] = 'theme.json missing'
        return entry
    except (OSError, ValueError) as e:
        entry['error'] = 'theme.json unreadable: %s' % e
        return entry
    if not isinstance(man, dict):
        entry['error'] = 'theme.json must be a JSON object'
        return entry
    if man.get('id') not in (None, tid):
        entry['error'] = 'theme.json id %r does not match directory %r' % (man.get('id'), tid)
        return entry
    for k in ('name', 'description', 'author'):
        v = man.get(k, '')
        if not isinstance(v, str):
            entry['error'] = '%s must be a string' % k
            return entry
        entry[k] = v[:200]
    if not entry['name']:
        entry['name'] = tid
    entry['version'] = str(man.get('version', '0'))[:40]
    modes = man.get('modes', ['dark'])
    if (not isinstance(modes, list) or not modes
            or any(m not in MODES for m in modes)):
        entry['error'] = 'modes must be a non-empty list drawn from %s' % (MODES,)
        return entry
    entry['modes'] = [m for m in MODES if m in modes]
    variants = man.get('variants', [])
    if not isinstance(variants, list):
        entry['error'] = 'variants must be a list'
        return entry
    seen = set()
    for v in variants:
        if (not isinstance(v, dict) or not isinstance(v.get('id'), str)
                or not RE_VARIANT_ID.match(v['id']) or v['id'] in seen
                or not isinstance(v.get('label', ''), str)):
            entry['error'] = 'each variant needs a unique id (^[a-z][a-z0-9-]{0,31}$) and a label'
            return entry
        seen.add(v['id'])
        entry['variants'].append({'id': v['id'], 'label': (v.get('label') or v['id'])[:60]})
    vl = man.get('variant_label', 'Variant')
    entry['variant_label'] = vl[:40] if isinstance(vl, str) else 'Variant'
    dv = man.get('default_variant')
    if entry['variants']:
        if dv is None:
            dv = entry['variants'][0]['id']
        if dv not in seen:
            entry['error'] = 'default_variant %r is not one of the variants' % dv
            return entry
        entry['default_variant'] = dv
    css = man.get('css', 'theme.css')
    js = man.get('js')
    for label, fname, required in (('css', css, True), ('js', js, False)):
        if fname is None and not required:
            continue
        if not isinstance(fname, str) or not RE_ASSET_FILE.match(fname) \
                or os.path.splitext(fname)[1] != '.' + label:
            entry['error'] = '%s must name a .%s file in the theme directory' % (label, label)
            return entry
        if not os.path.isfile(os.path.join(tdir, fname)):
            entry['error'] = '%s file %r not found' % (label, fname)
            return entry
    if source != 'builtin':
        bad = _world_writable(tdir, os.path.join(tdir, css),
                              *([os.path.join(tdir, js)] if js else []))
        if bad:
            entry['error'] = 'refusing world-writable %s' % bad
            return entry
    try:
        with open(os.path.join(tdir, css), errors='replace') as f:
            text = f.read()
    except OSError as e:
        entry['error'] = 'css unreadable: %s' % e
        return entry
    missing = [t for t in REQUIRED_TOKENS if not re.search(re.escape(t) + r'\s*:', text)]
    if missing:
        entry['error'] = 'theme.css does not define required tokens: %s' % ', '.join(missing)
        return entry
    ver = _file_version(os.path.join(tdir, css), *([os.path.join(tdir, js)] if js else []))
    base = ('/static/themes/%s/' % tid) if source == 'builtin' else ('/theme-assets/%s/' % tid)
    entry['css'] = '%s%s?v=%s' % (base, css, ver)
    entry['js'] = ('%s%s?v=%s' % (base, js, ver)) if js else None
    return entry


def _scan(base, source):
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if name.startswith('.') or name.startswith('_'):
            continue
        tdir = os.path.join(base, name)
        if not os.path.isdir(tdir):
            continue
        out.append(_read_theme(name, tdir, source))
    return out


_cache = {'key': None, 'themes': []}


def _dir_stamp(d):
    """Directory mtime (a theme added/removed) plus the mtimes of every
    theme's manifest/css/js, so an edited theme.css gets a fresh cache tag
    without a restart or a touch of the directory."""
    out = []
    try:
        st = os.stat(d)
        out.append((d, int(st.st_mtime), st.st_ino))
        for name in sorted(os.listdir(d)):
            sub = os.path.join(d, name)
            if not os.path.isdir(sub) or name.startswith(('.', '_')):
                continue
            for f in ('theme.json', 'theme.css', 'theme.js'):
                try:
                    out.append((name, f, int(os.stat(os.path.join(sub, f)).st_mtime)))
                except OSError:
                    pass
    except OSError:
        out.append((d, None, None))
    return tuple(out)


def discover(force=False):
    """The catalog: Nexus first, then built-ins, then drop-ins. A drop-in that
    reuses a built-in id is listed with an error rather than shadowing it.
    Rescans when either directory's mtime changes (a theme added or removed);
    an edited theme.css inside an existing dir is picked up by the version
    tag, which is recomputed on every rescan — call with force after a POST."""
    key = (_dir_stamp(BUILTIN_DIR), _dir_stamp(themes_dir()))
    if not force and _cache['key'] == key:
        return _cache['themes']
    themes = [_nexus_entry()]
    taken = {DEFAULT_THEME}
    for e in _scan(BUILTIN_DIR, 'builtin'):
        if e['id'] in taken:
            e['error'] = 'duplicate theme id'
        taken.add(e['id'])
        themes.append(e)
    for e in _scan(themes_dir(), 'drop-in'):
        if e['id'] in taken and not e.get('error'):
            e['error'] = 'id %r is taken by a built-in theme' % e['id']
        taken.add(e['id'])
        themes.append(e)
    for e in themes:
        if e.get('error'):
            _log.warning('theme %r not loaded: %s', e['id'], e['error'])
    _cache['key'] = key
    _cache['themes'] = themes
    return themes


def get_theme(tid):
    for e in discover():
        if e['id'] == tid:
            return e
    return None


def _public(e):
    return {k: v for k, v in e.items() if not k.startswith('_')}


# ─── Node default ──────────────────────────────────────────────────────

def load_default():
    try:
        with open(THEMES_FILE) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError('not an object')
    except (OSError, ValueError):
        d = {}
    tid = d.get('theme') if isinstance(d.get('theme'), str) else DEFAULT_THEME
    var = d.get('variant') if isinstance(d.get('variant'), str) else None
    theme = get_theme(tid)
    if theme is None or theme.get('error'):
        # The configured theme vanished or broke: fall back rather than hand
        # the browser a stylesheet URL that 404s.
        return {'theme': DEFAULT_THEME, 'variant': None, 'configured': tid,
                'fallback': True}
    vids = {v['id'] for v in theme['variants']}
    if var not in vids:
        var = theme['default_variant']
    return {'theme': tid, 'variant': var}


# ─── Routes ────────────────────────────────────────────────────────────

@bp.route('/api/themes')
def themes_list():
    """Public: the login screen is themed before anyone signs in."""
    return jsonify({'themes': [_public(e) for e in discover()],
                    'default': load_default(),
                    'drop_in_dir': themes_dir()})


@bp.route('/api/themes/default', methods=['POST'])
def themes_set_default():
    from .auth import _is_admin
    if not _is_admin():
        return err('Admin required', 403)
    body = request.get_json(silent=True) or {}
    tid = body.get('theme')
    if not isinstance(tid, str) or not RE_THEME_ID.match(tid):
        return err('Invalid theme id', 400)
    theme = get_theme(tid)
    if theme is None:
        return err('Unknown theme %r' % tid, 404)
    if theme.get('error'):
        return err('Theme %r cannot be used: %s' % (tid, theme['error']), 400)
    var = body.get('variant')
    if var is not None:
        if not isinstance(var, str) or var not in {v['id'] for v in theme['variants']}:
            return err('Unknown variant %r for theme %r' % (var, tid), 400)
    elif theme['variants']:
        var = theme['default_variant']
    write_json_atomic(THEMES_FILE, {'theme': tid, 'variant': var}, mode=0o644)
    return jsonify({'success': True, 'default': load_default()})


@bp.route('/theme-assets/<theme>/<path:filename>')
def theme_asset(theme, filename):
    """Serve a drop-in theme's files (built-ins ride /static). Public, like
    /static: css, fonts and the theme's own script, nothing else — the
    extension allowlist keeps a stray .py or dotfile out of reach, and
    send_from_directory confines traversal."""
    e = get_theme(theme)
    if e is None or e.get('error') or e['source'] != 'drop-in' or not e.get('_path'):
        return err('Unknown theme', 404)
    parts = filename.split('/')
    if any(p in ('', '.', '..') or p.startswith('.') or not RE_ASSET_FILE.match(p)
           for p in parts):
        return err('Not found', 404)
    if os.path.splitext(parts[-1])[1].lower() not in ASSET_EXT:
        return err('Not found', 404)
    return send_from_directory(e['_path'], filename)
