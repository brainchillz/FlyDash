"""SSH Server — the node's own OpenSSH daemon (Settings > SSH Server).

Three things, all admin-only (the page shows who is logged in from where and
decides who can log in at all):

- **Control:** start / stop / restart / reload and enable / disable at boot,
  through the blanket `systemctl` grant. Ubuntu runs sshd SOCKET-ACTIVATED
  (`ssh.socket` enabled and listening, `ssh.service` "disabled" and started
  on the first connection), RHEL and older Debian run the plain service —
  `unit_plan()` picks the units an action really has to touch, so "Stop" on
  Ubuntu stops the socket too (stopping only the service would be undone by
  the next connection) and "Enabled at boot" follows the socket there.
  Stop and disable are `needs_confirm`: the dashboard stays reachable, the
  remote shell does not.
- **Settings:** a typed allowlist (`KNOBS`) written to ONE drop-in,
  `/etc/ssh/sshd_config.d/01-nexus-dashboard.conf`. sshd takes the FIRST
  value it reads for a keyword and the distributions `Include` the drop-in
  directory at the top of sshd_config, so a drop-in beats the main file and
  an earlier-sorting drop-in beats a later one — hence `01-`, ahead of
  cloud-init's `50-cloud-init.conf` (which is where Ubuntu's
  `PasswordAuthentication` really comes from). The main file and every
  other drop-in are never touched; "Reset" removes the one file. Nothing
  free-text is offered: no `Match`, no `Include`, no `ForceCommand`,
  no `AuthorizedKeysCommand`, no `Subsystem` (the NOTIFYCMD rule — sshd
  runs those as root).
- **Apply is test-then-commit:** the new file is installed, `sshd -t`
  validates the WHOLE configuration, and a refusal puts the previous file
  back before anything is reloaded — a typo can never leave a daemon that
  will not start. Then `daemon-reload` (Ubuntu's sshd-socket-generator
  derives the socket's ports from the config), a socket restart only when
  the listening addresses actually changed, and a reload of the running
  service. Established sessions survive all of it.

What is shown as the value in force is `sshd -T` (the daemon's own answer,
never a re-implementation of its parser); it needs root, so a non-root
dashboard asks the `<prefix>-sshd` helper, which is also the only thing that
writes. Without root or the helper the page degrades to read-only with the
values it can read from the config files itself (`effective_source:
'files'`).

Lockout guards are warnings with `needs_confirm`, the firewall module's
precedent: changing the port or listen addresses, turning password or key
authentication off, setting AllowUsers / AllowGroups. Two cases are refused
outright: every authentication method off, and a port equal to the
dashboard's own.
"""
import glob
import json
import os
import re

from flask import Blueprint, g, jsonify, request

from ..core.config import DASHBOARD_PORT, HELPER_PREFIX
from ..core.runcmd import run, err

bp = Blueprint('sshd', __name__)

SSHD_BIN = os.environ.get('DASHBOARD_SSHD_BIN', '/usr/sbin/sshd')
SSHD_CONFIG = os.environ.get('DASHBOARD_SSHD_CONFIG', '/etc/ssh/sshd_config')
SSHD_DROPIN_DIR = os.environ.get('DASHBOARD_SSHD_DROPIN_DIR', '/etc/ssh/sshd_config.d')
SSHD_UNIT = os.environ.get('DASHBOARD_SSHD_UNIT', '')       # '' = detect ssh / sshd
DROPIN_NAME = '01-nexus-dashboard.conf'
SSHD_HELPER = HELPER_PREFIX + '-sshd'
HOST_KEY_GLOB = '/etc/ssh/ssh_host_*_key.pub'
MAX_DROPIN_BYTES = 8 * 1024

UNIT_ACTIONS = ('start', 'stop', 'restart', 'reload', 'enable', 'disable')
CONFIRM_ACTIONS = ('stop', 'disable')
SHOW_PROPS = 'Id,LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,MainPID'

YN = ('yes', 'no')
# Account and group names as sshd patterns: no whitespace, no quote, no
# shell or config metacharacter. `*` and `?` are sshd's own wildcards and
# `@host` restricts a user to a source, so they are allowed.
RE_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_.*?@:/-]{1,64}\Z')
RE_LISTEN_V4 = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\Z')
RE_LISTEN_V6 = re.compile(r'^[0-9A-Fa-f:]{2,39}\Z')
RE_INT = re.compile(r'^\d{1,6}\Z')
RE_PLAIN = re.compile(r'^[\x20-\x7e]*\Z')
RE_UNIT_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}\Z')

# (keyword, label, group, type, extra, help)
#   yn     — yes / no
#   enum   — extra = the choices
#   int    — extra = (lo, hi)
#   ports  — 1..4 distinct ports, space separated (one `Port` line each)
#   addrs  — 0..4 listen addresses (IPv4 / IPv6 literals, no port, no name)
#   names  — 1..32 account / group patterns on one line
KNOBS = [
    ('Port', 'Port', 'access', 'ports', None,
     'TCP port(s) sshd listens on. Up to four, space separated.'),
    ('ListenAddress', 'Listen addresses', 'access', 'addrs', None,
     'Bind only to these local addresses (IPv4 or IPv6 literals). Empty = every address.'),
    ('PermitRootLogin', 'Root login', 'access', 'enum',
     ('no', 'prohibit-password', 'forced-commands-only', 'yes'),
     'prohibit-password = root may log in with a key but never a password.'),
    ('AllowUsers', 'Allowed users', 'access', 'names', None,
     'Only these accounts may log in (space separated; * and ? match, user@host limits the source). '
     'Empty = no restriction. Everyone not listed is locked out.'),
    ('AllowGroups', 'Allowed groups', 'access', 'names', None,
     'Only members of these groups may log in. Empty = no restriction.'),
    ('PasswordAuthentication', 'Password authentication', 'auth', 'yn', None,
     'Log in with the account password. The Terminal app needs this on.'),
    ('PubkeyAuthentication', 'Public-key authentication', 'auth', 'yn', None,
     'Log in with a key from ~/.ssh/authorized_keys.'),
    ('KbdInteractiveAuthentication', 'Keyboard-interactive authentication', 'auth', 'yn', None,
     'PAM challenge-response (one-time codes and the like).'),
    ('PermitEmptyPasswords', 'Empty passwords', 'auth', 'yn', None,
     'Accept accounts whose password is empty. Leave off.'),
    ('MaxAuthTries', 'Authentication attempts', 'auth', 'int', (1, 20),
     'Attempts per connection before it is dropped.'),
    ('LoginGraceTime', 'Login grace time (s)', 'auth', 'int', (10, 600),
     'Seconds a connection may stay unauthenticated.'),
    ('MaxSessions', 'Sessions per connection', 'session', 'int', (1, 100),
     'Shell / exec / sftp sessions multiplexed over one connection.'),
    ('ClientAliveInterval', 'Keep-alive interval (s)', 'session', 'int', (0, 3600),
     'Probe an idle client every N seconds; 0 = never.'),
    ('ClientAliveCountMax', 'Keep-alive misses', 'session', 'int', (0, 20),
     'Unanswered probes before the session is closed.'),
    ('X11Forwarding', 'X11 forwarding', 'forwarding', 'yn', None, ''),
    ('AllowTcpForwarding', 'TCP forwarding', 'forwarding', 'enum', ('yes', 'local', 'remote', 'no'),
     'local = -L only, remote = -R only.'),
    ('AllowAgentForwarding', 'Agent forwarding', 'forwarding', 'yn', None, ''),
    ('GatewayPorts', 'Gateway ports', 'forwarding', 'enum', ('no', 'yes', 'clientspecified'),
     'Whether remote hosts may connect to ports forwarded for a client.'),
    ('UseDNS', 'Reverse DNS lookups', 'other', 'yn', None,
     'Resolve the client address at login (slow when DNS is).'),
    ('PrintMotd', 'Print /etc/motd', 'other', 'yn', None, ''),
]
KNOB_INDEX = {k[0]: k for k in KNOBS}
KNOB_BY_LOWER = {k[0].lower(): k[0] for k in KNOBS}
GROUPS = [('access', 'Access'), ('auth', 'Authentication'), ('session', 'Sessions'),
          ('forwarding', 'Forwarding'), ('other', 'Other')]
LISTENER_KEYS = ('Port', 'ListenAddress')
AUTH_KEYS = ('PasswordAuthentication', 'PubkeyAuthentication', 'KbdInteractiveAuthentication')


# ─── Validation / rendering (pure) ────────────────────────────────────

def _valid_v4(s):
    m = RE_LISTEN_V4.match(s)
    return bool(m) and all(int(x) <= 255 for x in m.groups())


def validate_setting(key, value):
    """(normalized value, None) or (None, error). The value is what goes on
    the config line(s); list types are returned space-joined."""
    spec = KNOB_INDEX.get(key)
    if not spec:
        return None, '%s is not a setting this page manages' % str(key)[:40]
    if not isinstance(value, str):
        return None, '%s: value must be text' % key
    # Only printable ASCII and plain spaces: a newline or tab must be refused,
    # never folded into a separator (it would otherwise start a second line).
    if not RE_PLAIN.match(value):
        return None, '%s: only letters, digits, punctuation and spaces are allowed' % key
    v = ' '.join(value.split(' '))
    v = ' '.join(x for x in v.split(' ') if x)
    if not v or len(v) > 1024:
        return None, '%s: empty or too long' % key
    typ, extra = spec[3], spec[4]
    if typ == 'yn':
        return (v, None) if v in YN else (None, '%s must be yes or no' % key)
    if typ == 'enum':
        return (v, None) if v in extra else (None, '%s must be one of: %s' % (key, ', '.join(extra)))
    if typ == 'int':
        if not RE_INT.match(v) or not extra[0] <= int(v) <= extra[1]:
            return None, '%s must be a whole number from %d to %d' % (key, extra[0], extra[1])
        return str(int(v)), None
    items = v.split(' ')
    if typ == 'ports':
        if len(items) > 4:
            return None, 'Port: at most four ports'
        out = []
        for p in items:
            if not RE_INT.match(p) or not 1 <= int(p) <= 65535:
                return None, 'Port: %s is not a port number (1-65535)' % p[:12]
            if str(int(p)) not in out:
                out.append(str(int(p)))
        return ' '.join(out), None
    if typ == 'addrs':
        if len(items) > 4:
            return None, 'ListenAddress: at most four addresses'
        out = []
        for a in items:
            if not (_valid_v4(a) or (':' in a and RE_LISTEN_V6.match(a))):
                return None, 'ListenAddress: %s is not an IPv4 or IPv6 address' % a[:45]
            if a not in out:
                out.append(a)
        return ' '.join(out), None
    if typ == 'names':
        if len(items) > 32:
            return None, '%s: at most 32 entries' % key
        for n in items:
            if not RE_NAME_PATTERN.match(n) or n.startswith('-'):
                return None, '%s: %r is not a valid name or pattern' % (key, n[:40])
        return ' '.join(dict.fromkeys(items)), None
    return None, '%s: unsupported type' % key


def render_dropin(overrides):
    """Drop-in text for {Keyword: normalized value}. Deterministic (KNOBS
    order), so a no-op save is byte-identical. Port and ListenAddress are
    one line per value, which is how sshd wants them repeated."""
    lines = ['# Written by the dashboard SSH Server page - settings chosen there.',
             '# sshd uses the FIRST value it reads for a keyword, so this file (sorted early)',
             '# wins over later drop-ins and over sshd_config. Edit on the page; "Reset"',
             '# removes this file. Nothing else under /etc/ssh is touched.']
    for spec in KNOBS:
        key = spec[0]
        if key not in overrides:
            continue
        v = overrides[key]
        if spec[3] in ('ports', 'addrs'):
            lines += ['%s %s' % (key, item) for item in v.split(' ')]
        else:
            lines.append('%s %s' % (key, v))
    return '\n'.join(lines) + '\n'


def parse_dropin(text):
    """{Keyword: value} for the managed keywords in OUR drop-in (canonical
    spelling; repeated Port / ListenAddress lines joined with a space)."""
    out = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.replace('=', ' ', 1).split(None, 1)
        if len(parts) != 2:
            continue
        key = KNOB_BY_LOWER.get(parts[0].lower())
        if not key:
            continue
        val = ' '.join(parts[1].split())
        if KNOB_INDEX[key][3] in ('ports', 'addrs') and key in out:
            out[key] += ' ' + val
        elif key not in out:
            out[key] = val
    return out


RE_LISTEN_T = re.compile(r'^(?:\[([0-9A-Fa-f:.]+)\]|([0-9.]+)):(\d+)\Z')


def parse_effective(text):
    """`sshd -T` output -> {Keyword: value} for the managed keywords, in the
    same normalized form `validate_setting` produces. `listenaddress` there
    is always `addr:port` and present even when nothing was configured, so
    the wildcard pair is reported as '' (= every address)."""
    multi = {}
    for raw in (text or '').splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            continue
        multi.setdefault(parts[0].lower(), []).append(parts[1].strip())
    out = {}
    for spec in KNOBS:
        key, typ = spec[0], spec[3]
        vals = multi.get(key.lower())
        if vals is None:
            continue
        if typ == 'ports':
            out[key] = ' '.join(dict.fromkeys(vals))
        elif typ == 'addrs':
            addrs = []
            for v in vals:
                m = RE_LISTEN_T.match(v)
                a = (m.group(1) or m.group(2)) if m else v
                if a not in ('0.0.0.0', '::') and a not in addrs:
                    addrs.append(a)
            out[key] = ' '.join(addrs)
        elif typ == 'names':
            out[key] = ' '.join(dict.fromkeys(' '.join(vals).split()))
        else:
            out[key] = vals[0]
    return out


def listeners(text):
    """The (sorted) listen sockets in an `sshd -T` dump — what decides
    whether a socket-activated listener has to be re-bound."""
    out = set()
    for raw in (text or '').splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ('port', 'listenaddress'):
            out.add(parts[0].lower() + ' ' + parts[1].strip())
    return sorted(out)


RE_INCLUDE = re.compile(r'^\s*Include\s+(.+?)\s*$', re.I)


def includes_dropins(main_text, dropin_dir):
    """True when sshd_config Includes the drop-in directory BEFORE any
    setting or Match block — the only arrangement in which a drop-in wins."""
    want = dropin_dir.rstrip('/')
    for raw in (main_text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = RE_INCLUDE.match(line)
        if m:
            for pat in m.group(1).split():
                pat = pat.strip('"')
                if not pat.startswith('/'):
                    pat = '/etc/ssh/' + pat
                if os.path.dirname(pat).rstrip('/') == want:
                    return True
            continue
        if line.split(None, 1)[0].lower() == 'match':
            return False
    return False


def files_effective(main_text, dropin_texts):
    """Best-effort first-value-wins read of the managed keywords from the
    config files themselves: drop-ins (in sorted order) then the main file,
    stopping at the first `Match`. Used only when `sshd -T` is out of reach;
    it cannot see compiled-in defaults, so a keyword nobody sets is absent."""
    out = {}
    for text in list(dropin_texts) + [main_text or '']:
        got = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.split(None, 1)[0].lower() == 'match':
                break
            for k, v in parse_dropin(line).items():
                if KNOB_INDEX[k][3] in ('ports', 'addrs') and k in got:
                    got[k] += ' ' + v
                elif k not in got:
                    got[k] = v
        for k, v in got.items():
            out.setdefault(k, v)
    return out


def parse_show(text):
    out = {}
    for line in (text or '').splitlines():
        k, sep, v = line.partition('=')
        if sep:
            out[k.strip()] = v.strip()
    return out


RE_WHO = re.compile(r'^(\S+)\s+(\S+)\s+(\d{4}-\d\d-\d\d \d\d:\d\d|\w{3}\s+\d+\s+\d\d:\d\d)\s*(?:\((.*)\))?\s*$')


def parse_who(text):
    """Remote logins from `who` (rows with a source in parentheses that is
    not a local display or tmux/screen pseudo-source)."""
    out = []
    for raw in (text or '').splitlines():
        m = RE_WHO.match(raw.strip())
        if not m or not m.group(4):
            continue
        src = m.group(4).strip()
        if src.startswith(':') or src.startswith('tmux') or src.startswith('screen'):
            continue
        out.append({'user': m.group(1), 'tty': m.group(2), 'since': m.group(3), 'from': src})
    return out


RE_KEYGEN = re.compile(r'^(\d+)\s+(\S+)\s+.*\(([A-Za-z0-9-]+)\)\s*$')


def parse_fingerprint(line):
    """`ssh-keygen -lf` line -> {bits, fingerprint, type}; the key's comment
    (user@host) is dropped."""
    m = RE_KEYGEN.match((line or '').strip())
    if not m:
        return None
    return {'bits': int(m.group(1)), 'fingerprint': m.group(2), 'type': m.group(3)}


def unit_plan(action, service, svc, sock):
    """The systemctl invocations an action needs, as argv tails. `svc` and
    `sock` are parsed `systemctl show` dicts ({} = unit absent).

    Socket-managed means the socket unit exists and is what is enabled or
    listening (Ubuntu): boot state follows the socket, "start" opens the
    socket (the service follows on the first connection) and "stop" must
    stop BOTH or the next connection starts the service again. Returns a
    list of ['verb', unit, ...]."""
    s, k = service + '.service', service + '.socket'
    sock_present = bool(sock) and sock.get('LoadState') == 'loaded'
    sock_managed = sock_present and (sock.get('UnitFileState') == 'enabled'
                                     or sock.get('ActiveState') == 'active')
    svc_active = (svc or {}).get('ActiveState') == 'active'
    if action == 'start':
        return [['start', k]] if sock_managed else [['start', s]]
    if action == 'stop':
        return [['stop', k, s]] if sock_present else [['stop', s]]
    if action == 'restart':
        if sock_managed:
            return [['restart', s]] if svc_active else [['restart', k]]
        return [['restart', s]]
    if action == 'reload':
        return [['reload', s]] if svc_active else []
    if action == 'enable':
        return [['enable', k]] if sock_managed or (sock_present and not svc_active) else [['enable', s]]
    if action == 'disable':
        return [['disable', k, s]] if sock_present else [['disable', s]]
    return []


def lockout_review(current, after, changed, dashboard_port):
    """(refusal or None, [warnings]) for a settings change. `current` and
    `after` are effective {Keyword: value} maps, `changed` the keywords
    whose value differs. Pure."""
    ports = (after.get('Port') or '').split()
    if 'Port' in changed and str(dashboard_port) in ports:
        return 'Port %s is the dashboard\'s own port' % dashboard_port, []
    if all(after.get(k) == 'no' for k in AUTH_KEYS):
        return ('Password, public-key and keyboard-interactive authentication would all be off - '
                'nobody could log in'), []
    w = []
    if 'Port' in changed:
        w.append('sshd will listen on port %s instead of %s. Open the new port in the firewall first; '
                 'clients, scripts and automation that connect to the old port will fail.'
                 % (', '.join(ports) or '?', ', '.join((current.get('Port') or '?').split())))
    if 'ListenAddress' in changed:
        w.append('sshd will accept connections only on %s. A session that arrives on any other address '
                 'is refused.' % (after.get('ListenAddress') or 'every address'))
    if 'PasswordAuthentication' in changed and after.get('PasswordAuthentication') == 'no':
        w.append('Password logins are turned off: only accounts with a working key (or another method) '
                 'can log in, and the Terminal app stops working.')
    if 'PubkeyAuthentication' in changed and after.get('PubkeyAuthentication') == 'no':
        w.append('Key logins are turned off: anything that connects with a key (backups, replication, '
                 'deployment tooling) will be refused.')
    for k, what in (('AllowUsers', 'accounts'), ('AllowGroups', 'groups')):
        if k in changed and after.get(k):
            w.append('%s is set: only these %s can log in - %s. Everyone else is locked out, including '
                     'the account you normally use if it is not listed.' % (k, what, after[k]))
    if 'PermitRootLogin' in changed and after.get('PermitRootLogin') == 'yes':
        w.append('root will be able to log in with a password.')
    if 'PermitEmptyPasswords' in changed and after.get('PermitEmptyPasswords') == 'yes':
        w.append('Accounts with an empty password will be able to log in without one.')
    return None, w


# ─── System side ──────────────────────────────────────────────────────

def _is_admin():
    return getattr(g, 'identity_role', None) == 'admin'


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _dropin_path():
    return os.path.join(SSHD_DROPIN_DIR, DROPIN_NAME)


def _show(unit):
    out, _e, rc = run(['systemctl', 'show', unit, '-p', SHOW_PROPS])
    s = parse_show(out) if rc == 0 else {}
    return s if s.get('LoadState') == 'loaded' else {}


def _service_name():
    """'ssh' (Debian/Ubuntu) or 'sshd' (RHEL). Debian's ssh.service carries
    the alias sshd.service, so ask for `ssh` first."""
    if SSHD_UNIT and RE_UNIT_NAME.match(SSHD_UNIT):
        return SSHD_UNIT[:-8] if SSHD_UNIT.endswith('.service') else SSHD_UNIT
    for name in ('ssh', 'sshd'):
        if _show(name + '.service') or _show(name + '.socket'):
            return name
    return 'ssh'


def _helper_ok():
    return os.path.isfile(SSHD_HELPER) and os.access(SSHD_HELPER, os.X_OK)


def _sshd_T():
    """(`sshd -T` text or None, how). Root asks sshd directly; otherwise the
    helper does (sshd -T reads the private host keys)."""
    if os.geteuid() == 0:
        out, _e, rc = run([SSHD_BIN, '-T'], no_sudo=True, timeout=20)
        return (out, 'sshd -T') if rc == 0 and out else (None, None)
    if _helper_ok():
        out, _e, rc = run([SSHD_HELPER, 'effective'], timeout=20)
        return (out, 'sshd -T') if rc == 0 and out else (None, None)
    return None, None


def _other_dropins():
    try:
        names = sorted(n for n in os.listdir(SSHD_DROPIN_DIR)
                       if n.endswith('.conf') and n != DROPIN_NAME)
    except OSError:
        return []
    return names


def _effective():
    """({Keyword: value}, source, raw `sshd -T` text or None)."""
    raw, how = _sshd_T()
    if raw:
        return parse_effective(raw), how, raw
    texts = []
    try:
        names = sorted(n for n in os.listdir(SSHD_DROPIN_DIR) if n.endswith('.conf'))
    except OSError:
        names = []
    for n in names:
        t = _read(os.path.join(SSHD_DROPIN_DIR, n))
        if t:
            texts.append(t)
    return files_effective(_read(SSHD_CONFIG), texts), 'files', None


def _edit_state():
    """(editable, reason)."""
    if not os.path.isfile(SSHD_BIN):
        return False, 'OpenSSH server is not installed (%s not found)' % SSHD_BIN
    main = _read(SSHD_CONFIG)
    if main is None:
        return False, '%s cannot be read' % SSHD_CONFIG
    if not includes_dropins(main, SSHD_DROPIN_DIR):
        return False, ('%s does not Include %s/*.conf ahead of its own settings, so a drop-in would have '
                       'no effect; this page never edits the main file' % (SSHD_CONFIG, SSHD_DROPIN_DIR))
    if os.geteuid() == 0 or _helper_ok():
        return True, ''
    return False, ('the dashboard is not root and the %s helper is not installed '
                   '(run the installer with --helpers-only)' % os.path.basename(SSHD_HELPER))


def _apply_as_root(text, service):
    """Install (or, text=None, remove) the drop-in, validate the whole
    configuration and reload. The helper carries the same steps for a
    non-root dashboard. Returns (result dict, error or None)."""
    path = _dropin_path()
    old = _read(path)
    before, _e, _rc = run([SSHD_BIN, '-T'], no_sudo=True, timeout=20)
    try:
        if text is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            os.makedirs(SSHD_DROPIN_DIR, 0o755, exist_ok=True)
            tmp = os.path.join(SSHD_DROPIN_DIR, '.' + DROPIN_NAME + '.tmp')
            with open(tmp, 'w') as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
    except OSError as e:
        return None, 'cannot write %s: %s' % (path, e)
    _o, e, rc = run([SSHD_BIN, '-t'], no_sudo=True, timeout=20)
    if rc != 0:
        try:
            if old is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, 'w') as f:
                    f.write(old)
        except OSError:
            pass
        return None, 'sshd refused the configuration, nothing was changed: ' + (e or '').strip()[-400:]
    after, _e, _rc = run([SSHD_BIN, '-T'], no_sudo=True, timeout=20)
    res = {'listeners_restarted': False, 'reloaded': False}
    sock = _show(service + '.socket')
    if sock:
        run(['systemctl', 'daemon-reload'], no_sudo=True)
        if sock.get('ActiveState') == 'active' and listeners(before) != listeners(after):
            _o, e, rc = run(['systemctl', 'restart', service + '.socket'], no_sudo=True)
            res['listeners_restarted'] = rc == 0
            if rc != 0:
                res['reload_error'] = (e or 'socket restart failed').strip()[-300:]
    if _show(service + '.service').get('ActiveState') == 'active':
        _o, e, rc = run(['systemctl', 'reload', service + '.service'], no_sudo=True)
        res['reloaded'] = rc == 0
        if rc != 0:
            res['reload_error'] = (e or 'reload failed').strip()[-300:]
    return res, None


def _apply(text, service):
    if os.geteuid() == 0:
        return _apply_as_root(text, service)
    argv = [SSHD_HELPER, 'reset', service] if text is None else [SSHD_HELPER, 'apply', service]
    out, e, rc = run(argv, input_data=text, timeout=60)
    if rc != 0:
        return None, (e or out or 'helper failed').strip()[-500:]
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {}
    return res if isinstance(res, dict) else {}, None


def _host_keys():
    out = []
    for p in sorted(glob.glob(HOST_KEY_GLOB)):
        o, _e, rc = run(['ssh-keygen', '-lf', p], no_sudo=True, timeout=10)
        fp = parse_fingerprint(o.splitlines()[0]) if rc == 0 and o else None
        if fp:
            fp['file'] = os.path.basename(p)
            out.append(fp)
    return out


def _version():
    _o, e, _rc = run([SSHD_BIN, '-V'], no_sudo=True, timeout=10)
    line = ((e or _o or '').strip().splitlines() or [''])[0]
    return line.split(',')[0].strip() if line.startswith('OpenSSH') else ''


def _status(full=True):
    installed = os.path.isfile(SSHD_BIN)
    service = _service_name()
    svc, sock = _show(service + '.service'), _show(service + '.socket')
    sock_present = bool(sock)
    sock_managed = sock_present and (sock.get('UnitFileState') == 'enabled'
                                     or sock.get('ActiveState') == 'active')
    svc_active = svc.get('ActiveState') == 'active'
    editable, reason = _edit_state()
    st = {'installed': installed and bool(svc or sock), 'service': service,
          'service_state': svc.get('ActiveState') or 'absent', 'service_sub': svc.get('SubState') or '',
          'service_enabled': svc.get('UnitFileState') == 'enabled',
          'since': svc.get('ActiveEnterTimestamp') or '',
          'socket_present': sock_present, 'socket_activated': sock_managed,
          'socket_state': sock.get('ActiveState') or '', 'socket_enabled': sock.get('UnitFileState') == 'enabled',
          'accepting': (sock.get('ActiveState') == 'active') if sock_managed else svc_active,
          'boot_enabled': (sock.get('UnitFileState') == 'enabled') if sock_managed
                          else svc.get('UnitFileState') == 'enabled',
          'editable': editable, 'readonly_reason': reason,
          'helper': SSHD_HELPER, 'dropin': _dropin_path(), 'dashboard_port': DASHBOARD_PORT}
    if not installed:
        st['error'] = 'OpenSSH server is not installed (%s not found)' % SSHD_BIN
    if not full:
        return st
    eff, source, _raw = _effective()
    dropin_text = _read(_dropin_path())
    overrides = parse_dropin(dropin_text)
    rows = []
    for key, label, group, typ, extra, help_ in KNOBS:
        row = {'key': key, 'label': label, 'group': group, 'type': typ, 'help': help_,
               'value': eff.get(key), 'override': overrides.get(key)}
        if typ == 'enum':
            row['choices'] = list(extra)
        elif typ == 'yn':
            row['choices'] = list(YN)
        elif typ == 'int':
            row['range'] = list(extra)
        # An override that sshd does not report back is being shadowed by a
        # drop-in that sorts before ours (first value wins).
        row['shadowed'] = bool(source == 'sshd -T' and key in overrides
                               and eff.get(key) is not None and eff.get(key) != overrides[key])
        rows.append(row)
    who, _e, rc = run(['who'], no_sudo=True, timeout=10)
    st.update({'version': _version(), 'settings': rows, 'groups': [list(x) for x in GROUPS],
               'effective_source': source, 'ports': (eff.get('Port') or '').split(),
               'dropin_present': dropin_text is not None, 'dropin_text': dropin_text or '',
               'other_dropins': _other_dropins(), 'sessions': parse_who(who) if rc == 0 else [],
               'host_keys': _host_keys()})
    return st


# ─── Routes ───────────────────────────────────────────────────────────

@bp.route('/api/sshd')
def sshd_status():
    if not _is_admin():
        return err('Administrator access required', 403)
    return jsonify(_status())


@bp.route('/api/sshd/action', methods=['POST'])
def sshd_action():
    if not _is_admin():
        return err('Administrator access required', 403)
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    if action not in UNIT_ACTIONS:
        return err('Unknown action')
    service = _service_name()
    svc, sock = _show(service + '.service'), _show(service + '.socket')
    if not svc and not sock:
        return err('no %s.service on this node' % service, 404)
    if action in CONFIRM_ACTIONS and not data.get('confirm'):
        return jsonify({'success': False, 'needs_confirm': True, 'warning': (
            'Stopping the SSH server ends remote shell access to this node: new ssh, scp, rsync and '
            'replication connections are refused until it is started again here. Sessions that are '
            'already open normally stay open. The dashboard itself stays reachable.' if action == 'stop' else
            'With the SSH server disabled at boot, this node comes back from a reboot with no remote '
            'shell until it is enabled again here.')})
    plan = unit_plan(action, service, svc, sock)
    if not plan:
        return jsonify({'success': True, 'action': action,
                        'note': 'sshd is not running - the next connection starts it with the current '
                                'configuration, there is nothing to reload'})
    ran = []
    for argv in plan:
        _o, e, rc = run(['systemctl'] + argv)
        ran.append('systemctl ' + ' '.join(argv))
        if rc != 0:
            return jsonify({'success': False, 'action': action, 'ran': ran,
                            'error': (e or 'systemctl failed').strip()[-400:]}), 500
    return jsonify({'success': True, 'action': action, 'ran': ran})


def _refuse_readonly():
    editable, reason = _edit_state()
    return None if editable else err('SSH settings are read-only on this node: ' + reason)


@bp.route('/api/sshd/settings', methods=['POST'])
def sshd_settings():
    """Merge {settings} into the drop-in: a keyword that is absent is left
    alone, present-but-blank (or null) removes the override, anything else
    is validated and set. An empty result removes the file."""
    if not _is_admin():
        return err('Administrator access required', 403)
    data = request.get_json(silent=True) or {}
    raw = data.get('settings')
    if not isinstance(raw, dict) or len(raw) > 64:
        return err('settings must be an object of {Keyword: value}')
    refusal = _refuse_readonly()
    if refusal:
        return refusal
    current, source, _raw = _effective()
    overrides = parse_dropin(_read(_dropin_path()))
    new = dict(overrides)
    for k, v in raw.items():
        if k not in KNOB_INDEX:
            return err('%s is not a setting this page manages' % str(k)[:40])
        if v is None or (isinstance(v, str) and not v.strip()):
            new.pop(k, None)
            continue
        nv, e = validate_setting(k, v if isinstance(v, str) else str(v))
        if e:
            return err(e)
        new[k] = nv
    # Carrying a value sshd already uses (and we were not overriding) would
    # only pin today's default; drop it so the file holds real choices.
    for k in list(new):
        if k not in overrides and current.get(k) == new[k]:
            del new[k]
    if new == overrides:
        return jsonify({'success': True, 'changed': [], 'overrides': overrides, 'noop': True,
                        'dropin': _dropin_path(), 'dropin_present': bool(overrides)})
    after = dict(current)
    after.update(new)
    cleared = [k for k in overrides if k not in new]
    changed = sorted(k for k in new if after.get(k) != current.get(k))
    refusal, warnings = lockout_review(current, after, changed, DASHBOARD_PORT)
    if refusal:
        return err(refusal)
    if any(k in LISTENER_KEYS or k in AUTH_KEYS or k.startswith('Allow') for k in cleared):
        warnings.append('Removing the override for %s hands that setting back to the system configuration '
                        '(another drop-in or sshd_config); check the value shown after applying.'
                        % ', '.join(k for k in cleared))
    if warnings and not data.get('confirm'):
        return jsonify({'success': False, 'needs_confirm': True, 'warning': '\n\n'.join(warnings),
                        'warnings': warnings})
    text = render_dropin(new) if new else None
    if text is not None and len(text.encode()) > MAX_DROPIN_BYTES:
        return err('settings too large')
    res, e = _apply(text, _service_name())
    if e:
        return err(e, 500)
    eff_after, source_after, _r = _effective()
    shadowed = sorted(k for k in new if source_after == 'sshd -T' and eff_after.get(k) != new[k])
    out = {'success': True, 'changed': changed, 'cleared': cleared, 'overrides': new,
           'dropin': _dropin_path(), 'dropin_present': bool(new), 'shadowed': shadowed}
    out.update(res or {})
    if shadowed:
        out['warning'] = ('Written, but sshd still reports a different value for %s: a drop-in that sorts '
                          'before %s sets it first.' % (', '.join(shadowed), DROPIN_NAME))
    return jsonify(out)


@bp.route('/api/sshd/settings/reset', methods=['POST'])
def sshd_settings_reset():
    if not _is_admin():
        return err('Administrator access required', 403)
    data = request.get_json(silent=True) or {}
    refusal = _refuse_readonly()
    if refusal:
        return refusal
    overrides = parse_dropin(_read(_dropin_path()))
    if _read(_dropin_path()) is None:
        return jsonify({'success': True, 'noop': True, 'dropin_present': False})
    if overrides and not data.get('confirm'):
        return jsonify({'success': False, 'needs_confirm': True, 'warning': (
            'This removes every setting chosen on this page (%s). Each goes back to whatever the system '
            'configuration says - including the port and who may log in.' % ', '.join(sorted(overrides)))})
    res, e = _apply(None, _service_name())
    if e:
        return err(e, 500)
    out = {'success': True, 'overrides': {}, 'dropin_present': False}
    out.update(res or {})
    return jsonify(out)


MODULE = {'id': 'sshd', 'order': 202, 'label': 'SSH Server', 'category': 'System',
          'nav': {'cat': 'system', 'cat_order': 100, 'pages': [
                  # order 55 follows Firewall (50)
                  {'id': 'sshd', 'label': 'SSH Server', 'icon': 'key',
                   'admin_only': True, 'order': 55}]},
          'blueprint': bp}
