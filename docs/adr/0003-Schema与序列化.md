# ADR-0003 Schema 与序列化

- 状态：已接受（2026-08）
- 适用范围：领域模型、配置、LLM 结构化输出（阶段 02/06 起实现）

## 背景

定版方案要求领域 Schema 与 DDL 能表达全部首期事实类型；LLM 结构化输出需要 JSON Schema；配置需要启动强校验与稳定 config hash。

## 决策

- **Pydantic v2**：领域模型（ExtractionDraftV1、CanonicalMemoryV4、claim envelope、profile 等）单一来源；
- 从 Pydantic 模型自动导出 **JSON Schema**，供 LLM structured output 使用；不允许手写两份不一致的 Schema；
- **pydantic-settings**：应用配置从配置文件 + 环境变量加载（`NOVELCANON_*` 前缀），启动时强校验，未知字段拒绝（`extra=forbid`）；
- 密钥只从安全环境读取，不进入配置快照、日志或数据库；
- 为所有配置生成稳定 **config hash**（规范化 JSON + SHA-256，字段排序），供 run 幂等与 checkpoint 键使用；
- 标准字段必须引用统一 ontology 与 state catalog；模型原始自由文本只进入明确允许的 raw 字段。

## 理由

- Pydantic 提供校验、序列化与 JSON Schema 导出一体能力，与后续 FastAPI 响应模型同源；
- "启动失败而不是静默使用不完整默认值"是阶段 01 的硬性验证项。

## 后果

- 任何领域结构变更先改 Pydantic 模型与对应 migration，再改实现；
- Schema 版本号与定版方案同步维护。

## 参考

- 定版方案 §4、§13
