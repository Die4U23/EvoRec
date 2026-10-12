# 同源码插桩开关 A/B/B/A：预登记

基于干净 `24a125c351c09c8116b8ba94e1dcc20fe609a967`（草稿 PR #60），新分支 `codex/r06-trace-abba-controls`。上轮插桩 24/24 成功，前轮未插桩 22/24、两次 504；客户端创建成本也不同，不能归因于 SQL 或插桩。本轮仅比较同一当前生产源码的观察状态，不是旧 SQL/新 SQL 对照，也不是稳定性验收。

协议先提交并推送，核对 origin SHA 后才构造新测试、查看新验证结果和运行完整包。诊断校验器及必要测试另行提交并推送；四轮使用同一干净实现提交、src/scripts/db migrations 逐文件指纹，期间不改跟踪文件。结果和问题台账另行提交，保留失败与原始目录，不改写历史反例。

## 固定口径

- 顺序固定 `a1 / b1 / b2 / a2`，A 关闭服务端插桩，B 开启 PR #60 时间线插桩。每轮独立自有 schema、API 进程、随机非 8000 loopback 端口。
- 每轮恰好两笔顺序参考，再 24 笔双并发；same sample profile/history、轮内同一 session、每笔新幂等键、dense/k=10，每笔新 HTTPX client、trust_env=False、client timeout=10 秒，无重试。UUID 和实际请求时间不冻结，不能称精确相同随机输入。
- GC 观察关闭、phase gate 关闭；不改 GC 策略、生产 SQL、连接/恢复策略、原 2 秒期限、单排名 worker、持久封印/校验或清退所有权。只读批准包 `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，137,249 项，NumPy；manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，模型 `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。
- 复用 `scripts.profile_r06_tcp`，共同 flags 为 `--samples 24 --concurrency 2 --no-gc-events`；A 另加 `--untraced`，B 不加。四个新目录为本地 artifacts/diagnostics/stable-trace-abba-20261011 下的 a1、b1、b2、a2。
- 测试与测量串行，测量不运行其他本项目测试/CPU 助手；每轮前记录时间、CPU/内存快照、Python/NumPy/HTTPX/psycopg/PostgreSQL 版本、源码/模型和 .env 指纹。快照不是连续负载或独占机器证明。业务 schema、.env、主目录和 8000 不操作。

## 独立结果门禁与问题记录

新增只检查原始报告/生命周期标记的工具，不改 HTTP 采样器或生产运行。逐轮重算 26 笔 index/phase/status/身份、24 笔分母和摘要、有限非负客户端分段耗时、源码/输入/设置、owned/ready/stopped 身份及正常 CPU 清退。B 重新核对 client/server 逐笔对应、阶段时钟、成功数据库覆盖、实际 504 时间线；A 的服务端和失败阶段仍 unknown，不从 B 填补。零成功身份/成功延迟及零失败时间线为 null，不能用空集合通过伪造覆盖。

每轮后独立实查确切 schema 不存在、API PID/监听退出，检查 .env/主目录/模型不变，再进入下一轮。完整 200/504 均保留；504 不引发重试或扩大样本。启动、配置、身份、观测完整性、清退异常，transport error 或非 200/504 停止剩余轮次，保留部分记录；不修复后自动重跑本协议。四轮都完整时才生成全系列摘要；工具退出 0 只代表证据结构与诊断门禁通过，零失败另列布尔值且不是 stable/SLA。

新 bug/异常在发现后立即写入本地忽略的问题事件（时间、轮次/sample、固定错误类别、原始路径、影响、状态），不存令牌、DSN、正文或异常消息；测量结束后并入[问题台账](stable-runtime-bugs.md)。明确区分已确认机制缺陷、504 症状、环境错误和未证实假说，修复关闭必须列出对应源码与负面控制。四轮时序/随机身份/主机变化限制因果；列出每轮全部成功/失败、client 创建/HTTP/总耗时，B 另列 ASGI、目录/排名/清退，不扣观察成本，不相加重叠阶段、不把 execute 标为锁等待。

本轮不追加复用 HTTP client、独立 session、SQL 计划、120 笔、故障注入或浏览器验收。根据是否取得真实失败时间线选择下一项有界修改；未复现就明确未知，不凭全绿关闭旧问题。team-mode 当前接口无必需 agent_type，主线程分机制/性能/复用审查，串行执行且不冒充独立评审。使用 git-ship 显式暂存、中文提交、保留依赖 #60 的草稿 PR 并检查精确最终 head CI；稳定门禁未完成不合并。
