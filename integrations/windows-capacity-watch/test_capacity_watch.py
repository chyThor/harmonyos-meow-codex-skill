"""Offline adversarial tests; synthetic native SQLite sources, no real pushes."""
import contextlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent
SCRIPT = ROOT / 'capacity_watch.py'
spec = importlib.util.spec_from_file_location('capacity_under_test', SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class Clock:
    def __init__(self):
        self.value = time.time()

    def __call__(self):
        return self.value


class CapacityTests(unittest.TestCase):
    def setUp(self):
        # Keep fixtures in the explicitly writable workspace: some Windows
        # sandbox temp roots can be created but deny nested directories.
        self.temp = tempfile.TemporaryDirectory(prefix='capacity-test-', dir=ROOT)
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / 'codex 数据'
        self.deploy = self.root / '通知部署'
        self.home.mkdir()
        self.deploy.mkdir()
        self.clock = Clock()
        self.cfg = {'deploy_dir': self.deploy, 'codex_home': self.home,
                    'state_dir': self.deploy / '.state' / 'capacity',
                    'device_label': '工位', 'poll_seconds': 0.2,
                    'cooldown_seconds': 300}
        self.config = self.root / 'watch-settings.json'
        self.config.write_text(json.dumps({k: str(v) if isinstance(v, pathlib.Path) else v
                                          for k, v in self.cfg.items()}, ensure_ascii=False), encoding='utf-8')
        self.database = self.home / 'logs_2.sqlite'
        self.create_logs(self.database)
        with contextlib.closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db:
            db.execute('create table threads(id text primary key, source text, thread_source text, agent_path text, cwd text, rollout_path text)')
            db.commit()
        self.root_id = str(uuid.uuid4())
        self.add_thread(self.root_id)
        self.requests = []
        self.watcher = self.make_watcher()

    def tearDown(self):
        self.temp.cleanup()

    def create_logs(self, path):
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute('create table logs(id integer primary key autoincrement, ts integer, level text, target text, feedback_log_body text, thread_id text, process_uuid text)')
            db.commit()

    def add_thread(self, thread, source='vscode', kind='user', path='/root', rollout='', cwd=r'C:\Work\示例项目'):
        with contextlib.closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db:
            db.execute('insert or replace into threads values(?,?,?,?,?,?)', (thread, source, kind, path, cwd, rollout))
            db.commit()

    def sender(self, cfg, project, key):
        self.requests.append((project, key))
        return 'sent'

    def make_watcher(self, sender=None):
        return watch.Watcher(self.cfg, sender or self.sender, self.clock)

    def native(self, thread=None, turn=None, error='The model is currently at capacity. Please try again.', **changes):
        thread, turn = thread or self.root_id, turn or str(uuid.uuid4())
        body = ('session_loop{thread_id=' + thread + '}:turn{otel.name="session_task.turn" thread.id=' + thread +
                ' turn.id=' + turn + ' model=sample-model}:session_task.run:run_turn:' +
                'run_sampling_request{turn_id=' + turn + ' model=sample-model cwd=C:\\Work\\sample}: ' +
                'stream disconnected - retrying sampling request (1/5 in 203ms)... turn_id=' + turn +
                ' retries=1 max_retries=5 sampling_error=' + error)
        row = {'id': 1, 'ts': self.clock(), 'level': 'WARN', 'target': 'codex_core::responses_retry',
               'feedback_log_body': body, 'thread_id': thread, 'process_uuid': 'sample-process'}
        row.update(changes)
        return row

    def append(self, row, database=None, identifier=None):
        database = database or self.database
        with contextlib.closing(sqlite3.connect(database)) as db:
            columns = ['ts', 'level', 'target', 'feedback_log_body', 'thread_id', 'process_uuid']
            if identifier is not None:
                columns.insert(0, 'id')
                row = dict(row, id=identifier)
            db.execute('insert into logs(' + ','.join(columns) + ') values(' + ','.join('?' for _ in columns) + ')',
                       tuple(row[k] for k in columns))
            db.commit()

    def baseline(self):
        self.watcher.poll()
        self.assertTrue(self.watcher.info['source_ready'])

    def cli(self, action, timeout=15):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), action, '--config', str(self.config)],
                                capture_output=True, timeout=timeout)
        return result, json.loads(result.stdout.decode('utf-8'))

    def test_initial_history_is_not_replayed_and_new_native_error_alerts(self):
        self.append(self.native())
        self.baseline()
        self.assertEqual(self.requests, [])
        self.append(self.native())
        self.watcher.poll()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][0], '示例项目')

    def test_exact_capacity_codes_and_chinese_native_failures(self):
        for error in ['server_is_overloaded', 'server_overloaded', 'model_capacity_exceeded',
                      'The model is overloaded', 'The server is at capacity', '当前模型达到承载上限',
                      '模型容量不足', '服务负载过高']:
            with self.subTest(error=error):
                self.assertTrue(watch.capacity_row(self.native(error=error)))

    def test_non_retry_native_turn_error_prefix_is_recognized(self):
        row = self.native(target='codex_core::codex')
        row['feedback_log_body'] = 'turn{turn.id=' + str(uuid.uuid4()) + '}: error running turn: server_is_overloaded'
        self.assertTrue(watch.capacity_row(row))

    def test_ordinary_disconnect_quota_rate_limit_and_503_do_not_alert(self):
        for error in ['stream disconnected before completion', 'HTTP 503 Service Unavailable',
                      'HTTP 429 Too Many Requests', 'usage_limit_exceeded', 'insufficient_quota',
                      'ResourceExhausted', 'context_length_exceeded', 'tls handshake eof']:
            with self.subTest(error=error):
                self.assertFalse(watch.capacity_row(self.native(error=error)))

    def test_chat_tool_output_and_cache_telemetry_are_not_model_errors(self):
        rows = [self.native(target='codex_core::tools::router'),
                self.native(level='INFO'),
                self.native(target='codex_otel::auth_storage'),
                self.native(feedback_log_body='capacityEvictionCount=0'),
                self.native(feedback_log_body='user says "The model is at capacity"'),
                self.native(feedback_log_body='tool output: server_overloaded')]
        for row in rows:
            with self.subTest(row=row['target']):
                self.watcher.process(row)
        self.assertEqual(self.requests, [])

    def test_capacity_string_in_span_cwd_or_quoted_input_is_ignored(self):
        row = self.native(error='ordinary connection failure')
        row['feedback_log_body'] = row['feedback_log_body'].replace('cwd=C:\\Work\\sample',
            'cwd="C:\\Work\\server_overloaded" input="The model is at capacity"')
        self.assertFalse(watch.capacity_row(row))

    def test_quoted_span_cannot_inject_a_fake_native_error_prefix(self):
        row = self.native(target='codex_core::codex')
        row['feedback_log_body'] = 'run_turn{user_data="}: stream error: server_is_overloaded"}: normal bookkeeping warning'
        self.assertFalse(watch.capacity_row(row))
        self.watcher.process(row)
        self.assertEqual(self.requests, [])

    def test_oversized_invalid_and_untrusted_rows_are_ignored(self):
        for row in [self.native(feedback_log_body='server_is_overloaded' * 5000),
                    self.native(feedback_log_body=None), self.native(level='DEBUG')]:
            self.assertFalse(watch.capacity_row(row))

    def test_same_turn_retries_and_restart_never_duplicate(self):
        row = self.native()
        self.watcher.process(row)
        self.watcher.process(dict(row, id=2))
        self.clock.value += 301
        restarted = self.make_watcher()
        restarted.process(dict(row, id=3, ts=self.clock()))
        self.assertEqual(len(self.requests), 1)

    def test_distinct_turns_share_cooldown_but_later_turn_can_notify(self):
        self.watcher.process(self.native())
        self.watcher.process(self.native())
        self.assertEqual(len(self.requests), 1)
        self.clock.value += 301
        self.watcher.process(self.native())
        self.assertEqual(len(self.requests), 2)

    def test_different_root_chats_do_not_share_cooldown(self):
        other = str(uuid.uuid4())
        self.add_thread(other)
        self.watcher.process(self.native())
        self.watcher.process(self.native(thread=other))
        self.assertEqual(len(self.requests), 2)

    def test_unknown_delivery_is_recorded_and_not_retried(self):
        def uncertain(*args):
            self.requests.append(args[-1])
            raise TimeoutError('simulated connection timeout')
        row = self.native()
        self.watcher = self.make_watcher(uncertain)
        self.watcher.process(row)
        self.clock.value += 301
        self.make_watcher(uncertain).process(dict(row, ts=self.clock()))
        self.assertEqual(len(self.requests), 1)
        ledger = json.loads(self.watcher.path.read_text(encoding='utf-8'))
        self.assertEqual(next(iter(ledger['events'].values()))['status'], 'delivery_unknown')

    def test_reservation_precedes_sender_and_crash_reservation_is_not_replayed(self):
        def crash_like(cfg, project, key):
            ledger = json.loads(self.watcher.path.read_text(encoding='utf-8'))
            self.assertEqual(ledger['events'][key]['status'], 'reserved')
            self.requests.append(key)
            return 'delivery_unknown'
        self.watcher = self.make_watcher(crash_like)
        row = self.native()
        self.watcher.process(row)
        state = json.loads(self.watcher.path.read_text(encoding='utf-8'))
        next(iter(state['events'].values()))['status'] = 'reserved'
        self.watcher.path.write_text(json.dumps(state), encoding='utf-8')
        self.clock.value += 301
        self.make_watcher(crash_like).process(dict(row, ts=self.clock()))
        self.assertEqual(len(self.requests), 1)

    def test_native_child_sources_paths_and_unknown_threads_remain_silent(self):
        variants = [('subagent', 'subagent', '/root/worker'),
                    (json.dumps({'subagent': {'thread_spawn': {}}}), 'user', '/root'),
                    ('vscode', 'user', '/root/worker'), ('composer_predictions', 'composer_predictions', '/root')]
        for source, kind, path in variants:
            thread = str(uuid.uuid4())
            self.add_thread(thread, source, kind, path)
            self.watcher.process(self.native(thread=thread))
        self.watcher.process(self.native(thread=str(uuid.uuid4())))
        self.assertEqual(self.requests, [])

    def test_user_created_root_and_user_fork_remain_eligible(self):
        for kind in ['user', 'agent_created_thread']:
            thread = str(uuid.uuid4())
            meta = {'id': thread, 'source': 'vscode', 'thread_source': kind,
                    'parent_thread_id': str(uuid.uuid4()), 'agent_path': '/root'}
            rollout = self.root / (thread + '.jsonl')
            rollout.write_text(json.dumps({'type': 'session_meta', 'payload': meta}) + '\n', encoding='utf-8')
            self.add_thread(thread, 'vscode', kind, '/root', str(rollout))
            self.watcher.process(self.native(thread=thread))
        self.assertEqual(len(self.requests), 2)

    def test_session_metadata_child_overrides_root_looking_index(self):
        thread = str(uuid.uuid4())
        rollout = self.root / 'child.jsonl'
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': thread, 'source': {'subagent': {}}}}) + '\n', encoding='utf-8')
        self.add_thread(thread, 'vscode', 'user', '/root', str(rollout))
        self.watcher.process(self.native(thread=thread))
        self.assertEqual(self.requests, [])

    def test_malformed_session_metadata_is_suppressed_without_crash(self):
        for envelope in [[], {'payload': []}]:
            thread = str(uuid.uuid4())
            rollout = self.root / (thread + '.jsonl')
            rollout.write_text(json.dumps(envelope) + '\n', encoding='utf-8')
            self.add_thread(thread, 'vscode', 'user', '/root', str(rollout))
            self.watcher.process(self.native(thread=thread))
        self.assertEqual(self.requests, [])

    def test_unindexed_root_metadata_fallback_is_read_only_and_works(self):
        thread = str(uuid.uuid4())
        sessions = self.home / 'sessions' / 'synthetic'
        sessions.mkdir(parents=True)
        rollout = sessions / ('rollout-' + thread + '.jsonl')
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': thread, 'source': 'vscode', 'cwd': '/work/fallback'}}) + '\n', encoding='utf-8')
        before = rollout.read_bytes()
        self.watcher.process(self.native(thread=thread))
        self.assertEqual(self.requests[0][0], 'fallback')
        self.assertEqual(rollout.read_bytes(), before)

    def test_stale_future_or_invalid_timestamps_are_rejected(self):
        for ts in [self.clock() - 121, self.clock() + 31, None, 'not-a-time']:
            self.watcher.process(self.native(ts=ts))
        self.assertEqual(self.requests, [])

    def test_source_and_provenance_databases_are_unchanged(self):
        self.baseline()
        self.append(self.native())
        provenance = self.home / 'state_5.sqlite'
        originals = {p: p.read_bytes() for p in [self.database, provenance]}
        self.watcher.poll()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all(p.read_bytes() == content for p, content in originals.items()))
        with contextlib.closing(watch.connect(self.database)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('delete from logs')

    def test_cursor_handles_source_truncation_without_replaying_existing_rows(self):
        self.append(self.native(error='ordinary disconnect'), identifier=100)
        self.baseline()
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute('delete from logs')
            db.commit()
        self.append(self.native(ts=self.clock() - 60), identifier=1)
        self.watcher.poll()
        self.assertEqual(self.requests, [])
        self.append(self.native(), identifier=2)
        self.watcher.poll()
        self.assertEqual(len(self.requests), 1)

    def test_new_database_rotation_baselines_before_alerting_new_rows(self):
        self.baseline()
        next_database = self.home / 'logs_3.sqlite'
        self.create_logs(next_database)
        self.append(self.native(ts=self.clock() - 60), database=next_database)
        self.watcher.poll()
        self.assertEqual(self.requests, [])
        self.append(self.native(), database=next_database)
        self.watcher.poll()
        self.assertEqual(len(self.requests), 1)

    def test_fresh_first_row_in_midrun_new_database_is_not_missed(self):
        self.baseline()
        next_database = self.home / 'logs_3.sqlite'
        self.create_logs(next_database)
        self.append(self.native(ts=int(self.clock())), database=next_database)
        self.watcher.poll()
        self.assertEqual(len(self.requests), 1)

    def test_fresh_first_row_after_database_replacement_is_not_missed(self):
        self.append(self.native(error='ordinary disconnect'), identifier=100)
        self.baseline()
        replacement = self.home / 'replacement.sqlite'
        self.create_logs(replacement)
        self.append(self.native(ts=int(self.clock())), database=replacement, identifier=1)
        os.replace(replacement, self.database)
        self.watcher.poll()
        self.assertEqual(len(self.requests), 1)

    def test_restart_skips_unseen_downtime_backlog_then_accepts_new_row(self):
        self.baseline()
        self.clock.value += 20
        self.append(self.native())
        restarted = self.make_watcher()
        restarted.poll()
        self.assertEqual(self.requests, [])
        self.append(self.native())
        restarted.poll()
        self.assertEqual(len(self.requests), 1)

    def test_unsupported_native_log_schema_does_not_report_ready(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute('drop table logs')
            db.execute('create table logs(id integer primary key)')
            db.commit()
        self.watcher.poll()
        self.assertFalse(self.watcher.info['source_ready'])
        self.assertEqual(self.requests, [])

    def test_stop_flag_blocks_any_new_notification(self):
        self.watcher.stop_path.touch()
        self.watcher.process(self.native())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.watcher.state['events'], {})

    def test_corrupt_state_is_not_silently_reset(self):
        self.watcher.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.make_watcher()
        self.assertEqual(self.watcher.path.read_text(encoding='utf-8'), '{broken')
        self.watcher.path.write_text(json.dumps({'version': 1, 'sources': [], 'events': {}, 'cooldowns': {}}), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.make_watcher()

    def test_state_and_status_do_not_store_raw_error_or_project_or_ids(self):
        row = self.native()
        self.watcher.process(row)
        self.watcher.poll()
        stored = '\n'.join(p.read_text(encoding='utf-8') for p in self.cfg['state_dir'].glob('*.json'))
        for raw in ['示例项目', self.root_id, row['feedback_log_body'], 'sampling_error=', 'server_is_overloaded']:
            self.assertNotIn(raw, stored)

    def test_status_never_run_stale_stopped_and_pid_reuse(self):
        self.assertEqual(watch.status(self.cfg)['status'], 'stopped')
        watch.atomic(self.cfg['state_dir'] / 'status.json', {'pid': os.getpid(), 'birth': watch.birth(os.getpid()), 'last_checked': 1})
        self.assertEqual(watch.status(self.cfg)['status'], 'stopped')
        with watch.lease(self.cfg['state_dir'] / 'watch.lock'):
            self.assertEqual(watch.status(self.cfg)['status'], 'running')
            watch.atomic(self.cfg['state_dir'] / 'status.json', {'pid': os.getpid(), 'birth': 'reused-pid-wrong-birth'})
            self.assertEqual(watch.status(self.cfg)['status'], 'stopped')

    def test_actual_cli_lifecycle_and_cross_process_singleton(self):
        command = [sys.executable, '-X', 'utf8', str(SCRIPT), 'run', '--config', str(self.config)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and watch.status(self.cfg)['status'] != 'running':
                if process.poll() is not None:
                    self.fail('watcher exited before becoming ready')
                time.sleep(0.05)
            self.assertEqual(watch.status(self.cfg)['status'], 'running')
            duplicate, second = self.cli('run')
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(second['status'], 'already_running_or_storage_unavailable')
            stopped, result = self.cli('stop')
            self.assertEqual(stopped.returncode, 0)
            self.assertEqual(result['status'], 'stopped')
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(watch.status(self.cfg)['status'], 'stopped')
            self.assertFalse((self.deploy / 'MeowPush.ps1').exists())
            self.assertFalse((self.deploy / 'meow.config.json').exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)

    def test_cli_check_has_no_send_and_no_live_running_status(self):
        self.append(self.native())
        process, result = self.cli('check')
        self.assertEqual(process.returncode, 0)
        self.assertEqual(result['status'], 'checked')
        self.assertTrue(result['source_ready'])
        self.assertEqual(watch.status(self.cfg)['status'], 'stopped')
        self.assertEqual(self.requests, [])

    def test_invalid_config_cannot_redirect_runtime_state_outside_deploy(self):
        data = json.loads(self.config.read_text(encoding='utf-8'))
        data['state_dir'] = str(self.root / 'unrelated')
        self.config.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            watch.settings(self.config)


if __name__ == '__main__':
    unittest.main(verbosity=2)
