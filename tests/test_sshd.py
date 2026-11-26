"""SSH Server module (4.2.0): setting validation, the drop-in round trip,
`sshd -T` parsing against a real capture, the unit plan for plain and
socket-activated sshd, the lockout review, the routes (admin-only, merge
semantics, needs_confirm, read-only degradation, root and helper write
paths) and the root helper's own line-by-line validation, loaded verbatim
from install.sh."""
import os
import re
import json
import types
import pytest

import app
from nexusdash.modules import sshd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, 'tests', 'fixtures', 'sshd')
MAIN = ('# main\nInclude /etc/ssh/sshd_config.d/*.conf\n\nKbdInteractiveAuthentication no\n'
        'UsePAM yes\nX11Forwarding yes\nPrintMotd no\n')


def _capture():
    with open(os.path.join(FIX, 'sshd-T.txt')) as f:
        return f.read()


def _show(load='loaded', active='active', sub='running', state='enabled'):
    return ('Id=x\nLoadState=%s\nActiveState=%s\nSubState=%s\nUnitFileState=%s\n'
            'ActiveEnterTimestamp=Mon 2026-11-09 00:58:29 UTC\nMainPID=42\n' % (load, active, sub, state))


SOCKET_BOX = {'ssh.service': _show(state='disabled'), 'ssh.socket': _show(sub='listening')}
PLAIN_BOX = {'sshd.service': _show()}


# ─── Pure: validation and the drop-in ─────────────────────────────────

@pytest.mark.parametrize('key,value,want', [
    ('Port', '22', '22'), ('Port', '2222  22', '2222 22'), ('Port', '022', '22'), ('Port', '22 22', '22'),
    ('ListenAddress', '10.0.0.5 fe80::1', '10.0.0.5 fe80::1'),
    ('PermitRootLogin', 'prohibit-password', 'prohibit-password'),
    ('PasswordAuthentication', 'no', 'no'), ('MaxAuthTries', '03', '3'),
    ('AllowUsers', 'alice  bob@10.0.0.* backup?', 'alice bob@10.0.0.* backup?'),
    ('AllowTcpForwarding', 'local', 'local'), ('GatewayPorts', 'clientspecified', 'clientspecified'),
])
def test_validate_setting_accepts_and_normalizes(key, value, want):
    assert sshd.validate_setting(key, value) == (want, None)


@pytest.mark.parametrize('key,value', [
    ('Port', '0'), ('Port', '65536'), ('Port', '22;23'), ('Port', '1 2 3 4 5'), ('Port', '22\n23'),
    ('ListenAddress', 'localhost'), ('ListenAddress', '10.0.0.256'), ('ListenAddress', '10.0.0.5:22'),
    ('PermitRootLogin', 'without-password'), ('PasswordAuthentication', 'true'),
    ('MaxAuthTries', '0'), ('MaxAuthTries', '21'), ('LoginGraceTime', '2m'),
    ('AllowUsers', 'alice\nMatch all'), ('AllowUsers', 'a"b'), ('AllowUsers', '-x'), ('AllowUsers', 'a$(id)'),
    ('AllowGroups', 'a b' * 40),
    # Never offered: anything that names a command or a file, and Match.
    ('ForceCommand', '/bin/sh'), ('AuthorizedKeysCommand', '/bin/sh'), ('Match', 'all'),
    ('Include', '/tmp/x'), ('Subsystem', 'sftp /bin/sh'), ('Banner', '/etc/passwd'),
    ('port', '22'),                      # keywords are matched exactly, canonical spelling
])
def test_validate_setting_refuses(key, value):
    v, e = sshd.validate_setting(key, value)
    assert v is None and e


def test_validate_setting_refuses_non_text_and_blank():
    assert sshd.validate_setting('Port', 22)[1]
    assert sshd.validate_setting('Port', '   ')[1]


def test_render_dropin_is_deterministic_and_round_trips():
    ov = {'UseDNS': 'no', 'Port': '2222 22', 'AllowUsers': 'alice bob', 'ListenAddress': '10.0.0.5 fe80::1',
          'PasswordAuthentication': 'no'}
    text = sshd.render_dropin(ov)
    assert text == sshd.render_dropin(dict(reversed(list(ov.items()))))       # order-independent
    body = [l for l in text.splitlines() if not l.startswith('#')]
    assert body == ['Port 2222', 'Port 22', 'ListenAddress 10.0.0.5', 'ListenAddress fe80::1',
                    'AllowUsers alice bob', 'PasswordAuthentication no', 'UseDNS no']
    assert sshd.parse_dropin(text) == ov
    assert text.endswith('\n') and 'Match' not in text and 'Include' not in text


def test_parse_dropin_ignores_unmanaged_and_is_case_insensitive():
    got = sshd.parse_dropin('# c\npasswordauthentication=yes\nBanner /etc/issue\nPORT 22\nport 2200\n')
    assert got == {'PasswordAuthentication': 'yes', 'Port': '22 2200'}


# ─── Pure: sshd -T and the config files ───────────────────────────────

def test_parse_effective_real_capture():
    eff = sshd.parse_effective(_capture())
    assert eff['Port'] == '22' and eff['ListenAddress'] == ''        # wildcard pair = every address
    assert eff['PermitRootLogin'] == 'prohibit-password'
    assert eff['PasswordAuthentication'] == 'yes' and eff['KbdInteractiveAuthentication'] == 'no'
    assert eff['MaxAuthTries'] == '6' and eff['LoginGraceTime'] == '120'
    assert 'AllowUsers' not in eff                                   # sshd -T omits unset lists
    # every keyword it does report is one validate_setting would accept back
    for k, v in eff.items():
        if v:
            assert sshd.validate_setting(k, v) == (v, None), k


def test_parse_effective_lists_and_addresses():
    eff = sshd.parse_effective('port 22\nport 2222\nlistenaddress 10.0.0.5:22\nlistenaddress [fe80::1]:2222\n'
                               'allowusers alice\nallowusers bob\n')
    assert eff == {'Port': '22 2222', 'ListenAddress': '10.0.0.5 fe80::1', 'AllowUsers': 'alice bob'}
    assert sshd.listeners('Port 22\nlistenaddress 0.0.0.0:22\nusedns no\n') == ['listenaddress 0.0.0.0:22', 'port 22']


def test_includes_dropins():
    d = '/etc/ssh/sshd_config.d'
    assert sshd.includes_dropins(MAIN, d)
    assert sshd.includes_dropins('Include sshd_config.d/*.conf\n', d)             # relative to /etc/ssh
    assert not sshd.includes_dropins('#Include /etc/ssh/sshd_config.d/*.conf\nPort 22\n', d)
    assert not sshd.includes_dropins('Match all\nInclude /etc/ssh/sshd_config.d/*.conf\n', d)
    assert not sshd.includes_dropins('Include /etc/ssh/other.d/*.conf\n', d)
    assert not sshd.includes_dropins(None, d)


def test_files_effective_first_value_wins_and_stops_at_match():
    eff = sshd.files_effective('Port 22\nPasswordAuthentication no\nMatch user x\nX11Forwarding no\n',
                               ['PasswordAuthentication yes\n', 'Port 2222\nPort 2200\n'])
    assert eff == {'PasswordAuthentication': 'yes', 'Port': '2222 2200'}


def test_parse_who_and_fingerprint():
    who = ('alice    pts/0        2026-11-26 10:02 (10.0.0.7)\n'
           'alice    tty1         2026-11-26 09:00\n'
           'bob      pts/3        2026-11-26 11:15 (:0)\n'
           'carol    pts/4        2026-11-26 11:20 (tmux(1234).%0)\n')
    assert sshd.parse_who(who) == [{'user': 'alice', 'tty': 'pts/0', 'since': '2026-11-26 10:02', 'from': '10.0.0.7'}]
    fp = sshd.parse_fingerprint('256 SHA256:abcDEF012+/x root@node1 (ED25519)')
    assert fp == {'bits': 256, 'fingerprint': 'SHA256:abcDEF012+/x', 'type': 'ED25519'}
    assert sshd.parse_fingerprint('garbage') is None


# ─── Pure: which units an action touches ──────────────────────────────

def test_unit_plan_socket_activated():
    svc, sock = sshd.parse_show(SOCKET_BOX['ssh.service']), sshd.parse_show(SOCKET_BOX['ssh.socket'])
    plan = lambda a, s=svc: sshd.unit_plan(a, 'ssh', s, sock)
    assert plan('start') == [['start', 'ssh.socket']]
    assert plan('stop') == [['stop', 'ssh.socket', 'ssh.service']]      # the service alone would come back
    assert plan('restart') == [['restart', 'ssh.service']]
    assert plan('reload') == [['reload', 'ssh.service']]
    assert plan('enable') == [['enable', 'ssh.socket']]
    assert plan('disable') == [['disable', 'ssh.socket', 'ssh.service']]
    idle = dict(svc, ActiveState='inactive')                            # nobody has connected yet
    assert plan('reload', idle) == [] and plan('restart', idle) == [['restart', 'ssh.socket']]


def test_unit_plan_plain_service():
    svc = sshd.parse_show(PLAIN_BOX['sshd.service'])
    for a in ('start', 'stop', 'restart', 'reload', 'enable', 'disable'):
        assert sshd.unit_plan(a, 'sshd', svc, {}) == [[a, 'sshd.service']]
    assert sshd.unit_plan('kill', 'sshd', svc, {}) == []


# ─── Pure: lockout review ─────────────────────────────────────────────

BASE = {'Port': '22', 'ListenAddress': '', 'PasswordAuthentication': 'yes', 'PubkeyAuthentication': 'yes',
        'KbdInteractiveAuthentication': 'no', 'PermitRootLogin': 'prohibit-password'}


def _review(**new):
    after = dict(BASE, **new)
    return sshd.lockout_review(BASE, after, sorted(k for k in new if after[k] != BASE.get(k)), 8443)


def test_lockout_review():
    assert _review(UseDNS='yes') == (None, [])
    refusal, w = _review(Port='8443')
    assert refusal and '8443' in refusal and not w
    refusal, w = _review(PasswordAuthentication='no', PubkeyAuthentication='no')
    assert refusal and 'nobody could log in' in refusal
    _r, w = _review(Port='2222')
    assert len(w) == 1 and '2222' in w[0] and 'firewall' in w[0]
    _r, w = _review(PasswordAuthentication='no')
    assert len(w) == 1 and 'Terminal' in w[0]
    _r, w = _review(PubkeyAuthentication='no', AllowUsers='alice', ListenAddress='10.0.0.5', PermitRootLogin='yes')
    assert len(w) == 4


# ─── Routes ───────────────────────────────────────────────────────────

class Box:
    def __init__(self, tmp, units):
        self.calls, self.units, self.tmp = [], dict(units), tmp
        self.sshd_t_rc, self.effective_text = 0, _capture()

    def run(self, args, input_data=None, no_sudo=False, timeout=120):
        self.calls.append(list(args))
        if args[:2] == ['systemctl', 'show']:
            return self.units.get(args[2], ''), '', 0
        if args[0] == 'systemctl':
            return '', '', 0
        if args[1:] == ['-t']:
            return '', ('' if self.sshd_t_rc == 0 else '/etc/ssh/x line 3: Bad configuration option'), self.sshd_t_rc
        if args[1:] == ['-T']:
            # the daemon's answer follows the drop-in the way sshd's would
            text = self.effective_text
            dropin = self.tmp / 'd' / sshd.DROPIN_NAME
            if dropin.exists():
                ov = sshd.parse_dropin(dropin.read_text())
                keep = [l for l in text.splitlines() if sshd.KNOB_BY_LOWER.get(l.split(' ')[0]) not in ov
                        and not (l.startswith('listenaddress') and 'Port' in ov)]
                for k, v in ov.items():
                    keep += ['%s %s' % (k.lower(), x) for x in v.split(' ')] if k in ('Port', 'AllowUsers', 'AllowGroups') else ['%s %s' % (k.lower(), v)]
                text = '\n'.join(keep) + '\n'
            return text, '', 0
        if args[1:] == ['-V']:
            return '', 'OpenSSH_10.2p1 Vendor-1, OpenSSL 3.5.5 27 Jan 2026\n', 1
        if args[0] == 'who':
            return 'alice    pts/0        2026-11-26 10:02 (10.0.0.7)\n', '', 0
        if args[0] == 'ssh-keygen':
            return '256 SHA256:abcDEF012 root@node1 (ED25519)\n', '', 0
        return '', '', 0

    def post(self, path, body):
        return self.client.post(path, json=body)

    def ran(self, verb):
        return [c for c in self.calls if c[0] == 'systemctl' and c[1] == verb]


@pytest.fixture
def box(monkeypatch, tmp_path):
    b = Box(tmp_path, SOCKET_BOX)
    (tmp_path / 'd').mkdir()
    (tmp_path / 'sshd_config').write_text(MAIN.replace('/etc/ssh/sshd_config.d', str(tmp_path / 'd')))
    (tmp_path / 'd' / '50-cloud-init.conf').write_text('PasswordAuthentication yes\n')
    binp = tmp_path / 'sshd'
    binp.write_text('#!/bin/sh\n')
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(sshd, 'run', b.run)
    monkeypatch.setattr(sshd, 'SSHD_BIN', str(binp))
    monkeypatch.setattr(sshd, 'SSHD_CONFIG', str(tmp_path / 'sshd_config'))
    monkeypatch.setattr(sshd, 'SSHD_DROPIN_DIR', str(tmp_path / 'd'))
    monkeypatch.setattr(sshd, 'SSHD_UNIT', '')
    monkeypatch.setattr(sshd, 'HOST_KEY_GLOB', str(tmp_path / 'sshd'))
    monkeypatch.setattr(sshd.os, 'geteuid', lambda: 0)
    b.dropin = tmp_path / 'd' / sshd.DROPIN_NAME
    app.app.config['TESTING'] = True
    b.client = app.app.test_client()
    return b


def test_status_document_socket_activated(box):
    st = box.client.get('/api/sshd').get_json()
    assert st['installed'] and st['service'] == 'ssh' and st['socket_activated'] and st['accepting']
    assert st['boot_enabled'] and not st['service_enabled'] and st['socket_enabled']
    assert st['editable'] and st['readonly_reason'] == '' and st['effective_source'] == 'sshd -T'
    assert st['ports'] == ['22'] and st['version'] == 'OpenSSH_10.2p1 Vendor-1'
    assert st['other_dropins'] == ['50-cloud-init.conf'] and not st['dropin_present']
    assert st['sessions'] == [{'user': 'alice', 'tty': 'pts/0', 'since': '2026-11-26 10:02', 'from': '10.0.0.7'}]
    assert st['host_keys'] == [{'bits': 256, 'fingerprint': 'SHA256:abcDEF012', 'type': 'ED25519', 'file': 'sshd'}]
    rows = {r['key']: r for r in st['settings']}
    assert [r['key'] for r in st['settings']] == [k[0] for k in sshd.KNOBS]
    assert rows['Port']['value'] == '22' and rows['Port']['override'] is None and not rows['Port']['shadowed']
    assert rows['PermitRootLogin']['choices'] and rows['MaxAuthTries']['range'] == [1, 20]


def test_everything_is_admin_only(box, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('ro', 'readonly'))
    assert box.client.get('/api/sshd').status_code == 403
    assert box.post('/api/sshd/action', {'action': 'reload'}).status_code == 403
    assert box.post('/api/sshd/settings', {'settings': {'UseDNS': 'yes'}}).status_code == 403
    assert box.post('/api/sshd/settings/reset', {}).status_code == 403
    assert not [c for c in box.calls if c[0] == 'systemctl' and c[1] != 'show'] and not box.dropin.exists()


def test_module_toggle_gates_the_routes(box, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'sshd'})
    assert box.client.get('/api/sshd').status_code == 403


def test_actions_use_the_plan_and_stop_needs_confirm(box):
    assert box.post('/api/sshd/action', {'action': 'reload'}).get_json()['ran'] == ['systemctl reload ssh.service']
    r = box.post('/api/sshd/action', {'action': 'stop'}).get_json()
    assert r['needs_confirm'] and not box.ran('stop')
    r = box.post('/api/sshd/action', {'action': 'stop', 'confirm': True}).get_json()
    assert r['success'] and box.ran('stop') == [['systemctl', 'stop', 'ssh.socket', 'ssh.service']]
    assert box.post('/api/sshd/action', {'action': 'disable'}).get_json()['needs_confirm']
    assert box.post('/api/sshd/action', {'action': 'enable'}).get_json()['ran'] == ['systemctl enable ssh.socket']
    assert box.post('/api/sshd/action', {'action': 'kill'}).status_code == 400
    assert box.post('/api/sshd/action', {'action': 'stop; reboot', 'confirm': True}).status_code == 400


def test_action_plain_service_and_absent(box):
    box.units = dict(PLAIN_BOX)
    assert box.post('/api/sshd/action', {'action': 'restart'}).get_json()['ran'] == ['systemctl restart sshd.service']
    box.units = {}
    assert box.post('/api/sshd/action', {'action': 'restart'}).status_code == 404
    assert box.client.get('/api/sshd').get_json()['installed'] is False


def test_reload_with_no_daemon_running_is_a_note(box):
    box.units = {'ssh.service': _show(active='inactive', sub='dead', state='disabled'), 'ssh.socket': SOCKET_BOX['ssh.socket']}
    r = box.post('/api/sshd/action', {'action': 'reload'}).get_json()
    assert r['success'] and 'nothing to reload' in r['note'] and not box.ran('reload')


def test_settings_write_test_reload(box):
    r = box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4', 'UseDNS': 'no', 'X11Forwarding': ''}})
    out = r.get_json()
    assert r.status_code == 200 and out['success'] and out['changed'] == ['MaxAuthTries']
    # UseDNS no is already what sshd uses -> not pinned; blank X11Forwarding = nothing to clear
    assert out['overrides'] == {'MaxAuthTries': '4'} and out['shadowed'] == []
    assert sshd.parse_dropin(box.dropin.read_text()) == {'MaxAuthTries': '4'}
    assert oct(box.dropin.stat().st_mode & 0o777) == '0o644'
    assert [box.tmp.joinpath('sshd').as_posix(), '-t'] in box.calls
    assert out['reloaded'] and box.ran('reload') == [['systemctl', 'reload', 'ssh.service']]
    assert box.ran('daemon-reload') and not box.ran('restart')          # ports unchanged: socket left alone
    st = box.client.get('/api/sshd').get_json()
    assert {r['key']: r for r in st['settings']}['MaxAuthTries']['override'] == '4' and st['dropin_present']


def test_settings_merge_absent_keeps_blank_clears_and_noop_is_byte_identical(box):
    box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4', 'MaxSessions': '5'}})
    before = box.dropin.read_bytes()
    n = len(box.calls)
    r = box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4'}}).get_json()      # absent key left alone
    assert r['noop'] and box.dropin.read_bytes() == before
    assert not [c for c in box.calls[n:] if c[0] == 'systemctl' and c[1] != 'show']
    r = box.post('/api/sshd/settings', {'settings': {'MaxSessions': ''}}).get_json()        # blank clears
    assert r['cleared'] == ['MaxSessions'] and sshd.parse_dropin(box.dropin.read_text()) == {'MaxAuthTries': '4'}
    r = box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': None}}).get_json()     # last one out removes the file
    assert r['success'] and not r['dropin_present'] and not box.dropin.exists()


def test_port_change_needs_confirm_then_rebinds_the_socket(box):
    r = box.post('/api/sshd/settings', {'settings': {'Port': '2222'}}).get_json()
    assert r['needs_confirm'] and '2222' in r['warning'] and not box.dropin.exists()
    r = box.post('/api/sshd/settings', {'settings': {'Port': '2222'}, 'confirm': True}).get_json()
    assert r['success'] and r['listeners_restarted'] and r['changed'] == ['Port']
    assert box.ran('restart') == [['systemctl', 'restart', 'ssh.socket']]
    assert box.calls.index(['systemctl', 'daemon-reload']) < box.calls.index(['systemctl', 'restart', 'ssh.socket'])


def test_hard_refusals_write_nothing(box):
    for body in ({'Port': str(sshd.DASHBOARD_PORT)},
                 {'PasswordAuthentication': 'no', 'PubkeyAuthentication': 'no'},
                 {'ForceCommand': '/bin/sh'}, {'Port': '22; reboot'}, {'AllowUsers': 'x\nMatch all'}):
        r = box.post('/api/sshd/settings', {'settings': body, 'confirm': True})
        assert r.status_code == 400, body
    assert box.post('/api/sshd/settings', {'settings': ['Port']}).status_code == 400
    assert not box.dropin.exists() and not box.ran('reload')


def test_sshd_refusal_restores_the_previous_file(box):
    box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4'}})
    before = box.dropin.read_bytes()
    n = len(box.calls)
    box.sshd_t_rc = 255
    r = box.post('/api/sshd/settings', {'settings': {'MaxSessions': '5'}})
    assert r.status_code == 500 and 'nothing was changed' in r.get_json()['error']
    assert box.dropin.read_bytes() == before
    assert not [c for c in box.calls[n:] if c[0] == 'systemctl' and c[1] in ('reload', 'restart', 'daemon-reload')]
    # ... and with no previous file, the refused one is removed again
    box.dropin.unlink()
    assert box.post('/api/sshd/settings', {'settings': {'MaxSessions': '5'}}).status_code == 500
    assert not box.dropin.exists()


def test_shadowed_override_is_reported(box):
    # an earlier-sorting drop-in wins: sshd keeps answering 6 whatever we write
    real = box.run
    box.run = lambda a, **k: (_capture(), '', 0) if a[1:] == ['-T'] else real(a, **k)
    sshd.run = box.run
    r = box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4'}}).get_json()
    assert r['success'] and r['shadowed'] == ['MaxAuthTries'] and 'sorts before' in r['warning']
    rows = {x['key']: x for x in box.client.get('/api/sshd').get_json()['settings']}
    assert rows['MaxAuthTries']['shadowed'] and rows['MaxAuthTries']['value'] == '6'


def test_reset_needs_confirm_and_removes_only_our_file(box):
    assert box.post('/api/sshd/settings/reset', {}).get_json()['noop']
    box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4'}})
    r = box.post('/api/sshd/settings/reset', {}).get_json()
    assert r['needs_confirm'] and 'MaxAuthTries' in r['warning'] and box.dropin.exists()
    r = box.post('/api/sshd/settings/reset', {'confirm': True}).get_json()
    assert r['success'] and not box.dropin.exists()
    assert (box.tmp / 'd' / '50-cloud-init.conf').exists() and (box.tmp / 'sshd_config').read_text().startswith('# main')


def test_read_only_when_main_config_does_not_include_the_dropins(box):
    (box.tmp / 'sshd_config').write_text('Port 22\n')
    st = box.client.get('/api/sshd').get_json()
    assert st['editable'] is False and 'does not Include' in st['readonly_reason']
    assert box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4'}}).status_code == 400
    assert not box.dropin.exists()


def test_not_root_without_helper_is_read_only_from_files(box, monkeypatch):
    monkeypatch.setattr(sshd.os, 'geteuid', lambda: 997)
    monkeypatch.setattr(sshd, 'SSHD_HELPER', str(box.tmp / 'no-such-helper'))
    st = box.client.get('/api/sshd').get_json()
    assert st['editable'] is False and 'helper is not installed' in st['readonly_reason']
    assert st['effective_source'] == 'files'
    rows = {r['key']: r for r in st['settings']}
    assert rows['PasswordAuthentication']['value'] == 'yes' and rows['X11Forwarding']['value'] == 'yes'
    assert rows['Port']['value'] is None                              # a compiled-in default: not visible from files
    assert box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4'}}).status_code == 400
    assert not [c for c in box.calls if c[1:] in (['-T'], ['-t'])]


def test_not_root_writes_through_the_helper(box, monkeypatch):
    helper = box.tmp / 'nexus-dashboard-sshd'
    helper.write_text('#!/bin/sh\n')
    helper.chmod(0o755)
    monkeypatch.setattr(sshd.os, 'geteuid', lambda: 997)
    monkeypatch.setattr(sshd, 'SSHD_HELPER', str(helper))
    seen = {}
    real = box.run

    def run(args, input_data=None, **k):
        if args[0] == str(helper):
            box.calls.append(list(args))
            seen[args[1]] = input_data
            if args[1] == 'effective':
                return _capture(), '', 0
            return json.dumps({'listeners_restarted': False, 'reloaded': True}) + '\n', '', 0
        return real(args, input_data=input_data, **k)
    monkeypatch.setattr(sshd, 'run', run)
    assert box.client.get('/api/sshd').get_json()['effective_source'] == 'sshd -T'
    r = box.post('/api/sshd/settings', {'settings': {'MaxAuthTries': '4'}}).get_json()
    assert r['success'] and r['reloaded']
    assert [str(helper), 'apply', 'ssh'] in box.calls and seen['apply'] == sshd.render_dropin({'MaxAuthTries': '4'})
    assert not box.dropin.exists()                                    # the app itself wrote nothing


# ─── The root helper, verbatim from install.sh ────────────────────────

def _helper_source(installer):
    src = open(os.path.join(ROOT, installer)).read()
    m = re.search(r'cat > "\$SSHD_HELPER" << \'HELPER\'\n(.*?)\nHELPER\n', src, re.S)
    assert m, installer
    return m.group(1)


@pytest.fixture(scope='module')
def helper():
    mod = types.ModuleType('sshd_helper')
    exec(compile(_helper_source('install.sh'), 'sshd_helper', 'exec'), mod.__dict__)
    return mod


def test_helper_is_identical_in_both_installers_and_tracks_the_app(helper):
    assert _helper_source('install.sh') == _helper_source('install-rhel.sh')
    assert set(helper.RULES) == {k[0] for k in sshd.KNOBS}
    assert helper.DROPIN == sshd.DROPIN_NAME and helper.MAX_BYTES == sshd.MAX_DROPIN_BYTES
    for installer in ('install.sh', 'install-rhel.sh'):
        assert 'NOPASSWD: /usr/local/sbin/nexus-dashboard-sshd\n' in open(os.path.join(ROOT, installer)).read()


def test_helper_accepts_everything_the_app_renders(helper):
    ov = {}
    for k, v in (('Port', '2222 22'), ('ListenAddress', '10.0.0.5 fe80::1'), ('PermitRootLogin', 'forced-commands-only'),
                 ('AllowUsers', 'alice bob@10.0.0.* backup?'), ('AllowGroups', 'wheel'),
                 ('PasswordAuthentication', 'no'), ('PubkeyAuthentication', 'yes'),
                 ('KbdInteractiveAuthentication', 'no'), ('PermitEmptyPasswords', 'no'), ('MaxAuthTries', '20'),
                 ('LoginGraceTime', '600'), ('MaxSessions', '100'), ('ClientAliveInterval', '3600'),
                 ('ClientAliveCountMax', '20'), ('X11Forwarding', 'no'), ('AllowTcpForwarding', 'remote'),
                 ('AllowAgentForwarding', 'no'), ('GatewayPorts', 'clientspecified'), ('UseDNS', 'yes'),
                 ('PrintMotd', 'yes')):
        ov[k], e = sshd.validate_setting(k, v)
        assert e is None, k
    assert set(ov) == {k[0] for k in sshd.KNOBS}
    text = sshd.render_dropin(ov)
    assert helper.validate(text) == text


@pytest.mark.parametrize('text', [
    'Match all\n', 'Include /tmp/x.conf\n', 'ForceCommand /bin/sh\n', 'AuthorizedKeysCommand /bin/sh\n',
    'Subsystem sftp /bin/sh\n', 'Port 22 23\n', 'Port 0\n', 'Port 70000\n', 'port 22\n', 'Port=22\n',
    'AllowUsers alice\tbob\n', 'AllowUsers a"b\n', 'AllowUsers -x\n', 'PasswordAuthentication yes # x\n',
    'PasswordAuthentication yes\nPasswordAuthentication no\n', 'Port 1\nPort 2\nPort 3\nPort 4\nPort 5\n',
    'UseDNS no\x00\n', 'UseDNS no\r\nMatch all\n',
    'PasswordAuthentication no\nPubkeyAuthentication no\nKbdInteractiveAuthentication no\n',
    '# ok\n' + 'UseDNS no\n' * 2, 'x' * 9000,
])
def test_helper_refuses(helper, text):
    with pytest.raises(SystemExit):
        helper.validate(text)


def test_helper_usage_is_narrow(helper, monkeypatch):
    monkeypatch.setattr(helper.os, 'geteuid', lambda: 0)
    for argv in ([], ['apply'], ['apply', 'cron'], ['apply', 'ssh', 'x'], ['reset', '../ssh'], ['effective', 'x'],
                 ['write', 'ssh']):
        monkeypatch.setattr(helper.sys, 'argv', ['h'] + argv)
        with pytest.raises(SystemExit):
            helper.main()
    monkeypatch.setattr(helper.os, 'geteuid', lambda: 997)
    monkeypatch.setattr(helper.sys, 'argv', ['h', 'effective'])
    with pytest.raises(SystemExit):
        helper.main()
