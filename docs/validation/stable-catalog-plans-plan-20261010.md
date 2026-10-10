# 同 SQL 耗时漂移：计划与统计状态观察预登记

上一轮固定八笔目录观察仍有约 0.61→2.42 秒漂移，采到临时文件读写，CPU unknown，旧 504 未关闭。本轮不调整生产 SQL、目录/持久 seal 校验、2 秒期限、单排名 worker、恢复/连接、GC、数据库参数或业务服务。

## 实现和反例门禁

增加默认关闭的 --observe-plans，只在自有 test_evorec_ 随机 schema、live autocommit 连接上使用。每笔正常调用前后各观察同一连接的目标 prepared statement custom/generic 选择计数、两张目录表的 pg_class 估计和 ANALYZE 统计、以及同 SQL/参数的 fresh EXPLAIN (FORMAT JSON)。全部元数据调用 prepare=False；不执行 EXPLAIN EXECUTE，避免额外改变目标 prepared 计数。[prepared 计数说明](https://www.postgresql.org/docs/18/view-pg-prepared-statements.html)、[计划选择与重规划](https://www.postgresql.org/docs/18/sql-prepare.html)

fresh EXPLAIN 是单独生成的未执行计划，不是测量语句的实际或缓存计划，不能将它直接当作 prepared 执行证据。统计可能滞后；前后元数据和测量语句不是原子视图。不以 unchanged last_autoanalyze 排除瞬时统计修改。[统计刷新说明](https://www.postgresql.org/docs/18/monitoring-stats.html)

输出仅白名单结构/估计及其摘要、设置、两张自有表统计、prepared 名和计数，不保存语句文本、参数、Output/Filter/Index Cond 等表达式、DSN 或凭据。树大小/深度有限制。实库正反控制证明非 ANALYZE 不调用会抛异常的函数、不改变 prepared 选择计数；强制 custom/generic 仅用于自有小型测试控制，完整模型观察不设置它们。验证 ANALYZE 后估计更新、未准备状态、跨 schema 拒绝、非法/过大计划拒绝及默认关闭边界。

## 一次完整包固定观察

干净提交上运行一次，baseline=`80c8e7709ae000da238dce96b23c1b06f117d191`，生产 SQL 相同。完整批准包 UUID=`bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`、137,249 项、manifest=`9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`。隔离 schema/服务，发布后正常停止空闲 API。

全部上架和三项下架各 baseline/new/new/baseline 共 8 笔，不重试、不扩样；两标签仅重复同 SQL，不称优化 A/B。每笔前后共 16 组快照，实际目录捕获摘要/资格必须一致；沿用 --observe-waits 有界采样。另采原工具的六份 ANALYZE 执行计划，不混入正常调用计时。元数据与采样开销明示，不与旧未插桩时长当作提速对比。源码/model/SQL 身份、所有失败、不完整状态和清退绑定留档。

本轮不运行 HTTP/最终两轮 120 笔。若观察显示计划或统计变化，只能据此选择后续一项受控对照，不能立即归因旧 504；若没有变化，保留未知，优先做实际计划或计算成本诊断。不修改多个变量或扩样追全绿。
