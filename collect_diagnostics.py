"""Export only structured metadata; works without a running proxy or Copilot login."""
from datetime import datetime, timedelta, timezone
import ctypes
import http.client
import json
from pathlib import Path
import platform
import re
import subprocess
import uuid
import zipfile

from diagnostics import safe_record
from supervisor import status, hidden_process_options

ROOT = Path(__file__).resolve().parent


def read_logs(path):
    try:
        with path.open('rb') as source:
            source.seek(0, 2)
            size = source.tell()
            source.seek(max(0, size - 1024*1024))
            if size > 1024*1024:
                source.readline(16384)  # Discard the partial first line.
            raw = source.read(1024*1024)
    except OSError:
        return ''
    records = []
    for line in raw.splitlines():
        if len(line) > 16384:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # Exclude legacy stdout/stderr and unstructured exception messages.
        if isinstance(record, dict) and 'event' in record:
            records.append(json.dumps(safe_record(record)))
    return '\n'.join(records)


def collect(root=ROOT):
    current = status(root)
    now = datetime.now(timezone.utc)
    summary = {'python': platform.python_version(), 'system': platform.system(),
               'release': platform.release(), 'machine': platform.machine(),
               'collected_at': now.isoformat(),
               'supervisor': {**safe_record(current), 'running': current['running']}}
    if platform.system() == 'Windows':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetTickCount64.restype = ctypes.c_ulonglong
        uptime = kernel.GetTickCount64() / 1000
        summary['os_uptime_seconds'] = round(uptime)
        summary['last_boot'] = (now-timedelta(seconds=uptime)).isoformat()
        try:
            result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                                     '-ExecutionPolicy', 'Bypass', '-File', str(root/'recovery-task.ps1'),
                                     '-Action', 'Status'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=15, **hidden_process_options())
            data = json.loads(result.stdout)
            summary['windows_recovery'] = safe_record(data) if isinstance(data, dict) else {'task_available': False}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            summary['windows_recovery'] = {'task_available': False}
    try:
        operation = json.loads((root/'.run/recovery-task.json').read_text(encoding='utf-8-sig'))
        if isinstance(operation, dict):
            summary['last_recovery_operation'] = safe_record(operation)
    except (OSError, ValueError):
        pass
    try:
        lock = json.loads((root / 'package-lock.json').read_text(encoding='utf-8-sig'))
        version = lock.get('package_version', '')
        if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+', version):
            summary['package_version'] = version
    except (OSError, ValueError):
        pass
    port = current.get('port', 8787)
    if type(port) is int and 1 <= port <= 65535:
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            connection.request('GET', '/diagnostics')
            response = connection.getresponse()
            data = json.loads(response.read(16384))
            if (response.status == 200 and isinstance(data, dict) and
                    data.get('service') == 'copilot-desktop-proxy' and
                    data.get('pid') == current.get('worker_pid')):
                summary['worker'] = safe_record(data)
                connection.close()
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=7)
                connection.request('GET', '/authorization')
                auth_response = connection.getresponse()
                auth = json.loads(auth_response.read(16384))
                if (auth_response.status == 200 and isinstance(auth, dict) and
                        auth.get('service') == 'copilot-desktop-proxy' and auth.get('pid') == current.get('worker_pid')):
                    summary['authorization'] = safe_record(auth)
            else:
                summary['worker_unavailable'] = True
        except (OSError, ValueError, http.client.HTTPException):
            if 'worker' not in summary:
                summary['worker_unavailable'] = True
            else:
                summary['authorization_check_unavailable'] = True
        finally:
            connection.close()
    output = root / '.diagnostics'
    output.mkdir(exist_ok=True)
    path = output / ('proxy-diagnostics-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%SZ-') + uuid.uuid4().hex[:6] + '.zip')
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('summary.json', json.dumps(summary, indent=2))
        archive.writestr('README.txt', 'Metadata only. No prompts, responses, credentials, account databases, usage history, hostnames or personal paths.\n'
                          'Logs are capped to the last 1 MiB of each rotation. A missing request_finished after request_started plus a worker exit indicates interruption.\n'
                          'resource_sample shows memory/concurrency; recover shows the restart cause and exit code. Times are UTC.\n'
                          'Summary includes capture/boot time, observed watchdog status, stop intent and Windows recovery task result.\n'
                          'A stale recorded phase is not proof that a process is running.\n'
                          'For a live owned worker, authorization includes only credential timing and account-selection flags. JWT timing is not proof of upstream access.\n'
                          'authorization.log samples local timing every 300 seconds without model requests. expiry_advanced means a later stored expiry was observed, not proof of the renewal trigger.\n'
                          'Each worker starts a new baseline; compare UTC expiry/issue times across PIDs to study restarts. request_finished contains timing captured before forwarding, not at completion.\n'
                          'Legacy unstructured logs are excluded. Nothing is uploaded automatically.\n')
        for name in ('proxy.log', 'supervisor.log', 'recovery.log', 'authorization.log'):
            for suffix in ('', '.1', '.2', '.3'):
                data = read_logs(root / (name + suffix))
                if data:
                    archive.writestr(name + suffix, data)
    return path


if __name__ == '__main__':
    print(collect())
