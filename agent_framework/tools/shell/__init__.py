"""Shell management — persistent bash sessions with security controls.

Extracted from builtin/shell.py to separate concerns:
- shell_manager.py: BashSession lifecycle + command execution
- process_registry.py: ShellSessionManager + background task tracking
"""

from agent_framework.tools.shell.shell_manager import BashSession
from agent_framework.tools.shell.process_registry import ShellSessionManager

__all__ = ["BashSession", "ShellSessionManager"]
