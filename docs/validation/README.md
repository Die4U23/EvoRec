# 验证证据索引

这里保存已执行检查的公开摘要与原始测试输出。建议先看 [当前状态](../STATUS.md) 确认阶段，再打开对应 JSON；XML 是测试框架输出，供追查用，不必作为阅读起点。检查记录只证明其中实际执行的范围，不代表线上性能、全新环境复建或所有规划功能已完成。

| 范围 | 首选记录 | 进一步检查 |
| --- | --- | --- |
| 推荐卡片商品信息 | [脚本负面测试与完整包浏览器核对](demo-card-metadata-20261005.json) | 真实标题补齐、迟到响应守卫；不生成图片、不重算推荐或自动反馈 |
| 独立 R06 演示入口 | [真实 TCP、浏览器观察与回归](r06-demo-entry-20261005.json) | 完整包 HTTP 闭环通过；浏览器重置未执行，不以其他检查替代；[启动说明](../product/demo-scope.md#独立-r06-推荐入口) |
| 完整 R06 请求期限修复 | [分段诊断与双环境 ASGI 复验](r06-request-deadline-20261005.json) | 原 2 秒期限不变；缓存固定摘要，不跳过实际 SQL 商品核对；HTTP/浏览器仍待验收 |
| 当前 Demo 与训练热门基线 | [分层验收与完整包超时](demo-baseline-20261005.json) | [范围出口](../product/demo-scope.md)；组件/回归通过不代表完整包服务通过 |
| 仓库交付卫生 | [仓库检查](repository-hygiene-cleanup.json) | [测试输出](repository-hygiene-tests.xml)、[仓库政策](../08-repository-policy.md) |
| M0 框架和接口 | [框架检查](m0-framework.json)、[架构检查](architecture-checks.json) | [M0 测试](m0-tests.xml) |
| M11 PostgreSQL | [实库迁移](m11-database-checks.json)、[服务接口](m11-postgres-api-checks.json) | 环境条件见[运行说明](../../README.md) |
| M12 反馈 | [反馈检查](m12-feedback-checks.json) | 当前边界见[状态记录](../STATUS.md) |
| M21 bundle 校验与受控加载 | [bundle 校验](m21-bundle-checks.json)、[受控加载](m21-controlled-load-checks.json) | [加载测试](m21-controlled-load-tests.xml) |
| R01–R03 | [R01 检查](r01-checks.json)、[训练检查](training-checks.json)、[内容检查](content-checks.json) | [实验索引](../experiments/README.md) |
| R04–R05 | [门控检查](gating-checks.json)、[排序检查](ranker-checks.json)、[R05 复验](replication-artifacts-checks.json) | [实验索引](../experiments/README.md) |
| R06 | [产物审计](r06-artifacts-checks.json)、[仓库检查](r06-repository-checks.json) | [区间复算](r06-interval-repeat.json) |
| R06 受控服务组件 | [冻结排序器](r06-ranker-component-20261003.json)、[冻结特征](r06-frozen-features-20261003.json)、[安全编码器](r06-safe-encoder-20261004.json)、[冻结召回](r06-controlled-retrieval-20261004.json) | 独立组件兼容性，不代表完整在线集成 |
| R06 可选召回加速 | [交错对照与数值验收](r06-retrieval-acceleration-20261004.json) | 热加载召回，不含排序/数据库/HTTP，不是线上 SLA |
| R06 请求快照适配 | [冻结子集与请求绑定](r06-snapshot-serving-20261004.json) | 同步组件，未接入数据库 admission、发布恢复或 API |
| R06 自包含冻结包 | [完整包与实际商品身份](r06-frozen-bundle-20261004.json) | 原字节组装和双环境重放，未切换活动模型或接入发布/API |
| R06 数据库准备 | [原子准备与实际表示重放](r06-catalog-preparation-20261004.json) | 真实全量包只在隔离 schema 验收；未接通的 R06 发布明确拒绝 |
| R06 异步排序与取消 | [有界队列与双环境实际批次](r06-async-ranking-20261004.json) | 运行线程须清退后确认取消；仍未接通数据库 admission 与在线发布 |
| R06 可选推荐服务 | [可信数据库快照、发布恢复与实际 API](r06-online-serving-20261005.json) | 默认关闭；完整包仅在隔离 schema 验收，单次延迟接近期限，不是生产 SLA |
| 普通推荐中断收敛 | [执行租约与客户端原键恢复](recommendation-crash-recovery-20261005.json) | 新协议重试时收敛失败，不自动续算；旧 NULL owner 的存活状态保持未知 |
| R02–R05 来源与干净复验 | [历史来源复原](r02-r05-provenance-reconstruction.json)、[干净复验汇总](r02-r05-clean-replications.json) | [R02 审计](r02-clean-replication-audit.json)、[R03 审计](r03-verified-clean-audit.json)、[R04 审计](r04-verified-clean-audit.json)、[R05 审计](r05-verified-clean-audit.json) |
| 发布前检查 | [发布预检](release-preflight.json) | 仍须按[验证与发布框架](../07-validation-release.md)核对未完成项 |

命名规则：`*-checks.json` 通常是一次阶段检查摘要；`*-tests.xml` 是对应测试运行的原始输出；`*-artifacts-checks.json` 与 `*-audit.json` 针对产物或轨迹。多个记录可能属于同一阶段的不同时间或不同环境，不应简单相加当作一个测试总数。需要复跑时先确认相应原始数据、模型和环境是否可用。
