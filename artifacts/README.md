# 产物区域

保存不可变模型、索引和实验结果。本目录产物不进入 Git。当前没有模型或实验结果。

bundle 按 UUID 建立独立目录，并在目录根放置 `manifest.json`。候选目录必须位于调用方指定的受管根目录中；目录名必须等于 `bundle_id`。清单采用 schema version 1，记录：

- bundle 身份、构建任务、Git 版本、工作区是否有改动、数据快照及训练截止时间；
- 排序器、内容编码器和语义码本 ID，以及它们与商品映射、商品向量、向量索引各自对应的文件角色；
- 商品条数与有序商品集合指纹，索引维度、类型、归一化、距离和构建参数；
- 支持路径、召回及排序预算，以及每个文件的相对 POSIX 路径、字节数和 SHA-256。

商品映射文件是 `[{"internal_id": 0, "item_id": "..."}]` 形式的 JSON 数组，内部 ID 必须从零连续且外部 ID 不重复。`item_set_sha256` 是按内部 ID 排序的外部 ID 数组经紧凑 UTF-8 JSON 编码后的 SHA-256。向量文件是连续的 float16 或 float32 矩阵，其字节数必须等于 `item_count × dimension × 类型字节数`。

运行 `python scripts/validate_bundle.py <managed-root> <bundle-dir>` 执行候选校验。工具拒绝未知清单字段、绝对或越界路径、符号链接、缺失/多余文件、大小或哈希不符、映射异常和向量形状不符；成功结果给出清单哈希，供后续持久发布状态引用。

`python scripts/load_bundle.py <managed-root> <bundle-dir>` 在上述检查后执行受控加载。当前白名单格式为：

- `item_embeddings_role`：小端连续 float32、逐行归一化的商品向量；
- `content_encoder_role`：`mean-history-v1` JSON 契约，用历史商品向量均值形成查询；
- `ranker_role`：`dot-product-v1` JSON，包含有限 scale、逐商品 bias 和至少一个黄金样本；
- `semantic_codebook_role`：`item-codebook-v1` JSON，包含与商品映射等长且唯一的语义码；
- `vector_index_role`：`flat-v1` JSON，内部 ID 必须完整覆盖映射并与清单的 cosine/归一化契约一致。

加载器不调用 pickle、`torch.load` 或任意 bundle 内代码；加载前再次核对所有哈希，并限制总字节、JSON 大小、商品数、维度、向量值和黄金样本数。加载完成后逐条核对预期分数与顺序，命令结果始终标记 `activated=false`。

这只是可移植 CPU 基线运行时和受控加载边界，尚未转换或接入 R06 冻结检查点，也不会更新数据库活动版本。通过检查不代表 bundle 已发布、服务已就绪或达到线上性能目标。失败产物不标 active，模板文件不标 completed。
