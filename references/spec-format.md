# spec 格式：怎么描述一个要生成的应用

生成器只认一份 JSON。默认 spec 在 `examples/spec-data-engineering.json`（数据工程链路开发平台，18 个模块）。
领域固定 `数据工程`，子方向选择 `数据采集、爬虫与RPA` / `数仓与数据湖` / `数据分析与数据应用` 中一个。

**改完先过一遍严禁题域自检**（命中即废题，没有绕过参数）：

```bash
python3 <skill>/scripts/scaffold_repo.py --check-spec --spec /path/to/spec.json
```

## 严禁题域：这些方向不能选

清单来自 `gsb-creator/references/0731_方案规则.md` 第 6 节，抄进本 skill 的
`scripts/repo_rules.py`。下表列出本副本保留的禁用方向；数据分析例外见后文，不能靠改名将无关项目伪装成数据工程：

| 类别 | 点名方向 |
|---|---|
| 🎮 游戏与图形 | 贪吃蛇、打砖块、俄罗斯方块、坦克大战及变种；粒子/物理模拟；塔防/2D 解谜/潜行/平台跳跃；喂食小动物；记忆翻牌/连连看；五子棋等棋类；2048；扫雷；打地鼠 |
| 🖥️ 本地/桌面工具 | 命令行 CLI、代码片段管理器、批量重命名、截图标注、文件管理/同步、书签、密码管理器 |
| 🏢 平台/业务系统 | 电商购物车/订单、RBAC 权限后台、仓库库存、投票问卷、考勤 OA、图书借阅、博客 CMS、医院挂号/问诊、外卖点餐、CRM、IM 私信、拍卖、停车场、工单客服、商品+Excel 导入、积分商城、XXX 预约系统 |
| 📊 数据可视化/前端页面 | 记账、健康健身、菜谱、天气、番茄钟、习惯打卡、音乐播放器、旅行日记、观影记录 |

最坑的是第三类：`库存` / `工单` / `预约` / `权限` 这些词在很多正常业务里都是顺手的模块名，
但只要出现在 spec 的应用名、描述、模块名、业务字段、状态或动作里，生成就直接被拒——
而且生成的 README 会带一张「模块 | 目录 | 业务对象 | 主要字段」的表，模块 key 与字段都会
在 README 里露出来，下游 `check_repo.py` 一读就是 BLOCK。

**换方向，别改字眼。** 把「库存」写成「在库」只是绕过关键词，题域还是那个题域。

### 数据工程适配

本副本按用户领域要求允许数据分析与数据应用，已移除旧表中的 `streamlit`、`csv-dashboard`、`报表统计` 禁用词；其他无关方向仍禁止。前端生成技术栈仍限 Vue/React。

选择一个主方向：采集/爬虫/RPA 可围绕接入、抽取、限速、重试和结果校验；数仓/数据湖可围绕分区、清洗、转换、维度、事实、质量、血缘和补数；分析/数据应用可围绕指标口径、聚合查询、分群、实验和数据服务。
模块与代码必须符合实际方向，不能仅替换领域标签。元数据合法不等于实际业务经过验证。

## 顶层字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `domain` | ✓ | 固定 `数据工程`，缺失或其他领域会被拒绝 |
| `subdirection` | ✓ | 上述三个准确值任选一个 |
| `app_name` | ✓ | 应用中文名，进标题、README、页面头部 |
| `app_slug` | ✓ | 英文短名，做包名与前端 package name；小写字母开头，只含小写字母、数字与连字符 |
| `app_desc` | ✓ | 一句话业务描述，进页面头部与 README |
| `frontend` | | `vue`（默认）或 `react`，可被 `--frontend` 覆盖 |
| `backend` | | `python`（默认，FastAPI）、`java`（Spring Boot 17），或 `none`（**纯前端仓库**，不生成后端）；可被 `--backend` 覆盖 |
| `port_frontend` | | 前端端口，默认 5173 |
| `port_backend` | | 后端端口，默认 8000（`backend: none` 时不使用） |
| `modules` | ✓ | 业务模块数组，**至少 16 个**才能保证容量 >= 30 |

## 模块字段

```json
{
  "key": "extractor",
  "name": "增量采集",
  "entity": "采集游标",
  "fields": ["来源标识", "增量字段", "游标位置", "批次大小", "起始水位", "截止水位"],
  "statuses": ["草稿", "待校验", "已启用", "已停用"],
  "actions": ["提交校验", "启用配置", "停用配置"],
  "action_targets": ["待校验", "已启用", "已停用"],
  "metrics": ["游标总数", "待校验游标", "已启用游标"],
  "desc": "维护增量采集游标和批次边界"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `key` | ✓ | 目录名、接口前缀与主体名；必须匹配 `^[a-z][a-z0-9_]*$`，不能是 Python 关键字、保留构建目录或 `health` / `overview` 系统端点；类名归一化后也不能互相冲突 |
| `name` | ✓ | 模块中文名，进页面标题与导航 |
| `entity` | ✓ | 业务对象中文名，进按钮与提示语 |
| `fields` | ✓ | 至少 3 个不重复的非空字段；页面查询、完整登记与 CSV 导出共用这组字段 |
| `statuses` | ✓ | 至少 2 个不重复状态；第一条是新建时的默认状态 |
| `actions` | ✓ | 至少 1 个不重复动作，进前端按钮与后端动作接口 |
| `action_targets` | | 与 `actions` 一一对应且必须属于 `statuses`；不写就按状态顺序往后推一格 |
| `metrics` | | 如填写必须正好 3 项；数值由当前数据动态计算，不再是固定常量 |
| `desc` | | 覆盖自动生成的页面说明 |

## 怎么写出「撑得起 30 条」的 spec

1. **模块数 >= 16**。少于 16 个主体，容量公式里的「主体数 × 2」就先卡住了。
2. **每个模块的 `fields` 写 6~8 个业务字段，别写「用户名 / 状态 / 数据」这类泛词**。
   主体识别会过滤掉 `GENERIC_TOKENS` 里的泛用词，泛词写多了这个文件派不出主体。
3. **`fields` / `statuses` / `actions` 要用具体业务词**：`增量游标`、`分区保留`、`异常行隔离`
   这种能撑起「规则与阈值」「状态流转」「边界与空态」等多种需求模式的题面；
   `数据管理`、`信息维护` 这种则会把所有题挤进同一格。
4. **模块之间业务对象要拉开**。同一主体 + 同一需求模式只能出一条题，
   16 个彼此独立的业务域比 16 个「XX 管理」更经得起出题。
5. **动作至少 2~3 个且方向不同**（正向推进 + 打回/作废），这样「状态流转」和
   「缺陷处置」两类题都有落点。

## 生成出来的目录

```text
<父目录>/<项目编号>/<项目编号>/         例如 cc10001/cc10001、YEL10003/YEL10003
├── frontend/                 Vue 3 或 React + Vite + TypeScript
│   ├── src/views/<key>/index.vue|tsx   每个模块一个页面（主体 = <key>）
│   ├── src/router/index.ts             React 走 src/App.tsx
│   ├── src/api/client.ts               统一请求封装
│   ├── src/stores/session.ts           会话状态（React 版省略）
│   ├── vite.config.ts                  server.open = false
│   ├── tests/functionality.spec.ts      全模块浏览器功能验收
│   ├── playwright.config.ts            Chromium E2E 配置
│   └── package.json / package-lock.json / index.html / tsconfig.json
├── backend/                  FastAPI 或 Spring Boot
│   ├── app/routers/<key>.py            每个模块一组接口（主体 = <key>）
│   ├── app/services/<key>.py           状态流转与字段校验
│   ├── app/store.py / seed.py          内存仓库与示例数据
│   └── requirements.txt / run.sh
├── .gitignore
├── .s11-domain.json / .s11-spec.json  领域与规范化业务快照（供验收和跨仓库查重）
├── README.md
├── docker-compose.yml
└── Makefile
```

**`backend` 写成 `none` 时是纯前端仓库**，上面的 `backend/` 整块不存在，
前端多出本地数据层：

```text
├── frontend/
│   ├── src/views/<key>/index.vue       每个模块一个页面（Vue：主体 = <key>）
│   ├── src/views/<key>/<Key>View.tsx   React：组件落在这里（主体 = <Key>View）
│   ├── src/views/<key>/index.tsx       React：只写一行 re-export（barrel）
│   ├── src/api/local-service.ts        列表 / 筛选 / 动作流转 / 导出 / 概览
│   ├── src/data/modules.ts             模块元数据：字段、状态、动作、流转目标
│   ├── src/data/seed.ts                示例数据（每模块三行）
│   ├── src/data/local-store.ts         localStorage 持久化
│   ├── src/data/types.ts               公共类型
│   └── vite.config.ts                  server.open = false，且**没有 /api 代理**
├── .gitignore
├── .s11-domain.json / .s11-spec.json  领域与规范化业务快照（供验收和跨仓库查重）
├── README.md
├── docker-compose.yml                  只有 frontend 一个 service
└── Makefile
```

`frontend/package.json` 里的 `repoKind` 字段（`fullstack` / `frontend-only`）就是形态标记，
`verify_repo.py` 与 `probe_startup.py` 都读它，不要手工删。

**外面那层 `<项目编号>` 是任务容器，里面那层才是仓库根**：`.git`、`.gitignore`、`frontend/`、
`backend/` 全在内层。`scaffold_repo.py --parent` 的输出里 `out` / `repo_root` 指内层、
`project.container` 指外层；`verify_repo.py`、`probe_startup.py`、`publish_github.py` 的
`--repo` 都传内层那个路径。用 `--out` 显式给路径时不套这一层，给的路径就是仓库根。

## Java 后端的额外前提

Java 版生成的是 Spring Boot 3 + Maven 工程（`pom.xml`、`Application.java`、
`controller/*Controller.java`、`service/*Service.java`）。**本 skill 无法在没装
JDK 17 + Maven 的机器上真启动它**，此时 `probe_startup.py` 会明确报
「本机没有 JDK/Maven，无法真启动」，这一条按未验证处理，不要写成已通过。
