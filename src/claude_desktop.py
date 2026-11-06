"""Opt-in Windows Claude Desktop gateway setup; no credentials are copied.

Uses HKCU where writable, or the App's local configuration library on an
unmanaged device. Records only constant settings for reversible removal.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from configure_claude import write_json
from paths import ROOT, SCRIPTS

POLICY_KEY = r'SOFTWARE\Policies\Claude'
BASE_URL = 'http://127.0.0.1:8787/desktop'
# REG_SZ is the portable format documented by Claude Desktop. JSON scalars
# are decoded by Desktop's policy reader. This placeholder is not a secret.
POLICY = {
    'inferenceProvider': 'gateway',
    'inferenceGatewayBaseUrl': BASE_URL,
    'inferenceGatewayApiKey': 'local-copilot-placeholder',
    'inferenceGatewayAuthScheme': 'bearer',
    'inferenceCredentialKind': 'static',
    'modelDiscoveryEnabled': 'true',
    'modelPrefer1mContext': 'true',
    'toolSearchEnabled': 'true',
    'chatTabEnabled': 'true',
    'coworkTabEnabled': 'true',
    'isClaudeCodeForDesktopEnabled': 'true',
}
OWNER = 'copilot-desktop-proxy/claude-desktop/v1'
# A fixed configuration-library entry id, never an account or conversation id.
LOCAL_ENTRY_ID = 'd127f92b-9fab-4e70-9516-207ef73ee6be'
LOCAL_VALUES = {key: True if value == 'true' else value for key, value in POLICY.items()}
LOCAL_META = {'appliedId': LOCAL_ENTRY_ID,
              'entries': [{'id': LOCAL_ENTRY_ID, 'name': 'Copilot Desktop Proxy'}]}
LOCAL_STATE = {'owner': OWNER, 'backend': 'local', 'values': LOCAL_VALUES}


def helper_values(script):
    values = {key: value for key, value in LOCAL_VALUES.items() if key != 'inferenceGatewayApiKey'}
    values.update(inferenceCredentialKind='helper-script', inferenceCredentialHelper=str(script),
                  inferenceCredentialHelperTtlSec=60, inferenceCredentialHelperTimeoutSec=180,
                  inferenceCredentialHelperSilentRefreshEnabled=True)
    return values


HELPER_VALUES = helper_values(SCRIPTS / 'claude-gateway-auth.ps1')
HELPER_STATE = {'owner': OWNER, 'backend': 'local', 'values': HELPER_VALUES}
# Releases up to 1.1.19 kept every script in the installation root.
LEGACY_HELPER_VALUES = helper_values(ROOT / 'claude-gateway-auth.ps1')
LEGACY_HELPER_STATE = {'owner': OWNER, 'backend': 'local', 'values': LEGACY_HELPER_VALUES}
LOCAL_STATES = (LOCAL_STATE, HELPER_STATE, LEGACY_HELPER_STATE)


class ConfigurationError(Exception):
    """A fixed, non-sensitive error code suitable for the launcher."""


def packaged_process():
    """True inside an MSIX-packaged app (e.g. a terminal hosted by such an app).

    Windows redirects that process's AppData writes into the package's private
    LocalCache, so Claude App would never see them: refuse instead of failing silently.
    """
    if os.name != 'nt':
        return False
    import ctypes
    length = ctypes.c_uint32(0)
    try:
        # APPMODEL_ERROR_NO_PACKAGE (15700) means an ordinary desktop process.
        return ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None) != 15700
    except (AttributeError, OSError):
        return False


def io_error(stage, error):
    reason = stage + ('_permission_denied' if isinstance(error, PermissionError) else '_io_error')
    result = ConfigurationError(reason)
    result.stage = stage
    result.winerror = getattr(error, 'winerror', None)
    result.io_type = type(error).__name__
    return result


def save_state(path, data):
    try:
        write_json(path, data)
    except OSError as error:
        raise io_error('state_write', error) from None


class RegistryPolicy:
    def __init__(self):
        import winreg
        self.reg = winreg

    def read(self, machine=False):
        reg = self.reg
        hive = reg.HKEY_LOCAL_MACHINE if machine else reg.HKEY_CURRENT_USER
        try:
            with reg.OpenKey(hive, POLICY_KEY, 0, reg.KEY_READ | reg.KEY_WOW64_64KEY) as key:
                return {name: (value, kind) for name, value, kind in
                        (reg.EnumValue(key, i) for i in range(reg.QueryInfoKey(key)[1]))}
        except FileNotFoundError:
            return {}

    def set(self, name, value):
        reg = self.reg
        try:
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, POLICY_KEY, 0,
                                 reg.KEY_SET_VALUE | reg.KEY_WOW64_64KEY) as key:
                reg.SetValueEx(key, name, 0, reg.REG_SZ, value)
        except OSError as error:
            raise io_error('registry_write', error) from None

    def can_write(self):
        """Request access without creating a key or writing a value."""
        reg = self.reg
        try:
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, POLICY_KEY, 0,
                                 reg.KEY_SET_VALUE | reg.KEY_WOW64_64KEY):
                    return True
            except FileNotFoundError:
                parent = POLICY_KEY.rsplit('\\', 1)[0]
                with reg.OpenKey(reg.HKEY_CURRENT_USER, parent, 0,
                                 reg.KEY_CREATE_SUB_KEY | reg.KEY_WOW64_64KEY):
                    return True
        except (PermissionError, FileNotFoundError):
            return False

    def delete(self, name):
        reg = self.reg
        with reg.OpenKey(reg.HKEY_CURRENT_USER, POLICY_KEY, 0,
                         reg.KEY_SET_VALUE | reg.KEY_WOW64_64KEY) as key:
            reg.DeleteValue(key, name)


def read_state(path):
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    # Never turn a modified state file into arbitrary registry deletion.
    if data not in ({'owner': OWNER, 'values': POLICY}, *LOCAL_STATES):
        raise ConfigurationError('state_conflict')
    return data


def machine_blocks(values):
    # Claude ignores HKCU if any directly stored SZ / EXPAND_SZ / DWORD exists
    # in HKLM, including an empty value or the unnamed default value.
    return any(kind in (1, 2, 4) for _, kind in values.values())


def local_configuration_present(local_appdata, roaming_appdata=None):
    # Do not replace an existing locally authored deployment with an HKCU
    # policy: managed settings shadow that entire profile, including safeguards.
    # File presence is sufficient; there is no need to read its credentials.
    roots = [Path(local_appdata)]
    if roaming_appdata:
        roots.append(Path(roaming_appdata))
    return any(any((root / 'Claude-3p' / 'configLibrary').glob('*.json')) for root in roots)


def apply(mode, registry, state_path, dry_run=False, local_config=False):
    state = read_state(state_path)
    if state in LOCAL_STATES:
        raise ConfigurationError('local_configuration_requires_local_handler')
    user = registry.read()
    machine = machine_blocks(registry.read(machine=True))
    owned = {name for name, value in POLICY.items() if user.get(name) == (value, 1)}
    conflicts = set(user) - owned if state else set(user)
    result = {'mode': mode, 'dry_run': dry_run, 'managed_by_this_proxy': state is not None,
              'configuration_source': 'registry',
              'machine_policy_present': machine, 'user_policy_conflict': bool(conflicts),
              'local_configuration_present': local_config,
              'configured': bool(state) and not machine and not conflicts and len(owned) == len(POLICY),
              'changed_values': 0, 'preserved_values': 0}
    if mode == 'status':
        return result
    if mode == 'enable':
        if local_config and state is None:
            raise ConfigurationError('local_configuration_conflict')
        if machine:
            raise ConfigurationError('machine_policy_conflict')
        if conflicts:
            raise ConfigurationError('user_policy_conflict')
        missing = set(POLICY) - owned if state else set(POLICY)
        result['changed_values'] = len(missing)
        if not dry_run:
            # Save ownership first, so an interrupted/partially failed install
            # remains recoverable. No previous values or secrets are backed up.
            save_state(state_path, {'owner': OWNER, 'values': POLICY})
            for name in sorted(missing):
                if registry.read().get(name) is not None:
                    raise ConfigurationError('policy_changed_during_setup')
                registry.set(name, POLICY[name])
            result['configured'] = True
            result['managed_by_this_proxy'] = True
        return result
    if mode != 'restore':
        raise ConfigurationError('unknown_mode')
    if state is None:
        return result
    if 'inferenceGatewayBaseUrl' in user and user['inferenceGatewayBaseUrl'] != (BASE_URL, 1):
        # A subsequently installed gateway may now depend on the shared flags.
        # Relinquish ownership without breaking that deployment.
        result['preserved_values'] = len(user)
        if not dry_run:
            state_path.unlink()
            result['managed_by_this_proxy'] = False
        return result
    # Even if the user has selected another gateway, preserve changed values
    # and remove only exact matches to settings that we created.
    result['changed_values'] = len(owned)
    result['preserved_values'] = len(set(user) - owned)
    if not dry_run:
        for name in sorted(owned):
            if registry.read().get(name) != (POLICY[name], 1):
                raise ConfigurationError('policy_changed_during_restore')
            registry.delete(name)
        state_path.unlink()
        result['configured'] = False
        result['managed_by_this_proxy'] = False
    return result


def read_local_file(path):
    # Only called for this integration's own files after validating its manifest.
    try:
        return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None
    except (ValueError, UnicodeError):
        raise ConfigurationError('local_configuration_changed') from None
    except OSError as error:
        raise io_error('local_configuration_read', error) from None


def create_local_file(path, expected):
    """Publish a complete new file without overwriting a concurrent App write.

    This integration is Windows-only: os.rename refuses an existing destination.
    """
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.copilot-config-', suffix='.tmp', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(expected, stream, indent=2)
            stream.write('\n')
        os.rename(temporary, path)
    except FileExistsError:
        raise ConfigurationError('local_configuration_changed_during_setup') from None
    except OSError as error:
        raise io_error('local_configuration_write', error) from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def apply_local(mode, registry, state_path, local_root, dry_run=False, roaming_root=None):
    state = read_state(state_path)
    owned = state in LOCAL_STATES
    user = registry.read()
    machine = machine_blocks(registry.read(machine=True))
    library = Path(local_root) / 'Claude-3p' / 'configLibrary'
    meta = library / '_meta.json'
    entry = library / (LOCAL_ENTRY_ID + '.json')
    active_values = state['values'] if owned else LOCAL_VALUES
    expected = {entry: active_values, meta: LOCAL_META}
    present = {path for path in expected if path.exists()}
    # Never inspect existing deployments until ownership has been established.
    values = {path: read_local_file(path) for path in present} if owned else {}
    matches = {path for path in present if owned and values[path] == expected[path]}
    changed = bool(present - matches)
    foreign = any(path not in expected for path in library.glob('*.json'))
    if roaming_root and Path(roaming_root) != Path(local_root):
        foreign = foreign or local_configuration_present(roaming_root)
    result = {'mode': mode, 'dry_run': dry_run, 'configuration_source': 'local',
              'managed_by_this_proxy': owned, 'machine_policy_present': machine,
              'user_policy_conflict': bool(user), 'local_configuration_present': bool(present) or foreign,
              'local_configuration_changed': changed, 'configured': owned and len(matches) == 2 and not user and not machine,
              'changed_values': 0, 'preserved_values': 0}
    if mode == 'status':
        return result
    if mode == 'enable':
        if machine:
            raise ConfigurationError('machine_policy_conflict')
        if user:
            raise ConfigurationError('user_policy_conflict')
        if foreign or changed:
            raise ConfigurationError('local_configuration_conflict')
        result['changed_values'] = len(LOCAL_VALUES) if len(matches) < 2 else 0
        if not dry_run:
            # A failed registry attempt with zero written values can migrate here.
            save_state(state_path, state if owned else LOCAL_STATE)
            for path in (entry, meta):
                if path in matches:
                    if read_local_file(path) != expected[path]:
                        raise ConfigurationError('local_configuration_changed_during_setup')
                else:
                    create_local_file(path, expected[path])
            result.update(configured=True, managed_by_this_proxy=True)
        return result
    if mode != 'restore':
        raise ConfigurationError('unknown_mode')
    if not owned:
        return result
    # Treat the library pointer and entry as one unit. If the user/App changed
    # either file (e.g. selected another deployment), preserve both and relinquish
    # ownership. Never edit or delete a user's other configuration or history.
    result['preserved_values'] = len(LOCAL_VALUES) if changed else 0
    result['changed_values'] = len(LOCAL_VALUES) if matches and not changed else 0
    if not dry_run:
        if not changed:
            for path in (meta, entry):
                if path in matches:
                    if read_local_file(path) != expected[path]:
                        raise ConfigurationError('local_configuration_changed_during_restore')
                    try:
                        path.unlink()
                    except OSError as error:
                        raise io_error('local_configuration_remove', error) from None
        state_path.unlink()
        result.update(configured=False, managed_by_this_proxy=False)
    return result


def configure_desktop(mode, registry, state_path, local_root, dry_run=False, roaming_root=None):
    if mode == 'migrate':
        try:
            read_state(state_path)
        except ConfigurationError:
            # Owned by another installation folder (or edited): never touch it.
            return {'mode': 'migrate', 'migrated': False, 'dry_run': dry_run, 'managed_by_this_proxy': False}
    state = read_state(state_path)
    if mode == 'enable-helper':
        return enable_local_helper(registry, state_path, local_root, dry_run, roaming_root)
    if mode == 'migrate':
        # Re-point a helper deployment after the installation layout changed.
        if state != LEGACY_HELPER_STATE:
            return {'mode': 'migrate', 'migrated': False, 'dry_run': dry_run}
        return {**enable_local_helper(registry, state_path, local_root, dry_run, roaming_root),
                'mode': 'migrate', 'migrated': not dry_run}
    if state in LOCAL_STATES:
        return apply_local(mode, registry, state_path, local_root, dry_run, roaming_root)
    local_config = local_configuration_present(local_root, roaming_root)
    if mode == 'enable':
        user = registry.read()
        # A manifest saved before a denied registry write is not permission to
        # shadow a local deployment created since that failure.
        if local_config and not user:
            raise ConfigurationError('local_configuration_conflict')
        if not user and not registry.can_write():
            return apply_local(mode, registry, state_path, local_root, dry_run, roaming_root)
        # Preserve policy/local-conflict precedence before checking write access.
        preview = apply(mode, registry, state_path, True, local_config)
        missing = any(user.get(name) != (value, 1) for name, value in POLICY.items())
        if missing and not registry.can_write():
            raise ConfigurationError('registry_write_permission_denied')
        if dry_run:
            return preview
    return apply(mode, registry, state_path, dry_run, local_config)


def enable_local_helper(registry, state_path, local_root, dry_run=False, roaming_root=None):
    """Explicit opt-in migration of this proxy's local deployment only."""
    state = read_state(state_path)
    if state not in LOCAL_STATES:
        raise ConfigurationError('helper_requires_owned_local_configuration')
    if registry.read() or machine_blocks(registry.read(machine=True)):
        raise ConfigurationError('policy_conflict')
    if roaming_root and Path(roaming_root) != Path(local_root) and local_configuration_present(roaming_root):
        raise ConfigurationError('local_configuration_conflict')
    library = Path(local_root) / 'Claude-3p' / 'configLibrary'
    entry = library / (LOCAL_ENTRY_ID + '.json')
    meta = library / '_meta.json'
    if read_local_file(meta) != LOCAL_META or any(p not in (entry, meta) for p in library.glob('*.json')):
        raise ConfigurationError('local_configuration_conflict')
    current = read_local_file(entry)
    # Claude seals static keys on first use. Explicit helper opt-in replaces this
    # field without decrypting it; all other deployment settings must match.
    legacy = {k: v for k, v in (current or {}).items() if k != 'inferenceGatewayApiKey'}
    expected_legacy = {k: v for k, v in LOCAL_VALUES.items() if k != 'inferenceGatewayApiKey'}
    if current not in (HELPER_VALUES, LEGACY_HELPER_VALUES) and legacy != expected_legacy:
        raise ConfigurationError('local_configuration_changed')
    if not dry_run:
        save_state(state_path, HELPER_STATE)
        if read_local_file(entry) != current:
            raise ConfigurationError('local_configuration_changed_during_setup')
        write_json(entry, HELPER_VALUES)
    return {'mode': 'enable-helper', 'configured': not dry_run, 'dry_run': dry_run,
            'configuration_source': 'local', 'managed_by_this_proxy': True}


@contextmanager
def state_lock(directory):
    """Serialize installers from different portable folders for this user."""
    import msvcrt
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / 'claude-desktop.lock').open('a+b')
    except OSError as error:
        raise io_error('state_lock', error) from None
    with handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ConfigurationError('another_configuration_is_running') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('enable', 'enable-helper', 'migrate', 'status', 'restore'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        directory = Path(os.environ['LOCALAPPDATA']) / 'CopilotDesktopProxy'
        path = directory / 'claude-desktop-integration.json'
        registry = RegistryPolicy()
        if os.environ.get('CLAUDE_USER_DATA_DIR') and args.mode in ('enable', 'enable-helper'):
            raise ConfigurationError('custom_desktop_profile_requires_manual_setup')
        if args.mode != 'status' and not args.dry_run and packaged_process():
            raise ConfigurationError('packaged_app_redirects_appdata')
        if args.mode == 'status' or args.dry_run:
            result = configure_desktop(args.mode, registry, path, os.environ['LOCALAPPDATA'],
                                       args.dry_run, os.environ.get('APPDATA'))
        else:
            with state_lock(directory):
                result = configure_desktop(args.mode, registry, path, os.environ['LOCALAPPDATA'],
                                           roaming_root=os.environ.get('APPDATA'))
        print(json.dumps({'ok': True, **result}))
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, ConfigurationError) else 'configuration_io_error'
        print(json.dumps({'ok': False, 'reason': reason,
                          'error_type': getattr(error, 'io_type', type(error).__name__),
                          'stage': getattr(error, 'stage', 'configuration'),
                          'winerror': getattr(error, 'winerror', None)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
