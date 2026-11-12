"""s11 领域边界、跨机器配置与认证隔离的行为回归。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))
import domain_rules
import github_settings as settings
import scaffold_repo as scaffold
import verify_repo as verifier
import publish_github as publisher


class GitHubConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/github.json').write_text('{"owner":"fengyin-go"}')
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.location = patch.object(settings, 'SKILL_DIR', self.root)
        self.location.start()
        self.addCleanup(self.location.stop)
        settings._authenticate.cache_clear()

    def test_default_is_target_account_without_reading_active_gh_account(self):
        with patch.object(settings.subprocess, 'run', side_effect=AssertionError('no gh call expected')):
            self.assertEqual(settings.resolve_config(), {'owner':'fengyin-go', 'account':'fengyin-go'})

    def test_local_machine_owner_controls_default_credential_account(self):
        (self.root / 'config/github.local.json').write_text('{"owner":"machine-user"}')
        self.assertEqual(settings.resolve_config(), {'owner':'machine-user', 'account':'machine-user'})

    def test_cli_env_file_and_default_priority(self):
        (self.root / 'config/github.local.json').write_text('{"owner":"local-user"}')
        external = self.root / 'external.json'
        external.write_text('{"owner":"file-user"}')
        os.environ['S11_GITHUB_CONFIG'] = str(external)
        self.assertEqual(settings.resolve_config()['owner'], 'file-user')
        os.environ['S11_GITHUB_OWNER'] = 'env-user'
        self.assertEqual(settings.resolve_config()['account'], 'env-user')
        self.assertEqual(settings.resolve_config('cli-user')['account'], 'cli-user')
        os.environ['S11_GITHUB_ACCOUNT'] = 'env-account'
        self.assertEqual(settings.resolve_config('cli-user', 'cli-account')['account'], 'cli-account')

    def test_organization_can_select_separate_credential_account(self):
        (self.root / 'config/github.local.json').write_text('{"owner":"my-org","account":"my-user"}')
        self.assertEqual(settings.resolve_config(), {'owner':'my-org', 'account':'my-user'})

    def test_explicit_missing_file_fails_instead_of_publishing_to_default(self):
        os.environ['S11_GITHUB_CONFIG'] = str(self.root / 'missing.json')
        with self.assertRaises(SystemExit):
            settings.resolve_config()

    def test_config_rejects_secret_fields_and_invalid_accounts(self):
        path = self.root / 'config/github.local.json'
        for config in ({'token':'never-store'}, {'owner':'user/repo'}, {'owner':None}):
            path.write_text(json.dumps(config))
            with self.assertRaises(SystemExit):
                settings.resolve_config()

    def test_subprocesses_receive_selected_config(self):
        config = settings.configure_from_args(argparse.Namespace(owner='machine-user', github_account=None))
        self.assertEqual(os.environ['S11_GITHUB_OWNER'], 'machine-user')
        self.assertEqual(os.environ['S11_GITHUB_ACCOUNT'], config['account'])

    def test_saved_target_credential_wins_over_unrelated_environment_token(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, 'saved-test-token' if 'token' in cmd else 'fengyin-go', '')
        with patch.object(settings.shutil, 'which', return_value='/bin/gh'), patch.object(settings.subprocess, 'run', side_effect=run):
            token, login = settings._authenticate('fengyin-go', 'unrelated-environment-token')
        self.assertEqual(login, 'fengyin-go')
        self.assertIn('--user', calls[0][0])
        self.assertEqual(calls[0][0][-1], 'fengyin-go')
        self.assertNotIn('GH_TOKEN', calls[0][1]['env'])
        self.assertEqual(calls[1][1]['env']['GH_TOKEN'], token)

    def test_wrong_environment_credential_is_rejected(self):
        results = [subprocess.CompletedProcess([], 1, '', ''), subprocess.CompletedProcess([], 0, 'other-user', '')]
        with patch.object(settings.shutil, 'which', return_value='/bin/gh'), patch.object(settings.subprocess, 'run', side_effect=results):
            with self.assertRaisesRegex(SystemExit, '拒绝使用错误账号'):
                settings._authenticate('fengyin-go', 'test-token')

    def test_missing_credential_names_the_required_account(self):
        with patch.object(settings.shutil, 'which', return_value='/bin/gh'), patch.object(settings.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            with self.assertRaisesRegex(SystemExit, 'fengyin-go'):
                settings._authenticate('fengyin-go', '')

    def test_github_git_requests_use_selected_auth_on_first_attempt(self):
        with patch.object(publisher, 'auth_args', return_value=['-c', 'credential.helper=']), patch.object(publisher, 'run') as run:
            publisher.git(['push', 'origin', 'main:main'], cwd=self.root, url='https://github.com/fengyin-go/demo.git')
        self.assertEqual(run.call_args.args[0], ['git', '-c', 'credential.helper=', 'push', 'origin', 'main:main'])

    def test_scaffold_checks_target_owner_using_target_environment(self):
        with patch.object(scaffold, 'gh_binary', return_value='/bin/gh'), patch.object(settings, 'auth_env', return_value={'GH_TOKEN':'test'}) as auth, patch.object(scaffold.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertTrue(scaffold.github_repo_exists('fengyin-go', 'cc10001'))
        auth.assert_called_once_with('fengyin-go')
        self.assertIn('fengyin-go/cc10001', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env'], {'GH_TOKEN':'test'})


class DataEngineeringDomain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = json.loads((SKILL / 'examples/spec-data-engineering.json').read_text())

    def test_each_allowed_subdirection_generates_verifiable_metadata(self):
        for index, direction in enumerate(domain_rules.SUBDIRECTIONS):
            spec = dict(self.spec, subdirection=direction)
            repo = self.root / str(index)
            scaffold.scaffold(spec, repo)
            check = domain_rules.check_repo(repo)
            self.assertTrue(check['ok'], check)
            self.assertEqual(check['subdirection'], direction)
            self.assertIn(direction, (repo / 'README.md').read_text())

    def test_other_domains_missing_fields_and_unknown_subdirections_write_nothing(self):
        for changes in ({'domain':'文化制作'}, {'domain':None}, {'subdirection':'其他'}, {'subdirection':None}):
            with self.assertRaises(SystemExit):
                scaffold.scaffold(dict(self.spec, **changes), self.root / 'not-created')
            self.assertFalse((self.root / 'not-created').exists())

    def test_load_spec_also_rejects_unrelated_domain(self):
        path = self.root / 'bad-spec.json'
        path.write_text(json.dumps(dict(self.spec, domain='影视制作')))
        with self.assertRaisesRegex(SystemExit, '只生成数据工程'):
            scaffold.load_spec(path, None, None)

    def test_static_verifier_rejects_removed_or_tampered_domain(self):
        repo = self.root / 'app'
        scaffold.scaffold(self.spec, repo)
        metadata = repo / domain_rules.METADATA_FILE
        for contents in (None, '{"domain":"影视制作"}', '[]'):
            if contents is None:
                metadata.unlink()
            else:
                metadata.write_text(contents)
            result = verifier.verify(repo, min_lines=2000, min_capacity=30, per_mode=3, per_subject=2)
            self.assertFalse(result['ok'])
            self.assertTrue(any('G0 数据工程领域' in failure for failure in result['failures']))

    def test_analytics_is_allowed_but_unrelated_directions_remain_banned(self):
        import repo_rules
        self.assertEqual(repo_rules.scan_banned_topics('数据分析报表统计 streamlit csv-dashboard'), [])
        self.assertTrue(repo_rules.scan_banned_topics('购物车'))
