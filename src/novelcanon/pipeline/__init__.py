"""流水线：run 生命周期、checkpoint、队列、重试、Token 账本、原子激活（阶段 04 实现）。

异步模型 worker → 有界队列 → 同步 single writer（ADR-0002）。
"""
