# 06 · HiGoE（tkw123/HiGOE）代码通读报告

> 通读日期：2026-08 ｜ 仓库：https://github.com/tkw123/HiGOE ｜ 许可证：未见 LICENSE 文件（默认保留所有权利，仅作研究参考）
> 定位：ACL 2026 论文《HiGoE: Hierarchical Graph of Evidence to Enhance Retrieval-Augmented Generation for Long-context Summarization》官方实现。
> 代码规模：10 个 Python 文件 / 3130 行，纯研究原型（论文复现代码，非工程框架）。
> 适用场景：长上下文**摘要**（QMSum 会议纪要 / BookSum 书籍 / GovReport 政府报告 / SQuALITY 故事），其中 BookSum 是书籍数据。

---

## 1. 项目概况与定位

HiGoE 的核心主张：长文档（如整本书）做摘要/问答时，朴素 RAG 召回 chunk 再拼 prompt 效果差（chunk 相互割裂、证据分散）。它构建 **"命题-证据层级图"**：

- 把文档 chunk 抽成**命题（claim/proposition）**——单句可验证事实；
- 命题 ↔ 证据 chunk 建边；
- 社区检测 + 社区摘要节点 → **层级图**；
- 查询时用 **PPR（个性化 PageRank）/ Heat Kernel 扩散**在图上传播，聚合相关命题作为答案证据。

**对用户方案的意义**：它提供了"文档 → 命题级事实图 → 层级摘要 → 图上扩散检索"的完整研究范式。用户方案中的"势力/事件/伏笔"本质都是"跨 chunk 的命题聚合"，HiGoE 的命题化 + 扩散是这一层的算法参考。**价值在思想与算法，不在代码**（研究代码不可直接复用）。

---

## 2. 整体架构与数据流

```
长文档 → TokenTextSplitter 分块
   │
   ▼
命题生成：每 chunk LLM 抽 1 句命题（CLAIM_GENERATE_LOCAL，≤30 词）
   │
   ▼
命题-证据图构建：命题节点 + chunk 节点 + 证据边（chunk→命题）
   │  （LLM judge 质量过滤：1-5 分，阈值以下剔除低质命题）
   ▼
层级化：
   ├─ 社区检测（networkx / 扩散法 detect_communities_diffusion）
   └─ 每社区 LLM 摘要节点 SUMMARY_NODE（summarizes 边连成员）→ 层级图
   │
   ▼
知识合成（knowledge_synthesizer_ppr）：
   ├─ 稠密检索召回种子节点（contriever + faiss）
   ├─ PPR / Heat Kernel 扩散矩阵 → 沿图传播得分
   └─ 聚合 top 命题（+社区摘要）→ LLM 生成答案
   │
   ▼
（可选）GNN 训练：命题图 + PPR 标签训练图网络头（train_lossnew.py，论文贡献点）
```

一句话：**文档 → 命题图 → 社区摘要层级 → PPR 扩散聚合 → 合成答案**。

---

## 3. 模块逐文件代码通读

### 3.1 `graph_construction.py`（740 行）★ 命题-证据图构建

- `CLAIM_GENERATE_LOCAL`：命题生成 prompt——"抽取一条简洁可验证的声明，单句 ≤30 词，排除疑问句，只输出声明"。
- `rag_retrieval` / `mem_retrieval`：稠密检索（contriever 编码 + faiss IP）召回 chunk 作为图构建输入 / 记忆节点检索。
- `record_graph_construction`：把"问题→支撑材料→响应命题"写入 networkx + dgl 图（命题节点、证据边、训练标签），用于后续 GNN 训练。
- `is_valid_question / is_valid_statement`：启发式质量过滤（长度 5-50 词、必须含问号/动词、去重率、错误标记剔除）。
- `llm_judge_claim_quality / llm_judge_question_quality + parse_*_judgment`：**LLM 裁判**按 1-5 分评估命题/问题质量（对照原文），低于阈值剔除。
- `llm2query / llm2claim`：LLM 生成问题/命题的封装。

### 3.2 `knowledge_synthesizer_ppr.py`（620 行）★ 层级化 + PPR 合成

- `detect_communities` / `detect_communities_diffusion`：社区检测（diffusion 版：用 PPR/Heat Kernel 影响矩阵选种子再聚类）。
- `summarize_community`：**社区 LLM 摘要**（成员节点文本拼接 → 摘要）。
- `create_hierarchical_graph_and_data` ★：**层级图构建**——每社区生成 SUMMARY_NODE（嵌入 + summarizes 边连成员），同步 networkx/dgl/训练标签（summary_response_id 关联社区摘要与答案）。
- `compute_ppr_matrix` / `compute_enhanced_ppr_matrix`：**个性化 PageRank 矩阵**（每行 = 以该节点为种子的 PPR 分布；增强版加平滑 + 行归一化）。
- `compute_heat_kernel_matrix`：热核扩散矩阵（t 参数）。
- `create_diffusion_based_summary`：扩散驱动的社区摘要。
- `evaluate_community_quality` / `adaptive_parameter_tuning`：社区质量评估 / 参数自适应。
- 主流程（约 590 行起）：构建图 → 社区 → 层级 → 选合成方法（ppr/heat_kernel）→ 查询时扩散聚合 → prompt 合成答案。

### 3.3 其他模块（职责记录）

- `retrieval.py`（90）：contriever 稠密检索（mean pooling 编码 + faiss IP/L2 搜索）。
- `prompt_pool.py`（153）：各数据集 prompt（QUERY_PROMPT_QMSUM / SQuALITY 剧情摘要 prompt / 问题生成）。
- `data_process.py`（204）：数据集加载（QMSum/WCEP/BookSum/GovReport/SQuALITY）+ 按文档切分。
- `train_lossnew.py`（397）：GNN 训练（命题图节点分类/排序头，论文贡献）。
- `training_preparation.py`（141）：训练数据准备。
- `eval.py`（447）/ `sum_eval.py`（91）：评估（BERTScore/ROUGE 等）。
- `utils.py`（247）：LLM API 调用封装（`get_llm_response_via_api`，openai 0.28 旧版 SDK）、工具函数。

---

## 4. 关键算法深挖

1. **命题化（Proposition Generation）**：把 chunk 压成单句命题——**信息从"段落级"降到"事实级"**，图检索的最小语义单元。对小说：可把章节事实（人物/关系/事件）做成命题节点。
2. **命题质量双层过滤**：规则（is_valid_statement）+ LLM judge（1-5 分）——防低质/幻觉命题污染图。
3. **层级摘要节点**：社区 → 一个 SUMMARY_NODE——与 Graphiti 社区、用户方案 Arc/Volume Memory 同一思路（社区=局部聚类，摘要=压缩）。
4. **PPR 扩散检索** ★：查询向量召回种子 → **沿图传播概率**（而非只取命中邻居）——能跨多跳聚合"语义远但图近"的证据，与 Mnemis System-2 的层级遍历异曲同工但更数学化。
5. **GNN 学习**：用 PPR 分数做标签训练图网络——**把扩散检索"可学习化"**（论文的主要贡献点；用户方案可不做）。

---

## 5. 与用户小说 RAG 方案的映射

| 用户方案组件 | HiGoE 对应物 | 结论 |
|---|---|---|
| Chapter Memory 细化 | 命题化（chunk→单句 claim） | ⚠️ 思路可用：把章节事实做成命题节点，但它是英文/摘要向 |
| 层级图（Arc/Volume→Book） | 社区检测 + SUMMARY_NODE 摘要节点 | ✅ 与 Mnemis 层级图同一思路的实现（纯 networkx+dgl） |
| 势力/剧情团块 | 社区（命题聚类） | ✅ 社区=势力/剧情线的一个可计算定义 |
| 图检索（Graph RAG 路径） | PPR / Heat Kernel 扩散 | ✅ 可借鉴：查询沿图扩散聚合而非仅邻居 |
| 压缩层 | 社区摘要节点 | ✅ 现成 |
| Reduce 汇总 | 扩散驱动社区摘要 | ✅ 思路 |
| 原文 Evidence | 证据边（chunk→命题）+ 稠密检索召回 | ✅ 现成 |
| 工程可复用度 | — | ❌ 研究代码：硬编码路径、openai 0.28 旧 API、需 torch+dgl+faiss、依赖训练 GNN 才完整 |

---

## 6. 可复用模块清单

**直接复用**
- 几乎无（研究原型：无打包、无配置化、依赖旧版库、数据集硬编码）。建议只读设计。

**需改造（读代码后重写）**
- PPR/Heat Kernel 扩散矩阵（~50 行纯 numpy，可移植到你的图检索层）。
- 命题生成 + LLM judge 双层过滤 prompt 结构（改中文/小说向）。
- 社区摘要节点构建（与 Graphiti 社区构建等价，可二选一）。

**仅参考**
- GNN 训练部分（train_lossnew/training_preparation）：用户方案是 RAG 非学习型，除非要上可学习检索。

---

## 7. 许可证与工程坑

- **无 LICENSE 文件** ⚠️ 默认版权保留——只做研究参考，勿直接复制进产品。
- 研究代码特征：绝对路径输出、无 main 入口封装（靠 argparse 脚本）、依赖 torch 1.12 + dgl + faiss-gpu + openai 0.28（旧 API `openai.ChatCompletion`）、需要 GPU。
- 数据集是英文会议/新闻/书籍/报告摘要——直接跑小说需换数据与 prompt。
- README 环境搭建指 CUDA 11.3 旧栈，现代环境需自行适配。

---

## 8. 成本与规模实测数据

- 论文未在 README 提供成本数据；QMSum/BookSum 等基准结果见论文。
- 成本特征：命题生成 = 每 chunk 1 次 LLM 调用（比"整章一次抽取"调用次数多但每次输出小）；社区摘要 = 每社区 1 次；LLM judge = 抽样部分命题（judge_sample_ratio）。
- 对用户 2000 章：若按命题化路线（每章 10-30 命题 + judge），调用次数 ≈ 2-4 万次量级，成本高于"整章抽取 + 图累计"路线；**仅当命题级粒度是硬需求时才值得**。

---

## 9. 通读结论

1. **HiGoE 提供的是"文档→命题图→层级→扩散检索"的完整研究范式**，与用户方案"层级 Memory + 图检索"在思想上高度同源；PPR 扩散是最值得移植的算法片段。
2. **它和 Mnemis 是"层级图检索"的两条路线**：Mnemis 用 LLM 逐层选择（System-2），HiGoE 用数学扩散（PPR）——用户方案可二选一或融合（先 LLM 定层、再扩散聚合）。
3. **工程复用价值低（研究代码、无许可证、旧栈）**，定位为"算法与设计参考"，与 LightRAG/GraphRAG 等工程框架互补。
