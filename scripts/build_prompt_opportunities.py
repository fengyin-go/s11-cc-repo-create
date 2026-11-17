#!/usr/bin/env python3
"""Validate prompt-opportunity briefs and write them outside the generated repo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import repo_rules as rules  # noqa: E402
import check_prompt_capacity  # noqa: E402
import topic_map_v3  # noqa: E402


VERSION = 1
TARGET_COUNT = 36
RESERVE_PER_MODE = 1


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (_text(raw) for raw in value) if item]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")
    return payload


def _repo_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "无法读取仓库 HEAD")
    return result.stdout.strip()


def _validate_anchor(repo: Path, anchor: Any, label: str) -> list[str]:
    if not isinstance(anchor, dict):
        return [f"{label} 必须是对象"]
    path_text = _text(anchor.get("path"))
    path = Path(path_text)
    if not path_text or path.is_absolute() or ".." in path.parts:
        return [f"{label}.path 必须是不逃逸仓库的相对路径"]
    target = repo / path
    if not target.is_file():
        return [f"{label}.path 不存在：{path_text}"]
    try:
        start = int(anchor.get("start_line"))
        end = int(anchor.get("end_line"))
    except (TypeError, ValueError):
        return [f"{label} 行号必须是整数"]
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if start < 1 or end < start or end > len(lines):
        return [f"{label} 行号范围无效"]
    excerpt = _text(anchor.get("excerpt"))
    region = "\n".join(lines[start - 1:end])
    if excerpt != region:
        return [f"{label}.excerpt 必须逐字等于完整连续原文"]
    return []


def _normalize_brief(repo: Path, brief: dict, *, selected: bool) -> tuple[dict, list[str]]:
    errors: list[str] = []
    forbidden = {"prompt", "final_prompt", "prompt_txt", "prompt_text"}
    if forbidden & set(brief):
        errors.append("机会卡不能包含最终题面字段：" + "、".join(sorted(forbidden & set(brief))))
    for key in ("opportunity_id", "task_type", "family", "story_summary",
                "current_behavior", "desired_change", "natural_prompt_angle"):
        if not _text(brief.get(key)):
            errors.append(f"缺少 {key}")
    chain = brief.get("data_chain")
    if not isinstance(chain, dict):
        errors.append("缺少 data_chain")
        chain = {}
    for key in ("input", "operation", "output", "observable"):
        if not _text(chain.get(key)):
            errors.append(f"data_chain.{key} 不能为空")
    modules = brief.get("modules")
    if not isinstance(modules, dict):
        errors.append("缺少 modules")
        modules = {}
    for key in ("primary", "secondary", "artifact", "consumer_behavior"):
        if not _text(modules.get(key)):
            errors.append(f"modules.{key} 不能为空")
    constraints = _text_list(brief.get("constraints"))
    if len(constraints) < 2:
        errors.append("constraints 至少需要两条")
    tradeoff = brief.get("design_tradeoff")
    if not isinstance(tradeoff, dict):
        errors.append("缺少 design_tradeoff")
        tradeoff = {}
    if not _text(tradeoff.get("axis")):
        errors.append("design_tradeoff.axis 不能为空")
    options = _text_list(tradeoff.get("options"))
    if len(set(options)) < 2:
        errors.append("design_tradeoff.options 至少需要两个不同方案")
    if not _text(tradeoff.get("invariant")):
        errors.append("design_tradeoff.invariant 不能为空")
    if not _text_list(brief.get("acceptance_signals")):
        errors.append("acceptance_signals 不能为空")
    if not _text_list(brief.get("avoid_patterns")):
        errors.append("avoid_patterns 不能为空")
    anchors = brief.get("source_anchors")
    if not isinstance(anchors, list) or len(anchors) < 2:
        errors.append("source_anchors 至少需要两条")
        anchors = []
    anchor_paths: set[str] = set()
    for index, anchor in enumerate(anchors):
        errors.extend(_validate_anchor(repo, anchor, f"source_anchors[{index}]"))
        if isinstance(anchor, dict):
            anchor_paths.add(_text(anchor.get("path")))
    if len(anchor_paths) < 2:
        errors.append("source_anchors 必须来自至少两个不同源码文件")
    if not selected:
        if not _text(brief.get("reserve_for_mode")):
            errors.append("备用机会必须填写 reserve_for_mode")
        if brief.get("status") != "reserve":
            errors.append("备用机会 status 必须为 reserve")
    else:
        if brief.get("status") != "selected":
            errors.append("正式机会 status 必须为 selected")
        if "sequence" not in brief:
            errors.append("正式机会必须填写 sequence")
    return {
        **brief,
        "constraints": constraints,
        "acceptance_signals": _text_list(brief.get("acceptance_signals")),
        "avoid_patterns": _text_list(brief.get("avoid_patterns")),
        "source_anchors": anchors,
    }, errors


def _validate_brief_set(repo: Path, topic_map: dict, brief_rows: list[Any]) -> dict:
    issues: list[str] = []
    plans = {
        int(plan["sequence"]): plan
        for plan in topic_map.get("task_plan", [])
        if isinstance(plan, dict) and str(plan.get("sequence")).isdigit()
    }
    selected_rows = [
        row for row in brief_rows
        if isinstance(row, dict) and row.get("status") == "selected"
    ]
    reserve_rows = [
        row for row in brief_rows
        if isinstance(row, dict) and row.get("status") == "reserve"
    ]
    if len(selected_rows) != TARGET_COUNT:
        issues.append(f"正式机会必须为 {TARGET_COUNT} 条，实际 {len(selected_rows)}")
    expected_reserves = len(rules.USABLE_MODES) * RESERVE_PER_MODE
    if len(reserve_rows) != expected_reserves:
        issues.append(f"备用机会必须为 {expected_reserves} 条，实际 {len(reserve_rows)}")

    normalized: list[dict] = []
    seen_sequences: set[int] = set()
    seen_opportunity_ids: set[str] = set()
    selected_modes: Counter[str] = Counter()
    for index, row in enumerate(selected_rows):
        item, errors = _normalize_brief(repo, row, selected=True)
        issues.extend(f"selected[{index}]: {error}" for error in errors)
        opportunity_id = _text(row.get("opportunity_id"))
        if opportunity_id and opportunity_id in seen_opportunity_ids:
            issues.append(f"opportunity_id 重复：{opportunity_id}")
        elif opportunity_id:
            seen_opportunity_ids.add(opportunity_id)
        try:
            sequence = int(row.get("sequence"))
        except (TypeError, ValueError):
            issues.append(f"selected[{index}] sequence 无效")
            continue
        if sequence in seen_sequences:
            issues.append(f"selected sequence 重复：{sequence}")
            continue
        seen_sequences.add(sequence)
        plan = plans.get(sequence)
        if plan is None:
            issues.append(f"selected sequence={sequence} 不在 repo-topic-map")
            continue
        for key in ("task_type", "family", "topic"):
            expected = _text(
                row.get(key) or (row.get("topic_id") if key == "topic" else "")
            )
            if expected != _text(plan.get(key)):
                issues.append(
                    f"selected sequence={sequence} 的 {key}={expected!r} "
                    f"与 map={_text(plan.get(key))!r} 不一致"
                )
        mode = _text(row.get("demand_mode"))
        if mode not in rules.USABLE_MODES:
            issues.append(
                f"selected sequence={sequence} 的 demand_mode 不可用：{mode or '空'}"
            )
        else:
            selected_modes[mode] += 1
        item["sequence"] = sequence
        item["task_contract"] = {
            "domain_fit": plan.get("domain_fit"),
            "difficulty_design": plan.get("difficulty_design"),
            "base_commit": topic_map.get("base_commit"),
        }
        normalized.append(item)

    mode_counts = dict(selected_modes)
    for mode in rules.USABLE_MODES:
        if mode_counts.get(mode, 0) != 3:
            issues.append(
                f"正式机会模式 {mode!r} 必须为 3 条，实际 {mode_counts.get(mode, 0)}"
            )
    extra_modes = set(mode_counts) - set(rules.USABLE_MODES)
    if extra_modes:
        issues.append("正式机会包含禁用或未知模式：" + "、".join(sorted(extra_modes)))

    reserve_modes: Counter[str] = Counter()
    for index, row in enumerate(reserve_rows):
        item, errors = _normalize_brief(repo, row, selected=False)
        issues.extend(f"reserve[{index}]: {error}" for error in errors)
        opportunity_id = _text(row.get("opportunity_id"))
        if opportunity_id and opportunity_id in seen_opportunity_ids:
            issues.append(f"opportunity_id 重复：{opportunity_id}")
        elif opportunity_id:
            seen_opportunity_ids.add(opportunity_id)
        mode = _text(row.get("reserve_for_mode"))
        if mode not in rules.USABLE_MODES:
            issues.append(
                f"reserve[{index}] reserve_for_mode 不可用：{mode or '空'}"
            )
        else:
            reserve_modes[mode] += 1
        normalized.append(item)
    for mode in rules.USABLE_MODES:
        if reserve_modes.get(mode, 0) != RESERVE_PER_MODE:
            issues.append(
                f"备用机会模式 {mode!r} 必须为 {RESERVE_PER_MODE} 条，"
                f"实际 {reserve_modes.get(mode, 0)}"
            )

    if seen_sequences != set(plans):
        issues.append("正式机会 sequence 必须与 repo-topic-map 的 36 条题位完全一致")
    return {
        "issues": issues,
        "plans": plans,
        "selected_rows": selected_rows,
        "reserve_rows": reserve_rows,
        "normalized": normalized,
        "mode_counts": mode_counts,
        "reserve_modes": dict(reserve_modes),
    }


def build(
    repo: str | Path,
    briefs_path: str | Path,
    out_path: str | Path | None = None,
    *,
    downstream_skill: str | Path | None = None,
    map_path: str | Path | None = None,
) -> dict:
    repo = Path(repo).expanduser().resolve()
    briefs_path = Path(briefs_path).expanduser().resolve()
    output = (
        Path(out_path).expanduser().resolve()
        if out_path is not None
        else repo.parent / "prompt-opportunities" / f"{repo.name}.json"
    )
    issues: list[str] = []
    if repo in output.parents:
        issues.append("机会地图不得写入生成仓库或其子目录")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        issues.append("--out 必须是普通文件且不能是符号链接")
    if not repo.is_dir():
        issues.append(f"仓库不存在：{repo}")
    if not briefs_path.is_file():
        issues.append(f"briefs 文件不存在：{briefs_path}")
    if issues:
        return {"ok": False, "issues": issues}

    map_path = (Path(map_path).expanduser().resolve() if map_path
                else rules.topic_map_path(repo))
    if not map_path.is_file():
        return {"ok": False, "issues": [f"缺少容量题位表：{map_path}"]}
    topic_map = _load_json(map_path)
    topic_map_v3.normalize_map(topic_map)
    briefs_doc = _load_json(briefs_path)
    brief_rows = briefs_doc.get("opportunities")
    if not isinstance(brief_rows, list):
        return {"ok": False, "issues": ["briefs.opportunities 必须是数组"]}

    capacity = check_prompt_capacity.check_capacity(
        repo,
        map_path=map_path,
        downstream_skill=downstream_skill,
        target=TARGET_COUNT,
    )
    if capacity.get("ok") is not True:
        return {
            "ok": False,
            "issues": ["下游容量闸未通过"] + list(capacity.get("issues") or []),
            "capacity": capacity,
        }

    validated = _validate_brief_set(repo, topic_map, brief_rows)
    issues.extend(validated["issues"])
    if issues:
        return {"ok": False, "issues": issues, "capacity": capacity}

    payload = {
        "schema_version": VERSION,
        "source_project": repo.name,
        "source_commit": _repo_head(repo),
        "source_base_commit": topic_map.get("base_commit"),
        "repo_topic_map_sha256": _sha(map_path),
        "briefs_source_sha256": _sha(briefs_path),
        "target_count": TARGET_COUNT,
        "selected_count": len(validated["selected_rows"]),
        "reserve_count": len(validated["reserve_rows"]),
        "selection_policy": "12-modes-x3-plus-one-reserve-per-mode",
        "mode_counts": validated["mode_counts"],
        "reserve_mode_counts": validated["reserve_modes"],
        "capacity_fingerprint": capacity.get("fingerprint"),
        "opportunities": validated["normalized"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, output)
    return {
        "ok": True,
        "version": VERSION,
        "out": str(output),
        "sha256": _sha(output),
        "source_commit": payload["source_commit"],
        "source_base_commit": payload["source_base_commit"],
        "repo_topic_map_sha256": payload["repo_topic_map_sha256"],
        "selected_count": len(validated["selected_rows"]),
        "reserve_count": len(validated["reserve_rows"]),
        "mode_counts": validated["mode_counts"],
    }


def validate_existing(
    repo: str | Path,
    opportunity_path: str | Path,
    *,
    capacity: dict | None = None,
    expected_head: str | None = None,
    map_path: str | Path | None = None,
) -> dict:
    """Validate a generated map without trusting its prior report or rewriting it."""
    repo = Path(repo).expanduser().resolve()
    opportunity_path = Path(opportunity_path).expanduser().resolve()
    issues: list[str] = []
    if not repo.is_dir():
        issues.append(f"仓库不存在：{repo}")
    if opportunity_path.is_relative_to(repo):
        issues.append("机会地图不得位于生成仓库内部")
    if opportunity_path.is_symlink() or not opportunity_path.is_file():
        issues.append(f"机会地图必须是仓库外普通文件：{opportunity_path}")
    map_path = (Path(map_path).expanduser().resolve() if map_path
                else rules.topic_map_path(repo))
    if not map_path.is_file():
        issues.append(f"缺少容量题位表：{map_path}")
    if issues:
        return {"ok": False, "version": VERSION, "issues": issues}

    try:
        topic_map = _load_json(map_path)
        topic_map_v3.normalize_map(topic_map)
        payload = _load_json(opportunity_path)
        head = expected_head or _repo_head(repo)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "version": VERSION, "issues": [str(exc)]}

    if int(payload.get("schema_version") or 0) != VERSION:
        issues.append(f"schema_version 必须为 {VERSION}")
    if _text(payload.get("source_commit")) != head:
        issues.append("机会地图 source_commit 与当前仓库 HEAD 不一致")
    if _text(payload.get("source_base_commit")) != _text(topic_map.get("base_commit")):
        issues.append("机会地图 source_base_commit 与 topic map.base_commit 不一致")
    if _text(payload.get("repo_topic_map_sha256")) != _sha(map_path):
        issues.append("repo_topic_map_sha256 与当前 topic map 不一致")
    if int(payload.get("target_count") or 0) != TARGET_COUNT:
        issues.append(f"target_count 必须为 {TARGET_COUNT}")
    if int(payload.get("selected_count") or 0) != TARGET_COUNT:
        issues.append(f"selected_count 必须为 {TARGET_COUNT}")
    expected_reserves = len(rules.USABLE_MODES) * RESERVE_PER_MODE
    if int(payload.get("reserve_count") or 0) != expected_reserves:
        issues.append(f"reserve_count 必须为 {expected_reserves}")
    if payload.get("selection_policy") != "12-modes-x3-plus-one-reserve-per-mode":
        issues.append("selection_policy 与正式 36+12 口径不一致")
    opportunities = payload.get("opportunities")
    if not isinstance(opportunities, list):
        issues.append("opportunities 必须是数组")
        opportunities = []

    validated = _validate_brief_set(repo, topic_map, opportunities)
    issues.extend(validated["issues"])
    if payload.get("mode_counts") != validated["mode_counts"]:
        issues.append("mode_counts 与实际机会卡不一致")
    if payload.get("reserve_mode_counts") != validated["reserve_modes"]:
        issues.append("reserve_mode_counts 与实际备用卡不一致")
    if capacity is not None:
        if capacity.get("ok") is not True:
            issues.append("容量闸未通过，不能验证正式机会地图")
        if payload.get("capacity_fingerprint") != capacity.get("fingerprint"):
            issues.append("机会地图 capacity_fingerprint 与当前容量结果不一致")

    return {
        "ok": not issues,
        "version": VERSION,
        "repo": str(repo),
        "opportunity_map": str(opportunity_path),
        "sha256": _sha(opportunity_path),
        "source_commit": head,
        "source_base_commit": topic_map.get("base_commit"),
        "repo_topic_map_sha256": _sha(map_path),
        "selected_count": len(validated["selected_rows"]),
        "reserve_count": len(validated["reserve_rows"]),
        "mode_counts": validated["mode_counts"],
        "issue_count": len(issues),
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--briefs", required=True)
    parser.add_argument("--out")
    parser.add_argument("--map")
    parser.add_argument("--downstream-skill")
    args = parser.parse_args(argv)
    result = build(
        args.repo,
        args.briefs,
        args.out,
        map_path=args.map,
        downstream_skill=args.downstream_skill,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
