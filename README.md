# s11-cc-repo-create

数据工程领域的出题底座仓库生成技能。使用入口见 [SKILL.md](SKILL.md)。

- 子方向任选一个：数据采集、爬虫与RPA；数仓与数据湖；数据分析与数据应用。
- Vue/React 前端，Python/Java 后端，或带本地数据层的纯前端形态。
- 默认示例 `examples/spec-data-engineering.json`，18 个数据工程模块。
- 默认发布账号 `fengyin-go`，每台电脑通过 `config/github.local.json` 配置 owner/account，凭证从该电脑的 gh 登录读取。
- 整批/历史跨仓库查重、原子生成、静态功能契约、干净克隆启动、Playwright 全功能 E2E 与 HEAD 绑定发布证据。
- 查询、登记、状态动作、筛选 CSV 导出和动态指标均有逐模块浏览器断言；前端依赖携带锁文件。

```bash
python3 scripts/github_settings.py
python3 -m unittest discover -s tests -v
python3 scripts/run_pipeline.py --parent "/path/to/父目录" --spec examples/spec-data-engineering.json --publish --confirm PUBLISH
```

本目录为原 `cc-repo-create` 的独立副本，不含原 `.git` 或凭证，不改变原技能。
