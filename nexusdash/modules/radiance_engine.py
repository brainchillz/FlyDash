"""Radiance — StillDeadcode's standalone Radiance inference engine (AI Tools >
Radiance; module id `radiance_engine`).

Not the `radiance` module. What the fleet has called "Radiance" since 3.9.0
is `stilldeadcode/vllm-radiance`, a vLLM fork whose boot parameters are
`Environment=` knobs on a `serve-tpN.sh` launcher. THIS is the author's
later standalone C++/HIP engine, image `stilldeadcode/radiance`, which takes
its configuration as command-line flags, speaks `/stats` + `/health` +
`/server_info` of its own beside the OpenAI routes and a vLLM-compatible
`/metrics`, and prints a per-card VRAM budget and one line per finished
request to its log. Same deployment shape as affinity: ONE systemd unit
(`radiance-flashnext.service` by default, `DASHBOARD_RADIANCE_ENGINE_UNIT`
overrides) whose ExecStart is a launcher that `docker run`s the engine in
the foreground with the engine's flags on the ExecStart line and the
launcher's GPUS / NAME / IMAGE as `Environment=`; `Conflicts=` on whatever
else wants the cards.

What the page shows, and where it comes from:

- unit state via `systemctl show -p …`; the flags from ExecStart (read-only
  — they are argv, the affinity precedent: no editor in this cut);
- readiness = `/v1/models` answering with a model id;
- live figures from the engine's own `/stats` (what its dashboard draws
  from): decode / prefill tok/s, the decode step time (best and recent, the
  number to compare runs by — tok/s moves with draft acceptance), draft
  acceptance, KV utilisation, running / waiting, per-card VRAM;
- the boot receipt from the container log, anchored on the LAST
  `I device: AQL backend` line so a restarted container never mixes two
  runs: peer-to-peer confirmed or not, the per-card budget (experts / KV /
  static GiB), the n-gram table placement, headroom warnings, the recent
  finished requests (`I req … done … N tok/s`), error and warning counts.
  Never recompute what the engine already printed (halogen precedent).

Control: start / stop / restart / enable / disable through the blanket
`systemctl` grant. Default-off: dead weight on a node without the unit, and
the page says "not installed" instead of erroring.
"""
import json
import os
import re

from flask import Blueprint, jsonify, request

from ..core.runcmd import run, run_safe, err, _num
from ..core.validators import RE_SERVICE
from .docker import DockerError, docker_raw, _dk_demux_logs, RE_DK_NAME
from .radiance import parse_show, parse_env_string, parse_exec_stop_name, parse_models, _http_get
from .affinity import parse_exec_argv, parse_args, parse_conflicts

bp = Blueprint('radiance_engine', __name__)

ENGINE_UNIT = os.environ.get('DASHBOARD_RADIANCE_ENGINE_UNIT', 'radiance-flashnext')
ENGINE_URL = os.environ.get('DASHBOARD_RADIANCE_ENGINE_URL', '')        # '' = http://127.0.0.1:<--port>
DEFAULT_PORT = 8300
DEFAULT_NAME = 'radiance-tp4'            # run.sh's NAME default
LOG_TAIL = 600                           # the boot receipt is ~380 lines; the rest is recent requests
WARM_START_S = 120                       # ~60-90 s to /v1/models with the container in page cache
RECENT_REQUESTS = 20

SHOW_PROPS = ('Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath,DropInPaths,'
              'NRestarts,ActiveEnterTimestamp,MainPID,Environment,ExecStart,ExecStop,Conflicts')
UNIT_ACTIONS = ('start', 'stop', 'restart', 'enable', 'disable')

# Flags worth naming on the page, from the ExecStart argv. Everything else
# the engine was given is listed verbatim under "other arguments".
NAMED_ARGS = (('--model', 'Model container'),
              ('--tp', 'Tensor-parallel ranks'),
              ('--tp-wire', 'All-reduce wire'),
              ('--p2p', 'Peer to peer'),
              ('--placement', 'Weight placement'),
              ('--ngram-placement', 'n-gram table'),
              ('--host-pool-mib', 'Pinned host pool (MiB)'),
              ('--gpu-headroom-mib', 'VRAM headroom (MiB)'),
              ('--max-model-len', 'Context (tokens)'),
              ('--max-num-seqs', 'Sequences per step'),
              ('--max-num-batched-tokens', 'Tokens per step'),
              ('--kv-cache-dtype', 'KV cache dtype'),
              ('--num-speculative-tokens', 'Speculative tokens'),
              ('--reasoning-effort-map', 'Reasoning effort map'),
              ('--served-model-name', 'Served model id'),
              ('--host', 'Listen address'),
              ('--port', 'Listen port'))


# ─── Pure parsers ─────────────────────────────────────────────────────

RE_START_MARK = re.compile(r'^I device: AQL backend, (\d+) device\(s\)')
RE_MODEL = re.compile(r'^I model: (\S+)\s+arch=(\S+)\s+quant=(\S+)\s+(\d+) layers\s+(\d+) ctx')
RE_P2P_ON = re.compile(r'^I device: the (\d+) cards reach each other\'s memory directly \(peer to peer\)')
RE_P2P_OFF = re.compile(r'^[IW] device: .*\b(?:host memory|no peer|without peer|peer access)\b', re.I)
RE_WIRE_LOSSY = re.compile(r'^W tp wire: LOSSY \((--tp-wire \S+)\)')
RE_SPEC = re.compile(r"^I speculation: '(\w+)' drafts (\d+) token\(s\) a step")
RE_NGRAM = re.compile(r'^I placement: n-gram table \(([\d.]+) GiB\) (\w+)')
RE_KV_POOL = re.compile(r'^I kv: the pool backs (\d+)% of the worst case \((\d+) sequences all at (\d+) tokens\), ([\d.]+) full-length sessions')
RE_BUDGET = re.compile(r'^\s+(card total|static weights|experts|kv cache|elastic|vram_weights|vram_kv)\s+([\d.]+) GiB')
RE_SETTLED = re.compile(r'^([IW])\[(\d+)\] card (\d+) settled at ([\d.]+) MiB free')
RE_SERVING = re.compile(r'^I serving on (http://\S+)')
RE_REQ = re.compile(r'^I req (\S+) done from (\S+) finish=(\S+) prompt=(\d+) \(cache (\d+)\) gen=(\d+)(?: reason=(\d+))?'
                    r' ttft=([\d.]+)s ([\d.]+) tok/s ([\d.]+)s')
RE_ERROR = re.compile(r'^E(?:\[\d+\])? ')
RE_WARN = re.compile(r'^W(?:\[\d+\])? ')
STARTUP_KEEP = ('I device', 'I model', 'W tp wire', 'I speculation', 'I placement', 'I kv', 'I serving', 'I server:',
                'W[', 'E ', 'E[')


def _int(s):
    try:
        return int(str(s).replace(',', ''))
    except (TypeError, ValueError):
        return None


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def startup_summary(log_text, recent=RECENT_REQUESTS):
    """The engine's boot receipt and recent finished requests from its log,
    anchored on the LAST start marker. Pure.
    {lines, devices, arch, quant, layers, ctx, p2p, wire_lossy, drafter,
     draft_tokens, ngram_gib, ngram_where, kv_backing_pct, kv_sessions,
     budget:{card total, static weights, experts, kv cache, elastic,
     vram_weights, vram_kv} (GiB per card), settled:[{card, free_mib, warned}],
     headroom_warnings, listening, ready, errors, warnings, requests:[…],
     requests_total, tokens_total, last_tps}"""
    lines = (log_text or '').splitlines()
    start = 0
    for i, line in enumerate(lines):
        if RE_START_MARK.match(line):
            start = i
    out = {'lines': [], 'devices': None, 'arch': None, 'quant': None, 'layers': None, 'ctx': None,
           'p2p': None, 'wire_lossy': False, 'drafter': None, 'draft_tokens': None,
           'ngram_gib': None, 'ngram_where': None, 'kv_backing_pct': None, 'kv_sessions': None,
           'budget': {}, 'settled': [], 'headroom_warnings': 0, 'listening': None, 'ready': False,
           'errors': 0, 'warnings': 0, 'requests': [], 'requests_total': 0, 'tokens_total': 0, 'last_tps': None}
    budget_block = False
    for raw in lines[start:]:
        line = raw.rstrip()
        if not line:
            continue
        m = RE_REQ.match(line)
        if m:
            req = {'id': m.group(1), 'client': m.group(2), 'finish': m.group(3), 'prompt': int(m.group(4)),
                   'cached': int(m.group(5)), 'gen': int(m.group(6)), 'reasoning': _int(m.group(7)),
                   'ttft_s': float(m.group(8)), 'tps': float(m.group(9)), 'seconds': float(m.group(10))}
            out['requests'].append(req)
            out['requests_total'] += 1
            out['tokens_total'] += req['gen']
            out['last_tps'] = req['tps']
            continue
        m = RE_START_MARK.match(line)
        if m:
            out['devices'] = int(m.group(1))
        m = RE_MODEL.match(line)
        if m:
            out['arch'], out['quant'] = m.group(2), m.group(3)
            out['layers'], out['ctx'] = int(m.group(4)), int(m.group(5))
        m = RE_P2P_ON.match(line)
        if m:
            out['p2p'] = True
        elif out['p2p'] is None and RE_P2P_OFF.match(line):
            out['p2p'] = False
        m = RE_WIRE_LOSSY.match(line)
        if m:
            out['wire_lossy'] = True
        m = RE_SPEC.match(line)
        if m:
            out['drafter'], out['draft_tokens'] = m.group(1), int(m.group(2))
        m = RE_NGRAM.match(line)
        if m:
            out['ngram_gib'], out['ngram_where'] = float(m.group(1)), m.group(2)
        m = RE_KV_POOL.match(line)
        if m:
            out['kv_backing_pct'], out['kv_sessions'] = int(m.group(1)), float(m.group(4))
        if line.startswith('I VRAM budget, per card'):
            budget_block = True
            out['lines'].append(line)
            continue
        if budget_block:
            m = RE_BUDGET.match(line)
            if m and m.group(1) not in out['budget']:
                out['budget'][m.group(1)] = float(m.group(2))
                out['lines'].append(line)
            if not line.startswith(' '):
                budget_block = False
        m = RE_SETTLED.match(line)
        if m:
            warned = m.group(1) == 'W'
            out['settled'].append({'card': int(m.group(3)), 'free_mib': float(m.group(4)), 'warned': warned})
            if warned:
                out['headroom_warnings'] += 1
            out['lines'].append(line)
            continue
        m = RE_SERVING.match(line)
        if m:
            out['listening'] = m.group(1)
            out['ready'] = True
        if RE_ERROR.match(line):
            out['errors'] += 1
        elif RE_WARN.match(line):
            out['warnings'] += 1
        if line.startswith(STARTUP_KEEP):
            out['lines'].append(line)
    out['requests'] = out['requests'][-recent:]
    return out


def _ns_ms(ns):
    ns = _num(ns)
    return round(ns / 1e6, 2) if ns else None


def parse_stats(text):
    """The engine's `/stats` → the figures the page and the hooks use. {} on
    anything that is not its JSON. Pure."""
    try:
        d = json.loads(text or '')
    except ValueError:
        return {}
    if not isinstance(d, dict) or 'version' not in d:
        return {}
    kv_util = _float(d.get('kv_util'))
    accept = _float(d.get('draft_accept'))
    cards = []
    for c in d.get('cards') or []:
        if isinstance(c, dict):
            used, total = _num(c.get('vram_used')), _num(c.get('vram_total'))
            cards.append({'index': c.get('index'), 'name': c.get('name'),
                          'vram_used_gib': round(used / 2 ** 30, 2) if used is not None else None,
                          'vram_total_gib': round(total / 2 ** 30, 2) if total is not None else None})
    positions = [round(100.0 * r, 1) for r in (_float(p.get('rate')) for p in (d.get('draft_positions') or [])
                                                if isinstance(p, dict)) if r is not None]
    link = d.get('link') if isinstance(d.get('link'), dict) else {}
    return {
        'model': d.get('model'), 'version': d.get('version'),
        'uptime_s': _float(d.get('uptime_s')), 'startup_s': _float(d.get('startup_s')),
        'running': _int(d.get('running')), 'waiting': _int(d.get('waiting')), 'in_flight': _int(d.get('in_flight')),
        'requests_total': _int(d.get('total_requests')), 'preemptions': _int(d.get('preemptions')),
        'kv_pct': round(kv_util * 100, 2) if kv_util is not None else None,
        'kv_tokens_used': _int(d.get('kv_tokens_used')), 'kv_tokens_total': _int(d.get('kv_tokens_total')),
        'decode_tps': _float(d.get('decode_tps')), 'prefill_tps': _float(d.get('prefill_tps')),
        'draft_accept_pct': round(accept * 100, 1) if accept is not None else None,
        'draft_positions_pct': positions,
        'step_ms_best': _ns_ms(d.get('step_ns_decode_best')), 'step_ms_recent': _ns_ms(d.get('step_ns_decode_recent')),
        'steps': _int(d.get('steps')), 'steps_decode': _int(d.get('steps_decode')),
        'world': _int(link.get('world')), 'cards': cards,
    }


# ─── Node state ───────────────────────────────────────────────────────

def _unit():
    u = ENGINE_UNIT[:-8] if ENGINE_UNIT.endswith('.service') else ENGINE_UNIT
    return u if RE_SERVICE.match(u) else 'radiance-flashnext'


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
          'url': ENGINE_URL or None, 'models': [], 'ready': False, 'conflicts': [],
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
    st['tp'] = _int(st['args'].get('tp'))
    st['container'] = env.get('NAME') or parse_exec_stop_name(s.get('ExecStop', '')) or DEFAULT_NAME
    st['url'] = ENGINE_URL or 'http://127.0.0.1:%d' % st['port']
    st['gpus'] = env.get('GPUS')
    st['image'] = env.get('IMAGE')
    st['conflict_units'] = parse_conflicts(s.get('Conflicts', ''))
    if st['running']:
        text, e = _http_get(st['url'], '/v1/models', 3)
        st['models'] = parse_models(text) if text else []
        st['models_error'] = e
        st['ready'] = bool(st['models'])
        if st['ready']:
            text, _e = _http_get(st['url'], '/stats', 3)
            st['stats'] = parse_stats(text) if text else {}
        st['startup'] = startup_summary(_container_logs(st['container'], LOG_TAIL))
    if full:
        st['conflicts'] = _conflict_states(st['conflict_units'])
        st['named_args'] = [{'flag': f, 'label': lbl, 'value': st['args'].get(f[2:])}
                            for f, lbl in NAMED_ARGS if f[2:] in st['args']]
        named = {f[2:] for f, _l in NAMED_ARGS}
        st['other_args'] = {k: v for k, v in st['args'].items() if k not in named}
    return st


# ─── Routes ───────────────────────────────────────────────────────────

@bp.route('/api/radiance_engine')
def radiance_engine_status():
    return jsonify(_base_status(full=True))


@bp.route('/api/radiance_engine/action', methods=['POST'])
def radiance_engine_action():
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
        res['note'] = '%s was stopped (Conflicts=) — the cards now belong to Radiance' % ', '.join(stopped)
    if action in ('start', 'restart') and res.get('success'):
        res['warm_start_s'] = WARM_START_S
    return jsonify(res)


@bp.route('/api/radiance_engine/logs')
def radiance_engine_logs():
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

def _engine_summary():
    """Additive /api/summary block `radiance_engine`: unit state, /v1/models
    readiness and the engine's own `/stats` figures. None (block absent)
    when the unit is not on this node. `model` + `state` + `ready` are what
    the fleet console folds into its inference chip."""
    st = _base_status()
    if not st['installed']:
        return None
    m = st.get('stats') or {}
    su = st.get('startup') or {}
    models = st.get('models') or []
    return {
        'installed': True, 'unit': st['unit'], 'state': st['active'], 'sub': st['sub'],
        'running': st['running'], 'enabled': st['enabled'], 'nrestarts': st['nrestarts'],
        'ready': st['ready'], 'port': st['port'], 'gpus': st.get('gpus'), 'tp': st.get('tp'),
        'model': models[0]['id'] if models else None, 'version': m.get('version'),
        'decode_tps': m.get('decode_tps'), 'prefill_tps': m.get('prefill_tps'),
        'step_ms': m.get('step_ms_recent'), 'step_ms_best': m.get('step_ms_best'),
        'draft_accept_pct': m.get('draft_accept_pct'), 'kv_pct': m.get('kv_pct'),
        'requests_running': m.get('running'), 'requests_waiting': m.get('waiting'),
        'requests_total': m.get('requests_total'), 'uptime_s': m.get('uptime_s'),
        'p2p': su.get('p2p'), 'errors': su.get('errors') or 0,
    }


def _engine_alerts():
    st = _base_status()
    if not st['installed']:
        return []
    out = []
    unit = st['unit']
    if st['enabled'] and not st['running']:
        out.append({'key': 'radiance_engine_down',
                    'message': 'Radiance (%s) is enabled at boot but %s' % (unit, st['active'] or 'not running')})
    if st['nrestarts'] > 0:
        out.append({'key': 'radiance_engine_restarting',
                    'message': 'Radiance (%s) has been restarted by systemd %d time(s) since boot — check its log'
                               % (unit, st['nrestarts'])})
    su = st.get('startup') or {}
    if su.get('errors'):
        out.append({'key': 'radiance_engine_errors',
                    'message': 'Radiance logged %d error line(s) since its last start — open the container log'
                               % su['errors']})
    if st['running'] and su.get('p2p') is False:
        out.append({'key': 'radiance_engine_p2p',
                    'message': 'Radiance is exchanging through host memory, not peer to peer — the cards do not '
                               'reach each other directly (check CONFIG_PCI_P2PDMA, Resizable BAR, IOMMU)'})
    if su.get('headroom_warnings'):
        out.append({'key': 'radiance_engine_headroom',
                    'message': 'Radiance warned on %d card(s) that less VRAM headroom was left than it allows for '
                               'first launches — raise --gpu-headroom-mib' % su['headroom_warnings']})
    if st['enabled']:
        for c in _conflict_states(st.get('conflict_units') or []):
            if c['enabled']:
                out.append({'key': 'radiance_engine_conflict:' + c['unit'],
                            'message': 'Both %s and %s are enabled at boot; they conflict (same GPUs) — '
                                       'whichever starts last stops the other' % (unit, c['unit'])})
    return out


ENGINE_HISTORY_METRICS = {'radiance_engine_tps', 'radiance_engine_step_ms', 'radiance_engine_accept',
                          'radiance_engine_kv_pct'}


def _engine_history():
    """The engine's own decode tok/s, recent decode step time, draft
    acceptance and KV utilisation from `/stats`, label = the unit. Nothing
    when the engine is not serving."""
    st = _base_status()
    m = st.get('stats')
    if not st['ready'] or not m:
        return []
    label = st['unit']
    return [('radiance_engine_tps', label, m.get('decode_tps')),
            ('radiance_engine_step_ms', label, m.get('step_ms_recent')),
            ('radiance_engine_accept', label, m.get('draft_accept_pct')),
            ('radiance_engine_kv_pct', label, m.get('kv_pct'))]


def _engine_metrics():
    st = _base_status()
    if not st['installed']:
        return []
    lines = ['# HELP storagedash_radiance_engine_up Radiance (standalone engine) unit active (1) or not (0)',
             '# TYPE storagedash_radiance_engine_up gauge',
             'storagedash_radiance_engine_up %d' % (1 if st['running'] else 0),
             '# HELP storagedash_radiance_engine_ready Radiance (standalone engine) answering /v1/models (1) or not (0)',
             '# TYPE storagedash_radiance_engine_ready gauge',
             'storagedash_radiance_engine_ready %d' % (1 if st['ready'] else 0)]
    m = st.get('stats') or {}
    gauges = [('decode_tps', 'Decode tokens/s, the engine\'s own recent figure', m.get('decode_tps')),
              ('prefill_tps', 'Prefill tokens/s, the engine\'s own recent figure', m.get('prefill_tps')),
              ('step_ms', 'Recent decode step time, milliseconds', m.get('step_ms_recent')),
              ('step_ms_best', 'Best decode step time since start, milliseconds', m.get('step_ms_best')),
              ('draft_accept_pct', 'Draft tokens accepted / drafted since start, percent', m.get('draft_accept_pct')),
              ('kv_pct', 'KV cache in use, percent', m.get('kv_pct')),
              ('requests_running', 'Sequences in the current step', m.get('running')),
              ('requests_waiting', 'Requests admitted and waiting', m.get('waiting'))]
    for name, help_, val in gauges:
        if val is None:
            continue
        n = 'storagedash_radiance_engine_' + name
        lines += ['# HELP %s %s' % (n, help_), '# TYPE %s gauge' % n, '%s %s' % (n, val)]
    return lines


# Labelled plainly "Radiance" (the vLLM fork's module is now "vLLM Radiance" in the UI); the id stays
# `radiance_engine` because `radiance` is the fork's id everywhere the contract reaches.
MODULE = {'id': 'radiance_engine', 'order': 125, 'label': 'Radiance', 'category': 'AI Tools',
          'default_enabled': False,
          'nav': {'cat': 'ai', 'cat_order': 40, 'pages': [
                  {'id': 'radiance_engine', 'label': 'Radiance', 'icon': 'flame'}]},
          'blueprint': bp,
          'summary': _engine_summary,
          'alerts': _engine_alerts,
          'history': _engine_history,
          'history_metrics': ENGINE_HISTORY_METRICS,
          'metrics': _engine_metrics}
