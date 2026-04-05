"""Local debug entrypoint.

Use ``python -m agent_framework.main`` during local development.
python -m agent_framework.main --no-qa-limit          # Q&A 无限轮次
python -m agent_framework.main --team-iterations 50    # teammate 每次 50 轮迭代
python -m agent_framework.main --config config/doubao.local.json

Production CLI usage should go through ``agent_framework.cli`` / ``agent-cli``.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_framework.cli import run


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()

