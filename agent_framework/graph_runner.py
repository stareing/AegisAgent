"""Config-driven graph runner — load a JSON config and execute a StateGraph.

Usage::

    python -m agent_framework.graph_runner --config config/graph_example.json
    python -m agent_framework.graph_runner --config config/graph_conditional.json
    python -m agent_framework.graph_runner --config config/graph_example.json --task "Explain quantum computing"
    python -m agent_framework.graph_runner --config config/graph_example.json --stream

Config format:
    {
      "graph": {
        "name": "...",
        "state": { field: {type, default, reducer?} },
        "nodes": { name: {type, task_key, output_key, system_prompt?} },
        "edges": [ {from, to} ],
        "conditional_edges": [ {from, condition, mapping, default?} ],
        "conditions": { name: {field, contains, default} },
        "finish_nodes": ["..."],
        "entry_state": { ... }
      },
      "model": { ... },
      ...
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import operator
import os
import sys
from pathlib import Path
from typing import Annotated, Any

from typing_extensions import TypedDict


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively resolve ${VAR} references in config values."""
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        return os.environ.get(obj[2:-1], "")
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _build_state_schema(state_def: dict[str, dict]) -> type:
    """Build a TypedDict class from the state definition in config.

    Supports: str, int, float, bool, list, dict.
    Reducers: "add" → operator.add (for list accumulation).
    """
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}

    type_map = {
        "str": str, "int": int, "float": float,
        "bool": bool, "list": list, "dict": dict,
    }
    default_map = {
        "str": "", "int": 0, "float": 0.0,
        "bool": False, "list": [], "dict": {},
    }
    reducer_map = {
        "add": operator.add,
    }

    for field_name, field_spec in state_def.items():
        field_type = type_map.get(field_spec.get("type", "str"), str)
        reducer_name = field_spec.get("reducer")

        if reducer_name and reducer_name in reducer_map:
            annotations[field_name] = Annotated[field_type, reducer_map[reducer_name]]
        else:
            annotations[field_name] = field_type

        if "default" in field_spec:
            defaults[field_name] = field_spec["default"]
        else:
            defaults[field_name] = default_map.get(field_spec.get("type", "str"), None)

    # Create TypedDict dynamically using the functional form
    state_cls = TypedDict("GraphState", annotations)  # type: ignore[call-overload]
    return state_cls


def _build_condition(cond_spec: dict) -> Any:
    """Build a router function from a condition spec."""
    field = cond_spec["field"]
    contains_map = cond_spec.get("contains", {})
    default_val = cond_spec.get("default", "")

    def _router(state: dict) -> str:
        value = str(state.get(field, ""))
        for keyword, route_key in contains_map.items():
            if keyword in value:
                return route_key
        return default_val

    return _router


def _build_transform(transform_spec: dict) -> Any:
    """Build a passthrough transform from a spec."""
    field = transform_spec.get("field", "")
    output_key = transform_spec.get("output_key", "decision")

    def _transform(state: dict) -> dict:
        value = str(state.get(field, ""))
        # Extract last meaningful word
        words = value.strip().split()
        decision = words[-1] if words else ""
        return {output_key: decision}

    return _transform


def build_graph_from_config(config: dict) -> tuple[Any, dict]:
    """Build a compiled graph from config dict.

    Returns (compiled_graph, entry_state).
    """
    from agent_framework.entry import AgentFramework
    from agent_framework.graph import START, END, StateGraph
    from agent_framework.graph.nodes import agent_node, passthrough_node
    from agent_framework.infra.config import FrameworkConfig

    graph_cfg = config["graph"]

    # Build state schema
    state_schema = _build_state_schema(graph_cfg.get("state", {"task": {"type": "str"}}))

    # Build framework for agent nodes (shared model config)
    fw_config = {k: v for k, v in config.items() if k != "graph"}
    fw_config = _resolve_env_vars(fw_config)
    framework_config = FrameworkConfig(**fw_config)
    frameworks: dict[str, AgentFramework] = {}

    # Build graph
    graph = StateGraph(state_schema)
    conditions = graph_cfg.get("conditions", {})

    for node_name, node_spec in graph_cfg.get("nodes", {}).items():
        node_type = node_spec.get("type", "agent")

        if node_type == "agent":
            # Each agent node gets its own framework instance
            # with optional custom system_prompt
            fw = AgentFramework(config=framework_config)
            fw.setup()
            if node_spec.get("system_prompt"):
                fw._agent.agent_config.system_prompt = node_spec["system_prompt"]
            frameworks[node_name] = fw

            node_fn = agent_node(
                fw,
                task_key=node_spec.get("task_key", "task"),
                output_key=node_spec.get("output_key", "result"),
            )
            graph.add_node(node_name, node_fn)

        elif node_type == "passthrough":
            transform_name = node_spec.get("transform")
            if transform_name and transform_name in conditions:
                transform = _build_transform(conditions[transform_name])
            else:
                transform = None
            graph.add_node(node_name, passthrough_node(transform))

    # Direct edges
    for edge in graph_cfg.get("edges", []):
        source = edge["from"]
        target = edge["to"]
        graph.add_edge(source, target)

    # Conditional edges
    for cond_edge in graph_cfg.get("conditional_edges", []):
        source = cond_edge["from"]
        condition_name = cond_edge["condition"]
        mapping = cond_edge.get("mapping", {})
        default_route = cond_edge.get("default")

        if condition_name in conditions:
            router = _build_condition(conditions[condition_name])
        else:
            # Fallback: route by state field
            router = lambda state: state.get(condition_name, default_route or END)

        # If mapping provided, use add_conditional_edges
        graph.add_conditional_edges(source, router, mapping)

    # Finish nodes
    for finish_node in graph_cfg.get("finish_nodes", []):
        graph.add_edge(finish_node, END)

    # Compile
    from agent_framework.graph import InMemorySaver
    compiled = graph.compile(
        checkpointer=InMemorySaver(),
        name=graph_cfg.get("name", "ConfigGraph"),
    )

    entry_state = graph_cfg.get("entry_state", {})
    return compiled, entry_state, frameworks


async def run_graph(config_path: str, task_override: str | None = None, stream: bool = False) -> None:
    """Load config and execute the graph."""
    config_text = Path(config_path).read_text()
    config = json.loads(config_text)

    compiled, entry_state, frameworks = build_graph_from_config(config)

    # Override task if provided via CLI
    if task_override:
        for key in ("task", "query", "input"):
            if key in entry_state:
                entry_state[key] = task_override
                break
        else:
            entry_state["task"] = task_override

    # Display graph structure
    structure = compiled.get_graph_structure()
    print(f"\n{'='*60}")
    print(f"  Graph: {structure['name']}")
    print(f"  Nodes: {', '.join(structure['nodes'])}")
    print(f"  Edges: {len(structure['edges'])} direct"
          + (f", {len(structure.get('conditional_edges', []))} conditional" if structure.get('conditional_edges') else ""))
    print(f"{'='*60}\n")

    if stream:
        from agent_framework.graph import StreamMode
        print("  [streaming mode]\n")
        async for event in compiled.stream(
            entry_state,
            config={"configurable": {"thread_id": "graph_run_1"}},
            stream_mode=StreamMode.VALUES,
        ):
            node = event.node
            data = event.data
            print(f"  --- {node} ---")
            for key, val in data.items():
                preview = str(val)[:200]
                print(f"    {key}: {preview}")
            print()
    else:
        result = await compiled.invoke(
            entry_state,
            config={"configurable": {"thread_id": "graph_run_1"}},
        )

        print("  Result:")
        print(f"  {'─'*56}")
        for key, val in result.items():
            val_str = str(val)
            if len(val_str) > 300:
                val_str = val_str[:300] + "..."
            print(f"  {key}: {val_str}")
        print(f"  {'─'*56}")

    # Cleanup
    for fw in frameworks.values():
        await fw.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a config-driven graph pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m agent_framework.graph_runner --config config/graph_example.json
  python -m agent_framework.graph_runner --config config/graph_example.json --task "Explain AI"
  python -m agent_framework.graph_runner --config config/graph_example.json --stream
        """,
    )
    parser.add_argument("--config", required=True, help="Path to graph config JSON")
    parser.add_argument("--task", help="Override the entry task")
    parser.add_argument("--stream", action="store_true", help="Stream node outputs")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run_graph(args.config, task_override=args.task, stream=args.stream))


if __name__ == "__main__":
    main()
