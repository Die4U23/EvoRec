# 结果连接旧版／租约复用候选：固定 A/B/B/A 预登记

依赖草稿 PR #62，新分支 `codex/r06-result-lease-abba-controls`。本轮只增加诊断证据校验器、反例测试和汇总，不修改生产实现、目录 SQL/校验、恢复锁、失败清理连接、排名队列、2 秒期限或 HTTP 模式。历史 R06-001 仍开放；上一轮 24/24 和 8/8 不作为历史失败已解决或因果收益证明。

本协议先提交并推送，再构造新测试或查看结果。校验器及必要测试另行提交推送；正式四轮使用干净冻结提交，src/scripts/db migrations 文件哈希与远程 SHA 一并记录。测量期间不改跟踪文件，结果和问题台账单独提交。不覆盖任何旧目录或失败证据。

## 冻结版本与隔离边界

- A：旧版 `2f50e6226380eba5e3a7c382dedb58b44ad7aeba`，结果写入另建连接；现有自有 `r06-capture-control-a` 工作树当前为干净 detached `84c9bbcb96f4e428ab5aaf7f6eebdc743e0dc418`。只在确认无跟踪改动后临时切到 A，四轮结束和独立核对后恢复原提交；不覆盖本地原始材料。
- B：本轮校验器通过并推送后的干净提交，测量前冻结 SHA。生产 src/db 字节须与 `1a15ebdbb2eafb1d5113a06ec4b1fb2456840c77` 一致；相对 A 仅 postgres.py 和 recommendation_execution.py 两个生产文件变化。既有 sample/profile/main 未插桩调用和服务 lab 等诊断依赖必须一致，profile 的唯一旧新函数差异为插桩成功覆盖 helper，本轮不调用它。
- 每轮直接在相应工作树运行同名 scripts.profile_r06_tcp，显式设置该树 src 的 PYTHONPATH，子进程 cwd 为该树。不得在 A 中导入 B 的生产包或反过来；源文件哈希绑定对应提交，不能只记检查器提交。
- 完整批准包 `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，137,249 项，NumPy；manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，模型 `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。业务 schema、主目录、.env、模型和业务 8000 不操作。

## 固定样本和停止规则

顺序固定 `a1 → b1 → b2 → a2`，不能因结果改变顺序、增加或重跑。每轮独立自有 schema、服务进程和随机非 8000 loopback 端口；完整发布全部上架目录，两个顺序参考及恰好 24 笔双并发。轮内同一 sample session，每笔新键；dense/k=10，原 2 秒期限、单排名 worker、队列容量 8。每笔新 HTTPX client、trust_env=False、timeout=10 秒、无重试；关闭服务插桩、GC 观察和 phase gate。源版本是唯一计划变量。

各树输出为 artifacts/diagnostics/stable-result-abba-20261011 下对应轮名；B 树保存系列校验及环境/独立资源核对，旧树文件只读取不复制改写。每轮前保存时间、CPU/内存快照、版本、完整源及模型指纹；启动、模型准备不算推理延迟。快照不能证明连续独占。

每轮完成后重新解析实际 26 笔：逐笔索引/分母、顺序与负载 phase、200 真实身份、全部 504、客户端创建/HTTP/总耗时、汇总与生命周期。不把脚本 exit 0 视作零失败。合法 504 保留且继续固定剩余轮；配置、启动、源或身份错误、transport error、非 200/504、正常清退或独立资源检查失败则停止后续轮，保存现有证据，不修复后自动重跑本协议。

每轮必须独立确认确切 owned schema、实际 API PID 和监听不存在，正常 CPU drain、无硬杀，主工作区/.env/模型保持原样；不能只信 stopped.json。四轮 run_id/schema 均不得复用。检查器只核对录制证据，明确 live_resource_checks_performed=false；主线程实查单独记录，不伪造数据库持久终态或同键重放。本批不保存请求正文/令牌/请求键，故删 schema 后持久终态未知；既有实库故障注入证据与本批性能分开。

## 结论与后续门禁

全部负载 96 笔及顺序 8 笔均纳入报告，分别列四轮成功/失败和 client 创建、HTTP、总时间 p50/p95，不只统计成功请求。所有服务器阶段和失败阶段本轮 unknown；不能用 HTTP 墙钟直接断言锁、GIL、SQL 或 CPU 排队，也不能用有限 A/B 波动宣称因果改进。新 bug/异常立即写忽略事件，保留源、原始目录、失败索引及类别；批次结束更新[台账](stable-runtime-bugs.md)。

若候选仍出现 504，本轮不追加插桩/改期限/worker，按新预登记固定少量失败阶段诊断再决定下一项修改。若候选四轮证据完整且零失败，才允许另行登记两轮各 120 笔、完整恢复/同键唯一/重置过期响应/浏览器与备份门禁；本轮不执行这些门禁，production_acceptance=false、sla_proven=false，R06-001 不关闭。

git-ship 显式暂存、中文提交/PR，不加 Claude trailer；保留依赖 #62 的草稿 PR，最终 head 的 push/PR CI 单独核对。team-mode 必需 agent_type 不可用，按质量、效率、复用三个视角主线程串行复核，不声称独立代理审核。未达到稳定门禁不合并。
