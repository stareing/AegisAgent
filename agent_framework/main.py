"""Local debug entrypoint.

Use ``python -m agent_framework.main`` during local development.
Production CLI usage should go through ``agent_framework.cli`` / ``agent-cli``.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_framework.cli import run


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()

