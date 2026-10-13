# R06 执行归属与快照取消修复：最新交付代码集成验证

日期：2026-10-12。状态：**本地开发修复通过，未提交、未推送，未发布稳定版**。

后续：此处保留开发阶段的实际状态。修复随后固定为本地提交 `488847a`，新的干净源码双轮结果见[冻结验证报告](r06-lifecycle-frozen-20261012.md)，不回写为本阶段已经完成。

## 来源与发现

本轮完成度检查发现：最新交付 head `f1b0205919a51b5b19f26b3b3260e3951b067c29` 仍未包含旧工作树的两处生命周期修复。直接对最新生产源码运行原反例，得到 **3 项测试、3 failure、0 error/skip**，耗时 9.678 秒；不是根据历史报告推断。

- 执行 owner 改变后，旧执行的失败清理仍把新 owner 的 accepted 请求改成 failed。
- 在 `R06RankingPort.from_snapshot` 的线程工作中取消请求，调用任务会在线程退出前结束，提前进行失败处理和释放执行租约。线程正常返回、迟到异常两个反例均失败。

原失败 XML 保留在 `C:/Users/7/.codex/worktrees/r06-probe-controls/proj/artifacts/completion-audit-20261012.xml`，SHA256 `329bdef349cd5b8ccfbfcb4c3c9d22264fdd6d5dfb80069e523339e39626404d`。旧工作树与原失败没有覆盖。

修复在上述 head 新建的 `codex/r06-lifecycle-integration` 工作树完成。只迁入生命周期补丁，并适配最新结果连接测试的一个调用；保留现有结果事务复用租约连接、目录校验、2 秒服务期限和单排名 worker。

## 修改与审阅

`_mark_failed` 在同一条 UPDATE 内同时比较 request_id、execution_owner 和 accepted 状态。owner 不匹配时不写入；同 owner 已 completed 的结果也不改写。这里没有新增 owner 转移或接管协议。

快照恢复工作保存为独立 task，外层 shield；收到取消时复用已有 `_drain`，等实际线程退出并消费迟到异常后再传播取消。重复取消不能提前释放租约。已完成结果重放及结果连接复用保持原行为。

反例覆盖 owner 改变后的持久状态、零结果、锁释放及原键恢复；快照线程的重复取消、迟到异常、执行登记与连接；真实 2 秒 API 期限触发后，线程仍阻塞时不得提前返回 504。测试没有放宽终态断言。主线程检查了完整 diff 和 `_drain` 语义；没有独立审查代理结果。

## 本轮执行结果

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 定向测试 | 30 passed，0 failure/error/skip，49.282 秒 | owner、取消、真实 API deadline、结果连接 |
| 扩大回归 | 694 passed，0 failure/error/skip，348.474 秒 | 18 模块，含恢复、入场/结果连接、R06/API、诊断门禁、架构与新 CLI |
| 完整冻结模型双并发 | 120/120 HTTP 200，0 次 504/传输失败/持久门禁失败 | 单个固定批次，无自动重试、无服务插桩 |
| 持久账本 | 120 条 completed，每条 10 件合法结果，身份一致、租约释放，0 条 accepted 残留 | 每笔独立请求键；门禁读取数据库核对 |
| 恢复 | 重启精确重放、同键唯一结果、断连后 completed、已入场崩溃恢复、新键计算均通过 | 6 个恢复终态审计均有效且租约释放 |

30 项是 694 项的重叠子集，不相加。三个相关测试模块已在 CI 清单中；本补丁的远端 CI 尚未运行。没有把旧集成的 1654/178/16/114 项记作本轮执行。

完整模型为 137,249 件商品，NumPy，bundle `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，model_version `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。manifest SHA256 `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`。每笔创建新 HTTP client，闭环并发 2，原 2 秒期限，单排名 worker。负载耗时 88.526 秒；客户端总耗时 p50 **1461.609 ms**、p95 **1565.185 ms**、p99 **1597.134 ms**，观测完成吞吐 **1.356 请求/秒**。总耗时包含客户端创建等成本，不是纯服务端耗时，也不是开放到达率容量测量。

崩溃测试先观察到本轮请求 accepted 且具有 owner，再终止确切的自有 API 进程；重启后原键收敛 failed、零结果，结果为 `interrupted_without_partial_result`。这次计划内硬杀与正常清退分别记录：共三个 API，两个正常 CPU drain，一个用于真实崩溃测试。

## 证据及清理

本工作树本地证据根为 `artifacts/diagnostics/lifecycle-integration-20261012/`。运行前写入 plan、原始 git diff 和源码指纹，调用现有 `exercise(..., 120, 2, strict_load=True)`；运行结束后比较全部指纹和补丁，均一致。开发 helper 没有修改正式干净提交门禁；verification 明确 `production_acceptance=false`、`sla_proven=false`。报告及索引在模型运行结束后才写入。

| 本地文件 | SHA256 |
| --- | --- |
| `artifacts/lifecycle-target-20261012.xml` | `b44c8161195745c9421228ebc9e547df0723e894daefb1cc865d16b70b2f53db` |
| `artifacts/lifecycle-regression-20261012.xml` | `06f49e03b201d5a4f51733b5cb25f0f3109be37432448f7a8ec569b5f84527ae` |
| `plan.json` | `1a4788cefaa96755ad0b97a0c995adceaeb57df3414e612142ce3e56f8e333e3` |
| `source.patch` | `67a8626255a562b098b333ad3aab8adae5c6e8caa2fa93f52d6f2d680e30dc11` |
| `observations.json` | `89a49a19e606bc17748488ff5c3fbeaf0d5722a2cfdfa4b675b5c4e371fc934d` |
| `verification.json` | `70b1a41eb9e7906a57d4f6fa563feb018067ca2451e809e0b4c6deb7df93628f` |

原始文件为忽略的本地产物，不提交令牌、数据库地址、模型或大体积响应。独立复核重新检查全部 120 条观测、源码/补丁摘要及原模型 manifest；查询精确自有 schema 已不存在，三个 PID 均不存在、三个端口均不再监听。`independent-check.json` 保存核对结果；PID 检查另由 PowerShell 实查。主目录和原最新交付工作树仍干净，旧生命周期工作树原有未提交文件保留。

## 完成边界

两项已复现的正确性缺陷已在最新交付代码上修复并验证，但仍是本地候选。此次完整模型仅运行一个固定 120 笔批次，不冒充新一轮双批次正式验收；没有新增本补丁的远端 CI、连续浏览器演示、作者验收或 release。

本轮没有 504，不能提供新的失败内部轨迹，不能证明历史间歇 504 的唯一根因已经消除。R06-001 保持开放；这些生命周期修复也不作为吞吐提升或 20 QPS/300 ms/99% SLA 证明。后续交付应绑定最终提交补齐 CI 与演示验收，不直接沿用当前未提交源码状态宣称正式发布。
