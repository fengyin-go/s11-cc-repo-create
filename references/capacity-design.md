# 反向容量设计：36 道困难题，不是 36 个页面

## 先区别三个数字

1. **理论扫描容量**：`min(36, 主体×2, 12模式×3)` 只是文件/主体扫描下限，保留兼容静态检查，不证明H1/H2/H3。
2. **原始候选池**：36 张正式机会卡 + 12 张同模式备用卡，共 48 个独特业务切入点；备用卡不发布为合格任务。
3. **合格题位容量**：至少 36 条通过真实下游 semantic-v2、类型意图、领域 v2 和困难 v3 的计划，且 12 个可用模式每个正好 3 条。正式提示词还须在下游通过 G16/G19、模式/主题及历史/平台全局查重，不能预支平台通过结论。

若历史占用导致后续候选不足，回到业务补足/替换，而不是给相同题面换名。每次下游规则更新重新读当前实现及校准案例，不放宽模式上限、比例或困难契约。

## 设计顺序

1. 选择具体数据业务，不选通用平台后台；先列真实输入样例、加工、持久化产物及下游消费结果。
2. 建立约 20～24 个真实业务 topic；各 topic 1～3 席、同 topic 跨 family。不得把一份数据拆成 20 个别名 topic。
3. 用 8～10 条非同质数据流覆盖业务对象，形成源码中真的存在的消费者、写入者、转换者，而不是所有模块均独立读写一张同构 JSON 台账。
4. 准备候选，先写业务故事卡、H1/H2/H3 与可失败场景，再实现最低必要底座。功能开发应有真实缺失能力；优化有可工作的现有能力与测量基线；Bugfix 只承认真实复现；重构说明行为不变；迁移有真实依赖/版本/存量目标。禁止先编 36 个 bug。
5. 给每个候选确认至少两个真实模块和独立数据结果、约束交互与未被题意固定的设计轴；如果将对象换成“普通配置”仍原样成立，优先淘汰。
6. 同时创建确定性的业务测试：脏行、重复批次、迟到数据、冲突版本、写入中断、恢复和下游可见性等按真实能力选用。保留原始数据和预期结果，不能只断言状态码或mock调用次数。
7. 稳定源码后逐题读源码、冻结锚点，运行容量闸。候选不足先补真实链路/计算能力，不加无关页面或复制服务。

## 可组合的业务能力（不是必须全实现的通用模板）

| 数据阶段 | 真实底座能力 | 困难题潜力来自 |
|---|---|---|
| 接入/抽取 | 文件/本地fixture适配、字段解析、批次/游标、来源数据 | 数据身份、断点语义、重复与迟到输入 |
| 加工/质量 | 清洗、schema映射、坏行隔离、质量规则、转换依赖 | 版本与失败恢复相互影响，产物一致性 |
| 落地/发布 | 分区、可追溯快照、变更集、可用版本 | 部分写入、历史可见性、重新加工 |
| 消费/分析 | 指标计算、分群、数据集/文件生成、消费记录 | 实际消费版本、口径与数据切片、重算 |

表中事务/游标/快照不是每题都必须套用的三板斧；不同候选应有不同失败原因和用户结果。同一个“重试+导出+快照”换20个对象，不算20道独立困难题。

## 文件契约

容量设计材料位于任务容器、源码仓库外：

```text
<container>/.s11-meta/
  candidate-pool.json
  repo-topic-map.json
  prompt-opportunity-briefs.json
```

`.s11-meta/candidate-pool.json` 保存候选 ID、topic、类型、主要模块、数据产物、约束交互、设计轴、状态、淘汰/储备原因。可用业务语言，不写最终任务题面/验证命令。新仓不得把这些材料提交进源码仓库。

`.s11-meta/repo-topic-map.json` 直接使用**当前下游格式**：

- `profile: gsb-semantic-v2`、`version: 1`、`difficulty_contract_version: 3`、`type_policy: capacity-first`、真实domain/subdirection。
- topics写domain_objects/domain_results/evidence/seat_capacity；task_plan含sequence、slug/task_type、topic/family/angle、完整domain_fit/difficulty_design。H1两种约束有交互，H2不是观察面，H3允许两种真正可行裁决。
- 每条task_plan再附本skill的 `support_evidence`（每项字段为 `module`、`role`、`artifact`、`behavior`、`path`、`start_line`、`end_line`、`excerpt`；主模块role用producer/writer/orchestrator，次模块名称/role/artifact与difficulty_design一致，excerpt为指定行区间完整连续原文）：两条以上指向真实不同业务模块的连续源码原文、行号与实际数据产物。测试、文档、注释/空壳不能替代业务源码。
- 不要把support_evidence只放在README后宣称通过；源码锚点是机器可回查的必要证据，语义仍需逐题复核。

**最终HEAD绑定：** 先提交业务源码得到最终源码HEAD；仓外map的`base_commit`直接写该HEAD。检查器通过Git回查锚点并验证工作树干净，不需要再为map制造一个“只含元数据”的提交。业务源码再变时必须刷新map、锚点、机会地图和全部证据。旧仓库仓库内`.s11-capacity/`只保留兼容读取。

## v3 difficulty_design 必填结构

顶层`difficulty_contract_version`和每个`task_plan[].difficulty_design.version`都必须为3。

每条约束必须使用稳定且不重复的`C1`、`C2`编号：

```json
{
  "constraints": [
    {
      "id": "C1",
      "kind": "transaction_consistency",
      "statement": "主数据版本与下游消费记录必须同时可见",
      "failure_if_omitted": "下游会读取到没有来源版本的半成品数据"
    },
    {
      "id": "C2",
      "kind": "failure_recovery",
      "statement": "恢复任务只能补写缺失记录，不能覆盖上一份完整产物",
      "failure_if_omitted": "重试会把已经可用的数据版本回退"
    }
  ]
}
```

`design_tradeoff.decision_axis`只能取`source_of_truth`、`effective_time`、`completion_precedence`、`version_adoption`、`retention_vs_reclaim`、`recovery_vs_cleanup`、`old_vs_new_priority`、`authority_conflict`、`scope_boundary`、`partial_success_boundary`、`concurrency_conflict`、`migration_switch`、`other`。

每个题位还必须有`workload_design`：

```json
{
  "version": 1,
  "expected_effective_tool_calls": 60,
  "call_floor_rationale": "跨两个模块完成实现需要读取源码、设计接口、补齐失败恢复并验证两条独立数据链路，预计不少于60次有效工具调用。",
  "work_slices": [
    {
      "module": "主业务模块",
      "implementation": "实现主数据创建、校验、写入和状态流转",
      "data_result": "主版本业务数据记录",
      "verification": "验证主版本字段完整且状态可以追溯"
    },
    {
      "module": "主业务模块",
      "implementation": "补齐去重、失败恢复和版本冲突处理",
      "data_result": "业务数据版本校验报告",
      "verification": "验证重试不会覆盖上一份完整产物"
    },
    {
      "module": "下游消费模块",
      "implementation": "读取已确认产物并生成下游消费记录",
      "data_result": "下游消费明细记录",
      "verification": "验证消费模块只读取已确认版本"
    },
    {
      "module": "下游消费模块",
      "implementation": "落库异常审计、处理回执和追溯信息",
      "data_result": "异常审计回执记录",
      "verification": "验证回执与主版本数据可以逐条追溯"
    }
  ],
  "verification_paths": [
    "backend/tests/test_pipeline.py::test_cross_module_flow",
    "backend/tests/test_pipeline.py::test_domain_snapshot_contract"
  ]
}
```

硬性下限：有效工具调用>=55、理由>=12字、至少4个切片、至少2个模块、每条切片有不同数据结果和独立验证、至少2条不同验证路径。`check_prompt_capacity.py`会对仓外旧map自动补齐结构字段；语义内容仍需独立review，不能把自动补齐当业务审核。

### cc10014 操作词映射

生成领域服务和`domain_fit.domain_chain.operation`只能使用下游批准词：采集、接入、抓取、爬取、导入、读取、解析、清洗、转换、标准化、关联、聚合、去重、校验、入湖、入库、落库、存储、写入、查询、计算、调度、回填、迁移、导出、切分、归并、合并、归档、水位。

| 模块key | 原词 | 生成值 |
|---|---|---|
| `batch_registry` | 批次 | 校验 |
| `error_handler` | 处理 | 校验 |
| `data_retry` | 重采 | 采集 |
| `collection_monitor` | 监控 | 查询 |
| `incremental_tracker` | 水位 | 调度 |
| `lineage_graph` | 血缘 | 关联 |
| `template_mgr` | 模板 | 标准化 |
| `archive_store` | 归档 | 归档 |

## 独立语义审核

在唯一一轮整体测试阶段，对原始代码、题位和提示词机会卡做一次独立 review（可复用已完成 worker 做别仓 review，不让其审核自己）；不要把“应该通过 36 道”的答案塞给 reviewer。逐题查：

- 主任务的领域数据结果是否真实；类型与实际缺失/错误/优化目标是否一致？
- 两个模块是否真的读写同一产物，还是详情和概览两个展示？
- H1两项约束是否相互影响，遗漏后有真实坏结果？
- H3两方案能否都成立，是否被另一句先写死？
- 同topic/family/mode及历史是否重复？同质化储备不得计数。

问题合并进唯一一轮集中修复清单；修复后只刷新锚点并运行容量材料硬闸，不追加一轮全量动态测试或第二轮修复。输出逐题问题及源码证据，不用一个“难度复核 pass”覆盖全部题位。此审核不代替下游最终题面写表的硬闸。

最后用 [prompt-opportunities.md](prompt-opportunities.md) 生成仓库外提示词机会地图。机会地图中的备用卡也必须有真实源码依据；不得把淘汰卡、虚构缺陷或仅换名字的候选写进正式计划。

## 交接到下游的最终SHA

交接时直接把仓外map复制到批次目录；`base_commit`已经是最终源仓HEAD。保留有效题位、刷新规则及历史去重并再跑下游完整硬闸，不回写源仓制造额外提交。
