#!/usr/bin/env python3
"""把准备好的仓库推到远端，默认是 GitHub 的 main 分支（其它 git 远端用 --remote-url）。

默认 dry-run：只打印「会建什么仓库、会推哪个提交、会跑哪几条命令」，不碰远端。
真推要显式加 `--execute`；--confirm 仅保留旧调用兼容。

推之前会拦这几件事（任一条不过就停，不做强推）：

  1. 不是 git 仓库 / 当前分支不是 main / 工作区有未提交改动；
  2. 静态验收没过（`verify_repo.py`，s11正式发布不可跳过）；
  3. 远端 main 上已经有本地没有的提交（分叉）——这种必须人工处理；
  4. 推完回读远端 SHA，和本地 HEAD 对不上就报错；
  5. GitHub 同名仓必须有本仓归属记录且匹配真实 ID，空仓也不能接管。

用法：

    python3 publish_github.py --repo /path/to/repo                       # 看计划
    python3 publish_github.py --repo /path/to/repo --execute
    python3 publish_github.py --repo /path/to/repo --owner my-org --name my-repo --visibility private \
        --execute
    python3 publish_github.py --repo /path/to/repo --remote-url git@gitlab.example.com:group/repo.git \
        --execute

token 只以一次性 `git -c http.extraHeader=...` 传参，不写进 .git/config，也不打印。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import shlex
from urllib.parse import urlparse
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import verify_repo as verifier  # noqa: E402
import repo_rules as rules  # noqa: E402
import github_settings
import check_similarity
import check_prompt_capacity

DEFAULT_BRANCH = "main"


def _redact_github_text(text: str, token: str = "") -> str:
    for secret in (token, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")):
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)", "***", text)
    return re.sub(r"(?i)(authorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)[^\s,;]+", r"\1***", text)


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = github_settings.auth_env() if Path(command[0]).name == "gh" else None
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env)
    if env is not None:
        # Scrub the actual selected credential before it can reach any report,
        # even when it came from gh's credential store rather than os.environ.
        token = env.get("GH_TOKEN", "")
        result.stdout = _redact_github_text(result.stdout, token)
        result.stderr = _redact_github_text(result.stderr, token)
    return result


def redact(command: list[str]) -> list[str]:
    """把命令行里的 token 洗掉再打印。"""
    safe: list[str] = []
    for item in command:
        if "Authorization: Basic" in item:
            safe.append("http.https://github.com/.extraHeader=Authorization: Basic ***")
        else:
            safe.append(item)
    return safe


def gh_binary() -> str:
    return shutil.which("gh") or "gh"


def github_token() -> str:
    return github_settings.auth_env()['GH_TOKEN']


def auth_args(url: str) -> list[str]:
    """远端是 github.com 时，用一次性 header 认证；其它远端交给本机 git 凭据。"""
    if "github.com" not in url:
        return []
    token = github_token()
    if not token:
        return []
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return [
        "-c", "credential.helper=",
        "-c", f"http.https://github.com/.extraHeader=Authorization: Basic {encoded}",
    ]


def git(args: list[str], *, cwd: Path, url: str = "") -> subprocess.CompletedProcess[str]:
    # 首次请求就使用所选账号，避免先用全局默认账号写入。
    return run(["git", *auth_args(url), *args], cwd=cwd)


def local_head(repo: Path, branch: str) -> str:
    result = run(["git", "rev-parse", branch], cwd=repo)
    if result.returncode != 0:
        raise SystemExit(f"本地没有分支 {branch}：{result.stderr.strip()}")
    return result.stdout.strip()


def remote_head(url: str, branch: str, repo: Path, *, allow_missing: bool = False) -> str:
    result = git(["ls-remote", url, f"refs/heads/{branch}"], cwd=repo, url=url)
    if result.returncode != 0:
        if allow_missing:
            return ""
        message = result.stderr.strip() or result.stdout.strip() or "ls-remote failed"
        raise SystemExit(f"读不到远端分支列表：{message}")
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}":
            return sha.strip()
    return ""


def repo_exists(full_name: str) -> bool:
    return run([gh_binary(), "repo", "view", full_name]).returncode == 0


class OwnershipError(ValueError):
    """Cannot establish ownership; preserve safe diagnostics, never auto-retry."""

    def __init__(self, message: str, *, github_error: dict | None = None):
        super().__init__(message)
        self.github_error = github_error


def _github_failure(stage: str, result=None, error: OSError | None = None) -> dict:
    if result is None:
        return {"stage": stage, "exit": None, "http_status": None,
                "diagnostic": _redact_github_text(str(error))[-4000:]}
    output = _redact_github_text(result.stdout or "").replace("\r\n", "\n")
    stderr = _redact_github_text(result.stderr or "")
    headers, separator, body = output.partition("\n\n")
    # gh repo create may report GraphQL/HTTP errors only on stderr. Retain those
    # verbatim (redacted), as rate limits are not always an HTTP 429 response.
    status = re.search(r"\bHTTP(?:/\S+)?\s+(\d{3})\b", output + "\n" + stderr, re.I)
    selected_headers = "\n".join(line for line in headers.splitlines()
        if re.match(r"(?:HTTP/|retry-after:|x-ratelimit-[\w-]+:)", line, re.I)) if separator else ""
    diagnostic = "\n".join(part for part in (selected_headers,
        (body if separator else output)[-1800:], stderr[-1800:]) if part)
    return {"stage": stage, "exit": result.returncode,
            "http_status": int(status.group(1)) if status else None,
            "diagnostic": diagnostic[-4000:]}


def ownership_path(repo: Path) -> Path:
    """One immutable receipt per resolved local repository, outside its Git tree."""
    repo = repo.resolve()
    key = hashlib.sha256(str(repo).encode()).hexdigest()[:24]
    return repo.parent / f".s11-github-ownership-{key}.json"


def github_repo_identity(full_name: str) -> dict | None:
    """Read GitHub's numeric repository ID. Only an actual HTTP 404 means absent.

    Do not collapse network/auth/CLI failures into a missing repository, and do
    not infer ownership from an empty remote or a matching name/commit.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", full_name):
        raise OwnershipError("GitHub owner/name 格式无效")
    try:
        result = run([gh_binary(), "api", "--include", f"repos/{full_name}"])
    except OSError as exc:
        raise OwnershipError("无法执行 GitHub 身份查询", github_error=_github_failure("identity", error=exc)) from exc
    headers, separator, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    status = re.match(r"HTTP/\S+\s+(\d{3})(?:\s|$)", headers)
    code = int(status.group(1)) if status else None
    if result.returncode != 0 and code == 404:
        return None
    if result.returncode != 0 or code != 200 or not separator:
        raise OwnershipError(f"无法读取 GitHub 仓库身份（exit={result.returncode}, HTTP={code}）；安全停止，不尝试接管",
                             github_error=_github_failure("identity", result))
    try:
        payload = json.loads(body)
        identity = {"id": payload["id"], "owner": payload["owner"]["login"],
                    "full_name": payload["full_name"]}
    except (ValueError, TypeError, KeyError) as exc:
        raise OwnershipError("GitHub 仓库身份响应无效",
                             github_error=_github_failure("identity", result)) from exc
    if (type(identity["id"]) is not int or identity["id"] <= 0
            or not isinstance(identity["owner"], str)
            or not isinstance(identity["full_name"], str)
            or identity["full_name"].casefold() != full_name.casefold()
            or identity["owner"].casefold() != full_name.split("/", 1)[0].casefold()):
        raise OwnershipError("GitHub 返回的 owner/full_name/id 与请求不符",
                             github_error=_github_failure("identity", result))
    return identity


def read_ownership(repo: Path) -> dict | None:
    path = ownership_path(repo)
    if path.is_symlink():
        raise OwnershipError(f"归属记录不能是符号链接：{path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise OwnershipError(f"无法读取归属记录：{path}") from exc
    if not isinstance(record, dict):
        raise OwnershipError(f"归属记录格式无效：{path}")
    return record


def verify_ownership(repo: Path, full_name: str, identity: dict | None) -> dict:
    record = read_ownership(repo)
    if record is None:
        raise OwnershipError(f"同名 GitHub 仓库已存在但缺少本仓归属记录，禁止接管：{ownership_path(repo)}")
    if identity is None:
        raise OwnershipError("已有归属证据但 GitHub 仓库不存在/不可见，保留证据，不自动重建或接管")
    expected_owner = full_name.split("/", 1)[0]
    if (type(record.get("schema_version")) is not int or record["schema_version"] != 1
            or record.get("host") != "github.com" or record.get("local_repo") != str(repo.resolve())
            or type(record.get("id")) is not int or record["id"] != identity["id"]
            or str(record.get("owner", "")).casefold() != expected_owner.casefold()
            or str(record.get("full_name", "")).casefold() != full_name.casefold()
            or identity["full_name"].casefold() != full_name.casefold()
            or identity["owner"].casefold() != expected_owner.casefold()):
        raise OwnershipError("归属记录的 local_repo/owner/full_name/id 与真实 GitHub 仓库身份不符，禁止接管")
    return record


def save_ownership(repo: Path, identity: dict, head: str, account: str) -> dict:
    """Atomically publish a durable, exclusive receipt; never overwrite evidence.

    A same-directory temporary file plus link provides atomic no-clobber
    publication (unlike replace, which could erase a competing receipt).
    """
    path = ownership_path(repo)
    record = {"schema_version": 1, "host": "github.com", **identity,
              "local_repo": str(repo.resolve()), "created_head": head, "account": account}
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=repo.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Fails closed if *any* existing receipt appeared.
        directory = os.open(repo.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)
    return record


def gh_repo_visibility(full_name: str) -> str | None:
    """读远端仓库的真实可见性；读不到（网络/权限）返回 None。"""
    result = run([gh_binary(), "repo", "view", full_name, "--json", "visibility"])
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout).get("visibility") or ""
    except json.JSONDecodeError:
        return None
    return str(value).strip().lower() or None


def edit_repo_visibility(full_name: str, visibility: str) -> subprocess.CompletedProcess[str]:
    return run([gh_binary(), "repo", "edit", full_name, "--visibility", visibility,
                "--accept-visibility-change-consequences"])


def ensure_visibility(full_name: str, visibility: str, *, execute: bool) -> dict[str, object]:
    """核对远端仓库可见性，必要时改成要求的值。

    生成的仓库必须是公开的：下游容器要能匿名 clone，private 会直接卡住出题链路。
    改动不做无声操作——前后值都写进返回值，`will_fix` 表示这一轮只计划、没执行。
    """
    result: dict[str, object] = {"problems": [], "will_fix": False, "fixed": False}
    before = gh_repo_visibility(full_name)
    result["before"] = before
    if before is None:
        result["after"] = None
        return result
    if before == visibility:
        result["after"] = before
        return result

    result["planned_command"] = ["gh", "repo", "edit", full_name, "--visibility", visibility,
                                 "--accept-visibility-change-consequences"]
    if not execute:
        result["after"] = visibility
        result["will_fix"] = True
        return result

    edited = edit_repo_visibility(full_name, visibility)
    if edited.returncode != 0:
        message = edited.stderr.strip() or edited.stdout.strip() or "gh repo edit failed"
        result["after"] = gh_repo_visibility(full_name)
        result["problems"].append(f"把 {full_name} 改成 {visibility} 失败：{message}")
        return result
    after = gh_repo_visibility(full_name)
    result["after"] = after
    # fixed 表示「现在真的是要求的可见性了」，不是「改动命令返回 0」
    result["fixed"] = after == visibility
    if after != visibility:
        result["problems"].append(
            f"{full_name} 当前可见性是 {after}，要求是 {visibility}；"
            f"手工改：gh repo edit {full_name} --visibility {visibility} "
            "--accept-visibility-change-consequences")
    return result


def resolve_owner(explicit: str | None) -> str:
    return github_settings.resolve_config(explicit)['owner']


def clean_https_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def configure_origin(repo: Path, url: str, branch: str) -> None:
    """origin 指向不带 token 的地址，免得 token 留在 .git/config 里。"""
    run(["git", "remote", "remove", "origin"], cwd=repo)
    result = run(["git", "remote", "add", "origin", url], cwd=repo)
    if result.returncode != 0:
        raise SystemExit(f"配置 origin 失败：{result.stderr.strip()}")
    run(["git", "config", f"branch.{branch}.remote", "origin"], cwd=repo)
    run(["git", "config", f"branch.{branch}.merge", f"refs/heads/{branch}"], cwd=repo)
    run(["git", "config", "--unset-all", f"branch.{branch}.pushRemote"], cwd=repo)


def check_delivery_evidence(repo: Path, head: str) -> dict[str, object]:
    """s11 仓库发布必须携带绑定当前 HEAD 的查重与启动联通证据。"""
    if not ((repo / ".s11-domain.json").is_file() or (repo / ".s11-spec.json").is_file()):
        return {"ok": True, "problems": [], "skipped": "非 s11 生成仓库"}
    problems: list[str] = []
    reports: dict[str, object] = {}
    for name in ("probe-report.json", "similarity-report.json"):
        path = repo.parent / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            problems.append(f"缺少或无法读取 {path}；先运行最终启动联通验收与查重")
            continue
        reports[name] = payload
        if payload.get("ok") is not True:
            problems.append(f"{name} 的 ok 不是 true")
        if payload.get("source_head") != head:
            problems.append(f"{name} 未绑定当前 HEAD {head[:12]}，需重新验收")
    probe = reports.get("probe-report.json")
    if isinstance(probe, dict):
        if probe.get("cleaned_up") is not True:
            problems.append("probe-report.json 未证明进程与资源清理完成")
        if probe.get("mode") != "clean-clone":
            problems.append("probe-report.json 必须来自干净克隆启动联通验收")
        steps = probe.get("steps") or []
        def passed_step(suffix: str) -> bool:
            return any(isinstance(step, dict) and step.get("ok") is True
                       and not step.get("skipped")
                       and str(step.get("step", "")).endswith(suffix) for step in steps)
        scope = probe.get("verification_scope")
        if scope not in ("startup-connectivity", "full-functional"):
            problems.append("probe-report.json 缺少明确的启动联通验证范围")
        if any(not isinstance(step, dict) or step.get("ok") is not True for step in steps):
            problems.append("probe-report.json 存在失败或无效步骤")
        for suffix in ("前端安装依赖", "前端首页可访问且有挂载点", "前端入口脚本可加载"):
            if not passed_step(suffix):
                problems.append(f"probe-report.json 缺少实际完成步骤：{suffix}")
        if verifier.detect_repo_kind(repo) != "frontend-only":
            for suffix in ("后端安装依赖", "后端 /api/health 就绪", "前端 -> 后端代理链路通"):
                if not passed_step(suffix):
                    problems.append(f"probe-report.json 缺少实际完成步骤：{suffix}")
        # 全功能证据可兼容，但不能把跳过 E2E 的调试报告冒充 full。
        # startup 不要求 E2E / build，也不触发额外测试，符合一次测试+一次修复预算。
        if scope == "full-functional" and not passed_step("浏览器全功能验收"):
            problems.append("probe-report.json 声称 full-functional 却缺少实际浏览器全功能验收")
    similarity = reports.get("similarity-report.json")
    if isinstance(similarity, dict) and (repo / ".s11-spec.json").is_file():
        try:
            spec = json.loads((repo / ".s11-spec.json").read_text(encoding="utf-8"))
            if similarity.get("fingerprint") != check_similarity.fingerprint(spec):
                problems.append("similarity-report.json 的业务指纹与当前 .s11-spec.json 不一致")
        except (OSError, json.JSONDecodeError):
            problems.append("无法核对 .s11-spec.json 业务指纹")
    return {"ok": not problems, "problems": problems, "reports": sorted(reports)}


def publish(args) -> dict[str, object]:
    repo = Path(args.repo).expanduser().resolve()
    branch = args.branch
    report: dict[str, object] = {
        "repo": str(repo),
        "branch": branch,
        "dry_run": not args.execute,
        "steps": [],
        "planned_commands": [],
    }
    problems: list[str] = []

    if not repo.is_dir():
        raise SystemExit(f"仓库目录不存在：{repo}")
    if not (repo / ".git").exists():
        raise SystemExit(f"还不是 git 仓库，先 git init 并提交：{repo}")

    head = local_head(repo, branch)
    report["commit"] = head

    status = run(["git", "status", "--porcelain"], cwd=repo).stdout.strip()
    is_s11 = (repo / ".s11-domain.json").is_file() or (repo / ".s11-spec.json").is_file()
    dirty_blocked = bool(status) and (not args.allow_dirty or is_s11)
    if dirty_blocked:
        problems.append("工作区有未提交改动，先提交再推：" + "；".join(status.splitlines()[:5]))
    if args.execute and args.skip_verify and is_s11:
        problems.append("s11 生成仓库发布不能使用 --skip-verify")
    report["steps"].append({"step": "工作区干净", "ok": not dirty_blocked,
                            "detail": status[:200] or "clean"})

    if not args.skip_verify:
        result = verifier.verify(repo, min_lines=2000, min_capacity=rules.MIN_CAPACITY,
                                 per_mode=rules.MAX_PER_MODE, per_subject=rules.MAX_PER_SUBJECT)
        report["steps"].append({"step": "静态验收通过", "ok": bool(result["ok"]),
                                "detail": "全部硬性要求通过" if result["ok"]
                                else "；".join(result["failures"])[:400]})
        if not result["ok"]:
            problems.append("静态验收没过，先修好再推（或显式 --skip-verify）")

    if args.execute and is_s11:
        # Re-evaluate the inexpensive capacity gate, never trust a hand-edited pass JSON.
        # This runs before authentication or any remote write, including --remote-url.
        capacity = check_prompt_capacity.check_capacity(repo)
        report["prompt_capacity"] = capacity
        capacity_ok = capacity.get("ok") is True
        report["steps"].append({
            "step": "G10 下游35题位困难容量", "ok": capacity_ok,
            "detail": "真实下游题位硬闸通过" if capacity_ok else "容量未通过；先增强业务及题位证据",
        })
        if not capacity_ok:
            problems.append("G10 下游困难题位不足或证据无效；不能发布CRUD草稿")

    if args.execute:
        evidence = check_delivery_evidence(repo, head)
        report["steps"].append({
            "step": "当前 HEAD 的查重与启动联通证据", "ok": evidence["ok"],
            "detail": "；".join(evidence["problems"]) or "报告有效",
        })
        problems.extend(evidence["problems"])

    if problems:
        report.update(ok=False, problems=problems)
        return report

    # 远端地址
    full_name = ""
    remote_ready = True
    if args.remote_url:
        remote_host = (urlparse(args.remote_url).hostname or "").lower().rstrip(".")
        if remote_host == "github.com" or re.match(r"(?:[^/@:\s]+@)?github\.com\.?:", args.remote_url, re.I):
            raise SystemExit("GitHub 远端请使用 --owner / --name，--remote-url 仅支持非 GitHub 远端")
        url = args.remote_url
    else:
        selected = github_settings.configure_from_args(args)
        github_settings.auth_env()  # 任何远端写操作前验证凭证所属账号
        owner = selected["owner"]
        report["github_owner"] = owner
        report["github_account"] = selected["account"]
        name = args.name or repo.name
        full_name = f"{owner}/{name}"
        url = clean_https_url(full_name)
        # Dry-run retains read-only planning. Real GitHub writes require a
        # creation receipt for THIS local repo, even when the remote is empty.
        create_cmd = [gh_binary(), "repo", "create", full_name, f"--{args.visibility}"]
        if args.description:
            create_cmd += ["--description", args.description]
        if args.execute:
            try:
                identity = github_repo_identity(full_name)
                exists = identity is not None
                report["remote_exists"] = exists
                report["repo_created"] = False
                if exists:
                    verify_ownership(repo, full_name, identity)
                else:
                    if read_ownership(repo) is not None:
                        raise OwnershipError("已有归属记录但远端不存在，保留证据，不自动重建")
                    report["planned_commands"].append(redact(create_cmd))
                    try:
                        created = run(create_cmd)
                    except OSError as exc:
                        raise OwnershipError("无法执行 GitHub 建仓命令",
                            github_error=_github_failure("create", error=exc)) from exc
                    if created.returncode != 0:
                        # A concurrent creator is NOT ours. Never retry as an
                        # existing repository and never manufacture a receipt.
                        raise OwnershipError("GitHub 创建失败/可能同名竞争冲突；未取得归属，禁止接管",
                                             github_error=_github_failure("create", created))
                    report["repo_created"] = True
                    identity = github_repo_identity(full_name)
                    if identity is None:
                        raise OwnershipError("创建后无法回读真实 GitHub 仓库 ID；停止，不推送")
                    save_ownership(repo, identity, head, selected["account"])
                    verify_ownership(repo, full_name, identity)
                report["github_ownership"] = {"path": str(ownership_path(repo)), **identity}
                remote_ready = True
            except (OwnershipError, OSError) as exc:
                report.update(ok=False, problems=[f"GitHub 归属保护：{exc}"])
                if isinstance(exc, OwnershipError) and exc.github_error is not None:
                    report["github_error"] = exc.github_error
                return report
        else:
            exists = repo_exists(full_name)
            report["remote_exists"] = exists
            report["repo_created"] = False
            remote_ready = exists
            if not exists:
                report["planned_commands"].append(redact(create_cmd))

    report["remote_url"] = url
    report["repo_url"] = f"https://github.com/{full_name}" if full_name else url

    # 生成的仓库必须是公开的：下游（GSB 容器）要能匿名 clone，private 会直接卡住出题链路。
    # 所以这里核对真实可见性，和请求的不一致就改过来，并把前后值都写进报告，不做无声修改。
    if full_name and remote_ready:
        # 先只规划；远端分叉或证据失败时不得提前改动已有仓库可见性。
        visibility = ensure_visibility(full_name, args.visibility, execute=False)
        report["visibility_before"] = visibility.get("before")
        report["visibility_after"] = visibility.get("after")
        report["visibility_will_fix"] = visibility.get("will_fix")
        report["visibility_fixed"] = visibility.get("fixed")
        if visibility.get("planned_command"):
            report["planned_commands"].append(redact(visibility["planned_command"]))
        problems.extend(visibility["problems"])

    # 远端 main 与本地的关系：只允许首次推送或快进，绝不强推
    if remote_ready:
        existing_remote_head = remote_head(url, branch, repo, allow_missing=bool(args.remote_url))
    else:
        existing_remote_head = ""
    if existing_remote_head:
        if existing_remote_head == head:
            action = "up-to-date"
        else:
            ancestor = run(["git", "merge-base", "--is-ancestor", existing_remote_head, head], cwd=repo)
            if ancestor.returncode == 0:
                action = "fast-forward"
            else:
                problems.append(
                    f"远端 {branch} 上有本地没有的提交（远端 {existing_remote_head[:8]}），"
                    "本脚本不做强推，请先人工合并或确认远端可以覆盖")
                action = "diverged"
    else:
        action = "create-branch"
    report["planned_action"] = action
    report["remote_head_before"] = existing_remote_head

    push_cmd = ["git", *auth_args(url), "push", "origin", f"{branch}:{branch}"]
    report["planned_commands"].append(redact(push_cmd))

    if problems:
        report["ok"] = False
        report["problems"] = problems
        return report

    if not args.execute:
        report["ok"] = True
        next_cmd = [sys.executable, str(Path(__file__).resolve()), "--repo", str(repo),
                    "--execute", "--confirm", "PUBLISH"]
        if full_name:
            next_cmd += ["--owner", report["github_owner"], "--github-account", report["github_account"],
                         "--name", args.name or repo.name, "--visibility", args.visibility]
        else:
            next_cmd += ["--remote-url", url]
        report["next"] = shlex.join(next_cmd)
        return report

    if full_name:
        # Recheck immediately before the first mutable visibility/origin/push
        # operation; a deleted/recreated/transferred remote must not slip through.
        try:
            verify_ownership(repo, full_name, github_repo_identity(full_name))
        except (OwnershipError, OSError) as exc:
            report.update(ok=False, problems=[f"GitHub 归属保护：{exc}"])
            if isinstance(exc, OwnershipError) and exc.github_error is not None:
                report["github_error"] = exc.github_error
            return report

    if full_name and remote_ready and report.get("visibility_will_fix"):
        visibility = ensure_visibility(full_name, args.visibility, execute=True)
        report["visibility_after"] = visibility.get("after")
        report["visibility_will_fix"] = False
        report["visibility_fixed"] = visibility.get("fixed")
        if visibility.get("problems"):
            report["ok"] = False
            report["problems"] = list(visibility["problems"])
            return report

    configure_origin(repo, url, branch)
    if action != "up-to-date":
        pushed = git(["push", "origin", f"{branch}:{branch}"], cwd=repo, url=url)
        if pushed.returncode != 0:
            message = pushed.stderr.strip() or pushed.stdout.strip() or "git push failed"
            raise SystemExit(f"推送 {branch} 失败：{message}")

    after = remote_head(url, branch, repo)
    report["remote_head_after"] = after
    if after != head:
        report["ok"] = False
        report["problems"] = [f"推完回读远端 {branch}={after[:8]}，与本地 HEAD {head[:8]} 不一致"]
        return report
    report["ok"] = True
    report["pushed"] = action != "up-to-date"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="把仓库推到 GitHub（默认 main 分支）")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--owner", help="GitHub owner，默认读取本技能 GitHub 配置")
    parser.add_argument("--name", help="仓库名，默认取仓库目录名")
    parser.add_argument("--visibility", choices=["public", "private", "internal"], default="public",
                        help="仓库可见性，默认 public。生成的仓库必须是公开的——"
                             "下游容器要能匿名 clone；发现远端是 private 会自动改成 public 并记录")
    parser.add_argument("--description", help="仓库描述")
    parser.add_argument("--remote-url", help="非 GitHub 远端（例如 GitLab / 本地裸仓库）：跳过 gh 建仓直接推")
    parser.add_argument("--skip-verify", action="store_true",
                        help="跳过静态验收。只有推「不是本 skill 生成的项目」的仓库时才用"
                             "（例如推 skill 自身的代码仓），推生成出来的项目一律不要加")
    parser.add_argument("--allow-dirty", action="store_true", help="允许带未提交改动推送（不建议）")
    parser.add_argument("--execute", action="store_true", help="真的执行；不加就是 dry-run")
    parser.add_argument("--confirm", help="兼容旧命令；--execute 已表达执行授权")
    parser.add_argument("--json", action="store_true")
    github_settings.add_arguments(parser)
    args = parser.parse_args()

    report = publish(args)
    if not args.json:
        header = "DRY-RUN（没有碰远端）" if not args.execute else "已执行"
        print(f"[{header}] {report.get('repo_url', '')} 分支 {report.get('branch')} "
              f"提交 {str(report.get('commit', ''))[:8]} 动作 {report.get('planned_action')}")
        for step in report.get("steps", []):
            print(("  通过  " if step["ok"] else "  未通过") + f" {step['step']}：{str(step['detail'])[:120]}")
        for problem in report.get("problems", []):
            print(f"  ✗ {problem}")
        if report.get("next"):
            print(f"  下一步：{report['next']}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
