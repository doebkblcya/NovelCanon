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
    cases: dict[str, list[str]] = {
        "import": ["import", "book.epub"],
        "index": ["index"],
        "extract": ["extract"],
        "activate": ["activate"],
        "query": ["query"],
        "inspect": ["inspect"],
    }
    for cmd, args in cases.items():
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{cmd} 失败: {result.output}"
        assert "尚未实现" in result.stdout


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
