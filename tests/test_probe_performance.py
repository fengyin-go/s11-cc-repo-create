"""探针性能/证据回归：仅 mock 项目安装、服务和浏览器；只真实运行临时 Git/Python。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import probe_startup as probe  # noqa: E402


class ProbePerformance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="probe-performance-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "app"
        self.frontend = self.repo / "frontend"
        (self.frontend / "tests").mkdir(parents=True)
        (self.frontend / "tests/functionality.spec.ts").write_text("// mock suite\n")
        (self.frontend / "package.json").write_text(json.dumps({"repoKind": "frontend-only"}))
        (self.frontend / "package-lock.json").write_text("{}\n")
        (self.repo / ".gitignore").write_text("node_modules/\n.venv/\ndist/\n")
        self.args = argparse.Namespace(timeout=1.0, logs=None, skip_install=False,
                                       skip_e2e=False, build=True, json_out=None, clean_clone=False, profile="full")
        self.commands = []
        self.spawn_commands = []
        self.events = {"pip": threading.Event(), "npm": threading.Event()}
        self.backend = self.repo / "backend"
        self.pip_code = 0
        self.npm_code = 0

    def command(self, command, *, cwd, **kwargs):
        self.commands.append((command, cwd, kwargs))
        if command[1:3] == ["-m", "venv"]:
            python = cwd / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
        elif command[0].endswith("/pip"):
            self.events["pip"].set()
            return subprocess.CompletedProcess(command, self.pip_code, "pip", "")
        elif command[1] in ("ci", "install"):
            (cwd / "node_modules").mkdir(exist_ok=True)
            self.events["npm"].set()
            return subprocess.CompletedProcess(command, self.npm_code, "npm", "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    def http(self, url):
        if url.endswith("/api/overview"):
            return 200, '{"modules": []}'
        if "/api/" in url:
            return 200, '{"ok": true, "items": []}'
        return 200, '<div id="app"></div>'

    def run_probe(self, **overrides):
        args = argparse.Namespace(**{**vars(self.args), **overrides})
        process = MagicMock(pid=12345)
        process.poll.return_value = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(probe, "source_state", return_value=("a" * 40, "")))
            stack.enter_context(patch.object(probe, "run_command", side_effect=self.command))
            stack.enter_context(patch.object(probe.shutil, "which", side_effect=lambda name: f"/mock/{name}"))
            def spawn(command, cwd, *args, **kwargs):
                self.spawn_commands.append(command)
                if not self.args.skip_install:
                    if cwd == self.frontend and not overrides.get("skip_install"):
                        self.assertTrue(self.events["npm"].is_set() or (cwd / "node_modules").is_dir())
                    if cwd == self.backend:
                        self.assertTrue(self.events["pip"].is_set())
                return process, None

            stack.enter_context(patch.object(probe, "spawn", side_effect=spawn))
            stack.enter_context(patch.object(probe, "terminate", side_effect=lambda *_: setattr(process.poll, "return_value", 0)))
            stack.enter_context(patch.object(probe, "listener_owned_by", return_value=(True, "mock owned listener")))
            stack.enter_context(patch.object(probe, "http_get", side_effect=self.http))
            self.business_scan = stack.enter_context(patch.object(
                probe, "discover_business_endpoints", return_value=["/api/demo"]))
            self.browser_check = stack.enter_context(patch.object(
                probe, "browser_runtime", return_value=(True, "mock Chromium")))
            return probe.run_probe(self.repo, args)

    def fullstack(self):
        (self.frontend / "package.json").write_text("{}")
        self.backend.mkdir()
        (self.backend / "requirements.txt").write_text("mock-fastapi==1\n")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def commit_fixture(self):
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("-c", "user.name=probe-test", "-c", "user.email=test@example.invalid",
                 "commit", "-q", "-m", "temporary fixture")
        return self.git("rev-parse", "HEAD")

    def clean_probe(self, implementation=None):
        result = {}

        def fake(clone, args):
            result["clone"] = clone
            if implementation:
                return implementation(clone, args)
            return {"ok": True, "cleaned_up": True, "steps": [
                {"step": "浏览器全功能验收", "ok": True, "detail": "mock only"}],
                "timings_seconds": {"e2e": 0.001, "total": 0.002}}

        def emitted(report, args):
            if "clone" in result:
                self.assertFalse(result["clone"].parent.exists(), "报告不能早于临时克隆清理")
            result["report"] = report

        with patch.object(probe, "run_probe", side_effect=fake) as runner, \
             patch.object(probe, "emit", side_effect=emitted):
            result["code"] = probe.run_clean_clone(self.repo, self.args)
            result["calls"] = runner.call_count
        return result

    def test_installs_and_complete_e2e_build_run_once_with_download_cache(self):
        with patch.dict(os.environ, {"npm_config_cache": str(self.root / "download-cache")}):
            report = self.run_probe()
        self.assertTrue(report["ok"], report["failed_steps"])
        installs = [cmd for cmd, _, _ in self.commands if cmd[1] == "ci"]
        self.assertEqual(len(installs), 1)
        self.assertIn("--prefer-offline", installs[0])
        self.assertIn("--no-audit", installs[0])
        self.assertEqual(sum(cmd[1:] == ["run", "test:e2e"] for cmd, _, _ in self.commands), 1)
        self.assertEqual(sum(cmd[1:] == ["run", "build"] for cmd, _, _ in self.commands), 1)
        self.assertEqual(len(self.spawn_commands), 1)
        self.assertIn("--strictPort", self.spawn_commands[0])
        e2e = next(kwargs for cmd, _, kwargs in self.commands if cmd[1:] == ["run", "test:e2e"])
        self.assertEqual(e2e["env"]["E2E_BASE_URL"], f"http://127.0.0.1:{report['live_ports']['frontend']}")
        self.assertNotEqual(report["ports"]["frontend"], report["ports"]["backend"])
        self.assertEqual(e2e["env"]["npm_config_cache"], str(self.root / "download-cache"))
        self.assertEqual(report["source_head"], "a" * 40)
        self.assertFalse(report["evidence_reused"])
        self.assertEqual(report["verification_scope"], "full-functional")
        self.assertEqual(report["probe_version"], probe.probe_version())
        for stage in ("port_allocation", "install_backend", "install_frontend", "backend_startup", "frontend_startup",
                      "browser_runtime", "e2e", "build", "install_join", "cleanup", "total"):
            self.assertGreaterEqual(report["timings_seconds"][stage], 0)
        self.assertGreaterEqual(report["timings_seconds"]["total"], report["timings_seconds"]["e2e"])

    def test_startup_skips_e2e_build_browser_and_business_scan(self):
        self.fullstack()
        report = self.run_probe(profile="startup", build=False)
        self.browser_check.assert_not_called()
        self.business_scan.assert_not_called()
        self.assertTrue(report["ok"], report["failed_steps"])
        self.assertEqual(report["verification_scope"], "startup-connectivity")
        self.assertEqual(report["verification_profile"], "startup")
        names = [step["step"] for step in report["steps"]]
        for required in ("前端首页可访问且有挂载点", "前端入口脚本可加载",
                         "后端 /api/health 就绪", "前端 -> 后端代理链路通"):
            self.assertIn(required, names)
        self.assertFalse(any("业务接口" in name or "overview" in name for name in names))
        self.assertFalse(any(cmd[1:2] == ["run"] for cmd, _, _ in self.commands))
        self.assertNotIn("browser_runtime", report["timings_seconds"])
        self.assertNotIn("e2e", report["timings_seconds"])
        self.assertNotIn("build", report["timings_seconds"])
        self.assertTrue(report["cleaned_up"])

    def test_failed_final_proxy_does_not_trigger_repair_or_full_test_retry(self):
        self.fullstack()
        original_http = self.http
        health_calls = []

        def broken_proxy(url):
            if url.endswith("/api/health"):
                health_calls.append(url)
                if len(health_calls) > 1:
                    return 502, "proxy unavailable"
            return original_http(url)

        self.http = broken_proxy
        report = self.run_probe(profile="startup", build=False)
        self.assertFalse(report["ok"])
        self.assertTrue(report["cleaned_up"])
        self.assertEqual(len(self.spawn_commands), 2)  # 每个服务只起一次
        self.assertEqual(len(health_calls), 2)  # 后端就绪 + 一次代理请求
        self.assertTrue(any("代理链路" in step["step"] for step in report["failed_steps"]))
        self.browser_check.assert_not_called()
        self.assertFalse(any(cmd[1:2] == ["run"] for cmd, _, _ in self.commands))

    def test_pure_frontend_startup_needs_no_backend_or_browser_assets(self):
        (self.frontend / "tests/functionality.spec.ts").unlink()
        report = self.run_probe(profile="startup", build=False)
        self.assertTrue(report["ok"], report["failed_steps"])
        self.assertEqual(len(self.spawn_commands), 1)
        self.assertFalse(any(cmd[1:] in (["run", "test:e2e"], ["run", "build"])
                             for cmd, _, _ in self.commands))

    def test_cli_defaults_to_startup_without_hidden_build(self):
        with patch.object(sys, "argv", ["probe", "--repo", str(self.repo), "--clean-clone"]), \
             patch.object(probe, "run_clean_clone", return_value=0) as run:
            self.assertEqual(probe.main(), 0)
        args = run.call_args.args[1]
        self.assertEqual(args.profile, "startup")
        self.assertFalse(args.build)
        self.assertFalse(args.skip_e2e)

    def test_cli_startup_rejects_conflicting_full_test_flags(self):
        for flag in ("--build", "--skip-e2e"):
            with self.subTest(flag=flag), \
                 patch.object(sys, "argv", ["probe", "--repo", str(self.repo), flag]), \
                 contextlib.redirect_stderr(io.StringIO()), \
                 patch.object(probe, "run_probe") as run:
                with self.assertRaises(SystemExit) as error:
                    probe.main()
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()

    def test_no_lock_uses_one_npm_install(self):
        (self.frontend / "package-lock.json").unlink()
        self.assertTrue(self.run_probe()["ok"])
        self.assertEqual(sum(cmd[1] == "install" for cmd, _, _ in self.commands), 1)

    def test_second_run_never_reuses_old_e2e_or_build_evidence(self):
        for _ in range(2):
            self.assertTrue(self.run_probe()["ok"])
        # 直接模式保留当前目录已有依赖语义，但功能测试与构建每次都真实调度。
        self.assertEqual(sum(cmd[1] == "ci" for cmd, _, _ in self.commands), 1)
        for script in ("test:e2e", "build"):
            self.assertEqual(sum(cmd[1:] == ["run", script] for cmd, _, _ in self.commands), 2)

    def test_explicit_debug_skip_flags_keep_existing_meaning(self):
        report = self.run_probe(skip_install=True, skip_e2e=True, build=False)
        self.assertTrue(report["ok"])
        self.assertFalse(self.commands)
        self.assertFalse(any(step["step"].endswith("浏览器全功能验收") for step in report["steps"]))

    def test_parallel_installers_complete_before_start_and_are_recorded_once(self):
        self.fullstack()
        original = self.command
        barrier = threading.Barrier(2)

        def delayed_with_separate_events(cmd, **kwargs):
            result = original(cmd, **kwargs)
            name = "pip" if cmd[0].endswith("/pip") else "npm" if cmd[1] == "ci" else None
            if name:
                self.events[name].clear()
                # 目录已出现但安装尚未完成，两路必须并发进入这里。
                barrier.wait(timeout=2)
                time.sleep(0.01)
                self.events[name].set()
            return result

        self.command = delayed_with_separate_events
        report = self.run_probe()
        self.assertTrue(report["ok"], report["failed_steps"])
        self.assertTrue(all(event.is_set() for event in self.events.values()))
        for name in ("后端安装依赖", "前端安装依赖"):
            self.assertEqual(sum(step["step"] == name for step in report["steps"]), 1)
        self.assertEqual(sum(cmd[0].endswith("/pip") for cmd, _, _ in self.commands), 1)

    def test_failed_partial_npm_tree_removed_and_failure_not_lost(self):
        self.npm_code = 1
        report = self.run_probe()
        self.assertFalse(report["ok"])
        self.assertTrue(report["cleaned_up"])
        self.assertFalse((self.frontend / "node_modules").exists())
        failures = [step for step in report["failed_steps"] if step["step"] == "前端安装依赖"]
        self.assertEqual(len(failures), 1)
        self.assertFalse(any(cmd[1:] == ["run", "test:e2e"] for cmd, _, _ in self.commands))

    def test_failed_pip_with_existing_python_binary_is_not_a_pass(self):
        self.fullstack()
        self.pip_code = 1
        report = self.run_probe()
        self.assertFalse(report["ok"])
        self.assertTrue(report["cleaned_up"])
        self.assertFalse((self.backend / ".venv").exists())
        self.assertTrue(any(step["step"] == "后端安装依赖" for step in report["failed_steps"]))

    def test_e2e_failure_is_not_hidden_by_successful_build(self):
        original = self.command

        def fail_e2e(cmd, **kwargs):
            if cmd[1:] == ["run", "test:e2e"]:
                self.commands.append((cmd, kwargs["cwd"], kwargs))
                return subprocess.CompletedProcess(cmd, 1, "", "actual test failed")
            return original(cmd, **kwargs)

        self.command = fail_e2e
        report = self.run_probe()
        self.assertFalse(report["ok"])
        self.assertTrue(report["cleaned_up"])
        self.assertTrue(any(step["step"] == "浏览器全功能验收" for step in report["failed_steps"]))

    def test_clean_clone_never_links_host_native_dependencies(self):
        head = self.commit_fixture()
        (self.frontend / "node_modules").mkdir()
        (self.frontend / "node_modules/native.node").write_bytes(b"host-native")
        (self.backend / ".venv").mkdir(parents=True)
        seen = []

        def inspect(clone, args):
            self.assertFalse((clone / "frontend/node_modules").exists())
            self.assertFalse((clone / "backend/.venv").exists())
            self.assertFalse((clone / "frontend/node_modules").is_symlink())
            seen.append(clone)
            return {"ok": True, "cleaned_up": True, "steps": []}

        result = self.clean_probe(inspect)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["report"]["source_head"], head)
        self.assertTrue(result["report"]["cleaned_up"])
        self.assertFalse(seen[0].exists())
        self.assertTrue((self.frontend / "node_modules/native.node").is_file())

    def test_clean_clone_rejects_uncommitted_business_spec_and_untracked_changes(self):
        self.commit_fixture()
        targets = [self.frontend / "package.json", self.repo / ".s11-spec.json",
                   self.frontend / "untracked-business.ts"]
        for target in targets:
            with self.subTest(target=target.name):
                before = target.read_bytes() if target.exists() else None
                target.write_text("changed business/spec")
                result = self.clean_probe()
                self.assertEqual(result["code"], 2)
                self.assertEqual(result["calls"], 0)
                self.assertFalse(result["report"]["ok"])
                self.assertTrue(result["report"]["cleaned_up"])
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)

    def test_source_mutated_during_clone_probe_invalidates_evidence(self):
        self.commit_fixture()

        def changed(clone, args):
            (self.frontend / "package.json").write_text("changed while running")
            return {"ok": True, "cleaned_up": True, "steps": []}

        result = self.clean_probe(changed)
        self.assertEqual(result["code"], 1)
        self.assertFalse(result["report"]["ok"])
        self.assertTrue(result["report"]["cleaned_up"])

    def test_inner_exception_and_failed_cleanup_cannot_produce_pass(self):
        self.commit_fixture()

        def failed(clone, args):
            raise RuntimeError("mock startup failure")

        result = self.clean_probe(failed)
        self.assertEqual(result["code"], 1)
        self.assertFalse(result["report"]["ok"])
        self.assertFalse(result["report"]["cleaned_up"])

    def test_clone_report_preserves_inner_failure_and_stage_timings(self):
        self.commit_fixture()

        def failed(clone, args):
            return {"ok": False, "cleaned_up": False, "steps": [
                {"step": "浏览器全功能验收", "ok": False}], "timings_seconds": {"e2e": 0.25}}

        result = self.clean_probe(failed)
        self.assertFalse(result["report"]["ok"])
        self.assertFalse(result["report"]["cleaned_up"])
        self.assertEqual(result["report"]["clean_clone_probe"]["timings_seconds"]["e2e"], 0.25)
        for stage in ("source_check", "clone", "probe", "cleanup", "total"):
            self.assertIn(stage, result["report"]["timings_seconds"])

    def test_clean_clone_without_git_head_is_rejected_without_probe(self):
        result = self.clean_probe()
        self.assertEqual(result["code"], 2)
        self.assertEqual(result["calls"], 0)
        self.assertFalse(result["report"]["ok"])
        self.assertTrue(result["report"]["cleaned_up"])

    def test_cleanup_error_cannot_return_success(self):
        # 只 mock 业务探针，不装包、不起服务；临时克隆删除错误必须体现在退出码。
        self.commit_fixture()
        emitted = []
        with patch.object(probe, "run_probe", return_value={"ok": True, "cleaned_up": True, "steps": []}), \
             patch.object(probe.shutil, "rmtree", side_effect=OSError("mock cleanup denied")), \
             patch.object(probe, "emit", side_effect=lambda report, args: emitted.append(report)), \
             patch.object(probe.tempfile, "mkdtemp", return_value=str(self.root / "clone-root")):
            # 指定工作区位于本测试 TemporaryDirectory 中，失败后由 tearDown 真正回收。
            (self.root / "clone-root").mkdir()
            code = probe.run_clean_clone(self.repo, self.args)
        self.assertEqual(code, 1)
        self.assertFalse(emitted[0]["ok"])
        self.assertFalse(emitted[0]["cleaned_up"])

    def test_cli_invalidates_previous_success_before_dispatch(self):
        path = self.root / "report.json"
        path.write_text('{"ok": true, "source_head": "old"}')

        def dispatch(repo, args):
            self.assertFalse(json.loads(path.read_text())["ok"])
            raise RuntimeError("simulated interruption")

        with patch.object(sys, "argv", ["probe", "--repo", str(self.repo), "--clean-clone",
                                         "--json-out", str(path)]), \
             patch.object(probe, "run_clean_clone", side_effect=dispatch):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                probe.main()
        self.assertFalse(json.loads(path.read_text())["ok"])

    def test_emit_is_atomic_and_keeps_stdout_json_compatible(self):
        self.args.json_out = str(self.root / "reports/probe.json")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            probe.emit({"ok": False, "cleaned_up": True}, self.args)
        self.assertEqual(json.loads(stdout.getvalue()), json.loads(Path(self.args.json_out).read_text()))
        self.assertEqual(len(list(Path(self.args.json_out).parent.iterdir())), 1)


class BrowserRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.frontend = Path(self.tmp.name)
        self.cli = self.frontend / "node_modules/.bin/playwright"
        self.cli.parent.mkdir(parents=True)
        self.cli.touch()

    def check(self, codes):
        results = [subprocess.CompletedProcess([], code, "", "failed" if code else "") for code in codes]
        with patch.object(probe.shutil, "which", return_value="/mock/node"), \
             patch.object(probe, "run_command", side_effect=results) as run:
            ok, detail = probe.browser_runtime(self.frontend, 1)
        return ok, detail, run.call_args_list

    def test_matching_runtime_smoke_success_does_not_install(self):
        ok, _, calls = self.check([0])
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("chromium.launch", calls[0].args[0][-1])

    def test_missing_or_wrong_revision_installs_local_cli_once_then_smokes(self):
        ok, _, calls = self.check([1, 0, 0])
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1].args[0], [str(self.cli), "install", "chromium"])
        self.assertFalse(any("npx" in str(call) for call in calls))

    def test_cache_directory_existence_cannot_mask_install_failure(self):
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(self.frontend)}):
            ok, _, calls = self.check([1, 1])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 2)

    def test_install_exit_zero_without_working_browser_is_not_ready(self):
        ok, _, _ = self.check([1, 0, 1])
        self.assertFalse(ok)

    def test_missing_project_cli_does_not_download_via_npx(self):
        self.cli.unlink()
        with patch.object(probe, "run_command") as run:
            ok, _ = probe.browser_runtime(self.frontend, 1)
        self.assertFalse(ok)
        run.assert_not_called()


class PortSafety(unittest.TestCase):
    def test_ephemeral_port_pair_is_distinct_and_kept_reserved(self):
        reservations = probe.reserve_probe_ports()
        try:
            ports = [sock.getsockname()[1] for sock in reservations]
            self.assertEqual(len(set(ports)), 2)
            self.assertNotIn(0, ports)
            for port in ports:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as contender:
                    with self.assertRaises(OSError):
                        contender.bind(("127.0.0.1", port))
        finally:
            for reserved in reservations:
                reserved.close()

    def test_concurrent_probes_never_reserve_the_same_ports(self):
        # 保留所有 socket 到所有并发申请完成；固定 8000/5173 方案会失败。
        with probe.ThreadPoolExecutor(max_workers=8) as pool:
            pairs = list(pool.map(lambda _: probe.reserve_probe_ports(), range(12)))
        try:
            ports = [sock.getsockname()[1] for pair in pairs for sock in pair]
            self.assertEqual(len(set(ports)), 24)
        finally:
            for pair in pairs:
                for reserved in pair:
                    reserved.close()

    def test_free_port_zero_returns_actual_os_port_not_zero(self):
        self.assertGreater(probe.free_port(0), 0)

    def test_foreign_listener_cannot_become_ready_even_if_http_would_pass(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = None
        with patch.object(probe, "listener_owned_by", return_value=(False, "foreign listener")), \
             patch.object(probe, "http_get", return_value=(200, '{"ok": true}')) as http:
            status, detail = probe.service_get(process, 12345, "/api/health", Path("/unused/log"))
        self.assertEqual(status, 0)
        self.assertIn("foreign", detail)
        http.assert_not_called()

    def test_process_exit_is_fast_failure_not_readiness_timeout(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = 1
        with patch.object(probe, "listener_owned_by") as owner, patch.object(probe, "http_get") as http:
            with self.assertRaisesRegex(RuntimeError, "exit=1"):
                probe.service_get(process, 12345, "/api/health", Path("/unused/log"))
        owner.assert_not_called()
        http.assert_not_called()

    def test_process_exit_during_http_cannot_pass_readiness(self):
        process = MagicMock(pid=1234)
        process.poll.side_effect = [None, 1]
        with patch.object(probe, "listener_owned_by", return_value=(True, "ours")), \
             patch.object(probe, "http_get", return_value=(200, '{"ok": true}')):
            with self.assertRaisesRegex(RuntimeError, "exit=1"):
                probe.service_get(process, 12345, "/api/health", Path("/unused/log"))

    def test_listener_replaced_during_http_does_not_pass(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = None
        with patch.object(probe, "listener_owned_by", side_effect=[(True, "ours"), (False, "replaced")]), \
             patch.object(probe, "http_get", return_value=(200, '{"ok": true}')):
            status, detail = probe.service_get(process, 12345, "/api/health", Path("/unused/log"))
        self.assertEqual(status, 0)
        self.assertEqual(detail, "replaced")

    def test_lsof_rejects_any_foreign_process_group(self):
        process = MagicMock(pid=100)
        with patch.object(probe.shutil, "which", return_value="/mock/lsof"), \
             patch.object(probe, "run_command", return_value=subprocess.CompletedProcess([], 0, "101\n102\n", "")), \
             patch.object(probe.os, "getpgid", side_effect=lambda pid: 100 if pid == 101 else 999):
            self.assertFalse(probe.listener_owned_by(process, 12345)[0])

    def test_cannot_observe_listener_fails_closed(self):
        with patch.object(probe.shutil, "which", return_value=None), patch.object(probe.sys, "platform", "darwin"):
            owned, detail = probe.listener_owned_by(MagicMock(pid=100), 12345)
        self.assertFalse(owned)
        self.assertIn("lsof", detail)

    def test_linux_proc_fallback_cross_checks_listener_inode_and_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / "net").mkdir()
            (proc / "net/tcp").write_text(
                "header\n0: 0100007F:3039 00000000:0000 0A 0:0 00:0 0 501 0 123456\n")
            (proc / "101/fd").mkdir(parents=True)
            (proc / "101/fd/3").symlink_to("socket:[123456]")
            def mapped_path(value):
                return proc / str(value).removeprefix("/proc").lstrip("/")

            with patch.object(probe.shutil, "which", return_value=None), \
                 patch.object(probe.sys, "platform", "linux"), \
                 patch.object(probe, "Path", side_effect=mapped_path), \
                 patch.object(probe.os, "getpgid", return_value=100) as group:
                process = MagicMock(pid=100)
                self.assertTrue(probe.listener_owned_by(process, 12345)[0])
                group.return_value = 999
                self.assertFalse(probe.listener_owned_by(process, 12345)[0])
                group.side_effect = PermissionError("mock proc permission denied")
                self.assertFalse(probe.listener_owned_by(process, 12345)[0])


@unittest.skipUnless(os.name == "posix", "探针进程组语义依赖 POSIX")
class ProcessCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def assert_stopped(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
            if not result.stdout.strip() or result.stdout.strip().startswith("Z"):
                return
            time.sleep(0.02)
        self.fail(f"子进程 {pid} 仍在运行")

    def child_script(self, parent_sleep):
        return ("import subprocess,sys,time;from pathlib import Path;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                f"Path({str(self.root / 'child.pid')!r}).write_text(str(p.pid));"
                f"time.sleep({parent_sleep})")

    def test_real_listener_is_owned_only_by_its_own_probe_group(self):
        if not probe.shutil.which("lsof") and not sys.platform.startswith("linux"):
            self.skipTest("需要 lsof 或 Linux /proc")
        reservations = probe.reserve_probe_ports()
        port = reservations[0].getsockname()[1]
        for reserved in reservations:
            reserved.close()
        process, _ = probe.spawn([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                                 self.root, self.root / "http.log")
        foreign = MagicMock(pid=os.getpgrp())
        foreign.poll.return_value = None
        try:
            ok, detail = probe.wait_for(lambda: probe.listener_owned_by(process, port), timeout=5, interval=0.05)
            self.assertTrue(ok, detail)
            self.assertFalse(probe.listener_owned_by(foreign, port)[0])
            self.assertEqual(probe.service_get(process, port, "/", self.root / "http.log")[0], 200)
            self.assertEqual(probe.service_get(foreign, port, "/", self.root / "http.log")[0], 0)
        finally:
            probe.terminate(process, self.root / "http.log")

    def test_command_timeout_kills_descendants_and_preserves_timeout_evidence(self):
        result = probe.run_command([sys.executable, "-c", self.child_script(60)],
                                   cwd=self.root, timeout=0.5)
        self.assertEqual(result.returncode, 124)
        self.assertIn("进程组已终止", result.stderr)
        pid = int((self.root / "child.pid").read_text())
        try:
            self.assert_stopped(pid)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_terminate_cleans_group_even_after_leader_exited(self):
        log = self.root / "service.log"
        process, _ = probe.spawn([sys.executable, "-c", self.child_script(0)], self.root, log)
        process.wait(timeout=3)
        pid = int((self.root / "child.pid").read_text())
        try:
            probe.terminate(process, log)
            self.assert_stopped(pid)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
