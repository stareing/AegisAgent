"""Tests for the new code tools: edit_file, grep_search, glob_files,
bash_exec, web_fetch, notebook_edit, todo_write/todo_read.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# All code tools use tmp_path which is outside the default sandbox (cwd).
@pytest.fixture(autouse=True)
def _allow_tmp_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FS_SANDBOX_ROOTS", str(tmp_path))


# ── edit_file ──────────────────────────────────────────────

class TestEditFile:
    def test_basic_replace(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import edit_file

        f = tmp_path / "test.py"
        f.write_text("def foo():\n    return 1\n")
        result = edit_file(str(f), "return 1", "return 42")
        assert "1 occurrence" in result
        assert f.read_text() == "def foo():\n    return 42\n"

    def test_replace_all(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import edit_file

        f = tmp_path / "test.txt"
        f.write_text("aaa bbb aaa ccc aaa")
        result = edit_file(str(f), "aaa", "xxx", replace_all=True)
        assert "3 occurrence" in result
        assert f.read_text() == "xxx bbb xxx ccc xxx"

    def test_not_unique_raises(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import edit_file

        f = tmp_path / "dup.txt"
        f.write_text("hello world hello")
        with pytest.raises(ValueError, match="appears 2 times"):
            edit_file(str(f), "hello", "hi")

    def test_not_found_raises(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import edit_file

        f = tmp_path / "miss.txt"
        f.write_text("something")
        with pytest.raises(ValueError, match="not found"):
            edit_file(str(f), "nonexistent", "x")

    def test_same_string_raises(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import edit_file

        f = tmp_path / "same.txt"
        f.write_text("hello")
        with pytest.raises(ValueError, match="identical"):
            edit_file(str(f), "hello", "hello")

    def test_file_not_found(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import edit_file

        with pytest.raises(FileNotFoundError):
            edit_file(str(tmp_path / "nonexistent.txt"), "a", "b")


# ── notebook_edit ──────────────────────────────────────────

class TestNotebookEdit:
    def _make_notebook(self, tmp_path: Path, cells: list[dict]) -> Path:
        nb = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": cells,
        }
        f = tmp_path / "test.ipynb"
        f.write_text(json.dumps(nb))
        return f

    def _code_cell(self, source: str) -> dict:
        return {
            "cell_type": "code",
            "metadata": {},
            "source": source.splitlines(keepends=True),
            "execution_count": None,
            "outputs": [],
        }

    def test_replace_cell(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import notebook_edit

        f = self._make_notebook(tmp_path, [self._code_cell("print(1)")])
        result = notebook_edit(str(f), 0, new_source="print(2)")
        assert "Replaced" in result
        nb = json.loads(f.read_text())
        assert "".join(nb["cells"][0]["source"]).strip() == "print(2)"

    def test_insert_after(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import notebook_edit

        f = self._make_notebook(tmp_path, [self._code_cell("cell0")])
        notebook_edit(str(f), 0, new_source="cell1", action="insert_after")
        nb = json.loads(f.read_text())
        assert len(nb["cells"]) == 2

    def test_delete_cell(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.code_edit import notebook_edit

        f = self._make_notebook(tmp_path, [
            self._code_cell("a"), self._code_cell("b"),
        ])
        notebook_edit(str(f), 0, action="delete")
        nb = json.loads(f.read_text())
        assert len(nb["cells"]) == 1


# ── grep_search ────────────────────────────────────────────

class TestGrepSearch:
    def test_basic_search(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import grep_search

        f = tmp_path / "code.py"
        f.write_text("def foo():\n    return 42\ndef bar():\n    pass\n")
        result = grep_search("def \\w+", str(tmp_path))
        assert result["total_matches"] == 2
        assert len(result["matches"]) == 2

    def test_case_insensitive(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import grep_search

        f = tmp_path / "log.txt"
        f.write_text("ERROR: something\nerror: other\nInfo: ok\n")
        result = grep_search("error", str(tmp_path), case_insensitive=True)
        assert result["total_matches"] == 2

    def test_glob_filter(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import grep_search

        (tmp_path / "a.py").write_text("match\n")
        (tmp_path / "b.txt").write_text("match\n")
        result = grep_search("match", str(tmp_path), glob="*.py")
        assert result["total_matches"] == 1

    def test_context_lines(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import grep_search

        f = tmp_path / "ctx.txt"
        f.write_text("line1\nline2\nMATCH\nline4\nline5\n")
        result = grep_search("MATCH", str(f), context_lines=1)
        match = result["matches"][0]
        assert "context_before" in match
        assert "context_after" in match

    def test_invalid_regex(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import grep_search

        with pytest.raises(ValueError, match="Invalid regex"):
            grep_search("[invalid", str(tmp_path))

    def test_single_file(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import grep_search

        f = tmp_path / "single.txt"
        f.write_text("hello world\n")
        result = grep_search("hello", str(f))
        assert result["total_matches"] == 1

    def test_respects_gitignore(self, tmp_path: Path) -> None:
        """Files matching .gitignore patterns are excluded by default."""
        from agent_framework.tools.builtin.search import grep_search, _load_gitignore_rules

        _load_gitignore_rules.cache_clear()
        # Simulate a git repo
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_text("build/\n*.log\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("keyword\n")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "out.py").write_text("keyword\n")
        (tmp_path / "debug.log").write_text("keyword\n")

        result = grep_search("keyword", str(tmp_path))
        files = [m["file"] for m in result["matches"]]
        assert any("app.py" in f for f in files)
        assert not any("build" in f for f in files)
        assert not any("debug.log" in f for f in files)

    def test_include_gitignored_flag(self, tmp_path: Path) -> None:
        """include_gitignored=True searches everything."""
        from agent_framework.tools.builtin.search import grep_search, _load_gitignore_rules

        _load_gitignore_rules.cache_clear()
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "app.py").write_text("keyword\n")
        (tmp_path / "debug.log").write_text("keyword\n")

        result = grep_search("keyword", str(tmp_path), include_gitignored=True)
        assert result["total_matches"] == 2

    def test_gitignore_negation(self, tmp_path: Path) -> None:
        """! negation patterns un-ignore previously ignored files."""
        from agent_framework.tools.builtin.search import grep_search, _load_gitignore_rules

        _load_gitignore_rules.cache_clear()
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_text("*.log\n!important.log\n")
        (tmp_path / "debug.log").write_text("keyword\n")
        (tmp_path / "important.log").write_text("keyword\n")
        (tmp_path / "app.py").write_text("keyword\n")

        result = grep_search("keyword", str(tmp_path))
        files = [m["file"] for m in result["matches"]]
        assert any("important.log" in f for f in files)
        assert not any("debug.log" in f for f in files)


# ── glob_files ─────────────────────────────────────────────

class TestGlobFiles:
    def test_recursive_glob(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import glob_files

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("")
        (tmp_path / "src" / "b.ts").write_text("")
        (tmp_path / "readme.md").write_text("")
        result = glob_files("**/*.py", str(tmp_path))
        assert len(result) == 1
        assert "a.py" in result[0]

    def test_sorted_by_mtime(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import glob_files

        import time
        f1 = tmp_path / "old.txt"
        f1.write_text("old")
        time.sleep(0.05)
        f2 = tmp_path / "new.txt"
        f2.write_text("new")
        result = glob_files("*.txt", str(tmp_path))
        assert "new.txt" in result[0]  # newest first

    def test_skips_hidden(self, tmp_path: Path) -> None:
        from agent_framework.tools.builtin.search import glob_files

        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "secret.py").write_text("")
        (tmp_path / "visible.py").write_text("")
        result = glob_files("**/*.py", str(tmp_path))
        assert len(result) == 1


# ── bash_exec ──────────────────────────────────────────────

class TestBashExec:
    def test_basic_command(self) -> None:
        from agent_framework.tools.builtin.shell import bash_exec, _ShellSessionManager
        _ShellSessionManager._sessions.clear()  # fresh session

        result = asyncio.run(bash_exec("echo hello"))
        assert result["exit_code"] == 0
        assert "hello" in result["output"]

    def test_persistent_cwd(self) -> None:
        from agent_framework.tools.builtin.shell import bash_exec, _ShellSessionManager
        _ShellSessionManager._sessions.clear()

        async def _run():
            await bash_exec("cd /tmp")
            result = await bash_exec("pwd")
            return result

        result = asyncio.run(_run())
        assert "/tmp" in result["output"]

    def test_exit_code(self) -> None:
        from agent_framework.tools.builtin.shell import bash_exec, _ShellSessionManager
        _ShellSessionManager._sessions.clear()

        result = asyncio.run(bash_exec("false"))
        assert result["exit_code"] != 0

    def test_timeout(self) -> None:
        from agent_framework.tools.builtin.shell import bash_exec, _ShellSessionManager
        _ShellSessionManager._sessions.clear()

        result = asyncio.run(bash_exec("sleep 10", timeout_seconds=1))
        assert result["timed_out"] is True

    def test_background_and_output(self) -> None:
        from agent_framework.tools.builtin.shell import bash_exec, bash_output, _ShellSessionManager
        _ShellSessionManager._sessions.clear()

        async def _run():
            bg = await bash_exec("echo bg_done", run_in_background=True)
            task_id = bg["task_id"]
            await asyncio.sleep(2)
            return bash_output(task_id)

        result = asyncio.run(_run())
        assert "bg_done" in result.get("output", "")


# ── kill_shell ─────────────────────────────────────────────

class TestKillShell:
    def test_kill(self) -> None:
        from agent_framework.tools.builtin.shell import bash_exec, kill_shell, _ShellSessionManager
        _ShellSessionManager._sessions.clear()

        async def _run():
            await bash_exec("echo start")
            msg = await kill_shell()
            return msg

        msg = asyncio.run(_run())
        assert "terminated" in msg.lower()


# ── web_fetch ──────────────────────────────────────────────

class TestWebFetch:
    def test_invalid_url(self) -> None:
        from agent_framework.tools.builtin.web import web_fetch

        with pytest.raises(ValueError, match="http"):
            web_fetch("not-a-url")

    def test_html_extraction(self) -> None:
        from agent_framework.tools.builtin.web import web_fetch

        html = "<html><head><title>Test</title></head><body><p>Hello World</p></body></html>"
        with patch("agent_framework.tools.builtin.web.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            mock_resp.read.return_value = html.encode()
            mock_resp.headers = {"Content-Type": "text/html"}
            result = web_fetch("https://example.com")
        assert result["title"] == "Test"
        assert "Hello World" in result["content"]

    def test_http_error(self) -> None:
        from agent_framework.tools.builtin.web import web_fetch

        with patch("agent_framework.tools.builtin.web.urlopen") as mock_open:
            from urllib.error import HTTPError
            mock_open.side_effect = HTTPError(
                "https://example.com", 404, "Not Found", {}, None
            )
            result = web_fetch("https://example.com")
        assert "error" in result
        assert "404" in result["error"]


# ── todo_write / todo_read ─────────────────────────────────

class TestTaskManager:
    def setup_method(self) -> None:
        from agent_framework.tools.builtin.task_manager import _TaskStore
        _TaskStore._instance = None

    def test_create_tasks(self) -> None:
        from agent_framework.tools.builtin.task_manager import todo_write, todo_read

        result = todo_write(json.dumps([
            {"title": "Task A", "priority": 1},
            {"title": "Task B"},
        ]))
        assert result["total_tasks"] == 2
        assert len(result["tasks"]) == 2

        read = todo_read()
        assert read["summary"]["total"] == 2
        assert read["summary"]["pending"] == 2

    def test_update_status(self) -> None:
        from agent_framework.tools.builtin.task_manager import todo_write, todo_read

        create = todo_write(json.dumps([{"title": "Do X"}]))
        task_id = create["tasks"][0]["id"]

        todo_write(json.dumps([{"id": task_id, "title": "Do X", "status": "completed"}]))
        read = todo_read()
        assert read["summary"]["completed"] == 1

    def test_priority_ordering(self) -> None:
        from agent_framework.tools.builtin.task_manager import todo_write, todo_read

        todo_write(json.dumps([
            {"title": "Low", "priority": 0},
            {"title": "High", "priority": 10},
        ]))
        read = todo_read()
        assert read["tasks"][0]["title"] == "High"

    def test_invalid_json(self) -> None:
        from agent_framework.tools.builtin.task_manager import todo_write

        with pytest.raises(ValueError, match="JSON"):
            todo_write("not json")
