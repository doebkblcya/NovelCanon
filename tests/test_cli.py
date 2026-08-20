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
