# R05 新排序模型配图

图片直接来自已完成报告，PNG 用于嵌入文章，SVG 用于放大。

[来源清单](manifest.json) · [实验报告](../../../../experiments/r05-ranker/report.md) · [项目实录](../../../evorec-project-log.md)

## R05 新排序模型训练曲线

4 次训练、35 轮。普通损失与冷样本加权损失的绝对值不直接比较；最佳轮次只看验证集。

![R05 新排序模型训练曲线](learning-curves.png)

[SVG](learning-curves.svg)

## R05 同候选池排序比较

ColdListMLP 只运行 seed 17；普通 ListMLP 有三个种子。R03 参考使用不同编码器与召回链路。

![R05 同候选池排序比较](test-comparison.png)

[SVG](test-comparison.svg)
