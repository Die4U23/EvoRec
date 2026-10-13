# 统一 Demo 入口与只读预检预登记

本轮 `codex/demo-delivery-preflight` 从 main `f554869039434b53010c8b8f930aae2286964ab4` 起步，新增入口/测试/简明交付说明；不修改 `src/db/web`、两个原 Demo 的运行/清退、期限、worker、模型或业务配置。开发后补确认 DEMO-002，只在共用 validate_isolation 增加继承 PGHOSTADDR/PGSERVICE 的启动路由保护，不扩展其他行为。独立 PR，精确源码/报告 head 的 CI 分开核对；旧失败与历史 PR 均保留。

## 入口边界

- `python -m scripts.demo catalog` / `r06`，复用 `run_catalog_demo.main` / `run_r06_demo.main`，不新增子进程或复制运行/清退逻辑。输出默认自有 artifacts/demo 随机 UUID 子目录；显式输出仍使用旧隔离 guard，8000/远端或隐式数据库/已有输出拒绝。
- `--env-file` 仅按 UTF-8 载入数据库与三个批准模型字段，文件大小有界，不执行 shell/插值、不载入管理令牌。显式文件优先，未指定时使用进程配置，缺数据库配置才尝试项目 .env。配置、错误、日志不得泄露 DSN/密码/管理令牌。
- `--check` 不创建输出、schema、服务或迁移；数据库用独立 read-only 连接、连接/语句预算，只 SELECT 查询最低版本与 CREATE-schema 权限。R06 必须可用 NumPy，直接复用受控完整加载/摘要验证，不自动改 stdlib、不下载/训练/激活模型。
- 依赖检查通过后，还检查实际导入的 evorec 是否来自当前 checkout 的 src；借用旧可编辑环境指向别树时拒绝，不自动重新安装或改 sys.path。开发原型已实查误接受另一 checkout，新增反例必须先保留失败，再修复和验证；不影响此前明确 PYTHONPATH 的主干验收。
- 路由补充：显式/继承 service 配置拒绝；未显式指定 hostaddr 时也核对继承 PGHOSTADDR，须为空或单个 loopback 地址。显式 loopback hostaddr 仍覆盖继承值。先保存三个失败反例/源快照，数据库 probe mocked，无真实远端连接；依据 [libpq 环境默认值](https://www.postgresql.org/docs/18/libpq-envars.html)补共享 guard，不修改父环境或 SSL/GSS 策略。原只读预检与业务保护边界不变。
- 正常启动只在 CLI 作用域替换 EVOREC 环境为数据库项，调用原入口；finally 恢复本进程原变量，不修改 .env/global/父进程。CLI 不是 web 并发组件。预检成功不是 ready/HTTP/验收；readiness 和停止由旧入口提供。

## 验证口径

1. 定向功能：两模式/配置优先级、重复/未闭合/超限/缺失配置、危险端口/路径/远端数据库、错误批准参数、只读数据库语句与缺权限/超时、无秘密输出、环境恢复/委托失败及中断。全新 XML/temp，失败立即保存源字节与事件，不重跑覆盖。
2. 独立删边界负控制：仅在忽略副本移除输出隔离检查，使用原危险路径用例实际失败；不能将 import error 计作负控制。
3. 服务环境完整 catalog/r06 既有入口回归和新入口测试、Node 三文件；accelerated 真 NumPy 小包预检，缺包不能 skip/冒充。CI 显式纳入新测试并检查零 failure/error/skip。
4. 冻结提交推送后，本机批准完整 137,249 项包运行新 CLI `--check`，保存无秘密输出，前后 schema 集合为 0/.env/18模型/主工作区和对照未变。本轮只读预检不是重新完整模型负载或浏览器。
5. 新 catalog CLI 在真实独立子进程/随机 schema/loopback 端口完成 ready、实际 HTTP 推荐与正常 stop；确认唯一 schema/确切 PID/监听、临时令牌已清退，保留输出。用已有 fixture 保护业务数据，不在共享 DSN 上直接 migrate。正式结果与开发测试、历史完整模型证据分开。
6. 冻结前补充：新 R06 CLI 的真实 TCP 复演使用既有 verify_r06_http.exercise 的固定序列（含原键/反馈/重置），完整批准包、NumPy、原 2 秒、无重试/插桩；记录客户端观察，不更换失败键或选择性删除请求。正常 stop 后独立检查 PID/监听/schema、模型/.env/保护树。此为单轮入口复演，不是新的 120 笔负载、浏览器或 SLA。

最终报告及 Git index 卫生/链接/源码解析检查通过后提交推送，保留 PR 并等待精确最终 head CI。完整交付复演、Ctrl+C 实机、作者三分钟讲解/亲自修改和 release/tag 仍为下一轮，不因本轮预检通过宣布全部完成。team-mode 缺必需 agent_type，主线程串行安全/效率/复用审查；git-ship 中文、显式路径，无 Claude trailer。
