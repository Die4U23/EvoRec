# 生命周期修复：干净提交双轮负载与恢复记录

日期：2026-10-12。执行源码：`488847a9ad71dbdc6c3a04facf44097fed03f7f0`。按[运行前提交的计划](r06-lifecycle-frozen-plan-20261012.md)，固定 r1 → r2，两个独立隔离环境，全部执行完成，未自动重跑。

**结论：该本地冻结提交通过指定双并发负载及恢复门禁；尚未推送、取得本补丁的远端 CI 或发布稳定版。** 这是本地干净提交验证，不称已推送源码验证。R06-001 历史间歇超时保持开放。

## 修复与回归来源

修复 owner 不匹配时的失败清理写入，以及快照恢复线程取消时提前释放执行租约。最新交付代码上的原 3 项失败、修复后 30/694 项重叠回归和未提交版本的开发负载均见[前一阶段报告](r06-lifecycle-integration-20261012.md)。冻结后生产和测试代码没有再改，不把已有 694 项称作本轮重新执行。

运行使用正式 `scripts.verify_r06_reliability` 的 `--samples 120 --concurrency 2 --strict-load`，不使用前轮开发 helper。两轮运行前后都由工具要求源码干净且 HEAD、全部 src/scripts/db migrations 指纹一致。无新增连接池、缓存、线程或期限修改。

## 固定配置及实际结果

完整批准模型 137,249 件商品，NumPy，dense/k=10，单排名 worker，原 2 秒 API 期限。bundle `bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec`；manifest SHA256 `9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6`。轮内同一示例会话，每笔新键和新 HTTPX client，关闭服务插桩/GC 观察，无自动重试。

| 指标 | r1 | r2 |
| --- | --- | --- |
| HTTP 成功/固定样本 | 120/120 | 120/120 |
| 504 / 传输失败 / 持久门禁失败 | 0 / 0 / 0 | 0 / 0 / 0 |
| 持久 completed / accepted 残留 | 120 / 0 | 120 / 0 |
| 客户端总耗时 p50 | 1627.822 ms | 1631.101 ms |
| 客户端总耗时 p95 | 1854.707 ms | 1798.527 ms |
| 客户端总耗时 p99 | 2056.550 ms | 1974.844 ms |
| 闭环批次时间 | 99.559 s | 99.251 s |
| 观测完成吞吐 | 1.205 请求/秒 | 1.209 请求/秒 |

每笔合法模型/会话/请求身份、10 件结果及位置/分数/来源均与独立只读账本匹配，执行租约释放。恢复请求另列，不计入负载 120 分母。客户端总耗时包括 client 创建和传输等成本；r1 p99 超过 2 秒不等于服务内部超时。这不是开放到达率容量测试，不推导 20 QPS 或优化倍数。

两轮各自完成初次 dense、真实进程重启后的精确原键重放、同键并发唯一结果、客户端断连后 completed、观察 accepted+owner 后的自有进程硬杀、重启原键中断收敛和新键成功。两次崩溃结果均为 `interrupted_without_partial_result`：原键 failed、零结果。每轮 6 个恢复持久审计均有效、租约释放。

## 独立检查与清退

每轮结束后，调用现有 `check_r06_stable_recovery.check_round`，从原始记录重新检查样本覆盖、汇总、模型、源码、恢复和生命周期；随后独立连接数据库确认确切 owned schema 不存在，TCP 确认对应端口不监听，PowerShell 确认对应 PID 不存在。r1 清理核对完成后才启动 r2。

两轮共六个 API 进程，四个正常 CPU drain、两个按预登记用于崩溃恢复的硬杀；两个自有 schema 均清除。只检查本轮记录的资源，不清理业务实例或其他工作树资源。

本地原始记录位于 `artifacts/diagnostics/lifecycle-frozen-20261012/`，以下 SHA256 对应各轮子目录。每个 `independent-check.json` 同时保存 checker 核对过的原始证据摘要。

| 轮次/文件 | SHA256 |
| --- | --- |
| r1/verification.json | `bbe26a116a20dfddc6bc5722330a2d57cbd828115cc0325e78b33866f6c30568` |
| r1/observations.json | `4f4916da84ba620f0d4731ff67f4e320989394a0486f8555520d95fd9946041e` |
| r1/independent-check.json | `c7cbefdc1a36b3a5b94516d8afa930cc45575ee0cc5ce208155a030ae0427f28` |
| r2/verification.json | `7b42191f9d025c23e29caffa3cb97e1c75374bcce41785abcd2652516928a7c7` |
| r2/observations.json | `02490d2384532b57fdb93d41737d32ed34007354c726f67862938b623600d5bd` |
| r2/independent-check.json | `d83baca102c437dba3da9ac16a8be4432102474862c3ca3c9bf697c89174842a` |

操作异常另记：r2 资源检查通过后，一条只读哈希打印命令出现 Python 括号语法错误；更正后完成打印，没有重跑模型、改变原始数据或掩盖应用失败。事件保存在 `reporting-note.json`。

## 后续交付边界

执行源码已有本地提交，结果报告另行提交。分支依赖尚未合并的统一入口 PR #69，后续草稿 PR 应以其 `codex/demo-delivery-preflight` 分支为 base，只展示本次修复和证据。远端 push/PR CI、连续浏览器复演与作者讲解/修改验收仍待完成；没有 merge、tag 或 release。

本轮没有 504，因而没有新的失败内部阶段证据；不关闭历史唯一根因未知，也不将两轮成功包装为长期稳定或 SLA。正式结果明确 `production_acceptance=false`、`sla_proven=false`。
