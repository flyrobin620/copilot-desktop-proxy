"""Read Copilot's own provider settings; keep authorization only in memory."""
import base64
import ctypes
from contextlib import closing
from ctypes import wintypes
import json
import hashlib
import os
from pathlib import Path
import sqlite3

APP_ROOT = Path(os.environ['LOCALAPPDATA']) / 'Microsoft/Copilot/User Data/CopilotCodingHome'
ALLOWED_UPSTREAMS = frozenset((
    'https://loki.delve.office.com/api/v1/llmapi',
    'https://df.loki.delve.office.com/api/v1/llmapi',
))


def validate_upstream(value):
    """Accept only verified Code backends, before accessing authorization."""
    if not isinstance(value, str) or value.rstrip('/') not in ALLOWED_UPSTREAMS:
        raise RuntimeError('Copilot upstream changed; review the configured endpoint before using it.')
    return value.rstrip('/')


def locate_db():
    if os.environ.get('COPILOT_PROXY_DB'):
        return Path(os.environ['COPILOT_PROXY_DB']).resolve()
    candidates = sorted(APP_ROOT.glob('*/data.db'), key=lambda p: p.stat().st_mtime, reverse=True)
    if len(candidates) != 1:
        raise RuntimeError('Expected one Copilot account database; set COPILOT_PROXY_DB explicitly.')
    return candidates[0]


def unprotect(value, provider_id):
    if not value.startswith('v1.dpapi.'):
        raise RuntimeError('Unsupported protected authorization format.')
    encrypted = base64.b64decode(value[len('v1.dpapi.'):], validate=True)

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    raw = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
    source, target = Blob(len(encrypted), raw), Blob()
    scope = hashlib.sha256(b'Undocked.ModelProviderAuthorization.v1\0' + provider_id.encode()).hexdigest()
    entropy = hashlib.sha256(b'dpapi-entropy\0' + scope.encode()).digest()
    entropy_raw = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
    entropy_blob = Blob(len(entropy), entropy_raw)
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy_blob), None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Windows could not decrypt Copilot authorization for this user.')
    try:
        return ctypes.string_at(target.data, target.size).decode('utf-8')
    finally:
        ctypes.memset(target.data, 0, target.size)
        kernel32.LocalFree(target.data)


def load_config():
    db = locate_db()
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        provider = conn.execute('SELECT settings_json FROM model_providers WHERE id=?',
                                ('undocked-anthropic',)).fetchone()
        if provider is None:
            raise RuntimeError('Copilot has no Anthropic provider configured.')
        settings = json.loads(provider['settings_json'])
        models = [dict(row) for row in conn.execute(
            'SELECT model_id, supported_reasoning_efforts FROM provider_models WHERE provider_id=?',
            ('undocked-anthropic',))]
    upstream = validate_upstream(settings.get('baseUrl'))
    authorization = unprotect(settings['authorizationProtected'], 'undocked-anthropic')
    if not authorization.startswith('Bearer ') or '\r' in authorization or '\n' in authorization:
        raise RuntimeError('Copilot authorization has an unexpected format.')
    headers = json.loads(settings['headersJson'])
    approved = {'X-ClientFeature', 'X-ClientScenario', 'X-ClientType', 'X-Llm-Api-Version'}
    headers = {key: value for key, value in headers.items() if key in approved}
    headers['Authorization'] = authorization
    return upstream, headers, models


if __name__ == '__main__':
    try:
        upstream, headers, models = load_config()
        print(json.dumps({'authorization': 'available', 'upstream': upstream,
                          'models': [m['model_id'] for m in models]}))
    except Exception as exc:
        print(json.dumps({'authorization': 'unavailable', 'error_type': type(exc).__name__,
                          'error': str(exc) if isinstance(exc, RuntimeError) else 'Configuration read failed.'}))
        raise SystemExit(1)
