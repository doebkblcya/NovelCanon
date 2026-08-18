# NovelCanon

将中文长篇小说转换为可追溯、可按章节查询的结构化知识库。
Turn long Chinese novels into a traceable, chapter-queryable structured knowledge base.

## 产品介绍 / Product

以 SQLite 为唯一权威数据源，把全文小说加工为带原文证据的结构化事实：统一实体标识、关系与状态演变、事件因果链、章节/卷/全书摘要，并提供全文检索、语义检索与结构化查询融合的问答能力。

NovelCanon builds a structured knowledge base from full-length Chinese novels on SQLite as the single source of truth: unified entity identities, relation/state evolution, event causality, chapter/volume/book summaries, and QA over hybrid full-text, semantic, and structured retrieval.

## 核心能力 / Highlights

- **实体消歧** / Entity resolution：跨章合并统一标识，别名随披露章节演进
- **事实与证据** / Claims & evidence：claim 锚定原文片段，assert/update/retract 版本链可复核
- **事件因果** / Event causality：事件链接表驱动因果链查询，路径置信度可解释
- **时间语义** / Temporal semantics：`knowledge_cutoff_chapter` 披露截止与 `world_at_chapter` 世界状态回放双维度查询
- **混合检索** / Hybrid retrieval：FTS + 向量 + 结构化查询按路线融合，回答附带证据与章节定位
- **分层摘要** / Hierarchical summaries：章节 → 卷 → 全书，按 `max_observed_ordinal` 失效重建

## 技术选型 / Tech Stack

- 存储 Storage：SQLite（WAL、事务、FTS/向量索引）
- 处理 Pipeline：逐章抽取 → 全书消歧 → 事件链接 → 证据验证 → 分层 Reduce
- 模型 Models：版本化 generation/embedding profile 引用，可替换

详见 Spec： [docs/定版方案.md](docs/定版方案.md)
