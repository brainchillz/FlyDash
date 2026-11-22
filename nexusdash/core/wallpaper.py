"""Wallpapers (4.0.0, FlyDash): the desktop background as a catalog.

Two sources, mirroring themes:

    static/wallpapers/<id>.svg|png|…   built-in, shipped with the app, served by /static
    <app_dir>/wallpapers/<id>.<ext>    operator drop-ins and admin uploads
                                       (DASHBOARD_WALLPAPERS_DIR), served by
                                       /wallpaper-assets/<file>

The browser picks a wallpaper per user (localStorage, like the theme); an
admin sets the node's default in ``wallpaper.json`` next to app.py for
browsers that never chose. With no node default configured (a fresh install,
or an admin who cleared it) the SHIPPED default ``DEFAULT_WALLPAPER`` applies
— the wallpaper is independent of the theme, so one default covers every
theme. ``GET /api/wallpaper`` and the asset route are
PUBLIC: the login screen sits on the wallpaper too, and nothing in them is
secret — image files and a list of names.

Trust model: an UPLOAD is an admin action through the UI, so it is confined
to raster formats verified by their magic bytes (PNG, JPEG, WebP) and a size
cap — never SVG, which can carry script. A DROP-IN placed by root over SSH may
additionally be an SVG (four of the five built-ins are; the default photo
``fly.jpg`` is the fifth). Filenames are allowlisted and
send_from_directory confines traversal.
"""
import os
import re
import json
import hashlib
import logging

from flask import Blueprint, jsonify, request, send_from_directory

from .config import APP_DIR, STATIC_DIR, write_json_atomic
from .runcmd import err

bp = Blueprint('wallpaper', __name__)
_log = logging.getLogger(__name__)

RE_WALLPAPER_ID = re.compile(r'^[a-z][a-z0-9-]{1,31}\Z')
RE_ASSET_FILE = re.compile(r'^[A-Za-z0-9._-]+\Z')
BUILTIN_DIR = os.path.join(STATIC_DIR, 'wallpapers')
# The built-in every fresh install shows until an admin picks another.
DEFAULT_WALLPAPER = 'fly'
WALLPAPER_FILE = os.environ.get('DASHBOARD_WALLPAPER_FILE',
                                os.path.join(APP_DIR, 'wallpaper.json'))
# What a drop-in directory may serve (root placed it); uploads are narrower.
DROPIN_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.svg')
UPLOAD_MAX = 8 * 1024 * 1024
# Magic-byte sniffing for uploads: the declared extension is never trusted.
_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', '.png'),
    (b'\xff\xd8\xff', '.jpg'),
)


def wallpapers_dir():
    return os.environ.get('DASHBOARD_WALLPAPERS_DIR', os.path.join(APP_DIR, 'wallpapers'))


def _sniff(head):
    """Image format from the first bytes, or None when it is not PNG/JPEG/WebP."""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return '.webp'
    return None


def _file_version(path):
    h = hashlib.sha1()
    try:
        st = os.stat(path)
        h.update(('%s:%d:%d' % (os.path.basename(path), int(st.st_mtime), st.st_size)).encode())
    except OSError:
        h.update(b'missing')
    return h.hexdigest()[:10]


def _label(wid):
    return wid.replace('-', ' ').title()


def _scan(base, source):
    """One catalog entry per image file whose stem is a valid id. Files that
    fail the allowlists are skipped silently (a stray README in the drop-in
    directory is not an error, it is just not a wallpaper)."""
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if name.startswith(('.', '_')) or not RE_ASSET_FILE.match(name):
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in DROPIN_EXT or not RE_WALLPAPER_ID.match(stem):
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        url_base = '/static/wallpapers/' if source == 'builtin' else '/wallpaper-assets/'
        out.append({'id': stem, 'label': _label(stem), 'source': source,
                    'url': '%s%s?v=%s' % (url_base, name, _file_version(path)),
                    '_file': name, '_dir': base})
    return out


def discover():
    """Built-ins first, then drop-ins; a drop-in reusing a built-in id is
    dropped (the built-in wins) rather than shadowing it. Cheap enough to
    rescan on every call — two small directory listings."""
    out = []
    taken = set()
    for e in _scan(BUILTIN_DIR, 'builtin'):
        if e['id'] in taken:
            continue
        taken.add(e['id'])
        out.append(e)
    for e in _scan(wallpapers_dir(), 'drop-in'):
        if e['id'] in taken:
            _log.warning('wallpaper %r in %s shadows a built-in; ignored', e['id'], e['_dir'])
            continue
        taken.add(e['id'])
        out.append(e)
    return out


def get_wallpaper(wid):
    for e in discover():
        if e['id'] == wid:
            return e
    return None


def _public(e):
    return {k: v for k, v in e.items() if not k.startswith('_')}


# ─── Node default ──────────────────────────────────────────────────────

def load_default():
    """The node default: the admin's configured id when it exists, else the
    shipped ``DEFAULT_WALLPAPER`` (when that built-in is present). A configured
    id whose file vanished is reported as ``configured`` + ``fallback`` while
    the shipped default takes its place, so the shell never paints a 404."""
    try:
        with open(WALLPAPER_FILE) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError('not an object')
    except (OSError, ValueError):
        d = {}
    wid = d.get('wallpaper') if isinstance(d.get('wallpaper'), str) else None
    shipped = DEFAULT_WALLPAPER if get_wallpaper(DEFAULT_WALLPAPER) is not None else None
    if wid is None:
        return {'wallpaper': shipped}
    if get_wallpaper(wid) is None:
        return {'wallpaper': shipped, 'configured': wid, 'fallback': True}
    return {'wallpaper': wid}


def _save_default(wid):
    write_json_atomic(WALLPAPER_FILE, {'wallpaper': wid}, mode=0o644)


# ─── Routes ────────────────────────────────────────────────────────────

@bp.route('/api/wallpaper')
def wallpaper_list():
    """Public: the login screen sits on the wallpaper before anyone signs in."""
    return jsonify({'wallpapers': [_public(e) for e in discover()],
                    'default': load_default(),
                    'shipped_default': DEFAULT_WALLPAPER,
                    'drop_in_dir': wallpapers_dir()})


@bp.route('/api/wallpaper/default', methods=['POST'])
def wallpaper_set_default():
    from .auth import _is_admin
    if not _is_admin():
        return err('Admin required', 403)
    body = request.get_json(silent=True) or {}
    wid = body.get('wallpaper')
    if wid is None:
        _save_default(None)
        return jsonify({'success': True, 'default': load_default()})
    if not isinstance(wid, str) or not RE_WALLPAPER_ID.match(wid):
        return err('Invalid wallpaper id', 400)
    if get_wallpaper(wid) is None:
        return err('Unknown wallpaper %r' % wid, 404)
    _save_default(wid)
    return jsonify({'success': True, 'default': load_default()})


@bp.route('/api/wallpaper/upload', methods=['POST'])
def wallpaper_upload():
    """Admin upload of a raster wallpaper. The bytes decide the format, the
    id comes from the `id` field or the sanitized filename stem, and a
    collision with a built-in is refused (the built-in would win anyway and
    the upload would silently never show)."""
    from .auth import _is_admin
    if not _is_admin():
        return err('Admin required', 403)
    f = request.files.get('file')
    if f is None:
        return err('No file uploaded (multipart field "file")', 400)
    data = f.read(UPLOAD_MAX + 1)
    if len(data) > UPLOAD_MAX:
        return err('File too large (max %d MiB)' % (UPLOAD_MAX // (1024 * 1024)), 413)
    ext = _sniff(data[:16])
    if ext is None:
        return err('Unsupported image: uploads must be PNG, JPEG or WebP', 400)
    raw = (request.form.get('id') or '').strip()
    if not raw:
        raw = os.path.splitext(os.path.basename(f.filename or ''))[0]
    wid = re.sub(r'[^a-z0-9-]+', '-', raw.lower()).strip('-')[:32]
    if not RE_WALLPAPER_ID.match(wid):
        return err('Invalid wallpaper id (letters, digits and dashes; 2-32 chars)', 400)
    existing = get_wallpaper(wid)
    if existing is not None and existing['source'] == 'builtin':
        return err('Id %r is taken by a built-in wallpaper' % wid, 409)
    d = wallpapers_dir()
    os.makedirs(d, mode=0o755, exist_ok=True)
    # Replace an existing drop-in of the same id, whatever its extension, so
    # the catalog never lists two files for one id.
    if existing is not None:
        try:
            os.unlink(os.path.join(existing['_dir'], existing['_file']))
        except OSError:
            pass
    target = os.path.join(d, wid + ext)
    tmp = '%s.tmp.%d' % (target, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return err('Could not store wallpaper: %s' % e, 500)
    return jsonify({'success': True, 'wallpaper': _public(get_wallpaper(wid))})


@bp.route('/api/wallpaper/<wid>', methods=['DELETE'])
def wallpaper_delete(wid):
    from .auth import _is_admin
    if not _is_admin():
        return err('Admin required', 403)
    if not RE_WALLPAPER_ID.match(wid or ''):
        return err('Invalid wallpaper id', 400)
    e = get_wallpaper(wid)
    if e is None:
        return err('Unknown wallpaper %r' % wid, 404)
    if e['source'] != 'drop-in':
        return err('Built-in wallpapers cannot be deleted', 400)
    try:
        os.unlink(os.path.join(e['_dir'], e['_file']))
    except OSError as ex:
        return err('Could not delete: %s' % ex, 500)
    if load_default().get('wallpaper') == wid or load_default().get('configured') == wid:
        _save_default(None)
    return jsonify({'success': True, 'default': load_default()})


@bp.route('/wallpaper-assets/<filename>')
def wallpaper_asset(filename):
    """Serve a drop-in wallpaper (built-ins ride /static). Public, like
    /static; the filename allowlist and extension allowlist keep anything
    that is not an image out of reach, and send_from_directory confines
    traversal."""
    if (not filename or filename.startswith('.') or not RE_ASSET_FILE.match(filename)
            or os.path.splitext(filename)[1].lower() not in DROPIN_EXT):
        return err('Not found', 404)
    stem = os.path.splitext(filename)[0]
    e = get_wallpaper(stem)
    if e is None or e['source'] != 'drop-in' or e['_file'] != filename:
        return err('Not found', 404)
    return send_from_directory(e['_dir'], filename)
