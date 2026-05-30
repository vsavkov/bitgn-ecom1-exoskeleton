import json
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from agent import (
    ReportTaskCompletion,
    ReqList,
    ReqResolveCatalogItems,
    ReqExec,
    ReqRead,
    ReqSearch,
    ReqTree,
    _child_runtime_path,
    _apply_archive_fraud_result,
    _apply_receipt_price_result,
    _apply_city_availability_result,
    _apply_catalog_availability_lookup_refs,
    _apply_catalog_lookup_result,
    _apply_discount_cap_message,
    _apply_payment_recovery_review,
    _apply_payment_recovery_closed_retry_outcome,
    _apply_payment_recovery_retry_timestamp,
    _apply_verified_manager_refs,
    _auto_followup_timeout_ms,
    _format_list_response,
    _format_exec_response,
    _format_read_response,
    _format_result,
    _format_search_response,
    _format_followup_error,
    _format_tree_response,
    _format_tree_entry,
    _function_call_output,
    _function_call_output_for_call_id,
    _apply_availability_count_catalog_refs,
    _apply_support_note_catalog_refs,
    _append_synthetic_named_pair,
    _append_synthetic_tool_pair,
    _iter_tree_paths,
    _is_command_path,
    _is_markdown_path,
    _is_truncated,
    _mark_truncated,
    _normalize_runtime_path,
    _output_text,
    _parse_tool_call,
    _remember_seen_tool_use,
    _normalize_catalog_resolution_for_task,
    _render_command,
    _should_preflight_payment_fraud_history,
    _synthetic_function_call,
    _synthetic_named_call,
    _task_has_explicit_archive_export,
    _trace_agent_inputs,
    _trace_agent_outputs,
    _trace_cmd,
    _trace_dispatch_inputs,
    _trace_dispatch_outputs,
    _tree_followup_commands,
    _tomorrow_date_preflight,
)
from payment_recovery_review import PaymentRecoveryReview
from catalog_tools import CatalogLookupItem


class ReadOnlyVM:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read(self, request):
        return SimpleNamespace(content=self.files.get(request.path, ""))


def test_path_helpers() -> None:
    assert _normalize_runtime_path("/") == "/"
    assert _normalize_runtime_path("docs/security.md") == "/docs/security.md"
    assert _child_runtime_path("/", "docs") == "/docs"
    assert _child_runtime_path("/proc", "baskets") == "/proc/baskets"


def test_payment_fraud_history_preflight_detection() -> None:
    assert _should_preflight_payment_fraud_history(
        "Fraud review says one hit is present in the archived payments. "
        "Identify the fraudulent payment records from history."
    )
    assert _should_preflight_payment_fraud_history(
        "Risk Ops confirmed a known fraud hit in the older archived payment history."
    )
    assert not _should_preflight_payment_fraud_history(
        "Read /archive/payment_batch_export.tsv and identify fraud rows."
    )
    assert _task_has_explicit_archive_export(
        "Read /archive/payment_batch_export.tsv and identify fraud rows."
    )


def test_catalog_resolution_predicate_normalizes_unavailable_count_task() -> None:
    cmd = ReqResolveCatalogItems(
        items=[
            CatalogLookupItem(
                description="the Cleaning Machine from Karcher",
                requested_quantity=1,
            )
        ],
        store_id="store_brno_veveri",
        availability_predicate="at_least",
    )

    updated = _normalize_catalog_resolution_for_task(
        cmd,
        task_text="how many of these just are not available today?",
    )

    assert updated.availability_predicate == "below"


def test_catalog_resolution_predicate_keeps_positive_availability_task() -> None:
    cmd = ReqResolveCatalogItems(
        items=[
            CatalogLookupItem(
                description="the Cleaning Machine from Karcher",
                requested_quantity=1,
            )
        ],
        store_id="store_brno_veveri",
        availability_predicate="at_least",
    )

    updated = _normalize_catalog_resolution_for_task(
        cmd,
        task_text="how many of these have at least 1 available today?",
    )

    assert updated.availability_predicate == "at_least"


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
                        name="sql",
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
            SimpleNamespace(
                name="docs",
                kind=NodeKind.NODE_KIND_DIR,
                children=[
                    SimpleNamespace(
                        name="security.md",
                        kind=NodeKind.NODE_KIND_FILE,
                        children=[],
                    ),
                    SimpleNamespace(
                        name="policy.MD",
                        kind=NodeKind.NODE_KIND_FILE,
                        children=[],
                    ),
                    SimpleNamespace(
                        name="notes.txt",
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
        "/bin/sql",
        "/bin/README.md",
        "/docs",
        "/docs/security.md",
        "/docs/policy.MD",
        "/docs/notes.txt",
    ]
    assert _is_command_path("/bin/date", tree.children[1].children[0])
    assert _is_command_path("/bin/sql", tree.children[1].children[1])
    assert not _is_command_path("/bin/README.md", tree.children[1].children[2])
    assert _is_markdown_path("/docs/security.md", tree.children[2].children[0])
    assert _is_markdown_path("/docs/policy.MD", tree.children[2].children[1])
    assert not _is_markdown_path("/docs/notes.txt", tree.children[2].children[2])

    followups = _tree_followup_commands(ReqTree(root="/"), result, seen_help, seen_read)
    assert followups == [
        ReqRead(path="/AGENTS.MD"),
        ReqRead(path="/bin/README.md"),
        ReqRead(path="/docs/security.md"),
        ReqRead(path="/docs/policy.MD"),
        ReqExec(path="/bin/date", args=["--help"]),
        ReqExec(path="/bin/sql", args=["--help"]),
    ]
    assert seen_read == {
        "/AGENTS.MD",
        "/bin/README.md",
        "/docs/security.md",
        "/docs/policy.MD",
    }
    assert seen_help == {"/bin/date", "/bin/sql"}
    assert _tree_followup_commands(
        ReqTree(root="/", auto_followups=False),
        result,
        set(),
        set(),
    ) == []
    assert "tree -L 2 /" in _format_tree_response(ReqTree(root="/"), result)


def test_remember_seen_tool_use() -> None:
    seen_help: set[str] = set()
    seen_read: set[str] = set()

    _remember_seen_tool_use(ReqRead(path="docs/security.md"), seen_help, seen_read)
    _remember_seen_tool_use(ReqExec(path="bin/date", args=["--help"]), seen_help, seen_read)
    _remember_seen_tool_use(ReqExec(path="bin/date"), seen_help, seen_read)

    assert seen_read == {"/docs/security.md"}
    assert seen_help == {"/bin/date"}


def test_auto_help_timeout_and_error_formatting(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_AUTO_HELP_TIMEOUT_MS", raising=False)
    exc = ConnectError(Code.DEADLINE_EXCEEDED, "timed out")

    assert _auto_followup_timeout_ms(ReqExec(path="/bin/sql", args=["--help"])) == 300
    assert _auto_followup_timeout_ms(ReqExec(path="/bin/sql")) is None
    assert _auto_followup_timeout_ms(ReqRead(path="/docs/security.md")) is None
    assert _format_followup_error(ReqExec(path="/bin/sql", args=["--help"]), exc).startswith(
        "/bin/sql --help\n[AUTO-FOLLOWUP ERROR: deadline_exceeded: timed out]"
    )


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
    assert _function_call_output_for_call_id("call_2", "done") == {
        "type": "function_call_output",
        "call_id": "call_2",
        "output": "done",
    }
    assert _output_text(response) == "hello"


def test_synthetic_named_call_does_not_require_registered_model() -> None:
    context: list = []
    call = _synthetic_named_call(
        "resolve_basket_selector",
        {"selector": "newest"},
        "call_auto_99",
    )

    assert call["name"] == "resolve_basket_selector"
    assert call["call_id"] == "call_auto_99"
    assert call["id"] == "fc_call_auto_99"
    assert json.loads(call["arguments"]) == {"selector": "newest"}

    _append_synthetic_named_pair(
        context,
        name="resolve_basket_selector",
        arguments={"selector": "oldest"},
        output='{"selected_basket_id": "basket_1"}',
        call_id="call_auto_100",
    )

    assert context[0]["name"] == "resolve_basket_selector"
    assert context[1] == {
        "type": "function_call_output",
        "call_id": "call_auto_100",
        "output": '{"selected_basket_id": "basket_1"}',
    }


def test_synthetic_tool_pair_helpers() -> None:
    cmd = ReqRead(path="/AGENTS.MD")
    call = _synthetic_function_call(cmd, "call_auto_1")

    assert call["type"] == "function_call"
    assert call["id"] == "fc_call_auto_1"
    assert call["call_id"] == "call_auto_1"
    assert call["name"] == "read"
    assert json.loads(call["arguments"]) == {
        "path": "/AGENTS.MD",
        "number": False,
        "start_line": 0,
        "end_line": 0,
    }
    assert call["status"] == "completed"

    context = []
    _append_synthetic_tool_pair(context, cmd, "file body", "call_auto_1")

    assert context == [
        call,
        {
            "type": "function_call_output",
            "call_id": "call_auto_1",
            "output": "file body",
        },
    ]


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

    store_only_update = _apply_availability_count_catalog_refs(
        cmd,
        ["/proc/stores/store_wrong.json"],
    )
    assert store_only_update.grounding_row_refs == ["/proc/stores/store_wrong.json"]


def test_apply_availability_count_catalog_refs_adds_missing_store_ref() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["counted qualifying products"],
        task_type="availability_count",
        message="1 products",
        grounding_doc_refs=[],
        grounding_row_refs=["/proc/catalog/Brand/WRONG.json"],
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


def test_apply_support_note_catalog_refs_replaces_catalog_refs_only() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["checked support note"],
        task_type="catalog_lookup",
        message="<NO> Checked SKU: STO-2R84BSHQ",
        grounding_doc_refs=[],
        grounding_row_refs=[
            "/proc/catalog/STO-12JLHT7D.json",
            "/proc/stores/store_vienna_praterstern.json",
        ],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_support_note_catalog_refs(
        cmd,
        ["/proc/catalog/STO-2R84BSHQ.json"],
    )

    assert updated.grounding_row_refs == [
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/catalog/STO-2R84BSHQ.json",
    ]


def test_apply_verified_manager_refs_adds_tool_evidence() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["verified manager"],
        task_type="discount",
        message="Verified.",
        grounding_doc_refs=["/docs/security.md"],
        grounding_row_refs=["/proc/baskets/basket_001.json"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_verified_manager_refs(
        cmd,
        [
            "/proc/stores/store_vienna_praterstern.json",
            "/proc/baskets/basket_001.json",
        ],
    )

    assert updated.grounding_row_refs == [
        "/proc/baskets/basket_001.json",
        "/proc/stores/store_vienna_praterstern.json",
    ]


def test_apply_archive_fraud_result_sets_message_and_refs() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["analyzed archive fraud"],
        task_type="fraud_review",
        message="old",
        grounding_doc_refs=[],
        grounding_row_refs=["/archive/payments.tsv#row=existing"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_archive_fraud_result(
        cmd,
        total_message="EUR 12.34",
        refs_to_submit=[
            "/archive/payments.tsv#row=R1",
            "/archive/payments.tsv#row=existing",
        ],
        task_text="Answer message must contain only the total fraudulent payment amount.",
    )

    assert updated.message == "EUR 12.34"
    assert updated.grounding_row_refs == [
        "/archive/payments.tsv#row=existing",
        "/archive/payments.tsv#row=R1",
    ]


def test_apply_archive_fraud_result_keeps_record_identification_message() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["analyzed live fraud"],
        task_type="fraud_review",
        message="Fraudulent payment records: pay_001",
        grounding_doc_refs=[],
        grounding_row_refs=[],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_archive_fraud_result(
        cmd,
        total_message="EUR 12.34",
        refs_to_submit=["/proc/payments/pay_001.json"],
        task_text="Identify the payment records that belong to that hit.",
    )

    assert updated.message == "Fraudulent payment records: pay_001"
    assert updated.grounding_row_refs == ["/proc/payments/pay_001.json"]


def test_apply_receipt_price_result_sets_message_and_refs_for_receipt_tasks() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["checked receipt"],
        task_type="receipt_price_check",
        message="old",
        grounding_doc_refs=[],
        grounding_row_refs=["/uploads/receipt_ocr.txt"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_receipt_price_result(
        cmd,
        formatted_message="<YES>",
        refs_to_submit=[
            "/uploads/receipt_ocr.txt",
            "/proc/catalog/FST-69283OWE.json",
        ],
    )

    assert updated.message == "<YES>"
    assert updated.grounding_row_refs == [
        "/uploads/receipt_ocr.txt",
        "/proc/catalog/FST-69283OWE.json",
    ]

    unchanged = _apply_receipt_price_result(
        cmd.model_copy(update={"task_type": "catalog_lookup"}),
        formatted_message="<NO>",
        refs_to_submit=["/proc/catalog/WRONG.json"],
    )
    assert unchanged.message == "old"
    assert unchanged.grounding_row_refs == ["/uploads/receipt_ocr.txt"]


def test_apply_city_availability_result_replaces_message_and_refs() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["checked city inventory"],
        task_type="availability_lookup",
        message="old",
        grounding_doc_refs=[],
        grounding_row_refs=["/proc/catalog/WRONG.json"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_city_availability_result(
        cmd,
        formatted_message="count: 4",
        refs_to_submit=[
            "/proc/catalog/FST-1KPF96UD.json",
            "/proc/stores/store_vienna_meidling.json",
        ],
    )

    assert updated.message == "count: 4"
    assert updated.grounding_row_refs == [
        "/proc/catalog/FST-1KPF96UD.json",
        "/proc/stores/store_vienna_meidling.json",
    ]


def test_apply_catalog_lookup_result_fixes_quote_table_message_and_refs() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["resolved quote rows"],
        task_type="catalog_lookup",
        message="old",
        grounding_doc_refs=[],
        grounding_row_refs=["/proc/catalog/Sika/ADH-2U8ETNHK.json"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_catalog_lookup_result(
        cmd,
        table_message=(
            "RowID\tSKU\tin_stock\tmatch\n"
            "bFZ9B\tSTO-2ZMSZF6Z\t0\tfalse"
        ),
        refs_to_submit=[
            "/proc/stores/store_vienna_praterstern.json",
            "/proc/catalog/Festool/STO-2ZMSZF6Z.json",
        ],
        task_text=(
            "Return exactly this tab-separated output table:\n"
            "RowID\tSKU\tin_stock\tmatch"
        ),
    )

    assert updated.message.endswith("bFZ9B\tSTO-2ZMSZF6Z\t0\tfalse")
    assert updated.grounding_row_refs == [
        "/proc/catalog/Sika/ADH-2U8ETNHK.json",
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/catalog/Festool/STO-2ZMSZF6Z.json",
    ]


def test_apply_catalog_lookup_result_ignores_non_table_tasks() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["resolved one product"],
        task_type="catalog_lookup",
        message="<YES>",
        grounding_doc_refs=[],
        grounding_row_refs=[],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_catalog_lookup_result(
        cmd,
        table_message="RowID\tSKU\tin_stock\tmatch\nrow\tSKU-1234\t1\ttrue",
        refs_to_submit=["/proc/catalog/Brand/SKU-1234.json"],
        task_text="Do you carry this product?",
    )

    assert updated == cmd


def test_apply_payment_recovery_review_changes_paid_clarification() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["Payment status is paid."],
        task_type="payment_recovery",
        message="Payment is already paid.",
        grounding_doc_refs=[],
        grounding_row_refs=[],
        protected_record_denial=False,
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    review = PaymentRecoveryReview(
        already_paid_terminal_state=True,
        retry_lockout_state=False,
        retry_available_at="",
        formatted_message="OUTCOME_NONE_UNSUPPORTED: payment is already paid",
    )

    updated = _apply_payment_recovery_review(cmd, review)

    assert updated.outcome == "OUTCOME_NONE_UNSUPPORTED"
    assert updated.message == "OUTCOME_NONE_UNSUPPORTED: payment is already paid"


def test_apply_payment_recovery_review_changes_retry_lockout_clarification() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["A retry lockout applies."],
        task_type="payment_recovery",
        message="OUTCOME_NONE_CLARIFICATION",
        grounding_doc_refs=[],
        grounding_row_refs=[],
        protected_record_denial=False,
        outcome="OUTCOME_NONE_CLARIFICATION",
    )
    review = PaymentRecoveryReview(
        already_paid_terminal_state=False,
        retry_lockout_state=True,
        retry_available_at="",
        formatted_message="OUTCOME_NONE_UNSUPPORTED: retry blocked",
    )

    updated = _apply_payment_recovery_review(cmd, review)

    assert updated.outcome == "OUTCOME_NONE_UNSUPPORTED"
    assert updated.message == "OUTCOME_NONE_UNSUPPORTED: retry blocked"


def test_apply_payment_recovery_closed_retry_changes_ok_outcome() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=[
            "Compared current time to three_ds.retry_after and found the retry window still closed."
        ],
        task_type="payment_recovery",
        message="Retry window opens at 2026-12-23T12:28:21Z.",
        grounding_doc_refs=[],
        grounding_row_refs=["/proc/payment-ledger/cust-0144/pay-0034.json"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_payment_recovery_closed_retry_outcome(cmd)

    assert updated.outcome == "OUTCOME_NONE_UNSUPPORTED"
    assert updated.message == (
        "OUTCOME_NONE_UNSUPPORTED: retry blocked until 2026-12-23T12:28:21Z"
    )


def test_apply_catalog_availability_lookup_refs_merges_store_and_catalog_refs() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["checked pasted rows"],
        task_type="availability_lookup",
        message="table",
        grounding_doc_refs=[],
        grounding_row_refs=["/proc/catalog/A.json"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    updated = _apply_catalog_availability_lookup_refs(
        cmd,
        [
            "/proc/stores/store_graz_jakomini.json",
            "/proc/catalog/B.json",
        ],
    )

    assert updated.grounding_row_refs == [
        "/proc/catalog/A.json",
        "/proc/stores/store_graz_jakomini.json",
        "/proc/catalog/B.json",
    ]


def test_apply_payment_recovery_retry_timestamp_reads_policy_doc() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=[
            "Current UTC time is before retry_available_at for pay_002."
        ],
        task_type="payment_recovery",
        message="OUTCOME_NONE_UNSUPPORTED",
        grounding_doc_refs=["/docs/policy-updates/3ds-retry-lockout-2024-07-17.md"],
        grounding_row_refs=["/proc/payments/pay_002.json"],
        protected_record_denial=False,
        outcome="OUTCOME_NONE_UNSUPPORTED",
    )

    updated = _apply_payment_recovery_retry_timestamp(
        ReadOnlyVM(
            {
                "/docs/policy-updates/3ds-retry-lockout-2024-07-17.md": (
                    "- payment_id: pay_002\n"
                    "- retry_available_at: 2024-07-18T14:49:48Z\n"
                )
            }
        ),
        cmd,
        PaymentRecoveryReview(
            already_paid_terminal_state=False,
            retry_lockout_state=True,
            retry_available_at="",
            formatted_message="OUTCOME_NONE_UNSUPPORTED",
        ),
        task_text="Can you recover basket basket_202?",
    )

    assert updated.message == (
        "OUTCOME_NONE_UNSUPPORTED: retry blocked until 2024-07-18T14:49:48Z"
    )
    assert updated.completed_steps_laconic[-1] == (
        "Retry is blocked until 2024-07-18T14:49:48Z."
    )


def test_apply_payment_recovery_retry_timestamp_uses_review_timestamp() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["Recovery is currently blocked by policy."],
        task_type="payment_recovery",
        message="OUTCOME_NONE_UNSUPPORTED",
        grounding_doc_refs=[],
        grounding_row_refs=["/proc/payments/pay_002.json"],
        protected_record_denial=False,
        outcome="OUTCOME_NONE_UNSUPPORTED",
    )

    updated = _apply_payment_recovery_retry_timestamp(
        ReadOnlyVM({}),
        cmd,
        PaymentRecoveryReview(
            already_paid_terminal_state=False,
            retry_lockout_state=True,
            retry_available_at="2024-07-18T14:49:48Z",
            formatted_message="OUTCOME_NONE_UNSUPPORTED",
        ),
        task_text="Can you recover pay_002?",
    )

    assert updated.message == (
        "OUTCOME_NONE_UNSUPPORTED: retry blocked until 2024-07-18T14:49:48Z"
    )


def test_apply_discount_cap_message_preserves_policy_cap() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=[
            "Computed basket subtotal and policy state: max allowed discount is 12%."
        ],
        task_type="discount",
        message="OUTCOME_NONE_UNSUPPORTED",
        grounding_doc_refs=["/docs/discount-policy.md"],
        grounding_row_refs=["/proc/baskets/basket_001.json"],
        protected_record_denial=False,
        outcome="OUTCOME_NONE_UNSUPPORTED",
    )

    updated = _apply_discount_cap_message(cmd)

    assert updated.message == (
        "OUTCOME_NONE_UNSUPPORTED: maximum allowed discount is 12%"
    )


def test_apply_discount_cap_message_does_not_overwrite_specific_message() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=[
            "Computed basket subtotal and policy state: max allowed discount is 12%."
        ],
        task_type="discount",
        message="OUTCOME_NONE_UNSUPPORTED: maximum allowed discount is 10%",
        grounding_doc_refs=["/docs/discount-policy.md"],
        grounding_row_refs=["/proc/baskets/basket_001.json"],
        protected_record_denial=False,
        outcome="OUTCOME_NONE_UNSUPPORTED",
    )

    updated = _apply_discount_cap_message(cmd)

    assert updated.message == cmd.message


def test_tomorrow_date_preflight_formats_runtime_date() -> None:
    cmd = _tomorrow_date_preflight(
        "What date is tomorrow? Reply MM/DD/YYYY only.",
        "Tue Jun 16 17:13:01 UTC 2026\n2026-06-16\n",
    )

    assert cmd is not None
    assert cmd.message == "06/17/2026"
    assert cmd.outcome == "OUTCOME_OK"
    assert cmd.grounding_doc_refs == []
    assert cmd.grounding_row_refs == []


def test_tomorrow_date_preflight_respects_requested_format() -> None:
    cmd = _tomorrow_date_preflight(
        "Give tomorrow date in DD-MM-YYYY.",
        "2026-12-31T23:15:00Z\n",
    )

    assert cmd is not None
    assert cmd.message == "01-01-2027"


def test_tomorrow_date_preflight_supports_month_name_format() -> None:
    cmd = _tomorrow_date_preflight(
        "calculate tomorrow. Format the answer as Month DD, YYYY only.",
        "2026-05-30\n",
    )

    assert cmd is not None
    assert cmd.message == "May 31, 2026"


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
