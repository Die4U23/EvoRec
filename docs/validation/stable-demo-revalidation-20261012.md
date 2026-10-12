# 修复后的完整整合重验

## 实际结论与来源

按[预登记](stable-demo-revalidation-plan-20261012.md)，干净已推送 `6b9d3484ae32ae97cf55a7ed7a6921fa3992c16d` 的一次完整本地检查通过，原整合失败不覆盖。新 [PR #68](https://github.com/Die4U23/EvoRec/pull/68) 直接面向 main；#51–#67 共 17 个原 head 逐一确认为祖先，main 在本地检查和源 CI 复核时仍为 `4557ad24bb8d8a48f2313ca76d160623fdc8a32c`。

231 个 `src/db/scripts/web/tests` Git blob 集合与旧候选相同，仅保存边界测试为既有 `13e1e2d` 修复值；其余 230 个不变。整个集合与 PR #67 的 `0d7a423` 相同；生产及 workflow 与 `88cc8f1` 无差异，原 230 文件浏览器快照除这一个测试外原始字节一致。预检同时保存全部 231 原始文件 SHA，不把换行归一化作为任意源码修改许可。

`src/db/scripts/web` 另与 `25a2a03f496522a3b863c53195a1fa11bdab9dc8`、`34fec8e7fcdf7e82d8321cb77b35aebce4bb287c` 精确 blob 相等。因此继续引用[原两轮 120 笔与恢复](stable-recovery-20261011.md)和[原实际浏览器/停机成对恢复](stable-browser-backup-20261011.md)，不改写其 source_commit，**本轮没有重新跑完整模型负载或浏览器**。

## 本轮真正执行的检查

| 实际命令范围 | testcase | failure / error / skip | 退出码 |
| --- | ---: | --- | ---: |
| workflow 提取的 53 个 service 文件，真实 PostgreSQL fixture | 1654 | 0 / 0 / 0 | 0 |
| accelerated 检索三个文件 | 178 | 0 / 0 / 0 | 0 |
| accelerated ranker 一个文件 | 16 | 0 / 0 / 0 | 0 |
| Node 三个页面交互文件 | 114 | 0 / 0 / 0 | 0 |

各环境/范围分开记分母，不合并成不同用例总数。服务测试比原 1653 多一个明确保存路径参数，两项实际参数均执行；Node 是模拟页面交互测试，不冒充浏览器。service 全新 basetemp 在本轮 artifacts 内。两套 pip check、现有四份契约导出和契约 diff 均 exit 0；未对共享 DSN 直接运行 migrate。

完整 wrapper session 28203 实际 exit 0（chunk 05501d），全部子进程已等待完成后才编辑报告。服务命令墙钟约 18 分 53 秒，是整套回归时间，不是推荐请求耗时；运行期间工作树保持冻结，未重试、未加 worker/调期限。只读观察曾看到 CPU 累计增加及新临时目录，不把这些活动当作通过证据。

来源预检、最终 marker 以及独立 source-ci 复核均确认：主工作区仍为 `6e44594f55dd180ec4bacce1ed8d19e7bf4aee61`、对照仍为 `84c9bbc`，两树干净；.env/原 18 模型文件未改。初末 test schema 均为 0；本轮四个确切测试进程已不存在，前轮三个 PID/监听也均为 0。只读查询，无额外清理其他 schema/进程，业务 8000 未重启。

## 原失败、负控制与网络错误

[原整合](stable-demo-integration-20261011.md)仍为 1653 项中 1 failure、0 error/skip；其 XML 摘要精确不变。[单独修复](checker-path-isolation-20261011.md)的同模块两种根各 112 项及有效删守卫 2/2 failure 原档保留；独立复核 mutant 只缺一条 guard、测试字节与本轮相同。也保留最初收集错误，不把它算成有效负控制。本轮完整通过不改写“初轮全部通过”。

QA-009 保留四次连接/备用通道错误。SSH 443 + HostKeyAlias=github.com 使用既有已信任 key 且 StrictHostKeyChecking=yes，已实际 ls-remote 和 push 成功；只改本轮命令，不修改全局 Git/known_hosts/remote，不关闭验证。22 端口关闭的根因仍 unknown，不当应用 504。[台账](stable-runtime-bugs.md)及时保留症状和实际更正。

## 精确 CI 与后续合并门禁

源提交的 [push 38206194766](https://github.com/Die4U23/EvoRec/actions/runs/38206194766) 与 [PR 38206249785](https://github.com/Die4U23/EvoRec/actions/runs/38206249785) 各 service-integration / retrieval-accelerated，共四项 completed/success，所有步骤成功；API head/event、job URL、PR rollup 四项身份一致，无 failure annotation。PR watch session 1532 实际 exit 0；source-ci 独立复核 session 55631 实际 exit 0。四个 job 的 Node20/强制 Node24 警告仍保留，不称零警告。

报告提交只改文档；最终 head 的四项 CI **另行**在 PR #68 核对并归档 final-ci，不用源旧绿色替代。只有最终 CI/来源/保护全通过且 main 未变才按协议普通 merge，不 squash/删分支；合并 SHA 与其独立两项 main push CI 结果由 PR #68 留档。旧 PR 记录和失败不删除，不声称旧草稿逐一独立合并。没有新 release/tag/版本号，也不是长期稳定、20 QPS/300ms/99%、异机灾备或历史超时唯一根因证明。

## 私有证据摘要

以下路径相对本轮忽略目录 `artifacts/diagnostics/stable-demo-revalidation-20261012`；原 XML/日志不提交 Git，公开协议足够明确各实际命令和边界。

| 原始文件 | SHA-256 |
| --- | --- |
| checks-r1/preflight.json | b70b62256b32e311d4ea13b9600a2b410ff7ede591db404ad3798863338c9736 |
| checks-r1/service.xml | 49338a399f579eafe0397f2aa15cf8664d8c87e1b5519cc503e49e0a80553939 |
| checks-r1/retrieval.xml | 25938be47ae8773c22132f1bb0a5426130718dfe78a1998b96fdd65152975ca4 |
| checks-r1/ranker.xml | e4311a497ae021daeb78c9140edcdbe6285908a028dd11afd6a0f7a37d89a0a4 |
| checks-r1/pages.xml | 4180d91306114c84e12c1f5f730b0d604a90a2277dfaabac0e2dca726c9d99e2 |
| checks-r1/final.json | 37958583abe7674d28eeba15f284beb80aee884a90086b8c5df17bc145aa8b8d |
| source-ci.json | f250cc35598e14a2519f2f35d3060f5bfebe11097aab9692db791fb538c32abb |
| run_revalidation.py | efc622f516da7aea3c9d5884337fb9bdae5e66c41f4eba7d4522aa0da44544a4 |
| archive_ci.py | b9b7c09493c1c2a88f4a3ad35b3e0d4352a059c6e5db51769a8765c74eadc631 |
| ssh22-failure.json | a20661cb090f9269077907298393abd65f629f66df2beb5a1cc1045024255401 |
| regression-exit.json | a50698357b3b366b8bd63d7e5fc61e67cc875e2a9820252ead190f0b4b975b13 |
| watch-source.json | 52afe7134c4a10fe2928bca9bd76e2d98e80851509983b1985badb5733c4bce6 |

旧失败 XML 仍在前轮目录，SHA `345abccb044ce59ffbd8039cbe7aedc8f626aa943c1c966f50d5dca45be923e5`，四份原公开报告摘要在预检/独立复核中逐一相同。team-mode 接口缺少必需 agent_type，主线程串行复核；git-ship 显式路径/中文提交，无 Claude trailer。
