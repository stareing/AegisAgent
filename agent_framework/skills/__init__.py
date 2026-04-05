"""File-based skill system (Claude Code-style).

Skills are defined as SKILL.md files with YAML frontmatter.
Discovery paths (priority order):
  1. Project: <cwd>/skills/<skill-name>/SKILL.md
  2. Personal: ~/.agent/skills/<skill-name>/SKILL.md
  3. Config: SkillsConfig.directories entries
"""
