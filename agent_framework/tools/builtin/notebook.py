"""Notebook editing tool — insert/replace/delete cells in .ipynb files.

Operates on Jupyter notebooks as JSON. No external dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE


def _read_notebook(path: str) -> dict[str, Any]:
    """Read and parse a notebook file."""
    p = Path(path)
    if not p.suffix == ".ipynb":
        raise ValueError(f"Not a notebook file: {path}")
    if not p.is_file():
        raise FileNotFoundError(f"Notebook not found: {path}")
    return json.loads(p.read_text(encoding="utf-8"))


def _write_notebook(path: str, notebook: dict[str, Any]) -> None:
    """Write a notebook back to disk."""
    Path(path).write_text(
        json.dumps(notebook, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _find_cell_index(cells: list[dict], cell_id: str) -> int:
    """Find cell by ID. Falls back to index if cell_id is numeric."""
    # Try ID match
    for i, cell in enumerate(cells):
        if cell.get("id") == cell_id:
            return i
    # Fallback: treat as numeric index
    try:
        idx = int(cell_id)
        if 0 <= idx < len(cells):
            return idx
    except (ValueError, TypeError):
        pass
    raise ValueError(f"Cell not found: {cell_id}")


def _make_cell(
    cell_type: str,
    source: str,
    cell_id: str | None = None,
) -> dict[str, Any]:
    """Create a new notebook cell."""
    import uuid
    cell = {
        "cell_type": cell_type,
        "source": source.splitlines(keepends=True),
        "metadata": {},
        "id": cell_id or str(uuid.uuid4())[:8],
    }
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell


@tool(
    name="notebook_edit",
    description=(
        "Edit a Jupyter notebook (.ipynb) file. "
        "Supports inserting, replacing, and deleting cells."
    ),
    category="code_edit",
    require_confirm=True,
    tags=["notebook", "jupyter", "edit"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="edit jupyter notebook ipynb cells",
    activity_description="Editing notebook",
    prompt="Edit Jupyter notebook cells. Supports insert, replace, and delete operations.",
)
def notebook_edit(
    notebook_path: str,
    edit_mode: str,
    cell_type: str = "code",
    cell_id: str = "0",
    new_source: str = "",
) -> dict:
    """Edit a Jupyter notebook cell.

    Args:
        notebook_path: Absolute path to the .ipynb file.
        edit_mode: "insert" | "replace" | "delete"
        cell_type: "code" | "markdown" (for insert mode).
        cell_id: Cell ID or numeric index (0-based).
        new_source: New cell source content (for insert/replace).

    Returns:
        Dict with operation result.
    """
    try:
        nb = _read_notebook(notebook_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        return {"success": False, "error": str(e)}

    cells = nb.get("cells", [])

    if edit_mode == "insert":
        new_cell = _make_cell(cell_type, new_source)
        # Insert after the specified cell
        try:
            idx = _find_cell_index(cells, cell_id)
            cells.insert(idx + 1, new_cell)
        except ValueError:
            # If cell_id not found, append to end
            cells.append(new_cell)

        nb["cells"] = cells
        _write_notebook(notebook_path, nb)
        return {
            "success": True,
            "edit_mode": "insert",
            "cell_id": new_cell["id"],
            "cell_type": cell_type,
        }

    elif edit_mode == "replace":
        try:
            idx = _find_cell_index(cells, cell_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        cells[idx]["source"] = new_source.splitlines(keepends=True)
        if cell_type:
            cells[idx]["cell_type"] = cell_type
        # Clear outputs on code cell replacement
        if cells[idx].get("cell_type") == "code":
            cells[idx]["outputs"] = []
            cells[idx]["execution_count"] = None

        _write_notebook(notebook_path, nb)
        return {
            "success": True,
            "edit_mode": "replace",
            "cell_id": cell_id,
            "cell_type": cells[idx]["cell_type"],
        }

    elif edit_mode == "delete":
        try:
            idx = _find_cell_index(cells, cell_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        removed = cells.pop(idx)
        nb["cells"] = cells
        _write_notebook(notebook_path, nb)
        return {
            "success": True,
            "edit_mode": "delete",
            "cell_id": cell_id,
            "deleted_type": removed.get("cell_type"),
        }

    else:
        return {
            "success": False,
            "error": f"Unknown edit_mode: {edit_mode}. Use 'insert', 'replace', or 'delete'.",
        }
