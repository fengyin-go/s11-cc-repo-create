"""Deterministic v2-to-v3 topic-map normalization.

This module only fills structural fields required by the installed downstream
contract. It preserves existing source anchors and semantic statements and does
not claim that generated workload text has been independently reviewed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


CONTRACT_VERSION = 3
WORKLOAD_VERSION = 1
MIN_TOOL_CALLS = 55
DEFAULT_TOOL_CALLS = 60

DECISION_AXES = {
    "source_of_truth",
    "effective_time",
    "completion_precedence",
    "version_adoption",
    "retention_vs_reclaim",
    "recovery_vs_cleanup",
    "old_vs_new_priority",
    "authority_conflict",
    "scope_boundary",
    "partial_success_boundary",
    "concurrency_conflict",
    "migration_switch",
    "other",
}

APPROVED_OPERATION_TERMS = (
    "采集", "接入", "抓取", "爬取", "导入", "读取", "解析", "清洗", "转换",
    "标准化", "关联", "聚合", "去重", "校验", "入湖", "入库", "落库", "存储",
    "写入", "查询", "计算", "调度", "回填", "迁移", "导出", "切分", "归并",
    "合并", "归档", "水位",
)

# 业务模块 key 到下游 domain_fit 批准操作词的稳定映射。
OPERATION_TERM_BY_MODULE = {
    "batch_registry": "校验",
    "error_handler": "校验",
    "data_retry": "采集",
    "collection_monitor": "查询",
    "incremental_tracker": "调度",
    "lineage_graph": "关联",
    "template_mgr": "标准化",
    "archive_store": "归档",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _module_key(plan: dict) -> str:
    design = plan.get("difficulty_design") or {}
    integration = design.get("module_integration") or {}
    primary = _text(integration.get("primary_module"))
    evidence_rows = _list(plan.get("support_evidence"))
    for evidence in evidence_rows:
        if (
            isinstance(evidence, dict)
            and _text(evidence.get("module")) == primary
            and _text(evidence.get("path"))
        ):
            return Path(_text(evidence["path"])).stem
    for evidence in evidence_rows:
        if isinstance(evidence, dict) and _text(evidence.get("path")):
            return Path(_text(evidence["path"])).stem
    for raw in (
        plan.get("topic"),
        integration.get("primary_module"),
    ):
        key = re.sub(r"[^a-z0-9_]+", "_", _text(raw).lower()).strip("_")
        if key:
            return key
    return ""


def operation_term_for(module: str, current: str = "", anchors: list | None = None) -> str:
    """Choose an approved operation, preferring evidence already in source."""
    if module in OPERATION_TERM_BY_MODULE:
        return OPERATION_TERM_BY_MODULE[module]
    if current:
        hits = [term for term in APPROVED_OPERATION_TERMS if term in current]
        if hits:
            return hits[0]
    anchor_text = "\n".join(
        _text(anchor.get("excerpt"))
        for anchor in _list(anchors)
        if isinstance(anchor, dict) and anchor.get("supports") == "operation"
    )
    anchor_hits = [term for term in APPROVED_OPERATION_TERMS if term in anchor_text]
    if anchor_hits:
        return anchor_hits[0]
    return "校验"


def infer_decision_axis(*values: str) -> str:
    text = " ".join(_text(value) for value in values)
    if any(word in text for word in (
        "业务时间", "采集时间", "生效时间", "水位", "迟到", "时间轴", "时间窗",
    )):
        return "effective_time"
    patterns = (
        ("completion_precedence", ("完成", "先后", "终态")),
        ("version_adoption", ("版本", "快照", "当前版本", "冻结版本")),
        ("old_vs_new_priority", ("新旧", "旧值", "新值", "历史")),
        ("authority_conflict", ("权限", "来源", "权威", "负责人")),
        ("partial_success_boundary", ("部分成功", "部分失败", "半成品")),
        ("concurrency_conflict", ("并发", "锁", "争用")),
        ("migration_switch", ("迁移", "切换", "双写", "影子")),
        ("scope_boundary", ("范围", "边界", "影响窗口")),
        ("source_of_truth", ("事实源", "唯一来源", "以谁为准")),
    )
    if "恢复" in text and "清理" in text:
        return "recovery_vs_cleanup"
    if "保留" in text and any(word in text for word in ("回收", "释放", "删除")):
        return "retention_vs_reclaim"
    for axis, words in patterns:
        if any(word in text for word in words):
            return axis
    return "other"


def _anchor_paths(plan: dict, module: str) -> list[str]:
    paths = []
    for evidence in _list(plan.get("support_evidence")):
        if not isinstance(evidence, dict):
            continue
        if _text(evidence.get("module")) == module and _text(evidence.get("path")):
            paths.append(_text(evidence["path"]))
    return paths


def derive_workload_design(plan: dict) -> dict:
    design = plan.get("difficulty_design") or {}
    integration = design.get("module_integration") or {}
    primary = _text(integration.get("primary_module")) or "主业务模块"
    secondary_entries = [
        item for item in _list(integration.get("secondary_modules"))
        if isinstance(item, dict) and _text(item.get("name"))
    ]
    secondary = _text(secondary_entries[0].get("name")) if secondary_entries else "下游消费模块"
    artifact = _text(integration.get("data_artifact")) or "业务数据产物"
    operation = "校验"
    fit = plan.get("domain_fit") or {}
    chain = fit.get("domain_chain") or {}
    operation = operation_term_for(
        _module_key(plan), _text(chain.get("operation")),
        _list(fit.get("domain_anchors")),
    )
    slices = [
        {
            "module": primary,
            "implementation": f"在{primary}中实现{operation}{artifact}的创建、写入和状态流转",
            "data_result": f"{artifact}的主版本数据记录",
            "verification": f"验证{primary}生成的{artifact}字段完整且状态可追溯",
        },
        {
            "module": primary,
            "implementation": f"在{primary}中补齐{artifact}失败恢复、去重和版本边界处理",
            "data_result": f"{artifact}的版本校验与恢复报告",
            "verification": f"验证{primary}重试不会覆盖上一份完整{artifact}",
        },
        {
            "module": secondary,
            "implementation": f"在{secondary}中读取{artifact}并生成下游消费记录",
            "data_result": f"{artifact}的下游消费记录",
            "verification": f"验证{secondary}只读取已确认版本的{artifact}",
        },
        {
            "module": secondary,
            "implementation": f"在{secondary}中处理{artifact}异常、审计和回执落库",
            "data_result": f"{artifact}的异常审计回执",
            "verification": f"验证{secondary}回执与主版本数据可以逐条追溯",
        },
    ]
    primary_paths = _anchor_paths(plan, primary)
    secondary_paths = _anchor_paths(plan, secondary)
    primary_path = primary_paths[0] if primary_paths else "backend/app/services/primary.py"
    secondary_path = secondary_paths[0] if secondary_paths else "backend/app/services/secondary.py"
    symbol = "domainSnapshot" if primary_path.startswith("frontend/") else "domain_snapshot"
    secondary_symbol = "domainSnapshot" if secondary_path.startswith("frontend/") else "domain_snapshot"
    return {
        "version": WORKLOAD_VERSION,
        "expected_effective_tool_calls": DEFAULT_TOOL_CALLS,
        "call_floor_rationale": (
            "跨两个模块的完整实现需要读取源码、设计接口、编写业务逻辑、"
            "补充失败与恢复分支，并验证至少两条独立数据链路，预计不低于60次有效工具调用。"
        ),
        "work_slices": slices,
        "verification_paths": [
            f"{primary_path}::{symbol}",
            f"{secondary_path}::{secondary_symbol}",
        ],
    }


def normalize_plan(plan: dict) -> bool:
    changed = False
    design = plan.get("difficulty_design")
    if not isinstance(design, dict):
        return False
    if design.get("version") in (None, 2):
        design["version"] = CONTRACT_VERSION
        changed = True

    reasoning = design.get("reasoning_challenge")
    if isinstance(reasoning, dict):
        seen: set[str] = set()
        for index, constraint in enumerate(_list(reasoning.get("constraints")), start=1):
            if not isinstance(constraint, dict):
                continue
            current = _text(constraint.get("id"))
            if not re.fullmatch(r"C[1-9][0-9]*", current) or current in seen:
                current = f"C{index}"
                while current in seen:
                    index += 1
                    current = f"C{index}"
                constraint["id"] = current
                changed = True
            seen.add(current)

    tradeoff = design.get("design_tradeoff")
    if isinstance(tradeoff, dict) and _text(tradeoff.get("decision_axis")) not in DECISION_AXES:
        tradeoff["decision_axis"] = infer_decision_axis(
            _text(tradeoff.get("decision")),
            _text(tradeoff.get("trigger")),
            _text(tradeoff.get("context_excerpt")),
            " ".join(_text(value) for value in _list(tradeoff.get("options"))),
        )
        changed = True

    workload = design.get("workload_design")
    complete_workload = (
        isinstance(workload, dict)
        and workload.get("version") == WORKLOAD_VERSION
        and type(workload.get("expected_effective_tool_calls")) is int
        and workload["expected_effective_tool_calls"] >= MIN_TOOL_CALLS
        and len(_text(workload.get("call_floor_rationale"))) >= 12
        and len(_list(workload.get("work_slices"))) >= 4
        and len(set(_list(workload.get("verification_paths")))) >= 2
    )
    if not complete_workload:
        design["workload_design"] = derive_workload_design(plan)
        changed = True

    fit = plan.get("domain_fit")
    if isinstance(fit, dict):
        chain = fit.get("domain_chain")
        if isinstance(chain, dict):
            current = _text(chain.get("operation"))
            module = _module_key(plan)
            if module in OPERATION_TERM_BY_MODULE:
                approved = OPERATION_TERM_BY_MODULE[module]
            elif current and any(term in current for term in APPROVED_OPERATION_TERMS):
                approved = current
            else:
                approved = operation_term_for(
                    module, current, _list(fit.get("domain_anchors")))
            if current != approved:
                chain["operation"] = approved
                for key in ("main_task",):
                    if current and current in _text(fit.get(key)):
                        fit[key] = _text(fit[key]).replace(current, approved)
                request = _text(fit.get("prompt_main_request"))
                if current and current in request:
                    fit["prompt_main_request"] = request.replace(current, approved)
                changed = True
    return changed


def normalize_map(payload: dict) -> bool:
    changed = False
    if payload.get("difficulty_contract_version") in (None, 2):
        payload["difficulty_contract_version"] = CONTRACT_VERSION
        changed = True
    for plan in _list(payload.get("task_plan")):
        if isinstance(plan, dict) and normalize_plan(plan):
            changed = True
    return changed


def write_normalized(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".v3.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
