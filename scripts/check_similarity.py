#!/usr/bin/env python3
"""跨仓库业务与源码相似度检查，并维护可迁移的生成登记册。

业务指纹刻意忽略 app_slug、技术栈、端口和模块 key，防止只换壳就绕过查重。
登记册默认在 <parent>/.s11-registry/registry.json；仓库内 .s11-spec.json 是权威 spec 快照。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import scaffold_repo  # noqa: E402

SPEC_FILE = ".s11-spec.json"
REGISTRY_DIR = ".s11-registry"
REGISTRY_FILE = "registry.json"
REGISTRY_VERSION = 1
SPEC_THRESHOLD = 0.82
STRUCTURE_THRESHOLD = 0.985
COMBINED_SPEC_THRESHOLD = 0.15


def canonical_business(spec: dict) -> dict:
    """去掉交付与技术外壳，只保留会决定业务内容的字段。"""
    modules = []
    for module in spec.get("modules", []):
        modules.append({
            field: module.get(field)
            for field in (
                "name", "entity", "fields", "statuses", "actions",
                "action_targets", "metrics", "desc",
            )
        })
    modules.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return {
        "domain": spec.get("domain"),
        "subdirection": spec.get("subdirection"),
        "app_name": spec.get("app_name"),
        "app_desc": spec.get("app_desc"),
        "modules": modules,
    }


def fingerprint(spec: dict) -> str:
    payload = json.dumps(canonical_business(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tokens(value: object) -> set[str]:
    text = str(value or "").strip().lower()
    result = set(re.findall(r"[a-z0-9_]+", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def module_text(module: dict) -> str:
    return " ".join(
        str(module.get(field) or "")
        if not isinstance(module.get(field), list)
        else " ".join(str(item) for item in module[field])
        for field in ("name", "entity", "fields", "desc")
    )


def best_module_overlap(left: list[dict], right: list[dict]) -> float:
    if not left or not right:
        return 0.0
    right_tokens = [tokens(module_text(module)) for module in right]
    left_tokens = [tokens(module_text(module)) for module in left]
    forward = sum(max((jaccard(item, other) for other in right_tokens), default=0.0) for item in left_tokens) / len(left)
    backward = sum(max((jaccard(item, other) for other in left_tokens), default=0.0) for item in right_tokens) / len(right)
    return (forward + backward) / 2


def list_tokens(spec: dict, fields: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for module in spec.get("modules", []):
        for field in fields:
            for item in module.get(field) or []:
                result |= tokens(item)
    return result


def compare_specs(left: dict, right: dict) -> dict[str, float | bool]:
    modules = best_module_overlap(left.get("modules", []), right.get("modules", []))
    fields = jaccard(list_tokens(left, ("fields",)), list_tokens(right, ("fields",)))
    workflow = jaccard(
        list_tokens(left, ("statuses", "actions", "action_targets")),
        list_tokens(right, ("statuses", "actions", "action_targets")),
    )
    application = jaccard(
        tokens(str(left.get("app_name") or "") + " " + str(left.get("app_desc") or "")),
        tokens(str(right.get("app_name") or "") + " " + str(right.get("app_desc") or "")),
    )
    score = 0.45 * modules + 0.25 * fields + 0.20 * workflow + 0.10 * application
    return {
        "exact": fingerprint(left) == fingerprint(right),
        "score": round(score, 6),
        "modules": round(modules, 6),
        "fields": round(fields, 6),
        "workflow": round(workflow, 6),
        "application": round(application, 6),
    }


def _business_sources(repo: Path) -> list[str]:
    files: list[Path] = []
    for pattern in (
        "frontend/src/views/**/*.vue", "frontend/src/views/**/*.tsx",
        "backend/app/routers/*.py", "backend/app/services/*.py",
        "backend/src/main/java/**/*Controller.java", "backend/src/main/java/**/*Service.java",
    ):
        files.extend(path for path in repo.glob(pattern) if path.is_file())
    # 文件名含模块 key，不应影响只换 key 的对比；按规范化内容而不是路径排序。
    normalized = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"(?m)//.*$|#.*$", " ", text)
        text = re.sub(r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)", "STR", text)
        text = re.sub(r"\b\d+(?:\.\d+)?\b", "NUM", text)
        text = re.sub(r"\s+", " ", text).strip()
        normalized.append(text)
    return sorted(normalized)


def _shingles(text: str, size: int = 8) -> set[int]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|STR|NUM|[^\s]", text)
    if len(words) < size:
        return {hash(" ".join(words))} if words else set()
    return {hash(" ".join(words[index:index + size])) for index in range(len(words) - size + 1)}


def compare_source(left: Path, right: Path) -> float:
    """比较剥离注释、字面量与空白后的业务代码结构；共享构建配置不参与。"""
    left_shingles: set[int] = set()
    right_shingles: set[int] = set()
    for text in _business_sources(left):
        left_shingles |= _shingles(text)
    for text in _business_sources(right):
        right_shingles |= _shingles(text)
    return jaccard(left_shingles, right_shingles)


def registry_path(parent: Path) -> Path:
    return parent / REGISTRY_DIR / REGISTRY_FILE


def load_registry(parent: Path) -> list[dict]:
    path = registry_path(parent)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"查重登记册无法读取：{path}（{exc}）") from None
    if not isinstance(payload, dict) or payload.get("version") != REGISTRY_VERSION:
        raise SystemExit(f"查重登记册版本不兼容：{path}")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit(f"查重登记册 entries 必须是数组：{path}")
    return entries


def discover_history(parent: Path) -> list[dict]:
    """登记册为主，仓库内 spec 为补充；旧仓库没有 spec 时明确报告覆盖缺口。"""
    entries = load_registry(parent)
    known = {entry.get("repo") for entry in entries}
    for spec_path in sorted(parent.glob(f"*/*/{SPEC_FILE}")):
        repo = spec_path.parent
        relative = str(repo.relative_to(parent))
        if relative in known:
            continue
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec = scaffold_repo.normalize_spec(spec)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        entries.append({"project_id": repo.name, "repo": relative, "spec": spec, "fingerprint": fingerprint(spec)})
    return entries


def evaluate(
    spec: dict,
    history: list[dict],
    *,
    repo: Path | None = None,
    parent: Path | None = None,
) -> dict:
    nearest: dict | None = None
    rejections: list[dict] = []
    for entry in history:
        other = entry.get("spec")
        if not isinstance(other, dict):
            continue
        comparison = compare_specs(spec, other)
        structure = 0.0
        other_repo = None
        if repo is not None and parent is not None and entry.get("repo"):
            candidate = parent / str(entry["repo"])
            if candidate.is_dir():
                other_repo = candidate
                structure = compare_source(repo, candidate)
        result = {
            "project_id": entry.get("project_id"),
            "repo": entry.get("repo"),
            **comparison,
            "source_structure": round(structure, 6),
        }
        if nearest is None or float(result["score"]) > float(nearest["score"]):
            nearest = result
        reasons = []
        if comparison["exact"]:
            reasons.append("业务 spec 完全相同（忽略技术栈、端口和模块 key）")
        if float(comparison["score"]) >= SPEC_THRESHOLD:
            reasons.append(f"业务语义相似度 {comparison['score']} >= {SPEC_THRESHOLD}")
        if other_repo is not None and structure >= STRUCTURE_THRESHOLD and float(comparison["score"]) >= COMBINED_SPEC_THRESHOLD:
            reasons.append(
                f"规范化业务源码相似度 {structure:.6f} >= {STRUCTURE_THRESHOLD}，且业务语义存在明显重合")
        if reasons:
            result["reasons"] = reasons
            rejections.append(result)
    return {
        "ok": not rejections,
        "fingerprint": fingerprint(spec),
        "history_count": len(history),
        "nearest": nearest,
        "rejections": rejections,
        "thresholds": {
            "spec": SPEC_THRESHOLD,
            "source_structure": STRUCTURE_THRESHOLD,
            "combined_spec": COMBINED_SPEC_THRESHOLD,
        },
    }


def record(parent: Path, repo: Path, project_id: str) -> dict:
    spec = scaffold_repo.normalize_spec(json.loads((repo / SPEC_FILE).read_text(encoding="utf-8")))
    relative = str(repo.resolve().relative_to(parent.resolve()))
    entry = {
        "project_id": project_id,
        "repo": relative,
        "fingerprint": fingerprint(spec),
        "spec": spec,
    }
    path = registry_path(parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / "registry.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        entries = load_registry(parent)
        comparison_history = [item for item in entries
                              if item.get("project_id") != project_id and item.get("repo") != relative]
        checked = evaluate(spec, comparison_history, repo=repo, parent=parent)
        if not checked["ok"]:
            raise SystemExit("登记前复核发现相同或雷同仓库：" + json.dumps(
                checked["rejections"], ensure_ascii=False)[:1200])
        entries = [item for item in entries if item.get("project_id") != project_id and item.get("repo") != relative]
        entries.append(entry)
        entries.sort(key=lambda item: str(item.get("project_id") or ""))
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix="registry-", suffix=".tmp", delete=False) as temporary:
                temporary_name = temporary.name
                temporary.write(json.dumps({
                    "version": REGISTRY_VERSION, "entries": entries,
                }, ensure_ascii=False, indent=2) + "\n")
            Path(temporary_name).replace(path)
        finally:
            if temporary_name and Path(temporary_name).exists():
                Path(temporary_name).unlink()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return {"ok": True, "registry": str(path), "entry": entry}


def main() -> int:
    parser = argparse.ArgumentParser(description="检查仓库/ spec 与本批及历史仓库是否相同或雷同")
    parser.add_argument("--parent", required=True, help="项目父目录，也是登记册所在位置")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", help="待检 spec JSON")
    source.add_argument("--repo", help="待检仓库根，读取 .s11-spec.json 并比较源码")
    parser.add_argument("--frontend", choices=["vue", "react"])
    parser.add_argument("--backend", choices=["python", "java", "none"])
    parser.add_argument("--record", action="store_true", help="检查通过后写入登记册；需配合 --repo")
    parser.add_argument("--project-id", help="登记的项目编号；缺省取仓库目录名")
    parser.add_argument("--json-out", help="同时把报告写到指定路径，供发布器核验")
    args = parser.parse_args()
    parent = Path(args.parent).expanduser().resolve()
    if args.spec:
        spec = scaffold_repo.load_spec(Path(args.spec).expanduser().resolve(), args.frontend, args.backend)
        repo = None
    else:
        repo = Path(args.repo).expanduser().resolve()
        try:
            spec = scaffold_repo.load_spec(repo / SPEC_FILE, args.frontend, args.backend)
        except SystemExit as exc:
            print(json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False, indent=2))
            return 1
    history = [entry for entry in discover_history(parent)
               if repo is None or not entry.get("repo") or (parent / str(entry["repo"])).resolve() != repo.resolve()]
    report = evaluate(spec, history, repo=repo, parent=parent)
    report["spec"] = str(args.spec) if args.spec else str(repo / SPEC_FILE)
    report["parent"] = str(parent)
    if repo is not None:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
        report["source_head"] = head.stdout.strip() if head.returncode == 0 else ""
    if args.record:
        if repo is None:
            parser.error("--record 需配合 --repo")
        if report["ok"]:
            report["recorded"] = record(parent, repo, args.project_id or repo.name)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_out:
        Path(args.json_out).expanduser().write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
