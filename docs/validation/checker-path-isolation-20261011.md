# 保存边界反例的独立项目隔离与反证

按[预登记](checker-path-isolation-plan-20261011.md)，干净已推送 13e1e2d0dd21008a214bda83cf6d892bf319f599 修复 GATE-002，只修改 tests/test_r06_trace_abba.py。**路径测试修复通过，不等于主干整合/完整本地回归通过。** [初轮整合](stable-demo-integration-20261011.md)的 1653 项/1 failure 保留，未自动重跑；main、PR #66 和其他候选 PR 未合并/关闭。

## 修复与实际执行

原反例隐含 pytest tmp_path 必在真实项目 artifacts 外；初轮临时根在 artifacts 内，真实 checker 合法允许而反例误报。新测试用 tmp_path 内的独立模拟项目，monkeypatch __file__ 只改变测试所见项目根，subprocess_sources 仍为原合成指纹。**实际 CLI 验证、路径解析、保存和 marker 不替换**；合成报告不写真实项目 artifacts。

两个参数分别构造项目外目录和 artifacts 根目录；各自均先检查合法子目录首次保存成功、第二次拒绝且原字节不变，再验证边界拒绝明确 failed_check=save boundary、非法 marker 不存在。有效输入资料完全通过前置系列检查，不能因缺文件或格式错误偶然拒绝。

| 固定检查 | 实际结果 |
| --- | --- |
| 同模块 / 临时根在真实 tmp | 112 testcase、0 failure/error/skip，exit 0 |
| 同模块 / 临时根在真实 artifacts | 112 testcase、0 failure/error/skip，exit 0 |
| 独立删守卫副本 | 仅删除 CLI 一条 save boundary require，原两个参数实际 2/2 failure、0 error/skip，exit 1 |
| 源码 CI | push 38142574242 / PR 38142609714，各 service-integration/retrieval-accelerated 四项 completed/success，所有步骤成功；API/rollup 独立核对，watch exit 0 |

两份 112 项是相同模块在两个根目录的条件复验，不是 224 个不同用例。负控制原测试字节与冻结源相同，生产真脚本未修改；私有 conftest 仅在测试子进程加载一行 mutant，其他验证函数来自真脚本。两个失败发生在守卫缺失后非法目录返回 0 的断言，证明反例可发现真实放行，不计为生产失败。

## 负控制收集错误与证据

初次独立负控制遗漏 tests 目录的 PYTHONPATH，无法导入 test_r06_timeout_trace：XML 为 1 个收集 testcase、1 error、0 failure/skip，两个边界用例均未执行。立即记录 QA-008、保留原 XML，不把 import error 当作有效负控制。补齐确切原 tests 路径后使用全新 XML/临时根，才获得上述 2/2 failure；测试及 mutant 字节不改。详见[问题台账](stable-runtime-bugs.md)。

忽略根 artifacts/diagnostics/stable-demo-integration-20261011；失败与更正文件不同名，不覆盖。

| 原始证据 | SHA-256 |
| --- | --- |
| path-outside.xml | b82d2cc5a1ff99c7e5375c3035ca990afbe496f6a450ecdd7f2adf3717bc094e |
| path-inside.xml | 4a97536e25f213375869b994fc855b2595980c2f08a4d5a26f1a59ad887ac9cd |
| path-negative.xml（收集错误） | 585c080e9b7720c52aba8c9b7d0ea92ee4a4dd6377ea14e1d71c495a49cda001 |
| path-negative-corrected.xml（有效反证） | 503c84b471851055c2079ab9e42ff36426e58c68119856ce83ff362388a1be57 |
| qa-008-negative-import.json | 3308086e8e508bee54d668d5ad491e302f21a87aa0a930a80c6d751cd2374cfa |

独立 source-ci 复核正常/错误/有效负控制的非空 XML、两副本字节、原 1653 项失败 XML 未变；相对 88cc8f1 的 231 个运行/测试 blob 只有这个测试文件不同，生产 src/db/scripts/web 精确不变。原始 230 文件除该测试外仍一致，保护主树/对照树/.env/18 个模型文件未变，测试 schema 集合为空。本轮不启动完整模型/API/浏览器，不删旧证据。

最终报告 head 的 push/PR 四项 CI 在[草稿 PR #67](https://github.com/Die4U23/EvoRec/pull/67)另核对，不用源旧 CI 替代最终提交。CI action 仍有 Node20/Node24 警告。下一轮将本修复纳入整合分支后，另行冻结/登记完整本地 workflow 检查，再决定 main 合并与 main CI；本轮不创建 release、标签或修改 package 版本。

team-mode 接口缺必需 agent_type，主线程按质量/效率/复用串行复核；git-ship 显式路径、中文 commit/PR，无 Claude trailer。GATE-002 可关闭；历史 R06-001/OBS-001 与整体稳定/SLA、作者实操边界不连带关闭。
