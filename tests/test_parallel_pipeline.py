"""Pipeline-only offline regression tests; every child process and network call is mocked.

    python3 -B -m unittest discover -s tests -p test_parallel_pipeline.py -v
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import wait as futures_wait
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_pipeline as pipeline


class MockChildren:
    """Exercise real _run/recording/limits with local files, never execute a child."""

    def __init__(self, parent: Path):
        self.parent = parent
        self.calls = []
        self.active = Counter()
        self.maximum = Counter()
        self.lock = threading.Lock()
        self.responses = {}
        self.barriers = {}
        self.barrier_counts = Counter()
        self.maps = True

    def __call__(self, cmd, **kwargs):
        script = Path(cmd[1]).name
        step = {
            "scaffold_repo.py": "生成", "verify_repo.py": "静态验收",
            "probe_startup.py": "真启动验收", "check_prompt_capacity.py": "出题容量验收",
            "publish_github.py": "推送 GitHub", "check_similarity.py": "跨仓库查重",
        }[script]  # Any unexpected command fails instead of leaking a real subprocess.
        if "--record" in cmd:
            step = "登记业务指纹"
        heavy = step in ("真启动验收", "出题容量验收")
        with self.lock:
            self.calls.append((step, list(cmd)))
            self.active[step] += 1
            self.maximum[step] = max(self.maximum[step], self.active[step])
            if heavy:
                self.active["heavy"] += 1
                self.maximum["heavy"] = max(self.maximum["heavy"], self.active["heavy"])
            barrier = self.barriers.get(step)
            self.barrier_counts[step] += 1
            use_barrier = barrier and self.barrier_counts[step] <= barrier.parties
        try:
            if use_barrier:
                barrier.wait(timeout=5)
            time.sleep(0.005)
            if step in self.responses:
                payload = self.responses[step]
                if isinstance(payload, BaseException):
                    raise payload
            elif step == "生成":
                spec = json.loads(Path(cmd[cmd.index("--spec") + 1]).read_text())
                # Use the real offline allocator: locking must prevent duplicate allocations.
                project = pipeline.scaffold_repo.pick_project_id(
                    self.parent, "cc", explicit_number=None, owner=None, check_github=False)
                root = self.parent / project["project_id"] / project["project_id"]
                root.mkdir(parents=True, exist_ok=False)
                (root / pipeline.SPEC_FILE).write_text(json.dumps(spec))
                if self.maps:
                    (root / ".s11-capacity").mkdir()
                    (root / ".s11-capacity/repo-topic-map.json").write_text('{"topics": ["mock"]}')
                project["container"] = str(root.parent)
                payload = {"ok": True, "project": project, "repo_root": str(root),
                           "app_name": spec["app_name"], "kind": "fullstack", "modules": 5,
                           "lines_written": 3000}
            elif step == "静态验收":
                payload = {"ok": True, "kind": "fullstack", "checks": {
                    "capacity": {"capacity": 40, "subject_count": 5}, "code_size": {"code_lines": 3000}}}
            elif step == "真启动验收":
                payload = {"ok": True, "cleaned_up": True, "source_head": "abc"}
            elif step == "出题容量验收":
                payload = {"ok": True, "target": 35, "capacity": 35}
            elif step == "推送 GitHub":
                payload = {"ok": True, "commit": "abc", "remote_head_after": "abc", "pushed": True,
                           "repo_url": "mock-owner/mock-repo", "visibility_after": "public"}
            else:
                payload = {"ok": True, "history_count": 0, "recorded": {"registry": "mock-registry"}}
            if "--json-out" in cmd:
                Path(cmd[cmd.index("--json-out") + 1]).write_text(json.dumps(payload))
            return subprocess.CompletedProcess(cmd, 0, "child log\n" + json.dumps(payload), "mock warning\n")
        finally:
            with self.lock:
                self.active[step] -= 1
                if heavy:
                    self.active["heavy"] -= 1

    def count(self, step):
        return sum(name == step for name, _ in self.calls)


class ParallelPipelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="s11-parallel-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.parent = self.root / "projects"
        self.parent.mkdir()
        self.children = MockChildren(self.parent)
        self.history = []
        self.comparisons = []
        self.start_patch(pipeline.subprocess, "run", side_effect=self.children)
        self.start_patch(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        self.start_patch(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.start_patch(pipeline.github_settings, "configure_from_args",
                         return_value={"owner": "mock-owner", "account": "mock-owner"})
        self.start_patch(pipeline.scaffold_repo, "load_spec", side_effect=self.load_spec)
        self.start_patch(pipeline.check_similarity, "discover_history", side_effect=lambda _: list(self.history))
        self.start_patch(pipeline.check_similarity, "evaluate", side_effect=self.evaluate)
        self.output = io.StringIO()
        stdout = redirect_stdout(self.output)
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def start_patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    @staticmethod
    def load_spec(path, frontend, backend):
        return json.loads(Path(path).read_text())

    def evaluate(self, spec, history, **kwargs):
        self.comparisons.append((spec, list(history), kwargs))
        return {"ok": not any(entry.get("spec", {}).get("id") == spec["id"] for entry in history),
                "fingerprint": str(spec["id"]), "history_count": len(history)}

    def spec(self, number):
        path = self.root / f"spec{number}.json"
        path.write_text(json.dumps({"id": number, "app_name": f"mock{number}", "backend": "python",
                                    "modules": [{"key": "mock"}]}))
        return path

    def prepared(self, number, *, has_map=True):
        root = self.parent / f"cc{10000 + number}" / f"cc{10000 + number}"
        root.mkdir(parents=True)
        (root / pipeline.SPEC_FILE).write_text(self.spec(number).read_text())
        (root / ".git").mkdir()
        if has_map:
            (root / ".s11-capacity").mkdir()
            (root / ".s11-capacity/repo-topic-map.json").write_text('{"topics": ["mock"]}')
        return root

    def args(self, **overrides):
        values = dict(parent=self.parent, jobs=4, probe_jobs=2, publish_jobs=1,
                      prepared_repo=None, prefix=None, frontend=None, backend=None, owner=None,
                      no_github_check=True, skip_probe=False, skip_install=False, timeout=None,
                      publish=False, visibility="public", remote_url=None, keep_going=False,
                      json_out=None)
        values.update(overrides)
        return argparse.Namespace(**values)

    def cli(self, *flags):
        report = self.root / "result.json"
        with patch.object(sys, "argv", ["run_pipeline.py", "--parent", str(self.parent),
                                        "--json-out", str(report), *map(str, flags)]):
            code = pipeline.main()
        return code, json.loads(report.read_text())

    def assert_released(self):
        with (self.parent / ".s11-registry/pipeline.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def test_cpu_defaults_and_stage_defaults(self):
        for cpu, jobs in [(32, 4), (2, 2), (None, 1)]:
            with self.subTest(cpu=cpu), patch.object(pipeline.os, "cpu_count", return_value=cpu):
                code, report = self.cli("--spec", self.spec(1), "--skip-probe")
            self.assertEqual(code, 0)
            self.assertEqual((report["jobs"], report["probe_jobs"], report["publish_jobs"]), (jobs, 2, 1))
            self.assertFalse(report["ready"])
            self.assert_released()

    def test_parallel_repos_with_serial_scaffold_and_default_stage_limits(self):
        self.children.barriers["静态验收"] = threading.Barrier(4)
        self.children.barriers["真启动验收"] = threading.Barrier(2)
        specs = [self.spec(i) for i in range(8)]
        records, skipped = pipeline.run_batch(specs, self.args(publish=True))
        self.assertEqual(skipped, [])
        self.assertTrue(all(r["ok"] for r in records), records)
        self.assertEqual(len({r["project_id"] for r in records}), 8)
        self.assertEqual([r["spec"] for r in records], list(map(str, specs)))
        self.assertEqual(self.children.maximum["生成"], 1)
        self.assertEqual(self.children.maximum["静态验收"], 4)
        self.assertEqual(self.children.maximum["真启动验收"], 2)
        self.assertLessEqual(self.children.maximum["heavy"], 2)
        self.assertEqual(self.children.maximum["推送 GitHub"], 1)
        self.assertTrue(any(r["wait_seconds"]["生成"] > 0.001 for r in records))

    def test_custom_probe_and_publish_limits_for_prepared(self):
        repos = [self.prepared(i + 1) for i in range(4)]
        self.children.barriers["静态验收"] = threading.Barrier(4)
        self.children.barriers["推送 GitHub"] = threading.Barrier(2)
        records, _ = pipeline.run_batch(repos, self.args(
            prepared_repo=repos, publish=True, probe_jobs=1, publish_jobs=2))
        self.assertTrue(all(r["ok"] for r in records), records)
        self.assertEqual(self.children.maximum["heavy"], 1)
        self.assertEqual(self.children.maximum["推送 GitHub"], 2)
        self.assertEqual(self.children.count("生成"), 0)

    def test_final_pipeline_dispatches_only_startup_not_full_tests(self):
        repo = self.prepared(42)
        records, _ = pipeline.run_batch([repo], self.args(prepared_repo=[repo]))
        self.assertTrue(records[0]["ok"], records)
        commands = [cmd for name, cmd in self.children.calls if name == "真启动验收"]
        self.assertEqual(len(commands), 1)
        self.assertIn("--clean-clone", commands[0])
        self.assertEqual(commands[0][commands[0].index("--profile") + 1], "startup")
        self.assertNotIn("--build", commands[0])
        self.assertNotIn("--skip-e2e", commands[0])

    def test_fail_fast_stops_new_dispatch_but_drains_running_repo(self):
        specs = [self.spec(i) for i in range(6)]
        second_started, release_second = threading.Event(), threading.Event()
        calls = []

        def worker(spec, args, index, total, **kwargs):
            calls.append(spec)
            if index == 1:
                if not second_started.wait(5):
                    raise AssertionError("second worker did not start")
                return {"spec": str(spec), "ok": False, "failed_step": "静态验收"}
            second_started.set()
            if not release_second.wait(5):
                raise AssertionError("scheduler did not drain failure")
            return {"spec": str(spec), "ok": True}

        def observe_failure(fs, **kwargs):
            done, pending = futures_wait(fs, **kwargs)
            if any(not future.result()["ok"] for future in done):
                release_second.set()
            return done, pending

        with patch.object(pipeline, "run_one", side_effect=worker), patch.object(
                pipeline, "wait", side_effect=observe_failure):
            records, skipped = pipeline.run_batch(specs, self.args(jobs=2))
        self.assertEqual(set(calls), set(specs[:2]))
        self.assertEqual([r["ok"] for r in records], [False, True])
        self.assertEqual(skipped, list(map(str, specs[2:])))

    def test_keep_going_collects_unexpected_worker_errors_and_preserves_order(self):
        specs = [self.spec(i) for i in range(5)]

        def worker(spec, args, index, total, **kwargs):
            if index == 1:
                raise pipeline.StepFailed("静态验收", "bad")
            if index == 2:
                raise ValueError("broken worker")
            return {"spec": str(spec), "ok": True}

        with patch.object(pipeline, "run_one", side_effect=worker):
            records, skipped = pipeline.run_batch(specs, self.args(keep_going=True, jobs=2))
        self.assertEqual(skipped, [])
        self.assertEqual([r["spec"] for r in records], list(map(str, specs)))
        self.assertEqual([r["ok"] for r in records], [False, False, True, True, True])
        self.assertEqual(records[0]["failed_step"], "静态验收")
        self.assertIn("ValueError", records[1]["reason"])

    def test_non_raising_failure_also_stops_dispatch_and_is_reported(self):
        self.children.responses["静态验收"] = {"ok": False, "reason": "bad static check"}
        code, report = self.cli("--spec", self.spec(1), "--spec", self.spec(2), "--jobs", 1)
        self.assertEqual(code, 1)
        self.assertEqual((report["specs_done"], report["specs_failed"]), (1, 1))
        self.assertTrue(report["stopped_early"])
        self.assertEqual(report["not_started"], [str(self.spec(2))])
        record = report["repos"][0]
        self.assertIn("repo_root", record)
        self.assertTrue(record["steps"]["生成"])
        self.assertFalse(record["steps"]["静态验收"])
        self.assertGreater(record["step_seconds"]["静态验收"], 0)
        self.assertGreaterEqual(record["duration_seconds"], sum(record["step_seconds"].values()))
        self.assertEqual(report["stage_seconds"], record["step_seconds"])
        self.assertGreater(report["preflight_seconds"], 0)
        self.assert_released()

    def test_publish_does_not_require_confirmation_and_legacy_argument_is_accepted(self):
        for extra in [[], ["--confirm", "PUBLISH"]]:
            with self.subTest(extra=extra):
                code, report = self.cli("--spec", self.spec(1), "--publish", *extra)
                self.assertEqual(code, 0)
                self.assertTrue(report["repos"][0]["ready"])
        commands = [cmd for step, cmd in self.children.calls if step == "推送 GitHub"]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(cmd[cmd.index("--confirm") + 1] == "PUBLISH" for cmd in commands))

    def test_publish_failure_only_offers_publish_retry_not_full_pipeline(self):
        self.children.responses["推送 GitHub"] = {"ok": False, "reason": "temporary publish outage"}
        code, report = self.cli("--spec", self.spec(1), "--publish")
        self.assertEqual(code, 1)
        record = report["repos"][0]
        self.assertEqual(record["failed_step"], "推送 GitHub")
        self.assertTrue(record["steps"]["登记业务指纹"])
        self.assertEqual(Path(record["retry_command"][1]), pipeline.PUBLISH)
        self.assertEqual(record["retry_command"][record["retry_command"].index("--repo") + 1], record["repo_root"])
        for step in ("生成", "静态验收", "真启动验收", "出题容量验收", "推送 GitHub"):
            self.assertEqual(self.children.count(step), 1, step)
        # Executing the suggested retry cannot call scaffold, verify or probe again.
        self.children.responses.pop("推送 GitHub")
        pipeline._run("推送 GitHub", record["retry_command"])
        self.assertEqual(self.children.count("推送 GitHub"), 2)
        self.assertEqual(self.children.count("生成"), 1)

    def test_scaffold_failure_does_not_offer_blind_regeneration_retry(self):
        self.children.responses["生成"] = OSError("write failure")
        record = pipeline.run_one(self.spec(1), self.args(), 1, 1)
        self.assertFalse(record["ok"])
        self.assertEqual(record["failed_step"], "生成")
        self.assertNotIn("retry_command", record)
        self.assertEqual(self.children.count("生成"), 1)

    def test_draft_is_not_ready_even_when_static_capacity_is_high(self):
        code, report = self.cli("--spec", self.spec(1))
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "draft")
        self.assertFalse(report["ready"])
        record = report["repos"][0]
        self.assertFalse(record["ready"])
        self.assertEqual(record["steps"]["出题容量验收"], "skipped")
        self.assertFalse(record["capacity_validated"])
        self.assertEqual(self.children.count("出题容量验收"), 0)
        self.assertEqual(self.children.count("推送 GitHub"), 0)

    def test_prepared_skips_generation_uses_saved_spec_and_requires_capacity(self):
        repos = [self.prepared(1), self.prepared(2)]
        code, report = self.cli("--prepared-repo", repos[0], "--prepared-repo", repos[1])
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "prepared")
        self.assertTrue(report["ready"])
        self.assertEqual(self.children.count("生成"), 0)
        for record, repo in zip(report["repos"], repos):
            self.assertEqual(record["repo_root"], str(repo))
            self.assertEqual(record["app_name"], json.loads((repo / pipeline.SPEC_FILE).read_text())["app_name"])
            self.assertEqual(record["steps"]["生成"], "skipped")
            self.assertTrue(record["steps"]["出题容量验收"])
            self.assertTrue((repo.parent / "prompt-capacity-report.json").is_file())
        for step, cmd in self.children.calls:
            if step == "出题容量验收":
                self.assertEqual(Path(cmd[1]), pipeline.CAPACITY)
                self.assertNotIn("--target", cmd)  # Gate's default target is authoritative.
                repo = Path(cmd[cmd.index("--repo") + 1])
                self.assertEqual(Path(cmd[cmd.index("--json-out") + 1]), repo.parent / "prompt-capacity-report.json")

    def test_missing_topic_map_is_capacity_failure_not_empty_success(self):
        repo = self.prepared(1, has_map=False)
        code, report = self.cli("--prepared-repo", repo, "--publish")
        self.assertEqual(code, 1)
        record = report["repos"][0]
        self.assertEqual(record["failed_step"], "出题容量验收")
        self.assertIn("repo-topic-map.json", record["reason"])
        self.assertFalse(record["ready"])
        self.assertEqual(record["retry_command"][1], str(pipeline.CAPACITY))
        for step in ("生成", "真启动验收", "登记业务指纹", "推送 GitHub"):
            self.assertEqual(self.children.count(step), 0)

    def test_capacity_rejection_stops_prepared_batch_and_never_publishes(self):
        repos = [self.prepared(1), self.prepared(2)]
        self.children.responses["出题容量验收"] = {"ok": False, "reason": "only 12 hard topics"}
        code, report = self.cli("--prepared-repo", repos[0], "--prepared-repo", repos[1],
                                "--jobs", 1, "--publish")
        self.assertEqual(code, 1)
        self.assertEqual(report["specs_done"], 1)
        self.assertEqual(report["not_started"], [str(repos[1])])
        self.assertEqual(report["repos"][0]["failed_step"], "出题容量验收")
        self.assertEqual(self.children.count("出题容量验收"), 1)
        self.assertEqual(self.children.count("推送 GitHub"), 0)

    def test_capacity_missing_script_is_failure_without_retry(self):
        repo = self.prepared(1)
        self.children.responses["出题容量验收"] = FileNotFoundError("gate unavailable")
        code, report = self.cli("--prepared-repo", repo)
        self.assertEqual(code, 1)
        self.assertEqual(report["repos"][0]["failed_step"], "出题容量验收")
        self.assertEqual(self.children.count("出题容量验收"), 1)
        self.assertEqual(self.children.count("登记业务指纹"), 0)

    def test_spec_publish_cannot_bypass_capacity_map_requirement(self):
        self.children.maps = False
        code, report = self.cli("--spec", self.spec(1), "--publish")
        self.assertEqual(code, 1)
        self.assertEqual(report["repos"][0]["failed_step"], "出题容量验收")
        self.assertFalse(report["ready"])
        self.assertEqual(self.children.count("推送 GitHub"), 0)

    def test_skip_probe_cannot_skip_prepared_capacity(self):
        repo = self.prepared(1)
        code, report = self.cli("--prepared-repo", repo, "--skip-probe")
        self.assertEqual(code, 0)
        self.assertEqual(self.children.count("出题容量验收"), 1)
        self.assertFalse(report["ready"])
        self.assertEqual(self.children.count("真启动验收"), 0)

    def test_preflight_checks_entire_batch_before_any_child_even_keep_going(self):
        for extra in [[], ["--keep-going"]]:
            with self.subTest(extra=extra):
                code, report = self.cli("--spec", self.spec(1), "--spec", self.spec(1), *extra)
                self.assertEqual(code, 2)
                self.assertEqual(report["stage"], "批量预检")
                self.assertEqual(len(report["reports"]), 2)
                self.assertEqual(self.children.calls, [])
                self.assert_released()

    def test_preflight_handles_invalid_spec_or_broken_registry_without_workers(self):
        for target, name in [(pipeline.scaffold_repo, "load_spec"),
                             (pipeline.check_similarity, "discover_history")]:
            with self.subTest(name=name), patch.object(target, name, side_effect=SystemExit("bad input")):
                code, report = self.cli("--spec", self.spec(1), "--keep-going")
            self.assertEqual(code, 2)
            self.assertIn("bad input", report["reason"])
            self.assertEqual(self.children.calls, [])
            self.assert_released()

    def test_prepared_preflight_excludes_self_history_but_checks_other_prepared_sources(self):
        repos = [self.prepared(1), self.prepared(2)]
        self.history = [{"repo": str(repo.relative_to(self.parent)), "project_id": repo.name,
                         "spec": json.loads((repo / pipeline.SPEC_FILE).read_text())} for repo in repos]
        code, report = self.cli("--prepared-repo", repos[0], "--prepared-repo", repos[1])
        self.assertEqual(code, 0, report)
        self.assertEqual(self.comparisons[0][1], [])
        _, previous, kwargs = self.comparisons[1]
        self.assertEqual([entry["repo"] for entry in previous], [str(repos[0].relative_to(self.parent))])
        self.assertEqual(kwargs, {"repo": repos[1], "parent": self.parent})

    def test_prepared_preflight_rejects_business_duplicates_without_any_generation(self):
        repos = [self.prepared(1), self.prepared(2)]
        (repos[1] / pipeline.SPEC_FILE).write_text((repos[0] / pipeline.SPEC_FILE).read_text())
        code, report = self.cli("--prepared-repo", repos[0], "--prepared-repo", repos[1])
        self.assertEqual(code, 2)
        self.assertEqual(report["stage"], "批量预检")
        self.assertEqual(self.children.calls, [])

    def test_prepared_paths_must_not_alias_or_escape_parent_or_share_evidence(self):
        repo = self.prepared(1)
        outside = self.root / "outside"
        outside.mkdir()
        flat_a, flat_b = self.parent / "flat-a", self.parent / "flat-b"
        flat_a.mkdir()
        flat_b.mkdir()
        for inputs in [(repo, repo), (outside,), (flat_a, flat_b)]:
            flags = [part for item in inputs for part in ("--prepared-repo", item)]
            with self.subTest(inputs=inputs):
                code, report = self.cli(*flags)
                self.assertEqual(code, 2)
                self.assertEqual(report["stage"], "批量预检")
        self.assertEqual(self.children.calls, [])

    def test_parent_lock_covers_preflight_workers_and_summary_then_is_released(self):
        original_emit = pipeline._emit_report
        original_run = self.children
        original_discover = pipeline.check_similarity.discover_history.side_effect

        def assert_locked():
            with (self.parent / ".s11-registry/pipeline.lock").open("a+") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def child(*args, **kwargs):
            assert_locked()
            return original_run(*args, **kwargs)

        def discover(parent):
            assert_locked()
            return original_discover(parent)

        def emit(*args):
            assert_locked()
            return original_emit(*args)

        with patch.object(pipeline.subprocess, "run", side_effect=child), patch.object(
                pipeline.check_similarity, "discover_history", side_effect=discover), patch.object(
                pipeline, "_emit_report", side_effect=emit):
            self.assertEqual(self.cli("--spec", self.spec(1))[0], 0)
        self.assert_released()

    def test_second_pipeline_is_rejected_before_preflight(self):
        directory = self.parent / ".s11-registry"
        directory.mkdir()
        with (directory / "pipeline.lock").open("a+") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, report = self.cli("--spec", self.spec(1))
        self.assertEqual(code, 2)
        self.assertEqual(report["stage"], "流水线互斥")
        self.assertEqual(self.comparisons, [])
        self.assertEqual(self.children.calls, [])

    def test_invalid_parallelism_and_input_combinations_are_parser_errors(self):
        spec, repo = self.spec(1), self.prepared(1)
        cases = [["--spec", str(spec), flag, value]
                 for flag in ("--jobs", "--probe-jobs", "--publish-jobs") for value in ("0", "-1", "bad")]
        cases += [[], ["--spec", str(spec), "--prepared-repo", str(repo)],
                  ["--prepared-repo", str(repo), "--frontend", "react"],
                  ["--spec", str(spec), "--publish", "--skip-probe"]]
        for flags in cases:
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), patch.object(
                    sys, "argv", ["run_pipeline.py", "--parent", str(self.parent), *flags]):
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
                self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.children.calls, [])

    def test_cleanup_failure_and_remote_mismatch_are_real_failed_stages(self):
        for step, payload in [("真启动验收", {"ok": True, "cleaned_up": False}),
                              ("推送 GitHub", {"ok": True, "commit": "abc", "remote_head_after": "other"})]:
            self.children.responses = {step: payload}
            record = pipeline.run_one(self.spec(1), self.args(publish=True), 1, 1)
            self.assertFalse(record["ok"])
            self.assertEqual(record["failed_step"], step)
            self.assertFalse(record["steps"][step])
            self.assertIn(step, record["step_seconds"])
            self.assertFalse(record["ready"])

    def test_child_errors_are_stage_labeled_and_not_automatically_retried(self):
        cases = [subprocess.TimeoutExpired(["fake"], 7200), FileNotFoundError("missing"),
                 subprocess.CompletedProcess([], 2, "", "child exploded"),
                 subprocess.CompletedProcess([], 0, "not JSON", ""),
                 subprocess.CompletedProcess([], 1, '{"ok":true}', ""),
                 subprocess.CompletedProcess([], 0, '{"ok":false,"steps":{"x":false}}', ""),
                 subprocess.CompletedProcess([], 0, '{"capacity":35}', ""),
                 subprocess.CompletedProcess([], 0, '{"ok":"true"}', "")]
        for result in cases:
            kwargs = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
            with self.subTest(result=result), patch.object(pipeline.subprocess, "run", **kwargs) as child:
                with self.assertRaises(pipeline.StepFailed) as raised:
                    pipeline._run("出题容量验收", ["python", "fake"])
                self.assertEqual(raised.exception.step, "出题容量验收")
                self.assertEqual(child.call_count, 1)

    def test_capacity_issues_survive_summary_and_keep_going_checks_all_prepared(self):
        repos = [self.prepared(1), self.prepared(2)]
        self.children.responses["出题容量验收"] = {"ok": False, "issues": ["qualified_plan_count below target 35"]}
        code, report = self.cli("--prepared-repo", repos[0], "--prepared-repo", repos[1],
                                "--publish", "--keep-going", "--jobs", 1)
        self.assertEqual(code, 1)
        self.assertEqual(report["specs_done"], 2)
        self.assertEqual(report["specs_failed"], 2)
        self.assertFalse(report["stopped_early"])
        self.assertTrue(all("qualified_plan_count" in record["reason"] for record in report["repos"]))
        self.assertEqual(self.children.count("推送 GitHub"), 0)
        self.assertEqual(self.children.count("生成"), 0)

    def test_prepared_without_saved_spec_fails_whole_preflight(self):
        repos = [self.prepared(1), self.prepared(2)]
        (repos[1] / pipeline.SPEC_FILE).unlink()
        code, report = self.cli("--prepared-repo", repos[0], "--prepared-repo", repos[1])
        self.assertEqual(code, 2)
        self.assertEqual(report["stage"], "批量预检")
        self.assertIn(pipeline.SPEC_FILE, report["reason"])
        self.assertEqual(self.children.calls, [])

    def test_prepared_validation_does_not_modify_existing_repository(self):
        repo = self.prepared(1)
        (repo / "business.py").write_text("# agent-prepared business logic\n")
        before = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
        self.assertEqual(self.cli("--prepared-repo", repo)[0], 0)
        after = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.children.count("生成"), 0)

    def test_prepared_still_checks_unselected_history(self):
        repo = self.prepared(1)
        self.history = [{"repo": "other-container/other-repo", "project_id": "other-repo",
                         "spec": json.loads((repo / pipeline.SPEC_FILE).read_text())}]
        code, report = self.cli("--prepared-repo", repo)
        self.assertEqual(code, 2)
        self.assertEqual(report["stage"], "批量预检")
        self.assertEqual(len(self.comparisons[0][1]), 1)
        self.assertEqual(self.children.calls, [])

    def test_malformed_worker_result_cannot_lose_batch_summary(self):
        with patch.object(pipeline, "run_one", return_value=None):
            code, report = self.cli("--spec", self.spec(1), "--jobs", 1)
        self.assertEqual(code, 1)
        self.assertEqual(report["specs_failed"], 1)
        self.assertIn("worker", report["repos"][0]["reason"])
        self.assert_released()

    def test_summary_write_error_retains_completed_records_and_releases_lock(self):
        report_directory = self.root / "not-a-file"
        report_directory.mkdir()
        with patch.object(sys, "argv", ["run_pipeline.py", "--parent", str(self.parent),
                                        "--spec", str(self.spec(1)), "--json-out", str(report_directory)]):
            code = pipeline.main()
        self.assertEqual(code, 1)
        self.assertIn('"summary_write_error"', self.output.getvalue())
        self.assertIn('"specs_done": 1', self.output.getvalue())
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
