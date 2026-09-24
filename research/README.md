# 研究工作区

已完成 R01–R06 离线实验，最新为同预算多兴趣召回和排序适配。[R06 运行指南](R06-multi-interest-guide.md)提供运行、审计与展示重建入口；[最新报告](../docs/experiments/r06-multi-interest/report.html)保留覆盖小幅变化与排名未获支持的结果。下文保留各阶段的历史复现入口，不同样本的指标分开比较。

## 实际模块

| 模块 | 职责 |
| --- | --- |
| download.py / sample.py | 有上限的前缀读取、完整文件校验、用户哈希采样、完整类别首次观察时间 |
| data.py / protocol.py | 数据校验、严格时间历史、训练词表、验证 / 测试访问顺序 |
| baselines.py / training_baselines.py | 热门、ItemCF、时间衰减热门与协同融合 |
| evaluation.py / runner.py | R01 时间回放、效果指标与资源记录 |
| neural.py / train.py | 因果自注意力模型、GPU 训练、早停、三种子、检查点与最终测试 |
| reporting.py | 逐轮更新 JSON / Markdown / HTML / PNG / SVG |
| scripts/audit_training.py | 从原始样本独立重建历史、核对全部测试轨迹与统计 |

研究模块位于 src/evorec/research/；审计脚本位于根目录 scripts/。

## 本机环境

服务环境为 .venv，研究环境为 .venv-research。当前研究环境以 system-site-packages 复用已有 PyTorch 2.9.0+cu126、NumPy 2.1.3 和 Matplotlib 3.9.3；它不是完全隔离的干净安装。本轮已补齐项目基础依赖，`pip check` 通过，并新增可用于新环境的合并锁文件；仍未把现有目录本身当作干净复建证据。

RTX 4060 Laptop GPU（8188 MiB）已通过实际前向、反向和完整训练验证。与本轮有关的 26 个依赖满足已安装元数据约束，见 [环境记录](environment-observed.json) 与 [训练依赖快照](requirements-training.lock.txt)。这不代表整个系统 Python 环境的所有第三方包均已检查。

已有本机环境可直接使用下面的研究解释器。新机器可在独立环境安装依赖快照和本项目：

```powershell
python -m venv .venv-research
.\.venv-research\Scripts\python.exe -m pip install -r research/requirements-research-dev.lock.txt
.\.venv-research\Scripts\python.exe -m pip install --no-deps -e .
.\.venv-research\Scripts\python.exe -m pip check
.\.venv-research\Scripts\python.exe scripts/check_environment.py research --require-cuda
```

安装需要网络，CUDA 包较大；合并锁文件将服务/数据库开发依赖与研究依赖固定在同一个可解析集合中。历史 `requirements-training.lock.txt` 和 `requirements-content.lock.txt` 继续作为既有实验环境记录，不改写。当前仍没有执行全新目录复建或 Linux 复建；训练入口要求 CUDA 可用。

## 取得数据

R01 前缀是开发用户排除列表的来源。已有对应文件时不重复下载；数据目录被 Git 忽略。

```powershell
.\.venv\Scripts\python.exe -m evorec.research.download --output datasets/video_games_r01.csv --limit 50000
.\.venv-research\Scripts\python.exe -m evorec.research.sample --source datasets/amazon2023/Video_Games.csv.gz --output datasets/video_games_r02.csv --development-sample datasets/video_games_r01.csv
```

R01 仅读取前缀，不保证整个 gzip CRC；R02 读取完整文件至 EOF 并检查 CRC。固定源文件约 114.8 MB，扫描 4,555,500 条记录后得到 226,172 条交互、137,103 个用户。采样、商品可用性代理、时间边界和模型词表规则见 [锁定协议](../docs/experiments/r02-protocol.md)。

程序拒绝覆盖已有样本及清单；失败时可能留下 .part 文件，应先检查错误，再选择新的输出路径。更换输入路径时同步更新配置，并重新记录数据指纹。

## 训练并持续更新报告

在项目根目录执行；每次输出目录必须是新的：

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.train --config research/configs/r02-training.json --output artifacts/runs/my-r02-training
```

一次运行包含 5 个统计基线、种子 17 的两组学习率比较、所选配置的种子 29 / 43 训练，以及冻结检查点后的测试。最多每模型 12 轮，连续 3 轮验证 NDCG@10 未提高时停止。没有可映射历史时回退到训练热门。

每轮完成后更新最新报告，保留运行目录内的完整系列记录、源代码快照、配置、最佳模型和逐请求轨迹。HTML 每 30 秒刷新；任务完成后不会自动开始下一轮。一个最新报告目录只供一个训练进程写入，避免并行实验互相覆盖。

所有记录型实验现在会在创建输出目录前检查 Git：`src/evorec/research`、`research/configs`、`tests` 或 `scripts` 有未提交变更就拒绝运行。忽略的数据和实验产物可以保留；范围外变更会写入运行来源记录。R02–R05 原运行仍如实标记为脏工作区，其精确源码与配置由[历史复原清单](provenance/r02-r05-source-reconstruction.json)和[检查结果](../docs/validation/r02-r05-provenance-reconstruction.json)补充，不冒充原始干净提交。另有四轮从已提交代码启动的[干净工作区复验](../docs/experiments/r02-r05-clean-replication.md)，其结果与原运行分开记录。

已完成运行目录为 artifacts/runs/r02-training-20260915，共 4 次训练、39 个 epoch。学习率选中 0.0003，三种子最佳轮次分别为 9、11、6。测试已查看，后续调参不能再把它宣称为未见测试；新的研究迭代先登记新的时间窗口或独立留出协议。

## 重建展示与独立审计

展示排版可以重建，不需要重复训练：

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.reporting --series artifacts/runs/r02-training-20260915/series.json
.\.venv-research\Scripts\python.exe scripts/audit_training.py --run artifacts/runs/r02-training-20260915 --output docs/validation/training-checks.json
```

展示文件同时记录训练源代码快照哈希与当前报告生成器哈希。训练结束后的图表排版改进不改写历史训练源码。

R01 仍可独立运行：

```powershell
.\.venv\Scripts\python.exe -m evorec.research.runner --config research/configs/r01-small.json --output artifacts/runs/my-r01-run
```

## 检查

普通环境可执行 pytest。当前 Windows 系统临时目录有权限问题，使用项目内全新的临时目录，避免覆盖已有实验文件：

```powershell
$researchTestTemp = Join-Path (Get-Location).Path ('tmp/pytest-' + [Guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp $researchTestTemp
$neuralTestTemp = Join-Path (Get-Location).Path ('tmp/pytest-' + [Guid]::NewGuid().ToString('N'))
.\.venv-research\Scripts\python.exe -m pytest tests/test_training_protocol.py tests/test_neural.py -p no:cacheprovider --basetemp $neuralTestTemp
```

环境本身可以先独立检查，失败时比从训练异常反推依赖更直接：

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py service
.\.venv\Scripts\python.exe scripts/check_environment.py database
.\.venv-research\Scripts\python.exe scripts/check_environment.py research --require-cuda
```

实测：服务环境 85 项通过，神经测试模块因该环境不含 Torch 跳过；研究专项 11 项通过，其中 6 项协议检查与前一套重叠。专项检查含因果掩码、损失下降、过滤回退、检查点一致性及协议不匹配拒绝。

## R04 冷商品策略与实验图表

冻结 R03 编码器与模型，用新的用户分组完成五种保留策略比较。验证集没有支持替换原融合策略。训练、推理模型与查询样本的冷商品定义分别保存，避免更换样本时悄悄改变比较对象。

[运行指南](R04-gating-guide.md) · [正式报告](../docs/experiments/r04-gating/report.md) · [失败定位](../docs/experiments/r04-gating/error-analysis.md)

最新回归为服务环境 96 项通过（神经模块在此环境跳过），研究环境 19 项通过，其中 8 项门控检查重叠。上方 85 / 11 为 R02 阶段记录。

## R05 新神经排序器

已完成时间滚动模拟冷商品训练，普通与冷加权目标消融，4 次训练共 35 轮。排序器、特征和训练入口分别位于 ranker.py、ranker_data.py、train_ranker.py；每轮更新 ranker_report.py 生成的报告。

[模型原理与运行](R05-ranker-guide.md) · [完成结果](../docs/experiments/r05-ranker/report.md) · [训练曲线](../docs/experiments/r05-ranker/report.html) · [独立审计](../docs/validation/ranker-checks.json)

原 R05 验证：服务环境 96 项通过，研究环境 28 项通过（8 项门控重叠）。该次运行冷加权只有 seed 17；后续复验另见下节。

## R05 冷加权固定配置复验

已重放 seed 17 并补齐 seed 29 / 43，共 30 轮；冷目标命中 26 / 24 / 27，整体 NDCG 为 0.009315 ± 0.000387。已完成 48 项用户聚类边际区间与逐请求排名审计。

[复验指南](R05-replication-guide.md) · [完成报告](../docs/experiments/r05-cold-replication/report.md) · [统计区间](../docs/experiments/r05-cold-replication/uncertainty.json)

专项 18 项、原排序器 9 项通过，服务 96 项通过、5 个依赖相关模块跳过。旧测试已经查看，本阶段不作为新的封闭测试。

## R06 同预算多兴趣召回与排序适配

新增 multi_interest.py、r06_data.py、run_multi_interest.py 与 r06_analysis.py。完成六次训练、74 轮、15 种方法的统一测试和全部候选来源/模型重放；50 项配对用户区间独立复算一致。验证规则保留原路径，未用测试结果重新选型。

[运行指南](R06-multi-interest-guide.md) · [完整结果](../docs/experiments/r06-multi-interest/report.md) · [交付检查](../docs/validation/r06-artifacts-checks.json)

最新研究专项 47 项通过；服务与数据 108 项通过、6 个依赖相关模块跳过；其中 6 项协议检查重叠。上方各节数字为对应历史阶段，不能累加作最新测试总数。

## 下一阶段

R06 测试已经查看。后续先解释无历史冷目标与候选增益未转化为排名增益的原因，再登记新实验；工程闭环按[建议评审](../docs/09-engineering-follow-up.md)推进，后续实现继续独立分支和阶段提交。
