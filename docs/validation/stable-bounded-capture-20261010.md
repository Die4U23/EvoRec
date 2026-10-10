# 冻结目录有界捕获：校验、完整包对照与未关闭风险

## 本轮结论

N+1 实际成员限界及校验反例通过；候选不能作为稳定版合并。未插桩初筛 24/24 成功，但预登记 A/B/B/A 的两轮候选合计只有 39/48 成功，9 次 504 全部保留。两批各 8 笔双并发的阶段诊断都没有复现失败，因此这 9 次失败的具体内部阶段仍未知，不以成功时间线倒推锁、GIL 或取消根因。

下一项优先检查目录执行计划的排序、临时块与哈希输入组织，之后再评估下架集合的 Python 校验成本。这里是有直接成本证据的调查优先级，不是已经证明这些操作导致了那 9 次 504。暂不调整连接复用、恢复锁、排名 worker、GC 或服务期限。

## 源码与环境绑定

- 旧 A：`84c9bbcb96f4e428ab5aaf7f6eebdc743e0dc418`；独立工作区 `C:/Users/7/.codex/worktrees/r06-capture-control-a/proj`。
- 候选 B：干净 `ef890930a088fffcbf32eb16c3d51522788ef5c5`；工作区 `C:/Users/7/.codex/worktrees/r06-probe-controls/proj`。每次测量首尾核对提交及 `src/`、`scripts/`、正式迁移的文件 SHA-256，原始报告保留这些哈希。
- 批准完整包：137,249 商品，bundle `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`；manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`；实际响应 model version `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。
- Windows 11、Python 3.12.6、i5-12450H / 12 逻辑 CPU，NumPy 环境；PostgreSQL 18.6、psycopg 3.3.6。CI 的 PostgreSQL 16 是另一种功能验证环境，不用于替代本机性能证据。
- A/B/B/A 启动前 CPU LoadPercentage 快照依次为 26%、36%、39%、31%，时间依次为 15:03:32、15:06:35、15:09:18、15:12:29（UTC+8）。仅是启动前快照，不是测量期间负载隔离或连续监控；Windows load average 未取得。客户端创建成本也明显波动，不宣称严格因果或统计显著性。
- 独立自有 schema 与随机 loopback 端口，原 2 秒期限、单排名 worker。主目录、`.env` 字节、业务库、批准模型与 8000 服务未修改；没有访问用户页面。

## 资源边界和校验

[实际 SQL](../../src/evorec/infrastructure/r06_catalog_capture.py)先 materialize 最多 N+1 条实际 bundle 成员，包括下架项，不先过滤批准 ID。实际数量不等于 N 时，商品连接及文本哈希分支不执行；成员变化优先返回 `bundle_members_changed`。N 的类型/上限也在发出 SQL 前检查。这个限界限制聚合输入，不自动证明底层扫描、单字段字节量或整体数据库成本有界。

数量相符时仍校验实际成员身份/内部索引、有效商品的实际 UTF-8 文本 SHA-256 与原始毫秒时间、NULL、资格集合；不使用数据库可修改的预存 digest。成员和商品数据取自同一条 SQL 的读视图。读指纹与原持久 canonical seal 分离，未换掉持久快照格式、已见集合、执行租约或清退语义。

[独立旧谓词对照](../../tests/test_r06_capture_equivalence.py)覆盖全上架、部分下架、全部下架、前后及大量额外成员、删除、同数量替换、索引变化、文本/时间/NULL、下架内容漂移与恢复上架。SQL 期间并发修改测试利用仅在自有 schema 存在的门闩函数，确认 writer 在 SELECT 哈希暂停期间提交；第一次仍采用该 statement 的旧视图，下一次看到修改。有效分支核对资格与原 canonical seal，异常分支核对错误及优先级。这是已覆盖用例中的等价证据，不声称穷举所有并发调度。

| 功能证据 | 结果 | 来源边界 |
| --- | --- | --- |
| 旧聚合候选开发 XML | 349 / 0 失败、错误、跳过 | dirty 开发态，随后候选归档到 `a65c1eb`；不是干净提交启动的正式验收 |
| N+1 初次开发对照 | 28 / 0 失败、错误、跳过 | 开发态，涵盖实际 SQL 并发门闩 |
| 扩展回归 | 378 / 0 失败、错误、跳过，448.548 s | 在源文件不变的开发态执行，之后原样提交 `ef89093`；不是干净提交启动 |
| 干净提交复验 | 34 / 0 失败、错误、跳过，41.757 s | 在干净 `ef89093` 首尾核对，目录 digest、等价和测量 helper 三模块 |

这些测试彼此重叠，不相加当作一个测试总数。恢复与连接模块的开发回归通过，不表示历史 orphan 失败根因已查清，更不替代完整包恢复门禁。

## 完整目录成本和执行计划

[测量工具](../../scripts/profile_r06_catalog_capture.py)在批准包发布的自有 schema 中停下空闲 API 后，以全部上架、3 项下架两种状态各执行旧/新/新/旧。驱动 execute 包含 SQL、驱动与接收成本；fetch/decode 也不是纯网络或纯 Python。旧行实现没有在该微测量中计时其完整 Python 文本/时间校验，因此不能从下表声称整个目录阶段净提速。

| 状态/实现 | 两次 execute ms | 两次 fetch/decode ms | 候选资格校验 ms | 返回行数 |
| --- | --- | --- | --- | --- |
| 全上架 / 旧 | 317.69 / 578.85 | 86.56 / 69.94 | 未测旧完整校验 | 137,249 |
| 全上架 / 新 | 674.68 / 679.51 | 0.53 / 0.01 | 0.02 / 0.02 | 1 |
| 3 项下架 / 旧 | 712.24 / 781.16 | 83.45 / 64.85 | 未测旧完整校验 | 137,249 |
| 3 项下架 / 新 | 2,830.76 / 2,679.40 | 0.04 / 0.02 | 190.83 / 142.57 | 1 |

传输结果行数及 decode 成本减少，但 SQL 计算没有消失；单连接、有限次数、缓存/负载变化下的观察不能直接替代服务请求延迟。特别是少量下架的 execute 已超过 2 秒，形成独立的明确风险，不能被全上架初筛成功掩盖。

另采集五份 `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON)`，不将其观察耗时混入 HTTP 结果：

- 候选本次成员计划使用 index scan + LIMIT；正常实际读取 137,249 条，越界读取 137,250 条。只能证明本次计划，不外推所有数据分布。
- 候选正常计划仍扫描 items 并出现 external merge / Disk。汇总 Aggregate 的 Temp Read/Write Blocks 为 7,877 / 7,318；节点块数包含子计划开销，不能重复相加。全上架/3 项下架 EXPLAIN execution time 分别为 2,991.15 / 2,919.22 ms，独立归档，不等同于未插桩驱动调用。
- 越界一项计划的 items scan Actual Loops=0、Actual Rows=0，确认该计划没有进入商品文本分支；仍存在 bounded_members 的物化成本，不能称为零成本。越界返回拒绝，执行计划 execution time 169.23 ms。

## 完整模型 HTTP 初筛与 A/B/B/A

每轮独立 schema/进程，相同 sample 历史、dense/k=10、两笔顺序预热后 24 笔闭环双并发，同一 session、不同新请求键；无自动重试。服务器插桩、GC 观察、阶段闸门关闭（不关闭 GC 本身）；每笔新建 HTTPX client。没有只给候选使用持久 client 或独立 session。所有失败进入分母。

| 轮次 | 源 | 200 / 504 | 创建 client p95 ms | HTTP 往返 p95 ms | 客户端总 p95 ms |
| --- | --- | --- | --- | --- | --- |
| 初筛 B | ef89093 | 24 / 0 | 352.37 | 1,751.55 | 2,084.35 |
| A1 | 84c9bbc | 17 / 7 | 1,273.11 | 2,645.23 | 3,735.55 |
| B2 | ef89093 | 22 / 2 | 631.20 | 2,292.65 | 2,799.95 |
| B3 | ef89093 | 17 / 7 | 725.57 | 2,298.06 | 2,968.48 |
| A4 | 84c9bbc | 0 / 24 | 729.40 | 2,794.99 | 3,478.06 |

各 p95 采用最近秩，分母为该轮全部 24 笔，包含失败；不同列的 p95 不一定属于同一请求，不能相加。四轮共 96 笔，旧 A 17/48 成功、新 B 39/48 成功。B 的稳定门禁不通过，不能用初筛全过或脚本退出 0 宣称修复；同样不能将客户端总耗时全部归给服务端。四轮顺序预热都为 200。

此前旧基线 10/24 成功、14 次 504 也继续保留。其两笔顺序 client 创建约 500.69/510.75 ms，HTTP 往返约 1,453.09/1,325.17 ms，不用于推断服务端余量。

## 阶段诊断及协议偏差

在读取初筛结果的同一条命令中提前启动了第一批 8 笔双并发阶段诊断，不符合原计划“只有初筛失败才执行”的顺序；已当场告知，原始记录全部保留，不算入初筛或 A/B/B/A。该批 8/8 成功，加两笔顺序共 10 笔；成功目录约 752–812 ms、队列等待最高约 127 ms。

两轮候选出现 9 次 504 后，另在执行前告知一批固定 8 笔双并发诊断。它也 8/8 成功，加两笔顺序共 10 笔；追踪覆盖、成功数据库边界及客户端/服务端状态对应均通过。负载成功请求目录 803–908 ms、排名等待 0.10–118.17 ms、排名执行 172–274 ms、写入 89–149 ms、lease close 0.13–0.38 ms。事件含起止和嵌套关系，不相加。

两批诊断都未抓到失败，不能替代未插桩失败，也没有期限触发、取消处理、后台结束、租约释放、响应完成的失败时间线。已停止增加样本，不以反复诊断直到“看起来全绿”作为验收。下一轮需要预登记覆盖失败时上述时刻的观测，保持同一个剩余预算和资源所有权；不能提前释放仍被后台使用的资源。

## 独立门禁和未执行项

主线程另从原始 requests 重算四轮 200/失败数，核对 24 笔分母、26 笔含预热、2 秒期限、无重试/插桩、manifest/数量与成功响应身份，不只检查诊断退出码。身份检查是现有真实响应 legal 条件，未在这个离线摘要中重建全部 item-level 持久账本。

八个本轮自有 schema 的 owner/stopped/run_id、子进程 CPU drain 标记独立核对；数据库 pg_namespace 查询确认八个确切 schema 都不存在。清理的是本轮临时数据，不能从页面恢复；原始报告与批准包保留。主目录仍干净，`.env` SHA-256 首尾为 `32f05a41febf54595c5cef010ecee661c47570ac9e0a41238501b9297939e267`。

两轮各 120 笔、完整包 orphan/已提交但客户端超时的同键重放、owner/租约/结果账本独立验收、浏览器 epoch/version 迟到响应、备份恢复和稳定版冻结仍未执行本轮最终验收。PR 保持草稿；CI 是精确 head 的功能回归门禁，不是负载失败消失的证据。

## 本地原始证据索引

原始目录位于候选工作区的 `artifacts/diagnostics/`，被 Git 忽略；A1/A4 从旧工作区复制保留，未改变字节。仓库只保存本摘要，不新增数据库、模型或大规模轨迹。

| 本地文件（相对候选工作区） | SHA-256 |
| --- | --- |
| artifacts/bounded-clean-20261010.xml | 632847af069e3ceb06408fb1836032ea852c49c95ee23bbfa2d1a507c3354df3 |
| artifacts/bounded-regression-20261010.xml | c2f13d64d1e9f400e94717bfad28579a19ab238ef9cc2d729bb69595819fa26b |
| artifacts/bounded-equivalence-20261010.xml | ed42129e22848998390418dd8829ef18aa5c38e620c22f3bc24a25d766998834 |
| artifacts/stable-digest-20261010.xml | 016440b211cc6062c14e18b24d640610010e29bf4d345cf987a59e456b746bd1 |
| artifacts/diagnostics/stable-bounded-catalog-20261010/catalog-profile.json | 2b106d5c17e37acc3c088594041e2fee9961d8eacfea5fb675e04022800f39ab |
| artifacts/diagnostics/stable-bounded-screen-20261010/profile.json | 18085381398825ecd70b807371e7952e22e10211791890745b7309dd9bf782a7 |
| artifacts/diagnostics/stable-abba-01-a-20261010/profile.json | 7173a80e274e14b1c142219c96c7ac415c2c2426fd27c63e362f638eb4170bb7 |
| artifacts/diagnostics/stable-abba-02-b-20261010/profile.json | cca27998d1782e11e8d3944b7c3d232af41641a6a0a05fe20692964fbd47fbf7 |
| artifacts/diagnostics/stable-abba-03-b-20261010/profile.json | 7d9d5c58ad548e2020432e7f63cf89247f6205eda3b4f89050f35d3b74c75f75 |
| artifacts/diagnostics/stable-abba-04-a-20261010/profile.json | 04286893d7cf5e531da9d73476d417737360c1257d461cd4b056fafe6f839c31 |
| artifacts/diagnostics/stable-bounded-trace-20261010/profile.json | 309afe56a53178dd66c179c9901928bff2450957d041771ecb4a679892e4cca9 |
| artifacts/diagnostics/stable-bounded-failure-trace-20261010/profile.json | 5742d0d0a3d4caa514f70ed9928f61f2e2276f7d807a1413beb074669e849457 |

复跑入口与冻结口径见[预登记计划](stable-runtime-plan-20261010.md)、[目录工具](../../scripts/profile_r06_catalog_capture.py)、[TCP 工具](../../scripts/profile_r06_tcp.py)。每次使用新的自有输出路径，不覆盖已有报告；profile CLI 退出 0 表示诊断完成，仍须独立断言失败数与身份/清退才能判定门禁。
