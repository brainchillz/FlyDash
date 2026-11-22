"""Terminal module (4.1.0): `ssh <user>@127.0.0.1` on a pty behind a websocket.

The safety argument is the command line, so most of this pins it: the target
is never the browser's, the login name cannot become an option, and the
client carries no credential of its own. The pump is exercised for real
against a stand-in binary on a real pty.
"""
import json
import stat
import threading
import time

import pytest

import app
from nexusdash.modules import terminal


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(terminal, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(terminal, 'audit', lambda *a, **k: None)
    monkeypatch.setattr(terminal, '_sessions', 0)
    monkeypatch.setattr(terminal, 'FAIL_GRACE', 0)
    app.app.config['TESTING'] = True
    return app.app.test_client()


class FakeWS:
    """Stands in for the flask-sock connection: scripted input, captured output."""
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False
        self.lock = threading.Lock()

    def send(self, data):
        with self.lock:
            self.sent.append(data)

    def receive(self, timeout=None):
        if self.closed:
            raise RuntimeError('closed')
        if self.incoming:
            item = self.incoming.pop(0)
            if callable(item):
                return item()
            return item
        time.sleep(0.05)
        return None

    def close(self):
        self.closed = True

    def output(self):
        with self.lock:
            return b''.join(d for d in self.sent if isinstance(d, bytes))

    def errors(self):
        out = []
        for d in self.sent:
            if isinstance(d, str):
                out.append(json.loads(d)['error'])
        return out


def _run(ws, query='?user=alice', headers=None):
    with app.app.test_request_context('/ws/terminal' + query, headers=headers or {}):
        app.app.preprocess_request()
        terminal.ws_terminal(ws)


# ─── the command line ───────────────────────────────────────────────────
def test_argv_target_is_loopback_and_fixed():
    argv = terminal.ssh_argv('alice', port=22)
    assert argv[0] == terminal.SSH_BIN
    assert argv[-4:] == ['-p', '22', '-l', 'alice', '127.0.0.1'][-4:]
    assert argv[-1] == '127.0.0.1'
    assert argv[1:3] == ['-F', '/dev/null']          # no user/system ssh_config
    assert argv[argv.index('-e') + 1] == 'none'


def test_argv_carries_no_credential_or_forwarding():
    opts = set(a for a in terminal.ssh_argv('alice', port=22))
    for needed in ('PubkeyAuthentication=no', 'HostbasedAuthentication=no',
                   'GSSAPIAuthentication=no', 'IdentityAgent=none',
                   'PreferredAuthentications=keyboard-interactive,password',
                   'ForwardAgent=no', 'ForwardX11=no', 'ClearAllForwardings=yes',
                   'ProxyCommand=none', 'ControlPath=none', 'PermitLocalCommand=no'):
        assert needed in opts, needed


def test_env_is_fixed_not_inherited(monkeypatch):
    monkeypatch.setenv('SSH_AUTH_SOCK', '/tmp/agent')
    monkeypatch.setenv('SSH_ASKPASS', '/bin/true')
    env = terminal.ssh_env()
    assert 'SSH_AUTH_SOCK' not in env and 'SSH_ASKPASS' not in env
    assert env['TERM'] == 'xterm-256color'


@pytest.mark.parametrize('name', ['alice', 'backupuser', '_svc', 'a.b-c_d', 'Alice'])
def test_login_name_accepted(name):
    assert terminal.RE_TERM_USER.match(name)


@pytest.mark.parametrize('name', [
    '', '-oProxyCommand=id', '-l', 'a b', 'a@b', 'root\n', 'a;b', 'a/b',
    '1abc', 'x' * 33, 'alice@192.168.10.5', '$(id)'])
def test_login_name_refused(name):
    assert not terminal.RE_TERM_USER.match(name)


def test_sshd_port_env_then_config_then_default(monkeypatch, tmp_path):
    cfg = tmp_path / 'sshd_config'
    cfg.write_text('#Port 22\nPort 2222 # moved\nPermitRootLogin no\n')
    monkeypatch.setattr(terminal, 'SSHD_CONFIG', str(cfg))
    monkeypatch.delenv('DASHBOARD_TERMINAL_SSH_PORT', raising=False)
    assert terminal.sshd_port() == 2222
    monkeypatch.setenv('DASHBOARD_TERMINAL_SSH_PORT', '2022')
    assert terminal.sshd_port() == 2022
    monkeypatch.setenv('DASHBOARD_TERMINAL_SSH_PORT', '99999')     # out of range
    assert terminal.sshd_port() == 2222
    monkeypatch.setattr(terminal, 'SSHD_CONFIG', str(tmp_path / 'absent'))
    monkeypatch.delenv('DASHBOARD_TERMINAL_SSH_PORT')
    assert terminal.sshd_port() == 22


@pytest.mark.parametrize('origin,host,ok', [
    (None, 'node1.example.com:8443', True),            # non-browser client
    ('https://node1.example.com:8443', 'node1.example.com:8443', True),
    ('https://NODE1.example.com:8443', 'node1.example.com:8443', True),
    ('https://evil.example.com', 'node1.example.com:8443', False),
    ('https://node1.example.com:9000', 'node1.example.com:8443', False),   # same site, other port
    ('null', 'node1.example.com:8443', False),
])
def test_same_origin(origin, host, ok):
    assert terminal.same_origin(origin, host) is ok


# ─── gating ─────────────────────────────────────────────────────────────
def test_module_is_default_off_and_admin_only_page():
    m = terminal.MODULE
    assert m['default_enabled'] is False
    assert m['nav']['pages'][0]['admin_only'] is True


def test_info_requires_admin(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('v', 'readonly'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    assert app.app.test_client().get('/api/terminal').status_code == 403


def test_info_403_when_module_disabled(admin, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: {'terminal'})
    r = admin.get('/api/terminal')
    assert r.status_code == 403 and 'disabled' in r.get_json()['error']


def test_info_shape(admin, monkeypatch):
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(terminal, 'SSH_BIN', '/bin/sh')
    j = admin.get('/api/terminal').get_json()
    assert j['available'] is True and j['host'] == '127.0.0.1'
    assert j['max_sessions'] == terminal.MAX_SESSIONS
    monkeypatch.setattr(terminal, 'SSH_BIN', '/nonexistent/ssh')
    j = admin.get('/api/terminal').get_json()
    assert j['available'] is False and 'not installed' in j['reason']


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('must not spawn')
    monkeypatch.setattr(terminal, '_spawn', boom)


def test_ws_refuses_non_admin(admin, monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('v', 'readonly'))
    _no_spawn(monkeypatch)
    ws = FakeWS()
    _run(ws)
    assert ws.closed and 'Administrator' in ws.errors()[0]


def test_ws_refuses_when_module_disabled(admin, monkeypatch):
    monkeypatch.setattr(terminal, 'load_disabled_modules', lambda: {'terminal'})
    _no_spawn(monkeypatch)
    ws = FakeWS()
    _run(ws)
    assert ws.closed and 'disabled' in ws.errors()[0]


def test_ws_refuses_cross_origin(admin, monkeypatch):
    _no_spawn(monkeypatch)
    ws = FakeWS()
    _run(ws, headers={'Origin': 'https://evil.example.com'})
    assert ws.closed and 'Cross-origin' in ws.errors()[0]


@pytest.mark.parametrize('q', ['', '?user=-oProxyCommand%3Did', '?user=a%20b', '?user=a%40b'])
def test_ws_refuses_bad_login_name(admin, monkeypatch, q):
    _no_spawn(monkeypatch)
    ws = FakeWS()
    _run(ws, query=q)
    assert ws.closed and 'Invalid login name' in ws.errors()[0]


def test_ws_session_cap(admin, monkeypatch):
    _no_spawn(monkeypatch)
    monkeypatch.setattr(terminal, '_sessions', terminal.MAX_SESSIONS)
    ws = FakeWS()
    _run(ws)
    assert 'Too many' in ws.errors()[0]
    assert terminal._sessions == terminal.MAX_SESSIONS        # not decremented


# ─── the pump, on a real pty ────────────────────────────────────────────
@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """A stand-in for ssh: prints its last three args and the tty size, then
    echoes one line back. Proves argv, the pty, resize and reaping."""
    script = tmp_path / 'fakessh'
    script.write_text('#!/bin/sh\n'
                      'for a in "$@"; do last3="$prev2 $prev $a"; prev2=$prev; prev=$a; done\n'
                      'echo "ARGS:$last3"\n'
                      'read line\n'
                      'echo "SIZE:$(stty size)"\n'
                      'echo "GOT:$line"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr(terminal, 'SSH_BIN', str(script))
    monkeypatch.setattr(terminal, 'ssh_env',
                        lambda: {'PATH': '/usr/bin:/bin', 'TERM': 'xterm'})
    return script


def test_bridge_roundtrip_resize_and_reap(admin, fake_ssh, monkeypatch):
    audits = []
    monkeypatch.setattr(terminal, 'audit', lambda *a, **k: audits.append(a))
    procs = []
    real_spawn = terminal._spawn

    def spy(argv, env):
        proc, fd = real_spawn(argv, env)
        procs.append(proc)
        return proc, fd
    monkeypatch.setattr(terminal, '_spawn', spy)

    ws = FakeWS([json.dumps({'type': 'resize', 'width': 132, 'height': 43}),
                 json.dumps({'type': 'stdin', 'data': 'hello\r'})])
    _run(ws)
    out = ws.output().decode()
    assert 'ARGS:-l alice 127.0.0.1' in out
    assert 'SIZE:43 132' in out
    assert 'GOT:hello' in out
    assert terminal._sessions == 0
    # Reaped (no zombie left behind), and it ran as its own session leader.
    assert procs[0].returncode is not None
    events = [a[4]['event'] for a in audits]
    assert events == ['open', 'close']
    assert audits[0][4]['login'] == 'alice' and audits[0][0] == 'tester'


def test_bridge_closes_when_module_disabled_mid_session(admin, fake_ssh, monkeypatch):
    state = {'off': False}
    monkeypatch.setattr(terminal, 'load_disabled_modules',
                        lambda: {'terminal'} if state['off'] else set())
    clock = {'t': time.time()}
    real_time = time.time

    def flip():
        state['off'] = True
        clock['t'] = real_time() + 60          # jump past the 15 s re-check
        return None
    monkeypatch.setattr(terminal.time, 'time', lambda: max(clock['t'], real_time()))
    ws = FakeWS([flip])
    _run(ws)
    assert any('disabled' in e for e in ws.errors())
    assert terminal._sessions == 0


def test_idle_timeout_closes_session(admin, fake_ssh, monkeypatch):
    monkeypatch.setenv('DASHBOARD_TERMINAL_IDLE', '1')
    ws = FakeWS()
    t0 = time.time()
    _run(ws)
    assert any('idle' in e for e in ws.errors())
    assert time.time() - t0 < 10


def test_refusal_waits_for_the_browser_to_read_it(admin, monkeypatch):
    """The error must not be followed by an immediate teardown (a browser
    reports that as a bare 1006 and drops the message): the handler lingers
    until the peer closes, bounded by FAIL_GRACE."""
    monkeypatch.setattr(terminal, 'FAIL_GRACE', 5)
    _no_spawn(monkeypatch)

    def peer_closes():
        raise RuntimeError('ConnectionClosed')
    ws = FakeWS([None, peer_closes])
    t0 = time.time()
    _run(ws, query='?user=a%20b')
    assert 'Invalid login name' in ws.errors()[0]
    assert ws.incoming == []                 # it did wait for the peer
    assert time.time() - t0 < 2              # and left as soon as it closed
