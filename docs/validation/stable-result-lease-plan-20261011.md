# 结果事务复用执行租约连接：预登记

基于干净 `2f50e6226380eba5e3a7c382dedb58b44ad7aeba`（草稿 PR #61），新分支 `codex/r06-result-lease-connection`。上一轮实际 504 的期限回调落在新建结果连接内，连接墙钟 103.859 ms，CPU 已结束；这支持检查连接成本，但不能认定唯一根因。本轮仅改变已登记执行租约的结果写入连接选择，不修改目录 SQL/校验、恢复锁、失败清理连接、排名 worker、2 秒期限、HTTP client 或重试策略。

协议先提交并推送，核对远程 SHA 后才构造新测试与查看验证结果。实现及测试另行提交并推送；完整包测量使用同一干净冻结实现提交，记录 src/scripts/db migrations 指纹，测量期间不改跟踪文件。结果和问题台账单独提交；不覆盖任何失败目录。

## 所有权和机制门禁

结果仅借用本 backend、本 request、已完成入场且仍登记的执行租约；持有连接 RLock 至事务提交或回滚退出。要求连接未关闭、autocommit、事务 IDLE、会话 advisory lock 仍持有；不能进入 Connection 自身的上下文而提前关闭租约。结果事务内原有 binding、模型、终态和 owner 校验继续保留；已登记租约的 accepted 行必须有匹配 owner。历史无登记执行的兼容写入仍独立连接，不因借用失败静默回退。关闭及重复取消仍等待后台写入结束，失败清理不能覆盖 completed。

真实 PostgreSQL 合成小目录测试核对写入/完成触发器 backend PID 等于租约 PID、结果阶段零新增连接、独立观察者可见原子提交，以及提交错误完整回滚且租约仍 idle/held。负面场景覆盖错误 backend/request、未 admitted/未登记、连接丢失/解锁、autocommit 关闭、外层事务、owner 丢失/变化、binding/模型/已完成内容不一致。实际阻塞事务验证 close 等待和两个同结果写入串行唯一；既有 repeated cancel、真实 2 秒 API 超时后的 late commit / 同键重放、终态恢复和 R06 快照回归继续执行。临时改回独立结果连接作为测试机制负控制，必须触发零新增连接断言，随后恢复候选并保留单独 XML。

诊断成功覆盖同时保留历史独立结果连接的完整 connect/commit/close 门禁，以及本候选明确的 borrowed transaction_context_exit 门禁；借用路径不得混入结果 connect/close 或错误/缺失事务退出。真实驱动确认 Transaction.__exit__ 不等于 Connection.__exit__，不能删掉提交观测来制造全绿。新增模块加入服务 CI 和显式卫生测试；质量、性能、复用三视角主线程串行复核，接口缺少 team-mode 必需 agent_type，不冒充独立代理评审。

## 固定完整包初筛

- 批准包 `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，137,249 项，NumPy；manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，模型 `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。业务 schema、主目录、.env、模型文件和 8000 不操作。
- 首轮复用 scripts.profile_r06_tcp，独立自有 schema/API/随机非 8000 loopback 端口，两笔顺序参考及恰好 24 笔双并发，关闭服务端插桩/GC 观察/phase gate；dense/k=10，轮内同一 sample session，每笔新键及新 HTTPX client，trust_env=False、client timeout=10 秒、不重试。目录为 artifacts/diagnostics/stable-result-lease-20261011/untraced。
- 第二轮固定两笔顺序及 8 笔双并发，开启现有阶段插桩但关闭 GC 观察/phase gate；保持其他口径，使用独立 schema/进程，目录 traced。首轮全部为合法 200/504 且身份、记录与正常清退完整才继续第二轮；即使首轮全成功，第二轮也只验证借用边界，不是扩大初筛或稳定验收。任一配置/启动/身份/覆盖/清退错误、transport error 或非 200/504 停止剩余测量，不修复后自动重跑。
- 每轮前记录时间、环境版本、CPU/内存快照、.env/源/模型指纹；快照不证明连续独占。每轮后独立确认确切 schema、API PID 和监听不存在、正常 CPU drain/无硬杀，并确认主目录、.env、模型不变。报告 client 创建/HTTP/总耗时，插桩轮另报目录、CPU 等待/执行、结果事务及清退，重叠阶段不相加，不把 execute 称为锁等待。

独立检查实际 26/10 笔逐笔分母、200 身份、504 数量、原始生命周期与源码绑定。脚本退出 0 只表示诊断完整，不意味着零失败；无失败轨迹覆盖为 null，未插桩失败阶段 unknown。完整包本批不保存用户级请求键/正文，不在清退后猜测持久终态；late commit 的持久/重放由实库合成故障注入单独证明。新 bug/异常发现即落盘本地忽略事件，测量后更新[问题台账](stable-runtime-bugs.md)，保留失败原始路径、类别、影响和状态，不存 DSN/令牌/正文。

本轮不执行旧/新 A/B/B/A、120 笔、浏览器或备份验收；它们仍是后续稳定门禁。初筛有限样本成功不能关闭 R06-001，不据历史不同批次差值宣称因果收益。用 git-ship 显式暂存、中文提交、不加 Claude trailer，保留依赖 #61 的草稿 PR 并检查精确最终 head CI；稳定门禁未完成不合并。
