# 修复后的本地演示整合重验预登记

本轮采用已确认的 `codex/r06-stable-demo-revalidation`，以 PR #67 最终 head `0d7a4237d7065271c80900a4969372c2376cb085` 为起点，另建直接面向 main 的整合 PR。不改生产功能，保留 PR #66 的完整失败和 PR #67 的正/负控制，不把重验覆盖为初轮成功。

## 冻结与等价门禁

- main 必须仍为 `4557ad24bb8d8a48f2313ca76d160623fdc8a32c`。候选生产 `src/db/scripts/web/.github` 与 `88cc8f17d10fb4a6a1652ea8a52bc11971505024` 精确 Git blob 相同。
- 231 个 `src/db/scripts/web/tests` blob 的文件集合不变，只允许 `tests/test_r06_trace_abba.py` 的 blob 为修复源 `13e1e2d0dd21008a214bda83cf6d892bf319f599` 的精确值；其余 230 个与旧候选相同。原浏览器快照 230 文件中，除该测试外逐一原始字节一致；明确五处既有 CRLF/LF 双摘要，不放宽一般源码检查。
- 源码提交并推送后才能测试，保存 exact head、全部 blob/原始文件 SHA、workflow、helper、原失败 XML 与前轮公开报告摘要。#51–#67 的原 head 应全部是本轮祖先。运行中不切分支、不编辑已跟踪文件或改写结果。

## 一次完整本地检查

1. 从当前 `.github/workflows/service-integration.yml` 提取 53 个服务测试文件；两套环境各 `pip check`，service 环境一次完整 pytest，全新 XML/临时根位于本轮 artifacts。预期 1654 个 testcase（旧 1653 加一个明确路径参数），零 failure/error/skip；另核对保存边界的两个实际用例均执行。缺失、异常、非零退出则停止后续，不自动重跑到绿色。
2. accelerated 环境分别执行 workflow 三个检索文件和一个 ranker 文件；Node 实际运行三个页面测试文件，分别保存 XML/退出码与非空分母，零 failure/error/skip。它们不是实际浏览器或完整模型负载。
3. 执行现有四份契约导出，数据库环境项仅对该子进程设为空；随后契约 diff 必须为零。不对共享 DSN 直接运行 migrate；数据库测试仅使用 fixture 自建随机 schema。
4. 启动前和各测试进程实际退出后，只读检查测试 schema 集合相同且为零、前轮三个 PID/监听仍不存在、主工作区/对照树/.env/原 18 模型文件和旧失败/报告摘要未变。异常即落盘事件，不清理其他 schema。
5. 结果报告另提交；显式暂存、Git index 卫生/链接/源码解析、diff check。源码提交和最终报告 head 的 push/PR 各两项 CI 分开核对 API、jobs、steps、rollup、watch 实际退出码，不用旧绿色替代。

## 合并和交付边界

仅本地上述完整检查、来源/保护及最终 head 四项 CI 全通过，且 main/head 未变化，才允许取消新整合 PR 草稿并以普通 merge commit 合并（不 squash、不删分支，保留 #51–#67 祖先和所有 PR 记录）。随后单独验证精确 main merge SHA 的两项 push CI。main 变化或任何门禁失败则不合并，并保留本轮失败。

两轮 120 笔和恢复仍引用 [原报告](stable-recovery-20261011.md) 的 `25a2a03`，实际浏览器/停机成对恢复仍引用 [原报告](stable-browser-backup-20261011.md) 的 `34fec8e`；只有生产源码精确等价时才引用，不改写其 source_commit，不称本轮重新压测。保留 [初轮 1653 项失败](stable-demo-integration-20261011.md)、[修复和有效负控制](checker-path-isolation-20261011.md)。不创建 release/tag、不升级版本、不宣称长期稳定/20 QPS/300ms/99% 或历史超时唯一根因。

本轮 Git SSH 22 连接关闭、443 默认主机名严格校验拒绝及两个未成功切换通道的尝试均先落盘。备用命令使用 443 + `HostKeyAlias=github.com` 复用既有已信任 GitHub 主机密钥，保持 StrictHostKeyChecking=yes；只对本轮命令指定，不改全局配置或关闭验证。team-mode 必需 agent_type 缺失，主线程串行；git-ship 显式文件/中文提交，无 Claude trailer。
