from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

from agent import (
    ReportTaskCompletion,
    ReqList,
    ReqExec,
    ReqRead,
    ReqSearch,
    ReqTree,
    _child_runtime_path,
    _format_list_response,
    _format_exec_response,
    _format_read_response,
    _format_result,
    _format_search_response,
    _format_tree_response,
    _format_tree_entry,
    _function_call_output,
    _apply_availability_count_catalog_refs,
    _iter_tree_paths,
    _is_command_path,
    _is_truncated,
    _mark_truncated,
    _normalize_runtime_path,
    _output_text,
    _parse_tool_call,
    _remember_seen_tool_use,
    _render_command,
    _trace_agent_inputs,
    _trace_agent_outputs,
    _trace_cmd,
    _trace_dispatch_inputs,
    _trace_dispatch_outputs,
    _tree_followup_commands,
)


def test_path_helpers() -> None:
    assert _normalize_runtime_path("/") == "/"
    assert _normalize_runtime_path("docs/security.md") == "/docs/security.md"
    assert _child_runtime_path("/", "docs") == "/docs"
    assert _child_runtime_path("/proc", "baskets") == "/proc/baskets"


def test_render_and_truncation_helpers() -> None:
    result = SimpleNamespace(truncated=True)

    assert _render_command("date", "today") == "date\ntoday"
    assert _is_truncated(result)
    assert "[TRUNCATED: hint]" in _mark_truncated(result, "body", "hint")
    assert _mark_truncated(SimpleNamespace(truncated=False), "body", "hint") == "body"


def test_format_tree_entry() -> None:
    tree = SimpleNamespace(
        name="root",
        children=[
            SimpleNamespace(name="a", children=[]),
            SimpleNamespace(name="b", children=[SimpleNamespace(name="c", children=[])]),
        ],
    )

    assert _format_tree_entry(tree) == [
        "`-- root",
        "    |-- a",
        "    `-- b",
        "        `-- c",
    ]


def test_tree_path_iteration_and_followup_selection() -> None:
    tree = SimpleNamespace(
        name="/",
        kind=NodeKind.NODE_KIND_DIR,
        children=[
            SimpleNamespace(
                name="AGENTS.MD",
                kind=NodeKind.NODE_KIND_FILE,
                children=[],
            ),
            SimpleNamespace(
                name="bin",
                kind=NodeKind.NODE_KIND_DIR,
                children=[
                    SimpleNamespace(
                        name="date",
                        kind=NodeKind.NODE_KIND_FILE,
                        children=[],
                    ),
                    SimpleNamespace(
                        name="README.md",
                        kind=NodeKind.NODE_KIND_FILE,
                        children=[],
                    ),
                ],
            ),
        ],
    )
    result = SimpleNamespace(root=tree, truncated=False)
    seen_help: set[str] = set()
    seen_read: set[str] = set()

    assert [path for path, _ in _iter_tree_paths("/", tree)] == [
        "/",
        "/AGENTS.MD",
        "/bin",
        "/bin/date",
        "/bin/README.md",
    ]
    assert _is_command_path("/bin/date", tree.children[1].children[0])
    assert not _is_command_path("/bin/README.md", tree.children[1].children[1])

    followups = _tree_followup_commands(ReqTree(root="/"), result, seen_help, seen_read)
    assert followups == [
        ReqRead(path="/AGENTS.MD"),
        ReqRead(path="/bin/README.md"),
        ReqExec(path="/bin/date", args=["--help"]),
    ]
    assert seen_read == {"/AGENTS.MD", "/bin/README.md"}
    assert seen_help == {"/bin/date"}
    assert "tree -L 2 /" in _format_tree_response(ReqTree(root="/"), result)


def test_remember_seen_tool_use() -> None:
    seen_help: set[str] = set()
    seen_read: set[str] = set()

    _remember_seen_tool_use(ReqRead(path="docs/security.md"), seen_help, seen_read)
    _remember_seen_tool_use(ReqExec(path="bin/date", args=["--help"]), seen_help, seen_read)
    _remember_seen_tool_use(ReqExec(path="bin/date"), seen_help, seen_read)

    assert seen_read == {"/docs/security.md"}
    assert seen_help == {"/bin/date"}


def test_format_read_search_exec_and_json_results() -> None:
    read_result = SimpleNamespace(
        path="/docs/security.md",
        content="line1\nline2",
        content_type="text/markdown",
        sha256="abc",
        truncated=False,
    )
    search_result = SimpleNamespace(
        matches=[
            SimpleNamespace(
                path="/docs/security.md",
                line=1,
                line_text="security",
            )
        ],
        truncated=False,
    )
    exec_result = SimpleNamespace(stdout="ok\n", stderr="warn\n", exit_code=2)
    list_result = SimpleNamespace(
        entries=[
            SimpleNamespace(name="docs", kind=NodeKind.NODE_KIND_DIR),
            SimpleNamespace(name="AGENTS.MD", kind=NodeKind.NODE_KIND_FILE),
        ]
    )

    assert "docs/" in _format_list_response(ReqList(path="/"), list_result)
    assert "cat /docs/security.md" in _format_read_response(
        ReqRead(path="/docs/security.md"), read_result
    )
    assert "/docs/security.md:1:security" in _format_search_response(
        ReqSearch(pattern="security", root="/docs"), search_result
    )
    assert "[exit 2]" in _format_exec_response(ReqExec(path="/bin/date"), exec_result)
    assert _format_result(ReqTree(root="/"), {"ok": True}) == '{\n  "ok": true\n}'


def test_trace_helpers() -> None:
    cmd = ReqRead(path="/AGENTS.MD", number=True)

    assert _trace_cmd(cmd) == {
        "tool": "read",
        "args": {
            "path": "/AGENTS.MD",
            "number": True,
            "start_line": 0,
            "end_line": 0,
        },
    }
    assert _trace_dispatch_inputs({"cmd": cmd})["tool"] == "read"
    assert _trace_dispatch_inputs({"cmd": "raw"}) == {"tool": "str"}
    assert _trace_dispatch_outputs(None) == {}
    assert _trace_dispatch_outputs("raw") == {"output": "raw"}
    assert _trace_agent_inputs({"task_text": "task"}) == {"task_text": "task"}
    assert _trace_agent_outputs({"ok": True}) == {"ok": True}
    assert _trace_agent_outputs("raw") == {"output": "raw"}


def test_function_call_and_output_text_helpers() -> None:
    tool_call = SimpleNamespace(call_id="call_1")
    response = SimpleNamespace(
        output=[
            SimpleNamespace(type="reasoning"),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(type="output_text", text="hello"),
                    SimpleNamespace(type="other", text="ignored"),
                ],
            ),
        ]
    )

    assert _function_call_output(tool_call, "done") == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "done",
    }
    assert _output_text(response) == "hello"


def test_apply_availability_count_catalog_refs_replaces_catalog_refs_only() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["counted qualifying products"],
        task_type="availability_count",
        message="1 products",
        grounding_doc_refs=["/docs/catalogue.md"],
        grounding_row_refs=[
            "/proc/catalog/Brand/WRONG.json",
            "/proc/stores/store_vienna_praterstern.json",
        ],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_availability_count_catalog_refs(
        cmd,
        [
            "/proc/stores/store_vienna_praterstern.json",
            "/proc/catalog/Brand/RIGHT.json",
        ],
    )

    assert updated.grounding_row_refs == [
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/catalog/Brand/RIGHT.json",
    ]

    unchanged = _apply_availability_count_catalog_refs(
        cmd,
        ["/proc/stores/store_wrong.json"],
    )
    assert unchanged.grounding_row_refs == cmd.grounding_row_refs


def test_parse_tool_call() -> None:
    parsed = _parse_tool_call(
        SimpleNamespace(name="read", arguments='{"path":"/AGENTS.MD","number":true}')
    )

    assert parsed == ReqRead(path="/AGENTS.MD", number=True)
    try:
        _parse_tool_call(SimpleNamespace(name="unknown", arguments="{}"))
    except ValueError as exc:
        assert "Unknown tool" in str(exc)
    else:
        raise AssertionError("expected ValueError")
