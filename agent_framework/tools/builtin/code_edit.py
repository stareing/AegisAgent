"""Built-in code editing tools.

Provides find-and-replace editing (edit_file) and Jupyter notebook
cell editing (notebook_edit).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_framework.tools.builtin.filesystem import _ensure_within_sandbox
from agent_framework.tools.decorator import tool


@tool(
    name="edit_file",
    description=(
        "Perform an exact string replacement in a file. "
        "Finds old_string and replaces it with new_string. "
        "By default old_string must be unique in the file; "
        "set replace_all=True to replace every occurrence."
    ),
    category="filesystem",
    require_confirm=True,
)
def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Edit a file by replacing an exact string match.

    Args:
        file_path: Absolute path to the file to modify.
        old_string: The exact text to find and replace.
        new_string: The replacement text (must differ from old_string).
        replace_all: If True, replace all occurrences. If False (default),
                     old_string must appear exactly once.

    Returns:
        Confirmation message with number of replacements made.
    """
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical — nothing to change")

    path = _ensure_within_sandbox(Path(file_path))
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    content = path.read_text(encoding="utf-8")
    count = content.count(old_string)

    if count == 0:
        raise ValueError(
            f"old_string not found in {file_path}. "
            "Verify the exact text including whitespace and indentation."
        )

    if not replace_all and count > 1:
        raise ValueError(
            f"old_string appears {count} times in {file_path}. "
            "Provide more surrounding context to make it unique, "
            "or set replace_all=True to replace every occurrence."
        )

    new_content = content.replace(old_string, new_string)
    path.write_text(new_content, encoding="utf-8")

    replaced = count if replace_all else 1
    return f"Replaced {replaced} occurrence(s) in {file_path}"


@tool(
    name="notebook_edit",
    description=(
        "Edit a Jupyter Notebook (.ipynb) cell by index. "
        "Can replace cell source, change cell type, or insert/delete cells."
    ),
    category="filesystem",
    require_confirm=True,
)
def notebook_edit(
    file_path: str,
    cell_index: int,
    new_source: str | None = None,
    cell_type: str | None = None,
    action: str = "replace",
) -> str:
    """Edit a Jupyter Notebook cell.

    Args:
        file_path: Path to the .ipynb file.
        cell_index: Zero-based index of the cell to edit.
        new_source: New source content for the cell. Required for
                    'replace' and 'insert' actions.
        cell_type: Cell type ('code', 'markdown', 'raw'). Only used
                   when inserting or changing type.
        action: One of 'replace', 'insert_before', 'insert_after', 'delete'.

    Returns:
        Confirmation message.
    """
    valid_actions = ("replace", "insert_before", "insert_after", "delete")
    if action not in valid_actions:
        raise ValueError(f"action must be one of {valid_actions}, got '{action}'")

    path = _ensure_within_sandbox(Path(file_path))
    if not path.exists():
        raise FileNotFoundError(f"Notebook not found: {file_path}")

    nb = json.loads(path.read_text(encoding="utf-8"))
    cells = nb.get("cells", [])

    if cell_index < 0 or cell_index >= len(cells):
        if action not in ("insert_before", "insert_after") or cell_index > len(cells):
            raise IndexError(
                f"cell_index {cell_index} out of range (notebook has {len(cells)} cells)"
            )

    def _make_cell(source: str, ctype: str) -> dict:
        cell = {
            "cell_type": ctype,
            "metadata": {},
            "source": source.splitlines(keepends=True),
        }
        if ctype == "code":
            cell["execution_count"] = None
            cell["outputs"] = []
        return cell

    resolved_type = cell_type or "code"

    if action == "delete":
        removed = cells.pop(cell_index)
        msg = f"Deleted cell {cell_index} (was {removed.get('cell_type', '?')})"

    elif action == "replace":
        if new_source is None:
            raise ValueError("new_source is required for 'replace' action")
        target = cells[cell_index]
        target["source"] = new_source.splitlines(keepends=True)
        if cell_type:
            target["cell_type"] = cell_type
            if cell_type == "code" and "outputs" not in target:
                target["outputs"] = []
                target["execution_count"] = None
        msg = f"Replaced cell {cell_index}"

    elif action == "insert_before":
        if new_source is None:
            raise ValueError("new_source is required for 'insert_before' action")
        cells.insert(cell_index, _make_cell(new_source, resolved_type))
        msg = f"Inserted {resolved_type} cell before index {cell_index}"

    elif action == "insert_after":
        if new_source is None:
            raise ValueError("new_source is required for 'insert_after' action")
        cells.insert(cell_index + 1, _make_cell(new_source, resolved_type))
        msg = f"Inserted {resolved_type} cell after index {cell_index}"

    nb["cells"] = cells
    path.write_text(json.dumps(nb, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return msg
