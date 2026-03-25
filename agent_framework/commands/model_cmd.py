"""Model switching /model slash command.

Modes:
  /model           - Show current model name and adapter type
  /model <name>    - Switch to a different model at runtime
  /model list      - List available models from catalog
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.commands.protocol import (
    CommandActionReturn,
    CommandContext,
    MessageAction,
    SlashCommand,
)

if TYPE_CHECKING:
    from agent_framework.entry import AgentFramework


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_framework(ctx: CommandContext) -> AgentFramework:
    """Extract the framework instance from command context."""
    fw = ctx.framework
    if fw is None:
        raise ValueError("Framework not available in command context")
    return fw


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _handle_list(ctx: CommandContext, _args: str) -> CommandActionReturn:
    """List available models from the catalog."""
    fw = _get_framework(ctx)
    models = fw.list_available_models()

    if not models:
        return MessageAction(content="No models found in catalog.")

    lines: list[str] = ["Available models:", ""]
    for entry in models:
        # ModelCatalog entries may be dicts or objects — handle both
        if isinstance(entry, dict):
            model_id = entry.get("model_id", entry.get("id", "?"))
            provider = entry.get("provider", "")
            display = f"  {model_id}"
            if provider:
                display += f"  ({provider})"
        else:
            model_id = getattr(entry, "model_id", getattr(entry, "id", str(entry)))
            provider = getattr(entry, "provider", "")
            display = f"  {model_id}"
            if provider:
                display += f"  ({provider})"
        lines.append(display)

    lines.append("")
    lines.append(f"Total: {len(models)} models")
    return MessageAction(content="\n".join(lines))


def _handle_model_root(ctx: CommandContext, args: str) -> CommandActionReturn:
    """Show current model or switch to a new one.

    - No args: display current model and adapter type.
    - With args (not 'list'): switch to the specified model.
    """
    fw = _get_framework(ctx)
    model_name = args.strip()

    # No argument — show current model info
    if not model_name:
        current_model = fw.config.model.default_model_name
        adapter_type = fw.config.model.adapter_type
        lines: list[str] = [
            "Current model:",
            f"  Model:   {current_model}",
            f"  Adapter: {adapter_type}",
        ]
        return MessageAction(content="\n".join(lines))

    # Switch model
    previous_model = fw.config.model.default_model_name

    # Update the configured model name
    fw.config.model.default_model_name = model_name

    # Normalize the model ID for the current adapter type
    try:
        normalized = fw.resolve_model_id(model_name)
        if normalized != model_name:
            fw.config.model.default_model_name = normalized
            model_name = normalized
    except Exception:
        # If normalization fails, keep the raw name
        pass

    # Recreate the model adapter with the new model name
    try:
        new_adapter = fw._create_model_adapter()
        if fw._deps is not None:
            fw._deps.model_adapter = new_adapter
    except Exception as exc:
        # Rollback on failure
        fw.config.model.default_model_name = previous_model
        return MessageAction(
            message_type="error",
            content=f"Failed to switch model to '{model_name}': {exc}",
        )

    return MessageAction(
        content=f"Model switched: {previous_model} -> {model_name}",
    )


# ---------------------------------------------------------------------------
# Exported SlashCommand instance
# ---------------------------------------------------------------------------

model_command = SlashCommand(
    name="model",
    description="Show or switch the active LLM model",
    category="model",
    handler=_handle_model_root,
    subcommands=[
        SlashCommand(
            name="list",
            description="List available models from catalog",
            aliases=["ls"],
            handler=_handle_list,
        ),
    ],
)
