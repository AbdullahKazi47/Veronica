from veronica.safety.permissions import PermissionLevel
from veronica.tools.execution_tools import (
    DeleteFileTool,
    ReadFileTool,
    RunShellCommandTool,
    WriteFileTool,
)


def test_write_then_read_file(tmp_path):
    target = tmp_path / "note.txt"
    write_result = WriteFileTool().run(path=str(target), content="hello veronica")
    assert write_result.success is True
    assert target.read_text(encoding="utf-8") == "hello veronica"

    read_result = ReadFileTool().run(path=str(target))
    assert read_result.success is True
    assert read_result.data["content"] == "hello veronica"


def test_read_file_missing():
    result = ReadFileTool().run(path="/definitely/does/not/exist.txt")
    assert result.success is False


def test_delete_file(tmp_path):
    target = tmp_path / "to_delete.txt"
    target.write_text("bye")
    result = DeleteFileTool().run(path=str(target))
    assert result.success is True
    assert not target.exists()


def test_delete_file_refuses_directories(tmp_path):
    result = DeleteFileTool().run(path=str(tmp_path))
    assert result.success is False


def test_shell_command_runs_and_captures_output():
    result = RunShellCommandTool().run(command="echo hello_from_veronica")
    assert result.success is True
    assert "hello_from_veronica" in result.message


def test_shell_command_blocks_dangerous_pattern():
    result = RunShellCommandTool().run(command="rm -rf /")
    assert result.success is False
    assert "blocked" in result.message.lower() or "refus" in result.message.lower()


def test_shell_command_blocks_fork_bomb():
    result = RunShellCommandTool().run(command=":(){ :|:& };:")
    assert result.success is False


def test_shell_command_reports_nonzero_exit():
    result = RunShellCommandTool().run(command="exit 1")
    assert result.success is False


def test_execution_tools_are_gated_appropriately():
    assert RunShellCommandTool().permission == PermissionLevel.CRITICAL
    assert DeleteFileTool().permission == PermissionLevel.CRITICAL
    assert WriteFileTool().permission == PermissionLevel.SENSITIVE
    assert ReadFileTool().permission == PermissionLevel.SAFE
