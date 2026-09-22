# 运行与部署边界

M1 进程内演示启动步骤见根目录 README。默认绑定 127.0.0.1；`/health/live` 为 API 存活检查，`/health/ready` 是持久业务依赖检查，目前仍返回 503。

本机开发已验证 PostgreSQL 18.6、Psycopg binary/pool 与 `evorec` 独立数据库账号，数据库仅监听 localhost。连接串放在被忽略的 `.env`，仓库只保留 `.env.example`。这项本机安装验证不等于迁移或持久化适配器已经完成；`db/schema.design.sql` 仍是设计草案，不得直接作为发布迁移。

后续部署采用 Linux 与 Docker Compose，服务包括 API、推理、后台任务和 PostgreSQL。当前尚未提供已验证的容器部署，不将草案当成可上线配置。

M1 创建并验证数据库迁移；M2 增加任务恢复及版本切换；V01 固定镜像与产物、验证持久化、权限、监控和恢复。扩大实例数前重新设计全局活动版本和下架确认机制。
