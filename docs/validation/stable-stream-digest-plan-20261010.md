# 流式窄投影候选：预登记

本轮基于干净 `6a2f2290bfb79a013c4439ccbc987d3b890dc300`（草稿 PR #58），只改实际目录 SQL 的投影与排序输入。原 2 秒期限、单排名 worker、连接/恢复、GC、实际成员与内容校验、持久 canonical seal、取消清退、完整批准模型和业务环境不变。上一轮额外 MATERIALIZED 帧增加临时写入，已否决；本轮不重复该设计。

## 功能与源码门禁

保留实际 N+1 成员物化和数量门禁。普通 source_rows 仅隔离列作用域；窄投影用 CASE 只为上架项计算实际 UTF-8 文本 SHA-256 与原始 int8 时间后缀，OFFSET 0 保留投影边界，不额外物化帧。聚合内部 ORDER BY internal_item_id 继续显式存在，不依赖子查询的隐含顺序。NULL 标志和所有下架成员都保留；不读取可修改的存储摘要或预先过滤批准 ID。

该优化依赖具体优化器组织，不承诺所有版本都有同一计划；PostgreSQL 18 的 is_simple_subquery 拒绝提升带 limitOffset 的子查询，仍需完整数据的实际执行计划验证。[优化器源码](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/optimizer/prep/prepjointree.c)、[聚合顺序](https://www.postgresql.org/docs/18/functions-aggregate.html)、[CASE 边界](https://www.postgresql.org/docs/18/functions-conditional.html)。

上轮仅在内存试 SQL：首轮 30 项有 1 项并发注入失败，已独立复现 AmbiguousColumn/42702；调整 source_rows 作用域后 30 项通过。原 XML 均保留，不算 Git 冻结候选的验收。本轮补强 gate 等待机制：线程已经异常/提前结束时立即报告，而非伪装为未到达锁；新增混合上/下架会抛异常函数及同 SQL 重新上架反例。现有独立旧行谓词、成员/索引/内容/NULL、资格与 seal、SQL 期间并发修改测试继续保留。

先运行目标检查，主线程分别复核质量、性能、复用，修复后冻结中文提交，在干净同一 HEAD 上复验。当前 team-mode 缺少角色参数，串行完成，不冒充独立代理审查。不提交 .env、模型、数据库或被忽略的原始大证据。

## 完整目录对照：固定一次

批准包 `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，137,249 项，manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，只读目录 E:/codex/proj/artifacts/bundles/r06-frozen-20261004-a，NumPy。基线为精确 `6a2f2290bfb79a013c4439ccbc987d3b890dc300` 字面 SQL，不导入历史代码。

复用既有目录工具，独立 schema/随机端口，停止空闲 API 后在同一连接执行全上架与 3 项下架，各固定基线/候选/候选/基线。不开等待或 fresh 计划插桩；分别记录 execute、fetch/decode、资格校验和完整摘要相等。之后另采两种正常状态和 N+1 越界的六份 EXPLAIN ANALYZE/BUFFERS/TIMING OFF，观察成本单列，不混入八笔调用或 HTTP。记录扫描/循环、排序和根临时块，不相加父子节点，不将估计宽度当实测字节。

每种正常状态候选两笔 execute 均不得高于该状态两笔基线的最大值，且候选中位数不得高于基线中位数；目标排序负担必须降低，根临时写入不得增加，越界须跳过 items/文本分支。任一不满足就保留所有结果并停止本轮 HTTP，撤回生产 SQL、保留候选 Git 对象和反例，不调整 work_mem、准备模式或期限重测追全绿。有限交错样本不能证明严格因果或稳定 SLA；目录门禁只决定是否值得继续初筛。

## 条件 HTTP 验收

功能与目录门禁均通过，才执行原口径两笔顺序预热 + 24 笔双并发初筛，完整包、dense/k=10、同一 session/不同新键、每笔新建 HTTPX client、关闭服务插桩和 GC 观察、原 2 秒期限、单 worker，不重试、不扩样。所有请求身份与清退独立断言，不只看退出码。

若初筛失败，固定补一批 2 笔顺序 + 8 笔双并发阶段诊断，保留失败和未知，不以成功轨迹倒推失败。若初筛全过，本轮只登记下一轮独立 schema/进程的 A/B/B/A 对照，不在本轮临时追加；旧失败仍未关闭，最终两轮 120 笔、恢复/浏览器/备份和稳定冻结依然独立待验收。交付草稿 PR 并检查精确最终 head CI，不自动合并。
