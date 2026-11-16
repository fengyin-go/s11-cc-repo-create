"""Offline tests: temporary Git repos, mocked process boundary, real negatives.

No generated pass scripts/reports are used as admission evidence. Real installed
validator tests skip explicitly when the optional downstream skill is absent.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_prompt_capacity.py -v
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_prompt_capacity.py'
spec = importlib.util.spec_from_file_location('prompt_capacity_under_test', SCRIPT)
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)
REAL_SKILL = capacity._default_downstream()
REQUIRED = ('validate_topic_map.py', 'check_repo_theme.py', 'repo_topic_policy.py',
            'difficulty_contract.py', 'domain_fit.py', 's11_policy.py', 'check_prompt_type_intent.py')
PRODUCER = ('def publish_orders(records):\n'
            '    订单数据集 = [dict(row) for row in records if row.get("id")]  # 解析订单数据\n'
            '    return 订单数据集\n')
CONSUMER = ('def export_orders(订单数据集):\n'
            '    return "\\n".join(str(row["id"]) for row in 订单数据集)  # 导出订单明细文件\n')


class CapacityFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='capacity-offline-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo with spaces'
        self.repo.mkdir()
        self.downstream = self.root / 'mocked-downstream-boundary'
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/orders.py').write_text(PRODUCER, encoding='utf-8')
        (self.repo / 'src/exports.py').write_text(CONSUMER, encoding='utf-8')
        (self.repo / '.gitignore').write_text('*.ignored.py\nnode_modules/\n', encoding='utf-8')
        self.commit('source S')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.map_path = self.repo / '.s11-capacity/repo-topic-map.json'
        self.payload = self.make_map(35)
        self.save_map()
        self.head = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args, check=True):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith('GIT_'):
                env.pop(key, None)
        return subprocess.run(['git', '-C', str(self.repo), *args], env=env,
            text=True, capture_output=True, check=check).stdout

    def commit(self, message):
        self.git('add', '-A')
        self.git('-c', 'user.email=capacity-test@example.invalid', '-c', 'user.name=Capacity Test',
                 '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-q', '-m', message)

    def make_map(self, count):
        plans, topics = [], []
        for index in range(1, count + 1):
            topic = f'topic-{(index - 1) // 3}'
            if (index - 1) % 3 == 0:
                topics.append({'id': topic, 'name': topic, 'module': '订单解析',
                    'keywords': [topic], 'evidence': ['src/orders.py'], 'seat_capacity': 3})
            plans.append({'sequence': index, 'slug': 'optimization', 'task_type': '功能优化',
                'topic': topic, 'family': f'优化既有能力-{index}', 'angle': '已发布订单数据集恢复',
                'domain_fit': {'version': 2, 'prompt_main_request': '请优化现有订单数据集解析链路，保留完整数据集。'},
                'difficulty_design': {'version': 2, 'module_integration': {
                    'primary_module': '订单解析', 'data_artifact': '订单数据集',
                    'secondary_modules': [{'name': '分析导出中心', 'role': 'consumer',
                        'artifact': '订单数据集', 'relationship': '读取同一批次数据集生成导出文件'}]}},
                'support_evidence': [
                    {'module': '订单解析', 'role': 'producer', 'artifact': '订单数据集',
                     'behavior': '过滤不含 id 的输入并产出订单数据集', 'path': 'src/orders.py',
                     'start_line': 1, 'end_line': 3, 'excerpt': PRODUCER.rstrip('\n')},
                    {'module': '分析导出中心', 'role': 'consumer', 'artifact': '订单数据集',
                     'behavior': '读取订单数据集并生成包含行标识的导出文件', 'path': 'src/exports.py',
                     'start_line': 1, 'end_line': 2, 'excerpt': CONSUMER.rstrip('\n')}]})
        for plan in plans:
            plan['difficulty_design']['module_anchors'] = [
                {**a, 'supports': 'primary' if i == 0 else 'secondary'}
                for i, a in enumerate(plan['support_evidence'])]
        return {'version': 1, 'profile': 'gsb-semantic-v2', 'type_policy': 'capacity-first',
            'difficulty_contract_version': 2, 'base_commit': self.base, 'domain': '数据工程',
            'subdirection': '数据分析与数据应用', 'max_capacity': count,
            'topics': topics, 'task_plan': plans}

    def save_map(self, commit=True):
        self.map_path.parent.mkdir(exist_ok=True)
        self.map_path.write_text(json.dumps(self.payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if commit:
            self.commit('capacity metadata only')

    def mocked_snapshot(self, *args):
        value = self.original_snapshot(*args)
        if args[2] == self.downstream / 'scripts':
            value['downstream_scripts'] = {name: capacity._sha(name.encode()) for name in REQUIRED}
            value['downstream_sha256'] = capacity._digest(value['downstream_scripts'])
        return value

    def validator_result(self):
        return {'ok': True, 'task_plan_count': len(self.payload['task_plan']),
                'version': 1, 'profile': 'gsb-semantic-v2', 'base_commit': self.base}

    def modes_result(self):
        records = [{'sequence': p['sequence'], 'mode': f'mode-{i // 3}'}
                   for i, p in enumerate(self.payload['task_plan'])]
        counts = dict(capacity.Counter(p['mode'] for p in records))
        return {'items': records, 'mode_counts': counts, 'checked_count': len(records), 'gate_version': 'mock'}

    @contextlib.contextmanager
    def mocks(self, side_effect=None):
        self.original_snapshot = capacity._snapshot
        with patch.object(capacity, '_snapshot', side_effect=self.mocked_snapshot), \
             patch.object(capacity, '_json_process', side_effect=side_effect or
                          [self.validator_result(), self.modes_result()]) as process:
            yield process

    def check(self, **kwargs):
        return capacity.check_capacity(self.repo, downstream_skill=self.downstream, **kwargs)

    def rejected(self, report, fragment=None):
        self.assertIs(report['ok'], False, report)
        self.assertTrue(report['issues'], report)
        if fragment:
            self.assertIn(fragment, '\n'.join(report['issues']))

class CapacityTest(CapacityFixture, unittest.TestCase):
    def test_downstream_env_override_api_and_cli_with_explicit_precedence(self):
        with self.mocks(), patch.dict(os.environ, {'S11_PROMPT_SKILL': str(self.downstream)}):
            report = capacity.check_capacity(self.repo)
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(report['downstream_skill'], str(self.downstream))
        with self.mocks(), patch.dict(os.environ, {'S11_PROMPT_SKILL': str(self.root / 'unavailable')}):
            self.assertTrue(self.check()['ok'])
        with self.mocks(), patch.dict(os.environ, {'S11_PROMPT_SKILL': str(self.downstream)}), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(capacity.main(['--repo', str(self.repo)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())['downstream_skill'], str(self.downstream))
        with patch.dict(os.environ, {'S11_PROMPT_SKILL': str(self.downstream)}):
            module_spec = importlib.util.spec_from_file_location('capacity_env_test', SCRIPT)
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
        self.assertEqual(module.DEFAULT_DOWNSTREAM, self.downstream)

    def test_noncapacity_document_changes_between_baseline_and_head_are_rejected(self):
        (self.repo / 'README.md').write_text('not a capacity-only change\n')
        self.commit('source baseline must not drift, even for docs')
        with self.mocks() as process:
            report = self.check()
        self.rejected(report, 'README.md')
        process.assert_not_called()

    def test_evidence_must_overlap_the_claimed_downstream_module_anchor(self):
        module_anchor = self.payload['task_plan'][0]['difficulty_design']['module_anchors'][1]
        module_anchor['path'] = 'src/orders.py'
        self.save_map()
        with self.mocks():
            self.rejected(self.check(), 'module_anchors')

    def test_cli_target_one_is_diagnostic_not_default_publication(self):
        self.payload = self.make_map(1)
        self.save_map()
        with self.mocks(), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = capacity.main(['--repo', str(self.repo), '--downstream-skill', str(self.downstream),
                                  '--target', '1'])
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(report['diagnostic_only'])
        self.assertEqual(report['qualified_plan_count'], 1)
        with self.mocks():
            self.rejected(self.check(target=35), 'target=35')

    def test_public_default_api_and_defaults(self):
        with self.mocks(), patch.object(capacity, 'DEFAULT_DOWNSTREAM', self.downstream), \
             patch.dict(os.environ, {'S11_PROMPT_SKILL': ''}):
            report = capacity.check_capacity(self.repo)
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(report['qualified_plan_count'], 35)
        self.assertEqual(report['source_head'], self.head)
        self.assertEqual(report['source_base_commit'], self.base)
        self.assertEqual(report['fingerprint']['head'], self.head)
        self.assertEqual(report['fingerprint']['source_base_commit'], self.base)
        self.assertEqual(report['map'], str(self.map_path))
        self.assertTrue(all(not flag for flag in report['verification_scope'].values()))
        command = report['downstream_command']
        self.assertIn('--require-domain-fit', command)
        self.assertIn('--require-difficulty-design', command)
        self.assertEqual(command[command.index('--expected-base-commit') + 1], self.base)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_committed_map_only_followups_are_allowed(self):
        (self.map_path.parent / 'notes.json').write_text('{"audit":"capacity metadata"}\n')
        self.commit('metadata followup')
        with self.mocks():
            report = self.check()
        self.assertTrue(report['ok'], report['issues'])
        self.assertNotEqual(report['source_head'], self.head)
        self.assertTrue(all(p.startswith('.s11-capacity/') for p in report['baseline_to_head_paths']))

    def test_capacity_directory_cannot_hide_non_json_business_code(self):
        for name in ('business.py', 'business.js', 'notes.txt', 'script.json.py'):
            with self.subTest(name=name):
                target = self.map_path.parent / name
                target.write_text('print("not metadata")\n')
                self.commit('forbidden code inside capacity directory')
                with self.mocks() as proc:
                    self.rejected(self.check(), name)
                proc.assert_not_called()
                target.unlink()
                self.commit('remove forbidden code')

    def test_capacity_json_must_be_actual_non_executable_regular_metadata(self):
        target = self.map_path.parent / 'extra.json'
        for content in ('print("code disguised as JSON")', '"not a metadata object"'):
            with self.subTest(content=content):
                target.write_text(content)
                self.commit('invalid metadata')
                with self.mocks():
                    self.rejected(self.check(), '普通 JSON 元数据')
        target.write_text('{}')
        target.chmod(0o755)
        self.commit('executable JSON is not metadata')
        with self.mocks():
            self.rejected(self.check(), '可执行权限')
        target.unlink()
        target.symlink_to(self.map_path)
        self.commit('symlinked JSON is not metadata')
        with self.mocks():
            self.rejected(self.check(), '符号链接')

    def test_source_change_between_S_and_H_is_rejected_even_when_clean(self):
        with (self.repo / 'src/orders.py').open('a') as f:
            f.write('# source changed after S\n')
        self.commit('forbidden source change')
        with self.mocks() as proc:
            report = self.check()
        self.rejected(report, 'S..H')
        proc.assert_not_called()

    def test_baseline_must_be_real_full_ancestor_commit(self):
        for value in ('HEAD', '', self.base[:10], '0' * 40):
            with self.subTest(value=value):
                self.payload['base_commit'] = value
                self.save_map()
                with self.mocks():
                    self.rejected(self.check(), 'base_commit' if value != '0' * 40 else 'git')

    def test_unrelated_baseline_rejected(self):
        self.git('checkout', '-q', '--orphan', 'unrelated')
        self.git('rm', '-rf', '.')
        (self.repo / 'other').write_text('unrelated')
        self.commit('other root')
        unrelated = self.git('rev-parse', 'HEAD').strip()
        self.git('checkout', '-q', 'main')
        self.payload['base_commit'] = unrelated
        self.save_map()
        with self.mocks():
            self.rejected(self.check(), '祖先')

    def test_missing_map_is_closed(self):
        self.map_path.unlink()
        with self.mocks() as proc:
            self.rejected(self.check(), '不是仓库内普通文件')
        proc.assert_not_called()

    def test_uncommitted_or_dirty_map_rejected(self):
        self.payload['note'] = 'not committed'
        self.save_map(commit=False)
        with self.mocks():
            self.rejected(self.check(), '不干净')

    def test_untracked_map_rejected(self):
        self.git('rm', '--cached', str(self.map_path.relative_to(self.repo)))
        with self.mocks():
            self.rejected(self.check(), 'map 未提交')

    def test_dirty_source_and_staged_source_rejected(self):
        (self.repo / 'src/orders.py').write_text(PRODUCER + '# dirty\n')
        with self.mocks():
            self.rejected(self.check(), '不干净')
        self.git('add', 'src/orders.py')
        with self.mocks():
            self.rejected(self.check(), '不干净')

    def test_untracked_source_and_ignored_source_rejected(self):
        for name in ('new.py', 'hidden.ignored.py'):
            with self.subTest(name=name):
                path = self.repo / 'src' / name
                path.write_text('print("uncommitted")\n')
                with self.mocks():
                    self.rejected(self.check(), '未提交' if 'ignored' in name else '不干净')
                path.unlink()

    def test_ignored_dependency_directory_does_not_dirty_source(self):
        path = self.repo / 'node_modules/pkg'
        path.mkdir(parents=True)
        (path / 'dep.js').write_text('module.exports = {};\n')
        with self.mocks():
            report = self.check()
        self.assertTrue(report['ok'], report['issues'])

    def test_assume_unchanged_cannot_hide_dirty_source(self):
        self.git('update-index', '--assume-unchanged', 'src/orders.py')
        (self.repo / 'src/orders.py').write_text(PRODUCER + '# hidden\n')
        with self.mocks():
            self.rejected(self.check(), 'assume-unchanged')

    def test_less_than_default_35_is_not_theoretical_capacity(self):
        self.payload = self.make_map(34)
        self.payload['max_capacity'] = 99999
        self.save_map()
        with self.mocks():
            report = self.check()
        self.rejected(report, 'target=35')
        self.assertEqual(report['qualified_plan_count'], 34)

    def test_custom_target_and_invalid_targets(self):
        self.payload = self.make_map(2)
        self.save_map()
        with self.mocks():
            diagnostic = self.check(target=2)
            self.assertTrue(diagnostic['ok'])
            self.assertTrue(diagnostic['diagnostic_only'])
            self.assertEqual(diagnostic['publication_target'], 35)
        for invalid in (0, -1, True, '35'):
            with self.subTest(target=invalid), self.mocks():
                self.rejected(self.check(target=invalid), 'target')

    def test_old_versions_and_policy_rejected(self):
        for key, value in (('version', 2), ('version', True), ('profile', 'legacy'),
                ('difficulty_contract_version', 1), ('type_policy', 'strict-ratio'), ('type_policy', None)):
            original = copy.deepcopy(self.payload)
            with self.subTest(key=key, value=value):
                self.payload[key] = value
                self.save_map()
                with self.mocks() as proc:
                    self.rejected(self.check(), key)
                proc.assert_not_called()
            self.payload = original

    def test_invalid_map_json_and_schema_closed(self):
        for payload in ('{', '[]', '{"version":1}', '{"task_plan": [null]}'):
            with self.subTest(payload=payload):
                self.map_path.write_text(payload)
                self.commit('invalid metadata')
                with self.mocks():
                    self.rejected(self.check())

    def test_duplicate_plan_sequences_closed(self):
        self.payload['task_plan'][1]['sequence'] = 1
        self.save_map()
        with self.mocks():
            self.rejected(self.check(), '唯一正整数')

    def test_no_downstream_has_no_fallback(self):
        report = self.check()
        self.rejected(report, '缺少下游脚本')
        self.assertEqual(report['qualified_plan_count'], 0)

    def test_downstream_rejection_is_never_counted(self):
        with self.mocks(side_effect=capacity.CapacityError('真实 H3 拒绝')):
            report = self.check()
        self.rejected(report, 'H3')
        self.assertEqual(report['qualified_plan_count'], 0)

    def test_downstream_ok_false_or_bad_count_is_rejected(self):
        for value in ({'ok': False, 'task_plan_count': 35}, {'ok': True, 'task_plan_count': 34},
                      {'ok': True, 'task_plan_count': '35'}, {'task_plan_count': 35}):
            with self.subTest(value=value), self.mocks(side_effect=[value]):
                self.rejected(self.check(), '下游未明确成功')

    def test_overfull_or_unknown_modes_closed_not_self_reported_mode(self):
        for mode in ('one-mode', ''):
            value = self.modes_result()
            for record in value['items']:
                record['mode'] = mode
            value['mode_counts'] = {mode: 35} if mode else {}
            self.payload['task_plan'][0]['mode'] = 'self-reported-distinct-mode'
            self.save_map()
            with self.subTest(mode=mode), self.mocks(side_effect=[self.validator_result(), value]):
                report = self.check()
                self.rejected(report, '每模式上限 3' if mode else '未识别主模式')
                self.assertEqual(report['qualified_plan_count'], 0)

    def test_corrupt_mode_result_counts_or_coverage_closed(self):
        for mutation in ('counts', 'duplicate', 'missing'):
            value = self.modes_result()
            if mutation == 'counts':
                value['mode_counts'] = {}
            elif mutation == 'duplicate':
                value['items'][1]['sequence'] = 1
            else:
                value['items'].pop()
            with self.subTest(mutation=mutation), self.mocks(side_effect=[self.validator_result(), value]):
                self.rejected(self.check(), '下游')

    def test_missing_evidence_and_single_surface_closed(self):
        plan = self.payload['task_plan'][0]
        for evidence in (None, [], plan['support_evidence'][:1]):
            with self.subTest(evidence=evidence):
                plan['support_evidence'] = evidence
                self.save_map()
                with self.mocks():
                    self.rejected(self.check(), 'support_evidence')

    def test_forged_anchor_noncontinuous_partial_and_wrong_lines_closed(self):
        anchor = self.payload['task_plan'][0]['support_evidence'][0]
        for excerpt in ('fabricated original source text', PRODUCER.splitlines()[0] + '\n' + PRODUCER.splitlines()[2],
                        PRODUCER.splitlines()[1].strip(), '...'):
            with self.subTest(excerpt=excerpt):
                anchor['excerpt'] = excerpt
                self.save_map()
                with self.mocks():
                    self.rejected(self.check(), '完整连续原文')
        anchor['start_line'], anchor['end_line'] = 100, 101
        self.save_map()
        with self.mocks():
            self.rejected(self.check(), '越界')

    def test_comment_or_docstring_only_original_anchor_is_not_behavior(self):
        # Both strings are authentic continuous text, but neither executes a
        # producer/consumer operation. This is structural, not semantic judging.
        self.assertFalse(capacity._contains_code('# publish real artifact here', '.py'))
        self.assertFalse(capacity._contains_code('"""produce and consume artifact"""', '.py'))
        self.assertFalse(capacity._contains_code('/* consume artifact */', '.ts'))
        self.assertFalse(capacity._contains_code('-- consume artifact', '.sql'))
        self.assertTrue(capacity._contains_code(PRODUCER, '.py'))
        # Exercise anchor validation in isolation; full-gate tests separately
        # enforce committed files, a clean worktree and the S..H restriction.
        target = self.repo / 'src/comment_only.py'
        target.write_text('# published artifact is consumed here\n')
        self.git('add', 'src/comment_only.py')
        with self.assertRaisesRegex(capacity.CapacityError, '只有注释'):
            capacity._anchor(self.repo, {'path': 'src/comment_only.py', 'start_line': 1,
                'end_line': 1, 'excerpt': '# published artifact is consumed here'},
                {'src/comment_only.py'}, business=True)

    def test_artifact_display_or_missing_behavior_closed(self):
        anchor = self.payload['task_plan'][0]['support_evidence'][1]
        for key, value in (('artifact', '伪造产物'), ('role', 'display'), ('behavior', ''), ('module', '伪模块')):
            original = copy.deepcopy(anchor)
            with self.subTest(key=key):
                anchor[key] = value
                self.save_map()
                with self.mocks():
                    self.rejected(self.check(), key)
            anchor.clear()
            anchor.update(original)

    def test_same_file_cannot_masquerade_as_cross_module(self):
        anchor = self.payload['task_plan'][0]['support_evidence'][1]
        anchor.update(path='src/orders.py', start_line=1, end_line=3, excerpt=PRODUCER.rstrip('\n'))
        self.save_map()
        with self.mocks():
            self.rejected(self.check(), '不同业务源码文件')

    def test_all_declared_secondary_modules_need_behavior_evidence(self):
        integration = self.payload['task_plan'][0]['difficulty_design']['module_integration']
        integration['secondary_modules'].append({'name': '审计写入', 'role': 'audit', 'artifact': '审计记录'})
        self.save_map()
        with self.mocks():
            self.rejected(self.check(), '未覆盖关键行为模块: 审计写入')

    def test_anchor_traversal_absolute_symlink_parent_and_nonbusiness_closed(self):
        (self.repo / 'linked').symlink_to(self.repo / 'src', target_is_directory=True)
        self.commit('symlink is forbidden diff too')
        anchor = self.payload['task_plan'][0]['support_evidence'][0]
        for name in ('../outside.py', str(self.repo / 'src/orders.py'), 'linked/orders.py', '.s11-capacity/repo-topic-map.json'):
            with self.subTest(name=name):
                anchor['path'] = name
                self.save_map()
                with self.mocks():
                    self.rejected(self.check(), 'support_evidence')

    def test_optional_bugfix_fields_checked_but_not_executed(self):
        plan = self.payload['task_plan'][0]
        plan.update(slug='bugfix', task_type='Bugfix')
        plan['bugfix_reproduction'] = {'command': 'DO_NOT_EXECUTE_ANYTHING',
            'expected_behavior': '保留已发布数据', 'observed_behavior': '重试覆盖已发布数据',
            'anchors': [copy.deepcopy(plan['support_evidence'][0])]}
        self.save_map()
        with self.mocks():
            report = self.check()
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(report['bugfix_reproduction_fields_checked'], 1)
        self.assertFalse(report['verification_scope']['bugfix_reproduction_executed'])
        plan['bugfix_reproduction']['observed_behavior'] = '保留已发布数据'
        self.save_map()
        with self.mocks():
            self.rejected(self.check(), '预期行为')

    def test_malformed_optional_bugfix_evidence_closed(self):
        for evidence in (None, {}, {'command': 'x', 'anchors': []}):
            self.payload['task_plan'][0]['bugfix_reproduction'] = evidence
            self.save_map()
            with self.subTest(evidence=evidence), self.mocks():
                self.rejected(self.check(), 'bugfix_reproduction')

    def test_fingerprint_changes_on_map_head_worktree_and_downstream(self):
        with self.mocks():
            first = self.check()
        self.payload['audit_note'] = 'changed metadata'
        self.save_map()
        with self.mocks():
            second = self.check()
        self.assertTrue(second['ok'], second['issues'])
        for field in ('head', 'map_sha256', 'worktree', 'sha256'):
            self.assertNotEqual(first['fingerprint'][field], second['fingerprint'][field])
        (self.repo / 'src/orders.py').write_text(PRODUCER + '# dirty\n')
        with self.mocks():
            dirty = self.check()
        self.assertNotEqual(second['fingerprint']['worktree'], dirty['fingerprint']['worktree'])
        self.rejected(dirty)
        self.git('checkout', '--', 'src/orders.py')
        self.downstream.joinpath('scripts').mkdir(parents=True)
        (self.downstream / 'scripts/extra.py').write_text('# additional installed rule\n')
        # Use actual script hashes here; required-script rejection is intentional.
        changed = capacity.check_capacity(self.repo, downstream_skill=self.downstream)
        self.assertNotEqual(second['fingerprint']['downstream_sha256'], changed['fingerprint']['downstream_sha256'])

    def test_no_cached_json_is_used_and_each_call_reruns_validator(self):
        with self.mocks() as proc:
            first = self.check()
            self.assertTrue(first['ok'])
            self.assertEqual(proc.call_count, 2)
        with self.mocks(side_effect=capacity.CapacityError('new downstream rejection')) as proc:
            self.rejected(self.check(), 'new downstream rejection')
            self.assertEqual(proc.call_count, 1)

    def test_mutation_during_check_invalidates_result(self):
        def changed(command, repo):
            if '-c' not in command:
                return self.validator_result()
            (self.repo / 'src/orders.py').write_text(PRODUCER + '# raced\n')
            return self.modes_result()
        with self.mocks(side_effect=changed):
            report = self.check()
        self.rejected(report, '检查期间')
        self.assertEqual(report['qualified_plan_count'], 0)

    def test_process_boundary_rejects_bad_json_exit_timeout_and_missing_binary(self):
        command = ['missing-validator']
        for proc in (subprocess.CompletedProcess(command, 0, b'not json', b''),
                     subprocess.CompletedProcess(command, 0, b'[]', b''),
                     subprocess.CompletedProcess(command, 1, b'{"ok":true}', b'rejected')):
            with self.subTest(proc=proc), patch.object(capacity, '_run', return_value=proc):
                with self.assertRaises(capacity.CapacityError):
                    capacity._json_process(command, self.repo)
        for error in (FileNotFoundError('no tool'), subprocess.TimeoutExpired(command, 1)):
            with self.subTest(error=error), patch.object(capacity.subprocess, 'run', side_effect=error):
                with self.assertRaises(capacity.CapacityError):
                    capacity._run(command, cwd=self.repo)

    def test_cli_json_exit_and_report_outside_repo(self):
        out = self.root / 'capacity-report.json'
        with self.mocks(), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = capacity.main(['--repo', str(self.repo), '--downstream-skill', str(self.downstream),
                                  '--json-out', str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), json.loads(out.read_text()))
        self.assertEqual(self.git('status', '--porcelain'), '')
        for protected in (self.map_path, self.repo / 'report.json', self.downstream / 'report.json'):
            with self.mocks(), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = capacity.main(['--repo', str(self.repo), '--downstream-skill', str(self.downstream),
                                      '--json-out', str(protected)])
            self.assertEqual(code, 1)
            self.rejected(json.loads(stdout.getvalue()), '写报告失败')

    def test_cli_missing_map_real_exit_is_one_not_traceback(self):
        self.map_path.unlink()
        proc = subprocess.run([sys.executable, '-B', str(SCRIPT), '--repo', str(self.repo)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.rejected(json.loads(proc.stdout))
        self.assertNotIn('Traceback', proc.stderr)

    def test_map_outside_repo_or_under_symlink_is_closed(self):
        outside = self.root / 'outside.json'
        outside.write_text('{}')
        report = capacity.check_capacity(self.repo, map_path=outside, downstream_skill=self.downstream)
        self.rejected(report)
        self.map_path.unlink()
        self.map_path.symlink_to(outside)
        with self.mocks():
            self.rejected(self.check(), '符号链接')


@unittest.skipUnless((REAL_SKILL / 'scripts/validate_topic_map.py').is_file(),
                     'real downstream skill not installed; no synthetic substitute')
class RealDownstreamNegativeTest(CapacityFixture, unittest.TestCase):
    def real_payload(self):
        self.payload = self.make_map(1)
        self.payload['topics'][0].update(domain_objects=['订单', '数据集'],
                                         domain_results=['订单明细文件', '字段完整'])
        plan = self.payload['task_plan'][0]
        plan['family'] = '优化既有能力'
        fit = plan['domain_fit']
        fit.update(main_task='优化现有链路的失败恢复能力，解析订单数据并导出订单明细文件', business_result='订单明细文件字段完整',
            domain_chain={'input': '订单数据', 'operation': '解析订单数据',
                          'output': '订单明细文件', 'observable': '导出文件字段完整'},
            prompt_main_request='请优化现有链路的失败恢复能力，解析订单数据并导出订单明细文件，保证导出文件字段完整。',
            genericization={'rewritten_with_generic_objects': '优化构建工具和路由配置',
                            'same_task_without_domain_semantics': False},
            source_evidence=['src/orders.py', 'src/exports.py'], conclusion='pass',
            domain_anchors=[{'path': 'src/orders.py', 'start_line': 2, 'end_line': 2,
                             'supports': 'operation', 'excerpt': PRODUCER.splitlines()[1]},
                            {'path': 'src/exports.py', 'start_line': 2, 'end_line': 2,
                             'supports': 'output', 'excerpt': CONSUMER.splitlines()[1]}])
        design = plan['difficulty_design']
        design.update(reasoning_challenge={
            'constraints': [
                {'kind': 'transaction_consistency', 'statement': '订单数据与来源批次必须同时可见',
                 'failure_if_omitted': '下游会读到没有来源的明细'},
                {'kind': 'failure_recovery', 'statement': '中断后只补写缺失记录，不能覆盖完整版本',
                 'failure_if_omitted': '重试会回退已经可用的数据结果'}],
            'why_interact': '恢复位置由已可见批次决定，写入范围又决定下游读取版本。',
            'prompt_excerpt': '中断后保留上一份完整订单数据集，重试只补写缺失记录。'},
            design_tradeoff={'decision': '恢复边界按采集时间还是业务时间确定',
                'options': ['按采集时间恢复', '按业务时间恢复'],
                'outcome_invariant': '恢复完成下游只能看到完整订单数据集',
                'why_open': '两种边界都可行，题面只固定可观察结果，不选择内部键。',
                'implementation_not_prescribed': True,
                'prompt_excerpt': '请自行选择恢复边界采用哪种时间，保证下游结果完整。'},
            medium_counterexample={'generic_task': '给订单列表增加导出按钮并保持列表和详情一致',
                                   'why_medium': '只做局部展示与保存，没有数据产物约束或设计判断'},
            hard_evidence=['事务与落库一致性', '断点续做与失败链路'],
            anti_patterns={k: False for k in ('single_surface_only', 'secondary_observation_only',
                'solution_prescribed', 'single_constraint', 'hard_evidence_tail_only',
                'example_data_only', 'cross_call_merge_only')},
            module_anchors=[{**a, 'supports': 'primary' if i == 0 else 'secondary'}
                            for i, a in enumerate(plan['support_evidence'])])
        design['module_integration']['prompt_excerpt'] = '分析导出中心必须读取同一份订单数据集并保留来源批次。'
        self.save_map()

    def real_check(self):
        return capacity.check_capacity(self.repo, downstream_skill=REAL_SKILL, target=1)

    def validator_command(self):
        return [sys.executable, '-B', str(REAL_SKILL / 'scripts/validate_topic_map.py'),
            '--map', str(self.map_path), '--repo', str(self.repo), '--require-domain-fit',
            '--require-difficulty-design', '--expected-base-commit', self.base]

    def test_real_validator_current_schema_negative_not_mocked(self):
        self.real_payload()
        self.payload['version'] = 2  # semantic-v2 profile != map format version 2
        self.save_map()
        with self.assertRaisesRegex(capacity.CapacityError, '只支持 version=1'):
            capacity._json_process(self.validator_command(), self.repo)

    def test_real_validator_rejects_display_only_H2(self):
        self.real_payload()
        self.payload['task_plan'][0]['difficulty_design']['module_integration']['secondary_modules'][0]['role'] = 'display'
        self.save_map()
        with self.assertRaisesRegex(capacity.CapacityError, '真实数据流角色'):
            capacity._json_process(self.validator_command(), self.repo)

    def test_real_validator_rejects_missing_H1(self):
        self.real_payload()
        del self.payload['task_plan'][0]['difficulty_design']['reasoning_challenge']
        self.save_map()
        report = self.real_check()
        self.rejected(report, 'reasoning_challenge')
        self.assertIn('downstream_command', report)
        self.assertEqual(report['qualified_plan_count'], 0)

    def test_real_validator_rejects_closed_H3(self):
        self.real_payload()
        self.payload['task_plan'][0]['difficulty_design']['design_tradeoff']['prompt_excerpt'] = '恢复边界必须固定按采集时间，禁止选择其他方案。'
        self.save_map()
        report = self.real_check()
        self.rejected(report, '真实选择权')

    def test_real_validator_rejects_domain_genericization(self):
        self.real_payload()
        self.payload['task_plan'][0]['domain_fit']['genericization']['same_task_without_domain_semantics'] = True
        self.save_map()
        self.rejected(self.real_check(), 'same_task_without_domain_semantics')

    def test_real_batch_mode_adapter_uses_current_theme_classifier(self):
        self.real_payload()
        command = [sys.executable, '-B', '-c', capacity.MODE_ADAPTER, str(REAL_SKILL / 'scripts'),
                   str(self.repo), str(self.map_path), self.base]
        result = capacity._json_process(command, self.repo)
        self.assertEqual(result['checked_count'], 1)
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['items'][0]['mode'])
        self.assertEqual(result['mode_counts'], {result['items'][0]['mode']: 1})


if __name__ == '__main__':
    unittest.main(verbosity=2)
