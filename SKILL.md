---
name: s11-cc-repo-create
description: 为 solo11 全自动生成可供 s11-gsb-prompt-create 出题的紧凑数据工程底座仓库，默认每仓至少36个通过下游H1/H2/H3 v3硬闸的真实题位，并在仓库外生成容量材料和提示词机会地图。支持共享前端/API、18个独立领域服务、多agent分仓并行、无人值守修复与清理、整体测试一轮、集中修复一轮、最终启动联通及GitHub发布。用于单仓/批量造项目和扩充出题容量；不代替下游最终提示词、全局查重、模型运行或平台提交。
---

# S11 高容量底座仓库生成

`<skill>` 指本文件所在目录。**生成器只是脚手架，不是成品生成器**；不得把18个互不相干的CRUD页、2000行代码或旧容量公式当作36道合格难题的证明。

## 默认目标与边界

- 领域固定 `数据工程`；子方向只能是 `数据采集、爬虫与RPA`、`数仓与数据湖`、`数据分析与数据应用`。未指定方向/名称/技术栈时自主选择，不询问。
- 默认每仓 **36个通过题位硬闸的候选**：12个可用模式各3条。设计时准备36张正式机会卡和12张同模式备用卡，共48个候选故事；储备不是有效容量。spec 至少18个模块是数学下限，实践建议20～24个真实业务topic。
- Vue/React + Python/Java，或有真实本地数据计算与持久化的纯前端。默认 Vue + Python，不为变技术栈制造跨仓差异。纯前端不能假装真实网络采集/数仓服务。
- 五类任务沿用下游准确名称和slug，`capacity-first`；30/30/30/5/5只是建议，不编造Bugfix或迁移目标，不人为埋bug凑数。
- 完成指：真实业务实现 + >=36题位通过 + 提示词机会地图校验 + 查重 + 紧凑体积预算 + 一轮整体测试/至多一轮集中修复 + 干净克隆启动联通 + 清理 + 用户要求的发布/本地交付。某仓不足就补业务、换候选，不改闸、不以备注放行。
- 默认使用compact布局：18个模块各自保留独立领域服务文件；前端只生成一个共享`WorkbenchView`，后端只生成一套通用资源路由。禁止重新复制18套完整页面、router或测试。
- 默认代码计行<=6000、普通clone文件硬上限60（基线约55，给真实业务测试留少量余量），并校验18个领域主体仍可派生36容量。`legacy`布局只用于旧模板回归，不是新仓默认交付。
- 只生成底座、题位储备和提示词设计素材，不创建36个任务GitHub仓、不写最终tasks.xlsx、不跑模型、不提交平台。最终题面和历史查重仍归下游，不能承诺平台必过。
- 生成提示词机会地图时必须写到生成仓库之外，默认 `<parent>/prompt-opportunities/<project_id>.json`；不得提交、推送或写入干净克隆。

## 1. 先读真实下游，再反向设计

在本机定位 `s11-gsb-prompt-create`（默认 `~/.codex/skills/s11-gsb-prompt-create`；迁移机器可用 `S11_PROMPT_SKILL` 或显式路径）。每轮读其 `SKILL.md` 及：

- `references/difficulty-hardening.md`：H1两种不同且互相作用的硬约束；H2第二模块真正消费/写入/持久化数据产物；H3同一冲突轴至少两种可行方案，题面留真实选择权。
- `references/domain-fit.md`、`references/semantic-v2-topic-map.md`、`references/generation-rules.md`：领域输入→加工→输出→可观察结果、主题/模式容量、跨项目去重。

**不复制冻结判据代替当前下游。** 下游缺失/规则不兼容时不得伪造合格结论；先保留可恢复状态，其他仓继续，最终报告阻塞。

读 [references/capacity-design.md](references/capacity-design.md) 和 [references/prompt-opportunities.md](references/prompt-opportunities.md)，先做候选矩阵、业务故事卡与真实数据流设计，再写代码。按业务对象划分约20～24个topic；别名、文件和页面不算独立业务主体。至少形成8～10组真正贯通的输入→转换→版本化产物→下游消费链路。普通概览、详情、下载按钮不是第二模块。缺失能力允许留给功能开发题，但支撑模块及数据链路必须已经存在。

## 2. 无人值守授权与异常决策

把用户的“生成N个仓库”视为执行本流程的完整授权。默认一直执行到约定交付（通常含新建底座仓的公开发布）；仅用户指定本地/不发布时停止在本地合格交付。**不在选型、spec、演练、测试、修复、清理、提交本次生成仓或发布新仓之前提问。** 进度通报不是确认请求。

- 自主补齐可推导参数、修复本次生成源码/测试、重建本次失败产物；每仓只做一轮整体测试，汇总后至多一轮集中修复，再做一次最终启动联通检查；最终仍失败就保留日志和恢复命令、标记阻塞，继续其他仓。不按根因分别重置轮次，不循环全量重造或掩盖失败。
- 可以直接删除本批明确拥有的生成容器、临时克隆、失败中间文件、进程和测试产物，无需确认。先解析真实路径并核对工作清单；禁止跟随符号链接删出容器，禁止删除父目录本身、用户原始输入/已有项目、其他批次和陌生远端。保留用户输入spec及必要失败证据。
- 不要求重复输入PUBLISH；脚本的 `--publish` / `--execute` 即表示执行，旧 `--confirm PUBLISH` 仅兼容。
- GitHub创建成功后将真实仓库ID/owner/本地路径归属凭据保存在仓外；恢复先核对该凭据及当前远端ID，不能把空仓或同名当成本批所有。历史仓缺凭据时只读保留、不自动认领。账号认证失败、无权限、遇到陌生仓重名/远端分叉时保守停止该仓，不静默换账号、强推或接管。上游按本技能机器配置认证（见 `references/local_environment.md`），不能把秘密写到日志；下游另要求当前 `gh api user` 身份且禁止 `fengyin-solo`，交接时报告身份不兼容，不擅自切账号。
- GitHub明确限流才等待Retry-After/指数退避，60秒至900秒、总预算2小时；等待期间推进其他仓。认证/重名不是限流，不盲重试。只重试失败的发布步骤，不重跑构建/E2E。
- 工具不存在/资源不足则自动采用可执行的保守方案并报告。不能靠skill文字绕过宿主必须执行的权限审批，也不能假称开了不存在的agent。

主控agent负责上面的自动决策、分派、修复和限流等待；脚本是确定性阶段执行器，不会自行启动编码agent或无限重跑。不要虚构 `--agents` / `--max-repairs` 参数，也不要因脚本一次失败就把修复交回用户。

## 3. 批量必须多agent分仓并行

用户要求多仓时，使用可用的多agent工具：默认同时2～4个仓库worker（上限取工具/CPU/内存可承受值）；一仓一个worker，主控负责批次预检、唯一编号和结果汇总。单仓无需为凑agent而增加协调开销。

读 [references/parallel-performance.md](references/parallel-performance.md)。主控先统一预检spec与历史差异性，串行分配编号/生成草稿，再并行委派。每个worker只写自己容器；禁止多个agent写同仓、共同spec、登记册或公共结果文件。worker完成实现、一轮整体测试及集中修复后及时归还，主控只做最终启动联通验收；不要等待所有设计全部完成才开工第一仓。

```bash
# 快速批量草稿（只是draft；不代表完成/可发布），内部串行保护编号分配
python3 <skill>/scripts/run_pipeline.py --parent <parent> \
  --spec <a.json> --spec <b.json> --skip-probe --jobs 4
```

读取汇总中每仓路径后，把独占容器交给worker实现真实数据处理、跨模块消费及新增业务行为测试，编写题位证据并提交main。若草稿步骤已登记指纹，不得因此把未完成仓认作合格；恢复保持原路径，预检仅排除该路径的自身登记项；不要移动容器后反复重试或清空登记册。确需搬迁时先核实身份并迁移本批对应登记项，不按同名ID排除陌生仓。

**worker任务约定：** 给目标路径、spec、下游路径及容量目标；要求直接写代码，完成一次整体测试并集中修复一次；输出修改文件、测试基线SHA/命令/结果、修复清单、候选淘汰原因、准备好的仓路径。不在每个模块后反复测试，不由主控再跑一遍同样测试。不得提交平台，不自行创建任务题库仓。共享数据流先由该仓worker完成，不把紧耦合模块再拆给其他agent。

## 4. 落地容量契约，禁止纸面凑数

源码仓库只提交运行代码和精简spec，不提交容量设计材料。容量材料位于任务容器、仓库外：

```
<container>/
  <project_id>/             可发布源码仓库
  .s11-meta/
    candidate-pool.json     原始候选及淘汰/储备原因，不计入合格数
    repo-topic-map.json     下游semantic-v2格式；>=36条task_plan
    prompt-opportunity-briefs.json
```

每题包括下游完整 `domain_fit` v2、`difficulty_design` v3、真实任务意图、跨模块带行号连续源码原文；再按 [capacity-design.md](references/capacity-design.md) 填写 `support_evidence`。Bugfix只能来自可复现的真实缺陷；不得为候选数埋缺陷。保留不同数据结果/约束交互/设计冲突轴，不能只改对象名、复用一个重试模板。

v3硬字段不能省略：约束写入唯一`C1`/`C2`编号；`design_tradeoff.decision_axis`取下游批准枚举；每题写`workload_design`（>=55次有效工具调用、>=4切片、>=2模块、不同数据结果、独立验证、>=2验证路径）。`domain_chain.operation`只能使用采集/接入/抓取/爬取/导入/读取/解析/清洗/转换/标准化/关联/聚合/去重/校验/入湖/入库/落库/存储/写入/查询/计算/调度/回填/迁移/导出/切分/归并/合并/归档/水位。`batch_registry`、`error_handler`等固定映射见[capacity-design.md](references/capacity-design.md)。

源码和容量 map 稳定后，用 `scripts/build_prompt_opportunities.py` 在仓库外生成提示词机会地图。机会地图记录业务故事、自然切入角度、H1/H2/H3设计事实、源码锚点和备用卡；不保存最终提示词，也不允许写进生成仓库。

源码稳定并提交后，把仓外map的 `base_commit` 直接写成最终源码HEAD。容量检查通过Git回查该HEAD中的源码锚点，不要求为map再制造元数据提交。业务源码变化后必须更新 `base_commit`、全部锚点并重跑容量闸。旧仓库仓库内 `.s11-capacity/` 继续兼容读取，但新仓不得放入源码仓库。

```bash
python3 <skill>/scripts/check_prompt_capacity.py --repo <repo> \
  --map <container>/.s11-meta/repo-topic-map.json \
  --downstream-skill <prompt-skill> --json-out <container>/prompt-capacity-report.json
```

该闸调用真实下游validator（领域/类型/H1/H2/H3），不调用模型或建题。结构通过不等于语义全对：主控/独立reviewer还需根据源码复核真实数据流、可执行性、开放设计轴及同质化。失败补业务或淘汰替换，不伪造锚点/缺陷、不写假的pass报告。下游模式上限每种3、每topic 1～3及单仓上限均保留；12个可用模式必须各3条，不足36道时明确未完成，不能降级交付。

仓外旧v2 map在首次检查时会由`topic_map_v3.py`原子补齐v3结构字段，报告记录`map_schema_upgraded: true`；仓库内旧布局map须显式加`--upgrade-map`再提交。自动补齐只解决结构，不替代真实workload、源码锚点和语义review。

## 5. 固定预算：测一轮 → 改一轮 → 只验启动联通

每仓独立计数，由worker与主控共享预算，换agent、换根因、重进流水线都不能重新计数：

1. **实现完后整体测试一轮。** 先提交待测源码基线，再一次性运行已有自动化测试、主要跨模块数据链路断言、必要类型/编译检查，并收集静态/容量/查重/语义review问题。提前写好新增业务断言，但不每写一个模块就全测。每个测试命令只执行一次，不开watch/自动重试；命令失败继续收集其他独立项的问题，不边测边改。没有现成测试时补最小关键链路断言，不能凭空填pass。浏览器全页遍历和生产构建不作为额外强制项；确需使用，只能计入这同一轮。
2. **集中修复至多一轮。** 将这一轮发现的问题合并成一张清单，一次性修改源码/测试/启动配置和容量锚点；无问题则跳过修复，不制造改动。不得删失败测试、放宽断言或改门禁来换通过。之后不重跑全量测试、逐页E2E或生产构建；必要静态/容量/查重仅核对最终材料，不升级成新一轮功能测试/修复。
3. **最终只验启动和联通一次。** 提交修复后的源码与容量元数据，在干净克隆中安装依赖，启动前后端，检查前端首页/入口脚本、后端健康接口、前端到后端代理链路。纯前端只验前端，不强造后端；不再跑完整E2E、遍历业务接口或生产构建。失败就记录阻塞，继续其他仓，不进入第二轮修复；不能将失败仓发布成已完成。

在仓外 `<container>/verification-round.json` 记录 `test_rounds: 1`、`repair_rounds: 0或1`、测试基线SHA/命令/退出码/日志、集中修复清单；最终报告另外记录最终HEAD的smoke结果。**修复前测试结果不冒充修复后全量通过**。独立语义review合并进首次问题清单，不另起一套全量动态验收。脚本只负责最终smoke与材料门禁，不会代主控执行首次测试或自动修复循环。

```bash
# worker完成一轮整体测试和集中修复并提交后，主控并行做最终启动联通/发布
python3 <skill>/scripts/run_pipeline.py --parent <parent> \
  --prepared-repo <repo-a> --prepared-repo <repo-b> \
  --jobs 4 --probe-jobs 2 --publish-jobs 1 --keep-going --publish \
  --json-out <parent>/pipeline-report.json

# 单仓最终smoke；默认profile即startup，无浏览器E2E/构建
python3 <skill>/scripts/probe_startup.py --repo <repo> --clean-clone \
  --profile startup --json-out <container>/probe-report.json
```

`--spec`仅生成草稿，不能用裸脚手架冒充高容量成品。`--prepared-repo`恢复原仓、不覆盖源码，但不重置测试/修复预算。`--skip-probe` / `--skip-install`只用于调试，不用于正式发布。发布器检查当前HEAD的**启动联通**与查重证据，重新核对静态/容量，但不补跑E2E或构建；`--skip-verify`不能绕过正式门禁。`--profile full`仅为显式首次整体测试保留，`--build`须与full一起用，绝不在最终流水线自动开启。

只复用依赖下载缓存，不跨平台搬native node_modules/虚拟环境。最终probe报告标明 `verification_scope: startup-connectivity`，不缓存旧smoke成功结果、不声称浏览器全部业务正确。仅发布网络重试可复用同HEAD的现有报告，不重跑流水线。日志在仓外，临时克隆删净，只清本次PID/进程组。详见 [parallel-performance.md](references/parallel-performance.md)。

交接给下游时，复制已审题位图到批次目录；它本来就在源码仓库外，`base_commit` 应已等于最终源仓HEAD。重新跑下游validator及历史查重，不改写已冻结源仓。题位设计是可复用储备，不是绕过下游重新核验的通行证。

完成后给：每仓路径/URL、SHA、布局、代码计行/文件数、领域/子方向、有效候选数与储备数、提示词机会地图路径/哈希、一次测试结果/一次修复清单/最终启动联通、静态/容量/查重/清理结果、各阶段耗时和缓存命中、发布结果、实际开了多少agent、阻塞与恢复命令。仅修改本skill时只报告实际改动及自测，不能声称已经生成36道题或已测到端到端提速倍数。

## 技能自检

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s <skill>/tests -q
```

只修改文档不跑生成项目全量E2E；脚本改动先跑对应单测，最终一次完整回归。规则细项见 [hard-rules.md](references/hard-rules.md)，spec字段见 [spec-format.md](references/spec-format.md)。下游难度与容量规则优先于旧静态公式。
