"""Agent definition loader — multi-source discovery and loading.

Loads AgentDefinition objects from markdown files with YAML frontmatter.
Search order (later overrides earlier by definition_id):
1. Builtin definitions (agent_framework/agent/definitions/)
2. User home (~/.agent_framework/agents/)
3. Project (.agent_framework/agents/)
4. Policy-managed (from FrameworkConfig)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_framework.infra.frontmatter import parse_frontmatter_file
from agent_framework.infra.logger import get_logger
from agent_framework.models.agent import AgentDefinition

logger = get_logger(__name__)

# Builtin definitions ship with the framework
_BUILTIN_DIR = Path(__file__).parent / "definitions"

# Well-known search directories (relative to user home / project root)
_USER_HOME_DIR_NAME = ".agent_framework/agents"
_PROJECT_DIR_NAMES = (".agent_framework/agents", ".claude/agents")


def _parse_definition(
    parsed: dict[str, Any],
    source: str,
    definition_id: str,
) -> AgentDefinition:
    """Convert parsed frontmatter+body into an AgentDefinition model."""
    fm = parsed["frontmatter"]
    body = parsed.get("body", "")
    source_path = str(parsed["path"])

    # Parse tools list from frontmatter
    tools_raw = fm.get("tools")
    tools: list[str] | None = None
    if isinstance(tools_raw, list):
        tools = tools_raw
    elif isinstance(tools_raw, str) and tools_raw != "*":
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]

    # Parse disallowed_tools
    disallowed_raw = fm.get("disallowed_tools", fm.get("disallowedTools", []))
    disallowed: list[str] = []
    if isinstance(disallowed_raw, list):
        disallowed = disallowed_raw
    elif isinstance(disallowed_raw, str):
        disallowed = [t.strip() for t in disallowed_raw.split(",") if t.strip()]

    # Parse mcp_servers (dict of name → config)
    mcp_raw = fm.get("mcp_servers", fm.get("mcpServers", {}))
    mcp_servers: dict[str, dict] = {}
    if isinstance(mcp_raw, dict):
        mcp_servers = mcp_raw

    return AgentDefinition(
        definition_id=definition_id,
        name=fm.get("name", definition_id),
        description=fm.get("description", ""),
        agent_type=fm.get("agent_type", fm.get("agentType", "general")),
        source=source,
        source_path=source_path,
        tools=tools,
        disallowed_tools=disallowed,
        permission_mode=fm.get("permission_mode", fm.get("permissionMode", "default")),
        model=fm.get("model"),
        mcp_servers=mcp_servers,
        system_instructions=body,
    )


def _scan_directory(
    directory: Path,
    source: str,
) -> list[AgentDefinition]:
    """Scan a directory for agent definition markdown files.

    Supports two layouts:
    - agents/<name>.md  (flat file, name from stem)
    - agents/<name>/agent.md  (directory with agent.md inside)
    """
    definitions: list[AgentDefinition] = []
    if not directory.is_dir():
        return definitions

    # Pattern 1: directory per agent with agent.md
    for child in sorted(directory.iterdir()):
        if child.is_dir():
            agent_file = child / "agent.md"
            if not agent_file.is_file():
                # Also check for any .md file
                md_files = list(child.glob("*.md"))
                if md_files:
                    agent_file = md_files[0]
                else:
                    continue
            parsed = parse_frontmatter_file(agent_file)
            if parsed:
                def_id = parsed["frontmatter"].get("name", child.name)
                definitions.append(_parse_definition(parsed, source, def_id))

    # Pattern 2: flat .md files
    for md_file in sorted(directory.glob("*.md")):
        if md_file.name.startswith("."):
            continue
        parsed = parse_frontmatter_file(md_file)
        if parsed:
            def_id = parsed["frontmatter"].get("name", md_file.stem)
            # Skip if already loaded from directory pattern
            if not any(d.definition_id == def_id for d in definitions):
                definitions.append(_parse_definition(parsed, source, def_id))

    return definitions


class AgentDefinitionLoader:
    """Multi-source agent definition discovery and loading.

    Definitions are loaded in priority order (later overrides earlier):
    1. Builtin (agent_framework/agent/definitions/)
    2. User home (~/.agent_framework/agents/)
    3. Project (.agent_framework/agents/ or .claude/agents/)
    4. Policy-managed (from config)
    5. Extra directories (from config)
    """

    def __init__(
        self,
        extra_directories: list[str] | None = None,
        project_root: Path | None = None,
        load_builtins: bool = True,
    ) -> None:
        self._extra_dirs = [Path(d) for d in (extra_directories or [])]
        self._project_root = project_root or Path.cwd()
        self._load_builtins = load_builtins
        self._definitions: dict[str, AgentDefinition] = {}

    def load_all(self) -> dict[str, AgentDefinition]:
        """Discover and load definitions from all sources.

        Returns dict of definition_id → AgentDefinition.
        Later sources override earlier ones with the same definition_id.
        """
        self._definitions.clear()

        # 1. Builtin definitions
        if self._load_builtins and _BUILTIN_DIR.is_dir():
            for defn in _scan_directory(_BUILTIN_DIR, "builtin"):
                self._definitions[defn.definition_id] = defn

        # 2. User home
        user_dir = Path.home() / _USER_HOME_DIR_NAME
        for defn in _scan_directory(user_dir, "user"):
            self._definitions[defn.definition_id] = defn

        # 3. Project directories
        for dir_name in _PROJECT_DIR_NAMES:
            project_dir = self._project_root / dir_name
            for defn in _scan_directory(project_dir, "project"):
                self._definitions[defn.definition_id] = defn

        # 4. Extra directories (from config)
        for extra_dir in self._extra_dirs:
            for defn in _scan_directory(extra_dir, "policy"):
                self._definitions[defn.definition_id] = defn

        logger.info(
            "agent_definitions.loaded",
            count=len(self._definitions),
            ids=list(self._definitions.keys()),
        )
        return dict(self._definitions)

    def get(self, definition_id: str) -> AgentDefinition | None:
        """Get a loaded definition by ID."""
        return self._definitions.get(definition_id)

    def list_definitions(self) -> list[AgentDefinition]:
        """List all loaded definitions."""
        return list(self._definitions.values())

    def list_by_type(self, agent_type: str) -> list[AgentDefinition]:
        """List definitions filtered by agent_type."""
        return [d for d in self._definitions.values() if d.agent_type == agent_type]
