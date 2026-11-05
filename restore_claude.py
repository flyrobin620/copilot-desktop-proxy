"""Remove this project's Claude integration without copying or logging credentials."""
import argparse
import codecs
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile


PROXY_URL = 'http://127.0.0.1:8787'
LOCAL_PLACEHOLDER = 'local-copilot-placeholder'
MODEL_KEYS = {
    'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL',
    'ANTHROPIC_CUSTOM_MODEL_OPTION',
}
MANAGED_ENV_KEYS = MODEL_KEYS | {
    'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
    'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS', 'CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY',
    'ANTHROPIC_CUSTOM_MODEL_OPTION_NAME', 'ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION',
    'ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES',
}
# Remove our default only when unchanged; preserve a later user override.
MANAGED_ENV_DEFAULTS = {'ENABLE_TOOL_SEARCH': 'true'}
for _family in ('OPUS', 'SONNET', 'HAIKU'):
    for _suffix in ('', '_NAME', '_DESCRIPTION', '_SUPPORTED_CAPABILITIES'):
        MANAGED_ENV_KEYS.add('ANTHROPIC_DEFAULT_' + _family + '_MODEL' + _suffix)

COPILOT_MODEL = re.compile(r'copilot-(?:auto|opus|sonnet|haiku)(?:\[1m\])?', re.I)
DESKTOP_MODEL = re.compile(r'claude-(?:opus|sonnet|haiku)-[0-9][0-9.\-]*(?:\[1m\])?', re.I)
# Match the exact command shape emitted by configure_claude, including an older
# install location, without opening that location or matching arbitrary commands.
OWN_HOOK = re.compile(
    r'powershell\.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File '
    r'"[^"\r\n]+[\\/]agency-start\.ps1"', re.I)
BEGIN = '# BEGIN COPILOT DESKTOP PROXY'
END = '# END COPILOT DESKTOP PROXY'


def managed_model(value, include_concrete=False):
    return isinstance(value, str) and bool(
        COPILOT_MODEL.fullmatch(value) or (include_concrete and DESKTOP_MODEL.fullmatch(value)))


def restore_settings(original):
    if not isinstance(original, dict) or not isinstance(original.get('env', {}), dict):
        raise ValueError('Settings and env must be objects.')
    data = deepcopy(original)
    removed_hooks = 0
    if 'hooks' in data:
        hooks = data['hooks']
        if not isinstance(hooks, dict):
            raise ValueError('Hooks must be an object.')
        if 'SessionStart' in hooks:
            groups = hooks['SessionStart']
            if not isinstance(groups, list):
                raise ValueError('SessionStart must be an array.')
            kept_groups = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                    raise ValueError('Invalid SessionStart group.')
                remaining = []
                group_removed = 0
                for hook in group['hooks']:
                    if not isinstance(hook, dict):
                        raise ValueError('Invalid SessionStart hook.')
                    command = hook.get('command')
                    if (hook.get('type') == 'command' and isinstance(command, str)
                            and OWN_HOOK.fullmatch(command)):
                        removed_hooks += 1
                        group_removed += 1
                    else:
                        remaining.append(hook)
                if not group_removed:
                    kept_groups.append(group)
                elif remaining:
                    kept_groups.append({**group, 'hooks': remaining})
            if removed_hooks:
                if kept_groups:
                    hooks['SessionStart'] = kept_groups
                else:
                    hooks.pop('SessionStart')
                if not hooks:
                    data.pop('hooks')

    env = data.get('env', {})
    base_url = env.get('ANTHROPIC_BASE_URL')
    owned_route = isinstance(base_url, str) and base_url.rstrip('/') == PROXY_URL
    # A different gateway may have been installed since Copilot. Remove our
    # startup hooks but leave that gateway's routing and model choices intact.
    reset_overrides = owned_route or (removed_hooks > 0 and not base_url)
    removed_env = 0
    if reset_overrides:
        for key, value in MANAGED_ENV_DEFAULTS.items():
            if env.get(key) == value:
                env.pop(key)
                removed_env += 1
        for key in MANAGED_ENV_KEYS:
            if key in env:
                env.pop(key)
                removed_env += 1
    if env.get('ANTHROPIC_AUTH_TOKEN') == LOCAL_PLACEHOLDER:
        env.pop('ANTHROPIC_AUTH_TOKEN')
        removed_env += 1
    if 'env' in data and not env and removed_env:
        data.pop('env')

    removed_model = False
    removed_options = 0
    if reset_overrides:
        if managed_model(data.get('model'), include_concrete=True):
            data.pop('model')
            removed_model = True
        if isinstance(data.get('availableModels'), list):
            previous = data['availableModels']
            kept = [m for m in previous if not managed_model(m, include_concrete=True)]
            removed_options = len(previous) - len(kept)
            if removed_options:
                if kept:
                    data['availableModels'] = kept
                else:
                    data.pop('availableModels')
    return data, {
        'env_keys_removed': removed_env, 'model_override_removed': removed_model,
        'model_options_removed': removed_options, 'session_hooks_removed': removed_hooks,
    }


def restore_profile(raw):
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        prefix = raw[:2]
        encoding = 'utf-16-le' if prefix == codecs.BOM_UTF16_LE else 'utf-16-be'
    else:
        prefix = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b''
        encoding = 'utf-8'
    lines = raw[len(prefix):].decode(encoding).splitlines(keepends=True)
    kept, inside, removed = [], False, 0
    for line in lines:
        marker = line.rstrip('\r\n')
        if marker == BEGIN:
            if inside:
                raise ValueError('Nested integration marker.')
            inside = True
            removed += 1
        elif marker == END:
            if not inside:
                raise ValueError('Unmatched integration marker.')
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise ValueError('Unclosed integration marker.')
    return prefix + ''.join(kept).encode(encoding), removed


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key.')
        result[key] = value
    return result


def replace_file(path, contents):
    # No backup: settings may hold authentication values. Replace only in the
    # original directory and do not emit content on errors.
    descriptor, temporary = tempfile.mkstemp(prefix='.claude-restore-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restore(settings_path, documents_path, dry_run=False):
    changes = []
    summary = {'dry_run': dry_run, 'settings_changed': False, 'profiles_changed': 0,
               'profile_blocks_removed': 0, 'env_keys_removed': 0,
               'model_override_removed': False, 'model_options_removed': 0,
               'session_hooks_removed': 0}
    if settings_path.exists():
        raw = settings_path.read_bytes()
        original = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object)
        updated, counts = restore_settings(original)
        summary.update(counts)
        if updated != original:
            contents = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            changes.append((settings_path, raw, contents))
            summary['settings_changed'] = True
    for folder in ('WindowsPowerShell', 'PowerShell'):
        path = documents_path / folder / 'profile.ps1'
        if not path.exists():
            continue
        raw = path.read_bytes()
        contents, blocks = restore_profile(raw)
        if contents != raw:
            changes.append((path, raw, contents))
            summary['profiles_changed'] += 1
            summary['profile_blocks_removed'] += blocks
    # Validate every input before changing any of them. Detect concurrent edits
    # so a preview/read cannot silently overwrite a newly changed file.
    if not dry_run:
        for path, original, _ in changes:
            if path.read_bytes() != original:
                raise ValueError('Configuration changed during restore; retry.')
        for path, _, contents in changes:
            replace_file(path, contents)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--documents', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        summary = restore(args.settings, args.documents, args.dry_run)
    except (OSError, ValueError, UnicodeError) as error:
        # JSON decoder/IO exceptions can contain source fragments or file paths.
        print(json.dumps({'restored': False, 'error_type': type(error).__name__}))
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
