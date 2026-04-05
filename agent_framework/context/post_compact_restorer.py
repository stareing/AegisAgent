"""Post-compaction context restorer.

After context compaction, re-injects recently-accessed files and active skill
instructions to maintain agent coherence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_framework.models.message import Message

if TYPE_CHECKING:
    from agent_framework.models.agent import Skill

logger = logging.getLogger(__name__)

# Maximum number of recently-accessed files to re-inject after compaction
_MAX_RESTORED_FILES = 5


class PostCompactRestorer:
    """Restores critical context after compression.

    After the compressor reduces session history, some recently-relevant
    context may be lost. This restorer re-injects:
    1. Top N recently-accessed file references as system hints
    2. Active skill system_prompt_addon (if present)

    The restorer only appends — it never removes or reorders existing messages.
    """

    def restore(
        self,
        compressed_messages: list[Message],
        recently_accessed_files: list[str] | None = None,
        active_skill: Skill | None = None,
    ) -> list[Message]:
        """Re-inject context lost during compaction.

        Args:
            compressed_messages: Message list produced by the compressor.
            recently_accessed_files: Ordered list of file paths (most recent first).
            active_skill: Currently active skill, if any.

        Returns:
            Augmented message list with restored context appended.
        """
        if not recently_accessed_files and not active_skill:
            return compressed_messages

        restoration_parts: list[str] = []

        # Re-inject top N recently-accessed files
        if recently_accessed_files:
            top_files = recently_accessed_files[:_MAX_RESTORED_FILES]
            file_list = "\n".join(f"- {f}" for f in top_files)
            restoration_parts.append(
                f"<recently-accessed-files>\n{file_list}\n</recently-accessed-files>"
            )
            logger.debug(
                "post_compact_restorer.files_restored count=%d", len(top_files)
            )

        # Re-inject active skill addon
        if active_skill and active_skill.system_prompt_addon:
            restoration_parts.append(
                f"<active-skill-context skill_id=\"{active_skill.skill_id}\">\n"
                f"{active_skill.system_prompt_addon}\n"
                f"</active-skill-context>"
            )
            logger.debug(
                "post_compact_restorer.skill_restored skill_id=%s",
                active_skill.skill_id,
            )

        if not restoration_parts:
            return compressed_messages

        restoration_msg = Message(
            role="user",
            content="<post-compaction-context>\n"
            + "\n\n".join(restoration_parts)
            + "\n</post-compaction-context>",
        )

        result = list(compressed_messages)
        result.append(restoration_msg)
        return result
