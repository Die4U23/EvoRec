# 自动导出的契约

运行 `python scripts/export_contracts.py` 更新。不要手工修改 JSON。

`openapi.json` 只列当前实现的状态接口。其余 JSON Schema 表示未来业务输入结构，不表示业务接口已实现。Pydantic 的跨字段校验（如曝光阈值、批内重复、反馈状态语义）不能仅凭 JSON Schema 完整表达，服务端模型是校验依据；业务约束仍须实库和端到端检查。
