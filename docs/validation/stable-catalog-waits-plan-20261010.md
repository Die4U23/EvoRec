# 目录数据库等待观测：预登记

上一轮窄帧候选已因完整目录成本恶化撤回。当前生产目录保持 N+1 有界捕获，既有 9 次候选 HTTP 504 尚未得到失败内部时间线。本轮只扩展隔离诊断工具，不修改生产 SQL、校验、持久 seal、2 秒期限、单排名 worker、连接复用、恢复锁、GC 或批准模型。

## 观测边界与功能门禁

开关默认关闭。启用后，每个被测目录调用使用一个独立 autocommit 只读采样连接，只读取自有数据库、同一角色、目标 PID 与 backend_start 对应的活动状态、等待事件及阻塞者数量。采样间隔 20 ms、最多 256 条；读取语句期限 500 ms、建连期限 3 秒。采样线程必须退出、读连接必须释放后才能交付报告。无活动样本、采样错误或达到容量均不能作为完整观察通过；它不表示连续覆盖。

不保存 SQL、参数、DSN、凭据或阻塞者 PID。等待事件是采样事实，execute 总耗时不是锁等待时长。采样间隙可能漏掉短等待，wait_event 为 NULL 也不证明正在执行 CPU 工作。[PostgreSQL 活动与等待说明](https://www.postgresql.org/docs/18/monitoring-stats.html)

Windows 原生 CPU 读取只用查询权限，核对 postgres.exe 与进程出生时间；计算同一 leader 的 user+kernel 累计时间差。不包括并行 worker，不以主机 PID 推断容器 CPU；不可验证的映射及读取失败均记 unknown，不能写成零。[GetProcessTimes 文档](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)

先执行实库主动睡眠、实际行锁、取消自有 SQL 的正反控制，检查原始异常、采样边界、默认关闭、失败记录、线程及连接清退；所有失败和跳过保留。此取消控制不等同于 API 504 后排名线程、请求租约和结果写入的清退验收。

## 一次固定完整目录观察

在干净提交运行一次完整批准包：UUID `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`、137,249 项、manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`。隔离 schema 与服务进程，发布后停止空闲 API 再测目录；商业服务和数据库不变。

固定全部上架、3 项下架两种状态，每种按 baseline/new/new/baseline 共 4 笔，合计 8 笔，不重试、不扩样。baseline 精确提交 `791311450f213e4cac7710c7e5f7285b2ffea240` 与本轮生产 SQL 相同，两种标签只是固定顺序重复观察，不是优化 A/B，也不是独立 HTTP 样本。全部摘要和资格集合必须相等。

另采两种状态各 2 份 EXPLAIN、N+1 越界各 1 份 EXPLAIN，共 6 份；这些语句不插入等待观察，观察成本单列。启用采样的 execute 数值不与旧未插桩数值直接作速度对比。记录源码/SQL 摘要、模型身份、数据库版本和设置、leader CPU、所有样本、截断/错误/覆盖、schema 和进程清理。源码中途变更、语义不等或观测不完整退出非零，仍保留原始记录；不重跑直到通过。

本轮不做 HTTP 吞吐或最终两轮 120 笔验收，不将成功目录调用外推解释旧 504。下一项修改只由本轮实际证据决定；无有效等待或 CPU 证据时保留根因未知。
