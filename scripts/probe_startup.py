#!/usr/bin/env python3
"""真起一遍：后端起服务 + 前端起 dev server，curl 到页面与接口，再全部停掉。

只做以下验证，不改仓库里的业务代码：

  1. 并行装依赖（前端 npm ci/install --prefer-offline，仅共享下载缓存；后端 venv + pip）。
     clean-clone 不复用宿主 node_modules/venv；直接运行时保留已有依赖/skip-install 语义。
  2. 起后端，轮询 /api/health；起前端，轮询首页 HTML；再从前端端口走一次 /api/health，
     证明 vite 代理到后端的链路通。**纯前端仓库**（前端 package.json 里
     repoKind = frontend-only）没有后端，这两步按「跳过」记录，不算失败。
  3. 默认 startup profile 到此为止：不跑浏览器 E2E，不做生产构建，不扫描全部业务接口。
     --profile full 仅供首次整体测试或用户显式要求；--build 也只能在 full 下启用。
  4. 无论成功失败，回收安装线程、停止进程组，再记录清理结果与阶段/总耗时。

clean-clone 的证据仅针对无未提交/未跟踪文件的 HEAD，结束后复查 HEAD/状态。
不提供跨运行证据缓存；报告明确标为 startup-connectivity，不冒充完整功能通过。
最终修复后仅运行默认 startup；首次整体测试与集中修复预算由主控维护，探针不循环修复。
每次为两个服务保留不同的 OS 临时端口，直到各自 spawn 前释放；前端 strictPort，
HTTP 前后检查进程存活与监听端口归属（lsof / Linux /proc），不能以别仓的 health 充数。

用法：

    python3 probe_startup.py --repo /path/to/repo
    python3 probe_startup.py --repo /path/to/repo --skip-install
"""

from __future__ import annotations

import argparse
import hashlib
from contextlib import contextmanager
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return int(probe.getsockname()[1])
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def reserve_probe_ports() -> list[socket.socket]:
    """两个 socket 同时保留到各自服务 spawn 前，避免不同探针抢固定默认端口。

    不用 SO_REUSEADDR/PORT。子服务不支持继承监听 FD，关闭到 bind 仍有微小窗口；
    readiness 必须再验证 listener 的进程组归属，不能把别的仓库的 HTTP 200 当成功。
    """
    sockets = []
    try:
        for _ in range(2):
            reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(reserved)
            reserved.bind(("127.0.0.1", 0))
        return sockets
    except BaseException:
        for reserved in sockets:
            reserved.close()
        raise


def listener_owned_by(process: subprocess.Popen, port: int) -> tuple[bool, str]:
    """只接受本次 start_new_session 进程组的 listener；缺少观测能力时 fail closed。"""
    lsof = shutil.which("lsof")
    if lsof:
        listing = run_command([lsof, "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                              cwd=Path.cwd(), timeout=5.0)
        pids = {int(line) for line in listing.stdout.splitlines() if line.isdigit()}
        if listing.returncode not in (0, 1) or not pids:
            return False, f"端口 {port} 尚未监听或无法核验 listener: {listing.stderr[-200:]}"
        try:
            owned = all(os.getpgid(pid) == process.pid for pid in pids)
        except (ProcessLookupError, PermissionError):
            return False, f"端口 {port} listener 已退出或归属不可读"
        return owned, f"port={port} listener_pids={sorted(pids)} expected_pgid={process.pid}"

    # Linux 无 lsof 的最小镜像：用 LISTEN socket inode 与本组进程 fd 交叉核对。
    if sys.platform.startswith("linux"):
        try:
            inodes = set()
            for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
                if table.exists():
                    for line in table.read_text().splitlines()[1:]:
                        fields = line.split()
                        if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                            inodes.add(fields[9])
            if not inodes:
                return False, f"端口 {port} 尚未监听"
            owned_inodes = set()
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if os.getpgid(int(entry.name)) != process.pid:
                        continue
                    for fd in (entry / "fd").iterdir():
                        try:
                            target = os.readlink(fd)
                            if target.startswith("socket:["):
                                owned_inodes.add(target[8:-1])
                        except FileNotFoundError:
                            pass
                except (ProcessLookupError, FileNotFoundError):
                    continue
            return inodes <= owned_inodes, f"port={port} socket 归属核验 expected_pgid={process.pid}"
        except (OSError, ValueError, IndexError) as error:
            return False, f"无法核验 /proc listener: {error}"
    return False, "无法核验监听进程归属：需要 lsof 或 Linux /proc（不接受仅 HTTP 200）"


def service_get(process: subprocess.Popen, port: int, path: str, log_path: Path) -> tuple[int, str]:
    def still_running():
        code = process.poll()
        if code is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:] if log_path.exists() else ""
            raise RuntimeError(f"服务进程 {process.pid} 已退出 exit={code}: {tail}")

    still_running()
    owned, detail = listener_owned_by(process, port)
    if not owned:
        return 0, detail
    response = http_get(f"http://127.0.0.1:{port}{path}")
    still_running()
    owned, detail = listener_owned_by(process, port)
    return response if owned else (0, detail)


def http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001 - 连接未就绪时各种异常都算「还没起来」
        return 0, str(error)


def wait_for(check, *, timeout: float, interval: float = 0.5) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        ok, detail = check()
        last = detail
        if ok:
            return True, detail
        time.sleep(interval)
    return False, last


def terminate(process: subprocess.Popen, log_path: Path) -> None:
    # start_new_session=True 令 pgid == pid。即使 npm/父进程已退出，也要清理同组子进程。
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n== 探针已停止该进程组 ==\n")


def spawn(command: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    environment = {**os.environ, **(env or {})}
    environment.setdefault("BROWSER", "none")
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True, env=environment,
        )
    finally:
        handle.close()  # 包括 Popen 失败的情形。
    return process, handle


def run_command(command: list[str], *, cwd: Path, timeout: float,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """限时执行；超时杀整个进程组，不能只杀 npm 留下 playwright/build 子进程。"""
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(command, 124, stdout,
                                               stderr + f"\n命令超过 {timeout:g} 秒，进程组已终止")
        raise


def write_report(report: dict[str, object], args) -> None:
    if getattr(args, "json_out", None):
        path = Path(args.json_out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 避免异常/中断后残留上一次成功证据，以及读到只写了一半的 JSON。
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def emit(report: dict[str, object], args) -> None:
    write_report(report, args)
    print(json.dumps(report, ensure_ascii=False, indent=2))


@contextmanager
def phase(timings: dict[str, float], name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        timings[name] = round(time.monotonic() - started, 6)


def source_state(repo: Path, timeout: float) -> tuple[str, str]:
    if not (repo / ".git").exists():
        return "", "仓库需要已提交的 git HEAD（不能借用父目录的 Git 状态）"
    head = run_command(["git", "rev-parse", "HEAD"], cwd=repo, timeout=timeout)
    status = run_command(["git", "status", "--porcelain", "--untracked-files=all"],
                         cwd=repo, timeout=timeout)
    if head.returncode or status.returncode:
        return "", "无法读取 git HEAD/工作区状态"
    return head.stdout.strip(), status.stdout.strip()


def probe_version() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def browser_runtime(frontend: Path, timeout: float) -> tuple[bool, str]:
    """验证当前项目锁定版本能真正启动 Chromium，不凭全局缓存目录存在猜测。"""
    node = shutil.which("node")
    cli = frontend / "node_modules" / ".bin" / "playwright"
    if not node or not cli.is_file():
        return False, "缺少 node 或项目本地 Playwright CLI（不使用 npx 隐式联网安装）"
    smoke = (
        "(async()=>{const b=await require('@playwright/test').chromium.launch({headless:true});"
        "await b.close()})().catch(e=>{console.error(e);process.exitCode=1})"
    )
    check = run_command([node, "-e", smoke], cwd=frontend, timeout=min(timeout, 30.0))
    if check.returncode == 0:
        return True, "当前项目 Chromium 实际启动/关闭成功，未重复安装"
    install = run_command([str(cli), "install", "chromium"], cwd=frontend,
                          timeout=max(300.0, timeout * 2))
    if install.returncode:
        return False, (install.stderr or install.stdout)[-600:] or "Chromium 安装失败"
    check = run_command([node, "-e", smoke], cwd=frontend, timeout=min(timeout, 30.0))
    return check.returncode == 0, (check.stderr or check.stdout)[-600:] or "安装后实际启动成功"


def detect_backend(repo: Path) -> dict[str, object]:
    backend = next((repo / name for name in ("backend", "server", "api") if (repo / name).is_dir()), None)
    if backend is None:
        return {"kind": "missing"}
    if (backend / "pom.xml").is_file() or (backend / "build.gradle").is_file():
        return {"kind": "java", "dir": backend}
    if (backend / "requirements.txt").is_file() or (backend / "app" / "main.py").is_file():
        return {"kind": "python", "dir": backend}
    return {"kind": "unknown", "dir": backend}


def detect_frontend(repo: Path) -> Path | None:
    for name in ("frontend", "web", "frontend-portal"):
        candidate = repo / name
        if (candidate / "package.json").is_file():
            return candidate
    for candidate in sorted(repo.iterdir()):
        if candidate.is_dir() and candidate.name.startswith(("frontend", "web")):
            if (candidate / "package.json").is_file():
                return candidate
    return None


def detect_kind(repo: Path) -> str:
    """仓库形态：前端 package.json 里 repoKind = frontend-only 就是纯前端仓库。

    与 verify_repo.detect_repo_kind 同一口径：显式字段，不靠「没有 backend 目录」猜。
    """
    frontend_dir = detect_frontend(repo)
    if frontend_dir is None:
        return "fullstack"
    try:
        payload = json.loads((frontend_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "fullstack"
    if str(payload.get("repoKind", "")).strip().lower() == "frontend-only":
        return "frontend-only"
    return "fullstack"


def detect_ports(repo: Path, backend_dir: Path | None, frontend_dir: Path | None) -> tuple[int, int]:
    backend_port, frontend_port = 8000, 5173
    if backend_dir:
        for candidate in (backend_dir / "app" / "config.py",
                          backend_dir / "src" / "main" / "resources" / "application.yml",
                          backend_dir / "src" / "main" / "resources" / "application.properties"):
            if not candidate.is_file():
                continue
            match = re.search(r"port\D{0,8}(\d{2,5})", candidate.read_text(encoding="utf-8", errors="replace"))
            if match:
                backend_port = int(match.group(1))
                break
    if frontend_dir:
        vite = frontend_dir / "vite.config.ts"
        if vite.is_file():
            match = re.search(r"port\s*:\s*(\d{2,5})", vite.read_text(encoding="utf-8", errors="replace"))
            if match:
                frontend_port = int(match.group(1))
    return backend_port, frontend_port


def default_spec_module_key() -> str | None:
    """默认 spec 里第一个模块的 key，用作探不到业务接口时的兜底。

    从默认 spec 现读，而不是写死一个模块名：题域换了、默认 spec 换了，
    这里会跟着走，不会留下一个指向旧题域的兜底接口。
    """
    try:
        import scaffold_repo  # 同目录，只借它的默认 spec 名字
        spec_path = Path(__file__).resolve().parent.parent / "examples" / scaffold_repo.DEFAULT_SPEC_NAME
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    modules = spec.get("modules") or []
    if modules and modules[0].get("key"):
        return str(modules[0]["key"])
    return None


def discover_business_endpoints(backend_dir: Path | None) -> list[str]:
    """从仓库里找出可探的业务接口，避免把某个题域的模块名写死。

    先扫 Python 后端的 app/routers/*.py 里的 APIRouter(prefix="/api/xxx")，
    再扫 Java 后端的 @RequestMapping("/api/xxx")；都扫不到时退回默认 spec 第一个
    模块对应的接口，保证老仓库的行为不变。
    """
    found: list[str] = []
    if backend_dir:
        routers = backend_dir / "app" / "routers"
        if routers.is_dir():
            for path in sorted(routers.glob("*.py")):
                if path.stem == "__init__":
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for match in re.findall(r"prefix\s*=\s*[\"']([^\"']+)[\"']", text):
                    if match not in found:
                        found.append(match)
        java_root = backend_dir / "src" / "main" / "java"
        if java_root.is_dir():
            for path in sorted(java_root.rglob("*.java")):
                text = path.read_text(encoding="utf-8", errors="replace")
                for match in re.findall(r"@RequestMapping\(\s*[\"']([^\"']+)[\"']", text):
                    if match not in found:
                        found.append(match)
    fallback_key = default_spec_module_key()
    return found or [f"/api/{fallback_key}" if fallback_key else "/api/health"]


def verification_metadata(args) -> dict[str, object]:
    profile = getattr(args, "profile", "startup")
    return {
        "verification_profile": profile,
        "verification_scope": "startup-connectivity" if profile == "startup" else "full-functional",
        "options": {"skip_install": args.skip_install, "skip_e2e": args.skip_e2e,
                    "build": args.build},
        "scope_note": ("仅证明安装、启动及HTTP联通；未验证全部业务、浏览器运行或生产构建"
                       if profile == "startup" else "显式整体测试；具体范围以实际步骤为准"),
    }


def run_clean_clone(repo: Path, args) -> int:
    """只验证提交快照；绝不把宿主 native node_modules/venv 挂进克隆。"""
    started = time.monotonic()
    timings: dict[str, float] = {}
    report: dict[str, object] = {
        "repo": str(repo), "mode": "clean-clone", "source_head": "", "steps": [],
        "probe_version": probe_version(), "evidence_reused": False,
        **verification_metadata(args),
        "timings_seconds": timings, "cleaned_up": False,
    }

    def record(name: str, passed: bool, detail: str, **extra: object) -> None:
        report["steps"].append({"step": name, "ok": passed, "detail": detail, **extra})

    workspace = None
    probe_started = False
    try:
        with phase(timings, "source_check"):
            head, dirty = source_state(repo, args.timeout)
            report["source_head"] = head
            record("源仓库 HEAD 与工作区状态", bool(head) and not dirty,
                   dirty[:600] or "已提交且无未提交/未跟踪文件")
        if not head or dirty:
            return_code = 2
        else:
            workspace = Path(tempfile.mkdtemp(prefix="repo-clean-clone-"))
            clone = workspace / repo.name
            with phase(timings, "clone"):
                cloned = run_command(["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(clone)],
                                     cwd=repo, timeout=args.timeout)
                record("克隆仓库到干净目录", cloned.returncode == 0,
                       (cloned.stderr or cloned.stdout or "ok").strip()[-300:])
            if cloned.returncode == 0:
                clone_head, clone_dirty = source_state(clone, args.timeout)
                record("克隆 HEAD 与源 HEAD 一致且工作区干净", clone_head == head and not clone_dirty,
                       f"HEAD={clone_head}; {clone_dirty or '工作区干净'}")
                carried = [str(path.relative_to(clone))
                           for name in ("node_modules", ".venv", "dist", "build")
                           for path in clone.rglob(name) if path.is_dir() or path.is_symlink()]
                record("依赖目录没有被提交进仓库", not carried,
                       "、".join(carried[:5]) or "无提交的依赖/构建目录", carried=carried[:10])
                if not any(step["ok"] is not True for step in report["steps"]):
                    # 同进程调用：避免外层超时杀掉探针解释器、遗留另起 session 的服务。
                    # 安装在全新克隆中各执行一次；仅 npm 下载缓存可跨克隆共享。
                    inner_args = argparse.Namespace(**vars(args))
                    inner_args.logs = None
                    with phase(timings, "probe"):
                        probe_started = True
                        payload = run_probe(clone, inner_args)
                    report["clean_clone_probe"] = payload
                    for step in payload["steps"]:
                        report["steps"].append({**step, "step": f"克隆内：{step['step']}"})
                    record("克隆里能装依赖、起服务并完成功能验收", payload["ok"] is True,
                           "全部通过" if payload["ok"] else "见失败步骤")
                    record("克隆内进程全部停止", payload["cleaned_up"] is True,
                           f"cleaned_up={payload['cleaned_up']}")
                    report["cleaned_up"] = payload["cleaned_up"]
                    source_logs = workspace / ".probe-logs" / repo.name
                    target_logs = repo.parent / ".probe-logs" / repo.name
                    if source_logs.is_dir():
                        shutil.copytree(source_logs, target_logs, dirs_exist_ok=True)
                        report["logs_dir"] = str(target_logs)
                with phase(timings, "source_recheck"):
                    final_head, final_dirty = source_state(repo, args.timeout)
                record("验收期间源 HEAD 与工作区未变化", final_head == head and not final_dirty,
                       final_dirty[:600] or f"HEAD={final_head}")
            return_code = 1
    except Exception as error:  # noqa: BLE001 - 失败证据也必须落盘
        record("干净克隆探针异常", False, f"{type(error).__name__}: {error}")
        return_code = 1
    finally:
        with phase(timings, "cleanup"):
            try:
                if workspace is not None:
                    shutil.rmtree(workspace)
                # 未起服务（前置检查失败）也是已清理；若起过则沿用 run_probe 的结果。
                report["cleaned_up"] = (report.get("clean_clone_probe", {}).get("cleaned_up", not probe_started)
                                        and (workspace is None or not workspace.exists()))
            except Exception as error:
                record("临时克隆清理", False, str(error))
                report["cleaned_up"] = False
        timings["total"] = round(time.monotonic() - started, 6)
    report["failed_steps"] = [step for step in report["steps"] if step["ok"] is not True]
    report["ok"] = not report["failed_steps"] and report["cleaned_up"] is True
    emit(report, args)
    return 0 if report["ok"] else return_code


def main() -> int:
    parser = argparse.ArgumentParser(description="真起一遍前端与后端，并验证页面能打开")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--timeout", type=float, default=180.0, help="每一步的等待上限（秒）")
    parser.add_argument("--skip-install", action="store_true", help="跳过装依赖（依赖已就绪时用）")
    parser.add_argument("--logs", help="日志目录，默认写到仓库外层的 .probe-logs")
    parser.add_argument("--clean-clone", action="store_true",
                        help="先把仓库 clone 到一个干净临时目录，再在克隆里跑指定范围探针"
                             "（证明不依赖任何未提交的本地残留）")
    parser.add_argument("--profile", choices=["startup", "full"], default="startup",
                        help="默认 startup：只验启动联通；full：首次整体测试时显式启用浏览器E2E")
    parser.add_argument("--build", action="store_true",
                        help="仅 --profile full：额外跑一次前端 npm run build；最终启动验收不使用")
    parser.add_argument("--skip-e2e", action="store_true",
                        help="仅 full 调试：跳过浏览器全功能验收；正式 startup 不需要此开关")
    parser.add_argument("--json-out", help="同时把报告写到指定路径，供发布器核验")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    if args.profile == "startup" and (args.build or args.skip_e2e):
        parser.error("startup 只验启动联通；--build / --skip-e2e 仅用于显式 --profile full")

    repo = Path(args.repo).expanduser().resolve()

    # 每次执行都先撤销旧成功证据。没有跨运行的成功证据缓存/跳过测试路径。
    write_report({"ok": False, "cleaned_up": False, "state": "running", "steps": []}, args)
    if args.clean_clone:
        return run_clean_clone(repo, args)
    report = run_probe(repo, args)
    emit(report, args)
    return 0 if report["ok"] else 1


def run_probe(repo: Path, args) -> dict[str, object]:
    started = time.monotonic()
    full = getattr(args, "profile", "startup") == "full"
    timings: dict[str, float] = {}
    with phase(timings, "source_check"):
        initial_head, initial_dirty = source_state(repo, args.timeout)
    logs = Path(args.logs).expanduser().resolve() if args.logs else repo.parent / ".probe-logs" / repo.name
    logs.mkdir(parents=True, exist_ok=True)

    backend = detect_backend(repo)
    frontend_dir = detect_frontend(repo)
    kind = detect_kind(repo)
    backend_dir = backend.get("dir")
    backend_port, frontend_port = detect_ports(repo, backend_dir, frontend_dir)

    steps: list[dict[str, object]] = []
    processes: list[tuple[subprocess.Popen, Path]] = []
    result: dict[str, object] = {
        "repo": str(repo),
        "source_head": initial_head,
        "probe_version": probe_version(),
        **verification_metadata(args),
        "evidence_reused": False,
        "timings_seconds": timings,
        "timing_note": "墙钟秒；安装与服务阶段重叠，startup 含等待安装时间，不能直接累加",
        "port_policy": "OS ephemeral reservations; readiness requires listener process-group ownership",
        "configured_ports": {"backend": backend_port, "frontend": frontend_port},
        "kind": kind,
        "backend": str(backend.get("kind")),
        "backend_dir": str(backend_dir) if backend_dir else "",
        "frontend_dir": str(frontend_dir) if frontend_dir else "",
        "ports": {"backend": backend_port, "frontend": frontend_port},
        "steps": steps,
        "logs_dir": str(logs),
    }
    ok = True

    def record(name: str, passed: bool, detail: str, **extra: object) -> None:
        nonlocal ok
        steps.append({"step": name, "ok": passed, "detail": detail, **extra})
        if not passed:
            ok = False

    def record_skip(name: str, detail: str, **extra: object) -> None:
        """跳过但不算失败：纯前端仓库本来就没有后端可起。"""
        steps.append({"step": name, "ok": True, "skipped": True, "detail": detail, **extra})

    live_backend_port: int | None = None
    live_frontend_port: int | None = None
    backend_process = None
    port_reservations: list[socket.socket] = []

    install_futures = {}
    install_pool = ThreadPoolExecutor(max_workers=2) if not args.skip_install else None

    def _install_backend() -> tuple[str, bool, str]:
        if kind == "frontend-only" or backend["kind"] != "python":
            return ("backend", True, "no install needed")
        venv = backend_dir / ".venv"
        python_bin = venv / "bin" / "python"
        if python_bin.exists():
            return ("backend", True, "already installed")
        installed = False
        try:
            proc = run_command([sys.executable, "-m", "venv", str(venv)],
                               cwd=backend_dir, timeout=args.timeout)
            if proc.returncode != 0:
                return ("backend", False, proc.stderr[-400:] or "venv create failed")
            pip = run_command(
                [str(venv / "bin" / "pip"), "install", "-q", "-r", "requirements.txt"],
                cwd=backend_dir, timeout=args.timeout * 2)
            installed = pip.returncode == 0
            pip_tail = "\n".join((pip.stderr or pip.stdout).strip().splitlines()[-3:])
            return ("backend", installed, pip_tail or "ok")
        finally:
            if not installed and venv.exists():
                shutil.rmtree(venv)

    def _install_frontend() -> tuple[str, bool, str]:
        if frontend_dir is None:
            return ("frontend", True, "no frontend")
        if (frontend_dir / "node_modules").is_dir():
            return ("frontend", True, "already installed")
        npm = shutil.which("npm")
        if npm is None:
            return ("frontend", False, "npm not found")
        install_command = "ci" if (frontend_dir / "package-lock.json").is_file() else "install"
        # npm 默认下载缓存（或调用方 npm_config_cache）只复用 tarball，不复用 native 产物。
        installed = False
        try:
            proc = run_command([npm, install_command, "--prefer-offline", "--no-audit", "--no-fund"],
                               cwd=frontend_dir, timeout=args.timeout * 2)
            installed = proc.returncode == 0
            return ("frontend", installed, (proc.stderr or proc.stdout)[-600:] or "ok")
        finally:
            partial = frontend_dir / "node_modules"
            if not installed and partial.exists():
                shutil.rmtree(partial)

    def install_timed(name, installer):
        with phase(timings, f"install_{name}"):
            return installer()

    def await_install(name):
        future = install_futures.pop(name, None)
        if future is not None:
            _, passed, detail = future.result()
            record("后端安装依赖" if name == "backend" else "前端安装依赖", passed, detail,
                   duration_seconds=timings[f"install_{name}"])
            return passed
        return True

    try:
        with phase(timings, "port_allocation"):
            port_reservations = reserve_probe_ports()
            backend_port, frontend_port = (int(sock.getsockname()[1]) for sock in port_reservations)
            result["ports"] = {"backend": backend_port, "frontend": frontend_port}
        if install_pool is not None:
            install_futures["backend"] = install_pool.submit(install_timed, "backend", _install_backend)
            install_futures["frontend"] = install_pool.submit(install_timed, "frontend", _install_frontend)
        with phase(timings, "backend_startup"):
            # ---------- 后端 ----------
            if kind == "frontend-only":
                record_skip("后端启动", "纯前端仓库：没有后端服务，数据由前端本地数据层提供（跳过）")
            elif backend["kind"] == "python":
                venv = backend_dir / ".venv"
                python_bin = venv / "bin" / "python"
                backend_installed = await_install("backend")
                if not backend_installed or not python_bin.exists():
                    record("后端依赖就绪", False, f"{python_bin} 不存在，请先装依赖或去掉 --skip-install")
                else:
                    port = backend_port
                    port_reservations[0].close()
                    process, _ = spawn(
                        [str(python_bin), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                         "--port", str(port)],
                        backend_dir, logs / "backend.log")
                    processes.append((process, logs / "backend.log"))
                    backend_process = process

                    def backend_ready():
                        status, body = service_get(process, port, "/api/health", logs / "backend.log")
                        return status == 200, f"status={status} body={body[:200]}"

                    ready, detail = wait_for(backend_ready, timeout=args.timeout)
                    record("后端 /api/health 就绪", ready, detail, port=port)
                    if ready:
                        live_backend_port = port
                        if full:
                            status, body = service_get(process, port, "/api/overview", logs / "backend.log")
                            record("后端 /api/overview 有数据", status == 200 and "modules" in body,
                                   f"status={status} body={body[:200]}")
                            endpoints = discover_business_endpoints(backend_dir)
                            probed = ("", 0, "")
                            for endpoint in endpoints:
                                status, body = service_get(process, port, endpoint, logs / "backend.log")
                                probed = (endpoint, status, body)
                                if status == 200 and "items" in body:
                                    break
                            endpoint, status, body = probed
                            record("后端业务接口可访问", status == 200 and "items" in body,
                                   f"{endpoint} status={status} body={body[:200]}"
                                   + ("" if len(endpoints) <= 1 else f"（共试了 {len(endpoints)} 个候选）"))
            elif backend["kind"] == "java":
                if shutil.which("mvn") is None or shutil.which("java") is None:
                    record("Java 后端启动", False,
                           "本机没有 JDK/Maven，无法真启动；请在装好 JDK17 + Maven 的机器上重跑本探针")
                else:
                    port = backend_port
                    port_reservations[0].close()
                    process, _ = spawn(
                        ["mvn", "-q", "-Dspring-boot.run.arguments=--server.port=" + str(port),
                         "spring-boot:run"], backend_dir, logs / "backend.log")
                    processes.append((process, logs / "backend.log"))
                    backend_process = process

                    def java_ready():
                        status, body = service_get(process, port, "/api/health", logs / "backend.log")
                        return status == 200, f"status={status} body={body[:200]}"

                    ready, detail = wait_for(java_ready, timeout=args.timeout)
                    record("Java 后端 /api/health 就绪", ready, detail, port=port)
                    if ready:
                        live_backend_port = port
                        if full:
                            status, body = service_get(process, port, "/api/overview", logs / "backend.log")
                            record("Java 后端 /api/overview 有数据", status == 200 and "modules" in body,
                                   f"status={status} body={body[:200]}")
                            endpoints = discover_business_endpoints(backend_dir)
                            probed = ("", 0, "")
                            for endpoint in endpoints:
                                status, body = service_get(process, port, endpoint, logs / "backend.log")
                                probed = (endpoint, status, body)
                                if status == 200 and "items" in body:
                                    break
                            endpoint, status, body = probed
                            record("Java 后端业务接口可访问", status == 200 and "items" in body,
                                   f"{endpoint} status={status} body={body[:200]}")
            else:
                record("后端启动", False, f"识别不出后端类型：{backend}")

        with phase(timings, "frontend_startup"):
            # ---------- 前端 ----------
            if frontend_dir is None:
                record("前端启动", False, "找不到前端目录")
            else:
                if not await_install("frontend"):
                    raise RuntimeError("前端依赖安装失败，不启动不完整的依赖树")
                port = frontend_port
                port_reservations[1].close()
                npm = shutil.which("npm") or "npm"
                frontend_env = {"BROWSER": "none", "CI": "1"}
                if live_backend_port:
                    # 后端可能落在非默认端口上，把真实地址告诉 vite 的代理，别让链路白断
                    frontend_env["VITE_PROXY_TARGET"] = f"http://127.0.0.1:{live_backend_port}"
                process, _ = spawn(
                    [npm, "run", "dev", "--", "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
                    frontend_dir, logs / "frontend.log",
                    env=frontend_env)
                processes.append((process, logs / "frontend.log"))
                def frontend_ready():
                    status, body = service_get(process, port, "/", logs / "frontend.log")
                    mount = 'id="app"' in body or 'id="root"' in body
                    return (status == 200 and mount), f"status={status} mount={mount} len={len(body)}"

                ready, detail = wait_for(frontend_ready, timeout=args.timeout)
                record("前端首页可访问且有挂载点", ready, detail, port=port)
                if ready:
                    live_frontend_port = port
                    entry_name = "main.ts" if (frontend_dir / "src" / "main.ts").exists() else "main.tsx"
                    status, entry = service_get(process, port, f"/src/{entry_name}", logs / "frontend.log")
                    record("前端入口脚本可加载", status == 200, f"status={status} len={len(entry)}")
                    if kind == "frontend-only":
                        record_skip("前端 -> 后端代理链路",
                                    "纯前端仓库没有后端，数据走本地数据层，不经过 vite 代理（跳过）")
                    elif live_backend_port:
                        backend_owned, detail = listener_owned_by(backend_process, live_backend_port)
                        status, body = service_get(process, port, "/api/health", logs / "frontend.log")
                        record("前端 -> 后端代理链路通", backend_owned and status == 200 and '"ok"' in body,
                               f"status={status} body={body[:200]}")
                    else:
                        record("前端 -> 后端代理链路通", False, "后端没起来，代理无从验证")

        # ---------- 浏览器整体测试（仅显式 full；最终启动验收不重复）----------
        if full and not args.skip_e2e:
            test_file = frontend_dir / "tests" / "functionality.spec.ts" if frontend_dir else None
            if not live_frontend_port or not frontend_dir or not test_file or not test_file.is_file():
                record("浏览器全功能验收", False, "前端未就绪或缺少 tests/functionality.spec.ts")
            else:
                with phase(timings, "browser_runtime"):
                    browser_ok, browser_detail = browser_runtime(frontend_dir, args.timeout)
                record("浏览器运行时就绪", browser_ok, browser_detail,
                       duration_seconds=timings["browser_runtime"])
                if browser_ok:
                    e2e_env = {**os.environ, "CI": "1", "E2E_BASE_URL": f"http://127.0.0.1:{live_frontend_port}"}
                    with phase(timings, "e2e"):
                        e2e = run_command([npm, "run", "test:e2e"], cwd=frontend_dir,
                                          timeout=max(360.0, args.timeout * 2), env=e2e_env)
                    frontend_status, frontend_detail = service_get(process, live_frontend_port, "/", logs / "frontend.log")
                    backend_status, backend_detail = 200, "无后端"
                    if live_backend_port:
                        backend_status, backend_detail = service_get(backend_process, live_backend_port, "/api/health",
                                                                     logs / "backend.log")
                    detail = (e2e.stderr or e2e.stdout)[-1200:] or "ok"
                    if frontend_status != 200 or backend_status != 200:
                        detail += f"\nE2E 后服务归属/健康复查失败：{frontend_detail[:200]} / {backend_detail[:200]}"
                    record("浏览器全功能验收", e2e.returncode == 0 and frontend_status == 200 and backend_status == 200,
                           detail, duration_seconds=timings["e2e"])

        # ---------- 生产构建（仅显式 full；startup连构建阶段都不调度）----------
        if full and args.build and frontend_dir is not None:
            with phase(timings, "build"):
                npm_bin = shutil.which("npm") or "npm"
                build_proc = run_command([npm_bin, "run", "build"], cwd=frontend_dir, timeout=args.timeout * 2)
                build_tail = "\n".join((build_proc.stderr or build_proc.stdout).strip().splitlines()[-3:])
                record("前端生产构建通过（typecheck + vite build）", build_proc.returncode == 0,
                       build_tail or "ok")

    except Exception as error:  # noqa: BLE001 - 仍然执行全部清理并落失败报告
        record("探针异常", False, f"{type(error).__name__}: {error}")
    finally:
        # 无论服务启动/安装失败，都回收安装线程，并读取每个安装结果一次。
        with phase(timings, "install_join"):
            if install_pool is not None:
                install_pool.shutdown(wait=True)
                for name in list(install_futures):
                    try:
                        await_install(name)
                    except Exception as error:
                        record(f"{name} 安装异常", False, str(error))
        result["live_ports"] = {"backend": live_backend_port, "frontend": live_frontend_port}
        with phase(timings, "cleanup"):
            for reserved in port_reservations:
                reserved.close()
            cleanup_errors = []
            for process, log_path in processes:
                try:
                    terminate(process, log_path)
                except Exception as error:  # noqa: BLE001 - 清理失败也必须进入报告
                    cleanup_errors.append(f"PID {process.pid}: {error}")
            result["cleaned_up"] = not cleanup_errors and all(process.poll() is not None for process, _ in processes)
            result["cleanup_errors"] = cleanup_errors

    if initial_head:
        with phase(timings, "source_recheck"):
            final_head, final_dirty = source_state(repo, args.timeout)
        record("验收绑定 HEAD 且工作区无未提交/未跟踪变化",
               not initial_dirty and not final_dirty and initial_head == final_head,
               (initial_dirty or final_dirty)[:600] or f"HEAD={final_head}")
    result["ok"] = ok and result["cleaned_up"] is True
    result["failed_steps"] = [step for step in steps if not step["ok"]]
    if result["cleaned_up"] is not True:
        result["failed_steps"].append({"step": "进程清理", "ok": False, "detail": "仍有进程未停止"})
    timings["total"] = round(time.monotonic() - started, 6)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
