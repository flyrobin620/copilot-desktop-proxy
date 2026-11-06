"""Authorization diagnostics containing only timing hints, never credentials or account IDs."""
import base64
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time

from app_config import APP_ROOT, locate_db
from diagnostics import exception_fields, safe_record


def credential_timing(headers, now=None):
    """JWT timing is an unverified local hint, never proof of upstream access."""
    result = {'credential_timing': 'unknown'}
    try:
        value = headers.get('Authorization', '')
        parts = value.removeprefix('Bearer ').split('.')
        if len(parts) != 3 or len(parts[1]) > 32768:
            return result
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        if not isinstance(claims, dict):
            return result
        now = time.time() if now is None else now
        expiry = claims.get('exp')
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or not 0 < expiry < 253402300800:
            return result
        remaining = expiry - now
        result.update(credential_timing='expired' if remaining <= 10 else 'not_expired',
                      credential_expires_in_seconds=round(remaining),
                      credential_expires_at=datetime.fromtimestamp(expiry, timezone.utc).isoformat(timespec='seconds'))
        issued = claims.get('iat')
        if type(issued) in (int, float) and math.isfinite(issued) and 0 < issued <= expiry:
            result.update(credential_issued_at=datetime.fromtimestamp(issued, timezone.utc).isoformat(timespec='seconds'),
                          credential_lifetime_seconds=round(expiry-issued))
        not_before = claims.get('nbf')
        if type(not_before) in (int, float) and math.isfinite(not_before) and now + 10 < not_before < 253402300800:
            result['credential_timing'] = 'not_yet_valid'
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        pass
    return result


class AuthorizationMonitor:
    """Observe the desktop's stored timing independently of requests; no upstream calls."""
    def __init__(self, log, loader, interval=300):
        self.log, self.loader, self.interval = log, loader, interval
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.last_sample_at = None
        self.previous = None
        self.previous_tick = None
        self.read_failed = False
        self.thread = threading.Thread(target=self.run, name='authorization-timing', daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return {'authorization_monitor_running': self.thread.is_alive(),
                    'authorization_logging_healthy': self.log.healthy,
                    'authorization_sample_interval_seconds': self.interval,
                    'authorization_last_sample_at': self.last_sample_at}

    def sample(self):
        tick = time.monotonic()
        observed = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        try:
            _, headers, _ = self.loader()
            timing = credential_timing(headers)
            del headers
        except Exception as exc:
            self.log.event('authorization_read_failed', credential_readable=False,
                           credential_observed_at=observed, **exception_fields(exc))
            self.read_failed = True
        else:
            fields = {**timing, 'credential_readable': True, 'credential_observed_at': observed}
            event = 'authorization_baseline' if self.previous is None else 'authorization_sample'
            if self.previous is not None:
                fields['observation_gap_seconds'] = round(tick-self.previous_tick)
                before = self.previous.get('credential_expires_at')
                after = timing.get('credential_expires_at')
                if before and after and before != after:
                    delta = round((datetime.fromisoformat(after)-datetime.fromisoformat(before)).total_seconds())
                    event = 'authorization_expiry_advanced' if delta > 0 else 'authorization_expiry_regressed'
                    fields.update(previous_credential_expires_at=before,
                                  previous_credential_observed_at=self.previous['credential_observed_at'],
                                  credential_expiry_change_seconds=delta)
                elif any(timing.get(key) != self.previous.get(key) for key in
                         ('credential_timing', 'credential_issued_at', 'credential_expires_at')):
                    event = 'authorization_timing_changed'
                elif self.read_failed:
                    event = 'authorization_read_recovered'
            fields['credential_read_recovered'] = self.read_failed
            self.log.event(event, **fields)
            # Retain only allowlisted times and states, never headers, claims or token hashes.
            self.previous = safe_record({**timing, 'credential_observed_at': observed})
            self.previous_tick = tick
            self.read_failed = False
        with self.lock:
            self.last_sample_at = observed

    def run(self):
        while not self.stopped.is_set():
            try:
                self.sample()
            except Exception as exc:
                self.log.event('authorization_monitor_error', **exception_fields(exc))
            if self.stopped.wait(self.interval):
                break

    def close(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=6)
        # An unexpectedly blocked local read must not block worker shutdown indefinitely.
        if not self.thread.is_alive():
            self.log.close()


def rejection_category(raw, authenticate=''):
    """Only exact known machine codes; never echo an upstream message or unknown code."""
    if 'error="invalid_token"' in authenticate.lower():
        return 'credential_rejected'
    categories = {
        'invalidtoken': 'credential_rejected', 'invalidauthenticationtoken': 'credential_rejected',
        'authenticationerror': 'credential_rejected', 'authenticationfailed': 'credential_rejected',
        'expiredauthenticationtoken': 'credential_expired', 'tokenexpired': 'credential_expired',
        'contentfilter': 'content_policy', 'contentfiltererror': 'content_policy',
        'responsibleaipolicyviolation': 'content_policy', 'contentpolicyviolation': 'content_policy',
        'permissionerror': 'access_denied', 'accessdenied': 'access_denied', 'forbidden': 'access_denied',
        'modelnotfound': 'model_access', 'modelaccessdenied': 'model_access',
    }
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 'unspecified'
        error = payload.get('error', payload)
        if not isinstance(error, dict):
            return 'unspecified'
        inner = error.get('innererror', error.get('inner_error', {}))
        # Specific inner codes take precedence over a generic forbidden wrapper.
        for item in (inner, error):
            if isinstance(item, dict):
                for key in ('code', 'type'):
                    value = item.get(key)
                    if isinstance(value, str) and len(value) <= 80:
                        normalized = value.lower().replace('_', '').replace('-', '')
                        if normalized in categories:
                            return categories[normalized]
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    return 'unspecified'


def account_selection(root):
    """Compare selected paths in memory, returning only counts, booleans and enums."""
    root = Path(root)
    result = {'database_selection': 'explicit' if os.environ.get('COPILOT_PROXY_DB') else 'automatic'}
    try:
        result['account_database_count'] = len(list(APP_ROOT.glob('*/data.db')))
        selected = locate_db()
        result['selected_database_exists'] = selected.is_file()
        for file, field in (('local-options.json', 'matches_saved_account'),
                            ('.run/recovery-options.json', 'matches_recovery_account')):
            path = root / file
            if not path.exists():
                continue
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            database = value.get('database') if isinstance(value, dict) else None
            if isinstance(database, str) and database:
                result[field] = selected == Path(database).resolve()
    except (OSError, ValueError, RuntimeError):
        result['account_selection_check_failed'] = True
    return result
