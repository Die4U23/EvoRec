# 自动导出的契约

运行 `python scripts/export_contracts.py` 更新。不要手工修改 JSON。

`openapi.json` 列当前已注册的健康、会话、推荐、反馈、商品及管理接口；`/app` 是未纳入 OpenAPI 的本地页面。其余 JSON Schema 单独描述已使用的业务输入结构，不能替代接口成功/错误响应约定。Pydantic 的跨字段校验（如曝光阈值、批内重复、反馈状态语义）不能仅凭 JSON Schema 完整表达，服务端模型是校验依据；业务约束仍须实库和端到端检查。
