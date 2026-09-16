# R05 冷加权复验运行指南

本阶段固定原 R05 设置，重放 seed 17 并补 seed 29 / 43。旧测试已查看，属于复验及事后统计补充。协议见 [登记文档](../docs/experiments/r05-cold-replication-protocol.md)。

## 版本与输入

协议登记提交为 91eb7d5；实现与结果分别提交到 codex/r05-cold-replication。实际训练记录自身代码 commit、源文件 SHA-256 与工作区状态。训练要求研究代码、配置及测试均已提交；无关用户文件不影响训练，但状态会如实保留。

依赖已有本地 R05 冻结运行 artifacts/runs/r05-ranker-20260916，及其来源 R03 运行、R01–R05 样本、商品目录和元数据。源运行 SHA-256 已写入配置。编码器、训练 / 验证 / 测试缓存与旧检查点逐一核验；旧运行不被覆盖。首次 clone 尚不能直接取得这些忽略产物，需按既有指南重建来源实验；当前配置绑定的是本机既有冻结运行，不宣称跨机器重放必定逐位一致。

研究环境沿用现有 Python / PyTorch / CUDA 环境。尚无干净环境复建证据。

## 执行顺序

在项目根目录，先完成实现提交，再运行：

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.replicate_ranker --output artifacts/runs/r05-cold-replication-20260916
.\.venv-research\Scripts\python.exe -m evorec.research.replication_analysis --run artifacts/runs/r05-cold-replication-20260916
```

第一步产生训练轨迹、检查点、验证 / 测试排名、运行摘要和实时报告；第二步重新计算排名指标、检查候选与时间合法性，并生成 48 项用户聚类区间。输出运行目录必须不存在，防止覆盖历史实验。

遇到 seed 17 权重或逐轮轨迹不一致时，程序拒绝标记复验通过。失败报告保留，应先诊断并提交修正，再用新运行目录执行；不通过修改历史证据强行匹配。

## 关键方法

- 每个种子只按全体验证 NDCG@10 选轮；所有检查点冻结后才重访旧测试。
- 与 RRF、CF-blend 同请求配对；用户重复出现的所有请求整体参与重采样。
- 保留请求加权平均。三种子的指标平均不是推荐集成。
- 10,000 次抽样，95% percentile 边际区间；固定检查点的不确定性与种子样本标准差分开。
- 区间不做多重比较校正，不能据个别区间宣称总体显著；跨用户商品和时间依赖仍未覆盖。
- 逐请求审计重算真实排名指标及候选合法性。原轨迹不含协同候选来源计数，故不伪造重建 mean_personalized_candidates / mean_popularity_fill。

专项测试使用 tests/test_uncertainty.py 与 tests/test_replication.py，覆盖不等用户请求数、成对列、零差值、无效输入、源文件漂移、权重变化、候选注入、排名指标篡改及查询 ID 对齐。
