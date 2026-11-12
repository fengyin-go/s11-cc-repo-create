# 环境与跨机器迁移

技能从当前目录解析相对资源，直接调用 PATH 中的 `python3`、`node`、`npm`、`git`、`gh`。不要复制某台电脑的绝对运行时路径。
首次运行现场执行 `python3 --version`、`node --version`、`npm --version`、`git --version`、`gh --version`；Java 后端还须验证 JDK 17 与 Maven。缺少 Java 环境时选择 Python，或如实标记 Java 真启动未验证。

## GitHub

默认配置 `config/github.json` 的 owner 为 `fengyin-go`，认证账号省略时等于 owner。
2026-11-11 在当前电脑通过 `scripts/github_settings.py` 实际读取已保存的该账号凭证并调用 GitHub API，账号验证成功。此记录不代表其他电脑已登录。

迁移时：

1. 复制整个 `s11-cc-repo-create` 目录，保留脚本、示例、references 和 config。
2. 检查 `config/github.local.json`，将 owner 改成新电脑的目标账号；组织仓库另配 account。该文件被 Git 忽略，但普通目录复制仍会携带，必须核对。
3. 用 `gh auth login --hostname github.com` 在新电脑完成认证；凭证不随技能复制。
4. 执行 `python3 <skill>/scripts/github_settings.py`，确认输出 owner/account 正确且 credential_valid 为 true。
5. 执行 `python3 -m unittest discover -s <skill>/tests -v` 验证脚本。自测不等于真实项目 G8 通过。

完整优先级和临时覆盖方法见主 SKILL.md 的「GitHub 账号与凭证」。不跟随 `gh` 全局活动账号，不需要全局切换账号。

## 命令与联网

含中文或空格的路径加引号；`<skill>` 展开为实际技能目录。
`--parent` 产出的仓库根是 `<父目录>/<编号>/<编号>`，不是外层容器。
`probe_startup.py --clean-clone --build` 会在克隆中安装依赖，需要 npm registry 与 PyPI 可达；日志默认在仓库父目录的 `.probe-logs/<仓库名>/`。

本副本未携带原技能的 `.git` 与远端绑定。不要使用原仓库的 git pull/push 命令更新新副本。
