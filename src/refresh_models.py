"""Refresh local Claude model settings from the proxy's current desktop catalog."""
import argparse
import json
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from configure_claude import ROOT, configure, write_json
from model_selection import ALIASES, client_model_id, resolve_model, default_auto_model, default_client_model_id, use_1m_context


def build_settings(template, models):
    settings = json.loads(json.dumps(template))
    catalog = [{'model_id': m.get('upstream_model', client_model_id(m['id']))}
               for m in models if m['id'].startswith('claude-')]
    model_details = {client_model_id(m['id']): m for m in models}
    for family in ('opus', 'sonnet', 'haiku'):
        try:
            current = client_model_id(resolve_model('copilot-' + family, catalog))
            key = 'ANTHROPIC_DEFAULT_' + family.upper() + '_MODEL'
            settings['env'][key] = default_client_model_id(current)
            # Claude Code deduplicates discovered models against these family rows.
            # Name the surviving row after the concrete version it actually selects.
            settings['env'][key + '_NAME'] = model_details[current].get('display_name') or current
            settings['env'][key + '_DESCRIPTION'] = 'Copilot desktop · fixed version (' + current + ')'
            if use_1m_context(current):
                settings['env'][key + '_DESCRIPTION'] += ' · 1M CLI window'
            if family == 'haiku':
                settings['env']['ANTHROPIC_SMALL_FAST_MODEL'] = default_client_model_id(current)
        except ValueError:
            pass
    # Concrete IDs give Claude Code the correct per-version capability detection.
    for key in list(settings['env']):
        if key.startswith('ANTHROPIC_DEFAULT_') and key.endswith('_SUPPORTED_CAPABILITIES'):
            settings['env'].pop(key)
    auto = default_auto_model(catalog)
    settings['model'] = auto
    for key in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_MODEL', 'ANTHROPIC_CUSTOM_MODEL_OPTION'):
        settings['env'][key] = auto
    large = auto.endswith('[1m]')
    settings['env']['ANTHROPIC_CUSTOM_MODEL_OPTION_NAME'] = (
        'Copilot Auto (1M · latest desktop Opus)' if large else 'Copilot Auto (latest desktop Opus)')
    settings['env']['ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION'] = (
        'Newest desktop Opus on each request. ' +
        ('1M context; CLI reserves output and compaction space.' if large else
         '1M disabled for this model by local policy; using the standard CLI window.'))
    caps_key = 'ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES'
    capabilities = [c for c in settings['env'].get(caps_key, '').split(',') if c and c != 'long_context']
    if large:
        capabilities.append('long_context')
    settings['env'][caps_key] = ','.join(capabilities)
    return settings, catalog


def refresh(port=8787, root=ROOT, global_path=None):
    opener = build_opener(ProxyHandler({}))
    with opener.open('http://127.0.0.1:' + str(port) + '/v1/models', timeout=10) as response:
        models = json.load(response)['data']
    template = json.loads((root / 'config' / 'claude-settings.json').read_text(encoding='utf-8-sig'))
    settings, catalog = build_settings(template, models)
    write_json(root / '.models' / 'claude-settings.json', settings)
    # Only refresh a global profile already opted into this gateway. No account values leave it.
    global_path = global_path or Path.home() / '.claude' / 'settings.json'
    if global_path.exists():
        existing = json.loads(global_path.read_text(encoding='utf-8-sig'))
        hook_commands = [h.get('command', '') for group in existing.get('hooks', {}).get('SessionStart', [])
                         for h in group.get('hooks', [])]
        # Also adopt the hook of releases that kept scripts in the root; configure() re-points it.
        hook_paths = (str(root / 'scripts' / 'agency-start.ps1'), str(root / 'agency-start.ps1'))
        installed = any(path in command for command in hook_commands for path in hook_paths)
        if installed and existing.get('env', {}).get('ANTHROPIC_BASE_URL') == 'http://127.0.0.1:8787':
            # Model discovery must not undo an explicit tool-search preference.
            # Enable Agency applies the template defaults during installation.
            if 'ENABLE_TOOL_SEARCH' in existing.get('env', {}):
                settings['env']['ENABLE_TOOL_SEARCH'] = existing['env']['ENABLE_TOOL_SEARCH']
            if isinstance(existing.get('availableModels'), list):
                settings['availableModels'] = [
                    default_client_model_id(m.get('upstream_model', m['id']))
                    if m['id'].startswith('claude-') else m['id'] for m in models
                ] + list(ALIASES) + [settings['model']]
            configure(global_path, settings, quiet=True)
    print(json.dumps({'models_refreshed':len(catalog), 'default_alias':settings['model'],
                      'default_model':resolve_model('copilot-auto',catalog)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    refresh(args.port)
