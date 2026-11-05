import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from model_selection import client_model_id, resolve_model, replace_model, default_auto_model, default_client_model_id, use_1m_context
from configure_claude import configure
from refresh_models import build_settings, refresh
from unittest.mock import patch


class ModelTests(unittest.TestCase):
    def test_auto_model_follows_catalog_and_pinned_id_does_not(self):
        catalog=[{'model_id':name} for name in ('claude-opus-4.8','claude-opus-5','claude-sonnet-5.5')]
        self.assertEqual(resolve_model('copilot-auto',catalog),'claude-opus-5')
        catalog.append({'model_id':'claude-opus-5-5'})
        self.assertEqual(resolve_model('copilot-auto',catalog),'claude-opus-5-5')
        self.assertEqual(resolve_model('claude-opus-5',catalog),'claude-opus-5')
        with self.assertRaises(ValueError): resolve_model('copilot-haiku',catalog)

    def test_alias_rewrite_preserves_all_other_json_bytes(self):
        raw=b'{ "metadata": {"model":"nested"}, "model" : "copilot-auto", "messages": [{"content":"model: copilot-auto"}], "value": 1.00 }'
        self.assertEqual(replace_model(raw,'claude-opus-5'),raw.replace(b'"model" : "copilot-auto"',b'"model" : "claude-opus-5"'))

    def test_client_version_names_map_to_exact_desktop_id(self):
        catalog=[{'model_id':'claude-haiku-4.5'}, {'model_id':'claude-opus-5'}]
        self.assertEqual(client_model_id('claude-haiku-4.5'),'claude-haiku-4-5')
        self.assertEqual(resolve_model('claude-haiku-4-5',catalog),'claude-haiku-4.5')
        self.assertEqual(resolve_model('claude-haiku-4.5',catalog),'claude-haiku-4.5')
        self.assertEqual(client_model_id('claude-opus-5'),'claude-opus-5')

    def test_context_hint_resolves_alias_and_pinned_models(self):
        catalog=[{'model_id':'claude-opus-4.8'}, {'model_id':'claude-opus-5'}]
        self.assertEqual(resolve_model('copilot-auto[1m]',catalog),'claude-opus-5')
        self.assertEqual(resolve_model('claude-opus-4-8[1M]',catalog),'claude-opus-4.8')
        self.assertEqual(default_auto_model(catalog),'copilot-auto[1m]')
        for model in ('claude-haiku-4-5', 'claude-opus-5.5', 'claude-opus-6', 'claude-sonnet-6'):
            self.assertTrue(use_1m_context(model))
            self.assertEqual(default_client_model_id(model), client_model_id(model) + '[1m]')
        self.assertEqual(default_auto_model([]),'copilot-auto[1m]')

    def test_context_denylist_normalizes_versions_and_only_changes_defaults(self):
        catalog = [{'model_id': 'claude-opus-5.5'}, {'model_id': 'claude-opus-5'}]
        for excluded in ('claude-opus-5.5', 'claude-opus-5-5'):
            with patch('model_selection.CONTEXT_1M_DENYLIST', frozenset({excluded})):
                for name in ('claude-opus-5.5', 'claude-opus-5-5', 'claude-opus-5-5[1M]'):
                    self.assertFalse(use_1m_context(name))
                    self.assertEqual(default_client_model_id(name), 'claude-opus-5-5')
                self.assertTrue(use_1m_context('claude-opus-5'))
                self.assertTrue(use_1m_context('claude-opus-6'))
                self.assertEqual(default_auto_model(catalog), 'copilot-auto')
                self.assertEqual(resolve_model('copilot-auto', catalog), 'claude-opus-5.5')
                self.assertEqual(resolve_model('copilot-auto[1m]', catalog), 'claude-opus-5.5')

    def test_configuration_merge_is_idempotent_and_preserves_existing_hooks(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/'settings.json'
            original={'env':{'UNRELATED':'unchanged','ANTHROPIC_MODEL':'old-model',
                             'ENABLE_TOOL_SEARCH':'auto','CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1'},
                      'permissions':{'defaultMode':'dontAsk'},'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'echo existing'}]}]}}
            path.write_text(json.dumps(original))
            with redirect_stdout(io.StringIO()): configure(path)
            first=path.read_text()
            with redirect_stdout(io.StringIO()): configure(path)
            self.assertEqual(path.read_text(),first)
            data=json.loads(first)
            self.assertEqual(data['permissions'],original['permissions'])
            self.assertEqual(data['env']['UNRELATED'],'unchanged')
            self.assertEqual(data['env']['ANTHROPIC_MODEL'],'copilot-auto[1m]')
            self.assertEqual(data['model'],'copilot-auto[1m]')
            self.assertEqual(data['env']['ENABLE_TOOL_SEARCH'],'true')
            self.assertEqual(data['env']['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'],'1')
            self.assertEqual(data['hooks']['SessionStart'][0]['hooks'][0]['command'],'echo existing')

    def test_configuration_keeps_unrelated_hook_mentioning_our_script(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/'settings.json'
            hook={'type':'command','command':'echo agency-start.ps1'}
            path.write_text(json.dumps({'hooks':{'SessionStart':[{'hooks':[hook]}]}}))
            configure(path,quiet=True)
            self.assertEqual(json.loads(path.read_text())['hooks']['SessionStart'][0]['hooks'],[hook])

    def test_refresh_updates_family_versions_and_keeps_auto_default(self):
        template=json.loads((Path(__file__).parent/'claude-settings.json').read_text())
        models=[{'id':'claude-opus-5','display_name':'Claude Opus 5'},
                {'id':'claude-sonnet-5-5','display_name':'Claude Sonnet 5.5','upstream_model':'claude-sonnet-5.5'},
                {'id':'claude-haiku-4-5','display_name':'Claude Haiku 4.5','upstream_model':'claude-haiku-4.5'}]
        first,_=build_settings(template,models)
        models.append({'id':'claude-opus-6','display_name':'Claude Opus 6'})
        second,_=build_settings(template,models)
        self.assertEqual(first['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'],'claude-opus-5[1m]')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'],'claude-opus-6[1m]')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'],'claude-haiku-4-5[1m]')
        self.assertEqual(second['env']['ANTHROPIC_SMALL_FAST_MODEL'],'claude-haiku-4-5[1m]')
        self.assertEqual(first['env']['ANTHROPIC_DEFAULT_OPUS_MODEL_NAME'],'Claude Opus 5')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_OPUS_MODEL_NAME'],'Claude Opus 6')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_SONNET_MODEL_NAME'],'Claude Sonnet 5.5')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME'],'Claude Haiku 4.5')
        self.assertIn('fixed version (claude-opus-5)', first['env']['ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION'])
        self.assertEqual(second['model'],'copilot-auto[1m]')
        self.assertEqual(second['env']['ANTHROPIC_MODEL'],'copilot-auto[1m]')
        self.assertEqual(second['env']['ANTHROPIC_CUSTOM_MODEL_OPTION'],'copilot-auto[1m]')
        self.assertEqual(template['env']['ANTHROPIC_DEFAULT_OPUS_MODEL_NAME'],'Copilot Opus (latest)')
        self.assertEqual(second['env']['CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY'],'1')
        self.assertEqual(first['model'],'copilot-auto[1m]')
        self.assertEqual(first['env']['ANTHROPIC_MODEL'],'copilot-auto[1m]')
        self.assertEqual(first['env']['ANTHROPIC_DEFAULT_MODEL'],'copilot-auto[1m]')
        self.assertEqual(first['env']['ANTHROPIC_CUSTOM_MODEL_OPTION'],'copilot-auto[1m]')
        self.assertIn('long_context',first['env']['ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES'])
        self.assertIn('long_context',second['env']['ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES'])
        self.assertEqual(second['env']['ENABLE_TOOL_SEARCH'], 'true')
        with TemporaryDirectory() as temp:
            path=Path(temp)/'settings.json'
            path.write_text(json.dumps({'availableModels':['custom-unrelated']}))
            configure(path,first,quiet=True)
            self.assertEqual(json.loads(path.read_text())['model'],'copilot-auto[1m]')
            self.assertIn('copilot-auto[1m]',json.loads(path.read_text())['availableModels'])
            configure(path,second,quiet=True)
            self.assertEqual(json.loads(path.read_text())['model'],'copilot-auto[1m]')
            self.assertIn('claude-opus-6[1m]', json.loads(path.read_text())['availableModels'])
            self.assertIn('custom-unrelated',json.loads(path.read_text())['availableModels'])

    def test_refresh_accepts_hinted_catalog_and_clears_stale_caps_for_denylisted_models(self):
        template=json.loads((Path(__file__).parent/'claude-settings.json').read_text())
        models=[{'id':'claude-opus-5-5[1m]', 'upstream_model':'claude-opus-5.5'},
                {'id':'claude-sonnet-5-5[1m]'}, {'id':'claude-haiku-4-5[1m]'}]
        first, catalog=build_settings(template, models)
        self.assertEqual(first['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'claude-opus-5-5[1m]')
        self.assertEqual(first['env']['ANTHROPIC_DEFAULT_SONNET_MODEL'], 'claude-sonnet-5-5[1m]')
        self.assertEqual(resolve_model(first['model'],catalog), 'claude-opus-5.5')
        with patch('model_selection.CONTEXT_1M_DENYLIST', frozenset({'claude-opus-5.5','claude-haiku-4.5'})):
            second,_=build_settings(first, models)
        self.assertEqual(second['model'], 'copilot-auto')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'claude-opus-5-5')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'], 'claude-haiku-4-5')
        self.assertEqual(second['env']['ANTHROPIC_SMALL_FAST_MODEL'], 'claude-haiku-4-5')
        self.assertEqual(second['env']['ANTHROPIC_DEFAULT_SONNET_MODEL'], 'claude-sonnet-5-5[1m]')
        self.assertNotIn('long_context',second['env']['ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES'])
        with TemporaryDirectory() as temp:
            path=Path(temp)/'settings.json'
            configure(path,template,quiet=True)
            configure(path,second,quiet=True)
            data=json.loads(path.read_text())
            self.assertEqual(data['model'], 'copilot-auto')
            self.assertFalse(any(k.startswith('ANTHROPIC_DEFAULT_') and k.endswith('_SUPPORTED_CAPABILITIES')
                                 for k in data['env']))
            self.assertEqual(data['env']['ENABLE_TOOL_SEARCH'],'true')
            snapshot=path.read_bytes()
            configure(path,second,quiet=True)
            self.assertEqual(path.read_bytes(),snapshot)

    def test_start_refresh_does_not_modify_unrelated_global_settings(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'claude-settings.json').write_text((Path(__file__).parent/'claude-settings.json').read_text())
            target=root/'global.json'; target.write_text('{"model":"existing-model"}')
            response=io.BytesIO(b'{"data":[{"id":"claude-opus-5"}]}')
            with patch('refresh_models.build_opener') as opener,redirect_stdout(io.StringIO()):
                opener.return_value.open.return_value=response
                refresh(root=root,global_path=target)
            self.assertEqual(target.read_text(),'{"model":"existing-model"}')
            self.assertTrue((root/'.models/claude-settings.json').exists())
            generated=json.loads((root/'.models/claude-settings.json').read_text())
            self.assertEqual(generated['env']['ANTHROPIC_DEFAULT_OPUS_MODEL_NAME'],'claude-opus-5')

    def test_start_refresh_migrates_opted_in_profile_without_losing_model_access(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'claude-settings.json').write_text((Path(__file__).parent/'claude-settings.json').read_text())
            target=root/'global.json'
            command='powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + str(root/'agency-start.ps1') + '"'
            target.write_text(json.dumps({'model':'copilot-auto', 'availableModels':['unrelated-model'],
                'env':{'ANTHROPIC_BASE_URL':'http://127.0.0.1:8787','UNRELATED':'keep',
                       'ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES':'thinking'},
                'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':command}]}]}}))
            # A running older proxy may still return bare IDs during an upgrade.
            models=[{'id':'claude-opus-5-5','upstream_model':'claude-opus-5.5'},
                    {'id':'claude-opus-4-8','upstream_model':'claude-opus-4.8'},
                    {'id':'claude-sonnet-5-5'}, {'id':'claude-haiku-4-5'}]
            for _ in range(2):
                with patch('refresh_models.build_opener') as opener, patch('configure_claude.ROOT', root), redirect_stdout(io.StringIO()):
                    opener.return_value.open.return_value=io.BytesIO(json.dumps({'data':models}).encode())
                    refresh(root=root,global_path=target)
                data=json.loads(target.read_text())
                self.assertEqual(data['model'],'copilot-auto[1m]')
                self.assertEqual(data['env']['ENABLE_TOOL_SEARCH'],'true')
                self.assertNotIn('CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS',data['env'])
                self.assertEqual(data['env']['UNRELATED'],'keep')
                self.assertNotIn('ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES',data['env'])
                self.assertTrue({'unrelated-model','claude-opus-5-5[1m]','claude-opus-4-8[1m]',
                                 'claude-sonnet-5-5[1m]','claude-haiku-4-5[1m]'}.issubset(data['availableModels']))
                self.assertEqual(data['availableModels'].count('claude-opus-5-5[1m]'),1)
            data['env']['ENABLE_TOOL_SEARCH']='false'
            data['env']['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS']='1'
            target.write_text(json.dumps(data))
            with patch('refresh_models.build_opener') as opener, patch('configure_claude.ROOT', root), redirect_stdout(io.StringIO()):
                opener.return_value.open.return_value=io.BytesIO(json.dumps({'data':models}).encode())
                refresh(root=root,global_path=target)
            data=json.loads(target.read_text())
            self.assertEqual(data['env']['ENABLE_TOOL_SEARCH'],'false')
            self.assertEqual(data['env']['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'],'1')

    def test_explicit_upgrade_removes_legacy_beta_only_from_own_profile(self):
        beta='CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'
        template=json.loads((Path(__file__).parent/'claude-settings.json').read_text())
        self.assertNotIn(beta,template['env'])
        with TemporaryDirectory() as temp:
            path=Path(temp)/'settings.json'
            for value in ('0','1'):
                configure(path,template,quiet=True)
                data=json.loads(path.read_text()); data['env'][beta]=value
                path.write_text(json.dumps(data))
                configure(path,template,quiet=True,migrate_legacy_beta=True)
                self.assertNotIn(beta,json.loads(path.read_text())['env'])
            # Moving the package does not prevent removal of the old override.
            data=json.loads(path.read_text()); data['env'][beta]='1'
            data['hooks']['SessionStart'][0]['hooks'][0]['command']=(
                'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "' +
                str(Path(temp)/'old package'/'agency-start.ps1') + '"')
            path.write_text(json.dumps(data))
            configure(path,template,quiet=True,migrate_legacy_beta=True)
            self.assertNotIn(beta,json.loads(path.read_text())['env'])
            data=json.loads(path.read_text()); data['env'][beta]='1'
            path.write_text(json.dumps(data))
            configure(path,template,quiet=True)
            self.assertEqual(json.loads(path.read_text())['env'][beta],'1')
            for unrelated in (
                {'env':{beta:'1'}},
                {**data,'env':{**data['env'],'ANTHROPIC_BASE_URL':'https://gateway.example.test'}},
                {**data,'hooks':{}},
            ):
                path.write_text(json.dumps(unrelated))
                configure(path,template,quiet=True,migrate_legacy_beta=True)
                self.assertEqual(json.loads(path.read_text())['env'][beta],'1')

    def test_configuration_cli_applies_generated_template_and_upgrade(self):
        root=Path(__file__).parent
        template=json.loads((root/'claude-settings.json').read_text())
        generated,_=build_settings(template,[{'id':'claude-opus-6'},{'id':'claude-haiku-5-5'}])
        with TemporaryDirectory(prefix='agency upgrade ') as temp:
            path=Path(temp)/'settings.json'
            source=Path(temp)/'generated settings.json'
            configure(path,template,quiet=True)
            data=json.loads(path.read_text()); data['env']['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS']='1'
            path.write_text(json.dumps(data))
            source.write_text(json.dumps(generated))
            result=subprocess.run([sys.executable,'-B','-X','utf8',str(root/'configure_claude.py'),
                                   '--settings',str(path),'--template',str(source),'--migrate-legacy-beta'],
                                  capture_output=True,text=True,timeout=10,check=True)
            self.assertTrue(json.loads(result.stdout)['configured'])
            data=json.loads(path.read_text())
            self.assertEqual(data['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'],'claude-opus-6[1m]')
            self.assertNotIn('ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES',data['env'])
            self.assertNotIn('CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS',data['env'])


if __name__=='__main__': unittest.main()
