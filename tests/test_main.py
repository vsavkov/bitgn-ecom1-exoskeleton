import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import main as benchmark_main
from config import CLI_CLR
from main import (
    RUNS_DIR,
    _benchmark_runs_dir,
    _benchmark_runs_name,
    _chunks,
    _color,
    _enum_name,
    _format_task_report,
    _run_artifact_path,
    _write_run_artifact,
)


def test_color_and_chunks() -> None:
    assert _color("text", "\x1b[31m") == f"\x1b[31mtext{CLI_CLR}"
    assert list(_chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_format_task_report_includes_completion_errors_and_skip() -> None:
    report = _format_task_report(
        {
            "task_id": "t01",
            "instruction": "Do the thing",
            "formatter_output": ["FORMAT: old -> new"],
            "completion_output": "agent OUTCOME_OK",
            "error": "boom",
            "end_trial_error": "end boom",
            "skipped": True,
        }
    )

    assert "Task: t01" in report
    assert "Do the thing" in report
    assert "FORMAT: old -> new" in report
    assert "agent OUTCOME_OK" in report
    assert "ERROR: boom" in report
    assert "END TRIAL ERROR: end boom" in report
    assert "Skipped by task filter." in report


def test_enum_name_uses_proto_name_or_raw_value() -> None:
    enum_type = SimpleNamespace(Name=lambda value: "KNOWN" if value == 1 else (_ for _ in ()).throw(ValueError))

    assert _enum_name(enum_type, 1) == "KNOWN"
    assert _enum_name(enum_type, 2) == "2"


def test_run_artifact_path_avoids_collisions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(benchmark_main, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(benchmark_main, "BENCH_ID", "bitgn/ecom1-dev")
    started = datetime(2026, 5, 29, 10, 0, 0)

    first = _run_artifact_path(started)
    assert first == tmp_path / "bitgn__ecom1-dev" / "run_20260529_100000.json"
    first.write_text("{}")
    assert _run_artifact_path(started) == (
        tmp_path / "bitgn__ecom1-dev" / "run_20260529_100000_02.json"
    )


def test_benchmark_runs_dir_uses_sanitized_bench_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(benchmark_main, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(benchmark_main, "BENCH_ID", "bitgn/ecom1-prod")

    assert _benchmark_runs_name("bitgn/ecom1-prod") == "bitgn__ecom1-prod"
    assert _benchmark_runs_dir() == tmp_path / "bitgn__ecom1-prod"


def test_write_run_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(benchmark_main, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(benchmark_main, "BENCH_ID", "bench")
    monkeypatch.setattr(benchmark_main, "MODEL_ID", "model")
    monkeypatch.setenv("LANGSMITH_PROJECT", "project")
    started = datetime(2026, 5, 29, 10, 0, 0).astimezone()
    trial = SimpleNamespace(
        task_id="t01",
        trial_id="trial-1",
        score_detail=["missing ref"],
        score=0,
        score_available=True,
        state=1,
        error="",
    )
    result = SimpleNamespace(
        trials=[trial],
        run_id="run-1",
        state=1,
        score=0,
        score_available=True,
    )

    path = _write_run_artifact(
        result,
        started,
        {
            "trial-1": {
                "instruction": "Task text",
                "langsmith_trace_id": "trace-1",
                "langsmith_run_id": "run-trace-1",
                "outcome": "OUTCOME_OK",
                "task_type": "catalog_lookup",
                "message": "PT-ABC-123",
                "grounding_refs": ["/proc/catalog/PT-ABC-123.json"],
                "completed_steps_laconic": ["Resolved the SKU."],
            }
        },
    )

    text = path.read_text()
    assert path.parent == tmp_path / "bench"
    assert '"task_text": "Task text"' in text
    assert '"grader_comment": "missing ref"' in text
    assert '"langsmith_trace_id": "trace-1"' in text
    assert '"message": "PT-ABC-123"' in text
    assert '"grounding_refs": [' in text
    assert RUNS_DIR != tmp_path


def test_write_run_artifact_keeps_local_outputs_when_scores_are_sealed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(benchmark_main, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(benchmark_main, "BENCH_ID", "bitgn/ecom1-prod")
    monkeypatch.setattr(benchmark_main, "MODEL_ID", "model")
    started = datetime(2026, 5, 30, 10, 0, 0).astimezone()
    result = SimpleNamespace(
        trials=[],
        run_id="sealed-run",
        state=1,
        score=0,
        score_available=False,
    )

    path = _write_run_artifact(
        result,
        started,
        {
            "trial-a": {
                "task_id": "t002",
                "instruction": "Second task",
                "langsmith_trace_id": "trace-2",
                "outcome": "OUTCOME_OK",
                "message": "<YES>",
                "grounding_refs": ["/proc/catalog/PT-YES.json"],
            },
            "trial-b": {
                "task_id": "t001",
                "instruction": "First task",
                "error": "boom",
            },
        },
    )

    assert path.parent == tmp_path / "bitgn__ecom1-prod"
    payload = json.loads(path.read_text())
    assert payload["score_available"] is False
    assert [case["task_id"] for case in payload["test_cases"]] == ["t001", "t002"]
    assert payload["test_cases"][0]["score"] is None
    assert payload["test_cases"][0]["error"] == "boom"
    assert payload["test_cases"][1]["langsmith_trace_id"] == "trace-2"
    assert payload["test_cases"][1]["message"] == "<YES>"


def test_write_run_artifact_can_include_(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(benchmark_main, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(benchmark_main, "BENCH_ID", "bench")
    monkeypatch.setattr(benchmark_main, "MODEL_ID", "model")
    monkeypatch.setenv("", "1")
    started = datetime(2026, 5, 30, 10, 0, 0).astimezone()
    trial = SimpleNamespace(
        task_id="t01",
        trial_id="trial-1",
        score_detail=[],
        score=1,
        score_available=True,
        state=1,
        error="",
    )
    result = SimpleNamespace(
        trials=[trial],
        run_id="run-1",
        state=1,
        score=1,
        score_available=True,
    )

    path = _write_run_artifact(
        result,
        started,
        {
            "trial-1": {
                "instruction": "Return the SKU only",
                "outcome": "OUTCOME_OK",
                "message": "PT-ABC-123",
            }
        },
    )

    payload = json.loads(path.read_text())
    assert payload[""]["scored_case_count"] == 1
    assert payload["test_cases"][0][""]["score"] == 1.0
