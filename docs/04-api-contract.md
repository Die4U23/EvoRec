# 接口约定

## 1. 当前可运行接口

| 方法与路径 | 行为 | 状态 |
| --- | --- | --- |
| GET /health/live | 报告 API 进程存活 | 已实现；200 |
| GET /health/ready | 通过可注入探针报告推荐业务依赖是否就绪 | 已实现；默认未配置依赖，返回 503；契约支持就绪时 200 |
| GET /api/v1/system | 版本、阶段、真实能力状态 | 已实现；200 |

当前 OpenAPI 由应用导出到 `docs/contracts/openapi.json`。会话、推荐、反馈、商品读取及下表注明“已实现”的管理接口已注册；配置 `EVOREC_DATABASE_URL` 后使用 PostgreSQL，否则使用进程内后端。`/app` 是本地页面入口。未注明已实现的接口仍是设计，调用未注册路径返回 404。

## 2. 目标业务接口

| 方法与路径 | 输入/输出要点 | 里程碑 |
| --- | --- | --- |
| POST /api/v1/sessions | 新会话 → ID、epoch、历史版本、仅返回一次的访问令牌；已实现 | M1 |
| GET /api/v1/sessions/{id} | `X-Session-Token` → 会话摘要与状态；已实现 | M1 |
| POST /api/v1/sessions/{id}/reset | `X-Session-Token` → 增加 epoch 和历史版本并清空状态；已实现 | M1 |
| POST /api/v1/recommendations | `X-Session-Token` + 推荐输入；可选 UUID `Idempotency-Key` 重放原结果；已实现 | M1/M23 |
| POST /api/v1/strategy-comparisons/preview | `X-Session-Token` + 会话版本、2–3 个不同的 `popular`/`dense`/`adaptive` 策略及 `k≤10`；同一只读快照下返回 Top-K、交集、独有商品、实际策略、回退原因和各路径耗时；不保存任务或推荐记录；已实现预览子集 | V0.3/M31 |
| POST /api/v1/strategy-comparisons | 同预览输入；可选 UUID `Idempotency-Key`；在当前请求中执行并原子保存完整对比，返回 `completed`、`persisted=true` 及输入快照；需 PostgreSQL | V0.3/F08 子集 |
| GET /api/v1/strategy-comparisons | `session_id` + `X-Session-Token`；按保存时间倒序分页查询本会话摘要（相同时间按 ID 倒序）；`offset` 默认 0、最大 10000，`limit` 默认 20、最大 50，含 `has_more` | V0.3/F08 子集 |
| GET /api/v1/strategy-comparisons/{id} | `session_id` 查询参数 + `X-Session-Token`，取回本会话已保存的历史快照与结果；不依赖当前活动商品库或运行时 | V0.3/F08 子集 |
| POST /api/v1/strategy-comparison-jobs | 同预览输入和可选 UUID `Idempotency-Key`，持久冻结输入并返回 202；独立 worker 执行单次对比，同键同输入重放现有状态 | V0.3/F08 子集 |
| GET /api/v1/strategy-comparison-jobs/{id} | `session_id` + `X-Session-Token`，查询本会话任务状态、已完成策略数、尝试数与错误码；完成后给出 `comparison_id` | V0.3/F08 子集 |
| POST /api/v1/strategy-comparison-jobs/{id}/cancel | `session_id` + `X-Session-Token`；排队任务立即取消，运行任务先进入 `cancelling`，当前路径退出后确认 `cancelled` | V0.3/F08 子集 |
| GET /api/v1/items、GET /api/v1/items/{id} | 商品列表/详情及有效状态；列表支持 `offset`（默认 0）与 `limit`（默认 100，最大 100），按商品 ID 排序，空列表表示末页；已实现，需 PostgreSQL | M23 |
| POST /api/v1/feedback | `X-Session-Token` + 反馈输入 → 原事件结果、最新历史版本；已实现 | M12 |
| POST /api/v1/admin/catalog/imports | JSON 批次按 `batch_id` 幂等导入；已有商品 ID 默认整批拒绝；已实现 | M23/V0.2 |
| POST /api/v1/admin/catalog/file-imports | 上传 CSV/JSON 文件并整批校验、导入；已实现 F05 子集，尚不生成内容索引或自动发布 | V0.2 |
| POST /api/v1/admin/catalog/file-import-jobs | 上传受限 CSV/JSON 文件并持久排队；同批次同文件返回已有状态，不同文件返回 409；`queued`/`validating` 返回 202，终态重放返回 200 | V0.2 |
| GET /api/v1/admin/catalog/file-import-jobs/{batch_id} | 管理员查询待校验、校验中、已导入或失败状态，含尝试次数、错误码与逐行错误；失败无部分入库 | V0.2 |
| POST /api/v1/admin/catalog/imports/{batch_id}/builds | 提交 `build_id`，固定当前版本和商品元数据快照，同步生成受控内容基线；同 ID 重试保留快照 | V0.2 |
| POST /api/v1/admin/catalog/imports/{batch_id}/build-jobs | 提交 `build_id` 并冻结快照；排队/处理中返回 202，已完成的同 ID 重放返回 200；失败后同 ID 重排，由独立 worker 构建 | V0.2 |
| GET /api/v1/admin/catalog/builds/{build_id} | 查询持久处理数量、失败原因、尝试次数和发布状态 | V0.2 |
| GET /api/v1/admin/catalog/builds/{build_id}/items | 分页预览完整候选版本的商品及当前可推荐状态 | V0.2 |
| POST /api/v1/admin/catalog/builds/{build_id}/publish | 提交 `operation_id`，按构建时固定的基础版本确认发布 | V0.2 |
| GET /api/v1/admin/catalog/imports/{id} | 查询已导入数量、时间、快照可用性和最近一次构建状态；已实现，管理员权限 | V0.2 |
| POST /api/v1/admin/catalog/imports/{id}/retry | 独立导入重试接口未实现；当前以原批次 ID 和原内容重发导入请求来幂等对账 | M2 |
| POST /api/v1/admin/bundles/{id}/register | 从固定受管根读取并验证白名单 CPU bundle；已实现 | M23 |
| GET /api/v1/admin/publication、POST /api/v1/admin/publication/recover | 读取持久发布状态、显式恢复；已实现 | M23 |
| POST /api/v1/admin/bundles/{id}/publish | `operation_id` + 预期活动版本；持久状态机与运行时对齐；已实现 | M23 |
| POST /api/v1/admin/items/{id}/deactivate | 事务更新有效状态与排除版本；已实现 | M23 |
| POST /api/v1/admin/bundles/{id}/rollback | `operation_id` + 预期活动版本；仅允许恢复曾成功发布的旧版本，重用受控切换并保留当前下架名单；已实现 | M2 |
| POST /api/v1/admin/comparisons | 固定输入和策略集合 → 后台任务 | M3 |
| GET /api/v1/admin/runs/{id} | 配置、指标、样本数、来源与结果引用 | M4 |
| GET /api/v1/admin/jobs/{id} | 任务状态与错误 | M2 |

业务响应尚待实现时，不返回 200 空数组或 202 假任务。实现时必须在 OpenAPI 中声明成功结构、错误结构和权限依赖。

## 3. 共享输入契约

`src/evorec/contracts.py` 定义推荐、反馈与 JSON 批次输入。其 JSON Schema 由同一代码导出到 `docs/contracts/`，避免手工维护两套字段。

推荐输入包含 session_id、expected_history_version、strategy、k；k 默认为 10，当前允许 1-50。策略名称不代表已经接入对应研究模型。创建会话后必须保存访问令牌，后续会话和推荐请求通过 `X-Session-Token` 提交；数据库只保存其 SHA-256 摘要。可选 `Idempotency-Key` 为 UUID：相同键、会话及语义输入返回原完成结果；仍在执行时返回可重试 409；不同输入或已失败的键返回 409。未提供时由服务生成请求 ID，不支持客户端重放。该令牌是当前本地演示的最小访问边界，不替代公网部署所需的完整身份系统。

策略预览使用同一时点的会话历史、隐藏名单、活动 bundle 与有效商品集合，对每个策略执行相同的合法性过滤；`comparison_id` 仅标识本次响应，`persisted=false` 表示结果不可重查、不可当作 F08 已保存的对比任务。`dense` 在运行时未加载时会明确报告回退到 `popular`，自适应路由当前也可能实际选择 `popular`；界面展示真实执行路径，不将两次热门排序伪称为模型差异。尚未接入 `generative`、`hybrid`，也没有批量评估、正式耗时指标或策略收益结论。

保存入口与预览共用同一执行和过滤逻辑，但只在 PostgreSQL 中保存完整结果及当时的历史、隐藏/收藏名单、有效商品集合、商品库版本、采样时间和 K。成功响应表示结果已提交；同一幂等键及输入返回原始结果（包括耗时），即使随后重置会话、下架商品或重启服务，也不重新计算。键对应不同会话、历史版本、策略顺序或 K 时返回 409。并发同键请求可能各执行一次，但事务只保留一个完整结果，两次响应均返回最终保存的版本；保存前中断不会留下半条记录，保存后响应丢失可用原键或 GET 对账。已保存的商品资格是历史快照，不能直接用于当前推荐或反馈。未提供键时由服务生成编号，客户端无法在响应丢失后定位该请求。同步入口仍保留兼容。

后台入口提交时冻结会话/商品快照、K、策略顺序、时间和当时已加载的 CPU runtime 清单指纹，不保存访问令牌。任务编号与完成后的对比编号相同，不能用同步入口抢占后台任务的编号；反向抢占也返回 409。每会话至多 10 个非终态任务，数据库队列至多 1000 个，达到上限返回 429。`python -m scripts.comparison_worker` 独立执行（也支持 `--once`）；没有 worker 时保持 `queued`。进度是已完成的策略条数，不是预计耗时百分比。完成时原子保存完整结果并更新 `completed`；失败/取消不保存部分策略结果，正常失败须显式新建任务，同键不会默默重算。

worker 每 5 秒续期 30 秒租约；崩溃后租约过期可按原输入重新领取，最多 3 次尝试。领取代数、owner 和租约有效期一起约束进度及提交；session advisory lock 保持到当前 CPU 计算真正退出，防止仍存活的执行者因心跳延迟被重复领取。运行取消先显示 `cancelling`，在策略边界或最终提交前确认，取消先于完成提交时不保存结果；完成先于取消时保留原结果。已加载的冻结 runtime 缺失或指纹变化时任务失败，不偷换成新 bundle 或无提示热门回退。上述范围仅为本机受控 CPU 单次对比，不证明数据库连接断开后的远端资源终止、GPU 强制取消、多机器高可用、后台批量样本评估或算法质量达标。

反馈包含 event_id、session_id、request_id、item_id、kind、observed_at。状态设置事件要求 desired_state；客户端曝光要求 visible_ratio 和 visible_duration_ms。未知字段拒绝，时间必须带时区。同一 `event_id` 同一规范化内容返回原历史版本并标记 `replayed=true`；同 ID 不同内容返回 409。反馈必须关联该会话当前 epoch 中真实返回的商品。详情事件响应额外返回确定性的 `exposure_event_id`；该派生曝光在同一事务中补记，并明确标为点击推断。

发布采用预期活动版本比较，避免两个管理操作覆盖彼此。匹配失败返回冲突，调用方重新读取状态后决定重试。单批大小、权限和商品存在性由接口与业务层检查。

回滚要求非空的 `expected_active_bundle_id`，目标须与之不同且曾成功发布。它重用发布状态机，重新校验受管产物与成员，保留数据库中当前商品下架状态；活动版本在请求前后发生变化则拒绝覆盖。回滚不会恢复已删除或损坏的产物。

文件导入使用原始请求体（不是 multipart）：请求头 `X-Admin-Token`、UUID `X-Batch-Id`，`Content-Type` 为 `text/csv` 或 `application/json`。CSV 首行必须包含 `item_id,title,category`，可选 `description,image_url`；JSON 为商品对象数组。单文件至多 20 MB（20,000,000 字节）、1–1000 件。字段错误返回 422，文件过大返回 413，已有 ID 或批次内容冲突返回 409；逐行错误位于响应的 `rows` 数组。相同批次与规范化内容返回 `replayed=true`，任何校验或冲突失败都不写入部分商品。导入成功仅写入管理商品表；进入推荐候选仍需后续内容处理与发布，不能把导入成功当作发布成功。

导入成功后可按批次 ID 查询持久导入记录与最近一次构建。尚无构建时 `latest_build=null`；旧迁移前的导入若缺少商品快照，`snapshot_available=false`，不会假装它可以构建。校验失败的文件没有成功导入记录，查询返回 404；网络响应丢失时先查询，若不存在则用原批次 ID 和原文件重发。

处理接口要求先完成导入，并配置 `EVOREC_BUNDLE_ROOT`。旧 `/builds` 在当前 HTTP 请求中同步运行；新 `/build-jobs` 先保存 `queued` 状态及固定快照并立即返回 202，独立运行的 `python -m scripts.catalog_worker` 读取任务、更新进度。worker 中断后，重启时将该任务重新排队并按原快照重试；正常构建失败则标为 `failed`，同一 `build_id` 再次提交可重排。旧同步构建中断仍标记为失败并需显式重试。一个构建的完整商品集合是处理开始时的活动版本商品与该批新商品的并集，最多 5,000 件；其他尚未确认的导入不会混入。预览默认每页 50 件、最多 100 件。确认发布以处理时捕获的活动版本为预期版本，版本已变化则返回 409 并要求新建构建。发布重用受控加载、黄金样本检查和持久切换；下架排除状态持续生效。旧批次若在本迁移前导入、未保存商品快照，不会猜测其成员，处理返回 `legacy_import_without_snapshot`。当前 worker 使用数据库 advisory lock 串行执行，不提供租约续期、分布式调度或大规模检索保障。

当前内容基线只对标题和类别做可复现的特征哈希与余弦匹配，生成确定性的内容分组编码和精确遍历的小规模索引。它没有训练权重，也没有接入 R06 模型；编码碰撞、词义理解和大规模检索质量仍需单独验证。构建产物保存商品快照、构建及运行路径的源码副本、Git 修订与脏工作区标记，模型文件不提交仓库。

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

业务错误使用 `{error: {code, message, retryable, request_id}}`；request_id 在请求创建之前可以为空。管理错误不暴露受管产物绝对路径；校验错误会保留稳定错误码。

## 5. 权限与运行范围

服务默认仅监听 127.0.0.1。当前会话令牌只提供对象级访问边界；管理接口使用环境变量中的至少 32 字符 `EVOREC_ADMIN_TOKEN`，缺失时拒绝管理操作。它不替代完整管理员身份、令牌轮换、速率限制或传输层部署配置，因此不可公开部署。管理员路径命名本身不是权限控制。

普通展示页面通过服务获取允许展示的结果，不接收数据库凭据、内部模型路径或原始用户数据。标题与描述按纯文本渲染。公网发布需另行完成身份、传输、日志和运行配置检查。
