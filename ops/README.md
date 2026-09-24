# 运行与部署边界

M1 进程内演示启动步骤见根目录 README。默认绑定 127.0.0.1；`/health/live` 为 API 存活检查，`/health/ready` 是持久业务依赖检查，目前仍返回 503。

本机开发已验证 PostgreSQL 18.6、Psycopg binary/pool 与 `evorec` 独立数据库账号，数据库仅监听 localhost。连接串放在被忽略的 `.env`，仓库只保留 `.env.example`。正式迁移位于 `db/migrations`，由 `scripts/migrate_database.py` 在 advisory lock 和单文件事务中执行；`db/schema.design.sql` 仅保留为早期设计草案，不得直接应用。

`0001_m1_core.sql` 和 `0002_m23_publication.sql` 已应用并检查校验和；`scripts/verify_m11_database.py` 验证会话重置版本、陈旧写入拒绝、重复请求拒绝、非法分数拒绝和行锁行为。HTTP 适配器、单实例受控 bundle 发布和恢复已有本地实现。数据库集成测试统一使用空的临时 schema，并在缺少数据库时失败；CI 用一次性 PostgreSQL 服务运行服务测试。多实例协调与公网运行不在本轮验证范围内。

后续部署采用 Linux 与 Docker Compose，服务包括 API、推理、后台任务和 PostgreSQL。当前尚未提供已验证的容器部署，不将草案当成可上线配置。

M1 创建并验证数据库迁移；M2 增加任务恢复及版本切换；V01 固定镜像与产物、验证持久化、权限、监控和恢复。扩大实例数前重新设计全局活动版本和下架确认机制。
