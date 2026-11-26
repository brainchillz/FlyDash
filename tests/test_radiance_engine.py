"""Radiance (standalone engine) module, id `radiance_engine` (3.12.0; 2.x
`--config` units since 4.1.1): the
engine's systemd unit — `systemctl show` / ExecStart argv parsing,
/v1/models readiness, the engine's own `/stats`, its boot receipt and
per-request lines from the container log, unit actions, logs, and the
summary/alerts/history/metrics hooks.

Fixtures are the REAL unit on the 4x R9700 box (2026-10-24, radiance
1.11.0, Qwen3.8-Flash-Next fp8-iq4r at TP=4, `--placement all_vram`):
`systemctl show`, the unit file, /v1/models, /stats after two requests and
the container log of that run. systemctl, HTTP and the docker socket are
faked at the module boundary.
"""
import json
import os
import pytest

import app
from nexusdash.modules import radiance_engine as re_

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'radiance_engine')


def _read(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


SHOW = _read('systemctl-show.txt')
UNIT = _read('radiance-flashnext.service')
MODELS = _read('models.json')
STATS = _read('stats.json')
LOG = _read('container-log.txt')
OTHER_SHOW = 'Id=%s.service\nLoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n'


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
    def __init__(self):
        self.show, self.models, self.stats, self.log = SHOW, MODELS, STATS, LOG
        self.others = {u: OTHER_SHOW % u for u in ('vllm-qwen', 'llama-server', 'affinity-deepseek')}
        self.calls = []

    def run(self, args, input_data=None, no_sudo=False, timeout=120):
        self.calls.append(list(args))
        if args[:2] == ['systemctl', 'show']:
            unit = args[2][:-8]
            if unit == 'radiance-flashnext':
                return (self.show, '', 0) if self.show else ('', 'not found', 1)
            if unit in self.others:
                return (self.others[unit], '', 0) if self.others[unit] else ('', 'not found', 1)
        if args[0] == 'journalctl':
            return '2026-10-24T05:14:12+0000 node run.sh[1]: I device: AQL backend, 4 device(s)\n', '', 0
        raise AssertionError('unexpected run %r' % (args,))

    def run_safe(self, args, input_data=None):
        self.calls.append(list(args))
        return {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0}

    def http(self, base, path, timeout):
        assert base == 'http://127.0.0.1:8300', base
        if path == '/v1/models':
            return (self.models, None) if self.models else (None, 'connection refused')
        if path == '/stats':
            return (self.stats, None) if self.stats else (None, 'HTTP 500')
        raise AssertionError('unexpected GET %s' % path)

    def docker_raw(self, method, path, body=None, timeout=60):
        assert path.startswith('/containers/radiance-tp4/logs?'), path
        payload = self.log.encode()
        frame = b'\x02\x00\x00\x00' + len(payload).to_bytes(4, 'big') + payload
        return 200, frame


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    monkeypatch.setattr(re_, 'run', f.run)
    monkeypatch.setattr(re_, 'run_safe', f.run_safe)
    monkeypatch.setattr(re_, '_http_get', f.http)
    monkeypatch.setattr(re_, 'docker_raw', f.docker_raw)
    monkeypatch.setattr(re_, 'ENGINE_URL', '')
    app.app.config['TESTING'] = True
    f.client = app.app.test_client()
    return f


# ─── Pure parsers against the real captures ───────────────────────────

def test_show_capture_parses():
    s = re_.parse_show(SHOW)
    assert s['ActiveState'] == 'active' and s['UnitFileState'] == 'enabled' and s['NRestarts'] == '0'
    env = re_.parse_env_string(s['Environment'])
    assert env == {'HOME': '/home/backupuser', 'GPUS': '0,1,2,3', 'NAME': 'radiance-tp4', 'IMAGE': 'stilldeadcode/radiance:latest'}
    argv = re_.parse_exec_argv(s['ExecStart'])
    args = re_.parse_args(argv)
    assert argv[:4] == ['--model', '/model.rad', '--tp', '4']
    assert args['tp'] == '4' and args['tp-wire'] == 'wht6' and args['p2p'] == 'on' and args['placement'] == 'all_vram'
    assert args['ngram-placement'] == 'ram' and args['gpu-headroom-mib'] == '128' and args['max-model-len'] == '262144'
    assert args['kv-cache-dtype'] == 'fp8' and args['num-speculative-tokens'] == '3' and args['max-num-seqs'] == '16'
    assert args['reasoning-effort-map'] == 'high=xhigh' and args['served-model-name'] == 'qwen3.8-flash-next'
    assert args['host'] == '0.0.0.0' and args['port'] == '8300'
    assert re_.parse_exec_stop_name(s['ExecStop']) == 'radiance-tp4'
    # shutdown.target is systemd's own; the three units that want the same cards remain
    assert re_.parse_conflicts(s['Conflicts']) == ['vllm-qwen', 'llama-server', 'affinity-deepseek']


def test_unit_file_capture_matches_show():
    assert 'Conflicts=vllm-qwen.service llama-server.service affinity-deepseek.service' in UNIT
    assert '--placement all_vram' in UNIT and '--p2p on' in UNIT and 'NAME=radiance-tp4' in UNIT


def test_startup_summary_from_real_log():
    su = re_.startup_summary(LOG)
    assert su['devices'] == 4 and su['arch'] == 'qwen4exp' and su['layers'] == 48 and su['ctx'] == 262144
    assert su['p2p'] is True and su['wire_lossy'] is True
    assert su['drafter'] == 'mtp' and su['draft_tokens'] == 3
    assert su['ngram_gib'] == 47.68 and su['ngram_where'] == 'ram'
    assert su['kv_backing_pct'] == 41 and su['kv_sessions'] == 6.5
    # the per-card VRAM budget, as the engine printed it (GiB)
    assert su['budget']['card total'] == 31.86 and su['budget']['static weights'] == 2.68
    assert su['budget']['experts'] == 14.83 and su['budget']['kv cache'] == 12.71
    assert su['budget']['vram_weights'] == 17.5 and su['budget']['vram_kv'] == 12.71
    # four cards settled with the 128 MiB headroom, none warned
    assert [c['card'] for c in su['settled']] == [0, 1, 2, 3] and su['headroom_warnings'] == 0
    assert all(c['free_mib'] > 240 and c['warned'] is False for c in su['settled'])
    assert su['listening'] == 'http://0.0.0.0:8300' and su['ready'] is True
    assert su['errors'] == 0 and su['warnings'] >= 1          # the lossy-wire warning at least
    # two finished requests in the capture: 87 tokens (33 of them reasoning) then 400 (cut by max_tokens)
    assert su['requests_total'] == 2 and su['tokens_total'] == 487 and su['last_tps'] == 208.6
    r0, r1 = su['requests']
    assert r0 == {'id': 'chatcmpl-4fc7670599c463a0fba778a1', 'client': '127.0.0.1:56786', 'finish': 'stop',
                  'prompt': 63, 'cached': 0, 'gen': 87, 'reasoning': 33, 'ttft_s': 0.08, 'tps': 210.9, 'seconds': 0.48}
    assert r1['finish'] == 'length' and r1['gen'] == 400 and r1['reasoning'] == 0 and r1['tps'] == 208.6
    # the kept lines are the engine's own receipt, starting at the backend line, ending at the server line
    assert su['lines'][0].startswith('I device: AQL backend') and su['lines'][-1].startswith('I server: listening')
    assert any(l.startswith('I VRAM budget, per card') for l in su['lines'])


def test_startup_summary_anchors_on_last_start_and_counts():
    second = LOG.replace('the 4 cards reach each other\'s memory directly (peer to peer)',
                         'the 4 cards exchange through host memory (no peer access)')
    second = second.replace('I[0] card 0 settled', 'W[0] card 0 settled')
    two_runs = LOG + '\n' + second + 'E[2] something broke\n' \
        + 'I req chatcmpl-x done from 10.0.0.1:1 finish=stop prompt=10 (cache 0) gen=20 ttft=0.05s 100.0 tok/s 0.20s\n'
    su = re_.startup_summary(two_runs)
    assert su['p2p'] is False and su['headroom_warnings'] == 1 and su['settled'][0]['warned'] is True
    assert su['errors'] == 1
    assert su['requests_total'] == 3 and su['last_tps'] == 100.0 and su['requests'][-1]['reasoning'] is None
    assert re_.startup_summary('')['ready'] is False and re_.startup_summary(None)['requests'] == []
    assert re_.startup_summary(None)['budget'] == {} and re_.startup_summary(None)['p2p'] is None


def test_startup_summary_loading_engine():
    cut = LOG.split('I serving on')[0]
    su = re_.startup_summary(cut)
    assert su['ready'] is False and su['listening'] is None and su['budget']['experts'] == 14.83


def test_parse_stats_from_real_capture():
    m = re_.parse_stats(STATS)
    assert m['model'] == 'qwen3.8-flash-next' and m['version'] == '1.11.0'
    assert m['startup_s'] == 50 and m['running'] == 0 and m['waiting'] == 0 and m['requests_total'] == 2
    assert m['step_ms_best'] == 11.26 and m['step_ms_recent'] == 11.34
    assert m['draft_accept_pct'] == 45.5 and m['draft_positions_pct'] == [71.2, 58.9, 55.8]
    assert m['kv_pct'] == 0.03 and m['kv_tokens_used'] == 568 and m['kv_tokens_total'] == 1698704
    assert m['decode_tps'] == pytest.approx(28.96, abs=0.01) and m['steps_decode'] == 207
    assert m['world'] == 4 and len(m['cards']) == 4
    assert m['cards'][0]['vram_total_gib'] == 31.86 and 31.0 < m['cards'][0]['vram_used_gib'] < 31.86


def test_parse_stats_rejects_other_json():
    assert re_.parse_stats('') == {} and re_.parse_stats('not json') == {} and re_.parse_stats('[]') == {}
    assert re_.parse_stats(json.dumps({'object': 'list', 'data': []})) == {}
    # the step fields read 0 until the first 32-step window completes → None, not 0.0
    m = re_.parse_stats(json.dumps({'version': '1.11.0', 'step_ns_decode_best': 0, 'step_ns_decode_recent': 0,
                                    'kv_util': 0, 'draft_accept': 0}))
    assert m['step_ms_best'] is None and m['step_ms_recent'] is None and m['kv_pct'] == 0 and m['draft_accept_pct'] == 0


# ─── Status / routes ──────────────────────────────────────────────────

def test_status_page_document(fake):
    r = fake.client.get('/api/radiance_engine')
    assert r.status_code == 200
    st = r.get_json()
    assert st['installed'] and st['running'] and st['ready'] and st['enabled'] and st['unit'] == 'radiance-flashnext'
    assert st['port'] == 8300 and st['container'] == 'radiance-tp4' and st['gpus'] == '0,1,2,3' and st['tp'] == 4
    assert st['image'] == 'stilldeadcode/radiance:latest'
    assert st['env'] == {'GPUS': '0,1,2,3', 'NAME': 'radiance-tp4', 'IMAGE': 'stilldeadcode/radiance:latest'}  # HOME hidden
    assert st['models'] == [{'id': 'qwen3.8-flash-next', 'root': '/model.rad', 'max_model_len': 262144}]
    assert st['stats']['step_ms_best'] == 11.26 and st['startup']['last_tps'] == 208.6
    assert [a['flag'] for a in st['named_args']][:5] == ['--model', '--tp', '--tp-wire', '--p2p', '--placement']
    assert st['other_args'] == {}
    assert [(c['unit'], c['present'], c['active'], c['enabled']) for c in st['conflicts']] == \
        [('vllm-qwen', True, False, False), ('llama-server', True, False, False), ('affinity-deepseek', True, False, False)]


def test_status_unit_missing(fake):
    fake.show = ''
    st = fake.client.get('/api/radiance_engine').get_json()
    assert st['installed'] is False and 'not found' in st['error']
    assert re_._engine_summary() is None and re_._engine_alerts() == [] and re_._engine_metrics() == []
    r = fake.client.post('/api/radiance_engine/action', json={'action': 'start'})
    assert r.status_code == 404


def test_status_inactive_skips_http_and_log(fake):
    fake.show = _show_text(ActiveState='inactive', SubState='dead', MainPID='0')
    st = re_._base_status()
    assert st['installed'] and not st['running'] and not st['ready'] and st['models'] == []
    assert 'startup' not in st and 'stats' not in st
    assert all(c[0] == 'systemctl' for c in fake.calls)


def test_status_engine_loading(fake):
    fake.models = None
    st = re_._base_status()
    assert st['running'] and st['ready'] is False and st['models_error'] == 'connection refused'
    assert 'stats' not in st                       # /stats is only asked once /v1/models answers
    assert st['startup']['ready'] is True          # the log is read regardless


def test_actions(fake):
    r = fake.client.post('/api/radiance_engine/action', json={'action': 'restart'}).get_json()
    assert r['success'] and r['action'] == 'restart' and r['warm_start_s'] == re_.WARM_START_S
    assert ['systemctl', 'restart', 'radiance-flashnext.service'] in fake.calls
    assert 'note' not in r
    # a running affinity-deepseek gets stopped by Conflicts= — the response says so
    fake.others['affinity-deepseek'] = fake.others['affinity-deepseek'].replace('ActiveState=inactive', 'ActiveState=active')
    r = fake.client.post('/api/radiance_engine/action', json={'action': 'start'}).get_json()
    assert 'affinity-deepseek was stopped' in r['note']
    r = fake.client.post('/api/radiance_engine/action', json={'action': 'disable'}).get_json()
    assert r['success'] and 'note' not in r and 'warm_start_s' not in r
    r = fake.client.post('/api/radiance_engine/action', json={'action': 'rm -rf'})
    assert r.status_code == 400


def test_logs(fake):
    r = fake.client.get('/api/radiance_engine/logs?source=container&tail=50').get_json()
    assert r['name'] == 'radiance-tp4' and 'I serving on http://0.0.0.0:8300' in r['logs']
    r = fake.client.get('/api/radiance_engine/logs?source=journal').get_json()
    assert r['name'] == 'radiance-flashnext.service' and 'AQL backend' in r['logs']
    assert fake.client.get('/api/radiance_engine/logs?source=etc').status_code == 400


# ─── Hooks ────────────────────────────────────────────────────────────

def test_summary_block(fake):
    s = re_._engine_summary()
    assert s['installed'] and s['state'] == 'active' and s['running'] and s['ready'] and s['enabled']
    assert s['model'] == 'qwen3.8-flash-next' and s['port'] == 8300 and s['gpus'] == '0,1,2,3' and s['tp'] == 4
    assert s['version'] == '1.11.0' and s['step_ms'] == 11.34 and s['step_ms_best'] == 11.26
    assert s['draft_accept_pct'] == 45.5 and s['kv_pct'] == 0.03 and s['requests_total'] == 2
    assert s['requests_running'] == 0 and s['requests_waiting'] == 0 and s['p2p'] is True and s['errors'] == 0
    # engine still loading: active, /v1/models refused → not ready, no model, no stats
    fake.models = None
    s = re_._engine_summary()
    assert s['running'] and s['ready'] is False and s['model'] is None and s['step_ms'] is None
    # through the real aggregator
    r = fake.client.get('/api/summary').get_json()
    assert r['radiance_engine']['unit'] == 'radiance-flashnext'


def test_alerts(fake):
    assert re_._engine_alerts() == []
    fake.show = _show_text(ActiveState='failed', SubState='failed', NRestarts='2')
    keys = {a['key']: a['message'] for a in re_._engine_alerts()}
    assert 'radiance_engine_down' in keys and 'failed' in keys['radiance_engine_down']
    assert 'radiance_engine_restarting' in keys and '2 time(s)' in keys['radiance_engine_restarting']
    # disabled + stopped on purpose is not an alert
    fake.show = _show_text(ActiveState='inactive', SubState='dead', UnitFileState='disabled')
    assert re_._engine_alerts() == []
    # both this and vllm-qwen enabled at boot → conflict alert, one per unit
    fake.show = SHOW
    fake.others['vllm-qwen'] = fake.others['vllm-qwen'].replace('UnitFileState=disabled', 'UnitFileState=enabled')
    assert {a['key'] for a in re_._engine_alerts()} == {'radiance_engine_conflict:vllm-qwen'}
    fake.others['vllm-qwen'] = OTHER_SHOW % 'vllm-qwen'
    # error lines, a headroom warning and a P2P-less link, each from the log
    fake.log = LOG + 'E[1] expert fetch failed\n'
    keys = {a['key']: a['message'] for a in re_._engine_alerts()}
    assert set(keys) == {'radiance_engine_errors'} and '1 error line' in keys['radiance_engine_errors']
    fake.log = LOG.replace('I[2] card 2 settled', 'W[2] card 2 settled')
    assert {a['key'] for a in re_._engine_alerts()} == {'radiance_engine_headroom'}
    fake.log = LOG.replace("the 4 cards reach each other's memory directly (peer to peer)",
                           'the 4 cards exchange through host memory (no peer access)')
    keys = {a['key']: a['message'] for a in re_._engine_alerts()}
    assert set(keys) == {'radiance_engine_p2p'} and 'host memory' in keys['radiance_engine_p2p']


def test_history_and_metrics(fake):
    h = re_._engine_history()
    assert [x[:2] for x in h] == [('radiance_engine_tps', 'radiance-flashnext'), ('radiance_engine_step_ms', 'radiance-flashnext'),
                                  ('radiance_engine_accept', 'radiance-flashnext'), ('radiance_engine_kv_pct', 'radiance-flashnext')]
    assert h[1][2] == 11.34 and h[2][2] == 45.5
    text = '\n'.join(re_._engine_metrics())
    assert 'storagedash_radiance_engine_up 1' in text and 'storagedash_radiance_engine_ready 1' in text
    assert 'storagedash_radiance_engine_step_ms 11.34' in text and 'storagedash_radiance_engine_step_ms_best 11.26' in text
    assert 'storagedash_radiance_engine_draft_accept_pct 45.5' in text
    fake.show = _show_text(ActiveState='inactive', SubState='dead')
    assert re_._engine_history() == []
    text = '\n'.join(re_._engine_metrics())
    assert 'storagedash_radiance_engine_up 0' in text and 'step_ms' not in text


def test_module_descriptor():
    assert re_.MODULE['id'] == 'radiance_engine' and re_.MODULE['default_enabled'] is False
    assert re_.MODULE['label'] == 'Radiance' and re_.MODULE['category'] == 'AI Tools'
    assert re_.MODULE['history_metrics'] == {'radiance_engine_tps', 'radiance_engine_step_ms', 'radiance_engine_accept',
                                             'radiance_engine_kv_pct'}
    assert 'radiance_engine' in app.MODULE_IDS
    # the vLLM fork keeps its id and only its label changed
    from nexusdash.modules import radiance as rd
    assert rd.MODULE['id'] == 'radiance' and rd.MODULE['label'] == 'vLLM Radiance'


# ─── Radiance 2.x: `--config FILE` on the unit, settings from the engine ──
# Fixtures: the same box on 2026-11-24 after the move to radiance 2.2.0 —
# `systemctl show` with `ExecStart=run.sh --config /radiance.yaml`, the
# container log of that start (19 requests), /stats and /server_info.

SHOW2 = _read('systemctl-show-2x.txt')
LOG2 = _read('container-log-2x.txt')
STATS2 = _read('stats-2x.json')
INFO2 = _read('server_info-2x.json')
INSPECT2 = json.dumps({'Mounts': [{'Source': '/srv/models/x.rad', 'Destination': '/model.rad'},
                                  {'Source': '/srv/radiance/radiance.yaml', 'Destination': '/radiance.yaml'}]})


@pytest.fixture
def fake2(fake, monkeypatch):
    fake.show, fake.stats, fake.log, fake.info = SHOW2, STATS2, LOG2, INFO2
    http1, gets = fake.http, []

    def http(base, path, timeout):
        gets.append(path)
        if path == '/server_info':
            return (fake.info, None) if fake.info else (None, 'HTTP 404')
        return http1(base, path, timeout)

    def docker_raw(method, path, body=None, timeout=60):
        if path == '/containers/radiance-tp4/json':
            return 200, INSPECT2.encode()
        return Fake.docker_raw(fake, method, path, body, timeout)

    monkeypatch.setattr(re_, '_http_get', http)
    monkeypatch.setattr(re_, 'docker_raw', docker_raw)
    fake.gets = gets
    return fake


def test_startup_summary_2x_log():
    su = re_.startup_summary(LOG2)
    assert su['ready'] is True and su['listening'] == 'http://0.0.0.0:8300'      # "I server: listening on"
    assert su['devices'] == 4 and su['arch'] == 'qwen4exp' and su['p2p'] is True and su['wire_lossy'] is True
    assert su['drafter'] == 'mtp' and su['draft_tokens'] == 3 and su['ngram_where'] == 'ram'
    assert su['budget']['experts'] == 14.83 and su['budget']['kv cache'] == 12.53
    assert su['errors'] == 0 and su['headroom_warnings'] == 0 and su['requests_total'] == 19
    cfg = {c['flag']: (c['value'], c['source']) for c in su['config']}
    assert cfg['--tp'] == ('4', 'models.qwen3.8-flash-next.tp')
    assert cfg['--port'] == ('8300', 'server.port')
    assert cfg['--kv-cache-dtype'] == ('i8', 'models.qwen3.8-flash-next.kv-cache-dtype')
    assert cfg['--gpu-headroom-mib'] == ('128', 'memory.gpu-headroom-mib')
    assert cfg['--kernels'][1] == 'auto' and cfg['--http-threads'] == ('32', 'auto')
    assert not any(line.startswith('I   --') for line in su['lines'])            # the report is not receipt text
    set_ = [c['flag'] for c in re_.config_settings(su['config'])]
    assert '--tp' in set_ and '--kernels' not in set_ and '--http-threads' not in set_


def test_startup_summary_1x_log_has_the_report_too():
    cfg = {c['flag']: c for c in re_.startup_summary(LOG)['config']}
    assert cfg['--tp']['value'] == '4' and cfg['--tp']['source'] == 'command line'
    assert cfg['--tp-wire-min-kb']['source'] == 'default'


def test_parse_server_info():
    rows = {r['flag']: r['value'] for r in re_.parse_server_info(INFO2)}
    assert rows == {'--model': '/model.rad', '--tp': '4', '--tp-wire': 'wht6', '--placement': 'all_vram',
                    '--ngram-placement': 'ram', '--max-model-len': '262144', '--max-num-seqs': '16',
                    '--max-num-batched-tokens': '2048', '--reasoning-effort-map': 'high=xhigh',
                    '--served-model-name': 'qwen3.8-flash-next'}
    assert re_.parse_server_info('') == [] and re_.parse_server_info('[]') == []
    assert re_.parse_server_info('{"server": {}}') == []


def test_status_2x_config_unit(fake2):
    st = fake2.client.get('/api/radiance_engine').get_json()
    assert st['installed'] and st['running'] and st['ready']
    assert st['argv'] == ['--config', '/radiance.yaml'] and st['config_file'] == '/radiance.yaml'
    assert st['config_host_path'] == '/srv/radiance/radiance.yaml'
    assert st['port'] == 8300 and st['tp'] == 4 and st['image'] == 'stilldeadcode/radiance:2.2.0'
    assert st['settings_from'] == 'log' and '/server_info' not in fake2.gets
    named = {a['flag']: a for a in st['named_args']}
    assert [a['flag'] for a in st['named_args']][:4] == ['--model', '--tp', '--tp-wire', '--p2p']
    assert named['--kv-cache-dtype']['value'] == 'i8' and named['--kv-cache-dtype']['label'] == 'KV cache dtype'
    assert named['--port']['source'] == 'server.port' and named['--max-num-seqs']['value'] == '16'
    assert 'config' not in st['other_args'] and 'kernels' not in st['other_args']
    assert st['stats']['version'] == '2.2.0' and st['startup']['ready'] is True


def test_status_2x_report_scrolled_out_uses_server_info(fake2):
    fake2.log = ''.join(line + '\n' for line in LOG2.splitlines() if line.startswith('I req '))
    st = fake2.client.get('/api/radiance_engine').get_json()
    assert st['ready'] and st['settings_from'] == 'server_info' and '/server_info' in fake2.gets
    assert st['tp'] == 4 and st['port'] == 8300                                   # no port there: the default
    assert {a['flag'] for a in st['named_args']} >= {'--model', '--tp', '--placement', '--max-model-len'}
    assert all(a['source'] == 'server_info' for a in st['named_args'])
    fake2.info = None                                                             # an engine without the route
    st = fake2.client.get('/api/radiance_engine').get_json()
    assert st['ready'] and st['settings_from'] is None and st['named_args'] == [] and st['tp'] is None


def test_status_2x_port_follows_the_report(fake2, monkeypatch):
    fake2.log = LOG2.replace('I   --port                             8300', 'I   --port                             8311')
    seen = []
    monkeypatch.setattr(re_, '_http_get', lambda base, path, timeout: (seen.append(base), (None, 'refused'))[1])
    st = re_._base_status()
    assert st['port'] == 8311 and st['url'] == 'http://127.0.0.1:8311' and seen == ['http://127.0.0.1:8311']


def test_status_2x_inactive_and_summary(fake2):
    block = re_._engine_summary()
    assert block['ready'] and block['tp'] == 4 and block['port'] == 8300 and block['version'] == '2.2.0'
    assert block['step_ms_best'] == re_.parse_stats(STATS2)['step_ms_best'] and block['p2p'] is True
    assert re_._engine_alerts() == []
    fake2.show = SHOW2.replace('ActiveState=active', 'ActiveState=inactive')
    st = fake2.client.get('/api/radiance_engine').get_json()
    assert st['config_file'] == '/radiance.yaml' and st['named_args'] == [] and st['settings_from'] is None
    assert st['config_host_path'] is None and st['other_args'] == {}
