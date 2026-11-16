"""Offline GitHub ownership-boundary tests; every gh/git request is mocked."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publish_github as publisher


class PublicationOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='publish-ownership-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'local repo'
        (self.repo / '.git').mkdir(parents=True)
        self.head = 'a' * 40
        self.identity = {'owner': 'test-owner', 'full_name': 'test-owner/example', 'id': 12345}
        self.args = argparse.Namespace(repo=str(self.repo), branch='main', owner='test-owner',
            name='example', visibility='public', description=None, remote_url=None,
            skip_verify=True, allow_dirty=False, execute=True, confirm=None)
        self.stack = self.enterContext(ExitStack())
        self.mocks = {}
        self.real_identity = publisher.github_repo_identity
        for name, value in (
            ('local_head', self.head),
            ('run', subprocess.CompletedProcess([], 0, '', '')),
            ('check_delivery_evidence', {'ok': True, 'problems': []}),
            ('repo_exists', True),
            ('github_repo_identity', self.identity),
            ('auth_args', []),
            ('ensure_visibility', {'before': 'public', 'after': 'public', 'problems': []}),
            ('remote_head', self.head),
            ('git', subprocess.CompletedProcess([], 0, '', '')),
            ('configure_origin', None),
        ):
            self.mocks[name] = self.stack.enter_context(patch.object(publisher, name, return_value=value))
        self.stack.enter_context(patch.object(publisher.github_settings, 'configure_from_args',
            return_value={'owner': 'test-owner', 'account': 'test-user'}))
        self.stack.enter_context(patch.object(publisher.github_settings, 'auth_env',
            return_value={'GH_TOKEN': 'test-only-secret'}))

    def receipt(self, **changes):
        record = {'schema_version': 1, 'host': 'github.com', **self.identity,
            'local_repo': str(self.repo), 'created_head': self.head, 'account': 'test-user'}
        record.update(changes)
        publisher.ownership_path(self.repo).write_text(json.dumps(record), encoding='utf-8')
        return record

    def assert_no_writes(self):
        self.mocks['configure_origin'].assert_not_called()
        self.mocks['ensure_visibility'].assert_not_called()
        self.assertFalse(any('push' in call.args[0] for call in self.mocks['git'].call_args_list))
        self.assertFalse(any(call.args[0][1:3] in (['repo', 'create'], ['repo', 'edit'])
                             for call in self.mocks['run'].call_args_list))

    def rejected(self, report, text=None):
        self.assertIs(report['ok'], False, report)
        self.assertTrue(report['problems'], report)
        if text:
            self.assertIn(text, '\n'.join(report['problems']))

    def test_existing_empty_repository_without_receipt_cannot_be_adopted(self):
        self.mocks['remote_head'].return_value = ''
        report = publisher.publish(self.args)
        self.rejected(report, '缺少本仓归属记录')
        self.assert_no_writes()
        self.mocks['remote_head'].assert_not_called()
        self.assertFalse(publisher.ownership_path(self.repo).exists())

    def test_matching_numeric_id_and_local_repository_can_resume_empty_remote(self):
        self.receipt()
        self.mocks['remote_head'].side_effect = ['', self.head]
        report = publisher.publish(self.args)
        self.assertTrue(report['ok'], report)
        self.assertTrue(report['pushed'])
        self.mocks['configure_origin'].assert_called_once()
        self.assertTrue(any(call.args[0][0] == 'push' for call in self.mocks['git'].call_args_list))
        self.assertEqual(self.mocks['github_repo_identity'].call_count, 2)
        self.assertFalse(report['repo_created'])

    def test_changed_github_id_rejects_even_empty_remote_and_preserves_receipt(self):
        self.receipt()
        before = publisher.ownership_path(self.repo).read_bytes()
        self.mocks['github_repo_identity'].return_value = {**self.identity, 'id': 67890}
        self.rejected(publisher.publish(self.args), '身份不符')
        self.assert_no_writes()
        self.assertEqual(publisher.ownership_path(self.repo).read_bytes(), before)

    def test_local_repo_owner_name_and_schema_mismatch_cannot_resume(self):
        for changes in ({'local_repo': str(self.root / 'other')}, {'owner': 'other'},
                        {'full_name': 'test-owner/other'}, {'id': True},
                        {'schema_version': 2}, {'host': 'not-github.example'}):
            with self.subTest(changes=changes):
                self.receipt(**changes)
                self.rejected(publisher.publish(self.args), '身份不符')
                self.assert_no_writes()

    def test_successful_creation_records_true_id_atomically_outside_repo_before_writes(self):
        self.mocks['github_repo_identity'].side_effect = [None, self.identity, self.identity]
        self.mocks['remote_head'].side_effect = ['', self.head]
        path = publisher.ownership_path(self.repo)
        real_link = publisher.os.link
        observations = []

        def atomic_link(source, destination):
            self.assertEqual(Path(source).parent, self.repo.parent)
            self.assertEqual(Path(destination), path)
            self.assertFalse(path.exists())
            json.loads(Path(source).read_text())  # complete JSON before publication
            observations.append('atomic receipt')
            real_link(source, destination)

        def visibility(*args, **kwargs):
            record = json.loads(path.read_text())
            self.assertEqual(record['id'], 12345)
            self.assertEqual(record['local_repo'], str(self.repo))
            self.assertEqual(record['owner'], 'test-owner')
            self.assertEqual(record['full_name'], 'test-owner/example')
            self.assertEqual(record['account'], 'test-user')
            return {'before': 'public', 'after': 'public', 'problems': []}

        self.mocks['ensure_visibility'].side_effect = visibility
        with patch.object(publisher.os, 'link', side_effect=atomic_link):
            report = publisher.publish(self.args)
        self.assertTrue(report['ok'], report)
        self.assertTrue(report['repo_created'])
        self.assertEqual(observations, ['atomic receipt'])
        self.assertEqual(path.parent, self.repo.parent)
        self.assertFalse(path.is_relative_to(self.repo))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.repo.iterdir()), [self.repo / '.git'])
        self.assertFalse(list(self.repo.parent.glob('*.tmp')))
        self.assertNotIn('test-only-secret', path.read_text())

    def test_creation_conflict_is_not_owned_and_never_retried_as_existing(self):
        self.mocks['github_repo_identity'].return_value = None

        def run(command, **kwargs):
            if command[1:3] == ['repo', 'create']:
                return subprocess.CompletedProcess(command, 1, '', 'name already exists')
            return subprocess.CompletedProcess(command, 0, '', '')

        self.mocks['run'].side_effect = run
        report = publisher.publish(self.args)
        self.rejected(report, '竞争冲突')
        self.assertEqual(self.mocks['github_repo_identity'].call_count, 1)
        self.mocks['configure_origin'].assert_not_called()
        self.mocks['ensure_visibility'].assert_not_called()
        self.assertFalse(publisher.ownership_path(self.repo).exists())
        self.mocks['git'].assert_not_called()

    def test_creation_rate_limit_diagnostics_are_preserved_without_retry_or_tokens(self):
        self.mocks['github_repo_identity'].return_value = None
        for message, expected_http in (
                ('HTTP 429 Too Many Requests; Retry-After: 90; ghp_PRIVATE_TOKEN', 429),
                ('GraphQL: API rate limit exceeded for user; Retry-After: 120', None)):
            with self.subTest(message=message):
                def run(command, **kwargs):
                    if command[1:3] == ['repo', 'create']:
                        return subprocess.CompletedProcess(command, 1, '', message)
                    return subprocess.CompletedProcess(command, 0, '', '')
                self.mocks['run'].side_effect = run
                report = publisher.publish(self.args)
                self.rejected(report)
                error = report['github_error']
                self.assertEqual(error['stage'], 'create')
                self.assertEqual(error['exit'], 1)
                self.assertEqual(error['http_status'], expected_http)
                self.assertIn('Retry-After', error['diagnostic'])
                self.assertIn('429' if expected_http else 'GraphQL', error['diagnostic'])
                self.assertNotIn('ghp_PRIVATE_TOKEN', json.dumps(report))
                self.assertFalse(publisher.ownership_path(self.repo).exists())
                self.mocks['configure_origin'].assert_not_called()
                self.mocks['ensure_visibility'].assert_not_called()

    def test_identity_429_retains_http_headers_and_body_and_never_creates(self):
        response = subprocess.CompletedProcess([], 1,
            'HTTP/2.0 429 Too Many Requests\r\nRetry-After: 75\r\n'
            'X-RateLimit-Remaining: 0\r\n\r\n{"message":"API rate limit exceeded"}',
            'gh: rate limited; Authorization: Bearer ghp_PRIVATE_TOKEN')
        self.mocks['run'].side_effect = lambda command, **kwargs: (
            response if 'api' in command else subprocess.CompletedProcess(command, 0, '', ''))
        # Exercise the real identity parser, not a synthetic OwnershipError.
        self.mocks['github_repo_identity'].side_effect = self.real_identity
        report = publisher.publish(self.args)
        self.rejected(report)
        error = report['github_error']
        self.assertEqual(error['stage'], 'identity')
        self.assertEqual(error['exit'], 1)
        self.assertEqual(error['http_status'], 429)
        self.assertIn('Retry-After: 75', error['diagnostic'])
        self.assertIn('X-RateLimit-Remaining: 0', error['diagnostic'])
        self.assertIn('API rate limit exceeded', error['diagnostic'])
        self.assertNotIn('ghp_PRIVATE_TOKEN', json.dumps(report))
        self.assert_no_writes()

    def test_network_failure_does_not_try_create_or_destroy_existing_evidence(self):
        self.receipt()
        before = publisher.ownership_path(self.repo).read_bytes()
        self.mocks['github_repo_identity'].side_effect = publisher.OwnershipError('network unavailable')
        self.rejected(publisher.publish(self.args), 'network unavailable')
        self.assert_no_writes()
        self.assertEqual(publisher.ownership_path(self.repo).read_bytes(), before)

    def test_network_failure_after_create_does_not_invent_id_or_modify_origin(self):
        self.mocks['github_repo_identity'].side_effect = [None, publisher.OwnershipError('read ID failed')]
        self.rejected(publisher.publish(self.args), 'read ID failed')
        self.assertFalse(publisher.ownership_path(self.repo).exists())
        self.mocks['configure_origin'].assert_not_called()
        self.mocks['ensure_visibility'].assert_not_called()
        self.mocks['git'].assert_not_called()

    def test_recorded_but_missing_remote_does_not_recreate_or_erase_evidence(self):
        self.receipt()
        before = publisher.ownership_path(self.repo).read_bytes()
        self.mocks['github_repo_identity'].return_value = None
        self.rejected(publisher.publish(self.args), '不自动重建')
        self.assert_no_writes()
        self.assertEqual(publisher.ownership_path(self.repo).read_bytes(), before)

    def test_id_changed_between_read_and_first_write_blocks_visibility_origin_push(self):
        self.receipt()
        self.mocks['github_repo_identity'].side_effect = [self.identity, {**self.identity, 'id': 67890}]
        self.mocks['ensure_visibility'].return_value = {
            'before': 'private', 'after': 'public', 'will_fix': True,
            'planned_command': ['gh', 'repo', 'edit', 'test-owner/example', '--visibility', 'public'],
            'problems': []}
        self.rejected(publisher.publish(self.args), '身份不符')
        self.assertTrue(all(call.kwargs['execute'] is False
                            for call in self.mocks['ensure_visibility'].call_args_list))
        self.mocks['configure_origin'].assert_not_called()
        self.assertFalse(any(call.args[0][0] == 'push' for call in self.mocks['git'].call_args_list))

    def test_receipt_write_failure_cannot_proceed(self):
        self.mocks['github_repo_identity'].side_effect = [None, self.identity]
        with patch.object(publisher.os, 'link', side_effect=OSError('disk full')):
            self.rejected(publisher.publish(self.args), 'disk full')
        self.assertFalse(publisher.ownership_path(self.repo).exists())
        self.assertFalse(list(self.root.glob('*.tmp')))
        self.mocks['configure_origin'].assert_not_called()
        self.mocks['ensure_visibility'].assert_not_called()

    def test_atomic_receipt_publication_never_overwrites_existing_evidence(self):
        self.receipt(id=555)
        before = publisher.ownership_path(self.repo).read_bytes()
        with self.assertRaises(FileExistsError):
            publisher.save_ownership(self.repo, self.identity, self.head, 'test-user')
        self.assertEqual(publisher.ownership_path(self.repo).read_bytes(), before)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_malformed_or_symlinked_receipt_is_rejected(self):
        path = publisher.ownership_path(self.repo)
        for content in ('{', '[]'):
            with self.subTest(content=content):
                path.write_text(content)
                self.rejected(publisher.publish(self.args), '归属记录')
                self.assert_no_writes()
        path.unlink()
        target = self.root / 'target.json'
        target.write_text('{}')
        path.symlink_to(target)
        self.rejected(publisher.publish(self.args), '符号链接')
        self.assert_no_writes()

    def test_dry_run_never_creates_repo_receipt_or_writes_origin_visibility_push(self):
        self.args.execute = False
        for exists in (True, False):
            with self.subTest(exists=exists):
                self.mocks['repo_exists'].return_value = exists
                report = publisher.publish(self.args)
                self.assertTrue(report['ok'], report)
                self.mocks['github_repo_identity'].assert_not_called()
                self.mocks['configure_origin'].assert_not_called()
                self.assertFalse(publisher.ownership_path(self.repo).exists())
                self.assertTrue(all(call.kwargs['execute'] is False
                    for call in self.mocks['ensure_visibility'].call_args_list))
                self.assertFalse(any('push' in call.args[0] for call in self.mocks['git'].call_args_list))
                self.assertFalse(any(call.args[0][1:3] == ['repo', 'create']
                    for call in self.mocks['run'].call_args_list))

    def test_github_remote_url_cannot_bypass_guard_with_host_casing(self):
        for url in ('git@GitHub.com:test-owner/example.git',
                    'https://GITHUB.COM./test-owner/example.git',
                    'ssh://git@github.com/test-owner/example.git'):
            with self.subTest(url=url):
                self.args.remote_url = url
                with self.assertRaisesRegex(SystemExit, '--owner / --name'):
                    publisher.publish(self.args)
                self.assert_no_writes()

    def test_final_identity_read_failure_preserves_new_receipt_but_stops_push(self):
        self.mocks['github_repo_identity'].side_effect = [None, self.identity,
            publisher.OwnershipError('network unavailable before writes')]
        report = publisher.publish(self.args)
        self.rejected(report, 'network unavailable before writes')
        self.assertEqual(json.loads(publisher.ownership_path(self.repo).read_text())['id'], 12345)
        self.mocks['configure_origin'].assert_not_called()
        self.assertFalse(any(call.args[0][0] == 'push' for call in self.mocks['git'].call_args_list))
        self.assertTrue(all(call.kwargs['execute'] is False
                            for call in self.mocks['ensure_visibility'].call_args_list))

    def test_non_github_local_bare_remote_retains_original_publish_path(self):
        self.args.remote_url = str(self.root / 'remote.git')
        self.mocks['remote_head'].side_effect = ['', self.head]
        self.assertTrue(publisher.publish(self.args)['ok'])
        self.mocks['github_repo_identity'].assert_not_called()
        self.mocks['repo_exists'].assert_not_called()
        self.mocks['ensure_visibility'].assert_not_called()
        self.mocks['configure_origin'].assert_called_once()
        self.assertFalse(publisher.ownership_path(self.repo).exists())


class GitHubIdentityResponseTest(unittest.TestCase):
    def response(self, status=200, payload=None, code=0):
        payload = payload if payload is not None else {
            'id': 123, 'owner': {'login': 'test-owner'}, 'full_name': 'test-owner/example'}
        return subprocess.CompletedProcess([], code,
            f'HTTP/2.0 {status} Response\r\nx-github-request-id: test\r\n\r\n' + json.dumps(payload), '')

    def test_reads_true_rest_numeric_id(self):
        with patch.object(publisher, 'run', return_value=self.response()) as run:
            self.assertEqual(publisher.github_repo_identity('test-owner/example')['id'], 123)
        self.assertIn('--include', run.call_args.args[0])

    def test_only_real_http_404_is_absent(self):
        with patch.object(publisher, 'run', return_value=self.response(404, {'message': 'Not Found'}, 1)):
            self.assertIsNone(publisher.github_repo_identity('test-owner/example'))
        for result in (self.response(403, {}, 1), self.response(500, {}, 1),
                       subprocess.CompletedProcess([], 1, '', 'network timeout'),
                       subprocess.CompletedProcess([], 1, '{"status":"404"}', '')):
            with self.subTest(result=result), patch.object(publisher, 'run', return_value=result):
                with self.assertRaises(publisher.OwnershipError):
                    publisher.github_repo_identity('test-owner/example')

    def test_rate_limit_is_not_missing_and_exposes_bounded_safe_diagnostic(self):
        response = self.response(429, {'message': 'API rate limit exceeded',
                                      'details': 'x' * 6000 + ' ghp_PRIVATE_TOKEN'}, 1)
        response.stdout = response.stdout.replace('x-github-request-id: test',
            'Retry-After: 91\r\nX-RateLimit-Remaining: 0')
        with patch.object(publisher, 'run', return_value=response):
            with self.assertRaises(publisher.OwnershipError) as caught:
                publisher.github_repo_identity('test-owner/example')
        error = caught.exception.github_error
        self.assertEqual(error['http_status'], 429)
        self.assertIn('Retry-After: 91', error['diagnostic'])
        self.assertLessEqual(len(error['diagnostic']), 4000)
        self.assertNotIn('ghp_PRIVATE_TOKEN', error['diagnostic'])

    def test_selected_credential_is_scrubbed_at_subprocess_boundary(self):
        # Credential-store tokens need not exist in os.environ or have a known
        # prefix. The actual selected token must still never reach diagnostics.
        token = 'opaque-test-credential-123456'
        response = subprocess.CompletedProcess([], 1, token, 'GraphQL: rate limited ' + token)
        with patch.object(publisher.github_settings, 'auth_env', return_value={'GH_TOKEN': token}), \
             patch.object(publisher.subprocess, 'run', return_value=response):
            result = publisher.run(['gh', 'repo', 'create', 'test-owner/example'])
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertIn('GraphQL', result.stderr)

    def test_bad_schema_renamed_or_transferred_repository_is_rejected(self):
        for payload in ({}, [], {'id': True, 'owner': {'login': 'test-owner'}, 'full_name': 'test-owner/example'},
                {'id': 123, 'owner': {'login': 'stranger'}, 'full_name': 'stranger/example'},
                {'id': 123, 'owner': {'login': 'test-owner'}, 'full_name': 'test-owner/different'}):
            with self.subTest(payload=payload), patch.object(publisher, 'run', return_value=self.response(payload=payload)):
                with self.assertRaises(publisher.OwnershipError):
                    publisher.github_repo_identity('test-owner/example')


if __name__ == '__main__':
    unittest.main(verbosity=2)
