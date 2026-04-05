"""Workspace templates — scaffolds project structure for new agent projects.

Provides idempotent initialization that creates config, skills, and team
directories with starter files. Skips files that already exist.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG = {
    "model": {
        "adapter_type": "litellm",
        "default_model_name": "gpt-4o-mini",
        "temperature": 0.7,
        "max_output_tokens": 4096,
    },
    "context": {
        "max_context_tokens": 128000,
        "adaptive_compaction": True,
    },
    "tools": {
        "shell_enabled": False,
    },
    "memory": {
        "store_type": "sqlite",
        "db_path": "data/memories.db",
    },
}

DEFAULT_SKILL_MD = """---
name: example
description: "Example skill template"
user-invocable: true
argument-hint: "Describe what you need"
---

## Instructions

You are a helpful assistant. Follow the user's instructions carefully.
"""

DEFAULT_AGENT_TEAM_README = """# Agent Team

Place team member definitions here as YAML files.
Each file defines a teammate role and capabilities.

Example: researcher.yaml
```yaml
role: researcher
skill_id: research
system_prompt_addon: "You are a research specialist."
max_iterations: 15
```
"""


# Template: relative_path -> content
TEMPLATES: dict[str, dict[str, str]] = {
    "default": {
        "config/default.json": json.dumps(DEFAULT_CONFIG, indent=2),
        "skills/example/SKILL.md": DEFAULT_SKILL_MD,
        ".agent-team/README.md": DEFAULT_AGENT_TEAM_README,
        "data/.gitkeep": "",
    },
    "minimal": {
        "config/default.json": json.dumps(DEFAULT_CONFIG, indent=2),
    },
}


def init_workspace(
    template: str = "default",
    target_dir: str | Path = ".",
) -> list[str]:
    """Initialize a workspace from a template.

    Creates directories and files. Skips files that already exist (idempotent).

    Returns list of created file paths (relative).
    """
    target = Path(target_dir)
    file_map = TEMPLATES.get(template)
    if file_map is None:
        available = ", ".join(sorted(TEMPLATES.keys()))
        raise ValueError(f"Unknown template '{template}'. Available: {available}")

    created: list[str] = []

    for rel_path, content in file_map.items():
        full_path = target / rel_path
        if full_path.exists():
            logger.info("workspace.skip_existing", path=rel_path)
            continue

        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        created.append(rel_path)
        logger.info("workspace.created", path=rel_path)

    return created


def list_templates() -> list[str]:
    """Return available template names."""
    return sorted(TEMPLATES.keys())
