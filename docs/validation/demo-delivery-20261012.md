# 统一 Demo 入口与预检验收

按[预登记及开发补充](demo-delivery-plan-20261012.md)，干净已推送 `4a1f1df1ccb7f04ff3f74272f6773ed0b85fdf94` 完成新入口的只读预检与真实 TCP 复演。main 基础为 `f554869`；[PR #69](https://github.com/Die4U23/EvoRec/pull/69) 保留本轮实现/失败/精确 CI，暂为草稿，不是 release。

## 实现与边界

`python -m scripts.demo catalog / r06` 统一配置、来源与依赖检查，默认生成新 UUID 输出；`--check` 有界读取允许的四个字段，检查只读数据库/权限及完整批准模型，不建目录/schema、不迁移或启服务。预检成功不是 ready/验收。运行复用两个旧入口的生命周期，不添加子进程/改清退；CLI 作用域清理业务 EVOREC 变量并在退出/异常后恢复。新旧入口共用 guard 只新增 service/继承 hostaddr 路由拒绝，保留显式 loopback 优先级和原 SSL/GSS 策略。

`src/db/web`、catalog 旧入口和算法/期限/worker 与 main 无差异；R06 旧脚本仅共享 guard 六行增补，不把完整脚本说成字节未改。操作见 [简明入口](../../ops/demo-quickstart.md)，配置样例不含真实凭据。当前主工作区仍在旧分支，未切换/重启 8000，不重新安装共享环境。

## 保留的开发缺口与有效负控制

- DEMO-001：原型107服务/198加速虽然通过，实际 E 可编辑包导入错树仍被依赖检查接受；新增反例实际1 failure/零error/skip，完整未提交源和事件保留。补 import_source 检查后原例通过；本轮冻结源在真实错树共享环境下 CLI 实际 exit1/code=evorec_source_mismatch，尚未连接数据库/API，不自动改 sys.path 或重新安装。
- DEMO-002：显式连接参数未覆盖 libpq 继承地址/service，原三个 guard 反例实际 failure、零error/skip；DB probe mocked，**未尝试远端连接**。按 [PostgreSQL 环境默认值](https://www.postgresql.org/docs/18/libpq-envars.html)补共享隔离保护，原例与显式/继承 loopback 正例通过。不是历史504归因或已发生的远端数据泄露。
- 独立副本仅删新入口对旧隔离 guard 的调用；冻结源原测试文件字节相同，项目外、artifacts 根及已有输出三个参数均真实 failure/exit1、零error/skip。不是 import error。首次副本和最终副本都保留，不改原源码保护。

[台账](stable-runtime-bugs.md)与三轮开发源字节快照、原反例 XML 保留；不同阶段不合并成“初轮全绿”。原主干1653项失败、四份历史公开报告也逐一摘要未变。

## 冻结源真正执行的验证

| 范围 | testcase | failure / error / skip | 实际退出 |
| --- | ---: | --- | ---: |
| 新 CLI、两个旧 Demo、仓库卫生四模块（实际 PostgreSQL） | 116 | 0 / 0 / 0 | 0 |
| 真 NumPy 包预检四例、检索/benchmark/ranker | 198 | 0 / 0 / 0 | 0 |
| Node 三个页面文件 | 114 | 0 / 0 / 0 | 0 |

两套 pip check通过，完整137,249项批准包 NumPy只读CLI预检通过，check-only目录未创建、schema仍为空。NumPy小包的DB probe stubbed，不冒充真实数据库；真实完整包预检使用实际只读连接。

新 catalog CLI：独立进程/随机schema/loopback，合成CSV错误原子拒绝、导入/构建/显式发布、新品资格、下架/回滚不复活及原键重放均通过；不是浏览器文件选择操作或R06任意新品。

新 R06 CLI：完整批准包/NumPy/原2秒/无重试或服务插桩，使用既有固定 verify_r06_http.exercise。示例/新用户、独立原始热度对照、真实dense身份/Top-10、原键精确重放、错误owner/改输入拒绝、详情/收藏/隐藏/撤销、full_seen、重置后三笔新计算及逐键重放、旧epoch反馈拒绝通过。14条推荐观察含12个200，以及**预期**错误owner的401、同键改输入的409；不把预期拒绝隐去或写成14个200。首次dense客户端1.124秒，重置后三笔1.211/1.158/1.238秒，单轮少样本，不算SLA或性能收益。

两入口stop文件正常退出，worker/CPU drain标记、唯一schema不存在、确切launcher/ready PID与监听均独立检查为0，catalog临时令牌删除；未硬杀。formal wrapper session14457实际exit0后才编辑文档。初末test schema=0、主工作区/对照/.env/原18模型文件未改，原模型未复制/训练/重算实验。输出与原始证据保留，临时业务数据已删除且页面不能恢复。

## CI 与交付剩余项

源码 [push 38214770405](https://github.com/Die4U23/EvoRec/actions/runs/38214770405)、[PR 38214848373](https://github.com/Die4U23/EvoRec/actions/runs/38214848373) 四项 completed/success、所有步骤成功；新测试显式纳入workflow，NumPy包不能缺失/skip。API head/event/jobs/rollup四项身份相同，无failure annotation；watch实际exit0、source-ci独立复核session85496实际exit0。Node20/Node24警告保留。

报告只改文档；最终head的新四项CI另在PR #69核对/归档，不用旧绿色替代。尚未完整连续浏览器演示、新CLI Ctrl+C实机或干净机器复建，不代签作者讲解/亲自修改；版本仍0.0.1，未创建tag/release。本轮不是新120笔负载、长期稳定、20QPS/300ms/99%或历史超时唯一根因证明。

## 原始证据摘要

路径相对忽略目录 `artifacts/diagnostics/demo-delivery-20261012`。raw XML/日志/模型不进Git。

| 文件 | SHA-256 |
| --- | --- |
| formal-r1/preflight.json | 44ce2d0df15e588aadce81bedb86971b9831cbe12821d52a4007a7033688b639 |
| formal-r1/service.xml | e2b7e32e35fc9647d339c68b8d952f57e0ba438b46ca48deee9c9b1d2451c4fe |
| formal-r1/numpy.xml | 8f8ab9a2f9467d10ef5c712748c6833b8dea67c9b3f05d8e7dd95845af7f3196 |
| formal-r1/pages.xml | 0ab600d3246c1c3dcc792350591ba62491393ac43f5c93ca64cbc0ea93a082c2 |
| formal-r1/final.json | 6425737021315fbb381b528127dca16bf3c5f131da27e5956cbcce962848e15c |
| formal-r1/r06-verified.json | 6f6ec70b272e2707442e5e3bcfdd4365d66fa496c27279fdb4c5e10386754c72 |
| formal-r1/catalog-verified.json | fd214571da95a027b0c897510032757447e418a1fd0db2b5ee4e04222632edb4 |
| formal-r1/r06-observations.json | fc550c0db6dacee157009f0eda69380f713cf4b668a69216e1403ae818c3d870 |
| source-ci.json | 78d92af27b94a2fbafd32b671499a0c16b53ce2d405cb621a83716ffda11202a |
| origin-before-fix.xml | 8db0b6dc5a2ca3ab7f03fa292db0afb8d133d1be6afc6f85c99dc6eba862d434 |
| routing-before-fix.xml | a22f98d4d22670430d47996f09093d7ea3b21c44293aa5b6e36cc2435fd100a7 |
| output-negative-final.xml | afa1e269134f6c0fd41b6cb085ab83d103766cf56c3f666604d3f5c1ac19e418 |

team-mode的角色参数不可用，主线程串行检查安全/效率/复用并分开开发与冻结验收；git-ship显式路径/中文提交/独立PR，无Claude trailer。不因技能或CI代替未运行的验收。
