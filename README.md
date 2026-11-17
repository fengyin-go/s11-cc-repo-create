# s11-cc-repo-create

为 solo11 生成紧凑数据工程底座，随后交给 `s11-gsb-prompt-create`。2026-11-17 起以 **36 个通过当前下游 H1/H2/H3 v3 的题位**为交付下限；源码行数和CRUD页面只做辅助检查，正式容量仍由真实题位计划决定。

- **容量优先**：12个可用模式各3条；36张正式机会卡+12张备用卡→源码/领域/类型/困难/主题审计→至少36个合格题位；不编造缺陷、不放宽闸门。
- **提示词机会地图**：机会卡写到生成仓库外，记录业务故事、数据链、冲突轴、锚点和自然切入角度，不保存最终题面。
- **无人值守**：自主选型、修复、清理本批拥有的生成产物和执行获授权的新仓发布；不二次询问PUBLISH，不接管既有项目/陌生远端。
- **双层并行**：多agent分仓实现，流水线 `--jobs` + `--probe-jobs` + `--publish-jobs` 分资源并行。
- **固定验证预算**：整体测试一轮→集中修复至多一轮→最终只验前后端启动/代理联通；不再补跑全量E2E和生产构建。支持prepared仓恢复、下载缓存、发布步骤单独重试和阶段计时。
- **紧凑布局**：18个模块各保留独立领域服务；前端只生成一个共享`WorkbenchView`，后端只生成一套通用资源路由。默认代码计行<=6000、基线约55个文件，硬上限60。
- **容量材料外置**：`<container>/.s11-meta/` 保存topic map、候选池和briefs，不进入可发布源码仓库；map直接绑定最终源码HEAD。
- **下游v3适配**：仓外旧v2 map在容量检查时自动原子升级为v3，补齐约束编号、`decision_axis`和`workload_design`；报告记录`map_schema_upgraded`。

先读 [SKILL.md](SKILL.md)。容量设计见 [references/capacity-design.md](references/capacity-design.md)，并行/测试预算见 [references/parallel-performance.md](references/parallel-performance.md)。

草稿：

```bash
python3 scripts/run_pipeline.py --parent /path/to/parent --spec /path/to/spec.json --skip-probe
```

agent增强真实业务并提交题位材料后：

```bash
python3 scripts/check_prompt_capacity.py --repo /path/to/repo \
  --map /path/to/parent/.s11-meta/repo-topic-map.json
python3 scripts/build_prompt_opportunities.py --repo /path/to/repo \
  --briefs /path/to/prompt-opportunity-briefs.json \
  --out /path/to/parent/prompt-opportunities/repo-name.json
python3 scripts/run_pipeline.py --parent /path/to/parent \
  --prepared-repo /path/to/repo --jobs 4 --probe-jobs 2 --publish-jobs 1 --publish
```

脚本不负责开启agent；由执行本skill的主控使用宿主多agent工具委派。题位硬闸通过不代表最终提示词/平台质检通过；最终题面与历史/平台去重仍在下游完成。

自检（不建真实远端、不消耗模型、不安装生成项目依赖）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

最终 `probe_startup.py` 默认 `--profile startup`；发布器接受完整启动联通证据，不要求E2E/build。`--profile full`（可加 `--build`）仅供首次测试或用户显式要求，不由最终流水线触发。测试和修复预算由执行本skill的主控记录到仓外 `verification-round.json`，脚本不会自行修复或重置预算。

新仓默认 `--layout compact`。`--layout legacy` 只用于旧模板回归，不用于正式发布。纯前端布局仍生成18个独立`frontend/src/domain/*.ts`领域主体，因此不能以“共享一个页面”为由降低容量。

`domain_chain.operation`只能使用下游批准的数据工程操作词。`batch_registry`、`error_handler`、`data_retry`、`collection_monitor`、`incremental_tracker`、`lineage_graph`、`template_mgr`和`archive_store`的固定映射见 `references/capacity-design.md`。
