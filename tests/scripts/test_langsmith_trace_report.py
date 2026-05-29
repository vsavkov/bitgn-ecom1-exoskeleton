from types import SimpleNamespace

from scripts.langsmith_trace_report import _parse_indices, _short, _tool_args, _tool_name


def test_short_handles_none_newlines_and_limit() -> None:
    assert _short(None, 10) == ""
    assert _short("a\nb", 10) == "a\\nb"
    assert _short("abcdef", 4) == "abc…"


def test_tool_helpers() -> None:
    assert _tool_args(SimpleNamespace(inputs={"args": {"path": "/docs"}})) == {
        "path": "/docs"
    }
    assert _tool_args(SimpleNamespace(inputs={"args": "bad"})) == {}
    assert _tool_name(SimpleNamespace(inputs={"tool": "read"}, name="fallback")) == "read"
    assert _tool_name(SimpleNamespace(inputs={}, name="fallback")) == "fallback"


def test_parse_indices_single_values_and_ranges() -> None:
    assert _parse_indices("1,3-5, 7") == {1, 3, 4, 5, 7}
