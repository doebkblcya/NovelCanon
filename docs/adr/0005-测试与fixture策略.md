# ADR-0005 测试与 fixture 策略

- 状态：已接受（2026-08）
- 适用范围：全项目质量门禁

## 背景

阶段 01 要求本地与 CI 的静态检查、类型检查、测试全部通过；测试必须无网络、无模型密钥可运行。

## 决策

- **pytest** + **pytest-cov**；**pytest-asyncio**（`asyncio_mode=auto`）用于异步模型调用相关测试；
- **ruff**（lint + format）与 **mypy**（类型检查；strict 规则随阶段逐步收紧）；
- 黄金 fixture 集中在 `fixtures/` 目录（文本、预期结果、schema 样例），与测试代码分离；
- 测试不得依赖网络或模型密钥；涉及真实外部依赖的用例用 `skipif`/`importorskip` 显式跳过；
- CI（GitHub Actions）：Python 3.13 + 3.14 矩阵，安装 → ruff → mypy → pytest，并增加 **SQLite 特性 smoke**（FTS5/JSON1 可用、`sqlite_version()` ≥ 3.41、sqlite-vec 扩展可加载）；
- 覆盖率阈值从阶段 02 起配置，阶段 01 不设硬性阈值。

## 理由

- "无网络无密钥可运行"是 01 退出标准，也是后续所有阶段测试的基线约束；
- CI 中验证 SQLite 编译特性与 vec 扩展加载，避免开发机可跑、CI 不可跑的隐性依赖。

## 后果

- 新增依赖必须保证在干净 CI 环境可安装、可测试；
- 真实模型/向量服务的集成测试一律标记并延迟到相应阶段。

## 参考

- docs/implementation/01-工程骨架.md
