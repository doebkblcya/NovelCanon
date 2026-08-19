# ADR-0006 向量与 Tokenizer 抽象边界

- 状态：已接受（2026-08）
- 适用范围：raw chunk、向量检索、token 计量（阶段 03 起实现）

## 背景

定版方案要求：raw chunk 按 embedding tokenizer 切分；向量索引随 raw chunk 版本重建、同一时刻仅激活一个版本；检索必须先限定 `book_id`，且若向量后端不能在 Top-K 前执行元数据过滤则按 book/profile 分表。sqlite-vec 为 pre-v1 实验项目，不得作为 P0 正确性前提。

## 决策

### VectorStore Protocol

```text
VectorStore Protocol
├── BruteForceVectorStore    # 测试/Pilot 基线（纯 Python，无扩展依赖）
└── SqliteVecVectorStore     # 生产候选
```

- 精确锁定 `sqlite-vec==0.1.9`，不使用 alpha；作为**实验后端**，不承诺 P0 正确性前提；
- 加载方式限定：`enable_load_extension(True)` → `sqlite_vec.load(conn)` → `enable_load_extension(False)`，由 SQLAlchemy `connect` event 统一处理；
- 启动时校验 `sqlite_version()` ≥ 3.41 与 `vec_version()`；
- **每种向量维数使用独立 vec0 表**；`book_id` 放入 vec0 **partition key**，`profile_id/index_version` 放入 metadata（或按 profile 分表）——`book_id` 不能只存在普通 `embedding_records` 表里依赖 `JOIN + WHERE`（不保证先过滤再 Top-K）；
- 检索接口返回稳定 record ID，元数据保存在普通表（`embedding_records`）。

### Tokenizer Protocol

```text
class Tokenizer(Protocol):
    tokenizer_id: str
    def encode(self, text: str) -> Sequence[int]: ...
    def decode(self, tokens: Sequence[int]) -> str: ...
    def count(self, text: str) -> int: ...
```

- tiktoken 只是**可选适配器**（`TiktokenAdapter`），不作为统一计量基准；未来可加 HF/SentencePiece adapter；测试用 `FakeTokenizer`；
- generation profile 与 embedding profile 分别指定 `tokenizer_id`；
- `tokenizer_id` 计入 raw chunk 的 `chunking_version`，tokenizer 更换自动失效重建（定版方案 §3.3）。

## 理由

- sqlite-vec 官方标注 pre-v1 且可能破坏性变更，因此 P0 正确性链路必须能脱离它运行（BruteForce 基线）；
- vec0 的 partition key / metadata 是 Top-K 前过滤的正规机制，普通表 JOIN 不保证该语义；
- 定版方案 §13 要求组件通过版本化 profile 引用、不绑定具体厂商。

## 后果

- P1 Pilot 可在无真实向量服务情况下跑通全链路；
- 阶段 03 需为向量后端写契约测试（按 book 隔离、Top-K 不被其他书挤占）。

## 参考

- 定版方案 §3.3、§8.1、§13
- <https://github.com/asg017/sqlite-vec/releases>
- <https://alexgarcia.xyz/blog/2024/sqlite-vec-metadata-release/index.html>
- <https://github.com/asg017/sqlite-vec/issues/196>
