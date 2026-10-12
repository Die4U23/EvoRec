# Demo 快速启动与停止

这是本地隔离 Demo，不操作业务目录或既有 8000；模型/原始数据不随 Git 分发。首次 clone 先按 [README](../README.md#运行本地服务演示) 安装 Python 3.12 服务环境（包括数据库驱动）、当前 checkout 的可编辑包，并准备可连接且有 CREATE-schema 权限的本机 PostgreSQL 16+。当前本机/CI 分别验证 18/16，不代表异机复建验收。以下命令在当前 checkout 根目录执行。

## 先演示新品内容基线

无需研究数据、NumPy 或训练模型。已有本机 `.env` 时：

```powershell
.\.venv\Scripts\python.exe -m scripts.demo catalog --env-file .env --check
.\.venv\Scripts\python.exe -m scripts.demo catalog --env-file .env
```

第一条只读检查，不建目录/schema、不迁移、不启动服务。第二条检查通过后复用原入口：在 `artifacts/demo/catalog-<UUID>` 创建本轮目录、随机 schema、临时令牌和一个 catalog worker，输出 `status: ready` 后才打开其随机 URL 的 `/app`。合成 24 件初始目录；不是冻结 R06 或真实质量评估。只出现 `preflight_passed` 不等于服务 ready，更不是验收报告。

页面操作及样例 CSV 见 [新品流程](../docs/product/demo-scope.md#独立新品内容基线入口)。管理令牌只从该轮输出的 `admin-token.txt` 获取，不输入业务令牌、不截图/提交令牌。导入不等于发布；构建/预览后须显式确认发布，下架/回滚不复活商品。

## 再演示真实冻结 R06

先按 [R06 指南](../research/R06-multi-interest-guide.md) 准备完整批准包与 NumPy 2.1.3 的 `.venv-accelerated`。不自动下载/训练模型，不会把任意新品 ID 放进旧模型。复制 [配置样例](demo.env.example) 为 Git 忽略的 `.env.demo`，填写数据库与三个批准模型字段；不需要管理令牌。

```powershell
if (Test-Path -LiteralPath .env.demo) { throw '.env.demo 已存在，请编辑原文件，不覆盖' }
Copy-Item ops/demo.env.example .env.demo
# 手动填写 .env.demo；不要打印数据库密码。
.\.venv-accelerated\Scripts\python.exe -m scripts.demo r06 --env-file .env.demo --check
.\.venv-accelerated\Scripts\python.exe -m scripts.demo r06 --env-file .env.demo
```

不要覆盖已存在的 `.env.demo`；已有文件直接编辑，不重复 Copy-Item。三个字段为批准包父目录 `EVOREC_DEMO_MANAGED_ROOT`、规范 bundle UUID `EVOREC_DEMO_BUNDLE_ID` 和批准 manifest SHA-256 `EVOREC_DEMO_MANIFEST_SHA256`，使用自己已核对的身份，不随意拿当前文件哈希充当审批。相对路径相对于 checkout。三项也可分别用 `--managed-root`、`--bundle-id`、`--expected-manifest-sha256` 显式覆盖。

`--check` 完整复用受控加载器验证包/组件/来源，NumPy 缺失或版本不符明确失败，不静默降级 stdlib；仅用只读数据库连接检查。随后正常启动会再次由原入口核对/准备本轮目录并发布到本轮 schema，默认管理员关闭、没有新品 worker。等 `status: ready`，核对真实策略/bundle/模型身份后体验推荐、收藏、隐藏、撤销和重置。

## 配置与导入来源

- 显式 `--env-file` 的四个允许字段覆盖同名进程变量；未指定时优先进程，缺数据库项才尝试本 checkout 的 `.env`。不执行 shell/插值；最多 16 KiB，重复键、链接文件、未闭合引号拒绝。值内 `#` 是字面量，不是行尾注释；不支持 `export` 或变量展开。
- 业务 EVOREC 管理/模型开关不会传入 Demo。CLI 仅在调用旧入口的单进程作用域替换环境，完成/异常后恢复；不改配置文件、父终端或全局设置。勿在 Web 服务线程中调用这个 CLI。
- `evorec_source_mismatch` 表示借用环境仍指向别的 checkout。为当前 checkout 建独立环境并安装 `-e .`，或明确设置本终端的 `PYTHONPATH` 指向当前根及 src；不要重新安装共享业务环境来掩盖来源。旧 8000 服务不因代码更新自动切换。
- `--port 8000`、远端/隐式数据库与已有输出均拒绝。默认输出自动生成新 UUID；可把新 artifacts 子目录作为模式后的可选位置参数。预检不预占端口，实际绑定冲突仍可能在启动时失败，不能当已 ready。
- 显式或继承的 service 路由配置拒绝；未显式提供 hostaddr 时，也校验继承的 PGHOSTADDR 必须为 loopback。驱动会用环境填充未指定连接参数，见 [PostgreSQL 说明](https://www.postgresql.org/docs/18/libpq-envars.html)。明确在独立终端配置本机 DSN，不靠清空 SSL/GSS 安全策略适配；入口不会自动修改这些变量。

## 正常停止与数据边界

从终端的 ready 输出获取**这一次**的 `output` 路径，在另一终端创建其 stop 文件：

```powershell
New-Item -ItemType File -Path '<本轮 output 路径>/stop'
```

等待启动终端真正退出，再检查本轮 `stopped.json`：catalog 的 `worker_drained` / `owned_schema_removed`，R06 的 `cpu_jobs_drained` / `owned_schema_removed` 均为 true；catalog 临时令牌应已删除。不要批量停止端口/删除 schema，也不要把仅存在 marker 当成独立清退验收。

旧入口支持 Ctrl+C 的 finally 清退，但本轮新包装入口的实机 Ctrl+C 仍待专门验证；当前推荐已实测的 stop 文件方式。正常停止会删除本轮临时演示数据，页面不能撤销；原模型和输出诊断保留。强杀/断电可能残留，按确切 owned.json 检查，不做宽泛清理。

整套连续演示和作者理解验收仍见 [三分钟讲解与作者参与](../docs/product/demo-scope.md#作者参与的最后一轮验收)。此入口不等于 public release、真实 R06 动态新品或工业级 SLA。
