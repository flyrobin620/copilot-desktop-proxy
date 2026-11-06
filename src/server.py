"""Loopback-only gateway to the Copilot Code model gateway (standalone sign-in or Copilot desktop)."""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

from adapter import InvalidRequest, StreamAdapter, completion, iter_sse, to_anthropic
from app_config import load_config
from usage_store import UsageStore, UsageObserver
from model_selection import ALIASES, resolve_model, replace_model, default_auto_model, default_client_model_id, use_1m_context
from diagnostics import (EventLog, exception_fields, load_options, resources, PACKAGE_VERSION,
                         request_metadata, upstream_error_metadata)
from parent_lifetime import watch_parent
from authorization import AuthorizationMonitor, credential_timing, rejection_category, account_selection
from native_compat import adapt_context_management, adapt_thinking_display, adapt_cache_ttl
from desktop_models import desktop_catalog, desktop_message_path
from token_refresh import TokenRefresher
from paths import ROOT, SRC

# One million tokens can exceed 4 MiB, particularly in JSON-escaped text.
MAX_BODY = 32 * 1024 * 1024


class ProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, options, event_log):
        self.options, self.event_log = options, event_log
        self.stats_lock = threading.Lock()
        self.stats = dict.fromkeys(('active_requests', 'active_connections', 'completed_requests'), 0)
        self.started, self.next_sample = time.monotonic(), 0
        self.upstream_timeout = options['upstream_timeout']
        self.client_timeout = options['client_timeout']
        super().__init__(address, handler)

    def count(self, name, amount=1):
        with self.stats_lock:
            self.stats[name] += amount

    def snapshot(self):
        with self.stats_lock:
            result = dict(self.stats)
        monitor = getattr(self, 'authorization_monitor', None)
        refresher = getattr(self, 'token_refresher', None)
        return {**result, **self.options, **resources(), **(monitor.snapshot() if monitor else {}),
                **(refresher.snapshot() if refresher else {'token_refresh_enabled': False}),
                'uptime_seconds': round(time.monotonic()-self.started),
                'local_throttling': False,
                'logging_healthy': self.event_log.healthy,
                'usage_recording_healthy': not getattr(self, 'usage_write_failed', False)}

    def process_request(self, request, address):
        self.count('active_connections')
        try:
            super().process_request(request, address)
        except BaseException:
            self.count('active_connections', -1)
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.count('active_connections', -1)

    def handle_error(self, request, address):
        # Base server prints exception messages; those may contain upstream/request data.
        self.event_log.event('handler_error', **exception_fields(sys.exception()))

    def service_actions(self):
        if time.monotonic() >= self.next_sample:
            # Initial interactive setup may finish after the worker has started.
            # Discover its atomic profile publication without restarting active chats.
            if getattr(self, 'discover_token_profile', False) and not getattr(self, 'token_refresher', None):
                try:
                    refresher = TokenRefresher.from_profile(self.event_log)
                    if refresher:
                        self.token_refresher = refresher
                        refresher.start()
                except Exception as exc:
                    self.event_log.event('token_refresh_failed', reason='invalid_refresh_configuration',
                                         **exception_fields(exc))
            self.event_log.event('resource_sample', **self.snapshot())
            self.next_sample = time.monotonic() + 30


def error_body(message, code='invalid_request', kind='invalid_request_error'):
    return {'error': {'message': message, 'type': kind, 'param': None, 'code': code}}


def token_expired(headers):
    return credential_timing(headers)['credential_timing'] == 'expired'


def conversation_id(request):
    """Keep Claude Code tool turns in one gateway conversation when it supplies a session ID."""
    metadata = request.get('metadata') if isinstance(request, dict) else None
    user_id = metadata.get('user_id') if isinstance(metadata, dict) else None
    if isinstance(user_id, str):
        try:
            parsed = json.loads(user_id)
            candidate = parsed.get('session_id') if isinstance(parsed, dict) else None
        except ValueError:
            match = re.search(r'(?:^|_)session_([0-9a-fA-F-]{36})(?:_|$)', user_id)
            candidate = match[1] if match else None
        try:
            value = uuid.UUID(candidate)
            if value.version == 4:
                return str(value)
        except (ValueError, TypeError, AttributeError):
            pass
    return str(uuid.uuid4())


def upstream_error(status, raw, headers, authenticate=''):
    if status == 401:
        result = error_body('Copilot rejected the current credential (401). Run "Sign In.cmd" in the proxy shortcuts folder (or refresh Copilot Code sign-in), '
                          'then retry. Claude Code /login does not renew this credential.',
                          'copilot_authentication_rejected', 'authentication_error')
        result['error']['upstream_error_category'] = rejection_category(raw, authenticate)
        return result
    if status == 403:
        category = rejection_category(raw, authenticate)
        result = error_body('Copilot denied this request (403); this alone does not prove authorization expired. '
                          f'Upstream category: {category}. Run Check Authorization.cmd in this proxy folder. '
                          'Claude Code /login does not renew Copilot authorization.',
                          'copilot_access_denied', 'permission_error')
        result['error']['upstream_error_category'] = category
        return result
    if status == 429:
        return error_body('Copilot returned a usage or rate limit. Retry later.', 'upstream_rate_limit', 'rate_limit_error')
    message = f'Copilot upstream returned HTTP {status}.'
    try:
        detail = json.loads(raw).get('error', {}).get('message')
        if isinstance(detail, str):
            message = detail[:2000]
    except (ValueError, AttributeError):
        pass
    secret = headers.get('Authorization', '')
    if secret:
        message = message.replace(secret, '[redacted]').replace(secret.removeprefix('Bearer '), '[redacted]')
    message = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted]', message)
    return error_body(message, 'upstream_error', 'api_error' if status >= 500 else 'invalid_request_error')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'CopilotLocalProxy/1.0'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(getattr(self.server, 'client_timeout', 60))
        self.stream_started = False

    def diagnostic(self, event, **fields):
        log = getattr(self.server, 'event_log', None)
        if log:
            log.event(event, request_id=getattr(self, 'request_id', ''),
                      stage=getattr(self, 'request_stage', 'local'), **fields)

    def count(self, name, amount=1):
        if hasattr(self.server, 'count'):
            self.server.count(name, amount)

    def log_message(self, *_args):
        # No prompt, response, URL query, token, or header logging.
        pass

    def send_json(self, status, payload, extra_headers=None):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.request_stage = 'client_write'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        if hasattr(self, 'request_id'):
            self.send_header('X-Proxy-Request-Id', self.request_id)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def send_response(self, code, message=None):
        self.response_status = code
        self.request_stage = 'client_write'
        super().send_response(code, message)

    def send_asset(self, name, content_type):
        raw = (SRC / 'web' / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def guard(self):
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.headers.get('Host', '').lower() not in allowed or self.headers.get('Origin'):
            self.send_json(403, error_body('Only local API clients are allowed.', 'local_clients_only'))
            return False
        return True

    def get_config(self, refresh=False):
        try:
            refresher = getattr(self.server, 'token_refresher', None)
            return refresher.get_config(refresh=refresh) if refresher else load_config()
        except Exception as exc:
            self.failure_code = 'copilot_unavailable'
            self.diagnostic('config_error', **exception_fields(exc))
            self.send_json(503, error_body('Copilot upstream authorization is unavailable. Run "Sign In.cmd" in shortcuts '
                                          '(or sign in to Copilot Code) under this Windows account and retry.',
                                          'copilot_unavailable', 'api_error'))
            return None

    def do_GET(self):
        if not self.guard():
            return
        path = urlsplit(self.path).path
        if path == '/livez':
            # Watchdog liveness is independent of account, upstream and usage database health.
            self.send_json(200, {'status': 'alive', 'service': 'copilot-desktop-proxy',
                                'pid': os.getpid(), 'supervisor_pid': getattr(self.server, 'supervisor_pid', 0)})
            return
        if path == '/diagnostics':
            self.send_json(200, {'service': 'copilot-desktop-proxy', 'pid': os.getpid(), 'version': PACKAGE_VERSION,
                                'supervisor_pid': getattr(self.server, 'supervisor_pid', 0),
                                **(self.server.snapshot() if hasattr(self.server, 'snapshot') else {})})
            return
        if path == '/authorization':
            # Explicit read-only troubleshooting; regular liveness/diagnostics stay independent of accounts.
            refresher = getattr(self.server, 'token_refresher', None)
            summary = ({'database_selection': 'not_used'} if getattr(refresher, 'standalone', False)
                       else account_selection(ROOT))
            try:
                _, headers, _ = refresher.get_config(refresh=False) if refresher else load_config()
                summary.update(credential_readable=True, **credential_timing(headers))
                if refresher:
                    summary.update(refresher.snapshot())
            except Exception as exc:
                summary.update(credential_readable=False, **exception_fields(exc))
            self.send_json(200, {'service': 'copilot-desktop-proxy', 'pid': os.getpid(),
                                'version': PACKAGE_VERSION, **summary})
            return
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/usage-viewer': ('index.html', 'text/html; charset=utf-8'),
                  '/dashboard.css': ('dashboard.css', 'text/css; charset=utf-8'),
                  '/dashboard.js': ('dashboard.js', 'text/javascript; charset=utf-8')}
        if path in assets:
            self.send_asset(*assets[path])
            return
        if path == '/usage':
            query = parse_qs(urlsplit(self.path).query)
            try:
                period = query.get('period', ['month'])[0]
                if period not in ('day', 'week', 'month'):
                    raise ValueError()
                page = int(query.get('page', ['1'])[0])
                offset = int(query.get('offset', ['0'])[0])
                if not -840 <= offset <= 840:
                    raise ValueError()
                report = self.server.usage_store.report(period, query.get('model', [''])[0], page, offset)
                report['recording_healthy'] = not getattr(self.server, 'usage_write_failed', False)
                self.send_json(200, report)
            except ValueError:
                self.send_json(400, error_body('Invalid report period, page or timezone offset.'))
            except Exception:
                self.send_json(503, error_body('Local usage statistics are unavailable.', 'usage_unavailable'))
            return
        if path not in ('/health', '/v1/models', '/desktop/v1/models'):
            self.send_json(404, error_body('Use /v1/models or /v1/chat/completions.', 'not_found'))
            return
        config = self.get_config()
        if not config:
            return
        _, headers, models = config
        if path == '/desktop/v1/models':
            self.send_json(200, desktop_catalog(models))
            return
        if path == '/health':
            expired = token_expired(headers)
            self.send_json(503 if expired else 200, {'status': 'authorization_expired' if expired else 'ready',
                'service': 'copilot-desktop-proxy', 'default_model': resolve_model('copilot-auto', models),
                'default_alias': default_auto_model(models),
                'default_reasoning_effort': self.server.default_effort, 'scope': 'loopback',
                'upstream_access_verified': False, **credential_timing(headers)})
        else:
            aliases = []
            for alias in ALIASES:
                try:
                    resolved = resolve_model(alias, models)
                    large = use_1m_context(resolved)
                    row = {'id': alias + ('[1m]' if large else ''), 'object': 'model', 'type': 'model',
                           'display_name': alias.replace('copilot-', 'Copilot ').title() +
                                           (' (1M CLI window)' if large else ' (standard CLI window)'),
                           'resolved_model': resolved, 'created': 0, 'owned_by': 'microsoft-copilot'}
                    aliases.append(row)
                    if alias == 'copilot-auto' and large:
                        aliases.append({**row, 'id': alias, 'display_name': 'Copilot Auto (200K CLI window)'})
                except ValueError:
                    pass
            self.send_json(200, {'object': 'list', 'has_more': False, 'data': [
                {'id': default_client_model_id(m['model_id']), 'object': 'model', 'type': 'model',
                 'display_name': m['model_id'].replace('claude-', 'Claude ').replace('opus-', 'Opus ').replace('sonnet-', 'Sonnet ').replace('haiku-', 'Haiku '),
                 'description': 'Copilot desktop · fixed version' + (' · 1M CLI window' if use_1m_context(m['model_id']) else ''), 'upstream_model': m['model_id'],
                 'created': 0, 'owned_by': 'microsoft-copilot'} for m in models] + aliases})

    def do_POST(self):
        # All forwarding, compatibility, accounting and isolation below stays
        # shared with the native API. The Desktop prefix never reaches Loki.
        self.path = desktop_message_path(self.path)
        self.request_id = uuid.uuid4().hex
        self.request_stage, self.failure_code = 'validation', None
        self.body_bytes, self.upstream_status = 0, 0
        self.usage_observer = UsageObserver()
        self.response_status = 500
        self.usage_model, self.usage_streaming = 'unknown', False
        self.usage_outcome = None
        self.authorization_timing = {}
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        try:
            self.handle_model_request()
        finally:
            path = urlsplit(self.path).path
            store = getattr(self.server, 'usage_store', None)
            if store and path in ('/v1/chat/completions', '/v1/messages'):
                try:
                    status = self.response_status
                    outcome = self.usage_outcome or ('success' if status == 200 else 'error')
                    if not self.usage_outcome and status == 200 and (self.usage_observer.failed or not self.usage_observer.complete):
                        outcome = 'incomplete'
                    store.record({'id': self.request_id, 'timestamp': timestamp, 'model': self.usage_model,
                                  'protocol': 'anthropic' if path == '/v1/messages' else 'openai',
                                  'streaming': int(self.usage_streaming), 'status': status, 'outcome': outcome,
                                  'duration_ms': round((time.monotonic() - started) * 1000),
                                  'usage_known': int(self.usage_observer.known), **self.usage_observer.tokens})
                except Exception as exc:
                    self.server.usage_write_failed = True
                    self.diagnostic('usage_write_failed', **exception_fields(exc))
            self.count('completed_requests')
            self.diagnostic('request_finished', protocol='anthropic' if path.startswith('/v1/messages') else 'openai',
                            model=self.usage_model, streaming=self.usage_streaming, status=self.response_status,
                            upstream_status=self.upstream_status, body_bytes=self.body_bytes,
                            duration_ms=round((time.monotonic()-started)*1000),
                            outcome=self.usage_outcome or ('success' if self.response_status == 200 and
                                self.usage_observer.complete and not self.usage_observer.failed else 'error'),
                            reason=self.failure_code or 'none', **self.authorization_timing)

    def handle_model_request(self):
        connection = None
        active = False
        headers = {}
        try:
            if not self.guard():
                return
            route = urlsplit(self.path)
            native = route.path in ('/v1/messages', '/v1/messages/count_tokens')
            if route.path != '/v1/chat/completions' and not native:
                self.send_json(404, error_body('Use /v1/chat/completions or /v1/messages.', 'not_found'))
                return
            if self.headers.get('Transfer-Encoding'):
                self.send_json(400, error_body('Chunked request bodies are not supported.'))
                return
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                self.send_json(415, error_body('Content-Type must be application/json.'))
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise InvalidRequest('Invalid Content-Length.') from None
            if size <= 0 or size > MAX_BODY:
                self.send_json(413, error_body('Request body must be between 1 byte and 32 MiB.', 'body_size_limit'))
                return
            self.body_bytes = size
            self.count('active_requests')
            active = True
            self.request_stage = 'client_read'
            self.diagnostic('request_started', body_bytes=size)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise InvalidRequest('Request body was incomplete.')
            try:
                request = json.loads(raw)
            except (ValueError, UnicodeError):
                raise InvalidRequest('Request body must be valid UTF-8 JSON.') from None
            self.request_stage = 'configuration'
            config = self.get_config(refresh=True)
            if not config:
                return
            base, headers, models = config
            endpoint = urlsplit(base)
            self.authorization_timing = {**credential_timing(headers),
                'credential_observed_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds')}
            if token_expired(headers):
                self.failure_code = 'copilot_credential_expired'
                self.diagnostic('authorization_rejected', reason=self.failure_code, **credential_timing(headers))
                self.send_json(401, error_body('The stored Copilot credential has expired. Run "Sign In.cmd" in the proxy shortcuts folder (or refresh Copilot Code sign-in), '
                    'then retry. Claude Code /login does not renew this credential.',
                    self.failure_code, 'authentication_error'))
                return
            if isinstance(request, dict) and isinstance(request.get('model'), str):
                try:
                    resolved_model = resolve_model(request['model'], models)
                    if resolved_model != request['model']:
                        raw = replace_model(raw, resolved_model)
                except ValueError as exc:
                    raise InvalidRequest(str(exc)) from None
                request['model'] = resolved_model
            if native:
                if not isinstance(request, dict):
                    raise InvalidRequest('The JSON body must be an object.')
                if request.get('model') not in {m['model_id'] for m in models}:
                    raise InvalidRequest('Unknown model. Use GET /v1/models for available models.')
                if not isinstance(request.get('stream', False), bool):
                    raise InvalidRequest('stream must be a boolean.')
                # Temporary DF Opus 5.5 workaround, keyed by the actual backend and
                # resolved model (including Auto); see native_compat.py / README.
                wire_body, payload, context_adapted = adapt_context_management(raw, request,
                    upstream_host=endpoint.hostname, resolved_model=request['model'],
                    enabled=getattr(self.server, 'options', {}).get('loki_context_management_compat', True))
                wire_body, payload, beta, display_adapted = adapt_thinking_display(
                    wire_body, payload, self.headers.get('anthropic-beta', ''),
                    upstream_host=endpoint.hostname, resolved_model=request['model'],
                    enabled=route.path == '/v1/messages' and
                    getattr(self.server, 'options', {}).get('loki_thinking_display_compat', True))
                if beta:
                    headers['anthropic-beta'] = beta
                wire_body, payload, cache_ttl_adapted = adapt_cache_ttl(wire_body, payload,
                    upstream_host=endpoint.hostname, resolved_model=request['model'],
                    enabled=getattr(self.server, 'options', {}).get('loki_cache_ttl_compat', True))
            else:
                context_adapted = False
                display_adapted = False
                cache_ttl_adapted = False
                payload = to_anthropic(request, models, self.server.default_effort)
                wire_body = json.dumps(payload).encode('utf-8')
            self.usage_model = payload['model']
            self.usage_streaming = payload.get('stream', False)
            include_usage = bool((request.get('stream_options') or {}).get('include_usage')) if not native else False
            headers.update({'Content-Type': 'application/json', 'anthropic-version': '2023-06-01',
                            'X-ConversationId': conversation_id(request), 'X-ClientCallId': str(uuid.uuid4())})
            if native and self.headers.get('anthropic-version'):
                headers['anthropic-version'] = self.headers['anthropic-version']
            features = request_metadata(payload, route.path, headers.get('anthropic-beta', ''))
            features['context_management_compat_applied'] = context_adapted
            features['thinking_display_compat_applied'] = display_adapted
            features['cache_ttl_compat_applied'] = cache_ttl_adapted
            features['upstream_environment'] = {'loki.delve.office.com': 'prod',
                'df.loki.delve.office.com': 'df'}.get(endpoint.hostname, 'other')
            upstream_path = endpoint.path + (route.path if native else '/v1/messages')
            if native and route.query:
                upstream_path += '?' + route.query
            # Retry only an explicit HTTP 401 before any response has reached the
            # client. Preserve exact request bytes, beta headers and call IDs.
            # Never replay a partial stream, disconnect, timeout, 403 or 429.
            for attempt in range(2):
                connection = http.client.HTTPSConnection(endpoint.hostname, timeout=30)
                self.request_stage = 'upstream_send'
                connection.request('POST', upstream_path, wire_body, headers)
                self.request_stage = 'upstream_headers'
                connection.sock.settimeout(self.server.upstream_timeout)
                response = connection.getresponse()
                refresher = getattr(self.server, 'token_refresher', None)
                if response.status != 401 or attempt or not refresher:
                    break
                self.diagnostic('upstream_401_refresh', upstream_status=401)
                renewed = refresher.after_401(headers.get('Authorization', '').removeprefix('Bearer '))
                if not renewed:
                    break
                response.close()
                connection.close()
                headers['Authorization'] = 'Bearer ' + renewed
                self.authorization_timing = credential_timing(headers)
                self.diagnostic('upstream_401_retry', upstream_status=401)
            del raw, wire_body, request, payload
            self.upstream_status = response.status
            self.diagnostic('upstream_response', upstream_status=response.status, model=self.usage_model, **features)
            self.request_stage = 'upstream_read'
            if response.status != 200:
                self.failure_code = 'upstream_http_error'
                raw = response.read(64 * 1024)
                self.diagnostic('upstream_http_error', upstream_status=response.status, **upstream_error_metadata(raw))
                if response.status in (401, 403):
                    self.failure_code = 'upstream_authentication_rejected' if response.status == 401 else 'upstream_access_denied'
                    self.diagnostic('authorization_rejected', upstream_status=response.status,
                                    upstream_error_category=rejection_category(raw, response.getheader('WWW-Authenticate', '')),
                                    **credential_timing(headers))
                error = upstream_error(response.status, raw, headers, response.getheader('WWW-Authenticate', ''))
                if native:
                    error = {'type': 'error', 'error': error['error']}
                self.send_json(response.status, error)
                return
            if native:
                self.forward_native(response, self.usage_streaming)
                return
            if not self.usage_streaming:
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise RuntimeError('Upstream response too large.')
                result = json.loads(raw)
                if result.get('type') != 'message':
                    raise RuntimeError('Unexpected upstream response.')
                self.usage_observer.event(result)
                self.send_json(200, completion(result, self.usage_model))
                return
            if 'text/event-stream' not in response.getheader('Content-Type', ''):
                raise RuntimeError('Expected an upstream event stream.')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache, no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('X-Proxy-Request-Id', self.request_id)
            self.end_headers()
            self.stream_started, self.close_connection = True, True
            adapter = StreamAdapter(self.usage_model, include_usage)
            self.request_stage = 'upstream_read'
            for event in iter_sse(response):
                self.usage_observer.event(event)
                if event.get('type') == 'error':
                    self.failure_code = 'upstream_stream_error'
                    self.send_event(upstream_error(502, json.dumps(event), headers))
                    return
                for chunk in adapter.feed(event):
                    self.send_event(chunk)
                if adapter.finished:
                    self.request_stage = 'client_write'
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                    return
            raise RuntimeError('Upstream stream ended before message_stop.')
        except InvalidRequest as exc:
            self.failure_code = 'invalid_request'
            self.send_json(400, error_body(str(exc)))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            self.diagnostic('connection_error', **exception_fields(exc))
            if self.request_stage.startswith('upstream'):
                self.report_error(502, 'The upstream connection was interrupted.', 'upstream_disconnect')
            else:
                self.usage_outcome, self.failure_code = 'disconnected', 'client_disconnect'
        except (TimeoutError, socket.timeout) as exc:
            self.diagnostic('timeout', **exception_fields(exc))
            if self.request_stage.startswith('client'):
                self.report_error(408, 'The local client connection timed out.', 'client_timeout')
            else:
                self.report_error(504, 'The upstream request timed out.', 'upstream_timeout')
        except Exception as exc:
            self.diagnostic('request_error', **exception_fields(exc))
            self.report_error(502, 'The upstream connection failed or returned an invalid response.', 'upstream_failure')
        finally:
            try:
                if connection:
                    connection.close()
            finally:
                if active:
                    self.count('active_requests', -1)

    def send_event(self, payload):
        self.request_stage = 'client_write'
        self.wfile.write(('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode('utf-8'))
        self.wfile.flush()
        self.request_stage = 'upstream_read'

    def forward_native(self, response, streaming):
        if not streaming:
            raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise RuntimeError('Upstream response too large.')
            try:
                self.usage_observer.event(json.loads(raw))
            except ValueError:
                pass
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw)
            return
        if 'text/event-stream' not in response.getheader('Content-Type', ''):
            raise RuntimeError('Expected an upstream event stream.')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('Connection', 'close')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('X-Proxy-Request-Id', self.request_id)
        self.end_headers()
        self.stream_started, self.close_connection = True, True
        self.request_stage = 'upstream_read'
        while True:
            block = response.read1(65536)
            if not block:
                raise RuntimeError('Upstream stream ended before message_stop.')
            self.usage_observer.feed(block)
            self.request_stage = 'client_write'
            self.wfile.write(block)
            self.wfile.flush()
            self.request_stage = 'upstream_read'
            if self.usage_observer.failed:
                self.usage_outcome, self.failure_code = 'error', 'upstream_stream_error'
                return
            if self.usage_observer.complete:
                return

    def report_error(self, status, message, code):
        self.failure_code, self.usage_outcome = code, 'error'
        try:
            payload = error_body(message, code, 'api_error')
            if self.stream_started:
                if urlsplit(self.path).path == '/v1/messages':
                    self.wfile.write(b'event: error\n')
                    self.send_event({'type': 'error', 'error': payload['error']})
                else:
                    self.send_event(payload)
            else:
                self.send_json(status, payload)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--default-effort', choices=['none', 'low', 'medium', 'high', 'xhigh', 'max'], default='xhigh')
    parser.add_argument('--timeout', type=int, default=None)
    parser.add_argument('--usage-db', default=str(ROOT / '.data' / 'usage.sqlite3'))
    args = parser.parse_args()
    root = ROOT
    log, server, parent_watch = EventLog(root / 'proxy.log'), None, None
    authorization_monitor = None
    token_refresher = None
    try:
        log.event('worker_starting', stage='options')
        options = load_options(root)
        if args.timeout is not None:
            if not 30 <= args.timeout <= 1800:
                raise ValueError('Timeout must be between 30 and 1800 seconds.')
            options['upstream_timeout'] = args.timeout
        # Bind even when authorization is temporarily unavailable; /health reports the problem.
        log.event('worker_starting', stage='bind')
        server = ProxyHTTPServer(('127.0.0.1', args.port), Handler, options, log)
        server.default_effort = args.default_effort
        server.supervisor_pid = int(os.environ.get('COPILOT_PROXY_SUPERVISOR_PID', '0'))
        parent_watch = watch_parent(server.supervisor_pid, server, log)
        try:
            token_refresher = TokenRefresher.from_profile(log)
        except Exception as exc:
            log.event('token_refresh_failed', reason='invalid_refresh_configuration', **exception_fields(exc))
        server.token_refresher = token_refresher
        server.discover_token_profile = True
        if token_refresher:
            token_refresher.start()
        authorization_monitor = AuthorizationMonitor(
            EventLog(root / 'authorization.log', max_bytes=1024*1024, backups=3),
            lambda: server.token_refresher.get_config(refresh=False) if server.token_refresher else load_config())
        server.authorization_monitor = authorization_monitor
        authorization_monitor.start()
        log.event('worker_starting', stage='usage_init')
        try:
            server.usage_store = UsageStore(args.usage_db)
        except Exception as exc:
            server.usage_write_failed = True
            log.event('usage_init_failed', **exception_fields(exc))
        log.event('worker_ready', port=args.port, **options, **resources())
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.event('worker_fatal', **exception_fields(exc))
        return 1
    finally:
        if server and getattr(server, 'token_refresher', None):
            server.token_refresher.close()
        if authorization_monitor:
            authorization_monitor.close()
        if parent_watch:
            parent_watch[0].set()
            parent_watch[1].join(timeout=2)
        if server:
            server.server_close()
        log.event('worker_stopped')
        log.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
