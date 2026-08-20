# 10 · NovelGraph（CSandbatch/novelgraph）代码通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/CSandbatch/novelgraph ｜ 许可证：MIT
> 定位：**本地优先的"小说创作生产环境"**——通过可审计的工作流 DAG 完成规划、起草、审计、完本（"The manuscript is the surface. The story is the system beneath it."）。**方向与用户方案相反（创作侧 vs 已完结书问答侧）**，但它的"正典债务（obligations/closure）模型"与"知识分层权限（visibility/capability）"是用户方案伏笔状态机与防剧透设计的**字段级最佳蓝本**。
> 代码规模：pnpm monorepo（core/cli/studio/site），核心 `packages/core/src/studio/` 14 个 TS 文件约 1500 行 + 单文件 SQLite 事务存储（`store.ts` 389 行内嵌 40+ 表 DDL）；npm 包 `@actalk/novelgraph` v0.5.0，Alpha。

---

## 1. 项目概况与定位

NovelGraph 是给**单个作者**的本地创作工具：Discovery Room（对话式立项）→ Story Charter（大纲宪章，审批后才解锁写作）→ 生产 DAG（研究→大纲→草稿→确定性校验→剧情图提案→连续性/谜题审计→读者面板→修订→再审计→审批→完本）。强调：

- **"创作断言是提案，只有作者审批服务能把它们提升为正典"**——系统绝不静默改正典。
- **完本 = 清算书的债务**：记录 setup/clue/promise/obligation/deadline，硬性义务未解决则阻止发布；可记录延迟或带理由豁免（waiver），豁免理由留在完本报告中。
- 英文向、本地优先、单作者、SQLite 为权威存储、无云同步、无账号系统。

**与用户方案的关系**：用户方案的"伏笔状态机（open/progress/payoff/possible）"与 NovelGraph 的"obligations（open/progressing/resolved/deferred/waived + hard + target_chapter + dependencies + waiver_reason）"是同一件事的两个实现——后者有完整持久化与完本闸门；它的 Fair-Play 规则包与"reader-visible / solution-authorized / author-only"三级可见性，正是"防剧透问答"的权限模型。

---

## 2. 整体架构与数据流

```
Discovery Room（对话）→ observations/scratchpads → 候选推力（core/stretch/wild）
   → Story Charter（审批）→ 解锁生产
生产 = 持久化 DAG（workflow.ts）
   research → outline → draft → deterministic validation
   → story-graph proposal → continuity/mystery audit → reader panel
   → revision → re-audit → approval → closure（closureReport 闸门）
每节点：类型输入输出 / 能力（capability）/ 产物可见性 / 预算 / 重试 / 依赖
```

关键设计：**图节点只声明状态，agent 执行由调用方提供**（`WorkflowHarness` 不调 LLM）——它是"工作流状态机"而非 agent 框架；所有变更走 `events` 审计日志 + `approvals` 审批 + `revisions` 版本链。

---

## 3. 模块逐文件代码通读（核心全读）

### 3.1 存储 `studio/store.ts`（389 行）★★（SQLite 单文件事务中枢）

- `node:sqlite` `DatabaseSync` + WAL + 5s busy timeout + **迁移版本化（schema_migrations 1/2/3）+ 迁移前自动备份**。
- 40+ 表（迁移内联 DDL）：
  - 结构：series/books/chapters/scenes/graph_entities/graph_edges
  - **债务**：obligations（kind: setup/clue/promise/dependency/commitment/reveal；hard 位；status: open/progressing/resolved/deferred/waived；target_chapter；owner_character_id；waiver_reason）+ obligation_dependencies
  - **审批/审计**：approvals（kind: canon-retcon/outline-change/character-major-change/research-admission/gate-waiver）、events（before/after JSON + rationale + approval_id + source_chapter_id）、revisions（版本链 + 恢复）
  - **读者面板**：reader_personas（role/weight/blocks_approval/prompt）+ reader_feedback（severity: info/warning/critical + evidence）
  - **工作流**：jobs（idempotency_key 唯一 + budget_cents/used_cents + cancellation）+ job_nodes（depends_on/capability/artifact_visibility）+ job_events + workflow_artifacts
  - **知识**：knowledge_bases（scope: literary/series/book/run）+ knowledge_claims（subject/predicate/value + provenance + status + approval_id）+ knowledge_links + knowledge_sources/chunks（FTS5 bm25 或 LIKE 兜底）+ research_items（url/excerpt/claim/status/citation）
  - **发现**：discovery_sessions/turns/observations/scratchpad_entries/story_thrust_candidates/story_charters/context_dossiers
  - **Fair-Play**（迁移 v2）：mystery_policies/solutions/suspects/evidence/timeline/access/knowledge/hypotheses/deductions + validation_runs/rule_findings/rule_waivers + solution_access_events
- `closureReport(bookId)`（342–371）★★：产出 `{publishable, findings[]}`——`publishable = 无 critical`。检查项：OPEN_HARD_OBLIGATION（硬义务未结）、UNJUSTIFIED_WAIVER（豁免无理由）、READER_PANEL_BLOCK（阻断型读者反馈未解决）、MYSTERY_SOLUTION_UNLOCKED、MYSTERY_NO_PAYOFF_EVIDENCE（无已回收且有 payoff 章的线索）、MYSTERY_AUDIT_REQUIRED（策略变更后未重审计）、rule_findings 未决项、_WAIVED 警告——**这就是"完本 = 债务清算"的落地实现**。

### 3.2 工作流 `studio/workflow.ts`（124 行）★

- `WorkflowHarness`：start（要求已审批 Story Charter + 幂等 job）→ readyNodes（按能力过滤 + 依赖完成）→ beginNode/completeNode（能力校验 + 扣费 + 状态机）→ failNode（retryable 回 pending / 否则 blocked）→ resume（blocked/failed 全重置重跑）→ requestCancellation。
- `canRead(visibility, capabilities)`：reader-visible → story:read 或 reader-view:read 或 solution:read；solution-authorized → 仅 solution:read；author-only → 无 agent 可读——**三级可见性的读取控制**。
- 节点能力枚举（domain.ts AgentCapability）：research/write/canon/publish + story:read/reader-view:read/solution:read/solution:write/research:web/draft:write/canon:propose/approval:request/publish:export/discovery:*/scratchpad:*/literary:read/series:read/book:read/charter:propose。

### 3.3 谜题规格 `studio/mystery-spec.ts`（184 行）★★（字段级蓝本价值最高）

- 规则包版本化：`FAIR_PLAY_RULE_PACK = "fair-play-detective-2026@1"`。
- `ArtifactVisibilitySchema`：**reader-visible / solution-authorized / author-only**（防剧透三级）。
- `MysteryEvidenceSchema`：kind（physical/testimony/digital/forensic/document/chronology/behavioral/spatial/linguistic/social）、reliability（established/highly-probable/plausible/unresolved）、visibility、**firstAppearanceChapter/revealChapter**、required、redHerring、corroborates/contradicts；digital/forensic 扩展带 provenance/chain-of-custody/possibleManipulation/**oracleSource**/establishesGuiltAlone/narratorFalseFact。
- `TimelineEventSchema`：earliest/latest + timeKind（fixed/estimated/reported/inferred/**falsified**）+ reliability + conflicts。
- `KnowledgeRecordSchema`：characterId + fact + state（**knows/believes/conceals/misunderstands/learns**）+ chapter + visibility——**角色知识边界**。
- `HypothesisSchema`：kind（initial/alternative/false/true）+ supporting/falsifying evidence + equallySupported。
- `DeductionSchema`：conclusion + evidenceIds + sequence + visibility。
- **22 条规则目录**（FAIR_PLAY_RULE_CATALOG）★★：FP-DISCLOSURE-001（关键证据必须 reader-visible）、FP-DISCLOSURE-002（关键证据需有首次出场章）、**FP-DISCLOSURE-003（证据必须先于揭示出现）**、FP-DEDUCTION-001（推导必须引用已有证据）、CAUSAL-SOLUTION-001/002（密封谜底 + 锁定）、ACCESS-001（责任需有文档化访问）、TIME-RANGE-001（时间线区间必须可能）、DIGITAL/FORENSIC-*（数字/法证证据需来源与限制、防 oracle）、ALT-SOLUTION-001（测试替代解）、FP-RED-HERRING-001（误导需文档化反转）、CHARACTER-LOGIC-001、RETROSPECTIVE-001（揭晓后前文仍须自洽）、STEREOTYPE-001、ORACLE-001、READER-TRUST-001（叙事不得隐藏关键事实）——severity: blocker/major/moderate/minor/advisory；permittedActions: revise/review/dismiss/waive/change-mode。
- 模式策略 `MODE_POLICIES`：strict-golden-age（必须谋杀 + 单人主犯 + 禁豁免）等四种模式约束。

### 3.4 谜题引擎 `studio/mystery-engine.ts`（227 行）★

- 密封谜底：`mystery_solutions`（locked + revision），已锁定修改需 **canon-retcon 审批 + 触发重审计**（`audit_generation` 递增）。
- 策略变更（mode 切换）需审批 + 全部审计失效；`getSolution` 做 solution:read 能力校验并记录 solution_access_events。
- `ReaderProjection`：policy/suspects/evidence/timeline/knowledge 的**能力过滤投影**——读者视角永远看不到 solution-authorized 内容。

### 3.5 发现室 `studio/discovery.ts`（143 行）+ 知识库 `knowledge.ts`（37 行）

- DiscoveryEngine：会话/轮次/观察（provenance + confidence）/scratchpad（agent_role: sol-orchestrator/terra-specialist/luna-worker）/story_thrust_candidates/Story Charter 审批。
- KnowledgeBase：`ensureKnowledgeBases` 建 literary/series/book 三层知识库 + `knowledge_claims`（subject/predicate/value + provenance: author-stated/agent-inferred/literary-guidance/agent-proposed + status: working/unresolved/proposed/rejected/approved/superseded）；检索用 **FTS5 bm25**（缺失则 LIKE 兜底）。

### 3.6 领域模型 `domain.ts`（123 行）

- 全部 zod schema：PublicationTarget/ReaderPathway/ObligationKind（setup/clue/promise/dependency/commitment/reveal）/ApprovalKind/JobStatus/KnowledgeScope/ClaimProvenance/AgentRole/StoryThrustKind。

---

## 4. 关键机制深挖

1. **正典债务模型（obligations + closureReport）**：伏笔/承诺/依赖作为"有状态、有硬/软属性、有目标章、有依赖、有豁免"的记录，完本时强制清算——用户方案"伏笔状态机持久化"的完整字段级答案。
2. **三级可见性 + 能力过滤投影**：reader-visible / solution-authorized / author-only 贯穿证据、时间线、知识记录与工作流产物，读取受 capability 门控——**防剧透问答的权限模型**（答案只带读者已见证据）。
3. **正典守卫**：任何"创作断言"只是 proposal，进正典需审批（canon-retcon/outline-change/gate-waiver）；密封谜底锁定后修改需 retcon 审批并强制重审计。
4. **规则包版本化 + 审计闸门**：22 条规则按 suite/severity/status 记录，策略变更递增 audit_generation 使旧审计失效——质量门可演进、可豁免、可追溯。
5. **审计完备**：所有变更写 events（before/after + rationale + 来源章）+ revisions 版本链 + approvals 决策链——与"LLM 调用审计日志"同哲学。
6. **本地事务纪律**：BEGIN IMMEDIATE 原子操作、迁移前自动备份、WAL——单文件 SQLite 当权威库的工程样板。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | NovelGraph 对应物 | 结论 |
|---|---|---|
| **伏笔状态机（四态持久化）** | **obligations**（kind: setup/clue/promise/reveal + status: open/progressing/resolved/deferred/waived + hard + target_chapter + dependencies + waiver_reason）+ `closureReport` 清算 | ✅✅ **字段级直接照抄**（鉴来四态 → 该表结构） |
| **防剧透问答** | 三级可见性（reader-visible/solution-authorized/author-only）+ capability 过滤投影 + `mystery_access`（角色访问矩阵） | ✅✅ **权限模型现成** |
| 伏笔回收闭环 | closureReport 的 MYSTERY_NO_PAYOFF_EVIDENCE（无已回收线索则不可发布） | ✅ 思路（答案侧变为"回收章必须可回查"） |
| 跨章细节考据 | mystery_evidence（firstAppearanceChapter/revealChapter/redHerring/corroborates/contradicts）+ timeline（timeKind 含 falsified） | ✅ 字段参考 |
| 势力/阵营 | mystery_evidence 的 social 类 + 无 faction 表 | ⚠️ 弱（创作侧不需要势力图谱） |
| 人物关系/图谱 | graph_entities/graph_edges（无类型化关系语义） | ⚠️ 弱，不如 LoreGraph/graph-every-novel |
| 分层记忆（Arc/Book） | knowledge scopes（literary/series/book/run）是**权限分层**非内容摘要分层 | ⚠️ 语义不同，不可混用 |
| 质量保障 | 规则包 + validation_runs + rule_findings + waivers | ✅ 质量门模板（rule → severity → 可豁免） |
| 增量/修正 | revisions 版本链 + approvals（retcon 审批）+ events 审计 | ✅ 修正覆盖层的工程样板 |
| 检索/问答 | **无 RAG**（knowledge FTS 只检索外部资料） | ❌ 与方案无关 |
| 抽取/图谱 | **无 LLM 抽取管线**（agent 执行由调用方提供） | ❌ 与方案无关 |

---

## 6. 可复用模块清单

**直接复用（MIT，字段/结构照抄）**
- `obligations` 表 + `closureReport` 逻辑：伏笔状态机的持久化 schema 与"回收闸门"判定。
- `mystery-spec.ts`：evidence（firstAppearanceChapter/revealChapter/redHerring/corroborates/contradicts）、timeline（timeKind 含 falsified）、knowledge records（knows/believes/conceals/learns）、deduction（conclusion + evidenceIds）——跨章细节考据的字段全集。
- 三级可见性 + capability 过滤投影：防剧透问答的读取控制。
- 规则包目录（22 条）+ validation_runs/rule_findings/rule_waivers：质量门的规则化模板（用户方案的"卷末盘点"可规则化）。
- `events`（before/after + rationale）+ `approvals` + `revisions`：修正覆盖层与审计日志。
- 迁移版本化 + 迁移前备份 + WAL 单文件 SQLite：本地存储工程样板。

**需改造**
- 方向倒转：创作侧（写书）→ 问答侧（读已完结书）；obligations 由"作者登记"变为"Map 抽取自动登记"。
- 英文向 → 中文网文；无 LLM 抽取 → 接入 LoreGraph/graph-every-novel 的抽取。
- 与图谱底座对接：obligations 的 owner_character_id 关联到实体 canonical id。

**仅参考**
- Discovery Room（立项对话）、reader panel（读者反馈）、prose-patterns（文风审计）、workflow DAG（agent 工作流，方案不需要）。

---

## 7. 许可证与工程坑

- **MIT** ✅ 完全自由。
- **Alpha**：v0.5.0，README 明示部分生产节点仍用兼容适配器、系列图谱视图开发中。
- **依赖**：Node.js 22.13+（`node:sqlite` 需新运行时）、pnpm workspace、zod。
- **边界（README 自述）**：English-only、local-first、single-author、无云同步无协作、SQLite 为权威存储；"完本闸门建立的是内部核算，不是文学价值"。
- 无 LLM 成本数据：agent 执行由调用方提供，本仓库不含任何模型调用（成本工程在本项目不可评估）。

---

## 8. 成本与规模实测数据

- 无（不调 LLM）。对用户方案的直接价值不在成本而在**数据模型**：obligations/evidence/visibility 三组表直接决定"伏笔表与防剧透"怎么建，省去自研 schema 的试错。

---

## 9. 通读结论

1. **NovelGraph 是"伏笔状态机 + 防剧透"的字段级最佳蓝本**：obligations 表 + closureReport + 三级可见性 + 规则包，比鉴来助手（只判定不写回）完整一个量级；MIT 可直接照抄 schema。
2. **它整体不可用**（创作侧、无抽取、无 RAG、英文向），定位为"概念与 schema 参考"，与 LoreGraph（抽取/证据）、Graphiti（图/时序）互补——**LoreGraph 补"从书里挖出事实"，NovelGraph 补"怎么记录伏笔与控剧透"，Graphiti 补"存图与按时间查询"**。
3. **对定版方案的具体贡献**：① 伏笔表定稿可直接用 obligations 的字段集（含 hard/dependencies/waiver）；② 防剧透问答引入 reader-visible/solution-authorized 可见性 + 能力过滤；③ 质量门可加"卷末规则审计"（rule finding + waiver）。
