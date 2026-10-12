# 超时清退时间线：机制已补齐，本批未复现历史 504

## 结论与来源

按[预登记协议](stable-timeout-timeline-plan-20261010.md)，只增强隔离诊断和负面门禁。生产 SQL、单排名 worker、原 2 秒期限、连接/恢复、持久校验和资源所有权未改。完整模型两笔顺序与 24 笔双并发全部成功；**没有失败内部轨迹，历史 504 未关闭，不能称稳定版或性能修复**。不追加请求、重试、A/B/B/A、120 笔或浏览器验收。

- 协议 `40e64fadc707419e42c2cebb5b8157f057f22d7e` 先独立提交并推送，核对 origin 后才构造测试；未重复上一轮晚于测量推送的偏差。
- 实现/测试 `503358658dcf17bc88305a3763c5f28576ad8661` 提交并推送；同一干净 HEAD 复验后只执行一次固定批次。之后只归档文档，原始目录不覆盖。
- 只读批准包 `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，137,249 项，manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，模型 `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`，NumPy。原始 profile 保存源码/脚本/迁移逐文件哈希，独立复算一致。
- Windows 11、Python 3.12.6、NumPy 2.1.3、psycopg 3.3.6、PostgreSQL 18.6。准备前 `2026-10-10T23:15:28.6918097+08:00` CPU 快照 10%、12 逻辑核；非持续负载或 BLAS 总 CPU 证据。

## 修复诊断门禁，不改运行策略

原脚本只强制成功数据库轨迹覆盖；504 缺少取消清退证据也可能达到“诊断完成”。本轮为 opt-in 进程补原 `asyncio.Timeout._on_timeout` 回调、Recommend 工作流、原数据库/CPU `_drain`、真实 CPU 工作、ASGI response-start/最后 body send 完成。固定标签、单调时钟与请求局部 ContextVar，不存正文、参数、SQL、令牌或异常消息；保留原调用/返回对象/异常/取消/资源归属。私有回调缺失拒绝安装，退出恢复补丁；仅验证本地及 CI 的 Python 3.12，不承诺其他内部实现兼容。

504 门禁要求有限非负、有序的回调、TimeoutError 工作流、完成的清退和响应；进入 execution admission 后须成功关闭租约，已建立连接时须有真实 driver close。CPU/入场/写入退出后才关闭租约；恢复尚未创建租约的超时不伪造关闭。CLI 重新检查原始 504 行与数量，拒绝缺失/错误/NaN/负数/倒置、伪造 coverage 或隐瞒失败。无 504 时 `timeout_trace_count=0, timeout_traces_complete=null`，不是空集合全称断言通过。

回调时刻不是名义期限或第一次取消送达；drain 不是强杀线程；send 完成不是客户端收到。父子阶段重叠，不相加；execute 含驱动/锁/网络/结果接收，不等于纯 SQL、锁等待或 GIL。插桩成本未扣除。

## 功能与负面测试

各组重叠不累加，均零失败/错误/跳过。开发检查不替代干净冻结版本。

| 阶段 | 数量 | XML 时间 | 本地证据 SHA-256 |
| --- | ---: | ---: | --- |
| 首次开发，五模块 | 314 | 55.350 s | `timeout-dev-20261010.xml`：`5654a98ecd7b05f1912fed02147597afe7ff9a5ee5e1b2093baedee6e47720d0` |
| 三方面复核补强，十一模块 | 430 | 131.625 s | `timeout-review-20261010.xml`：`b0b6a8dd23d512a847a71b9fad704682853bad61b91d8e818151b425e0df3ba6` |
| 追加双回调归属，三模块 | 100 | 17.903 s | `timeout-finaldev-20261010.xml`：`67cedccc1ff3b736c3448e249978f0041bc9100551eecbbb5d323f2ca1127b5c` |
| 干净 5033586，同十一模块 | 431 | 129.713 s | `timeout-clean-20261010.xml`：`39839972cb29b614dcc30ebdeb20a27f04e55aa37ee619675d358f2e0416efd7` |

XML 位于本地忽略的 artifacts。干净模块为 timeout_trace、tcp_profile、tcp_control、borrowed_trace、phase_gate、profile、async、admission_connection、recommendation_recovery、architecture、repository_hygiene。CI 加入新模块，卫生测试检查恰好执行一次；PostgreSQL 服务测试仍要求零跳过。

真实合成包/PostgreSQL/API 保留原 2 秒期限，分别阻塞恢复、入场、CPU、结果写入。到期后请求不提前完成，入场/CPU/写入仍持有租约，真实工作退出后才清退、关闭租约、返回 504。恢复超时无请求行；入场/CPU 超时 failed 且无结果；写入晚于期限提交时保持 completed/两条结果，同键 API 重放 200。重复取消真实 CPU、并发回调归属、补丁恢复和秘密不记录均覆盖。这是机制验证，非完整 137,249 项故障性能验收。

按 team-mode 的执行检查点和质量/性能/复用复核；接口缺少必需 agent_type，主线程串行完成，不冒充独立评审。复用 observer/原 drain，未扩大生产 API 或无关重构；新增时间戳/锁/包装成本仍算插桩。

## 固定完整模型批次

命令：`python -m scripts.profile_r06_tcp artifacts/diagnostics/stable-timeout-timeline-20261010 E:/codex/proj/artifacts/bundles/r06-frozen-20261004-a bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec --expected-manifest-sha256 9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6 --samples 24 --concurrency 2 --no-gc-events`。使用 .venv-accelerated，trace 开，phase gate/GC 观察关；同一 sample session、每笔新键、dense/k=10、每笔新 HTTPX client、不重试。两笔顺序参考和 24 笔 load 共 26 个 200，无 transport error。

| load 24 笔 | p50 | p95 | p99 |
| --- | ---: | ---: | ---: |
| 创建 HTTP client | 313.840 ms | 357.755 ms | 359.187 ms |
| HTTP 往返 | 1,368.733 ms | 1,628.054 ms | 1,645.812 ms |
| client 总耗时 | 1,672.747 ms | 1,952.159 ms | 1,987.245 ms |
| 服务端 ASGI | 1,356.570 ms | 1,604.925 ms | 1,618.421 ms |

load wall 20.499462 s，闭环完成约 1.17076 请求/s；包含客户端创建和固定双并发，非最大吞吐或 20 QPS 证明。

| 服务端 load 阶段（不可相加） | p50 | p95 | 最大 |
| --- | ---: | ---: | ---: |
| publication recovery | 97.862 ms | 235.265 ms | 281.292 ms |
| execution lease/admission 父阶段 | 806.335 ms | 926.321 ms | 1,009.316 ms |
| catalog capture 子阶段 | 694.804 ms | 806.346 ms | 815.676 ms |
| 内层目录 execute | 652.719 ms | 753.606 ms | 774.300 ms |
| CPU queue wait | 0.134 ms | 109.585 ms | 128.511 ms |
| CPU work | 181.091 ms | 259.424 ms | 347.744 ms |
| result write | 110.586 ms | 222.320 ms | 235.364 ms |
| execution lease close | 0.147 ms | 0.381 ms | 0.479 ms |

独立断言 26 个 client/server index 0..25 精确对应、状态/身份一致，源码/模型身份，所有阶段有限非负且位于 ASGI 内，26 个成功数据库轨迹完整，workflow/CPU/send/close 各恰好一次，无 deadline callback；正确保留失败覆盖 null。不只看 exit 0，该出口仅表示诊断完整。

上一轮未插桩 24 笔为 22/24、两次 504；client 创建 p50 945.142 ms、HTTP p95 2,175.572 ms。本轮 313.840/1,628.054 ms，属于不同批次/观测状态，非配对因果对照，客户端成本漂移尚未解释。生产实现未改，不能把较快归功于超时修复；成功轨迹不能解释先前失败。

## 资源和原始证据

run `7f2ca03f-8bbc-424d-9c81-fdf092417d01`；owned schema `test_evorec_47689c2a94c647629a4296cf545313bf`；API PID 9136、端口 51671。正常 CPU drain、无硬杀；独立查询 owned schema 不存在、test_evorec_ namespace 总数 0，CIM 确认进程消失、监听清单确认该端口消失。删除仅本轮隔离 schema 和测试行，原始证据保留；未记录逐 DB backend PID，不宣称逐 PID 释放另已证明。

.env SHA 仍 `32f05a41febf54595c5cef010ecee661c47570ac9e0a41238501b9297939e267`；只读 manifest 不变，主目录干净仍 `6e44594f55dd180ec4bacce1ed8d19e7bf4aee61`。业务 schema/服务/8000 不操作。

本地忽略目录 `artifacts/diagnostics/stable-timeout-timeline-20261010`：

- `profile.json` SHA `650c20a2c53515c2c2a0711c5ac4ab2fc998ff01dcece851fccdb7119244e6ef`。
- `observations.json` SHA `bb0308b54334fbcd35e9a6f49c31e394c1d1f67328b754f2487b0b703cc45199`。
- `process-1/profile.json` SHA `a386de329d67d565c277b39c6d89c4851ae29ece3bc723c3998f717ad0a6d54d`。
- `artifacts/timeout-preflight-20261010.json` SHA `3b9304662b8c80e83bb32f30350320c195cca36134c8ab71951c9cd1981f16ab`；owned/ready/stopped run ID 交叉一致。

## 下一项边界

本轮如期停止，无失败轨迹可选择 worker、恢复锁或 SQL 修改。下一轮先预登记同一生产 SQL、原期限/worker/客户端口径下插桩开/关的固定交错对照，单列客户端创建成本；保留全部轮次，不追全绿或强归因。该对照尚未登记或执行。最终未插桩稳定负载、两轮 120 笔、恢复/浏览器/备份和精确稳定冻结仍未完成；新的失败门禁不是这些验收的替代。

新草稿 PR 依赖 PR #59，保留候选栈，不合并。源码提交 5033586 的 push CI（run 38062684141）两个 job 已成功；文档最终 head 与 PR 触发的 CI 在发布后另核对，不借用旧 head 绿灯。
