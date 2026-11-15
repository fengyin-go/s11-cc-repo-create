# 本机环境（已实测，2026-11-12）

本文件记录 s11-cc-repo-create skill 在**这台机器**上实际验证过的路径与工具版本。
跑生成、验收、真启动探针、推 GitHub 之前先读这里。

## 平台

| 项 | 值 |
|---|---|
| 操作系统 | macOS（darwin） |
| 默认 shell | zsh |

## 本 skill 的安装位置

```text
~/.kunlunxiaozhi/skills/s11-cc-repo-create
```

下文用 `<skill>` 指代这个目录。所有脚本都在 `<skill>/scripts/` 下，不依赖任何其他 skill。

## 运行时（全部走 PATH，无需写绝对路径）

| 工具 | PATH 上的实际来源 | 版本 | 备注 |
|---|---|---|---|
| `python3` | `~/.workbuddy/binaries/python/versions/3.13.12/bin/python3` | 3.13.12 | 隔离运行时；`pip` 26.0 可用；`venv` 可用 |
| `node` | `~/.workbuddy/binaries/node/versions/22.22.2/bin/node` | v22.22.2 | 隔离运行时 |
| `npm` | `~/.workbuddy/binaries/node/versions/22.22.2/bin/npm` | 10.9.7 | 生成/构建前端用它 |
| `git` | Apple Git | 2.50.1 | 生成器的 `--git-init`、探针的 `git clone` 都靠它 |
| `gh` | `~/.local/bin/gh` | 2.95.0 | 已登录账号 `daofaziran888`（活跃）、`fengyin-solo`、`yuenlai`；未登录 `fengyin-go` |

`python3` / `node` / `npm` 都已在 PATH 上指向隔离运行时，**直接写命令名即可**，不要硬编码绝对路径。

## 没有装 / 与预期不符

| 工具 | 状态 | 影响 |
|---|---|---|
| JDK | 装了 JDK 25（Zulu），不是技能要求的 JDK 17 | Java 后端探针可能因版本差异失败 |
| Maven (`mvn`) | **未安装** | Java 后端（Spring Boot）无法真启动验收 |

**本机出题与交付请统一用 `--backend python`（FastAPI）或 `--backend none`（纯前端）。**
Java 版的 G8 一律按未验证处理，不能写成已通过。

## GitHub 账号配置

- **默认配置** `config/github.json`：`owner = "fengyin-go"`（仓库模板的原目标）
- **本机覆盖** `config/github.local.json`：`owner = "daofaziran888"`（2026-11-12 已切换并验证）
- **凭证**：`gh` 已登录 `daofaziran888`（活跃账号），已通过 `github_settings.py` 验证 (`credential_valid: true`)
- 每次脚本运行时 `github.local.json` 的 owner 会覆盖默认的 fengyin-go，凭证从 gh 的 daofaziran888 token 读取
- **不需要**本机另登录 `fengyin-go`，不需要全局 `gh auth switch`
- 如需切回 `fengyin-solo` 或其他账号：修改 `github.local.json` 的 owner，并用 `gh auth token --hostname github.com --user <账号>` 确认该账号已登录

## 命令注意事项

1. **路径带中文或空格必须加引号。** 本机工作目录就带中文和空格：
   `/Users/fengyin/项目/字节/solo11/s11任务目录 1`

2. **`<skill>` 要展开成绝对路径。** 示例：
   ```bash
   python3 ~/.kunlunxiaozhi/skills/s11-cc-repo-create/scripts/run_pipeline.py \
     --parent "/Users/fengyin/项目/字节/solo11/s11任务目录 1" \
     --spec ~/.kunlunxiaozhi/skills/s11-cc-repo-create/examples/spec-data-engineering.json \
     --publish --confirm PUBLISH
   ```

3. **真启动探针会联网**：`probe_startup.py` 会用锁文件执行 `npm ci`、安装 Python 锁定依赖并准备 Playwright Chromium，需要 npm registry、PyPI 与浏览器下载源可达。

4. **探针日志**默认落在「被验仓库的父目录」下的 `.probe-logs/<仓库名>/`。

5. **自测已验证**：105 个测试全部通过（2026-11-15 执行 `python3 -m unittest discover -s <skill>/tests -v`）；Vue/React × Python 与两种纯前端形态另完成干净克隆 E2E 和生产构建。Java 因本机缺 Maven 仅做静态/契约验收，未宣称真启动通过。

## 跨机器迁移备忘

如果要把这个 skill 复制到别的电脑：
1. 复制整个 `s11-cc-repo-create` 目录
2. 修改 `config/github.local.json` 的 owner 为目标账号；或删除该文件使用默认 `fengyin-go`
3. 用 `gh auth login --hostname github.com` 登录目标账号
4. 执行 `python3 <skill>/scripts/github_settings.py` 验证
5. 执行 `python3 -m unittest discover -s <skill>/tests -v` 跑自测
6. 更新本文件记录新电脑的工具版本和路径