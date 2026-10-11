# 完整 R06 浏览器及停机备份恢复门禁预登记

依赖草稿 PR #64；新分支 `codex/r06-browser-backup-gates`，基线 `d6e12c314ceedeb756e37be008c0f3c7401f78e4`。本轮只补最终验收，不修改生产 src/db、2 秒期限、目录/快照校验、排名单 worker 或租约清退。既有两轮 120 笔及历史失败保持原样，不追加负载或以新结果改写旧失败。

本协议先提交推送，再构造临时样本和执行。正式场景固定干净提交，记录 src/scripts/db/web 字节摘要、模型身份、环境、实际状态和全部失败；辅助工具及原始证据放忽略目录并单独保存摘要。主工作区、业务 schema/8000、.env、原批准模型、控制工作树不操作。

## 固定环境和顺序

完整批准包 bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec，137,249 商品，NumPy；manifest 9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6，model_version eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc。先复制完整批准包到本轮独占 artifacts 子目录，逐文件核对摘要，再用已有 R06ServiceLab 创建唯一自有 test_evorec schema、独立 API 进程和非 8000 loopback 端口。关闭服务端插桩、GC 观察、phase gate、管理员和后台 worker；原 API 期限 2 秒、worker=1、queue=8。

同一实际浏览器标签只由主线程操作。使用仅绑定 loopback 的受控代理传递原页面和真实 HTTP，不注入页面脚本、不伪造服务响应；代理仅在一个新 dense 请求真实完成后暂缓其响应，其他请求继续转发。该场景有观测/延迟干预，不与未插桩性能测量混用。请求键、令牌及响应只留本轮内存；原始统计只输出固定标签、状态、数字和布尔值。

1. 浏览器选择 sample，首次自动 dense/k=10 成功；核对实际策略、包/模型、商品详情、收藏、隐藏/撤销、反馈后新 dense 排除已见商品。用独立只读数据库观察请求/结果绑定、唯一 completed、顺序/分数及租约释放。
2. 只暂缓一笔已完成的反馈后 dense 响应；在实际重置前按 UI 安全规则确认本轮临时会话重置。重置须 epoch 增一、历史回到固定训练种子、收藏/隐藏清空；首次自动新 dense 成功且恢复初始 Top-10。随后释放旧 epoch 响应，页面不得覆盖新结果/会话，旧请求持久 completed 不受重置或失败清理影响。刷新页面后保持新会话版本和恢复结果。
3. 正常停止 API 并确认 CPU drain，禁止代理再提交写入；独立检查零 accepted、无未释放请求租约。用 PostgreSQL 18.6 pg_dump/custom/no-owner/no-privileges 只备份确切自有 schema，复制完整受管目录，记录数据库 dump 和每个产物摘要。
4. 原自有产物移动到本轮可恢复隔离目录，不触碰原批准包；缺失产物时用原生产恢复路径检查 admission 关闭，不把它说成实际 HTTP 就绪检查。仅删除该停止服务的自有 schema，确认不存在后 pg_restore；从备份复制到全新受管目录并核对全部摘要，执行迁移检查和原生产恢复路径。备份数据只留本地忽略目录，不提交凭据、用户轨迹或模型。
5. 启动实际恢复 API，/health/ready=200；同一会话 epoch/version/history/favorites/hidden 保持，恢复前的原键精确重放（包括旧 epoch completed），独立数据库账本一致；一笔显式新键 dense 成功且合法。实际浏览器刷新核对恢复状态。最后关闭自有标签、代理、API，等待真实进程退出和正常 drain，删除唯一自有 schema，独立实查 PID/监听/schema 均不存在，保护路径摘要不变。

## 判断、停止和交付

每一步保存观察后才判断；首次失败即保留部分证据、执行安全清退并停止后续正式步骤，不自动换键、修改期限、增加样本或重跑追全绿。无法证明所有权或进程退出时拒绝删除数据库。若发现新 bug，先追加本地事件和对应反例，再另行登记修复/复验，不能覆盖本轮失败。

复用既有备份 PostgreSQL 测试及 Node 页面反例；补缺失的旧推荐响应/重置反例时单独提交实现和测试，再冻结正式运行。检查非空、零失败/错误/跳过，静态正确性/效率/复用串行复核；缺少 team-mode 要求的 agent_type，不声称独立代理审核。computer-use 用实际浏览器操作，不能用 Node 模拟代替 UI 证据；git-ship 显式路径、中文提交/PR、无 Claude trailer。

本轮结果只能补“指定本地完整模型浏览器及停机成对备份恢复”的证据，不是在线备份、异机灾备、长期稳定、300ms/20QPS/99% SLA；作者连续计时讲解和亲自修改代码不由 AI 代签。保留依赖 #64 的 PR 记录并检查精确最终 head push/PR 四项 CI，所有最终门禁具备后再决定合并/版本冻结。

## 启动前检查更正

首次 02f697c 启动前将整个 tests 工作区与 LF Git blob 做原始字节比较，被 `tests/web_evaluations.test.cjs` 的 CRLF 拒绝；实查仅换行差异，core.autocrlf=true、text=auto、Git 工作区干净。该次未创建 schema/API、HTTP 样本为 0，保存 QA-004 事件、失败辅助工具及部分源码快照，不算浏览器或备份验收失败/通过。下一次使用全新 attempt-2；运行源码 src/scripts/db/web 仍须原始字节等于 Git blob，仅 tests 下 .cjs 可接受已核对的 CRLF/LF 等价，并保存工作区原始字节/哈希和对应 Git blob 哈希、具体路径。除此之外的差异仍拒绝，不修改保护文件或生产代码。批量读取 Git index blob 代替逐文件启动 Git，减少启动检查开销，不改变 HTTP 口径。

attempt-2 仍在启动前拒绝 web/evaluations.html，零 schema/API/HTTP。随后一次性只读遍历全部检查路径，实际恰有五个纯 CRLF/LF 差异：tests/web_evaluations.test.cjs、tests/web_file_import.test.cjs、tests/web_results.test.cjs、web/evaluations.html、web/evaluations.js。保存第二次失败，不再猜测范围；下一次全新 attempt-3 仅这五个明确路径允许换行等价，全部归档原始字节与双摘要。生产 src/scripts/db、推荐页面 web/index.html 及其他路径仍原始字节严格匹配。此为检查器修正，不是生产候选变化或正式样本重跑。
