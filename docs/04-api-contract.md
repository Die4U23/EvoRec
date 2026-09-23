# 接口约定

## 1. 当前可运行接口

| 方法与路径 | 行为 | 状态 |
| --- | --- | --- |
| GET /health/live | 报告 API 进程存活 | 已实现；200 |
| GET /health/ready | 通过可注入探针报告推荐业务依赖是否就绪 | 已实现；默认未配置依赖，返回 503；契约支持就绪时 200 |
| GET /api/v1/system | 版本、阶段、真实能力状态 | 已实现；200 |

当前 OpenAPI 由应用导出到 `docs/contracts/openapi.json`。会话创建、查询、重置和推荐已经注册；配置 `EVOREC_DATABASE_URL` 后使用 PostgreSQL，否则使用进程内后端。下方其余业务接口仍是设计，调用未注册路径返回 404。

## 2. 目标业务接口

| 方法与路径 | 输入/输出要点 | 里程碑 |
| --- | --- | --- |
| POST /api/v1/sessions | 新会话 → ID、epoch、历史版本、仅返回一次的访问令牌；已实现 | M1 |
| GET /api/v1/sessions/{id} | `X-Session-Token` → 会话摘要与状态；已实现 | M1 |
| POST /api/v1/sessions/{id}/reset | `X-Session-Token` → 增加 epoch 和历史版本并清空状态；已实现 | M1 |
| POST /api/v1/recommendations | `X-Session-Token` + 推荐输入 → 请求 ID、版本、实际策略、商品；已实现持久化热门回退演示 | M1 |
| GET /api/v1/items/{id} | 商品详情与有效状态 | M1 |
| POST /api/v1/feedback | `X-Session-Token` + 反馈输入 → 原事件结果、最新历史版本；已实现 | M12 |
| POST /api/v1/admin/catalog/imports | 文件或统一 JSON 批次 → 任务 ID | M2 |
| GET /api/v1/admin/catalog/imports/{id} | 进度、错误与产物状态 | M2 |
| POST /api/v1/admin/catalog/imports/{id}/retry | 校验原内容后幂等重试 | M2 |
| POST /api/v1/admin/bundles/{id}/publish | 带当前活动版本的发布请求 | M2 |
| POST /api/v1/admin/items/{id}/deactivate | 更新有效状态并确认过滤生效 | M2 |
| POST /api/v1/admin/bundles/{id}/rollback | 恢复兼容版本，保留当前下架名单 | M2 |
| POST /api/v1/admin/comparisons | 固定输入和策略集合 → 后台任务 | M3 |
| GET /api/v1/admin/runs/{id} | 配置、指标、样本数、来源与结果引用 | M4 |
| GET /api/v1/admin/jobs/{id} | 任务状态与错误 | M2 |

业务响应尚待实现时，不返回 200 空数组或 202 假任务。实现时必须在 OpenAPI 中声明成功结构、错误结构和权限依赖。

## 3. 共享输入契约

`src/evorec/contracts.py` 定义推荐、反馈与 JSON 批次输入。其 JSON Schema 由同一代码导出到 `docs/contracts/`，避免手工维护两套字段。

推荐输入包含 session_id、expected_history_version、strategy、k；k 默认为 10，当前允许 1-50。策略名称不代表已经接入对应模型。创建会话后必须保存访问令牌，后续会话和推荐请求通过 `X-Session-Token` 提交；数据库只保存其 SHA-256 摘要。该令牌是当前本地演示的最小访问边界，不替代公网部署所需的完整身份系统。

反馈包含 event_id、session_id、request_id、item_id、kind、observed_at。状态设置事件要求 desired_state；曝光要求 visible_ratio 和 visible_duration_ms。未知字段拒绝，时间必须带时区。同一 `event_id` 同一规范化内容返回原历史版本并标记 `replayed=true`；同 ID 不同内容返回 409。反馈必须关联该会话当前 epoch 中真实返回的商品。

发布采用预期活动版本比较，避免两个管理操作覆盖彼此。匹配失败返回冲突，调用方重新读取状态后决定重试。单批大小、权限和商品存在性由接口与业务层检查。

## 4. 状态码与错误

| 状态码 | 语义 |
| --- | --- |
| 200 / 201 | 已完成读取或创建；返回可追溯对象 |
| 202 | 任务已持久记录并可查询，尚未完成 |
| 401 / 403 | 缺少身份或不具备权限 |
| 404 | 对象不存在，或当前版本未注册接口 |
| 409 | 历史、epoch、幂等内容或发布版本冲突 |
| 413 | 上传体积超限 |
| 422 | 输入字段、格式或语义校验失败 |
| 429 | 接收队列或请求配额达到上限 |
| 503 | 业务依赖未就绪、不可用或无可用回退 |
| 504 | 推荐超过截止时间且未能完成有效回退 |

业务错误统一目标格式为 `{error: {code, message, retryable, request_id}}`；request_id 在请求创建之前可以为空。会话不存在、历史冲突和推荐超时已经使用该结构，反馈与管理错误仍待实现。

## 5. 权限与运行范围

服务默认仅监听 127.0.0.1。当前会话令牌只提供对象级访问边界，尚无用户身份、令牌轮换、速率限制或传输层部署配置，因此不可公开部署。M2 管理接口上线之前接入管理员身份和授权检查，管理员路径命名本身不是权限控制。

普通展示页面通过服务获取允许展示的结果，不接收数据库凭据、内部模型路径或原始用户数据。标题与描述按纯文本渲染。公网发布需另行完成身份、传输、日志和运行配置检查。
