# R06 运行指南：同预算多兴趣召回与排序适配

R06 比较 A/B/C/D 四条路径，具体公式、选型和统计比较见[预登记协议](../docs/experiments/r06-multi-interest-protocol.md)。当前实现使用 R05 冻结编码器及冷加权检查点，新增近期多兴趣召回，保持每路 200、候选并集最多 400 的预算。

## 运行前

按研究环境说明准备 .venv-research，使用已有完整源文件及 R01–R05 数据清单。R05 两次运行必须完整且哈希匹配。新样本为用户桶 4；与旧桶交集非零会中止。所有模型、样本、来源轨迹和源码快照只写忽略目录。

正式训练入口要求源码、配置、检查脚本和测试已经提交；不要求把无关的 LICENSE 加入仓库。输出必须新建，拒绝覆盖历史运行。

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.run_multi_interest --config research/configs/r06-multi-interest.json --output artifacts/runs/r06-multi-interest-20260917
```

运行依次扫描新用户样本、重建两条候选路径的训练样例、核对均值路径训练缓存与 R05 完全一致、构造验证候选、评估冻结基线、训练 D/C 各三个种子、验证重载、登记选型，最后才开放测试。全部进度写入运行目录 series.json。网络中断后先查看进程和该文件，避免重复运行；异常目录保留，未提供自动续训。

## 审计

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.r06_analysis --run artifacts/runs/r06-multi-interest-20260917
```

重新构造训练、验证、测试的全部候选来源与特征；重新推理模型，重算排名及分组指标；验证训练样例和选轮来源，再计算 50 项预登记用户聚类区间。原始轨迹不公开，公开汇总结果、图表及检查证据。

A/B 冻结模型使用旧 R05 验证选轮，C/D 使用 R06 验证选轮。B-A 检验固定排序器下召回的影响，C-D 是两边都重训练的同阶段对照。C-B 还包含检查点重新选轮的影响，不能全归因于新候选。

## 文件卫生与版本

[仓库约定](../docs/08-repository-policy.md)定义公开范围；博客和实习材料不提交。每个运行绑定代码 commit，结果交付独立提交。R06 当前依赖 R05 分支；PR 的基底先设为 codex/r05-cold-replication。正式结果出来前不预先声明收益。

专项测试位于 tests/test_multi_interest.py，覆盖独立逐点计算、目标标签隔离、严格时间过滤、缺失历史、同分顺序、预算、选型和封闭测试门槛。tests/test_repository_hygiene.py 验证暂存区检查不会依赖被忽略的本地文件。
