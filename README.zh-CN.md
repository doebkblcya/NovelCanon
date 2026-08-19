# NovelCanon

将中文长篇小说转换为可追溯、可按章节查询的结构化知识库。

[English](https://github.com/doebkblcya/NovelCanon/blob/main/README.md) | **中文**

## 产品介绍

以 SQLite 为唯一权威数据源，把全文小说加工为带原文证据的结构化事实：统一实体标识、关系与状态演变、事件因果链、章节/卷/全书摘要，并提供全文检索、语义检索与结构化查询融合的问答能力。

## 核心能力

- **实体消歧**：跨章合并提及为稳定 canonical ID，别名随披露顺序演进
- **事实与证据**：每条 claim 锚定原文片段，assert/update/retract 版本链可复核
- **事件因果**：一等事件链接表驱动因果链查询，路径置信度可解释
- **时间语义**：`knowledge_cutoff_chapter` 披露截止与 `world_at_chapter` 世界状态回放两个独立查询维度
- **混合检索**：FTS + 向量 + 结构化查询按路线融合，回答附带证据与章节定位
- **分层摘要**：章节 → 卷 → 全书，按 `max_observed_ordinal` 失效重建

## 技术选型

- 存储：SQLite（WAL、事务、FTS/向量索引）
- 处理：逐章抽取 → 全书消歧 → 事件链接 → 证据验证 → 分层 Reduce
- 模型：通过版本化 generation/embedding profile 引用，可替换

详见规范： [docs/定版方案.md](docs/定版方案.md)

## 开发

前置：安装 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --extra vec --extra tokenizer --dev   # 安装依赖（含 sqlite-vec 与 tiktoken）
uv run pytest                                # 测试
uv run ruff check .                          # lint
uv run ruff format .                         # 格式化
uv run mypy src/novelcanon                   # 类型检查
uv run novelcanon --help                     # CLI
```

技术选型与工程约束见 [docs/adr/](docs/adr/)（6 个 ADR）。
