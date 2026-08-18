# 03 · graph-every-novel（Renakoni/graph-every-novel）代码通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/Renakoni/graph-every-novel ｜ 许可证：MIT
> 定位：**本地优先的"长篇小说 → 人物关系图谱"引擎**。逐章 LLM 抽取 → 本地累计状态 → 量化 → 稳定图筛选 → 导出 `character_graph.json` 供图谱前端消费。
> 代码规模：54 个 Python 文件 / 约 3 万行（核心 `src/novel_graph_engine/` 38 个文件）；配套前端 [novel-graph-viz](https://github.com/Renakoni/novel-graph-viz)。
> **这是用户方案"Map 阶段（Chapter Memory）"最直接、最完整的工程参考，且自带可外推的真实成本实测。**

---

## 1. 项目概况与定位

Graph Every Novel 处理的主链路：导入小说 → 整理章节 → **逐章抽取**（人物、事件、互动、关系线索）→ **跨章累计**（本地 SQLite 状态）→ **量化**（重要度、关系支持度、图结构支持度）→ **稳定图筛选** → 导出。它明确反对"单章瞬时判断"，强调"能跨章节、跨上下文站得住的结果"。

技术栈：Python 3.11 + Flet（桌面 UI）+ SQLite + Pydantic + NetworkX + OpenAI 兼容 API。单人/小团队开发，代码直白、中文注释与文档齐全（`docs/量化.md`、`docs/设计说明.md`、`docs/HANDOFF.md`）。

**与用户方案的关系**：它不涉及向量库/图数据库/检索（没有 RAG 部分），但它把"逐章 Map + 跨章累计"这一层做到了生产级细节，**是 Chapter Memory 的字段 schema 与状态合并机制的现成蓝本**。用户方案的压缩层/检索层需要另配，但本章是"怎么把每章变成结构化记忆"的最佳参考。

---

## 2. 整体架构与数据流

```
小说文件 (EPUB/TXT/JAR)
      │ import_epub / import_txt / import_jar
      ▼
ProjectWorkspace（工作区：chapters/ + analysis/ + state/analysis.sqlite + export/ + logs/）
      │
      ▼
AnalysisEngine.analyze_chapter()
  ├─ _extract_chunk：长章分块 → 逐块 LLM 抽取（AnalysisResult）
  ├─ _merge_results_recursively：多块结果 LLM 递归合并
  ├─ _run_relation_sweep_if_needed：关系不足时对"主角×重要角色对"补抽
  └─ merge_against_memory：与全书记忆合并（对齐/归一化）
      │
      ▼
ProjectAnalysisStore.upsert_chapter_analysis → rebuild_memory_state
（SQLite：chapter_analyses + entity_states + relation_states + pair_relation_states
  + memory 表（最近摘要 / book_memory 全书记忆）+ 修正表）
      │
      ▼
recompute_entity_quantification（六维加权 → importance/tier，带迟滞）
      │
      ▼
exporter：build_character_graph_export
  稳定图筛选（支持度/时间/锚定/互惠多维打分）→ character_graph.json
```

一句话：**每章一次"抽取→合并→记忆回写"循环，全书跑完得到稳定的人物关系图谱。**

---

## 3. 模块逐文件代码通读

### 3.1 章节分析 Schema `schema/analysis.py`（303 行）★★（最值钱的部分）

`AnalysisResult`（单章分析 = 用户方案的 Chapter Memory 字段级参考）：

| 字段 | 说明 | 对应小说能力 |
|---|---|---|
| `summary` | 本章摘要 | 前情提要 |
| `outline[]` | 事件大纲（order/text/involved_names） | 剧情骨架 |
| `entities[]` | `EntityMention{name, aliases, chapter_role, tier, importance_score, summary, confidence}` | 人物出场；`aliases` 现场收集别名；`chapter_role` ∈ protagonist/major_actor/supporting/mentioned/background/transient |
| `identity_revelations[]` | `IdentityRevelation{source_name, target_name, revelation_type, evidence}` | **马甲/身份揭示**（"少年=杨过"、"神秘人=主角"）——网文伏笔核心 |
| `events[]` | `ChapterEvent{event_type, narrative_weight, participants[], location, evidence_quotes}` | 剧情事件；`narrative_weight` ∈ core/mainline/turning_point/detail/background |
| `interactions[]` | `InteractionUpdate{from,to,interaction_type,impact_level,polarity,evidence_quotes}` | 单次互动（并肩作战/背叛/决斗…） |
| `directed_relations[]` | `DirectedRelationUpdate{from,to,raw_label,structural_base,dynamic_stance,affinity/intensity/stability/strength 四分数,evidence_quotes,relation_analysis}` | **有向关系状态**（师徒/敌对/君臣…+动态立场） |
| `pair_relations[]` | `PairRelationUpdate{entity_a,b,shared_structural_base,pair_summary,shared_intensity_score}` | 双人无向共享关系（反复出现的对子） |
| `fact_updates[]` | `FactUpdate{subject,predicate,object_value,is_state,retracted}` | 事实状态（可撤回） |
| `unresolved[]` | `UnresolvedMention{surface_name, reason, candidates}` | **显式暴露消歧失败**（供人工/后续修正） |

所有枚举带 `*_raw`（原文短语）+ `enum_reason`（选择理由）审计字段，细节全部放 summary/evidence——**"受控枚举 + 原文证据"是防止 LLM 枚举幻觉的工业级做法**。

### 3.2 本体 `project/ontology.py`（371 行）★

中文网文关系枚举体系（可直接抄）：
- `STRUCTURAL_BASES`（关系结构基类 24 种）：血亲（亲子/手足/配偶/婚约/宗族/收养）、同门（同阵营/同队/师兄弟）、朋友/发小/对手组合、主仆/君臣/师徒/师生/上下级/雇佣、同事/同学/室友、契约/誓言/监护、恋人、临时同盟、敌对阵营…
- `DYNAMIC_STANCES`（动态立场 18 种）：友好/信任/守护/依赖/仰慕/爱慕/戒备/不信任/敌对/畏惧/怨恨/嫉妒/执念/顺从/支配/矛盾…
- `INTERACTION_TYPES`（互动 28 种）：日常/合作/并肩作战/救援/守护/告白/承诺/赠予/教导/命令/冲突/威胁/背叛/欺骗/揭示/分离/重逢/牺牲/羞辱/和解/决斗/训练/契约/附身/精神控制/政治博弈/认亲…
- 全部带**中文别名归一化映射**（"师徒"→master_disciple、"情敌"→rival_pair…），LLM 输出中文别名也能归一到受控值。

### 3.3 分析引擎 `engine/analysis_engine.py`（1426 行）★★

`AnalysisEngine` 核心方法：
- `analyze_chapter`：整章入口。长章节 `_extract_chunk` 分块抽取 → `_merge_results_recursively` 递归合并 → 关系回扫 → `merge_against_memory` 记忆合并。
- `_extract_chunk_json / _extract_chunk_oplog`：JSON 与"操作日志"两种抽取输出模式（oplog 为实验特性）。
- `_repair_analysis_result`：schema 修复（`render_schema_repair_prompt`）——LLM 输出不合 schema 时二次修复。
- `_run_relation_sweep_if_needed`（633）★：**关系回扫**。当主角数量多但抽出的关系太少时，对"主角×重要角色 + 事件参与者对"（`_build_relation_sweep_candidate_pairs`）用 LLM 补一轮关系抽取——补偿单章抽取不全。
- `_merge_results_recursively`（706）★：分块结果按 `chunk_group_size` 分组 → 每组 LLM 合并（`_merge_result_batch`）→ 递归至单一结果；单组退化时本地合并兜底。
- `merge_against_memory`（824）★★：**跨章记忆合并**。把 `recent_summary`（近几章摘要）+ `book_memory`（全书记忆，拆成 entity_registry + relation_history）注入 `STATE_MERGE` prompt，让 LLM 把本章抽取与全书已知状态对齐（改名、归一化、去重）→ 返回规范化 AnalysisResult。失败时回退为原始抽取结果（降级不中断）。

### 3.4 状态存储 `project/analysis_store.py`（6226 行）★★（SQLite 记忆中枢）

- 表结构：`chapter_analyses`（每章原始 AnalysisResult JSON）、`entity_states`（`EntityState`：canonical_name/appearance_count/chapter_role_distribution/recent_chapter_orders/tier…）、`relation_states`、`pair_relation_states`、`memory`（`get_recent_summary` / `get_book_memory`）、修正表（`entity_corrections` 人工改名、`pair_relation_override`）。
- `rebuild_memory_state`（1194）：从全部章节分析**重建/增量更新**实体/关系状态与记忆文本——状态累计中枢。
- `canonicalize_result`（3103）：基于已累计的实体状态把本章结果的名字规范化为 canonical 名（含别名表）。
- `apply_entity_corrections`（2834）：人工修正覆盖层（用户把"少年"改名"杨过"）。
- `get_book_memory`（2172）：拼装全书记忆文本（entity_registry + relation_history，`build_state_merge_context` 3429）。
- 量化状态暂存（stash/load）、关系决策审计日志、调试快照齐全。

### 3.5 量化 `project/quantification.py`（446 行）★

`recompute_entity_quantification`：人物重要度 = 六维加权和：
- centrality（图中心性，权重 0.26）+ recent_presence（近期出场衰减，0.12）+ event_participation（事件参与，0.22）+ relation_intensity（关系强度累积，0.20）+ pair_structure（双人结构，0.10）+ chapter_role（章节角色分布，0.10）
- 各维先归一化，再按 value(0.7)+rank 百分位(0.3) 合成 importance_score（0-100）。
- 分档：core（5%）/ active（20%）/ background / transient，**带迟滞（`_apply_tier_hysteresis`）防抖动**——人物等级不因单章波动跳变。
- 近期窗口自适应（全书 8%，6~30 章之间）；`transient` 有严格上限（出现 1 次、重要度 ≤15）。

### 3.6 导出与稳定图筛选 `project/exporter.py`（1767 行）★★

`_compute_stable_graph_eligibility_score`：**每条候选边多维打分，决定是否进入最终稳定图**：
- temporal_persistence（时间持久性 = 提及数 + 最近出现衰减，`0.58*mention + 0.42*recency`）
- pairwise_temporal_support（成对时间支持 = 双人强度 + 共同邻居 + 时序游走支持）
- recurrence（复发：多次提及 + 多次共同出场）
- reciprocity_or_role_consistency（互惠/角色一致性：无向 pair 边 / 反向边加分）
- projection_support（跨投影一致性 ≥90 分强加分）
- typed_anchor_score（结构基类锚定：师徒/血亲类天然高权重）
- **锚定提示检测**（`_detect_stable_anchor_hint`：summary 中"结拜/拜师"等强语义短语）
- 弱关系过滤（`_is_weak_unknown_pair_for_viewer` 等）→ 连通性恢复保底（`_restore_connectivity_safeguard_pair_edges`：保证图不散架）
- 输出三种导出：`project.json`（全量）、`viewer_project.json`、`character_graph.json`（前端主输入）。

### 3.7 提示词 `prompts/defaults.py`（685 行）★

- `CONTROLLED_ENUM_RULES`：受控枚举纪律（枚举只允许表内值、细节进 summary、带 *_raw/enum_reason 审计）。
- `EXTRACTION_SYSTEM_PROMPT`：完整章节抽取 schema（上文 3.1 的 JSON 模板，含实体/关系/事件/互动/身份揭示/事实更新）。
- `MERGE_SYSTEM_PROMPT / STATE_MERGE_SYSTEM_PROMPT`：分块合并与记忆合并的 LLM 角色设定。
- `RELATION_SWEEP_SYSTEM_PROMPT`：关系回扫。
- `render_extraction_prompt / render_merge_prompt / render_state_merge_prompt / render_relation_sweep_prompt / render_schema_repair_prompt`：各自渲染函数。

### 3.8 AI 客户端 `ai/`（职责记录）

- `openai_compatible_client.py`（279）：统一 chat 客户端（含 max_tokens 控制、超时）。
- `local_pair_classifier.py` / `openai_local_pair_classifier.py` / `openai_profile_relation_projector.py`：**本地无 LLM 的降级分类器**（规则/启发式）与"档案关系投影"（profile projection：把角色档案里的长期关系补进图）。
- `local_relation_adjudicator.py`：**关系裁决器协议**——对不稳定的双人共享关系做定点裁决（README 强烈建议启用；本地规则版 + OpenAI 版）。

### 3.9 外壳层（职责记录）

- `project/workspace.py`（4450）：工作区目录管理、导入（epub/txt/jar）、章节增删改（rename/split/merge）。
- `importers/`：epub（357）/txt（341）/jar（158）解析器（章节切分 50+ 格式）。
- `pipeline/chunking.py`：章节分块。
- `ui/flet_app.py`（3301）：桌面 UI。
- `utils/`：`ai_debug_logger`（每次 LLM 调用落盘 prompt/response 审计）、`diagnostic_bundle`（出问题打包排查）、`enum_audit`（枚举越界审计）、`json_utils`（JSON 容错解析）。
- `experimental/oplog_*`：实验性"操作日志"抽取（oplog 解析器，未转正）。
- `tools/`：chapter_pair_benchmark（章节对基准）、UI 启动脚本。
- `tests/`：14 个测试文件（`test_quantification.py` 897 行、`test_project_workspace.py` 4019 行等）。

---

## 4. 关键算法/机制深挖

1. **跨章记忆合并（merge_against_memory）**：每章抽取结果不直接落库，而是先与"全书记忆"（entity_registry + relation_history）交给 LLM 合并对齐——**这本质是用户方案"Chapter Memory 与已有记忆增量融合"的实现**，且用 LLM 而非规则做改名归一。
2. **受控枚举纪律**：所有关系/事件/角色字段只允许受控枚举值，细节与证据进 summary/evidence_quotes，附 *_raw + enum_reason 审计。防止 LLM 自由造标签导致跨章无法对齐。
3. **关系回扫**：单章抽取关系不足时补一轮"主角×重要角色对"的定向抽取，保证关键关系不因单章视角遗漏。
4. **稳定图筛选**：跨章累计后不直接导出，而是用"时间持久性+互惠+复发+锚定"等多维证据打分，只留"全书视角站得住"的边——对应用户方案"跨章节剧情与细节"的质量门槛。
5. **人物量化 + 迟滞**：六维加权重要度 + 分档 + 迟滞防抖动。
6. **身份揭示**：`identity_revelations` 显式建模"同人异名/马甲"（伏笔回收的原型）。
7. **降级优先**：任何 LLM 步骤失败都回退（抽取失败→空结果、记忆合并失败→原始抽取），全书流程不中断——2000 章离线跑批的鲁棒性范本。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | graph-every-novel 对应物 | 结论 |
|---|---|---|
| 章节 Chunk | importers + chunking（长章分块） | ✅ 现成 |
| Map：Chapter Memory | `AnalysisResult` schema + `analyze_chapter`（抽取→合并→记忆对齐） | ✅ **字段级直接复用**（比鉴来助手 schema 更丰富：身份揭示/事件/互动/双人关系） |
| 人物/关系/势力/事件/伏笔 | entities/directed_relations/pair_relations/events/identity_revelations + 网文本体枚举 | ✅ 现成（伏笔≈identity_revelations + fact_updates，需补显式伏笔状态机） |
| 跨章累计 | analysis_store（SQLite 状态）+ merge_against_memory + canonicalize_result | ✅ 现成 |
| 压缩层主检索源 | book_memory / recent_summary（记忆文本） | ✅ 现成雏形（但未做向量化/图存储） |
| Reduce 分层汇总 | 无（只累计不分层摘要） | ⚠️ 需接 Graphiti 社区/Saga 或自建 |
| 图/向量/层级检索 | 无（NetworkX 仅算中心性） | ⚠️ 需外接（Graphiti/LightRAG） |
| 原文 Evidence 回查 | evidence_quotes（抽取时保留原文引文） | ✅ 部分：有引文，无章节全文向量化 |
| 成本控制 | `_with_max_tokens`、provider 配置、降级路径 | ✅ 现成 |

---

## 6. 可复用模块清单

**直接复用（MIT，代码可整体借鉴）**
- `schema/analysis.py` 全套字段模型 + 枚举归一化 validator（改造成你的 Chapter Memory JSON schema 的底稿）。
- `ontology.py` 中文网文关系/立场/互动枚举 + 中文别名映射（直接抄）。
- `merge_against_memory` 的 STATE_MERGE prompt 与调用结构（跨章记忆合并）。
- `relation_sweep`（关系回扫）与 `_merge_results_recursively`（分块递归合并）。
- `quantification.py` 人物重要度六维加权 + 迟滞分档。
- exporter 的稳定图筛选多维打分。
- 受控枚举纪律（CONTROLLED_ENUM_RULES）+ enum_audit + ai_debug_logger（LLM 调用全审计）。

**需改造**
- 状态存储：SQLite → 或并入 Graphiti 图库（若走图方案）或保持 SQLite + 向量库双写。
- 本体枚举按你的题材扩充（玄幻/都市/科幻枚举不同）。
- 补"伏笔状态机"（开放/推进/回收）与"势力变迁"显式字段。

**仅参考**
- Flet UI、importers（若自有章节文件格式则跳过）、oplog 实验模块。

---

## 7. 许可证与工程坑

- **许可证 MIT** ✅。
- 无版本锁定的工程化依赖管理（pyproject.toml 852B，纯手写）；项目处于 v0.1.0，代码直白但文档（HANDOFF）也提示仍在迭代。
- **成本实测（README 官方）**：《红楼梦》120 章 / 858,628 字：输入 3,659,440 + 输出 897,498 = 4,556,938 tokens，¥5.43（doubao-seed-2.0-lite ≤32k 档，未计缓存），总耗时 1h45m，单章 19s~2m13s。
- 引擎要点：逐章顺序处理（记忆合并依赖前文，不能随意乱序并行）；每章约 3~5 次 LLM 调用（抽取 + 可选回扫 + 记忆合并 + 分块合并），比鉴来助手重，但产出是结构化关系状态。
- 关系裁决器（local_relation_adjudicator）README 建议启用，能显著收束不稳定关系。

---

## 8. 成本与规模实测数据（外推）

| 规模 | 估算总 Token | 估算成本 | 估算耗时 |
|---|---|---|---|
| 85.8 万字（实测） | 456 万 | ¥5.4 | 1h45m |
| 400 万字（×4.7） | ~2,100 万 | ¥25–80 | ~8–10h |
| 700 万字（×8.2） | ~3,700 万 | ¥45–140 | ~14–18h |

备注：上表为"逐章抽取+记忆合并"单管线成本（用户方案的 Map 层）。加 Reduce（卷/书摘要）、图构建、伏笔字段后总成本约为 2~3 倍，即 400 万字 ¥50–240、700 万字 ¥90–420（DeepSeek 级模型）。**这是目前最可信的外推基线**，用户方案的总体成本评估应以它为本、其他项目数据作交叉验证。

---

## 9. 通读结论

1. **graph-every-novel 是"逐章 Map + 跨章累计"层的最佳参考**：Chapter Memory 的字段 schema、网文本体枚举、跨章记忆合并、人物量化、稳定图筛选全部现成且工业级。
2. **它与 Graphiti/Mnemis 互补**：它解决"把章变成记忆"，Graphiti 解决"记忆存图 + 检索"，Mnemis 解决"检索路径选择"——三者拼起来 ≈ 用户方案除伏笔状态机外的全部。
3. **最大短板**：没有向量/图/层级检索（它的产出是给前端看的图谱，不是给 LLM 检索的记忆），需要外接；伏笔追踪只有"身份揭示"雏形，需补状态机。
