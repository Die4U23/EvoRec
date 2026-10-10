# 同 SQL 漂移复核：fresh 计划不变，未发生目标 generic 切换

本轮按[预登记](stable-catalog-plans-plan-20261010.md)补充逐笔计划与统计状态，未修改生产 SQL、实际校验、持久 seal、2 秒期限、单排名 worker、连接/恢复或业务环境。完整观察不能关闭此前九次 HTTP 504，也不是稳定版门禁通过。

## 可执行结论

同一目录 SQL 仍从约 0.64 秒变为 2.4 秒，但 16 组前后 fresh 计划的结构和估计摘要完全相同。两张表的 ANALYZE 时间/计数、reltuples/relpages 在这些快照中没有变化。目标具名 prepared 在第六笔后才出现，后三笔 custom=1→2→3、generic=0；第三、四、五笔已经超过 2 秒。因此本轮不支持以目标具名 generic 切换解释本轮早快后慢，不先改 prepare_threshold、plan_cache_mode 或 ANALYZE 设置。

这不是“实际执行计划完全不变”或“排除所有统计修改”：fresh EXPLAIN 不是实际/缓存计划，统计还会滞后，快照之间存在间隙。这里只排除未经证据的修改方向。下一项优先测实际目录哈希、连接及排序成本；若试窄投影，必须避免上轮新增物化临时写入，先过等价与完整目录成本门禁，不同时调其他参数。

## 来源与功能门禁

运行绑定干净提交 `6d6efcd975585e70ba25e4c4791dd12582e7f87c`，首尾 HEAD、src/scripts/db/migrations 字节摘要不变。baseline 为 `80c8e7709ae000da238dce96b23c1b06f117d191`；SQL SHA-256=`98b626558bda6700e0e6945f5ce550cad988687a68c17268efc648319542fccf`，独立核对与当前 SQL 相同。baseline/new 仅执行标签，不是优化 A/B。

完整批准包 UUID=`bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，137,249 商品，manifest=`9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，model version=`eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。环境为 Windows、PostgreSQL 18.6、psycopg 3.3.6、NumPy；work_mem=4MB、plan_cache_mode=auto、jit=on、prepare_threshold=5。20:24:12 +08:00 的启动 CPU 快照为 16%、12 逻辑核，不是连续负载隔离证明。

完整包准备在目录测量之前完成；准备较上一轮慢，自有 Python 进程检查显示仍存活，累计 CPU=155.609 s 的一次观察不表示瞬时 CPU 占用。本轮没有重启、重试或追加批次，启动/准备开销不计入八笔 execute。

新增开关默认关闭，只读取自有随机 schema 和同一连接的状态；元数据调用全部 prepare=False。目标文本在本地按受支持的内部占位符转换、仅用于匹配 pg_prepared_statements，不导出 SQL/参数/statement。fresh 计划只保留白名单结构与估计，去掉 Output/Filter/Index Cond 等表达式，有节点数量和深度上限。原工具另采六份原始 ANALYZE 计划的行为未改。

主线程分别复核质量、性能和复用：拒绝非自有 schema、缺少目录表和非法计划，保留失败阶段；默认关闭无新增元数据调用，非执行 EXPLAIN 不改变目标选择计数；沿用既有 lab/marker 生命周期，不新增生产连接所有权。team-mode 接口缺少要求的 agent_type，本轮串行完成，没有独立代理评审。

| 阶段 | XML 项数 | 失败 / 错误 / 跳过 | 时长 s |
| --- | --- | --- | --- |
| 首轮开发：超长 pytest 标签 | 95 | 0 / 2 / 0 | 11.229 |
| 缩短标签后的开发检查 | 111 | 0 / 0 / 0 | 10.970 |
| 补充 CLI/失败保存控制后 | 116 | 0 / 0 / 0 | 12.725 |
| 干净 6d6efcd 七模块复测 | 147 | 0 / 0 / 0 | 84.443 |
| 报告归档后的计划/helper/卫生复测 | 68 | 0 / 0 / 0 | 2.699 |

首轮超长参数标签使 Windows 的 PYTEST_CURRENT_TEST 超过 32,767 字符，setup/teardown 两处错误保留，不算通过；仅缩短测试 ID，没有改待测拒绝逻辑。五组范围重叠，不相加；最后一组含待提交报告，实现仍与 6d6efcd 相同，之后仅补档结果。正反控制证明：真实强制 custom/generic 测试的目标计数不会被快照增加；非 ANALYZE 的 fresh EXPLAIN 不调用自有 VOLATILE 抛异常函数，直接调用该函数确实失败；ANALYZE 后 pg_class 估计变化能被记录；缺失/空批次、容量/深度、错误阶段和跨 schema 均拒绝。强制设置仅在小型隔离测试中使用，没有用于完整模型观察。

## 一次固定八笔与十六组快照

按全上架/三项下架各 baseline/new/new/baseline 一次执行，不重试、不扩样。同状态完整捕获摘要相等；资格数分别均为 137,249 / 137,246。前后元数据成本与资格校验不包含在 execute；本轮含等待采样，不与旧未插桩结果当作提速 A/B。

| 顺序/状态/标签 | execute ms | 资格校验 ms | 目标具名 prepared（前→后） | active 样本（NULL / Read / Write） |
| --- | --- | --- | --- | --- |
| 1 全上架 baseline | 643.29 | 0.015 | 无→无 | 16 / 0 / 4 |
| 2 全上架 new | 1,753.31 | 0.015 | 无→无 | 53 / 1 / 2 |
| 3 全上架 new | 2,405.85 | 0.017 | 无→无 | 67 / 2 / 8 |
| 4 全上架 baseline | 2,358.83 | 0.011 | 无→无 | 66 / 4 / 5 |
| 5 三项下架 baseline | 2,368.06 | 347.34 | 无→无 | 64 / 1 / 10 |
| 6 三项下架 new | 2,359.12 | 294.43 | 无→custom=1 / generic=0 | 64 / 3 / 7 |
| 7 三项下架 new | 2,380.41 | 345.05 | custom=1→2 / generic=0 | 66 / 2 / 8 |
| 8 三项下架 baseline | 2,357.30 | 337.75 | custom=2→3 / generic=0 | 62 / 3 / 10 |

第六笔后目标名为 `_pg3_1`，from_sql=false。前五笔的“无”只表示视图中没有该目标的具名条目，不能伪造 custom=0；它不统计所有未命名协议执行。prepare_threshold 的语义与实际计数分开核对，不能混淆 psycopg 自动准备与 PostgreSQL generic/custom 选择。[psycopg 自动准备](https://www.psycopg.org/psycopg3/docs/advanced/prepare.html)、[当前会话计数](https://www.postgresql.org/docs/18/view-pg-prepared-statements.html)

十六份 fresh_plan_sha256 均为 `cc0231f0b4a705289547eb0826ac386861d68e5d4ca2d1a2fe01fe2b441b1e40`，shape_sha256 均为 `f0b0af8923c0075d2b8713167ffba1e0b52664b7da23504324db70d28020592c`。估计根成本 89,359.8，目录主体含 Hash Join、items Seq Scan、Sort、Aggregate；这些是估计，不是实际阶段耗时或实际 prepared 计划。

两表 reltuples 始终为 137,249；bundle_items relpages/relallvisible=1,144/1,144，items=8,649/8,649。manual ANALYZE 时间分别为 20:26:10.710942 / 20:26:10.647011，auto ANALYZE 为 20:26:37.397696 / 20:26:39.935718（+08:00），manual/auto 计数均为 1。三项下架后 items 的累计 n_mod_since_analyze/n_dead_tup 稍后可见为 3；没有借机运行 ANALYZE 或冻结统计。快照不是原子视图，累计统计可滞后，fresh 计划不等于执行语句缓存计划。[PostgreSQL 统计刷新](https://www.postgresql.org/docs/18/monitoring-stats.html)、[准备语句与重规划](https://www.postgresql.org/docs/18/sql-prepare.html)

十六组元数据观察耗时 4.42–22.16 ms，八笔 fetch/decode 0.016–0.049 ms。等待观察 8/8 均有 active 样本、正常结束，无采样错误或截断；采到 IO/BuffileRead、IO/BuffileWrite，未采到 Lock，阻塞者计数均为 0，实际最大相邻采样间隔 32.23–48.56 ms。NULL 不等于 CPU，样本数量不换算精确等待时长；CPU 均 unknown，不能归因 GIL 或排除短锁等待。下架 Python 校验本轮更慢，保留真实数值，不以预启动 CPU 快照解释。

## 六份另采实际执行计划

正常计划四份 Execution Time 为 2,496.319 / 2,489.863 / 2,443.046 / 2,470.598 ms，均有 137,249 items 扫描、24,752 kB external merge/Disk 排序，根 Temp Read/Write=7,877/7,887。没有 JIT 字段，不把 jit=on 当作已发生 JIT。它们是额外 ANALYZE 语句，不是上述八笔的实际执行轨迹，不与其计时混合。

N+1 两份为 128.589 / 131.791 ms，实际成员数 137,250，items Actual Loops/Rows 均为 0，根临时写块 569。两次实际捕获均拒绝 bundle_members_changed。完整目录校验、资源边界与资格语义没有回退。

## 独立断言、清理与交付边界

除诊断退出码外，主线程独立断言 8 笔/16 组/6 份计划、模型及 SQL 身份、全部源码字节摘要、资格数量、树白名单、两个计划摘要重算、同一 target PID/自有 schema、观察器完整性，以及所有失败状态。精确 schema 与九个自有数据库 PID 另查已消失，不只相信生命周期标志。

run_id=`92669a52-a539-422c-89ad-cfb81a9b5d63`，schema=`test_evorec_6de4a56fe373439781e912f05ea6b246`，空闲 API PID=33444、port=56841。正常 CPU jobs drain，未硬杀；独立检查进程/监听均消失，剩余 test_evorec_ namespace=0。只移除本轮临时数据库数据，无法从页面恢复，原始证据与只读模型保留。

`E:/codex/proj` 仍干净于 `6e44594f55dd180ec4bacce1ed8d19e7bf4aee61`；未操作业务 schema 或 8000，.env SHA-256=`32f05a41febf54595c5cef010ecee661c47570ac9e0a41238501b9297939e267` 不变。后续只归档文档，生产 src 与 baseline 无差异。

草稿 PR 保留诊断实现和负面结论，不合并稳定版。精确最终 head CI 独立在 PR 核对。此前 504 的失败内部阶段、取消拖尾、独立 session/客户端复用对照、两轮 120 笔、恢复/浏览器/备份及版本冻结仍未关闭。

## 本地原始证据

以下是被 Git 忽略的 artifacts 路径；公开仓库只提交实现、测试、计划与摘要，不提交数据库、模型或原始大采样。

| 文件 | SHA-256 |
| --- | --- |
| artifacts/plan-dev-20261010.xml | 0dad77a18b2a7d9ee4e39ea492258487de3b0e11124e3f0ef77bf1341bb2e29c |
| artifacts/plan-dev2-20261010.xml | 2e781971da2ac3c4df487d2273eb08b0e19edb63410c332d3d2d65828dbe1b8f |
| artifacts/plan-final-dev-20261010.xml | de896e55ff74ac5116693d605b8d8997a3dbf04c0cb89802014e939502b4d0b3 |
| artifacts/plan-clean-20261010.xml | a627171d4cf12a34be53899fb861e3912e12ebab260ec6e3229f7378a7b08d35 |
| artifacts/plan-report-20261010.xml | 63f765dc3af4f1fadca7f0925a28c9640bf7be17d827da1d3557a3c60bf22d0a |
| artifacts/diagnostics/stable-catalog-plans-20261010/catalog-profile.json | f2641569e812e904f6302b9ef3973d0c8f8fe465147b5b06327c62b31bd0a528 |
| artifacts/diagnostics/stable-catalog-plans-20261010/catalog-observations.json | b832c45ce3558fd4eb840ff335f24da46fa54f7768928a008509dec3c28fc3fe |

scripts/r06_planning_observer.py 字节摘要=`a7771cad5043c9427a691962ff751f25c9c1d5728c6ea4666e0177e6a46866bb`，与运行报告相等；其余源码摘要保存在原始 JSON 中，可按精确提交重建。
