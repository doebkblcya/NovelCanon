"""存储层：SQLAlchemy Core（同步）+ SQLite、repository、迁移（阶段 02 实现）。

约束（ADR-0002）：单 writer、WAL、busy_timeout、foreign_keys=ON；
migration 全部手写 revision，禁用 autogenerate。
"""
