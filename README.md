# EvoRec

面向动态商品库冷启动问题的推荐研究项目：从统计与内容召回，到候选融合、神经排序和失败诊断。

项目由 **Die4U23** 发起并主导。项目目标与总体方向由作者提出，研究范围、优先级和推进取舍由作者决定；具体方案通过实验迭代。实现、测试与文档整理使用 AI 编程助手辅助，算法结论以仓库中的可复查证据为准。

**已完成：M0 状态服务 + R01–R06 离线实验。** R06 完成多兴趣召回、六次排序训练、完整候选与模型重放及 50 项配对区间；在线推荐服务尚未接入。

[R06 图表报告](docs/experiments/r06-multi-interest/report.html) · [运行与复核](research/R06-multi-interest-guide.md) · [预登记协议](docs/experiments/r06-multi-interest-protocol.md)

## 当前结果

R06 用新的用户分组比较同候选数量预算下的均值内容召回与近期多兴趣召回。测试共 12,524 个正反馈请求、9,373 名用户；与 R01–R05 用户交集为零。验证规则保留 **A-frozen-s17**，本轮没有证据支持替换原路径。

| 方法族（三个种子） | 内容召回 | 排序器 | 整体 NDCG@10 均值 ± 样本标准差 | 冷 Recall@20 均值 |
| --- | --- | --- | ---: | ---: |
| A-frozen | 均值 | 冻结 R05 | 0.010074 ± 0.000129 | 0.003774 |
| B-frozen | 均值＋多兴趣 | 同一组冻结模型 | 0.010040 ± 0.000117 | 0.003917 |
| C-adapted | 均值＋多兴趣 | 重新训练 | 0.009820 ± 0.000216 | 0.003678 |
| D-adapted | 均值 | 同阶段重训练对照 | 0.010155 ± 0.000027 | 0.003583 |

- **覆盖有小幅变化：** 冷目标进入候选池由 115 / 6,978 增至 122 / 6,978；无历史冷目标仍为 0 / 3,928。
- **排序收益未获支持：** B-A 的整体 NDCG 差值为 −0.000033769，95% 边际区间 [−0.000260567, +0.000184014]；匹配重训练 C-D 为 −0.000335551，[−0.000686012, +0.000000421]。两者均跨过零。
- **计算有成本：** 测试批次的均值内容路径用时 7.865 秒，新增多兴趣计算另用 12.469 秒。候选数量相同不代表计算量相同；这不是线上延迟测量。

共完成 6 次训练、74 轮。全部候选来源、特征和模型输出重新计算核对；50 项预登记区间独立复算一致。区间按用户聚类重采样 10,000 次，条件于固定检查点，不含训练随机性且未做多重比较校正；三个种子的指标平均不是集成推荐。

R06 测试现已查看，后续调参需要新协议。静态商品元数据缺少历史版本，仍无线上收益证据。R05 的[原始报告](docs/experiments/r05-ranker/report.md)与[三种子复验](docs/experiments/r05-cold-replication/report.md)保留，但不同用户样本的指标不能直接当作跨轮提升。

## 研究过程

| 阶段 | 研究问题与结果 | 证据 |
| --- | --- | --- |
| R01 | 跑通时间回放与统计基线 | [报告](docs/experiments/r01-feasibility-report.md) |
| R02 | SASRec-style 未超过 CF-blend，保留负结果 | [报告与训练曲线](docs/experiments/training-report.html) |
| R03 | 内容路径打通冷商品入口，融合改善整体排序但损失部分冷命中 | [报告](docs/experiments/r03-content/report.html) |
| R04 | 冷商品保留和规则门控未获验证集支持，定位落选环节 | [报告](docs/experiments/r04-gating/report.html)、[失败定位](docs/experiments/r04-gating/error-analysis.md) |
| R05 | 滚动模拟冷商品训练与残差 listwise 排序 | [报告](docs/experiments/r05-ranker/report.html)、[逐请求审计](docs/validation/ranker-checks.json) |
| R05 复验 | 重放 seed 17、补齐 seed 29/43，并计算 48 项用户聚类区间 | [报告](docs/experiments/r05-cold-replication/report.html)、[复验指南](research/R05-replication-guide.md) |
| R06 | 同预算多兴趣召回与匹配重训练，覆盖略增但未支持替换原路径 | [报告](docs/experiments/r06-multi-interest/report.html)、[完整复核](docs/validation/r06-artifacts-checks.json) |

公开仓库包含代码、配置、汇总报告和图表。原始数据、模型及用户级轨迹留在本地忽略目录；首次复现需按[研究说明](research/README.md)准备数据与训练。当前研究环境复用了系统包，尚无干净环境或 Linux 复建证据。

## 从这里开始

| 想了解什么 | 入口 |
| --- | --- |
| 整体架构与实现过程 | [从数据、模型到服务的完整讲解](docs/10-architecture-and-implementation.md) |
| 完整项目如何推进 | [项目框架与交付路线](docs/01-project-framework.md) |
| 模块怎样连接 | [系统架构](docs/02-architecture.md)、[架构决策](docs/architecture/decisions.md) |
| 数据与接口如何约定 | [数据设计](docs/03-data-design.md)、[接口约定](docs/04-api-contract.md) |
| 算法怎样研究和评价 | [研究协议](docs/05-research-protocol.md)、[R01 实验报告](docs/experiments/r01-feasibility-report.md) |
| 任务依赖与需求覆盖 | [交付计划](docs/06-delivery-plan.md) |
| 怎样证明可以发布 | [验证与发布](docs/07-validation-release.md) |
| 当前完成到哪里 | [进度与验证记录](docs/STATUS.md) |
| 原始产品方案 | [展示版 PRD](EvoRec_展示版PRD.md) |
| 为什么选择这些技术 | [技术栈选型报告](EvoRec_技术栈选型报告.md) |

## 工程布局

```text
docs/                 项目治理、架构、数据、接口、研究与验收
src/evorec/domain/     不可变请求快照与合法推荐规则
src/evorec/application/ 推荐编排、就绪查询与依赖接口
src/evorec/infrastructure/ 外部依赖适配器；目前仅就绪阻塞状态
src/evorec/api/        可运行的 FastAPI 状态接口
src/evorec/bootstrap.py 依赖组装入口
src/evorec/contracts.py 共享输入契约
tests/                契约、应用用例、服务与分层边界检查
db/                   PostgreSQL 设计草案，尚未应用
src/evorec/research/   数据采样、统计基线、GPU 序列训练、时间评价与持续报告
research/             实验配置与运行说明
web/                  前端页面与状态规划，尚未实现
cpp/                  C++ 性能扩展的接入条件
ops/                  开发运行与部署边界说明
scripts/              契约导出等工程工具
datasets/             本地数据区域，内容不进入版本控制
artifacts/            模型、索引与结果区域，内容不进入版本控制
```

## 运行 M0 服务

服务环境使用 Python 3.12；算法基线使用独立环境。以下操作在项目根目录执行。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m uvicorn evorec.api.app:app --host 127.0.0.1 --port 8000
```

如果项目内已有安装完成的 `.venv`，直接执行最后一行。Linux 对应解释器为 `.venv/bin/python`。锁文件固定运行及测试依赖；首次安装仍需联网取得依赖及构建工具。此版本只在当前 Windows / Python 3.12 环境验证，Linux 部署仍需执行相同检查。

- `GET /health/live`：进程存活，返回 200。
- `GET /health/ready`：推荐业务尚未接入，返回 503 并列明阻塞项。
- `GET /api/v1/system`：返回真实版本、阶段与能力状态。
- `GET /openapi.json`：当前已经实现的接口说明。交互文档入口 `/docs` 的页面资源可能需要网络。

检查与契约导出：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts/export_contracts.py
```

业务接口设计见文档；未注册的接口返回 404。存活检查通过不代表模型或数据库已经可用。当前不启动数据库、不下载模型、不公开部署服务。

## 下一项工作

R06 已完成并保留未获支持的结果。研究上先解释无历史冷目标不可达与候选增益未转化为排名增益的原因，再登记后续实验；工程上按[建议评审与验收顺序](docs/09-engineering-follow-up.md)推进最小持久化闭环及 bundle 校验。每阶段分别提交协议、实现和结果，独立开发使用 codex/ 分支；这些后续模块尚未实现。

实验运行入口见 [研究工作区](research/README.md)；本机测试临时目录权限的处理方式也记录在该页。

## 仓库范围

仓库维护项目源码、研究配置、测试、实验报告与结果图表。博客和求职材料在本地单独维护，不参与版本发布。[文件卫生与版本约定](docs/08-repository-policy.md)约束每次提交。
