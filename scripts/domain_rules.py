"""s11 数据工程领域约束，生成与验收共用。"""
from __future__ import annotations

import json
from pathlib import Path

DOMAIN = '数据工程'
SUBDIRECTIONS = ('数据采集、爬虫与RPA', '数仓与数据湖', '数据分析与数据应用')
METADATA_FILE = '.s11-domain.json'


def validate(spec: dict) -> dict[str, str]:
    if not isinstance(spec, dict):
        raise SystemExit('领域信息必须是 JSON 对象')
    if spec.get('domain') != DOMAIN:
        raise SystemExit('本技能只生成数据工程仓库：spec.domain 必须为「数据工程」')
    subdirection = spec.get('subdirection')
    if subdirection not in SUBDIRECTIONS:
        raise SystemExit('spec.subdirection 必须选择：' + '；'.join(SUBDIRECTIONS))
    return {'domain': DOMAIN, 'subdirection': subdirection}


def check_repo(repo: Path) -> dict:
    try:
        metadata = json.loads((repo / METADATA_FILE).read_text(encoding='utf-8'))
        values = validate(metadata)
    except (OSError, ValueError, AttributeError, SystemExit) as exc:
        return {'ok': False, 'problems': [f'{METADATA_FILE} 缺失或不合法：{exc}']}
    return {'ok': True, 'problems': [], **values}
