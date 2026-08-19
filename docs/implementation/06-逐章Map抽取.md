# 06 逐章 Map 抽取

## 目标

接入真实生成模型，按章产生严格符合 ExtractionDraftV1 的候选事实，同时保持模型输出与 canonical 数据库之间的隔离。

## 前置条件

- 阶段 05 的固定 Draft 端到端闭环通过。
- generation profile、调用治理和 Token 账本可用。
- 准备 10–20 章带人工标注的开发样本。

## 输入边界

Map 只读取当前章以及明确配置的有限上下文。其职责是抽取本章可确定信息，不得：

- 生成 canonical_id；
- 生成最终 evidence 坐标；
- 引用未提供的未来章节；
- 构造最终跨章 event link；
- 把猜测写成确定事实。

压缩未经过 Pilot 前，默认输入规范化原文。

## 输出契约

必须输出：

- mention_id；
- local_event_id；
- provisional_claim_id；
- ref_source_segment；
- local_causes；
- cause_candidates；
- claims；
- unresolved。

所有枚举值经过 ontology/state catalog 校验。无法标准化的内容进入 raw 字段或 unresolved，不得动态扩展标准值。

## 实施顺序

### 1. Prompt 与 Schema 版本化

- system instruction、few-shot、Schema 分别存储但共同计算 prompt version。
- Prompt 明确“只抽取已披露事实”和证据定位要求。
- 结构化输出模式由 generation profile 声明。
- 任一 Prompt 或 Schema 改变都会使 checkpoint 失效。

### 2. 请求构建

- 按模型 tokenizer 计算实际输入。
- 超过窗口时按稳定 segment 拆分，保存段间映射。
- 固定上下文、章节正文和 Schema 的排列。
- 为请求生成可审计但不泄露密钥的 hash。

### 3. 响应校验

按以下层次拒绝错误：

1. 结构化输出解析；
2. JSON/类型 Schema；
3. ID 仅引用当前 Draft 内对象；
4. 枚举和必填字段；
5. ref_source_segment 范围；
6. 本章披露边界；
7. 基础业务不变量。

无效输出保存错误摘要和响应 hash；完整原始响应按安全配置保存，不进入正式事实表。

### 4. 重试策略

- 传输失败、限流和服务端暂时错误可以重试。
- Schema 错误最多进行有限次数的结构修复请求。
- 输入无效和确定性契约错误不重试。
- 每次重试单独计量。
- **失败调用也可审计（验收 P1）**：provider 内部重试耗尽时（Usage 尚未
  构造），失败尝试数与消耗的 prompt token 估计附加到最终异常
  （provider_retry_count / provider_input_tokens），runner 在异常路径
  读取并入账本——Token 账本覆盖成功、失败和重试调用。

### 5. 质量迭代

优先修复系统性错误：

- 漏实体；
- 将描述误判为事实；
- 未来结果泄露；
- relation 方向错误；
- state value 未标准化；
- 证据引用无法回映射。

Prompt 指标和数据模型指标分开记录，不通过改变黄金答案掩盖回归。

## 产物

- generation provider 适配器；
- 版本化 Map prompt；
- Draft parser/validator；
- 请求分段与 ref 映射；
- 10–20 章抽取开发集；
- 按类型统计的抽取报告。

## 验证

- 每个成功章节都产生 Schema 合法 Draft。
- Draft 中不存在 canonical_id 或最终 event ID。
- 所有引用 ID 都能在当前 Draft 内解析。
- ref_source_segment 均落在当前输入对应的原文范围。
- 同配置重复运行可以命中 checkpoint。
- Token 账本覆盖所有成功、失败和重试调用。

## 退出标准

- 开发样本可以无人工修复地完成 Draft 入 staging。
- 契约错误率达到预设阈值，且错误不会进入 active 数据。
- 失败章节可单独重跑。
- 抽取质量已足够进入证据对齐评估，而非仅凭主观阅读判断。
