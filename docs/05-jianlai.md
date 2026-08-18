# 05 · 鉴来助手 novel-copilot-backend（haoyunh415-create）代码通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/haoyunh415-create/novel-copilot-backend ｜ 许可证：MIT
> 定位：**生产级"小说 AI 伏笔雷达"SaaS 后端**——Chrome 扩展 + 油猴脚本前端，FastAPI + DeepSeek API + SQLite。功能：无剧透前情提要、伏笔雷达（跨章追踪）、人物关系图、无剧透问答。已上架 Chrome 商店，支持 25+ 小说网站。
> 代码规模：`main.py` 3070 行（单体路由+存储+商业逻辑）+ `services/ai_service.py` 37KB（AI 核心）+ Chrome 扩展（content.js 107KB）+ 油猴三版本。**9 个项目中唯一的纯 Prompt 工程架构（无 RAG/无向量库/无图库）——是用户方案的"轻量对照原型"。**

---

## 1. 项目概况与定位

鉴来助手解决的是"追更读者"场景：读者在某小说网站读到第 N 章，插件把当前章发给后端 → AI 生成摘要/人物/伏笔/关系图 → 无剧透问答（只能基于已读章节）。核心卖点：

- **无剧透**：所有分析只基于已读章节，prompt 明确禁止引用后文/百科/评论/模型记忆。
- **伏笔雷达**：线索带可信度评分，跨章状态追踪（开放中/推进中/已回收）。
- **渐进式分析**：摘要 3-5 秒先出，人物与伏笔 8-12 秒后补——体验与成本平衡。
- **缓存与去重**：同文本同参数结果全局缓存（text_hash），多用户共享，几乎零重复成本。

**与用户方案的关系**：它证明了"逐章抽取 + SQLite 记忆 + 纯 prompt 问答"的最小架构能支撑真实产品；它的伏笔状态机是用户方案"伏笔能力"的现成参考；它的 30 章记忆窗口上限恰好反证了 2000 章必须走分层检索。

---

## 2. 整体架构与数据流

```
小说章节（25+ 平台，前端脚本抓取）
   │ POST /api/analyze（或 progressive 渐进式 / stream 流式）
   ▼
main.py：限流 → 书籍匹配/创建（书名/URL 前缀）→ text_hash 缓存查
  → 额度扣减（签到/积分）→ 8000 字段落截断
   ▼
ai_service.py：
  ├─ analyze_text / analyze_text_stream（单次全量：摘要+人物+伏笔+术语+关系图 JSON）
  ├─ analyze_summary_only（~5s）→ analyze_details_only（~8-12s）〔渐进式〕
  └─ check_foreshadowing_payoff（新章 vs 历史伏笔 → echo/progress/payoff/possible）
   ▼
analyses 表（每章 result_json 落库 = 章节记忆）+ analysis_cache 全局缓存
   ▼
/api/ask：_load_memories（最近 30 章）→ answer_from_memory（直接灌上下文）
/api/review：最近 N 章 → 追更回顾
/api/report/full：全书复盘（Map-Reduce：≤60章单次 / >60章分批→阶段总结→汇总）
/api/foreshadowing/check：伏笔回收检测
```

一句话：**每章一次 LLM 调用产出结构化记忆落 SQLite；问答/回顾/复盘都从记忆文本拼 prompt，不做任何检索。**

---

## 3. 模块逐文件代码通读（全部读完）

### 3.1 `services/ai_service.py`（37KB，上轮已全文通读）★★

| 函数 | 作用 | 对方案的启示 |
|---|---|---|
| `analyze_text` | 单章全量分析（严格 JSON：summary/characters/foreshadowing/terms/graph） | ≈ 最简 Chapter Memory schema（5 字段） |
| `analyze_summary_only` / `analyze_details_only` | 渐进式拆分（摘要快、详情慢） | 成本/体验分阶段思路 |
| `analyze_text_stream` | SSE 流式（connecting/reading/parsing 三阶段） | 长任务体验 |
| `answer_from_memory` | 记忆问答：最近 30 章摘要+人物+伏笔+术语灌 prompt，要求引用章节标题、拒绝编造 | **无检索问答的极限参考（30 章窗口）** |
| `review_recent_chapters` | 追更回顾（主线/人物动向/伏笔/阅读上下文/提示） | 轻量 Reduce |
| `check_foreshadowing_payoff` | **伏笔回收检测**：历史伏笔 vs 当前章 → match_type=echo(重复)/progress(推进)/payoff(回收)/possible(不确定) + reader_message | **伏笔状态机的判定核心** |
| `suggest_questions` | 基于已读生成"该问但没想到的问题" | 交互增强 |
| `generate_full_report` | **全书复盘 Map-Reduce**：≤60 章单次；>60 章按 60 章分批 → `_generate_chunk_summary`（阶段总结）→ `_do_final_report`（汇总报告，六个板块：主线/节点/人物/伏笔/世界观/建议） | **用户方案 Reduce 层的直接蓝本**（比 Graphiti Saga 更贴近"书评式报告"） |
| `_extract_json` / `_fix_json` / `_fix_json_aggressive` / `_strip_ai_chatter` | 多策略 JSON 容错（去 AI 废话/代码块/尾逗号/未闭合串/控制字符） | 2000 次 LLM 调用的刚需 |
| `_call_ai` | 统一调用：温度 0.2、超时 35s、重试 2 次（429/5xx 指数退避） | 稳健调用模板 |
| `SUMMARY_RULES` | 简洁 160-240 / 标准 350-550 / 详细 700-1000 字三档摘要规则 | 摘要粒度分级 |

### 3.2 `main.py`（3070 行）★（本轮补读）

**数据库（10 表）**：
- `books`（每本书，UNIQUE(username,title)）、`analyses`（**每章分析记忆**：chapter_title/chapter_index/text_hash/result_json，UNIQUE(username,text_hash,detail_level,spoiler_free)）、`analysis_cache`（**全局结果缓存**，跨用户共享）、`users/orders/usage_logs`（积分商业体系）、`email_codes/email_logs/refresh_tokens/kv_store`。

**关键端点逻辑**：
- `POST /api/analyze`：限流（类别+2s 节流）→ 书籍匹配/创建（书名或 URL 前缀）→ 缓存命中直接返回（`cached:true`）→ 额度扣减 + 签到 → 8000 字段落截断（提示截断百分比）→ `analyze_text` → 落库。**生产级完整链路**。
- `POST /api/analyze/progressive`：渐进式（先摘要后详情，两段缓存）。
- `_load_memories(limit=30)`：按 created_at 倒序取最近 30 章 → 转 memories 列表（summary/characters/foreshadowing/terms 精简字段）+ 章节范围描述。
- `POST /api/ask`：`_do_ask` 用最近 30 章记忆 → `answer_from_memory`。
- `POST /api/foreshadowing/check`：**伏笔回收检测端点**——拿最新分析结果 + 历史伏笔 → `check_foreshadowing_payoff`。
- `GET /api/books/{id}/foreshadowing`：**跨章伏笔列表**——遍历全部章节分析的 foreshadowing 字段，组装 clue 列表（带 id/confidence/related_entities/status="open"），按置信度排序。**status 目前是展示用默认 open，真正的状态推进靠 check 端点**。
- `POST /api/report/full`：全书复盘（积分 20，>60 章走分批 Map-Reduce）。
- 管理端：/api/admin/*（用户/订单/额度）、track 统计、邮件日志。
- 防滥用：签到 +8 次/天、购买额度、限流。

### 3.3 前端 `JianLai_Helper/`（职责记录）

- `content.js`（107KB）：**25+ 小说网站内容抓取与注入**（章节文本提取、按钮、弹窗、图谱渲染），前端核心。
- `background.js` / `popup.*` / `manifest.json`（MV3）/ `vis-network.min.js`（关系图渲染库）。
- `userscript/`：油猴三版本（标准/GF/手机 Alook）。
- `docs/`：Chrome 商店上架清单、运营文案（非技术）。

---

## 4. 关键机制深挖

1. **伏笔状态机**：抽取时每章标 0-3 条线索（clue/reason/confidence）；`check_foreshadowing_payoff` 用 LLM 把当前章与历史伏笔比对，判定 echo/progress/payoff/possible 四态——**跨章伏笔追踪的最廉价实现（无需向量相似度，靠 LLM 语义判定）**。局限：仅比对最近 30 条、状态不写回持久层（status 字段未真正更新），2000 章场景需改成"伏笔表 + 状态写回 + 定期盘点"。
2. **无剧透约束**：prompt 硬规则（不引用后文/百科/评论/模型记忆）+ spoiler_free 开关——**用户方案"从摘要追溯到原文、绝不剧透"的同类约束**。
3. **纯记忆问答的窗口上限**：`answer_from_memory` 只灌最近 30 章（`memories[-30:]` + 每章截断 characters/foreshadowing/terms 各 8 条）——token 可控，但"跨 100 章的伏笔"必然回答不了 → 反证分层检索必要性。
4. **缓存经济**：text_hash + detail_level + spoiler_free 三维唯一；全局缓存跨用户共享——**2000 章批量处理时同文本去重能省大量重复调用**。
5. **Map-Reduce 复盘**：60 章一批 → 阶段总结 → 最终报告（六板块含伏笔追踪：埋设章节 + 是否回收）——完整的分层汇总实现，比 Graphiti Saga 摘要更贴近网文用户诉求。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | 鉴来助手对应物 | 结论 |
|---|---|---|
| 章节 Chunk | 前端抓取整章 + 8000 字截断 | ⚠️ 截断会丢细节（2000 章场景建议分段抽取） |
| Map：Chapter Memory | analyses 表 result_json（summary/characters/foreshadowing/terms/graph） | ✅ **最简 schema**，字段可直接扩展 |
| 人物/关系/势力/事件/伏笔 | characters/graph（人物+关系）、terms、foreshadowing（伏笔） | ⚠️ 无势力/事件显式字段（graph 只有人物边） |
| **伏笔追踪** | foreshadowing 抽取 + check_foreshadowing_payoff（echo/progress/payoff）+ 跨章列表 | ✅✅ **唯一直接实现"伏笔能力"的项目** |
| 跨章累计 | analyses 表按书聚合 + _load_memories | ✅ 简单累计（无状态合并/去重） |
| 压缩层主检索源 | 30 章记忆直接灌 prompt | ⚠️ 窗口上限 30 章——2000 章需分层 |
| Query Router | 无（单一记忆问答） | ⚠️ 需自建 |
| Reduce 分层汇总 | generate_full_report（60 章分批→阶段→汇总） | ✅ 现成蓝本 |
| 回查原文 Evidence | 无（记忆只有摘要，无原文引用） | ⚠️ 需补（用户方案已有） |
| 质量保障 | JSON 容错、重试、降级、缓存 | ✅ 现成 |

---

## 6. 可复用模块清单

**直接复用（MIT）**
- `check_foreshadowing_payoff` 的伏笔状态判定 prompt 与调用结构（四态 match_type）。
- `generate_full_report` 的 Map-Reduce 复盘（60 章批 → 阶段总结 → 汇总六板块）——用户方案 Reduce 层的实现模板。
- `_extract_json/_fix_json/_strip_ai_chatter` 全套 JSON 容错（2000 次调用刚需）。
- `_load_memories` 的章节记忆组织格式（chapter_title+summary+characters+foreshadowing+terms 精简结构）。
- text_hash 缓存模式（三维唯一 + 全局共享）。
- 无剧透 prompt 约束模板 + SUMMARY_RULES 三档摘要。

**需改造**
- 伏笔状态持久化：从"每次比对最近 30 条 + 不写回"升级为"伏笔表（open/progress/payoff 状态 + 埋设章 + 回收章）+ 每章盘点写回"。
- 8000 字截断 → 分段抽取（大上下文模型或分块合并）。
- 人物关系累积：目前每章独立 graph，无跨章合并（需接 graph-every-novel/AI-Reader-V2 的累计逻辑）。

**仅参考**
- 积分/签到/订单商业体系、邮件、管理后台、前端抓取脚本。

---

## 7. 许可证与工程坑

- **MIT** ✅ 自由使用。
- 单体文件（main.py 3070 行）——可读但维护性差；`services/` 只有 3 个文件，逻辑集中。
- 生产依赖：DeepSeek API + SQLite + Nginx；无异步（同步 requests/FastAPI def 端点）——并发高时是瓶颈，但对个人规模足够。
- 伏笔 status 字段目前默认 "open" 不写回——**README/界面展示的状态机比代码实现更完整**，复用时要落地持久化。
- 免费策略：3 次免登录试用 + 注册 10 次 + 每日签到 8 次——成本控制（额度）是产品化关键，可借鉴到用户方案的调用配额设计。
- 无向量/无图：全部逻辑靠 prompt + SQLite，跑不了"跨百章细节检索"类问题（设计边界，非缺陷）。

---

## 8. 成本与规模实测数据

- 单章 1 次主调用（渐进式则 2 次：摘要 + 详情），输出 ~4096 tokens 上限。无剧透 + 8000 字截断 + 缓存 → 单章成本极低（DeepSeek 级别单章 <¥0.01）。
- 对用户 2000 章：仅"逐章分析"层成本 ≈ graph-every-novel 实测的 1/3~1/2（schema 更简、无记忆合并调用）；但它不做跨章累计/检索，所以总方案成本需叠加检索层。
- 问答成本：每次问答应答者自担 30 章记忆 token（约 3-5K），2000 章场景该方案不可行（记忆窗口不够）——再次印证分层必要性。

---

## 9. 通读结论

1. **鉴来助手是"伏笔能力"与"最小成本架构"的参照系**：伏笔抽取+四态判定+跨章列表直接可用；纯 prompt+SQLite 证明了几十万字规模的最低成本形态。
2. **它的 30 章窗口失效点 = 用户方案分层检索的起点**：2000 章必须用 Chapter→Arc→Book 分层记忆替代"最近 N 章直接灌"。
3. **与 graph-every-novel/AI-Reader-V2 互补**：它最简（无累计/无校验/无图），正好作为用户 pilot 阶段的基线实现（先复刻它跑通，再加分层与检索量化增量价值）。
