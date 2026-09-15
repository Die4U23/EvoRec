# EvoRec

动态商品库推荐研究与演示系统。

**当前版本：M0 工程框架与分层架构。** 已建立项目文档、数据契约、最小 API，以及可独立检查的推荐应用用例。推荐模型、业务数据库、商品发布、前端页面和性能优化按后续里程碑接入。当前不会返回模拟推荐结果或虚构实验指标。

## 从这里开始

| 想了解什么 | 入口 |
| --- | --- |
| 完整项目如何推进 | [项目框架与交付路线](docs/01-project-framework.md) |
| 模块怎样连接 | [系统架构](docs/02-architecture.md)、[架构决策](docs/architecture/decisions.md) |
| 数据与接口如何约定 | [数据设计](docs/03-data-design.md)、[接口约定](docs/04-api-contract.md) |
| 算法怎样研究和评价 | [研究协议](docs/05-research-protocol.md) |
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
research/             算法研究模块边界与实验配置模板
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

执行交付计划中的 **R01 数据与基线可行性验证**：确定一个数据子集、时间协议与可运行基线，记录样本数、资源使用和结果，再进入推荐闭环开发。
