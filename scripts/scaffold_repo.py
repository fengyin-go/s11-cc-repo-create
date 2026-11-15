#!/usr/bin/env python3
"""生成一个满足硬性要求的代码仓库（全栈，或纯前端）。

硬性要求（全部由 scripts/verify_repo.py 机器复核，别手工判断）：

  1. 前端 Vue 或 React；后端 Java 或 Python（`--backend none` 时只出纯前端，
     数据走 `frontend/src/data` 下的本地数据层，不再要求后端目录）。
  2. 前端 dev server 关掉「自动打开页面」（vite 的 server.open = false）。
  3. 仓库根目录有 .gitignore，覆盖依赖、缓存、构建产物、日志、环境文件等。
  4. 源码总行数 >= 2000 行（不含依赖目录、构建产物、锁文件）。
  5. 不含禁用题型（代码理解），业务代码落在扫描模式覆盖的路径里。
  6. 主体数与需求模式数算出来的容量 >= 30（见 references/hard-rules.md）。
  7. **题域不能命中 0731 严禁出题清单**（命中即废题）。这条在生成之前就拦：
     spec 的应用名 / 描述 / 模块名 / 业务字段 / 状态 / 动作里出现禁用词就直接拒绝生成，
     没有绕过参数。命中即换题域，别改措辞绕。

用法：

    python3 scaffold_repo.py --out /path/to/repo --spec spec.json
    python3 scaffold_repo.py --out /path/to/repo --frontend react --backend java
    python3 scaffold_repo.py --out /path/to/repo --backend none         # 纯前端仓库
    python3 scaffold_repo.py --out /path/to/repo --git-init      # 顺手 git init + 首次提交
    python3 scaffold_repo.py --check-spec --spec spec.json       # 只自检题域，不生成任何目录

生成完不会自动装依赖；装依赖、起服务、验收统一走 SKILL.md 的「验收」一节。
"""

from __future__ import annotations

import argparse
import copy
import json
import keyword
import pprint
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import repo_rules as rules  # noqa: E402
import github_settings
import domain_rules

DEFAULT_SPEC_NAME = "spec-data-engineering.json"

# 项目编号：<前缀 2~4 个字母><5 位数>，5 位数首位固定是 1（10001–19999），例如 cc10001。
PROJECT_ID_RE = re.compile(r"^([A-Za-z]{2,4})(1\d{4})$")
DEFAULT_PREFIX = "cc"
NUMBER_FLOOR = 10001
NUMBER_CEILING = 19999
# 最多往后找多少个编号：本地 + GitHub 都占用时才会继续往后，正常不会用到这么多。
MAX_NUMBER_PROBES = 200

# 模块 key 会变成 frontend/src/views/<key>/ 的目录名。下面这些名字在生成的 .gitignore 里
# 是依赖 / 构建产物规则（`dist/`、`target/`、`**/target/`…），撞名会让整个模块的页面被 git
# 忽略：本地看着文件都在，干净克隆里少一个页面，前端构建直接报 TS2307。真踩过一次
# （模块 key = target，被 `**/target/` 吃掉），所以在 spec 阶段就禁止，不留到探针才发现。
RESERVED_MODULE_KEYS = frozenset({
    "node_modules", "dist", "build", "out", "target", "coverage", "logs",
    "classes", "eggs", "env", "venv", "__pycache__",
    # 系统级 API 已占用 /api/health 与 /api/overview，业务模块不能覆盖它们。
    "health", "overview",
})

APP_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MODULE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_prefix(prefix: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{2,4}", prefix or ""):
        raise SystemExit(f"前缀必须是 2~4 个字母（例如 cc / YEL），收到 {prefix!r}")
    return prefix


def split_project_id(name: str) -> tuple[str, int] | None:
    match = PROJECT_ID_RE.match(name)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def local_numbers(directory: Path) -> set[int]:
    """扫这个目录里已经存在的编号（不区分前缀，编号在同一序列里往后走）。"""
    numbers: set[int] = set()
    if not directory.is_dir():
        return numbers
    for child in sorted(directory.iterdir()):
        parsed = split_project_id(child.name)
        if parsed:
            numbers.add(parsed[1])
    return numbers


def gh_binary() -> str | None:
    return shutil.which("gh")


def resolve_owner(explicit: str | None) -> str:
    return github_settings.resolve_config(explicit)['owner']


def github_repo_exists(owner: str, name: str) -> bool | None:
    """True/False 表示 GitHub 上的查询结果；None 表示查不了（没装 gh 或没登录）。"""
    gh = gh_binary()
    if not gh or not owner:
        return None
    result = subprocess.run([gh, "repo", "view", f"{owner}/{name}"],
                            capture_output=True, text=True, env=github_settings.auth_env(owner))
    if result.returncode and "404" not in result.stderr and "Could not resolve to a Repository" not in result.stderr:
        return None
    return result.returncode == 0


def pick_project_id(
    parent: Path,
    prefix: str,
    *,
    explicit_number: int | None,
    owner: str | None,
    check_github: bool,
) -> dict[str, object]:
    """定下这个项目的编号：本地已有的往后排，GitHub 上已存在的也往后排。"""
    prefix = validate_prefix(prefix)
    if explicit_number is not None:
        if not (NUMBER_FLOOR <= int(explicit_number) <= NUMBER_CEILING):
            raise SystemExit(
                f"--number 必须是 {NUMBER_FLOOR}~{NUMBER_CEILING} 之间的 5 位数，首位是 1")
        name = f"{prefix}{int(explicit_number)}"
        if (parent / name).exists():
            raise SystemExit(f"编号 {name} 的目录已经存在：{parent / name}")
        if check_github and github_repo_exists(owner, name):
            raise SystemExit(f"GitHub 上已经有 {owner}/{name}，换一个 --number 或去掉它让脚本自动往后排")
        return {
            "project_id": name, "prefix": prefix, "number": int(explicit_number),
            "picked_from": "explicit", "github_checked": bool(check_github and owner),
            "skipped": [],
        }

    known = local_numbers(parent)
    start = max(known) + 1 if known else NUMBER_FLOOR
    skipped: list[dict[str, str]] = []
    github_checked = {"value": False}
    for number in range(start, start + MAX_NUMBER_PROBES):
        if number > NUMBER_CEILING:
            raise SystemExit(
                f"编号已经超过 {NUMBER_CEILING}（5 位数首位是 1 的上限），"
                "换一个前缀或手工清理一批编号再建")
        name = f"{prefix}{number}"
        if (parent / name).exists():
            skipped.append({"project_id": name, "reason": "本地目录已存在"})
            continue
        exists = github_repo_exists(owner, name) if check_github else None
        if exists is not None:
            github_checked["value"] = True
        if exists:
            skipped.append({"project_id": name, "reason": "GitHub 上已存在"})
            continue
        return {
            "project_id": name, "prefix": prefix, "number": number,
            "picked_from": f"本地最大编号 {max(known)} 往后排" if known else "从 10001 起",
            "github_checked": github_checked["value"], "skipped": skipped,
        }
    raise SystemExit(f"往后找了 {MAX_NUMBER_PROBES} 个编号都被占用，先人工清理再建")


# ---------------------------------------------------------------------------
# spec 读取
# ---------------------------------------------------------------------------
SPEC_TOPIC_SCALARS = ("app_name", "app_slug", "app_desc")
SPEC_TOPIC_LIST_FIELDS = ("fields", "statuses", "actions", "action_targets", "metrics")
SPEC_TOPIC_MODULE_SCALARS = ("key", "name", "entity", "desc")


def spec_topic_corpus(spec: dict) -> dict[str, str]:
    """把 spec 摊成「字段路径 -> 文本」，供严禁题域扫描逐字段定位命中点。

    覆盖应用级三个字段 + 每个模块的标识、名称、业务对象、说明，以及
    fields / statuses / actions / action_targets / metrics 五组列表。
    """
    corpus: dict[str, str] = {}
    for field in SPEC_TOPIC_SCALARS:
        corpus[field] = str(spec.get(field) or "")
    modules = spec.get("modules") if isinstance(spec.get("modules"), list) else []
    for index, module in enumerate(modules, start=1):
        if not isinstance(module, dict):
            corpus[f"modules[{index}]"] = str(module)
            continue
        key = str(module.get("key") or f"module{index}")
        for field in SPEC_TOPIC_MODULE_SCALARS:
            corpus[f"modules[{key}].{field}"] = str(module.get(field) or "")
        for field in SPEC_TOPIC_LIST_FIELDS:
            values = module.get(field) if isinstance(module.get(field), list) else []
            for item in values:
                corpus[f"modules[{key}].{field}[]"] = (
                    corpus.get(f"modules[{key}].{field}[]", "") + "\n" + str(item))
    return corpus


def scan_spec_topics(spec: dict) -> list[dict[str, str]]:
    """扫描 spec 是否命中严禁出题题域，返回 [{category, keyword, label, field}]。"""
    hits: list[dict[str, str]] = []
    for field, text in spec_topic_corpus(spec).items():
        for category, keyword in rules.scan_banned_topics(text):
            hits.append({
                "category": category,
                "keyword": keyword,
                "label": rules.describe_banned_topic(category, keyword),
                "field": field,
            })
    return hits


def banned_topic_report(hits: list[dict[str, str]], spec_path: object = None) -> str:
    """把命中项渲染成给人看的报错文案。"""
    by_label: dict[str, set[str]] = {}
    for hit in hits:
        by_label.setdefault(hit["label"], set()).add(hit["field"])
    lines = [
        "题域命中 0731 严禁出题清单（命中即废题），拒绝生成。",
        f"依据：{rules.BANNED_TOPIC_SOURCE}（抄写日期 {rules.BANNED_TOPIC_SNAPSHOT_DATE}）",
    ]
    if spec_path is not None:
        lines.append(f"spec：{spec_path}")
    lines.append("命中项：")
    for label in sorted(by_label):
        fields = "、".join(sorted(by_label[label])[:6])
        lines.append(f"  - {label}  出现在 {fields}")
    lines += [
        "",
        "这不是措辞问题，是题域问题：换个方向重写 spec，不要靠改字眼绕过去。",
        "可用方向与禁用清单见 references/spec-format.md「严禁题域：这些方向不能选」一节。",
    ]
    return "\n".join(lines)


def _string_list(value: object, *, label: str, minimum: int = 1,
                 allow_duplicates: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise SystemExit(f"{label} 必须是至少含 {minimum} 项的字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise SystemExit(f"{label} 只能包含非空字符串")
    result = [item.strip() for item in value]
    if not allow_duplicates and len(set(result)) != len(result):
        raise SystemExit(f"{label} 不能有重复项")
    return result


def normalize_spec(raw_spec: dict, frontend: str | None = None, backend: str | None = None) -> dict:
    """在落盘前完成全部结构校验和默认值归一化。

    这个函数同时服务 --check-spec、直接 scaffold 调用和流水线预检，避免文档说可选、
    生成器却在写到一半时才 KeyError 的分叉行为。
    """
    if not isinstance(raw_spec, dict):
        raise SystemExit("spec 顶层必须是 JSON 对象")
    spec = copy.deepcopy(raw_spec)
    domain_rules.validate(spec)
    if frontend:
        spec["frontend"] = frontend
    if backend:
        spec["backend"] = backend
    spec.setdefault("frontend", "vue")
    spec.setdefault("backend", "python")
    spec.setdefault("port_frontend", 5173)
    spec.setdefault("port_backend", 8000)
    for field in ("app_name", "app_slug", "app_desc"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise SystemExit(f"spec.{field} 必须是非空字符串")
        spec[field] = spec[field].strip()
    if not APP_SLUG_RE.fullmatch(spec["app_slug"]):
        raise SystemExit("spec.app_slug 只能用小写字母、数字和单个连字符，且必须以字母开头")
    if spec["frontend"] not in {"vue", "react"}:
        raise SystemExit(f"前端只支持 vue / react，收到 {spec['frontend']!r}")
    if spec["backend"] not in {"python", "java", "none"}:
        raise SystemExit(
            f"后端只支持 python / java / none（none = 纯前端仓库），收到 {spec['backend']!r}")
    for field in ("port_frontend", "port_backend"):
        value = spec[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
            raise SystemExit(f"spec.{field} 必须是 1024~65535 的整数端口")
    if spec["port_frontend"] == spec["port_backend"] and spec["backend"] != "none":
        raise SystemExit("全栈仓库的前后端端口不能相同")

    modules = spec.get("modules")
    if not isinstance(modules, list) or len(modules) < 16:
        raise SystemExit("spec.modules 必须至少包含 16 个模块，才能满足容量下限")
    seen: set[str] = set()
    seen_classes: set[str] = set()
    normalized_modules = []
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            raise SystemExit(f"modules[{index}] 必须是 JSON 对象")
        module = copy.deepcopy(module)
        for field in ("key", "name", "entity"):
            if not isinstance(module.get(field), str) or not module[field].strip():
                raise SystemExit(f"modules[{index}] 缺少非空字符串字段 {field}")
            module[field] = module[field].strip()
        key = module["key"]
        if key in RESERVED_MODULE_KEYS:
            raise SystemExit(f"模块 key 不能叫 {key!r}：会撞依赖目录或系统级 /api/{key} 端点")
        if not MODULE_KEY_RE.fullmatch(key) or keyword.iskeyword(key):
            raise SystemExit(
                f"模块 key {key!r} 不合法：只支持小写字母/数字/下划线、以字母开头，且不能是 Python 关键字")
        if key in seen:
            raise SystemExit(f"模块 key 重复：{key}")
        generated_class = class_name(key)
        if generated_class in seen_classes:
            raise SystemExit(f"模块 key {key!r} 与其他模块生成相同的类名 {generated_class}，请调整命名")
        seen.add(key)
        seen_classes.add(generated_class)
        module["fields"] = _string_list(module.get("fields"), label=f"模块 {key}.fields", minimum=3)
        module["statuses"] = _string_list(module.get("statuses"), label=f"模块 {key}.statuses", minimum=2)
        module["actions"] = _string_list(module.get("actions"), label=f"模块 {key}.actions", minimum=1)
        targets = module.get("action_targets")
        if targets is None:
            targets = [module["statuses"][min(i + 1, len(module["statuses"]) - 1)]
                       for i in range(len(module["actions"]))]
        module["action_targets"] = _string_list(
            targets, label=f"模块 {key}.action_targets", minimum=len(module["actions"]),
            allow_duplicates=True)
        if len(module["action_targets"]) != len(module["actions"]):
            raise SystemExit(f"模块 {key} 的 actions 与 action_targets 数量必须一致")
        missing_targets = sorted(set(module["action_targets"]) - set(module["statuses"]))
        if missing_targets:
            raise SystemExit(f"模块 {key} 的 action_targets 引用了未定义状态：{'、'.join(missing_targets)}")
        if module.get("metrics") is not None:
            module["metrics"] = _string_list(module["metrics"], label=f"模块 {key}.metrics", minimum=3)
            if len(module["metrics"]) != 3:
                raise SystemExit(f"模块 {key}.metrics 必须正好有 3 项，对应页面三张指标卡")
        if module.get("desc") is not None and not isinstance(module["desc"], str):
            raise SystemExit(f"模块 {key}.desc 必须是字符串")
        page_shape = module.get("page_shape", "crud-table")
        if page_shape not in ("crud-table", "dag-graph", "job-timeline", "analysis-dashboard"):
            raise SystemExit(
                f"模块 {key}.page_shape 必须是 crud-table、dag-graph、job-timeline、analysis-dashboard 之一，"
                f"不支持 {page_shape!r}")
        if "page_shape" in module:
            module["_page_shape_explicit"] = True
        module["page_shape"] = page_shape
        normalized_modules.append(module)
    # 只有当 spec 显式声明了 page_shape 时才要求多样性；兼容旧 spec
    explicit_shapes = [m for m in normalized_modules if m.get("_page_shape_explicit")]
    if explicit_shapes:
        non_crud_count = sum(1 for m in normalized_modules if m["page_shape"] != "crud-table")
        if non_crud_count < 2:
            raise SystemExit(
                f"至少 2 个模块需要非 crud-table 的 page_shape，避免全仓库页面同质化。"
                f"当前只有 {non_crud_count} 个")
    spec["modules"] = normalized_modules
    return spec


def load_spec(path: Path | None, frontend: str | None, backend: str | None) -> dict:
    if path is None:
        path = SKILL_DIR / "examples" / DEFAULT_SPEC_NAME
    path = Path(path)
    try:
        raw_spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"spec 无法读取或不是合法 JSON：{path}（{exc}）") from None
    # 题域命中优先报告，避免被无关格式错误遮住真正的判废原因。
    topic_hits = scan_spec_topics(raw_spec) if isinstance(raw_spec, dict) else []
    if topic_hits:
        raise SystemExit(banned_topic_report(topic_hits, spec_path=path))
    return normalize_spec(raw_spec, frontend, backend)


def render(template: str, **values: object) -> str:
    text = template
    for key, value in values.items():
        text = text.replace(f"__{key}__", str(value))
    return text


def class_name(key: str) -> str:
    return "".join(part.capitalize() for part in key.replace("-", "_").split("_") if part)


def json_array(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in items) + "]"


def ts_array(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in items) + "]"


def py_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in items) + "]"


def py_literal(value: object, *, indent: int | None = None) -> str:
    """把 Python 对象渲染成 Python 字面量。

    json.dumps 会把布尔值写成 true/false，直接落进 .py 文件就是 NameError；
    pprint 输出的是合法 Python 字面量，且中文不转义，所以用它。
    """
    width = 100 if indent is not None else 10_000
    return pprint.pformat(value, width=width, sort_dicts=False)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")


def line_count(text: str) -> int:
    return len(text.rstrip("\n").splitlines())


# ---------------------------------------------------------------------------
# gitignore / README / compose —— 三个技术栈共用
# ---------------------------------------------------------------------------
GITIGNORE = """\
# ===== 依赖目录 =====
node_modules/
**/node_modules/
.pnp
.pnp.js
.yarn/cache
.pnpm-store/
.venv/
venv/
env/
__pycache__/
*.py[cod]
*.egg-info/
.eggs/
dist/
eggs/
pip-wheel-metadata/

# ===== 构建产物 =====
dist/
build/
out/
target/
**/target/
.gradle/
classes/
*.class
*.jar
*.war
*.local

# ===== 包管理器 / 构建缓存 =====
.npm/
.cache/
.parcel-cache/
.vite/
.turbo/
.sass-cache/
*.tsbuildinfo
.eslintcache
.stylelintcache
.mypy_cache/
.ruff_cache/
.pytest_cache/
.tox/
.nox/

# ===== 日志 =====
logs/
*.log
npm-debug.log*
yarn-debug.log*
yarn-error.log*
pnpm-debug.log*

# ===== 环境变量与本地配置 =====
.env
.env.local
.env.*.local
*.env
!.env.example

# ===== 测试覆盖率 =====
coverage/
test-results/
playwright-report/
.nyc_output/
htmlcov/
*.lcov

# ===== IDE / 编辑器 =====
.idea/
.vscode/
*.iml
*.swp
*.swo
*~

# ===== 操作系统 =====
.DS_Store
.AppleDouble
.LSOverride
Thumbs.db
ehthumbs.db

# ===== 技能元数据（本地生成物，不入仓库） =====
.s11-domain.json
.s11-spec.json
Thumbs.db
desktop.ini

# ===== 运行期本地文件 =====
*.sqlite3
*.db
*.pid
*.seed
"""

README = """\
# __APP_NAME__

__APP_DESC__

这是一个前后端分离的管理平台：前端 __FRONTEND_LABEL__，后端 __BACKEND_LABEL__。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 __FRONTEND_LABEL__ 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  __BACKEND_LABEL__ 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
__BACKEND_SETUP__
__BACKEND_RUN__
```

健康检查：`curl http://127.0.0.1:__BACKEND_PORT__/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:__FRONTEND_PORT__/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:__BACKEND_PORT__`。

## 业务模块

__MODULE_TABLE__

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
"""

COMPOSE = """\
services:
  backend:
    build: ./backend
    ports:
      - "__BACKEND_PORT__:__BACKEND_PORT__"
    environment:
      - APP_ENV=local
  frontend:
    build: ./frontend
    ports:
      - "__FRONTEND_PORT__:__FRONTEND_PORT__"
    environment:
      # 前端容器里的 127.0.0.1 是它自己，必须走 Compose 服务名访问后端。
      VITE_PROXY_TARGET: http://backend:__BACKEND_PORT__
    depends_on:
      - backend
"""

MAKEFILE = """\
.PHONY: install backend frontend

install:
\tcd backend && __BACKEND_INSTALL__
\tcd frontend && npm ci

backend:
\tcd backend && __BACKEND_RUN__

frontend:
\tcd frontend && npm run dev
"""


# ---------------------------------------------------------------------------
# 前端：Vue
# ---------------------------------------------------------------------------
VITE_CONFIG_VUE = """\
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址默认取本文件里写的端口，起服务时可以用 VITE_PROXY_TARGET 覆盖，
// 这样换端口调试或做启动探针时前端不用改代码。
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:__BACKEND_PORT__'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: __FRONTEND_PORT__,
    // 关掉自动打开页面：起服务时只打印地址，不拉起浏览器
    open: false,
    strictPort: false,
    proxy: {
      '/api': {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
"""

VITE_CONFIG_REACT = """\
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端地址默认取本文件里写的端口，起服务时可以用 VITE_PROXY_TARGET 覆盖，
// 这样换端口调试或做启动探针时前端不用改代码。
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:__BACKEND_PORT__'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: __FRONTEND_PORT__,
    // 关掉自动打开页面：起服务时只打印地址，不拉起浏览器
    open: false,
    strictPort: false,
    proxy: {
      '/api': {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
"""

PACKAGE_JSON_VUE = """\
{
  "name": "__APP_SLUG__-frontend",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "repoKind": "__REPO_KIND__",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc --noEmit && vite build",
    "preview": "vite preview",
    "typecheck": "vue-tsc --noEmit",
    "test:e2e": "playwright test"
  },
  "dependencies": {
    "pinia": "^2.1.7",
    "vue": "^3.4.21",
    "vue-router": "^4.3.0"
  },
  "devDependencies": {
    "@types/node": "^20.11.30",
    "@playwright/test": "^1.56.1",
    "@vitejs/plugin-vue": "^5.0.4",
    "typescript": "^5.4.3",
    "vite": "^5.2.6",
    "vue-tsc": "^2.0.7"
  }
}
"""

PACKAGE_JSON_REACT = """\
{
  "name": "__APP_SLUG__-frontend",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "repoKind": "__REPO_KIND__",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "preview": "vite preview",
    "typecheck": "tsc --noEmit",
    "test:e2e": "playwright test"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.23.1"
  },
  "devDependencies": {
    "@types/node": "^20.11.30",
    "@playwright/test": "^1.56.1",
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "typescript": "^5.4.3",
    "vite": "^5.2.6"
  }
}
"""

PLAYWRIGHT_CONFIG_TS = """\
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 300_000,
  expect: { timeout: 10_000 },
  workers: 1,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173',
    browserName: 'chromium',
    headless: true,
    actionTimeout: 10_000,
  },
  reporter: 'line',
})
"""

FUNCTIONALITY_TEST_TS = """\
import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

type ModuleCase = {
  key: string
  fields: string[]
  actions: string[]
  actionTargets: string[]
  shape: string
}

const modules: ModuleCase[] = __MODULES__
const repoKind = '__REPO_KIND__'
const crudModules = modules.filter(m => m.shape === 'crud-table')
const dagModules = modules.filter(m => m.shape === 'dag-graph')
const timelineModules = modules.filter(m => m.shape === 'job-timeline')
const analysisModules = modules.filter(m => m.shape === 'analysis-dashboard')

function parseCsv(text: string): string[][] {
  const records: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false
  const input = text.replace(/^\\uFEFF/, '')
  for (let index = 0; index < input.length; index++) {
    const char = input[index]
    if (quoted) {
      if (char === '"' && input[index + 1] === '"') { cell += '"'; index++ }
      else if (char === '"') { quoted = false }
      else { cell += char }
    } else if (char === '"' && cell === '') { quoted = true }
    else if (char === ',') { row.push(cell); cell = '' }
    else if (char === '\\n') {
      row.push(cell.replace(/\\r$/, ''))
      if (row.some(v => v !== '')) records.push(row)
      row = []; cell = ''
    } else { cell += char }
  }
  row.push(cell.replace(/\\r$/, ''))
  if (row.some(v => v !== '')) records.push(row)
  return records
}

test('CRUD模块：筛选、导出、登记、动作、指标全链路', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (crudModules.length === 0) { test.skip(); return }

  for (const mod of crudModules) {
    await page.goto(`/${mod.key}`)
    await page.waitForTimeout(3000)
    const bodyRows = page.locator('tbody tr[data-testid^="row-"]')
    await expect(bodyRows).toHaveCount(3)
    const firstValue = await bodyRows.nth(0).locator('td').nth(0).innerText()

    await page.getByTestId(`filter-${mod.fields[0]}`).fill(firstValue)
    await page.getByTestId('query').click()
    await expect(bodyRows).toHaveCount(1)
    await page.getByTestId('reset').click()
    await expect(bodyRows).toHaveCount(3)

    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('export').click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`${mod.key}.csv`)

    const createdValues = mod.fields.map((f, i) => `验收-${mod.key}-${i+1}`)
    await page.getByTestId('open-create').click()
    await expect(page.getByTestId('create-modal')).toBeVisible()
    for (const [i, field] of mod.fields.entries()) {
      await page.getByTestId(`create-${field}`).fill(createdValues[i])
    }
    await page.getByTestId('submit-create').click()
    await expect(page.getByTestId('create-modal')).toHaveCount(0)
    await expect(bodyRows).toHaveCount(4)

    const createdRow = page.locator('tbody tr').filter({ hasText: createdValues[0] })
    for (const [i, action] of mod.actions.entries()) {
      await createdRow.getByRole('button', { name: action, exact: true }).click()
      await expect(createdRow).toHaveAttribute('data-status', mod.actionTargets[i])
    }
  }

  expect(browserErrors).toEqual([])
})

test('DAG拓扑模块：节点渲染、详情、动作、导出', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (dagModules.length === 0) { test.skip(); return }

  for (const mod of dagModules) {
    await page.goto(`/${mod.key}`)
    await page.waitForTimeout(3000)
    await expect(page.locator('svg.dag-svg')).toBeVisible()
    const nodes = page.locator('g.dag-node')
    const count = await nodes.count()
    expect(count).toBeGreaterThanOrEqual(3)

    await nodes.first().click()
    await expect(page.getByTestId('dag-detail')).toBeVisible()

    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('export').click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`${mod.key}.csv`)

    await page.getByTestId('open-create').click()
    await expect(page.getByTestId('create-modal')).toBeVisible()
    for (const [i, field] of mod.fields.slice(0,3).entries()) {
      await page.locator('.create-field input').nth(i).fill(`E2E-${mod.key}-${i}`)
    }
    await page.getByTestId('submit-create').click()
    await expect(page.getByTestId('create-modal')).toHaveCount(0)
  }

  expect(browserErrors).toEqual([])
})

test('时间线模块：甘特条渲染、日期筛选、日志查看、登记', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (timelineModules.length === 0) { test.skip(); return }

  for (const mod of timelineModules) {
    await page.goto(`/${mod.key}`)
    await expect(page.locator('.timeline-bar').first()).toBeVisible({ timeout: 10000 })
    const bars = page.locator('.timeline-bar')
    const count = await bars.count()
    expect(count).toBeGreaterThanOrEqual(3)

    await bars.first().click()
    await expect(page.getByTestId('timeline-log')).toBeVisible()

    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('export').click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`${mod.key}.csv`)

    await page.getByTestId('open-create').click()
    await expect(page.getByTestId('create-modal')).toBeVisible()
    for (const [i, field] of mod.fields.slice(0,3).entries()) {
      await page.locator('.create-field input').nth(i).fill(`E2E-${mod.key}-${i}`)
    }
    await page.getByTestId('submit-create').click()
    await expect(page.getByTestId('create-modal')).toHaveCount(0)
  }

  expect(browserErrors).toEqual([])
})

test('分析看板模块：KPI卡片、运行分析、图表渲染', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (analysisModules.length === 0) { test.skip(); return }

  for (const mod of analysisModules) {
    await page.goto(`/${mod.key}`)
    await expect(page.getByTestId('kpi-card').first()).toBeVisible({ timeout: 10000 })
    const kpiCards = page.getByTestId('kpi-card')
    const kpiCount = await kpiCards.count()
    expect(kpiCount).toBeGreaterThanOrEqual(2)
    await expect(kpiCards.first().locator('strong')).toBeVisible()

    await page.getByTestId('run-analysis').click()
    await expect(page.locator('.analysis-bar').first()).toBeVisible({ timeout: 5000 })
    await expect(page.locator('.data-table tbody tr').first()).toBeVisible({ timeout: 5000 })
  }

  expect(browserErrors).toEqual([])
})

test('概览汇总', async ({ page }) => {
  test.setTimeout(300_000)
  await page.goto('/')
  await expect(page.locator('.stat-value').nth(1)).toBeVisible()
  // 验证概览页加载正常即可，不硬编码精确计数（取决于各模块 E2E 登记成功数）
})
"""

INDEX_HTML = """\
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>__APP_NAME__</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.__MAIN_EXT__"></script>
  </body>
</html>
"""

TSCONFIG_VUE = """\
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "jsx": "preserve",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "esModuleInterop": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "skipLibCheck": true,
    "noEmit": true,
    "baseUrl": ".",
    "paths": { "@/*": ["src/*"] }
  },
  "include": ["src/**/*.ts", "src/**/*.d.ts", "src/**/*.vue", "vite.config.ts"]
}
"""

TSCONFIG_REACT = """\
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "jsx": "react-jsx",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "esModuleInterop": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "skipLibCheck": true,
    "noEmit": true,
    "baseUrl": ".",
    "paths": { "@/*": ["src/*"] }
  },
  "include": ["src/**/*.ts", "src/**/*.tsx", "vite.config.ts"]
}
"""

MAIN_VUE = """\
import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import './styles/global.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
"""

ENV_D_TS = """\
/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  readonly VITE_APP_NAME?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
"""

MAIN_REACT = """\
import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import './styles/global.css'

ReactDOM.createRoot(document.getElementById('app') as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
"""

APP_VUE = """\
<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">__APP_NAME__</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">__APP_DESC__</span>
        <span class="head-user">当前值班：{{ store.operator }} · {{ store.shiftLabel }}</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()

const navItems = __NAV_ITEMS__
</script>
"""

APP_REACT = """\
import { useState } from 'react'
import { Link, Route, Routes } from 'react-router-dom'

import Overview from './views/Overview'
__REACT_VIEW_IMPORTS__

const navItems = __NAV_ITEMS__

export default function App() {
  const [operator] = useState('值班管理员')

  return (
    <div className="app-shell">
      <aside className="app-side">
        <h1 className="app-title">__APP_NAME__</h1>
        <nav className="nav-list">
          {navItems.map((item) => (
            <Link key={item.path} to={item.path} className="nav-item">
              {item.label}
            </Link>
          ))}
        </nav>
      </aside>
      <main className="app-main">
        <header className="app-head">
          <span className="head-desc">__APP_DESC__</span>
          <span className="head-user">当前值班：{operator}</span>
        </header>
        <Routes>
          <Route path="/" element={<Overview />} />
__REACT_ROUTES__
        </Routes>
      </main>
    </div>
  )
}
"""

ROUTER_TS = """\
import { createRouter, createWebHistory } from 'vue-router'

__VIEW_IMPORTS__

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
__ROUTES__
  ],
})

export default router
"""

CLIENT_TS = """\
/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}
"""

SESSION_STORE = """\
import { defineStore } from 'pinia'

export const useSessionStore = defineStore('session', {
  state: () => ({
    operator: '值班管理员',
    shiftLabel: '白班 08:00-20:00',
    scope: '__APP_SCOPE__',
  }),
  getters: {
    canOperate: (state) => state.operator.length > 0,
  },
  actions: {
    setShift(label: string) {
      this.shiftLabel = label
    },
  },
})
"""

GLOBAL_CSS = """\
:root {
  color-scheme: light;
  font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif;
  --brand: #1f6feb;
  --border: #d8dee6;
  --muted: #64748b;
}
* { box-sizing: border-box; }
body { margin: 0; background: #f6f8fb; color: #1f2937; }
.app-shell { display: flex; min-height: 100vh; }
.app-side { width: 220px; background: #101828; color: #e5e7eb; padding: 16px 12px; }
.app-title { font-size: 16px; margin: 0 0 16px; }
.nav-list { display: flex; flex-direction: column; gap: 4px; }
.nav-item { color: #cbd5e1; text-decoration: none; padding: 6px 8px; border-radius: 6px; font-size: 13px; }
.nav-item.router-link-active { background: #1f6feb; color: #fff; }
.app-main { flex: 1; padding: 16px 20px; }
.app-head { display: flex; justify-content: space-between; color: var(--muted); font-size: 13px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin: 12px 0; }
.page-desc { color: var(--muted); font-size: 13px; margin: 4px 0 0; }
.stat-row { display: flex; gap: 12px; margin-bottom: 12px; }
.stat-card { flex: 1; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.stat-label { display: block; color: var(--muted); font-size: 12px; }
.stat-value { font-size: 20px; }
.filter-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 12px; }
.filter-item span { display: block; font-size: 12px; color: var(--muted); }
.data-table { width: 100%; border-collapse: collapse; background: #fff; }
.data-table th, .data-table td { border: 1px solid var(--border); padding: 8px 10px; font-size: 13px; text-align: left; }
.empty-state { text-align: center; color: var(--muted); }
.row-actions { display: flex; gap: 6px; }
.btn { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 12px; cursor: pointer; }
.btn.primary { background: var(--brand); border-color: var(--brand); color: #fff; }
.btn.ghost { background: transparent; }
.link { border: none; background: none; color: var(--brand); cursor: pointer; padding: 0; }
.page-foot { display: flex; justify-content: space-between; margin-top: 8px; font-size: 12px; color: var(--muted); }
.error-text { color: #b42318; }
.modal-backdrop { position: fixed; inset: 0; background: rgb(15 23 42 / 35%); display: grid; place-items: center; padding: 20px; z-index: 20; }
.modal { width: min(560px, 100%); max-height: 90vh; overflow: auto; background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 20px 50px rgb(15 23 42 / 20%); }
.create-field { display: block; margin-bottom: 10px; }
.create-field span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }
.create-field input { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 6px; padding: 8px; }
/* DAG 拓扑图 */
.dag-wrap { position: relative; overflow: hidden; border: 1px solid var(--border); border-radius: 8px; background: #fff; min-height: 320px; user-select: none; }
.dag-svg { width: 100%; height: 100%; }
.dag-node { cursor: grab; transition: stroke 0.15s; }
.dag-node text { font-size: 12px; fill: #1f2937; }
.dag-node.selected rect { stroke: var(--brand); stroke-width: 2; }
.dag-edge { stroke: #94a3b8; stroke-width: 1.5; fill: none; marker-end: url(#arrowhead); }
.dag-detail { border-top: 1px solid var(--border); padding: 12px; background: #fafbfc; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.dag-detail dt { font-size: 12px; color: var(--muted); }
.dag-detail dd { font-size: 13px; margin: 0 0 8px 0; }
/* 时间线 */
.timeline-wrap { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; overflow-x: auto; }
.timeline-row { display: flex; align-items: center; margin-bottom: 8px; padding: 4px 0; border-bottom: 1px solid #f0f2f5; }
.timeline-label { width: 140px; font-size: 13px; flex-shrink: 0; }
.timeline-track { flex: 1; position: relative; height: 28px; background: #f0f2f5; border-radius: 4px; overflow: hidden; }
.timeline-bar { position: absolute; top: 4px; height: 20px; border-radius: 3px; font-size: 11px; line-height: 20px; padding: 0 6px; color: #fff; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.timeline-bar.status-待执行 { background: #94a3b8; }
.timeline-bar.status-运行中 { background: #3b82f6; }
.timeline-bar.status-已完成 { background: #22c55e; }
.timeline-bar.status-失败 { background: #ef4444; }
.timeline-log { padding: 10px 14px; background: #1e293b; color: #e2e8f0; border-radius: 6px; font-family: monospace; font-size: 12px; max-height: 180px; overflow-y: auto; margin-top: 8px; }
/* 分析看板 */
.analysis-summary { display: flex; gap: 12px; flex-wrap: wrap; }
.analysis-card { flex: 1; min-width: 140px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.analysis-card .trend-up { color: #22c55e; }
.analysis-card .trend-down { color: #ef4444; }
.analysis-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 12px 0; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.analysis-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.analysis-form input, .analysis-form select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.analysis-chart { display: flex; align-items: flex-end; gap: 8px; height: 160px; padding: 12px 0; }
.analysis-bar { flex: 1; border-radius: 4px 4px 0 0; background: var(--brand); min-width: 24px; position: relative; transition: height 0.4s; }
.analysis-bar span { position: absolute; bottom: -20px; left: 50%; transform: translateX(-50%); font-size: 10px; color: var(--muted); white-space: nowrap; }
"""

VUE_VIEW = """\
<template>
  <section class="page" data-module="__KEY__">
    <header class="page-head">
      <div>
        <h2>__NAME__管理</h2>
        <p class="page-desc">__DESC__</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记__ENTITY__</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出__NAME__清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无__NAME__数据，可先登记__ENTITY__</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记__ENTITY__</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条__NAME__记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/__KEY__'
const columns = __FIELDS__
const actions = __ACTIONS__
const statuses = __STATUSES__
const metricNames = __METRICS_NAMES__

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = '__KEY__.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('__NAME__动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '__NAME__操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('__ENTITY__列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '__NAME__列表读取失败'
  }
}

onMounted(reload)
</script>
"""

VUE_DAG_GRAPH = """\
<template>
  <section class="page" data-module="__KEY__">
    <header class="page-head">
      <div><h2>__NAME__</h2><p class="page-desc">__DESC__</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记__ENTITY__</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出关系清单</button>
      </div>
    </header>
    <div class="dag-wrap" ref="dagRef">
      <svg class="dag-svg" :viewBox="`0 0 ${svgW} ${svgH}`">
        <defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" fill="#94a3b8"/></marker></defs>
        <g v-for="edge in graph.edges" :key="edge.from+edge.to">
          <line class="dag-edge" :x1="nodePos[edge.from]?.x||0" :y1="nodePos[edge.from]?.y||0" :x2="nodePos[edge.to]?.x||0" :y2="nodePos[edge.to]?.y||0"/>
          <text :x="(nodePos[edge.from]?.x+nodePos[edge.to]?.x)/2" :y="(nodePos[edge.from]?.y+nodePos[edge.to]?.y)/2 - 6" font-size="10" fill="#64748b" text-anchor="middle">{{ edge.label }}</text>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :class="['dag-node', {selected: selectedNode?.id===node.id}]" @click="selectNode(node)" @mousedown="startDrag($event, node)">
          <rect :x="nodePos[node.id]?.x - 60" :y="nodePos[node.id]?.y - 20" width="120" height="40" rx="6" :fill="nodeColor(node.status)"/>
          <text :x="nodePos[node.id]?.x" :y="nodePos[node.id]?.y + 4" text-anchor="middle">{{ node.name || node.label }}</text>
        </g>
      </svg>
    </div>
    <div v-if="selectedNode" class="dag-detail" data-testid="dag-detail">
      <dl><dt>编号</dt><dd>{{ selectedNode.id }}</dd></dl>
      <dl><dt>名称</dt><dd>{{ selectedNode.name || selectedNode.label }}</dd></dl>
      <dl v-for="f in detailFields" :key="f"><dt>{{ f }}</dt><dd>{{ selectedNode[f] ?? '—' }}</dd></dl>
      <div style="grid-column:1/-1; margin-top:8px; display:flex; gap:6px;">
        <button v-for="action in actions" :key="action" class="btn" :data-testid="`action-${selectedNode.id}-${action}`" @click="runAction(action, selectedNode)">{{ action }}</button>
      </div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记__ENTITY__</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <footer class="page-foot"><span>{{ graph.nodes.length }} 个节点</span></footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/__KEY__'
const columns = __FIELDS__
const actions = __ACTIONS__
const statuses = __STATUSES__
const STATUS_COLORS: Record<string,string> = { '草稿':'#e2e8f0','待校验':'#fef3c7','已启用':'#d1fae5','已停用':'#fee2e2' }
const nodeColor = (s: string) => STATUS_COLORS[s] || '#e2e8f0'
const graph = reactive<{nodes: any[], edges: any[]}>({ nodes: [], edges: [] })
const nodePos = ref<Record<number,{x:number;y:number}>>({})
const selectedNode = ref<any>(null)
const detailFields = computed(() => columns.filter((f: string) => !['id','name','label','status'].includes(f)))
const svgW = ref(800); const svgH = ref(400)
const errorMessage = ref('')
const creating = ref(false); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
let dragNode: any = null; const dagRef = ref<HTMLElement|null>(null)
function layout(nodes: any[]) {
  const w = 800; const h = Math.max(400, nodes.length * 80)
  svgW.value = w; svgH.value = h
  const pos: Record<number,{x:number;y:number}> = {}
  nodes.forEach((n,i) => { pos[n.id] = { x: 120 + (i % 5) * 150, y: 50 + Math.floor(i / 5) * 80 } })
  nodePos.value = pos
}
function selectNode(node: any) { selectedNode.value = selectedNode.value?.id === node.id ? null : node }
function startDrag(e: MouseEvent, node: any) { dragNode = node; const onMove = (ev: MouseEvent) => { if(!dragNode) return; nodePos.value[node.id] = { x: ev.offsetX, y: ev.offsetY } }; const onUp = () => { dragNode = null; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }; document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp) }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes) } catch(e) { errorMessage.value = '加载失败' }
}
async function runAction(action: string, node: any) {
  try { await request(`${ENDPOINT}/${node.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) }); await reload() } catch(e) { errorMessage.value = '操作失败' }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
"""

VUE_JOB_TIMELINE = """\
<template>
  <section class="page" data-module="__KEY__">
    <header class="page-head">
      <div><h2>__NAME__</h2><p class="page-desc">__DESC__</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记__ENTITY__</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记__ENTITY__</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/__KEY__'
const columns = __FIELDS__
const actions = __ACTIONS__
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
"""

VUE_ANALYSIS_DASHBOARD = """\
<template>
  <section class="page" data-module="__KEY__">
    <header class="page-head">
      <div><h2>__NAME__</h2><p class="page-desc">__DESC__</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/__KEY__'
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({})
const paramFields = __FIELDS__.filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => __FIELDS__.slice(0, 5))
function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function runAnalysis() {
  loading.value = true
  try { const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) }); const data = await res.json(); results.value = data.items || []; chartData.value = (data.items || []).map((r: any) => ({ label: Object.values(r)[0] as string, value: Object.values(r).filter(v => typeof v === 'number')[0] as number || 0 })).slice(0, 6) } catch(e) { results.value = [] }
  loading.value = false
}
onMounted(fetchSummary)
</script>
"""

REACT_VIEW = """\
import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/__KEY__'
const columns = __FIELDS__
const actions = __ACTIONS__
const statuses = __STATUSES__
const metricNames = __METRICS_NAMES__

type Row = Record<string, string | number | null>

export default function __CLASS__View() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const [showCreate, setShowCreate] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createValues, setCreateValues] = useState<Record<string, string>>({})
  const stats = useMemo(() => [
    { label: metricNames[0], value: total },
    { label: metricNames[1], value: rows.filter((row) => row.status === statuses[1]).length },
    { label: metricNames[2], value: rows.filter((row) => row.status === statuses[statuses.length - 1]).length },
  ], [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('__ENTITY__列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '__NAME__列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  function openCreate() {
    setCreateValues(Object.fromEntries(columns.map((field) => [field, ''])))
    setErrorMessage('')
    setShowCreate(true)
  }

  async function exportRows() {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}/export?${query}`)
      if (!response.ok) throw new Error('导出失败，请稍后重试')
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url
      link.download = '__KEY__.csv'
      link.click()
      URL.revokeObjectURL(url)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '导出失败')
    }
  }

  async function submitCreate(event: React.FormEvent) {
    event.preventDefault()
    setCreating(true)
    setErrorMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST', body: JSON.stringify(createValues),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
      setShowCreate(false)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '登记失败')
    } finally {
      setCreating(false)
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '__NAME__动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '__NAME__操作失败')
    }
  }

  return (
    <section className="page" data-module="__KEY__">
      <header className="page-head">
        <div>
          <h2>__NAME__管理</h2>
          <p className="page-desc">__DESC__</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" data-testid="open-create" type="button" onClick={openCreate}>
            登记__ENTITY__
          </button>
          <button className="btn" data-testid="export" type="button" onClick={() => void exportRows()}>
            导出__NAME__清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              data-testid={`filter-${field}`}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" data-testid="query" type="submit">查询</button>
        <button className="btn ghost" data-testid="reset" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} data-testid={`row-${row.id}`} data-status={String(row.status)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} data-testid={`action-${row.id}-${action}`} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无__NAME__数据，可先登记__ENTITY__</td>
            </tr>
          )}
        </tbody>
      </table>

      {showCreate && (
        <div className="modal-backdrop" data-testid="create-modal">
          <form className="modal" onSubmit={submitCreate}>
            <h3>登记__ENTITY__</h3>
            {columns.map((field) => (
              <label key={field} className="create-field">
                <span>{field}</span>
                <input
                  value={createValues[field] ?? ''}
                  data-testid={`create-${field}`}
                  required
                  onChange={(event) => setCreateValues((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
            <div className="page-actions">
              <button className="btn" type="button" onClick={() => setShowCreate(false)}>取消</button>
              <button className="btn primary" data-testid="submit-create" type="submit" disabled={creating}>
                {creating ? '提交中…' : '确认登记'}
              </button>
            </div>
          </form>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条__NAME__记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
"""

DASHBOARD_VUE = """\
<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = __FALLBACK_CARDS__
    moduleRows.value = __FALLBACK_MODULES__
  }
})
</script>
"""

DASHBOARD_REACT = """\
import { useEffect, useState } from 'react'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const fallbackCards = __FALLBACK_CARDS__
const fallbackModules = __FALLBACK_MODULES__

export default function Overview() {
  const [cards, setCards] = useState<Overview['cards']>(fallbackCards)
  const [moduleRows, setModuleRows] = useState<Overview['modules']>(fallbackModules)

  useEffect(() => {
    fetchJson<Overview>('/api/overview')
      .then((payload) => {
        setCards(payload.cards)
        setModuleRows(payload.modules)
      })
      .catch(() => undefined)
  }, [])

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>运营概览</h2>
          <p className="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
        </div>
      </header>
      <div className="stat-row">
        {cards.map((card) => (
          <article key={card.label} className="stat-card">
            <span className="stat-label">{card.label}</span>
            <strong className="stat-value">{card.value}</strong>
          </article>
        ))}
      </div>
      <table className="data-table">
        <thead>
          <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
        </thead>
        <tbody>
          {moduleRows.map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
              <td>{row.created}</td>
              <td>{row.pending}</td>
              <td>{row.abnormal}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
"""

ENV_DEVELOPMENT = """\
VITE_API_BASE=
VITE_APP_NAME=__APP_NAME__
"""


# ---------------------------------------------------------------------------
# 纯前端（backend = none）：本地数据层 + 不接后端的页面
#
# 与全栈版的差别只有两处：vite 不再代理 /api，页面也不再发 HTTP，改成直接调
# src/api/local-service.ts。数据用示例数据播种，写进浏览器 localStorage。
# 这样「纯前端」不是把后端删掉留个空壳，而是换了一条真实的本地数据链路。
# ---------------------------------------------------------------------------
VITE_CONFIG_VUE_LOCAL = """\
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 纯前端应用：没有后端，也就没有 /api 代理，数据全部走 src/api/local-service.ts。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: __FRONTEND_PORT__,
    // 关掉自动打开页面：起服务时只打印地址，不拉起浏览器
    open: false,
    strictPort: false,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
"""

VITE_CONFIG_REACT_LOCAL = """\
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 纯前端应用：没有后端，也就没有 /api 代理，数据全部走 src/api/local-service.ts。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: __FRONTEND_PORT__,
    // 关掉自动打开页面：起服务时只打印地址，不拉起浏览器
    open: false,
    strictPort: false,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
"""

LOCAL_TYPES_TS = """\
/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
"""

LOCAL_MODULES_TS = """\
import type { ModuleMeta } from './types'

// 模块元数据由仓库生成时写入：字段、状态、动作、流转目标都在这里，页面不再各自写一遍。
__MODULE_BLOCKS__

export const MODULE_BY_KEY: Map<string, ModuleMeta> = new Map(
  MODULES.map((item) => [item.key, item]),
)
"""

LOCAL_SEED_TS = """\
import type { EntryRow } from './types'

// 示例数据：首次打开时播种，之后浏览器里的改动优先，重置才会回到这份。
__SEED_BLOCKS__
"""

LOCAL_STORE_TS = """\
import { SEED_ROWS } from './seed'
import type { EntryRow } from './types'

// 本地持久化：数据放在 localStorage 里，刷新、关掉再打开都还在。
const STORAGE_KEY = '__APP_SLUG__:entries'

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function readStorage(): Record<string, EntryRow[]> {
  const fallback = clone(SEED_ROWS)
  if (typeof window === 'undefined' || !window.localStorage) {
    return fallback
  }
  const raw = window.localStorage.getItem(STORAGE_KEY)
  if (!raw) {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(fallback))
    return fallback
  }
  try {
    const parsed = JSON.parse(raw) as Record<string, EntryRow[]>
    return { ...fallback, ...parsed }
  } catch {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(fallback))
    return fallback
  }
}

let cache: Record<string, EntryRow[]> | null = null

export function allRows(): Record<string, EntryRow[]> {
  if (cache === null) {
    cache = readStorage()
  }
  return cache
}

export function listRows(key: string): EntryRow[] {
  return allRows()[key] ?? []
}

export function saveRows(key: string, rows: EntryRow[]): void {
  const next = { ...allRows(), [key]: rows }
  cache = next
  if (typeof window !== 'undefined' && window.localStorage) {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  }
}

export function resetRows(key: string): EntryRow[] {
  const rows = clone(SEED_ROWS[key] ?? [])
  saveRows(key, rows)
  return rows
}

export function storageKey(): string {
  return STORAGE_KEY
}
"""

LOCAL_SERVICE_TS = """\
import { MODULE_BY_KEY } from '@/data/modules'
import { allRows, listRows, resetRows, saveRows } from '@/data/local-store'
import type { ActionResult, EntryRow, ModuleMeta, OverviewResult, PageResult } from '@/data/types'

// 会写进数据的「往回走」动作：命中就把这条记录标成异常态，看板上能一眼看出来。
const NEGATIVE_ACTIONS = ['撤销', '作废', '拒绝', '驳回', '停用', '忽略', '下线', '回滚']

export function moduleMeta(key: string): ModuleMeta {
  const meta = MODULE_BY_KEY.get(key)
  if (!meta) {
    throw new Error(`没有登记名为 ${key} 的业务模块`)
  }
  return meta
}

export function filterRows(rows: EntryRow[], filters: Record<string, string>): EntryRow[] {
  const pairs = Object.entries(filters).filter(([, value]) => value.trim() !== '')
  if (pairs.length === 0) {
    return rows
  }
  return rows.filter((row) =>
    pairs.every(([field, value]) => String(row[field] ?? '').includes(value.trim())),
  )
}

export function listEntries(key: string, filters: Record<string, string> = {}): PageResult {
  const matched = filterRows(listRows(key), filters)
  return { items: matched, total: matched.length, page: 1, size: matched.length }
}

export function createEntry(key: string, values: Record<string, string>): ActionResult {
  const meta = moduleMeta(key)
  const missing = meta.fields.slice(0, 3).filter((field) => !values[field]?.trim())
  if (missing.length) {
    return { ok: false, message: `缺少必填字段：${missing.join('、')}` }
  }
  const rows = listRows(key)
  const id = Math.max(0, ...rows.map((row) => Number(row.id) || 0)) + 1
  const entry: EntryRow = {
    id,
    ...Object.fromEntries(meta.fields.map((field) => [field, values[field]?.trim() ?? ''])),
    status: meta.statuses[0],
    pending: true,
    abnormal: false,
  }
  saveRows(key, [...rows, entry])
  return { ok: true, message: `${meta.entity}已登记` }
}

export function runAction(key: string, id: number, action: string): ActionResult {
  const meta = moduleMeta(key)
  const target = meta.actionTargets[action]
  if (!target) {
    return { ok: false, message: `${meta.entity}没有登记「${action}」这个动作` }
  }
  const rows = listRows(key)
  const index = rows.findIndex((row) => Number(row.id) === id)
  if (index < 0) {
    return { ok: false, message: `没有找到编号为 ${id} 的${meta.entity}` }
  }
  const lastStatus = meta.statuses[meta.statuses.length - 1]
  const updated: EntryRow = {
    ...rows[index],
    status: target,
    pending: target !== lastStatus,
    abnormal: NEGATIVE_ACTIONS.some((verb) => action.startsWith(verb)),
  }
  const next = [...rows]
  next[index] = updated
  saveRows(key, next)
  return { ok: true, message: `${meta.entity}已${action}，当前状态「${target}」` }
}

export function resetModule(key: string): PageResult {
  resetRows(key)
  return listEntries(key)
}

function csvCell(value: unknown): string {
  let text = value === null || value === undefined ? '' : String(value)
  if (/^[=+@-]/.test(text)) text = `'${text}`
  return `"${text.replace(/"/g, '""')}"`
}

export function exportEntries(
  key: string,
  filters: Record<string, string> = {},
): { filename: string; content: string } {
  const meta = moduleMeta(key)
  const header = ['id', ...meta.fields, 'status']
  const lines = [header.map(csvCell).join(',')]
  for (const row of filterRows(listRows(key), filters)) {
    lines.push([row.id, ...meta.fields.map((field) => row[field] ?? ''), row.status].map(csvCell).join(','))
  }
  return { filename: `${meta.key}.csv`, content: `\\uFEFF${lines.join('\\n')}` }
}

export function downloadEntries(key: string, filters: Record<string, string> = {}): void {
  const { filename, content } = exportEntries(key, filters)
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export function loadOverview(): OverviewResult {
  const rows = allRows()
  const modules = [...MODULE_BY_KEY.values()].map((meta) => {
    const entries = rows[meta.key] ?? []
    return {
      name: meta.name,
      created: entries.length,
      pending: entries.filter((row) => row.pending).length,
      abnormal: entries.filter((row) => row.abnormal).length,
    }
  })
  const cards = [
    { label: '业务模块', value: modules.length },
    { label: '登记总量', value: modules.reduce((sum, item) => sum + item.created, 0) },
    { label: '待处理', value: modules.reduce((sum, item) => sum + item.pending, 0) },
    { label: '异常量', value: modules.reduce((sum, item) => sum + item.abnormal, 0) },
  ]
  return { cards, modules }
}
"""

VUE_VIEW_LOCAL = """\
<template>
  <section class="page" data-module="__KEY__">
    <header class="page-head">
      <div>
        <h2>__NAME__管理</h2>
        <p class="page-desc">__DESC__</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记__ENTITY__</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出__NAME__清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无__NAME__数据，可先登记__ENTITY__</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记__ENTITY__</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条__NAME__记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  downloadEntries,
  createEntry,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/local-service'
import type { EntryRow } from '@/data/types'

const meta = moduleMeta('__KEY__')
const columns = __FIELDS__
const actions = __ACTIONS__
const statuses = __STATUSES__
const metricNames = __METRICS_NAMES__

const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])
const statusSummary = computed(() =>
  statuses.map((status: string) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  reload()
}

function exportRows() {
  downloadEntries(meta.key, filters.value)
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  const result = applyAction(meta.key, Number(row.id), action)
  if (!result.ok) {
    errorMessage.value = result.message
    return
  }
  reload()
}

function reload() {
  errorMessage.value = ''
  try {
    const payload = listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '__NAME__列表读取失败'
  }
}

onMounted(reload)
</script>
"""

REACT_VIEW_LOCAL = """\
import { useCallback, useEffect, useMemo, useState } from 'react'

import { createEntry, downloadEntries, listEntries, moduleMeta, runAction as applyAction } from '@/api/local-service'
import type { EntryRow } from '@/data/types'

const meta = moduleMeta('__KEY__')
const columns = __FIELDS__
const actions = __ACTIONS__
const statuses = __STATUSES__
const metricNames = __METRICS_NAMES__

export default function __CLASS__View() {
  const [rows, setRows] = useState<EntryRow[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const [showCreate, setShowCreate] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createValues, setCreateValues] = useState<Record<string, string>>({})
  const stats = useMemo(() => [
    { label: metricNames[0], value: total },
    { label: metricNames[1], value: rows.filter((row) => row.status === statuses[1]).length },
    { label: metricNames[2], value: rows.filter((row) => row.status === statuses[statuses.length - 1]).length },
  ], [rows, total])

  const reload = useCallback(() => {
    setErrorMessage('')
    try {
      const payload = listEntries(meta.key, filters)
      setRows(payload.items)
      setTotal(payload.total)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '__NAME__列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    reload()
  }, [reload])

  function openCreate() {
    setCreateValues(Object.fromEntries(columns.map((field) => [field, ''])))
    setErrorMessage('')
    setShowCreate(true)
  }

  function submitCreate(event: React.FormEvent) {
    event.preventDefault()
    setCreating(true)
    setErrorMessage('')
    try {
      const result = createEntry(meta.key, createValues)
      if (!result.ok) throw new Error(result.message)
      setShowCreate(false)
      reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '登记失败')
    } finally {
      setCreating(false)
    }
  }

  function runAction(action: string, row: EntryRow) {
    setErrorMessage('')
    const result = applyAction(meta.key, Number(row.id), action)
    if (!result.ok) {
      setErrorMessage(result.message)
      return
    }
    reload()
  }

  return (
    <section className="page" data-module="__KEY__">
      <header className="page-head">
        <div>
          <h2>__NAME__管理</h2>
          <p className="page-desc">__DESC__</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" data-testid="open-create" type="button" onClick={openCreate}>
            登记__ENTITY__
          </button>
          <button className="btn" data-testid="export" type="button" onClick={() => downloadEntries(meta.key, filters)}>
            导出__NAME__清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <p className="status-legend">
        {statuses.map((status: string) => (
          <span key={status} className="legend-item">
            {status}：{rows.filter((row) => String(row.status) === status).length}
          </span>
        ))}
      </p>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              data-testid={`filter-${field}`}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" data-testid="query" type="submit">查询</button>
        <button className="btn ghost" data-testid="reset" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>当前状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} data-testid={`row-${row.id}`} data-status={String(row.status)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.status}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} data-testid={`action-${row.id}-${action}`} className="link" type="button" onClick={() => runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无__NAME__数据，可先登记__ENTITY__</td>
            </tr>
          )}
        </tbody>
      </table>

      {showCreate && (
        <div className="modal-backdrop" data-testid="create-modal">
          <form className="modal" onSubmit={submitCreate}>
            <h3>登记__ENTITY__</h3>
            {columns.map((field) => (
              <label key={field} className="create-field">
                <span>{field}</span>
                <input
                  value={createValues[field] ?? ''}
                  data-testid={`create-${field}`}
                  required
                  onChange={(event) => setCreateValues((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
            <div className="page-actions">
              <button className="btn" type="button" onClick={() => setShowCreate(false)}>取消</button>
              <button className="btn primary" data-testid="submit-create" type="submit" disabled={creating}>
                {creating ? '提交中…' : '确认登记'}
              </button>
            </div>
          </form>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条__NAME__记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
"""

DASHBOARD_VUE_LOCAL = """\
<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="refresh">重新统计</button>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
    <footer class="page-foot">
      <span>数据保存在本机浏览器里，换浏览器或清缓存会回到示例数据</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { loadOverview } from '@/api/local-service'
import type { OverviewResult } from '@/data/types'

const cards = ref<OverviewResult['cards']>([])
const moduleRows = ref<OverviewResult['modules']>([])

function refresh() {
  const payload = loadOverview()
  cards.value = payload.cards
  moduleRows.value = payload.modules
}

onMounted(refresh)
</script>
"""

DASHBOARD_REACT_LOCAL = """\
import { useCallback, useEffect, useState } from 'react'

import { loadOverview } from '@/api/local-service'
import type { OverviewResult } from '@/data/types'

export default function Overview() {
  const [cards, setCards] = useState<OverviewResult['cards']>([])
  const [moduleRows, setModuleRows] = useState<OverviewResult['modules']>([])

  const refresh = useCallback(() => {
    const payload = loadOverview()
    setCards(payload.cards)
    setModuleRows(payload.modules)
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>运营概览</h2>
          <p className="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={refresh}>重新统计</button>
        </div>
      </header>
      <div className="stat-row">
        {cards.map((card) => (
          <article key={card.label} className="stat-card">
            <span className="stat-label">{card.label}</span>
            <strong className="stat-value">{card.value}</strong>
          </article>
        ))}
      </div>
      <table className="data-table">
        <thead>
          <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
        </thead>
        <tbody>
          {moduleRows.map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
              <td>{row.created}</td>
              <td>{row.pending}</td>
              <td>{row.abnormal}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <footer className="page-foot">
        <span>数据保存在本机浏览器里，换浏览器或清缓存会回到示例数据</span>
      </footer>
    </section>
  )
}
"""

LOCAL_CSS_EXTRA = """\
.status-legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 10px; font-size: 12px; color: var(--muted); }
.legend-item { background: #eef2f7; border-radius: 999px; padding: 2px 10px; }
"""

README_LOCAL = """\
# __APP_NAME__

__APP_DESC__

这是一个**纯前端**管理平台：__FRONTEND_LABEL__，仓库里没有后端服务。业务数据由
`frontend/src/data/` 下的本地数据层提供：首次打开用示例数据播种，之后的登记、筛选与状态流转
结果都持久化在浏览器 `localStorage` 里，刷新或重开浏览器都还在。dev server 已关掉自动打开页面，
启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 __FRONTEND_LABEL__ 前端（唯一运行单元）
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/local-service.ts   本地数据服务：列表、筛选、动作流转、导出
│   ├── src/data/             模块元数据 / 示例数据 / localStorage 持久化
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false，无 /api 代理）
├── .gitignore
└── docker-compose.yml
```

## 启动

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:__FRONTEND_PORT__/`，dev server 不会自动打开浏览器，需要自己访问。

生产构建：

```bash
cd frontend
npm run build
```

## 业务模块

__MODULE_TABLE__

## 约定

- 每个模块的页面在 `frontend/src/views/<模块>/index.vue`，页面只负责渲染，读写统一走
  `frontend/src/api/local-service.ts`。
- 字段、状态、动作与流转目标集中在 `frontend/src/data/modules.ts`；示例数据在
  `frontend/src/data/seed.ts`。
- 状态流转只允许在 `local-service.ts` 里改，页面组件不做业务判断。
- 想回到初始数据：清掉浏览器里 `__STORAGE_KEY__` 这一项，或调用 `resetModule(模块)`。
"""

COMPOSE_LOCAL = """\
services:
  frontend:
    build: ./frontend
    ports:
      - "__FRONTEND_PORT__:__FRONTEND_PORT__"
"""

MAKEFILE_LOCAL = """\
.PHONY: install frontend build

install:
\tcd frontend && npm ci

frontend:
\tcd frontend && npm run dev

build:
\tcd frontend && npm run build
"""


# ---------------------------------------------------------------------------
# 后端：Python（FastAPI）
# ---------------------------------------------------------------------------
REQUIREMENTS = """\
# 2026-11-15 在 Python 3.13 干净环境实际安装并通过全栈 E2E 的完整锁定版本。
annotated-doc==0.0.6
annotated-types==0.8.0
anyio==4.16.0
click==8.5.1
fastapi==0.163.0
h11==0.16.0
httptools==0.9.0
idna==3.20
opentelemetry-api==1.45.2
pydantic==2.14.3
pydantic_core==2.50.4
python-dotenv==1.2.4
PyYAML==6.0.3
starlette==1.10.0
typing-inspection==0.4.5
typing_extensions==4.16.0
uvicorn==0.54.0
uvloop==0.23.0
watchfiles==1.3.0
websockets==17.4
"""

BACKEND_MAIN = '''\
"""__APP_NAME__ 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port __BACKEND_PORT__
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.store import store

app = FastAPI(title="__APP_NAME__", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
'''

ROUTERS_INIT = '''\
"""业务模块路由汇总。

这里统一按别名导入再暴露 ROUTERS：模块名有可能和内置名撞车（某个业务模块就叫 dict、list
这种名字时），按名字直接 import 会把内置类型覆盖掉，函数注解在运行时求值就会报
'module' object is not subscriptable。
"""
from __future__ import annotations

__IMPORTS__

ROUTERS = [__MODULES__]
'''

BACKEND_CONFIG = '''\
"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "__APP_NAME__"
    env: str = "local"
    port: int = __BACKEND_PORT__
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:__FRONTEND_PORT__",
            "http://localhost:__FRONTEND_PORT__",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200


settings = Settings()
'''

BACKEND_STORE = '''\
"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
'''

BACKEND_SCHEMAS = '''\
"""接口出入参模型：列表分页、动作结果与各模块的明细结构。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    """登记或修改一条业务记录时提交的字段集合。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    """状态动作使用独立契约，避免与登记的 values 载荷混用。"""

    action: str
    remark: str | None = None

__SCHEMAS__
'''

ROUTER_PY = '''\
"""__NAME__接口：维护__ENTITY__，覆盖__ACTION_JOIN__等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.__KEY__ import __CLASS__Service

router = APIRouter(prefix="/api/__KEY__", tags=["__NAME__"])

service = __CLASS__Service()

LIST_FIELDS = __FIELDS__
STATUSES = __STATUSES__


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按__FIELD0__检索"),
    status: str | None = Query(default=None, description="__STATUS_JOIN__"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、__FIELD0__关键字与状态过滤__NAME__列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\\ufeff")
    writer = csv.writer(output, lineterminator="\\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="__KEY__.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条__ENTITY__明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"__ENTITY__ {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条__ENTITY__，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="__ENTITY__已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条__ENTITY__执行__ACTION_JOIN__；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
'''

SERVICE_PY = '''\
"""__NAME__业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "__KEY__"
LIST_FIELDS = __FIELDS__
REQUIRED_FIELDS = __REQUIRED_FIELDS__
STATUS_ORDER = __STATUSES__
ACTION_RULES = __ACTION_RULES__
NEGATIVE_ACTIONS = __NEGATIVE_ACTIONS__


class __CLASS__Service:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("__FIELD0__", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"__ENTITY__ {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于__NAME__可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"__ENTITY__已{action}"
'''

ROUTER_DAG_PY = '''\
"""__NAME__ DAG 拓扑接口：返回节点、连线图结构。"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.__KEY__ import __CLASS__Service

router = APIRouter(prefix="/api/__KEY__", tags=["__NAME__"])
service = __CLASS__Service()
LIST_FIELDS = __FIELDS__
STATUSES = __STATUSES__


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def get_graph() -> dict[str, Any]:
    """返回节点与边组成的 DAG 图数据。"""
    return service.get_graph()


@router.get("/export")
def export_entries() -> StreamingResponse:
    nodes, edges = service.raw_data()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["type", "id", "name", "from", "to", "label", "status"])
    for node in nodes:
        writer.writerow(["node", node.get("id",""), csv_cell(node.get("name","")), "", "", "", node.get("status","")])
    for edge in edges:
        writer.writerow(["edge", "", "", edge.get("from",""), edge.get("to",""), edge.get("label",""), ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=__KEY__.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"__ENTITY__ {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="__ENTITY__已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
'''

SERVICE_DAG_PY = '''\
"""__NAME__ DAG 图服务：生成节点、边示例结构与流转规则。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "__KEY__"
REQUIRED_FIELDS = __REQUIRED_FIELDS__
STATUS_ORDER = __STATUSES__
ACTION_RULES = __ACTION_RULES__
NEGATIVE_ACTIONS = __NEGATIVE_ACTIONS__


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


class __CLASS__Service:
    def get_graph(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        nodes: list[dict] = []
        edges: list[dict] = []
        for i, row in enumerate(rows):
            nodes.append({"id": int(row.get("id", i+1)), "name": str(row.get(list(row.keys())[1] if len(row)>1 else "name", "")), "status": str(row.get("status", STATUS_ORDER[0])),
                          **{k: row[k] for k in row if k not in ("id","status","pending","abnormal")}})
        for i in range(len(nodes)-1):
            edges.append({"from": nodes[i]["id"], "to": nodes[i+1]["id"], "label": "依赖"})
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id",0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"__ENTITY__ {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于__NAME__可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"__ENTITY__已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]
'''

ROUTER_TIMELINE_PY = '''\
"""__NAME__ 时间线接口：返回作业时间序列结构。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.__KEY__ import __CLASS__Service

router = APIRouter(prefix="/api/__KEY__", tags=["__NAME__"])
service = __CLASS__Service()
LIST_FIELDS = __FIELDS__
STATUSES = __STATUSES__


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.get_timeline()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=__KEY__.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"__ENTITY__ {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="__ENTITY__已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
'''

SERVICE_TIMELINE_PY = '''\
"""__NAME__ 时间线服务：生成作业时间序列和示例数据。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "__KEY__"
REQUIRED_FIELDS = __REQUIRED_FIELDS__
STATUS_ORDER = __STATUSES__
ACTION_RULES = __ACTION_RULES__
NEGATIVE_ACTIONS = __NEGATIVE_ACTIONS__


class __CLASS__Service:
    def get_timeline(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get(list(row.keys())[1] if len(row) > 1 else "name", "")),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "logs": f"[{start}] 作业启动\\n[{start}] 正在处理...\\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"__ENTITY__ {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于__NAME__可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"__ENTITY__已{action}"
'''

ROUTER_ANALYSIS_PY = '''\
"""__NAME__ 分析看板接口：汇总指标、参数运行与结果导出。"""
from __future__ import annotations

import csv
import io
import random
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.__KEY__ import __CLASS__Service

router = APIRouter(prefix="/api/__KEY__", tags=["__NAME__"])
service = __CLASS__Service()
LIST_FIELDS = __FIELDS__
STATUSES = __STATUSES__


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": [
        {"label": "覆盖数据源", "value": random.randint(12, 45), "trend": random.randint(-12, 25)},
        {"label": "最新指标值", "value": round(random.uniform(0.72, 0.99), 3), "trend": random.randint(-8, 15)},
        {"label": "异常记录", "value": random.randint(0, 8), "trend": -random.randint(1, 10)},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=__KEY__.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"__ENTITY__ {entry_id} 不存在")
    return entry
'''

SERVICE_ANALYSIS_PY = '''\
"""__NAME__ 分析看板服务：汇总指标、运行分析并生成模拟结果。"""
from __future__ import annotations

import random
from typing import Any

from app.store import store

MODULE = "__KEY__"
REQUIRED_FIELDS = __REQUIRED_FIELDS__
STATUS_ORDER = __STATUSES__
ACTION_RULES = __ACTION_RULES__
NEGATIVE_ACTIONS = __NEGATIVE_ACTIONS__


class __CLASS__Service:
    def list_results(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            d["指标值"] = round(random.uniform(0.65, 0.98), 3)
            d["对比基准"] = round(random.uniform(0.60, 0.92), 3)
            d["差异"] = round(d["指标值"] - d["对比基准"], 3)
            results.append(d)
        return results

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        count = int(params.get("样本范围", params.get("并行数量", "6")))
        results: list[dict] = []
        for i in range(count):
            results.append({
                "id": i + 1,
                "分组": f"分组{(i%3)+1}",
                "指标值": round(random.uniform(0.62, 1.02), 3),
                "对比基准": round(random.uniform(0.58, 0.95), 3),
                "差异": round(random.uniform(-0.2, 0.3), 3),
            })
        return results

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)
'''

BACKEND_SEED = '''\
"""示例数据：每个模块给几条不同状态的记录，方便起服务后立刻看到内容。"""
from __future__ import annotations

from typing import Any

SEED_ROWS: dict[str, list[dict[str, Any]]] = {
__SEED_TABLE__
}
'''

BACKEND_RUN_SH = """\
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port __BACKEND_PORT__
"""

JAVA_RUN_SH = """\
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v mvn >/dev/null 2>&1; then
  echo "需要 JDK 17 与 Maven：请先安装后再启动后端" >&2
  exit 1
fi
exec mvn -q spring-boot:run
"""

BACKEND_DOCKERFILE = """\
FROM python:3.12-slim
WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE __BACKEND_PORT__
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "__BACKEND_PORT__"]
"""

BACKEND_DOCKERFILE_JAVA = """\
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /srv/app
COPY pom.xml ./
COPY src ./src
RUN mvn -q -DskipTests package

FROM eclipse-temurin:17-jre
WORKDIR /srv/app
COPY --from=build /srv/app/target/*.jar app.jar
EXPOSE __BACKEND_PORT__
CMD ["java", "-jar", "app.jar"]
"""


# ---------------------------------------------------------------------------
# 后端：Java（Spring Boot）
# ---------------------------------------------------------------------------
JAVA_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.2.5</version>
    <relativePath/>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>__APP_SLUG__-backend</artifactId>
  <version>1.0.0</version>
  <name>__APP_NAME__ backend</name>
  <properties>
    <java.version>17</java.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-validation</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-test</artifactId>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
"""

JAVA_APPLICATION = """\
package com.example.__PKG__;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/** __APP_NAME__ 后端服务入口：默认监听 __BACKEND_PORT__。 */
@SpringBootApplication
public class Application {

    public static void main(String[] args) {
        SpringApplication.run(Application.class, args);
    }
}
"""

JAVA_CONTROLLER = """\
package com.example.__PKG__.controller;

import com.example.__PKG__.service.__CLASS__Service;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** __NAME__接口：维护__ENTITY__，覆盖__ACTION_JOIN__等动作。 */
@RestController
@RequestMapping("/api/__KEY__")
public class __CLASS__Controller {

    private static final List<String> LIST_FIELDS = List.of(__FIELDS__);
    private final __CLASS__Service service;

    public __CLASS__Controller(__CLASS__Service service) {
        this.service = service;
    }

    private Map<String, String> filters(Map<String, String> params) {
        Map<String, String> filters = new LinkedHashMap<>();
        for (String field : LIST_FIELDS) {
            String value = params.get(field);
            if (value != null && !value.isBlank()) {
                filters.put(field, value.trim());
            }
        }
        return filters;
    }

    /** 按页面字段、__FIELD0__关键字与状态过滤__NAME__列表。 */
    @GetMapping
    public Map<String, Object> list(
            @RequestParam Map<String, String> params,
            @RequestParam(required = false) String keyword,
            @RequestParam(required = false) String status,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int size) {
        if (page < 1 || size < 1 || size > 200) {
            throw new IllegalArgumentException("分页必须满足 page >= 1 且 1 <= size <= 200");
        }
        Map<String, String> filters = filters(params);
        List<Map<String, Object>> items = service.list(filters, keyword, status, page, size);
        return Map.of("items", items, "total", service.count(filters, keyword, status), "page", page, "size", size);
    }

    /** 下载当前筛选条件下的 UTF-8 CSV。 */
    @GetMapping("/export")
    public ResponseEntity<byte[]> export(
            @RequestParam Map<String, String> params,
            @RequestParam(required = false) String keyword,
            @RequestParam(required = false) String status) {
        List<Map<String, Object>> items = service.list(filters(params), keyword, status, 1, 1000000);
        StringBuilder csv = new StringBuilder("\\ufeff");
        csv.append(csvCell("id"));
        for (String field : LIST_FIELDS) {
            csv.append(',').append(csvCell(field));
        }
        csv.append(',').append(csvCell("status"));
        csv.append('\\n');
        for (Map<String, Object> item : items) {
            csv.append(csvCell(String.valueOf(item.get("id"))));
            for (String field : LIST_FIELDS) {
                csv.append(',').append(csvCell(String.valueOf(item.getOrDefault(field, ""))));
            }
            csv.append(',').append(csvCell(String.valueOf(item.getOrDefault("status", ""))));
            csv.append('\\n');
        }
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=__KEY__.csv")
                .contentType(new MediaType("text", "csv", StandardCharsets.UTF_8))
                .body(csv.toString().getBytes(StandardCharsets.UTF_8));
    }

    private String csvCell(String value) {
        String text = value == null ? "" : value;
        if (text.startsWith("=") || text.startsWith("+") || text.startsWith("-") || text.startsWith("@")) {
            text = "'" + text;
        }
        return "\\\"" + text.replace("\\\"", "\\\"\\\"") + "\\\"";
    }

    /** 读取单条__ENTITY__明细；不存在时给出可读的错误说明。 */
    @GetMapping("/{id}")
    public Map<String, Object> detail(@PathVariable long id) {
        return service.find(id)
                .map(row -> Map.<String, Object>of("ok", true, "entry", row))
                .orElseGet(() -> Map.of("ok", false, "message", "__ENTITY__不存在或已归档"));
    }

    /** 登记一条__ENTITY__，缺字段时说明原因而不是静默丢弃。 */
    @PostMapping
    public Map<String, Object> create(@RequestBody Map<String, Object> values) {
        List<String> missing = service.validate(values);
        if (!missing.isEmpty()) {
            return Map.of("ok", false, "message", "缺少必填字段：" + String.join("、", missing));
        }
        return Map.of("ok", true, "message", "__ENTITY__已登记", "entry", service.create(values));
    }

    /** 对单条__ENTITY__执行__ACTION_JOIN__；不允许的动作会被拦下并说明原因。 */
    @PostMapping("/{id}/actions")
    public Map<String, Object> runAction(@PathVariable long id, @RequestBody Map<String, Object> payload) {
        String action = String.valueOf(payload.getOrDefault("action", ""));
        return service.runAction(id, action);
    }
}
"""

JAVA_SERVICE = """\
package com.example.__PKG__.service;

import org.springframework.stereotype.Service;

import java.util.*;

/** __NAME__业务规则：状态流转、字段校验与筛选口径都收在这里。 */
@Service
public class __CLASS__Service implements ModuleService {

    private static final List<String> LIST_FIELDS = List.of(__FIELDS__);
    private static final List<String> REQUIRED_FIELDS = List.of(__REQUIRED_FIELDS__);
    private static final List<String> STATUS_ORDER = List.of(__STATUSES__);
    private static final Map<String, String> ACTION_RULES = Map.of(__ACTION_RULES__);
    private static final List<String> NEGATIVE_ACTIONS = List.of(__NEGATIVE_ACTIONS__);

    private final List<Map<String, Object>> rows = new ArrayList<>();
    private long sequence = 0L;

    public __CLASS__Service() {
        seed();
    }

    /** 模块中文名，概览页按它给每一行命名。 */
    @Override
    public String label() {
        return "__NAME__";
    }

    private void seed() {
        for (int index = 0; index < 3; index++) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id", ++sequence);
            for (String field : LIST_FIELDS) {
                row.put(field, field + "样例" + (index + 1));
            }
            row.put("status", STATUS_ORDER.get(Math.min(index, STATUS_ORDER.size() - 1)));
            row.put("pending", index < 2);
            row.put("abnormal", index == 1);
            rows.add(row);
        }
    }

    public List<Map<String, Object>> list(Map<String, String> filters, String keyword, String status, int page, int size) {
        List<Map<String, Object>> filtered = filter(filters, keyword, status);
        int from = (page - 1) * size;
        int to = Math.min(filtered.size(), from + size);
        if (from >= to) {
            return List.of();
        }
        return filtered.subList(from, to);
    }

    @Override
    public int count(Map<String, String> filters, String keyword, String status) {
        return filter(filters, keyword, status).size();
    }

    @Override
    public int flaggedCount(String flag) {
        int count = 0;
        for (Map<String, Object> row : rows) {
            if (Boolean.TRUE.equals(row.get(flag))) {
                count++;
            }
        }
        return count;
    }

    private List<Map<String, Object>> filter(Map<String, String> filters, String keyword, String status) {
        List<Map<String, Object>> filtered = new ArrayList<>();
        for (Map<String, Object> row : rows) {
            if (status != null && !status.isBlank() && !status.equals(row.get("status"))) {
                continue;
            }
            if (keyword != null && !keyword.isBlank()
                    && !String.valueOf(row.get(LIST_FIELDS.get(0))).toLowerCase().contains(keyword.toLowerCase())) {
                continue;
            }
            boolean matches = true;
            for (Map.Entry<String, String> entry : filters.entrySet()) {
                String actual = String.valueOf(row.getOrDefault(entry.getKey(), ""));
                if (!actual.toLowerCase().contains(entry.getValue().toLowerCase())) {
                    matches = false;
                    break;
                }
            }
            if (matches) {
                filtered.add(row);
            }
        }
        return filtered;
    }

    public Optional<Map<String, Object>> find(long id) {
        return rows.stream().filter(row -> Objects.equals(row.get("id"), id)).findFirst();
    }

    public List<String> validate(Map<String, Object> values) {
        List<String> missing = new ArrayList<>();
        for (String field : REQUIRED_FIELDS) {
            Object value = values.get(field);
            if (value == null || String.valueOf(value).trim().isEmpty()) {
                missing.add(field);
            }
        }
        return missing;
    }

    public Map<String, Object> create(Map<String, Object> values) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("id", ++sequence);
        for (String field : LIST_FIELDS) {
            row.put(field, values.get(field));
        }
        row.put("status", STATUS_ORDER.get(0));
        row.put("pending", true);
        row.put("abnormal", false);
        rows.add(row);
        return row;
    }

    public Map<String, Object> runAction(long id, String action) {
        Optional<Map<String, Object>> found = find(id);
        if (found.isEmpty()) {
            return Map.of("ok", false, "message", "__ENTITY__不存在或已归档");
        }
        if (!ACTION_RULES.containsKey(action)) {
            return Map.of("ok", false, "message", "动作「" + action + "」不属于__NAME__可执行范围");
        }
        Map<String, Object> row = found.get();
        String target = ACTION_RULES.get(action);
        row.put("status", target);
        row.put("pending", !target.equals(STATUS_ORDER.get(STATUS_ORDER.size() - 1)));
        row.put("abnormal", NEGATIVE_ACTIONS.contains(action));
        return Map.of("ok", true, "message", "__ENTITY__已" + action, "entry", row);
    }
}
"""

JAVA_OVERVIEW = """\
package com.example.__PKG__.controller;

import com.example.__PKG__.service.OverviewService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/** 运营概览接口：把各业务模块的待处理量汇总成看板卡片。 */
@RestController
public class OverviewController {

    private final OverviewService overviewService;

    public OverviewController(OverviewService overviewService) {
        this.overviewService = overviewService;
    }

    /** 健康检查：确认服务已经监听、示例数据已经就绪。 */
    @GetMapping("/api/health")
    public Map<String, Object> health() {
        return Map.of("ok", true, "app", "__APP_NAME__", "modules", overviewService.moduleCount());
    }

    /** 看板卡片 + 各模块明细。 */
    @GetMapping("/api/overview")
    public Map<String, Object> overview() {
        return overviewService.overview();
    }
}
"""

JAVA_MODULE_SERVICE = """\
package com.example.__PKG__.service;

import java.util.Map;

/** 各业务模块共用的能力：给出模块名与记录条数，供概览页汇总。 */
public interface ModuleService {

    /** 模块中文名。 */
    String label();

    /** 按页面字段、关键字与状态统计记录条数。 */
    int count(Map<String, String> filters, String keyword, String status);

    /** 统计指定布尔标记的记录数。 */
    int flaggedCount(String flag);
}
"""

JAVA_OVERVIEW_SERVICE = """\
package com.example.__PKG__.service;

import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 汇总各业务模块的关键指标，供运营概览页使用。 */
@Service
public class OverviewService {

    private final List<ModuleService> moduleServices;

    public OverviewService(List<ModuleService> moduleServices) {
        this.moduleServices = moduleServices;
    }

    public int moduleCount() {
        return moduleServices.size();
    }

    public Map<String, Object> overview() {
        List<Map<String, Object>> modules = new ArrayList<>();
        int created = 0;
        for (ModuleService service : moduleServices) {
            int rows = service.count(Map.of(), null, null);
            created += rows;
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("name", service.label());
            item.put("created", rows);
            item.put("pending", service.flaggedCount("pending"));
            item.put("abnormal", service.flaggedCount("abnormal"));
            modules.add(item);
        }
        int pending = modules.stream().mapToInt(item -> (int) item.get("pending")).sum();
        int abnormal = modules.stream().mapToInt(item -> (int) item.get("abnormal")).sum();
        return Map.of(
                "cards", List.of(
                        Map.of("label", "业务模块", "value", moduleServices.size()),
                        Map.of("label", "今日新增", "value", created),
                        Map.of("label", "待处理", "value", pending),
                        Map.of("label", "异常量", "value", abnormal)),
                "modules", modules);
    }
}
"""

JAVA_APP_YML = """\
server:
  port: __BACKEND_PORT__
spring:
  application:
    name: __APP_SLUG__-backend
logging:
  level:
    root: INFO
"""

FRONTEND_DOCKERFILE = """\
FROM node:20-alpine
WORKDIR /srv/web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
EXPOSE __FRONTEND_PORT__
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
"""


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def module_desc(module: dict) -> str:
    if module.get("desc"):
        return module["desc"]
    fields = "、".join(module["fields"][:4])
    return f"维护{module['entity']}，围绕{fields}做登记、筛选与状态流转。"


def module_metrics(module: dict) -> str:
    metrics = module.get("metrics")
    if not metrics:
        metrics = [f"累计{module['name']}", f"待处理{module['name']}", f"异常{module['name']}"]
    return metrics


# 命中这些动词的动作会把记录标记成异常态，方便页面与看板区分「正常推进」和「被打回」。
NEGATIVE_ACTION_VERBS = ("撤销", "作废", "拒绝", "驳回", "停用", "忽略", "下线", "回滚", "失败")


def sample_value(field: str, module_key: str, module_name: str, index: int) -> object:
    """按字段名造一条像样的示例值，别让「联系电话」里躺着业务名称。"""
    number = index + 1
    if any(token in field for token in ("电话", "手机", "联系方式")):
        return f"1380000{number:04d}"
    if any(token in field for token in ("时间", "日期", "期限", "时段", "投运")):
        return f"2026-09-{number:02d}"
    if "邮箱" in field:
        return f"operator{number}@example.com"
    if any(token in field for token in ("数量", "库存", "排序号")):
        return number * 10
    if any(token in field for token in ("金额", "价格", "费用")):
        return round(number * 12.5, 2)
    if any(token in field for token in ("编码", "编号", "账号", "单号", "证号")):
        return f"{module_key.upper()[:4]}-{number:04d}"
    return f"{module_name}样例{number}"


def module_actions(module: dict) -> dict[str, object]:
    """模块的动作、状态与状态流转表；流转目标在 spec 里显式给出，缺省按状态顺序推进。"""
    actions = list(module.get("actions") or [f"提交{module['name']}"])
    statuses = list(module.get("statuses") or ["待处理", "处理中", "已完成"])
    explicit = module.get("action_targets")
    if explicit:
        if len(explicit) != len(actions):
            raise SystemExit(
                f"模块 {module['key']} 的 action_targets 数量与 actions 不一致（{len(explicit)} != {len(actions)}）")
        targets = list(explicit)
    else:
        targets = [statuses[min(index + 1, len(statuses) - 1)] for index in range(len(actions))]
    unknown = [target for target in targets if target not in statuses]
    if unknown:
        raise SystemExit(f"模块 {module['key']} 的 action_targets 出现未定义状态：{unknown}")
    rules = {action: target for action, target in zip(actions, targets)}
    negative = [action for action in actions if action.startswith(NEGATIVE_ACTION_VERBS)]
    return {
        "actions": actions,
        "statuses": statuses,
        "rules": rules,
        "negative": negative,
    }


def build_nav_items(modules: list[dict]) -> str:
    items = [{"label": "运营概览", "path": "/"}]
    for module in modules:
        items.append({"label": module["name"], "path": f"/{module['key']}"})
    body = ", ".join(
        "{ label: " + json.dumps(item["label"], ensure_ascii=False)
        + ", path: " + json.dumps(item["path"]) + " }"
        for item in items
    )
    return f"[{body}]"


def indent_block(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def build_local_module_blocks(modules: list[dict]) -> str:
    """纯前端版的模块元数据：字段 / 状态 / 动作 / 流转目标都落进 modules.ts。"""
    blocks: list[str] = []
    for module in modules:
        flow = module_actions(module)
        blocks.append(
            "  {\n"
            f"    key: {json.dumps(module['key'])},\n"
            f"    name: {json.dumps(module['name'], ensure_ascii=False)},\n"
            f"    entity: {json.dumps(module['entity'], ensure_ascii=False)},\n"
            f"    desc: {json.dumps(module_desc(module), ensure_ascii=False)},\n"
            f"    fields: {ts_array(module['fields'])},\n"
            f"    statuses: {ts_array(flow['statuses'])},\n"
            f"    actions: {ts_array(flow['actions'])},\n"
            f"    actionTargets: {json.dumps(flow['rules'], ensure_ascii=False)},\n"
            f"    metrics: {ts_array(module_metrics(module))},\n"
            "  },"
        )
    return "export const MODULES: ModuleMeta[] = [\n" + "\n".join(blocks) + "\n]"


def build_local_seed_blocks(modules: list[dict]) -> str:
    """纯前端版的示例数据：每个模块三行，字段值与后端 seed 用同一套采样口径。"""
    blocks: list[str] = []
    for module in modules:
        statuses = module_actions(module)["statuses"]
        rows: list[dict] = []
        for index in range(3):
            row: dict[str, object] = {
                "id": index + 1,
                "status": statuses[index % len(statuses)],
                "pending": index < 2,
                "abnormal": index == 1,
            }
            for field in module["fields"]:
                row[field] = sample_value(field, module["key"], module["name"], index)
            rows.append(row)
        body = indent_block(json.dumps(rows, ensure_ascii=False, indent=2), 2)
        head, _, tail = body.partition("\n")
        blocks.append(f"  {json.dumps(module['key'])}: {head.lstrip()}\n{tail},")
    return "export const SEED_ROWS: Record<string, EntryRow[]> = {\n" + "\n".join(blocks) + "\n}"


def _pick_page_template(module: dict, frontend_only: bool, **render_kw: object) -> str:
    """根据模块的 page_shape 选择对应的页面模板。

    crud-table → 现有列表模板
    dag-graph → DAG 拓扑图
    job-timeline → 作业时间线
    analysis-dashboard → 分析看板
    纯前端模式下非 crud-table 回退到 crud-table。
    """
    shape = module.get("page_shape", "crud-table")
    if frontend_only and shape != "crud-table":
        shape = "crud-table"
    template = {
        "crud-table": VUE_VIEW_LOCAL if frontend_only else VUE_VIEW,
        "dag-graph": VUE_DAG_GRAPH,
        "job-timeline": VUE_JOB_TIMELINE,
        "analysis-dashboard": VUE_ANALYSIS_DASHBOARD,
    }.get(shape, VUE_VIEW)
    return render(template, **render_kw)


def gen_frontend_vue(spec: dict, out: Path) -> int:
    modules = spec["modules"]
    frontend_only = spec["backend"] == "none"
    fe = out / "frontend"
    lines = 0
    files: dict[Path, str] = {}
    files[fe / "package.json"] = render(
        PACKAGE_JSON_VUE, APP_SLUG=spec["app_slug"], FRONTEND_PORT=spec["port_frontend"],
        REPO_KIND="frontend-only" if frontend_only else "fullstack")
    files[fe / "package-lock.json"] = render(
        (SKILL_DIR / "templates/package-lock-vue.json").read_text(encoding="utf-8"),
        APP_SLUG=spec["app_slug"])
    files[fe / "vite.config.ts"] = (
        render(VITE_CONFIG_VUE_LOCAL, FRONTEND_PORT=spec["port_frontend"]) if frontend_only
        else render(VITE_CONFIG_VUE, FRONTEND_PORT=spec["port_frontend"],
                    BACKEND_PORT=spec["port_backend"]))
    files[fe / "index.html"] = render(
        INDEX_HTML, APP_NAME=spec["app_name"], MAIN_EXT="ts")
    files[fe / "tsconfig.json"] = TSCONFIG_VUE
    files[fe / ".env.development"] = render(ENV_DEVELOPMENT, APP_NAME=spec["app_name"])
    files[fe / "Dockerfile"] = render(
        FRONTEND_DOCKERFILE, FRONTEND_PORT=spec["port_frontend"])
    files[fe / "src/main.ts"] = MAIN_VUE
    files[fe / "src/env.d.ts"] = ENV_D_TS
    files[fe / "src/App.vue"] = render(
        APP_VUE, APP_NAME=spec["app_name"], APP_DESC=spec["app_desc"],
        NAV_ITEMS=build_nav_items(modules))
    files[fe / "src/stores/session.ts"] = render(SESSION_STORE, APP_SCOPE=spec["app_name"])
    files[fe / "src/styles/global.css"] = (
        GLOBAL_CSS + LOCAL_CSS_EXTRA if frontend_only else GLOBAL_CSS)

    if frontend_only:
        files[fe / "src/data/types.ts"] = LOCAL_TYPES_TS
        files[fe / "src/data/modules.ts"] = render(
            LOCAL_MODULES_TS, MODULE_BLOCKS=build_local_module_blocks(modules))
        files[fe / "src/data/seed.ts"] = render(
            LOCAL_SEED_TS, SEED_BLOCKS=build_local_seed_blocks(modules))
        files[fe / "src/data/local-store.ts"] = render(
            LOCAL_STORE_TS, APP_SLUG=spec["app_slug"])
        files[fe / "src/api/local-service.ts"] = LOCAL_SERVICE_TS
    else:
        files[fe / "src/api/client.ts"] = CLIENT_TS
    files[fe / "playwright.config.ts"] = PLAYWRIGHT_CONFIG_TS
    files[fe / "tests/functionality.spec.ts"] = render(
        FUNCTIONALITY_TEST_TS,
        MODULES=json.dumps([
            {"key": module["key"], "fields": module["fields"], "actions": module["actions"],
             "actionTargets": module["action_targets"], "shape": module.get("page_shape", "crud-table")}
            for module in modules
        ], ensure_ascii=False, indent=2),
        REPO_KIND="frontend-only" if frontend_only else "fullstack")

    view_imports = ["import Overview from '@/views/Overview.vue'"]
    routes = []
    for module in modules:
        key = module["key"]
        cls = class_name(key)
        view_imports.append(f"const {cls} = () => import('@/views/{key}/index.vue')")
        routes.append(
            "    { path: '" + f"/{key}" + "', name: '" + key + "', component: " + cls + " },"
        )
        files[fe / f"src/views/{key}/index.vue"] = _pick_page_template(
            module, frontend_only, KEY=key, NAME=module["name"], ENTITY=module["entity"],
            DESC=module_desc(module), FIELDS=ts_array(module["fields"]),
            ACTIONS=ts_array(module.get("actions") or [f"提交{module['name']}"]),
            STATUSES=ts_array(module.get("statuses") or ["待处理", "处理中", "已完成"]),
            METRICS_NAMES=json.dumps(module_metrics(module), ensure_ascii=False))
    files[fe / "src/router/index.ts"] = render(
        ROUTER_TS, VIEW_IMPORTS="\n".join(view_imports), ROUTES="\n".join(routes))

    fallback_cards = json.dumps(
        [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}], ensure_ascii=False)
    fallback_modules = json.dumps(
        [{"name": module["name"], "created": 0, "pending": 0, "abnormal": 0} for module in modules],
        ensure_ascii=False)
    files[fe / "src/views/Overview.vue"] = (
        DASHBOARD_VUE_LOCAL if frontend_only
        else render(DASHBOARD_VUE, FALLBACK_CARDS=fallback_cards, FALLBACK_MODULES=fallback_modules))

    for path, content in files.items():
        write(path, content)
        lines += line_count(content)
    return lines


def gen_frontend_react(spec: dict, out: Path) -> int:
    modules = spec["modules"]
    frontend_only = spec["backend"] == "none"
    fe = out / "frontend"
    lines = 0
    files: dict[Path, str] = {}
    files[fe / "package.json"] = render(
        PACKAGE_JSON_REACT, APP_SLUG=spec["app_slug"], FRONTEND_PORT=spec["port_frontend"],
        REPO_KIND="frontend-only" if frontend_only else "fullstack")
    files[fe / "package-lock.json"] = render(
        (SKILL_DIR / "templates/package-lock-react.json").read_text(encoding="utf-8"),
        APP_SLUG=spec["app_slug"])
    files[fe / "vite.config.ts"] = (
        render(VITE_CONFIG_REACT_LOCAL, FRONTEND_PORT=spec["port_frontend"]) if frontend_only
        else render(VITE_CONFIG_REACT, FRONTEND_PORT=spec["port_frontend"],
                    BACKEND_PORT=spec["port_backend"]))
    files[fe / "index.html"] = render(INDEX_HTML, APP_NAME=spec["app_name"], MAIN_EXT="tsx")
    files[fe / "tsconfig.json"] = TSCONFIG_REACT
    files[fe / ".env.development"] = render(ENV_DEVELOPMENT, APP_NAME=spec["app_name"])
    files[fe / "Dockerfile"] = render(
        FRONTEND_DOCKERFILE, FRONTEND_PORT=spec["port_frontend"])
    files[fe / "src/main.tsx"] = MAIN_REACT
    files[fe / "src/env.d.ts"] = ENV_D_TS
    files[fe / "src/styles/global.css"] = (
        GLOBAL_CSS + LOCAL_CSS_EXTRA if frontend_only else GLOBAL_CSS)

    if frontend_only:
        files[fe / "src/data/types.ts"] = LOCAL_TYPES_TS
        files[fe / "src/data/modules.ts"] = render(
            LOCAL_MODULES_TS, MODULE_BLOCKS=build_local_module_blocks(modules))
        files[fe / "src/data/seed.ts"] = render(
            LOCAL_SEED_TS, SEED_BLOCKS=build_local_seed_blocks(modules))
        files[fe / "src/data/local-store.ts"] = render(
            LOCAL_STORE_TS, APP_SLUG=spec["app_slug"])
        files[fe / "src/api/local-service.ts"] = LOCAL_SERVICE_TS
    else:
        files[fe / "src/api/client.ts"] = CLIENT_TS
    files[fe / "playwright.config.ts"] = PLAYWRIGHT_CONFIG_TS
    files[fe / "tests/functionality.spec.ts"] = render(
        FUNCTIONALITY_TEST_TS,
        MODULES=json.dumps([
            {"key": module["key"], "fields": module["fields"], "actions": module["actions"],
             "actionTargets": module["action_targets"], "shape": module.get("page_shape", "crud-table")}
            for module in modules
        ], ensure_ascii=False, indent=2),
        REPO_KIND="frontend-only" if frontend_only else "fullstack")

    view_imports = []
    routes = []
    for module in modules:
        key = module["key"]
        cls = class_name(key)
        view_imports.append(f"import {cls}View from './views/{key}'")
        routes.append(
            "          <Route path=\"" + f"/{key}" + "\" element={<" + cls + "View />} />"
        )
        view_body = render(
            REACT_VIEW_LOCAL if frontend_only else REACT_VIEW,
            KEY=key, NAME=module["name"], ENTITY=module["entity"],
            DESC=module_desc(module), CLASS=cls,
            FIELDS=json.dumps(module["fields"], ensure_ascii=False, indent=2),
            ACTIONS=json.dumps(module.get("actions") or [f"提交{module['name']}"],
                                ensure_ascii=False, indent=2),
            STATUSES=json.dumps(module.get("statuses") or ["待处理", "处理中", "已完成"],
                                ensure_ascii=False, indent=2),
            METRICS_NAMES=json.dumps(module_metrics(module), ensure_ascii=False, indent=2),
        )
        if frontend_only:
            # 主体识别会跳过 index.tsx 这种入口名，所以组件本身落到 <Key>View.tsx，
            # index.tsx 只做出口（标准的 React barrel 写法）。
            files[fe / f"src/views/{key}/index.tsx"] = (
                f"export {{ default }} from './{cls}View'\n")
            files[fe / f"src/views/{key}/{cls}View.tsx"] = view_body
        else:
            files[fe / f"src/views/{key}/index.tsx"] = view_body
    fallback_cards = json.dumps(
        [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}], ensure_ascii=False)
    fallback_modules = json.dumps(
        [{"name": module["name"], "created": 0, "pending": 0, "abnormal": 0} for module in modules],
        ensure_ascii=False)
    files[fe / "src/views/Overview.tsx"] = (
        DASHBOARD_REACT_LOCAL if frontend_only
        else render(DASHBOARD_REACT, FALLBACK_CARDS=fallback_cards,
                    FALLBACK_MODULES=fallback_modules))
    files[fe / "src/App.tsx"] = render(
        APP_REACT, APP_NAME=spec["app_name"], APP_DESC=spec["app_desc"],
        NAV_ITEMS=build_nav_items(modules),
        REACT_VIEW_IMPORTS="\n".join(view_imports),
        REACT_ROUTES="\n".join(routes))

    for path, content in files.items():
        write(path, content)
        lines += line_count(content)
    return lines


def gen_backend_python(spec: dict, out: Path) -> int:
    modules = spec["modules"]
    be = out / "backend"
    lines = 0
    files: dict[Path, str] = {}
    files[be / "requirements.txt"] = REQUIREMENTS
    files[be / "run.sh"] = render(BACKEND_RUN_SH, BACKEND_PORT=spec["port_backend"])
    files[be / "Dockerfile"] = render(BACKEND_DOCKERFILE, BACKEND_PORT=spec["port_backend"])
    files[be / "app/__init__.py"] = ""
    files[be / "app/services/__init__.py"] = ""
    files[be / "app/main.py"] = render(
        BACKEND_MAIN, APP_NAME=spec["app_name"], BACKEND_PORT=spec["port_backend"])
    files[be / "app/routers/__init__.py"] = render(
        ROUTERS_INIT,
        IMPORTS="\n".join(
            f"from app.routers import {module['key']} as router_{module['key']}"
            for module in modules),
        MODULES=", ".join(f"router_{module['key']}" for module in modules),
    )
    files[be / "app/config.py"] = render(
        BACKEND_CONFIG, APP_NAME=spec["app_name"], BACKEND_PORT=spec["port_backend"],
        FRONTEND_PORT=spec["port_frontend"])
    files[be / "app/store.py"] = BACKEND_STORE

    schema_blocks = []
    seed_blocks = []
    for module in modules:
        cls = class_name(module["key"])
        schema_blocks.append(
            "\n\nclass " + cls + "Entry(BaseModel):\n"
            f'    """{module["entity"]}明细结构。"""\n\n'
            + "\n".join(
                f"    {('field_' + str(index))}: str | None = None  # {field}"
                for index, field in enumerate(module["fields"])
            )
        )
        rows = []
        statuses_for_seed = module_actions(module)["statuses"]
        for index in range(3):
            row = {"id": index + 1, "status": statuses_for_seed[index % len(statuses_for_seed)],
                   "pending": index < 2, "abnormal": index == 1}
            for field in module["fields"]:
                row[field] = sample_value(field, module["key"], module["name"], index)
            rows.append(row)
        seed_blocks.append(
            "    " + json.dumps(module["key"], ensure_ascii=False) + ": "
            + py_literal(rows, indent=4)
        )
    files[be / "app/schemas.py"] = render(BACKEND_SCHEMAS, SCHEMAS="".join(schema_blocks))
    files[be / "app/seed.py"] = render(BACKEND_SEED, SEED_TABLE=",\n".join(seed_blocks))

    for module in modules:
        key = module["key"]
        cls = class_name(key)
        flow = module_actions(module)
        statuses = flow["statuses"]
        actions = flow["actions"]
        shape = module.get("page_shape", "crud-table")
        if shape == "dag-graph":
            router_tpl, service_tpl = ROUTER_DAG_PY, SERVICE_DAG_PY
        elif shape == "job-timeline":
            router_tpl, service_tpl = ROUTER_TIMELINE_PY, SERVICE_TIMELINE_PY
        elif shape == "analysis-dashboard":
            router_tpl, service_tpl = ROUTER_ANALYSIS_PY, SERVICE_ANALYSIS_PY
        else:
            router_tpl, service_tpl = ROUTER_PY, SERVICE_PY
        files[be / f"app/routers/{key}.py"] = render(
            router_tpl,
            KEY=key, NAME=module["name"], ENTITY=module["entity"], CLASS=cls,
            FIELDS=py_list(module["fields"]),
            STATUSES=py_list(statuses),
            FIELD0=module["fields"][0],
            ACTION_JOIN="、".join(actions),
            STATUS_JOIN="、".join(statuses),
        )
        files[be / f"app/services/{key}.py"] = render(
            service_tpl,
            KEY=key, NAME=module["name"], ENTITY=module["entity"], CLASS=cls,
            FIELDS=py_list(module["fields"]),
            REQUIRED_FIELDS=py_list(module["fields"][:3]),
            STATUSES=py_list(statuses),
            FIELD0=module["fields"][0],
            ACTION_RULES=json.dumps(flow["rules"], ensure_ascii=False),
            NEGATIVE_ACTIONS=py_list(flow["negative"]),
        )

    for path, content in files.items():
        write(path, content)
        lines += line_count(content)
    return lines


def gen_backend_java(spec: dict, out: Path) -> int:
    modules = spec["modules"]
    be = out / "backend"
    pkg = spec["app_slug"].replace("-", "")
    lines = 0
    files: dict[Path, str] = {}
    files[be / "pom.xml"] = render(JAVA_POM, APP_SLUG=spec["app_slug"], APP_NAME=spec["app_name"])
    files[be / "run.sh"] = render(JAVA_RUN_SH)
    files[be / "Dockerfile"] = render(BACKEND_DOCKERFILE_JAVA, BACKEND_PORT=spec["port_backend"])
    files[be / "src/main/resources/application.yml"] = render(
        JAVA_APP_YML, APP_SLUG=spec["app_slug"], BACKEND_PORT=spec["port_backend"])
    files[be / f"src/main/java/com/example/{pkg}/Application.java"] = render(
        JAVA_APPLICATION, PKG=pkg, APP_NAME=spec["app_name"], BACKEND_PORT=spec["port_backend"])
    files[be / f"src/main/java/com/example/{pkg}/controller/OverviewController.java"] = render(
        JAVA_OVERVIEW, PKG=pkg, APP_NAME=spec["app_name"])
    files[be / f"src/main/java/com/example/{pkg}/service/ModuleService.java"] = render(
        JAVA_MODULE_SERVICE, PKG=pkg)
    files[be / f"src/main/java/com/example/{pkg}/service/OverviewService.java"] = render(
        JAVA_OVERVIEW_SERVICE, PKG=pkg)

    for module in modules:
        key = module["key"]
        cls = class_name(key)
        flow = module_actions(module)
        statuses = flow["statuses"]
        actions = flow["actions"]
        action_rules = list(flow["rules"].items())
        files[be / f"src/main/java/com/example/{pkg}/controller/{cls}Controller.java"] = render(
            JAVA_CONTROLLER, PKG=pkg, KEY=key, NAME=module["name"], ENTITY=module["entity"],
            CLASS=cls, FIELD0=module["fields"][0], ACTION_JOIN="、".join(actions),
            FIELDS=", ".join(json.dumps(f, ensure_ascii=False) for f in module["fields"]))
        files[be / f"src/main/java/com/example/{pkg}/service/{cls}Service.java"] = render(
            JAVA_SERVICE, PKG=pkg, KEY=key, NAME=module["name"], ENTITY=module["entity"],
            CLASS=cls,
            FIELDS=", ".join(json.dumps(f, ensure_ascii=False) for f in module["fields"]),
            REQUIRED_FIELDS=", ".join(json.dumps(f, ensure_ascii=False)
                                      for f in module["fields"][:3]),
            STATUSES=", ".join(json.dumps(s, ensure_ascii=False) for s in statuses),
            ACTION_RULES=", ".join(
                f"{json.dumps(a, ensure_ascii=False)}, {json.dumps(t, ensure_ascii=False)}"
                for a, t in action_rules),
            NEGATIVE_ACTIONS=", ".join(json.dumps(a, ensure_ascii=False)
                                       for a in flow["negative"]) or '"__none__"',
        )

    for path, content in files.items():
        write(path, content)
        lines += line_count(content)
    return lines


def build_readme(spec: dict) -> str:
    modules = spec["modules"]
    table = "\n".join(
        f"| {module['name']} | `{module['key']}` | {module['entity']} | "
        f"{'、'.join(module['fields'][:3])} |"
        for module in modules
    )
    frontend_label = "Vue 3 + Vite + TypeScript" if spec["frontend"] == "vue" else "React + Vite + TypeScript"
    module_table = "| 模块 | 目录 | 业务对象 | 主要字段 |\n| --- | --- | --- | --- |\n" + table
    if spec["backend"] == "none":
        return render(
            README_LOCAL,
            APP_NAME=spec["app_name"], APP_DESC=spec["app_desc"],
            FRONTEND_LABEL=frontend_label,
            FRONTEND_PORT=spec["port_frontend"],
            STORAGE_KEY=f"{spec['app_slug']}:entries",
            MODULE_TABLE=module_table,
        )
    backend_label = "FastAPI（Python）" if spec["backend"] == "python" else "Spring Boot（Java 17）"
    backend_setup = "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    backend_run = "./run.sh"
    return render(
        README,
        APP_NAME=spec["app_name"], APP_DESC=spec["app_desc"],
        FRONTEND_LABEL=frontend_label, BACKEND_LABEL=backend_label,
        FRONTEND_PORT=spec["port_frontend"], BACKEND_PORT=spec["port_backend"],
        BACKEND_SETUP=backend_setup, BACKEND_RUN=backend_run,
        MODULE_TABLE=module_table,
    )


def repo_kind(spec: dict) -> str:
    return "frontend-only" if spec["backend"] == "none" else "fullstack"


def _scaffold_in_place(spec: dict, out: Path) -> dict:
    """在空目录中写文件；外部统一通过原子 scaffold 入口调用。"""
    out.mkdir(parents=True, exist_ok=False)
    frontend_only = spec["backend"] == "none"
    lines = 0
    lines += gen_frontend_vue(spec, out) if spec["frontend"] == "vue" else gen_frontend_react(spec, out)
    if not frontend_only:
        lines += gen_backend_python(spec, out) if spec["backend"] == "python" else gen_backend_java(spec, out)

    root_files = {
        out / ".gitignore": GITIGNORE,
        out / "README.md": build_readme(spec) + f"\n领域：{spec['domain']}\n子方向：{spec['subdirection']}\n",
        out / domain_rules.METADATA_FILE: json.dumps(domain_rules.validate(spec), ensure_ascii=False, indent=2) + "\n",
        # 规范化 spec 随仓库保存，供跨仓库查重和能力验收使用；不含本机路径或凭证。
        out / ".s11-spec.json": json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        out / "docker-compose.yml": (
            render(COMPOSE_LOCAL, FRONTEND_PORT=spec["port_frontend"]) if frontend_only
            else render(COMPOSE, BACKEND_PORT=spec["port_backend"],
                        FRONTEND_PORT=spec["port_frontend"])),
        out / "Makefile": (
            MAKEFILE_LOCAL if frontend_only
            else render(MAKEFILE,
                        BACKEND_INSTALL="python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
                        BACKEND_RUN="./run.sh")),
        out / ".env.example": "APP_ENV=local\n",
    }
    for path, content in root_files.items():
        write(path, content)
        lines += line_count(content)

    run_sh = out / "backend" / "run.sh"
    if run_sh.exists():
        run_sh.chmod(0o755)
    return {"files": sum(1 for _ in out.rglob("*") if _.is_file()), "lines_written": lines}


def scaffold(spec: dict, out: Path) -> dict:
    """完整预检后在同盘临时目录生成，再原子移动到目标位置。

    任何 schema、模板或写盘错误都不会留下一个看似可交付的半成品仓库。
    """
    topic_hits = scan_spec_topics(spec) if isinstance(spec, dict) else []
    if topic_hits:
        raise SystemExit(banned_topic_report(topic_hits))
    normalized = normalize_spec(spec)
    out = Path(out).expanduser().resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit(f"目标目录非空，先清空或换一个目录：{out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f".{out.name}.build-", dir=out.parent))
    temporary_out = workspace / "repo"
    try:
        result = _scaffold_in_place(normalized, temporary_out)
        if out.exists():
            out.rmdir()  # 只允许空目录占位
        temporary_out.replace(out)
        return result
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成一个满足硬性要求的代码仓库（全栈或纯前端）")
    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument("--out", help="输出目录的完整路径（必须为空或不存在）")
    target.add_argument("--parent", help="父目录：按项目编号建 <父目录>/<前缀><5 位数>/<前缀><5 位数>，外层是任务容器、内层才是仓库根，例如 cc10001/cc10001")
    parser.add_argument("--spec", help=f"题库 spec json；默认用 examples/{DEFAULT_SPEC_NAME}")
    parser.add_argument("--check-spec", action="store_true",
                        help="只读 spec、校验格式并扫严禁题域，打印报告后退出，不生成任何目录")
    parser.add_argument("--frontend", choices=["vue", "react"], help="覆盖 spec 里的前端技术栈")
    parser.add_argument("--backend", choices=["python", "java", "none"],
                        help="覆盖 spec 里的后端技术栈；none = 只生成纯前端仓库")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX,
                        help=f"项目编号前缀，2~4 个字母，默认 {DEFAULT_PREFIX}（大小写按传入的原样用）")
    parser.add_argument("--number", type=int, default=None,
                        help="指定 5 位编号（10001~19999）；不传就从目录里已有的编号往后排")
    parser.add_argument("--owner", help="GitHub owner，用来查这个编号的仓库是否已存在；默认读取本技能 GitHub 配置")
    parser.add_argument("--no-github-check", action="store_true",
                        help="不查 GitHub（离线时用）；此时只按本地目录往后排编号")
    parser.add_argument("--git-init", action="store_true", help="顺手 git init 并做一次提交")
    github_settings.add_arguments(parser)
    args = parser.parse_args()
    github_settings.configure_from_args(args)

    spec_path = Path(args.spec).expanduser() if args.spec else SKILL_DIR / "examples" / DEFAULT_SPEC_NAME

    if args.check_spec:
        # 只自检：读 spec -> 校验格式 -> 扫严禁题域，不碰磁盘。
        try:
            spec = load_spec(spec_path, args.frontend, args.backend)
        except SystemExit as exc:
            print(json.dumps({
                "ok": False,
                "spec": str(spec_path),
                "reason": str(exc),
            }, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps({
            "ok": True,
            "spec": str(spec_path),
            "app_name": spec["app_name"],
            "domain": spec["domain"],
            "subdirection": spec["subdirection"],
            "modules": len(spec["modules"]),
            "kind": repo_kind(spec),
            "banned_topic_hits": [],
            "gate_version": rules.GATE_VERSION,
        }, ensure_ascii=False, indent=2))
        return 0

    if not args.out and not args.parent:
        parser.error("需要 --out 或 --parent 之一（或改用 --check-spec 只自检 spec）")

    spec = load_spec(spec_path, args.frontend, args.backend)

    project: dict[str, object] = {}
    if args.parent:
        parent = Path(args.parent).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        check_github = not args.no_github_check
        owner = resolve_owner(args.owner) if check_github else args.owner
        project = pick_project_id(parent, args.prefix, explicit_number=args.number,
                                  owner=owner, check_github=check_github)
        # 多套一层：外层 <编号> 是任务容器，内层 <编号> 才是仓库根。
        # 即 <父目录>/cc10001/cc10001/{frontend,backend,...}
        container = parent / str(project["project_id"])
        out = container / str(project["project_id"])
        project["container"] = str(container)
        if not project.get("github_checked") and check_github:
            project["github_check_note"] = "没能查 GitHub（gh 未安装 / 未登录 / 未解析到 owner），只按本地目录排的编号"
    else:
        out = Path(args.out).expanduser().resolve()
        parsed = split_project_id(out.name)
        if parsed:
            project = {"project_id": out.name, "prefix": parsed[0], "number": parsed[1],
                       "picked_from": "--out 目录名", "github_checked": False, "skipped": []}

    result = scaffold(spec, out)

# 规范化 spec 已在仓库内保存；外层再留一份便于任务管理，绝不删除用户输入文件。
    if args.parent and args.spec:
        src = Path(args.spec).expanduser().resolve()
        shutil.copy2(out / ".s11-spec.json", container / src.name)

    if args.git_init:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=out, check=True)
        subprocess.run(["git", "add", "-A"], cwd=out, check=True)
        subprocess.run(["git", "-c", "user.email=repo@example.com", "-c", "user.name=repo",
                        "commit", "-q", "-m",
                        f"chore: 初始化{'纯前端' if spec['backend'] == 'none' else '全栈'}项目骨架"],
                       cwd=out, check=True)

    print(json.dumps({
        "ok": True,
        "out": str(out),
        "repo_root": str(out),
        "project": project,
        "app_name": spec["app_name"],
        "domain": spec["domain"],
        "subdirection": spec["subdirection"],
        "frontend": spec["frontend"],
        "backend": spec["backend"],
        "kind": repo_kind(spec),
        "modules": len(spec["modules"]),
        "files": result["files"],
        "lines_written": result["lines_written"],
        "next": f"python3 {SCRIPT_DIR / 'verify_repo.py'} --repo {out}",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
