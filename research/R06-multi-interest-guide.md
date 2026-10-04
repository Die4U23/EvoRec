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

2026-10-04 CI 修正：JSON 限定 UTF-8、嵌套最多 32 层，由组件在调用解析器前显式检查，字符串中的括号和转义引号不计层数。不能依赖特定 CPython 补丁版本触发递归异常；原先本地 3.12.6 与 CI 3.12.14 对 5,000 层输入的隐式处理不同，初次 push/PR 检查均失败。边界断言保留，另补 32/33 层与转义用例；完整记录见 [CI 修正](../docs/validation/r06-features-ci-fix-20261004.json)。

构造器接收完整已见集合、最多 50 条正反馈历史，以及已经召回且严格合法的 CF/content 各最多 200 项。去重排序后的并集最多 400 项；已见、未知商品或首次交互时间不严格早于请求的候选直接拒绝，不静默改动 provider 排名。未知或零向量历史不贡献向量，但仍占据原衰减位置和历史长度；保留原 sklearn 对极小 float32 范数的处理。上下文与 8 个标量不接收目标标签；与排序器联合执行前，核对特征指纹、原/选型归档、原 items/vectors/验证缓存哈希及两个协议 ID。

真实冻结导出核对 137,249 件商品、128 维向量；固定 4 个验证请求共 1,399 个候选，上下文最大误差 `5.96e-8`、标量最大误差 `1.79e-7`（固定特征门槛 `1e-6`）。接入已批准排序组件后，分数最大误差 `7.15e-7`，稳定 Top-20 一致。这是选定请求的组件兼容性回放，不是全验证集、历史 GPU、召回覆盖或线上效果验收。源码快照包含真实 dirty 状态和 11 份关键模块字节副本；自身或联合验证失败、报告写入失败或用户中断均撤下新组件 manifest，保留诊断文件，不影响旧组件。尚不提供进程被强制杀死时的原子提交保障。

为重建验证请求的历史和完整已见集合，导出器校验并解析完整原始评分 CSV；只生成验证请求，不生成或评价测试请求。读取来源轨迹的查询 ID 只用于对齐固定位置，不按目标/命中筛选；原始标识和请求轨迹留在忽略目录。不得将此阶段描述为“完全没读测试时段数据”。

**仍不接入在线推荐或发布。** 这里的可用时间是研究用首次交互代理，不是线上商品上架时间。尚缺安全 TF-IDF/SVD 新文本变换、CF/content 召回导出、线上会话/商品版本适配及完整 bundle 发布恢复。当前服务的活动模型和业务数据未切换。验收汇总见[本轮记录](../docs/validation/r06-frozen-features-20261003.json)。

## 冻结文本编码器安全导出（2026-10-04）

新增[研究导出器](../src/evorec/research/export_encoder.py)、[纯服务编码器](../src/evorec/infrastructure/content_encoder.py)与[批准哈希重载入口](../scripts/load_content_encoder.py)。复用 `A-frozen-s17` 所用的 R05 TF-IDF/SVD：18,820 个词项、128 维；保留原 15,824 份拟合文档、训练边界 `1483228800000` 毫秒及拟合来源指纹，不重新训练、不按已查看的测试结果选型。

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.export_encoder artifacts/exports/my-r06-encoder --allow-trusted-joblib --features-component artifacts/exports/my-r06-features --expected-features-manifest-sha256 <已批准的特征manifest_sha256>
.\.venv\Scripts\python.exe -m scripts.load_content_encoder artifacts/exports/my-r06-encoder --expected-manifest-sha256 <已批准的编码器manifest_sha256> --features-component artifacts/exports/my-r06-features --expected-features-manifest-sha256 <已批准的特征manifest_sha256>
```

`joblib` 可执行代码，不是通用安全格式。第一条命令只用于本项目可信本地冻结研究文件；要求显式确认参数，并再次核对实际读取的完整字节哈希后，通过内存缓冲区反序列化这些相同字节，避免校验路径与随后加载文件不同。不能将第三方未知 joblib 交给此入口。第二条命令在服务环境执行，不需要或导入 Torch、NumPy、SciPy、sklearn、joblib、pickle；也不会反序列化旧格式。单独核验编码器时可同时省略两个特征参数，提供其中一个会拒绝执行。

受控目录恰好包含 `manifest.json`、`vocabulary.json`、`weights.f32`、`validation.json`。词项保持原排序索引；权重先存 IDF，再按词项优先保存 SVD 系数，小端 float32。严格复用 Unicode 小写、双字符以上词分词、单词与相邻双词、`1 + log(tf)`、IDF、TF-IDF L2、SVD 投影与输出 L2；空文本/OOV 返回零向量，极小投影不被放大。服务清单固定 Unicode 数据版本与算法，不接收任意正则、函数或可执行对象；不承诺与 sklearn 位级相同。

资源上限：20,000 个词项、128 维、单个 JSON/权重文件 16 MiB、单文本 32,768 字符和 4,096 个分词、2–16 个参考样本、参考文本总长 131,072 字符。JSON 强制 UTF-8 与 32 层深度，拒绝重复键、非有限值、未知字段、错误形状/词项顺序、路径与哈希漂移。空文本与有表示的参考样本都必须存在；固定绝对向量误差门槛为 `1e-6`。配对特征组件时核对维度及七项共同来源/协议身份，不能仅核对维度或词表大小。哈希仅验证一致性，不能替代可信来源和明确批准。

本地真实导出固定核对五个商品库位置与六个合成边界文本，共 11 项参考；相对原冻结 sklearn 变换最大误差 `5.960464477539063e-8`，五个位置相对原存储向量的误差为零。未查看目标标签、评价测试请求或重新计算召回；这里的商品元数据仍继承静态元数据可用性假设。原始词表、权重、文本样本和源码副本都留在忽略目录，公开[汇总验收](../docs/validation/r06-safe-encoder-20261004.json)。

输出、`-source/` 与 `-verification.json` 必须使用新路径。记录真实 dirty 状态、基础提交、十二份关键文件字节副本和依赖版本；组件重放、配对、报告写入失败或中断会撤销本次新建 manifest，保留诊断文件，不覆盖旧组件/报告。独占创建报告，若另一写入者抢先创建，保留其文件。仍不保证进程被强制终止时的原子发布。

最终本地导出从实现提交 `376b0f352929f4576480631b9bb57848083dcbca` 的干净工作树重放，十二份源码副本与该提交 Git blob 哈希逐项一致；早期 dirty 诊断目录保留，不改写其记录。依赖版本和原始研究输入仍需按清单准备，未声称完成全新机器环境复建。

**仍不是在线 R06 模型包。** `ContentEncoder.encode(text)` 是独立文本变换，不负责商品入库、召回、线上历史适配、模型版本发布或恢复。CF/content 召回导出与整条推荐链路接入仍待完成；未切换当前服务、修改业务数据或重启 8000 服务。#15 的目标曾停留在旧排序分支，主线补齐通过 [PR #16](https://github.com/Die4U23/EvoRec/pull/16) 独立处理，不因显示已合并就假设 main 已包含依赖。

## 冻结 CF/content 召回组件（2026-10-04）

新增[研究导出器](../src/evorec/research/export_retrieval.py)、[纯服务召回器](../src/evorec/infrastructure/r06_retrieval.py)与[批准哈希重载入口](../scripts/load_r06_retrieval.py)。此时 #16、#17 均已合入 main，以上历史记录保留原阶段边界。本阶段重建原训练期的确定性 ItemCF/RecentPopular 统计，不拟合新的文本或神经模型、不选 B/C/D、不评价测试请求；完整评分 CSV 仍被解析后按训练截止时间严格过滤，不能说未读测试时段数据。

```powershell
.\.venv-research\Scripts\python.exe -m evorec.research.export_retrieval artifacts/exports/my-r06-retrieval --features-component artifacts/exports/my-r06-features --expected-features-manifest-sha256 <已批准的特征manifest_sha256> --ranker-component artifacts/exports/my-r06-ranker --expected-ranker-manifest-sha256 <已批准的排序manifest_sha256>
.\.venv\Scripts\python.exe -m scripts.load_r06_retrieval artifacts/exports/my-r06-retrieval --expected-manifest-sha256 <已批准的召回manifest_sha256> --features-component artifacts/exports/my-r06-features --expected-features-manifest-sha256 <已批准的特征manifest_sha256> --ranker-component artifacts/exports/my-r06-ranker --expected-ranker-manifest-sha256 <已批准的排序manifest_sha256>
```

导出必须使用新的 `artifacts/` 子目录，核对冻结评分/商品来源并加载批准的特征、排序组件。CF 原规则：用户最多 100 件正评分商品、每件最多 100 个余弦共现邻居、历史距离衰减 `.8`，与 365 天半衰期 prior 以 `.25/.75` 混合；先取 prior 的前 1000 件再过滤，不能改为先过滤再截断。只在研究侧重建统计，服务侧不读原 CSV、joblib、Torch 或任何可执行模型。

内容侧复用冻结商品向量与已有历史构造器，显式舍入 float32 乘积及逐项累加，规范名 `f32-product-sequential-f32-sum-v1`；严格合法且有表示的商品按分数降序、ID 升序选取，负但有限的内容分数也须保留。空/未知/零向量历史走冻结 prior 回退，有效历史不足 200 件时再按 prior 补足，不重复或凭空填商品。两路各最多 200 件；`R06Retrieval.build_pool(history, seen, timestamp_ms)` 构造原最多 400 件并集，可继续用 `PoolFeatures.score(ranker)` 核对来源后排序。

请求最多 50 条历史、10,000 件完整已见商品，已见集合必须覆盖历史；已知历史及返回商品的首次交互代理时间须严格早于请求，未知历史不贡献表示但仍占衰减位置。召回源目录只能有 `manifest.json`、`statistics.json`、`neighbors.bin`、`validation.json`：CSR 小端 uint32 偏移/索引与 float64 相似度，热度计数保留 float64 精度。上限 200,000 件商品、2,000,000 条边、每行 100 邻居、图/JSON 单文件 32 MiB、2–4 个完整 provider 参考，须同时含有表示的历史和冷启动回退；禁止未知字段、重复键、深层 JSON、非有限/越界数字、自环/重复/乱序边、路径或哈希漂移。结合批准特征 manifest、十一项共同来源/协议、训练 prior 一致性后才执行；哈希仍不能替代可信不可变目录。

真实固定四个原验证请求的 CF 和 content 完整 200 件顺序、1,399 件候选及最终 Top-20 均通过，排序分数最大误差 `7.15e-7`。初次 float64 归约导出失败后撤下 manifest，诊断产物不覆盖；改为明确 float32 算术后重放通过，没有用候选集合或放宽误差隐藏交换顺序。不同矩阵库/GPU 归约仍可能改变近等分次序，四个请求通过不是全验证集或跨设备所有请求的位级一致证明。

最终从干净提交 `0e2a4f45484cdeb39a2a06d8e8a87acfc9e0e33a` 导出，十六份关键源码副本与 Git blob 一致。输出、`-source/` 和独占创建的 `-verification.json` 都须使用新路径；参考重放、排序配对、报告写入异常或中断会撤下本次 manifest，保留诊断文件，保护旧组件/报告与抢先创建的其他写入者报告。尚不保证进程被强制杀死时原子提交；版本、输入和依赖记录不等于全新机器环境复建。

**仍不是在线完整 bundle。** 本机干净回放中有历史召回约 7–8 秒，冷启动约 0.0012 秒，仅是单次离线观察。还须内容扫描性能优化、线上会话及新增/修改/下架商品的版本适配、完整模型包发布恢复与推荐接口集成；本轮不扩展冻结商品库，不切换当前服务或业务数据。详见[公开验收摘要](../docs/validation/r06-controlled-retrieval-20261004.json)。

## 可选 CPU 内容召回加速（2026-10-04）

默认 `content_backend="stdlib"` 不变，原 `.venv` 无需添加 NumPy。显式 `content_backend="numpy"` 或 CLI `--content-backend numpy` 选择[分块内核](../src/evorec/infrastructure/_content_numpy.py)；只复用已批准的冻结向量与召回文件，不重建统计、重训或激活模型。缺少依赖、版本不匹配或算术自检失败时拒绝加载，不静默换后端。

```powershell
.\.venv\Scripts\python.exe -m venv .venv-accelerated
.\.venv-accelerated\Scripts\python.exe -m pip install -r requirements-retrieval.lock.txt
.\.venv-accelerated\Scripts\python.exe -m pip install --no-deps -e .
.\.venv-accelerated\Scripts\python.exe -m pip check
.\.venv-accelerated\Scripts\python.exe -m scripts.load_r06_retrieval artifacts/exports/my-r06-retrieval --expected-manifest-sha256 <已批准的召回manifest_sha256> --features-component artifacts/exports/my-r06-features --expected-features-manifest-sha256 <已批准的特征manifest_sha256> --ranker-component artifacts/exports/my-r06-ranker --expected-ranker-manifest-sha256 <已批准的排序manifest_sha256> --content-backend numpy
```

[可选锁文件](../requirements-retrieval.lock.txt)固定 NumPy `2.1.3`，不需要 Torch、SciPy、sklearn 或 joblib。CI 将可选环境与纯服务环境分开，两者不互相代替；加速测试出现跳过也算失败。锁定版本和算术回放不是完整依赖安全审计或跨设备位级正确性证明。

每块最多 4,096 件，向量是原不可变字节的只读视图。先用独立 float32 乘法，再沿特征轴逐项 float32 累加，不使用 `dot`、BLAS、`sum` 或融合乘加；首项先加正零，保留原标量协议的零符号。参见 NumPy 的[乘法类型控制](https://numpy.org/doc/2.1/reference/generated/numpy.multiply.html)和[逐项累加及中间类型](https://numpy.org/doc/2.1/reference/generated/numpy.ufunc.accumulate.html)。128 维时单个乘积矩阵最多 2 MiB，不代表进程总 RSS 上限；无共享可变数组或请求结果缓存。过滤、负分、同分 ID 顺序、CF 和 prior 回退规则不变。

[交错基准入口](../scripts/benchmark_r06_retrieval.py)接收与加载命令相同的三个组件目录及批准哈希，并要求新的 `artifacts/` 子目录：

```powershell
.\.venv-accelerated\Scripts\python.exe -m scripts.benchmark_r06_retrieval artifacts/benchmarks/my-r06-comparison --features-component artifacts/exports/my-r06-features --expected-features-manifest-sha256 <已批准的特征manifest_sha256> --retrieval-component artifacts/exports/my-r06-retrieval --expected-retrieval-manifest-sha256 <已批准的召回manifest_sha256> --ranker-component artifacts/exports/my-r06-ranker --expected-ranker-manifest-sha256 <已批准的排序manifest_sha256> --rounds 2
```

它在同一进程交替 ABBA/BAAB，每个固定参考请求每轮各执行两次完整召回，计时不含加载、候选特征构造和排序校验。每次计时后检查完整 provider 顺序、并集、上下文、标量、分数及 Top-20；失败不留下通过报告，诊断源码保留，旧证据和竞争写入者的报告不覆盖。报告保存各次耗时、中位数、环境与源码副本/哈希；执行期间源码变动会拒绝通过。运行前仍应提交实现、确认干净工作树；不能以 dirty 快照替代 Git 绑定，也不保证进程强制终止时的原子发布。这只验收固定请求的数值兼容性与热加载召回速度，不是完整推荐接口、并发容量、全验证集质量或线上 SLA。

本机验收使用原批准的三份组件字节，从干净实现提交 `8946b777e86fe7665ff7e50ba5ad4c9f7e0d3332` 运行到 `artifacts/benchmarks/r06-acceleration-20261004-c`，十三份源码快照与 Git blob 逐项匹配。四个原固定请求、两轮 32 次召回全部通过顺序与候选/排序检查。有历史请求合并中位数：标准库 `3.667804 s`、NumPy `0.076364 s`，约 `48.03×`；三个有历史请求各自约 `46.09× / 48.37× / 50.16×`。冷启动两者约 `1.2 ms`，没有同级收益。旧的 7–8 秒是另一环境的单次观察，不用于本轮速度比；无需为了加速重新导出、批准或激活组件。当前 `.venv-accelerated` 已确认只有可选 NumPy 而无研究执行依赖，原 `.venv` 仍无 NumPy。

测试范围、失败修正及未完成边界见[公开验收摘要](../docs/validation/r06-retrieval-acceleration-20261004.json)。

## 冻结商品子集的请求快照适配（2026-10-04）

[快照适配器](../src/evorec/infrastructure/r06_serving.py)将原受控特征、召回、排序组件组合成同步 `R06SnapshotRanker`，接收 `FrozenR06Request`，返回带原 `RequestBinding` 的 `RankedBatch` 和模型版本/召回信息。现有 `dense` 执行类别在此表示 R06 CF+content 候选及冻结残差排序，候选来源明确为 `r06-a-frozen-s17`；不声称该组件已实现异步 `RankingPort`、现有 CPU dot-product 路线或生成式模型。模型版本绑定策略名、bundle UUID 和三份组件 manifest 哈希；后端加速选择不改变模型身份。

请求必须由可信 admission/发布器提供：明确捕获的毫秒时间、完整已见集合、不可变会话/商品快照、批准特征 manifest 与冻结原商品来源哈希。适配器核对 UUID 和版本类型、组件共同来源及同一不可变特征对象；原始商品哈希是来源身份声明，不是当前数据库内容的自动扫描，也不是授权/签名。不能允许公网客户端自己填一个旧哈希来“证明”商品未修改，未来发布/入场层仍须校验实际商品内容并生成声明。

会话历史最多 10,000 条，只有最近 50 条进入原衰减/上下文/长度特征；未知历史不提前删去，仍占位置。传入已见集合须覆盖**整个**会话历史，历史之外的曝光/交互也须由入场层补齐；隐藏与收藏状态会合并进排除集，合并后最多 10,000 件，不能靠截断已见集满足预算。已知的完整历史须严格早于请求时间。适配器不读数据库、不取当前时钟、不缓存请求结果、不主动变更会话。

冻结商品子集策略 `r06-a-frozen-subset-v1` 允许对原商品做可售/下架选择。`eligible_items` 在 CF 邻居、prior、内容扫描与两路 top-200 选取**之前**过滤，CF 归一化也在合法候选上计算，不先取 200 件再丢下架项。原 prior 的“先截前 1000 件再过滤”规则仍保留；零 prior 商品不会凭空进入 CF 回退。完全相同的全库视图复现原参考；真实子集的 provider 顺序/RRF 可以变化，属于明确的服务策略，不冒称原研究所有子集已有质量证据。

子集之外的新 ID 直接拒绝；同 ID 的文本/表示修改需要新的真实内容身份和批准产物，旧身份声明不能自动发现调用者谎报的修改。不将新文本编码器直接套在旧 CF 图/向量索引上，也不扩展冻结研究商品库。零合法候选返回空结果，不补造商品。同步组件未实现取消、排队或模型/索引租约，不能直接塞入异步 API 后就宣布超时可中止 CPU 推理。

[记录型回放入口](../scripts/verify_r06_serving.py)复用已验收的完整 provider/特征/排序判据，额外检查实际返回批次与领域合法 Top-20；不只重新计算一个旁路候选池。必须先提交并确认干净工作树，输出使用新的 `artifacts/` 子目录：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_r06_serving artifacts/replays/my-r06-snapshot --features-component artifacts/exports/my-r06-features --expected-features-manifest-sha256 <批准特征manifest_sha256> --retrieval-component artifacts/exports/my-r06-retrieval --expected-retrieval-manifest-sha256 <批准召回manifest_sha256> --ranker-component artifacts/exports/my-r06-ranker --expected-ranker-manifest-sha256 <批准排序manifest_sha256>
```

默认不需要 NumPy；在独立加速环境使用同一命令并追加 `--content-backend numpy`。回放从原固定验证参考构造合成请求/bundle UUID，不是创建真实会话或发布 bundle；不读取目标标签或评价测试请求。报告与十六份源码副本保留在忽略目录，失败/中断不保留通过报告，旧证据和竞争写入者的报告不覆盖。还不保证进程被强制终止时的原子落盘。

本地从干净实现提交 `da14f1a96d807248f7bc448e4ff01cd9cc231892` 分别运行到 `artifacts/replays/r06-snapshot-serving-20261004-c`（纯服务标准库）和 `-d`（独立 NumPy 环境）。每份十六个源码副本与当前文件和对应 Git blob 一致，两环境模型身份相同。四个原固定请求各 399/400/400/200 件候选，完整 provider 顺序、实际批次绑定/分数与领域合法 Top-20 通过；最大上下文/标量/分数误差分别 `5.96e-8 / 1.79e-7 / 7.15e-7`。真实商品子集尚无新质量评价，601 件受控合成库仅证明过滤前置与足量补齐；没有用它替代原模型/数据库的发布验收。

完整测试范围、失败修正与未完成边界见[公开验收摘要](../docs/validation/r06-snapshot-serving-20261004.json)。

## 自包含冻结 bundle 组装与真实商品身份2026-10-04

[组装器](../scripts/assemble_r06_bundle.py)将四个批准组件的原字节复制进独立 UUID 目录，同时保存原首次出现时间表与研究文本。新格式 `r06-frozen-bundle-v1` 与旧 demo CPU bundle 分开，不改旧格式、不自动注册/切换模型，不增添 A 方法没有的生成器/语义码本。[加载器](../scripts/load_r06_bundle.py)必须接收批准的外层 manifest 哈希，复查内层哈希、共同来源、参考样本、固定布局与资源上限。哈希检测漂移，不证明不可信来源的真实性。

features 的 `catalog_sha256` 只标识时间表，**不包含商品文本**。完整包另核对 encoder 的原 `metadata_sha256`：时间表 ID 集须等于冻结映射，时间须与 features 相同，文本 ID 不可超出映射。缺失文本明确视为空，不填造标题。逐件身份是 UTF-8 紧凑 JSON `[item_id, first_seen_ms, original_text]` 的 SHA-256；不修改训练指纹或原批准组件。

`FrozenR06Bundle.score(context, timestamp_ms, full_seen, catalog_items)` 接收可信入场层捕获的实际 `FrozenCatalogItem` 列表，不接收客户端自报哈希。记录须无重复且精确覆盖可售子集，每件 ID/原文本/时间都相符；新增、编辑和漏项拒绝，下架可形成合法子集。原文本来自研究标题/类别规则，不能用当前商家标题代替。现有数据库尚未生产此快照；调用者谎报旧内容时，组件不能自动探测数据库变化。

模型版本由格式和完整外层 manifest 摘要生成，绑定 UUID、四组件、两源文件、策略和组装提交；后端与请求子集不改变版本。原 `RequestBinding`、完整已见、最近 50 条建模与 top-200 前过滤保留。encoder 固定来源完整性，不授权把新商品放入旧 CF 图。

先提交代码、确认工作树干净，再使用新的忽略目录。所有批准值应来自受信验收记录，不能对未知输入现算哈希就当作批准。参数示例（尖括号需替换）：

```powershell
$bundleId = [guid]::NewGuid().ToString()
.\.venv\Scripts\python.exe -m scripts.assemble_r06_bundle artifacts/bundles/r06-frozen --bundle-id $bundleId --features-component <特征目录> --expected-features-manifest-sha256 <批准特征哈希> --retrieval-component <召回目录> --expected-retrieval-manifest-sha256 <批准召回哈希> --ranker-component <排序目录> --expected-ranker-manifest-sha256 <批准排序哈希> --encoder-component <编码器目录> --expected-encoder-manifest-sha256 <批准编码器哈希> --catalog datasets/video_games_r02.catalog.json --metadata datasets/video_games_r03.metadata.json
.\.venv\Scripts\python.exe -m scripts.load_r06_bundle artifacts/bundles/r06-frozen "artifacts/bundles/r06-frozen/$bundleId" --expected-manifest-sha256 <批准外层哈希>
.\.venv\Scripts\python.exe -m scripts.verify_r06_bundle artifacts/replays/my-r06-bundle artifacts/bundles/r06-frozen "artifacts/bundles/r06-frozen/$bundleId" --expected-manifest-sha256 <批准外层哈希>
```

先验证临时包，独占创建新目标，manifest 最后写入。失败/中断撤下自己创建的 marker，保留诊断文件，不覆盖旧包或竞争写入者。失败目录用新 UUID 重试，不原地覆盖。固定 18 文件，禁止链接/额外文件；单文件 128 MiB、全包 256 MiB、源 JSON 32 MiB、外 manifest 16 KiB。不承诺断电/fsync 持久性或强制杀进程后的原子发布，也不等于数据库发布事务。

[记录重放](../scripts/verify_r06_bundle.py)检查实际商品、完整 provider 顺序、特征、实际批次分数/身份及领域合法 Top-20，保存 22 份源码副本。独立加速环境可追加 `--content-backend numpy`。只回放原固定验证请求，不重训、不评价测试目标或创建真实会话。尚缺数据库真实内容快照、活动包发布/恢复、异步队列/取消与租约、推荐 API 注册及端到端性能验收；现有 8000 服务不变。

本轮真实包和双环境干净重放、完整测试及初次失败修正见[验收摘要](../docs/validation/r06-frozen-bundle-20261004.json)。源码基线 `ba2e056`，四组件 15 文件与原件逐字节一致；137,249 件原表示、四个固定参考的完整顺序/批次/Top-20 通过。没有全库向量重编码、真实线上子集质量评价或浏览器验收。

## 冻结商品数据库准备（2026-10-04）

在追加迁移 `0009_r06_catalog_preparation.sql` 后，管理员可调用 `POST /api/v1/admin/r06/bundles/{bundle_id}/prepare`，带 `X-Admin-Token` 和 JSON `{"expected_manifest_sha256":"<已批准的外层 SHA-256>"}`。这会向所配置的数据库导入整个冻结原商品库，并注册为 `runtime_kind=r06-frozen-bundle-v1`、`ready`。**它是显式的数据库写入，不会发布、切换活动模型或重启服务；不能直接拿业务库做验收。** 不支持客户端提供文本、时间或 `activate` 字段。

一笔事务保存原 `r06_model_text`、`r06_first_seen_ms` 和连续有序的成员映射。展示标题只截取原文本前 300 个字符，空文本用原 ID；分类标明 `R06 frozen corpus`，不补造图片或新商品。研究首次出现时间是交互代理，不是商家上架时间；原文本也不具有历史可见时间的额外证明。同 ID 的普通商品或不同表示会令整个准备失败，绝不覆盖已有商品；已下架状态不会被重放恢复。

幂等重放重新加载批准原包并逐件核对**数据库实际**原文本、时间、展示字段和有序成员，不只比对一条保存的哈希。包、路径、登记身份或实际表示漂移都会拒绝。旧活动 demo 保持不变。在线适配尚未接通时，R06 `publish/rollback` 返回 `503 r06_online_not_enabled`，不生成发布操作；手工绕过发布器强行改指针时恢复器关闭入场，不把 R06 当旧 dot-product 模型运行。

只在独立、可创建临时 schema 的 PostgreSQL 中执行记录型验收，先提交实现并确认干净工作树，输出用新的忽略目录：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_r06_catalog_preparation artifacts/replays/my-r06-catalog artifacts/bundles/r06-frozen <bundle-uuid> --expected-manifest-sha256 <已批准外层哈希>
```

入口只创建自己随机命名的临时 schema，迁移、原子准备、进程对象重建后的重放、全部实际表示核对、真实 SQL 修改后的漂移拒绝和未接通发布的拒绝均在该 schema 完成。仅在清理成功后写通过报告，失败保留诊断源码且不覆盖旧输出。报告绑定干净 Git 提交与 32 份关键源码副本，不保存连接串、原商品文本或真实用户信息。强制终止进程可能遗留临时 schema，不能声称具备强杀后的自动清理或断电持久性。

这层还不是可信请求 admission：完整已见集合、请求时间/实际可售内容快照的生产与持久化、异步排序队列/取消/模型租约、完整 R06 发布恢复及推荐接口仍需接通。合成小包测试只验证机制，真实包验证另行记录，不把二者混作模型质量或线上性能证据。

## 有界异步 RankingPort 与取消清退（2026-10-04）

[R06RankingPort](../src/evorec/infrastructure/r06_async.py)已实现异步 `rank(context, command)`，每个实例捕获一个受控冻结包和不可变请求，构造时核对实际商品文本/时间与可售集合。构造包含全量来源检查，未来可信 admission 必须在工作线程内执行，不能在 HTTP 事件循环上调用。这里只有显式 `dense` 的 R06 路径；请求/会话/历史/商品快照或实际输出绑定错配拒绝，不暗中改走 popular。模型身份为完整外层包版本，不随 CPU 后端变化。

同一运行时应复用一个 `R06CPUQueue`，默认 1 个 CPU 工作线程、8 个排队位置（运行与排队合计 9）。线程锁支持 API 与后台工作者的不同事件循环；容量满返回 `429 r06_queue_full`，关闭/不可用返回 `503`，不额外无限排队。组件不会自行创建数据库快照、持久化请求、发布模型或启动服务；为每个请求创建独立队列会绕过全局容量限制，不能这样集成。

排队任务取消后不执行并释放位置；运行中不能杀死 Python 线程，会保留捕获包的强引用，等待实际计算结束，丢弃结果后才确认取消。重复取消、超时及 `aclose()` 都按这个边界清退，故超时确认可能晚于名义截止时间，**不是硬截止或 CPU 可中断承诺**。同步 `close()` 只拒绝新任务/取消排队，不等待运行结束；服务退出应 `await aclose()`。这只证明内存对象租约，不是完整数据库发布、索引文件租约或淘汰协调。

记录型验收从干净实现提交重放原四个固定参考，调用的是真实 `RankingPort.rank`，检查实际批次候选顺序/分数/绑定与合法 Top-20，而不是旁路同步结果。使用新的忽略目录，无需数据库：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_r06_async artifacts/replays/my-r06-async artifacts/bundles/r06-frozen <完整bundle目录> --expected-manifest-sha256 <已批准外层哈希>
```

在独立 NumPy 环境使用同一包和命令，另加 `--content-backend numpy`。报告保存 31 份关键源码副本，失败/中断只撤销自己创建的通过报告，保护旧目录与竞争写入者证据。不重新训练或评价测试目标，不将离线从原包构造的快照冒称实际数据库入场或浏览器端到端验收。

下一层仍需完整已见/时间/真实数据库内容快照生产与持久化、在线后端路由、发布恢复及推荐/比较 API 的模型身份接入；在这些闭环完成前，数据库准备层继续拒绝 R06 发布。
