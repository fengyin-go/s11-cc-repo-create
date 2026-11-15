"""业务差异性、生成前校验、功能契约与发布证据的回归测试。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import check_similarity
import publish_github
import scaffold_repo


class QualityGatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = json.loads((SKILL / "examples/spec-data-engineering.json").read_text(encoding="utf-8"))

    def test_documented_optional_ports_receive_defaults(self):
        spec = copy.deepcopy(self.spec)
        spec.pop("port_frontend")
        spec.pop("port_backend")
        normalized = scaffold_repo.normalize_spec(spec)
        self.assertEqual(normalized["port_frontend"], 5173)
        self.assertEqual(normalized["port_backend"], 8000)

    def test_invalid_identifiers_and_flows_fail_before_output(self):
        cases = []
        for key in ("../escape", "class", "health", "foo-bar"):
            spec = copy.deepcopy(self.spec)
            spec["modules"][0]["key"] = key
            cases.append(spec)
        spec = copy.deepcopy(self.spec)
        spec["modules"][0]["action_targets"] = ["未定义状态"]
        cases.append(spec)
        spec = copy.deepcopy(self.spec)
        spec["port_frontend"] = spec["port_backend"]
        cases.append(spec)
        for index, spec in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(SystemExit):
                scaffold_repo.scaffold(spec, self.root / f"invalid-{index}")
            self.assertFalse((self.root / f"invalid-{index}").exists())

    def test_business_fingerprint_ignores_stack_ports_and_module_keys(self):
        changed = copy.deepcopy(self.spec)
        changed["app_slug"] = "another-shell"
        changed["frontend"] = "react"
        changed["backend"] = "none"
        changed["port_frontend"] = 5300
        for module in changed["modules"]:
            module["key"] = module["key"] + "_renamed"
        self.assertEqual(check_similarity.fingerprint(self.spec), check_similarity.fingerprint(changed))
        self.assertFalse(check_similarity.evaluate(changed, [{"spec": self.spec}])["ok"])

    def test_application_rename_only_is_rejected(self):
        changed = copy.deepcopy(self.spec)
        changed["app_name"] = "完全新的名称"
        changed["app_desc"] = "完全不同的应用说明。"
        changed["app_slug"] = "renamed-only"
        result = check_similarity.evaluate(changed, [{"spec": self.spec, "project_id": "old"}])
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(result["nearest"]["score"], check_similarity.SPEC_THRESHOLD)

    def test_genuinely_different_business_spec_passes(self):
        changed = copy.deepcopy(self.spec)
        changed["app_name"] = "跨源流式质量实验台"
        changed["app_desc"] = "处理实时窗口、抽样置信度和数据产品订阅。"
        changed["subdirection"] = "数据分析与数据应用"
        for index, module in enumerate(changed["modules"]):
            module.update({
                "name": f"主题{index}聚合", "entity": f"对象{index}",
                "desc": f"围绕第{index}组样本做窗口聚合与偏差分析。",
                "fields": [f"维度{index}{field}" for field in range(8)],
                "statuses": [f"初始{index}", f"计算{index}", f"完成{index}", f"终止{index}"],
                "actions": [f"启动{index}", f"确认{index}", f"终止{index}"],
                "action_targets": [f"计算{index}", f"完成{index}", f"终止{index}"],
                "metrics": [f"对象{index}总量", f"计算{index}对象", f"完成{index}对象"],
            })
        result = check_similarity.evaluate(changed, [{"spec": self.spec, "project_id": "old"}])
        self.assertTrue(result["ok"], result)

    def test_generated_contract_and_e2e_assets_exist_for_all_python_and_local_forms(self):
        forms = (("vue", "python"), ("react", "python"), ("vue", "none"), ("react", "none"))
        for frontend, backend in forms:
            with self.subTest(frontend=frontend, backend=backend):
                repo = self.root / f"{frontend}-{backend}"
                scaffold_repo.scaffold(scaffold_repo.normalize_spec(self.spec, frontend, backend), repo)
                self.assertTrue((repo / "frontend/tests/functionality.spec.ts").is_file())
                self.assertTrue((repo / "frontend/playwright.config.ts").is_file())
                all_text = "\n".join(path.read_text(encoding="utf-8") for path in repo.rglob("*")
                                     if path.is_file() and path.suffix in {".vue", ".tsx", ".ts", ".py"})
                self.assertNotIn("尚未接入审批流", all_text)
                self.assertIn("data-testid=", all_text)
                if backend == "python":
                    router = (repo / "backend/app/routers/datasource.py").read_text(encoding="utf-8")
                    self.assertLess(router.index('@router.get("/export")'), router.index('@router.get("/{entry_id}"'))
                    self.assertIn("payload: ActionPayload", router)
                    self.assertIn("filters=query_filters(request)", router)
                else:
                    self.assertIn("export function createEntry", (repo / "frontend/src/api/local-service.ts").read_text())

    def test_compose_uses_service_name_for_proxy(self):
        repo = self.root / "compose"
        scaffold_repo.scaffold(self.spec, repo)
        compose = (repo / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("VITE_PROXY_TARGET: http://backend:8000", compose)

    def test_registry_rejects_same_business_after_stack_change(self):
        first = self.root / "cc10001" / "cc10001"
        scaffold_repo.scaffold(self.spec, first)
        check_similarity.record(self.root, first, "cc10001")
        second = self.root / "cc10002" / "cc10002"
        scaffold_repo.scaffold(scaffold_repo.normalize_spec(self.spec, "react", "none"), second)
        history = check_similarity.discover_history(self.root)
        result = check_similarity.evaluate(check_similarity.scaffold_repo.normalize_spec(self.spec), history,
                                           repo=second, parent=self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["nearest"]["exact"])

    def test_source_similarity_is_measured_for_same_stack_rename(self):
        first = self.root / "cc10003" / "cc10003"
        scaffold_repo.scaffold(self.spec, first)
        changed = copy.deepcopy(self.spec)
        changed.update(app_name="另一个应用名称", app_desc="另一段业务说明。", app_slug="other-app")
        second = self.root / "cc10004" / "cc10004"
        scaffold_repo.scaffold(changed, second)
        check_similarity.record(self.root, first, "cc10003")
        result = check_similarity.evaluate(changed, check_similarity.discover_history(self.root),
                                           repo=second, parent=self.root)
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(result["nearest"]["source_structure"], check_similarity.STRUCTURE_THRESHOLD)

    def test_delivery_evidence_requires_current_head_and_real_e2e_step(self):
        repo = self.root / "app"
        scaffold_repo.scaffold(self.spec, repo)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                        "commit", "-q", "-m", "init"], cwd=repo, check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                              text=True, check=True).stdout.strip()
        missing = publish_github.check_delivery_evidence(repo, head)
        self.assertFalse(missing["ok"])
        (repo.parent / "probe-report.json").write_text(json.dumps({
            "ok": True, "source_head": head, "cleaned_up": True,
            "steps": [{"step": "克隆内：后端 /api/health 就绪", "ok": True}],
        }), encoding="utf-8")
        (repo.parent / "similarity-report.json").write_text(json.dumps({
            "ok": True, "source_head": head, "fingerprint": check_similarity.fingerprint(self.spec),
        }), encoding="utf-8")
        no_e2e = publish_github.check_delivery_evidence(repo, head)
        self.assertFalse(no_e2e["ok"])
        self.assertTrue(any("浏览器全功能" in problem for problem in no_e2e["problems"]))
        probe_path = repo.parent / "probe-report.json"
        probe = json.loads(probe_path.read_text())
        probe["steps"].append({"step": "克隆内：浏览器全功能验收", "ok": True})
        probe_path.write_text(json.dumps(probe), encoding="utf-8")
        self.assertTrue(publish_github.check_delivery_evidence(repo, head)["ok"])
        stale = publish_github.check_delivery_evidence(repo, "0" * 40)
        self.assertFalse(stale["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
