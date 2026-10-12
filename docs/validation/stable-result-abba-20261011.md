# 结果连接旧／新固定对照：104 笔成功，稳定验收仍未完成

结论：按[先推送的预登记](stable-result-abba-plan-20261011.md)，完整 137,249 商品、NumPy、原 2 秒期限、单 worker、全部上架目录、未插桩 A/B/B/A 四轮负载各 24/24，另各两笔顺序成功。全部 104 个身份有效，无 504、其他状态或 transport error，四轮均独立实查正常清退。旧版也全成功，不能关闭历史 R06-001 或把有限差值称为因果收益；本轮不是稳定版／SLA 通过。

## 版本、机制与测量口径

协议 `631ebb292b61958cb6e3cb4840fb7ab20ee6f0d6` 先提交推送，核对远程 SHA 后才新增测试和测量。A 为干净旧版 `2f50e6226380eba5e3a7c382dedb58b44ad7aeba`，B 为干净冻结且已推送 `c3a89d9d7ff31387e0e133da9b73d3b5158dcdb1`。本轮只新增校验器、测试、CI 选择和文档；B 的 src/db 与上轮修复 `1a15ebdbb2eafb1d5113a06ec4b1fb2456840c77` 字节相同，相对 A 仅 postgres.py 与 recommendation_execution.py 两个生产文件变化。结果事务安全复用／断连缺陷修复机制见[上一轮报告](stable-result-lease-20261011.md)。

双版本校验器要求注册的生产差异、完整源哈希绑定，诊断 scripts 只允许本校验器新增及 profile 的插桩覆盖 helper 变化；删除该 helper 后整个 AST 相同，sample/profile/main、服务 lab 与其他依赖保持一致。每轮从相应工作树启动，PYTHONPATH 显式指向该树 src；启动前独立子进程确认四个生产／诊断模块文件均属于该树。记录 A 的 147 个、B 的 148 个 src/scripts/db migrations 文件 SHA。B 的两处生产字节 SHA 仍为 `334f74df224c7a91eb352d1e18f52e812e054e30ef356e2e3596e4d6ddeba199`、`0f6b2ffea2e5b666b9e353624eda85457817954ccc0e4a06e1b4363887e94bd6`。

批准包 `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`，manifest `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`，模型 `eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc`。Python 3.12.6、NumPy 2.1.3、HTTPX 0.28.1、psycopg 3.3.6、Windows／PostgreSQL 18.6。四轮固定顺序，各自新 schema/API/随机非 8000 loopback 端口，轮内同一 sample 历史，dense/k=10、每笔新键／新 client、trust_env=False、client timeout=10 秒、不重试，关闭 trace、GC 观察、phase gate。没有改连接池、worker、队列容量 8、目录 SQL、恢复或期限。

## 全部请求结果

以下分位数单位 ms，使用每轮全部 24 笔负载，nearest-rank，不合并源码不同的分布，不扣观察成本。客户端总耗时包含创建及 HTTP 往返；启动、完整包准备与清退不在请求延迟内。各阶段分位数不能直接相加。

| 轮次／版本 | 负载成功／504 | 顺序成功 | 创建 p50／p95 | HTTP p50／p95 | 总计 p50／p95 | 负载墙钟 s |
| --- | --- | --- | --- | --- | --- | --- |
| A1 旧版 | 24/24／0 | 2/2 | 337.556／592.196 | 1263.668／1492.735 | 1636.439／1943.084 | 20.325 |
| B1 候选 | 24/24／0 | 2/2 | 362.846／555.959 | 1212.350／1337.510 | 1592.512／1736.063 | 19.447 |
| B2 候选 | 24/24／0 | 2/2 | 347.086／427.090 | 1261.488／1408.983 | 1621.637／1740.342 | 19.354 |
| A2 旧版 | 24/24／0 | 2/2 | 333.591／468.332 | 1335.773／1669.339 | 1683.584／2140.229 | 20.772 |

两版分别 48/48 负载和 4/4 顺序成功，全部身份通过；无新增请求或重跑。候选两轮 HTTP p95 较低，但 B2 p50 与 A1 接近，主机／客户端成本仍波动；不据四轮建立因果或长期可靠性结论。所有 ASGI、SQL、队列、CPU、结果写入及失败内部阶段本轮均 unknown；无失败不以空集证明超时机制。完整包持久终态／同键重放本批未采样，不能由停止后文件反推。

每轮前 CPU 快照为 14%、16%、11%、8%，空闲物理内存 3,518,748、3,391,252、3,544,236、3,538,212 KiB。它们只说明采样时刻，不证明连续独占／CPU 原因。创建 p95 427–592 ms 仍有波动，OBS-001 保留开放。

## 真实测试及验收操作错误

干净 c3a89d9 九模块 418 项，零失败、错误、跳过，127.877 秒；覆盖本轮双来源、遗漏／来源混用／隐藏失败／全失败／插桩与额外生产变化反例、CLI 新文件保存与重复保存拒绝、源码变化后拒绝、真实 TCP／四阶段 2 秒超时、结果事务 ownership/commit/rollback/close 与恢复。原修复版的 520 项证据仍只称上一轮，不冒充本轮重复执行；模块范围有交叠，不累加人数。

| 原始 XML（均本地 artifacts 下） | tests／failures／errors／skipped | SHA256 |
| --- | --- | --- |
| result-abba-target-20261011.xml，开发首组 | 147／0／0／0 | `c21406889ac1fd5c26ea260683ba988741b9276477ed7ec171625b067475351e` |
| result-abba-review-20261011.xml，遗漏数据库变量 | 355／0／14／0 | `9beff37f8ec8c5e7087eade2d656dbcb296507262ca92a43db81b34dd74e6fba` |
| result-abba-db-review-20261011.xml，显式配置更正 | 355／0／0／0 | `485af0903948267ba4b3e23f46845783575f647dfcb5d0d69ba0d49ba5c9b96e` |
| result-abba-final-target-20261011.xml，提交前定向 | 155／0／0／0 | `7fa9f73a583a69f717d4aacc3020875834f6142d3c84ddd38a9c28df2a14c0e0` |
| result-abba-clean-20261011.xml，干净冻结源 | 418／0／0／0 | `1c516ade2cc98865304e4aff025c8394650b3ddfee7b13ced669a16b6f803bc2` |

QA-002 是验收命令漏传 EVOREC_DATABASE_URL，不是生产 bug；fixture 在初始化报错而非跳过。第一份失败 XML 和基于协议提交的四文件完整字节快照保留，立即落盘事件；更正环境后另开路径，未开始／重跑完整包测量。QA-003 是四轮完成后额外只读审查尚在运行时提前恢复 A 工作树，来源保护拒绝（session 47060，exit 1）。记录失败后等待进程退出，临时切回精确 A，仅重做只读资源审查；确认 exit 0 后才恢复旧提交。没有增加、覆盖或重跑四轮请求。这两项及其修正单独记录在[台账](stable-runtime-bugs.md)，不把纠正后的成功抹成“一开始全通过”。

## 独立清退与原始绑定

校验器重算 26×4 逐笔分母、200 身份、504、汇总、源及录制 lifecycle；series zero_failures=true，但 production_acceptance=false、sla_proven=false。校验器的 live_resource_checks_performed=false；启动／结束／独立机器和数据库观察在本地 launcher 单独执行，不能把离线文件检查称为现场实查。

四轮各自正常 CPU drain、无硬杀；逐轮及额外总复查独立确认下列确切 schema、实际 PID、监听均不存在，最终 test schema 总数 0。临时请求数据已随 schema 删除、不能恢复；原始报告、失败 XML、快照及模型保留。主目录仍为干净 `6e44594f55dd180ec4bacce1ed8d19e7bf4aee61`，.env SHA `32f05a41febf54595c5cef010ecee661c47570ac9e0a41238501b9297939e267`，模型三文件 SHA 与上一轮相同，业务 8000 未操作。对照工作树最终恢复干净 detached `84c9bbcb96f4e428ab5aaf7f6eebdc743e0dc418`，恢复后核对 A 原始文件哈希未变。

| 轮次 | 自有 schema | 实际 API PID／port | profile.json SHA256 |
| --- | --- | --- | --- |
| A1 | test_evorec_defebf5da4234c3fb1a6b5bf7358e86b | 27280／51042 | `026b96fe38e10826eb8ec04087a46e9792c2289ed79f7012b66f5951134db3a4` |
| B1 | test_evorec_59ff553c1a6844b998d5272b274baa5d | 13768／51262 | `1c5e807c3892acb3f111b63dc45deb6246d0d55671f8292a22de798de5548c64` |
| B2 | test_evorec_7f6df1f3a0cb4f4fb2f182cfcfe61ecd | 26956／61595 | `874718bc39af86ad8955dd731f55306f89bb1d97339b958f13ebbfb05513d572` |
| A2 | test_evorec_1075a91e516b4907a3885a6895152cf9 | 3700／57112 | `8a106cdff63181336f87e09527d6dd2489b2fff0bbdee43f8faf443f1e09c4b8` |

A 原始目录在 r06-capture-control-a 工作树，B 和辅助记录在 r06-probe-controls，均为 artifacts/diagnostics/stable-result-abba-20261011 下对应轮名。完整 raw 指纹见 series-check/archive-summary；辅助 SHA：

| 本地记录 | SHA256 |
| --- | --- |
| environment.json（双方完整源指纹及版本） | `d04e619c54a7fa5489b85752775dff0aaa28923fe28f93222dbbaf5616d6888f` |
| series-check.json（公开检查器四轮） | `10a72dffdf9949cda404c88603b78d73f75f1647221da9d4e13a9cf235e2bbc1` |
| archive-summary.json（离线绑定、顺序、XML） | `a8c0107393483a8b82a78b18e2f83c0fb939d5148391324e1081c4874c3cf653` |
| final-live-all.json（实际只读总复查成功） | `190b448133748f34ac6f76a67958ebc452833dc5eeb15de342a9925ea7fa674c` |
| restoration.json | `706b83a1743ccf6e912ad0211c21210277fa176535e00b1f5c19ec80e9a2b14b` |
| qa-002-corrected.json（含失败源完整 SHA） | `db9f59fe835dd390c4763d817fc2653f84a63e685ac90810ea6ff5e4c8394f7c` |
| qa-003-premature-restore.json（失败未覆盖） | `c12f8861df0563ea0dba51e0f318336e7baa825f204c02dee9ffd331dd4b1598` |
| run_controls.py（本地固定执行与实查） | `0f37c4a094ac4b2f5392a1d0108d8bf7cbdbbd7ccd31dd786a00f103b73fb38a` |
| archive_checks.py（仅离线校验） | `464045706e2c40af96418f6d24dacbb9ff604fce6d72b825710b6ea71a8d9761` |
| final_live.py（只读总实查） | `4b426112bdc2e4bde18b6f1df9f30ad4a1dc87cef47cd015b1c8d588d4f7dfe8` |

## CI、审核与下一门禁

冻结 c3a89d9 的 push run 38129944588、PR run 38130193945 均完成，两项服务／数值 job 各 SUCCESS，四项通过。新增测试明确列入服务 workflow 和卫生断言；本地 Git index 卫生检查通过，凭据扫描仅覆盖常见签名、不是穷尽审计。git-ship 中文提交、显式暂存、不加 Claude trailer，保留依赖 #62 的草稿 [PR #63](https://github.com/Die4U23/EvoRec/pull/63)。结果提交最终 head 的 CI 另核对，不沿用 c3a89d9 的绿灯；本报告测量绑定仍是 c3a89d9，不改写成报告提交。

team-mode 必需 agent_type 接口不可用，本轮主线程串行分质量／效率／复用复核：保留现有单轮检查、只增加双来源与原始门禁，不变更在线路径；没有独立代理审核证据。下一项允许另行预登记并执行候选两轮各 120 笔，以及完整模型恢复／同键唯一／过期会话响应、浏览器和备份验证。本批尚未执行这些稳定门禁，R06-001 和 OBS-001 保持开放，不合并或发布稳定版，不外推 20 QPS、300 ms 或长期 SLA。
