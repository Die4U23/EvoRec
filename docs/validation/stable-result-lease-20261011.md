# 结果事务复用执行租约：实库反例与完整包初筛

## 结论和来源

按[先推送的预登记](stable-result-lease-plan-20261011.md)，实现结果事务安全复用候选，干净提交的 520 项回归通过；完整 137,249 商品固定未插桩 load **24/24**、插桩 load **8/8**，各另有两笔顺序 200，全部 36 个响应身份通过。十笔插桩请求每笔只观察到两次数据库连接，结果阶段无新增连接，并有成功的借用事务退出边界。**仅通过本轮候选初筛与机制检验，不是稳定版通过**；旧/新固定对照、两轮 120 笔/恢复/浏览器/备份尚未执行，历史 R06-001 不关闭。

协议 `1d2d366291df44ec61cbcfccb789f30fe3c34a3a` 提交推送并核对远端后才构造测试；实现/测试 `1a15ebdbb2eafb1d5113a06ec4b1fb2456840c77` 另行提交推送。本轮干净复验和两轮完整包均绑定后者及逐文件 src/scripts/db migrations 指纹，测量期间无跟踪文件修改。报告和台账单独提交。草稿 [PR #62](https://github.com/Die4U23/EvoRec/pull/62) 依赖 #61，不合并或替换业务 8000 服务。

仅改变已登记执行的结果写入连接选择及必要安全检查；失败清理仍独立连接。保持目录 SQL、N+1/持久封印/实际数据校验、恢复锁、单 worker、原 2 秒期限、客户端、零重试不变。目录文件 SHA `6736359f9707d4840a788d5cb4c225254d157771e223475e94dcd6ec34b9982a`；postgres.py SHA `334f74df224c7a91eb352d1e18f52e812e054e30ef356e2e3596e4d6ddeba199`；recommendation_execution.py SHA `0f6b2ffea2e5b666b9e353624eda85457817954ccc0e4a06e1b4363887e94bd6`。

## 所有权、取消和兼容边界

借用前检查 backend/request、已 admitted/仍登记、连接未关闭、autocommit、事务 IDLE 及 advisory lock；RLock 覆盖完整事务直到提交/回滚和退出检查，close 不能提前释放租约。只进入 connection.transaction()，不进入会关闭 socket 的 Connection 上下文。事务内原 binding、模型、请求终态和结果内容校验继续保留，已登记 accepted 行必须有匹配 owner；不能通过擦除 owner 伪装成 legacy。无登记执行的历史兼容路径仍独立连接，具有 durable owner 的 accepted 行没有匹配 executor 时仍拒绝。

实库触发器记录两条商品写入及 completed 更新的 backend PID，三笔均等于租约 PID；结果写入与相同内容重复保存无新连接，独立观察者看到唯一 completed/两条结果，行锁已释放但 advisory lock 保留至 close。延期提交触发器失败回滚全部结果，连接仍 IDLE/held；错误 backend/request、缺失 owner、模型/binding 不一致、已完成内容变化、丢失/关闭/解锁/未登记租约、未 admitted、autocommit 关闭和外层事务均有反例。

真实事务退出前/提交后门控证明 close 等待所有权锁；两个结果线程串行且只插入一次。原重复取消 drain、实际 2 秒 API 四阶段超时、late commit 保持 completed 并同键重放、崩溃终态/快照恢复与 R06 输入等价回归均在干净复验中通过。无租约 completed 的重复保存仍独立连接，失败清理不会覆盖 completed。

机制负控制在独立 pytest 进程中临时把借用方法改回独立连接，零新增连接断言按预期失败；没有改跟踪源码，插件退出恢复方法。诊断门禁保留历史完整 connect/commit/close 分支，新借用分支必须有明确、无错误的 Transaction.__exit__，不能混入结果 connect/commit/close 或用旧分支遮盖失败退出。真实 PostgreSQL commit/rollback 及真实 TCP 证明新边界，不是删掉旧提交检查来制造全绿。

## R06-002：开发中发现的真实提交前断连缺陷

首组和早期扩大回归全绿之后，审查补充“最后 UPDATE 完成、COMMIT 前真实关闭租约 socket”的反例，首次 1/1 失败：save 未抛出 ManagementError。追加独立持久状态观察后第二次 1/1 失败，确认 save 无异常返回而数据库仍 accepted、结果 0 行。原因是已关闭连接的 psycopg Transaction.__exit__ 可以正常返回而没有提交，不能只依赖上下文无异常来认定成功。

这是本轮借用候选中的缺陷，不是历史 504 的根因证明；该故障直接调用 save，没有观察 HTTP 响应，不把潜在 HTTP 200 写成实际观察。首次失败发生在 dirty 开发源，已立即保存基准提交及八个修改文件的完整字节快照和 SHA（bug-r06-002-source / event）；第二次增强测试已包含在最终实现提交，去掉结果事务退出后的状态检查即可重建该反例。不将开发 XML 冒充干净提交运行。

修复在 `1a15ebd`：结果事务正常退出后仍要求 socket 未关闭、autocommit/IDLE；丢失则明确失败，不提前释放仍运行的资源。两个原始失败 XML 未删除或覆盖，定向 61 项及干净 520 项复验通过；[问题台账](stable-runtime-bugs.md)将 R06-002 关闭，不连带关闭 R06-001 或 OBS-001。

| 阶段 | tests / failures / errors / skipped | XML time | 原始 XML SHA-256 |
| --- | --- | --- | --- |
| 首组定向（尚无断连反例） | 60 / 0 / 0 / 0 | 18.364 s | `fc7865ba34120b51618e2e7526601d8fc6f1d16cee7eaaa481eb5973cd2dbaea` |
| 独立连接机制负控制（预期失败） | 1 / 1 / 0 / 0 | 1.083 s | `ecf9f3c00d8442f6c1c930aff94a6e843842130add1de19e564e61260a842051` |
| 早期扩大回归（尚无断连修复） | 519 / 0 / 0 / 0 | 349.508 s | `b7b7aa215d1c8f61ad34d301223ac8d729f59d9de3404cbbc6595eddce956f80` |
| 提交前断连首次失败 | 1 / 1 / 0 / 0 | 见原 XML | `83e2e8139cecd959ffca7ea943604f7a9c03d7b8b2b39965c4e2c015189d146a` |
| 独立状态增强反例失败 | 1 / 1 / 0 / 0 | 见原 XML | `489f5a8fe83c69f11177dd9b3f2bc7be5962c000cdf37e364d4a678ac403e385` |
| 修复后定向 | 61 / 0 / 0 / 0 | 19.325 s | `75ee0c637bac9e08e260410b918bbe85877829ccfa4c094773d6ac245f7843b1` |
| 干净已推送 1a15ebd，十四模块 | 520 / 0 / 0 / 0 | 349.237 s | `08d710c2c29351bd141ece716bf6b738d994faf7dcf2d194ce21cdbb244d757f` |

XML 在本地 artifacts/result-lease-{target,negative,review,close-bug,close-state,fix,clean}-20261011.xml；范围重叠，不能相加为独立样本。项目内新 basetemp、no:cacheprovider，测试和模型测量串行。干净复验模块：result_connection、admission_connection、recommendation_recovery、session_admission_lock、r06_online、r06_capture_equivalence、r06_catalog_digest、r06_tcp_profile、r06_timeout_trace、r06_trace_abba、r06_borrowed_trace、postgres_api、recommendation、repository_hygiene。

## 固定完整模型结果

批准包 `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`、137,249 项、NumPy；manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，模型 `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。Windows、Python 3.12.6、NumPy 2.1.3、HTTPX 0.28.1、psycopg 3.3.6、PostgreSQL 18.6。每轮独立 schema/进程、随机非 8000 loopback 端口；same sample session/history、每笔新键、dense/k=10、每笔新 HTTPX client、trust_env=False、client timeout=10 秒、不重试。GC 观察/phase gate 关闭。

单位 ms，百分位为 nearest-rank，分母分别为固定 24/8 个 load，含全部请求；准备/启动不计入请求延迟。

| 轮次 | load 200 / 504 | client 创建 p50 / p95 | HTTP p50 / p95 | 总耗时 p50 / p95 | ASGI p95 | load wall |
| --- | --- | --- | --- | --- | --- | --- |
| 未插桩初筛 | 24 / 0 | 294.908 / 348.195 | 1193.856 / 1458.881 | 1482.635 / 1760.825 | unknown | 18.355834 s |
| 插桩边界检查 | 8 / 0 | 297.460 / 339.254 | 1192.003 / 1369.023 | 1487.319 / 1707.897 | 1343.156 | 6.210420 s |

各两笔顺序参考均 200，没有其他状态或 transport error，36 个实际 200 身份均正确。插桩十笔成功数据库覆盖完整，每笔 connect_count=2（publication recovery、execution lease/admission）；result connect/close=0，borrowed transaction exit 各一次。未观察 504，timeout_trace_count=0、timeout_traces_complete=null，不用空集表示失败机制已经通过。未插桩服务器阶段仍 unknown。本批完整包未保存用户级请求键/正文或检查持久终态重放，不能在 schema 删除后猜测；合成故障注入的 durable 证据单列上文。

插桩 8 个 load 的父子阶段 p50 / p95：目录捕获 658.288 / 677.882，CPU queue/dispatch wait 0.293 / 102.674，实际 CPU work 186.026 / 269.182，result write 5.952 / 24.414，借用事务退出 0.378 / 1.669。父子区间重叠不相加，execute 不是纯 SQL 或锁等待，CPU wait 不是已证明 GIL。结果新建连接在本轮成功轨迹中消失，但旧 B1 的 103.859 ms 与当前 parent 耗时不是受控性能差值；旧未插桩 A1/A2 同样曾 24/24，因此本轮不能证明历史 504 已修复。

首轮前 CPU 快照 7%，空闲物理内存 3,244,864 KiB；次轮前 23%、2,939,248 KiB，12 逻辑核。清退后快照 12%、2,906,788 KiB。不是连续负载或独占机器证明，没有扣除观察成本；时序/随机身份/主机变化仍限制因果，未外推长期 SLA、20 QPS 或 300 ms。

## 资源、证据与交付边界

离线检查重新核对 26/10 笔 index/phase/status/身份、实际失败数和 load 摘要、源/模型/设置、镜像观测和正常退出；零失败独立列出，不以诊断 exit 0 替代。主线程另实查确切 schema、API PID/监听消失，正常 CPU drain、无硬杀：

- 未插桩：schema test_evorec_d772d4f75c7b4fd682210b8faf4083f0，API PID 12644、port 56005。
- 插桩：schema test_evorec_6b4485bf2bd7403fa9386ed9f849a335，API PID 32028、port 56255。

两轮自有 schema 已删除，临时请求/结果不再保留；原始诊断、失败 XML、源码快照和批准模型保留。独立检查均为 schema_count=0、pid_count=0、listener_count=0、total_test_schema_count=0。主目录 HEAD `6e44594f55dd180ec4bacce1ed8d19e7bf4aee61` 及 clean 状态不变；.env SHA `32f05a41febf54595c5cef010ecee661c47570ac9e0a41238501b9297939e267`、包 manifest/catalog/metadata SHA 不变，业务 schema 和 8000 未操作。

原始目录为本地 artifacts/diagnostics/stable-result-lease-20261011。关键 SHA：

| 原始文件 | SHA-256 |
| --- | --- |
| untraced/profile.json | `d3214bc8418cc03055873b4f3f10f6b9a3fb4238007e14dd3db149e034baa858` |
| traced/profile.json | `18195bc381a330022aa92d08c035af31b40f2eb921ec2696c09090cbdeb48e84` |
| traced/process-1/profile.json | `17fbb948dd0a6b3b27bf349cb392635980f6b3dd1e800af7e62b461d41ff95a7` |
| untraced-record-check.json | `160d5130aa5ff2b39598abe19339a80335ed4ad28137524e3b92035739f138f7` |
| traced-record-check.json | `7027762f1a22a6cf61ff7b8bdac9f872ec0bed4a848c7d2a52e9aecb699ad8e2` |
| untraced-live-check.json | `bc65e1b47dcf55bb27d3c83b8a67b2f7c0950026f33dee9fcd2cc0c28e613ded` |
| traced-live-check.json | `ae88cb6ba39f07dfc4cb922088b4f888984ad1fe0a740e8bf10e3c800f43ee33` |
| 本地离线校验命令 verify_records.py | `b1b2dc641c6729c1bc23fbcd4fc7abebe498930762951d96344d04cba6b5e148` |
| 本地独立退出命令 check_live.ps1 | `62dd3e3c9d4412310c0bd82a0e572fb0e86c8c5a00aadf7fa1d994f81f5d3a1b` |

记录检查使用原固定 24 笔 check_arm 和本地固定 8 笔断言，后者重新核对原始行、逐笔驱动/退出顺序及无结果连接，不改 HTTP 采样器；离线记录明确 live_resource_checks_performed=false，独立实查另存，不混淆。原始完整 SHA、环境和即时事件均保存在上述本地目录，无 DSN/令牌/正文。

team-mode 接口无必需 agent_type，主线程按质量/性能/复用三视角串行审查，没有独立代理评审：提交前断连风险由实际反例发现并修复；RLock 覆盖事务、防锁顺序反转和终态守卫复核；不引入连接池/缓存/额外 worker；复用现有实库夹具、trace 和 drain，保留 admission 不同的历史 reconciliation 事务语义，不做跨范围重构。git-ship 使用显式路径、中文提交、无 Claude trailer，保留草稿 PR。

源实现 1a15ebd 的 push/PR CI 四项通过：push run 38115477232（数值 23 s、服务 3m6s），PR run 38115544769（数值 24 s、服务 2m25s），都绑定同一完整 source SHA。报告提交后必须另核对精确最终 head，旧源的通过不能替代该检查。下一项先登记旧/新固定 A/B/B/A；本轮到此停止，不追加压测、扩大期限或合并稳定版。
