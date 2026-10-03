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

## 冻结排序组件安全导出（2026-10-03）

新增[研究导出入口](../src/evorec/research/export_ranker.py)和[纯服务加载器](../src/evorec/infrastructure/residual_ranker.py)。只转换公开归档验证选中的 `A-frozen-s17`：复用 R05 冷加权 seed 17 / epoch 3 检查点，不因已经查看的 R06 测试结果改选 B/C/D，不训练新权重。

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.export_ranker artifacts/exports/my-r06-ranker
.\.venv\Scripts\python.exe -m scripts.load_residual_ranker artifacts/exports/my-r06-ranker --expected-manifest-sha256 <导出返回的manifest_sha256>
```

每次必须使用新的 `artifacts/` 子目录。导出器核对 R05 原运行、复验、检查点、商品向量和 R06 验证候选缓存的归档哈希；检查点协议使用 R05 训练协议 `2624e6561e71af4b`，选型使用 R06 协议 `e11840f33a2fec8d`，不混淆两者。研究环境只以 `torch.load(weights_only=True)` 读取已核对的检查点，不读取编码器 joblib，也不读测试集、验证目标或查询 ID 数组。

组件目录只能含三份文件：`manifest.json`、`weights.f32`、`validation.json`。权重按三层 Linear 的行优先 weight / bias 顺序保存为小端 float32；执行固定 `520 → 128 → 64 → 1`、精确 erf GELU、tanh 残差和原 RRF 基分。服务环境不需要 Torch、NumPy、sklearn、joblib 或 pickle。浮点计算使用 Python double，中间结果不承诺与 Torch float32 位级相同；输出转回 float32，回放要求绝对误差不超过固定 `1e-5`，并要求稳定 Top-20 顺序完全相同。空历史只用原 RRF 基分，padding 在导出参考样本前移除。

加载器限制输入最多 400 个候选、维度最多 128、隐藏层最多 256 / 128、1–4 个参考请求、JSON 单文件最多 8 MiB、权重文件最多 4 MiB。拒绝重复 JSON 键、未知字段、非有限或极端数值、错误形状/标量顺序、路径或格式变化、未列出文件、哈希漂移和黄金样本错配。哈希只证明一致性，不证明来源可信；目录须可信且不可变，批准后用 manifest 哈希固定加载身份。

参考请求固定取验证缓存中可表示历史的首条、中间条、末条，以及首条空历史，不按命中与否挑样本。导出同时在组件目录之外保存 `-source/` 源码副本和 `-verification.json` 核对记录，包含基础提交、真实 dirty 状态、六份关键模块的源码哈希与实际依赖版本。导出失败不留下可加载的 manifest；历史目录不覆盖。源码副本和基础提交用于还原这次导出，不代表已经重建全部历史研究环境。

**这不是可发布的在线 bundle。** 组件输入是已经构造好的候选向量、上下文和 8 个标量；不负责 CF/content 召回、TF-IDF/SVD 变换、可用时间/已见过滤、在线特征构造、商品版本绑定或发布恢复。旧的完整 bundle 校验会拒绝这个组件格式。后续须完成这些适配，核对整条链路，再显式发布；没有对当前服务执行发布、修改业务数据或重启操作。

## 冻结商品与候选特征组件（2026-10-03）

在排序组件之后，新增[特征导出器](../src/evorec/research/export_features.py)、[纯服务特征构造器](../src/evorec/infrastructure/r06_features.py)和[批准哈希重载入口](../scripts/load_r06_features.py)。仍使用 `A-frozen-s17` 的原商品向量，不重新拟合编码器、训练排序器或按测试效果改选模型。

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.export_features artifacts/exports/my-r06-features --ranker-component artifacts/exports/my-r06-ranker
.\.venv\Scripts\python.exe -m scripts.load_r06_features artifacts/exports/my-r06-features --expected-manifest-sha256 <特征manifest_sha256> --ranker-component artifacts/exports/my-r06-ranker --expected-ranker-manifest-sha256 <排序manifest_sha256>
```

本阶段研究导出固定使用[已验收排序组件记录](../docs/validation/r06-ranker-component-20261003.json)批准的 manifest 哈希；第二条命令的特征哈希取本次导出结果。只重载特征时可同时省略两个排序参数；只提供其中一个会拒绝执行。输出、`-source/` 和 `-verification.json` 均须使用新目录，不能覆盖旧证据。

特征目录只能包含 `manifest.json`、`items.json`、`vectors.f32`、`validation.json`。商品映射、原首次交互时间、训练交互存在标记和归一化 prior 全部冻结；训练标记包含低评分交互，不能以正反馈 prior 是否非零代替。小端 float32 向量按商品 ID 排序存储，服务加载为不可变缓冲区；manifest 哈希固定批准身份。加载限制 200,000 件商品、128 维、JSON 单文件 32 MiB、向量 128 MiB、1–4 个参考请求，拒绝格式、路径、资源、数值、来源和参考特征漂移。哈希不能代替可信不可变目录或授权。

构造器接收完整已见集合、最多 50 条正反馈历史，以及已经召回且严格合法的 CF/content 各最多 200 项。去重排序后的并集最多 400 项；已见、未知商品或首次交互时间不严格早于请求的候选直接拒绝，不静默改动 provider 排名。未知或零向量历史不贡献向量，但仍占据原衰减位置和历史长度；保留原 sklearn 对极小 float32 范数的处理。上下文与 8 个标量不接收目标标签；与排序器联合执行前，核对特征指纹、原/选型归档、原 items/vectors/验证缓存哈希及两个协议 ID。

真实冻结导出核对 137,249 件商品、128 维向量；固定 4 个验证请求共 1,399 个候选，上下文最大误差 `5.96e-8`、标量最大误差 `1.79e-7`（固定特征门槛 `1e-6`）。接入已批准排序组件后，分数最大误差 `7.15e-7`，稳定 Top-20 一致。这是选定请求的组件兼容性回放，不是全验证集、历史 GPU、召回覆盖或线上效果验收。源码快照包含真实 dirty 状态和 11 份关键模块字节副本；自身或联合验证失败、报告写入失败或用户中断均撤下新组件 manifest，保留诊断文件，不影响旧组件。尚不提供进程被强制杀死时的原子提交保障。

为重建验证请求的历史和完整已见集合，导出器校验并解析完整原始评分 CSV；只生成验证请求，不生成或评价测试请求。读取来源轨迹的查询 ID 只用于对齐固定位置，不按目标/命中筛选；原始标识和请求轨迹留在忽略目录。不得将此阶段描述为“完全没读测试时段数据”。

**仍不接入在线推荐或发布。** 这里的可用时间是研究用首次交互代理，不是线上商品上架时间。尚缺安全 TF-IDF/SVD 新文本变换、CF/content 召回导出、线上会话/商品版本适配及完整 bundle 发布恢复。当前服务的活动模型和业务数据未切换。验收汇总见[本轮记录](../docs/validation/r06-frozen-features-20261003.json)。
