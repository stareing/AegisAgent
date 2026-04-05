"""Shell session registry — manages multiple persistent shell sessions.

Each session is identified by a string key. The default key "default"
preserves backward compatibility with single-session callers.
"""

from __future__ import annotations

from agent_framework.tools.shell.shell_manager import BashSession


class ShellSessionManager:
    """Per-session shell manager.

    Each session is identified by a string key. The default key "default"
    preserves backward compatibility with existing callers.

    Not a global singleton — can be instantiated per framework instance.
    Class-level dict is kept for backward compat with existing builtin tools.
    """

    _sessions: dict[str, BashSession] = {}

    @classmethod
    def get(cls, session_id: str = "default") -> BashSession:
        """Return (or create) the BashSession for session_id."""
        if session_id not in cls._sessions:
            cls._sessions[session_id] = BashSession()
        return cls._sessions[session_id]

    @classmethod
    async def kill_all(cls) -> None:
        """Terminate every tracked session and clear the registry."""
        for session in list(cls._sessions.values()):
            await session.kill()
        cls._sessions.clear()

    @classmethod
    def list_sessions(cls) -> list[str]:
        """List all active session IDs."""
        return list(cls._sessions.keys())
