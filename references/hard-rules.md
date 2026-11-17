# 硬性要求与容量口径

> 2026-11-17：以下 G5 旧公式只保留为草稿静态下限。正式底座另须通过 `check_prompt_capacity.py`：至少 36 个真实下游领域/类型/H1/H2/H3 v3 题位，且 12 个可用模式各 3 条。容量材料位于源码仓库外，CRUD 页数与源码行数不能替代。详见 [capacity-design.md](capacity-design.md) 和 [prompt-opportunities.md](prompt-opportunities.md)。

这个 skill 生成的每个仓库都要过 G0～G7 静态闸门与 G8 功能契约前置，全部由 `scripts/verify_repo.py` 机器复核；
再由 `scripts/probe_startup.py` 做最终干净克隆启动联通检查（不再全量E2E/构建），由 `scripts/check_similarity.py` 做 G9 跨仓库查重。任何一道不过就不算生成完成。

| 闸门 | 要求 | 由谁判 |
|---|---|---|
| G0 领域 | `.s11-domain.json` 的 domain 为数据工程，subdirection 为三个允许值之一；实际业务方向另须审核 | `verify_repo.py` |
| G1 结构 | 前端 Vue 或 React、后端 Java 或 Python，双端各有依赖清单与启动入口；默认共享`WorkbenchView`和一套资源路由。纯前端不要求后端，但必须有`local-service.ts`、`frontend/src/domain/*.ts`和本地持久化 | `verify_repo.py` |
| G2 启动 | 前端 dev server 关掉「自动打开页面」 | `verify_repo.py` |
| G3 忽略 | 根目录 `.gitignore` 覆盖依赖 / 缓存 / 构建产物 / 日志 / 环境文件 | `verify_repo.py` |
| G4 体量 | 源码计行 2000～6000；普通clone文件基线约55、硬上限60；不含依赖、构建物、锁文件和仓外容量材料 | `verify_repo.py` |
| G5 草稿容量 | 静态公式 >=36，不证明困难题有效容量 | `verify_repo.py` |
| G6 合规 | 不是禁用题型仓库，无密钥、无超大文件、无违规业务域，且题域不命中 0731 严禁出题清单 | `verify_repo.py` |
| G7 环境 | 基础环境自洽：Python 全量可编译、后端入口能 import、依赖都声明、没有写死本机路径 | `verify_repo.py` |
| G8 功能 | 静态逐模块功能契约；一次整体测试/至多一次集中修复后，干净克隆装依赖、起服务、验首页/入口/health/代理；不重复全量E2E或构建；纯前端跳过不存在的后端/代理；进程和临时克隆均清理 | `verify_repo.py` + `probe_startup.py` |
| G10 困难容量 | >=36 个当前下游语义题位；12 模式各 3 条；领域/类型/H1/H2/H3 v3及业务源码证据；仓外map直接绑定最终HEAD | `check_prompt_capacity.py`，publisher 每次正式发布前复核 |
| G9 差异 | 整批 spec 与历史业务指纹预检，生成后再比较规范化业务源码；相同或超过相似度阈值的仓库拒绝交付 | `check_similarity.py` |

## G7 基础环境自洽（专治「本地能跑、换台机器就炸」）

`verify_repo.py` 的 `check_env_preconditions` 逐项验：

| 检查 | 为什么 |
|---|---|
| 所有 `.py` 都要能 `compile` | 语法错误必须在交付前发现 |
| 不许把 `true` / `false` / `null` 当 Python 变量 | 语法合法、一 import 就 `NameError`（真踩过：种子数据用 JSON 序列化写进 `.py`） |
| 真 import 一次后端入口（`app.main`） | 编译抓不到「少依赖」「写错模块名」；缺的包若已在 requirements 里声明，就记 `deferred` 交给启动探针 |
| 后端 import 的三方包必须写进 `requirements.txt` | 换台机器少装一个包就起不来 |
| 前端 import 的三方包必须写进 `package.json` | 同上 |
| 前端要有 `dev` 与 `build` 两个脚本 | 起不来、也验不了 |
| 源码里不许出现 `/Users/...`、`/home/...`、`/private/tmp/...` 这类本机绝对路径 | 写死路径换台机器必炸 |

## G8 真启动怎么算通过

```bash
python3 scripts/probe_startup.py --repo <repo> --clean-clone --profile startup
```

`--clean-clone` 不是可选装饰：它先把仓库 `git clone` 到临时目录再跑，因此同时证明了三件事——

1. 依赖目录**没有**被提交进仓库（`node_modules` / `.venv` / `dist` / `build` 都不在克隆里）；
2. 仓库里没有「未提交但必需」的文件（克隆出来的工作区是干净的）；
3. 现场装依赖就能起来，不靠任何本地残留。

通过的判据：后端 `/api/health` 正常；前端首页有挂载点、入口脚本可加载；
全栈从前端端口走 `/api/health` 返回健康响应，两个服务均由本次进程组监听。
纯前端不要求后端/代理。结束时 `cleaned_up: true`（进程全停），临时克隆目录删除，
日志复制到源仓库外层 `.probe-logs/`。报告 `verification_scope: startup-connectivity`。

按用户约定，整体测试仅一轮，集中修复至多一轮，最后只做上述启动联通。
不要求修复后浏览器逐模块点击、持久化全回归或生产build；首次测试SHA和最终smoke SHA分别记录。
显式 `--profile full` 可用于首次整体测试（可加 `--build`），不在最终pipeline默认启用，
不把“启动联通通过”写成“全业务/E2E/生产构建通过”。

## G9 跨仓库查重

```bash
python3 scripts/check_similarity.py --parent <parent> --repo <repo> --json-out <container>/similarity-report.json
```

业务指纹忽略技术栈、端口、app_slug 和模块 key；模块顺序不影响结果。预检覆盖本批全部输入与
`<parent>/.s11-registry/registry.json`，生成后还比较剥离注释/字面量后的业务源码结构。
报告必须保留最近邻、分项分数和阈值；没有历史快照的旧仓库不在自动覆盖范围内，需先归档，不能漏报成“全量已查”。

## 推送到 GitHub（main 分支）

```bash
python3 scripts/publish_github.py --repo <repo>                     # dry-run
python3 scripts/publish_github.py --repo <repo> --execute
```

先决条件与拒绝项：GitHub正式发布需要本仓创建归属凭据与当前真实仓库ID匹配，陌生同名/空仓也不接管；必须是 git 仓库、当前分支是 `main`、工作区干净、静态验收通过；
还必须有绑定当前 HEAD 的 `probe-report.json`（mode=clean-clone、verification_scope=startup-connectivity、实际依赖安装/前后端启动联通通过且cleaned_up=true）与
`similarity-report.json`（业务指纹一致）。**远端 `main` 上有本地没有的提交时拒推，脚本不做强推**。
分支已是最新时报 `up-to-date`，不重复推。推完回读远端 `main` 的 SHA 与本地 HEAD 比对。

**生成的仓库必须是 public**（`--visibility` 默认 `public`）：下游容器要能匿名 clone，
private 会卡住整条出题链路。脚本推送前后各读一次真实可见性，不是 private 就改成 public，
并把 `visibility_before` / `visibility_after` / `visibility_fixed` 写进报告；改不动就报错。
这是「生成的业务仓库」的口径——skill 自己的代码仓不受这条约束，按需要单独设。

凭据：读取本技能配置，默认 owner/account 为 `fengyin-go`；按指定 account 从 `gh auth token --user` 取凭证，缺失才尝试环境变量，且必须通过 API 核对所属账号。配置方式见 SKILL.md。
token 只以一次性 `git -c http.extraHeader=...` 传参，**不写进 `.git/config`、不打印**；
`origin` 始终指向不带 token 的 `https://github.com/<owner>/<name>.git`。
远端不是 GitHub 时用 `--remote-url` 直接推。

## 严禁题域（与禁用题型并列，但判废逻辑不同）

`禁用题型` 说的是仓库**长不成**那种样子，补业务模块就能救；
`严禁题域` 说的是**这个方向根本不许出题**——仓库做得再合规、容量再够，命中就是废题。

清单权威源是 `gsb-creator/references/0731_方案规则.md` 第 6 节「严禁出题清单（命中即废题）」，
本 skill 的冻结快照在 `scripts/repo_rules.py` 的 `BANNED_TOPIC_KEYWORDS`（抄写日期 2026-09-23，
以旧规则为基础，按 s11 的数据工程要求移除 streamlit、csv-dashboard、报表统计三个禁用词）与
`BANNED_TOPIC_EXTRA_TERMS`（0731 原文里点名、但关键词表漏掉的题域级词）。四类：

- 🎮 游戏与图形：贪吃蛇、打砖块、俄罗斯方块、坦克大战及变种；粒子/物理模拟；塔防/2D 解谜/潜行/平台跳跃；喂食小动物；记忆翻牌/连连看；五子棋等棋类；2048；扫雷；打地鼠
- 🖥️ 本地/桌面工具：命令行 CLI、代码片段管理器、批量重命名、截图标注、文件管理/同步、书签、密码管理器
- 🏢 平台/业务系统：电商购物车/订单、RBAC 权限后台、仓库库存、投票问卷、考勤 OA、图书借阅、博客 CMS、医院挂号/问诊、外卖点餐、CRM、IM 私信、拍卖、停车场、工单客服、商品+Excel 导入、积分商城、XXX 预约系统
- 📊 数据可视化/前端页面：记账、健康健身、菜谱、天气、番茄钟、习惯打卡、音乐播放器、旅行日记、观影记录

**拦在两处，都不给绕过参数：**

| 位置 | 语料 | 行为 |
|---|---|---|
| `scaffold_repo.py` 生成前（`load_spec` → `scan_spec_topics`） | spec 的 `app_name` / `app_slug` / `app_desc` + 每模块 `key` / `name` / `entity` / `desc` / `fields` / `statuses` / `actions` / `action_targets` / `metrics` | 拒绝生成，逐条点名「哪个词出现在哪个字段」，不落任何目录 |
| `verify_repo.py` 的 `check_forbidden`（G6） | 仓库目录名 + README + 全部文本源码 | 判 G6 不通过，`banned_topic_hits` 带文件路径 |

匹配口径是**大小写不敏感的子串匹配**（与下游 `check_repo.py` 的 `kw in text` 一致），
不做分词、不做词边界判断。仓库侧比下游严一档：下游只读「仓库名 + README」，
这里连源码一起扫——生成的 README 里本来就有一张带模块 key 与业务字段的模块表，
源码命中基本等于 README 也会命中。

`BANNED_TOPIC_EXTRA_TERMS` 的准入规则：候选词必须先过 `examples/` 里的默认 spec 与两个
真实 spec 的回归，**误伤的一律不收**。已经明确排除的有 `cli` / `im`（子串炸弹，会命中
`client` / `import`）与 `看板` / `报表` / `导入` / `同步` / `文件`（是需求模式里的正常词，
且出厂 spec 自己就有「运营看板」模块）。

**换题域，不要改字眼。** 把「库存」写成「在库」绕过关键词，题域还是那个题域，
下游换个口径照样判废。

## 禁用题型

当前 S11 只使用五种任务类型：`功能开发` `Bugfix` `功能优化` `代码重构` `依赖升级与迁移`。
需求模式仍为 13 类，但 `代码理解` 是禁用项；正式容量按其余 12 类计算，所以仓库也不能长成只能讲代码怎么走的样子。

机器判据（`check_forbidden`）：

- compact布局必须有一个共享`WorkbenchView`且spec模块>=8；全栈Python/Java必须有>=8个独立领域Service。
  只有文档、只有算法片段、把全部业务塞进一个通用文件的仓库都会被拦下。
- 命中密钥特征（私钥、`AKIA...`、`ghp_...`、`sk-...`、硬编码口令）→ 拦下。
- 命中违规业务域（赌博、色情、毒品、枪支、金融黑灰产、攻击工具）→ 拦下。
- 单个文件超过 5MB → 拦下（产物快照会被判 too large）。

## 容量怎么算出来的

平台查重判的是**「主体 + 需求模式」**这两根轴：同一主体出多了、或同一需求模式出多了，
都会被判废。所以一个仓库能出多少条不是「想写多少写多少」，而是座位数：

```text
容量 = min(单仓库上限, 主体数 × 每主体上限, 可用需求模式数 × 每模式上限)
```

默认参数：

| 参数 | 值 | 说明 |
|---|---|---|
| 单仓库上限 | 36 | 同一个仓库里两两比对，铺得越多最近邻越近 |
| 每主体上限 | 2 | 同一个主体的第二条必须跨任务家族（新增能力类 vs 缺陷修复） |
| 每模式上限 | 3 | 全局默认值（`check_repo_theme.DEFAULT_MAX_PER_MODE`，2026-09-22 由 2 改成 3） |
| 可用需求模式 | 12 | 需求模式词典共 13 类，去掉禁用的 `代码理解` |

所以：`每模式上限 3` 时，模式席位 = 12 × 3 = 36；主体数必须至少 18，
容量才可能达到 `min(36, 18 × 2, 36) = 36`。新仓 spec 的 18 个模块是数学下限，
实践建议 20～24 个真实 topic；实际主体数、静态容量和下游困难题位必须分别复核。

静态公式通过不等于可交付：正式容量还要由 `check_prompt_capacity.py` 证明 36 条计划
恰好覆盖 12 个可用模式且每个模式 3 条，并逐条通过领域、类型、H1/H2/H3 v3与源码锚点检查。
降级到 35 条、减少模式或降低每模式上限都不算完成。

### 主体是什么

旧扫描主体 = 仓库里一个可以被单独改的代码位置，派生规则是冻结的（见 `scripts/repo_rules.py`）：

- 只扫这些路径下的文件：`frontend*/src/**`、`backend/app/**`、`**/src/main/java/**`、
  `**/src/**/*.cpp`、`frontend*/**/*.css` 等（完整列表见 `REPO_SOURCE_PATTERNS`）。
- 一个文件算一个主体，主体名默认取文件名；`index` / `main` / `page` / `list` / `home`
  这类入口文件改取父目录名（`views/visitor/index.vue` → `visitor`）。
- 文件里必须至少含 3 条**非泛用中文串**才算一个主体（`GENERIC_TOKENS` 里那些
  「用户 / 状态 / 数据 / 功能」不算）。这条决定了**生成的文件必须带真实中文业务文案**，
  纯英文代码派不出主体。
- 依赖目录、构建产物目录一律不参与派生。

compact布局不再为每个模块复制页面/路由，但全栈仓库仍为每个模块生成独立
`backend/app/services/<key>.py`（Java为`<Class>Service.java`）；纯前端为每个模块生成
`frontend/src/domain/<key>.ts`。这些文件承载各自业务字段、状态、动作和数据快照逻辑，
共同保留18个可独立修改主体，因此缩仓不以降低36题位容量为代价。

## 规则同步

`repo_rules.py` 是出题闸门口径的**冻结快照**（容量/主体口径抄写日期 2026-09-22，
严禁题域清单抄写日期 2026-09-23），本 skill 靠它自给自足。
如果目标机器上已经有出题闸门脚本，可以顺手比对一次，别让快照悄悄过期：

```bash
# 容量/主体/需求模式口径，指向 check_repo_theme.py
python3 scripts/verify_repo.py --rules-drift <check_repo_theme.py 的路径>

# 严禁题域清单，指向 check_repo.py
python3 scripts/verify_repo.py --banned-drift <check_repo.py 的路径>
```

两步都是可选的：没有对应脚本时本 skill 照常独立工作。比对项是常量级相等，
所以两边口径只要有一处不同就会报出来。`--rules-drift` 里严禁清单那一项是**可选比对**：
对方脚本没有 `BANNED_TOPIC_KEYWORDS` / `BANNED_KEYWORDS` 时静默跳过
（老的 `check_repo_theme.py` 不带这份清单，不该因此判失败），输出里以
`their_banned_keywords: "absent"` 体现。

s11 的分析领域例外会与旧技能形成预期差异；不要把这三项自动恢复成禁用。其他差异逐项判断后更新 `repo_rules.py` 与 `GATE_VERSION`，
然后重跑 `tests/test_gates.py`。`--rules-drift` 的比对项：`REPO_SOURCE_PATTERNS`、
`PARENT_NAMED_STEMS`、`GENERIC_DIR_NAMES`、`GENERIC_TOKENS`、`IGNORED_SOURCE_DIR_PARTS`、
`DEMAND_MODE_LEXICON`；`--banned-drift` 的比对项：`BANNED_TOPIC_KEYWORDS`（按分类逐词比）。

## 生成出来的仓库还要满足的工程约定

- 前端 dev server：Vite 写 `server.open = false`；若换成 CRA / Webpack，
  对应写 `BROWSER=none` 或 `devServer.open: false`。`verify_repo.py` 会扫这三处。
- `.gitignore` 必须在仓库根目录，且覆盖：`node_modules/`、`dist/`、`build/`、`target/`、
  `__pycache__/`、`.venv/`、包管理器缓存、`.vite`/`*.tsbuildinfo`、日志、`.env*`、
  覆盖率目录、IDE 目录、`.DS_Store`。
- 依赖目录（`node_modules`、`.venv` 等）一旦落地，必须被 `.gitignore` 命中，
  且不出现在 `git status --porcelain` 里——这条会被 `git check-ignore` 实测。
- 根目录带 `docker-compose.yml`（含 `services`）、`README.md`、`Makefile`，
  方便下游按「已容器化，可一键起环境」判定环境可复现等级。
- **模块 key 不能和依赖 / 构建产物目录撞名**：key 会变成领域服务文件名和前端领域模块名，
  而 `.gitignore` 里有 `dist/`、`build/`、`out/`、`target/`、`**/target/`、`coverage/`、
  `logs/`、`node_modules/` 这些规则。撞名的后果是页面被 git 忽略：本地静态验收全绿、
  干净克隆里少一个页面、前端构建报 `TS2307: Cannot find module '@/views/<key>/index.vue'`。
  `scaffold_repo.py` 用 `RESERVED_MODULE_KEYS` 在 spec 阶段就拒（`target` → 改成 `threat`）；
  `verify_repo.py` 的 G3 另有**反向检查**：逐个 `frontend*/src/views/*` 目录跑
  `git check-ignore`，被命中就报「业务模块目录被 .gitignore 命中，页面不会进仓库」。

## 纯前端仓库（`--backend none`）的额外口径

纯前端仓库走同一套闸门，只是三处按形态放宽/收紧：

| 闸门 | 全栈 | 纯前端 |
|---|---|---|
| G1 结构 | 必须有 `backend/`（`app/main.py` 或 `pom.xml` + Controller） | 不要求 `backend/`；必须有 `frontend/src/api/local-service.ts` 与 `frontend/src/data/*.ts` |
| G6 禁用题型 | 页面 >= 8 **且** 接口文件 >= 8 | 只数页面（>= 8） |
| G8 最终启动联通 | 后端 `/api/health`、前端首页/入口脚本、前端 -> 后端代理链路；不做业务接口遍历/E2E/build | 后端与代理记 `skipped: true`（不计失败），只验前端首页/入口脚本；不做E2E/build |

形态判定**不靠「有没有 backend 目录」**，而是读 `frontend/package.json` 的
`repoKind` 字段（生成时写入 `fullstack` / `frontend-only`）。`verify_repo.py` 与
`probe_startup.py` 都按这个字段走，也可以用 `verify_repo.py --kind frontend-only|fullstack`
显式覆盖。这样做的原因：如果按「缺 backend 目录」自动降级，一个后端目录被误删的全栈仓库
就能悄悄通过 G1，闸门等于失效——`tests/test_gates.py` 里有专门的反例用例守着这条。

React 变体还有一条主体派生的坑：上游口径会跳过 `index.tsx` / `index.ts` / `main.tsx` 这类入口文件名，
所以纯前端 React 的共享工作台保留在 `src/views/WorkbenchView.tsx`；每个业务主体落在
`src/domain/<key>.ts`，不再为每个模块生成独立页面组件。
`export { default } from './<Key>View'`（标准 barrel）。否则 18 个页面一个主体都派生不出来，
容量会掉到 16 条。
