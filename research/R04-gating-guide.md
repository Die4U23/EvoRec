# R04 运行与原理：冻结模型的冷商品策略实验

R04 已完成，最终按验证集保留原融合策略。它检验增加冷商品位置与历史门控是否有收益；没有重新训练双塔，也没有实现学习型路由器。

## 先看结果

- [正式报告与完整指标](../docs/experiments/r04-gating/report.md)
- [展示页面](../docs/experiments/r04-gating/report.html)
- [测试后失败定位](../docs/experiments/r04-gating/error-analysis.md)
- [协议（查看结果前登记）](../docs/experiments/r04-gating-protocol.md)
- [独立审计](../docs/validation/gating-checks.json)

验证 11,460 个正反馈事件，其中模型冷且可用 4,047 个。五种策略均命中 7 个冷目标，按整体 NDCG 下限与同分规则选择 fixed。测试 12,797 个事件、6,996 个冷目标；固定融合三种子 NDCG@10=0.009745 ± 0.000297。种子标准差不是置信区间。

## 为什么固定训练数据

新查询样本使用用户哈希余数 2，与前几轮用户不重合。请求历史来自这批新用户，但文本编码器、双塔参数、协同统计及“模型是否见过某商品”的集合全部来自 R03。

若改用 R04 早期交互定义模型冷，标签就不再反映被冻结模型实际见过什么。FrozenPolicyProtocol 保留新样本事件用于时间历史，同时绑定 R03 训练数据。检查点加载时使用原 R03 协议及精确内容向量指纹。

模型冷包括“未出现在训练期任意评分记录中”；热门及协同统计仅使用训练正反馈。标题与类别采用静态快照假设，首次完整类别交互时间仅作商品可用性代理。

## 规则与选择

原融合内容权重 0.5，倒数排名常数 60。内容候选采用固定 SVD 相似度排序，候选已过滤请求时不可用商品和已见商品。

- fixed：保持原 Top200。
- reserve-1 / reserve-2：检查 Top20 冷商品数量，不足时从内容候选补入；已有冷商品受保护。
- gated-1 / gated-2：额外要求可表示历史至少 2 件，一致性至少 0.6。

一致性是按 0.8 衰减的单位内容向量加权平均的模长。一个保留位置放在第 20 位，两个位置放在第 10、20 位。内容候选不足时不伪造填充。

验证 seed 17 整体 NDCG 必须至少达到 fixed 的 97%；在合格策略中最大化冷 Recall@20，同分再看整体 NDCG、较少保留位置和声明顺序。选型落盘后才开启测试。规则接口只接收历史信号与候选，不接收目标标签。

## 当前本机重建报告与配图

以下在项目根目录运行，复用已完成的本地数据和模型：

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.gating_report --run artifacts/runs/r04-gating-20260915
.\.venv-research\Scripts\python.exe scripts/audit_gating.py --run artifacts/runs/r04-gating-20260915 --output docs/validation/gating-checks.json
.\.venv-research\Scripts\python.exe scripts/analyze_gating_errors.py
```

失败分析脚本目前固定读取本次 R04 运行；它使用测试标签诊断，不能用于训练或验证选型。实验图表随结果报告生成，来源为相应的结果 JSON；博客发布资源另在本地维护。

## 重新执行模型评估

已有完整运行保存在 artifacts/runs/r04-gating-20260915，禁止覆盖。重跑需要冻结的 R03 编码器、三个检查点、数据与清单，输出路径必须全新：

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.run_gating --config research/configs/r04-gating.json --output artifacts/runs/my-r04-replay
```

复跑相同数据只验证重现性，不算新的未见实验。如果需要独立报告目录，先复制配置并修改 report_directory；不要覆盖已经归档的结果。修改模型、样本或阈值需要登记新协议，不能继续把 R04 测试作为未见测试。

run_gating.py 会验证冻结依赖代码、编码器与模型指纹，保留源代码快照、配置、排名和门控轨迹。报告生成器单独记录自己的哈希，不改写训练或评估快照。

## 检查与边界

本轮服务环境 96 项通过、2 个神经模块跳过；研究环境 19 项通过，其中 8 项门控测试重叠。服务环境负责 API / 数据检查，研究环境负责 Torch 模型；本机研究环境没有 FastAPI，不能直接用它收集整套服务测试。

独立审计重建验证和测试历史、五类分组指标、覆盖率、候选合法性、历史一致性、准确保留顺序、选型下限和三种子统计。共核对 196,730 条排名与 146,879 条门控记录。

当前研究环境复用系统包，未验证干净安装或 Linux 重建。两个候选路径全部计算，离线批量性能不能作为在线延迟或预算路由节省。公开推荐服务、数据库发布与前端仍待接入。
