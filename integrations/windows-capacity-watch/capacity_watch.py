"""Read-only native Codex capacity-error monitor. Never logs raw errors or chat."""
import argparse
import contextlib
import ctypes
import hashlib
import importlib.util
import json
import ntpath
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
VERSION = 1
CAPACITY = re.compile(r'(?i)server_is_overloaded|server_overloaded|model_capacity_exceeded|\b(?:model|server)\b.{0,65}\b(?:at capacity|overloaded|capacity limit|high demand)\b|(?:当前模型|模型|服务).{0,25}(?:承载上限|容量上限|容量不足|负载过高)')
NATIVE_PREFIX = re.compile(r'(?i)^(?:stream error:|turn error:|error running turn:|error while processing turn:|response error:|upstream error:|failed to (?:sample|complete|stream|create response):)')
NATIVE_TARGETS = {'codex_core::codex', 'codex_core::client', 'codex_core::stream_events_utils', 'codex_api::endpoint::responses', 'codex_api::endpoint::responses::stream', 'codex_core::responses_retry', 'codex_api::endpoint::responses_websocket'}


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def settings(path):
    path = pathlib.Path(path).resolve()
    if path.stat().st_size > 65536:
        raise ValueError('invalid_settings')
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    for key in ('deploy_dir', 'codex_home', 'state_dir'):
        cfg[key] = pathlib.Path(cfg[key]).expanduser().resolve()
    if cfg['device_label'] not in ('笔记本', '工位'):
        raise ValueError('invalid_device')
    if not cfg['state_dir'].is_relative_to(cfg['deploy_dir'] / '.state'):
        raise ValueError('invalid_state_directory')
    cfg['poll_seconds'] = float(cfg.get('poll_seconds', 2))
    cfg['cooldown_seconds'] = float(cfg.get('cooldown_seconds', 300))
    if not 0.2 <= cfg['poll_seconds'] <= 60 or not 30 <= cfg['cooldown_seconds'] <= 3600:
        raise ValueError('invalid_interval')
    return cfg


@contextlib.contextmanager
def lease(path, blocking=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def birth(pid):
    if os.name != 'nt':
        try:
            return pathlib.Path('/proc', str(pid), 'stat').read_text().split()[21]
        except OSError:
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def status(cfg):
    data, alive = {}, False
    try:
        data = json.loads((cfg['state_dir'] / 'status.json').read_text(encoding='utf-8'))
        alive = bool(data.get('birth')) and birth(data.get('pid', 0)) == data['birth']
    except (OSError, ValueError, TypeError):
        return {'ok': True, 'status': 'stopped'}
    try:
        with lease(cfg['state_dir'] / 'watch.lock'):
            locked = False
    except OSError:
        locked = True
    return {'ok': True, 'status': 'running' if alive and locked else 'stopped', 'version': VERSION,
            'healthy': bool(alive and locked and time.time() - (data.get('last_checked') or 0) < 60),
            'last_checked': data.get('last_checked'), 'source_ready': data.get('source_ready', False),
            'last_outcome': data.get('last_outcome'), 'alerts': data.get('alerts', 0)}


def capacity_row(row):
    """Only trusted native log targets and actual error fields qualify."""
    if row.get('level') not in ('WARN', 'ERROR') or row.get('target') not in NATIVE_TARGETS:
        return False
    body = row.get('feedback_log_body')
    if not isinstance(body, str) or len(body) > 65536:
        return False
    if row['target'] == 'codex_core::responses_retry':
        if ': stream disconnected - retrying sampling request (' not in body or ' sampling_error=' not in body:
            return False
        error = body.rsplit(' sampling_error=', 1)[1]
    else:
        # Native tracing spans precede the real message. Do not inspect span
        # attributes (which can include user-controlled cwd or tool input).
        error = body.rsplit('}: ', 1)[-1]
        if not NATIVE_PREFIX.match(error):
            return False
    return bool(CAPACITY.search(error))


def connect(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute('pragma query_only=on')
    return connection


def root_info(home, thread):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', str(thread)):
        return None
    for path in sorted(home.glob('state_*.sqlite'), reverse=True):
        try:
            with contextlib.closing(connect(path)) as db:
                columns = {row[1] for row in db.execute('pragma table_info(threads)')}
                names = [key for key in ('source', 'thread_source', 'agent_path', 'cwd', 'rollout_path', 'turn_trigger') if key in columns]
                row = db.execute('select ' + ','.join(names) + ' from threads where id=?', (thread,)).fetchone()
                if row:
                    record = dict(row)
                    rollout = pathlib.Path(record.get('rollout_path') or '')
                    if rollout.is_file():
                        with rollout.open(encoding='utf-8') as file:
                            envelope = json.loads(file.readline(65537))
                        if not isinstance(envelope, dict) or not isinstance(envelope.get('payload'), dict):
                            return None
                        meta = envelope['payload']
                        if child(meta) or internal(meta):
                            return None
                    return checked_root(record)
        except (OSError, ValueError, sqlite3.Error):
            continue
    sessions = home / 'sessions'
    if sessions.is_dir():
        for path in sessions.rglob('*' + thread + '*.jsonl'):
            try:
                with path.open(encoding='utf-8') as file:
                    envelope = json.loads(file.readline(65537))
                if not isinstance(envelope, dict) or not isinstance(envelope.get('payload'), dict):
                    continue
                meta = envelope['payload']
                if (meta.get('id') or meta.get('session_id')) == thread:
                    return checked_root(meta)
            except (OSError, ValueError):
                continue
    return None


def child(record):
    source = record.get('source')
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            pass
    kind = str(record.get('thread_source') or '').replace('-', '_')
    child_source = isinstance(source, dict) and 'subagent' in source
    child_source = child_source or isinstance(source, str) and source in ('subagent', 'sub_agent', 'sub-agent')
    return child_source or kind in ('subagent', 'sub_agent') or record.get('agent_path') not in (None, '', '/root', 'root')


def internal(record):
    names = {'thread_title', 'thread_title_generation', 'title_generation', 'thread_description', 'composer_prediction', 'composer_predictions', 'ambient_suggestion', 'ambient_suggestions', 'command_suggestions', 'suggestion_generation', 'followup_suggestions'}
    for key in ('source', 'thread_source', 'turn_trigger', 'turn-trigger', 'client', 'task_type'):
        value = record.get(key)
        if isinstance(value, str) and value.lower().replace('-', '_') in names:
            return True
    return False


def checked_root(record):
    if child(record) or internal(record):
        return None
    kind = record.get('thread_source')
    source = record.get('source')
    if kind and kind not in ('user', 'agent_created_thread'):
        return None
    if kind not in ('user', 'agent_created_thread') and source not in ('vscode', 'cli', 'exec', 'app-server'):
        return None
    project = ntpath.basename(str(record.get('cwd') or '').rstrip('/\\')) or '任务'
    project = re.sub(r'https?\S+|[\x00-\x1f\x7f/\\]', '', project)[:36] or '任务'
    return {'project': project}


def send(cfg, project, key):
    executable = pathlib.Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = [str(executable), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(cfg['deploy_dir'] / 'MeowPush.ps1'),
               '-Title', 'GPT(' + cfg['device_label'] + ')·' + project,
               '-Message', '模型容量不足：当前服务繁忙，任务可能正在自动重试。可稍候查看，或切换其他可用模型。',
               '-Source', 'codex-capacity', '-EventKey', key, '-RetryCount', '1', '-Quiet']
    try:
        result = subprocess.run(command, capture_output=True, timeout=35, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return 'sent' if result.returncode == 0 else 'delivery_unknown'
    except (OSError, subprocess.TimeoutExpired):
        return 'delivery_unknown'


class Watcher:
    def __init__(self, cfg, sender=send, clock=time.time):
        self.cfg, self.sender, self.clock = cfg, sender, clock
        self.directory = cfg['state_dir']
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stop_path = self.directory / 'stop.flag'
        self.path = self.directory / 'cursor.json'
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state.get('version') != VERSION or not all(isinstance(self.state.get(k), dict) for k in ('sources', 'events', 'cooldowns')):
                raise ValueError('state_corrupt')
        else:
            self.state = {'version': VERSION, 'sources': {}, 'events': {}, 'cooldowns': {}}
        self.info = {'pid': os.getpid(), 'birth': birth(os.getpid()), 'last_checked': None, 'source_ready': False, 'last_outcome': None, 'alerts': 0}
        self.first_poll = True
        self.started = self.clock()

    def save(self):
        atomic(self.path, self.state)

    def process(self, row):
        now = self.clock()
        if self.stop_path.exists() or not capacity_row(row):
            return
        try:
            stamp = float(row.get('ts', 0))
        except (TypeError, ValueError):
            return
        if not now - 120 <= stamp <= now + 30:
            return
        info = root_info(self.cfg['codex_home'], row.get('thread_id'))
        if not info:
            return
        scope = hashlib.sha256(str(row['thread_id']).encode()).hexdigest()
        turn = re.search(r'\bturn(?:\.id|_id)=([0-9a-fA-F-]{36})\b', row['feedback_log_body'])
        event = 'turn:' + turn.group(1) if turn else str(row.get('process_uuid', '')) + '|' + str(row['id'])
        key = hashlib.sha256((scope + '|' + event).encode()).hexdigest()
        if key in self.state['events'] or now - float(self.state['cooldowns'].get(scope, 0)) < self.cfg['cooldown_seconds']:
            return
        self.state['events'][key] = {'status': 'reserved', 'at': now}
        self.state['cooldowns'][scope] = now
        self.save()  # A crash/network timeout after this point must not resend.
        if self.stop_path.exists():
            outcome = 'cancelled'
        else:
            try:
                outcome = self.sender(self.cfg, info['project'], key)
            except Exception:
                outcome = 'delivery_unknown'
        if outcome not in ('sent', 'delivery_unknown', 'cancelled'):
            outcome = 'delivery_unknown'
        self.state['events'][key]['status'] = outcome
        self.info['last_outcome'] = outcome
        self.info['alerts'] += int(outcome == 'sent')
        self.save()

    def poll(self):
        candidates = []
        for path in self.cfg['codex_home'].glob('logs_*.sqlite'):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        databases = [path for stamp, path in sorted(candidates, reverse=True)[:4]]
        self.info['source_ready'] = False
        for path in databases:
            key = hashlib.sha256(str(path).encode()).hexdigest()
            try:
                with contextlib.closing(connect(path)) as db:
                    db.execute('begin')
                    db.execute('select id,ts,level,target,feedback_log_body,thread_id,process_uuid from logs limit 0')
                    maximum = db.execute('select coalesce(max(id),0) from logs').fetchone()[0]
                    source = self.state['sources'].get(key)
                    identity = str(path.stat().st_ino)
                    if self.first_poll or source is None or source['last_id'] > maximum or source.get('identity') != identity:
                        # First observation, database replacement or reset: never replay history.
                        source = {'last_id': maximum if self.first_poll else 0, 'identity': identity}
                        self.state['sources'][key] = source
                        self.info['source_ready'] = True
                        if self.first_poll:
                            continue
                    rows = db.execute("select id,ts,level,target,feedback_log_body,thread_id,process_uuid from logs where id>? and id<=? and level in ('ERROR','WARN') order by id limit 200", (source['last_id'], maximum)).fetchall()
                    self.info['source_ready'] = True
                for row in rows:
                    try:
                        eligible = float(row['ts']) >= int(self.started)
                    except (TypeError, ValueError):
                        eligible = False
                    if eligible:
                        self.process(dict(row))
                source['last_id'] = rows[-1]['id'] if len(rows) == 200 else maximum
            except (sqlite3.Error, OSError):
                self.info['last_outcome'] = 'source_unavailable'
        self.first_poll = False
        if len(self.state['events']) > 2000:
            self.state['events'] = dict(sorted(self.state['events'].items(), key=lambda item: item[1]['at'])[-2000:])
        self.state['cooldowns'] = {key: stamp for key, stamp in self.state['cooldowns'].items() if self.clock() - stamp < 86400}
        self.save()
        self.info['last_checked'] = int(self.clock())
        atomic(self.directory / 'status.json', self.info)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'status', 'stop', 'check'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    cfg = settings(args.config)
    if args.action == 'status':
        return status(cfg)
    if args.action == 'stop':
        cfg['state_dir'].mkdir(parents=True, exist_ok=True)
        (cfg['state_dir'] / 'stop.flag').touch()
        deadline = time.monotonic() + 40
        while status(cfg)['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.2)
        return status(cfg)
    try:
        with lease(cfg['state_dir'] / 'watch.lock'):
            watcher = Watcher(cfg)
            if args.action == 'check':
                watcher.poll()
                return {'ok': True, 'status': 'checked', 'source_ready': watcher.info['source_ready']}
            watcher.stop_path.unlink(missing_ok=True)
            watcher.poll()
            while not watcher.stop_path.exists():
                time.sleep(cfg['poll_seconds'])
                watcher.poll()
    except OSError:
        return {'ok': False, 'status': 'already_running_or_storage_unavailable'}
    return {'ok': True, 'status': 'stopped'}


if __name__ == '__main__':
    try:
        result = main()
    except Exception:
        result = {'ok': False, 'status': 'operation_failed'}
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get('ok') else 1)
