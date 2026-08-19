# ADR-0002 SQLite 访问与迁移

- 状态：已接受（2026-08）
- 适用范围：存储层（阶段 02 起实现）

## 背景

定版方案以 SQLite 为唯一权威数据源，要求 single writer、WAL、事务、可恢复迁移。需要确定访问方式与迁移工具。

## 决策

- **SQLAlchemy 2.0 Core（同步）+ Python 内置 `sqlite3` driver**；不采用 aiosqlite；
- 连接参数固定：`WAL`、`busy_timeout`、`foreign_keys=ON`；
- **单独 writer service**：异步模型 worker 产出 Draft → 有界队列 → 同步 single writer 批量事务写入 staging；
- FastAPI（P4）查询使用同步 `def` endpoint，或从 async endpoint 经线程池调用同步 repository；
- 网络模型调用继续使用 asyncio / httpx，与存储层解耦；
- **Alembic** 做版本管理，但 **migration 全部手写 revision，禁用 autogenerate**——尤其 FTS5 虚表、vec 虚表、trigger、partial 索引、active 视图、SQLite 表重建式迁移、ontology/state catalog 数据迁移；
- 每个 migration 至少验证：空库 → 最新；上一版本备份 → 最新；升级后 `foreign_key_check`；升级后 FTS/vec smoke 查询；失败时旧库保持可恢复。

## 理由

- aiosqlite 是"每个连接后跟一个后台线程包装同步 pysqlite"，并非真正非阻塞，反而带来 transaction/connection hook 复杂化、每连接加载 vec 扩展、虚表与递归 CTE 调试困难等成本；
- 与定版方案 §8.2「单 writer 消费有界队列并批量事务写入」一致，同步存储事务简单可靠；
- 并发收益来自模型调用（worker 级），SQLite 写入端本来就是串行化点，async 化没有收益。

## 后果

- 所有复杂 SQL（FTS、递归 CTE、vec0、窗口）以 `text()` 手写，不依赖 ORM 表达式；
- 阶段 03 起连接初始化必须统一挂 SQLAlchemy `connect` event（extension 加载、PRAGMA）；
- 阶段 04 实现 writer service 时不得滑回 aiosqlite。

## 参考

- 定版方案 §8.2
- <https://docs.sqlalchemy.org/en/21/dialects/sqlite.html#aiosqlite>
