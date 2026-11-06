"""Account-bound silent renewal. Credentials stay in child pipes and memory.

Profiles with ``"source": "standalone"`` need no Copilot desktop app: the
gateway URL and client headers come from the private profile, the model list
from the gateway's /models endpoint. Older profiles (``"database"``) keep
reading those values from the local Copilot Code database.
"""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from app_config import approved_headers, fetch_models, load_config, locate_db, validate_upstream
from authorization import credential_timing
from paths import ROOT, SRC

CLIENT_ID = '0ec893e0-5785-4de6-99da-4ed124e5296c'
RESOURCE = '42c8f7bd-c7a8-4ba9-bc1c-91edfb716247'
SCOPE = 'api://' + RESOURCE + '/.default'


def profile_dir(root=ROOT):
    # USERPROFILE avoids packaged-app LOCALAPPDATA redirection across launchers.
    identity = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:24]
    return Path.home() / '.copilot-desktop-proxy' / 'auth' / identity


def claims(token):
    """Local binding hints only; this is not signature verification."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return {}
        result = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, UnicodeError):
        return {}


class RefreshError(Exception):
    pass


def write_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class ModelCatalog:
    """Gateway model list, cached in memory and as non-secret ids in the profile."""

    def __init__(self, path, fetch=fetch_models, clock=time.monotonic, ttl=900, retry=60):
        self.path, self.fetch, self.clock, self.ttl, self.retry = path, fetch, clock, ttl, retry
        self.lock = threading.Lock()
        self.fetched_at, self.retry_at = None, float('-inf')
        self.source = 'none'
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            self.models = [m for m in cached if isinstance(m, dict) and isinstance(m.get('model_id'), str)]
            self.source = 'cache' if self.models else 'none'
        except (OSError, ValueError, TypeError):
            self.models = []

    def get(self, base, headers, refresh=True):
        with self.lock:
            now = self.clock()
            stale = self.fetched_at is None or now - self.fetched_at >= self.ttl
            # Always try once when nothing is known; otherwise only background/request refreshes.
            if (refresh and stale or not self.models) and now >= self.retry_at:
                try:
                    models = self.fetch(base, headers)
                    self.models, self.fetched_at, self.source = models, now, 'gateway'
                    try:
                        write_private_json(self.path, models)
                    except OSError:
                        pass
                except Exception:
                    self.retry_at = now + self.retry
            if not self.models:
                raise RefreshError('model_catalog_unavailable')
            return [dict(model) for model in self.models]


def validate_token(token, expires_at, settings, now=None):
    now = time.time() if now is None else now
    if (not isinstance(token, str) or not token or any(c.isspace() for c in token)
            or len(token) > 65536 or type(expires_at) not in (int, float)
            or not math.isfinite(expires_at) or expires_at <= now + 60):
        raise RefreshError('invalid_token_result')
    decoded = claims(token)
    if decoded:
        if (decoded.get('aud') not in (RESOURCE, 'api://' + RESOURCE)
                or decoded.get('azp', decoded.get('appid')) != CLIENT_ID
                or 'LLM.Read' not in decoded.get('scp', '').split()
                or decoded.get('tid', '').casefold() != settings['tenant_id'].casefold()
                or decoded.get('oid', '').casefold() != settings['object_id'].casefold()):
            raise RefreshError('token_identity_mismatch')
        expiry = decoded.get('exp')
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now + 60:
            raise RefreshError('invalid_token_result')
        expires_at = min(expires_at, expiry)
    return token, expires_at


class TokenRefresher:
    def __init__(self, settings, log, loader=None, acquire=None, clock=time.time, catalog=None):
        self.settings, self.log, self.clock = settings, log, clock
        self.standalone = settings.get('source') == 'standalone'
        self.loader = loader or (self._standalone_config if self.standalone else load_config)
        self.catalog = catalog or (ModelCatalog(profile_dir() / 'models.json') if self.standalone else None)
        self.acquire = acquire or self._acquire
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.token, self.expires_at = None, 0
        self.renew_token = None
        self.next_attempt, self.last_force = 0, float('-inf')
        self.failures, self.last_error = 0, None

    @classmethod
    def from_profile(cls, log):
        path = profile_dir() / 'settings.json'
        if not path.exists():
            return None
        settings = json.loads(path.read_text(encoding='utf-8'))
        if settings.get('enabled') is not True:
            return None
        standalone = settings.get('source') == 'standalone'
        for key in ('python', 'username', 'tenant_id', 'object_id') + (() if standalone else ('database',)):
            if not isinstance(settings.get(key), str) or not settings[key]:
                raise RefreshError('invalid_refresh_configuration')
        if standalone:
            try:
                validate_upstream(settings.get('upstream'))
                if not approved_headers(settings.get('headers')):
                    raise RuntimeError
            except RuntimeError:
                raise RefreshError('invalid_refresh_configuration') from None
        return cls(settings, log)

    def _standalone_config(self):
        return (validate_upstream(self.settings['upstream']),
                approved_headers(self.settings['headers']), None)

    def _acquire(self, force):
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        try:
            result = subprocess.run(
                [self.settings['python'], '-B', str(SRC / 'token_helper.py'), 'acquire'],
                input=json.dumps({'settings': self.settings, 'force': force,
                                  'token_to_renew': self.renew_token if force else None}),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=30, **options)
            value = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise RefreshError('refresh_timeout') from None
        except (OSError, ValueError):
            raise RefreshError('helper_unavailable') from None
        if result.returncode or value.get('ok') is not True:
            reason = value.get('reason')
            if reason not in ('interaction_required', 'account_mismatch', 'cache_unavailable',
                              'broker_unavailable', 'temporarily_unavailable'):
                reason = 'refresh_failed'
            raise RefreshError(reason)
        return value['access_token'], value['expires_at']

    def _check_binding(self, headers):
        if not self.standalone and (os.path.normcase(str(locate_db()))
                                    != os.path.normcase(self.settings['database'])):
            raise RefreshError('account_mismatch')
        decoded = claims(headers.get('Authorization', '').removeprefix('Bearer '))
        if decoded and (decoded.get('oid', '').casefold() != self.settings['object_id'].casefold()
                        or decoded.get('tid', '').casefold() != self.settings['tenant_id'].casefold()):
            raise RefreshError('account_mismatch')

    def _current(self, headers):
        if self.token and self.expires_at > self.clock() + 10:
            return self.token
        return headers.get('Authorization', '').removeprefix('Bearer ')

    def get_config(self, refresh=True, rejected=None):
        with self.lock:
            base, original, models = self.loader()
            headers = dict(original)
            try:
                self._check_binding(headers)
            except RefreshError:
                self.token, self.expires_at = None, 0
                self.last_error = 'account_mismatch'
                raise
            now = self.clock()
            current = self._current(headers)
            remaining = (self.expires_at - now if current == self.token else
                         credential_timing({'Authorization': 'Bearer ' + current}, now=now)
                         .get('credential_expires_in_seconds', 0))
            # A concurrent request already replaced the rejected token: reuse it.
            force = rejected is not None and current == rejected
            due = (self.token is None or remaining <= 300)
            permitted = (now >= self.next_attempt or (force and not self.failures)) and (
                not force or now - self.last_force >= 30)
            # Standalone mode has no desktop credential: a read-only caller may
            # perform the first acquisition (still subject to failure backoff).
            cold = self.standalone and self.token is None
            if (refresh or cold) and permitted and (force or (rejected is None and due)):
                if force:
                    self.last_force = now
                self.log.event('token_refresh_started', reason='upstream_401' if force else 'scheduled_or_expiry')
                try:
                    self.renew_token = current
                    token, expiry = self.acquire(force or (self.token is not None and remaining <= 300))
                    token, expiry = validate_token(token, expiry, self.settings, self.clock())
                    # Re-read account binding after a potentially slow broker call.
                    self._check_binding(self.loader()[1])
                    self.token, self.expires_at = token, expiry
                    self.failures, self.last_error = 0, None
                    self.next_attempt = self.clock() + 30
                    self.log.event('token_refresh_succeeded', credential_expires_in_seconds=round(expiry-self.clock()))
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, RefreshError) else 'refresh_failed'
                    if reason == 'account_mismatch':
                        self.token, self.expires_at = None, 0
                        raise
                    self.last_error = reason
                    self.failures += 1
                    self.next_attempt = self.clock() + min(300, 30 * 2 ** min(self.failures-1, 4))
                    self.log.event('token_refresh_failed', reason=reason,
                                   retry_seconds=round(self.next_attempt-self.clock()))
                finally:
                    self.renew_token = None
            current = self._current(headers)
            if not current:
                # Standalone mode has no desktop credential to fall back on.
                raise RefreshError(self.last_error or 'token_unavailable')
            headers['Authorization'] = 'Bearer ' + current
        if models is None:
            # Outside the token lock: a slow catalog fetch never delays renewal.
            models = self.catalog.get(base, headers, refresh=refresh)
        return base, headers, models

    def after_401(self, rejected):
        try:
            _, headers, _ = self.get_config(rejected=rejected)
            token = headers['Authorization'].removeprefix('Bearer ')
            # A force refresh that returns the same rejected credential cannot help.
            return token if token and token != rejected else None
        except Exception:
            return None

    def snapshot(self):
        # Diagnostics must never wait on network/broker acquisition.
        return {'token_refresh_enabled': True,
                'credential_source': 'standalone' if self.standalone else 'copilot_db',
                'token_refresh_running': bool(self.thread and self.thread.is_alive()),
                'token_refresh_state': self.last_error or ('ready' if self.token else 'pending'),
                'token_refresh_failures': self.failures}

    def start(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.get_config()
                except Exception:
                    self.log.event('token_refresh_failed', reason='configuration_unavailable')
                self.stop.wait(30)
        self.thread = threading.Thread(target=run, name='token-refresh', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=32)

