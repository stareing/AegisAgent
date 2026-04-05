"""Tests for built-in tools: filesystem and system."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agent_framework.tools.builtin.filesystem import (file_exists,
                                                      list_directory,
                                                      read_file, write_file)
from agent_framework.tools.builtin.system import get_env, run_command

# =====================================================================
# Filesystem tools
# =====================================================================


@pytest.fixture(autouse=True)
def _allow_tmp_sandbox(monkeypatch, tmp_path):
    """Allow test tmp_path in filesystem sandbox."""
    monkeypatch.setenv("AGENT_FS_SANDBOX_ROOTS", str(tmp_path))


class TestReadFile:

    def test_read_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = read_file(str(f))
        # Now returns cat -n format with line numbers
        assert "1\thello world" in result

    def test_read_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_file(str(tmp_path / "nonexistent" / "file.txt"))

    def test_read_directory_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not a file"):
            read_file(str(tmp_path))

    def test_read_with_encoding(self, tmp_path):
        f = tmp_path / "utf8.txt"
        f.write_text("cafe\u0301", encoding="utf-8")
        content = read_file(str(f), encoding="utf-8")
        assert "cafe" in content


class TestWriteFile:

    def test_write_new_file(self, tmp_path):
        target = tmp_path / "output.txt"
        result = write_file(str(target), "test content")
        assert target.exists()
        assert target.read_text() == "test content"
        assert "12 characters" in result

    def test_write_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "sub" / "dir" / "file.txt"
        write_file(str(target), "nested")
        assert target.exists()
        assert target.read_text() == "nested"

    def test_write_overwrites_existing(self, tmp_path):
        target = tmp_path / "overwrite.txt"
        target.write_text("old")
        write_file(str(target), "new")
        assert target.read_text() == "new"


class TestListDirectory:

    def test_list_basic(self, tmp_path):
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.py").touch()
        result = list_directory(str(tmp_path))
        assert "a.txt" in result
        assert "b.py" in result

    def test_list_with_pattern(self, tmp_path):
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.py").touch()
        result = list_directory(str(tmp_path), pattern="*.py")
        assert "b.py" in result
        assert "a.txt" not in result

    def test_list_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            list_directory(str(tmp_path / "nonexistent_dir"))

    def test_list_file_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.touch()
        with pytest.raises(ValueError, match="not a directory"):
            list_directory(str(f))

    def test_list_empty_directory(self, tmp_path):
        sub = tmp_path / "empty"
        sub.mkdir()
        result = list_directory(str(sub))
        assert result == []

    def test_list_sorted(self, tmp_path):
        (tmp_path / "c.txt").touch()
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()
        result = list_directory(str(tmp_path))
        assert result == ["a.txt", "b.txt", "c.txt"]


class TestFileExists:

    def test_exists_true(self, tmp_path):
        f = tmp_path / "exists.txt"
        f.touch()
        assert file_exists(str(f)) is True

    def test_exists_false(self):
        assert file_exists("/nonexistent/file.xyz") is False

    def test_exists_directory(self, tmp_path):
        assert file_exists(str(tmp_path)) is True


# =====================================================================
# System tools
# =====================================================================


class TestRunCommand:

    def test_echo(self):
        result = run_command("echo hello")
        assert result["return_code"] == 0
        assert "hello" in result["stdout"]

    def test_failing_command(self):
        result = run_command("false")
        assert result["return_code"] != 0

    def test_timeout(self):
        result = run_command("sleep 10", timeout_seconds=1)
        assert result["return_code"] == -1
        assert "timed out" in result["stderr"].lower()

    def test_cwd(self, tmp_path):
        result = run_command("pwd", cwd=str(tmp_path))
        assert str(tmp_path) in result["stdout"]

    def test_stderr_capture(self):
        result = run_command("echo err >&2")
        assert "err" in result["stderr"]


class TestGetEnv:

    def test_existing_env_var(self):
        result = get_env("PATH")
        assert len(result) > 0

    def test_nonexistent_env_var_default(self):
        result = get_env("DEFINITELY_NOT_SET_XYZ_12345", default="fallback")
        assert result == "fallback"

    def test_nonexistent_env_var_empty_default(self):
        result = get_env("DEFINITELY_NOT_SET_XYZ_12345")
        assert result == ""
