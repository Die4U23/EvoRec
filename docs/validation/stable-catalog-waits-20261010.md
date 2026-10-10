# 完整目录等待观察：有临时文件等待，超时根因仍未关闭

本轮按[预登记](stable-catalog-waits-plan-20261010.md)只增加默认关闭的诊断观察；生产 src 与 `791311450f213e4cac7710c7e5f7285b2ffea240` 完全相同。原 2 秒期限、单排名 worker、批准包、实际目录校验和持久 seal 均未调整。没有运行 HTTP 负载，也没有修改业务环境。

## 来源、实现与测试

实测绑定干净提交 `cd6082dd40243fea0438a725478e9b125e75e6b9`，首尾 HEAD/源码摘要不变。baseline 为 `791311450f213e4cac7710c7e5f7285b2ffea240`；从 Git 读取的 SQL SHA-256 是 `98b626558bda6700e0e6945f5ce550cad988687a68c17268efc648319542fccf`，独立核对与当前 SQL 字面量相同。baseline/new 只是固定执行标签，不是两个优化版本。

使用完整批准包 UUID `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`、137,249 商品，manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，model version `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。环境为 Windows、PostgreSQL 18.6、psycopg 3.3.6、NumPy；work_mem=4MB、plan_cache_mode=auto、jit=on、prepare_threshold=5。开始前 19:51:46 +08:00 的 CPU 快照为 3%、12 逻辑核，只是启动快照，不是连续负载隔离证明。

观察器只读取目标自有 PID+backend_start、同库同角色的状态、等待事件及阻塞者数量；不导出 SQL、参数、凭据、DSN 或阻塞者身份。容量和错误会使观察不完整，CLI 空记录也不能通过。默认关闭时不创建采样连接；失败 SQL 仍保留原异常与观测，读线程退出后才报告。

主线程分别复核质量、性能和复用边界：CPU 读取失败不能遮盖 SQL 异常或遗留读线程；采样容量明确、默认关闭不额外查询；沿用自有 schema/服务生命周期和脱敏 marker，不改生产路径。team-mode 接口缺少规定的 agent_type，本轮未派遣代理，也不声称独立代理评审。

测试正反控制覆盖真实 pg_sleep、真实 FOR UPDATE 行锁、pg_cancel_backend 取消自有睡眠 SQL、读连接 PID 消失、目标连接仍可用、CPU 出生身份/未知映射、原生访问失败、容量截断、无活动覆盖、默认关闭、失败保存与 CLI 完整性。

| 阶段 | 项数 | 失败 / 错误 / 跳过 | 时长 s |
| --- | --- | --- | --- |
| 首轮开发：临时目录父路径缺失 | 69 | 0 / 9 / 0 | 8.697 |
| 修正测试 basetemp 路径后的开发检查 | 85 | 0 / 0 / 0 | 8.230 |
| 干净 cd6082d 六模块复测 | 116 | 0 / 0 / 0 | 52.029 |
| 报告归档后的观察器/helper/卫生复测 | 85 | 0 / 0 / 0 | 7.790 |

首轮 9 项 setup 错误不能算通过；没有改产品代码来消除它们，改用已存在 artifacts 下的全新临时目录。四组范围重叠，不相加。最后一组含待提交报告，代码仍与 cd6082d 相同；之后只补档这项结果。实库 SQL 取消控制不证明 API 504 的排名线程、执行租约和结果写入清退。

## 固定八笔调用：保留漂移而非选择好结果

按两种状态各 baseline/new/new/baseline 执行，一次固定批次、不重试、不扩样。全上架资格数均为 137,249；3 项下架均为 137,246，同状态完整摘要相等。下表仅列 active 且位于调用窗口内的样本；NULL 表示未报告等待事件，不等于 CPU 执行证明。

| 状态/顺序 | execute ms | 资格校验 ms | active 样本 | NULL / BuffileRead / BuffileWrite |
| --- | --- | --- | --- | --- |
| 全上架 / baseline | 672.76 | 0.008 | 21 | 18 / 1 / 2 |
| 全上架 / new | 606.82 | 0.009 | 19 | 18 / 0 / 1 |
| 全上架 / new | 1,074.42 | 0.014 | 34 | 28 / 3 / 3 |
| 全上架 / baseline | 2,388.02 | 0.014 | 76 | 67 / 4 / 5 |
| 3 项下架 / baseline | 2,379.19 | 117.66 | 75 | 62 / 3 / 10 |
| 3 项下架 / new | 2,383.48 | 117.17 | 76 | 61 / 4 / 11 |
| 3 项下架 / new | 2,328.33 | 131.60 | 74 | 61 / 7 / 6 |
| 3 项下架 / baseline | 2,424.83 | 120.26 | 77 | 63 / 6 / 8 |

八笔 fetch/decode 为 0.0096–1.0266 ms。8/8 观察器均正常结束，有 active 样本且没有采样错误/截断，全部阻塞者计数为 0，未采到 Lock。每笔实际最大相邻采样间隔为 32.05–39.78 ms；20 ms 是设置的采样休眠，不是实际固定频率或连续覆盖。等待样本数量不换算精确等待时长，未采到锁也不能排除短锁等待。[PostgreSQL 活动与等待说明](https://www.postgresql.org/docs/18/monitoring-stats.html)

5/8 的 driver execute 超过 2 秒，但这里不是 HTTP 请求，因此不能报作 5 个 504；同时启用了观察，不与旧未插桩耗时直接比较。同一 SQL 的全上架第四笔已明显变慢，不能把后半段耗时全部归于下架 Python 校验。

八笔 leader CPU 均为 unknown，不能当零或推断 CPU/GIL 根因。补充只读核验一个新建自有 PostgreSQL 后端时，Windows 原生句柄读取返回 PermissionError；原始八笔没有逐笔导出拒绝原因，不能将这个补查当作八笔各自错误证明。本轮不提升权限或修改服务进程 ACL。CPU 接口仅在 image/出生时间可验证时读取 leader 的 user+kernel，不包含并行 worker。[GetProcessTimes 文档](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)

## 六份另采执行计划与清理

全上架两份 EXPLAIN Execution Time 为 2,488.00 / 2,505.05 ms；下架两份为 2,477.38 / 2,503.90 ms。四份正常计划均扫描 137,249 items，external merge / Disk 排序文件 24,752 kB，根 Temp Read/Write Blocks=7,877/7,887。没有 JIT 字段，不从 jit=on 推断发生了 JIT。

两份越界计划为 122.10 / 129.81 ms；实际成员 N+1=137,250，items 的 Actual Loops=0、Actual Rows=0，捕获均以 bundle_members_changed 拒绝；根临时写块为 569。所有 EXPLAIN 独立于八笔观察，没有将计划时间或各节点临时块之和当作请求成本。

run_id=`dc882c9a-50c1-4a92-9589-14db420baa00`，自有 schema=`test_evorec_eb24e968d41749049dcdb19968df98fd`。空闲 API 在目录观察前正常停止，PID=32708、port=54424，CPU jobs drained=true，无硬杀。结束后独立查询确认该 schema 已移除、自有目标及八个读后端共 9 个 PID 已从 pg_stat_activity 消失，进程/监听均消失，剩余 test_evorec_ namespace=0。

只删除本轮临时数据库数据，页面无法恢复；原始证据和只读模型保留。主目录 `E:/codex/proj` 仍干净于 `6e44594f55dd180ec4bacce1ed8d19e7bf4aee61`，未操作业务 schema/8000；.env SHA-256 保持 `32f05a41febf54595c5cef010ecee661c47570ac9e0a41238501b9297939e267`。

## 可执行结论与下一项

实际采到临时文件等待，且另采计划确认磁盘排序/临时块；没有本轮锁等待证据，CPU 原因仍未知。因此暂不改恢复锁、连接、worker 或期限。下一轮先把每笔目录调用的计划形状、表统计/ANALYZE 状态及 prepared statement 状态绑定起来，核验同 SQL 早快后慢是否伴随计划变化；不先认定自动 ANALYZE 或 generic plan 就是根因，也不调多个参数。

八笔成功 SQL 的观察不能解释此前 9 次 HTTP 504。失败请求时间线、取消拖尾、独立 session/客户端复用诊断、两轮 120 笔、恢复/浏览器/备份和稳定冻结仍是未完成门禁。本轮交付诊断草稿 PR，不合并为稳定版；精确最终 head CI 在 PR 核对。

## 本地原始证据

路径相对工作区，均在 Git 忽略的 artifacts 内；公开仓库仅保存本报告和代码，不提交数据库、模型或大量原始采样。

| 文件 | SHA-256 |
| --- | --- |
| artifacts/wait-dev-20261010.xml | 483a9bace09f74607f75c1219d2f1acb3daf1c062d0a3d977b13c6f5e601a45d |
| artifacts/wait-dev2-20261010.xml | 3552b6bbf3bc59c34860849d908f10fe8ba50f68bb44fa5b72642d033bf6a962 |
| artifacts/wait-clean-20261010.xml | f3edeb566b3f3c29e39cf0b4cb57eb161ada3ab9a2263c4d3cbcf3d7c334656a |
| artifacts/wait-final-docs-20261010.xml | 8297803da2b94587f891a8ed16028a4eb9109dc68f43bc2f4f654f99cdfbf9dd |
| artifacts/diagnostics/stable-catalog-waits-20261010/catalog-profile.json | 145d8a700e9e2d17561113ed216b3c9c4bbbec2121d7703d21f11c48eed40a15 |
| artifacts/diagnostics/stable-catalog-waits-20261010/catalog-observations.json | b757eeabad755aaeb4f213e6da3c7dc3da4a671176c86dc735f5b426f34e9194 |

观测器源码字节 SHA-256=`b5d2c3ea0285c0a94bc5dc29fc6ba1f51b70747f55b16cd424d4c4c783b6f734`，与运行报告中 scripts/r06_database_observer.py 相等；完整 src/scripts/db/migrations 摘要留在原始报告。后续只归档文档，不改本轮被测实现。
