# Noyra M3c 模型载荷与长期存储复核

日期：2026-08-14  
范围：model request/response 的长期增长、缓存恢复、训练导出和 runtime export。

## 修复内容

新增 `payload_codec`，对超过阈值的 terminal model call request/response JSON 使用 zlib + URL-safe base64 压缩。压缩发生在 storage maintenance 中，保留原始 response hash、request hash、状态和 idempotency 语义。

以下路径会自动解压后再使用：

* `ModelLedger._call_from_row()` 的 cached result/reconcile/recovery；
* training export 的 model IO JSONL；
* runtime export 的 model_calls 表。

压缩失败会以 `IntegrityError` 暴露，不会返回损坏的模型响应。未达到压缩收益阈值的短文本保持原格式，兼容旧数据库。

## 验证

* 新增 terminal model response 压缩后 cached response 恢复测试；
* M3 相关专项测试通过；
* 当前全量测试、静态、类型、依赖和部署检查继续通过。

## 边界

该阶段降低了 SQLite 热数据的增长速度，但不是完整冷迁移。model payload 仍在 SQLite 中，只是压缩存储；behavior logs、interactions 和 training provenance 仍然保留热元数据。真正的云端 tombstone、恢复下载和释放本地 cold segment 仍需单独设计，不能把压缩误报为完成了 P1-02。
