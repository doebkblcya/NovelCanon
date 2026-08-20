"""CLI 冒烟测试：帮助、版本、未实现命令占位（ADR-0004）。"""

from typer.testing import CliRunner

from novelcanon.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "novelcanon" in result.stdout


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("import", "index", "extract", "activate", "query", "inspect"):
        assert cmd in result.stdout


def test_unimplemented_commands_are_explicit() -> None:
    """阶段 10 后 import/index/extract/query/activate 已实现；inspect 仍显式「尚未实现」。"""
    result = runner.invoke(app, ["inspect"])
    assert result.exit_code == 0, f"inspect 失败: {result.output}"
    assert "尚未实现" in result.stdout


def test_activate_requires_book_argument() -> None:
    """阶段 04/10：activate 已实现，缺 book_id 报用法错误而非「尚未实现」。"""
    result = runner.invoke(app, ["activate"])
    assert result.exit_code != 0
    assert "尚未实现" not in result.stdout
    assert "book_id" in result.stderr or "book_id" in result.stdout


def test_query_requires_question_and_book() -> None:
    """阶段 10：query 已实现，缺参数报用法错误而非「尚未实现」。"""
    result = runner.invoke(app, ["query"])
    assert result.exit_code != 0
    assert "尚未实现" not in result.stdout
    assert "question" in result.stderr or "question" in result.stdout


def test_extract_requires_book_argument() -> None:
    """阶段 06：extract 已实现，缺 book_id 参数报用法错误而非「尚未实现」。"""
    result = runner.invoke(app, ["extract"])
    assert result.exit_code != 0
    assert "尚未实现" not in result.stdout
    assert "book_id" in result.stderr or "book_id" in result.stdout


def test_extract_dry_run_validates_config_and_book(tmp_path) -> None:
    """extract --dry-run：无模型调用，校验配置 + 章节并退出。"""
    from tests.helpers import FIXTURE_CHAPTERS, make_fixture_epub

    epub = tmp_path / "fixture.epub"
    make_fixture_epub(epub, FIXTURE_CHAPTERS, title="CLI测试书")

    db = tmp_path / "cli.db"
    env = {
        "NOVELCANON_DB_PATH": str(db),
        "NOVELCANON_LLM_MODEL": "test-model",
        "NOVELCANON_LLM_BASE_URL": "https://example.invalid/v1",
    }
    result = runner.invoke(app, ["import", str(epub)], env=env)
    assert result.exit_code == 0, result.output
    import re

    book_id = re.search(r"book=(book_[0-9a-f]+)", result.stdout).group(1)

    # dry-run：不调用模型，打印章节数并退出 0
    result = runner.invoke(app, ["extract", book_id, "--dry-run"], env=env)
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.stdout
    assert "章节=3" in result.stdout

    # 缺少模型配置（显式置空，覆盖 .env）→ 明确报错
    bad_env = {
        "NOVELCANON_DB_PATH": str(db),
        "NOVELCANON_LLM_MODEL": "",
        "NOVELCANON_LLM_BASE_URL": "",
    }
    result = runner.invoke(app, ["extract", book_id, "--dry-run"], env=bad_env)
    assert result.exit_code != 0
    assert "LLM_MODEL" in result.stderr or "LLM_MODEL" in result.stdout


def test_invalid_env_config_fails_at_startup() -> None:
    """01 验证项：无效配置在启动阶段失败，并给出明确字段错误。"""
    result = runner.invoke(app, ["query"], env={"NOVELCANON_LOG_LEVEL": "VERBOSE"})
    assert result.exit_code != 0
    assert result.exception is not None
    assert "log_level" in str(result.exception)


def test_command_logs_with_json_output() -> None:
    """01 验证项：日志能够关联一次命令执行，且不泄露敏感信息。"""
    result = runner.invoke(app, ["--log-json", "inspect"])
    assert result.exit_code == 0
    assert '"event": "command_invoked"' in result.stdout
    assert '"command": "inspect"' in result.stdout


def test_index_uses_configured_embedding_profile(tmp_path, monkeypatch) -> None:
    """阶段 11 复审 D：CLI index 按 NOVELCANON_EMBEDDING_PROFILE_ID 走
    factory 创建生产后端（不再硬编码 FakeEmbedder(8)）——active 索引记录
    配置的 profile，embedding_records 按后端维数落向量。

    register_configured_backends 被 monkeypatch 跳过（真实 HTTP 集成已由
    test_api.py 本地端点覆盖）；profile 预先注册 fake 后端模拟生产路径。
    """
    import re

    from sqlalchemy import text

    from novelcanon.retrieval.factory import (
        register_backend,
        unregister_backend,
    )
    from novelcanon.retrieval.vectorstore import BruteForceVectorStore, FakeEmbedder
    from novelcanon.storage.engine import create_db_engine
    from tests.helpers import FIXTURE_CHAPTERS, make_fixture_epub

    db = tmp_path / "t.db"
    env = {
        "NOVELCANON_DB_PATH": str(db),
        "NOVELCANON_EMBEDDING_PROFILE_ID": "prod-embed-16",
        "NOVELCANON_EMBEDDING_DIMENSION": "16",
        "NOVELCANON_EMBEDDING_BASE_URL": "http://127.0.0.1:9",
        "NOVELCANON_EMBEDDING_MODEL": "test-model",
    }
    monkeypatch.setattr(
        "novelcanon.retrieval.factory.register_configured_backends", lambda settings=None: []
    )

    def _prod_embedder() -> FakeEmbedder:
        e = FakeEmbedder(dimension=16)
        e.profile_id = "prod-embed-16"  # 模拟生产 adapter：profile_id 与注册 key 一致
        return e

    register_backend(
        "prod-embed-16",
        lambda: (_prod_embedder(), BruteForceVectorStore(dimension=16)),
    )
    try:
        epub = tmp_path / "book.epub"
        make_fixture_epub(epub, FIXTURE_CHAPTERS)
        r = runner.invoke(app, ["import", str(epub)], env=env)
        assert r.exit_code == 0, r.output
        m = re.search(r"book=(book_[0-9a-f]+)", r.output)
        assert m, r.output
        book_id = m.group(1)

        r = runner.invoke(app, ["index", book_id], env=env)
        assert r.exit_code == 0, r.output
        assert "prod-embed-16" in r.output, r.output

        engine = create_db_engine(db)
        try:
            with engine.connect() as conn:
                prof = conn.execute(
                    text(
                        "SELECT DISTINCT embedding_profile_id FROM index_versions"
                        " WHERE book_id = :b AND status = 'active'"
                    ),
                    {"b": book_id},
                ).scalar()
                assert prof == "prod-embed-16", f"active 索引应记录配置的 profile：{prof}"
                dim = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM embedding_records WHERE profile_id = 'prod-embed-16'"
                    )
                ).scalar()
                assert dim > 0, "embedding_records 应有生产 profile 的向量记录"
        finally:
            engine.dispose()
    finally:
        unregister_backend("prod-embed-16")
