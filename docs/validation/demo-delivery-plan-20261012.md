# 统一 Demo 入口与只读预检预登记

本轮 `codex/demo-delivery-preflight` 从 main `f554869039434b53010c8b8f930aae2286964ab4` 起步，只新增入口/测试/简明交付说明，不修改 `src/db/web`、两个原 Demo、期限、worker、模型或业务配置。独立 PR，精确源码/报告 head 的 CI 分开核对；旧失败与历史 PR 均保留。

## 入口边界

- `python -m scripts.demo catalog` / `r06`，复用 `run_catalog_demo.main` / `run_r06_demo.main`，不新增子进程或复制运行/清退逻辑。输出默认自有 artifacts/demo 随机 UUID 子目录；显式输出仍使用旧隔离 guard，8000/远端或隐式数据库/已有输出拒绝。
- `--env-file` 仅按 UTF-8 读取数据库与三个批准模型字段，文件大小有界，不执行 shell/插值、不读取管理令牌。显式文件优先，未指定时使用进程配置，缺数据库配置才尝试项目 .env。配置、错误、日志不得泄露 DSN/密码/管理令牌。
- `--check` 不创建输出、schema、服务或迁移；数据库用独立 read-only 连接、连接/语句预算，只 SELECT 查询最低版本与 CREATE-schema 权限。R06 必须可用 NumPy，直接复用受控完整加载/摘要验证，不自动改 stdlib、不下载/训练/激活模型。
- 正常启动只在 CLI 作用域替换 EVOREC 环境为数据库项，调用原入口；finally 恢复本进程原变量，不修改 .env/global/父进程。CLI 不是 web 并发组件。预检成功不是 ready/HTTP/验收；readiness 和停止由旧入口提供。

## 验证口径

1. 定向功能：两模式/配置优先级、重复/未闭合/超限/缺失配置、危险端口/路径/远端数据库、错误批准参数、只读数据库语句与缺权限/超时、无秘密输出、环境恢复/委托失败及中断。全新 XML/temp，失败立即保存源字节与事件，不重跑覆盖。
2. 独立删边界负控制：仅在忽略副本移除输出隔离检查，使用原危险路径用例实际失败；不能将 import error 计作负控制。
3. 服务环境完整 catalog/r06 既有入口回归和新入口测试、Node 三文件；accelerated 真 NumPy 小包预检，缺包不能 skip/冒充。CI 显式纳入新测试并检查零 failure/error/skip。
4. 冻结提交推送后，本机批准完整 137,249 项包运行新 CLI `--check`，保存无秘密输出，前后 schema 集合为 0/.env/18模型/主工作区和对照未变。本轮只读预检不是重新完整模型负载或浏览器。
5. 新 catalog CLI 在真实独立子进程/随机 schema/loopback 端口完成 ready、实际 HTTP 推荐与正常 stop；确认唯一 schema/确切 PID/监听、临时令牌已清退，保留输出。用已有 fixture 保护业务数据，不在共享 DSN 上直接 migrate。正式结果与开发测试、历史完整模型证据分开。

最终报告及 Git index 卫生/链接/源码解析检查通过后提交推送，保留 PR 并等待精确最终 head CI。完整交付复演、Ctrl+C 实机、作者三分钟讲解/亲自修改和 release/tag 仍为下一轮，不因本轮预检通过宣布全部完成。team-mode 缺必需 agent_type，主线程串行安全/效率/复用审查；git-ship 中文、显式路径，无 Claude trailer。
