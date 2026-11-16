# s11-cc-repo-create

为solo11生成数据工程底座，随后交给 `s11-gsb-prompt-create`。2026-11-16起以**至少35个通过当前下游H1/H2/H3 v2的题位**为交付下限；旧源码行数、主体数和CRUD脚手架只做草稿检查。

- **容量优先**：约42个原始候选→源码/领域/类型/困难/主题审计→至少35个合格题位；不编造缺陷、不放宽闸门。
- **无人值守**：自主选型、修复、清理本批拥有的生成产物和执行获授权的新仓发布；不二次询问PUBLISH，不接管既有项目/陌生远端。
- **双层并行**：多agent分仓实现，流水线 `--jobs` + `--probe-jobs` + `--publish-jobs` 分资源并行。
- **固定验证预算**：整体测试一轮→集中修复至多一轮→最终只验前后端启动/代理联通；不再补跑全量E2E和生产构建。支持prepared仓恢复、下载缓存、发布步骤单独重试和阶段计时。

先读 [SKILL.md](SKILL.md)。容量设计见 [references/capacity-design.md](references/capacity-design.md)，并行/测试预算见 [references/parallel-performance.md](references/parallel-performance.md)。

草稿：

```bash
python3 scripts/run_pipeline.py --parent /path/to/parent --spec /path/to/spec.json --skip-probe
```

agent增强真实业务并提交题位材料后：

```bash
python3 scripts/check_prompt_capacity.py --repo /path/to/repo
python3 scripts/run_pipeline.py --parent /path/to/parent \
  --prepared-repo /path/to/repo --jobs 4 --probe-jobs 2 --publish-jobs 1 --publish
```

脚本不负责开启agent；由执行本skill的主控使用宿主多agent工具委派。题位硬闸通过不代表最终提示词/平台质检通过；最终题面与历史/平台去重仍在下游完成。

自检（不建真实远端、不消耗模型、不安装生成项目依赖）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

最终 `probe_startup.py` 默认 `--profile startup`；发布器接受完整启动联通证据，不要求E2E/build。`--profile full`（可加 `--build`）仅供首次测试或用户显式要求，不由最终流水线触发。测试和修复预算由执行本skill的主控记录到仓外 `verification-round.json`，脚本不会自行修复或重置预算。
