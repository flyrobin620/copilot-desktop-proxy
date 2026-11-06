"""Merge opt-in gateway settings without logging or backing up authentication values."""
import argparse
import json
from pathlib import Path
import os
import tempfile
from restore_claude import OWN_HOOK, PROXY_URL

from paths import ROOT


def configure(path, template=None, quiet=False, migrate_legacy_beta=False, dry_run=False):
    data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get('env', {}), dict):
        raise ValueError('Expected settings and env to be JSON objects.')
    template = template or json.loads((ROOT / 'config' / 'claude-settings.json').read_text(encoding='utf-8-sig'))
    env = data.setdefault('env', {})
    # Explicit upgrade only: remove the 0/1 override shipped by earlier versions.
    # Ordinary model refresh must preserve a user's later beta preference.
    beta_key = 'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'
    def own_hook(hook):
        command = hook.get('command')
        return (hook.get('type') == 'command' and isinstance(command, str)
                and OWN_HOOK.fullmatch(command))

    # The exact emitted hook shape also recognizes an older package directory;
    # upgrading must not require opening or executing that old location.
    installed = any(own_hook(h)
                    for group in data.get('hooks', {}).get('SessionStart', []) for h in group.get('hooks', []))
    if (migrate_legacy_beta and installed and env.get('ANTHROPIC_BASE_URL') == PROXY_URL
            and beta_key not in template['env'] and env.get(beta_key) in ('0', '1')):
        env.pop(beta_key)
    # Generated concrete IDs use CLI capability detection plus the context hint.
    # Drop stale family overrides so an old long_context flag cannot defeat an opt-out.
    for family in ('OPUS', 'SONNET', 'HAIKU'):
        key = 'ANTHROPIC_DEFAULT_' + family + '_MODEL_SUPPORTED_CAPABILITIES'
        if key not in template['env']:
            env.pop(key, None)
    # Leave the existing auth field in place; the loopback gateway ignores client credentials.
    # If absent, use the nonsecret client placeholder already supplied by the local profile.
    for key, value in template['env'].items():
        if key == 'ANTHROPIC_AUTH_TOKEN' and key in env:
            continue
        env[key] = value
    env['ANTHROPIC_MODEL'] = template['model']
    data['model'] = template['model']
    if isinstance(data.get('availableModels'), list):
        family_models = [template['env']['ANTHROPIC_DEFAULT_' + family + '_MODEL']
                         for family in ('OPUS', 'SONNET', 'HAIKU')]
        for model in (template['model'], *family_models, 'copilot-auto','copilot-opus','copilot-sonnet','copilot-haiku', *template.get('availableModels', [])):
            if model not in data['availableModels']: data['availableModels'].append(model)
    command = 'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + str(ROOT / 'scripts' / 'agency-start.ps1') + '"'
    hooks = data.setdefault('hooks', {}).setdefault('SessionStart', [])
    # Replace our previous hook only, preserving Agency and user hooks.
    kept = []
    for group in hooks:
        remaining = [h for h in group.get('hooks', []) if not own_hook(h)]
        if remaining: kept.append({**group, 'hooks': remaining})
    kept.append({'hooks':[{'type':'command','command':command,'timeout':30}]})
    data['hooks']['SessionStart'] = kept
    if not dry_run:
        write_json(path, data)
    if not quiet:
        print(json.dumps({'configured':not dry_run,'dry_run':dry_run,'base_url':env['ANTHROPIC_BASE_URL'],
                          'default_model':data['model'],'custom_option':env['ANTHROPIC_CUSTOM_MODEL_OPTION_NAME']}))


def write_json(path, data):
    contents = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
    if path.exists() and path.read_text(encoding='utf-8-sig') == contents:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.copilot-settings-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
            output.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings',type=Path,required=True)
    parser.add_argument('--template',type=Path,help='Use the freshly generated model settings.')
    parser.add_argument('--migrate-legacy-beta',action='store_true',
                        help='Remove a legacy beta override from this installation during an explicit upgrade.')
    parser.add_argument('--dry-run', action='store_true', help='Validate the settings merge without writing files.')
    arguments=parser.parse_args()
    try:
        template = json.loads(arguments.template.read_text(encoding='utf-8-sig')) if arguments.template else None
        configure(arguments.settings, template, migrate_legacy_beta=arguments.migrate_legacy_beta,
                  dry_run=arguments.dry_run)
    except Exception:
        # Never echo user configuration or a JSON parser's input in a launcher error.
        print(json.dumps({'configured': False, 'reason': 'settings_validation_or_write_failed'}))
        raise SystemExit(1)
