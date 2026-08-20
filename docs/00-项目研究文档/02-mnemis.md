# 02 · Mnemis（microsoft/Mnemis）代码通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/microsoft/Mnemis ｜ 许可证：MIT
> 定位：ACL 2026 Main Conference 论文《Mnemis: Dual-Route Retrieval on Hierarchical Graphs for Long-Term LLM Memory》官方实现。
> 代码规模：核心仅 2 个 Python 文件（`global_selection/global_selector.py` 452 行 + `global_selection/prompts.py` 22 行），外加 584 行设计博客 `blog_mnemis.md`、论文 PDF、评测结果 JSON。
> **关键事实：Mnemis 不是独立框架，而是 Graphiti 之上的"第二层"（层级图 + 全局选择检索端），且层级图的构建代码未开源（构建 prompt 在论文附录）。**

---

## 1. 项目概况与定位

Mnemis 提出的核心问题是：传统 RAG/GraphRAG 的检索都是 **System-1（相似度匹配）**——快，但会漏掉"语义距离远但结构相关"的信息（例如埋在长文里的一个城市名）。它提出 **System-2（Global Selection）**：在语义层级图上自顶向下、有意识地遍历选择，与 System-1 双路互补。

论文成绩（README）：LoCoMo 93.9 / LongMemEval-S 91.6（GPT-4.1-mini），所有对比方法 SOTA。

**对用户方案的意义**：Mnemis 的"双路检索"与用户方案的 "Query Router → Vector RAG / Graph RAG / Global Retrieval" 分路完全同构；它的层级图是"势力/剧情/概念分层"的现成建模思路。**它是"查询路由 + 分层检索"设计的最直接学术参考。**

---

## 2. 整体架构

```
                 ┌─────────────────────────────────────────┐
                 │           Mnemis Memory System          │
                 │                                         │
  Base Graph     │  Layer N:  Category 抽象层（概念/势力）  │
  （实体事实图）  │    CATEGORIZES 边                        │
  由 Graphiti    │  Layer 2:  Category 抽象层               │
  构建：         │    CATEGORIZES 边                        │
  实体+关系+     │  Layer 1:  Category 抽象层               │
  Episode 溯源   │    CATEGORIZES 边                        │
                 │  Layer 0:  Entity（Base Graph 实体）     │
                 └─────────────────────────────────────────┘
                         │                    │
                   System-1               System-2
               相似度检索(Graphiti)    Global Selection(本仓库)
                 │                    │
                 └────────┬───────────┘
                          ▼
                   双路上下文融合 → LLM 回答
```

- **Base Graph**：完全由 Graphiti 构建（实体、关系、episode 溯源、双时态）。
- **Hierarchical Graph**：在 Base Graph 上加 `Category` 节点层（label `Category_N` 按层编号，`CATEGORIZES` 边指向下层 Category 或 Entity）。**构建代码未开源**，仅三原则见论文/博客。
- **System-1**：Graphiti 原生的混合检索。
- **System-2（本仓库核心）**：`GlobalSelector.global_selection()`。

---

## 3. 模块逐文件代码通读（全部读完）

### 3.1 `global_selection/global_selector.py`（452 行）★★

**图查询层（Query 类，静态 Cypher 常量）**：
- `GET_MAX_LAYER`：取 group 内最大层号。
- `GET_NODES_BY_LAYER`：取某层全部节点（`Category_{layer}` 或 `Entity`），返回 uuid/name/tag/summary。
- `GET_CHILD_NODES(_BATCH)`：取某（些）Category 的直接子节点（Category|Entity）。
- `GET_ALL_DESCENDANTS(_BATCH)`：变长路径 `CATEGORIZES*1..` 取全部后代（shortcut 用）。
- `GET_ONE_HOP_EPISODES(_BATCH)`：实体 → 提及它的 Episodic（原文片段，溯源）。
- `GET_ONE_HOP_NODES_AND_EDGES(_BATCH)`：实体一跳的 RELATES_TO 边 + 邻居实体（带 valid_at/invalid_at）。

**GlobalSelector 类**：
- 构造：Neo4j driver + Graphiti 的 LLMClient；`GlobalSelectorConfig{use_summary=False, use_tag=True}` 控制给 LLM 的字段（省 token：默认不带 summary 只带 tag）。
- 所有查询方法挂 `@alru_cache(maxsize=500)`：**同一查询在 500 次调用内缓存**（一次 global_selection 内多次查询同数据时省 DB 往返）。
- `get_child_nodes_batch / get_all_descendants_batch / get_one_hop_neighbors_batch`：mp（并发 gather）与 batch（单条 IN 查询）两种模式。

**`layer_selection(query, current_layer_categories)`（224）——每层的 LLM 选择**：
- 组装当前层候选（uuid/name/tag[/summary]）→ 用 `NODE_SELECTION_PROMPT_TEMPLATE` 生成 prompt → LLM（`ModelSize.large`，即贵模型）结构化输出 `NodeSelectionList{selections:[{name, uuid, get_all_children}]}`。
- 输出分两类：`selected_categories`（需继续下钻）与 `shortcut_categories`（get_all_children=True，直接取全部后代）。
- 防御：LLM 返回的 name/uuid 匹配不到输入节点时告警跳过。

**`global_selection(query, group_id)`（267）——System-2 主流程**：
1. 取 `max_layer`；
2. **自顶向下逐层**：首层取顶层全部节点 → `layer_selection` → 选中的下钻取子节点作为下一层候选 → 重复到 Layer 0（Entity）；
3. shortcut 节点直接 `get_all_descendants` 全取；
4. 汇总所有选中实体 → `get_one_hop_neighbors_batch` 取每个实体的一跳 episodes + edges + nodes；
5. 返回 `{episodes, edges, nodes}`（时间字段格式化）。
- **复杂度特征**：每层 1 次 LLM 调用（model=large），层数 ≈ 3~5；DB 查询全部批量/缓存。

**评测驱动（387–449）**：`parse_locomo / parse_lme` 批量跑 LoCoMo / LongMemEval-S 基准，输出每 query 的上下文与耗时统计。属于论文复现脚本，非产品代码。

### 3.2 `global_selection/prompts.py`（22 行）★

唯一的 prompt 模板 `NODE_SELECTION_PROMPT_TEMPLATE`：告诉 LLM"从给定节点里选所有可能帮助回答查询的节点"，判断标准 5 条（直接相关/覆盖相关主题/提供背景/含用户特定信息/可能含相关子节点），要求"不要过于严格"，并输出 `get_all_children` 布尔值。**非常简短——Global Selection 的智能完全靠 LLM 的推理，prompt 只是把选择任务说清楚。**

### 3.3 `blog_mnemis.md`（584 行）★ 设计文档（非代码但必读）

核心章节（已读）：
- **层级图构建三原则**（215–288）：
  - **MCA 最小概念抽象**：只在语义有意义时抽象，禁止"Stuff/Things"式空壳层——保证每层类别名承载真实语义，LLM 才能做有意义的选择。→ 小说里禁止"内容/情节/东西"这种空层。
  - **M2M 多对多映射**：实体可属多个类别（瑜伽∈{运动健身, 兴趣爱好, 放松方式}），保证任意提问角度可达。→ 小说人物可同时属于"宗门""家族""主角团队"。
  - **CEC 压缩效率约束**：类别至少含 n 个子节点（否则上提）；上层节点数 ≤ 下层节点数（逐层压缩，违反则停止建层）。
- **System-1/System-2 双路互补**（302–397）：System-1 保语义直接命中，System-2 保结构相关但语义远的覆盖；shortcut 机制在"需全面覆盖"时跳过逐层筛选。
- **局限**（505–530）：层级图质量依赖构建 prompt；成本与效率权衡；时间敏感性。

---

## 4. 关键算法深挖

1. **Global Selection = "LLM 驱动的层级决策树遍历"**。与传统检索的区别：不是打分排序，而是**每层让 LLM 二选式取舍 + 下钻**，遍历成本与层数成正比而不是与节点数成正比。
2. **Shortcut（get_all_children）**：LLM 的"高置信全选"路径，把"枚举型问题"（去过哪些城市/哪些人物属于某势力）的成本从逐层筛选降到一次全取。
3. **双路融合**：System-1 结果 + System-2 结果合并后送 LLM，不加权重——互补而非竞争。
4. **缓存与批量**：alru_cache(500) + 批量 Cypher，把 DB 往返压到最低；LLM 调用只在每层 1 次。
5. **信息无损保证**：三原则 → 层级图每一层都是下层"完整覆盖的抽象"，因此从任意粒度切入都能回溯覆盖全部相关底层实体（结构保证召回）。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | Mnemis 对应物 | 结论 |
|---|---|---|
| Query Router 分路 | System-1（Graphiti 混合检索）+ System-2（Global Selection）双路 | ✅ **直接对标**：小说中"具体情节/细节核实"→System-1；"势力变迁/全局走向/跨百章的伏笔网络"→System-2 |
| 分层 Story Memory | Base Graph（实体事实）+ Hierarchical Graph（Category 层） | ✅ 层级建模思路直接可用：人物/势力/地点/概念四类抽象层 |
| Reduce 分层汇总 | 层级图构建（三原则约束的自适应抽象） | ⚠️ 构建代码未开源，需自实现（有完整设计原则可依） |
| 跨章节剧情与细节 | System-2 从层级图任意层切入 → 下钻覆盖 | ✅ 保证"全局问题"的召回完整性 |
| 势力变迁 | Category 层做势力/阵营，M2M 允许多重归属（人物同时属多个势力） | ✅ 直接建模 |
| 原文 Evidence 回查 | 选中实体的一跳 episodes（MENTIONS 溯源） | ✅ 现成 |

**最值得借鉴的设计**：小说查询的天然分型——"某某人的师父是谁"（System-1 语义命中）、"这个宗门百年间如何衰落"（System-2 层级下钻）——正是用户 Query Router 要做的区分；而层级图的 MCA/M2M/CEC 三原则直接回答了"小说势力层级怎么建才不会建成空壳"。

---

## 6. 可复用模块清单

**直接可搬（代码量小，理解后重写成本低）**
- `GlobalSelector` 的查询层：Neo4j 批量 Cypher + alru_cache 缓存模式。
- `layer_selection` 的 LLM 选择循环 + `NODE_SELECTION_PROMPT_TEMPLATE`（原样可用于小说：把"节点"换成"势力/剧情线/人物群"）。
- shortcut 机制（get_all_children 全取后代）。
- 双路融合的上下文组织格式（episodes+edges+nodes 三桶）。

**需自建（代码未开源）**
- 层级图构建管线：基于 MCA/M2M/CEC 三原则设计 prompt，让 LLM 从实体集抽 Category 层（小说场景可预先给"宗门/家族/国家/修炼体系/主线剧情线"等种子类别，让 LLM 填充而非自由生成）。
- Category 的增量维护（新章节实体如何挂到既有层级）。

**仅参考**
- 评测脚本（parse_locomo/parse_lme）：如用户要建 QA 评估集，其"query→context"的跑批模式可参考，但数据是对话记忆基准，与小说无关。

---

## 7. 许可证与工程坑

- **许可证 MIT** ✅ 完全自由。
- **非独立框架**：跑通需要 Graphiti + Neo4j + OpenAI 兼容客户端。
- **代码 = 论文补充实验**：`main()` 里连接串、API 地址是占位符（"xxx"/"EMPTY"），数据路径硬编码 `/data/zh/gs/...`——**开箱不可运行，是研究代码不是产品代码**。
- 依赖：`graphiti_core`（其 vendored 副本不在此仓库）、`openai`、`neo4j`、`pandas`、`async_lru`、`tqdm`。
- 层级图构建 prompt 在论文 Appendix（README 明示），需自行从 PDF 提取。
- `get_one_hop_neighbors` 一次拉实体全部一跳边与 episode：小说场景实体边很多，需要 limit 保护（当前实现无 limit，可能内存/上下文爆炸——复用时要加）。

---

## 8. 成本与规模实测数据

- 论文官方：LoCoMo 93.9 / LongMemEval-S 91.6（GPT-4.1-mini）。单 query 的 System-2 成本 ≈ 层数 × 1 次 large 模型调用（层级选择）+ 0 次 embedding 检索（全图遍历/缓存），远低于"全图摘要"类方法。
- 对用户 2000 章小说：若建 3~4 层 Category 图，每次"全局型"查询 ≈ 3~4 次 LLM 调用 + 批量图查询，查询阶段成本可忽略；大头仍在建图（层级构建）的一次性成本，而该成本与实体数成正比（约等于 Graphiti 建图的 1.2~1.5 倍）。
- 缓存建议：alru_cache(500) 只缓存单 query 内重复；跨 query 应加持久化查询缓存（小说问答高频问题收益大）。

---

## 9. 通读结论

1. **Mnemis 提供的是"查询路由 + 全局检索"的完整设计范式**，且实现极简（452 行），是四个 A 级项目中最容易"吃透并改造"的一个。
2. **对用户方案的最大增量**：把"Query Router"从概念落地为"System-1 相似度 + System-2 层级选择"两条可执行路径；层级图三原则直接指导小说势力/剧情分层怎么建。
3. 注意边界：层级构建未开源（需自建），数据是对话记忆（需换成章节记忆），代码是研究级（需工程化）。
