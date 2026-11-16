#!/usr/bin/env python3
"""Read-only, fail-closed admission of *planned* downstream difficult tasks.

Public API: check_capacity(repo, map_path=None, downstream_skill=None, target=35)
returns a JSON-serializable report. verify/publish MUST call it afresh with
target=35 and require report['ok'] is True, not trust an old JSON file or the
count alone. Smaller targets are diagnostics only (diagnostic_only=True).
S11_PROMPT_SKILL overrides the downstream default, including at call time.
No cache, network, Git writes, prompt generation, or business-source execution
is used. CLI exits 0 for the requested target, 1 on rejection/unavailable tools.

Map: version=1, profile=gsb-semantic-v2, difficulty_contract_version=2,
explicit type_policy=capacity-first, base_commit=S (full source-baseline SHA).
Commit source baseline S first, then commit only JSON metadata under
.s11-capacity/ to HEAD H. Require S to be an ancestor of H and git diff S..H
to contain ONLY non-executable regular *.json metadata under .s11-capacity/. Pass S to the downstream --expected-base-commit; source_head=H
and source_base_commit=S. Both commits and the clean worktree are fingerprinted.

Additional task_plan[i].support_evidence is a list of >=2 objects:
  {module, role, artifact, behavior, path, start_line, end_line, excerpt}
Use difficulty_design.module_integration's exact module/artifact/secondary role
names; primary role is producer/writer/orchestrator. Cover the primary and ALL
secondary modules using distinct tracked business-source files. Each support
anchor must overlap that module's downstream module_anchors in the same path.
excerpt must be the COMPLETE continuous lines [start_line,end_line], without line-number prefixes
or ellipses (LF/CRLF is normalized). behavior describes the operation, not merely
presentation. Display/observation roles are not accepted as behavioral evidence.
Comment/docstring-only anchors are rejected, but code tokens alone are NOT
semantic proof. These checks establish provenance and declared dataflow only:
a human still must check the code actually implements the claimed behavior.

Optional task_plan[i].bugfix_reproduction:
  {command, expected_behavior, observed_behavior, anchors: [{path,start_line,
    end_line,excerpt}, ...]}
When supplied on a bugfix, validate structure and original anchors only. Never
execute command or claim reproduction/causality was verified. Missing is allowed.

Mode quota uses the installed check_repo_theme.check (including batch template
removal), not a copied lexicon or a self-reported mode. Its other gates are NOT
claimed passed here. Input is domain_fit.prompt_main_request plus the three
H1/H2/H3 prompt_excerpts, deduplicated in that order; final prompts don't exist
at this stage and must be checked separately by downstream. CLI/API ok=True
for target=1 is diagnostic success, NOT permission to publish 35 tasks.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tokenize
from collections import Counter
from typing import Any

DEFAULT_DOWNSTREAM = Path(os.environ.get('S11_PROMPT_SKILL') or
                          Path.home() / '.codex/skills/s11-gsb-prompt-create').expanduser()
SOURCE_SUFFIXES = frozenset({'.py', '.pyi', '.js', '.jsx', '.ts', '.tsx', '.mjs',
    '.cjs', '.vue', '.svelte', '.go', '.java', '.kt', '.kts', '.rs', '.rb', '.php',
    '.cs', '.cpp', '.cc', '.c', '.h', '.hpp', '.scala', '.sql', '.sh'})
GENERATED_DIRS = frozenset({'node_modules', '.venv', 'venv', '__pycache__',
    'dist', 'build', 'coverage', '.next', '.nuxt', 'target', '.git'})
NON_BUSINESS_DIRS = GENERATED_DIRS | {'tests', 'test', '__tests__', 'fixtures',
    'docs', 'documentation', 'examples', '.s11-capacity'}

# This adapter ONLY delegates to the installed downstream. It contains no H1/H2/
# H3 rules, classifiers, or synthetic pass results. check()'s general ok is not
# used: only the actual batch-classified primary modes are capacity inputs.
MODE_ADAPTER = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import check_repo_theme as theme
from repo_topic_policy import load_topic_map
m = load_topic_map(repo=Path(sys.argv[2]), explicit=sys.argv[3], required=True,
                   expected_base_commit=sys.argv[4])
items = []
for p in m["task_plan"]:
    d = p.get("difficulty_design") or {}
    texts = [(p.get("domain_fit") or {}).get("prompt_main_request", "")]
    texts += [(d.get(k) or {}).get("prompt_excerpt", "") for k in
              ("reasoning_challenge", "module_integration", "design_tradeoff")]
    text = "\n".join(dict.fromkeys(t.strip() for t in texts if isinstance(t, str) and t.strip()))
    items.append((str(p["sequence"]), text))
r = theme.check(items, topic_map=m, max_per_mode=3)
print(json.dumps({"checked_count": r["checked_count"], "mode_counts": r["mode_counts"],
    "items": [{"sequence": int(p["label"]), "mode": p["mode"]} for p in r["items"]],
    "gate_version": r["gate_version"]}, ensure_ascii=False))
'''


class CapacityError(ValueError):
    """An expected, reportable gate failure."""


def _default_downstream() -> Path:
    return Path(os.environ.get('S11_PROMPT_SKILL') or DEFAULT_DOWNSTREAM).expanduser().resolve()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Parent shells' Git/Python configuration must not redirect repo/imports.
    for name in list(env):
        if name.startswith('GIT_') or name in {'PYTHONPATH', 'PYTHONHOME'}:
            env.pop(name, None)
    env.update(PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
    try:
        return subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapacityError(f"无法运行/超时 {command[0]}: {exc}") from exc


def _git(repo: Path, *args: str) -> bytes:
    proc = _run(['git', '-C', str(repo), *args], cwd=repo)
    if proc.returncode:
        raise CapacityError(f"git {' '.join(args)} 失败: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _relative_file(root: Path, raw: Any) -> tuple[Path, str]:
    if not _text(raw):
        raise CapacityError('path 必须是非空相对路径')
    rel = Path(raw)
    if rel.is_absolute() or '..' in rel.parts or '.git' in rel.parts:
        raise CapacityError(f'不安全 path: {raw}')
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise CapacityError(f'path 不允许符号链接（含父目录）: {raw}')
    if not current.is_file() or not current.resolve().is_relative_to(root):
        raise CapacityError(f'path 不是仓库内普通文件: {raw}')
    return current, rel.as_posix()


def _snapshot(repo: Path, map_path: Path, scripts: Path) -> dict:
    root = Path(os.fsdecode(_git(repo, 'rev-parse', '--show-toplevel')).strip()).resolve()
    if root != repo:
        raise CapacityError('--repo 必须是 Git 仓库根目录')
    head = _git(repo, 'rev-parse', '--verify', 'HEAD').decode().strip()
    status = _git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                  '--ignore-submodules=none')
    tracked = sorted(set(os.fsdecode(p) for p in _git(repo, 'ls-files', '-z').split(b'\0') if p))
    untracked = sorted(set(os.fsdecode(p) for p in _git(
        repo, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if p))
    ignored_sources = []
    for raw in _git(repo, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').split(b'\0'):
        if raw:
            name = os.fsdecode(raw)
            p = Path(name)
            if p.suffix.lower() in SOURCE_SUFFIXES and not (set(p.parts) & GENERATED_DIRS):
                ignored_sources.append(name)
    paths = sorted(set(tracked + untracked + ignored_sources))
    files = {}
    for name in paths:
        path = repo / name
        if path.is_symlink():
            files[name] = {'symlink': os.readlink(path)}
        elif path.is_file():
            files[name] = {'sha256': _sha(path.read_bytes()), 'mode': path.stat().st_mode & 0o777}
        elif path.is_dir():
            raise CapacityError(f'不支持未展开的子模块/目录: {name}')
        else:
            files[name] = {'missing': True}
    flags = _git(repo, 'ls-files', '-v', '-z').split(b'\0')
    hidden = [os.fsdecode(f) for f in flags if f and (chr(f[0]).islower() or f[:1] == b'S')]
    downstream = {}
    if scripts.is_dir():
        for path in sorted(scripts.rglob('*.py')):
            if '__pycache__' not in path.parts:
                if path.is_symlink():
                    raise CapacityError(f'下游脚本不允许符号链接: {path}')
                downstream[path.relative_to(scripts).as_posix()] = _sha(path.read_bytes())
    worktree = {'status_sha256': _sha(status), 'index_sha256': _sha(_git(repo, 'ls-files', '--stage', '-z')),
                'files_sha256': _digest(files), 'file_count': len(files)}
    return {'head': head, 'worktree': worktree, 'map_sha256': _sha(map_path.read_bytes())
            if map_path.is_file() else None, 'downstream_scripts': downstream,
            'downstream_sha256': _digest(downstream), 'checker_sha256': _sha(Path(__file__).read_bytes()),
            'dirty_status': status.decode(errors='replace').replace('\0', '\n'),
            'ignored_sources': sorted(ignored_sources), 'hidden_index_flags': hidden,
            'tracked': tracked}


def _contains_code(region: str, suffix: str) -> bool:
    """Reject comment/docstring-only anchors, without claiming semantic proof."""
    if suffix in {'.py', '.pyi'}:
        try:
            for token in tokenize.generate_tokens(io.StringIO(region).readline):
                if token.type in {tokenize.NAME, tokenize.OP, tokenize.NUMBER}:
                    return True
        except (tokenize.TokenError, IndentationError):
            # A region may end in the middle of an expression; never turn an
            # unparseable/comment-only region into positive behavior evidence.
            return False
        return False
    # This is only a comment filter, not a cross-language behavior classifier.
    cleaned = re.sub(r'/\*.*?\*/|//[^\n]*|^[ \t]*(?:\#|--)[^\n]*', '',
                     region, flags=re.DOTALL | re.MULTILINE)
    return bool(cleaned.strip())


def _anchor(repo: Path, raw: Any, tracked: set[str], *, business: bool) -> tuple[str, str]:
    if not isinstance(raw, dict):
        raise CapacityError('anchor 必须是对象')
    path, rel = _relative_file(repo, raw.get('path'))
    if rel not in tracked:
        raise CapacityError(f'anchor 源码未提交/未跟踪: {rel}')
    if business and (path.suffix.lower() not in SOURCE_SUFFIXES or set(Path(rel).parts) & NON_BUSINESS_DIRS):
        raise CapacityError(f'anchor 必须是业务源码而非展示说明/测试/生成物: {rel}')
    start, end = raw.get('start_line'), raw.get('end_line')
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise CapacityError('start_line/end_line 必须是合法整数范围（1-based）')
    lines = path.read_text(encoding='utf-8').splitlines()
    if end > len(lines):
        raise CapacityError(f'anchor 行号越界: {rel}:{start}-{end}')
    region = '\n'.join(lines[start - 1:end])
    excerpt = raw.get('excerpt')
    if not _text(excerpt) or len(excerpt.strip()) < 8 or excerpt.replace('\r\n', '\n') != region:
        raise CapacityError(f'anchor excerpt 必须逐字等于完整连续原文（不能拼接/省略）: {rel}:{start}-{end}')
    if business and not _contains_code(region, path.suffix.lower()):
        raise CapacityError(f'anchor 只有注释/说明，不能作为业务行为证据: {rel}:{start}-{end}')
    return rel, region


def _support_errors(repo: Path, plan: dict, tracked: set[str]) -> list[str]:
    errors = []
    design = plan.get('difficulty_design')
    integration = design.get('module_integration') if isinstance(design, dict) else None
    if not isinstance(integration, dict):
        return ['support_evidence 无法绑定 difficulty_design.module_integration']
    primary = integration.get('primary_module')
    secondaries = integration.get('secondary_modules')
    if not _text(primary) or not isinstance(secondaries, list) or not secondaries:
        return ['support_evidence 缺少可绑定的主模块/次模块']
    expected = {primary: (integration.get('data_artifact'), {'producer', 'writer', 'orchestrator'})}
    for secondary in secondaries:
        if not isinstance(secondary, dict) or not _text(secondary.get('name')) or not _text(secondary.get('role')):
            return ['support_evidence 次模块声明无效']
        name = secondary['name']
        if name in expected:
            return ['support_evidence 跨模块名称不能相同/重复']
        expected[name] = (secondary.get('artifact'), {secondary['role']})
    evidence = plan.get('support_evidence')
    if not isinstance(evidence, list) or len(evidence) < 2:
        return ['support_evidence 每题至少两条带行号连续原文锚点及 artifact']
    module_anchors = design.get('module_anchors')
    if not isinstance(module_anchors, list) or not module_anchors:
        return ['support_evidence 无法绑定 difficulty_design.module_anchors']
    covered: dict[str, set[str]] = {}
    for i, item in enumerate(evidence):
        try:
            rel, _ = _anchor(repo, item, tracked, business=True)
            module = item.get('module')
            if not isinstance(module, str) or module not in expected:
                raise CapacityError('module 必须匹配 difficulty_design 的主/次模块')
            linked = [a for a in module_anchors if isinstance(a, dict)
                      and a.get('module') == module and a.get('path') == rel
                      and type(a.get('start_line')) is int and type(a.get('end_line')) is int
                      and a['start_line'] <= item['end_line'] and a['end_line'] >= item['start_line']]
            if not linked:
                raise CapacityError('anchor 必须与该模块 difficulty_design.module_anchors 的源码区间重叠')
            artifact, roles = expected[module]
            if not _text(artifact) or item.get('artifact') != artifact:
                raise CapacityError('artifact 必须与该模块困难设计的数据产物一致')
            role = item.get('role')
            if not isinstance(role, str) or role not in roles or role in {'display', 'view', 'observer', 'observation', 'presentation'}:
                raise CapacityError('role 必须是设计声明的数据流角色，不能仅展示/观察')
            if not _text(item.get('behavior')):
                raise CapacityError('behavior 必须描述关键操作；词面本身不能证明行为语义')
            covered.setdefault(module, set()).add(rel)
        except (CapacityError, OSError, UnicodeError) as exc:
            errors.append(f'support_evidence[{i}]: {exc}')
    for module in expected:
        if module not in covered:
            errors.append(f'support_evidence 未覆盖关键行为模块: {module}')
    if len(set().union(*covered.values()) if covered else set()) < 2:
        errors.append('support_evidence 必须锚定至少两个不同业务源码文件')
    # One file cannot masquerade as evidence for two different modules.
    for module, paths in covered.items():
        other_paths = set().union(*(v for k, v in covered.items() if k != module))
        if not paths - other_paths:
            errors.append(f'support_evidence 模块 {module} 缺少独立源码文件')
    return errors


def _reproduction_errors(repo: Path, plan: dict, tracked: set[str]) -> list[str]:
    if 'bugfix_reproduction' not in plan:
        return []
    obj = plan['bugfix_reproduction']
    if plan.get('slug') != 'bugfix' or not isinstance(obj, dict):
        return ['bugfix_reproduction 只能是 bugfix 题的对象字段']
    errors = []
    for key in ('command', 'expected_behavior', 'observed_behavior'):
        if not _text(obj.get(key)):
            errors.append(f'bugfix_reproduction.{key} 必须非空')
    if obj.get('expected_behavior') == obj.get('observed_behavior'):
        errors.append('bugfix_reproduction 预期行为与观察到的错误行为不能相同')
    anchors = obj.get('anchors')
    if not isinstance(anchors, list) or not anchors:
        errors.append('bugfix_reproduction.anchors 必须是非空数组')
    else:
        for i, anchor in enumerate(anchors):
            try:
                _anchor(repo, anchor, tracked, business=False)
            except (CapacityError, OSError, UnicodeError) as exc:
                errors.append(f'bugfix_reproduction.anchors[{i}]: {exc}')
    return errors


def _json_process(command: list[str], repo: Path) -> dict:
    proc = _run(command, cwd=repo)
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, UnicodeError):
        raise CapacityError(f'下游未返回有效 JSON (exit={proc.returncode}): '
            f'{proc.stderr.decode(errors="replace")[-3000:]}') from None
    if not isinstance(payload, dict):
        raise CapacityError('下游 JSON 必须是对象')
    if proc.returncode:
        raise CapacityError(f'下游拒绝 (exit={proc.returncode}): '
            f'{payload.get("error", payload)}; {proc.stderr.decode(errors="replace")[-1000:]}')
    return payload


def check_capacity(repo: str | Path, map_path: str | Path | None = None,
                   downstream_skill: str | Path | None = None, target: int = 35) -> dict:
    """Fresh read-only gate for verify/publish; never consumes a previous report.

    qualified_plan_count is all-or-nothing for integrity, downstream, mode and
    evidence gates (0 if any fails). A valid 34-task map reports 34 but ok=False
    for target=35. issues is always a readable list[str]. Unexpected programming
    errors are not converted into success. See module docstring for schemas.
    """
    repo = Path(repo).expanduser().resolve()
    map_path = Path(map_path).expanduser().absolute() if map_path else repo / '.s11-capacity/repo-topic-map.json'
    skill = Path(downstream_skill).expanduser().resolve() if downstream_skill else _default_downstream()
    scripts = skill / 'scripts'
    issues: list[str] = []
    report = {'ok': False, 'schema_version': 1, 'repo': str(repo), 'map': str(map_path),
        'downstream_skill': str(skill), 'target': target, 'publication_target': 35,
        'diagnostic_only': type(target) is int and target < 35, 'planned_count': 0,
        'qualified_plan_count': 0, 'qualification_policy': 'all-or-nothing-except-target',
        'issues': issues, 'fingerprint': None, 'mode_counts': {},
        'verification_scope': {'final_prompt_quality_verified': False,
            'platform_pass_verified': False, 'behavior_semantics_verified': False,
            'bugfix_reproduction_executed': False, 'full_theme_gate_verified': False},
        'limitations': ['仅验证已提交计划的下游设计门槛、主模式配额和原文锚点；不证明最终 prompt 质量或平台通过。',
            '角色、artifact 和 behavior 是可追溯声明；不能仅靠词面证明跨模块真实行为，仍须人工语义复核。',
            '可选 bugfix_reproduction 仅查字段和锚点，不运行复现命令，不验证缺陷因果。',
            '模式输入为计划主请求及 H1/H2/H3 摘录，最终题面、历史去重和完整主题门需下游另验。']}
    before = None
    try:
        if type(target) is not int or target < 1:
            raise CapacityError('target 必须是正整数')
        if not repo.is_dir():
            raise CapacityError(f'仓库不存在: {repo}')
        _, map_rel = _relative_file(repo, str(map_path.relative_to(repo)))
        before = _snapshot(repo, map_path, scripts)
        report['source_head'] = before['head']
        if before['dirty_status']:
            issues.append('工作树/索引不干净（业务源码及 map 必须提交）: ' + before['dirty_status'])
        if before['ignored_sources']:
            issues.append('存在被忽略的未提交源码: ' + ', '.join(before['ignored_sources']))
        if before['hidden_index_flags']:
            issues.append('不允许 assume-unchanged/skip-worktree 隐藏源码变更: ' + ', '.join(before['hidden_index_flags']))
        if map_rel not in before['tracked']:
            issues.append('map 未提交：请放在 repo/.s11-capacity/repo-topic-map.json 并提交')
        # A staged-but-uncommitted map is rejected by dirty_status above.
        raw = json.loads(map_path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            raise CapacityError('map 顶层必须是对象')
        for key, value in (('version', 1), ('profile', 'gsb-semantic-v2'),
                ('difficulty_contract_version', 2), ('type_policy', 'capacity-first')):
            if raw.get(key) != value or (isinstance(value, int) and type(raw.get(key)) is not int):
                issues.append(f'map.{key} 必须是 {value!r}（不能用过期版本/基线）')
        base = raw.get('base_commit')
        if not isinstance(base, str) or len(base) not in {40, 64} or any(c not in '0123456789abcdef' for c in base):
            raise CapacityError('map.base_commit 必须是已提交源码基线 S 的完整 SHA，不能是 HEAD/空值/缩写')
        resolved_base = _git(repo, 'rev-parse', '--verify', base + '^{commit}').decode().strip()
        if resolved_base != base:
            raise CapacityError('map.base_commit 不是精确的 commit SHA')
        ancestry = _run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', base, before['head']], cwd=repo)
        if ancestry.returncode != 0:
            raise CapacityError('源码基线 S 必须是当前 HEAD H 的祖先')
        changed = [os.fsdecode(p) for p in _git(repo, 'diff', '--no-ext-diff', '--no-renames',
                    '--name-only', '-z', base, before['head'], '--').split(b'\0') if p]
        forbidden = [p for p in changed if not p.startswith('.s11-capacity/') or not p.endswith('.json')]
        if forbidden:
            issues.append('S..H 只允许 .s11-capacity/ 下 JSON 元数据，存在源码/其他路径 diff: ' + ', '.join(forbidden))
        for rel in changed:
            if rel in forbidden:
                continue
            candidate = repo / rel
            # Deletion of a JSON metadata file is permitted; all surviving files
            # must really be JSON data, not executable or symlinked code.
            if not candidate.exists() and not candidate.is_symlink():
                continue
            try:
                candidate, _ = _relative_file(repo, rel)
                if candidate.stat().st_mode & 0o111:
                    raise CapacityError('JSON 元数据不能带可执行权限')
                metadata = json.loads(candidate.read_text(encoding='utf-8'))
                if not isinstance(metadata, (dict, list)):
                    raise CapacityError('JSON 元数据顶层必须是对象或数组')
            except (CapacityError, OSError, ValueError, UnicodeError) as exc:
                issues.append(f'S..H 容量材料不是普通 JSON 元数据 {rel}: {exc}')
        if not map_rel.startswith('.s11-capacity/'):
            issues.append('map 必须位于仓库 .s11-capacity/ 下并在源码基线 S 之后单独提交')
        report['source_base_commit'] = base
        report['baseline_to_head_paths'] = changed
        plans = raw.get('task_plan')
        if not isinstance(plans, list) or not plans or any(not isinstance(p, dict) for p in plans):
            raise CapacityError('task_plan 必须是非空对象数组')
        report['planned_count'] = len(plans)
        sequences = [p.get('sequence') for p in plans]
        if any(type(s) is not int or s < 1 for s in sequences) or len(set(sequences)) != len(plans):
            raise CapacityError('task_plan.sequence 必须是唯一正整数')
        tracked = set(before['tracked'])
        for plan in plans:
            for error in _support_errors(repo, plan, tracked) + _reproduction_errors(repo, plan, tracked):
                issues.append(f'task_plan sequence={plan["sequence"]}: {error}')
        report['bugfix_reproduction_fields_checked'] = sum('bugfix_reproduction' in p for p in plans)
        # No fallback: all installed scripts/imports must actually be available.
        for required in ('validate_topic_map.py', 'check_repo_theme.py', 'repo_topic_policy.py',
                         'difficulty_contract.py', 'domain_fit.py', 's11_policy.py', 'check_prompt_type_intent.py'):
            if required not in before['downstream_scripts']:
                issues.append(f'缺少下游脚本: {scripts / required}')
        if not issues:
            command = [sys.executable, '-I', '-B', str(scripts / 'validate_topic_map.py'),
                '--map', str(map_path), '--repo', str(repo), '--require-domain-fit',
                '--require-difficulty-design', '--expected-base-commit', base]
            report['downstream_command'] = command
            validated = _json_process(command, repo)
            report['downstream_result'] = validated
            if validated.get('ok') is not True or type(validated.get('task_plan_count')) is not int or validated['task_plan_count'] != len(plans):
                raise CapacityError('下游未明确成功或 task_plan_count 与实际计划不一致')
            if validated.get('version') != 1 or validated.get('profile') != 'gsb-semantic-v2' or validated.get('base_commit') != base:
                raise CapacityError('下游返回的 version/profile/base_commit 与当前 map 契约不一致')
            modes = _json_process([sys.executable, '-I', '-B', '-c', MODE_ADAPTER,
                                   str(scripts), str(repo), str(map_path), base], repo)
            records = modes.get('items')
            if modes.get('checked_count') != len(plans) or not isinstance(records, list) or len(records) != len(plans):
                raise CapacityError('下游主模式分类数量不一致')
            counts = Counter()
            seen = set()
            for item in records:
                if not isinstance(item, dict) or type(item.get('sequence')) is not int or item['sequence'] not in sequences or item['sequence'] in seen:
                    raise CapacityError('下游主模式记录丢失/重复/sequence 不匹配')
                seen.add(item['sequence'])
                mode = item.get('mode')
                if not _text(mode):
                    issues.append(f'task_plan sequence={item["sequence"]}: 下游未识别主模式')
                else:
                    counts[mode] += 1
            if dict(counts) != modes.get('mode_counts'):
                raise CapacityError('下游 mode_counts 与逐题主模式记录不一致')
            report['mode_counts'] = dict(counts)
            report['mode_gate_version'] = modes.get('gate_version')
            report['mode_input'] = 'domain_fit.prompt_main_request + H1/H2/H3 prompt_excerpts'
            for mode, count in counts.items():
                if count > 3:
                    issues.append(f'主模式 {mode!r} 共 {count} 题，超过每模式上限 3')
        after = _snapshot(repo, map_path, scripts)
        if before != after:
            issues.append('检查期间 HEAD/工作树/map/下游脚本发生变化，结果作废，请重跑')
        identity = {k: before[k] for k in ('head', 'worktree', 'map_sha256',
            'downstream_sha256', 'checker_sha256')}
        identity.update(source_base_commit=base, repo=str(repo), map=str(map_path), downstream_skill=str(skill),
                        target=target, schema_version=1)
        report['fingerprint'] = {**identity, 'sha256': _digest(identity)}
        report['downstream_script_sha256'] = before['downstream_scripts']
        if not issues:
            report['qualified_plan_count'] = len(plans)
        if len(plans) < target:
            issues.append(f'合格计划数量不足目标：计划 {len(plans)}，qualified_plan_count={report["qualified_plan_count"]}，target={target}')
        report['ok'] = not issues and report['qualified_plan_count'] >= target
    except (CapacityError, OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
        issues.append(f'容量校验失败: {exc}')
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--map', dest='map_path')
    parser.add_argument('--downstream-skill', default=str(_default_downstream()))
    parser.add_argument('--target', type=int, default=35)
    parser.add_argument('--json-out', help='可选报告文件；必须在被检查仓库及下游 skill 之外')
    args = parser.parse_args(argv)
    result = check_capacity(args.repo, args.map_path, args.downstream_skill, args.target)
    if args.json_out:
        output = Path(args.json_out).expanduser().absolute()
        resolved = output.resolve()
        protected = [Path(args.repo).expanduser().resolve(), Path(args.downstream_skill).expanduser().resolve(),
                     Path(__file__).resolve().parents[1]]
        try:
            if any(resolved.is_relative_to(root) for root in protected):
                raise CapacityError('--json-out 不得写入被检查仓库/下游/检查器 skill，避免污染或覆盖输入')
            if output.is_symlink() or (output.exists() and not output.is_file()):
                raise CapacityError('--json-out 必须是普通文件且不能是符号链接')
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        except (OSError, CapacityError) as exc:
            result['ok'] = False
            result['issues'].append(f'写报告失败: {exc}')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
