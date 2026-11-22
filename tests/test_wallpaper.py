"""Wallpapers (4.0.0, FlyDash): the desktop background catalog.

Built-ins under static/wallpapers/, drop-ins/uploads under <app>/wallpapers/,
a public list + asset route (the login screen sits on it), the admin node
default, and uploads confined to PNG/JPEG/WebP by magic bytes.
"""
import io
import os
import json
import pytest

import app
from nexusdash.core import wallpaper as wp
from nexusdash.core import auth

BUILTINS = ('dusk', 'slate', 'ember', 'forest')      # the SVG built-ins
SHIPPED = 'fly'                                        # the JPEG photo every fresh install shows
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 64
WEBP = b'RIFF' + b'\x10\x00\x00\x00' + b'WEBP' + b'VP8 ' + b'\x00' * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


@pytest.fixture
def dropins(monkeypatch, tmp_path):
    d = tmp_path / 'wallpapers'
    d.mkdir()
    monkeypatch.setenv('DASHBOARD_WALLPAPERS_DIR', str(d))
    monkeypatch.setattr(wp, 'WALLPAPER_FILE', str(tmp_path / 'wallpaper.json'))
    return d


@pytest.fixture
def client(monkeypatch, dropins):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('a', 'admin'))
    return app.app.test_client()


@pytest.fixture
def as_role(monkeypatch):
    """Switch the resolved identity mid-test (the fixtures above patch the
    same symbol, so two clients in one test would share the LAST patch)."""
    return lambda role: monkeypatch.setattr(app, '_resolve_identity', lambda: ('u', role))


def _upload(client, data, filename='pic.png', wid=None):
    form = {'file': (io.BytesIO(data), filename)}
    if wid is not None:
        form['id'] = wid
    return client.post('/api/wallpaper/upload', data=form, content_type='multipart/form-data')


# ─── Catalog ────────────────────────────────────────────────────────────

def test_builtin_catalog(dropins):
    cat = {e['id']: e for e in wp.discover()}
    assert set(cat) >= set(BUILTINS)
    for wid in BUILTINS:
        e = cat[wid]
        assert e['source'] == 'builtin'
        assert e['url'].startswith('/static/wallpapers/%s.svg?v=' % wid)
        assert e['label'] == wid.title()
        p = os.path.join(wp.BUILTIN_DIR, wid + '.svg')
        assert os.path.getsize(p) < 4096
        text = open(p).read()
        assert '<script' not in text and '<text' not in text


def test_shipped_default_is_a_builtin_jpeg(dropins):
    assert wp.DEFAULT_WALLPAPER == SHIPPED
    e = wp.get_wallpaper(SHIPPED)
    assert e['source'] == 'builtin'
    assert e['url'].startswith('/static/wallpapers/%s.jpg?v=' % SHIPPED)
    p = os.path.join(wp.BUILTIN_DIR, SHIPPED + '.jpg')
    with open(p, 'rb') as f:
        assert f.read(3) == b'\xff\xd8\xff'
    assert os.path.getsize(p) < 1024 * 1024               # keep the login page light
    # no wallpaper.json at all → the shipped default, without a fallback flag
    assert wp.load_default() == {'wallpaper': SHIPPED}


def test_shipped_default_absent_reports_none(dropins, monkeypatch):
    monkeypatch.setattr(wp, 'DEFAULT_WALLPAPER', 'never-shipped')
    assert wp.load_default() == {'wallpaper': None}


def test_list_is_public_and_shaped(dropins):
    assert 'wallpaper_list' in auth.PUBLIC_ENDPOINTS and 'wallpaper_asset' in auth.PUBLIC_ENDPOINTS
    app.app.config['TESTING'] = True
    c = app.app.test_client()            # no identity patched: unauthenticated
    r = c.get('/api/wallpaper')
    assert r.status_code == 200
    body = r.get_json()
    assert {'wallpapers', 'default', 'shipped_default', 'drop_in_dir'} <= set(body)
    assert body['default'] == {'wallpaper': SHIPPED}      # fresh install: the shipped photo
    assert body['shipped_default'] == SHIPPED
    for e in body['wallpapers']:
        assert set(e) == {'id', 'label', 'url', 'source'}     # nothing private leaks
    assert c.get('/static/wallpapers/dusk.svg').status_code == 200


def test_dropin_listed_and_served_with_traversal_refused(dropins, client):
    (dropins / 'my-photo.png').write_bytes(PNG)
    (dropins / 'notes.txt').write_text('x')             # not a wallpaper
    (dropins / 'dusk.png').write_bytes(PNG)             # shadows a built-in → ignored
    (dropins / 'Bad Name.png').write_bytes(PNG)         # invalid id → ignored
    cat = {e['id']: e for e in client.get('/api/wallpaper').get_json()['wallpapers']}
    assert cat['my-photo']['source'] == 'drop-in'
    assert cat['my-photo']['label'] == 'My Photo'
    assert cat['my-photo']['url'].startswith('/wallpaper-assets/my-photo.png?v=')
    assert cat['dusk']['source'] == 'builtin'
    assert 'notes' not in cat and 'bad-name' not in cat
    assert client.get('/wallpaper-assets/my-photo.png').status_code == 200
    assert client.get('/wallpaper-assets/notes.txt').status_code == 404
    assert client.get('/wallpaper-assets/dusk.png').status_code == 404      # built-ins ride /static
    assert client.get('/wallpaper-assets/..%2Fwallpaper.json').status_code == 404
    assert client.get('/wallpaper-assets/.hidden.png').status_code == 404
    assert client.get('/wallpaper-assets/my-photo.svg').status_code == 404  # wrong extension for the id


# ─── Node default ───────────────────────────────────────────────────────

def test_set_and_clear_default(dropins, client, as_role):
    r = client.post('/api/wallpaper/default', json={'wallpaper': 'ember'})
    assert r.status_code == 200 and r.get_json()['default'] == {'wallpaper': 'ember'}
    assert json.load(open(wp.WALLPAPER_FILE)) == {'wallpaper': 'ember'}
    assert client.get('/api/wallpaper').get_json()['default'] == {'wallpaper': 'ember'}
    assert client.post('/api/wallpaper/default', json={'wallpaper': 'nope'}).status_code == 404
    assert client.post('/api/wallpaper/default', json={'wallpaper': '../x'}).status_code == 400
    as_role('readonly')
    assert client.post('/api/wallpaper/default', json={'wallpaper': 'dusk'}).status_code == 403
    as_role('admin')
    # clearing the node default returns to the shipped photo
    r = client.post('/api/wallpaper/default', json={'wallpaper': None})
    assert r.status_code == 200 and r.get_json()['default'] == {'wallpaper': SHIPPED}


def test_default_pointing_at_vanished_file_falls_back(dropins, client):
    (dropins / 'gone.png').write_bytes(PNG)
    assert client.post('/api/wallpaper/default', json={'wallpaper': 'gone'}).status_code == 200
    (dropins / 'gone.png').unlink()
    d = client.get('/api/wallpaper').get_json()['default']
    assert d['wallpaper'] == SHIPPED and d['configured'] == 'gone' and d['fallback'] is True


# ─── Uploads ────────────────────────────────────────────────────────────

def test_upload_accepts_raster_by_magic(dropins, client):
    for data, fname, want_ext in ((PNG, 'alpha.png', '.png'), (JPG, 'beta.jpeg', '.jpg'), (WEBP, 'gamma.webp', '.webp')):
        r = _upload(client, data, fname)
        assert r.status_code == 200, r.get_json()
        w = r.get_json()['wallpaper']
        assert w['source'] == 'drop-in'
        stored = dropins / (w['id'] + want_ext)
        assert stored.exists() and (stored.stat().st_mode & 0o777) == 0o644
    # the declared extension is not trusted: PNG bytes named .jpg land as .png
    r = _upload(client, PNG, 'tricky.jpg', wid='tricky')
    assert r.status_code == 200 and (dropins / 'tricky.png').exists()
    # explicit id is sanitized
    r = _upload(client, PNG, 'x.png', wid='My Office Wall!')
    assert r.status_code == 200 and r.get_json()['wallpaper']['id'] == 'my-office-wall'


def test_upload_rejects_svg_text_oversize_collision_and_readonly(dropins, client, as_role):
    assert _upload(client, SVG, 'evil.svg').status_code == 400
    assert _upload(client, b'hello world' * 10, 't.png').status_code == 400
    assert _upload(client, PNG, 'dusk.png').status_code == 409          # built-in id
    big = PNG + b'\x00' * (wp.UPLOAD_MAX + 1 - len(PNG))
    assert _upload(client, big, 'big.png').status_code == 413
    assert _upload(client, PNG, '9.png').status_code == 400              # id too short / bad
    as_role('readonly')
    assert _upload(client, PNG, 'alpha.png').status_code == 403
    as_role('admin')
    assert client.post('/api/wallpaper/upload', data={}, content_type='multipart/form-data').status_code == 400
    assert not any(p.name.endswith('.tmp') or '.tmp.' in p.name for p in dropins.iterdir())


def test_upload_replaces_same_id_regardless_of_extension(dropins, client):
    assert _upload(client, PNG, 'wall.png', wid='wall').status_code == 200
    assert _upload(client, JPG, 'wall.jpg', wid='wall').status_code == 200
    names = sorted(p.name for p in dropins.iterdir())
    assert names == ['wall.jpg']


# ─── Delete ─────────────────────────────────────────────────────────────

def test_delete_dropin_only_and_clears_default(dropins, client, as_role):
    assert _upload(client, PNG, 'mine.png', wid='mine').status_code == 200
    assert client.post('/api/wallpaper/default', json={'wallpaper': 'mine'}).status_code == 200
    as_role('readonly')
    assert client.delete('/api/wallpaper/mine').status_code == 403
    as_role('admin')
    assert client.delete('/api/wallpaper/dusk').status_code == 400        # built-in
    assert client.delete('/api/wallpaper/nope').status_code == 404
    assert client.delete('/api/wallpaper/..').status_code in (400, 404)
    r = client.delete('/api/wallpaper/mine')
    assert r.status_code == 200 and r.get_json()['default'] == {'wallpaper': SHIPPED}
    assert not (dropins / 'mine.png').exists()
