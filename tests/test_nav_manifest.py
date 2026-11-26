"""/api/modules/nav — the manifest the frontend renders the whole sidebar
from. The expected literal below is hand-derived from the pre-3.0 static nav
in templates/index.html (category order, item order, labels, icons,
admin-only flags, module linkage): the manifest MUST reproduce it exactly or
the rendered sidebar drifts from the original DOM.
"""
import pytest

import app
from nexusdash.core import registry


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


# (cat, label, [(page, label, icon, module, admin_only)]) in sidebar order.
EXPECTED_NAV = [
    ('storage', 'Storage MGMT', [
        ('disks', 'Disks', 'disk', 'disks', False),
        ('zfs', 'ZFS Pools', 'db', 'zfs', False),
        ('lvm', 'LVM', 'lay', 'lvm', False),
        ('mdraid', 'MD RAID', 'box3', 'mdraid', False),
        ('schedules', 'Auto-Snapshots', 'clock', 'schedules', False),
        ('replication', 'Replication', 'repeat', 'replication', False),
        ('maintenance', 'Maintenance', 'wrench', 'maintenance', False)]),
    ('sharing', 'Sharing', [
        ('iscsi', 'iSCSI Targets', 'loc', 'iscsi', False),
        ('nfs', 'NFS Exports', 'share', 'nfs', False),
        ('smb', 'SMB/CIFS', 'folder', 'smb', False),
        ('minidlna', 'DLNA Media', 'camera', 'minidlna', False)]),
    ('ai', 'AI Tools', [
        ('llamacpp', 'LLama.cpp', 'flame', 'llamacpp', False),
        # Added post-3.0: the model library / Hugging Face downloader. A second
        # PAGE of the llamacpp module rather than a module of its own, so it
        # carries module='llamacpp' and is hidden by that toggle.
        ('models', 'Models', 'dl', 'llamacpp', False),
        # 3.5.0: the halogen-flash-server compose stack (default-off module).
        ('halogen', 'Halogen', 'mon', 'halogen', False),
        # 3.9.0: the vLLM Radiance systemd unit (default-off module).
        # 3.12.0: the vLLM fork's page is labelled "vLLM Radiance" (id unchanged: compatibility contract).
        ('radiance', 'vLLM Radiance', 'flame', 'radiance', False),
        # 3.10.0: StillDeadcode's affinity engine, a systemd unit like Radiance (default-off module).
        ('affinity', 'Affinity', 'box3', 'affinity', False),
        # 3.12.0: the author's standalone Radiance engine (stilldeadcode/radiance), labelled plainly "Radiance".
        ('radiance_engine', 'Radiance', 'flame', 'radiance_engine', False),
        ('gpu', 'GPU', 'cpu', 'gpu', False)]),
    ('lxd', 'LXD / Incus', [
        ('instances', 'Instances', 'mon', 'instances', False),
        ('images', 'Images', 'pkg', 'images', False),
        ('ctnetworks', 'Instance Networks', 'net', 'ctnetworks', False),
        ('portforward', 'Port Forward', 'swap', 'portforward', False)]),
    ('docker', 'Docker', [
        ('docker', 'Containers & Images', 'pkg', 'docker', False),
        ('compose', 'Compose Stacks', 'box3', 'compose', False)]),
    ('web', 'Web', [
        ('caddy', 'Caddy Proxy', 'link', 'caddy', True)]),
    ('dns', 'DNS', [
        ('dnshosts', 'DNS Overrides', 'glb', 'dnsmasq', False),
        ('dhcp', 'DHCP', 'swap', 'dnsmasq', False),
        ('dnsconfig', 'DNS Config', 'file', 'dnsmasq', True)]),
    ('power', 'Power', [
        ('nut', 'UPS Server', 'plug', 'nut', True),
        ('upsmon', 'UPS Monitor', 'batt', 'upsmon', True)]),
    # 4.1.0: ssh-to-localhost in a window (default-off module).
    ('terminal', 'Terminal', [
        ('terminal', 'Terminal', 'term', 'terminal', True)]),
    ('system', 'System', [
        ('services', 'Services', 'toggle', None, False),
        ('updates', 'Updates', 'dl', 'updates', False),
        ('tasks', 'Scheduled Tasks', 'cal', None, False),
        ('logs', 'Logs', 'log', None, False),
        ('network', 'Network', 'net', None, True),
        ('firewall', 'Firewall', 'shield', 'firewall', True),
        # 4.2.0: the node's own OpenSSH daemon.
        ('sshd', 'SSH Server', 'key', 'sshd', True),
        ('account', 'My Account', 'user', None, False),
        # 4.0.0 (FlyDash): theme / mode / wallpaper on their own page.
        ('appearance', 'Appearance', 'image', None, False),
        ('users', 'Users & Tokens', 'users', None, True),
        ('notifications', 'Notifications', 'bell', None, True),
        ('certificate', 'Certificate', 'cert', None, True),
        # 3.6.0: node configuration export / restore.
        ('nodeconfig', 'Backup & Restore', 'file', None, True),
        ('audit', 'Audit Log', 'list', None, True),
        # 3.6.0: every enabled module's prerequisites, pass/fail + the fix.
        ('diagnostics', 'Diagnostics', 'wrench', None, True),
        ('modules', 'Modules', 'sli', None, True)]),
]


def test_nav_manifest_matches_pre30_sidebar(client):
    nav = client.get('/api/modules/nav').get_json()['nav']
    got = [(c['cat'], c['label'],
            [(i['page'], i['label'], i['icon'], i['module'], i['admin_only'])
             for i in c['items']])
           for c in nav['categories']]
    assert got == EXPECTED_NAV


def test_nav_manifest_modules_shape(client):
    body = client.get('/api/modules/nav').get_json()
    mods = {m['id']: m for m in body['modules']}
    assert set(mods) == app.MODULE_IDS
    # /api/modules' original keys are all present per entry, plus the extras
    for m in body['modules']:
        assert {'id', 'label', 'category', 'enabled', 'loaded', 'installed',
                'builtin', 'version', 'assets', 'dashboard_card',
                'ui_pages'} <= set(m)
        assert m['builtin'] is True
    # metrics: registered, default-off, no nav entry anywhere
    assert mods['metrics']['enabled'] is True     # load_disabled stubbed empty
    all_pages = {i['page'] for c in body['nav']['categories'] for i in c['items']}
    assert 'metrics' not in all_pages


def test_nav_endpoint_never_module_gated():
    assert registry.module_for_endpoint('registry.modules_nav_get') is None


# ─── Apps manifest (4.0.0, FlyDash) ──────────────────────────────────
# The desktop shell renders APPS from `nav.apps`, additive beside the
# untouched `categories`. (app id, label, icon, [(page, group)]).

EXPECTED_APPS = [
    ('storage', 'Storage', 'disk', [
        ('disks', None), ('zfs', None), ('lvm', None), ('mdraid', None),
        ('schedules', None), ('replication', None), ('maintenance', None)]),
    ('sharing', 'Sharing', 'share', [('nfs', None), ('smb', None), ('minidlna', None)]),
    ('san', 'SAN', 'loc', [('iscsi', None)]),
    ('ai', 'AI Tools', 'flame', [
        ('llamacpp', None), ('models', None), ('halogen', None), ('radiance', None),
        ('affinity', None), ('radiance_engine', None), ('gpu', None)]),
    ('virtualization', 'Virtualization', 'mon', [
        ('instances', None), ('images', None), ('ctnetworks', None), ('portforward', None)]),
    ('containers', 'Containers', 'pkg', [('docker', None), ('compose', None)]),
    ('web', 'Reverse Proxy', 'link', [('caddy', None)]),
    ('dns', 'DNS & DHCP', 'glb', [('dnshosts', None), ('dhcp', None), ('dnsconfig', None)]),
    ('power', 'UPS', 'plug', [('nut', None), ('upsmon', None)]),
    # 4.1.0: ssh-to-localhost in a window (default-off module, its own app).
    ('terminal', 'Terminal', 'term', [('terminal', None)]),
    ('settings', 'Settings', 'sli', [
        ('services', 'Node'), ('updates', 'Node'), ('tasks', 'Node'), ('network', 'Node'),
        ('firewall', 'Node'),
        ('sshd', 'Security'),
        ('users', 'Security'), ('certificate', 'Security'), ('audit', 'Security'),
        ('logs', 'Maintenance'), ('notifications', 'Maintenance'),
        ('nodeconfig', 'Maintenance'), ('diagnostics', 'Maintenance'),
        ('account', 'Desktop'), ('appearance', 'Desktop'), ('modules', 'Desktop')]),
]


def test_apps_manifest_shape_and_grouping(client):
    body = client.get('/api/modules/nav').get_json()
    nav = body['nav']
    assert set(nav) == {'categories', 'apps'}          # additive beside categories
    got = [(a['id'], a['label'], a['icon'], [(s['page'], s['group']) for s in a['sections']])
           for a in nav['apps']]
    assert got == EXPECTED_APPS
    orders = [a['order'] for a in nav['apps']]
    assert orders == sorted(orders)
    for a in nav['apps']:
        for s in a['sections']:
            assert {'page', 'label', 'icon', 'module', 'admin_only', 'group'} <= set(s)
    # every page in the categories appears in exactly one app, and vice versa
    cat_pages = [i['page'] for c in nav['categories'] for i in c['items']]
    app_pages = [s['page'] for a in nav['apps'] for s in a['sections']]
    assert sorted(cat_pages) == sorted(app_pages)
    assert len(app_pages) == len(set(app_pages))


def test_apps_manifest_relabels_modules_and_keeps_flags(client):
    nav = client.get('/api/modules/nav').get_json()['nav']
    settings = next(a for a in nav['apps'] if a['id'] == 'settings')
    by_page = {s['page']: s for s in settings['sections']}
    assert by_page['modules']['label'] == 'Apps'          # apps only …
    cat_sys = next(c for c in nav['categories'] if c['cat'] == 'system')
    assert next(i for i in cat_sys['items'] if i['page'] == 'modules')['label'] == 'Modules'   # … categories untouched
    assert by_page['network']['admin_only'] is True and by_page['network']['module'] is None
    assert by_page['firewall']['module'] == 'firewall'
    san = next(a for a in nav['apps'] if a['id'] == 'san')
    assert [s['module'] for s in san['sections']] == ['iscsi']
    sharing = next(a for a in nav['apps'] if a['id'] == 'sharing')
    assert 'iscsi' not in [s['module'] for s in sharing['sections']]


def test_apps_manifest_plugin_category_becomes_its_own_app(fresh_registry):
    """A module whose nav cat is not in the table (an operator plugin) is an
    app of its own named after its category and ordered after the built-ins;
    `app` places a module into an existing app; `app_group` names its rail
    group; an app that ends up with no sections is omitted."""
    registry.register_module({'id': 'zzplug', 'label': 'Plug', 'category': 'Tools',
                              'blueprint': None, 'order': 1000, 'version': '1',
                              'nav': {'cat': 'tools', 'cat_order': 90,
                                      'pages': [{'id': 'zzplug', 'label': 'Plug', 'icon': 'pkg'}]}},
                             source='plugin')
    registry.register_module({'id': 'zzbays', 'label': 'Bays', 'category': 'Tools',
                              'blueprint': None, 'order': 1001, 'app': 'storage',
                              'app_group': 'Enclosure',
                              'nav': {'cat': 'tools', 'cat_order': 90,
                                      'pages': [{'id': 'zzbays', 'label': 'Bays', 'icon': 'disk'}]}},
                             source='plugin')
    apps = {a['id']: a for a in registry._apps_manifest()}
    assert apps['tools']['label'] == 'Tools' and apps['tools']['icon'] == 'pkg'
    assert apps['tools']['order'] == 1090
    assert [s['page'] for s in apps['tools']['sections']] == ['zzplug']
    bays = next(s for s in apps['storage']['sections'] if s['page'] == 'zzbays')
    assert bays['group'] == 'Enclosure' and bays['module'] == 'zzbays'
    # settings always has the core pages; nothing else declared → no other apps
    assert set(apps) == {'tools', 'storage', 'settings'}
    assert 'san' not in apps and 'sharing' not in apps          # zero sections → omitted
