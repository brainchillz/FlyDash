"""Themes (3.11.0): drop-in directories that redefine the token block.

Covers discovery + validation of built-ins and drop-ins, the public catalog
and asset routes, the admin node default, and the contracts the UI relies
on: style.css has no colour outside the token blocks, every built-in defines
the full token set in every mode it claims, index.html carries the pre-paint
attach, and each theme's JS registers its hooks under its own id.
"""
import os
import re
import stat
import json
import pytest

import app
from nexusdash.core import themes as th
from nexusdash.core import auth

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTINS = ('phosphor', 'construct', 'pipboy', 'brushed', 'nextstep', 'amber', 'solarized', 'amiga')


def _reset():
    th._cache['key'] = None
    th._cache['themes'] = []


@pytest.fixture
def dropins(monkeypatch, tmp_path):
    d = tmp_path / 'themes'
    d.mkdir()
    monkeypatch.setenv('DASHBOARD_THEMES_DIR', str(d))
    monkeypatch.setattr(th, 'THEMES_FILE', str(tmp_path / 'themes.json'))
    _reset()
    yield d
    _reset()


def _write_theme(d, tid, manifest=None, css=None, js=None):
    t = d / tid
    t.mkdir()
    man = {'name': tid.title(), 'modes': ['dark']}
    if manifest is not None:
        man.update(manifest)
    (t / 'theme.json').write_text(json.dumps(man))
    if css is None:
        css = 'html[data-theme="%s"]{%s}' % (tid, ''.join('%s:#123;' % k for k in th.REQUIRED_TOKENS))
    if css is not False:
        (t / 'theme.css').write_text(css)
    if js:
        (t / 'theme.js').write_text(js)
    for p in [t] + [t / f for f in os.listdir(t)]:
        os.chmod(p, 0o755 if p.is_dir() else 0o644)      # the CI runner's umask is 002
    return t


@pytest.fixture
def client(monkeypatch, dropins):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('a', 'admin'))
    return app.app.test_client()


# ─── Built-ins ──────────────────────────────────────────────────────────

def test_builtin_catalog_is_clean(dropins):
    cat = {t['id']: t for t in th.discover()}
    assert cat['nexus']['css'] is None and cat['nexus']['modes'] == ['dark', 'light']
    for tid in BUILTINS:
        t = cat[tid]
        assert not t.get('error'), (tid, t.get('error'))
        assert t['source'] == 'builtin'
        assert t['css'].startswith('/static/themes/%s/theme.css?v=' % tid)
        assert t['modes'] == ['dark', 'light']
    assert cat['pipboy']['variants'][0]['id'] == 'green'
    assert cat['pipboy']['default_variant'] == 'green'
    assert cat['pipboy']['variant_label'] == 'HUD colour'
    assert cat['phosphor']['js'] is None
    assert cat['construct']['js'] and cat['pipboy']['js']
    assert list(cat)[0] == 'nexus'


def _css_blocks(text):
    """{selector: body} for every top-level rule block (no nesting in these files)."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return {sel.strip().lstrip('}').strip(): body for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', text)}


@pytest.mark.parametrize('tid', BUILTINS)
def test_builtin_defines_every_token_in_every_mode(tid):
    text = open(os.path.join(ROOT, 'static', 'themes', tid, 'theme.css')).read()
    blocks = _css_blocks(text)
    dark = blocks['html[data-theme="%s"]' % tid]
    light = blocks['html[data-theme="%s"].theme-light' % tid]
    for tok in th.REQUIRED_TOKENS:
        assert re.search(re.escape(tok) + r'\s*:', dark), (tid, 'dark', tok)
        assert re.search(re.escape(tok) + r'\s*:', light), (tid, 'light', tok)
    # Every rule is scoped to the theme, so nothing bleeds during a swap.
    for sel in blocks:
        if sel.startswith('@') or sel.startswith('from') or sel.startswith('to') or re.match(r'^\d', sel):
            continue
        assert 'html[data-theme="%s"]' % tid in sel, (tid, sel)


def test_builtin_fonts_are_self_hosted():
    """No request leaves the network for a font: every @font-face url() names
    a file beside the stylesheet, and that file exists."""
    for tid in BUILTINS:
        tdir = os.path.join(ROOT, 'static', 'themes', tid)
        text = open(os.path.join(tdir, 'theme.css')).read()
        assert 'fonts.googleapis.com' not in text and 'http' not in text
        for url in re.findall(r'url\(([^)]+)\)', text):
            url = url.strip('\'"')
            assert '/' not in url and os.path.isfile(os.path.join(tdir, url)), (tid, url)


def test_theme_js_registers_under_its_id():
    for tid in BUILTINS:
        p = os.path.join(ROOT, 'static', 'themes', tid, 'theme.js')
        if not os.path.exists(p):
            continue
        text = open(p).read()
        assert 'window.NexusThemes.%s' % tid in text, tid
        assert re.search(r'\bmount\s*\(', text) and re.search(r'\bunmount\s*\(', text), tid


# ─── Drop-ins ───────────────────────────────────────────────────────────

def test_dropin_loads_and_is_served(dropins, client):
    _write_theme(dropins, 'mytheme', {'modes': ['dark', 'light'], 'js': 'theme.js',
                                      'variants': [{'id': 'a', 'label': 'A'}]},
                 js='window.NexusThemes={mytheme:{}}')
    t = th.get_theme('mytheme')
    assert not t.get('error') and t['source'] == 'drop-in'
    assert t['css'].startswith('/theme-assets/mytheme/theme.css?v=')
    assert t['default_variant'] == 'a'
    r = client.get('/theme-assets/mytheme/theme.css')
    assert r.status_code == 200 and b'--bg' in r.data
    assert client.get('/theme-assets/mytheme/theme.js').status_code == 200
    # Refusals: traversal, dotfiles, wrong extension, built-ins via this route.
    assert client.get('/theme-assets/mytheme/../theme.css').status_code in (404, 400)
    assert client.get('/theme-assets/mytheme/.hidden.css').status_code == 404
    (dropins / 'mytheme' / 'evil.py').write_text('x')
    assert client.get('/theme-assets/mytheme/evil.py').status_code == 404
    assert client.get('/theme-assets/pipboy/theme.css').status_code == 404
    assert client.get('/theme-assets/nope/theme.css').status_code == 404


def test_dropin_validation_errors(dropins):
    _write_theme(dropins, 'Bad_Name')
    _write_theme(dropins, 'nocss', css=False)
    _write_theme(dropins, 'badmode', {'modes': ['sepia']})
    _write_theme(dropins, 'badvar', {'variants': [{'id': 'A!'}]})
    _write_theme(dropins, 'pipboy')                       # id taken by a built-in
    _write_theme(dropins, 'fewtokens', css='html{--bg:#000}')
    _write_theme(dropins, 'nomanifest', css=None)
    os.remove(dropins / 'nomanifest' / 'theme.json')
    _write_theme(dropins, 'badjson', css=None)
    (dropins / 'badjson' / 'theme.json').write_text('{not json')
    _write_theme(dropins, 'ww')
    os.chmod(dropins / 'ww' / 'theme.css', 0o666)
    cat = {t['id']: t for t in th.discover()}
    assert 'not a valid theme id' in cat['Bad_Name']['error']
    assert 'variant' in th._read_theme('v1', str(dropins / 'badvar'), 'drop-in')['error']
    assert 'not found' in cat['nocss']['error']
    assert 'modes' in cat['badmode']['error']
    assert 'variant' in cat['badvar']['error']
    assert 'built-in' in cat['pipboy']['error'] and cat['pipboy']['source'] == 'drop-in'
    assert 'required tokens' in cat['fewtokens']['error'] and '--primary' in cat['fewtokens']['error']
    assert 'missing' in cat['nomanifest']['error']
    assert 'unreadable' in cat['badjson']['error']
    assert 'world-writable' in cat['ww']['error']
    # Built-ins are untouched by any of it; the real pipboy is still first.
    assert [t for t in th.discover() if t['id'] == 'pipboy'][0]['source'] == 'builtin'


def test_dropin_edit_refreshes_version_tag(dropins):
    t = _write_theme(dropins, 'edit')
    v1 = th.get_theme('edit')['css']
    css = t / 'theme.css'
    css.write_text(css.read_text() + '\n/* edited */')
    os.utime(css, (os.stat(css).st_atime, os.stat(css).st_mtime + 5))
    v2 = th.get_theme('edit')['css']
    assert v1 != v2


# ─── Routes ─────────────────────────────────────────────────────────────

def test_catalog_route_is_public(dropins, monkeypatch):
    assert 'themes_list' in auth.PUBLIC_ENDPOINTS and 'theme_asset' in auth.PUBLIC_ENDPOINTS
    app.app.config['TESTING'] = True
    monkeypatch.setattr(app, '_resolve_identity', lambda: (None, None))
    c = app.app.test_client()
    r = c.get('/api/themes')
    assert r.status_code == 200
    j = r.get_json()
    assert j['default'] == {'theme': 'nexus', 'variant': None}
    assert {t['id'] for t in j['themes']} >= {'nexus', *BUILTINS}
    assert all('_path' not in t for t in j['themes'])
    assert j['drop_in_dir'] == str(dropins)
    # The node-default write is NOT public.
    assert c.post('/api/themes/default', json={'theme': 'pipboy'}).status_code in (401, 403)


def test_set_node_default(dropins, client, monkeypatch):
    r = client.post('/api/themes/default', json={'theme': 'pipboy', 'variant': 'amber'})
    assert r.status_code == 200 and r.get_json()['default'] == {'theme': 'pipboy', 'variant': 'amber'}
    assert json.load(open(th.THEMES_FILE)) == {'theme': 'pipboy', 'variant': 'amber'}
    assert stat.S_IMODE(os.stat(th.THEMES_FILE).st_mode) == 0o644
    # Variant defaults when omitted; refused when unknown.
    assert client.post('/api/themes/default', json={'theme': 'pipboy'}).get_json()['default']['variant'] == 'green'
    assert client.post('/api/themes/default', json={'theme': 'pipboy', 'variant': 'pink'}).status_code == 400
    assert client.post('/api/themes/default', json={'theme': 'nope'}).status_code == 404
    assert client.post('/api/themes/default', json={'theme': '../x'}).status_code == 400
    assert client.post('/api/themes/default', json={'theme': 'phosphor'}).get_json()['default'] == {'theme': 'phosphor', 'variant': None}
    # A broken theme cannot become the default; a vanished one falls back.
    _write_theme(dropins, 'broken', css='html{}')
    assert client.post('/api/themes/default', json={'theme': 'broken'}).status_code == 400
    _write_theme(dropins, 'gone')
    assert client.post('/api/themes/default', json={'theme': 'gone'}).status_code == 200
    import shutil
    shutil.rmtree(dropins / 'gone')
    _reset()
    d = th.load_default()
    assert d['theme'] == 'nexus' and d.get('fallback') and d['configured'] == 'gone'
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('v', 'readonly'))
    assert client.post('/api/themes/default', json={'theme': 'nexus'}).status_code == 403


# ─── Frontend contracts ─────────────────────────────────────────────────

def test_style_css_has_no_colour_outside_token_blocks():
    """A theme can only restyle what the stylesheet routes through tokens.
    Every hex / rgb() literal must sit on a `--token:` line, so any new
    hard-coded colour fails here instead of surviving one theme and not the
    next (the 3.11.0 sweep moved the last seven into tokens)."""
    text = open(os.path.join(ROOT, 'static', 'css', 'style.css')).read()
    bad = [l.strip() for l in text.splitlines()
           if re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(', l) and not re.match(r'\s*--[a-z-]+\s*:', l)]
    assert bad == [], bad


def test_js_has_no_inline_hex_colours():
    """Inline style="color:#…" in rendered HTML, and xterm's fixed black, both
    ignored the theme. Only var(--…) and the token helper are allowed."""
    js_dir = os.path.join(ROOT, 'static', 'js')
    hits = []
    for f in sorted(os.listdir(js_dir)):
        text = open(os.path.join(js_dir, f)).read()
        for m in re.finditer(r'style="[^"]*#[0-9a-fA-F]{3,6}\b[^"]*"', text):
            hits.append((f, m.group(0)))
        if 'new Terminal(' in text:
            assert "theme: { background: '#" not in text, f
    assert hits == [], hits


def test_index_attaches_cached_theme_before_paint():
    html = open(os.path.join(ROOT, 'templates', 'index.html')).read()
    head, body = html.split('<body', 1)   # the desktop shell's body carries a class
    assert 'nexusTheme.v1' in head and "setAttribute('data-theme'" in head
    assert head.index('/static/css/style.css') < head.index('nexusTheme.v1'), 'theme link must follow style.css'
    assert '/static/js/themes.js' in body
    assert body.index('/static/js/core.js') < body.index('/static/js/themes.js')


def test_assets_are_prefixed_with_the_page_base():
    """Through the NexusController's drill-in the node's page lives under
    /nodes/<id>/ and only HTML-literal /static/ refs get rewritten. A theme
    stylesheet attached at runtime must carry the same prefix, derived from
    where the page's own scripts were loaded; the cache key is per base so one
    node's pick never pre-paints another's drill-in. (Bit on node1 2026-10-18:
    "light/dark changes immediately but the theme doesn't".)"""
    js = open(os.path.join(ROOT, 'static', 'js', 'themes.js')).read()
    assert 'script[src*="/static/js/themes.js"]' in js
    assert 'link.href = THEME_BASE + t.css' in js and 's.src = THEME_BASE + t.js' in js
    assert "'nexusTheme.v1' + (THEME_BASE ? ':' + THEME_BASE : '')" in js
    # API calls stay unprefixed: the drill-in's fetch shim owns /api/*.
    assert "fetch('/api/themes')" in js and 'THEME_BASE + \'/api' not in js
    head = open(os.path.join(ROOT, 'templates', 'index.html')).read().split('<body>', 1)[0]
    assert 'link[href$="/static/css/style.css"]' in head and 'l.href = base + t.css' in head
    assert "'nexusTheme.v1' + (base ? ':' + base : '')" in head


def test_loader_only_accepts_same_origin_theme_assets():
    js = open(os.path.join(ROOT, 'static', 'js', 'themes.js')).read()
    m = re.search(r'function _themeAssetOk\(u\) \{.*?return typeof u === .string.\s*&& (/.*?/)\.test\(u\);', js, re.S)
    assert m, 'asset allowlist regex not found'
    pat = re.compile(m.group(1).strip('/'))
    for ok in ('/static/themes/pipboy/theme.css?v=abc123', '/theme-assets/my-theme/theme.js'):
        assert pat.search(ok), ok
    for bad in ('//evil/x.css', 'https://evil/x.css', '/static/js/core.js', '/theme-assets/../x.css',
                '/theme-assets/x/../../y.css', '/static/themes/pipboy/theme.css?v=1&x=2'):
        assert not pat.search(bad), bad
