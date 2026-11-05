"""Real child-process recovery tests without accounts or upstream requests."""
import errno
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from diagnostics import safe_record
from supervisor import Policy, ProbeResult, Supervisor, is_running, probe, probe_result, start, status, stop, hidden_process_options, prepare, scheduled_run

WORKER = '''
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('--port',type=int)
parser.add_argument('--default-effort')
args=parser.parse_args()
root=Path(__file__).parent
count=root/'boots'
boot=int(count.read_text())+1 if count.exists() else 1
count.write_text(str(boot))
mode=(root/'mode').read_text() if (root/'mode').exists() else ''
if mode=='exit': sys.exit(2)
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        unavailable=self.path=='/health' or (mode=='hang-once' and boot==1)
        data={'service':'copilot-desktop-proxy','pid':os.getpid(),'status':'authorization_expired' if unavailable else 'alive'}
        raw=json.dumps(data).encode()
        self.send_response(503 if unavailable else 200)
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
'''


class ProbeTests(unittest.TestCase):
    def test_only_matching_worker_success_or_busy_is_alive(self):
        cases = [(200, 'copilot-desktop-proxy', 123, 'alive', True, 'alive'),
                 (503, 'copilot-desktop-proxy', 123, 'busy', True, 'alive'),
                 (503, 'copilot-desktop-proxy', 123, 'unavailable', False, 'not_alive'),
                 (200, 'other', 123, 'alive', False, 'identity_mismatch'),
                 (200, 'copilot-desktop-proxy', 456, 'alive', False, 'identity_mismatch'),
                 (500, 'copilot-desktop-proxy', 123, 'alive', False, 'http_status')]
        for status_code, service, pid, state, alive, reason in cases:
            with self.subTest(status=status_code, service=service, pid=pid, state=state), \
                 patch('supervisor.http.client.HTTPConnection') as connection:
                response = connection.return_value.getresponse.return_value
                response.status = status_code
                response.read.return_value = json.dumps({'service': service, 'pid': pid, 'status': state}).encode()
                self.assertEqual(probe_result(8787, 123), ProbeResult(alive, reason, 'http', status_code))
                self.assertIs(probe(8787, 123), alive)
                response.read.assert_called_with(4097)
                self.assertEqual(connection.return_value.close.call_count, 2)

    def test_malformed_local_responses_are_not_treated_as_refusals(self):
        for raw in (b'PRIVATE_RESPONSE', b'[]', b'x' * 4097):
            with self.subTest(length=len(raw)), patch('supervisor.http.client.HTTPConnection') as connection:
                response = connection.return_value.getresponse.return_value
                response.status, response.read.return_value = 200, raw
                result = probe_result(8787, 123)
                self.assertEqual(result, ProbeResult(False, 'invalid_response', 'http', 200))
                self.assertNotIn('PRIVATE', repr(result))
                connection.return_value.close.assert_called_once()

    def test_errors_are_classified_by_stage_without_exception_text(self):
        windows_refused = OSError('PRIVATE_ERROR')
        windows_refused.winerror = 10061
        cases = [('connect', ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE_ERROR'), 'connection_refused'),
                 ('connect', OSError(10061, 'PRIVATE_ERROR'), 'connection_refused'),
                 ('connect', windows_refused, 'connection_refused'),
                 ('connect', TimeoutError('PRIVATE_ERROR'), 'timeout'),
                 ('getresponse', TimeoutError('PRIVATE_ERROR'), 'timeout'),
                 ('connect', PermissionError(errno.EACCES, 'PRIVATE_ERROR'), 'connection_error'),
                 ('getresponse', ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE_ERROR'), 'connection_error'),
                 ('getresponse', http.client.HTTPException('PRIVATE_ERROR'), 'invalid_response')]
        for method, error, reason in cases:
            with self.subTest(method=method, reason=reason), patch('supervisor.http.client.HTTPConnection') as connection:
                getattr(connection.return_value, method).side_effect = error
                result = probe_result(8787, 123)
                self.assertEqual(result, ProbeResult(False, reason, 'connect' if method == 'connect' else 'http'))
                self.assertNotIn('PRIVATE', repr(result))
                connection.return_value.close.assert_called_once()

    def test_closed_loopback_listener_reports_refusal(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            result = probe_result(sock.getsockname()[1], 123, Policy().probe_timeout)
        self.assertEqual(result, ProbeResult(False, 'connection_refused', 'connect'))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='proxy watchdog ')
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'server.py').write_text(WORKER)
        (self.root / 'refresh_models.py').write_text(
            "from pathlib import Path\np=Path(__file__).parent/'refreshes'\np.write_text(str(int(p.read_text())+1 if p.exists() else 1))\n")
        shutil.copyfile(Path(__file__).parent / 'supervisor.py', self.root / 'supervisor.py')
        shutil.copyfile(Path(__file__).parent / 'diagnostics.py', self.root / 'diagnostics.py')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.supervisor = Supervisor(self.root, self.port, policy=Policy(
            check_interval=.1, probe_timeout=.1, startup_grace=.2,
            failure_window=.2,
            initial_backoff=.1, max_backoff=.4, stable_after=.5))
        self.errors, self.thread = [], None

    def tearDown(self):
        stop(self.root, timeout=8)
        if self.thread:
            self.thread.join(timeout=8)
            self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])
        self.temporary.cleanup()

    def wait_for(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition(): return
            if self.errors: raise self.errors[0]
            time.sleep(.05)
        self.fail('Timed out waiting for watchdog state: ' + str(status(self.root)))

    def run_supervisor(self):
        def run():
            try: self.supervisor.run()
            except BaseException as exc: self.errors.append(exc)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def simulate_probes(self, *, startup_grace, failure_window, spawn_delay, stop_at, is_alive,
                        check_interval=1, failure_threshold=2, probe_duration=0, exit_on_failure=False):
        # Deterministic scheduling stalls: never saturate the real machine or
        # launch/terminate a real worker to test timeout accounting.
        clock = [0.0]
        boots, events = [], []
        self.supervisor.stop_file.unlink(missing_ok=True)
        self.supervisor.restart_count = 0
        self.supervisor.policy = Policy(check_interval=check_interval, probe_timeout=.1,
            startup_grace=startup_grace, failure_threshold=failure_threshold, failure_window=failure_window,
            initial_backoff=.1, max_backoff=.2, stable_after=1)

        def spawn():
            clock[0] += spawn_delay
            boots.append(clock[0])
            worker = SimpleNamespace(pid=len(boots), returncode=None)
            worker.poll = lambda: worker.returncode
            self.supervisor.worker = worker

        def check(*_):
            value = is_alive(clock[0], len(boots))
            result = value if isinstance(value, ProbeResult) else ProbeResult(value, 'alive' if value else 'timeout', 'http')
            clock[0] += probe_duration
            if exit_on_failure and not result.alive:
                self.supervisor.worker.returncode = 2
            return result

        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= stop_at:
                self.supervisor.stop_file.touch()
            return self.supervisor.stop_file.exists()

        with patch('supervisor.time.monotonic', side_effect=lambda: clock[0]), \
             patch('supervisor.probe_result', side_effect=check), \
             patch.object(self.supervisor, 'spawn_worker', side_effect=spawn), \
             patch.object(self.supervisor, 'stop_worker'), \
             patch.object(self.supervisor, 'refresh_models'), \
             patch.object(self.supervisor, 'wait', side_effect=wait), \
             patch.object(self.supervisor, 'event', side_effect=lambda event, **fields: events.append(
                 {'at': clock[0], 'event': event, **fields})):
            self.assertTrue(self.supervisor.run())
        return boots, events

    def test_slow_process_creation_does_not_consume_the_workers_startup_grace(self):
        boots, events = self.simulate_probes(startup_grace=5, failure_window=2,
            spawn_delay=10, stop_at=16, is_alive=lambda now, boot: now >= 14)
        self.assertEqual(len(boots), 1)
        failures = [event for event in events if event['event'] == 'probe_failed']
        self.assertTrue(failures)
        self.assertTrue(all(event['in_startup_grace'] and event['failures'] == 0 for event in failures))
        self.assertFalse(any(event['event'] == 'recover' for event in events))

    def test_transient_probe_failures_reset_without_restarting_a_live_worker(self):
        boots, events = self.simulate_probes(startup_grace=0, failure_window=5,
            spawn_delay=0, stop_at=15, is_alive=lambda now, boot: not (2 <= now < 6 or 8 <= now < 11))
        self.assertEqual(len(boots), 1)
        failures = [event for event in events if event['event'] == 'probe_failed']
        self.assertGreaterEqual(len(failures), 4)
        self.assertGreaterEqual(sum(event['failures'] == 1 for event in failures), 2)
        self.assertFalse(any(event['event'] == 'recover' for event in events))
        self.assertTrue(all(event['phase'] == 'running' and 'duration_ms' in event for event in failures))

    def test_persistent_probe_failure_still_recovers_after_the_confirmation_window(self):
        boots, events = self.simulate_probes(startup_grace=0, failure_window=4,
            spawn_delay=0, stop_at=10, is_alive=lambda now, boot: boot > 1 or now < 2)
        self.assertEqual(len(boots), 2)
        recovery = [event for event in events if event['event'] == 'recover']
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]['reason'], 'liveness_failed')
        failures = [event for event in events if event['event'] == 'probe_failed']
        self.assertGreaterEqual(recovery[0]['at'] - failures[0]['at'], 4)
        self.assertLess(recovery[0]['at'] - failures[0]['at'], 6)

    def test_running_worker_refusals_recover_on_third_check_without_waiting_sixty_seconds(self):
        refused = ProbeResult(False, 'connection_refused', 'connect')
        boots, events = self.simulate_probes(startup_grace=60, failure_window=60,
            spawn_delay=0, stop_at=22, check_interval=5, failure_threshold=3,
            is_alive=lambda now, boot: True if boot > 1 or now < 1 else refused)
        self.assertEqual(len(boots), 2)
        failures = [event for event in events if event['event'] == 'probe_failed']
        recovery = [event for event in events if event['event'] == 'recover']
        self.assertEqual(len(failures), 3)
        self.assertEqual([event['refused_failures'] for event in failures], [1, 2, 3])
        self.assertTrue(all(event['worker_alive'] and not event['in_startup_grace'] for event in failures))
        self.assertEqual(recovery[0]['reason'], 'listener_refused')
        self.assertGreaterEqual(recovery[0]['at'] - failures[0]['at'], 10)
        self.assertLess(recovery[0]['at'] - failures[0]['at'], 11)
        self.assertLess(recovery[0]['at'], 16)
        for event in failures:
            fields = {key: value for key, value in event.items() if key != 'at' and value is not None}
            self.assertEqual(safe_record(fields), fields)

    def test_refusals_do_not_consume_startup_grace_or_terminate_a_slow_start(self):
        refused = ProbeResult(False, 'connection_refused', 'connect')
        boots, events = self.simulate_probes(startup_grace=60, failure_window=60,
            spawn_delay=10, stop_at=85, check_interval=5, failure_threshold=3,
            is_alive=lambda now, boot: True if now >= 73 else refused)
        self.assertEqual(len(boots), 1)
        failures = [event for event in events if event['event'] == 'probe_failed']
        self.assertTrue(any(event['at'] >= 70 and event['failures'] == 1 for event in failures))
        self.assertTrue(all(event['failures'] == 0 for event in failures if event['at'] < 70))
        self.assertFalse(any(event['event'] == 'recover' for event in events))

    def test_permanent_startup_refusals_recover_after_grace_and_three_checks(self):
        refused = ProbeResult(False, 'connection_refused', 'connect')
        boots, events = self.simulate_probes(startup_grace=60, failure_window=60,
            spawn_delay=0, stop_at=80, check_interval=5, failure_threshold=3,
            is_alive=lambda now, boot: True if boot > 1 else refused)
        self.assertEqual(len(boots), 2)
        recovery = next(event for event in events if event['event'] == 'recover')
        self.assertEqual(recovery['reason'], 'listener_refused')
        self.assertGreaterEqual(recovery['at'], 70)
        self.assertLess(recovery['at'], 72)

    def test_other_failure_or_success_resets_the_fast_refusal_streak(self):
        refused = ProbeResult(False, 'connection_refused', 'connect')
        for interruption in (True, ProbeResult(False, 'timeout', 'http'),
                             ProbeResult(False, 'identity_mismatch', 'http', 200)):
            with self.subTest(interruption=interruption):
                calls = [0]
                def check(now, boot):
                    calls[0] += 1
                    return interruption if calls[0] % 3 == 0 else refused
                boots, events = self.simulate_probes(startup_grace=0, failure_window=60,
                    spawn_delay=0, stop_at=16, check_interval=1, failure_threshold=3,
                    is_alive=check)
                self.assertEqual(len(boots), 1)
                self.assertFalse(any(event['event'] == 'recover' for event in events))
                self.assertLessEqual(max(event['refused_failures'] for event in events
                                        if event['event'] == 'probe_failed'), 2)

    def test_timeouts_keep_sixty_second_tolerance_and_mixed_failures_remain_bounded(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                calls = [0]
                def check(now, boot):
                    if boot > 1 or now < 1:
                        return True
                    calls[0] += 1
                    return ProbeResult(False, 'connection_refused' if mixed and calls[0] % 2 == 0 else 'timeout', 'connect')
                boots, events = self.simulate_probes(startup_grace=60, failure_window=60,
                    spawn_delay=0, stop_at=90, check_interval=5, failure_threshold=3,
                    probe_duration=5, is_alive=check)
                self.assertEqual(len(boots), 2)
                failures = [event for event in events if event['event'] == 'probe_failed']
                recovery = next(event for event in events if event['event'] == 'recover')
                self.assertEqual(recovery['reason'], 'liveness_failed')
                self.assertGreaterEqual(recovery['at'] - failures[0]['at'], 60)
                self.assertLess(recovery['at'] - failures[0]['at'], 71)

    def test_exit_during_failed_probe_recovers_without_liveness_window(self):
        boots, events = self.simulate_probes(startup_grace=60, failure_window=60,
            spawn_delay=0, stop_at=12, probe_duration=5, exit_on_failure=True,
            is_alive=lambda now, boot: boot > 1)
        self.assertEqual(len(boots), 2)
        failure = next(event for event in events if event['event'] == 'probe_failed')
        recovery = next(event for event in events if event['event'] == 'recover')
        self.assertFalse(failure['worker_alive'])
        self.assertEqual(failure['exit_code'], 2)
        self.assertEqual(recovery['reason'], 'worker_exited')
        self.assertEqual(recovery['at'], 5)
        self.assertEqual(recovery['exit_code'], 2)

    def test_crash_recovers_and_refreshes_without_changing_history(self):
        history = self.root / '.data' / 'history-fixture'
        history.parent.mkdir()
        history.write_bytes(b'existing usage history')
        self.run_supervisor()
        self.wait_for(lambda: self.supervisor.worker and probe(self.port, self.supervisor.worker.pid, .1))
        self.wait_for(lambda: (self.root / 'refreshes').exists() and (self.root / 'refreshes').read_text() == '1')
        worker = self.supervisor.worker
        worker.terminate()  # Exact Popen handle created by this isolated test.
        self.wait_for(lambda: self.supervisor.worker.pid != worker.pid and probe(self.port, self.supervisor.worker.pid, .1))
        self.wait_for(lambda: int((self.root / 'refreshes').read_text()) == 2)
        self.assertEqual(history.read_bytes(), b'existing usage history')
        self.assertEqual(self.supervisor.restart_count, 1)

    def test_repeated_liveness_failure_recovers(self):
        (self.root / 'mode').write_text('hang-once')
        self.run_supervisor()
        self.wait_for(lambda: (self.root / 'boots').exists() and int((self.root / 'boots').read_text()) >= 2
                      and self.supervisor.worker and probe(self.port, self.supervisor.worker.pid, .1))
        self.assertIn('liveness_failed', (self.root / 'supervisor.log').read_text())

    def test_auth_failure_does_not_restart_and_intentional_stop_stays_stopped(self):
        self.run_supervisor()
        self.wait_for(lambda: self.supervisor.worker and probe(self.port, self.supervisor.worker.pid, .1))
        worker = self.supervisor.worker
        time.sleep(.7)  # The fixture always returns authorization_expired for /health.
        self.assertEqual(self.supervisor.worker.pid, worker.pid)
        self.assertFalse(Supervisor(self.root, self.port).run())
        stop(self.root, timeout=8)
        self.thread.join(timeout=8)
        time.sleep(.5)
        self.assertFalse(is_running(self.root))
        self.assertIsNotNone(worker.poll())
        self.assertFalse((self.root / 'server.pid').exists())
        self.assertEqual((self.root / 'boots').read_text(), '1')

    def test_startup_failure_backs_off_and_stop_cancels_retry(self):
        (self.root / 'mode').write_text('exit')
        self.run_supervisor()
        self.wait_for(lambda: self.supervisor.restart_count >= 3)
        stop(self.root, timeout=8)
        self.thread.join(timeout=8)
        boots = (self.root / 'boots').read_text()
        time.sleep(.6)
        self.assertEqual((self.root / 'boots').read_text(), boots)
        log = (self.root / 'supervisor.log').read_text()
        self.assertIn('"delay": 0.1', log)
        self.assertIn('"delay": 0.2', log)
        self.assertIn('"delay": 0.4', log)
        self.assertIn('"exit_code": 2', log)

    def test_port_conflict_does_not_start_a_worker(self):
        with socket.socket() as sock:
            if os.name == 'nt': sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            with self.assertRaisesRegex(RuntimeError, 'already in use'):
                start(self.root, self.port)
        self.assertFalse(is_running(self.root))
        self.assertFalse((self.root / 'boots').exists())

    def test_state_file_write_failure_does_not_kill_supervision(self):
        self.run_supervisor()
        self.wait_for(lambda: self.supervisor.worker and probe(self.port, self.supervisor.worker.pid, .1))
        with patch('supervisor.os.replace', side_effect=PermissionError('fixture')):
            self.supervisor.write_state('running')
        self.assertTrue(self.thread.is_alive())
        self.assertTrue(probe(self.port, self.supervisor.worker.pid, .1))
        self.assertIn('state_write_failed', (self.root / 'supervisor.log').read_text())

    def test_concurrent_start_commands_share_one_detached_watchdog(self):
        command = [sys.executable, '-B', str(self.root / 'supervisor.py'), 'start', '--port', str(self.port)]
        starters = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     **hidden_process_options()) for _ in range(2)]
        results = []
        for process in starters:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            results.append(json.loads(stdout))
        self.assertEqual(results[0]['supervisor_pid'], results[1]['supervisor_pid'])
        self.wait_for(lambda: status(self.root).get('worker_pid') and probe(self.port, status(self.root)['worker_pid'], .1))
        self.assertEqual((self.root / 'boots').read_text(), '1')
        stop(self.root, timeout=8)
        self.assertFalse(is_running(self.root))

    def test_scheduled_start_never_clears_an_intentional_stop(self):
        prepare(self.root, self.port)
        self.assertTrue((self.root / '.run' / 'recovery-options.json').exists())
        stop(self.root)
        scheduled_run(self.root)
        self.assertFalse(is_running(self.root))
        self.assertFalse((self.root / 'boots').exists())
        self.assertTrue(status(self.root)['stop_requested'])
        self.assertEqual(status(self.root)['phase'], 'stopped')

    def test_stale_running_state_is_not_reported_as_running(self):
        (self.root / '.run').mkdir(exist_ok=True)
        self.supervisor.write_state('running')
        current = status(self.root)
        self.assertEqual(current['phase'], 'not_running')
        self.assertEqual(current['recorded_phase'], 'running')
        self.assertTrue(current['state_stale'])
        self.assertFalse(current['running'])

    def test_scheduled_configuration_failure_is_logged_without_private_text(self):
        (self.root / '.run').mkdir(exist_ok=True)
        (self.root / '.run' / 'recovery-options.json').write_text('PRIVATE_INVALID_OPTIONS')
        result = subprocess.run([sys.executable, '-B', str(self.root / 'supervisor.py'), 'run', '--scheduled'],
                                capture_output=True, timeout=5, **hidden_process_options())
        self.assertEqual(result.returncode, 1)
        log = (self.root / 'recovery.log').read_text()
        self.assertIn('recovery_entry_failed', log)
        self.assertNotIn('PRIVATE_', log)
        self.assertNotIn(b'PRIVATE_', result.stdout)

    def test_transient_state_file_access_denied_recovers(self):
        (self.root / '.run').mkdir(exist_ok=True)
        replace = os.replace
        attempts = []
        def transient(source, target):
            attempts.append(True)
            if len(attempts) == 1:
                raise PermissionError('simulated sharing violation')
            return replace(source, target)
        with patch('supervisor.os.replace', side_effect=transient):
            self.supervisor.write_state('running')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(status(self.root)['recorded_phase'], 'running')


if __name__ == '__main__':
    unittest.main()
