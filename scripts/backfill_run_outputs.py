#!/usr/bin/env python3
"""Backfill saved run JSON artifacts with observable LangSmith agent outputs.

This script does not start BitGN runs or trials. It only reads LangSmith root
traces that were already produced by earlier runs and writes local JSON files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langsmith import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import load_dotenv  # noqa: E402

DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "bitgn__ecom1-dev"

OUTPUT_FIELDS = (
    "completed",
    "completed_steps_laconic",
    "completion_output",
    "fallback",
    "grounding_refs",
    "message",
    "outcome",
    "protected_record_denial",
    "task_type",
)
LIST_FIELDS = {"completed_steps_laconic", "grounding_refs"}


@dataclass(frozen=True)
class TaskPreview:
    task_id: str
    text: str


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _chunks[T](items: list[T], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _case_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cases = payload.get("test_cases")
    if isinstance(cases, list):
        return [case for case in cases if isinstance(case, dict)]
    return []


def _trace_id_for_case(case: dict[str, Any]) -> str:
    return str(
        case.get("langsmith_run_id")
        or case.get("langsmith_trace_id")
        or case.get("trace_id")
        or ""
    ).strip()


def _run_id(run: Any) -> str:
    return str(getattr(run, "id", "") or "")


def _trace_id(run: Any) -> str:
    return str(getattr(run, "trace_id", "") or _run_id(run))


def _run_error(run: Any) -> str:
    return str(getattr(run, "error", "") or "")


def _normalize_text(value: str) -> str:
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = value.replace("\\|", "|")
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def parse_task_list(path: Path | None) -> list[TaskPreview]:
    if path is None or not path.exists():
        return []

    previews: list[TaskPreview] = []
    pattern = re.compile(r"^-\s+`(?P<task_id>t\d+)`\s+[—-]\s+(?P<text>.*)$")
    for line in path.read_text().splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        text = re.sub(r"\s+\(status:.*$", "", match.group("text")).strip()
        previews.append(TaskPreview(task_id=match.group("task_id"), text=text))
    return previews


def match_task_id(task_text: str, previews: list[TaskPreview]) -> str:
    if not previews:
        return ""

    candidates = _task_id_candidates(task_text, previews)
    if not candidates:
        return ""
    best_length = candidates[0][0]
    best = [task_id for length, task_id in candidates if length == best_length]
    return best[0] if len(best) == 1 else ""


def match_task_ids(task_texts: list[str], previews: list[TaskPreview]) -> list[str]:
    used: set[str] = set()
    result: list[str] = []
    for task_text in task_texts:
        task_id = ""
        for _, candidate in _task_id_candidates(task_text, previews):
            if candidate not in used:
                task_id = candidate
                break
        if task_id:
            used.add(task_id)
        result.append(task_id)
    return result


def _task_id_candidates(task_text: str, previews: list[TaskPreview]) -> list[tuple[int, str]]:
    normalized_task = _normalize_text(task_text)
    candidates: list[tuple[int, str]] = []
    for preview in previews:
        normalized_preview = _normalize_text(preview.text)
        if "..." in normalized_preview:
            normalized_preview = normalized_preview.split("...", 1)[0].strip()
        if len(normalized_preview) < 24 and normalized_task != normalized_preview:
            continue
        if normalized_task.startswith(normalized_preview):
            candidates.append((len(normalized_preview), preview.task_id))
    return sorted(candidates, key=lambda item: item[0], reverse=True)


def _fetch_runs_by_ids(client: Client, ids: list[str], project: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    unique_ids = sorted({run_id for run_id in ids if run_id})
    for chunk in _chunks(unique_ids, 100):
        runs = client.list_runs(
            project_name=project or None,
            run_ids=chunk,
            select=[
                "id",
                "trace_id",
                "start_time",
                "end_time",
                "inputs",
                "outputs",
                "error",
            ],
            limit=len(chunk),
        )
        for run in runs:
            result[_run_id(run)] = run
            result[_trace_id(run)] = run
    return result


def _fetch_runs_by_window(
    client: Client,
    *,
    project: str,
    started_at: datetime,
    finished_at: datetime,
    margin_seconds: int,
) -> list[Any]:
    window_start = (started_at - timedelta(seconds=margin_seconds)).astimezone(timezone.utc)
    window_end = (finished_at + timedelta(seconds=margin_seconds)).astimezone(timezone.utc)
    runs = client.list_runs(
        project_name=project or None,
        run_type="chain",
        is_root=True,
        start_time=window_start,
        select=[
            "id",
            "trace_id",
            "start_time",
            "end_time",
            "inputs",
            "outputs",
            "error",
        ],
        limit=100,
    )
    return sorted(
        [run for run in runs if window_start <= run.start_time <= window_end],
        key=lambda run: run.start_time,
    )


def _set_if_missing(
    case: dict[str, Any],
    key: str,
    value: Any,
    *,
    overwrite: bool,
) -> None:
    if value is None:
        return
    if key in LIST_FIELDS and not isinstance(value, list):
        return
    if overwrite or key not in case or case.get(key) in (None, "", []):
        case[key] = value


def merge_case_from_run(
    case: dict[str, Any],
    run: Any,
    *,
    overwrite: bool = False,
) -> bool:
    changed = False
    before = json.dumps(case, sort_keys=True, default=str)
    outputs = run.outputs or {}
    inputs = run.inputs or {}

    _set_if_missing(case, "task_text", inputs.get("task_text"), overwrite=overwrite)
    _set_if_missing(case, "trace_id", _trace_id(run), overwrite=overwrite)
    _set_if_missing(case, "langsmith_trace_id", _trace_id(run), overwrite=overwrite)
    _set_if_missing(case, "langsmith_run_id", _run_id(run), overwrite=overwrite)
    _set_if_missing(case, "error", _run_error(run), overwrite=overwrite)

    for field in OUTPUT_FIELDS:
        _set_if_missing(case, field, outputs.get(field), overwrite=overwrite)

    after = json.dumps(case, sort_keys=True, default=str)
    if after != before:
        changed = True
    return changed


def case_from_run(run: Any, *, task_id: str = "") -> dict[str, Any]:
    task_text = str((run.inputs or {}).get("task_text") or "")
    case: dict[str, Any] = {
        "task_id": task_id,
        "task_text": task_text,
        "trial_id": "",
        "trace_id": _trace_id(run),
        "langsmith_trace_id": _trace_id(run),
        "langsmith_run_id": _run_id(run),
        "score": None,
        "score_available": False,
        "state": "",
        "score_detail": [],
        "grader_comment": "",
        "error": _run_error(run),
    }
    merge_case_from_run(case, run, overwrite=True)
    return case


def backfill_payload(
    payload: dict[str, Any],
    *,
    client: Client,
    project: str,
    task_previews: list[TaskPreview],
    margin_seconds: int,
    overwrite: bool,
) -> tuple[dict[str, Any], int]:
    cases = _case_items(payload)
    changed_count = 0

    if cases:
        runs_by_id = _fetch_runs_by_ids(
            client,
            [_trace_id_for_case(case) for case in cases],
            project,
        )
        for case in cases:
            run = runs_by_id.get(_trace_id_for_case(case))
            if run is None:
                continue
            if merge_case_from_run(case, run, overwrite=overwrite):
                changed_count += 1
        changed_count += _fill_missing_task_ids(cases, task_previews)
        payload["test_cases"] = cases
        return payload, changed_count

    started_at = _parse_datetime(payload.get("started_at"))
    finished_at = _parse_datetime(payload.get("finished_at"))
    if started_at is None or finished_at is None:
        return payload, changed_count

    runs = _fetch_runs_by_window(
        client,
        project=project,
        started_at=started_at,
        finished_at=finished_at,
        margin_seconds=margin_seconds,
    )
    task_runs = [run for run in runs if (run.inputs or {}).get("task_text")]
    task_texts = [str((run.inputs or {}).get("task_text") or "") for run in task_runs]
    task_ids = match_task_ids(task_texts, task_previews)
    new_cases = [
        case_from_run(run, task_id=task_id)
        for run, task_id in zip(task_runs, task_ids, strict=True)
    ]
    payload["test_cases"] = new_cases
    return payload, len(new_cases)


def _fill_missing_task_ids(cases: list[dict[str, Any]], previews: list[TaskPreview]) -> int:
    if not previews:
        return 0

    used = {str(case.get("task_id") or "") for case in cases if case.get("task_id")}
    changed = 0
    for case in cases:
        if case.get("task_id"):
            continue
        task_text = str(case.get("task_text") or "")
        for _, candidate in _task_id_candidates(task_text, previews):
            if candidate in used:
                continue
            case["task_id"] = candidate
            used.add(candidate)
            changed += 1
            break
    return changed


def backfill_file(
    path: Path,
    *,
    client: Client,
    project: str,
    task_previews: list[TaskPreview],
    margin_seconds: int,
    overwrite: bool,
    dry_run: bool,
) -> tuple[int, int]:
    original_text = path.read_text()
    payload = json.loads(original_text)
    artifact_project = str(payload.get("langsmith_project") or project)
    payload, changed_count = backfill_payload(
        payload,
        client=client,
        project=artifact_project,
        task_previews=task_previews,
        margin_seconds=margin_seconds,
        overwrite=overwrite,
    )
    case_count = len(_case_items(payload))
    next_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not dry_run and next_text != original_text:
        path.write_text(next_text)
    return changed_count, case_count


def _run_paths(args: argparse.Namespace) -> list[Path]:
    if args.files:
        return [Path(file) for file in args.files]
    runs_dir = Path(args.runs_dir)
    return sorted(runs_dir.glob("run_*.json"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--project", default="")
    parser.add_argument("--task-list", type=Path)
    parser.add_argument("--margin-seconds", type=int, default=60)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    client = Client()
    task_previews = parse_task_list(args.task_list)
    project = args.project or ""

    total_changed = 0
    for path in _run_paths(args):
        changed_count, case_count = backfill_file(
            path,
            client=client,
            project=project,
            task_previews=task_previews,
            margin_seconds=args.margin_seconds,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        total_changed += changed_count
        action = "would update" if args.dry_run else "updated"
        print(f"{action} {path}: changed_cases={changed_count} total_cases={case_count}")

    if args.dry_run:
        print(f"dry run complete: changed_cases={total_changed}")


if __name__ == "__main__":
    main()
