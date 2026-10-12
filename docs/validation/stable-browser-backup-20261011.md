# 完整 R06 浏览器会话安全与停机备份恢复

按[预登记及启动前更正](stable-browser-backup-plan-20261011.md)，干净已推送 `34fec8e7fcdf7e82d8321cb77b35aebce4bb287c` 完成真实浏览器反馈/重置/旧响应隔离，以及完整模型的停机成对备份恢复。没有生产实现修改或追加负载，原 2 秒期限和单排名 worker 不变。**通过的是下列具体场景，不是长期稳定、生产发布或 SLA。**

本轮只追加两个 Node 反例、协议和报告，生产 src/db/scripts/web 与 PR #64 的 d6e12c3 无差异。协议 7c05658 先提交推送；反例 02f697c 另行提交；启动前检查修正 1118c4d/34fec8e 在正式场景之前提交推送。冻结源、230 个 src/scripts/db/web/tests 文件原始字节及摘要、私有 helper 摘要均归档；生产核心和推荐页面字节等于 Git blob。明确列出的五处测试/评估静态页 CRLF/LF 等价保存原始工作区与 Git blob 双摘要，不把 Git clean 等同于原始字节一致。

## 环境和实际结果

Windows；Python 3.12.6、PostgreSQL/pg_dump/pg_restore 18.6、psycopg 3.3.6、NumPy 2.1.3、HTTPX 0.28.1。批准包 bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec，137,249 商品，18 个文件共 128,908,673 字节；manifest 9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6，model_version eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc。只读原包复制到本轮私有目录，不移动或修改原包。管理员/后台 worker/服务插桩/GC 观察/phase gate 关闭，排名 worker=1、queue=8、API 期限 2 秒。

| 场景 | 实际观察及独立检查 |
| --- | --- |
| 首次自动 dense | 实际浏览器 sample/k=10，正确包/模型、10 件唯一商品、真实标题；不是热门冒充 R06 |
| 详情、收藏、隐藏/撤销 | 四次真实反馈，history_version 0→1→2→3→4；收藏 1 件、撤销后 hidden 为空，反馈后新 dense 排除两件已见商品。两笔 completed/结果顺序分数身份一致、租约释放、零 accepted |
| 旧响应与重置交错 | 唯一第三笔真实 dense 已完成（独立 SQL 三笔 completed/30 行/零 accepted），代理仅暂缓响应；获得本轮页面重置确认后 epoch 0→1、version 4→5，固定种子恢复、收藏/隐藏清空，首次自动新 dense 200/10 件且恢复初始 Top-10 |
| 旧响应到达 | 旧 Top-K 与新 Top-K 不同，避免无辨别力的页面比较；释放真实旧响应后，会话及整段推荐可见文本与释放前逐字相同。四笔账本仍 completed/精确结果/租约释放/零 accepted |
| 刷新持久会话 | 实际浏览器刷新，固定种子 0060501960、收藏为空、version=5；页面不缓存旧推荐卡片或自动再发请求，不把它说成“刷新自动重绘上一次推荐” |
| 停机成对备份 | API 正常 CPU drain/退出、禁止代理写入、零 accepted/无请求租约后，pg_dump/custom/no-owner/no-privileges 只备份确切自有 schema；dump 9,399,842 字节，同时复制完整受管目录，18 文件摘要一致 |
| 缺失产物防护 | 仅移动本轮模型副本到可恢复目录；原生产 manager.recover 检查 admission_open=false。这里没有启动缺失产物 API，**不是实际 HTTP 503 就绪证据** |
| 成对恢复 | 只删除已停机的自有 schema，确认不存在后 pg_restore；产物复制到全新目录、18 文件逐一摘要一致、迁移检查零待应用。新实际 API ready=200，同一 session epoch/version/history/favorite/hidden 精确恢复 |
| 原键与新计算 | 恢复后四个原键均 200/原完整正文精确一致，包含旧 epoch completed；另一个显式新键 dense 200/合法 10 件/原初始 Top-10。独立账本五笔 completed、精确顺序/分数/来源及模型快照、租约释放、零 accepted。实际浏览器刷新保持 version=5/收藏为空 |

浏览器转发 POST 共 10 次：创建会话 1（201）、反馈 4（200）、重置 1（200）、dense 4（200）。恢复后另有真实 TCP 推荐 5 次：原键重放 4、新键计算 1，均 200；共 9 次推荐 HTTP、5 个唯一推荐行，不合并为负载样本或成功率实验。没有自动重试、补样、重跑正式场景、延长期限或增加 worker。

代理延迟只发生在上游已完成的一笔响应上，未伪造页面/服务正文或注入页面 JavaScript；它用于会话安全，**不是未插桩性能证据**。未采服务阶段时间，不据这一批解释历史 504 或客户端成本漂移。真实浏览器 AX/截图观察保存在本轮工具记录，当前接口未导出可链接图片文件；私有 JSON 只是主线程观察摘要，不冒称独立截图复算。协议“刷新保持恢复结果”的表述过宽：本轮只证明刷新后的会话及持久原键终态，不证明自动重绘旧卡片。

## 测试机制和启动前错误

精确干净 34fec8e 单独执行页面脚本 **114 项**、实库备份/卫生 **17 项**，零 failure/error/skip；XML 直接数非空 testcase，不依赖退出码或 Node 不存在的 testsuite 计数字段。两个新反例分别验证重置前推荐迟到成功/失败不能覆盖新结果、污染标签页存储或复活旧请求键。

负控制在独立副本仅删除推荐成功/失败分支的两条旧请求守卫，生产页面不变；原两个新反例实际 **2/2 failure、exit 1、零 error/skip**。独立复核副本与源仅差这两行、测试字节完全相同，证明确实能发现该类回归。负控制失败保留，不计为生产失败或混入 114 项。

QA-004 两次启动前拒绝完整留档：第一次在 tests/web_evaluations.test.cjs，第二次在 web/evaluations.html；两次均零 schema/API/HTTP。一次性完整只读核对后限制五个具体 CRLF/LF 等价路径；attempt-3 的 230 文件原始快照/双摘要及正式场景通过。不是生产缺陷，也不能把原两次写成首次全绿。详见[问题台账](stable-runtime-bugs.md)。

## 清退、原始摘要和 CI

唯一自有 schema `test_evorec_38b07e5036404bddb5c252f001b0db41`，run_id `5c7262bd-cc5b-4972-ad5d-774f1a7efe0b`。两个实际 API PID 6976/33196、父代理 PID 33236，监听 52177/63336/52185；两个 API 均正常 CPU drain、无硬杀。关闭自建标签、代理与 API，等待父执行 session 15688 实际 exit 0 后，独立只读 PostgreSQL/CIM/TCP 实查精确 schema/PID/监听均不存在，确认全部来源和保护路径未变，再整理报告。业务 8000、主工作区/.env、原模型与对照工作树不操作。

只删除本轮实时测试数据库；本地 dump、完整备份/隔离产物、原始证据和失败快照保留，仍可在隔离环境恢复，不上传模型、dump、轨迹或凭据。原始根为忽略目录 `artifacts/diagnostics/stable-browser-backup-20261011`，正式数据位于 attempt-3，前两次失败目录不覆盖。

| 本地原始证据 | SHA-256 |
| --- | --- |
| source-environment.json | 3af1735fb3376d92bdab5d7567cb1826c76bf9036edd31854db9d06d37f94519 |
| reset-check.json / browser-check.json | 615b91a0224032a8b421c9990e10bcc8db6eb8bbd13a00316b1b4b7c6017a17b / 71d1330c212d018621eadfceaa5d265648f24f2fa2505008a20c86f3f3c31ae2 |
| backup.json / catalog.dump | c9a2fd44200b0757831f41767693ab2e1c8321e5546123f18394beec56bd4865 / 943e46a950152cd70e0cfe353c85b976521d81bd98233e6b7b690a1d81e53e2e |
| restoration-check.json | b1849d99d8c4284231bd698d6db01c1db600e04e4d41096abe77caf813d92ca4 |
| final-check.json / independent-final.json | e058da032580dae4d93e17b32d658f118d4e4d20a7967f6672a3d5ce8863174c / 80b5a1b63a9667f15edbe0cec82753b786dd6a93ef461d5cc14bd61cf1f9b569 |
| 页面 114 项 / 实库 17 项 XML | 0d6cbb3da0f941e5d64e691b21ea966bb359f0c9485d93638f5883da57758849 / 8671f527926775fce3330cfdc4647ee8256a24b526a39fb7ed2f62b2ed8ba6ef |
| 两项负控制 XML | 08df1d2eaba9fb421c67a99ab5efaf9366ec24f827e3b4dc5c1c441ab5911c17 |
| QA-004 第一/第二次事件 | 8ca5fd8312b78a8b5b1df83cb1aaee3dc3c239ceaeb0a35ad824f9aedafc1d9a / d065b0e6f08da59f584106802dbfe74ae73db8b83cce64c8bf4ff71e46aff58e |

冻结源 34fec8e 的 push 38137633699 与 PR 38137989177，service-integration/retrieval-accelerated 四项均 completed/success；watch exit 0，GitHub API/PR rollup/本地与 origin SHA 独立核对。只检查冻结源码 CI 不等于最终报告 head CI，后者在[草稿 PR #65](https://github.com/Die4U23/EvoRec/pull/65)另行核对。CI 存在 actions Node20 弃用/强制 Node24 警告，不称没有警告，不混入运行时修改。

team-mode 所需 agent_type 不可用，主线程按正确性/效率/复用串行复核；computer-use 操作唯一浏览器，git-ship 保留依赖 #64 的 PR。结合[原双轮负载及恢复](stable-recovery-20261011.md)，已补齐当前候选的指定工程场景；仍须整理未合并依赖、验证 main 整合版本/冻结入口，并由作者连续讲解和亲自修改。历史 R06-001/OBS-001 的唯一根因、长期稳定、在线备份、异机灾备、全新机器复建及研究/SLA 目标不计完成。
