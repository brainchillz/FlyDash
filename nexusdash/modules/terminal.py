"""Terminal — an xterm.js window onto `ssh <user>@127.0.0.1`.

The dashboard grants NO shell. It runs the stock ssh client on a pty, pointed
at this node's own sshd on loopback, and relays bytes; sshd (PAM, the
account's own password) decides who gets in. A dashboard admin session
therefore reaches a login prompt, never a prompt — the same thing port 22
offers, behind one more login. Nothing here needs root, sudoers or a helper:
the client runs as whatever the dashboard runs as.

What keeps that true:

- The target is fixed. Host is 127.0.0.1 and the port is this node's sshd
  port (config/env), never anything from the browser — otherwise this is an
  ssh jump box into the LAN.
- The login name is the only browser input that reaches argv; it is an
  anchored allowlist that cannot start with '-'.
- The client is stripped of every credential and convenience of its own:
  `-F /dev/null` (no user or system ssh_config), public-key / host-based /
  GSSAPI auth off, no agent, no forwarding, no escape character, no control
  socket, a scrubbed environment. On a node whose dashboard runs as root this
  is what stops root's own keys from logging in without a prompt.
- Admin only, default-off module, toggle re-checked per connection and while
  a session runs, and the websocket handshake must be same-origin (a page on
  another origin must not be able to open it with the admin's cookie).
- Open and close are audited with the login name. Keystrokes and output are
  never recorded.

Browser protocol is the other consoles': JSON {"type":"stdin"|"resize"} up,
raw bytes down.
"""
import errno
import fcntl
import glob
import json
import os
import pty
import re
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
from urllib.parse import urlsplit

from flask import Blueprint, g, jsonify, request

from ..core.audit import audit
from ..core.registry import load_disabled_modules
from ..core.runcmd import err

bp = Blueprint('terminal', __name__)

SSH_BIN = os.environ.get('DASHBOARD_TERMINAL_SSH', '/usr/bin/ssh')
SETSID_BIN = '/usr/bin/setsid'   # util-linux; gives the child its controlling tty
SSH_HOST = '127.0.0.1'          # fixed by design — see the module docstring
SSHD_CONFIG = '/etc/ssh/sshd_config'
MAX_SESSIONS = 4
IDLE_DEFAULT = 1800             # seconds with no traffic either way
FAIL_GRACE = 2.0                # seconds a refused client gets to read why

# Login names as useradd accepts them (plus the dots and capitals some sites
# use). The first character rules out an option-looking argument.
RE_TERM_USER = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]{0,31}\Z')
_RE_PORT_LINE = re.compile(r'^\s*Port\s+(\d{1,5})\s*(?:#.*)?$', re.I | re.M)

_sessions = 0
_sessions_lock = threading.Lock()


def _env_int(name, default, lo, hi):
    try:
        v = int(os.environ.get(name, ''))
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def idle_timeout():
    """Seconds of silence (both directions) before a session is closed;
    0 disables. DASHBOARD_TERMINAL_IDLE."""
    return _env_int('DASHBOARD_TERMINAL_IDLE', IDLE_DEFAULT, 0, 86400)


def sshd_port():
    """This node's sshd port: DASHBOARD_TERMINAL_SSH_PORT, else the first
    `Port` in sshd_config (+ sshd_config.d), else 22. Best effort — a node
    whose port lives only in a systemd socket unit sets the env var."""
    port = _env_int('DASHBOARD_TERMINAL_SSH_PORT', 0, 1, 65535)
    if port:
        return port
    for path in [SSHD_CONFIG] + sorted(glob.glob(SSHD_CONFIG + '.d/*.conf')):
        try:
            with open(path) as f:
                m = _RE_PORT_LINE.search(f.read())
        except OSError:
            continue
        if m and 1 <= int(m.group(1)) <= 65535:
            return int(m.group(1))
    return 22


def ssh_argv(user, port=None):
    """The whole command line. `user` must already have passed RE_TERM_USER."""
    opts = [
        'BatchMode=no',
        # No credential of the dashboard's own may ever answer for the user.
        'PubkeyAuthentication=no', 'HostbasedAuthentication=no',
        'GSSAPIAuthentication=no', 'IdentityAgent=none', 'IdentitiesOnly=yes',
        'PreferredAuthentications=keyboard-interactive,password',
        'NumberOfPasswordPrompts=3',
        # Loopback to our own sshd: nobody but root can answer on this
        # address, so host-key pinning buys nothing and a stored key would
        # only turn the next host-key regeneration into a refusal.
        'StrictHostKeyChecking=no', 'UserKnownHostsFile=/dev/null',
        'GlobalKnownHostsFile=/dev/null', 'UpdateHostKeys=no',
        'ForwardAgent=no', 'ForwardX11=no', 'ClearAllForwardings=yes',
        'Tunnel=no', 'PermitLocalCommand=no', 'ProxyCommand=none',
        'ControlMaster=no', 'ControlPath=none',
        'ConnectTimeout=10', 'ServerAliveInterval=30', 'ServerAliveCountMax=3',
        'LogLevel=ERROR',
    ]
    argv = [SSH_BIN, '-F', '/dev/null', '-tt', '-e', 'none']
    for o in opts:
        argv += ['-o', o]
    argv += ['-p', str(port or sshd_port()), '-l', user, SSH_HOST]
    return argv


def ssh_env():
    """A fixed environment: nothing inherited (SSH_AUTH_SOCK, SSH_ASKPASS,
    the service's own variables)."""
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'TERM': 'xterm-256color',
            'LANG': os.environ.get('LANG') or 'C.UTF-8', 'HOME': '/nonexistent'}


def same_origin(origin, host):
    """True when a websocket handshake's Origin is this dashboard itself.
    No Origin at all is a non-browser client, which cannot be made to carry
    someone else's cookie — allowed; it still has to authenticate."""
    if not origin:
        return True
    try:
        return urlsplit(origin).netloc.lower() == (host or '').lower() != ''
    except ValueError:
        return False


def _set_winsize(fd, rows, cols):
    rows = max(1, min(int(rows), 500))
    cols = max(1, min(int(cols), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))


def _spawn(argv, env):
    """Run argv on a fresh pty as its own session, the pty its controlling
    terminal (ssh reads the password from /dev/tty). Returns (Popen,
    master_fd). `setsid -c` does the session + TIOCSCTTY step so the fork is
    subprocess's C one — a Python-level pty.fork() in this threaded server
    can deadlock in the child."""
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([SETSID_BIN, '-c'] + argv, stdin=slave,
                                stdout=slave, stderr=slave, env=env,
                                close_fds=True, cwd='/')
    except Exception:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


def _reap(proc, fd):
    """Hang up the session and collect the child; never leaves a zombie."""
    try:
        os.close(fd)                 # the pty going away is itself a SIGHUP
    except OSError:
        pass
    for sig in (signal.SIGHUP, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)     # setsid made it a session leader
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            pass


def _term_fail(ws, message):
    """Tell the browser why, then let IT close. Returning straight after the
    send tears the socket down under the message: the browser then reports a
    bare abnormal closure (1006) and the operator never sees the reason. The
    page closes as soon as it has the error; a client that does not is
    dropped after FAIL_GRACE."""
    try:
        ws.send(json.dumps({'type': 'error', 'error': message}))
        deadline = time.time() + FAIL_GRACE
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            ws.receive(timeout=left)     # raises once the peer has closed
    except Exception:
        pass
    _hang_up(ws)


def _hang_up(ws):
    """Close frame, then FIN. Under werkzeug, flask-sock answers a finished
    handler with an ordinary HTTP response on the same socket; a browser reads
    that after our close frame as "Invalid frame header" and reports 1006
    instead of a clean close. Shutting the socket first makes that write a
    dropped connection, which werkzeug ignores."""
    try:
        ws.close()
    except Exception:
        pass
    try:
        ws.sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def _unavailable():
    """Reason the terminal cannot work on this node, or None."""
    if not (os.path.isfile(SSH_BIN) and os.access(SSH_BIN, os.X_OK)):
        return 'the ssh client (%s) is not installed on this node' % SSH_BIN
    if not os.access(SETSID_BIN, os.X_OK):
        return '%s (util-linux) is not installed on this node' % SETSID_BIN
    return None


@bp.route('/api/terminal')
def terminal_info():
    if getattr(g, 'identity_role', None) != 'admin':
        return err('Administrator access required', 403)
    reason = _unavailable()
    return jsonify({'success': True, 'available': reason is None, 'reason': reason,
                    'host': SSH_HOST, 'port': sshd_port(),
                    'sessions': _sessions, 'max_sessions': MAX_SESSIONS,
                    'idle_timeout': idle_timeout()})


def ws_terminal(ws):
    """Bridge an xterm.js session to `ssh <user>@127.0.0.1`. require_login
    authenticated the handshake; the rest is checked here because websocket
    routes are not blueprint-scoped."""
    if getattr(g, 'identity_role', None) != 'admin':
        return _term_fail(ws, 'Administrator access required')
    if 'terminal' in load_disabled_modules():
        return _term_fail(ws, "module 'terminal' is disabled on this node")
    if not same_origin(request.headers.get('Origin'), request.host):
        return _term_fail(ws, 'Cross-origin terminal connections are refused')
    user = request.args.get('user', '')
    if not RE_TERM_USER.match(user):
        return _term_fail(ws, 'Invalid login name')
    reason = _unavailable()
    if reason:
        return _term_fail(ws, reason)

    global _sessions
    with _sessions_lock:
        if _sessions >= MAX_SESSIONS:
            return _term_fail(ws, 'Too many open terminals on this node (limit %d)'
                            % MAX_SESSIONS)
        _sessions += 1

    who, ip = getattr(g, 'identity_name', None), request.remote_addr
    started = time.time()
    proc = fd = None
    try:
        proc, fd = _spawn(ssh_argv(user), ssh_env())
        audit(who, ip, 'WS', '/ws/terminal', {'login': user, 'event': 'open'}, 200)
        _bridge(ws, fd)
    except Exception:
        pass
    finally:
        if proc is not None:
            _reap(proc, fd)
            audit(who, ip, 'WS', '/ws/terminal',
                  {'login': user, 'event': 'close',
                   'seconds': int(time.time() - started)}, 200)
        with _sessions_lock:
            _sessions -= 1
        _hang_up(ws)


def _bridge(ws, fd):
    """Pump until either side ends, the module is switched off, or the session
    has been silent for the idle timeout."""
    stop = threading.Event()
    last = [time.time()]
    idle = idle_timeout()

    def pump_pty_to_browser():
        try:
            while not stop.is_set():
                try:
                    data = os.read(fd, 16384)
                except OSError as e:
                    if e.errno == errno.EINTR:
                        continue
                    break                   # EIO: the child closed the pty
                if not data:
                    break
                last[0] = time.time()
                ws.send(data)
        except Exception:
            pass
        finally:
            stop.set()

    threading.Thread(target=pump_pty_to_browser, daemon=True).start()

    checked = time.time()
    while not stop.is_set():
        msg = ws.receive(timeout=5)
        now = time.time()
        if now - checked >= 15:
            checked = now
            if 'terminal' in load_disabled_modules():
                _term_fail(ws, "module 'terminal' was disabled on this node")
                break
        if msg is None:
            if idle and now - last[0] >= idle:
                _term_fail(ws, 'Closed after %d minutes idle' % (idle // 60))
                break
            continue
        last[0] = now
        try:
            obj = json.loads(msg) if isinstance(msg, str) else None
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict) and obj.get('type') == 'resize':
            try:
                _set_winsize(fd, obj.get('height', 24), obj.get('width', 80))
            except (OSError, ValueError, TypeError):
                pass
            continue
        if isinstance(obj, dict) and obj.get('type') == 'stdin':
            msg = obj.get('data', '')
        data = msg.encode('utf-8') if isinstance(msg, str) else bytes(msg)
        while data:
            data = data[os.write(fd, data):]
    stop.set()


def terminal_register_ws(sock):
    """Attach the terminal websocket to the app's flask-sock instance."""
    sock.route('/ws/terminal')(ws_terminal)


# ─── Module descriptor (consumed by core.registry at create_app) ───────
# Default-off: it puts this node's ssh login behind the dashboard's port, so
# it is an operator's per-node decision (think twice where port 22 is
# deliberately firewalled and :8443 is not).
MODULE = {'id': 'terminal', 'order': 205, 'label': 'Terminal', 'category': 'Terminal',
          'default_enabled': False,
          'nav': {'cat': 'terminal', 'cat_order': 95, 'pages': [
                  {'id': 'terminal', 'label': 'Terminal', 'icon': 'term',
                   'admin_only': True}]},
          'blueprint': bp,
          # Descriptor spelling, not a module-level `register_ws`: that name is
          # facade-pinned to the LXD console's (tests/test_facade_winners.py).
          'websockets': terminal_register_ws}
