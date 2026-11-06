"""Explicit one-request check through this folder's running proxy. Saves only metadata."""
import argparse
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import uuid

from diagnostics import PACKAGE_VERSION, safe_record
from supervisor import status

from paths import ROOT


def request(port, path, payload=None, timeout=5):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        headers = {'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'}
        connection.request('POST' if payload is not None else 'GET', path,
                           json.dumps(payload).encode() if payload is not None else None, headers)
        response = connection.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Diagnostic response exceeds limit.')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Unexpected diagnostic response.')
        return response.status, value
    finally:
        connection.close()


def check(root=ROOT, model=None):
    report = {'version': PACKAGE_VERSION, 'time': datetime.now(timezone.utc).isoformat(),
              'result': 'not_checked'}
    try:
        current = status(root)
        port = current.get('port', 8787)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid port.')
        code, live = request(port, '/diagnostics')
        if (code != 200 or live.get('service') != 'copilot-desktop-proxy' or
                not current['running'] or live.get('pid') != current.get('worker_pid')):
            report['result'] = 'start_proxy_from_this_folder'
            return report
        report['worker_version'] = safe_record(live).get('version', 'unknown')
        code, authorization = request(port, '/authorization')
        if code != 200 or authorization.get('pid') != live['pid']:
            report['result'] = 'upgrade_and_restart_proxy'
            return report
        report['authorization'] = safe_record(authorization)
        code, health = request(port, '/health')
        if code != 200:
            report['result'] = 'local_authorization_unavailable'
            report['health_status'] = code
            return report
        model = model or health.get('default_model')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}(?:\[1m\])?', model):
            raise ValueError('Invalid model.')
        report['model'] = model
        # Fixed content only, no history, tools, thinking or client-specific beta headers.
        code, value = request(port, '/v1/messages', {'model': model, 'max_tokens': 64,
                               'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}]}, timeout=90)
        report['probe_http_status'] = code
        if code == 200 and value.get('type') == 'message':
            report['result'] = 'minimal_request_succeeded'
        else:
            report['result'] = 'minimal_request_failed'
            error = value.get('error', {})
            if isinstance(error, dict):
                report['rejection'] = safe_record({key: error[key] for key in
                    ('upstream_error_category',) if key in error})
        # Re-read current metadata to spot account option mismatch or expiry after a long probe.
        try:
            code, after = request(port, '/authorization')
            if code == 200 and after.get('pid') == live['pid']:
                report['authorization_after'] = safe_record(after)
            else:
                report['authorization_after_unavailable'] = True
        except (OSError, ValueError, http.client.HTTPException, RuntimeError) as exc:
            # A later metadata failure must not erase the observed upstream outcome.
            report.update(authorization_after_unavailable=True, authorization_after_error_type=type(exc).__name__)
    except (OSError, ValueError, http.client.HTTPException, RuntimeError) as exc:
        report.update(result='check_unavailable', error_type=type(exc).__name__)
    return report


def guidance(report):
    authorization = report.get('authorization', {})
    if authorization.get('matches_saved_account') is False or authorization.get('matches_recovery_account') is False:
        return 'The running worker uses a different database selection from saved startup settings. Stop this proxy, confirm the intended local account selection, then start agency claude again.'
    result = report['result']
    if result == 'minimal_request_succeeded':
        return 'The minimal request succeeded. Retry a fresh Claude conversation with the same model; if only the original conversation fails, compare its request features and the authorization_rejected log category. This test does not prove the original request is now accepted.'
    if result == 'minimal_request_failed':
        return 'The minimal request also failed. Share this metadata report and Collect Diagnostics.cmd output. They distinguish credential timing from an upstream rejection; do not use Claude /login to refresh Copilot credentials.'
    if result == 'local_authorization_unavailable':
        return 'The selected local provider is unavailable or its stored credential has expired. Run "Sign In.cmd" in the shortcuts folder (or check that Copilot Code and the proxy use the same Windows user). See authorization metadata; no upstream test was sent.'
    if result == 'upgrade_and_restart_proxy':
        return 'Stop the installed proxy, upgrade its files to 1.1.8 or later, then run agency claude and this check again.'
    if result == 'start_proxy_from_this_folder':
        return 'Run this check from the folder that owns the running proxy, or start this folder through agency claude first. No model request was sent.'
    return 'The check could not complete. Verify the local proxy is running and collect diagnostics. No credentials or response contents are saved.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', help='Exact model to test; defaults to the proxy default.')
    args = parser.parse_args()
    print('Checking local authorization metadata and sending one short model request if locally ready.', flush=True)
    report = check(model=args.model)
    folder = ROOT / '.diagnostics'
    folder.mkdir(exist_ok=True)
    path = folder / ('authorization-check-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%SZ-') + uuid.uuid4().hex[:6] + '.json')
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    print(guidance(report))
    print('Metadata report: ' + str(path))
    return 0 if report['result'] == 'minimal_request_succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
