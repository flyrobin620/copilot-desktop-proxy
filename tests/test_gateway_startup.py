"""Credential helper cold/warm startup and owned local configuration migration."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

import claude_desktop as desktop
import test_claude_desktop as fixtures

from tests import ROOT


class HelperConfigurationTests(unittest.TestCase):
    def test_migration_of_sealed_placeholder_remains_checkable_and_restorable(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state.json'
            registry = fixtures.MemoryRegistry()
            registry.writable = False
            desktop.configure_desktop('enable', registry, state, root)
            entry = root / 'Claude-3p' / 'configLibrary' / (desktop.LOCAL_ENTRY_ID + '.json')
            values = json.loads(entry.read_text())
            values['inferenceGatewayApiKey'] = 'synthetic-sealed-placeholder'
            entry.write_text(json.dumps(values))
            desktop.configure_desktop('enable-helper', registry, state, root, dry_run=True)
            self.assertEqual(json.loads(entry.read_text()), values)
            desktop.configure_desktop('enable-helper', registry, state, root)
            self.assertEqual(json.loads(entry.read_text()), desktop.HELPER_VALUES)
            self.assertTrue(desktop.configure_desktop('status', registry, state, root)['configured'])
            desktop.configure_desktop('enable', registry, state, root)
            self.assertEqual(json.loads(entry.read_text()), desktop.HELPER_VALUES)
            desktop.configure_desktop('restore', registry, state, root)
            self.assertFalse(entry.exists())

    def test_migration_preserves_unrelated_user_changes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state.json'
            registry = fixtures.MemoryRegistry()
            registry.writable = False
            desktop.configure_desktop('enable', registry, state, root)
            entry = root / 'Claude-3p' / 'configLibrary' / (desktop.LOCAL_ENTRY_ID + '.json')
            values = {**desktop.LOCAL_VALUES, 'coworkTabEnabled': False}
            entry.write_text(json.dumps(values))
            with self.assertRaises(desktop.ConfigurationError):
                desktop.configure_desktop('enable-helper', registry, state, root)
            self.assertEqual(json.loads(entry.read_text()), values)


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows helper')
class HelperProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="proxy helper ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copyfile(ROOT / 'scripts' / 'claude-gateway-auth.ps1', self.root / 'claude-gateway-auth.ps1')
        (self.root / 'launcher-common.ps1').write_text('''
function Get-ProxyHealth {
    param([string]$Path)
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'alive')) {
        return @{service='copilot-desktop-proxy'}
    }
    return $null
}
''')
        (self.root / 'start.ps1').write_text('''
Write-Output 'startup output must not become a credential'
New-Item -ItemType File -Path (Join-Path $PSScriptRoot 'alive') -Force | Out-Null
Add-Content -LiteralPath (Join-Path $PSScriptRoot 'launches') -Value 'started'
exit 0
''')

    def invoke(self):
        return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
            'Bypass', '-File', str(self.root / 'claude-gateway-auth.ps1')], capture_output=True,
            text=True, timeout=15)

    def test_cold_start_then_warm_start_print_only_placeholder(self):
        for _ in range(2):
            result = self.invoke()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'local-copilot-placeholder')
        self.assertEqual((self.root / 'launches').read_text().strip(), 'started')

    def test_start_failure_does_not_emit_a_credential(self):
        (self.root / 'start.ps1').write_text('exit 1')
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('local-copilot-placeholder', result.stdout)


if __name__ == '__main__':
    unittest.main()
