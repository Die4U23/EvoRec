# R03 内容召回：运行与阅读说明

本阶段在已有序列推荐之外增加一条内容路径。商品即使没有出现在当前模型的训练交互中，只要内容能够被冻结编码器表示，且在请求时已经可用，就可以进入检索范围。

## 两个不同的学习过程

第一步用训练商品的标题和类别拟合 TF-IDF，再用 SVD 得到 128 维稠密向量。词表、IDF 和降维矩阵都只从训练商品学习；完整商品库使用同一编码器转换。缺失内容或没有已知词的商品仍然保留缺失状态。

第二步训练双塔 MLP。用户塔输入历史商品内容向量的时间顺序加权平均，商品塔输入候选商品内容向量。训练让正反馈商品与用户表示更接近。这个双塔没有商品 ID 嵌入，因此可以处理训练交互没有覆盖的新商品；它没有替代已实现的 SASRec-style 序列模型。

候选检索先屏蔽尚不可用、已经交互和无法表示的商品，再进行精确 Top-200 排序。无内容历史时回退到训练期近期热门。融合路径将协同候选与内容候选做加权倒数排名融合，权重通过验证集选择。

## 实际产物

| 内容 | 位置 |
| --- | --- |
| 固定配置 | [r03-content.json](configs/r03-content.json) |
| 实验假设和采样规则 | [R03 协议](../docs/experiments/r03-content-protocol.md) |
| 最新报告 | [图表页面](../docs/experiments/r03-content/report.html)、[文字报告](../docs/experiments/r03-content/report.md) |
| 数据准备 | src/evorec/research/content_data.py |
| 文本编码、双塔和检索 | src/evorec/research/content.py |
| 训练与选型 | src/evorec/research/train_content.py |
| 自动报告 | src/evorec/research/content_report.py |
| 独立审计 | scripts/audit_content.py |
| 新增依赖快照 | [requirements-content.lock.txt](requirements-content.lock.txt) |
| 实际环境检查 | [环境记录](environment-content-observed.json) |

训练目录 artifacts/runs/r03-content-20260915/ 保存冻结编码器、内容向量、最佳模型、源代码快照及逐请求轨迹。原始商品元数据和用户样本保存在 datasets/，不进入 Git。

## 可重复运行

在项目根目录执行。当前机器已经准备好输入；准备程序会拒绝覆盖完成的样本。首次执行需要官方交互文件及 R01 开发用户样本，详见 [研究入口](README.md)。

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.content_data --config research/configs/r03-content.json
```

如果只在元数据下载阶段中断，保留已生成的样本，并继续元数据处理：

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.content_data --config research/configs/r03-content.json --metadata-only
```

下载支持精确字节范围续传，每次恢复后都校验完整 gzip 和 SHA-256。若服务器不支持正确的字节范围，程序会报错，不会把错误页面追加为数据。

启动新的实验时必须选择新的输出目录：

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.train_content --config research/configs/r03-content.json --output artifacts/runs/my-content-run
```

每轮更新独立的 R03 报告目录；一个报告目录只允许一个实验写入。程序失败后保留已生成的文件，不会自动从中途训练状态恢复。重试训练应使用新目录；已有成功运行不要覆盖。

完成后独立审计：

```powershell
.\.venv-research\Scripts\python.exe scripts/audit_content.py --run artifacts/runs/r03-content-20260915 --output docs/validation/content-checks.json
```

## 如何解释结果

整体 NDCG 衡量前几位推荐质量；模型冷商品 Recall 检查新商品入口是否有效。两者可能朝不同方向变化，应同时查看。固定内容相似度可能更容易召回冷商品，监督训练则可能更偏向训练热门。

本轮元数据来自静态快照，没有历史版本时间戳，因此属于静态内容假设下的辅助实验。用户与 R01 / R02 不重合，但商品和日历区间仍有重合。不能把本轮分数与旧样本直接计算提升率，也不能据此宣称严格时间回放、线上 CTR 或生产性能。

三个种子分别选验证最佳检查点。均值表示三个独立模型指标的平均，样本标准差不是置信区间；本轮没有进行统计显著性检验。
