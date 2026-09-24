# EvoRec

面向动态商品库冷启动问题的推荐研究项目：从统计与内容召回，到候选融合、神经排序和失败诊断。

项目由 **Die4U23** 发起并主导。项目目标与总体方向由作者提出，研究范围、优先级和推进取舍由作者决定；具体方案通过实验迭代。实现、测试与文档整理使用 AI 编程助手辅助，算法结论以仓库中的可复查证据为准。

**已完成：本地持久化推荐演示 + R01–R06 离线实验。** 服务可创建、查询和重置带访问令牌的会话，并通过既有 `Recommend` 用例返回可追溯推荐；配置数据库后，会话、请求和结果写入 PostgreSQL。受控 CPU bundle 可登记、发布与重启恢复，真实 R06 模型仍未接入。R06 完成多兴趣召回、六次排序训练、完整候选与模型重放及 50 项配对区间。

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

先看[文档导航](docs/README.md)：按当前状态、系统设计、研究实验和验证证据分层查找。常用的三个直接入口是[架构与实现过程](docs/10-architecture-and-implementation.md)、[当前进度](docs/STATUS.md)和[实验索引](docs/experiments/README.md)。早期计划与原始 PRD 在导航中保留，但不作为当前完成度的依据。

## 工程布局

```text
docs/                 项目治理、架构、数据、接口、研究与验收
src/evorec/domain/     不可变请求快照与合法推荐规则
src/evorec/application/ 推荐编排、就绪查询与依赖接口
src/evorec/infrastructure/ 外部依赖适配器；包含内存与 PostgreSQL 演示后端
src/evorec/api/        可运行的 FastAPI 状态、会话与推荐接口
src/evorec/bootstrap.py 依赖组装入口
src/evorec/contracts.py 共享输入契约
tests/                契约、应用用例、服务与分层边界检查
db/                   PostgreSQL 正式迁移及保留的早期设计草案
src/evorec/research/   数据采样、统计基线、GPU 序列训练、时间评价与持续报告
research/             实验配置与运行说明
web/                  零构建的本地推荐与管理页面
cpp/                  C++ 性能扩展的接入条件
ops/                  开发运行与部署边界说明
scripts/              契约导出等工程工具
datasets/             本地数据区域，内容不进入版本控制
artifacts/            模型、索引与结果区域，内容不进入版本控制
```

## 运行本地服务演示

服务环境使用 Python 3.12；算法基线使用独立环境。以下操作在项目根目录执行。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe scripts/check_environment.py service
.\.venv\Scripts\python.exe scripts/check_environment.py database
.\.venv\Scripts\python.exe -m uvicorn evorec.api.app:app --host 127.0.0.1 --port 8000
```

如果项目内已有安装完成的 `.venv`，先运行两项环境检查再启动服务。锁文件包含服务测试和 PostgreSQL 所需的 Psycopg binary/pool；驱动存在不代表本机已经安装或启动 PostgreSQL。没有设置 `EVOREC_DATABASE_URL` 时，服务使用进程内后端。需要持久化时，将 `.env.example` 复制为被忽略的 `.env` 并替换密码；管理操作还需设置至少 32 字符的随机 `EVOREC_ADMIN_TOKEN` 和受管目录 `EVOREC_BUNDLE_ROOT`，再把变量载入当前进程。不要提交真实凭据。Linux 对应解释器为 `.venv/bin/python`。首次安装仍需联网取得依赖及构建工具；此版本只在当前 Windows / Python 3.12 环境验证。

- `GET /health/live`：进程存活，返回 200。
- `GET /health/ready`：检查数据库发布屏障和活动受控运行时；仅配置旧演示种子时仍返回 503，发布受控 bundle 且恢复对齐后可以返回 200。
- `GET /api/v1/system`：返回真实版本、阶段与能力状态；配置数据库后 persistence 为 true。
- `POST /api/v1/sessions`：默认创建无历史的新用户会话；传入 `{"profile_id":"sample"}` 创建有 `demo-coop` 初始浏览历史的示例用户。访问令牌只在创建响应中返回。示例用户要求活动商品库包含该演示商品，否则返回 409。
- `GET /api/v1/sessions/{id}`、`POST /api/v1/sessions/{id}/reset`：通过 `X-Session-Token` 查询或重置会话。
- `POST /api/v1/recommendations`：通过同一令牌执行快照绑定、回退、过滤、Top-K 与结果记录；可选 UUID `Idempotency-Key` 对相同输入重放原结果，不同输入返回 409。受控 bundle 的 `dense` 路径是可移植 CPU 基线，不是 R06 研究模型。
- `POST /api/v1/feedback`：校验反馈来自该会话真实返回的商品；按 `event_id` 幂等记录，并在有效状态变化时推进历史版本。
- `GET /app`：本地 Web 页面，选择用户后自动推荐，支持详情、收藏切换、隐藏撤销和重置；`GET /api/v1/items` 与 `GET /api/v1/items/{id}` 读取商品，进程内模式提供 24 件明确标记的虚构演示商品。更新代码后需重启服务，旧会话不迁移。
- `/api/v1/admin/`：使用 `X-Admin-Token` 导入商品、下架、登记受控 bundle、按预期活动版本发布并恢复。未配置令牌时拒绝管理操作。
- `GET /openapi.json`：当前已经实现的接口说明。交互文档入口 `/docs` 的页面资源可能需要网络。

检查与契约导出：

完整测试现在把 PostgreSQL 集成测试视为必需项：先为当前进程设置 `EVOREC_DATABASE_URL`，测试会在该数据库中创建并清理独立的临时 schema；未设置或无法连接时明确失败，不再静默跳过。仅验证服务代码时可运行 `.github/workflows/service-integration.yml` 中列出的轻量测试集合，CI 会启动一次性 PostgreSQL 服务。

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts/export_contracts.py
```

校验本地候选 bundle（产物目录保持忽略，不提交模型文件）：

```powershell
.\.venv\Scripts\python.exe scripts/validate_bundle.py artifacts artifacts/<bundle-uuid>
.\.venv\Scripts\python.exe scripts/load_bundle.py artifacts artifacts/<bundle-uuid>
```

第一条命令只验证清单结构、受管路径、封闭文件集合、SHA-256、商品映射和向量字节契约。第二条再次校验完整性，在资源上限内加载白名单 JSON/float32 CPU 运行时并回放黄金样本；仅运行这两条检查不会切换活动版本。正式管理发布还要求先导入商品、登记 bundle，并持久记录发布操作。格式与边界见[产物区域说明](artifacts/README.md)。

本机 PostgreSQL 已准备好且 `.env` 配置完成时，可以应用并验证迁移：

```powershell
$env:EVOREC_DATABASE_URL = (Get-Content .env | Select-String '^EVOREC_DATABASE_URL=').Line.Split('=', 2)[1]
.\.venv\Scripts\python.exe scripts/migrate_database.py
.\.venv\Scripts\python.exe scripts/migrate_database.py --check
.\.venv\Scripts\python.exe scripts/verify_m11_database.py
.\.venv\Scripts\python.exe scripts/seed_demo_catalog.py
.\.venv\Scripts\python.exe -m pytest tests/test_postgres_api.py
Remove-Item Env:EVOREC_DATABASE_URL
```

迁移按文件校验 SHA-256，并通过 PostgreSQL advisory lock 串行执行；已应用文件发生变化时拒绝继续。验证脚本使用临时 UUID 数据检查版本冲突、请求幂等、非法分数和会话行锁，结束后清理测试数据。演示种子脚本会将演示 bundle 设为活动版本，仅应在初始本地演示时运行；发布真实受控 bundle 后不要再次运行它。受控发布使用 `db/migrations/0002_m23_publication.sql` 的操作记录与接收屏障，重启会核对数据库指针和受管产物再恢复接收。

商品查询、JSON 批次导入、下架、受控 bundle 登记/发布与恢复已注册；没有后台任务队列、文件上传、真实 R06 模型适配或公网身份系统。只在单实例、本地可信环境演示，不能把管理令牌当作公网授权体系。`dense` 路径超过 bundle 排序预算时只使用确定性的有界候选子集，并明确标记 `candidate_budget_truncated`；不据此宣称全库在线检索质量。

## 下一项工作

R06 已完成并保留未获支持的结果。研究上先解释无历史冷目标不可达与候选增益未转化为排名增益的原因，再登记后续实验；工程上下一步是接入真实冻结模型、完善后台任务/租约、运行时版本回收、管理员身份与公网安全，以及真正的前端构建与部署。每阶段继续区分实现、测试和线上证据。

实验运行入口见 [研究工作区](research/README.md)；本机测试临时目录权限的处理方式也记录在该页。

## 仓库范围

仓库维护项目源码、研究配置、测试、实验报告与结果图表。博客和求职材料在本地单独维护，不参与版本发布。[文件卫生与版本约定](docs/08-repository-policy.md)约束每次提交。
