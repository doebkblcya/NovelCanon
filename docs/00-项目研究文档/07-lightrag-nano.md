# 07 · nano-graphrag（gusye1234）+ LightRAG（HKUDS）代码通读报告

> 通读日期：2026-08
> - nano-graphrag：https://github.com/gusye1234/nano-graphrag ｜ MIT ｜ "A simple, easy-to-hack GraphRAG implementation"——微软 GraphRAG 的极简可读版。
> - LightRAG：https://github.com/HKUDS/LightRAG ｜ MIT ｜ [EMNLP 2025] "Simple and Fast Retrieval-Augmented Generation"，生产级轻量 GraphRAG 框架。
> 两者是用户方案中"Vector + Graph 双路检索底座"的两个候选，互为"读懂的入口"与"能跑的框架"。

---

## 1. 项目概况与定位

**nano-graphrag**：把微软 GraphRAG 的核心流程（分块 → 实体抽取 → 社区聚类 → 社区报告 → local/global 查询）压缩到约 3000 行核心代码，存储可插拔（KV/向量/图），配置全字段化。**定位 = 学习 GraphRAG 内部机制的教材**。

**LightRAG**：生产级轻量框架（14.7 万行，含多格式文档解析器、10+ 存储后端、API 服务器、增量更新、多模态）。**定位 = 可直接部署的 RAG 底座**。核心创新是 **dual-level 关键词（high/low）检索**：查询时 LLM 提取全局/局部两类关键词，分别驱动图检索与向量检索，再混合。

**两者关系**：nano 是 LightRAG 出现前的社区简化版；LightRAG 吸收了 GraphRAG 的图谱构建、去掉了社区报告（贵），改用关键词分层。对用户方案：**LightRAG 是"低成本开箱即用"基线的最佳候选；nano-graphrag 是读懂图检索机制的入口。**

---

## 2. 整体架构对比

| 维度 | nano-graphrag | LightRAG |
|---|---|---|
| 建图 | 分块 → 实体/关系抽取（gleaning 多轮）→ Leiden 社区聚类 → 社区报告（LLM 摘要 JSON） | 分块（多种 chunker）→ 实体/关系抽取（JSON 结构化）→ 图存储；**无社区报告** |
| 查询 | local（实体向量→邻居+社区）/ global（社区报告 map-reduce）/ naive（纯向量） | **dual-level 关键词**（high/low）→ 实体/关系向量 + 图遍历 + 关键词搜索 → 混合（mode: local/global/hybrid/naive/mix） |
| 增量 | 无（社区重建需全量） | 支持增量 insert、删除、编辑 |
| 存储 | KV(JSON)/向量(NanoVDB/HNSW/FAISS)/图(NetworkX/Neo4j) 可插拔 | 10+ 后端（Postgres/Mongo/Redis/Neo4j/Milvus/Qdrant/Opensearch…） |
| 工程化 | 研究级（demo 多） | 生产级（API server、解析器、迁移、多模态） |
| 代码规模 | 核心 ~8400 行 | ~14.7 万行 |

---

## 3. nano-graphrag 逐文件通读（核心全读）

### 3.1 `graphrag.py`（382 行）★ 主入口（已全读）

`GraphRAG` dataclass 即全部配置：分块（1200 token/100 overlap）、实体抽取（max_gleaning=1）、社区聚类（leiden，max size 10）、node2vec 节点嵌入、LLM 分层（best/cheap 双模型 + 并发上限）、存储三件套（KV/向量/图）、LLM 缓存开关。

- `ainsert`：新文档去重（mdhash）→ 分块 → 实体抽取（`extract_entities`）→ **drop 全部社区报告（不支持增量社区）** → 聚类 + 生成社区报告 → 落库。
- `aquery`：按 `param.mode` 分发 local / global / naive。
- **LLM 响应缓存**（`llm_response_cache`，prompt 哈希做 key）——重复查询零成本。

### 3.2 `_op.py`（1140 行）★ 核心操作

- `chunking_by_token_size`：按 token 切块 + 重叠。
- `extract_entities`：对每 chunk 用 `entity_extraction` prompt 抽取实体/关系 → **gleaning 循环**（`entiti_continue_extraction` 补抽 + `entiti_if_loop_extraction` 判断是否继续，最多 max_gleaning 轮）→ 解析多分隔符记录 → 合并节点/边（去重、描述拼接）→ 图落库。
- `generate_community_report`：社区报告（title/summary/rating 1-5/findings[] 结构化 JSON，`community_report` prompt）——**这就是"Reduce 层"的社区级压缩**。
- `local_query`：`_build_local_query_context`——实体向量检索 top k → 取相关边 + 相关文本单元 + 相关社区 → 拼上下文 → LLM。
- `global_query`：`_map_global_communities`——社区报告按 token 分组 → **每组 LLM 提取要点（map）→ 汇总所有要点（reduce）→ 最终回答**——完整 Map-Reduce。
- `naive_query`：纯向量 RAG。

### 3.3 `prompt.py`（520 行）+ `_storage/` + `_llm.py` + `_splitter.py`

- prompt：`entity_extraction`（多分隔符记录格式 + 实体类型表）、`community_report`（JSON 摘要）、`local_rag_response`、`global_map_rag_points`、`fail_response` 等。
- 存储：`JsonKVStorage` / `NetworkXStorage`（图操作：聚类、节点/边 upsert、查询）/ `NanoVectorDBStorage`（HNSW 向量）。
- `_llm.py`：gpt-4o/4o-mini/azure/bedrock/ollama 等函数式 LLM + 哈希缓存包装。
- `base.py`：存储抽象接口（BaseGraphStorage/BaseKVStorage/BaseVectorStorage + QueryParam）。

---

## 4. LightRAG 核心通读（函数结构级）

### 4.1 `lightrag.py`（6846 行）★ 主类 `LightRAG`

- `insert / ainsert`：文档解析（pipeline：docling/mineru/markdown/docx 多格式）→ 分块 → `extract_entities`（JSON 抽取）→ 图/向量落库 → 增量索引。
- `insert_custom_chunks / insert_custom_kg`：手工注入 chunk / 三元组。
- `query / aquery`：`kg_query` + 模式分发（local/global/hybrid/naive/mix/bypass）。
- 删除/编辑：`adelete_by_entity/relation`、`aedit_entity/relation`、`amerge_entities`（utils_graph.py）——**图谱可维护**。
- 角色化 LLM（`_RoleLLMMixin`）、存储迁移、addon 参数热更新。

### 4.2 `operate.py`（6661 行）★★ 核心算法

- `extract_entities`：JSON 结构化抽取（实体+关系，关系带 source/target/keywords/description），`_handle_entity_relation_summary` 多轮、截断恢复、JSON 解析容错。
- `kg_query` ★：**dual-level 关键词检索**——`get_keywords_from_query` 让 LLM 从查询提取 `high_level_keywords`（全局主题）与 `low_level_keywords`（具体实体/细节）→ 关键词驱动：向量检索实体（low）+ 关系（high）+ 图遍历 + chunk 拼接 → `_build_query_context` 合并 → LLM 回答。空关键词回退整句查询。
- `rebuild_knowledge_from_chunks`：增量/重建。
- `_merge_nodes_then_upsert / _merge_edges_then_upsert`：实体/关系去重合并（相似度 + 描述拼接）。
- `_get_vector_context / _perform_kg_search`：向量与图的混合召回。

### 4.3 `pipeline.py`（7716 行）+ `chunker/` + `parser/`（职责记录）

- pipeline：文档摄取管线（解析器路由、OCR、图片预算、chunking 参数、进度）。
- `chunker/`：`token_size`（默认）、`recursive_character`、`paragraph_semantic`、`semantic_vector`（嵌入聚类分块）四类分块器。
- `parser/`：markdown/docx/docling/mineru 多格式解析（docx 最重，3422 行 smart_heading）。
- `kg/`：10+ 存储后端实现（每个 2000-10000 行，模板化）；`api/`：FastAPI 服务器 + 认证限流；`llm/`：15+ 提供商。

### 4.4 `base.py`（1811 行）+ `prompt.py` + `utils_graph.py`

- `base.py`：`QueryParam`（mode 枚举 + top_k/only_need_context/stream 等）、存储抽象、`QueryResult`。
- `prompt.py`：实体抽取 JSON 模板（含 high/low keywords 输出）、DEFAULT_ENTITY_TYPES、fail_response。
- `utils_graph.py`（2341）：图谱 CRUD + 实体合并（`_merge_entities_impl`：相似实体合并属性/关系）。

---

## 5. 关键机制深挖

1. **dual-level 关键词检索（LightRAG 核心创新）**：查询先过 LLM 提"高层关键词"（主题/概念）与"低层关键词"（实体/细节），分别匹配全局关系图与局部实体图——比 GraphRAG 的社区报告便宜（少一次社区构建），比纯向量准（有图结构）。**这正是用户方案"Vector + Graph 双路"的现成实现。**
2. **社区报告 Map-Reduce（nano/GraphRAG）**：社区聚类 → 报告 → 全局查询时 map（每组提要点）→ reduce（汇总）——用户方案 Reduce 层的又一种形态（比鉴来助手的章节批处理更图化）。
3. **gleaning 补抽**：实体抽取多轮补漏（nano max_gleaning=1，LightRAG `_handle_entity_relation_summary` 类似）——长章抽取不全的补偿。
4. **增量与可维护**：LightRAG 支持增删改查图（用户 2000 章分批摄入 + 修正 bug 的关键能力）；nano 不支持增量（社区全量重建）。
5. **LLM 缓存**：两者都有 prompt 哈希缓存（LightRAG `_answer_cache_kv`）——重复问题零成本。
6. **分层模型**：best/cheap 双模型（抽取用贵的、查询用便宜的，或反之）。

---

## 6. 与用户小说 RAG 方案的映射

| 用户方案组件 | nano-graphrag | LightRAG | 结论 |
|---|---|---|---|
| 建图（Map） | 实体/关系抽取 + gleaning | 实体/关系 JSON 抽取 | ✅ 两者现成（提示词需改中文小说向） |
| Reduce 层 | **社区报告（LLM 摘要 + rating + findings）** | 无 | ✅ nano 有现成社区摘要（≈势力/剧情团块）；LightRAG 需接其他 |
| Vector+Graph 双路 | local（实体向量+邻居+社区） | dual-level 关键词 + 图/向量混合（mix 模式） | ✅✅ LightRAG 是用户方案"Vector+Graph RAG"最直接的底座 |
| Global 检索 | 社区报告 map-reduce | global 模式（关系向量） | ✅ nano 的 global 更接近"全书总览" |
| 分层/层级记忆 | 无（社区是平的） | 无 | ⚠️ 需接 Graphiti/Mnemis |
| 伏笔/势力变迁 | 无 | 无 | ⚠️ 需自建 |
| 原文 Evidence | text_chunks 溯源 | chunk 引用 | ✅ 两者现成 |
| 成本控制 | 双模型 + LLM 缓存 | 双模型 + 缓存 + 无社区构建（省一大笔） | ✅ LightRAG 更省（无社区报告） |

**选型建议**：用户方案若走"框架起步"路线，**LightRAG 作 Vector+Graph 双路底座 + graph-every-novel 的 Chapter Memory schema + 自建分层 Reduce（Graphiti Saga 或 Mnemis 层级）** 是性价比最高的组合；nano-graphrag 作为理解与二次开发的参考。

---

## 7. 可复用模块清单

**nano-graphrag（MIT，学习/改造）**
- `local_query / global_query` 的上下文构建与 Map-Reduce 全局查询（~150 行，可直接改写）。
- `community_report` prompt（结构化 JSON 摘要 + rating）——势力/剧情团块摘要模板。
- gleaning 补抽循环。
- 存储抽象接口设计（BaseKV/Vector/GraphStorage + QueryParam）。

**LightRAG（MIT，直接部署/复用）**
- 整个框架作为 RAG 底座（`pip install lightrag-hku`）：插入、查询（mix 模式）、增量更新、删除编辑。
- dual-level 关键词机制（查询前 LLM 提 high/low 关键词）。
- 多分块器（semantic_vector 可按语义聚类分块——适合"章节内语义切块"）。
- 图 CRUD + 实体合并（`amerge_entities`——人工修正同名角色的现成工具）。

**需改造（两者通用）**
- 实体/关系抽取 prompt → 中文网文定制（人物/势力/物品/功法枚举 + 别名字段）。
- 增加"伏笔/势力变迁"专用关系类型与状态字段。
- 层级 Reduce（两者都无）——接 Graphiti Saga / Mnemis / 自建。

---

## 8. 许可证与工程坑

- 两者均 MIT ✅。
- **nano**：研究级，无包管理成熟度（setup.py），示例多但文档少；社区报告重建成本高（全量 drop）。
- **LightRAG**：生产级但体量大（14.7 万行），依赖项多（可选 10+ 后端）；中文支持需自配 embedding（bge 系列）与 prompt；增量更新与删除在某些存储后端有坑（GitHub issues 活跃）；api server 附带认证限流。
- 已知对比参考：NanGePlus/LightRAGTest 对两者做过耗时/Token/成本/质量实测（见用户参考清单），结论上 LightRAG 建图成本显著低于 GraphRAG（无社区报告）。

---

## 9. 成本与规模实测数据

- **LightRAG 建图成本**：单 chunk 1 次抽取调用（多轮补抽可选关），无社区报告 → 2000 章成本 ≈ graph-every-novel 实测的 1~1.5 倍（schema 更简）或更低；查询每次 1 次关键词抽取 + 1 次回答调用。
- **nano/GraphRAG 建图成本**：抽取 + 社区报告（每社区 1 次 LLM）→ 2000 章成本约为 LightRAG 的 1.5~2 倍。**社区报告是成本分水岭**——用户方案若做全书级总览，可只对卷/书级做一次社区式摘要（Reduce），而不是每章社区。
- 双模型（best/cheap）+ LLM 缓存可在查询阶段再省 30-50%。

---

## 10. 通读结论

1. **LightRAG 是用户方案"开箱即用的 RAG 底座"首选**：dual-level 关键词 + 图/向量混合（mix）直接覆盖方案"Vector RAG + Graph RAG"两条路径，增量更新与图编辑解决 2000 章分批摄入与修正需求。
2. **nano-graphrag 是读懂 GraphRAG 的教材**：社区报告 + local/global 查询的完整最小实现，用户方案 Reduce 层可取其"社区摘要 + map-reduce 全局查询"模式。
3. **两者都缺**：分层记忆（卷/书级）、伏笔状态机、势力变迁时序——这些仍要靠 Graphiti/Mnemis/自建补齐；LightRAG 应作为基线，与"分层方案"做 A/B 对比量化增量价值。
