# 流式窄投影：目录成本降低，HTTP 稳定门禁仍失败

按[本轮计划](stable-stream-digest-plan-20261010.md)只改实际目录 SQL 的投影边界；完整目录门禁通过，但未插桩双并发初筛 **22/24 成功、2 次 504**。随后固定阶段诊断 10/10 成功，没有捕获失败内部阶段。本轮不执行 A/B/B/A、两轮 120 笔、浏览器/恢复/备份或稳定冻结；不自动合并。

## 源码与方法

基线是 `6a2f2290bfb79a013c4439ccbc987d3b890dc300`（草稿 PR #58），候选与本地预登记冻结于干净 `bf075029ffcef4882db509821e96690c2d4b285a`，全部正式观察首尾 HEAD 和 src/scripts/正式迁移字节摘要不变。基线 SQL SHA-256=`98b626558bda6700e0e6945f5ce550cad988687a68c17268efc648319542fccf`，候选 SQL=`2990ce4ead90c2c54ba331a19ff653416a449c20954b7ac148094d704ea6d211`。基线只读取 Git 字面 SQL，不运行历史代码。

协议与实现先在本地 Git 冻结，但首次推送发生在观察结束后；这没有遵守[仓库约定](../08-repository-policy.md)的远端先登记顺序，应保留为流程偏差，而非声称远端预注册。未训练新模型，所有门禁在观察前的本地提交中固定，未在看到结果后修改、扩样或重跑批次；Git 可还原干净候选，但不能补造先前的远端时间戳。

批准包 UUID=`bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，137,249 商品，manifest=`9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，model version=`eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。Windows、PostgreSQL 18.6、psycopg 3.3.6、NumPy；work_mem=4MB、plan_cache_mode=auto、jit=on、prepare_threshold=5。未修改期限、单 worker、连接/恢复、GC、持久 seal 或资源清退。

保留实际 N+1 成员物化及数量门禁；source_rows 只隔离列作用域，窄投影用 CASE 计算上架项实际 UTF-8 文本哈希与原时间后缀，OFFSET 0 保留投影边界，不新增帧物化。聚合内部的索引顺序仍显式指定，NULL 标志与所有下架成员仍校验。计划组织依赖优化器，不是所有版本的永久性能保证。[优化器源码](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/optimizer/prep/prepjointree.c)、[聚合顺序](https://www.postgresql.org/docs/18/functions-aggregate.html)、[CASE 边界](https://www.postgresql.org/docs/18/functions-conditional.html)

## 功能与测试机制

| 检查 | 项数 | 失败 / 错误 / 跳过 | 秒 |
| --- | --- | --- | --- |
| 上轮首个内存试验 | 30 | 1 / 0 / 0 | 51.801 |
| 上轮修订内存试验 | 30 | 0 / 0 / 0 | 48.985 |
| 本轮源码候选开发检查 | 54 | 0 / 0 / 0 | 48.048 |
| 干净 bf07502 七模块复测 | 151 | 0 / 0 / 0 | 57.676 |
| 归档报告后的 helper/架构/卫生复测 | 38 | 0 / 0 / 0 | 0.387 |

范围重叠，不累加。第一项是并发注入在移到 join 投影后产生 item_id 列歧义，独立复现 AmbiguousColumn/42702，不是实际并发一致性通过；修订作用域后原测试通过。内存试验不是 Git 冻结候选验收。新增 gate 等待控制：捕获线程异常会立即抛原异常，提前完成也失败，不再将 SQL 错误隐藏为十秒未到达锁。

新增实库混合上/下架函数只对指定项抛异常：下架时 SQL 能校验其余资格，重新上架后同一 SQL 确实抛异常。原成员增删/同数量替换、索引、文本/时间/NULL、全部下架/恢复、资格与持久 seal、SQL 执行中修改反例均保留。主线程分别复核质量、性能、复用；team-mode 缺少要求的角色参数，串行完成，不声称独立代理审查。复用现有测量与隔离生命周期，无新生产配置或公共 API。

## 完整目录：固定八笔与六份另采计划

不开等待或 fresh 计划观察；同一自有目录/连接，全上架和三项下架各基线/候选/候选/基线。程序严格比较完整 CatalogCapture，资格分别 137,249 / 137,246，均只返回一行；独立校验源与模型身份、八笔顺序、计数、错误、六份计划及预定数值门禁。

| 状态 / 实现 | 两次 execute ms | 两次资格校验 ms |
| --- | --- | --- |
| 全上架 / 基线 | 685.05 / 2,036.53 | 0.010 / 0.010 |
| 全上架 / 候选 | 591.35 / 578.28 | 0.009 / 0.008 |
| 三项下架 / 基线 | 2,554.02 / 2,514.88 | 126.71 / 126.39 |
| 三项下架 / 候选 | 2,379.53 / 2,363.45 | 136.69 / 126.37 |

fetch/decode 为 0.008–0.024 ms。基线本身存在早快后慢；有限交错不是严格因果或线上提速证明。两种状态均满足候选上界/中位数不劣于当轮基线的继续门禁，但下架 SQL 仍超过 2 秒。execute 是驱动调用墙钟，不是纯 SQL，也不等于锁等待。启动 CPU 快照为 2026-10-10T21:07:09+08:00 的 5%、12 逻辑核，不是连续负载隔离；完整包准备不计入八笔 SQL。

| 状态 / 实现 | 另采 Execution Time ms | 排序 kB | 根 Temp Read / Write Blocks |
| --- | --- | --- | --- |
| 全上架 / 基线 | 2,715.564 | 24,752 | 7,877 / 7,887 |
| 全上架 / 候选 | 2,475.402 | 12,904 | 6,396 / 6,403 |
| 三项下架 / 基线 | 2,639.810 | 24,752 | 7,877 / 7,887 |
| 三项下架 / 候选 | 2,498.604 | 12,904 | 6,396 / 6,403 |
| 越界一项 / 基线 | 135.097 | 25 | 0 / 569 |
| 越界一项 / 候选 | 127.287 | 25 | 0 / 569 |

正常计划均扫描 137,249 items，external merge/Disk 排序。候选保留 Subquery Scan 投影，没有额外 Materialize 节点；排序文件降低约 47.9%、根临时写入降低约 18.8%，是本批实测成本，不是 HTTP 提速百分比。连接哈希端两者仍为 4 批、Peak Memory Usage=6,948 kB、临时写块 2,277，未被此次投影消除。父子块计数包含重叠，不相加；TIMING OFF 不能给节点分配精确时间。越界读取实际 N+1=137,250，items Actual Rows/Loops=0，捕获均拒绝 bundle_members_changed。

## HTTP：保留两次失败

独立 schema/服务进程；两笔顺序参考均 200。随后 24 笔双并发，原 2 秒期限、单 worker、同一 sample session/不同新键、dense/k=10、每笔新建 HTTPX client，服务器插桩、GC 观察与 phase gate 均关闭；22 次 200、2 次 504，无传输错误，所有成功响应身份合法。脚本退出 0 只表示诊断完整，独立验收明确判为 failed_2_of_24。

失败样本 3 / 7 的 HTTP 往返为 2,312.77 / 2,175.57 ms，客户端创建为 1,001.27 / 957.27 ms。全部负载的客户端创建 p50/p95=945.14/1,001.27 ms，HTTP 往返 p50/p95=1,620.39/2,175.57 ms，总耗时 p50/p95=2,575.92/3,133.04 ms；这些分位数不相加，客户端总耗时不当作服务端耗时。负载墙钟 32.194 s，不外推目标 QPS/SLA。正确启动前 CPU 快照为 21:12:19+08:00 的 3%，不是持续负载证明。

启动前曾误用 --no-trace，argparse 在创建目录或发请求前拒绝；确认目录不存在后改为 --untraced，不属于请求重试。目录独立核验首次误读停止文件 normal_cpu_drain 字段，正确字段为 cpu_jobs_drained；错误保留，修正后重新核验原始报告，而非重跑测量。后续 shell 显式检查核验退出码，避免后续命令覆盖失败。

## 固定阶段诊断没有解释失败

按计划仅补一批 2 顺序 + 8 双并发，trace 开启、GC/phase gate 关闭；10/10 为 200，客户端/服务端样本一一对应、共同时间轴有效，10 个成功请求的必要数据库阶段均完整，源和模型仍为同一 bf07502。ASGI 墙钟 1,108.51–1,493.19 ms；目录总阶段 643.12–743.97 ms，其中 execute 606.43–704.84 ms；召回/排名阶段 157.42–286.47 ms，包含排队与调度，不是纯 CPU。嵌套阶段不相加，插桩有观察成本，不将不同批次差值当提速或失败原因。

此前两次 504 的内部阶段、期限触发与取消/租约清退时间线仍未知。该诊断没有失败，不能把成功轨迹倒推到它们，也不追加样本直到复现。下一轮优先固定与初筛同大小/输入的失败时间线诊断口径；在捕获失败前不按猜测修改 worker、恢复锁或连接设置。Hash Join 临时写入是明确的成本候选，不是已证明的 504 根因。

## 清理与交付边界

三批自有 run/schema 均记录正常 CPU drain、无硬杀；逐个 schema 另查已不存在，自有服务进程/监听均确认退出，剩余 test_evorec_ namespace=0。未插桩目录批次没有记录数据库 PID，因此不声称逐个数据库后端释放已独立测得。

- 目录：run 04f0b238-81c0-43c6-84b4-6ad02e98baff，schema test_evorec_cc08ece62e2049fcb8247c060c65175b，PID 33092 / port 56663。
- HTTP：run 60b860e4-4180-4096-8953-b68de22bee54，schema test_evorec_cbfdbb573ad0431b86e1126e8f11470d，PID 26016 / port 60464。
- 阶段：run 53f53fba-96f2-40f1-a718-8784bb51f9ae，schema test_evorec_2bb22e215e1c48bda5362adf5fb52d19，PID 33332 / port 49700。

只删除自有临时数据库数据，页面无法恢复；原始文件与只读模型保留。主目录保持干净，.env SHA-256=`32f05a41febf54595c5cef010ecee661c47570ac9e0a41238501b9297939e267` 未变，不操作业务 schema 或 8000。候选保留在草稿分支，不将性能局部收益包装为稳定版修复。归档只改文档，精确最终 head 的 CI 在 PR 另行核对。

## 本地原始证据

均在被 Git 忽略的 artifacts，不提交数据库、模型或原始大采样。

| 文件 | SHA-256 |
| --- | --- |
| stream-explore-20261010.xml | 528f1b39d4b180a8b95f8f8a3692422ea4f066b5932ecc134ffd68997a74391d |
| stream-explore2-20261010.xml | 902a909ba3aad06d79f4031f6505f37b75ba432ddda4b9e5603dea81bb8e782a |
| stream-dev-20261010.xml | 0fb084de7e869a1d75b2a2af166e664dc4ef98b008b5cd1a7c70eea476b09ba7 |
| stream-clean-20261010.xml | adbe5edf3e35df32cab00732a73c9eaf0a16151ab37b3e66e67cd73e7b060593 |
| stream-report-20261010.xml | c338ab532afff1192c0411380316d1ad3c21971a5567f079288d253d3f6e8357 |
| diagnostics/stable-stream-catalog-20261010/catalog-profile.json | 4624b16ce6c844b2130b5eb82f20b835cf05a81c874986b54c4fc1ae923e1b93 |
| diagnostics/stable-stream-catalog-20261010/catalog-observations.json | 2601d69c05642ed9cba30e7f93127676814b928d31157617f09fa7d53f9bf8d6 |
| diagnostics/stable-stream-http-20261010/profile.json | 2d4a21c21c174ba4f2e3b49f525c3295ddd5d4d043c2ca7b0881dd70d5fdb1f0 |
| diagnostics/stable-stream-http-20261010/observations.json | 85f9881db790b909e2c829c056ee6646ac9a7ef11240e75bf3b43c68c4e0ccc1 |
| diagnostics/stable-stream-phases-20261010/profile.json | d7ef309f7ebf89aeaa0d52a66411de4c0c363f944f40c9d6f14ef0298c2c11da |
| diagnostics/stable-stream-phases-20261010/observations.json | 26e4f93cec568d0f8a588b885ed5e8afab7f37b1edfb533681a504434db3fadc |
