#!/usr/bin/env python3
"""Generate an HTML score heatmap from saved benchmark run artifacts."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs"
DEFAULT_OUTPUT = PROJECT_ROOT / "runs.html"


@dataclass(frozen=True)
class TestCase:
    task_id: str
    score: float | None
    trace_id: str | None
    comment: str


@dataclass(frozen=True)
class RunRecord:
    path: Path
    started_at: str
    model_id: str
    score: float | None
    cases: dict[str, TestCase]


def _natural_key(value: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _coerce_score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _case_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cases = payload.get("test_cases")
    if isinstance(cases, list):
        return [case for case in cases if isinstance(case, dict)]

    legacy_tasks = payload.get("tasks")
    if isinstance(legacy_tasks, list):
        return [case for case in legacy_tasks if isinstance(case, dict)]

    return []


def _load_run(path: Path) -> RunRecord:
    payload = json.loads(path.read_text())
    started_at = str(payload.get("started_at") or "")
    cases: dict[str, TestCase] = {}

    for raw_case in _case_items(payload):
        task_id = str(raw_case.get("task_id") or raw_case.get("id") or "").strip()
        if not task_id:
            continue

        score = _coerce_score(raw_case.get("score"))
        detail = raw_case.get("score_detail")
        if isinstance(detail, list):
            detail_text = "\n".join(str(item) for item in detail)
        else:
            detail_text = str(detail or "")

        comment = str(raw_case.get("grader_comment") or "").strip()
        if not comment:
            comment = detail_text.strip()

        trace_id = raw_case.get("trace_id") or raw_case.get("langsmith_trace_id")
        cases[task_id] = TestCase(
            task_id=task_id,
            score=score,
            trace_id=str(trace_id) if trace_id else None,
            comment=comment,
        )

    return RunRecord(
        path=path,
        started_at=started_at,
        model_id=str(payload.get("model_id") or ""),
        score=_coerce_score(payload.get("score")),
        cases=cases,
    )


def _load_runs(runs_dir: Path) -> list[RunRecord]:
    records = [_load_run(path) for path in sorted(runs_dir.glob("run_*.json"))]
    return sorted(
        records,
        key=lambda record: (
            _parse_datetime(record.started_at) or datetime.min,
            record.path.name,
        ),
    )


def _interpolate(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))


def _score_color(score: float | None) -> tuple[str, str]:
    if score is None:
        return "#f3f4f6", "#6b7280"

    value = max(0.0, min(1.0, score))
    red = (220, 38, 38)
    amber = (245, 158, 11)
    green = (22, 163, 74)
    if value < 0.5:
        rgb = _interpolate(red, amber, value / 0.5)
    else:
        rgb = _interpolate(amber, green, (value - 0.5) / 0.5)

    background = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    luminance = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
    text = "#111827" if luminance > 0.58 else "#ffffff"
    return background, text


def _format_score(score: float | None) -> str:
    if score is None:
        return "n/a"
    if score == 0 or score == 1:
        return str(int(score))
    return f"{score:.2f}".rstrip("0").rstrip(".")


def _run_label(record: RunRecord) -> str:
    parsed = _parse_datetime(record.started_at)
    if parsed:
        return parsed.strftime("%m-%d %H:%M")
    return record.path.stem.removeprefix("run_")


def _cell_title(task_id: str, record: RunRecord, case: TestCase | None) -> str:
    lines = [f"{task_id} / {record.path.name}"]
    if case:
        lines.append(f"score: {_format_score(case.score)}")
        if case.trace_id:
            lines.append(f"trace: {case.trace_id}")
        if case.comment:
            lines.append(case.comment)
    else:
        lines.append("score: n/a")
    return "\n".join(lines)


def _render_html(records: list[RunRecord]) -> str:
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    task_ids = sorted({task_id for record in records for task_id in record.cases}, key=_natural_key)
    totals = [sum(case.score or 0 for case in record.cases.values()) for record in records]

    colgroup = ["<col class=\"task-col\">"] + ["<col class=\"run-col\">" for _ in records]
    header_cells = ["<th class=\"task-head\">task</th>"]
    for record in records:
        label = escape(_run_label(record))
        model = escape(record.model_id)
        total = escape(_format_score(record.score))
        header_cells.append(
            "<th>"
            f"<div class=\"run-label\">{label}</div>"
            f"<div class=\"run-score\">score {total}</div>"
            f"<div class=\"run-model\" title=\"{model}\">{model}</div>"
            "</th>"
        )

    body_rows = []
    for task_id in task_ids:
        cells = [f"<th class=\"task-id\">{escape(task_id)}</th>"]
        for record in records:
            case = record.cases.get(task_id)
            score = case.score if case else None
            background, text = _score_color(score)
            title = escape(_cell_title(task_id, record, case), quote=True)
            cells.append(
                "<td "
                f"class=\"score-cell\" title=\"{title}\" "
                f"style=\"background:{background};color:{text}\">"
                f"{escape(_format_score(score))}"
                "</td>"
            )
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    footer_cells = ["<th class=\"task-id\">sum</th>"]
    for total in totals:
        footer_cells.append(f"<td class=\"sum-cell\">{escape(_format_score(total))}</td>")

    if not records:
        body_rows.append("<tr><td class=\"empty\">No run_*.json files found in runs/.</td></tr>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ECOM run scores</title>
<style>
:root {{
  color-scheme: light;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f8fafc;
  color: #111827;
}}
body {{
  margin: 0;
  padding: 24px;
}}
h1 {{
  margin: 0 0 6px;
  font-size: 24px;
  font-weight: 700;
  letter-spacing: 0;
}}
.meta {{
  margin: 0 0 18px;
  color: #4b5563;
  font-size: 13px;
}}
.table-wrap {{
  max-width: 100%;
  overflow: auto;
  border: 1px solid #d1d5db;
  background: #ffffff;
}}
table {{
  border-collapse: separate;
  border-spacing: 0;
  min-width: max-content;
}}
col.task-col {{
  width: 88px;
}}
col.run-col {{
  width: 112px;
}}
th, td {{
  border-right: 1px solid #d1d5db;
  border-bottom: 1px solid #d1d5db;
  padding: 8px 10px;
  font-size: 13px;
  text-align: center;
  white-space: nowrap;
}}
thead th {{
  position: sticky;
  top: 0;
  z-index: 2;
  background: #e5e7eb;
  vertical-align: bottom;
}}
.task-head, .task-id {{
  position: sticky;
  left: 0;
  z-index: 3;
  background: #f3f4f6;
  text-align: left;
  font-weight: 700;
}}
thead .task-head {{
  z-index: 4;
  background: #e5e7eb;
}}
.run-label {{
  font-weight: 700;
}}
.run-score {{
  margin-top: 3px;
  color: #374151;
  font-size: 12px;
}}
.run-model {{
  max-width: 96px;
  margin-top: 3px;
  overflow: hidden;
  color: #6b7280;
  font-size: 11px;
  text-overflow: ellipsis;
}}
.score-cell {{
  font-variant-numeric: tabular-nums;
  font-weight: 700;
}}
tfoot th, tfoot td {{
  position: sticky;
  bottom: 0;
  background: #111827;
  color: #ffffff;
  font-weight: 700;
}}
tfoot .task-id {{
  z-index: 3;
  background: #111827;
  color: #ffffff;
}}
.empty {{
  padding: 20px;
  color: #6b7280;
  text-align: left;
}}
</style>
</head>
<body>
<h1>ECOM run scores</h1>
<p class="meta">Generated {escape(generated_at)} from {len(records)} run file(s), {len(task_ids)} task(s).</p>
<div class="table-wrap">
<table>
<colgroup>{''.join(colgroup)}</colgroup>
<thead><tr>{''.join(header_cells)}</tr></thead>
<tbody>
{''.join(body_rows)}
</tbody>
<tfoot><tr>{''.join(footer_cells)}</tr></tfoot>
</table>
</div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    records = _load_runs(args.runs_dir)
    html = _render_html(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html)
    print(f"wrote {args.output} from {len(records)} run file(s)")


if __name__ == "__main__":
    main()
