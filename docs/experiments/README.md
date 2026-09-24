# 实验索引

按阶段先读协议，再读报告；`archive/` 是当时运行系列的公开归档，`results.json` 与图表是报告配套材料，不是另一轮独立实验。完整本地运行产物与原始数据不在 Git 中。运行准备见[研究说明](../../research/README.md)，研究结论总览见[仓库首页](../../README.md)。

| 阶段 | 冻结协议或前提 | 优先阅读 | 补充证据 |
| --- | --- | --- | --- |
| R01 可行性 | [研究总路线](../05-research-protocol.md) | [可行性报告](r01-feasibility-report.md) | [结果](r01-video-games-20260915.json) |
| R02 序列基线 | [R02/R03 时间评价协议](r02-protocol.md) | [训练报告](training-report.md) | [原运行归档](archive/r02-training-20260915.json) |
| R03 内容路径 | [内容协议](r03-content-protocol.md) | [内容报告](r03-content/report.md) | [原运行归档](archive/r03-content-20260915.json) |
| R04 规则门控 | [门控协议](r04-gating-protocol.md) | [门控报告](r04-gating/report.md) | [失败定位](r04-gating/error-analysis.md) |
| R05 排序器 | [排序协议](r05-ranker-protocol.md) | [排序报告](r05-ranker/report.md) | [原运行归档](archive/r05-ranker-20260916.json) |
| R05 三种子复验 | [复验协议](r05-cold-replication-protocol.md) | [复验报告](r05-cold-replication/report.md) | [运行指南](../../research/R05-replication-guide.md) |
| R06 多兴趣召回 | [多兴趣协议](r06-multi-interest-protocol.md) | [最终报告](r06-multi-interest/report.md) | [运行指南](../../research/R06-multi-interest-guide.md) |

## 来源修复与跨阶段复验

R02–R05 原运行的工作区曾标记为脏；[来源复原清单](../../research/provenance/r02-r05-source-reconstruction.json)保存精确源码恢复方式，[独立复原检查](../validation/r02-r05-provenance-reconstruction.json)给出核验结果。之后完成的[干净工作区复验](r02-r05-clean-replication.md)是新的运行，不改写原记录。其 [汇总与审计](../validation/r02-r05-clean-replications.json)可从验证索引继续查阅。

HTML 报告是 Markdown 报告的可视化阅读版本，图表位于各阶段 `figures/`。这些文件与上述报告配套保留，避免将同一阶段误读为多份不同结论。
