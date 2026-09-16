# EvoRec

面向动态商品库冷启动问题的推荐研究项目：从统计与内容召回，到候选融合、神经排序和失败诊断。

**已完成：M0 状态服务 + R01–R05 离线实验。** R05 排序器完成 4 次 GPU 训练、35 轮及独立审计；在线推荐服务尚未接入。

[最新实验报告](docs/experiments/r05-ranker/report.html) · [模型原理与运行](research/R05-ranker-guide.md) · [持续博客](docs/blog/evorec-project-log.md) · [实习指导评审与后续路线](docs/reviews/2026-09-16-internship-roadmap.md)

## 当前结果

R05 在同一批测试请求上比较如下；完整实验规则和数据边界见[正式报告](docs/experiments/r05-ranker/report.md)。

| 方法 | 全部请求 NDCG@10（12,720） | 有历史请求 NDCG@10（5,214） | 冷目标 Top20 命中 / 7,088 |
| --- | ---: | ---: | ---: |
| CF-blend | 0.007188 | 0.005291 | 0 |
| RRF | 0.006537 | 0.003702 | 6 |
| ListMLP-s17 | 0.009426 | 0.010751 | 11 |
| **ColdListMLP-s17（验证规则所选）** | **0.009119** | **0.010002** | **26** |

- **排序收益：** 冷加权模型相对 CF-blend 的整体 NDCG 观测差值为 +26.9%；相对同候选池 RRF 为 +39.5%。CF-blend 是完整路径对照，RRF 是固定候选池的排序对照。
- **人群差异：** 7,506 个无正反馈历史请求使用相同回退列表。全部请求指标保留为原实验主口径，有历史分组用于补充解释。
- **主要瓶颈：** 只有 127 / 7,088 个冷目标进入候选并集（1.79%）；下一步重点是候选覆盖与排序适配。

以上为离线观测结果，冷加权目前只有 seed 17，尚无显著性结论。普通 ListMLP 的三种子 NDCG 为 0.009546 ± 0.000315，标准差不属于冷加权模型。静态商品元数据缺少历史版本；各轮用户样本不同，跨轮数字不直接解释为提升。[本次指标复核](docs/validation/internship-guidance-checks.json)

## 研究过程

| 阶段 | 研究问题与结果 | 证据 |
| --- | --- | --- |
| R01 | 跑通时间回放与统计基线 | [报告](docs/experiments/r01-feasibility-report.md) |
| R02 | SASRec-style 未超过 CF-blend，保留负结果 | [报告与训练曲线](docs/experiments/training-report.html) |
| R03 | 内容路径打通冷商品入口，融合改善整体排序但损失部分冷命中 | [报告](docs/experiments/r03-content/report.html) |
| R04 | 冷商品保留和规则门控未获验证集支持，定位落选环节 | [报告](docs/experiments/r04-gating/report.html)、[失败定位](docs/experiments/r04-gating/error-analysis.md) |
| R05 | 滚动模拟冷商品训练与残差 listwise 排序 | [报告](docs/experiments/r05-ranker/report.html)、[逐请求审计](docs/validation/ranker-checks.json) |

公开仓库包含代码、配置、汇总报告和图表。原始数据、模型及用户级轨迹留在本地忽略目录；首次复现需按[研究说明](research/README.md)准备数据与训练。当前研究环境复用了系统包，尚无干净环境或 Linux 复建证据。

## 从这里开始

| 想了解什么 | 入口 |
| --- | --- |
| 完整项目如何推进 | [项目框架与交付路线](docs/01-project-framework.md) |
| 模块怎样连接 | [系统架构](docs/02-architecture.md)、[架构决策](docs/architecture/decisions.md) |
| 数据与接口如何约定 | [数据设计](docs/03-data-design.md)、[接口约定](docs/04-api-contract.md) |
| 算法怎样研究和评价 | [研究协议](docs/05-research-protocol.md)、[R01 实验报告](docs/experiments/r01-feasibility-report.md) |
| 任务依赖与需求覆盖 | [交付计划](docs/06-delivery-plan.md) |
| 怎样证明可以发布 | [验证与发布](docs/07-validation-release.md) |
| 当前完成到哪里 | [进度与验证记录](docs/STATUS.md) |
| 持续阅读项目实践 | [EvoRec 项目实录](docs/blog/evorec-project-log.md)、[更新模板](docs/blog/update-template.md) |
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

R05 已实现并训练候选内排序。下一项研究优先复验冷加权模型的种子波动，并提升冷商品候选覆盖；新的调参或方法选择需登记新留出协议，已查看的测试结果保留为阶段证据。

实验运行入口见 [研究工作区](research/README.md)；本机测试临时目录权限的处理方式也记录在该页。
