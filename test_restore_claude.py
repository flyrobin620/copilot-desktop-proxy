import codecs
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from configure_claude import configure
from refresh_models import build_settings
from restore_claude import BEGIN, END, restore


class RestoreClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / 'settings.json'
        self.documents = self.root / 'Documents'

    def write_settings(self, data):
        self.settings.write_text(json.dumps(data), encoding='utf-8')

    def add_profile(self, folder, encoding='utf-8-sig'):
        path = self.documents / folder / 'profile.ps1'
        path.parent.mkdir(parents=True)
        before = '# personal settings\r\nfunction personal { Get-Date }\r\n'
        after = 'Set-Alias personal-date Get-Date\r\n'
        path.write_bytes((before + BEGIN + '\r\n# owned loader\r\n' + END + '\r\n' + after).encode(encoding))
        return path, (before + after).encode(encoding)

    def test_enable_refresh_restore_preserves_personal_settings_and_is_idempotent(self):
        # Authentication slots are empty deliberately: no credentials are used,
        # but the test still proves existing slots are not deleted or replaced.
        original = {
            'env': {'UNRELATED': 'keep', 'ANTHROPIC_AUTH_TOKEN': '', 'ANTHROPIC_API_KEY': ''},
            'permissions': {'defaultMode': 'dontAsk'}, 'effortLevel': 'high',
            'availableModels': ['custom-model'],
            'hooks': {'SessionStart': [{'matcher': 'startup', 'hooks': [
                {'type': 'command', 'command': 'echo existing'}]}],
                'Stop': [{'hooks': [{'type': 'command', 'command': 'echo stopped'}]}]},
        }
        self.write_settings(original)
        template = json.loads((Path(__file__).parent / 'claude-settings.json').read_text(encoding='utf-8'))
        generated, _ = build_settings(template, [
            {'id': 'claude-opus-5', 'display_name': 'Claude Opus 5'},
            {'id': 'claude-haiku-4-5', 'display_name': 'Claude Haiku 4.5'},
        ])
        generated['availableModels'] = ['claude-opus-5', 'claude-haiku-4-5']
        configure(self.settings, generated, quiet=True)
        profiles = [self.add_profile('WindowsPowerShell'), self.add_profile('PowerShell', 'utf-16')]
        result = restore(self.settings, self.documents)
        self.assertTrue(result['settings_changed'])
        self.assertEqual(result['profiles_changed'], 2)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')), original)
        for path, expected in profiles:
            self.assertEqual(path.read_bytes(), expected)
        snapshot = self.settings.read_bytes()
        self.assertFalse(restore(self.settings, self.documents)['settings_changed'])
        self.assertEqual(self.settings.read_bytes(), snapshot)

    def test_placeholder_and_integration_only_settings_are_removed(self):
        configure(self.settings, quiet=True)
        restore(self.settings, self.documents)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')), {})

    def test_restore_preserves_user_changed_tool_search_mode(self):
        configure(self.settings, quiet=True)
        data = json.loads(self.settings.read_text(encoding='utf-8'))
        data['env']['ENABLE_TOOL_SEARCH'] = 'false'
        self.write_settings(data)
        restore(self.settings, self.documents)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')),
                         {'env': {'ENABLE_TOOL_SEARCH': 'false'}})

    def test_preview_does_not_write_and_missing_files_are_not_created(self):
        self.assertFalse(restore(self.settings, self.documents)['settings_changed'])
        self.assertFalse(self.settings.exists())
        configure(self.settings, quiet=True)
        path, _ = self.add_profile('PowerShell')
        before = self.settings.read_bytes(), path.read_bytes()
        self.assertTrue(restore(self.settings, self.documents, dry_run=True)['settings_changed'])
        self.assertEqual((self.settings.read_bytes(), path.read_bytes()), before)

    def test_a_new_gateway_keeps_its_routing_models_and_real_auth_slot(self):
        configure(self.settings, quiet=True)
        data = json.loads(self.settings.read_text(encoding='utf-8'))
        data['env'].update({'ANTHROPIC_BASE_URL': 'https://gateway.example.test',
                            'ANTHROPIC_MODEL': 'custom', 'ANTHROPIC_AUTH_TOKEN': ''})
        data['model'] = 'custom'
        data['availableModels'] = ['custom', 'claude-opus-5']
        self.write_settings(data)
        expected = {k: v for k, v in data.items() if k != 'hooks'}
        restore(self.settings, self.documents)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')), expected)

    def test_unrelated_file_bytes_and_commands_mentioning_script_are_preserved(self):
        data = {'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': 'echo agency-start.ps1'},
            {'type': 'command', 'command': 'powershell.exe -File "C:\\personal\\agency-start.ps1"'},
        ]}]}, 'model': 'sonnet'}
        self.settings.write_bytes(codecs.BOM_UTF8 + json.dumps(data).encode())
        before = self.settings.read_bytes()
        self.assertFalse(restore(self.settings, self.documents)['settings_changed'])
        self.assertEqual(self.settings.read_bytes(), before)

    def test_broken_profile_aborts_before_settings_are_written(self):
        configure(self.settings, quiet=True)
        before = self.settings.read_bytes()
        path, _ = self.add_profile('PowerShell')
        path.write_text(BEGIN + '\n# truncated', encoding='utf-8')
        with self.assertRaises(ValueError):
            restore(self.settings, self.documents)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_malformed_settings_abort_before_profiles_are_written(self):
        path, _ = self.add_profile('PowerShell')
        before = path.read_bytes()
        for text in ('{"env":', '{"env":{},"env":{}}', '{"hooks":{"SessionStart":{}}}'):
            self.settings.write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError):
                restore(self.settings, self.documents)
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
