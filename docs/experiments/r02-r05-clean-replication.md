# R02–R05 干净工作区复验（2026-09-24）

## 结论

旧运行的 `working_tree_dirty=true` 不作改写。[历史来源复原](../validation/r02-r05-provenance-reconstruction.json)解决了当时源码、配置字节不可由单一基础提交完整恢复的问题；本次另从已提交的 `7186c874cfa9a6a7c4129704afb75e54fbee76a2` 启动四个新运行，四轮 `working_tree_dirty=false`、`experiment_paths_clean=true`。独立审计均通过，与原运行相比，冻结配置、验证集选择、种子汇总和合计 38 个测试方法的质量及覆盖指标一致；R02、R03、R05 合计重训 122 个 epoch。可复验摘要及运行系列 SHA-256 见[核验记录](../validation/r02-r05-clean-replications.json)。

| 阶段 | 新运行目录（`artifacts/runs/` 下） | 训练 epoch | 测试方法 | 独立审计 |
| --- | --- | ---: | ---: | --- |
| R02 | `r02-clean-replication-20260923` | 39 | 8 | [记录](../validation/r02-clean-replication-audit.json) |
| R03 | `r03-verified-clean-replication-20260924` | 48 | 12 | [记录](../validation/r03-verified-clean-audit.json) |
| R04 | `r04-verified-clean-replication-20260924` | 0 | 10 | [记录](../validation/r04-verified-clean-audit.json) |
| R05 | `r05-verified-clean-replication-20260924` | 35 | 8 | [记录](../validation/r05-verified-clean-audit.json) |

运行系列、逐请求轨迹、模型、数据仍在本机忽略目录，不随 Git 提交。核验记录固定每个系列的原始字节 SHA-256，并以规范化 JSON SHA-256 固定四份已发表的审计记录。检查器逐份核对原始脏标记、新运行的干净标记、提交中的 31 份研究源码快照、配置、选择、种子汇总、训练轮数、测试指标，以及审计与系列的绑定。可在保留本地运行目录的机器上执行：

```powershell
.\.venv-research\Scripts\python.exe scripts\check_clean_replications.py
```

## 差异与边界

- R04 的协议 ID 与旧运行不同，因为新运行绑定了此次重新生成的 R03 上游运行；配置、选择和测试指标仍一致。
- R03 的 `encoder.joblib` 容器字节 SHA-256 不同，`items.json`、`vectors.npy` 字节 SHA-256 相同。不能因此声称所有产物逐字节相同；独立审计和测试指标一致。
- R02 的基线 `evaluation_wall_seconds` 不同。核验只剔除该耗时字段，未剔除质量与覆盖字段。
- R03/R04/R05 依赖静态元数据时间假设；本次沿用原协议，并未消除该研究限制。
- 本次复用本机已有研究依赖环境，未从全新机器安装验证。旧测试已被查看，复验是可重复性证据，不是新的密封测试；不证明统计显著性或线上效果。
- 中途或曾在脏工作区启动的试跑不纳入上述结论。只有表内四个完成且通过审计的新运行构成此次证据。
