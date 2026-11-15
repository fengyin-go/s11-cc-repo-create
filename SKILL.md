---
name: s11-cc-repo-create
description: 为 solo11 生成数据工程领域的出题底座仓库，子方向限数据采集、爬虫与RPA，数仓与数据湖，数据分析与数据应用。支持 Vue/React 配 Python/Java 或纯前端；完成跨仓库查重、静态验收、干净克隆启动、浏览器全功能 E2E 与 GitHub 发布，GitHub owner 和认证账号按机器配置。用于生成仓库、造项目和建立出题底座，不负责题面、模型轨迹或产物验收。
disable-model-invocation: true
---

# s11 数据工程仓库生成

从原 `cc-repo-create` 独立复制，原技能保留。所有脚本、规则和示例都在本目录，不依赖其他 skill。
下文 `<skill>` 指本技能所在目录，常见安装位置是 `~/.kunlunxiaozhi/skills/s11-cc-repo-create`，实际使用时按当前安装目录解析，不能写死某台机器路径。

## 领域与子方向

仓库领域统一为 **数据工程**。每份 spec 必须填写 `domain: "数据工程"`，并从下面选择一个 `subdirection`，使用以下准确值：

1. `数据采集、爬虫与RPA`：数据接入、网页抽取、增量采集、自动化流程、失败重试、采集结果校验。
2. `数仓与数据湖`：原始落地、清洗转换、分层建模、分区治理、质量校验、血缘、补数。
3. `数据分析与数据应用`：指标口径、聚合查询、分析实验、数据服务、数据产品与应用。

用户指定子方向时按指定方向；未指定可任选一个，不必再问。模块可覆盖相关环节，但必须有清楚的主方向。
应用名称、业务对象、字段、规则和代码都必须服务于数据工程；不能把原影视或普通管理项目仅改 `domain` 就当作合规项目。
`load_spec` 和 `scaffold` 校验领域，生成根目录 `.s11-domain.json` 与 README 领域说明；`verify_repo.py` 的 G0 再核对。机器校验负责元数据边界，生成者仍须检查实际业务与领域一致。

默认示例为 [examples/spec-data-engineering.json](examples/spec-data-engineering.json)：数据工程链路开发平台，主方向「数仓与数据湖」，18 个模块。
参考 [references/spec-format.md](references/spec-format.md) 编写其他 spec。模块至少 16 个，每个模块推荐 6～8 个具体字段、2～3 个不同方向的动作。

## GitHub 账号与凭证

**本副本默认发布到 `fengyin-go`，不跟随 `gh` 全局当前账号。** 查编号、建仓、查询可见性和推送必须使用同一套配置。

默认值在 [config/github.json](config/github.json)。迁移到其他电脑时，创建 `config/github.local.json`，例如：

```json
{
  "owner": "该电脑的GitHub账号"
}
```

`account` 可选，省略则等于最终 owner；发布到组织时可用 `{"owner":"组织名","account":"有权限的个人账号"}`。
配置只允许 `owner` / `account`，禁止存 token。`github.local.json` 已被忽略；复制整个技能会连同它一起复制，迁移后应先检查本机覆盖值。

字段优先级：命令行 `--owner` / `--github-account` > 环境变量 `S11_GITHUB_OWNER` / `S11_GITHUB_ACCOUNT` > `--github-config` 或 `S11_GITHUB_CONFIG` 指定文件 > `config/github.local.json` > `config/github.json`。
额外配置文件按字段覆盖；`account` 未设置时才跟随最终 owner。指定文件必须存在。
流水线、生成器、发布器都支持这三个命令行参数，流水线会把解析结果传给子进程。

先执行只读检查：

```bash
python3 <skill>/scripts/github_settings.py
# 临时覆盖：
python3 <skill>/scripts/github_settings.py --owner another-user
# 本机配置：
python3 <skill>/scripts/github_settings.py --github-config /path/to/github.json
```

脚本优先读取 `gh auth token --hostname github.com --user <account>` 的已存凭证，缺失时才尝试进程中的 `GH_TOKEN` / `GITHUB_TOKEN`；使用前调用 GitHub API 核对所属账号，不匹配就停止，不回退到其他账号。
缺少凭证时，告知用户缺哪个账号，让其通过 `gh auth login --hostname github.com` 登录，再重跑检查。token 不输出、不写进 skill 或生成仓库。无需执行 `gh auth switch`，也不改变其他技能的全局默认账号。

## 环境检查

首次运行或换机器时读 [references/local_environment.md](references/local_environment.md)，现场检查 Python、Node、npm、git、gh；Java 还需 JDK 17 与 Maven。不要把旧机器工具版本或安装状态当作当前事实。
命令中含中文或空格的路径须加引号。真启动探针需要联网安装 npm / Python 依赖。

## 标准流程

用户要求生成并交付仓库时，默认完整执行：定 spec → 整批/历史查重预检 → 原子生成 → 静态与功能契约验收 → 生成后源码查重 → 干净克隆真启动与浏览器全功能 E2E → 登记指纹 → 发布 GitHub。
用户明确要求只做本地时不发布。生成或验收通过后继续完成已授权的链路，不额外停下来要求重复确认。
本技能不写题面、不建 A/B 分支、不跑 Claude，不修改其他出题技能的账号。

先检查 spec：

```bash
python3 <skill>/scripts/scaffold_repo.py --check-spec --spec /path/to/spec.json
```

首选完整流水线，多份 spec 可重复传入，按顺序完成：

```bash
python3 <skill>/scripts/run_pipeline.py \
  --parent "/path/to/父目录" \
  --spec /path/to/spec.json \
  --publish --confirm PUBLISH
```

不加 `--publish` 时只做本地生成与验收。可用 `--frontend vue|react`、`--backend python|java|none` 覆盖 spec。
`none` 生成纯前端项目，带 `frontend/src/api/local-service.ts` 和 `frontend/src/data/`，数据持久化到 localStorage。
形态由 `frontend/package.json` 的 `repoKind` 标记；不能靠缺后端目录推断成纯前端。
纯前端适合数据应用交互底座；不能宣称它执行了真实网络采集或真实数仓计算。

需要分步时：

```bash
python3 <skill>/scripts/scaffold_repo.py --parent "/path/to/父目录" --spec /path/to/spec.json --git-init
python3 <skill>/scripts/verify_repo.py --repo "/path/to/仓库根" --json
python3 <skill>/scripts/check_similarity.py --parent "/path/to/父目录" --repo "/path/to/仓库根" --json-out "/path/to/任务容器/similarity-report.json"
python3 <skill>/scripts/probe_startup.py --repo "/path/to/仓库根" --clean-clone --build --json-out "/path/to/任务容器/probe-report.json"
python3 <skill>/scripts/publish_github.py --repo "/path/to/仓库根" --execute --confirm PUBLISH
```

编号规则保留原逻辑：默认 `cc10001` 起，前缀可用 `--prefix` 改成 2～4 个字母，编号 10001～19999；`s11-` 是技能名前缀，不改变仓库编号格式。
父目录模式固定生成 `<父目录>/<编号>/<编号>/`：外层是任务容器，内层是仓库根；`--repo` 一律传内层。显式 `--out` 则直接以给定路径为仓库根。
扫描本地编号并查询配置 owner 下的远端编号，已有编号跳过；`--number` 显式编号冲突就报错。离线可 `--no-github-check`，此时只能声称检查过本地编号。

## 跨仓库不重复 / 不雷同

- 每份仓库保存规范化 `.s11-spec.json`；业务指纹忽略技术栈、端口、app_slug 和模块 key，换壳不能绕过。
- `run_pipeline.py` 在落盘前先对所有输入和历史仓库做整批预检；同批重复也会拒绝，不产生部分批次。
- 生成后 `check_similarity.py` 再比较业务语义与规范化业务源码，报告最近邻、分项分数和拒绝原因。
- 登记册位于 `<parent>/.s11-registry/registry.json`，只登记完成本地验收的仓库。同一父目录的流水线互斥，登记册写入另有文件锁，避免并发预检穿仓；不要为并行绕过互斥，需并行时用不同父目录并在合并前复查。旧仓库没有 `.s11-spec.json` 时无法自动回溯，应先补做人工/脚本归档，不能宣称已覆盖这些旧仓库。
- 阈值集中在 `scripts/check_similarity.py`；调整前用现有仓库分布做基线并跑 `tests/test_quality.py`，不要为让某批通过而临时放宽。

## 验收与发布约束

详见 [references/hard-rules.md](references/hard-rules.md)。必须真实运行，不许猜测过程或结果：

- G0 领域为数据工程，子方向合法。
- G1 前端 Vue/React；全栈有 Python/Java 后端，纯前端有本地数据层与明确形态标记。
- G2 Vite `server.open = false`；G3 `.gitignore` 覆盖依赖、缓存、产物与环境文件。
- G4 源码至少 2000 行；G5 容量至少 30，公式为 `min(35, 主体数 × 2, 12 × 3)`。
- G6 无密钥、超大文件与不相关禁用题域；G7 语法、入口、依赖声明和路径可移植。
- G8 前置检查逐模块功能锚点、创建/查询/动作/CSV 契约、锁文件与 E2E 资产；随后在干净克隆里真装依赖、启动服务、验证接口与前端代理、执行浏览器全功能测试并构建前端。测试逐模块覆盖查询命中/无结果/重置、筛选导出解析、完整登记、每个动作的状态变化和指标；纯前端另验刷新持久化。结束必须 `cleaned_up: true`，临时克隆必须删除。
- G9 跨仓库查重：整批 spec 预检 + 生成后业务源码复核，通过后才登记和发布。

s11 明确允许「数据分析与数据应用」，因此原规则的 `streamlit` / `csv-dashboard` / `报表统计` 不再作为禁用关键词。数据工程业务仍需落地，不能靠静态图表凑容量。技术栈限制保持 Vue/React，放开领域关键词并不增加 Streamlit 生成器。其余禁用方向保留，完整表见 `scripts/repo_rules.py`。

真实网页验收由生成仓库自带的 Playwright 用例执行，并检查浏览器脚本错误与 `/api` 4xx/5xx。报告只能证明其中列出的功能，不再把首页 HTML 或健康检查当作全功能可用证据。
长任务持续跟踪到结束，向用户报告有意义的进展；失败先修复，必要时报告具体阻塞。`--skip-probe` / `--skip-e2e` 仅供本地调试；`--publish` 与 `--skip-probe` 互斥，发布器还会核验绑定当前 HEAD 的 `probe-report.json` 与 `similarity-report.json`。

发布默认公开仓库、main 分支。要求工作区干净、静态验收通过；远端分叉拒推，不强推。
使用配置账号的凭证做 gh 与 git 请求；origin 不带 token，认证 header 只供当次命令使用。
推送后核对远端 main SHA 与本地 HEAD，并复核真实可见性。现有远端可见性不符合请求时先记录计划，只有证据、分叉等前置检查全通过后才实际调整；不要向未经授权的现有仓库发布。
`--remote-url` 仅供非 GitHub 远端或本地裸仓库测试，不作为绕过 GitHub owner 配置的通道。

交付报告至少列仓库根、领域/子方向、体量/容量、静态与启动实测结果、GitHub URL、owner/认证账号、远端 SHA 对齐结果；未执行项如实注明。

## 技能自检

```bash
python3 -m unittest discover -s <skill>/tests -v
```

用例覆盖生成与验收、编号、纯前端、可见性、流水线、本地裸仓库推送、spec 预检、跨仓库相同/雷同反例、功能契约、HEAD 证据，以及 s11 领域限制与账号选择。
测试不创建 GitHub 仓库；真实账号有效性单独用 `github_settings.py` 检查。
