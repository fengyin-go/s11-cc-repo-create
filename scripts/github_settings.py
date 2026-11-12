"""便携的 GitHub owner / 认证账号配置；凭证只从 gh 或进程环境读取。"""
from __future__ import annotations

import argparse
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SKILL_DIR = Path(__file__).resolve().parent.parent


def read_config(path: Path, *, required: bool = False) -> dict:
    if not path.is_file():
        if required:
            raise SystemExit(f'GitHub 配置不存在：{path}')
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SystemExit(f'GitHub 配置无法读取：{path}（{type(exc).__name__}）') from None
    if not isinstance(value, dict) or set(value) - {'owner', 'account'}:
        raise SystemExit(f'GitHub 配置只允许 owner / account，不得存放 token：{path}')
    return value


def resolve_config(owner: str | None = None, account: str | None = None) -> dict[str, str]:
    values = read_config(SKILL_DIR / 'config/github.json', required=True)
    values.update(read_config(SKILL_DIR / 'config/github.local.json'))
    if os.environ.get('S11_GITHUB_CONFIG'):
        values.update(read_config(Path(os.environ['S11_GITHUB_CONFIG']).expanduser(), required=True))
    selected_owner = owner or os.environ.get('S11_GITHUB_OWNER') or values.get('owner')
    selected_account = account or os.environ.get('S11_GITHUB_ACCOUNT') or values.get('account') or selected_owner
    for label, value in [('owner', selected_owner), ('account', selected_account)]:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', value):
            raise SystemExit(f'GitHub {label} 必须是有效账号或组织名，请检查配置')
    return {'owner': selected_owner, 'account': selected_account}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--github-account', help='认证账号；个人仓库默认等于 owner，组织可单独指定成员账号')
    parser.add_argument('--github-config', help='本机 JSON 配置路径（owner / account，不含凭证）')


def configure_from_args(args) -> dict[str, str]:
    if getattr(args, 'github_config', None):
        os.environ['S11_GITHUB_CONFIG'] = str(Path(args.github_config).expanduser().resolve())
    selected = resolve_config(getattr(args, 'owner', None), getattr(args, 'github_account', None))
    # 传给流水线的子进程，确保查号、建仓、推送都选同一账号；不切换 gh 全局账号。
    os.environ['S11_GITHUB_OWNER'] = selected['owner']
    os.environ['S11_GITHUB_ACCOUNT'] = selected['account']
    return selected


@lru_cache(maxsize=8)
def _authenticate(account: str, supplied_token: str) -> tuple[str, str]:
    gh = shutil.which('gh')
    if not gh:
        raise SystemExit('未安装 gh，请先安装 GitHub CLI 并登录配置的账号')
    clean_env = os.environ.copy()
    clean_env.pop('GH_TOKEN', None)
    clean_env.pop('GITHUB_TOKEN', None)
    clean_env['GH_HOST'] = 'github.com'
    clean_env['GH_PROMPT_DISABLED'] = '1'
    try:
        saved = subprocess.run([gh, 'auth', 'token', '--hostname', 'github.com', '--user', account],
                               env=clean_env, capture_output=True, text=True, timeout=30)
        token = saved.stdout.strip() if saved.returncode == 0 else supplied_token
        if not token:
            raise SystemExit(f'没有账号 {account} 的可用凭证。请运行 gh auth login --hostname github.com 后重试；不要把 token 写进 skill')
        auth_env = {**clean_env, 'GH_TOKEN': token}
        result = subprocess.run([gh, 'api', 'user', '--jq', '.login'], env=auth_env,
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise SystemExit(f'验证 GitHub 账号 {account} 超时，请检查网络后重试') from None
    if result.returncode != 0:
        raise SystemExit(f'GitHub 账号 {account} 的凭证验证失败，请重新登录或检查网络')
    login = result.stdout.strip()
    if login.lower() != account.lower():
        raise SystemExit(f'凭证所属账号是 {login}，配置要求 {account}，拒绝使用错误账号')
    return token, login


def auth_env(owner: str | None = None, account: str | None = None) -> dict[str, str]:
    selected = resolve_config(owner, account)
    token, _ = _authenticate(selected['account'], os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or '')
    env = os.environ.copy()
    env.pop('GITHUB_TOKEN', None)
    env.update(GH_TOKEN=token, GH_HOST='github.com', GH_PROMPT_DISABLED='1')
    return env


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='只读检查 s11 GitHub 配置与凭证，不创建仓库、不输出 token')
    parser.add_argument('--owner')
    add_arguments(parser)
    args = parser.parse_args()
    selected = configure_from_args(args)
    auth_env()
    print(json.dumps({'ok': True, **selected, 'credential_valid': True}, ensure_ascii=False, indent=2))
