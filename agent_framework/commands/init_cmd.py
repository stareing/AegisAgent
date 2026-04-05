"""Slash command: /init — analyze project and generate CLAUDE.md.

Ported from Gemini CLI's /init command. Checks whether CLAUDE.md already
exists in the working directory. If it does, returns an informational
message. Otherwise, returns a SubmitPromptAction with a comprehensive
analysis prompt that instructs the LLM to explore the project and write
a complete CLAUDE.md.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agent_framework.commands.protocol import (
    CommandActionReturn,
    MessageAction,
    SlashCommand,
    SubmitPromptAction,
)

if TYPE_CHECKING:
    from agent_framework.commands.protocol import CommandContext

# ---------------------------------------------------------------------------
# The analysis prompt submitted to the LLM when no CLAUDE.md exists.
# Production-quality: instructs the agent to use its available tools
# (glob_files, list_directory, read_file) to explore the repo, then
# generate a complete CLAUDE.md.
# ---------------------------------------------------------------------------

_INIT_PROMPT = """\
You are an AI coding agent. Your task is to analyze the current project \
directory and generate a comprehensive CLAUDE.md file that will serve as \
persistent instructional context for all future interactions with this codebase.

**Analysis Process — follow these steps in order:**

1. **High-level exploration**
   - Use `list_directory` on the project root to see top-level files and folders.
   - Use `glob_files` with patterns like `**/*.py`, `**/*.ts`, `**/*.go`, \
`**/*.rs`, `**/*.java` (pick the most likely language) to understand the \
source layout.

2. **Read foundational files**
   - Read `README.md` (or `README.rst`, `README.txt`) if it exists — this is \
the single best source of project intent.
   - Read the primary build/config manifest: `pyproject.toml`, `package.json`, \
`Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`, `Makefile`, or equivalent.
   - Read any existing style/contribution guides (e.g., `CONTRIBUTING.md`, \
`.editorconfig`, `setup.cfg`).

3. **Iterative deep dive (up to 10 additional files)**
   - Based on your initial findings, select the most architecturally \
significant source files — entry points, core modules, routers, schemas, \
test configuration.
   - Read each file. Let your discoveries guide which file to read next; do \
not decide all files up front.

4. **Classify the project**
   - **Code project:** presence of source files, build manifests, dependency \
lock files, `src/` or `lib/` directories.
   - **Non-code project:** documentation collections, data sets, research \
notes, configuration bundles.

5. **Generate CLAUDE.md**

   Write the complete file to `CLAUDE.md` in the project root using \
`write_file`. The content MUST be well-formatted Markdown and MUST include \
the following sections:

   **For a code project:**

   - **Project Overview** — concise summary of purpose, main technologies, \
language version, and high-level architecture.
   - **Commands** — exact commands to build, test, lint, and run the project. \
Infer from `scripts` in `package.json`, `Makefile` targets, `pyproject.toml` \
`[project.scripts]`, or CI configuration. If a command cannot be determined, \
write a `TODO` placeholder.
   - **Architecture** — describe the directory layout, key modules/packages, \
data flow, and any layered or plugin-based patterns.
   - **Conventions** — coding style (formatter, linter), naming conventions, \
import ordering, testing practices, commit message format, and any project-\
specific rules you can infer from the code.

   **For a non-code project:**

   - **Directory Overview** — purpose and contents of the directory.
   - **Key Files** — the most important files with brief descriptions.
   - **Usage** — how the contents are intended to be consumed.

6. **Final check**
   - Re-read the generated CLAUDE.md to verify it is accurate, complete, and \
does not contain hallucinated commands or paths.
"""


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _handle_init(context: CommandContext, _args: str = "") -> CommandActionReturn:
    """Execute the /init command."""
    working_dir: str = getattr(context, "framework", None)
    # Prefer explicit working directory from context.config or framework;
    # fall back to cwd.
    if hasattr(context, "config") and context.config is not None:
        working_dir = getattr(context.config, "working_directory", None) or os.getcwd()
    else:
        working_dir = os.getcwd()

    claude_md_path = os.path.join(working_dir, "CLAUDE.md")

    if os.path.isfile(claude_md_path):
        return MessageAction(
            message_type="info",
            content=(
                "A CLAUDE.md file already exists in this directory. "
                "No changes were made."
            ),
        )

    return SubmitPromptAction(content=_INIT_PROMPT)


# ---------------------------------------------------------------------------
# Exported SlashCommand instance
# ---------------------------------------------------------------------------

init_command = SlashCommand(
    name="init",
    description="Analyze the project and generate a CLAUDE.md context file",
    handler=_handle_init,
    category="project",
)
