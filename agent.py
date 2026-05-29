import json
import shlex
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    List,
    Literal,
    ParamSpec,
    TypeVar,
    cast,
)

import openai
from annotated_types import Ge, Le
from archive_fraud import ReqAnalyzeArchiveFraudExport, analyze_archive_fraud_export
from payment_fraud import ReqAnalyzePaymentFraudHistory, analyze_payment_fraud_history
from answer_formatter import format_completion_message
from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
from bitgn.vm.ecom.ecom_pb2 import (
    AnswerRequest,
    DeleteRequest,
    ExecRequest,
    FindRequest,
    ListRequest,
    NodeKind,
    Outcome,
    ReadRequest,
    SearchRequest,
    StatRequest,
    TreeRequest,
    WriteRequest,
)
from catalog_tools import ReqResolveCatalogItems, resolve_catalog_items
from checkout_preflight import ambiguous_checkout_preflight, selected_basket_preflight
from security_preflight import security_preflight
from config import (
    CLI_BLUE,
    CLI_CLR,
    CLI_GREEN,
    CLI_RED,
    CLI_YELLOW,
    env_flag,
    env_int,
    openai_client_kwargs,
    render_prompt,
)
from connectrpc.errors import ConnectError
from evidence_ledger import EvidenceLedger
from google.protobuf.json_format import MessageToDict
from langsmith.run_helpers import get_current_run_tree
from langsmith.wrappers import wrap_openai
from manager_verification import ReqVerifyStoreManager, verify_store_manager
from openai import OpenAI
from openai.types.responses import (
    FunctionToolParam,
    ResponseFunctionToolCall,
    ResponseInputParam,
)
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, Field, ValidationError
from submission_refs import (
    availability_count_refs_from_catalog_result,
    catalog_refs_from_refs,
    dedupe_refs,
    is_catalog_ref,
    submission_refs as _submission_refs,
    support_note_refs_from_catalog_result,
)
from task_classifier import classify_task

if TYPE_CHECKING:
    P = ParamSpec("P")
    R = TypeVar("R")

    def traceable(*args: Any, **kwargs: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def decorator(func: Callable[P, R]) -> Callable[P, R]:
            return func

        return decorator

else:
    from langsmith import traceable


TaskType = Literal[
    "count",
    "availability_count",
    "availability_lookup",
    "catalog_lookup",
    "receipt_price_check",
    "checkout",
    "discount",
    "payment_recovery",
    "refund",
    "fraud_review",
    "other",
]


class ReportTaskCompletion(BaseModel):
    completed_steps_laconic: List[str]
    task_type: TaskType = Field(
        default="other",
        description=(
            "Classify for reference postprocessing. Use count only for "
            "aggregate catalogue/reporting counts where row refs should be "
            "suppressed; availability_count for inventory-threshold counts; "
            "otherwise the closest domain type."
        ),
    )
    message: str = Field(
        description=(
            "Exact final user-visible answer. If the task asks for an exact "
            "format, contain only that format. Use <YES>/<NO> only for yes/no "
            "questions without another exact format."
        )
    )
    grounding_doc_refs: List[str] = Field(
        default_factory=list,
        description=(
            "Authoritative document paths used for the final answer or decision."
        ),
    )
    protected_record_denial: bool = Field(
        description=(
            "True only when refusing because the current identity must not "
            "access, use, disclose, or rely on the requested records."
        ),
    )
    grounding_row_refs: List[str] = Field(
        default_factory=list,
        description=(
            "Concrete runtime record or upload paths used for the final answer "
            "or action. Exclude exploratory or ruled-out paths unless requested."
        ),
    )
    outcome: Literal[
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]


class ReqTree(BaseModel):
    level: int = Field(2, description="max tree depth, 0 means unlimited")
    root: str = Field("/", description="absolute root path to inspect")
    auto_followups: bool = Field(
        True,
        description=(
            "when true, automatically read Markdown files and command --help "
            "entries discovered in the tree"
        ),
    )


class ReqFind(BaseModel):
    name: str
    root: str = "/"
    kind: Literal["all", "files", "dirs"] = "all"
    limit: Annotated[int, Ge(1), Le(20)] = 10


class ReqSearch(BaseModel):
    pattern: str
    limit: Annotated[int, Ge(1), Le(20)] = 10
    root: str = "/"


class ReqList(BaseModel):
    path: str = "/"


class ReqRead(BaseModel):
    path: str
    number: bool = Field(False, description="return 1-based line numbers")
    start_line: Annotated[int, Ge(0)] = Field(
        0, description="1-based inclusive line; 0 means from the first line"
    )
    end_line: Annotated[int, Ge(0)] = Field(
        0, description="1-based inclusive line; 0 means through the last line"
    )


class ReqWrite(BaseModel):
    path: str
    content: str


class ReqDelete(BaseModel):
    path: str


class ReqStat(BaseModel):
    path: str


class ReqExec(BaseModel):
    path: str
    args: List[str] = Field(default_factory=list)
    stdin: str = ""


OUTCOME_BY_NAME = {
    "OUTCOME_OK": Outcome.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
}

TOOL_MODELS: dict[str, type[BaseModel]] = {
    "tree": ReqTree,
    "find": ReqFind,
    "search": ReqSearch,
    "list": ReqList,
    "read": ReqRead,
    "write": ReqWrite,
    "delete": ReqDelete,
    "stat": ReqStat,
    "exec": ReqExec,
    "analyze_archive_fraud_export": ReqAnalyzeArchiveFraudExport,
    "analyze_payment_fraud_history": ReqAnalyzePaymentFraudHistory,
    "resolve_catalog_items": ReqResolveCatalogItems,
    "verify_store_manager": ReqVerifyStoreManager,
    "report_completion": ReportTaskCompletion,
}

TOOL_NAMES_BY_MODEL = {model: name for name, model in TOOL_MODELS.items()}


def _responses_function_tool(
    model: type[BaseModel],
    *,
    name: str,
    description: str,
) -> FunctionToolParam:
    tool = openai.pydantic_function_tool(model, name=name, description=description)
    function = tool["function"]
    return FunctionToolParam(
        type="function",
        name=function["name"],
        description=description,
        parameters=function["parameters"],
        strict=function["strict"],
    )


MAIN_PROMPT = render_prompt("main.j2")

TOOLS: list[FunctionToolParam] = [
    _responses_function_tool(
        ReqTree,
        name="tree",
        description=(
            "List a runtime filesystem tree under an absolute path. Tree output is "
            "enriched once per trial when auto_followups is true: extensionless "
            "file entries include their '<path> --help' output, and Markdown "
            "files are read case-insensitively. Repeated enrichment for the same "
            "path is suppressed. Set auto_followups=false for broad directory "
            "overviews where the file contents would be too noisy."
        ),
    ),
    _responses_function_tool(
        ReqFind,
        name="find",
        description="Find runtime filesystem entries by name under an absolute path.",
    ),
    _responses_function_tool(
        ReqSearch,
        name="search",
        description="Search text files in the runtime filesystem.",
    ),
    _responses_function_tool(
        ReqList,
        name="list",
        description="List direct children of a runtime filesystem directory.",
    ),
    _responses_function_tool(
        ReqRead,
        name="read",
        description="Read a runtime file by absolute path.",
    ),
    _responses_function_tool(
        ReqWrite,
        name="write",
        description="Write a runtime file by absolute path.",
    ),
    _responses_function_tool(
        ReqDelete,
        name="delete",
        description="Delete a runtime file by absolute path.",
    ),
    _responses_function_tool(
        ReqStat,
        name="stat",
        description="Stat a runtime filesystem path.",
    ),
    _responses_function_tool(
        ReqExec,
        name="exec",
        description=(
            "Execute an absolute runtime command path. Use /bin/sql with SQL in "
            "stdin for catalogue and state queries. Do not use this to run "
            "non-runtime interpreters for archive TSV analysis; use the archive "
            "fraud helper instead."
        ),
    ),
    _responses_function_tool(
        ReqAnalyzeArchiveFraudExport,
        name="analyze_archive_fraud_export",
        description=(
            "Analyze an archived payment TSV under /archive for fraud incident "
            "rows. Use this for archive payment fraud-review tasks and total "
            "fraud amount questions. Returns total_message formatted as EUR "
            "%d.%02d plus refs_to_submit in the required row-ref format."
        ),
    ),
    _responses_function_tool(
        ReqAnalyzePaymentFraudHistory,
        name="analyze_payment_fraud_history",
        description=(
            "Detect fraud incidents inside the live /proc/payments transaction "
            "history. Use this for any fraud-review task that asks about "
            "current or archived payment records inside /proc/payments (not "
            "/archive/*.tsv exports). Returns total_message (EUR %d.%02d), "
            "fraud_payment_ids, and refs_to_submit (/proc/payments/<id>.json) "
            "based on velocity rules over customer_id, payment_method "
            "fingerprint, and device fingerprint across distant store cities. "
            "Do not run ad-hoc SQL fraud heuristics; rely on this helper."
        ),
    ),
    _responses_function_tool(
        ReqResolveCatalogItems,
        name="resolve_catalog_items",
        description=(
            "Strict helper for exact catalogue and store availability tasks. "
            "Pass raw product descriptions plus optional store_id/quantity "
            "thresholds. Returns exact SKU matches, availability, and canonical "
            "refs; unsupported schemas or unparsed descriptions raise an error."
        ),
    ),
    _responses_function_tool(
        ReqVerifyStoreManager,
        name="verify_store_manager",
        description=(
            "Verify that a named employee is assigned to a named store and has "
            "the store_manager role. Use this for pure manager/store "
            "verification questions and for discount or approval tasks that "
            "mention a named manager. The result includes refs_to_submit based "
            "on the actual SQL verification."
        ),
    ),
    _responses_function_tool(
        ReportTaskCompletion,
        name="report_completion",
        description=(
            "Submit the final task answer to the ECOM runtime. The message is "
            "the exact final answer that will be graded; keep explanations in "
            "completed_steps_laconic and split references between "
            "grounding_doc_refs and grounding_row_refs."
        ),
    ),
]


def _format_tree_entry(entry, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "`-- " if is_last else "|-- "
    lines = [f"{prefix}{branch}{entry.name}"]
    child_prefix = f"{prefix}{'    ' if is_last else '|   '}"
    children = list(entry.children)
    for idx, child in enumerate(children):
        lines.extend(
            _format_tree_entry(
                child,
                prefix=child_prefix,
                is_last=idx == len(children) - 1,
            )
        )
    return lines


def _render_command(command: str, body: str) -> str:
    return f"{command}\n{body}"


def _is_truncated(result) -> bool:
    return getattr(result, "truncated", False)


def _mark_truncated(result, body: str, hint: str) -> str:
    if not _is_truncated(result):
        return body
    marker = f"[TRUNCATED: {hint}]"
    if not body:
        return marker
    return f"{body}\n{marker}"


def _format_tree_response(cmd: ReqTree, result) -> str:
    root = result.root
    if not root.name:
        body = "."
    else:
        lines = [root.name]
        children = list(root.children)
        for idx, child in enumerate(children):
            lines.extend(_format_tree_entry(child, is_last=idx == len(children) - 1))
        body = "\n".join(lines)

    level_arg = f" -L {cmd.level}" if cmd.level > 0 else ""
    body = _mark_truncated(
        result,
        body,
        "tree output hit a limit; use a narrower root or search for a specific term",
    )
    return _render_command(f"tree{level_arg} {cmd.root}", body)


def _format_list_response(cmd: ReqList, result) -> str:
    if not result.entries:
        body = "."
    else:
        body = "\n".join(
            f"{entry.name}/" if entry.kind == NodeKind.NODE_KIND_DIR else entry.name
            for entry in result.entries
        )
    return _render_command(f"ls {cmd.path}", body)


def _format_read_response(cmd: ReqRead, result) -> str:
    if cmd.start_line > 0 or cmd.end_line > 0:
        start = cmd.start_line if cmd.start_line > 0 else 1
        end = cmd.end_line if cmd.end_line > 0 else "$"
        command = f"sed -n '{start},{end}p' {cmd.path}"
    elif cmd.number:
        command = f"cat -n {cmd.path}"
    else:
        command = f"cat {cmd.path}"
    body = _mark_truncated(
        result,
        result.content,
        "file output hit a limit; use start_line/end_line to read a smaller range",
    )
    return _render_command(command, body)


def _format_search_response(cmd: ReqSearch, result) -> str:
    root = shlex.quote(cmd.root or "/")
    pattern = shlex.quote(cmd.pattern)
    body = "\n".join(
        f"{match.path}:{match.line}:{match.line_text}" for match in result.matches
    )
    body = _mark_truncated(
        result,
        body,
        "search hit limit reached; narrow the pattern/root or raise the limit",
    )
    return _render_command(f"rg -n --no-heading -e {pattern} {root}", body)


def _format_exec_response(cmd: ReqExec, result) -> str:
    path = shlex.quote(cmd.path)
    args = " ".join(shlex.quote(arg) for arg in cmd.args)
    command = f"{path} {args}".strip()
    if cmd.stdin:
        label = "SQL" if cmd.path == "/bin/sql" else "STDIN"
        command = f"{command} <<'{label}'\n{cmd.stdin.rstrip()}\n{label}"

    body_parts = []
    if result.stdout:
        body_parts.append(result.stdout.rstrip())
    if result.stderr:
        body_parts.append(f"stderr:\n{result.stderr.rstrip()}")
    if getattr(result, "exit_code", 0):
        body_parts.append(f"[exit {result.exit_code}]")
    body = "\n".join(body_parts) if body_parts else "."
    return _render_command(command, body)


def _format_result(cmd: BaseModel, result) -> str:
    if result is None:
        return "{}"
    if isinstance(result, dict | list):
        return json.dumps(result, ensure_ascii=False, indent=2)
    if isinstance(cmd, ReqTree):
        return _format_tree_response(cmd, result)
    if isinstance(cmd, ReqList):
        return _format_list_response(cmd, result)
    if isinstance(cmd, ReqRead):
        return _format_read_response(cmd, result)
    if isinstance(cmd, ReqSearch):
        return _format_search_response(cmd, result)
    if isinstance(cmd, ReqExec):
        return _format_exec_response(cmd, result)
    return json.dumps(MessageToDict(result), indent=2)


def _trace_cmd(cmd: BaseModel) -> dict:
    return {
        "tool": TOOL_NAMES_BY_MODEL.get(type(cmd), type(cmd).__name__),
        "args": cmd.model_dump(),
    }


def _trace_dispatch_inputs(inputs: dict) -> dict:
    cmd = inputs.get("cmd")
    if isinstance(cmd, BaseModel):
        return _trace_cmd(cmd)
    return {"tool": type(cmd).__name__}


def _trace_dispatch_outputs(output) -> dict:
    if output is None:
        return {}
    try:
        return MessageToDict(output, preserving_proto_field_name=True)
    except Exception:
        return {"output": str(output)}


def _trace_agent_inputs(inputs: dict) -> dict:
    return {
        "task_text": inputs.get("task_text"),
    }


def _trace_agent_outputs(output) -> dict:
    if isinstance(output, dict):
        return output
    return {"output": output}


def _normalize_runtime_path(path: str) -> str:
    if not path or path == "/":
        return "/"
    return f"/{path.strip('/')}"


def _child_runtime_path(parent: str, name: str) -> str:
    parent = _normalize_runtime_path(parent)
    if parent == "/":
        return f"/{name}"
    return f"{parent}/{name}"


def _iter_tree_paths(root_path: str, entry):
    root_path = _normalize_runtime_path(root_path)
    yield root_path, entry
    for child in list(getattr(entry, "children", []) or []):
        name = getattr(child, "name", "")
        if not name:
            continue
        yield from _iter_tree_paths(_child_runtime_path(root_path, name), child)


def _remember_seen_tool_use(cmd: BaseModel, seen_help: set[str], seen_read: set[str]) -> None:
    if isinstance(cmd, ReqRead):
        seen_read.add(_normalize_runtime_path(cmd.path))
    if isinstance(cmd, ReqExec) and cmd.args == ["--help"]:
        seen_help.add(_normalize_runtime_path(cmd.path))


def _is_command_path(path: str, entry) -> bool:
    if getattr(entry, "kind", None) != NodeKind.NODE_KIND_FILE:
        return False
    name = path.rsplit("/", 1)[-1]
    return "." not in name


def _is_markdown_path(path: str, entry) -> bool:
    if getattr(entry, "kind", None) != NodeKind.NODE_KIND_FILE:
        return False
    return path.rsplit("/", 1)[-1].lower().endswith(".md")


def _tree_followup_commands(
    cmd: ReqTree,
    result,
    seen_help: set[str],
    seen_read: set[str],
) -> list[BaseModel]:
    if not cmd.auto_followups:
        return []

    help_commands: list[BaseModel] = []
    read_commands: list[BaseModel] = []

    for path, entry in _iter_tree_paths(cmd.root, result.root):
        if getattr(entry, "kind", None) != NodeKind.NODE_KIND_FILE:
            continue

        if _is_command_path(path, entry) and path not in seen_help:
            seen_help.add(path)
            help_commands.append(ReqExec(path=path, args=["--help"]))

        if _is_markdown_path(path, entry) and path not in seen_read:
            seen_read.add(path)
            read_commands.append(
                ReqRead(path=path, number=False, start_line=0, end_line=0)
            )

    return read_commands + help_commands


def _format_result_with_tree_followups(
    vm: EcomRuntimeClientSync,
    cmd: BaseModel,
    result,
    seen_help: set[str],
    seen_read: set[str],
    debug: bool,
) -> str:
    parts = [_format_result(cmd, result)]
    if not isinstance(cmd, ReqTree):
        return parts[0]

    for followup in _tree_followup_commands(cmd, result, seen_help, seen_read):
        try:
            followup_result = dispatch(vm, followup)
            parts.append(_format_result(followup, followup_result))
        except ConnectError as exc:
            if debug:
                print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")

    return "\n\n".join(parts)


@traceable(
    run_type="tool",
    name="ECOM Runtime Tool",
    process_inputs=_trace_dispatch_inputs,
    process_outputs=_trace_dispatch_outputs,
)
def dispatch(vm: EcomRuntimeClientSync, cmd: BaseModel, *, task_text: str = ""):
    if isinstance(cmd, ReqTree):
        return vm.tree(TreeRequest(root=cmd.root, level=cmd.level))
    if isinstance(cmd, ReqFind):
        return vm.find(
            FindRequest(
                root=cmd.root,
                name=cmd.name,
                kind={
                    "all": NodeKind.NODE_KIND_UNSPECIFIED,
                    "files": NodeKind.NODE_KIND_FILE,
                    "dirs": NodeKind.NODE_KIND_DIR,
                }[cmd.kind],
                limit=cmd.limit,
            )
        )
    if isinstance(cmd, ReqSearch):
        return vm.search(
            SearchRequest(root=cmd.root, pattern=cmd.pattern, limit=cmd.limit)
        )
    if isinstance(cmd, ReqList):
        return vm.list(ListRequest(path=cmd.path))
    if isinstance(cmd, ReqRead):
        return vm.read(
            ReadRequest(
                path=cmd.path,
                number=cmd.number,
                start_line=cmd.start_line,
                end_line=cmd.end_line,
            )
        )
    if isinstance(cmd, ReqWrite):
        return vm.write(WriteRequest(path=cmd.path, content=cmd.content))
    if isinstance(cmd, ReqDelete):
        return vm.delete(DeleteRequest(path=cmd.path))
    if isinstance(cmd, ReqStat):
        return vm.stat(StatRequest(path=cmd.path))
    if isinstance(cmd, ReqExec):
        return vm.exec(ExecRequest(path=cmd.path, args=cmd.args, stdin=cmd.stdin))
    if isinstance(cmd, ReqAnalyzeArchiveFraudExport):
        return analyze_archive_fraud_export(vm, cmd)
    if isinstance(cmd, ReqAnalyzePaymentFraudHistory):
        return analyze_payment_fraud_history(vm, cmd)
    if isinstance(cmd, ReqResolveCatalogItems):
        return resolve_catalog_items(vm, cmd)
    if isinstance(cmd, ReqVerifyStoreManager):
        return verify_store_manager(vm, cmd)
    if isinstance(cmd, ReportTaskCompletion):
        return vm.answer(
            AnswerRequest(
                message=cmd.message,
                outcome=OUTCOME_BY_NAME[cmd.outcome],
                refs=_submission_refs(cmd, vm, task_text=task_text),
            )
        )
    raise ValueError(f"Unknown command: {cmd}")


def _function_call_output(tool_call, output: str) -> dict:
    return _function_call_output_for_call_id(tool_call.call_id, output)


def _function_call_output_for_call_id(call_id: str, output: str) -> dict:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


def _synthetic_function_call(cmd: BaseModel, call_id: str) -> dict:
    name = TOOL_NAMES_BY_MODEL.get(type(cmd))
    if name is None:
        raise ValueError(f"Unknown synthetic tool command: {cmd}")
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(cmd.model_dump(mode="json"), ensure_ascii=False),
        "status": "completed",
    }


def _append_synthetic_tool_pair(
    context: list[Any],
    cmd: BaseModel,
    output: str,
    call_id: str,
) -> None:
    context.append(_synthetic_function_call(cmd, call_id))
    context.append(_function_call_output_for_call_id(call_id, output))


def _synthetic_named_call(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
        "status": "completed",
    }


def _append_synthetic_named_pair(
    context: list[Any],
    *,
    name: str,
    arguments: dict,
    output: str,
    call_id: str,
) -> None:
    context.append(_synthetic_named_call(name, arguments, call_id))
    context.append(_function_call_output_for_call_id(call_id, output))


def _parse_tool_call(tool_call) -> BaseModel:
    name = tool_call.name
    model = TOOL_MODELS.get(name)
    if model is None:
        raise ValueError(f"Unknown tool: {name}")
    args = json.loads(tool_call.arguments or "{}")
    return model.model_validate(args)


def _output_text(resp) -> str:
    text = getattr(resp, "output_text", None)
    if text:
        return text

    chunks = []
    for item in resp.output or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", None) == "output_text":
                chunks.append(content.text)
    return "\n".join(chunks)


def _format_completion(cmd: ReportTaskCompletion, refs: list[str] | None = None) -> str:
    status = CLI_GREEN if cmd.outcome == "OUTCOME_OK" else CLI_YELLOW
    lines: list[str] = [f"{status}agent {cmd.outcome}{CLI_CLR}. Summary:"]
    for item in cmd.completed_steps_laconic:
        lines.append(f"- {item}")
    lines.append(f"\n{CLI_BLUE}AGENT SUMMARY: {cmd.message}{CLI_CLR}")
    for ref in refs if refs is not None else _submission_refs(cmd):
        lines.append(f"- {CLI_BLUE}{ref}{CLI_CLR}")
    return "\n".join(lines)


def _print_completion(cmd: ReportTaskCompletion, refs: list[str] | None = None) -> None:
    print(_format_completion(cmd, refs))


def _apply_availability_count_catalog_refs(
    cmd: ReportTaskCompletion,
    canonical_refs: list[str],
) -> ReportTaskCompletion:
    canonical_catalog_refs = catalog_refs_from_refs(canonical_refs)
    if cmd.task_type != "availability_count" or not canonical_catalog_refs:
        return cmd

    # Availability-count graders expect the final refs to describe the products
    # that actually qualify. The catalogue helper already computes that set, so
    # keep non-catalog operational refs and replace model-invented catalog refs.
    row_refs = dedupe_refs(
        [
            *(ref for ref in cmd.grounding_row_refs if not is_catalog_ref(ref)),
            *canonical_catalog_refs,
        ]
    )
    return cmd.model_copy(update={"grounding_row_refs": row_refs})


def _apply_support_note_catalog_refs(
    cmd: ReportTaskCompletion,
    checked_refs: list[str],
) -> ReportTaskCompletion:
    if cmd.task_type != "catalog_lookup" or not checked_refs:
        return cmd

    row_refs = dedupe_refs(
        [
            *(ref for ref in cmd.grounding_row_refs if not is_catalog_ref(ref)),
            *checked_refs,
        ]
    )
    return cmd.model_copy(update={"grounding_row_refs": row_refs})


def _apply_verified_manager_refs(
    cmd: ReportTaskCompletion,
    refs_to_submit: list[str],
) -> ReportTaskCompletion:
    if not refs_to_submit:
        return cmd

    row_refs = dedupe_refs([*cmd.grounding_row_refs, *refs_to_submit])
    return cmd.model_copy(update={"grounding_row_refs": row_refs})


def _apply_archive_fraud_result(
    cmd: ReportTaskCompletion,
    *,
    total_message: str,
    refs_to_submit: list[str],
) -> ReportTaskCompletion:
    if not total_message and not refs_to_submit:
        return cmd

    updates: dict[str, Any] = {}
    if total_message:
        updates["message"] = total_message
    if refs_to_submit:
        updates["grounding_row_refs"] = dedupe_refs(
            [*cmd.grounding_row_refs, *refs_to_submit]
        )
    return cmd.model_copy(update=updates)


@traceable(
    run_type="chain",
    name="ECOM Agent",
    process_inputs=_trace_agent_inputs,
    process_outputs=_trace_agent_outputs,
)
def run_agent(
    model: str,
    harness_url: str,
    task_text: str,
    *,
    print_completion: bool = True,
) -> dict:
    run_tree = get_current_run_tree()
    langsmith_run_id = str(run_tree.id) if run_tree and run_tree.id else None
    langsmith_trace_id = str(run_tree.trace_id) if run_tree and run_tree.trace_id else langsmith_run_id

    client = wrap_openai(OpenAI(**openai_client_kwargs()))
    formatter_client = OpenAI(**openai_client_kwargs())
    vm = EcomRuntimeClientSync(harness_url)
    debug = env_flag("AGENT_DEBUG")
    max_steps = env_int("AGENT_MAX_STEPS", 75, minimum=1)
    context: list[Any] = []
    tree_help_paths: set[str] = set()
    tree_read_paths: set[str] = set()
    formatter_output_lines: list[str] = []
    ledger = EvidenceLedger()
    final_result: dict = {
        "completed": False,
        "langsmith_run_id": langsmith_run_id,
        "langsmith_trace_id": langsmith_trace_id,
        "formatter_output": formatter_output_lines,
    }

    synthetic_call_index = 0

    def append_synthetic_tool_result(cmd: BaseModel, output: str) -> None:
        nonlocal synthetic_call_index
        synthetic_call_index += 1
        _append_synthetic_tool_pair(
            context,
            cmd,
            output,
            f"call_auto_{synthetic_call_index}",
        )

    # Run the task classifier in a background thread so its helper LLM call
    # overlaps with the synchronous must startup tools. The future is awaited
    # only when the security/checkout preflights actually need the result, so
    # the wall-clock latency from the classifier is hidden behind the gRPC
    # round-trips that the must loop already pays for.
    classifier_pool = ThreadPoolExecutor(max_workers=1)
    try:
        classification_future = classifier_pool.submit(
            classify_task, formatter_client, task_text
        )

        must: list[BaseModel] = [
            ReqRead(path="/AGENTS.MD"),
            ReqTree(level=2, root="/", auto_followups=False),
            ReqTree(level=3, root="/bin"),
            ReqTree(level=3, root="/docs"),
            ReqExec(path="/bin/date"),
            ReqExec(path="/bin/id"),
        ]

        for cmd in must:
            result = dispatch(vm, cmd)
            _remember_seen_tool_use(cmd, tree_help_paths, tree_read_paths)
            formatted = _format_result(cmd, result)
            if debug:
                print(f"{CLI_GREEN}AUTO{CLI_CLR}: {formatted}")
            append_synthetic_tool_result(cmd, formatted)

            if not isinstance(cmd, ReqTree):
                continue

            for followup in _tree_followup_commands(
                cmd, result, tree_help_paths, tree_read_paths
            ):
                try:
                    followup_result = dispatch(vm, followup)
                    _remember_seen_tool_use(followup, tree_help_paths, tree_read_paths)
                    followup_formatted = _format_result(followup, followup_result)
                    if debug:
                        print(f"{CLI_GREEN}AUTO{CLI_CLR}: {followup_formatted}")
                    append_synthetic_tool_result(followup, followup_formatted)
                except ConnectError as exc:
                    if debug:
                        print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")

        classification = classification_future.result()
    finally:
        classifier_pool.shutdown(wait=False)

    def _finalize_preflight(cmd: ReportTaskCompletion) -> dict:
        dispatch(vm, cmd, task_text=task_text)
        completion_refs = _submission_refs(cmd, vm, task_text=task_text)
        result_payload = {
            "completed": True,
            "langsmith_run_id": langsmith_run_id,
            "langsmith_trace_id": langsmith_trace_id,
            "formatter_output": formatter_output_lines,
            "completion_output": _format_completion(cmd, completion_refs),
            "outcome": cmd.outcome,
            "task_type": cmd.task_type,
            "protected_record_denial": cmd.protected_record_denial,
            "message": cmd.message,
            "grounding_refs": completion_refs,
            "completed_steps_laconic": cmd.completed_steps_laconic,
        }
        if print_completion:
            _print_completion(cmd, completion_refs)
        return result_payload

    denial = security_preflight(vm, classification)
    if denial is not None:
        denial_task_type = "discount" if denial.reason == "customer_discount_claimed_manager_approval" else "other"
        cmd = ReportTaskCompletion(
            completed_steps_laconic=denial.completed_steps_laconic,
            task_type=denial_task_type,
            message=denial.message,
            grounding_doc_refs=denial.doc_refs,
            grounding_row_refs=denial.row_refs,
            protected_record_denial=denial.protected_record_denial,
            outcome="OUTCOME_DENIED_SECURITY",
        )
        return _finalize_preflight(cmd)

    ambiguous_checkout = ambiguous_checkout_preflight(vm, classification)
    if ambiguous_checkout is not None:
        basket_list = ", ".join(ambiguous_checkout.basket_ids)
        cmd = ReportTaskCompletion(
            completed_steps_laconic=[
                "Detected a checkout request without an explicit basket id.",
                "Found multiple active baskets for the current customer.",
                "Asked for clarification instead of choosing a basket.",
            ],
            task_type="checkout",
            message=f"Which basket should I check out? I found multiple active baskets: {basket_list}.",
            grounding_doc_refs=[],
            protected_record_denial=False,
            grounding_row_refs=ambiguous_checkout.basket_refs,
            outcome="OUTCOME_NONE_CLARIFICATION",
        )
        return _finalize_preflight(cmd)

    selected_basket = selected_basket_preflight(vm, classification)
    if selected_basket is not None:
        synthetic_call_index += 1
        # Surface a deterministic basket resolution so the model treats the
        # selector ("newest"/"oldest") as already decided and proceeds with
        # ordinary checkout policy instead of asking for clarification.
        _append_synthetic_named_pair(
            context,
            name="resolve_basket_selector",
            arguments={"selector": selected_basket.selector},
            output=json.dumps(
                {
                    "selector": selected_basket.selector,
                    "selected_basket_id": selected_basket.basket_id,
                    "selected_basket_ref": selected_basket.basket_ref,
                    "note": (
                        "Deterministic selector resolved. Use this basket for "
                        "the checkout decision; cite its record path in row refs."
                    ),
                },
                ensure_ascii=False,
            ),
            call_id=f"call_auto_{synthetic_call_index}",
        )

    context.append({"role": "user", "content": task_text})

    for i in range(max_steps):
        step = f"STEP_{i + 1}"
        started = time.time()
        resp = client.responses.create(
            model=model,
            instructions=MAIN_PROMPT,
            input=cast(ResponseInputParam, context),
            tools=TOOLS,
            tool_choice="required",
            parallel_tool_calls=True,
            reasoning=Reasoning(effort="high"),
            max_output_tokens=16384,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        tool_calls = [
            cast(ResponseFunctionToolCall, item)
            for item in resp.output or []
            if getattr(item, "type", None) == "function_call"
        ]

        if not tool_calls:
            if debug:
                print(
                    f"{CLI_RED}ERR{CLI_CLR}: response returned no function_call items "
                    f"despite tool_choice=required ({elapsed_ms} ms)\n{_output_text(resp)}"
                )
            context.extend(resp.output or [])
            context.append(
                {"role": "user", "content": "Use a function tool. Text-only answers are invalid."}
            )
            continue

        if debug:
            print(
                f"{CLI_GREEN}{step}{CLI_CLR}: {len(tool_calls)} responses tool call(s) "
                f"({elapsed_ms} ms)"
            )
        context.extend(resp.output or [])

        completed = False
        for idx, tool_call in enumerate(tool_calls, start=1):
            try:
                cmd = _parse_tool_call(tool_call)
                if debug:
                    print(f"  [{idx}/{len(tool_calls)}] {tool_call.name}: {cmd}")
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                txt = f"Invalid tool call: {exc}"
                if debug:
                    print(f"{CLI_RED}ERR{CLI_CLR}: {txt}")
                context.append(_function_call_output(tool_call, txt))
                continue

            if isinstance(cmd, ReportTaskCompletion):
                cmd = ledger.apply_to_completion(cmd)
                completion_refs = _submission_refs(cmd, vm, task_text=task_text)
                formatted_message = format_completion_message(
                    formatter_client,
                    task_text=task_text,
                    current_message=cmd.message,
                    outcome=cmd.outcome,
                    completed_steps_laconic=cmd.completed_steps_laconic,
                    grounding_refs=completion_refs,
                    debug=debug,
                    output_lines=None if print_completion else formatter_output_lines,
                )
                cmd = cmd.model_copy(update={"message": formatted_message})

            result: Any | None = None
            try:
                result = dispatch(vm, cmd, task_text=task_text)
                _remember_seen_tool_use(cmd, tree_help_paths, tree_read_paths)
                txt = _format_result_with_tree_followups(
                    vm, cmd, result, tree_help_paths, tree_read_paths, debug
                )
                if debug:
                    print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt}")
            except ConnectError as exc:
                txt = str(exc.message)
                if debug:
                    print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")

            context.append(_function_call_output(tool_call, txt))

            if isinstance(cmd, ReqResolveCatalogItems):
                ledger.merge_availability_count(
                    availability_count_refs_from_catalog_result(result)
                )
                ledger.merge_support_note(
                    support_note_refs_from_catalog_result(result)
                )
            if isinstance(
                cmd, (ReqAnalyzeArchiveFraudExport, ReqAnalyzePaymentFraudHistory)
            ) and isinstance(result, dict):
                total_message = result.get("total_message")
                refs_to_submit = result.get("refs_to_submit")
                ledger.merge_fraud_result(
                    refs=[
                        ref
                        for ref in (refs_to_submit if isinstance(refs_to_submit, list) else [])
                        if isinstance(ref, str)
                    ],
                    total_message=total_message if isinstance(total_message, str) else "",
                )
            if isinstance(cmd, ReqVerifyStoreManager) and isinstance(result, dict):
                refs_to_submit = result.get("refs_to_submit")
                ledger.merge_manager_verified(
                    [
                        ref
                        for ref in (refs_to_submit if isinstance(refs_to_submit, list) else [])
                        if isinstance(ref, str)
                    ]
                )

            if isinstance(cmd, ReportTaskCompletion):
                completion_refs = _submission_refs(cmd, vm, task_text=task_text)
                final_result = {
                    "completed": True,
                    "langsmith_run_id": langsmith_run_id,
                    "langsmith_trace_id": langsmith_trace_id,
                    "formatter_output": formatter_output_lines,
                    "completion_output": _format_completion(cmd, completion_refs),
                    "outcome": cmd.outcome,
                    "task_type": cmd.task_type,
                    "protected_record_denial": cmd.protected_record_denial,
                    "message": cmd.message,
                    "grounding_refs": completion_refs,
                    "completed_steps_laconic": cmd.completed_steps_laconic,
                }
                if print_completion:
                    _print_completion(cmd, completion_refs)
                completed = True
                break

        if completed:
            break

    if not final_result["completed"]:
        fallback_cmd = ReportTaskCompletion(
            completed_steps_laconic=[
                f"Reached the agent step budget of {max_steps} without a final completion.",
            ],
            message=f"Could not complete within the agent step budget of {max_steps}.",
            protected_record_denial=False,
            outcome="OUTCOME_ERR_INTERNAL",
        )
        try:
            fallback_refs = _submission_refs(fallback_cmd, vm, task_text=task_text)
            dispatch(vm, fallback_cmd, task_text=task_text)
            final_result = {
                "completed": True,
                "fallback": "step_budget_exhausted",
                "langsmith_run_id": langsmith_run_id,
                "langsmith_trace_id": langsmith_trace_id,
                "formatter_output": formatter_output_lines,
                "completion_output": _format_completion(fallback_cmd, fallback_refs),
                "outcome": fallback_cmd.outcome,
                "task_type": fallback_cmd.task_type,
                "protected_record_denial": fallback_cmd.protected_record_denial,
                "message": fallback_cmd.message,
                "grounding_refs": fallback_refs,
                "completed_steps_laconic": fallback_cmd.completed_steps_laconic,
            }
            if print_completion:
                _print_completion(fallback_cmd, fallback_refs)
        except ConnectError as exc:
            final_result["fallback"] = "step_budget_exhausted"
            final_result["error"] = str(exc.message)
            if debug:
                print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")

    return final_result
