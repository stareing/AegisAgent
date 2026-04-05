from __future__ import annotations

import json


class CLIConfirmationHandler:
    """Default CLI-based confirmation handler."""

    async def request_confirmation(
        self, tool_name: str, arguments: dict, description: str
    ) -> bool:
        print(f"\n--- Tool Confirmation Required ---")
        print(f"Tool: {tool_name}")
        print(f"Description: {description}")
        print(f"Arguments: {json.dumps(arguments, indent=2, ensure_ascii=False)}")
        response = input("Allow execution? [y/N]: ").strip().lower()
        return response in ("y", "yes")


class AutoApproveConfirmationHandler:
    """Confirmation handler that auto-approves everything."""

    async def request_confirmation(
        self, tool_name: str, arguments: dict, description: str
    ) -> bool:
        return True
