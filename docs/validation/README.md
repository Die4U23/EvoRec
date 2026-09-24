# 验证证据索引

这里保存已执行检查的公开摘要与原始测试输出。建议先看 [当前状态](../STATUS.md) 确认阶段，再打开对应 JSON；XML 是测试框架输出，供追查用，不必作为阅读起点。检查记录只证明其中实际执行的范围，不代表线上性能、全新环境复建或所有规划功能已完成。

| 范围 | 首选记录 | 进一步检查 |
| --- | --- | --- |
| 仓库交付卫生 | [仓库检查](repository-hygiene-cleanup.json) | [测试输出](repository-hygiene-tests.xml)、[仓库政策](../08-repository-policy.md) |
| M0 框架和接口 | [框架检查](m0-framework.json)、[架构检查](architecture-checks.json) | [M0 测试](m0-tests.xml) |
| M11 PostgreSQL | [实库迁移](m11-database-checks.json)、[服务接口](m11-postgres-api-checks.json) | 环境条件见[运行说明](../../README.md) |
| M12 反馈 | [反馈检查](m12-feedback-checks.json) | 当前边界见[状态记录](../STATUS.md) |
| M21 bundle 校验与受控加载 | [bundle 校验](m21-bundle-checks.json)、[受控加载](m21-controlled-load-checks.json) | [加载测试](m21-controlled-load-tests.xml) |
| R01–R03 | [R01 检查](r01-checks.json)、[训练检查](training-checks.json)、[内容检查](content-checks.json) | [实验索引](../experiments/README.md) |
| R04–R05 | [门控检查](gating-checks.json)、[排序检查](ranker-checks.json)、[R05 复验](replication-artifacts-checks.json) | [实验索引](../experiments/README.md) |
| R06 | [产物审计](r06-artifacts-checks.json)、[仓库检查](r06-repository-checks.json) | [区间复算](r06-interval-repeat.json) |
| R02–R05 来源与干净复验 | [历史来源复原](r02-r05-provenance-reconstruction.json)、[干净复验汇总](r02-r05-clean-replications.json) | [R02 审计](r02-clean-replication-audit.json)、[R03 审计](r03-verified-clean-audit.json)、[R04 审计](r04-verified-clean-audit.json)、[R05 审计](r05-verified-clean-audit.json) |
| 发布前检查 | [发布预检](release-preflight.json) | 仍须按[验证与发布框架](../07-validation-release.md)核对未完成项 |

命名规则：`*-checks.json` 通常是一次阶段检查摘要；`*-tests.xml` 是对应测试运行的原始输出；`*-artifacts-checks.json` 与 `*-audit.json` 针对产物或轨迹。多个记录可能属于同一阶段的不同时间或不同环境，不应简单相加当作一个测试总数。需要复跑时先确认相应原始数据、模型和环境是否可用。
