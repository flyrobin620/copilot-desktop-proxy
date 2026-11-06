"""Explicit Windows integration test. Creates/removes one isolated per-user task; no upstream."""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

from supervisor import hidden_process_options, prepare, probe, status, stop

ROOT = Path(__file__).resolve().parent
WORKER = '''
import argparse,json,os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from parent_lifetime import watch_parent
from diagnostics import EventLog
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('--port',type=int)
parser.add_argument('--default-effort')
args=parser.parse_args()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        raw=json.dumps({'service':'copilot-desktop-proxy','pid':os.getpid(),'status':'alive'}).encode()
        self.send_response(200)
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
log=EventLog(Path(__file__).parent/'proxy.log')
watch=watch_parent(int(os.environ['COPILOT_PROXY_SUPERVISOR_PID']),server,log)
try: server.serve_forever()
finally:
    watch[0].set()
    watch[1].join(timeout=2)
    server.server_close()
    log.close()
'''


def task(root, action):
    result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                             '-ExecutionPolicy', 'Bypass', '-File', str(root/'recovery-task.ps1'),
                             '-Action', action, '-Python', sys.executable],
                            capture_output=True, timeout=35, **hidden_process_options())
    data = json.loads(result.stdout)
    if result.returncode:
        raise RuntimeError('Recovery task operation failed: ' + json.dumps(data))
    return data


class ProcessHandle:
    def __init__(self, pid):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x00100001, False, pid)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self):
        if not self.kernel.TerminateProcess(self.handle, 23):
            raise ctypes.WinError(ctypes.get_last_error())

    def exited(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 0

    def close(self):
        self.kernel.CloseHandle(self.handle)


def wait_ready(root, port, previous=None, timeout=100):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status(root)
        if current['running'] and current.get('supervisor_pid') != previous and current.get('worker_pid'):
            if probe(port, current['worker_pid'], .3): return current
        time.sleep(.2)
    raise AssertionError('Recovery deadline exceeded: ' + json.dumps(status(root)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='Existing isolated fixture directory from an earlier incomplete test.')
    args = parser.parse_args()
    if os.name != 'nt': raise SystemExit('Windows is required.')
    root = args.root.resolve() if args.root else ROOT/'.qa'/('Windows recovery '+uuid.uuid4().hex)
    qa = (ROOT/'.qa').resolve()
    if not root.is_relative_to(qa) or root == qa:
        raise SystemExit('The fixture must be inside this package .qa directory.')
    root.mkdir(parents=True, exist_ok=True)
    # Only known fixture files are written, never a production proxy folder.
    for name in ('supervisor.py', 'diagnostics.py', 'parent_lifetime.py', 'recovery-task.ps1'):
        shutil.copyfile(ROOT/name, root/name)
    (root/'server.py').write_text(WORKER, encoding='utf-8')
    (root/'refresh_models.py').write_text('raise SystemExit(0)\n')
    print(json.dumps({'fixture': str(root)}), flush=True)
    handles = []
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        prepare(root, port)
        result = task(root, 'Enable')
        assert result['task_owned'] and result['task_enabled']
        first = wait_ready(root, port)
        print(json.dumps({'test':'scheduler_started_watchdog','passed':True}), flush=True)
        for mode in ('both_processes', 'watchdog_only'):
            current = status(root)
            assert current['running'] and probe(port, current['worker_pid'], .3)
            worker, supervisor = ProcessHandle(current['worker_pid']), ProcessHandle(current['supervisor_pid'])
            handles.extend((worker, supervisor))
            started = time.monotonic()
            supervisor.terminate()
            if mode == 'both_processes': worker.terminate()
            recovered = wait_ready(root, port, previous=current['supervisor_pid'])
            assert worker.exited(), 'Orphan worker still owns the port'
            assert recovered['worker_pid'] != current['worker_pid']
            print(json.dumps({'test':mode,'recovery_seconds':round(time.monotonic()-started,2),'passed':True}), flush=True)
        stop(root)
        disabled = task(root, 'Disable')
        assert disabled['task_enabled'] is False
        # Exercise a scheduler launch racing with a stop; it must retain the tombstone.
        process = subprocess.run([sys.executable, '-B', str(root/'supervisor.py'), 'run', '--scheduled'],
                                 capture_output=True, timeout=8, **hidden_process_options())
        assert process.returncode == 0 and not status(root)['running'] and status(root)['stop_requested']
        print(json.dumps({'test':'intentional_stop_stays_stopped','passed':True}), flush=True)
    finally:
        stop(root)
        task(root, 'Remove')
        for handle in handles: handle.close()
    print(json.dumps({'result':'PASS','finished_at':datetime.now(timezone.utc).isoformat()}), flush=True)


if __name__ == '__main__':
    main()
