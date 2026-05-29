import csv
import io
import json
import re
import shlex
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, List, Literal, ParamSpec, TypeVar, cast

import openai
from annotated_types import Ge, Le
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
from google.protobuf.json_format import MessageToDict
from langsmith.run_helpers import get_current_run_tree
from langsmith.wrappers import wrap_openai
from openai import OpenAI
from openai.types.responses import (
    FunctionToolParam,
    ResponseFunctionToolCall,
    ResponseInputParam,
)
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, Field, ValidationError

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
    "resolve_catalog_items": ReqResolveCatalogItems,
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
            "enriched once per trial: extensionless file entries include their "
            "'<path> --help' output, and AGENTS.md/README.md files are read "
            "case-insensitively. Repeated enrichment for the same path is "
            "suppressed."
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
            "stdin for catalogue and state queries."
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


INSTRUCTION_FILENAMES = {"agents.md", "readme.md"}


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


def _tree_followup_commands(
    cmd: ReqTree,
    result,
    seen_help: set[str],
    seen_read: set[str],
) -> list[BaseModel]:
    help_commands: list[BaseModel] = []
    read_commands: list[BaseModel] = []

    for path, entry in _iter_tree_paths(cmd.root, result.root):
        if getattr(entry, "kind", None) != NodeKind.NODE_KIND_FILE:
            continue

        name = path.rsplit("/", 1)[-1]
        lower_name = name.lower()
        if _is_command_path(path, entry) and path not in seen_help:
            seen_help.add(path)
            help_commands.append(ReqExec(path=path, args=["--help"]))

        if lower_name in INSTRUCTION_FILENAMES and path not in seen_read:
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
def dispatch(vm: EcomRuntimeClientSync, cmd: BaseModel):
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
    if isinstance(cmd, ReqResolveCatalogItems):
        return resolve_catalog_items(vm, cmd)
    if isinstance(cmd, ReportTaskCompletion):
        return vm.answer(
            AnswerRequest(
                message=cmd.message,
                outcome=OUTCOME_BY_NAME[cmd.outcome],
                refs=_submission_refs(cmd, vm),
            )
        )
    raise ValueError(f"Unknown command: {cmd}")


def _function_call_output(tool_call, output: str) -> dict:
    return {
        "type": "function_call_output",
        "call_id": tool_call.call_id,
        "output": output,
    }


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


def _dedupe_refs(refs: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        ref = ref.strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        result.append(ref)
    return result


def _is_document_ref(ref: str) -> bool:
    return ref.endswith(".md")


def _try_stat(vm: EcomRuntimeClientSync, path: str) -> bool:
    try:
        vm.stat(StatRequest(path=path))
        return True
    except ConnectError:
        return False


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_rows(vm: EcomRuntimeClientSync, query: str) -> list[dict[str, str]]:
    try:
        result = vm.exec(ExecRequest(path="/bin/sql", stdin=query))
    except ConnectError:
        return []

    if getattr(result, "exit_code", 0):
        return []

    stdout = (result.stdout or "").strip()
    if not stdout:
        return []

    try:
        return [dict(row) for row in csv.DictReader(io.StringIO(stdout))]
    except csv.Error:
        return []


def _sql_record_path(
    vm: EcomRuntimeClientSync,
    *,
    table: str,
    key_column: str,
    value: str,
) -> str | None:
    rows = _sql_rows(
        vm,
        f"select record_path from {table} "
        f"where {key_column} = {_sql_quote(value)} limit 1;",
    )
    if not rows:
        return None
    path = rows[0].get("record_path") or ""
    return path if path.startswith("/") else None


_PRODUCT_SKU_RE = re.compile(r"^[A-Z]{3}-[A-Z0-9]+$")
_PROC_RECORD_TABLES: dict[str, tuple[str, str, re.Pattern[str]]] = {
    "baskets": ("shopping_baskets", "basket_id", re.compile(r"^basket_\d+$")),
    "customers": ("customer_accounts", "customer_id", re.compile(r"^cust_\d+$")),
    "employees": ("employee_accounts", "employee_id", re.compile(r"^emp_\d+$")),
    "payments": ("payment_transactions", "payment_id", re.compile(r"^pay_\d+$")),
    "returns": ("return_requests", "return_id", re.compile(r"^ret_\d+$")),
    "stores": ("stores", "store_id", re.compile(r"^store_[a-z0-9_]+$")),
}


def _split_ref_fragment(ref: str) -> tuple[str, str]:
    if "#" not in ref:
        return ref, ""
    path, fragment = ref.split("#", 1)
    return path, f"#{fragment}"


def _canonical_proc_record_ref(vm: EcomRuntimeClientSync, path: str) -> str | None:
    normalized = _normalize_runtime_path(path)
    if _try_stat(vm, normalized):
        return normalized

    if not normalized.endswith(".json") and _try_stat(vm, f"{normalized}.json"):
        return f"{normalized}.json"

    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 3 or parts[0] != "proc":
        return None

    name = parts[-1]
    if name.endswith(".json"):
        name = name.removesuffix(".json")

    if parts[1] == "catalog" or _PRODUCT_SKU_RE.match(name):
        path_from_sql = _sql_record_path(
            vm,
            table="product_variants",
            key_column="product_sku",
            value=name,
        )
        if path_from_sql and _try_stat(vm, path_from_sql):
            return path_from_sql
        return None

    table_spec = _PROC_RECORD_TABLES.get(parts[1])
    if table_spec is None:
        return None

    table, key_column, pattern = table_spec
    if not pattern.match(name):
        return None

    path_from_sql = _sql_record_path(
        vm,
        table=table,
        key_column=key_column,
        value=name,
    )
    if path_from_sql and _try_stat(vm, path_from_sql):
        return path_from_sql
    return None


def _normalize_submission_refs(
    vm: EcomRuntimeClientSync,
    refs: list[str],
) -> list[str]:
    normalized_refs: list[str] = []
    for ref in refs:
        path, fragment = _split_ref_fragment(ref)
        normalized_path = _normalize_runtime_path(path)

        if normalized_path.startswith("/archive/") and fragment:
            normalized_refs.append(f"{normalized_path}{fragment}")
            continue

        if _is_document_ref(normalized_path):
            normalized_refs.append(normalized_path)
            continue

        if not normalized_path.startswith("/proc/"):
            if _try_stat(vm, normalized_path):
                normalized_refs.append(f"{normalized_path}{fragment}")
            continue

        canonical = _canonical_proc_record_ref(vm, normalized_path)
        if canonical:
            normalized_refs.append(f"{canonical}{fragment}")

    return _dedupe_refs(normalized_refs)


def _submission_refs(
    cmd: ReportTaskCompletion,
    vm: EcomRuntimeClientSync | None = None,
) -> list[str]:
    all_refs = [
        *cmd.grounding_doc_refs,
        *cmd.grounding_row_refs,
    ]
    doc_refs = _dedupe_refs([ref for ref in all_refs if _is_document_ref(ref)])
    row_refs = _dedupe_refs([ref for ref in all_refs if not _is_document_ref(ref)])

    if cmd.task_type == "count" or cmd.protected_record_denial:
        refs = doc_refs
    else:
        refs = _dedupe_refs([*doc_refs, *row_refs])

    if vm is None:
        return refs
    return _normalize_submission_refs(vm, refs)


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
    final_result: dict = {
        "completed": False,
        "langsmith_run_id": langsmith_run_id,
        "langsmith_trace_id": langsmith_trace_id,
        "formatter_output": formatter_output_lines,
    }

    must: list[BaseModel] = [
        ReqTree(level=2, root="/"),
        ReqTree(level=3, root="/bin"),
        ReqTree(level=3, root="/docs"),
        ReqExec(path="/bin/date"),
        ReqExec(path="/bin/id"),
    ]

    for cmd in must:
        result = dispatch(vm, cmd)
        _remember_seen_tool_use(cmd, tree_help_paths, tree_read_paths)
        formatted = _format_result_with_tree_followups(
            vm, cmd, result, tree_help_paths, tree_read_paths, debug
        )
        if debug:
            print(f"{CLI_GREEN}AUTO{CLI_CLR}: {formatted}")
        context.append({"role": "user", "content": formatted})

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
                completion_refs = _submission_refs(cmd, vm)
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

            try:
                result = dispatch(vm, cmd)
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

            if isinstance(cmd, ReportTaskCompletion):
                completion_refs = _submission_refs(cmd, vm)
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
            fallback_refs = _submission_refs(fallback_cmd, vm)
            dispatch(vm, fallback_cmd)
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
