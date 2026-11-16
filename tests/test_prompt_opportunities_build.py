"""Tests for the external prompt-opportunity map builder."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_prompt_opportunities as builder
import repo_rules


class OpportunityBuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s11-build-opportunities-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src/orders.py").write_text(
            "def build_orders(rows):\n    订单数据集 = [dict(row) for row in rows]\n    return 订单数据集\n",
            encoding="utf-8",
        )
        (self.repo / "src/export.py").write_text(
            "def export_orders(订单数据集):\n    return str(订单数据集)\n",
            encoding="utf-8",
        )
        self.map_path = self.repo / ".s11-capacity/repo-topic-map.json"
        self.map_path.parent.mkdir()
        plans = []
        for index in range(36):
            plans.append({
                "sequence": index + 1,
                "slug": "development",
                "task_type": "功能开发",
                "topic": f"topic-{index % 18}",
                "family": f"新增能力-{index}",
                "angle": repo_rules.USABLE_MODES[index // 3],
            })
        self.map_path.write_text(json.dumps({
            "version": 1,
            "profile": "gsb-semantic-v2",
            "difficulty_contract_version": 2,
            "type_policy": "capacity-first",
            "domain": "数据工程",
            "subdirection": "数据分析与数据应用",
            "base_commit": "a" * 40,
            "topics": [],
            "task_plan": plans,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.briefs_path = self.root / "briefs.json"
        self.payload = self.make_briefs()
        self.write_briefs()

    def brief(self, index: int, mode: str, *, selected: bool) -> dict:
        item = {
            "opportunity_id": f"O-{index}",
            "status": "selected" if selected else "reserve",
            "task_type": "功能开发",
            "family": f"新增能力-{(index - 1) % 36 if selected else index}",
            "story_summary": "订单原始记录需要形成可核对的数据集。",
            "current_behavior": "订单结果只保留临时字段。",
            "desired_change": "形成完整订单数据集并供导出中心消费。",
            "natural_prompt_angle": "从下游无法核对订单结果切入。",
            "data_chain": {
                "input": "订单原始记录",
                "operation": "解析订单记录",
                "output": "订单数据集",
                "observable": "导出文件保留订单编号",
            },
            "modules": {
                "primary": "订单解析",
                "secondary": "分析导出中心",
                "artifact": "订单数据集",
                "consumer_behavior": "导出中心读取同一订单数据集",
            },
            "constraints": ["字段必须完整", "重复导入只生成一份结果"],
            "design_tradeoff": {
                "axis": "按批次重算还是按记录增量更新",
                "options": ["按批次重算", "按记录增量更新"],
                "invariant": "下游只能看到一份完整订单数据集",
            },
            "source_anchors": [
                {"path": "src/orders.py", "start_line": 1, "end_line": 1,
                 "excerpt": "def build_orders(rows):"},
                {"path": "src/export.py", "start_line": 1, "end_line": 1,
                 "excerpt": "def export_orders(订单数据集):"},
            ],
            "acceptance_signals": ["订单编号可追溯"],
            "avoid_patterns": ["不要写成普通列表页"],
        }
        if selected:
            item["sequence"] = index
            item["topic_id"] = f"topic-{(index - 1) % 18}"
            item["demand_mode"] = mode
        else:
            item["reserve_for_mode"] = mode
        return item

    def make_briefs(self) -> dict:
        selected = [
            self.brief(index + 1, repo_rules.USABLE_MODES[index // 3], selected=True)
            for index in range(36)
        ]
        reserve = [
            self.brief(100 + index, mode, selected=False)
            for index, mode in enumerate(repo_rules.USABLE_MODES)
        ]
        return {"schema_version": 1, "opportunities": selected + reserve}

    def write_briefs(self):
        self.briefs_path.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def capacity_report(self):
        return {
            "ok": True,
            "target": 36,
            "qualified_plan_count": 36,
            "issues": [],
            "fingerprint": {"sha256": "mock"},
        }

    def test_builds_valid_external_map(self):
        out = self.root / "prompt-opportunities/repo.json"
        with patch.object(builder.check_prompt_capacity, "check_capacity",
                          return_value=self.capacity_report()), patch.object(
                builder, "_repo_head", return_value="b" * 40):
            result = builder.build(self.repo, self.briefs_path, out)
        self.assertTrue(result["ok"], result)
        self.assertTrue(out.is_file())
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["selected_count"], 36)
        self.assertEqual(payload["reserve_count"], 12)
        self.assertEqual(payload["selection_policy"],
                         "12-modes-x3-plus-one-reserve-per-mode")
        self.assertEqual(payload["mode_counts"],
                         {mode: 3 for mode in repo_rules.USABLE_MODES})
        self.assertEqual(payload["reserve_mode_counts"],
                         {mode: 1 for mode in repo_rules.USABLE_MODES})
        self.assertFalse((self.repo / "prompt-opportunities").exists())

    def test_existing_map_is_revalidated_without_rewriting(self):
        out = self.root / "prompt-opportunities/repo.json"
        capacity = self.capacity_report()
        with patch.object(builder.check_prompt_capacity, "check_capacity",
                          return_value=capacity), patch.object(
                builder, "_repo_head", return_value="b" * 40):
            built = builder.build(self.repo, self.briefs_path, out)
            before = out.read_bytes()
        checked = builder.validate_existing(
            self.repo,
            out,
            capacity=capacity,
            expected_head="b" * 40,
        )
        self.assertTrue(checked["ok"], checked)
        self.assertEqual(checked["sha256"], built["sha256"])
        self.assertEqual(out.read_bytes(), before)

    def test_existing_map_with_stale_capacity_fingerprint_is_rejected(self):
        out = self.root / "prompt-opportunities/repo.json"
        with patch.object(builder.check_prompt_capacity, "check_capacity",
                          return_value=self.capacity_report()), patch.object(
                builder, "_repo_head", return_value="b" * 40):
            builder.build(self.repo, self.briefs_path, out)
        stale = self.capacity_report()
        stale["fingerprint"] = {"sha256": "changed"}
        checked = builder.validate_existing(
            self.repo,
            out,
            capacity=stale,
            expected_head="b" * 40,
        )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("capacity_fingerprint" in issue for issue in checked["issues"]))

    def test_rejects_output_inside_repo(self):
        with patch.object(builder.check_prompt_capacity, "check_capacity",
                          return_value=self.capacity_report()), patch.object(
                builder, "_repo_head", return_value="b" * 40):
            result = builder.build(
                self.repo,
                self.briefs_path,
                self.repo / "prompt-opportunities.json",
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("不得写入生成仓库" in issue for issue in result["issues"]))

    def test_rejects_final_prompt_fields(self):
        self.payload["opportunities"][0]["final_prompt"] = "最终题面"
        self.write_briefs()
        with patch.object(builder.check_prompt_capacity, "check_capacity",
                          return_value=self.capacity_report()), patch.object(
                builder, "_repo_head", return_value="b" * 40):
            result = builder.build(self.repo, self.briefs_path, self.root / "out.json")
        self.assertFalse(result["ok"])
        self.assertTrue(any("最终题面字段" in issue for issue in result["issues"]))

    def test_requires_three_selected_per_mode(self):
        self.payload["opportunities"] = [
            item for item in self.payload["opportunities"]
            if item.get("sequence") != 1
        ]
        self.write_briefs()
        with patch.object(builder.check_prompt_capacity, "check_capacity",
                          return_value=self.capacity_report()), patch.object(
                builder, "_repo_head", return_value="b" * 40):
            result = builder.build(self.repo, self.briefs_path, self.root / "out.json")
        self.assertFalse(result["ok"])
        self.assertTrue(any("正式机会必须为 36 条" in issue or "必须为 3 条" in issue
                            for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()
