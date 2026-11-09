"""Bounded, metadata-only diagnostics. Never log exception text, bodies or headers."""
import ctypes
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import threading

PACKAGE_VERSION = '1.1.21'
DEFAULT_OPTIONS = {'upstream_timeout': 600, 'client_timeout': 60, 'loki_context_management_compat': True,
                   'loki_thinking_display_compat': True, 'loki_cache_ttl_compat': True,
                   'loki_tool_search_beta_compat': True}
RANGES = {'upstream_timeout': (30, 1800), 'client_timeout': (5, 300)}
# Upgrades may retain their existing options file. These fields no longer limit requests.
LEGACY_CAPACITY_OPTIONS = frozenset(('max_concurrency', 'max_connections'))
NUMBERS = frozenset('pid supervisor_pid worker_pid port restart_count exit_code delay retry_seconds '
                   'failures refused_failures duration_ms body_bytes upstream_status status active_requests active_connections '
                   'rejected_requests rejected_connections completed_requests uptime_seconds threads rss_mb '
                   'peak_rss_mb private_mb available_memory_mb max_concurrency max_connections '
                   'upstream_timeout client_timeout errno winerror task_last_result task_missed_runs os_uptime_seconds '
                   'probe_timeout_seconds startup_grace_seconds liveness_failure_window_seconds liveness_failure_seconds '
                   'credential_expires_in_seconds credential_lifetime_seconds credential_expiry_change_seconds '
                   'observation_gap_seconds authorization_sample_interval_seconds account_database_count '
                   'request_metadata_version message_count tool_count deferred_tool_count tool_search_tool_count '
                   'tool_reference_count max_tokens thinking_budget_tokens beta_count beta_unknown_count '
                   'cache_control_count cache_control_1h_count'.split())
NUMBERS = NUMBERS | frozenset(('message_role_user_count', 'message_role_assistant_count',
    'token_refresh_failures',
    'message_role_system_count', 'message_role_other_count', 'message_output_config_count',
    'message_effort_count', 'message_timing_count', 'message_clear_at_count',
    'message_unknown_field_count', 'top_level_unknown_field_count',
    'content_text_count', 'content_thinking_count', 'content_redacted_thinking_count',
    'content_tool_use_count', 'content_tool_result_count', 'content_tool_addition_count',
    'content_tool_removal_count', 'content_image_count', 'content_document_count',
    'content_other_count', 'inline_tool_definition_count', 'tool_strict_count',
    'tool_schema_non_object_count', 'cache_control_scope_count'))
WORDS = frozenset('event phase recorded_phase reason stage error_type outcome protocol model task_state '
                  'credential_timing upstream_error_category database_selection token_refresh_state credential_source'.split())
BOOLEANS = frozenset(('streaming', 'logging_healthy', 'usage_recording_healthy', 'stop_requested',
                      'token_refresh_enabled', 'token_refresh_running',
                      'worker_alive', 'in_startup_grace',
                      'state_stale', 'task_exists', 'task_enabled', 'task_owned', 'task_available', 'recovery_armed',
                      'local_throttling', 'credential_readable', 'selected_database_exists', 'matches_saved_account',
                      'matches_recovery_account', 'account_selection_check_failed', 'upstream_access_verified',
                      'authorization_monitor_running', 'authorization_logging_healthy', 'credential_read_recovered',
                      'request_metadata_complete', 'has_output_config', 'has_output_format', 'has_context_management',
                      'has_temperature', 'has_top_p', 'has_top_k', 'has_service_tier', 'has_stop_sequences',
                      'upstream_error_json', 'upstream_generic_rejection', 'upstream_beta_unsupported',
                      'loki_context_management_compat',
                      'context_management_compat_applied', 'loki_thinking_display_compat',
                      'thinking_display_compat_applied', 'loki_cache_ttl_compat', 'cache_ttl_compat_applied',
                      'loki_tool_search_beta_compat', 'tool_search_beta_compat_applied'))

# Only fixed protocol categories may be persisted, including during support export.
ENUMS = {
    'endpoint_kind': ('messages', 'count_tokens', 'chat_completions', 'other'),
    'upstream_environment': ('prod', 'df', 'other'),
    'thinking_mode': ('absent', 'enabled', 'disabled', 'adaptive', 'other'),
    'effort_mode': ('absent', 'none', 'low', 'medium', 'high', 'xhigh', 'max', 'other'),
    'tool_choice_mode': ('absent', 'auto', 'any', 'tool', 'none', 'other'),
    'output_format_mode': ('absent', 'text', 'json_schema', 'other'),
    'upstream_error_type': ('absent', 'invalid_request_error', 'authentication_error', 'permission_error',
                            'not_found_error', 'rate_limit_error', 'api_error', 'overloaded_error', 'other'),
    'upstream_error_code': ('absent', 'invalid_request', 'invalid_request_error', 'bad_request',
                            'context_length_exceeded', 'model_not_found', 'unsupported_parameter',
                            'InvalidRequest', 'BadRequest', 'other'),
    'speed_mode': ('absent', 'standard', 'fast', 'other'),
    'thinking_display_mode': ('absent', 'summarized', 'omitted', 'highlights', 'updates', 'other'),
}
BETA_FAMILIES = {
    'context-1m': 'beta_context_1m',
    'interleaved-thinking': 'beta_interleaved_thinking',
    'advanced-tool-use': 'beta_advanced_tool_use',
    'tool-search': 'beta_tool_search',
    # Claude Code's gateway-mode value is tool-search-tool-2025-10-19.
    'tool-search-tool': 'beta_tool_search',
    'context-management': 'beta_context_management',
    'effort': 'beta_effort',
    'adaptive-thinking': 'beta_adaptive_thinking',
    'structured-outputs': 'beta_structured_outputs',
    'claude-code': 'beta_claude_code',
    'prompt-caching': 'beta_prompt_caching',
    # Protocol families observed in the bundled Claude 2.1.287 registry.
    # Names are fixed categories, never copied from an incoming header. These
    # flags diagnose gateway compatibility; they do not enable or strip betas.
    'prompt-caching-scope': 'beta_prompt_caching_scope',
    'prompt-caching-evict': 'beta_prompt_caching_evict',
    'cache-keepalive': 'beta_cache_keepalive',
    'extended-cache-ttl': 'beta_extended_cache_ttl',
    'fast-mode': 'beta_fast_mode',
    'redact-thinking': 'beta_redact_thinking',
    'thinking-resumption': 'beta_thinking_resumption',
    'thinking-token-count': 'beta_thinking_token_count',
    'task-budgets': 'beta_task_budgets',
    'context-hint': 'beta_context_hint',
    'mid-conversation-system': 'beta_mid_conversation_system',
    'per-turn-control': 'beta_per_turn_control',
    'timing': 'beta_timing',
    'mid-conversation-tool-changes': 'beta_mid_conversation_tool_changes',
    'inline-tools': 'beta_inline_tools',
    'server-side-fallback': 'beta_server_side_fallback',
    'fallback-credit': 'beta_fallback_credit',
    'dangerous-tool-use': 'beta_dangerous_tool_use',
    'thinking-display-updates': 'beta_thinking_display_updates',
    'thinking-binding-controls': 'beta_thinking_binding_controls',
    'message-threads': 'beta_message_threads',
    'mid-conversation-system-clear-at': 'beta_mid_conversation_system_clear_at',
}
BOOLEANS = BOOLEANS | frozenset(BETA_FAMILIES.values())

MESSAGE_ROLES = {role: 'message_role_' + role + '_count' for role in ('user', 'assistant', 'system')}
CONTENT_TYPES = {kind: 'content_' + kind + '_count' for kind in (
    'text', 'thinking', 'redacted_thinking', 'tool_use', 'tool_result',
    'tool_addition', 'tool_removal', 'image', 'document')}
KNOWN_REQUEST_FIELDS = frozenset(('model', 'messages', 'max_tokens', 'stream', 'system',
    'tools', 'tool_choice', 'thinking', 'output_config', 'output_format', 'context_management',
    'temperature', 'top_p', 'top_k', 'service_tier', 'stop_sequences', 'metadata', 'speed'))
KNOWN_MESSAGE_FIELDS = frozenset(('role', 'content', 'output_config', 'clear_at'))


def load_options(root):
    path = Path(root) / 'proxy-options.json'
    options = dict(DEFAULT_OPTIONS)
    if path.exists():
        if path.stat().st_size > 4096:
            raise ValueError('proxy-options.json is too large.')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict) or value.keys() - options.keys() - LEGACY_CAPACITY_OPTIONS:
            raise ValueError('Unknown setting in proxy-options.json.')
        options.update({key: value[key] for key in options if key in value})
    for key, (low, high) in RANGES.items():
        if type(options[key]) is not int or not low <= options[key] <= high:
            raise ValueError('Invalid proxy option: ' + key)
    if type(options['loki_context_management_compat']) is not bool:
        raise ValueError('Invalid proxy option: loki_context_management_compat')
    if type(options['loki_thinking_display_compat']) is not bool:
        raise ValueError('Invalid proxy option: loki_thinking_display_compat')
    if type(options['loki_cache_ttl_compat']) is not bool:
        raise ValueError('Invalid proxy option: loki_cache_ttl_compat')
    if type(options['loki_tool_search_beta_compat']) is not bool:
        raise ValueError('Invalid proxy option: loki_tool_search_beta_compat')
    return options


def safe_record(record):
    """Also used by the support exporter: unknown fields and free text are dropped."""
    safe = {}
    for key, value in record.items():
        if key in NUMBERS and type(value) in (int, float):
            safe[key] = value
        elif key in WORDS and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value):
            safe[key] = value
        elif key in BOOLEANS and type(value) is bool:
            safe[key] = value
        elif key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            safe[key] = value
        elif key == 'request_id' and isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value):
            safe[key] = value
        elif key in ('time', 'updated_at', 'last_boot', 'collected_at', 'task_last_run', 'task_next_run',
                     'credential_expires_at', 'credential_issued_at', 'credential_observed_at',
                     'previous_credential_expires_at', 'previous_credential_observed_at',
                     'authorization_last_sample_at') and isinstance(value, str) and re.fullmatch(r'[0-9T:.+Z-]{20,40}', value):
            safe[key] = value
        elif key == 'version' and isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value):
            safe[key] = value
        elif key == 'frames' and isinstance(value, list):
            safe[key] = [frame for frame in value[-8:] if isinstance(frame, str) and
                           re.fullmatch(r'(?:server|adapter|usage_store|supervisor|diagnostics|app_config|model_selection|native_compat|parent_lifetime|authorization)\.py:[0-9]+:[A-Za-z_][A-Za-z_0-9]{0,80}', frame)]
    return safe


def _enum_value(field, value):
    if value is None:
        return 'absent'
    return value if isinstance(value, str) and value in ENUMS[field] else 'other'


def request_metadata(payload, path, beta_header):
    """Describe the forwarded structure, never text, tool/schema names, IDs or raw headers.

    This is observational only: malformed/unknown fields remain on the wire unchanged.
    A diagnostic failure must not prevent forwarding or expose exception text.
    """
    try:
        fields = {'request_metadata_version': 2, 'request_metadata_complete': True,
                  'endpoint_kind': {'/v1/messages': 'messages', '/v1/messages/count_tokens': 'count_tokens',
                                    '/v1/chat/completions': 'chat_completions'}.get(path, 'other')}
        messages = payload.get('messages')
        messages = messages if isinstance(messages, list) else []
        tools = payload.get('tools')
        tools = tools if isinstance(tools, list) else []
        fields.update(message_count=len(messages), tool_count=len(tools), deferred_tool_count=0,
                      tool_search_tool_count=0, tool_reference_count=0, cache_control_count=0, cache_control_1h_count=0)
        fields.update(dict.fromkeys((*MESSAGE_ROLES.values(), *CONTENT_TYPES.values(),
            'message_role_other_count', 'message_output_config_count', 'message_effort_count',
            'message_timing_count', 'message_clear_at_count', 'message_unknown_field_count',
            'content_other_count', 'inline_tool_definition_count', 'tool_strict_count',
            'tool_schema_non_object_count', 'cache_control_scope_count'), 0))
        fields['top_level_unknown_field_count'] = sum(k not in KNOWN_REQUEST_FIELDS for k in payload)

        def observe_block(block):
            if not isinstance(block, dict):
                fields['content_other_count'] += 1
                return
            kind = block.get('type')
            if kind == 'tool_reference':
                fields['tool_reference_count'] += 1
            else:
                field = CONTENT_TYPES.get(kind) if isinstance(kind, str) else None
                fields[field or 'content_other_count'] += 1
            if kind in ('tool_addition', 'tool_removal'):
                tool = block.get('tool')
                if isinstance(tool, dict) and tool.get('type') == 'tool_definition':
                    fields['inline_tool_definition_count'] += 1
            observe_cache(block)

        def observe_cache(block):
            cache = block.get('cache_control')
            if isinstance(cache, dict):
                fields['cache_control_count'] += 1
                fields['cache_control_1h_count'] += int(cache.get('ttl') == '1h')
                fields['cache_control_scope_count'] += int('scope' in cache)

        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fields['deferred_tool_count'] += int(tool.get('defer_loading') is True)
            fields['tool_strict_count'] += int(tool.get('strict') is True)
            # Built-in tools may have no input_schema. Count only present schemas;
            # never traverse schema properties, names, descriptions or examples.
            if 'input_schema' in tool:
                schema = tool['input_schema']
                fields['tool_schema_non_object_count'] += int(
                    not isinstance(schema, dict) or schema.get('type') != 'object')
            kind = tool.get('type')
            if isinstance(kind, str) and re.fullmatch(r'tool_search_tool_(?:regex|bm25)(?:_\d{8})?', kind):
                fields['tool_search_tool_count'] += 1
            observe_cache(tool)
        system = payload.get('system')
        if isinstance(system, str):
            fields['content_text_count'] += 1
        for block in system if isinstance(system, list) else []:
            observe_block(block)
        for message in messages:
            if not isinstance(message, dict):
                fields['message_role_other_count'] += 1
                continue
            role = message.get('role')
            role_field = MESSAGE_ROLES.get(role) if isinstance(role, str) else None
            fields[role_field or 'message_role_other_count'] += 1
            fields['message_unknown_field_count'] += sum(k not in KNOWN_MESSAGE_FIELDS for k in message)
            fields['message_clear_at_count'] += int('clear_at' in message)
            if 'output_config' in message:
                fields['message_output_config_count'] += 1
                config = message['output_config']
                if isinstance(config, dict):
                    fields['message_effort_count'] += int('effort' in config)
                    fields['message_timing_count'] += int('timing' in config)
            content = message.get('content')
            if isinstance(content, str):
                fields['content_text_count'] += 1
            for block in content if isinstance(content, list) else []:
                observe_block(block)
                # Tool references from search are inside a tool_result; never traverse tool inputs/schemas.
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    nested = block.get('content')
                    for child in nested if isinstance(nested, list) else []:
                        observe_block(child)
        thinking = payload.get('thinking')
        output = payload.get('output_config')
        choice = payload.get('tool_choice')
        fields['thinking_mode'] = _enum_value('thinking_mode', thinking.get('type') if isinstance(thinking, dict) else thinking)
        fields['thinking_display_mode'] = _enum_value('thinking_display_mode',
            thinking.get('display') if isinstance(thinking, dict) else None)
        fields['speed_mode'] = _enum_value('speed_mode', payload.get('speed'))
        fields['effort_mode'] = _enum_value('effort_mode', output.get('effort') if isinstance(output, dict) else output)
        fields['tool_choice_mode'] = _enum_value('tool_choice_mode', choice.get('type') if isinstance(choice, dict) else choice)
        output_format = output.get('format', payload.get('output_format')) if isinstance(output, dict) else payload.get('output_format')
        fields['output_format_mode'] = _enum_value('output_format_mode',
            output_format.get('type') if isinstance(output_format, dict) else output_format)
        for key in ('output_config', 'output_format', 'context_management', 'temperature', 'top_p',
                    'top_k', 'service_tier', 'stop_sequences'):
            fields['has_' + key] = key in payload
        for key, value in (('max_tokens', payload.get('max_tokens')),
                           ('thinking_budget_tokens', thinking.get('budget_tokens') if isinstance(thinking, dict) else None)):
            if type(value) is int and 0 <= value <= 2**31 - 1:
                fields[key] = value
        fields.update({key: False for key in BETA_FAMILIES.values()})
        fields.update(beta_count=0, beta_unknown_count=0)
        for beta in beta_header.split(',') if isinstance(beta_header, str) else []:
            if not beta.strip():
                continue
            fields['beta_count'] += 1
            match = re.fullmatch(r'([a-z0-9-]+)-(?:\d{4}-\d{2}-\d{2}|\d{8})', beta.strip())
            field = BETA_FAMILIES.get(match[1]) if match else None
            if field:
                fields[field] = True
            else:
                fields['beta_unknown_count'] += 1
        return safe_record(fields)
    except Exception:
        return {'request_metadata_version': 2, 'request_metadata_complete': False}


# Exact upstream wording for a refused anthropic-beta value (PROD, 2026-11-09). Only this
# boolean is recorded; the message itself and the refused value are never logged.
UPSTREAM_BETA_UNSUPPORTED = 'The requested anthropic-beta feature is not supported.'


def upstream_error_metadata(raw):
    """Exact known error types/codes only; no free-text classification or body logging."""
    fields = {'upstream_error_json': False}
    try:
        data = json.loads(raw)
        fields['upstream_error_json'] = True
        error = data.get('error') if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        fields['upstream_error_type'] = _enum_value('upstream_error_type', error.get('type'))
        fields['upstream_error_code'] = _enum_value('upstream_error_code', error.get('code'))
        fields['upstream_generic_rejection'] = error.get('message') == 'Upstream rejected the request.'
        fields['upstream_beta_unsupported'] = error.get('message') == UPSTREAM_BETA_UNSUPPORTED
    except (ValueError, TypeError, RecursionError):
        pass
    return fields


def exception_fields(exc):
    frames, trace = [], exc.__traceback__
    while trace:
        frames.append(f'{Path(trace.tb_frame.f_code.co_filename).name}:{trace.tb_lineno}:{trace.tb_frame.f_code.co_name}')
        trace = trace.tb_next
    return safe_record({'error_type': type(exc).__name__, 'errno': getattr(exc, 'errno', None),
                        'winerror': getattr(exc, 'winerror', None), 'frames': frames})


class QuietRotatingHandler(RotatingFileHandler):
    healthy = True

    def handleError(self, record):
        self.healthy = False  # A full disk must not terminate a model request or print its data.


class EventLog:
    def __init__(self, path, max_bytes=2*1024*1024, backups=3):
        self.handler = None
        try:
            self.handler = QuietRotatingHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
            self.handler.setFormatter(logging.Formatter('%(message)s'))
        except OSError:
            pass

    @property
    def healthy(self):
        return bool(self.handler and self.handler.healthy)

    def event(self, event, **fields):
        if not self.handler:
            return
        try:
            record = safe_record({'time': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                                  'pid': os.getpid(), 'version': PACKAGE_VERSION, 'event': event, **fields})
            self.handler.handle(logging.LogRecord('proxy', logging.INFO, '', 0,
                                                  json.dumps(record, allow_nan=False), (), None))
        except Exception:
            self.handler.healthy = False

    def close(self):
        if self.handler:
            self.handler.close()


def resources():
    result = {'threads': threading.active_count()}
    if os.name != 'nt':
        return result
    try:
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak', 'working', 'quota_peak_paged', 'quota_paged',
                                                   'quota_peak_nonpaged', 'quota_nonpaged', 'pagefile', 'peak_pagefile', 'private')]
        class Memory(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in ('total', 'available', 'total_page', 'available_page',
                                                       'total_virtual', 'available_virtual', 'extended')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetTickCount64.restype = ctypes.c_ulonglong
        result['os_uptime_seconds'] = round(kernel.GetTickCount64()/1000)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            result.update(rss_mb=round(counters.working/2**20, 1), peak_rss_mb=round(counters.peak/2**20, 1),
                          private_mb=round(counters.private/2**20, 1))
        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            result['available_memory_mb'] = round(memory.available/2**20, 1)
    except (OSError, AttributeError):
        pass
    return result
