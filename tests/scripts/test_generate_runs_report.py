from pathlib import Path

from scripts.generate_runs_report import (
    RunRecord,
    TestCase as RunTestCase,
    _case_items,
    _cell_title,
    _coerce_score,
    _format_score,
    _interpolate,
    _load_run,
    _load_runs,
    _natural_key,
    _parse_datetime,
    _render_html,
    _run_label,
    _score_color,
)


def test_runs_report_helpers(tmp_path: Path) -> None:
    assert _natural_key("t10") > _natural_key("t2")
    assert _parse_datetime("2026-05-29T10:00:00+00:00") is not None
    assert _parse_datetime("bad") is None
    assert _coerce_score("0.5") == 0.5
    assert _coerce_score("bad") is None
    assert _case_items({"test_cases": [{"task_id": "t01"}, "bad"]}) == [
        {"task_id": "t01"}
    ]
    assert _case_items({"tasks": [{"id": "t02"}]}) == [{"id": "t02"}]
    assert _interpolate((0, 0, 0), (10, 20, 30), 0.5) == (5, 10, 15)
    assert _score_color(None) == ("#f3f4f6", "#6b7280")
    assert _score_color(0)[0] == "#dc2626"
    assert _format_score(None) == "n/a"
    assert _format_score(1) == "1"
    assert _format_score(0.6) == "0.6"

    path = tmp_path / "run_20260529_100000.json"
    path.write_text(
        """
        {
          "started_at": "2026-05-29T10:00:00+00:00",
          "model_id": "gpt-test",
          "score": "0.5",
          "test_cases": [
            {
              "task_id": "t01",
              "task_text": "Task text",
              "score": 1,
              "langsmith_trace_id": "trace-1",
              "score_detail": ["detail"]
            }
          ]
        }
        """
    )

    record = _load_run(path)
    assert record.model_id == "gpt-test"
    assert record.cases["t01"] == RunTestCase(
        task_id="t01",
        task_text="Task text",
        score=1.0,
        trace_id="trace-1",
        comment="detail",
    )
    assert _run_label(record) == "05-29 10:00"
    assert "Task text" in _cell_title("t01", record, record.cases["t01"])
    fallback = RunRecord(
        path=tmp_path / "run_custom.json",
        started_at="",
        model_id="",
        score=None,
        cases={},
    )
    assert _run_label(fallback) == "custom"


def test_load_runs_sorts_by_started_at_and_render_html(tmp_path: Path) -> None:
    newer = tmp_path / "run_new.json"
    older = tmp_path / "run_old.json"
    older.write_text(
        '{"started_at":"2026-05-29T09:00:00+00:00","test_cases":[{"task_id":"t02","score":0}]}'
    )
    newer.write_text(
        '{"started_at":"2026-05-29T10:00:00+00:00","test_cases":[{"task_id":"t01","score":1}]}'
    )

    records = _load_runs(tmp_path)
    assert [record.path.name for record in records] == ["run_old.json", "run_new.json"]
    html = _render_html(records)
    assert "ECOM run scores" in html
    assert "t01" in html
    assert "sum" in html
    assert "No run_*.json" in _render_html([])
