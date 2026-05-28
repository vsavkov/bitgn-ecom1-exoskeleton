#!/usr/bin/env python3
"""Inspect ECOM Agent traces from LangSmith without starting BitGN runs."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langsmith import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_dotenv


@dataclass(frozen=True)
class RootRun:
    index: int
    run: Any


def _short(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\n", "\\n")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _tool_args(run: Any) -> dict[str, Any]:
    inputs = run.inputs or {}
    args = inputs.get("args")
    return args if isinstance(args, dict) else {}


def _tool_name(run: Any) -> str:
    inputs = run.inputs or {}
    return str(inputs.get("tool") or run.name)


def _load_roots(client: Client, project: str, limit: int) -> list[RootRun]:
    runs = list(
        client.list_runs(
            project_name=project,
            run_type="chain",
            is_root=True,
            limit=limit,
            select=["id", "start_time", "inputs", "outputs", "extra"],
        )
    )
    return [RootRun(i, run) for i, run in enumerate(sorted(runs, key=lambda r: r.start_time), 1)]


def _print_root(root: RootRun) -> None:
    run = root.run
    meta = (run.extra or {}).get("metadata") or {}
    instruction = (run.inputs or {}).get("task_text", "")
    outputs = run.outputs or {}
    print(
        f"{root.index:02d} {run.start_time.isoformat()} {run.id} "
        f"rev={meta.get('revision_id')}"
    )
    print(f"  task: {_short(instruction, 220)}")
    print(f"  outcome: {outputs.get('outcome')} message: {_short(outputs.get('message'), 220)}")
    refs = outputs.get("grounding_refs") or []
    if refs:
        print(f"  refs[{len(refs)}]: {_short(refs, 320)}")


def _print_children(client: Client, run_id: str, output_limit: int) -> None:
    run = client.read_run(run_id, load_child_runs=True)
    print("\nROOT")
    _print_root(RootRun(0, run))
    print("\nCHILDREN")
    for child in sorted(run.child_runs or [], key=lambda r: r.start_time):
        if child.run_type == "llm":
            outputs = child.outputs or {}
            print(f"- llm {child.start_time.isoformat()} id={child.id}")
            if outputs.get("output"):
                print(f"  output: {_short(outputs.get('output'), output_limit)}")
            continue

        if child.run_type != "tool":
            continue

        tool = _tool_name(child)
        args = _tool_args(child)
        print(f"- tool {tool} {child.start_time.isoformat()} id={child.id}")
        print(f"  args: {_short(json.dumps(args, ensure_ascii=False, sort_keys=True), output_limit)}")
        if child.outputs is not None:
            print(
                f"  outputs: {_short(json.dumps(child.outputs, ensure_ascii=False, sort_keys=True), output_limit)}"
            )


def _parse_indices(raw: str) -> set[int]:
    indices: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            indices.update(range(int(start), int(end) + 1))
        else:
            indices.add(int(part))
    return indices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="bitgn-ecom1")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--indices", help="Comma-separated root run indices, e.g. 3,6,14-16")
    parser.add_argument("--run-id", help="Inspect one root run by id")
    parser.add_argument("--children", action="store_true", help="Include child LLM/tool spans")
    parser.add_argument("--output-limit", type=int, default=900)
    args = parser.parse_args()

    load_dotenv()
    client = Client()

    if args.run_id:
        _print_children(client, args.run_id, args.output_limit)
        return

    roots = _load_roots(client, args.project, args.limit)
    selected = roots
    if args.indices:
        wanted = _parse_indices(args.indices)
        selected = [root for root in roots if root.index in wanted]

    for root in selected:
        _print_root(root)
        if args.children:
            _print_children(client, str(root.run.id), args.output_limit)
        print()


if __name__ == "__main__":
    main()
