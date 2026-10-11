# 完整 R06 双轮负载与持久恢复验收

结论：干净已推送 `25a2a03f496522a3b863c53195a1fa11bdab9dc8` 固定两轮各 120 笔双并发，240/240 HTTP 200，逐笔身份/持久结果/租约释放全部通过；两轮完整重启、同键、断连与真实崩溃恢复通过。只证明指定本地场景，不证明长期稳定或 20 QPS/300 ms/99%；浏览器重置过期结果、备份恢复仍待本版本最终复演，不合并稳定版。

## 冻结范围和可反证门禁

[协议](stable-recovery-plan-20261011.md)先提交推送 `b30f8c2fb7408057b3681a5d5169eb73db77726a`，实现/测试独立提交推送 25a2a03，再跑干净回归与正式样本。生产 src/db 共 94 文件与 e54d5a7 字节完全一致；全部 src/scripts/db migrations 共 149 文件哈希保存在 environment.json。测量期间无跟踪改动，结果/台账另提交。

完整批准包 bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec，137,249 项、NumPy；manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，model_version `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。Windows/Python 3.12.6、NumPy 2.1.3、HTTPX 0.28.1、psycopg 3.3.6、PostgreSQL 18.6，12 逻辑 CPU。每轮前 CPU 快照 14%/23%、可用内存 3,414,712/3,482,848 KiB；不是连续独占证据。

原 2 秒 API 期限、单排名 worker/队列 8、dense/k=10、全上架、轮内同一 sample session；每笔新键、新 HTTPX client、trust_env=False、timeout=10、无自动重试；关闭服务插桩/GC 观察/phase gate。启动、完整准备和重启不计入请求延迟。主目录/.env/模型/业务 schema/8000 及对照工作树不操作。

新增 opt-in `--strict-load` 收齐所有样本再判定：200 仍须请求键/session epoch/history_version/模型正确、有限分数、无重复/已见；独立一致只读数据库核对 120 个唯一 completed、捕获时刻/批准资格/顺序/分数/来源与响应相同，实际 pg_locks 无执行租约、无 accepted 遗留。请求键、令牌、正文仅临时内存，不落盘。原诊断模式仍标为 measured_not_sla，不能只看 exit 0。

独立 checker 重算逐笔汇总/分母，核对 source、模型、三次进程身份、正常清退/计划硬杀及六项持久恢复见证；离线重查不声称执行 live 检查。主线程逐轮实查 schema/PID/监听，并在 launcher exit 0 后额外只读总核对，没有新增推荐请求。

## 功能检查和实际失败

开发首组 183 项通过后补查失败终态，发现 GATE-001：新 oracle 把内部 execution_interrupted/execution_failed 与 API recommendation_interrupted/recommendation_idempotency_conflict 直接比较，合法失败重放被误拒。三项新增测试实际 1 通过、2 失败，立即记录事件和基于协议的六文件完整字节快照；修复映射后原反例通过。未改生产行为，正式测量尚未开始。详见[台账](stable-runtime-bugs.md)。

| 独立 XML 后缀 | tests | failures/errors/skipped | SHA256 |
| --- | ---: | --- | --- |
| target | 183 | 0/0/0 | 52006c4df37e625ace4e085b7139866dc991838c221ab4ec8825604c33473e38 |
| terminal-before | 3 | 2/0/0 | 4fdf5752cf963013f2c9594841b52a92e72a15c53671314dcce8ed573462ec00 |
| review | 383 | 0/0/0 | 6ce328149ecaa4cb0911393977a61efec599d5acb79ca2d41fee907be8842b9d |
| final-target | 187 | 0/0/0 | f5943c4315f83553cc9d8dd5fe6673a0c3bdad5d3aa70d7568c5c1793d548ec1 |
| count-final | 69 | 0/0/0 | b630a680b9c3a503f9b599484286b7a384508de6a0dfd7b968aa294c61f0dc5e |
| clean（冻结源码，133.630 秒） | 385 | 0/0/0 | 8ffac83a2894fa87676898a5996b0ec6038b2f664d5e76e4e2d446f6e147c138 |

文件为 artifacts/stable-recovery-对应后缀-20261011.xml，互有重叠，不累加。干净十模块覆盖严格 oracle、旧诊断、真实 PG 锁、结果/入场连接、取消 drain、真实超时和恢复。反例覆盖 504/transport、错键/epoch/version/模型、非法结果/成员、未提交/少行/错位置/未释放锁、缺样本/错汇总及假恢复/清退；原两项失败在干净 385 项中通过，失败输出未覆盖。

## 固定实测与恢复

| 轮次 | 成功/全部 | HTTP/身份/持久门禁失败 | 创建 p50/p95 ms | HTTP 往返 p50/p95 ms | 总耗时 p50/p95 ms | 负载墙钟 s |
| --- | --- | --- | --- | --- | --- | --- |
| r1 | 120/120 | 0/0/0 | 294.179/314.034 | 1166.452/1298.104 | 1464.193/1584.819 | 87.495081 |
| r2 | 120/120 | 0/0/0 | 293.540/315.940 | 1164.338/1271.969 | 1460.077/1577.011 | 86.665455 |

全部样本总耗时 p99 为 1720.087/1684.257 ms；校验 p95 0.065/0.064 ms、close p95 0.173/0.167 ms。阶段百分位不相加，client 创建不算服务端工作；内部时序/纯 SQL/等待阶段 unknown。闭环观察完成率约 1.372/1.385 requests/s，不是开放到达容量。无 504、其他状态或 transport，不以空失败集证明历史超时根因已解决。

两轮均：首次推荐 → 正常停止/新进程同键精确重放和 session epoch/history 恢复 → 同键双并发唯一 completed → 120 笔负载 → 客户端断连后原键 completed，响应与原账本一致 → 观察 accepted+owner 后硬杀已验证 API → 新进程原键 recommendation_interrupted/nonretry，重复原键相同终态，failed/零结果 → 显式新键 completed。六项独立持久见证通过，租约全部释放，无 accepted 遗留。

恢复相关推荐另列，每轮 11 次 HTTP 尝试（含断连及崩溃在途请求），不算进 120 分母，不是负载重试。首次/重启重放总耗时分别 1287.541/508.608、1205.497/522.795 ms；整轮含准备、重启和清退 219.125/214.812 s，不是推理延迟。

## 证据、清退与 CI

原始目录 artifacts/diagnostics/stable-recovery-20261011；保留环境/源码指纹、preflight、invocation、逐轮 recorded/live check、series、archive-summary、额外 final-live-all 及 GATE-001 原事件/修复/快照。两个临时 schema/请求数据已删除，不能通过页面撤销；报告/XML/快照/模型保留。

- r1 run_id ce3fe3a5-f76d-4e4a-a635-7cb1fe463eef，schema test_evorec_c86b813517094e63bf3e4c9d9ea30fe9；PID 11616/24428/32780、端口 59595/59635/49778。
- r2 run_id 2769cb8f-841f-45aa-8afc-e87c7d368865，schema test_evorec_4e7a38dfa5d048e7b2dee0cd13ea0cf6；PID 31240/23968/23568、端口 49895/49930/50488。
- 每轮 process 1/3 正常 CPU drain，process 2 是唯一计划硬杀；不虚报全部正常终止。逐轮及总实查六 PID/监听和两个 schema 不存在，test schema 总数 0；源码/主目录/.env/模型/对照工作树 unchanged。

| 原始文件 | SHA256 |
| --- | --- |
| r1/verification.json | 0fbae4e23c995a016ea51fa9580abeea66c7e89509076632eb6d7076da28bc89 |
| r1/observations.json | 94a54a8dd17f3cd2ec080704ae4dd838d54504f70ec82fe9c69c02e834c32437 |
| r2/verification.json | 916ef5f1af4d9d3eb60bea8ae14c227176bfeaae76d24ba514d2315970e45d4a |
| r2/observations.json | 7763848e2d95185fa908521d9e3029df9b5234d9fc8e207f737e558efda121dc |
| environment.json | 03b5b667455806ea87d3baf9da2d3f6d334577ee845dd71c131f0882a48f6d76 |
| series-check.json | 82a26500abd469f2895e13f8b97779d893cb8520751828618c09864081ec23b6 |
| archive-summary.json | b11f6d5c90ea569462ede11f338dcb8e87e7610896926c34e7600f16ed94e38a |
| final-live-all.json | 577c8a6f35fbf1da0ae5b4b2034478bd980556efc4f6f5b5db1002f5bdf1bda6 |
| bug-gate-001.json / gate-001-corrected.json | e420039135a74a3f43ad2ad0cf748ef8dd57e53832b70eee993d4b312822deaf / 7256bb37d88bb0cf29ec0c9b3e97578db36c91c94e02a7a0168205cd1afdb9d1 |

源 25a2a03 push run 38133159632（service 114451497307、numeric 114451497425）及 PR run 38133206215（service 114451633646、numeric 114451633438）全 success；ci-source-25a2a03.json SHA 1d59def66d37f14940a18c841035547206e548e45bec5b434571d43b82e8a286。结果最终提交四项 CI 单独核对并记录于[草稿 PR #64](https://github.com/Die4U23/EvoRec/pull/64)及 ci-final-对应 head 文件，不借用源 CI。

git-ship 保留依赖 #63 的中文草稿 PR、显式暂存、无 Claude trailer；team-mode agent_type 不可用，主线程按正确性/效率/复用串行复核，不声称独立代理审核。只关闭 GATE-001；指定负载/恢复门禁通过，但 R06-001 历史根因及 OBS-001 未解释；production_acceptance=false、sla_proven=false。下一项独立登记浏览器重置/过期响应和备份恢复，再决定稳定候选合并。
