# 诊断保存边界测试隔离预登记

来源是[整合初轮](stable-demo-integration-20261011.md)的 GATE-002：冻结 1c178a1、53 文件/1653 项实际 1 failure。原记录不覆盖。使用独立 codex/r06-checker-path-isolation，依赖草稿 PR #66，仅修复一个测试文件及报告，不改生产 CLI/运行时、模型、期限、数据库或工作流。

测试自己的“项目”移到 tmp_path 内，monkeypatch checker.__file__ 只供计算模拟项目根，保持真实保存/来源结构/结果检查函数。允许的 artifacts 子目录必须首次保存成功、第二次拒绝且旧字节不变；项目外目录及 artifacts 根目录分别拒绝，必须明确返回 save boundary，不能因输入资料损坏偶然失败。

冻结推送修复提交后，相同 trace ABBA 模块固定各运行一次：一份 pytest 临时根在真实项目 tmp/，一份在真实 artifacts/。XML 分别非空、零 failure/error/skip，独立核对 testcase。生成资料只在自有模拟项目，不向真实仓库 artifacts 写合成诊断。失败停止，不跳过反例，不自动全套重跑。

负控制仅在独立副本删除生产 CLI 的一条 save boundary require；不改真脚本。原两个边界反例必须实际 failure/exit 1/零 error/skip，保证构造仍能发现真实守卫缺失；原始负控制 XML 保留，不混进正常通过数量。

本轮不重跑 1653 项，不运行完整模型或真实浏览器；完整整合门禁仍待单独登记。显式路径提交、新 PR 保留 #66 的失败记录；精确修复源与最终报告 head 的 push/PR 四项 CI 单独核对，CI 自己的完整服务回归不改写成本地复验。主工作区/.env/原模型/对照树/8000/main 均不动，不先合并。
