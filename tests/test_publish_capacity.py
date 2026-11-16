"""The publication boundary must enforce real G10, before any remote writes."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_github as publisher


class PublicationCapacityTests(unittest.TestCase):
    def test_draft_is_rejected_before_remote_even_with_valid_old_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / ".s11-domain.json").write_text('{"domain":"数据工程"}')
            # A fabricated external report is never a substitute for evaluating G10.
            (repo.parent / "prompt-capacity-report.json").write_text(json.dumps({
                "ok": True, "source_head": "a" * 40, "qualified_plan_count": 50,
            }))
            args = argparse.Namespace(repo=str(repo), branch="main", execute=True,
                                      allow_dirty=False, skip_verify=False, remote_url="unused")
            with patch.object(publisher, "local_head", return_value="a" * 40), \
                 patch.object(publisher, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                 patch.object(publisher.verifier, "verify", return_value={"ok": True}), \
                 patch.object(publisher, "check_delivery_evidence", return_value={"ok": True, "problems": []}), \
                 patch.object(publisher, "configure_origin") as origin, \
                 patch.object(publisher, "repo_exists") as remote:
                result = publisher.publish(args)
            self.assertFalse(result["ok"])
            self.assertTrue(any("G10" in item for item in result["problems"]))
            origin.assert_not_called()
            remote.assert_not_called()

    def test_s11_publication_requires_external_opportunity_map_before_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / ".s11-domain.json").write_text('{"domain":"数据工程"}')
            args = argparse.Namespace(
                repo=str(repo), branch="main", execute=True, allow_dirty=False,
                skip_verify=False, remote_url="unused", opportunity_map=None,
            )
            capacity = {"ok": True, "fingerprint": {"sha256": "capacity"}}
            with patch.object(publisher, "local_head", return_value="a" * 40), \
                 patch.object(publisher, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                 patch.object(publisher.verifier, "verify", return_value={"ok": True}), \
                 patch.object(publisher.check_prompt_capacity, "check_capacity", return_value=capacity), \
                 patch.object(publisher, "check_delivery_evidence", return_value={"ok": True, "problems": []}), \
                 patch.object(publisher, "configure_origin") as origin, \
                 patch.object(publisher, "repo_exists") as remote:
                result = publisher.publish(args)
            self.assertFalse(result["ok"])
            self.assertTrue(any("机会地图" in item for item in result["problems"]))
            self.assertFalse(result["prompt_opportunities"]["ok"])
            origin.assert_not_called()
            remote.assert_not_called()

    def test_publication_accepts_smoke_without_e2e_build_but_requires_connectivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".s11-domain.json").write_text('{"domain":"数据工程"}')
            head = "a" * 40
            probe = {"ok": True, "source_head": head, "mode": "clean-clone", "cleaned_up": True,
                     "verification_scope": "startup-connectivity",
                     "steps": [{"step": name, "ok": True} for name in (
                         "前端安装依赖", "后端安装依赖", "前端首页可访问且有挂载点",
                         "前端入口脚本可加载", "后端 /api/health 就绪", "前端 -> 后端代理链路通") ]}
            report = repo.parent / "probe-report.json"
            (repo.parent / "similarity-report.json").write_text(json.dumps({"ok": True, "source_head": head}))
            report.write_text(json.dumps(probe))
            self.assertTrue(publisher.check_delivery_evidence(repo, head)["ok"])
            for change in ("mode", "link", "install", "entry", "health", "scope", "failure", "cleanup", "full"):
                with self.subTest(change=change):
                    bad = json.loads(json.dumps(probe))
                    if change == "mode":
                        bad["mode"] = "local"
                    elif change == "link":
                        bad["steps"].pop()
                    elif change == "install":
                        bad["steps"][0]["skipped"] = True
                    elif change == "entry":
                        bad["steps"][3]["ok"] = False
                    elif change == "health":
                        bad["steps"][4]["skipped"] = True
                    elif change == "scope":
                        bad.pop("verification_scope")
                    elif change == "failure":
                        bad["steps"].append({"step": "other failed check", "ok": False})
                    elif change == "cleanup":
                        bad["cleaned_up"] = False
                    else:
                        bad["verification_scope"] = "full-functional"
                    report.write_text(json.dumps(bad))
                    self.assertFalse(publisher.check_delivery_evidence(repo, head)["ok"])

    def test_frontend_only_publication_needs_no_backend_but_still_requires_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "frontend").mkdir(parents=True)
            (repo / ".s11-domain.json").write_text('{"domain":"数据工程"}')
            (repo / "frontend/package.json").write_text('{"repoKind":"frontend-only"}')
            head = "a" * 40
            probe = {"ok": True, "source_head": head, "mode": "clean-clone", "cleaned_up": True,
                     "verification_scope": "startup-connectivity",
                     "steps": [{"step": name, "ok": True} for name in (
                         "前端安装依赖", "前端首页可访问且有挂载点", "前端入口脚本可加载")]}
            report = repo.parent / "probe-report.json"
            (repo.parent / "similarity-report.json").write_text(json.dumps({"ok": True, "source_head": head}))
            report.write_text(json.dumps(probe))
            self.assertTrue(publisher.check_delivery_evidence(repo, head)["ok"])
            probe["steps"].pop()
            report.write_text(json.dumps(probe))
            self.assertFalse(publisher.check_delivery_evidence(repo, head)["ok"])

    def test_execute_flag_is_sufficient_without_second_confirmation(self):
        with patch.object(sys, "argv", ["publish_github.py", "--repo", "/unused", "--execute", "--json"]), \
             patch.object(publisher, "publish", return_value={"ok": True}) as publish, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(publisher.main(), 0)
        self.assertTrue(publish.call_args.args[0].execute)
        self.assertIsNone(publish.call_args.args[0].confirm)
