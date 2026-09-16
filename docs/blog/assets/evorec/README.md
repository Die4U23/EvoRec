# EvoRec 博客图片资源

10 张图，每张同时提供 PNG 与可缩放 SVG。PNG 用于文章嵌图，SVG 用于放大与排版。

结果图直接复制各阶段程序生成的图表，来源 JSON、图片指纹和协议编号记录在 [清单](manifest.json)。两张机制图由项目代码生成，不包含模拟结果。

[离线图库](index.html) · [项目实录](../../evorec-project-log.md)

| 图片 | 用途与说明 | 格式 |
| --- | --- | --- |
| 内容双塔结构与实现边界 | 图中为已运行的离线结构；R03 训练，R04 冻结复用。在线业务接入仍是计划。 | [PNG](content-tower-architecture.png) / [SVG](content-tower-architecture.svg) |
| 冷商品保留与门控流程 | 五种策略在实验前固定。门控只读取历史信号；未跳过任一检索路径。 | [PNG](cold-reservation-flow.png) / [SVG](cold-reservation-flow.svg) |
| R04 冷商品落选位置分析 | 测试后诊断：比较同一批冷目标的命中位置；排序集合可能交叉，不是严格漏斗。 | [PNG](r04-error-localization.png) / [SVG](r04-error-localization.svg) |
| R02 序列模型训练曲线 | R02：39 轮、4 次训练。损失下降不保证验证指标持续改善。 | [PNG](r02-learning-curves.png) / [SVG](r02-learning-curves.svg) |
| R02 序列模型与统计基线 | R02 独立样本。三种子标准差不是置信区间；序列模型未超过强协同基线。 | [PNG](r02-test-comparison.png) / [SVG](r02-test-comparison.svg) |
| R03 内容双塔训练曲线 | R03：48 轮、4 次训练。按验证指标保留检查点；静态内容可用假设。 | [PNG](r03-learning-curves.png) / [SVG](r03-learning-curves.svg) |
| R03 整体排序与冷商品召回 | R03 独立样本。内容融合改善整体排序，但冷商品命中少于纯内容路径。 | [PNG](r03-test-comparison.png) / [SVG](r03-test-comparison.svg) |
| R04 验证集策略比较 | R04 冻结 R03 模型，使用新用户验证选择策略；红线为整体 NDCG 下限。 | [PNG](r04-validation.png) / [SVG](r04-validation.svg) |
| R04 测试集消融 | 固定 seed 17 的五种预登记策略；测试中的差异不用于回改选型。 | [PNG](r04-test-ablation.png) / [SVG](r04-test-ablation.svg) |
| R04 门控活动与冷商品曝光 | 改变列表的请求比例和新增冷商品位置数量，与目标命中率分别评价。 | [PNG](r04-gate-activity.png) / [SVG](r04-gate-activity.svg) |

复建：在项目根目录、已完成三轮结果报告后，使用研究环境运行 scripts/build_blog_assets.py。

R02/R03/R04 查询用户不同，不将跨轮绝对分数变化标为算法提升。R03/R04 依赖静态内容可用假设。
