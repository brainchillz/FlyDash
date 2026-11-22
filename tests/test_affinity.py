"""Affinity module (3.10.0): the affinity engine's systemd unit — `systemctl
show` / ExecStart argv parsing, /v1/models readiness, the engine's own
startup + per-request lines from the container log, unit actions, logs,
and the summary/alerts/history/metrics hooks.

Fixtures are the REAL unit on node7 (2026-10-16, 4x R9700,
DeepSeek-V4-Flash-0731 2.875bpw, every expert resident): `systemctl show`,
the unit file, /v1/models and the container log after one request.
systemctl, HTTP and the docker socket are faked at the module boundary.
"""
import os
import pytest

import app
from nexusdash.modules import affinity as af

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'affinity')


def _read(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


SHOW = _read('systemctl-show.txt')
UNIT = _read('affinity-deepseek.service')
MODELS = _read('models.json')
LOG = _read('container-log.txt')
QWEN_SHOW = 'Id=vllm-qwen.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n'
LLAMA_SHOW = 'Id=llama-server.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n'


def _show_text(**over):
    lines = []
    for line in SHOW.splitlines():
        k = line.split('=', 1)[0]
        if k in over:
            lines.append('%s=%s' % (k, over.pop(k)))
        else:
            lines.append(line)
    for k, v in over.items():
        lines.append('%s=%s' % (k, v))
    return '\n'.join(lines) + '\n'


class Fake:
    def __init__(self, show=SHOW, qwen=QWEN_SHOW, llama=LLAMA_SHOW, models=MODELS, log=LOG):
        self.show, self.qwen, self.llama, self.models, self.log = show, qwen, llama, models, log
        self.calls = []

    def run(self, args, input_data=None, no_sudo=False, timeout=120):
        self.calls.append(list(args))
        if args[:2] == ['systemctl', 'show']:
            if args[2] == 'affinity-deepseek.service':
                return (self.show, '', 0) if self.show else ('', 'not found', 1)
            if args[2] == 'vllm-qwen.service':
                return (self.qwen, '', 0) if self.qwen else ('', 'not found', 1)
            if args[2] == 'llama-server.service':
                return (self.llama, '', 0) if self.llama else ('', 'not found', 1)
        if args[0] == 'journalctl':
            return '2026-10-16T01:58:55+0000 node7 run.sh[1]: [build] affinity …\n', '', 0
        raise AssertionError('unexpected run %r' % (args,))

    def run_safe(self, args, input_data=None):
        self.calls.append(list(args))
        return {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0}

    def http(self, base, path, timeout):
        assert base == 'http://127.0.0.1:8095', base
        assert path == '/v1/models', path
        return (self.models, None) if self.models else (None, 'connection refused')

    def docker_raw(self, method, path, body=None, timeout=60):
        assert path.startswith('/containers/affinity-4/logs?'), path
        payload = self.log.encode()
        frame = b'\x02\x00\x00\x00' + len(payload).to_bytes(4, 'big') + payload
        return 200, frame


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(af, 'run', f.run)
    monkeypatch.setattr(af, 'run_safe', f.run_safe)
    monkeypatch.setattr(af, '_http_get', f.http)
    monkeypatch.setattr(af, 'docker_raw', f.docker_raw)
    monkeypatch.setattr(af, 'AFFINITY_URL', '')
    app.app.config['TESTING'] = True
    f.client = app.app.test_client()
    return f


# ─── Pure parsers against the real captures ───────────────────────────

def test_show_capture_parses():
    s = af.parse_show(SHOW)
    assert s['ActiveState'] == 'active' and s['UnitFileState'] == 'enabled' and s['NRestarts'] == '0'
    env = af.parse_env_string(s['Environment'])
    assert env == {'HOME': '/home/backupuser', 'GPUS': '4', 'NAME': 'affinity-4', 'AFF_ALLOW_NRANK': '1'}
    argv = af.parse_exec_argv(s['ExecStart'])
    assert argv == ['--placement-strategy', 'static', '--dspark', 'on', '--kv-size', '262144',
                    '--kv-commit', '262144', '--host', '0.0.0.0', '--port', '8095']
    assert af.parse_args(argv) == {'placement-strategy': 'static', 'dspark': 'on', 'kv-size': '262144',
                                   'kv-commit': '262144', 'host': '0.0.0.0', 'port': '8095'}
    assert af.parse_exec_stop_name(s['ExecStop']) == 'affinity-4'
    # shutdown.target is systemd's own, not a competitor for the cards
    assert af.parse_conflicts(s['Conflicts']) == ['llama-server', 'vllm-qwen']


def test_parse_args_edge_cases():
    assert af.parse_args([]) == {}
    assert af.parse_args(['--no-echo', '--port', '8095', '--verbose']) == {'no-echo': 'on', 'port': '8095', 'verbose': 'on'}
    assert af.parse_exec_argv('') == [] and af.parse_exec_argv('garbage') == []
    # a lone launcher path with no arguments
    assert af.parse_exec_argv('{ path=/x/run.sh ; argv[]=/x/run.sh ; ignore_errors=no }') == []


def test_unit_file_capture_matches_show():
    assert 'Conflicts=vllm-qwen.service llama-server.service' in UNIT
    assert 'AFF_ALLOW_NRANK=1' in UNIT and '--placement-strategy static' in UNIT


def test_startup_summary_from_real_log():
    su = af.startup_summary(LOG)
    assert su['build'].startswith('affinity') and su['tp'] == 4
    assert su['layers'] == 43 and su['experts'] == 256 and su['top_k'] == 6
    assert su['dspark'] == '3 stages, block 5'
    assert su['kv_reserved'] == 262144 and su['kv_backed'] == 262144 and su['kv_gib'] == 2.82
    assert su['experts_resident'] == 11008 and su['experts_total'] == 11008 and su['resident_pct'] == 100.0
    assert su['placement_gib'] == 97.6 and su['placement_s'] == 42.2
    assert su['host_pool'].startswith('disabled (nothing to pool)')
    assert su['listening'] == 'http://0.0.0.0:8095' and su['ready'] is True
    assert su['errors'] == 0
    # one finished request in the capture
    assert su['requests_total'] == 1 and su['tokens_total'] == 50 and su['last_tps'] == 71.1
    r = su['requests'][0]
    assert r == {'slot': 0, 'tokens': 50, 'seconds': 0.70, 'tps': 71.1, 'blocks': 19, 'drafted': 95,
                 'accepted': 32, 'accept_pct': 33.7, 'tok_per_block': 2.63}
    assert su['mean_accept_pct'] == 33.7
    # the kept startup lines are the engine's, in order, without the VRAM table rows
    assert su['lines'][0].startswith('[build] affinity') and 'listening on http://0.0.0.0:8095' in su['lines'][-1]
    assert not any(l.startswith('  ') for l in su['lines'])


def test_startup_summary_anchors_on_last_start_and_sums_requests():
    two_runs = LOG + '\n' + LOG.replace('11008/11008 experts resident (100.0%)', '4953/11008 experts resident (45.0%)') \
        + 'slot 0: 100 tok in 2.00s (50.0 t/s), 40 blocks, 200 drafted, 100 accepted (50.0%, 2.50 tok/block)\n' \
        + 'error: something\n'
    su = af.startup_summary(two_runs)
    assert su['experts_resident'] == 4953 and su['resident_pct'] == 45.0       # the second run only
    assert su['requests_total'] == 2 and su['tokens_total'] == 150 and su['last_tps'] == 50.0
    assert su['mean_accept_pct'] == round(100.0 * (32 + 100) / (95 + 200), 1)
    assert su['errors'] == 1
    assert af.startup_summary('')['ready'] is False and af.startup_summary(None)['requests'] == []


def test_startup_summary_loading_engine():
    cut = LOG.split('listening on')[0]
    su = af.startup_summary(cut)
    assert su['ready'] is False and su['listening'] is None and su['experts_resident'] == 11008


# ─── Status / routes ──────────────────────────────────────────────────

def test_status_page_document(fake):
    r = fake.client.get('/api/affinity')
    assert r.status_code == 200
    st = r.get_json()
    assert st['installed'] and st['running'] and st['ready'] and st['enabled'] and st['unit'] == 'affinity-deepseek'
    assert st['port'] == 8095 and st['container'] == 'affinity-4' and st['gpus'] == '4'
    assert st['env'] == {'GPUS': '4', 'NAME': 'affinity-4', 'AFF_ALLOW_NRANK': '1'}   # HOME hidden
    assert st['models'] == [{'id': 'DeepSeek-V4-Flash', 'root': None, 'max_model_len': None}]
    assert st['startup']['last_tps'] == 71.1
    assert [a['flag'] for a in st['named_args']] == ['--placement-strategy', '--dspark', '--kv-size', '--kv-commit', '--host', '--port']
    assert st['other_args'] == {}
    assert [(c['unit'], c['present'], c['active'], c['enabled']) for c in st['conflicts']] == \
        [('llama-server', True, False, False), ('vllm-qwen', True, False, False)]


def test_status_unit_missing(fake):
    fake.show = ''
    st = fake.client.get('/api/affinity').get_json()
    assert st['installed'] is False and 'not found' in st['error']
    assert af._affinity_summary() is None and af._affinity_alerts() == [] and af._affinity_metrics() == []
    r = fake.client.post('/api/affinity/action', json={'action': 'start'})
    assert r.status_code == 404


def test_status_inactive_skips_http_and_log(fake):
    fake.show = _show_text(ActiveState='inactive', SubState='dead', MainPID='0')
    st = af._base_status()
    assert st['installed'] and not st['running'] and not st['ready'] and st['models'] == [] and 'startup' not in st
    assert all(c[0] == 'systemctl' for c in fake.calls)


def test_status_engine_loading(fake):
    fake.models = None
    st = af._base_status()
    assert st['running'] and st['ready'] is False and st['models_error'] == 'connection refused'


def test_actions(fake):
    r = fake.client.post('/api/affinity/action', json={'action': 'restart'}).get_json()
    assert r['success'] and r['action'] == 'restart' and r['warm_start_s'] == af.WARM_START_S
    assert ['systemctl', 'restart', 'affinity-deepseek.service'] in fake.calls
    assert 'note' not in r                                   # nothing conflicting was running
    # a running vllm-qwen gets stopped by Conflicts= — the response says so
    fake.qwen = QWEN_SHOW.replace('ActiveState=inactive', 'ActiveState=active')
    r = fake.client.post('/api/affinity/action', json={'action': 'start'}).get_json()
    assert 'vllm-qwen was stopped' in r['note']
    r = fake.client.post('/api/affinity/action', json={'action': 'disable'}).get_json()
    assert r['success'] and 'note' not in r and 'warm_start_s' not in r
    r = fake.client.post('/api/affinity/action', json={'action': 'rm -rf'})
    assert r.status_code == 400


def test_logs(fake):
    r = fake.client.get('/api/affinity/logs?source=container&tail=50').get_json()
    assert r['name'] == 'affinity-4' and 'listening on http://0.0.0.0:8095' in r['logs']
    r = fake.client.get('/api/affinity/logs?source=journal').get_json()
    assert r['name'] == 'affinity-deepseek.service' and '[build]' in r['logs']
    assert fake.client.get('/api/affinity/logs?source=etc').status_code == 400


# ─── Hooks ────────────────────────────────────────────────────────────

def test_summary_block(fake):
    s = af._affinity_summary()
    assert s['installed'] and s['state'] == 'active' and s['running'] and s['ready'] and s['enabled']
    assert s['model'] == 'DeepSeek-V4-Flash' and s['port'] == 8095 and s['gpus'] == '4' and s['tp'] == 4
    assert s['experts_resident'] == 11008 and s['resident_pct'] == 100.0
    assert s['tps'] == 71.1 and s['draft_accept_pct'] == 33.7 and s['requests_recent'] == 1 and s['errors'] == 0
    # engine still loading: active, /v1/models refused → not ready, no model
    fake.models = None
    s = af._affinity_summary()
    assert s['running'] and s['ready'] is False and s['model'] is None
    # through the real aggregator
    r = fake.client.get('/api/summary').get_json()
    assert r['affinity']['unit'] == 'affinity-deepseek'


def test_alerts(fake):
    assert af._affinity_alerts() == []
    fake.show = _show_text(ActiveState='failed', SubState='failed', NRestarts='2')
    keys = {a['key']: a['message'] for a in af._affinity_alerts()}
    assert 'affinity_down' in keys and 'failed' in keys['affinity_down']
    assert 'affinity_restarting' in keys and '2 time(s)' in keys['affinity_restarting']
    # disabled + stopped on purpose is not an alert
    fake.show = _show_text(ActiveState='inactive', SubState='dead', UnitFileState='disabled')
    assert af._affinity_alerts() == []
    # both this and vllm-qwen enabled at boot → conflict alert, one per unit
    fake.show = SHOW
    fake.qwen = QWEN_SHOW.replace('UnitFileState=disabled', 'UnitFileState=enabled')
    keys = {a['key'] for a in af._affinity_alerts()}
    assert keys == {'affinity_conflict:vllm-qwen'}
    # error lines in the log since the last start
    fake.qwen = QWEN_SHOW
    fake.log = LOG + 'error: expert fetch failed\n'
    keys = {a['key']: a['message'] for a in af._affinity_alerts()}
    assert set(keys) == {'affinity_errors'} and '1 error line' in keys['affinity_errors']


def test_history_and_metrics(fake):
    assert af._affinity_history() == [('affinity_tps', 'affinity-deepseek', 71.1),
                                      ('affinity_accept', 'affinity-deepseek', 33.7)]
    text = '\n'.join(af._affinity_metrics())
    assert 'storagedash_affinity_up 1' in text and 'storagedash_affinity_ready 1' in text
    assert 'storagedash_affinity_last_decode_tps 71.1' in text
    assert 'storagedash_affinity_experts_resident_pct 100.0' in text
    fake.show = _show_text(ActiveState='inactive', SubState='dead')
    assert af._affinity_history() == []
    text = '\n'.join(af._affinity_metrics())
    assert 'storagedash_affinity_up 0' in text and 'last_decode_tps' not in text


def test_module_descriptor():
    assert af.MODULE['id'] == 'affinity' and af.MODULE['default_enabled'] is False
    assert af.MODULE['category'] == 'AI Tools' and af.MODULE['history_metrics'] == {'affinity_tps', 'affinity_accept'}
    assert 'affinity' in app.MODULE_IDS
