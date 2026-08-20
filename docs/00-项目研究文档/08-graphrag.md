# 08 · Microsoft GraphRAG（microsoft/graphrag）核心管线通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/microsoft/graphrag ｜ 许可证：MIT
> 定位：微软官方模块化图基 RAG 系统（2024 年提出，2025 年重构为多包架构）。新版拆分 8 个包（graphrag / -common / -input / -chunking / -llm / -storage / -vectors / -cache），核心 422 个 Python 文件 / 约 3.7 万行。
> 本次通读范围：核心 `packages/graphrag`（index 构建管线 + query 检索）+ 各包职责。GraphRAG 是"社区摘要 + 层级报告 + local/global 检索"范式的定义者，用户方案 Reduce 层的官方参照。

---

## 1. 项目概况与定位

GraphRAG 提出"文档 → 知识图谱（实体/关系/声明）→ **分层社区（Leiden 聚类）→ 每层社区 LLM 摘要报告** → local/global 双路检索"范式。相比朴素 RAG 的最大价值：**全局性问题**（"整本书讲了什么"）可以通过社区报告的 map-reduce 回答，而不只是局部 chunk 拼接。

新版架构：CLI（`graphrag index` / `graphrag query`）+ Python API + 配置化工作流（workflows）+ 可插拔存储/LLM/向量。另有 drift-search（DRIFT 深度检索）等新检索策略。

**与用户方案的关系**：GraphRAG 是"层级摘要（Reduce）"的最完整官方实现；它的 local/global 检索对应用户方案 Query Router 的"局部细节/全局总览"分路。**主要短板**：建图成本高（社区报告 LLM 调用量大）、增量更新弱（有 update 工作流但复杂）、中文需自配 prompt。

---

## 2. 整体架构与数据流

```
CLI: graphrag index
────────────────────────────────────────────────────────
workflows（顺序编排，可配置化）：
  load_input_documents（多格式输入）
  → create_base_text_units（TokenTextSplitter 分块）
  → extract_graph（LLM 实体+关系抽取，JSON 容错）
  → create_communities（Leiden 聚类 → 分层社区 level 0/1/2…）
  → create_community_reports（每层每社区 LLM 报告：summary/rating/findings/claims）
  → finalize_graph → prune_graph（可选裁剪）
  → generate_text_embeddings（实体/文本单元/关系向量）
  →（可选 extract_covariates：声明/主张抽取）
  输出表：documents / text_units / entities / relationships / communities /
          community_reports / covariates / embeddings
────────────────────────────────────────────────────────
查询：graphrag query
  local_search（实体为中心：实体+邻居+文本单元+社区报告 混合上下文）
  global_search（社区报告 → map 每组要点 → reduce 汇总回答）
  drift_search（DRIFT：全局入门 + 局部深挖迭代）
  basic_search（朴素向量）
```

一句话：**建图管线产生"实体/关系/声明图 + 分层社区报告"，检索按问题类型选 local（局部细节）/ global（全局总览）/ drift（深度探索）。**

---

## 3. 核心模块通读

### 3.1 构建工作流 `index/workflows/` ★（分层 Reduce 参考）

- `create_base_text_units.py`：分块（text_splitting 的 TokenTextSplitter）。
- `extract_graph.py`：实体/关系抽取工作流（`graph_extractor.py` 188 行：多线程 LLM 抽取 + JSON 修复；实体类型可配置）。
- `create_communities.py`（207）：**Leiden 分层社区检测**——图的层级社区划分（level 0 最细 → 高层级更粗），社区是后续报告与全局检索的基本单元。
- `create_community_reports.py`（199）★：**社区报告生成**——读实体/关系/社区/声明表 → 每社区构建局部上下文（实体详情+边+声明）→ `summarize_communities`（按 **level 分层**逐层生成报告，`build_level_context` 层级上下文）→ 落 community_reports 表。报告含 summary/rating/findings/claims。
- `extract_covariates.py`：声明抽取（事件型主张：subject/predicate/object/status/start_time/end_time + description）——**对应用户方案的"事件/伏笔"建模雏形**。
- `generate_text_embeddings.py`：实体/关系/文本单元向量化。
- `finalize_graph.py` / `prune_graph.py`：图收尾与裁剪。
- `update_*` 系列：增量更新工作流（update_documents/entities/communities/reports）。

### 3.2 检索 `query/structured_search/` ★

- `global_search/search.py`（521）★：**GlobalSearch**——社区报告 → `_map_response_single_batch`（报告按 token 分组成批，每批 LLM 提取要点 JSON）→ `_reduce_response`（要点汇总 → 最终答案）。**Map-Reduce 全局问答**。
- `local_search/search.py`（183）+ `mixed_context.py`（493）★：**LocalSearch**——实体为中心的混合上下文：相关实体 + 一跳邻居边 + 相关文本单元 + 相关社区报告（`_build_community_context` / `_build_text_unit_context` / `_build_local_context`）。
- `drift_search/`（464+229+245+229）：DRIFT——先用全局搜索（社区报告）定"主题种子"，再迭代扩展实体/边，多轮收敛。
- `basic_search/search.py`（182）：朴素向量 RAG（对照组）。
- `context_builder/`：community_context / local_context / conversation_history（多轮对话记忆）。
- `question_gen/local_gen.py`：问题生成（对话引导）。

### 3.3 CLI 与配置 `cli/` + `config/`（职责记录）

- `cli/main.py`（481）+ `cli/query.py`（397）+ `cli/index.py`：`graphrag index` / `graphrag query` / `graphrag prompt-tune`（提示词自动调优）/ `graphrag initialize`。
- `config/defaults.py`（385）：全量默认配置（chunking/entity_extraction/communities/community_reports/embeddings…）。
- `prompt_tune/`：**提示词调优**——自动生成定制化的实体/关系抽取与社区报告 prompt（读文档生成领域提示词）——用户小说场景可直接用 `graphrag prompt-tune`。

### 3.4 支撑包（职责记录）

- `graphrag-common`：数据模型（Entity/Relationship/CommunityReport/Covariate/TextUnit）、时间戳、token 工具。
- `graphrag-input`：多格式输入（csv/json/jsonl/parquet/text/markitdown）。
- `graphrag-chunking`：分块器（token/sentence）。
- `graphrag-llm`：LLM 抽象（OpenAI/Azure/自定义 + 缓存 + 限流 + 多线程）。
- `graphrag-storage`：表存储抽象（pandas 内存 / parquet / cosmos / postgres 等）。
- `graphrag-vectors`：向量索引（Azure AI Search / cosmosdb / 本地 lancedb 等）。
- `graphrag-cache`：LLM 响应缓存（json/sqlite/azure blob…）。

---

## 4. 关键机制深挖

1. **分层社区（Leiden level）**：图聚类产生多粒度社区层级（level 0 细 → 高层粗）——**这就是"层级摘要"的官方实现**：每层社区都有 LLM 报告，全局查询从最粗层切入。用户方案 Arc/Volume/Book 分层可映射为"社区 level 高→低"或"卷/书级报告"。
2. **全局检索 = 社区报告 Map-Reduce**：map（每批报告提要点）→ reduce（汇总）——与 nano-graphrag `global_query`、鉴来助手 `generate_full_report` 同一模式的工业级版本。
3. **局部检索 = 实体中心混合上下文**：实体向量命中 → 一跳边 + 文本单元 + 社区报告四路拼接（`LocalSearchMixedContext`）。
4. **声明抽取（Covariates）**：事件型主张带时间窗与状态——"谁/何时/对谁/做了什么"的结构化事件，是用户方案"事件/势力变迁/伏笔"建模的可选补充。
5. **提示词调优（prompt-tune）**：读领域文档自动生成定制 prompt——中文网文场景减少手写 prompt 成本。
6. **增量更新**：update_* 工作流存在但复杂度高（社区/报告增量重建），LightRAG 的增量更轻——用户方案如选 GraphRAG 需评估此点。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | GraphRAG 对应物 | 结论 |
|---|---|---|
| 建图（Map） | extract_graph（实体/关系）+ extract_covariates（声明） | ✅ 现成（prompt 需调优为网文） |
| **Reduce 分层汇总** | **分层社区 + 每层社区报告** | ✅✅ **官方最强实现**，用户方案 Reduce 的首选参照 |
| Global 检索（全书总览） | global_search（报告 map-reduce） | ✅✅ 现成 |
| Local 检索（具体情节） | local_search（实体中心混合上下文） | ✅ 现成 |
| 深度探索 | drift_search | ✅ 可选增强 |
| 势力/剧情团块 | 社区（实体聚类） | ✅ 社区≈势力团块，报告≈势力总览 |
| 事件/伏笔 | covariates（声明：subject/predicate/status/时间窗） | ✅ 事件建模可用，伏笔状态需扩展 |
| 原文 Evidence | text_units + 文本单元检索 | ✅ 现成 |
| 成本控制 | — | ⚠️ 社区报告成本高（nano/LightRAG 均验证），需评估 |
| 增量更新 | update_* 工作流 | ⚠️ 复杂，LightRAG 更轻 |

---

## 6. 可复用模块清单

**直接复用（MIT，pip install graphrag）**
- 整套 index 管线 + 查询（作为完整基线跑通，再评估是否需要自建分层）。
- `graphrag prompt-tune`：自动生成中文网文向抽取/报告 prompt。
- global_search 的 map-reduce 报告查询（可移植到自建方案）。
- local_search 的混合上下文构建逻辑（实体+边+文本单元+报告）。
- covariates（声明）抽取 schema（事件建模）。

**需改造/评估**
- 中文 embedding（bge 系列）与 prompt 调优。
- 增量更新：GraphRAG 官方 update 复杂——若需"分批摄入 + 修正"，评估 LightRAG 或自建。
- 社区报告成本：2000 章若每社区报告成本爆炸，可只对"卷/书级"做社区式报告（借鉴思路而非全量照搬）。

**仅参考**
- 各存储/向量后端实现、CLI、多格式输入。

---

## 7. 许可证与工程坑

- **MIT** ✅。
- 依赖较重：需要 pandas/parquet 工作流，默认配置面向 Azure（向量/存储），本地化需换 lancedb/本地向量库。
- **建图成本最高**（三个候选框架中）：实体抽取 + 每社区报告（Leiden 社区数可能上千）。NanGePlus/LightRAGTest 实测：GraphRAG 建图成本显著高于 LightRAG。
- 新版重构后文档分散（docs/ 目录 + mkdocs），CLI 是主要入口；社区版无 Azure 也能跑（lancedb + 本地模型）。
- 提示词调优（prompt-tune）是中文场景的必需品——默认英文 prompt 直接跑中文小说效果差。

---

## 8. 成本与规模实测数据

- 官方无整书级数据；社区实测（NanGePlus 等）：GraphRAG 建图 token 消耗约为 LightRAG 的 2-4 倍（社区报告为主因）。
- 外推：若用户方案用 GraphRAG 全量（2000 章实体抽取 + 全社区报告），成本 ≈ graph-every-novel 实测的 3~5 倍（400 万字 ¥150~600 区间，DeepSeek 级）。
- **成本控制建议**：实体抽取全量做，社区报告只对"高层级社区（卷/书级）"生成，或改用 LightRAG + 自建卷级摘要。

---

## 9. 通读结论

1. **GraphRAG 定义了"层级社区摘要 + local/global 检索"范式**，是用户方案 Reduce 层与全局检索的官方参照；它的分层社区报告比鉴来助手/Graphiti 的摘要更图化、更完整。
2. **工程选型上它是"重方案"**：建图成本最高、增量复杂、默认 Azure 向——适合作为**对照基线**（跑通看上限）而非默认底座；默认底座建议 LightRAG（轻）或 Graphiti（图+记忆）。
3. **prompt-tune 与 covariates（声明）是两个值得单独借鉴的模块**：前者省中文 prompt 工作，后者是事件建模的现成 schema。
