# 01 · Graphiti（getzep/graphiti）代码通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/getzep/graphiti ｜ 许可证：Apache-2.0
> 定位：面向 AI Agent 的**时序上下文图谱（Temporal Context Graph）**框架，Zep 公司的开源记忆引擎，也是微软 Mnemis 的基础实现。
> 代码规模：核心 `graphiti_core/` 158 个 Python 文件 / 约 3.7 万行；另有 server（FastAPI）、mcp_server、examples、tests。

---

## 1. 项目概况与定位

Graphiti 解决的问题是：**如何让 LLM 应用拥有可增量更新、可追溯、可查询"过去某个时刻事实"的知识记忆**。

与静态知识图谱/GraphRAG 的关键差异：

| 维度 | Graphiti | 传统 GraphRAG（如 MS GraphRAG） |
|---|---|---|
| 数据更新 | 增量、实时，无需全量重算 | 批量离线建图 |
| 时间语义 | 双时态（bi-temporal）：`valid_at` / `invalid_at` + 事实自动失效 | 基本无 |
| 溯源 | 每条事实（边）记录来源 episode（原始数据） | 弱 |
| 本体 | 预设（Pydantic 模型）+ 学习两种 | 无自定义 |
| 检索 | 语义 + BM25 + 图遍历混合，亚秒级 | 秒级~数十秒 |
| 场景 | Agent 记忆 / 对话上下文 | 文档级全局问答 |

**与用户小说 RAG 方案的关系**：Graphiti 的 "Episode → 实体/关系抽取 → 图存储 → 混合检索" 主链路，与方案的 "章节 Chunk → Map 构建 Chapter Memory → 图存储 → 多路检索" 一一对应；它的 Saga（叙事弧）+ 增量摘要机制正是方案 "Arc/Volume Memory" 的现成实现。**它是四个 A 级项目中工程完成度最高、最接近"可直接当底座用"的一个。**

---

## 2. 整体架构与数据流

```
原始输入（对话/文本/JSON）＝ Episode
        │  add_episode() / add_episode_bulk()
        ▼
┌────────────────────────────────────────────────────┐
│ 1. 抽取：LLM 抽取实体节点（extract_nodes）         │
│ 2. 消歧：语义候选搜索 → 确定性相似度 → LLM 判定    │
│    （resolve_extracted_nodes，防重复实体）          │
│ 3. 抽取：LLM 抽取关系边（extract_edges）           │
│ 4. 边消歧 + 矛盾检测：重复边合并 / 矛盾边时序失效  │
│    （resolve_extracted_edges，invalid_at 机制）     │
│ 5. 属性/摘要：节点 attributes + summary 增量更新    │
│ 6. 存储：节点/边/Episode 边 + 全部嵌入落库         │
│ 7. （可选）社区构建 / Saga 关联                    │
└────────────────────────────────────────────────────┘
        ▼
  图存储：Neo4j / FalkorDB / Neptune / (Kuzu 已废弃)
        ▼
  search() / search_()：4 个 scope × 多检索方法 × 多重排器
    Edge / Node / Episode / Community
    BM25 / cosine / BFS  →  RRF / MMR / CrossEncoder / NodeDistance / EpisodeMentions
```

核心数据流一句话：**每来一条 Episode，抽取实体与关系 → 与已有图消歧合并 → 矛盾关系自动失效 → 全部带溯源与时间戳落库；查询时按问题类型在四层图元素上做混合检索。**

---

## 3. 模块逐文件代码通读（核心深读）

### 3.1 主入口 `graphiti_core/graphiti.py`（1793 行）★

`Graphiti` 类是整个框架的门面，所有对外 API：

- `add_episode(name, episode_body, source_description, reference_time, ...)`：单条处理主流程。步骤：校验 → 取前文 episodes（默认最近 `RELEVANT_SCHEMA_LIMIT=10` 条作为抽取上下文）→ `extract_nodes` → `resolve_extracted_nodes` → `_extract_and_resolve_edges`（含边失效检测）→ `extract_attributes_from_nodes`（节点摘要增量）→ `_process_episode_data`（落库 + 可选 Saga 关联）→ 可选 `update_community`。**注意注释明确要求顺序调用、建议放后台队列（FastAPI background tasks / Celery）。**
- `add_episode_bulk(bulk_episodes, ...)`：批量版。先全部 episode 落库 → 逐个取前文上下文 → 批量抽取/内存去重 → 批量 resolve → 批量落库。**用户 2000 章场景应优先走 bulk**。
- `search(query, center_node_uuid=None, ...)`：基础检索，返回 EntityEdge 列表（RRF 或 NodeDistance 配方）。
- `search_(query, config, ...)`：高级检索（替代废弃的 `_search`），返回完整 SearchResults（edges+nodes+episodes+communities + 各自重排分数）。
- `build_communities()`：社区聚类 + 社区摘要节点生成（清除旧社区 → 聚类 → LLM 摘要 → 嵌入 → 落库）。
- `summarize_saga(saga_id)`：★ Saga 增量摘要。**双水印机制**：
  - `last_summarized_at`（墙钟时间）：下一轮增量过滤用（补录数据也能被拾取）；
  - `last_summarized_episode_valid_at`（事件时间）：对外暴露"摘要覆盖到哪个时间点"。
  - 首次调用汇总全部 episodes；之后只取新增，且把旧摘要作为 LLM 上下文（不丢信息）。
  - **这就是用户方案 Arc/Volume → Book Memory 分层 Reduce 的现成蓝本**。
- `add_triplet(source_node, edge, target_node)`：直接手工添加三元组（带自动消歧与嵌入）。
- `remove_episode()`：删除 episode 及仅由其产生的边/节点。
- 辅助：`retrieve_episodes`、`get_nodes_and_edges_by_episode`、`build_indices_and_constraints`、`close`。
- `token_tracker` 属性：暴露 LLM token 用量统计（`get_usage / get_total_usage / print_summary`）——成本控制直接可用。

### 3.2 数据模型 `nodes.py`（1122 行）★ / `edges.py`（1046 行）★

**节点层次**（均继承 `Node`：uuid/name/group_id/labels/created_at）：

| 类 | 关键字段 | 语义 |
|---|---|---|
| `EpisodicNode` | `source`, `source_description`, `content`(原始正文), `valid_at`(事件时间), `entity_edges`(本 episode 产生的边), `episode_metadata` | **≈ 小说的"一章"**。`valid_at` 是故事内时间，可用来排章节顺序 |
| `EntityNode` | `name_embedding`, `summary`(周边边摘要，增量更新), `attributes`(自定义属性 dict) | **≈ 人物/势力/地点/物品/概念实体**。`labels` 可打自定义类型标签 |
| `CommunityNode` | `name_embedding`, `summary` | 社区摘要节点（GraphRAG 式） |
| `SagaNode` | `summary`, `first/last_episode_uuid`, `last_summarized_at`, `last_summarized_episode_valid_at` | **≈ 卷/弧 Memory**：一条 Saga 挂一串 Episode（HAS_EPISODE 边），存滚动摘要 |

**边层次**：

| 类 | 关键字段 | 语义 |
|---|---|---|
| `EntityEdge` | `name`(关系名), `fact`(事实句,被嵌入), `fact_embedding`, `episodes`(来源列表), `valid_at`/`invalid_at`(双时态), `expired_at`, `reference_time`, `attributes` | **≈ 人物关系/事件事实**。`invalid_at` 设值=该关系已被新信息推翻 |
| `EpisodicEdge` | — | Episode → 实体 的 MENTIONS 边（溯源：哪章提到谁） |
| `CommunityEdge` | — | 社区 → 成员 HAS_MEMBER 边 |
| `HasEpisodeEdge` | — | Saga → Episode |
| `NextEpisodeEdge` | — | Episode 链式 NEXT_EPISODE（章节顺序） |

**嵌入策略**：实体嵌 `name`，边嵌 `fact` 全文（`generate_embedding`）。检索命中边=命中一条事实句。

### 3.3 检索编排 `search/search.py`（875 行）★ + `search_config_recipes.py`（223 行）★

`search()` 主流程：嵌入查询向量 → **4 个 scope 并行**（edge/node/episode/community）→ 每个 scope 按配置执行检索方法 → 重排 → 汇总 SearchResults。

每个 scope 的方法/重排器（配置驱动，全部可组合）：

- 检索方法：`bm25`（全文）、`cosine_similarity`（向量）、`bfs`（图遍历扩展，深度 ≤3）
- 重排器：`rrf`（融合排序，默认）、`mmr`（最大边际相关，λ 可调）、`cross_encoder`（交叉编码精排）、`node_distance`（到中心节点的图距离）、`episode_mentions`（被引用 episode 数）

**现成配方**（`search_config_recipes.py`）：
- `COMBINED_HYBRID_SEARCH_RRF`：edge/node/episode/community 全 scope × (bm25+cosine) × RRF —— 默认均衡
- `COMBINED_HYBRID_SEARCH_CROSS_ENCODER`：加 BFS 扩展 + cross-encoder 精排 —— **质量优先，`search_()` 默认配方**
- `EDGE_HYBRID_SEARCH_NODE_DISTANCE`：以某实体为中心的相关事实重排 —— **"围绕某人物发生了什么"类问题**
- `EDGE_HYBRID_SEARCH_EPISODE_MENTIONS`：按被提及次数 —— **"这角色活跃在哪些章"**
- 另有 node/community 单 scope 变体

**与用户 Query Router 的映射**：Graphiti 的 scope×方法×重排器 组合矩阵，正是方案"按问题类型选择检索路径"的工程实现——可以直接用配置配方表达"人物关系问题→edge scope + node_distance；势力变迁问题→community scope；细节核实→episode scope"。

### 3.4 检索工具 `search/search_utils.py`（2048 行）★

- `rrf`（1764）：Reciprocal Rank Fusion，多路结果融合。
- `maximal_marginal_relevance`（1885）：MMR 去重保多样性。
- `node_distance_reranker`（1782）：基于图 BFS 距离的重排。
- `episode_mentions_reranker`（1844）。
- `edge_fulltext_search` / `edge_similarity_search` / `edge_bfs_search`（185/291/439）：边三路检索，返回 EntityEdge。
- `node_*`（563/656/774）、`episode_fulltext_search`（870）、`community_*`（956/1045）：对应 scope。
- `hybrid_node_search`（1163）、`get_relevant_nodes`（1237）/`get_relevant_edges`（1391）：**抽取时"取相关已有节点/边做上下文"**（防止 LLM 重复建实体、保持一致性）——小说逐章处理时这就是"前文记忆注入"。
- `get_edge_invalidation_candidates`（1576）：找可能被新事实推翻的旧边。
- `get_mentioned_nodes` / `get_episodes_by_mentions` / `get_communities_by_nodes`：溯源查询（章节↔实体↔社区互查）。
- 常量：`RELEVANT_SCHEMA_LIMIT=10`、`DEFAULT_MIN_SCORE=0.6`、`MAX_SEARCH_DEPTH=3`。

### 3.5 实体抽取与消歧 `utils/maintenance/node_operations.py`（1032 行）★★（小说方案最关键模块）

**`extract_nodes()`（70）**：组装上下文（当前 episode 全文 + 前 10 章内容 + 实体类型定义表）→ LLM 结构化输出 `ExtractedEntity{name, entity_type_id, episode_indices}` → 过滤空名 → 折叠完全重复 → 返回。

**`resolve_extracted_nodes()`（627）—— 三级消歧，直接对应小说"指代消解/同名合并"问题**：
1. `_semantic_candidate_search`（418）：对每个新实体名做向量相似度搜索已有节点（候选 ≤15，阈值 0.6）；
2. `_resolve_with_similarity`（dedup_helpers 220）：确定性消歧——精确归一化名匹配（无论长短都试）→ 熵门槛（防短名/泛称误合）→ MinHash/LSH + Jaccard 模糊匹配（阈值 0.9）；
3. `_resolve_with_llm`（467）：仍未定的批量送 LLM（`dedupe_nodes` prompt），按 candidate_id 判定"合并到谁 / 新建"，带防呆护栏（越界 id 忽略、重复 id 去重、缺失 id 告警）。
- `_promote_resolved_node`（dedup_helpers 170）：合并时若新实体类型更具体则升级标签（如 "Entity" → "Character"）。
- `extract_attributes_from_nodes`（726）+ `_extract_entity_summaries_batch`（833）：节点摘要增量更新——**把旧摘要+新边信息合并生成新摘要（`summarize_nodes` prompt），避免每次全量重写**。这是"人物档案随剧情演进"的机制。

**对用户方案的意义**：小说里"孙悟空/美猴王/齐天大圣/行者"这类别名合并，Graphiti 的"语义候选 + 精确归一化 + MinHash 模糊 + LLM 兜底"四层管线是可直接复用的现成设计；摘要增量更新机制可直接用于"人物档案"维护。

### 3.6 边抽取与矛盾处理 `utils/maintenance/edge_operations.py`（911 行）★

- `build_episodic_edges`（52）：Episode → 其抽取实体 的 MENTIONS 边（溯源）。
- `extract_edges`（117）：LLM 抽取关系三元组（source/target 用抽取阶段的实体 id），带 `episode_indices` 归属。
- `resolve_extracted_edges`（325）：精确去重 → 为每条新边搜"同节点对已有边"+"全局相似边"两路候选 → 送 LLM 判定 `resolve_extracted_edge`（623）。
- `resolve_edge_contradictions`（538）★：**矛盾检测——新事实与旧事实冲突时，把旧边 `invalid_at` 设为当前时间（软删除），新边入图**。这是"势力变迁/关系变化"（如 A 与 B 从同盟变敌对）的图模型基础。
- `_extract_edge_timestamps`（576）：让 LLM 给边标注 `valid_at`/`invalid_at`（故事内时间语义）。

### 3.7 消歧辅助 `utils/maintenance/dedup_helpers.py`（296 行）★

- 字符串归一化（exact/fuzzy 两套）、名字熵计算（`_name_entropy`，阈值 1.5）、shingle + MinHash（32 置换）+ LSH 分桶（4 带）加速模糊候选查找、Jaccard 相似度（阈值 0.9）。
- `_build_candidate_indexes`：一次遍历预计算所有查找结构，避免每节点重复建索引。

### 3.8 社区构建 `utils/maintenance/community_operations.py`（367 行）★

- `label_propagation`（93）：标签传播聚类（按边权重加权投票，平局取大社区）→ 得到社区簇。
- `build_community`（174）：**社区摘要=两两配对 LLM 摘要的二叉树归并**（`summarize_pair`），直到剩一条；再 `generate_summary_description` 生成社区名。成本 O(log n) 次 LLM 调用。
- `update_community` / `determine_entity_community`（340/274）：实体进社区时增量更新社区摘要。
- **对用户方案的意义**：社区≈"势力/阵营/剧情团块"，归并摘要≈ Reduce 层的"势力变迁总览"；`build_communities` 可用作卷级/全书级聚合。

### 3.9 内容分块 `utils/content_chunking.py`（826 行）

- `estimate_tokens`（4 字符/token 粗估）、`should_chunk`（按类型+密度阈值 0.15 判断是否需分块）。
- 三种分块器：`chunk_text_content`（按句子优先，fallback 按大小，带 overlap）、`chunk_json_content`、`chunk_message_content`。
- 全局分块参数在 `helpers.py`：`CHUNK_TOKEN_SIZE=3000`、`CHUNK_OVERLAP_TOKENS=200`、`CHUNK_MIN_TOKENS=1000`。
- **用户方案"过长章节再细分"可直接用这套**（但中文按句切分需适配——它按 `。！？` 等边界切，中文兼容性好）。

### 3.10 LLM 抽象 `llm_client/`（client.py 295 行 + 各 provider）

- `LLMClient`（client.py）：统一 `generate_response(messages, response_model, max_tokens, model_size)` 接口；**Pydantic response_model 的 JSON Schema 直接注入 prompt**（结构化输出）；指数退避重试（4 次，5–120s）；**`TokenUsageTracker` 全程 token 记账**；可选磁盘 cache（`LLMCache`，md5 消息指纹）。
- 多 provider：OpenAI / Azure / Anthropic / Gemini / Groq / OpenAI 兼容（`openai_generic_client`，支持 DeepSeek/Ollama/vLLM）。`model_size`（small/medium/large）决定用哪个模型——**成本分层：小模型干粗活、大模型干细活**。
- `_apply_attribute_extraction_preamble`（client.py 159）：给属性抽取加严格前置指令，防止 LLM 把 schema 描述当值复制——很实用的小技巧。

### 3.11 抽取提示词 `prompts/` ★（小说场景定制模板库）

- `extract_nodes.py`（654 行）：抽取实体。**大量负面规则**：不抽代词/抽象概念/泛称/裸亲属称谓（"dad"→必须"Jordan's dad"）/裸物品词；实体类型经 Pydantic docstring 注入 prompt。message/json/text 三版本。
- `extract_edges.py`（309）、`extract_nodes_and_edges.py`（312）：关系抽取与合并抽取。
- `dedupe_nodes.py`（225）/`dedupe_edges.py`（103）：消歧判定。
- `summarize_nodes.py`（142）：节点摘要增量 / 两两合并 / 摘要描述。
- `summarize_sagas.py`（139）：Saga（弧）摘要。
- `eval.py`（164）：抽取质量自评提示词。
- `lib.py`：`prompt_library` 统一注册表（所有 prompt 集中管理、可版本化）——**提示词工程的组织范式**。

### 3.12 外壳层（职责记录，未逐行）

- `driver/`：`GraphDriver` 抽象（execute_query + 图操作接口），后端实现 Neo4j / FalkorDB / Neptune / Kuzu(废弃) 四套 × 每套 14 个 operations 文件（entity/episode/community/saga node ops、edge ops、search ops 等），高度模板化重复。`search_interface` 封装全文/向量检索（Neo4j fulltext+vector index、FalkorDB、Neptune AOSS）。
- `embedder/`：OpenAI / Azure / Voyage / Gemini 嵌入客户端。
- `cross_encoder/`：OpenAI / BGE / Gemini 重排客户端。
- `models/nodes|edges/node_db_queries.py`（394/343）：节点/边的 Cypher 查询常量（返回字段、save query）。
- `namespaces/nodes.py|edges.py`（355×2）：`graphiti.nodes.entity.save()` 式链式 API。
- `telemetry/`、`tracer.py`：OpenTelemetry 可选追踪；`decorators.py`：多 group_id 处理。
- `mcp_server/`：MCP 服务器（Docker + Neo4j/FalkorDB）；`server/`：FastAPI REST 服务（ingest/retrieve 路由 + DTO）。
- `utils/bulk_utils.py`（634）：批量导入管线（RawEpisode、add_nodes_and_edges_bulk、dedupe 批量变体、retrieve_previous_episodes_bulk）。
- `utils/ontology_utils/`：实体类型校验；`utils/text_utils.py`：`MAX_SUMMARY_CHARS` 等常量。

---

## 4. 关键算法/机制深挖

1. **双时态关系失效**：`EntityEdge.valid_at/invalid_at`。新边与旧边矛盾 → 旧边 `invalid_at=now`（保留历史），新边生效。查询时默认过滤 `invalid_at` 已过的边。→ 小说中"人物关系/势力归属随时间变化"的图建模。
2. **多级实体消歧**（成本-质量权衡典范）：向量候选（便宜）→ 确定性规则（零成本）→ LLM 兜底（贵但准）。每个新实体平均 1 次向量检索 + 偶发 1 次 LLM 判定。
3. **社区归并摘要**：两两配对 LLM 摘要，O(log n) 调用次数而非 O(n)。
4. **Saga 增量摘要双水印**：墙钟水印管"处理到哪"，事件时间水印管"内容覆盖到哪"。
5. **抽取一致性**：抽取时把已有相关节点/边（`get_relevant_nodes`）注入 prompt，让 LLM 复用旧实体名，从源头减少新别名。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | Graphiti 对应物 | 结论 |
|---|---|---|
| 原文库 / Evidence | `EpisodicNode.content`（`store_raw_episode_content=True` 时保留全文） | ✅ 直接支持，且 episode↔实体 MENTIONS 边即"摘要→原文"映射 |
| 章节 Chunk | Episode + `content_chunking`（长章切分带 overlap） | ✅ 现成 |
| Map：Chapter Memory | `add_episode`：抽取实体/关系/属性/摘要 | ✅ 现成（提示词需按中文小说定制） |
| 人物/关系/势力/事件 | EntityNode（人物/势力=自定义 entity_types 标签）+ EntityEdge（关系）+ 社区（势力团块） | ✅ 现成，`entity_types` 用 Pydantic 自定义 |
| Reduce：Arc/Volume→Book | `summarize_saga`（卷/弧）+ `build_communities`（全局聚合） | ✅ 现成，双水印增量 |
| Vector+Graph+Hierarchical 索引 | 节点/边嵌入 + Neo4j fulltext/vector + 图遍历 + 社区层级 | ✅ 现成 |
| Query Router | `search_config_recipes` 配置矩阵（scope×方法×重排器） | ✅ 现成，配方即路由 |
| 回查原文 Evidence | episode scope 检索 + `get_episodes_by_mentions` 溯源 | ✅ 现成 |
| 伏笔跨章追踪 | **无直接实现**（Graphiti 是事实图谱，不含"伏笔状态机"） | ⚠️ 需自建（参考鉴来助手） |

---

## 6. 可复用模块清单

**直接复用（pip install graphiti-core 或 vendor 核心）**
- 整个 `graphiti_core` 作为图记忆底座：`Graphiti` 类 + 四个图数据库后端 + 混合检索全套。
- `search_config_recipes` 配方体系：查询路由的最小改造成本。
- 双时态边模型 + 矛盾失效：势力变迁/关系变化的图存储。
- Saga 增量摘要（双水印）：卷级/弧级 Memory 的 Reduce。
- 社区构建（label propagation + 归并摘要）：势力团块。
- `TokenUsageTracker` + `model_size` 分层：成本控制。
- 三级实体消歧管线：指代消解（需把 minhash 归一化换成中文规则）。

**需改造**
- 抽取/摘要提示词：Graphiti 面向对话/消息，需按网文定制（人物别称、外号、代词消解上下文、伏笔字段）。
- `content_chunking`：中文适配（现按句子边界切，中文句号兼容，但需验证）。
- 实体类型：新增 Character/Faction/Location/Item/Event/ChekhovGun（伏笔）等 Pydantic 模型。

**仅参考（不建议直接依赖）**
- MCP server / REST server：如不需要即跳过。
- Kuzu 后端已废弃；Neptune 需 AWS。

---

## 7. 许可证与工程坑

- **许可证：Apache-2.0** ✅ 可商用、可改、可闭源分发（保留声明即可）。
- **依赖重**：Neo4j 5.26+ 或 FalkorDB 1.1.2+；LLM 建议 OpenAI/Anthropic/Gemini（structured output 支持差的小模型会抽取失败）。
- **AGENTS.md 实测坑**：
  - `make test` 默认依赖可达的 Neo4j（`NEO4J_PASSWORD=testpass make test`），无库会挂连接重试；
  - FalkorDB 异步驱动在并发查询时掉连接（"Connection closed by server"）——本地混检建议 Neo4j 后端；
  - `tests/test_add_triplet.py` 有预存失败（mock embedder 未 stub `create_batch`），与测试环境无关；
  - 运行任何东西前设 `GRAPHITI_TELEMETRY_ENABLED=false`（PostHog 遥测，可 opt-out）。
- 小模型适配：`OpenAIGenericClient` 支持 DeepSeek/Ollama/vLLM，但小模型 JSON schema 遵循度差，`structured_output_mode` 需调 `json_schema`/`json_object`。
- **成本注意**：社区构建与 Saga 摘要会额外消耗 LLM 调用；抽取一致性会注入前文上下文（前 10 章全文），token 消耗比裸抽取高——用户方案中"压缩层"的价值正在于此（Graphiti 用原文注入，方案用 Chapter Memory 注入更省）。

---

## 8. 成本与规模实测数据

Graphiti 官方未提供整书级成本数据。可参考的间接结论：

- 单条 episode 处理 ≈ 4~7 次 LLM 调用（抽取节点 1 + 消歧 ≤1 + 抽取边 1 + 边消歧 1 + 属性/摘要 1~2 + 社区可选）。比鉴来助手单章 1~2 次调用重一个量级，但产出是结构化的图。
- 用户 2000 章场景：若全量 `add_episode_bulk` + 建社区 + 逐卷 summarize_saga，LLM 调用量 ≈ 2000×(4~7) + 社区归并 O(log n) + 卷数×2，落在 1~2 万次调用区间；结合 graph-every-novel 的实测（85.8 万字人物抽取 456 万 tokens/¥5.4），Graphiti 全管线（含摘要与消歧）估计为 3~5 倍成本，即 400 万字约 ¥80~250、700 万字约 ¥140~450（DeepSeek 级价格），仍属可行范围，且可用 `model_size` 分层进一步压。

---

## 9. 通读结论

1. **Graphiti 是用户方案"图 + 分层记忆"部分的完整工程底座**，架构与方案思路同源（尤其 Saga 增量摘要 = Arc/Volume Reduce，社区 = 势力/剧情团块）。
2. **方案中"图 RAG"一条路可以直接基于 Graphiti 搭建**，省去从零开发；需自建的部分是：中文网文定制的抽取提示词、伏笔状态机、以及"压缩层作为主检索源"的替代（Graphiti 默认用原文 episode 做上下文注入）。
3. 它的消歧/摘要/检索三块设计是其他项目（Mnemis、graph-every-novel）的共同地基，先读透本仓库再读 Mnemis 会事半功倍。
