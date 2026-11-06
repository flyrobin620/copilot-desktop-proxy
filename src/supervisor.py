"""Own and recover one local proxy worker; never replay model requests."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from diagnostics import EventLog, exception_fields

from paths import ROOT


class Lease:
    """An OS-released file lock; a stale PID file is not proof of ownership."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a+b')
        if self.file.seek(0, 2) == 0:
            self.file.write(b'0')
            self.file.flush()
        self.held = False

    def acquire(self):
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
            return True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            return False

    def close(self):
        # Closing the handle releases the OS lock, including on process exit.
        self.file.close()


@contextmanager
def control_lock(root):
    lease = Lease(root / '.run' / 'control.lock')
    try:
        deadline = time.monotonic() + 35
        while not lease.acquire():
            if time.monotonic() >= deadline:
                raise RuntimeError('Another start or stop operation is still running.')
            time.sleep(.1)
        yield
    finally:
        lease.close()


def is_running(root):
    lease = Lease(root / '.run' / 'supervisor.lock')
    try:
        return not lease.acquire()
    finally:
        lease.close()


def status(root):
    try:
        data = json.loads((root / '.run' / 'supervisor.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    running = is_running(root)
    stopped = (root / '.run' / 'stop').exists()
    return {**data, 'recorded_phase': data.get('phase', 'unknown'),
            'phase': data.get('phase', 'starting') if running else 'stopped' if stopped else 'not_running',
            'running': running, 'state_stale': not running and data.get('phase') not in (None, 'stopped'),
            'stop_requested': stopped}


def hidden_process_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}


@dataclass(frozen=True)
class ProbeResult:
    alive: bool
    reason: str
    stage: str
    status: int | None = None


def probe_result(port, pid, timeout=2):
    """Classify local liveness without retaining response or exception text."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    stage, status = 'connect', None
    try:
        connection.connect()
        stage = 'http'
        connection.request('GET', '/livez')
        response = connection.getresponse()
        status = response.status
        raw = response.read(4097)
        if status not in (200, 503):
            return ProbeResult(False, 'http_status', stage, status)
        if len(raw) > 4096:
            return ProbeResult(False, 'invalid_response', stage, status)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return ProbeResult(False, 'invalid_response', stage, status)
        if data.get('service') != 'copilot-desktop-proxy' or data.get('pid') != pid:
            return ProbeResult(False, 'identity_mismatch', stage, status)
        alive = status == 200 or data.get('status') == 'busy'
        return ProbeResult(alive, 'alive' if alive else 'not_alive', stage, status)
    except TimeoutError:
        return ProbeResult(False, 'timeout', stage, status)
    except OSError as exc:
        refused = stage == 'connect' and (isinstance(exc, ConnectionRefusedError)
            or exc.errno in (errno.ECONNREFUSED, 10061) or getattr(exc, 'winerror', None) == 10061)
        return ProbeResult(False, 'connection_refused' if refused else 'connection_error', stage, status)
    except (ValueError, http.client.HTTPException):
        return ProbeResult(False, 'invalid_response', stage, status)
    finally:
        connection.close()


def probe(port, pid, timeout=2):
    # Keep the boolean API used by local status checks and recovery verification.
    return probe_result(port, pid, timeout).alive


@dataclass(frozen=True)
class Policy:
    check_interval: float = 5
    probe_timeout: float = 5
    failure_threshold: int = 3
    startup_grace: float = 60
    failure_window: float = 60
    initial_backoff: float = 1
    max_backoff: float = 30
    stable_after: float = 60


class Supervisor:
    def __init__(self, root, port=8787, effort='xhigh', policy=None):
        self.root, self.port, self.effort = root, port, effort
        self.policy = policy or Policy()
        self.stop_file = root / '.run' / 'stop'
        self.worker = None
        self.restart_count = 0
        self.logger = None

    def event(self, event, **fields):
        if self.logger:
            self.logger.event(event, **fields)

    def write_state(self, phase, **fields):
        data = {'supervisor_pid': os.getpid(), 'worker_pid': self.worker.pid if self.worker else None,
                'port': self.port, 'phase': phase, 'restart_count': self.restart_count,
                'updated_at': datetime.now(timezone.utc).isoformat(), **fields}
        path = self.root / '.run' / 'supervisor.json'
        temporary = path.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps(data), encoding='utf-8')
            for attempt in range(4):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    # A concurrent Windows reader may temporarily deny delete-sharing.
                    time.sleep(.025 * (attempt + 1))
        except OSError as exc:
            # Antivirus/file contention or a full disk must not kill the watchdog.
            self.event('state_write_failed', **exception_fields(exc))

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop_file.exists() and time.monotonic() < deadline:
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
        return self.stop_file.exists()

    def stop_worker(self):
        # Popen retains the handle of the child we created; never kill a PID from disk.
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(timeout=5)
        path = self.root / 'server.pid'
        try:
            if self.worker and path.exists() and path.read_text().strip() == str(self.worker.pid):
                path.unlink()
        except OSError as exc:
            self.event('pid_cleanup_failed', **exception_fields(exc))

    def spawn_worker(self):
        handles = []
        try:
            for name in ('server.stdout.log', 'server.stderr.log'):
                path = self.root / name
                if path.exists() and path.stat().st_size > 1024 * 1024:
                    os.replace(path, path.with_suffix('.log.1'))
                handles.append(path.open('ab'))
            env = {**os.environ, 'COPILOT_PROXY_SUPERVISOR_PID': str(os.getpid())}
            self.worker = subprocess.Popen(
                [sys.executable, '-B', '-u', str(self.root / 'src' / 'server.py'), '--port', str(self.port),
                 '--default-effort', self.effort], cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                stdout=handles[0], stderr=handles[1], **hidden_process_options())
            (self.root / 'server.pid').write_text(str(self.worker.pid), encoding='ascii')
            self.write_state('starting')
            self.event('worker_started', worker_pid=self.worker.pid)
        finally:
            for handle in handles:
                handle.close()

    def refresh_models(self):
        try:
            result = subprocess.run([sys.executable, '-B', str(self.root / 'src' / 'refresh_models.py'),
                                     '--port', str(self.port)], cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=15, **hidden_process_options())
            self.event('model_refresh', status=result.returncode)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.event('model_refresh_failed', **exception_fields(exc))

    def run(self):
        lease = Lease(self.root / '.run' / 'supervisor.lock')
        try:
            if not lease.acquire():
                return False
            self.logger = EventLog(self.root / 'supervisor.log', max_bytes=1024*1024, backups=3)
            self.event('supervisor_started', port=self.port,
                       probe_timeout_seconds=self.policy.probe_timeout,
                       startup_grace_seconds=self.policy.startup_grace,
                       liveness_failure_window_seconds=self.policy.failure_window)
            self.write_state('starting')
            backoff = self.policy.initial_backoff
            while not self.stop_file.exists():
                healthy_since = None
                failed_since = None
                failures = 0
                refused_failures = 0
                reason = 'worker_exited'
                exit_code = None
                try:
                    self.spawn_worker()
                    # Process creation / state writes can be slow on a busy host.
                    # Give the new worker its full grace period after spawning.
                    started = time.monotonic()
                    next_check = started
                    refreshed = False
                    next_heartbeat = 0
                    while not self.stop_file.exists() and self.worker.poll() is None:
                        now = time.monotonic()
                        if now >= next_heartbeat:
                            self.event('supervisor_heartbeat', worker_pid=self.worker.pid,
                                       restart_count=self.restart_count)
                            self.write_state('running' if refreshed else 'starting')
                            next_heartbeat = now + 30
                        if now >= next_check:
                            probe_started = time.monotonic()
                            result = probe_result(self.port, self.worker.pid, self.policy.probe_timeout)
                            checked_at = time.monotonic()
                            next_check = checked_at + self.policy.check_interval
                            # The owned child can exit while HTTP is being checked.
                            # Recheck its handle before treating failure as a live hang.
                            exit_code = self.worker.poll()
                            if exit_code is not None:
                                if not result.alive:
                                    self.event('probe_failed', worker_pid=self.worker.pid,
                                               reason='worker_exited', stage=result.stage,
                                               worker_alive=False, exit_code=exit_code,
                                               duration_ms=round((checked_at-probe_started)*1000))
                                break
                            if result.alive:
                                failures = 0
                                refused_failures = 0
                                failed_since = None
                                healthy_since = checked_at if healthy_since is None else healthy_since
                                if not refreshed:
                                    self.write_state('running')
                                    self.refresh_models()
                                    refreshed = True
                            else:
                                healthy_since = None
                                in_grace = not refreshed and checked_at - started < self.policy.startup_grace
                                if not in_grace:
                                    failed_since = checked_at if failed_since is None else failed_since
                                    failures += 1
                                    refused_failures = refused_failures + 1 if result.reason == 'connection_refused' else 0
                                unresponsive = 0 if failed_since is None else checked_at - failed_since
                                self.event('probe_failed', worker_pid=self.worker.pid, failures=failures,
                                           phase='running' if refreshed else 'starting',
                                           reason=result.reason, stage=result.stage, status=result.status,
                                           worker_alive=True, in_startup_grace=in_grace,
                                           refused_failures=refused_failures,
                                           duration_ms=round((checked_at-probe_started)*1000),
                                           liveness_failure_seconds=round(unresponsive, 3))
                                # Repeated TCP refusals recover sooner, but never consume
                                # startup grace or accumulate across other failure kinds.
                                if not in_grace and refused_failures >= self.policy.failure_threshold:
                                    reason = 'listener_refused'
                                    break
                                # A few slow localhost responses under CPU pressure
                                # must not tear down an otherwise productive worker.
                                # Confirm persistent failure; a successful probe resets it.
                                if (not in_grace and failures >= self.policy.failure_threshold
                                        and unresponsive >= self.policy.failure_window):
                                    reason = 'liveness_failed'
                                    break
                        self.wait(.2)
                    exit_code = self.worker.poll()
                except OSError as exc:
                    reason = 'worker_error_' + type(exc).__name__
                    self.event('worker_error', **exception_fields(exc))
                finally:
                    self.stop_worker()
                if self.stop_file.exists():
                    break
                if healthy_since is not None and time.monotonic() - healthy_since >= self.policy.stable_after:
                    backoff = self.policy.initial_backoff
                self.restart_count += 1
                self.event('recover', reason=reason, delay=backoff, exit_code=exit_code,
                           worker_pid=self.worker.pid if self.worker else 0, restart_count=self.restart_count)
                self.write_state('backoff', reason=reason, retry_seconds=backoff, exit_code=exit_code)
                if self.wait(backoff):
                    break
                backoff = min(backoff * 2, self.policy.max_backoff)
            return True
        except Exception as exc:
            self.event('supervisor_fatal', **exception_fields(exc))
            raise
        finally:
            try:
                if lease.held:
                    self.stop_worker()
                    self.write_state('stopped' if self.stop_file.exists() else 'exited')
                if self.logger:
                    self.event('supervisor_stopped', stop_requested=self.stop_file.exists())
            finally:
                if self.logger:
                    self.logger.close()
                lease.close()


def prepare(root, port=8787, effort='xhigh'):
    """Explicit user start arms OS recovery; scheduler runs must never clear a stop."""
    with control_lock(root):
        current = status(root)
        if current['running'] and current.get('port') != port:
            raise RuntimeError('This folder already supervises another port. Stop it first.')
        selected = {'port': port, 'effort': effort}
        if os.environ.get('COPILOT_PROXY_DB'):
            selected['database'] = os.environ['COPILOT_PROXY_DB']
        path = root / '.run' / 'recovery-options.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(selected), encoding='utf-8')
        os.replace(temporary, path)
        (root / '.run' / 'stop').unlink(missing_ok=True)
    return status(root)


def scheduled_run(root):
    # Stop's tombstone wins, including when a scheduled launch races with Stop Proxy.
    if (root / '.run' / 'stop').exists():
        return
    path = root / '.run' / 'recovery-options.json'
    if not path.exists():
        return
    selected = json.loads(path.read_text(encoding='utf-8'))
    port, effort = selected.get('port'), selected.get('effort')
    if type(port) is not int or not 1 <= port <= 65535 or effort not in ('none', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('Invalid recovery options.')
    if selected.get('database'):
        os.environ['COPILOT_PROXY_DB'] = selected['database']
    else:
        os.environ.pop('COPILOT_PROXY_DB', None)
    Supervisor(root, port, effort).run()


def start(root, port=8787, effort='xhigh'):
    with control_lock(root):
        current = status(root)
        if current['running']:
            if current.get('port') != port:
                raise RuntimeError('This folder already supervises another port. Stop it first.')
            return current
        # Refuse port conflicts instead of repeatedly spawning workers or killing an unrelated owner.
        with socket.socket() as sock:
            if os.name == 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                raise RuntimeError('The proxy port is already in use. Stop the owning copy first.') from None
        (root / '.run' / 'stop').unlink(missing_ok=True)
        process = subprocess.Popen([sys.executable, '-B', str(root / 'src' / 'supervisor.py'), 'run',
                                    '--port', str(port), '--effort', effort], cwd=root,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **hidden_process_options())
        for _ in range(50):
            current = status(root)
            if current['running'] and current.get('supervisor_pid') == process.pid:
                return current
            if process.poll() is not None:
                break
            time.sleep(.1)
        raise RuntimeError('The watchdog did not start. Check supervisor.log and folder permissions.')


def stop(root, timeout=25):
    with control_lock(root):
        (root / '.run' / 'stop').touch()
        deadline = time.monotonic() + timeout
        while is_running(root):
            if time.monotonic() >= deadline:
                raise RuntimeError('The watchdog did not stop in time; the stop request remains active.')
            time.sleep(.1)
    return status(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'status', 'run', 'prepare'])
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--effort', choices=['none', 'low', 'medium', 'high', 'xhigh', 'max'], default='xhigh')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    try:
        if args.action == 'run':
            if args.scheduled:
                scheduled_run(ROOT)
            else:
                Supervisor(ROOT, args.port, args.effort).run()
            return
        result = (start(ROOT, args.port, args.effort) if args.action == 'start' else
                  prepare(ROOT, args.port, args.effort) if args.action == 'prepare' else
                  stop(ROOT) if args.action == 'stop' else status(ROOT))
        print(json.dumps(result))
    except Exception as exc:
        log = EventLog(ROOT / 'recovery.log', max_bytes=256*1024, backups=2)
        log.event('recovery_entry_failed', **exception_fields(exc))
        log.close()
        print(json.dumps({'error': type(exc).__name__}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
