# 主干整合初轮：来源和 CI 通过，本地回归拒绝

按[预登记](stable-demo-integration-plan-20261011.md)，独立整合分支冻结源 1c178a16a9e6c866223f56d72243599534901b7d。**本轮未通过完整门禁，不合并 main，不关闭原 PR，也不发布稳定版。**

## 来源检查

远端 main 为 4557ad24bb8d8a48f2313ca76d160623fdc8a32c。merge 5f98909 的完整 tree 与 PR #65 的 88cc8f1 完全相同：dc301a8c79fd335c3fadea6de1cd4d2f75a56da2。PR #51–#65 的 15 个精确 head 均逐一核对为冻结源祖先，保留历史与撤回记录；不是逐一接受中间候选的生产结论。PR #44 的旧连接试验未追加，尚未关闭。

src/db/scripts/web/tests 全部 **231 个 Git blob** 与 88cc8f1 精确一致，增删也检查；前轮原始字节快照的 230 文件仍匹配，额外的 db/schema.design.sql 此轮以 Git blob 核对，不将两种分母混为一谈。src/db/scripts/web 另与双轮负载源 25a2a03 比较零差异；历史模型负载/浏览器/备份结果仍绑定各自原提交，不冒称本轮重测。

## 本轮固定执行结果

| 检查 | 实际结果 |
| --- | --- |
| 两套 Python 环境 pip check | service/accelerated 各 exit 0 |
| 完整 workflow 服务集合 | 从冻结 workflow 提取 53 个文件；1653 testcase，1652 通过、1 failure、0 error、0 skip，实际 exit 1 |
| 本地 Node/加速检索/ranker | 因服务回归失败停止，未执行，不计通过 |
| 冻结源 CI | push 38140998564 / PR 38141108270，各 service-integration/retrieval-accelerated 四项 completed/success；API/rollup 精确核对，watch exit 0 |

失败是 tests/test_r06_trace_abba.py::test_cli_save_uses_fresh_marker_only_in_artifacts。测试将 tmp_path 当成项目 artifacts 外部路径；本轮使用全新的 artifacts/.../pytest-service，实际就在允许范围内，真实 checker 合法保存/返回 0，而测试误期望返回 1。由实际路径、测试 268 行、checker save boundary 与生成 marker 共同确认，**不是生产守卫失效，也不是应用超时**。测试构造依赖临时根位置，记为 GATE-002；完整 XML、stdout/stderr、来源和失败事件保留，不改写为全绿。

所有本地测试执行 session 79847 实际 exit 1 已等待；CI 总复核 session 83275 实际 exit 0 后才切换修复分支。只读实查测试 schema 集合仍为初始空集，前轮三个确切 PID/监听不存在；主工作区、控制树/.env/原模型及 230 原始源未改。测试的合成资料及私有证据保留，不批量清理陌生 schema 或上传原始包。

## 操作错误和下一步

QA-005：读取 tree 的未引用花括号被 PowerShell 误解析，exit 1；引用 revision 后精确核对通过。QA-006：额外进程查询在过滤器中重复 CIM 枚举，超出单次等待且多次未结束，主动只中断自有 session 49365，exit 1；一次快照后在内存过滤的更正命令 exit 0。没有中断应用测试，也没有据这些命令推断服务 504。详见[台账](stable-runtime-bugs.md)。

下一项单独分支修复测试几何条件：独立模拟项目；允许子目录成功/不覆盖旧 marker，项目外与 artifacts 根目录明确拒绝；临时根在真实 artifacts 内外分别运行相同模块，并对删守卫副本做负控制。[修复预登记](checker-path-isolation-plan-20261011.md)。生产脚本不改，不自动重跑本轮 1653 项；原整合 [PR #66](https://github.com/Die4U23/EvoRec/pull/66) 保持草稿，后续完整整合门禁另行登记。

## 本地原始摘要

忽略根 artifacts/diagnostics/stable-demo-integration-20261011；checks-r1 全新且不覆盖。

| 证据 | SHA-256 |
| --- | --- |
| checks-r1/service.xml | 345abccb044ce59ffbd8039cbe7aedc8f626aa943c1c966f50d5dca45be923e5 |
| checks-r1/preflight.json | b8d7f856f76418eccf04bfb8051af70b4228a0e8f90164c0d048e33187b9ff73 |
| bug-gate-002.json | cf0c71e81046d62388f4998c07ba08c2bc25f22ad7fb6faeba542d3bdbaa7fd1 |
| frozen-ci.json | a8e07968e52b974eb15e4b3c71f464a2a75c5defc6fbad50f51779553d8d6bcd |
| QA-005 / QA-006 | 46df4a68fc415c20dac9f780ea305c0ad0374b8ff5ff64fa23e2d56d773a1bae / 7df568518a11f0328d14591973c4838c214b1820572df7c6802973d6efe8dc1a |

team-mode 代理接口缺必需 agent_type，串行质量/效率/复用复核；git-ship 新分支、显式暂存中文提交，无 Claude trailer。CI action 仍有 Node20/Node24 兼容警告。任何 CI 成功不覆盖本轮失败，不宣称长期稳定或 SLA。
