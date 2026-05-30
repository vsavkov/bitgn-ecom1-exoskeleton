from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.backfill_run_outputs import (
    case_from_run,
    match_task_id,
    match_task_ids,
    merge_case_from_run,
    parse_task_list,
)


def _run(**kwargs):
    defaults = {
        "id": "run-1",
        "trace_id": "trace-1",
        "start_time": datetime(2026, 5, 30, 8, 10, tzinfo=timezone.utc),
        "inputs": {"task_text": "sku for makita dhs680 body level saw, no batteries pls. sku only."},
        "outputs": {
            "completed": True,
            "outcome": "OUTCOME_OK",
            "message": "PT-SAW-MAK-DHS680-BODY",
            "grounding_refs": ["/proc/catalog/Makita/PT-SAW-MAK-DHS680-BODY.json"],
            "completed_steps_laconic": ["Resolved the SKU."],
        },
        "error": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_parse_task_list_and_match_truncated_preview(tmp_path: Path) -> None:
    task_list = tmp_path / "tasks.md"
    task_list.write_text(
        "- `t001` — sku for makita dhs680 body level saw, no batteries pls. sku only. "
        "(status: `done`, time: `16.2s`, value: `50`, note: `-`)\n"
        "- `t002` — At PowerTools, how many of these SKUs have at least 2 units: PT-ABC... "
        "(status: `done`, time: `-`, value: `-`, note: `-`)\n"
    )

    previews = parse_task_list(task_list)

    assert [preview.task_id for preview in previews] == ["t001", "t002"]
    assert match_task_id(
        "At PowerTools, how many of these SKUs have at least 2 units: PT-ABC, PT-DEF",
        previews,
    ) == "t002"


def test_match_task_ids_assigns_repeated_truncated_previews(tmp_path: Path) -> None:
    task_list = tmp_path / "tasks.md"
    task_list.write_text(
        "- `t015` — Risk Ops is reviewing a two-year-old archive export. Read /archi... (status: `done`)\n"
        "- `t035` — Risk Ops is reviewing a two-year-old archive export. Read /archi... (status: `done`)\n"
    )
    previews = parse_task_list(task_list)

    assert match_task_ids(
        [
            "Risk Ops is reviewing a two-year-old archive export. Read /archive/a.tsv.",
            "Risk Ops is reviewing a two-year-old archive export. Read /archive/b.tsv.",
        ],
        previews,
    ) == ["t015", "t035"]


def test_merge_case_from_run_fills_observable_output_fields() -> None:
    case = {"task_id": "t001", "message": ""}

    changed = merge_case_from_run(case, _run())

    assert changed is True
    assert case["task_text"].startswith("sku for makita")
    assert case["outcome"] == "OUTCOME_OK"
    assert case["message"] == "PT-SAW-MAK-DHS680-BODY"
    assert case["grounding_refs"] == ["/proc/catalog/Makita/PT-SAW-MAK-DHS680-BODY.json"]
    assert case["langsmith_run_id"] == "run-1"


def test_case_from_run_creates_sealed_prod_case_shape() -> None:
    case = case_from_run(_run(), task_id="t001")

    assert case["task_id"] == "t001"
    assert case["score"] is None
    assert case["score_available"] is False
    assert case["outcome"] == "OUTCOME_OK"
