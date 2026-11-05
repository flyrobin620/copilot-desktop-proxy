"""Backend validation without reading an account or decrypting authorization."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app_config import load_config, validate_upstream


PROD = 'https://loki.delve.office.com/api/v1/llmapi'
DF = 'https://df.loki.delve.office.com/api/v1/llmapi'


class AuthorizationReached(Exception):
    pass


class UpstreamValidationTests(unittest.TestCase):
    def test_verified_backends_and_trailing_slashes(self):
        for endpoint in (PROD, DF):
            for suffix in ('', '/', '//'):
                with self.subTest(endpoint=endpoint, suffix=suffix):
                    self.assertEqual(validate_upstream(endpoint + suffix), endpoint)

    def test_unreviewed_destinations_and_url_overrides_are_rejected(self):
        for endpoint in (
            'https://msit.loki.delve.office.com/api/v1/llmapi',
            'http://df.loki.delve.office.com/api/v1/llmapi',
            'https://df.loki.delve.office.com.example.invalid/api/v1/llmapi',
            'https://df.loki.delve.office.com@example.invalid/api/v1/llmapi',
            'https://example.invalid@df.loki.delve.office.com/api/v1/llmapi',
            'https://df.loki.delve.office.com:443/api/v1/llmapi',
            DF + '?destination=example.invalid', DF + '#fragment',
            DF + '/../../elsewhere', DF + '/v1/messages', DF + '\n', ' ' + DF,
            '', None, 1, True, [], {},
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(RuntimeError, '^Copilot upstream changed;'):
                    validate_upstream(endpoint)

    def provider_database(self, root, endpoint):
        path = root / 'fixture.db'
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE model_providers (id TEXT, settings_json TEXT)')
            connection.execute('CREATE TABLE provider_models '
                               '(provider_id TEXT, model_id TEXT, supported_reasoning_efforts TEXT)')
            # No credential or encrypted payload is present in this fixture.
            settings = {'baseUrl': endpoint, 'authorizationProtected': None}
            connection.execute('INSERT INTO model_providers VALUES (?, ?)',
                               ('undocked-anthropic', json.dumps(settings)))
            connection.commit()
        return path

    def test_load_config_accepts_df_and_prod_before_authorization(self):
        for endpoint in (PROD, DF):
            with self.subTest(endpoint=endpoint), TemporaryDirectory() as directory:
                db = self.provider_database(Path(directory), endpoint)
                with patch('app_config.locate_db', return_value=db), \
                     patch('app_config.unprotect', side_effect=AuthorizationReached) as decrypt:
                    with self.assertRaises(AuthorizationReached):
                        load_config()
                    decrypt.assert_called_once_with(None, 'undocked-anthropic')

    def test_load_config_rejects_unknown_backend_without_decrypting(self):
        with TemporaryDirectory() as directory:
            db = self.provider_database(Path(directory), 'https://example.invalid')
            with patch('app_config.locate_db', return_value=db), \
                 patch('app_config.unprotect') as decrypt:
                with self.assertRaisesRegex(RuntimeError, '^Copilot upstream changed;'):
                    load_config()
                decrypt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
