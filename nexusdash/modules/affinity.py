"""Affinity — StillDeadcode's standalone DeepSeek-V4-Flash engine (AI Tools > Affinity).

Same author and the same shape as Radiance: ONE systemd unit
(`affinity-deepseek.service` by default, `DASHBOARD_AFFINITY_UNIT`
overrides) whose ExecStart is a launcher that `docker run`s the engine in
the foreground, with the engine's arguments on the ExecStart command line
and the launcher's GPUS / NAME / AFF_* switches as `Environment=`. The
unit declares `Conflicts=` on whatever else wants the cards (vllm-qwen,
llama-server), so a start stops those — the page says so.

What differs from Radiance, and why this is its own module rather than a
flag on that one:

- **No /metrics, no /health.** The server speaks `/v1/models`,
  `/v1/chat/completions` and `/v1/completions` only. Readiness is
  `/v1/models` answering with a model id; everything else the operator
  wants to see — resident experts, VRAM placement, per-request speed and
  draft acceptance — the engine prints to its own stdout/stderr, so it is
  read back from the container log through the docker socket, anchored on
  the LAST `[build] affinity` start marker (the halogen budget-lines
  precedent: never recompute what the engine already printed).
- **No boot-parameter editor in this cut.** The settings are argv, not
  `Environment=`; the page shows them read-only.

Control: start / stop / restart / enable / disable through the blanket
`systemctl` grant. Default-off (halogen/radiance precedent): dead weight on
a node without the unit, and the page says "not installed" instead of
erroring.
"""
import os
import re
import shlex

from flask import Blueprint, jsonify, request

from ..core.runcmd import run, run_safe, err, _num
from ..core.validators import RE_SERVICE
from .docker import DockerError, docker_raw, _dk_demux_logs, RE_DK_NAME
from .radiance import parse_show, parse_env_string, parse_exec_stop_name, parse_models, _http_get

bp = Blueprint('affinity', __name__)

AFFINITY_UNIT = os.environ.get('DASHBOARD_AFFINITY_UNIT', 'affinity-deepseek')
AFFINITY_URL = os.environ.get('DASHBOARD_AFFINITY_URL', '')          # '' = http://127.0.0.1:<--port>
DEFAULT_PORT = 8095
DEFAULT_NAME = 'affinity-4'              # run.sh's NAME default at GPUS=4
LOG_TAIL = 400                           # enough for the start block + recent requests
WARM_START_S = 90                        # ~60 s load on 4 cards (static placement); the UI says so
IGNORED_CONFLICTS = ('shutdown.target',)

SHOW_PROPS = ('Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath,DropInPaths,'
              'NRestarts,ActiveEnterTimestamp,MainPID,Environment,ExecStart,ExecStop,Conflicts')
UNIT_ACTIONS = ('start', 'stop', 'restart', 'enable', 'disable')

# Settings worth naming on the page, from the ExecStart argv. Everything
# else the launcher was given is listed verbatim under "other arguments".
NAMED_ARGS = (('--placement-strategy', 'Expert placement'),
              ('--dspark', 'DSpark MTP draft'),
              ('--kv-size', 'KV positions reserved'),
              ('--kv-commit', 'KV positions committed'),
              ('--host', 'Listen address'),
              ('--port', 'Listen port'),
              ('--slots', 'Slots'),
              ('--temp', 'Temperature'))


# ─── Pure parsers ─────────────────────────────────────────────────────

RE_ARGV = re.compile(r'argv\[\]=(.*?) ; ignore_errors=')


def parse_exec_argv(show_value):
    """`systemctl show`'s ExecStart (`{ path=/x/run.sh ; argv[]=/x/run.sh --a b ; ignore_errors=… }`)
    → the argv list after the launcher, or []. Pure."""
    m = RE_ARGV.search(show_value or '')
    if not m:
        return []
    try:
        words = shlex.split(m.group(1))
    except ValueError:
        return []
    return words[1:] if words else []


def parse_args(argv):
    """`--flag value` pairs → dict; a flag followed by another flag (or the
    end) maps to 'on'. Pure."""
    out = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('--'):
            if i + 1 < len(argv) and not argv[i + 1].startswith('--'):
                out[a[2:]] = argv[i + 1]
                i += 2
                continue
            out[a[2:]] = 'on'
        i += 1
    return out


def parse_conflicts(show_value):
    """`Conflicts=` → the unit names (sans .service), minus systemd's own
    shutdown.target. Pure."""
    out = []
    for w in (show_value or '').split():
        if w in IGNORED_CONFLICTS:
            continue
        out.append(w[:-8] if w.endswith('.service') else w)
    return out


RE_START_MARK = re.compile(r'^\[build\] affinity\b')
RE_TP = re.compile(r'^tensor parallel across (\d+) GPUs')
RE_MODEL = re.compile(r'^model: (\d+) layers, (\d+) experts \(top-(\d+)\), hidden (\d+), vocab (\d+)')
RE_DSPARK = re.compile(r'^dspark: (\d+) stages, block (\d+)')
RE_KV = re.compile(r'^kv cache: ([\d,]+) positions reserved, ([\d,]+) backed with ([\d.]+) GiB')
RE_RESIDENT = re.compile(r'^(?:static placement|heat placement|placement): (\d+)/(\d+) experts resident \(([\d.]+)%\), ([\d.]+) GiB VRAM in ([\d.]+) s')
RE_HOST_POOL = re.compile(r'^host pool: (.*)$')
RE_DENSE = re.compile(r'^dense on gpu: (\d+) tensors, ([\d.]+) GiB across (\d+) ranks')
RE_LISTEN = re.compile(r'^listening on (http://\S+)')
RE_SLOT = re.compile(r'^slot (\d+): (\d+) tok in ([\d.]+)s \(([\d.]+) t/s\), (\d+) blocks, (\d+) drafted, (\d+) accepted '
                     r'\(([\d.]+)%, ([\d.]+) tok/block\)')
RE_ERROR = re.compile(r'^(error:|fatal:|.*\bHIP error\b|cross-rank: MISMATCH)')
STARTUP_KEEP = ('tensor parallel', 'model:', 'dspark:', 'kv cache:', 'prefill', 'dense on gpu', 'static placement',
                'heat placement', 'host pool', 'page cache', 'listening on', 'routed experts', 'placement headroom')


def _int(s):
    try:
        return int(str(s).replace(',', ''))
    except ValueError:
        return None


def startup_summary(log_text, recent=20):
    """The engine's own figures from its log, anchored on the LAST start
    marker so a restarted container never mixes two runs. Pure.
    {lines, build, tp, layers, experts, top_k, dspark, kv_reserved, kv_backed,
     kv_gib, experts_resident, experts_total, resident_pct, placement_gib,
     placement_s, host_pool, listening, ready, errors, requests:[…],
     requests_total, tokens_total, last_tps, mean_accept_pct}"""
    lines = (log_text or '').splitlines()
    start = 0
    for i, line in enumerate(lines):
        if RE_START_MARK.match(line):
            start = i
    run_lines = lines[start:]
    out = {'lines': [], 'build': None, 'tp': None, 'layers': None, 'experts': None, 'top_k': None,
           'dspark': None, 'kv_reserved': None, 'kv_backed': None, 'kv_gib': None,
           'experts_resident': None, 'experts_total': None, 'resident_pct': None,
           'placement_gib': None, 'placement_s': None, 'host_pool': None, 'listening': None,
           'ready': False, 'errors': 0, 'requests': [], 'requests_total': 0, 'tokens_total': 0,
           'last_tps': None, 'mean_accept_pct': None}
    accepted = drafted = 0
    for raw in run_lines:
        line = raw.rstrip()
        if not line:
            continue
        if RE_START_MARK.match(line):
            out['build'] = line[len('[build] '):].strip()
            out['lines'].append(line)
            continue
        m = RE_SLOT.match(line)
        if m:
            req = {'slot': int(m.group(1)), 'tokens': int(m.group(2)), 'seconds': float(m.group(3)),
                   'tps': float(m.group(4)), 'blocks': int(m.group(5)), 'drafted': int(m.group(6)),
                   'accepted': int(m.group(7)), 'accept_pct': float(m.group(8)), 'tok_per_block': float(m.group(9))}
            out['requests'].append(req)
            out['requests_total'] += 1
            out['tokens_total'] += req['tokens']
            out['last_tps'] = req['tps']
            drafted += req['drafted']
            accepted += req['accepted']
            continue
        if RE_ERROR.match(line):
            out['errors'] += 1
        m = RE_TP.match(line)
        if m:
            out['tp'] = int(m.group(1))
        m = RE_MODEL.match(line)
        if m:
            out['layers'], out['experts'], out['top_k'] = int(m.group(1)), int(m.group(2)), int(m.group(3))
        m = RE_DSPARK.match(line)
        if m:
            out['dspark'] = '%s stages, block %s' % (m.group(1), m.group(2))
        m = RE_KV.match(line)
        if m:
            out['kv_reserved'], out['kv_backed'], out['kv_gib'] = _int(m.group(1)), _int(m.group(2)), float(m.group(3))
        m = RE_RESIDENT.match(line)
        if m:
            out['experts_resident'], out['experts_total'] = int(m.group(1)), int(m.group(2))
            out['resident_pct'], out['placement_gib'], out['placement_s'] = float(m.group(3)), float(m.group(4)), float(m.group(5))
        m = RE_HOST_POOL.match(line)
        if m:
            out['host_pool'] = m.group(1).strip()
        m = RE_LISTEN.match(line)
        if m:
            out['listening'] = m.group(1)
            out['ready'] = True
        if line.startswith(STARTUP_KEEP):
            out['lines'].append(line)
    if drafted:
        out['mean_accept_pct'] = round(100.0 * accepted / drafted, 1)
    out['requests'] = out['requests'][-recent:]
    return out


# ─── Node state ───────────────────────────────────────────────────────

def _unit():
    u = AFFINITY_UNIT[:-8] if AFFINITY_UNIT.endswith('.service') else AFFINITY_UNIT
    return u if RE_SERVICE.match(u) else 'affinity-deepseek'


def _show(unit):
    out, _e, rc = run(['systemctl', 'show', unit + '.service', '-p', SHOW_PROPS])
    if rc != 0:
        return {}
    return parse_show(out)


def _conflict_states(units):
    """Each conflicting unit's presence / activity / boot enablement."""
    out = []
    for u in units:
        if not RE_SERVICE.match(u):
            continue
        s = _show(u)
        present = bool(s) and s.get('LoadState') == 'loaded'
        out.append({'unit': u, 'present': present,
                    'active': present and s.get('ActiveState') == 'active',
                    'enabled': present and s.get('UnitFileState') == 'enabled'})
    return out


def _container_logs(name, tail):
    if not RE_DK_NAME.match(name or ''):
        return ''
    try:
        status, raw = docker_raw('GET', '/containers/%s/logs?stdout=1&stderr=1&tail=%d' % (name, tail), timeout=20)
    except DockerError:
        return ''
    if status >= 400:
        return ''
    return _dk_demux_logs(raw).decode('utf-8', 'replace')


def _base_status(full=False):
    unit = _unit()
    st = {'unit': unit, 'installed': False, 'active': 'unknown', 'sub': '', 'enabled': None,
          'running': False, 'nrestarts': 0, 'since': '', 'pid': 0, 'fragment': '',
          'env': {}, 'args': {}, 'argv': [], 'port': DEFAULT_PORT, 'container': DEFAULT_NAME,
          'url': AFFINITY_URL or None, 'models': [], 'ready': False, 'conflicts': [],
          'warm_start_s': WARM_START_S}
    s = _show(unit)
    if not s or s.get('LoadState') != 'loaded' or not s.get('FragmentPath'):
        st['error'] = 'unit %s.service not found on this node' % unit
        return st
    st.update({'installed': True, 'active': s.get('ActiveState', ''), 'sub': s.get('SubState', ''),
               'enabled': s.get('UnitFileState') == 'enabled', 'running': s.get('ActiveState') == 'active',
               'nrestarts': int(_num(s.get('NRestarts')) or 0), 'since': s.get('ActiveEnterTimestamp', ''),
               'pid': int(_num(s.get('MainPID')) or 0), 'fragment': s.get('FragmentPath', '')})
    env = parse_env_string(s.get('Environment', ''))
    st['env'] = {k: v for k, v in env.items() if k not in ('HOME', 'PATH')}
    st['argv'] = parse_exec_argv(s.get('ExecStart', ''))
    st['args'] = parse_args(st['argv'])
    st['port'] = int(_num(st['args'].get('port')) or DEFAULT_PORT)
    st['container'] = env.get('NAME') or parse_exec_stop_name(s.get('ExecStop', '')) or DEFAULT_NAME
    st['url'] = AFFINITY_URL or 'http://127.0.0.1:%d' % st['port']
    st['gpus'] = env.get('GPUS')
    st['conflict_units'] = parse_conflicts(s.get('Conflicts', ''))
    if st['running']:
        text, e = _http_get(st['url'], '/v1/models', 3)
        st['models'] = parse_models(text) if text else []
        st['models_error'] = e
        st['ready'] = bool(st['models'])
        st['startup'] = startup_summary(_container_logs(st['container'], LOG_TAIL))
    if full:
        st['conflicts'] = _conflict_states(st['conflict_units'])
        st['named_args'] = [{'flag': f, 'label': lbl, 'value': st['args'].get(f[2:])}
                            for f, lbl in NAMED_ARGS if f[2:] in st['args']]
        named = {f[2:] for f, _l in NAMED_ARGS}
        st['other_args'] = {k: v for k, v in st['args'].items() if k not in named}
    return st


# ─── Routes ───────────────────────────────────────────────────────────

@bp.route('/api/affinity')
def affinity_status():
    return jsonify(_base_status(full=True))


@bp.route('/api/affinity/action', methods=['POST'])
def affinity_action():
    data = request.get_json(silent=True) or {}
    action = data.get('action', '')
    if action not in UNIT_ACTIONS:
        return err('Unknown action')
    unit = _unit()
    s = _show(unit)
    if not s or s.get('LoadState') != 'loaded':
        return err('unit %s.service not found on this node' % unit, 404)
    stopped = []
    if action in ('start', 'restart'):
        stopped = [c['unit'] for c in _conflict_states(parse_conflicts(s.get('Conflicts', ''))) if c['active']]
    res = dict(run_safe(['systemctl', action, unit + '.service']))
    res['action'] = action
    if stopped and res.get('success'):
        res['note'] = '%s was stopped (Conflicts=) — the cards now belong to affinity' % ', '.join(stopped)
    if action in ('start', 'restart') and res.get('success'):
        res['warm_start_s'] = WARM_START_S
    return jsonify(res)


@bp.route('/api/affinity/logs')
def affinity_logs():
    source = request.args.get('source', 'container')
    tail = max(1, min(int(_num(request.args.get('tail')) or 200), 5000))
    st = _base_status()
    if not st['installed']:
        return err(st.get('error') or 'unit not found', 404)
    if source == 'journal':
        out, e, rc = run(['journalctl', '-u', st['unit'] + '.service', '--no-pager', '-n', str(tail),
                          '--output=short-iso'])
        return jsonify({'source': 'journal', 'name': st['unit'] + '.service',
                        'logs': out if rc == 0 else (e or 'journalctl failed')})
    if source == 'container':
        return jsonify({'source': 'container', 'name': st['container'],
                        'logs': _container_logs(st['container'], tail) or '(no container log — not running, or the docker socket is not readable)'})
    return err('source must be journal or container')


# ─── Hooks (summary / alerts / history / metrics) ─────────────────────

def _affinity_summary():
    """Additive /api/summary block `affinity`: unit state, /v1/models
    readiness and the engine's own last figures from its log. None (block
    absent) when the unit is not on this node. `model` + `state` + `ready`
    are what the fleet console folds into its inference chip."""
    st = _base_status()
    if not st['installed']:
        return None
    su = st.get('startup') or {}
    models = st.get('models') or []
    return {
        'installed': True, 'unit': st['unit'], 'state': st['active'], 'sub': st['sub'],
        'running': st['running'], 'enabled': st['enabled'], 'nrestarts': st['nrestarts'],
        'ready': st['ready'], 'port': st['port'], 'gpus': st.get('gpus'), 'tp': su.get('tp'),
        'model': models[0]['id'] if models else None,
        'experts_resident': su.get('experts_resident'), 'experts_total': su.get('experts_total'),
        'resident_pct': su.get('resident_pct'), 'host_pool': su.get('host_pool'),
        'kv_backed': su.get('kv_backed'),
        'tps': su.get('last_tps'), 'draft_accept_pct': su.get('mean_accept_pct'),
        'requests_recent': su.get('requests_total') or 0, 'errors': su.get('errors') or 0,
    }


def _affinity_alerts():
    st = _base_status()
    if not st['installed']:
        return []
    out = []
    unit = st['unit']
    if st['enabled'] and not st['running']:
        out.append({'key': 'affinity_down',
                    'message': 'Affinity (%s) is enabled at boot but %s' % (unit, st['active'] or 'not running')})
    if st['nrestarts'] > 0:
        out.append({'key': 'affinity_restarting',
                    'message': 'Affinity (%s) has been restarted by systemd %d time(s) since boot — check its log'
                               % (unit, st['nrestarts'])})
    su = st.get('startup') or {}
    if su.get('errors'):
        out.append({'key': 'affinity_errors',
                    'message': 'Affinity logged %d error line(s) since its last start — open the container log'
                               % su['errors']})
    if st['enabled']:
        for c in _conflict_states(st.get('conflict_units') or []):
            if c['enabled']:
                out.append({'key': 'affinity_conflict:' + c['unit'],
                            'message': 'Both %s and %s are enabled at boot; they conflict (same GPUs) — '
                                       'whichever starts last stops the other' % (unit, c['unit'])})
    return out


AFFINITY_HISTORY_METRICS = {'affinity_tps', 'affinity_accept'}


def _affinity_history():
    """The last request's decode speed and the run's mean draft acceptance,
    label = the unit. Nothing when the engine is not serving."""
    st = _base_status()
    su = st.get('startup')
    if not st['running'] or not su:
        return []
    label = st['unit']
    return [('affinity_tps', label, su.get('last_tps')),
            ('affinity_accept', label, su.get('mean_accept_pct'))]


def _affinity_metrics():
    st = _base_status()
    if not st['installed']:
        return []
    lines = ['# HELP storagedash_affinity_up Affinity unit active (1) or not (0)',
             '# TYPE storagedash_affinity_up gauge',
             'storagedash_affinity_up %d' % (1 if st['running'] else 0),
             '# HELP storagedash_affinity_ready Affinity answering /v1/models (1) or not (0)',
             '# TYPE storagedash_affinity_ready gauge',
             'storagedash_affinity_ready %d' % (1 if st['ready'] else 0)]
    su = st.get('startup') or {}
    gauges = [('last_decode_tps', 'Decode tokens/s of the most recent request', su.get('last_tps')),
              ('draft_accept_pct', 'Draft tokens accepted / drafted since start, percent', su.get('mean_accept_pct')),
              ('experts_resident_pct', 'Routed experts resident in VRAM, percent', su.get('resident_pct')),
              ('tokens_generated_recent', 'Tokens generated in the requests still in the log tail', su.get('tokens_total'))]
    for name, help_, val in gauges:
        if val is None:
            continue
        n = 'storagedash_affinity_' + name
        lines += ['# HELP %s %s' % (n, help_), '# TYPE %s gauge' % n, '%s %s' % (n, val)]
    return lines


MODULE = {'id': 'affinity', 'order': 124, 'label': 'Affinity', 'category': 'AI Tools',
          'default_enabled': False,
          'nav': {'cat': 'ai', 'cat_order': 40, 'pages': [
                  {'id': 'affinity', 'label': 'Affinity', 'icon': 'box3'}]},
          'blueprint': bp,
          'summary': _affinity_summary,
          'alerts': _affinity_alerts,
          'history': _affinity_history,
          'history_metrics': AFFINITY_HISTORY_METRICS,
          'metrics': _affinity_metrics}
