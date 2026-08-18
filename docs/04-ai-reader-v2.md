# 04 · AI-Reader-V2（mouseart2025/AI-Reader-V2）代码通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/mouseart2025/AI-Reader-V2 ｜ 许可证：AGPL-3.0（商用需另购商业许可）
> 定位：**中文小说分析可视化平台**——上传 TXT/MD 小说 → 章节切分 → 实体预扫描 → 逐章 LLM 抽取 ChapterFact → 聚合为人物/地点/物品/组织档案 → 关系图/世界地图/时间线/百科全书可视化 + RAG 问答。React + FastAPI + SQLite + ChromaDB，支持 Ollama 本地 + 10 大云端 LLM。
> 代码规模：后端 187 个 Python 文件 / 约 6 万行 + 前端 React/TS + 550 后端测试。**9 个项目中体量最大、中文小说场景积累最深（v0.72，多轮《红楼梦》《西游记》《水浒传》真实书回归）。**

---

## 1. 项目概况与定位

AI-Reader-V2 是"AI 小说分析"方向最完整的开源实现，功能覆盖：书架管理、50+ 章节格式切分、实体预扫描（jieba+LLM）、逐章抽取（人物/关系/地点/物品/组织/事件/概念）、别名解析、世界地图（含地理位置层级）、多泳道时间线、人物关系图、势力图、百科全书、场景索引、RAG 问答、设定集导出（md/docx/pdf/xlsx）、模型基准测试。

README 声明"数据分析质量处于密集迭代期，未达可实用阶段"——但它的**质量工程**（golden standard 回归、FactValidator、防幻觉、命名管线、L1/L2/L3 变更分级）恰恰是其他项目没有的，对用户方案是"中文场景工程质量"的最大参考。

**与用户方案的关系**：它的"实体预扫描 + 逐章抽取 + 别名解析 + RAG 问答"完整覆盖方案 Map 层与轻量 Query 层；**别名解析（指代消解）与幻觉过滤是其他项目缺失、而中文网文最需要的两块**。它还发布过 ChiNovelKE 中文小说知识抽取 benchmark（可作验证集底子）。

---

## 2. 整体架构与数据流

```
TXT/MD 小说
   │ 章节切分（chapter_splitter，50+ 格式）
   ▼
[可选] 实体预扫描 entity_pre_scanner（jieba 分词 + n-gram 频次 + 称谓/命名模式 regex → LLM 分类）
   │ 输出 entity_dictionary（高频实体 + 别名 + 类型），注入抽取提示词
   ▼
逐章分析 AnalysisService
   ├─ ContextSummaryBuilder：构建前文上下文（前 N 章摘要 + 全部已知地点 + 共指消解指令）
   ├─ ChapterFactExtractor：LLM 抽取 ChapterFact（长章分段抽取 + merge）
   ├─ FactValidator：形态过滤（泛称/空名/坏实体剔除、地名修复、别名合并）
   └─ 写库 + WebSocket 进度推送（pause/resume/cancel，失败重试，错误分级）
   ▼
SQLite（15 表）+ ChromaDB（章节/实体嵌入）
   ▼
聚合层：entity_aggregator（PersonProfile 等动态聚合）→ alias_resolver（Union-Find 别名合并）
   → relation_utils（70+ 关系归一化）→ visualization_service（图/时间线）→ world_structure（地理层级）
   ▼
查询层 query_service：实体/关键词/关系过滤 → 拼上下文 → LLM 流式回答（带来源章节）
```

一句话：**预扫描词典 → 每章一次结构化抽取（带前文上下文）→ 校验 → 落库 → 聚合出档案与图谱 → 轻量规则检索 + LLM 问答。**

---

## 3. 模块逐文件代码通读（核心深读）

### 3.1 ChapterFact 模型 `models/chapter_fact.py`（136 行）★★（Chapter Memory schema 之二）

单章分析 = 用户方案 Chapter Memory 的第二个现成 schema（与 graph-every-novel 互补）：

| 字段 | 内容 |
|---|---|
| `characters[]` | CharacterFact{name, **new_aliases**, appearance, abilities_gained, locations_in_chapter}——**new_aliases 逐章现场收集** |
| `relationships[]` | RelationshipFact{person_a, person_b, relation_type, **is_new**, **previous_type**, evidence}——**关系变化显式建模** |
| `locations[]` | LocationFact{name, type, parent, peers, description, role(setting/referenced/boundary)} |
| `spatial_relationships[]` | 空间关系（contains/adjacent/direction/travel_path…带证据与置信度） |
| `item_events[]` | 物品流转（出现/获得/使用/赠予/消耗/丢失/损毁） |
| `org_events[]` | **组织/势力事件**（OrgEventFact{org_name, member, role, action: 加入/离开/晋升/阵亡/叛出/逐出, org_relation}）——**势力变迁** |
| `events[]` | EventFact{summary, type(战斗/成长/社交/旅行), importance, participants, location} |
| `new_concepts[]` | 概念（修炼体系/种族/货币/功法…） |
| `world_declarations[]` | 世界观声明（区域划分/层存在/传送门/区域位置） |

特点：**关系带 is_new/previous_type（关系变化追踪）、org_events 带行动枚举（势力成员变迁）、逐章 new_aliases 收集**——这三个字段直接支撑用户方案的"人物关系/势力变迁/伏笔"能力。

### 3.2 别名解析 `services/alias_resolver.py`（920 行）★★★（中文指代消解核心）

- **Union-Find 合并别名组**：`_UnionFind`（按 size 合并）+ 两路别名来源融合：`entity_dictionary.aliases`（预扫描）+ `ChapterFact.characters[].new_aliases`（逐章抽取）。
- **canonical 选择**：`name_authority.pick_canonical()`（单点权威，最短高频名优先）。
- **`_alias_safety_level()` 多级安全过滤** ★：**超大规模"不安全别名"黑名单**（`_KINSHIP_TERMS` 亲属称谓、`_GENERIC_PERSON_ALIASES` 泛称/代词/头衔/角色称呼/指代语，数百条）：这些词在不同章节指不同人，若作为 Union-Find 节点会产生"假桥"把无关角色合并——不安全名只透传其安全别名，不注册为 UF 节点。这是中文网文指代消解的**关键工程积累**（v0.66~0.71 多轮《西游》《红楼》《水浒》回归打磨）。
- **用户修正覆盖层**：`entity_overrides`（手动合并/拆分/改名），override 叠加在自动归并之后，可撤销、不污染原文；冲突检测（`get_alias_conflicts`）。
- 缓存：每书 alias map 内存缓存 + 失效钩子。

### 3.3 抽取管线 `extraction/`（核心）

- `chapter_fact_extractor.py`（586）：`ChapterFactExtractor.extract`——按 TokenBudget 决定截断/分段（>7000 字分 2 段、>12000 分 3 段，段间在段边界切），分段抽取后 `_merge_chapter_facts` 去重合并；VoT（验证式思考）引导注入；`prompt_registry` 集中管理提示词；`_is_transient_error` + 重试。
- `entity_pre_scanner.py`（716）：Phase 1 jieba 分词 + n-gram 频次 + 对话归属 regex + 后缀模式 + 命名模式（"叫作/名叫/绰号"）→ 候选；数字前缀名恢复（"二愣子"/"三太子"）；命名来源实体绕过频次门槛。Phase 2 LLM 分类 + 别名。→ **字典注入抽取提示词提升识别质量**。
- `context_summary_builder.py`（810）：**前文上下文构建（共指消解）**——注入全部已知地点 + 共指指令（让 LLM 把"小城"解析为"青牛镇"）；始终注入实体字典与世界观结构；`_build_macro_hub_section` 注入宏观地理骨架（顶层视角）。
- `fact_validator.py`（2070）：**抽取后校验**——中文地名形态过滤（专名+通名结构）、人物泛称过滤、数字前缀名修正（"愣子"→"二愣子"）、别名合并（A 把 B 列为别名则 B 并入 A）、同名地点消歧（夹道→某地·夹道）、空名/坏实体剔除。
- `name_resolver.py`：抽取期命名解析（L3 关键文件）。
- `synopsis_generator.py` / `scene_llm_extractor.py`：简介生成 / 场景抽取。

### 3.4 问答 `services/query_service.py`（513 行）★（轻量 Query 层参考）

无向量检索的**规则路由问答**：
- `_extract_entities_from_question`：从问题中做最长匹配找已知实体名（`all_entities` 全集 + alias_map 映射回 canonical）。
- `_build_entity_context`：**以实体为中心组织上下文**——遍历全部章节事实，筛出含这些实体的章节块（人物/关系/地点/物品/组织/事件/概念分块，带 `[第N章]` 来源标注，4000 字上限）。
- `_build_keyword_context`：关键词匹配事件摘要（2000 字上限）。
- `_RELATION_QUERY_KEYWORDS` + `_detect_relation_filter`：**问题关键词→关系类型路由**（"师徒"→师徒/师生/传授/拜师；"敌人"→敌对/仇敌/对手…）——粗糙但零成本的"Query Router"原型。
- `query_stream`：多路上下文（实体+关键词+档案+历史）合并 → LLM 流式回答 → `_extract_source_chapters` 从回答提取来源章节。

### 3.5 幻觉过滤 `services/hallucination_filter.py` ★（零成本质量保障）

**幻觉孤岛过滤**：抽取出的实体名如果在**原文全文字符串中从未出现**（本名与别名都查不到）→ 剔除。纯字符串匹配、零 LLM 调用、全文拼接缓存 + 结果缓存。解决小模型预训练知识泄漏（把别的书的角色写进来）——**2000 章跑批的质量保底，可直接照搬**。

### 3.6 Token 预算自动缩放 `infra/context_budget.py`（197 行）★★（成本控制核心）

- 所有 LLM 参数由**模型上下文窗口**线性插值派生（8K→保守本地值，128K→宽松云端值）：章节截断长度 8000→50000、上下文摘要上限 6000→18000、num_ctx、超时等。
- 本地 Ollama 模型上限 16K（防 KV cache 爆炸）。
- `detect_context_window_ollama`：启动时探测模型 context_length。
- **对用户方案的意义**：不同模型（本地/云端）自动适配预算，是"控制 Token 成本"的工程模板。

### 3.7 关系归一化 `services/relation_utils.py`

`_RELATION_TYPE_NORM`（70+ 条目）：LLM 自由生成的关系类型变体 → canonical（先精确后子串）；`classify_relation_category` 分 6 类（family/intimate/hierarchical/social/hostile/other）。图边聚合用 Counter 频次而非"最新章获胜"。

### 3.8 聚合与可视化 `services/`（职责记录）

- `entity_aggregator.py`（764）：从 ChapterFacts **动态聚合** PersonProfile/LocationProfile（不落独立表）——关系按阶段（RelationStage）收集多证据、去重、归一化、分类。
- `visualization_service.py`（2127）：关系图数据（边频次聚合、类别计数）、地图、时间线数据（6 源事件聚合 + 噪声过滤）。
- `world_structure_agent.py`（3053）+ `geo_skills/`：地理层级（父级投票 + 后缀等级 + 环检测 + Edmonds 最大权树优化 + 快照回滚）。
- `conflict_detector.py`（590）：跨章父级分歧检测。
- `name_authority.py`（498）：命名决策单一事实来源（canonical 选择、泛称过滤、别名安全级别）。
- `profile_quality_checker.py`：关系突变/自引用/参与者修复 + LLM 聚合审查。
- `cost_service.py`：成本统计；`series_bible_service.py`：设定集导出。
- `geo_resolver.py`（1804）：真实世界坐标匹配（GeoNames，检测 realistic/mixed/fantasy 场景）。
- `scene_extractor.py` / `scene_transition_analyzer.py`：场景/剧本面板。

### 3.9 其他（职责记录）

- `db/`：SQLite 15 表（novels/chapters/chapter_facts/entity_dictionary/conversations/messages/user_state/analysis_tasks/map_layouts/map_user_overrides/world_structures/layer_layouts/world_structure_overrides/benchmark_records/bookmarks）+ ChromaDB（章节嵌入 + 实体嵌入）。
- `api/routes/`：22 个路由（analysis/chapters/chat/conflicts/encyclopedia/entities/entity_overrides/export_import/factions/map/novels/prescan/scenes/series_bible/settings/timeline/usage/world_structure…）；`websocket/`：分析进度 + 聊天流。
- `infra/`：llm_client（Ollama + OpenAI 兼容 10 家 + Anthropic 双格式）、secret_store、config。
- `utils/`：chapter_splitter（1363，50+ 格式）、chapter_classifier、text_processor、eval_dashboard（评估仪表盘 + 黄金标准消融）。
- `scripts/synthesize_novel.py`：合成小说生成（测试用）。

---

## 4. 关键机制深挖

1. **三级命名管线**（NameResolver → AliasResolver → NameAuthority）：抽取期解析 + 聚合期 Union-Find 合并 + 单一权威决策源（canonical/别名安全级别/泛称过滤全在 name_authority 一处），配合 L3 变更分级与回归测试（golden standard 5941 节点）。
2. **不安全别名黑名单**：数百条亲属/泛称/头衔/指代词直接封禁进入别名图——这是中文小说"同名不同人/一人多名"防误合并的核心武器。
3. **零成本幻觉过滤**：名字 grounding 检查（原文全文子串匹配）。
4. **关系变化显式化**：RelationshipFact.is_new/previous_type + org_events.action → "关系/势力变迁"可直接查询。
5. **Token 预算随模型缩放**：一套代码适配 8K~128K 模型，自动派生所有截断/超时参数。
6. **动态聚合**：档案不落表，查询时从章节事实实时聚合（减少存储一致性负担，代价是查询慢）。
7. **分析韧性**：错误分级（timeout/parse_error/content_policy/http_error/unknown）、任务恢复、失败章节重试、WebSocket 实时进度 + ETA。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | AI-Reader-V2 对应物 | 结论 |
|---|---|---|
| 原文库 / Evidence | SQLite chapters 表全文 + ChromaDB 章节嵌入 | ✅ 现成 |
| 章节 Chunk | chapter_splitter（50+ 格式）+ 长章分段 | ✅ 现成（最成熟） |
| Map：Chapter Memory | ChapterFact（含 new_aliases/关系变化/组织事件/概念/事件） | ✅ **字段级参考**，比 graph-every-novel 更简但势力事件更明确 |
| 人物/关系/势力 | CharacterFact/RelationshipFact/OrgEventFact + entity_profiles | ✅ 现成 |
| 指代消解 | alias_resolver（Union-Find + 不安全别名黑名单 + overrides） | ✅✅ **全项目最可复用** |
| 跨章累计 | entity_aggregator 动态聚合 + RelationStage 多证据 | ✅ 现成 |
| 压缩层主检索源 | 章节事实（ChromaDB 向量 + 规则筛选） | ✅ 部分（有向量，无图） |
| Query Router | `_RELATION_QUERY_KEYWORDS` 关系路由 + 实体/关键词分流 | ⚠️ 粗糙原型，可升级为真正的 router |
| 回查原文 | 答案来源章节提取 + 场景索引定位原文 | ✅ 现成 |
| 伏笔追踪 | **无显式伏笔状态机**（relationships.is_new 近似） | ⚠️ 需自建 |
| 势力变迁 | org_events.action 枚举（加入/离开/晋升/阵亡/叛出/逐出） | ✅ 现成字段 |
| 质量保障 | FactValidator + hallucination_filter + golden standard + 模型基准 | ✅✅ 现成 |

---

## 6. 可复用模块清单

**直接复用（AGPL 注意：复用代码需开源衍生品；若只借鉴设计则不触发）**
- `alias_resolver` 全量设计 + 不安全别名黑名单（中文网文指代消解最优解）。
- `hallucination_filter`（零成本 grounding 检查）。
- `context_budget`（token 预算自动缩放）。
- `chapter_fact.py` 模型字段（Chapter Memory schema 底稿）+ `org_events` 势力变迁枚举。
- `relation_utils` 关系归一化（70+ 映射 + 6 分类）。
- `chapter_splitter` 章节切分。
- `query_service` 的实体中心上下文构建 + 来源标注 + 关系关键词路由。
- `_RELATION_QUERY_KEYWORDS` 关系问题路由表。

**需改造**
- 抽取提示词按你的小说定制；VoT/分段策略继承。
- 聚合为图（Graphiti）或保持 SQLite+ChromaDB 双库。

**仅参考**
- 前端（React 可视化）、地图/地理（geo_skills 体量大且与 RAG 无关）、导出渲染器。

---

## 7. 许可证与工程坑

- **AGPL-3.0** ⚠️：直接复制代码到闭源/商用项目有传染风险；**建议作为"设计参考 + 概念照搬"而非代码复用**，或与作者洽商业许可。这是它和 graph-every-novel（MIT）、Graphiti（Apache-2.0）的重要差别。
- 体量大（6 万行后端 + 前端），通读/复用成本高；文档 CLAUDE.md 极详尽（架构、L1/L2/L3 分级、单一事实来源原则、DB schema、Env 变量）。
- 依赖 ChromaDB + BAAI/bge-base-zh-v1.5（中文嵌入）+ jieba；LLM 支持 Ollama 与云端 10 家。
- 测试 498+，含 golden standard 回归（5941 节点 5 本名著）——工程质量参考价值高。
- 质量迭代仍在进行（README 自述"未达可实用阶段"），版本变化快。

---

## 8. 成本与规模实测数据

- 官方基准：`POST /model-benchmark` 用 golden standard 打分（entity_recall×0.6 + relation_recall×0.4），记录 benchmark_records。
- 成本特征：单章 1 次主抽取调用（长章 2~3 段）+ 可选预扫描（全书 1 次）+ 后处理校验（0 LLM）。**比 graph-every-novel 的每章 3~5 次调用更省**——因为校验/合并大量用规则而非 LLM。
- 对用户 2000 章：若直接复用其管线（预扫描 + 单章抽取 + 校验），成本 ≈ graph-every-novel 实测的 1~1.5 倍（它做了预扫描和更宽 schema），即 400 万字约 ¥30~120、700 万字约 ¥50~210（DeepSeek 级）。
- Token 预算自适应 + 失败重试 + 降级路径齐全，适合长跑。

---

## 9. 通读结论

1. **AI-Reader-V2 是"中文小说 Map 层 + 轻量 Query 层"质量最高的参考**，别名解析、幻觉过滤、Token 预算、关系归一化四项可无痛搬到用户方案。
2. **它的 Query 层证明了"规则路由 + 结构化事实拼接"在没有图/向量库时也能回答大多数人物/关系/势力问题**——可作为用户方案的轻量基线，再叠加 Graphiti/Mnemis 的分层检索做增强对比。
3. **最大短板**：无图数据库、无层级记忆（档案动态聚合但无卷/书级摘要）、无伏笔状态机；AGPL 许可证限制直接代码复用。
