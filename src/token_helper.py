"""Windows WAM helper. The acquire subcommand uses private stdin/stdout pipes only."""
import argparse
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import time

from token_refresh import (CLIENT_ID, SCOPE, RefreshError, claims, profile_dir, validate_token,
                           write_private_json)


def force_acquire(app, account, token_to_renew):
    """Pinned MSAL 1.39 adapter: force_refresh alone skips only its Python cache.

    The broker's native cache needs AccessTokenToRenew as well. This hook is
    confined to this single-call helper process, restored immediately, and never
    modifies installed packages. The parent enforces a process-level timeout.
    """
    import msal
    from msal import broker
    if msal.__version__ != '1.39.0':
        raise RefreshError('broker_unavailable')
    original = broker._build_msal_runtime_auth_params

    def build(*args, **kwargs):
        params = original(*args, **kwargs)
        params.set_access_token_to_renew(token_to_renew)
        return params

    broker._build_msal_runtime_auth_params = build
    try:
        return app.acquire_token_silent_with_error([SCOPE], account=account, force_refresh=True)
    finally:
        broker._build_msal_runtime_auth_params = original


def application():
    import msal
    from msal_extensions import FilePersistenceWithDataProtection, PersistedTokenCache
    folder = profile_dir()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        cache = PersistedTokenCache(FilePersistenceWithDataProtection(str(folder / 'token_cache.bin')))
    except Exception:
        raise RefreshError('cache_unavailable') from None
    return msal.PublicClientApplication(CLIENT_ID,
        authority='https://login.microsoftonline.com/common', enable_broker_on_windows=True,
        token_cache=cache, timeout=15)


def acquire(settings, force=False, interactive=False, token_to_renew=None):
    app = application()
    accounts = [a for a in app.get_accounts(username=settings['username'])
                if a.get('local_account_id', '').casefold() == settings['object_id'].casefold()
                and settings['tenant_id'].casefold() in (
                    a.get('realm', '').casefold(),
                    a.get('home_account_id', '').rsplit('.', 1)[-1].casefold())]
    if len(accounts) > 1:
        raise RefreshError('account_mismatch')
    result = None
    if accounts:
        if force and not token_to_renew:
            cached = app.acquire_token_silent_with_error([SCOPE], account=accounts[0])
            token_to_renew = (cached or {}).get('access_token')
        result = (force_acquire(app, accounts[0], token_to_renew) if force and token_to_renew else
                  app.acquire_token_silent_with_error([SCOPE], account=accounts[0], force_refresh=force))
    if not result or 'access_token' not in result:
        if not interactive:
            error = (result or {}).get('error', '')
            raise RefreshError('temporarily_unavailable' if error in ('temporarily_unavailable', 'server_error')
                               else 'interaction_required')
        app.acquire_token_interactive([SCOPE], login_hint=settings['username'],
                                      parent_window_handle=app.CONSOLE_WINDOW_HANDLE)
        # Picker hints are not an identity guarantee; reacquire for the exact identity.
        return acquire(settings, force=force, interactive=False)
    token = result['access_token']
    identity = result.get('id_token_claims') or claims(token)
    if (identity.get('oid', '').casefold() != settings['object_id'].casefold()
            or identity.get('tid', '').casefold() != settings['tenant_id'].casefold()):
        raise RefreshError('account_mismatch')
    lifetime = result.get('expires_in', 0)
    if type(lifetime) not in (int, float) or lifetime <= 60:
        raise RefreshError('refresh_failed')
    return validate_token(token, time.time() + lifetime, settings)


def copilot_identity():
    """The account selected in Copilot Code, if that optional app is present."""
    from app_config import load_config
    try:
        _, headers, _ = load_config()
    except Exception:
        return {}
    return claims(headers['Authorization'].removeprefix('Bearer '))


def interactive_identity(username=None):
    """Microsoft sign-in without any Copilot app; returns ID claims only."""
    app = application()
    result = app.acquire_token_interactive([SCOPE], login_hint=username,
                                          parent_window_handle=app.CONSOLE_WINDOW_HANDLE)
    if not result or 'access_token' not in result:
        raise RefreshError('interaction_required')
    return result.get('id_token_claims') or claims(result['access_token'])


def gateway_settings():
    """Prefer the gateway values of an installed Copilot Code, else built-in defaults."""
    from app_config import DEFAULT_HEADERS, DEFAULT_UPSTREAM, read_provider
    try:
        upstream, headers = read_provider()
        return upstream, {**DEFAULT_HEADERS, **headers}
    except Exception:
        return DEFAULT_UPSTREAM, dict(DEFAULT_HEADERS)


def publish(identity, interactive=False):
    """Validate a standalone profile end to end, then publish it atomically."""
    from app_config import fetch_models
    upstream, headers = gateway_settings()
    settings = {'enabled': True, 'source': 'standalone', 'python': sys.executable,
                'username': identity.get('preferred_username') or identity.get('upn'),
                'tenant_id': identity.get('tid'), 'object_id': identity.get('oid'),
                'upstream': upstream, 'headers': headers}
    if not all(isinstance(settings[k], str) and settings[k] for k in ('username', 'tenant_id', 'object_id')):
        raise RefreshError('account_mismatch')
    token, expiry = acquire(settings, interactive=interactive)
    try:
        models = fetch_models(upstream, {**headers, 'Authorization': 'Bearer ' + token})
    except Exception:
        raise RefreshError('model_catalog_unavailable') from None
    write_private_json(profile_dir() / 'models.json', models)
    write_private_json(profile_dir() / 'settings.json', settings)
    return {'ok': True, 'configured': True, 'source': 'standalone', 'models': len(models),
            'credential_expires_in_seconds': round(expiry-time.time())}


def initialize(interactive, username=None):
    identity = copilot_identity()
    current = (identity.get('preferred_username') or identity.get('upn') or '').casefold()
    if username and current != username.casefold():
        identity = {}
    if not identity:
        if not interactive:
            raise RefreshError('interaction_required')
        identity = interactive_identity(username)
    return publish(identity, interactive=interactive)


def migrate():
    """Silently convert a database-bound profile; never opens sign-in UI."""
    current = json.loads((profile_dir() / 'settings.json').read_text(encoding='utf-8'))
    if current.get('source') == 'standalone' and current.get('python') == sys.executable:
        return {'ok': True, 'configured': True, 'source': 'standalone', 'migrated': False}
    identity = {'preferred_username': current.get('username'), 'tid': current.get('tenant_id'),
                'oid': current.get('object_id')}
    return {**publish(identity), 'migrated': True}


def status():
    """Metadata-only profile summary for installers; no network or broker calls."""
    path = profile_dir() / 'settings.json'
    if not path.exists():
        return {'ok': True, 'configured': False}
    current = json.loads(path.read_text(encoding='utf-8'))
    python = current.get('python') if isinstance(current.get('python'), str) else ''
    return {'ok': True, 'configured': current.get('enabled') is True,
            'source': 'standalone' if current.get('source') == 'standalone' else 'copilot_db',
            'python': python, 'python_exists': bool(python) and Path(python).is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('initialize', 'migrate', 'status', 'acquire', 'check'))
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--username', help='Microsoft account to sign in with (login hint).')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        # SDK output must not corrupt the pipe protocol or expose auth details.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if args.mode == 'initialize':
                value = initialize(args.interactive, args.username)
            elif args.mode == 'migrate':
                value = migrate()
            elif args.mode == 'status':
                value = status()
            else:
                message = json.load(sys.stdin) if args.mode == 'acquire' else {
                    'settings': json.loads((profile_dir() / 'settings.json').read_text()), 'force': args.force}
                token, expiry = acquire(message['settings'], force=message.get('force', False),
                                       token_to_renew=message.get('token_to_renew'))
                value = {'ok': True, 'expires_at': expiry,
                         'credential_expires_in_seconds': round(expiry-time.time())}
                if args.mode == 'acquire':
                    value['access_token'] = token
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RefreshError) else 'helper_unavailable'
        value = {'ok': False, 'reason': reason}
    print(json.dumps(value))
    return 0 if value['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
