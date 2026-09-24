# 文档导航

不必按文件名从 `01` 读到 `10`。先看下面四个入口，再按问题查专题；`docs/experiments/` 与 `docs/validation/` 保存可追溯证据，不是连续阅读的教程。

| 要做什么 | 首选入口 | 说明 |
| --- | --- | --- |
| 理解项目整体 | [架构与实现过程](10-architecture-and-implementation.md) | 从数据、模型到服务的主线；区分已实现和待实现。 |
| 看当前完成度 | [状态与验证记录](STATUS.md) | 按时间记录增量，不能把早期阶段描述当成最新状态。 |
| 运行或复现实验 | [研究运行说明](../research/README.md) → [实验索引](experiments/README.md) | 先看数据与环境要求，再选具体阶段。 |
| 查某项结论的原始核验 | [验证证据索引](validation/README.md) | JSON 摘要、测试 XML 和独立审计按主题归类。 |

## 按主题查阅

| 主题 | 文档 | 使用边界 |
| --- | --- | --- |
| 当前服务与模块设计 | [系统架构](02-architecture.md)、[架构决策](architecture/decisions.md) | 对照 [状态记录](STATUS.md) 判断哪些仍是设计。 |
| 数据、接口与数据库 | [数据设计](03-data-design.md)、[接口约定](04-api-contract.md)、[机器可读契约](contracts/README.md)、[正式数据库迁移](../db/migrations/0001_m1_core.sql) | `db/schema.design.sql` 是早期草案，不是迁移脚本。 |
| 研究协议与实验结果 | [研究路线草案](05-research-protocol.md)、[实验索引](experiments/README.md) | 各轮冻结协议和报告优先于早期总路线。 |
| 交付与仓库规则 | [仓库政策](08-repository-policy.md)、[验证与发布框架](07-validation-release.md) | 发布框架含未完成验收项，不代表已经上线。 |
| 产品原始材料 | [产品文档索引](product/README.md) | PRD 与选型依据，不代表当前实现状态。 |

## 历史计划与专题记录

[项目框架](01-project-framework.md)、[交付计划](06-delivery-plan.md)记录早期路线与编号，部分阶段命名或“待实现”描述已被后续实现超越；需要当前结论时以 [STATUS](STATUS.md) 和代码为准。[工程建议评审](09-engineering-follow-up.md)记录特定日期的取舍，不是新的总架构。保留这些材料是为了追溯决策，不建议将它们与当前状态并排阅读。

数据、模型、逐请求轨迹及本地环境位于被 Git 忽略的目录。仓库内的实验归档、图表和审计摘要是公开证据，不等于包含可直接重跑的全部原始产物。
