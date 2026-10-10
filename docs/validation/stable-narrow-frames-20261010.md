# 窄帧目录候选：性能否决与撤回

## 结论

候选 `d8525ae1fb0c5d754cc88753f7b5cf903073377d` 的实库校验与完整目录摘要对照通过，但性能门禁否决。先物化校验帧虽减少排序文件大小，额外物化却增加总临时写入；3 项下架时两次 execute 都慢于基线。本轮按[预登记计划](stable-narrow-frames-plan-20261010.md)停止，不执行 HTTP 初筛、A/B/B/A 或两轮 120 笔，不增加样本、修改参数或换期限追求通过。

已原样撤回生产 SQL 到 `f21d7ae`，保留 Git 中的候选提交、精确历史 SQL 对照工具、实库跳过哈希的正负控制与本报告。当前 PR 不提供性能修复；上一轮 9 次 504、失败内部阶段未知、恢复/浏览器/备份/稳定冻结仍未关闭。

## 源码、环境与功能

- 候选首尾干净 `d8525ae`，原始报告记录全部 src/scripts/正式迁移摘要；基线仅从精确提交 `f21d7aee503a41df7dbc02e0c915d9e92f9df835` 的字面 SQL 读取，不导入或执行历史 Python。Git 原始 module SHA-256 为 `4004e40a9bb4a072dccdc2f78fb5ef0d195546272e08ab12b93b2d3e40e657a6`，SQL SHA-256 为 `98b626558bda6700e0e6945f5ce550cad988687a68c17268efc648319542fccf`。
- 批准包 137,249 商品，bundle `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，实际 model version `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`，NumPy 环境；包只读，未使用小模型替代。
- Windows 11、Python 3.12.6、i5-12450H / 12 逻辑 CPU；PostgreSQL 18.6、psycopg 3.3.6，work_mem=4MB、plan_cache_mode=auto、jit=on、prepare_threshold=5，只读取未调整。启动前 CPU 快照 11%，时间 2026-10-10T15:54:58+08:00；不是持续负载隔离。测试与目录测量串行，不外推 PostgreSQL 16 CI 的性能。
- 早期开发三模块 46 项通过；扩展七模块 144 项通过、216.740 s；复核后两模块 42 项通过、44.586 s；最终候选在干净提交上三模块 51 项通过、40.916 s。均零失败/错误/跳过；开发记录不是精确最终提交验收，重叠批次不累加。
- 独立旧行谓词继续核对成员/索引、文本/时间/NULL、下架与恢复、资格集合与持久 canonical seal、SQL 执行中并发修改。新增会抛异常的实库文本函数验证越界及全部下架跳过哈希；相同 instrumented SQL 在合法数量且有上架商品时确实抛异常，避免测试函数没有被使用造成假通过。
- 主线程完成质量、性能、复用三个角度的复核。team-mode 所需 agent_type 未在当前接口提供，依技能约束留在主线程执行，没有新派遣子代理、没有修改个人代理配置；不声称独立代理审查。

## 同一目录的基线/候选对照

同一自有 schema/连接、相同批准输入，两种状态各固定基线/候选/候选/基线。基线和候选都解码同一 CatalogCapture 并执行未修改的资格校验；每个状态完整摘要相等，不仅比返回行数。均只返回一行。execute 是驱动及数据库调用墙钟时间，不是纯 SQL；资格校验单列，不相加不同轮次分位数。

| 状态 | 实现 | 两次 execute ms | 两次 fetch/decode ms | 两次资格校验 ms |
| --- | --- | --- | --- | --- |
| 全上架 | 基线 | 596.96 / 1,492.07 | 0.02 / 0.01 | 0.01 / 0.01 |
| 全上架 | 窄帧 | 620.52 / 630.00 | 0.01 / 0.01 | 0.01 / 0.01 |
| 3 项下架 | 基线 | 2,468.06 / 2,386.55 | 0.05 / 0.03 | 117.05 / 115.65 |
| 3 项下架 | 窄帧 | 2,554.27 / 2,594.76 | 0.01 / 0.01 | 114.38 / 118.31 |

全上架基线自身明显漂移，不能利用第四次变慢宣称候选总体提速。少量下架时候选两次调用均比本次两个基线慢，并且都超过 2 秒；资格校验仍约 115–118 ms，没有借机改动它。有限交错样本不证明严格因果，但已经不满足本轮继续负载验收的保守门禁。

另采六份 EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON)，不混入 HTTP 或未插桩调用。节点临时块包含子计划开销，以下只列根节点，不累加：

| 状态/实现 | Execution Time ms | Sort Space Used kB | Sort Method | 根 Temp Read/Write Blocks |
| --- | --- | --- | --- | --- |
| 全上架/基线 | 2,459.82 | 24,752 | external merge / Disk | 7,877 / 7,887 |
| 全上架/窄帧 | 2,712.09 | 16,408 | external sort / Disk | 7,504 / 9,552 |
| 3 项下架/基线 | 2,439.24 | 24,752 | external merge / Disk | 7,877 / 7,887 |
| 3 项下架/窄帧 | 2,720.60 | 16,408 | external sort / Disk | 7,504 / 9,552 |
| 越界一项/基线 | 132.79 | 25 | quicksort / Memory | 0 / 569 |
| 越界一项/窄帧 | 129.86 | 25 | quicksort / Memory | 0 / 569 |

本次正常计划仍扫描 137,249 items。窄帧排序文件较小，但物化后总写块反增 1,665；EXPLAIN 总执行也更慢，不用局部指标掩盖整体退化。两个越界计划读取实际 N+1=137,250 成员，items 分支 Actual Loops=0、Actual Rows=0；实际捕获均以 bundle_members_changed 拒绝。LIMIT 限制聚合输入，不自动证明底层扫描或字段字节量有界。

选用物化来固定计算组织、保留聚合内显式顺序依据 [PostgreSQL CTE 文档](https://www.postgresql.org/docs/18/queries-with.html)和[聚合顺序文档](https://www.postgresql.org/docs/18/functions-aggregate.html)；文档保证不等于性能收益，收益必须由实测决定。

## 撤回后的交付与下一步

最终生产 src 与 f21d7ae 没有差异。最终回归与精确 head CI 在 PR 留档，不能将候选的干净 51 项直接当作撤回后完整源的全部验证。保留新工具的 --baseline-commit 入口及 immutable SHA、Git 对象类型、字面读取/不执行、原始字节摘要测试；原有全行对照模式仍保留。

下一轮仍先定位目录 SQL：在正常字段投影之外，进一步拆解实际文本哈希、join 和排序的成本，尤其是早期约 0.6 秒、后期约 2.4 秒的来源；需要服务端真实执行阶段与等待事件，而非将 execute 耗时命名成锁等待。未取得证据前不增 worker、不调 work_mem、不延长期限、不改连接/恢复临界区，也不把 EXPLAIN 额外观测的时间当作请求时间线。

本轮临时 schema 为 test_evorec_da79d8af55c64b379498e4eea50d4888，run_id 为 069b6f26-9cf4-4955-9c91-125bf9dbc88c。生命周期报告记录正常 CPU drain、未硬杀和自有 schema 已移除；主线程另检查数据库 namespace 与已记录进程/端口。临时数据库数据不可恢复，原始文件和只读批准包保留；主目录、业务 schema、.env、8000 未修改。

## 本地证据

下列路径相对工作区，均在被 Git 忽略的 artifacts 中，不将数据库、模型或原始大轨迹加入仓库。

| 文件 | SHA-256 |
| --- | --- |
| artifacts/narrow-equivalence-20261010.xml | 54fd01fd8ea526768f11e044767f4f0278042007c4a02ae4de2c6fbe3aa080b0 |
| artifacts/narrow-regression-20261010.xml | c3d2f09c34263be5a81ca2dc101907afa4a4d6c4d82ccaff08996c8676a8c8ab |
| artifacts/narrow-final-dev-20261010.xml | 1f30a6ffc379380441911c4c3a96521b7754d65d532c411811a2cc34686beb32 |
| artifacts/narrow-clean-20261010.xml | 642ad539089b3a8cf63ddc540d287b9e8c9bf1a9565c90f0797b54fd7b72e4ce |
| artifacts/diagnostics/stable-narrow-catalog-20261010/catalog-profile.json | 59fbeb14f90dc47c53921b4196c9fe82e02aae447a1a0e4ca8124abb55cb46b8 |
