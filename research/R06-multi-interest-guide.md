# R06 运行指南：同预算多兴趣召回与排序适配

R06 比较 A/B/C/D 四条路径，具体公式、选型和统计比较见[预登记协议](../docs/experiments/r06-multi-interest-protocol.md)。当前实现使用 R05 冻结编码器及冷加权检查点，新增近期多兴趣召回，保持每路 200、候选并集最多 400 的预算。

## 已完成运行

2026-09-17 完成 artifacts/runs/r06-multi-interest-20260917，绑定实现提交 bf73ce7b002d7fa9ca3c327dc291efea6b42c908。六次训练共 74 轮，验证选中 A-frozen-s17。冷目标入池 115 → 122 / 6,978，但固定排序器与匹配重训练的主要区间均跨过零。

[完整报告](../docs/experiments/r06-multi-interest/report.md) · [产物核对](../docs/validation/r06-artifacts-checks.json) · [区间复算](../docs/validation/r06-interval-repeat.json)。测试已查看；再次训练必须新输出目录，后续调参须新协议，不能把同一测试继续称为未见数据。

## 运行前

按研究环境说明准备 .venv-research，使用已有完整源文件及 R01–R05 数据清单。R05 两次运行必须完整且哈希匹配。新样本为用户桶 4；与旧桶交集非零会中止。所有模型、样本、来源轨迹和源码快照只写忽略目录。

正式训练入口要求源码、配置、检查脚本和测试已经提交；不要求把无关的 LICENSE 加入仓库。输出必须新建，拒绝覆盖历史运行。

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.run_multi_interest --config research/configs/r06-multi-interest.json --output artifacts/runs/my-r06-reproduction
```

运行依次扫描新用户样本、重建两条候选路径的训练样例、核对均值路径训练缓存与 R05 完全一致、构造验证候选、评估冻结基线、训练 D/C 各三个种子、验证重载、登记选型，最后才开放测试。全部进度写入运行目录 series.json。网络中断后先查看进程和该文件，避免重复运行；异常目录保留，未提供自动续训。

## 审计

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.r06_analysis --run artifacts/runs/r06-multi-interest-20260917
```

重新构造训练、验证、测试的全部候选来源与特征；重新推理模型，重算排名及分组指标；验证训练样例和选轮来源，再计算 50 项预登记用户聚类区间。原始轨迹不公开，公开汇总结果、图表及检查证据。

A/B 冻结模型使用旧 R05 验证选轮，C/D 使用 R06 验证选轮。B-A 检验固定排序器下召回的影响，C-D 是两边都重训练的同阶段对照。C-B 还包含检查点重新选轮的影响，不能全归因于新候选。

## 重建报告与交付检查

从已完成并审计通过的运行生成汇总，不重新训练：

```powershell
.\.venv-research\Scripts\python.exe scripts/build_r06_report.py --run artifacts/runs/r06-multi-interest-20260917
.\.venv-research\Scripts\python.exe scripts/verify_r06_intervals.py --run artifacts/runs/r06-multi-interest-20260917
.\.venv-research\Scripts\python.exe scripts/check_r06_artifacts.py --run artifacts/runs/r06-multi-interest-20260917
```

独立区间复算会再次读取本地排名轨迹并执行 50 × 10,000 次用户抽样。交付检查绑定这次固定运行的计数、原提交、源码快照、报告、图表清单和测试记录；它是本轮验收工具，不是任意新实验的通用判定器。目前要求位于 codex/r06-multi-interest 分支。

报告位于 docs/experiments/r06-multi-interest；新复现实验若需生成独立报告，应在其登记配置中使用独立 report_directory，避免覆盖本次交付页面。原始运行归档拒绝以不同内容覆盖。

## 文件卫生与版本

[仓库约定](../docs/08-repository-policy.md)定义公开范围；博客和实习材料不提交。每个运行绑定代码 commit，结果交付独立提交。R06 当前依赖 R05 分支；PR 的基底先设为 codex/r05-cold-replication。协议、实现、工程建议评审与结果分别提交；保留训练原提交以便追溯，不补造开发日期。

专项测试位于 tests/test_multi_interest.py，覆盖独立逐点计算、目标标签隔离、严格时间过滤、缺失历史、同分顺序、预算、选型和封闭测试门槛。tests/test_repository_hygiene.py 验证暂存区检查不会依赖被忽略的本地文件。
