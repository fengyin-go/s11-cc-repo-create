#!/usr/bin/env python3
"""批次预检后并行跑「生成 -> 静态验收 -> 真启动 -> 推 GitHub」。

父目录总锁覆盖整个批次；默认失败停止派发，已运行的仓库仍完成。
--keep-going 继续派发剩余 spec。--publish 即授权发布，无须重复确认。
不自动重试整条链路：失败报告保留现场，并给出失败子步骤的重试命令。
--spec 是草稿模式，不默认声称 ready。增强后用 --prepared-repo 复用现有仓库，
跳过生成并强制执行 35 难题容量门禁；所有发布也必须完成该门禁。
最终真启动只跑 startup profile，不补跑浏览器E2E或生产构建。
首次整体测试与至多一次集中修复由主控agent执行并记录；本脚本不自动测试/修复循环。

    python3 scripts/run_pipeline.py --parent /path/to/父目录 \
      --spec /path/to/a.json --spec /path/to/b.json --publish

不加 --publish 时只生成 + 验收，不碰远端（本地预演用）。
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
import github_settings
import scaffold_repo
import check_similarity

SCAFFOLD = SCRIPT_DIR / "scaffold_repo.py"
VERIFY = SCRIPT_DIR / "verify_repo.py"
PROBE = SCRIPT_DIR / "probe_startup.py"
SIMILARITY = SCRIPT_DIR / "check_similarity.py"
PUBLISH = SCRIPT_DIR / "publish_github.py"
CAPACITY = SCRIPT_DIR / "check_prompt_capacity.py"
SPEC_FILE = ".s11-spec.json"


class StepFailed(RuntimeError):
    """某一步没通过：带上可读原因，交给上层决定是停还是继续。"""

    def __init__(self, step: str, detail: str):
        super().__init__(f"{step} 未通过：{detail}")
        self.step = step
        self.detail = detail


class PipelineLimits:
    """只在一个批次内共享；父目录之间无需相互等待。"""

    def __init__(self, probe_jobs: int = 2, publish_jobs: int = 1):
        # scaffold 的 pick_project_id 是扫描/选号，随后才 mkdir，并非原子预留。
        # 因而必须把整个 scaffold 子进程串行化，不仅锁住父进程中的选号。
        self.scaffold = threading.Lock()
        self.probe = threading.BoundedSemaphore(probe_jobs)
        self.publish = threading.BoundedSemaphore(publish_jobs)


def _extract_json(text: str) -> dict:
    """容许 JSON 前有进度日志，但报告必须是对象。"""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            try:
                payload = json.loads("\n".join(lines[idx:]))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    raise StepFailed("解析输出", f"拿不到 JSON 对象：{text[-400:]}")


def _run(step: str, cmd: list[str]) -> dict:
    """只执行一次子步骤；尤其发布失败不能触发重新生成仓库。"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=7200)
    except subprocess.TimeoutExpired:
        raise StepFailed(step, "子步骤超过 7200 秒，已停止；请查看对应日志与网络/依赖状态") from None
    except OSError as exc:
        raise StepFailed(step, f"无法启动子步骤：{exc}") from exc
    combined = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    try:
        # stderr 的警告不应破坏 stdout 中已完整输出的 JSON。
        try:
            payload = _extract_json(result.stdout or "")
        except StepFailed:
            payload = _extract_json(combined)
    except StepFailed as exc:
        detail = (combined.strip()[-400:] if result.returncode else exc.detail)
        raise StepFailed(step, detail or f"退出码 {result.returncode}，无输出") from None
    if result.returncode != 0 or payload.get("ok") is not True:
        if "ok" not in payload:
            raise StepFailed(step, "子脚本输出里没有 ok 字段："
                             + json.dumps(payload, ensure_ascii=False)[:300])
        steps = payload.get("steps")
        failed = ([s for s in steps if isinstance(s, dict) and not s.get("ok")]
                  if isinstance(steps, list) else None)
        reason = (payload.get("reason") or payload.get("failures") or payload.get("issues")
                  or payload.get("problems") or failed or "退出码非 0 或 ok=false")
        raise StepFailed(step, json.dumps(reason, ensure_ascii=False)[:400])
    return payload


def run_one(spec_path: Path, args: argparse.Namespace, index: int, total: int,
            *, limits: PipelineLimits | None = None) -> dict:
    limits = limits or PipelineLimits()
    started = time.monotonic()
    prepared = bool(getattr(args, "prepared_repo", None))
    repo_input = spec_path if prepared else None
    spec_path = spec_path / SPEC_FILE if prepared else spec_path
    tag = f"[{index}/{total}]"
    record = {"spec": str(spec_path), "mode": "prepared" if prepared else "draft", "ready": False,
              "steps": {}, "step_seconds": {}, "wait_seconds": {}}
    active_step = "读取已准备仓库" if prepared else "生成"
    active_command: list[str] = []

    def log(step: str, detail: str) -> None:
        print(f"{tag} {step} ... {detail}", flush=True)

    @contextmanager
    def stage(name: str, cmd: list[str], gate=None, *, required_file: Path | None = None):
        nonlocal active_step, active_command
        active_step, active_command = name, cmd
        waiting = time.monotonic()
        with gate if gate is not None else nullcontext():
            running = time.monotonic()
            record["wait_seconds"][name] = running - waiting
            log(name, "开始")
            try:
                if required_file is not None and not required_file.is_file():
                    raise StepFailed(name, f"缺少容量题位图：{required_file}；请先增强实际数据链路并撰写 topic map")
                yield _run(name, cmd)
                record["steps"][name] = True
            finally:
                record["step_seconds"][name] = time.monotonic() - running

    def skipped(name: str, detail: str) -> None:
        record["steps"][name] = "skipped"
        record["step_seconds"][name] = 0.0
        record["wait_seconds"][name] = 0.0
        log(name, detail)

    try:
        if prepared:
            repo_root = repo_input
            container = str(repo_root.parent)
            record.update({"repo_root": str(repo_root), "project_id": repo_root.name,
                           "container": container})
            active_step = "读取已准备仓库"
            reading = time.monotonic()
            try:
                spec = scaffold_repo.load_spec(spec_path, None, None)
                record.update({"app_name": spec["app_name"], "kind": scaffold_repo.repo_kind(spec),
                               "modules": len(spec["modules"])})
                record["steps"][active_step] = True
                record["wait_seconds"][active_step] = 0.0
            finally:
                record["step_seconds"][active_step] = time.monotonic() - reading
            skipped("生成", "使用已准备仓库，不生成、不初始化或修改 Git 提交")
        else:
            cmd = [sys.executable, str(SCAFFOLD), "--parent", str(args.parent), "--spec", str(spec_path)]
            for option in ("prefix", "frontend", "backend", "owner"):
                value = getattr(args, option)
                if value:
                    cmd += [f"--{option}", value]
            if args.no_github_check:
                cmd.append("--no-github-check")
            cmd.append("--git-init")
            with stage("生成", cmd, limits.scaffold) as scaffold:
                repo_root = Path(scaffold["repo_root"])
                record["repo_root"] = str(repo_root)
                record["project_id"] = scaffold["project"]["project_id"]
                container = scaffold["project"].get("container") or str(repo_root.parent)
                record.update({
                    "container": container, "app_name": scaffold["app_name"],
                    "kind": scaffold.get("kind"), "modules": scaffold["modules"],
                    "lines_written": scaffold["lines_written"],
                })
                # 规范化 spec 由 scaffold 保存；不重复复制、不删除输入。
                log("生成", f"{record['project_id']} {record['app_name']}（{record['kind']}）-> {repo_root}")
        with stage("静态验收", [sys.executable, str(VERIFY), "--repo", str(repo_root), "--json"]) as verify:
            capacity = verify.get("checks", {}).get("capacity") or {}
            record.update({
                "capacity": capacity.get("capacity"), "subject_count": capacity.get("subject_count"),
                "code_lines": (verify.get("checks", {}).get("code_size") or {}).get("code_lines"),
                "verified_kind": verify.get("kind"),
            })
            log("静态验收", f"通过（形态 {verify.get('kind')}，出题容量 {record['capacity']} 条 / "
                f"主体 {record['subject_count']} 个 / {record['code_lines']} 行）")

        with stage("跨仓库查重", [sys.executable, str(SIMILARITY), "--parent", str(args.parent),
                                "--repo", str(repo_root)]) as similarity:
            record["similarity"] = {"nearest": similarity.get("nearest"),
                                    "history_count": similarity.get("history_count")}
            if container:
                Path(container, "similarity-report.json").write_text(
                    json.dumps(similarity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log("跨仓库查重", f"通过（历史 {similarity.get('history_count')} 个）")

        # 静态 CRUD 检查不代表足够支撑 35 道难题。prepared 验收及任何发布都强制过此门禁。
        if prepared or args.publish:
            capacity_cmd = [sys.executable, str(CAPACITY), "--repo", str(repo_root),
                            "--json-out", str(Path(container) / "prompt-capacity-report.json")]
            with stage("出题容量验收", capacity_cmd, limits.probe,
                       required_file=repo_root / ".s11-capacity" / "repo-topic-map.json") as capacity_report:
                record["prompt_capacity"] = capacity_report
                record["capacity_validated"] = True
                log("出题容量验收", "通过（独立容量门禁，target 默认 35）")
        else:
            record["capacity_validated"] = False
            skipped("出题容量验收", "--spec 仅生成并检查草稿；增强数据链路和 topic map 后使用 --prepared-repo")

        if args.skip_probe:
            skipped("真启动验收", "已按 --skip-probe 跳过")
        else:
            probe_cmd = [sys.executable, str(PROBE), "--repo", str(repo_root), "--clean-clone", "--profile", "startup"]
            if args.skip_install:
                probe_cmd.append("--skip-install")
            if args.timeout:
                probe_cmd += ["--timeout", str(args.timeout)]
            with stage("真启动验收", probe_cmd, limits.probe) as probe:
                record["cleaned_up"] = probe.get("cleaned_up")
                if container:
                    Path(container, "probe-report.json").write_text(
                        json.dumps(probe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if record["cleaned_up"] is not True:
                    raise StepFailed("真启动验收", "探针未确认进程清理成功，禁止登记与发布")
                log("真启动验收", "通过（进程清理 True）")

        # registry 自身已有文件锁和原子写，不再把全部仓库的验收串行化。
        with stage("登记业务指纹", [sys.executable, str(SIMILARITY), "--parent", str(args.parent),
                                 "--repo", str(repo_root), "--record", "--project-id",
                                 str(record["project_id"])]) as registered:
            record["registry"] = registered.get("recorded", {}).get("registry")

        if not args.publish:
            skipped("推送 GitHub", "未加 --publish，只做本地预演")
        else:
            publish_cmd = [sys.executable, str(PUBLISH), "--repo", str(repo_root),
                           "--visibility", args.visibility, "--json", "--execute", "--confirm", "PUBLISH"]
            # 保留发布子脚本现有协议；授权已由顶层 --publish 明确给出。
            if args.owner:
                publish_cmd += ["--owner", args.owner]
            if args.remote_url:
                publish_cmd += ["--remote-url", args.remote_url]
            with stage("推送 GitHub", publish_cmd, limits.publish) as published:
                record.update({key: published.get(key) for key in (
                    "repo_url", "visibility_after", "commit", "remote_head_after", "pushed")})
                record["remote_matches_local"] = bool(
                    published.get("commit") and published["commit"] == published.get("remote_head_after"))
                if not record["remote_matches_local"]:
                    raise StepFailed("推送 GitHub", "远端 HEAD 与本地提交不一致")
                if not args.remote_url and record["visibility_after"] != args.visibility:
                    raise StepFailed("推送 GitHub", f"仓库可见性不是要求的 {args.visibility}")
                log("推送 GitHub", f"{record.get('repo_url')} 可见性 {record.get('visibility_after')} 远端=本地 True")
        record["ok"] = True
        record["ready"] = bool(record.get("capacity_validated") and record["steps"].get("真启动验收") is True)
    except (Exception, SystemExit) as exc:
        record.update({
            "ok": False, "failed_step": active_step,
            "reason": exc.detail if isinstance(exc, StepFailed) else f"{type(exc).__name__}: {exc}",
        })
        record["steps"][active_step] = False
        # 没有自动重试。生成失败可能已留下目录，不提供盲目重跑 scaffold 的命令。
        if active_command and active_step not in ("生成", "读取已准备仓库"):
            record["retry_command"] = list(active_command)
            record["recovery"] = (
                "最终启动联通失败，记录阻塞并继续其他仓；不自动进入第二轮修复。命令仅供后续明确返修或中断恢复"
                if active_step == "真启动验收" else
                "检查现有仓库后仅恢复失败子步骤，不重置一测一改预算；不要重跑生成链路，后续步骤仍需完成")
        else:
            record["recovery"] = "先检查现有仓库与 spec，不要盲目重跑生成链路"
        log("失败", f"{active_step}：{record['reason']}")
    finally:
        record["duration_seconds"] = time.monotonic() - started
    return record


def run_batch(specs: list[Path], args: argparse.Namespace) -> tuple[list[dict], list[str]]:
    """有界派发：最多 jobs 个在途任务，不把整个批次提前塞入线程池队列。"""
    limits = PipelineLimits(args.probe_jobs, args.publish_jobs)
    stop = threading.Event()
    results: dict[int, dict] = {}

    def worker(index: int) -> dict:
        started = time.monotonic()
        try:
            result = run_one(specs[index], args, index + 1, len(specs), limits=limits)
            if not isinstance(result, dict) or "ok" not in result:
                raise ValueError("worker 没有返回含 ok 的仓库报告")
        except (Exception, SystemExit) as exc:
            # 防止单个 worker 的意外异常丢失其他已运行仓库的最终结果。
            result = {"spec": str(specs[index] / SPEC_FILE if getattr(args, "prepared_repo", None) else specs[index]),
                      "ok": False, "ready": False,
                      "failed_step": exc.step if isinstance(exc, StepFailed) else "流水线",
                      "reason": exc.detail if isinstance(exc, StepFailed) else f"{type(exc).__name__}: {exc}",
                      "steps": {}, "step_seconds": {}, "wait_seconds": {},
                      "duration_seconds": time.monotonic() - started}
        if not result.get("ok"):
            stop.set()  # worker 返回前就通知调度方，不等主线程处理 future。
        return result

    next_index = 0
    with ThreadPoolExecutor(max_workers=args.jobs, thread_name_prefix="repo-pipeline") as pool:
        pending = {}
        while pending or next_index < len(specs):
            while (next_index < len(specs) and len(pending) < args.jobs
                   and (args.keep_going or not stop.is_set())):
                pending[pool.submit(worker, next_index)] = next_index
                next_index += 1
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                results[index] = future.result()
    return [results[i] for i in sorted(results)], [str(spec) for spec in specs[next_index:]]


def _emit_report(report: dict, args: argparse.Namespace) -> None:
    if args.json_out:
        try:
            path = Path(args.json_out).expanduser().absolute()
            resolved = path.resolve()
            protected = [SKILL_DIR]
            protected += [Path(repo).resolve() for repo in (getattr(args, "prepared_repo", None) or [])]
            protected += [Path(item["repo_root"]).resolve() for item in report.get("repos", [])
                          if item.get("repo_root")]
            inputs = [Path(spec).resolve() for spec in (getattr(args, "spec", None) or [])]
            if path.is_symlink() or any(resolved.is_relative_to(root) for root in protected) or resolved in inputs:
                raise OSError("--json-out 必须位于被验仓库之外，不能覆盖skill、输入spec或跟随符号链接")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            report.update({"ok": False, "summary_write_error": str(exc)})
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _run_locked(specs: list[Path], args: argparse.Namespace, parent: Path, selected_github: dict) -> int:
    started = time.monotonic()
    preflight_reports = []
    spec_path = None
    # 所有输入预检先完成，keep-going 也不能放过批次冲突/无效输入。
    prepared = bool(args.prepared_repo)
    try:
        preflight_history = check_similarity.discover_history(parent)
        history_count = len(preflight_history)
        if prepared:
            if len(set(specs)) != len(specs):
                raise StepFailed("批量预检", "--prepared-repo 不得重复指定同一个仓库")
            # 发布器在 repo.parent 读取证据，同一批不能让两个仓库覆盖同一个报告。
            if len({repo.parent for repo in specs}) != len(specs):
                raise StepFailed("批量预检", "已准备仓库必须各有独立容器目录，避免验收报告相互覆盖")
            for repo in specs:
                if repo == parent or not repo.is_relative_to(parent):
                    raise StepFailed("批量预检", f"已准备仓库必须位于 --parent 之内以便登记：{repo}")
            # 重验收不能被自身历史指纹拦住；其他输入会在下面逐一追加并参与同批对比。
            selected = set(specs)
            preflight_history = [entry for entry in preflight_history
                                 if not entry.get("repo") or (parent / entry["repo"]).resolve() not in selected]
        for item in specs:
            repo = item if prepared else None
            spec_path = repo / SPEC_FILE if prepared else item
            normalized = scaffold_repo.load_spec(spec_path, None if prepared else args.frontend,
                                                 None if prepared else args.backend)
            if prepared:
                result = check_similarity.evaluate(normalized, preflight_history, repo=repo, parent=parent)
            else:
                result = check_similarity.evaluate(normalized, preflight_history)
            preflight_reports.append({"spec": str(spec_path), **result})
            if not result["ok"]:
                raise StepFailed("批量预检", "输入与历史或本批其他输入雷同")
            entry = {"project_id": f"本批输入:{spec_path.name}", "spec": normalized,
                     "fingerprint": result["fingerprint"]}
            if prepared:
                entry.update({"project_id": repo.name, "repo": str(repo.relative_to(parent))})
            preflight_history.append(entry)
    except (Exception, SystemExit) as exc:
        _emit_report({"ok": False, "stage": "批量预检", "spec": str(spec_path) if spec_path else None,
                      "reason": str(exc), "reports": preflight_reports,
                      "duration_seconds": time.monotonic() - started}, args)
        return 2
    preflight_seconds = time.monotonic() - started
    print(f"批量预检通过：{len(specs)} 份 spec，历史仓库 {history_count} 个", flush=True)

    running = time.monotonic()
    repos, not_started = run_batch(specs, args)
    stage_seconds: dict[str, float] = {}
    for repo in repos:
        for stage, seconds in repo.get("step_seconds", {}).items():
            stage_seconds[stage] = stage_seconds.get(stage, 0.0) + seconds
    report = {
        "ok": all(r.get("ok") for r in repos) and len(repos) == len(specs),
        "parent": str(parent), "publish_requested": bool(args.publish),
        "published": bool(args.publish) and len(repos) == len(specs)
                     and all(r.get("ok") and r.get("steps", {}).get("推送 GitHub") is True for r in repos),
        "published_count": sum(r.get("ok") and r.get("steps", {}).get("推送 GitHub") is True for r in repos),
        "github": selected_github,
        "jobs": args.jobs, "probe_jobs": args.probe_jobs, "publish_jobs": args.publish_jobs,
        "specs_total": len(specs), "specs_done": len(repos),
        "mode": "prepared" if prepared else "draft",
        "ready": len(repos) == len(specs) and all(r.get("ready") for r in repos),
        "specs_failed": sum(not r.get("ok") for r in repos),
        "stopped_early": bool(not_started), "not_started": not_started, "repos": repos,
        "preflight_seconds": preflight_seconds, "pipeline_seconds": time.monotonic() - running,
        "stage_seconds": stage_seconds,  # 并行任务的阶段累计时间，不是墙钟时间。
        "duration_seconds": time.monotonic() - started,
    }
    print(f"\n=== 汇总：{sum(1 for r in repos if r.get('ok'))}/{len(specs)} 通过，"
          f"{report['specs_failed']} 失败，{len(not_started)} 未派发；"
          f"总耗时 {report['duration_seconds']:.2f}s ===")
    for r in repos:
        state = "通过" if r.get("ok") else f"未通过（{r.get('failed_step', '?')}）"
        print(f"  {r.get('project_id') or Path(r['spec']).stem:<10} {state:<18} "
              f"{r.get('app_name') or ''} {r.get('repo_url') or ''}")
    _emit_report(report, args)
    return 0 if report["ok"] else 1


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("并发数必须是正整数")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="批次预检后并行生成、验收并发布多个仓库")
    parser.add_argument("--parent", required=True, help="父目录，编号从这里往后排")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--spec", action="append", help="草稿 spec json，可重复传多次；不默认视为 ready")
    inputs.add_argument("--prepared-repo", action="append", help="已增强并提交的仓库根目录，可重复；跳过生成，强制容量门禁")
    parser.add_argument("--jobs", type=_positive_int, default=min(4, os.cpu_count() or 1),
                        help="仓库流水线并发数，默认 min(4, CPU 数)")
    parser.add_argument("--probe-jobs", type=_positive_int, default=2, help="重验收并发上限，默认 2")
    parser.add_argument("--publish-jobs", type=_positive_int, default=1, help="发布并发上限，默认 1")
    parser.add_argument("--prefix", help="项目编号前缀，默认 cc")
    parser.add_argument("--frontend", choices=["vue", "react"])
    parser.add_argument("--backend", choices=["python", "java", "none"], help="none = 纯前端仓库")
    parser.add_argument("--owner", help="GitHub owner，默认读取本技能 GitHub 配置")
    parser.add_argument("--visibility", choices=["public", "private", "internal"], default="public")
    parser.add_argument("--publish", action="store_true", help="授权发布；不加只做本地生成 + 验收")
    parser.add_argument("--confirm", help="兼容旧调用的参数，不再要求 --confirm PUBLISH")
    parser.add_argument("--remote-url", help="非 GitHub 远端（GitLab / 本地裸仓库），跳过可见性校验")
    parser.add_argument("--skip-probe", action="store_true", help="跳过干净克隆真启动，发布时禁止")
    parser.add_argument("--skip-install", action="store_true", help="探针跳过装依赖")
    parser.add_argument("--timeout", type=float, help="探针每步等待上限（秒）")
    parser.add_argument("--no-github-check", action="store_true", help="生成时不查 GitHub 编号占用（离线用）")
    parser.add_argument("--keep-going", action="store_true", help="失败后继续派发；默认停止派发，已运行可完成")
    parser.add_argument("--json-out", help="同时将完整汇总 JSON 写入此路径")
    github_settings.add_arguments(parser)
    args = parser.parse_args()
    selected_github = github_settings.configure_from_args(args)
    if args.publish and args.skip_probe:
        parser.error("发布必须完成干净克隆启动联通验收，禁止同时使用 --publish 和 --skip-probe")
    if args.publish and args.skip_install:
        parser.error("发布必须在干净克隆中实际安装依赖，禁止 --publish 与 --skip-install")
    if args.prepared_repo and (args.frontend or args.backend):
        parser.error("--prepared-repo 使用仓库实际 spec，不允许 --frontend / --backend 覆盖")
    specs = [Path(s).expanduser().resolve() for s in (args.prepared_repo or args.spec)]
    for item in specs:
        if args.prepared_repo:
            if not item.is_dir():
                parser.error(f"已准备仓库不存在：{item}")
        elif not item.is_file():
            parser.error(f"spec 不存在：{item}")
    if args.json_out:
        output = Path(args.json_out).expanduser().resolve()
        protected = [SKILL_DIR, *specs] if args.prepared_repo else [SKILL_DIR]
        if any(output.is_relative_to(root) for root in protected) or (not args.prepared_repo and output in specs):
            parser.error("--json-out 必须位于仓库/skill之外，且不能覆盖输入spec")
    parent = Path(args.parent).expanduser().resolve()
    args.parent = parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_dir = parent / ".s11-registry"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "pipeline.lock").open("a+") as pipeline_lock:
        try:
            fcntl.flock(pipeline_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _emit_report({"ok": False, "stage": "流水线互斥",
                          "reason": f"父目录 {parent} 已有另一个生成流水线在运行，请等待其完成"}, args)
            return 2
        try:
            return _run_locked(specs, args, parent, selected_github)
        finally:
            # 包括预检失败和 worker 全部收尾后才释放；main() 可在同进程安全重入。
            fcntl.flock(pipeline_lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())
