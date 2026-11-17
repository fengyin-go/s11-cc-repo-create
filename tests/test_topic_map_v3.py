"""Topic-map v3 normalization tests for the installed downstream contract."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import topic_map_v3  # noqa: E402


def plan(module: str, operation: str) -> dict:
    return {
        "sequence": 1,
        "topic": module,
        "domain_fit": {
            "domain_chain": {
                "input": "原始数据记录",
                "operation": operation,
                "output": "业务数据集",
                "observable": "业务数据集可供下游查询",
            },
            "main_task": f"{operation}原始数据并生成业务数据集",
            "prompt_main_request": f"请{operation}原始数据并生成业务数据集。",
            "domain_anchors": [],
        },
        "difficulty_design": {
            "version": 2,
            "reasoning_challenge": {
                "constraints": [
                    {"kind": "transaction_consistency", "statement": "主数据与回执同时可见"},
                    {"kind": "failure_recovery", "statement": "恢复不能覆盖完整版本"},
                ]
            },
            "module_integration": {
                "primary_module": "主业务模块",
                "secondary_modules": [
                    {"name": "下游消费模块", "role": "consumer", "artifact": "业务数据集"}
                ],
                "data_artifact": "业务数据集",
            },
            "design_tradeoff": {
                "decision": "迟到数据按业务时间还是采集时间恢复",
                "trigger": "迟到记录与历史更正落在已发布窗口",
                "options": ["按业务时间恢复", "按采集时间恢复"],
            },
        },
        "support_evidence": [
            {
                "module": "主业务模块",
                "path": f"backend/app/services/{module}.py",
                "excerpt": "domain_snapshot",
            },
            {
                "module": "下游消费模块",
                "path": "backend/app/services/consumer.py",
                "excerpt": "domain_snapshot",
            },
        ],
    }


class TopicMapV3Test(unittest.TestCase):
    def test_upgrades_required_v3_fields_and_workload(self):
        item = plan("batch_registry", "批次")
        payload = {"difficulty_contract_version": 2, "task_plan": [item]}
        self.assertTrue(topic_map_v3.normalize_map(payload))
        self.assertEqual(payload["difficulty_contract_version"], 3)
        design = item["difficulty_design"]
        self.assertEqual(design["version"], 3)
        self.assertEqual([c["id"] for c in design["reasoning_challenge"]["constraints"]],
                         ["C1", "C2"])
        self.assertEqual(design["design_tradeoff"]["decision_axis"], "effective_time")
        workload = design["workload_design"]
        self.assertGreaterEqual(workload["expected_effective_tool_calls"], 55)
        self.assertGreaterEqual(len(workload["work_slices"]), 4)
        self.assertGreaterEqual(len(set(workload["verification_paths"])), 2)

    def test_cc10014_operation_mapping_uses_only_approved_terms(self):
        mappings = {
            "batch_registry": "校验",
            "error_handler": "校验",
            "data_retry": "采集",
            "collection_monitor": "查询",
            "incremental_tracker": "调度",
            "lineage_graph": "关联",
            "template_mgr": "标准化",
            "archive_store": "归档",
        }
        for module, expected in mappings.items():
            with self.subTest(module=module):
                item = plan(module, "批次")
                self.assertTrue(topic_map_v3.normalize_plan(item))
                operation = item["domain_fit"]["domain_chain"]["operation"]
                self.assertEqual(operation, expected)
                self.assertIn(expected, topic_map_v3.APPROVED_OPERATION_TERMS)
                self.assertIn(expected, item["domain_fit"]["prompt_main_request"])

    def test_duplicate_constraint_ids_are_reassigned(self):
        item = plan("batch_registry", "批次")
        constraints = item["difficulty_design"]["reasoning_challenge"]["constraints"]
        constraints[0]["id"] = "C1"
        constraints[1]["id"] = "C1"
        topic_map_v3.normalize_plan(item)
        self.assertEqual([value["id"] for value in constraints], ["C1", "C2"])


if __name__ == "__main__":
    unittest.main()
